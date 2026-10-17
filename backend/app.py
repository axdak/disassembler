#!/usr/bin/env python3
"""FastAPI 后端：项目 CRUD、启动运行、状态、章节、产物预览、日志、设置、队列 + WebSocket 事件流。

鉴权：受保护接口需 Authorization: Bearer <token> 或 X-Session-Token；WebSocket 用 ?token=<token>。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

# runner 导入时会把 engine/scripts 加入 sys.path
from runner import QueueRunner, RunManager
from db import Database
from hub import BroadcastHub
from models import (
    AppendApply, AppendPreview, FromBook, Project, ProjectCreate, QueueAdd, RecoverSnapshot,
    Run, RunConfig, RunRangeUpdate, RunStart, SettingsUpdate, StageEvent, Status, TaskCreate, WorkbenchTask,
    MAX_TASK_CONCURRENCY,
)
from orchestrator import Orchestrator  # noqa: F401
from pipeline_state import PipelineState
from run_pipeline import run_cmd, script_path
from task_adapters import (
    AnalysisAdapter, AppendChaptersTaskAdapter, ChapterAnalysisRerunAdapter,
    ChapterDeltaRerunAdapter, ChapterStructureAdapter, ChapterVisualAdapter,
    ExportAdapter, FinalizeAdapter, FormatSynopsisAdapter, GovernanceTaskAdapter,
    MigrateChronologyAdapter, NormalizeAdapter, RecoverStoryAdapter,
    RepairAdapter, list_recover_snapshots,
)
from workbench import WorkbenchTaskManager, WriterCoordinator
from artifact_status import (
    ArtifactStatusResponse,
    chapter_cards,
    collect_artifact_status,
    stale_task_count,
    task_summary,
)
from revisions import project_revisions
from storyer_export import export_storyer_document
from worker_admission import worker_admission

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


# worker 日志文件名规范：新格式带微秒；兼容读取旧的秒级日志名。
_LOG_NAME_RE = re.compile(
    r"^(.+?)(?:_ch(\d{3}))?_(\d{8}_\d{6}(?:_\d{6})?)\.txt$"
)

# 全书分析任务名（orchestrator.FULLBOOK_TASKS 的镜像，避免跨模块耦合）
_FULLBOOK_TASK_TYPES = {
    "summary", "characters", "plot", "style", "worldview", "settings", "plotlines",
    "outline", "detailed_outline", "narrative_structure", "report",
}


def _parse_log_name(name: str) -> tuple[str, int]:
    """解析 worker 日志文件名 -> (task_type, seq)。无法识别时 task_type 为空、seq 为 0。"""
    m = _LOG_NAME_RE.match(name)
    if not m:
        return "", 0
    return m.group(1), int(m.group(2) or 0)


def _stage_for_task_type(task_type: str) -> str:
    """worker 任务类型 -> 流水线阶段（中文，用于日志按阶段分组）。"""
    if task_type in ("analysis", "analysis_regenerate"):
        return "章节分析"
    if task_type in ("delta", "repair_chapter"):
        return "章节Delta"
    if task_type == "chapter_structure":
        return "章节结构"
    if task_type in ("visual_assets", "visual_assets_aggregate"):
        return "视觉资产"
    if task_type in ("final_draft", "repair_final"):
        return "最终整理"
    if task_type == "audit_correction":
        return "治理"
    if task_type.endswith("_reduce") or task_type in _FULLBOOK_TASK_TYPES:
        return "全书分析"
    return "其他"


def create_app(
    db_path: str,
    token: str,
    orchestrator_factory=Orchestrator,
    task_adapters: Optional[dict] = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        application.state.queue.stop()
        application.state.workbench.shutdown()

    app = FastAPI(
        title="Novel Disassembler Desktop Backend",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.db = Database(db_path)
    app.state.hub = BroadcastHub()
    app.state.writer_coordinator = WriterCoordinator()
    app.state.manager = RunManager(
        app.state.db, app.state.hub, orchestrator_factory, app.state.writer_coordinator
    )
    app.state.queue = QueueRunner(app.state.db, app.state.manager)
    app.state.token = token
    # 清理上一会话残留的 running 运行（崩溃/关闭未正常结束），解锁 UI 以便续跑
    for run_id, project_id in app.state.db.mark_running_runs_interrupted():
        app.state.db.add_event(run_id, project_id, "run_status", detail="interrupted")
    app.state.db.mark_running_tasks_interrupted()
    # 默认适配器集合：调用方未提供时，注册全部生产用 workbench 适配器。
    if task_adapters is None:
        append_adapter = AppendChaptersTaskAdapter()
        governance_adapter = GovernanceTaskAdapter()
        default_adapters: dict = {
            "analysis": AnalysisAdapter(),
            "chapter_visual": ChapterVisualAdapter(),
            "chapter_structure": ChapterStructureAdapter(),
            "chapter_analysis_rerun": ChapterAnalysisRerunAdapter(),
            "chapter_delta_rerun": ChapterDeltaRerunAdapter(),
            "export": ExportAdapter(),
            "governance": governance_adapter,
            "append_chapters": append_adapter,
            "recover_story": RecoverStoryAdapter(),
            "migrate_chronology": MigrateChronologyAdapter(),
            "finalize": FinalizeAdapter(),
            "normalize": NormalizeAdapter(),
            "repair": RepairAdapter(),
            "format_synopsis": FormatSynopsisAdapter(),
        }
    else:
        default_adapters = dict(task_adapters)
    app.state.workbench = WorkbenchTaskManager(
        app.state.db, app.state.hub, default_adapters, app.state.manager
    )
    # Wire the manager reference for adapters that create follow-up tasks.
    for adapter in app.state.workbench.adapters.values():
        if isinstance(adapter, AppendChaptersTaskAdapter) and adapter.manager is None:
            adapter.manager = app.state.workbench
    # 未完成的自动运行队列在应用重启后直接续跑，不要求用户再次点击开始。
    if app.state.queue.list():
        app.state.queue.start()

    def resolve_run_config(override: Optional[dict]) -> RunConfig:
        # 存档设置作为默认值，调用方显式传入的字段覆盖（前端通常传空 config，需靠这里注入设置）。
        merged = {k: v for k, v in app.state.db.all_settings().items() if k in RunConfig.model_fields}
        # 局部覆盖与完整配置分开：大窗口下仅修改输出，不能先用默认小窗口误拦截。
        # 历史遗留键可忽略，但新传入的未知键必须交给 RunConfig 拒绝。
        merged.update(override or {})
        try:
            return RunConfig(**merged)
        except ValidationError as exc:
            # 历史版本可能已保存无效配置；转换为可理解的 422，而不是在启动任务时返回 500。
            raise HTTPException(status_code=422, detail=f"已保存的运行设置无效: {exc}") from exc

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

    def _task_or_404(pid: int, task_id: int) -> dict:
        _project_or_404(pid)
        task = app.state.db.get_task(task_id)
        if not task or task["project_id"] != pid:
            raise HTTPException(status_code=404, detail="task not found")
        return task

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
        project_dir = (
            Path(body.project_dir).resolve()
            if body.project_dir
            else (src.parent / f"拆书_{stem}").resolve()
        )
        existing = app.state.db.get_project_by_path(str(project_dir))
        if existing:
            # 重复点击“从书创建”应幂等返回已有项目，不能重新切分并覆盖用户产物。
            return existing
        if project_dir.exists() and any(project_dir.iterdir()):
            raise HTTPException(
                status_code=409,
                detail="目标项目目录已存在且非空；请接管该目录或选择其他目录",
            )
        project_dir.mkdir(parents=True, exist_ok=True)
        rc = run_cmd(
            [sys.executable, str(script_path("run_pipeline.py")), "split", str(project_dir), str(src)],
            timeout=600,
        )
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"split failed rc={rc}")
        return app.state.db.create_project(str(project_dir), stem)

    @app.get("/projects/{pid}", dependencies=[Depends(require_token)])
    def project_status(pid: int):
        project = _project_or_404(pid)
        active = app.state.db.get_active_run(pid)
        state = PipelineState.load(Path(project["path"]))
        events = app.state.db.list_events(pid, limit=50)
        revisions = project_revisions(Path(project["path"]))
        tasks = app.state.workbench.list(pid, revisions=revisions)
        return Status(
            project=Project(**project),
            active_run=Run(**active) if active else None,
            current_stage=state.current_stage,
            completed=state.completed,
            last_error=state.last_error or None,
            recent_events=[StageEvent(**e) for e in events],
            revisions=revisions,
            task_summary=task_summary(tasks),
            stale_task_count=stale_task_count(Path(project["path"]), tasks, revisions=revisions),
        )

    @app.put("/projects/{pid}/run-range", dependencies=[Depends(require_token)], response_model=Project)
    def update_project_run_range(pid: int, body: RunRangeUpdate):
        """保存概览页的截止章；重启后仍按项目回填。"""
        project = app.state.db.set_project_run_end_chapter_seq(pid, body.end_chapter_seq)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    @app.delete("/projects/{pid}", dependencies=[Depends(require_token)])
    def delete_project(pid: int):
        _project_or_404(pid)
        if (
            app.state.manager.is_running(pid)
            or app.state.queue.contains(pid)
            or app.state.db.has_nonterminal_tasks(pid)
        ):
            raise HTTPException(status_code=409, detail="项目仍有运行中或排队任务，不能删除")
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

    @app.post(
        "/projects/{pid}/tasks",
        dependencies=[Depends(require_token)],
        response_model=WorkbenchTask,
    )
    def create_task(pid: int, body: TaskCreate):
        _project_or_404(pid)
        try:
            return app.state.workbench.create(pid, body.type, body.params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get(
        "/projects/{pid}/tasks",
        dependencies=[Depends(require_token)],
        response_model=list[WorkbenchTask],
    )
    def list_tasks(pid: int):
        _project_or_404(pid)
        return app.state.workbench.list(pid)

    @app.get(
        "/projects/{pid}/tasks/{task_id}",
        dependencies=[Depends(require_token)],
        response_model=WorkbenchTask,
    )
    def get_task(pid: int, task_id: int):
        _task_or_404(pid, task_id)
        return app.state.workbench.get(task_id)

    @app.post(
        "/projects/{pid}/tasks/{task_id}/cancel",
        dependencies=[Depends(require_token)],
        response_model=WorkbenchTask,
    )
    def cancel_task(pid: int, task_id: int):
        _task_or_404(pid, task_id)
        return app.state.workbench.cancel(task_id)

    @app.post(
        "/projects/{pid}/tasks/{task_id}/retry",
        dependencies=[Depends(require_token)],
        response_model=WorkbenchTask,
    )
    def retry_task(pid: int, task_id: int):
        _task_or_404(pid, task_id)
        try:
            return app.state.workbench.retry(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

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
        revisions = project_revisions(pdir)
        tasks = app.state.workbench.list(pid, revisions=revisions)
        cards_by_seq = {c.seq: c for c in chapter_cards(pdir, tasks, revisions=revisions)}
        chdir = pdir / "章节处理"
        out = []
        for ch in chapters:
            fn = ch.get("filename", "")
            stem = Path(fn).stem if fn else ""
            seq = ch.get("seq")
            card_row = cards_by_seq.get(seq) if seq is not None else None
            cards = card_row.cards if card_row is not None else {}
            out.append({
                "seq": seq,
                "filename": fn,
                "title": ch.get("title", ""),
                "has_analysis": bool(fn) and (chdir / fn).is_file(),
                "has_delta": bool(stem) and (
                    (chdir / "Delta" / f"{stem}.json").is_file() or (chdir / f"{stem}.json").is_file()
                ),
                "analysis": cards.get("analysis").model_dump() if cards.get("analysis") else None,
                "delta": cards.get("delta").model_dump() if cards.get("delta") else None,
                "visual": cards.get("visual").model_dump() if cards.get("visual") else None,
                "structure": cards.get("structure").model_dump() if cards.get("structure") else None,
                "stale": card_row.stale if card_row is not None else False,
                "latest_task": card_row.latest_task if card_row is not None else None,
                "error": card_row.error if card_row is not None else None,
            })
        return out

    @app.post("/projects/{pid}/export/storyer", dependencies=[Depends(require_token)])
    def project_storyer_export(pid: int):
        """按当前最终结构和章节产物生成可由故事家一次导入的完整 JSON。"""
        project = _project_or_404(pid)
        try:
            return export_storyer_document(Path(project["path"]))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/projects/{pid}/artifacts/status",
        dependencies=[Depends(require_token)],
        response_model=ArtifactStatusResponse,
    )
    def project_artifact_status(pid: int):
        project = _project_or_404(pid)
        pdir = Path(project["path"])
        revisions = project_revisions(pdir)
        tasks = app.state.workbench.list(pid, revisions=revisions)
        return collect_artifact_status(pdir, tasks, revisions=revisions)

    @app.get(
        "/projects/{pid}/recover/snapshots",
        dependencies=[Depends(require_token)],
        response_model=list[RecoverSnapshot],
    )
    def project_recover_snapshots(pid: int):
        """只读预览：列出 story_after_chNNN 快照及其可重放到的章节序号。"""
        project = _project_or_404(pid)
        return list_recover_snapshots(Path(project["path"]))

    @app.post(
        "/projects/{pid}/chapters/append-preview",
        dependencies=[Depends(require_token)],
    )
    def chapters_append_preview(pid: int, body: AppendPreview):
        """只读预览：切分新原文并比对旧索引，返回 AppendPlan，不写盘。"""
        project = _project_or_404(pid)
        pdir = Path(project["path"])
        src = Path(body.source_file).resolve()
        if not src.is_file():
            raise HTTPException(status_code=400, detail="source file not found")
        split_dir = pdir / "原文拆解"
        from split_chapters_append import plan_append
        try:
            plan = plan_append(
                str(src), str(split_dir),
                pattern=body.pattern, increment_only=body.increment_only,
            )
        except SystemExit as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return plan

    @app.post(
        "/projects/{pid}/chapters/apply",
        dependencies=[Depends(require_token)],
        response_model=WorkbenchTask,
    )
    def chapters_apply(pid: int, body: AppendApply):
        """把预览出的 plan 排入 append_chapters 写者任务。"""
        _project_or_404(pid)
        plan = body.plan or {}
        if plan.get("conflict"):
            raise HTTPException(
                status_code=422,
                detail=f"plan has a prefix conflict: {plan['conflict'].get('message', '')}",
            )
        if not plan.get("appended"):
            raise HTTPException(status_code=422, detail="plan has no new chapters to append")
        try:
            return app.state.workbench.create(
                pid, "append_chapters", {"plan": plan}
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

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
            task_type, seq = _parse_log_name(f.name)
            out.append({
                "name": f.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "task_type": task_type,
                "seq": seq,
                "stage": _stage_for_task_type(task_type),
            })
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

    @app.get("/settings/concurrency", dependencies=[Depends(require_token)])
    def get_concurrency_policy():
        # Runtime metadata is read-only and separate from persisted task settings.
        return {**worker_admission.configuration(), "max_task_concurrency": MAX_TASK_CONCURRENCY}

    @app.put("/settings", dependencies=[Depends(require_token)])
    def update_settings(body: SettingsUpdate):
        # 整组先验收，任何一项无效都不得部分落库；只持久化本次提交字段。
        def validate(values):
            # Ignore obsolete stored keys, while still rejecting unknown submitted keys.
            known = {k: v for k, v in values.items() if k in RunConfig.model_fields}
            return RunConfig(**{**known, **body.settings}).model_dump()
        try:
            return app.state.db.patch_settings(body.settings, validate)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- 单任务队列 ----

    @app.post("/queue", dependencies=[Depends(require_token)])
    def queue_add(body: QueueAdd):
        _project_or_404(body.project_id)
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
