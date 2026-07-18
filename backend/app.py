#!/usr/bin/env python3
"""FastAPI 后端：项目 CRUD、启动运行、状态、章节、产物预览、日志、设置、队列 + WebSocket 事件流。

鉴权：受保护接口需 Authorization: Bearer <token> 或 X-Session-Token；WebSocket 用 ?token=<token>。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# runner 导入时会把 engine/scripts 加入 sys.path
from runner import QueueRunner, RunManager
from db import Database
from hub import BroadcastHub
from models import FromBook, Project, ProjectCreate, QueueAdd, Run, RunConfig, RunStart, SettingsUpdate, StageEvent, Status
from orchestrator import Orchestrator  # noqa: F401
from pipeline_state import PipelineState
from run_pipeline import run_cmd, script_path

ARTIFACT_MAX_CHARS = 200_000


def _safe_project_path(project_dir: Path, rel: str) -> Optional[Path]:
    """把 rel 解析到 project_dir 下，拒绝目录穿越。"""
    base = project_dir.resolve()
    try:
        target = (base / rel).resolve()
    except Exception:
        return None
    if target != base and base not in target.parents:
        return None
    return target


def create_app(db_path: str, token: str, orchestrator_factory=Orchestrator) -> FastAPI:
    app = FastAPI(title="Novel Disassembler Desktop Backend", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.db = Database(db_path)
    app.state.hub = BroadcastHub()
    app.state.manager = RunManager(app.state.db, app.state.hub, orchestrator_factory)
    app.state.queue = QueueRunner(app.state.db, app.state.manager)
    app.state.token = token
    # 清理上一会话残留的 running 运行（崩溃/关闭未正常结束），解锁 UI 以便续跑
    for run_id, project_id in app.state.db.mark_running_runs_interrupted():
        app.state.db.add_event(run_id, project_id, "run_status", detail="interrupted")

    def resolve_run_config(override: Optional[RunConfig]) -> RunConfig:
        # 存档设置作为默认值，调用方覆盖（前端通常传空 config，需靠这里注入设置）。
        merged: dict = {**app.state.db.all_settings()}
        merged.update((override or RunStart().config).model_dump())
        return RunConfig(**{k: v for k, v in merged.items() if k in RunConfig.model_fields})

    def require_token(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        else:
            provided = request.headers.get("x-session-token", "")
        if provided != app.state.token:
            raise HTTPException(status_code=401, detail="invalid session token")

    def _project_or_404(pid: int) -> dict:
        project = app.state.db.get_project(pid)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/projects", dependencies=[Depends(require_token)])
    def list_projects():
        return app.state.db.list_projects()

    @app.post("/projects", dependencies=[Depends(require_token)])
    def create_project(body: ProjectCreate):
        path = str(Path(body.path).resolve())
        existing = app.state.db.get_project_by_path(path)
        if existing:
            return existing
        return app.state.db.create_project(path, body.name or "")

    @app.post("/projects/from-book", dependencies=[Depends(require_token)])
    def create_from_book(body: FromBook):
        src = Path(body.source_file).resolve()
        if not src.is_file():
            raise HTTPException(status_code=400, detail="source file not found")
        stem = src.stem
        project_dir = Path(body.project_dir).resolve() if body.project_dir else src.parent / f"拆书_{stem}"
        project_dir.mkdir(parents=True, exist_ok=True)
        rc = run_cmd(
            [sys.executable, str(script_path("run_pipeline.py")), "split", str(project_dir), str(src)],
            timeout=600,
        )
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"split failed rc={rc}")
        existing = app.state.db.get_project_by_path(str(project_dir))
        return existing or app.state.db.create_project(str(project_dir), stem)

    @app.get("/projects/{pid}", dependencies=[Depends(require_token)])
    def project_status(pid: int):
        project = _project_or_404(pid)
        active = app.state.db.get_active_run(pid)
        state = PipelineState.load(Path(project["path"]))
        events = app.state.db.list_events(pid, limit=50)
        return Status(
            project=Project(**project),
            active_run=Run(**active) if active else None,
            current_stage=state.current_stage,
            completed=state.completed,
            last_error=state.last_error or None,
            recent_events=[StageEvent(**e) for e in events],
        )

    @app.delete("/projects/{pid}", dependencies=[Depends(require_token)])
    def delete_project(pid: int):
        _project_or_404(pid)
        app.state.db.delete_project(pid)
        return {"ok": True}

    @app.post("/projects/{pid}/run", dependencies=[Depends(require_token)])
    def start_run(pid: int, body: RunStart):
        # 存档设置作为默认值，请求体覆盖（前端 run() 传空 config，需靠这里注入设置）。
        config = resolve_run_config(body.config)
        try:
            run_id = app.state.manager.start(pid, config)
        except KeyError:
            raise HTTPException(status_code=404, detail="project not found")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"run_id": run_id}

    @app.post("/projects/{pid}/cancel", dependencies=[Depends(require_token)])
    def cancel_run(pid: int):
        return {"cancelled": app.state.manager.cancel(pid)}

    @app.get("/projects/{pid}/events", dependencies=[Depends(require_token)])
    def project_events(pid: int, limit: int = Query(200, ge=1, le=2000)):
        _project_or_404(pid)
        return app.state.db.list_events(pid, limit=limit)

    @app.get("/projects/{pid}/chapters", dependencies=[Depends(require_token)])
    def project_chapters(pid: int):
        project = _project_or_404(pid)
        pdir = Path(project["path"])
        index = pdir / "原文拆解" / "_索引.json"
        if not index.is_file():
            return []
        try:
            chapters = json.loads(index.read_text(encoding="utf-8")).get("chapters", [])
        except (json.JSONDecodeError, OSError):
            return []
        chdir = pdir / "章节处理"
        out = []
        for ch in chapters:
            fn = ch.get("filename", "")
            stem = Path(fn).stem if fn else ""
            out.append({
                "seq": ch.get("seq"),
                "filename": fn,
                "title": ch.get("title", ""),
                "has_analysis": bool(fn) and (chdir / fn).is_file(),
                "has_delta": bool(stem) and (chdir / f"{stem}.json").is_file(),
            })
        return out

    @app.get("/projects/{pid}/artifact", dependencies=[Depends(require_token)])
    def project_artifact(pid: int, path: str = Query(...)):
        project = _project_or_404(pid)
        target = _safe_project_path(Path(project["path"]), path)
        if target is None or not target.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        size = target.stat().st_size
        content = target.read_text(encoding="utf-8", errors="replace")[:ARTIFACT_MAX_CHARS]
        return {"path": path, "size": size, "truncated": size > ARTIFACT_MAX_CHARS, "content": content}

    @app.get("/projects/{pid}/logs", dependencies=[Depends(require_token)])
    def project_logs_list(pid: int):
        project = _project_or_404(pid)
        ldir = Path(project["path"]) / "质量治理" / "worker日志"
        if not ldir.is_dir():
            return []
        out = []
        for f in sorted(ldir.glob("*.txt"), reverse=True):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out

    @app.get("/projects/{pid}/logs/{name}", dependencies=[Depends(require_token)])
    def project_log(pid: int, name: str):
        project = _project_or_404(pid)
        target = _safe_project_path(Path(project["path"]), f"质量治理/worker日志/{name}")
        if target is None or not target.is_file():
            raise HTTPException(status_code=404, detail="log not found")
        content = target.read_text(encoding="utf-8", errors="replace")[:ARTIFACT_MAX_CHARS]
        return {"name": name, "content": content}

    @app.get("/settings", dependencies=[Depends(require_token)])
    def get_settings():
        return app.state.db.all_settings()

    @app.put("/settings", dependencies=[Depends(require_token)])
    def update_settings(body: SettingsUpdate):
        for k, v in body.settings.items():
            app.state.db.set_setting(k, v)
        return app.state.db.all_settings()

    # ---- 单任务队列 ----

    @app.post("/queue", dependencies=[Depends(require_token)])
    def queue_add(body: QueueAdd):
        pos = app.state.queue.add(body.project_id, resolve_run_config(body.config))
        return {"position": pos}

    @app.get("/queue", dependencies=[Depends(require_token)])
    def queue_list():
        return app.state.queue.list()

    @app.delete("/queue/{position}", dependencies=[Depends(require_token)])
    def queue_remove(position: int):
        return {"removed": app.state.queue.remove(position)}

    @app.post("/queue/start", dependencies=[Depends(require_token)])
    def queue_start():
        return {"started": app.state.queue.start()}

    @app.post("/queue/stop", dependencies=[Depends(require_token)])
    def queue_stop():
        app.state.queue.stop()
        return {"stopped": True}

    # ---- WebSocket 事件流 ----

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, token: str = Query("")):
        if token != app.state.token:
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event_dict: dict):
            loop.call_soon_threadsafe(queue.put_nowait, event_dict)

        app.state.hub.subscribe(on_event)

        async def wait_disconnect():
            try:
                while True:
                    await ws.receive()
            except WebSocketDisconnect:
                return

        disconnect_task = asyncio.create_task(wait_disconnect())
        try:
            while True:
                get_task = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {get_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    get_task.cancel()
                    break
                if get_task in done:
                    try:
                        await ws.send_json(get_task.result())
                    except WebSocketDisconnect:
                        break
        finally:
            disconnect_task.cancel()
            app.state.hub.unsubscribe(on_event)

    return app
