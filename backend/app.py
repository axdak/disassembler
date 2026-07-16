#!/usr/bin/env python3
"""FastAPI 后端：项目 CRUD、启动运行、状态、事件、设置 + WebSocket 事件流。

鉴权：所有受保护接口需 Authorization: Bearer <token> 或 X-Session-Token；
WebSocket 用 ?token=<token>。token 由 main.py 启动时随机生成。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# runner 导入时会把 engine/scripts 加入 sys.path
from runner import RunManager
from db import Database
from hub import BroadcastHub
from models import Project, ProjectCreate, Run, RunStart, SettingsUpdate, StageEvent, Status
from orchestrator import Orchestrator  # noqa: F401
from pipeline_state import PipelineState


def create_app(db_path: str, token: str, orchestrator_factory=Orchestrator) -> FastAPI:
    app = FastAPI(title="Novel Disassembler Desktop Backend", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.db = Database(db_path)
    app.state.hub = BroadcastHub()
    app.state.manager = RunManager(app.state.db, app.state.hub, orchestrator_factory)
    app.state.token = token

    def require_token(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        else:
            provided = request.headers.get("x-session-token", "")
        if provided != app.state.token:
            raise HTTPException(status_code=401, detail="invalid session token")

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

    @app.get("/projects/{pid}", dependencies=[Depends(require_token)])
    def project_status(pid: int):
        db = app.state.db
        project = db.get_project(pid)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        active = db.get_active_run(pid)
        state = PipelineState.load(Path(project["path"]))
        events = db.list_events(pid, limit=50)
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
        if not app.state.db.get_project(pid):
            raise HTTPException(status_code=404, detail="project not found")
        app.state.db.delete_project(pid)
        return {"ok": True}

    @app.post("/projects/{pid}/run", dependencies=[Depends(require_token)])
    def start_run(pid: int, body: RunStart):
        try:
            run_id = app.state.manager.start(pid, body.config)
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
        return app.state.db.list_events(pid, limit=limit)

    @app.get("/settings", dependencies=[Depends(require_token)])
    def get_settings():
        return app.state.db.all_settings()

    @app.put("/settings", dependencies=[Depends(require_token)])
    def update_settings(body: SettingsUpdate):
        for k, v in body.settings.items():
            app.state.db.set_setting(k, v)
        return app.state.db.all_settings()

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
            # 阻塞直到客户端断开；与 queue.get() 并发，先到先处理
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
