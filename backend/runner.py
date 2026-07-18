#!/usr/bin/env python3
"""编排器封装：后台线程运行 Orchestrator，事件持久化到 SQLite + 广播到 hub。"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# 把 engine/scripts 加入 sys.path 以导入编排器
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))

from events import EventBus, Event  # noqa: E402
from orchestrator import Config, Orchestrator  # noqa: E402

from db import Database  # noqa: E402
from hub import BroadcastHub  # noqa: E402
from models import RunConfig  # noqa: E402
from workbench import WriterCoordinator  # noqa: E402


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def runconfig_to_config(rc: RunConfig) -> Config:
    return Config(
        worker_provider=rc.worker_provider,
        worker_command=rc.worker_command,
        worker_loop=rc.worker_loop,
        worker_retries=rc.worker_retries,
        worker_timeout_seconds=rc.worker_timeout_seconds,
        max_attempts=rc.max_attempts,
        stagnant_limit=rc.stagnant_limit,
        max_repair_rounds=rc.max_repair_rounds,
        analysis_concurrency=rc.analysis_concurrency,
        chapter_mode=rc.chapter_mode,
        fullbook_tasks=tuple(rc.fullbook_tasks),
        export_source=rc.export_source,
    )


class _ActiveRun:
    def __init__(self, run_id: int, project_id: int, thread: threading.Thread, orch: Orchestrator):
        self.run_id = run_id
        self.project_id = project_id
        self.thread = thread
        self.orch = orch


class RunManager:
    def __init__(self, db: Database, hub: BroadcastHub,
                 orchestrator_factory: Callable[..., Orchestrator] = Orchestrator,
                 writer_coordinator: Optional[WriterCoordinator] = None):
        self.db = db
        self.hub = hub
        self.factory = orchestrator_factory
        self._active: dict[int, _ActiveRun] = {}
        self._lock = threading.Lock()
        self.writer_coordinator = writer_coordinator or WriterCoordinator()

    def is_running(self, project_id: int) -> bool:
        with self._lock:
            h = self._active.get(project_id)
            return h is not None and h.thread.is_alive()

    def active_run_id(self, project_id: int) -> Optional[int]:
        with self._lock:
            h = self._active.get(project_id)
            return h.run_id if h else None

    def start(self, project_id: int, run_config: RunConfig) -> int:
        if self.is_running(project_id):
            raise RuntimeError(f"project {project_id} already running")
        writer_owner = object()
        if not self.writer_coordinator.try_acquire(project_id, writer_owner):
            raise RuntimeError(f"project {project_id} writer active")
        project = self.db.get_project(project_id)
        if not project:
            self.writer_coordinator.release(project_id, writer_owner)
            raise KeyError(f"project {project_id} not found")
        run_id = None
        try:
            config = runconfig_to_config(run_config)
            run = self.db.create_run(project_id, asdict(config))
            run_id = run["id"]

            bus = EventBus()
            orch = self.factory(Path(project["path"]), config, bus)
        except Exception:
            try:
                if run_id is not None:
                    self.db.update_run(
                        run_id, status="blocked", rc=-1, ended_at=_now_iso()
                    )
            finally:
                self.writer_coordinator.release(project_id, writer_owner)
            raise

        def on_event(ev: Event) -> None:
            edict = {
                "run_id": run_id, "project_id": project_id,
                "type": ev.type, "stage": ev.stage or "", "detail": ev.detail or "",
                "data": ev.data or {}, "ts": ev.ts,
            }
            self.db.add_event(run_id, project_id, ev.type, ev.stage or "",
                              ev.detail or "", ev.data or {}, str(ev.ts))
            self.hub.broadcast(edict)

        bus.subscribe(on_event)

        def worker() -> None:
            try:
                try:
                    rc = orch.run()
                except Exception as exc:  # noqa: BLE001
                    rc = -1
                    try:
                        self.db.add_event(
                            run_id, project_id, "pipeline_error", detail=str(exc)
                        )
                    except Exception:
                        pass
                    self.hub.broadcast({
                        "run_id": run_id, "project_id": project_id,
                        "type": "pipeline_error", "detail": str(exc), "ts": time.time(),
                    })
                status = {0: "done", 2: "cancelled"}.get(rc, "blocked")
                try:
                    self.db.add_event(
                        run_id, project_id, "run_status", detail=status
                    )
                except Exception:
                    pass
                self.hub.broadcast({"run_id": run_id, "project_id": project_id,
                                    "type": "run_status", "detail": status, "ts": time.time()})
                self.db.update_run(run_id, status=status, rc=rc, ended_at=_now_iso())
            finally:
                with self._lock:
                    self._active.pop(project_id, None)
                self.writer_coordinator.release(project_id, writer_owner)

        t = threading.Thread(target=worker, daemon=True, name=f"run-{run_id}")
        with self._lock:
            self._active[project_id] = _ActiveRun(run_id, project_id, t, orch)
        try:
            t.start()
        except Exception:
            try:
                with self._lock:
                    active = self._active.get(project_id)
                    if active is not None and active.thread is t:
                        self._active.pop(project_id, None)
                self.db.update_run(
                    run_id, status="blocked", rc=-1, ended_at=_now_iso()
                )
            finally:
                self.writer_coordinator.release(project_id, writer_owner)
            raise
        return run_id

    def cancel(self, project_id: int) -> bool:
        with self._lock:
            h = self._active.get(project_id)
        if h is None or not h.thread.is_alive():
            return False
        h.orch.cancel()
        return True


class QueueRunner:
    """单任务队列：顺序运行多个项目；当前项目完成/阻塞/取消后继续下一个。"""

    def __init__(self, db: Database, manager: "RunManager"):
        self.db = db
        self.manager = manager
        self._queue: list[tuple[int, RunConfig]] = []
        self._lock = threading.RLock()  # 可重入：start() 持锁时调用 is_running()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def add(self, project_id: int, config: RunConfig) -> int:
        with self._lock:
            self._queue.append((project_id, config))
            return len(self._queue)

    def list(self) -> list:
        with self._lock:
            return [{"position": i + 1, "project_id": pid} for i, (pid, _) in enumerate(self._queue)]

    def remove(self, position: int) -> bool:
        with self._lock:
            if 1 <= position <= len(self._queue):
                self._queue.pop(position - 1)
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="queue")
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._queue:
                    break
                project_id, config = self._queue.pop(0)
            if self.manager.is_running(project_id):
                continue
            try:
                run_id = self.manager.start(project_id, config)
            except KeyError:
                continue
            except RuntimeError:
                # A workbench writer currently owns the project.  Preserve the
                # legacy queue row and retry after its admission is released.
                with self._lock:
                    self._queue.insert(0, (project_id, config))
                if self._stop.wait(0.05):
                    break
                continue
            while not self._stop.is_set():
                run = self.db.get_run(run_id)
                if not run or run["status"] != "running":
                    break
                time.sleep(1.0)
