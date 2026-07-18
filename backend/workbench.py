"""Persistent scheduler for independent workbench tasks."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from db import Database
from hub import BroadcastHub
from revisions import project_revisions, task_is_stale


WRITER_TASK_TYPES = frozenset({
    "append_chapters",
    "chapter_analysis_rerun",
    "chapter_delta_rerun",
    "governance",
})
TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WriterCoordinator:
    """Atomic, process-local admission for project fact writers."""

    def __init__(self):
        self._lock = threading.RLock()
        self._owners: dict[int, object] = {}
        self._readers: dict[int, set[object]] = {}

    def try_acquire(self, project_id: int, owner: object) -> bool:
        with self._lock:
            if project_id in self._owners or self._readers.get(project_id):
                return False
            self._owners[project_id] = owner
            return True

    def release(self, project_id: int, owner: object) -> None:
        with self._lock:
            if self._owners.get(project_id) is owner:
                self._owners.pop(project_id, None)

    def has_writer(self, project_id: int) -> bool:
        with self._lock:
            return project_id in self._owners

    def try_acquire_reader(self, project_id: int, owner: object) -> bool:
        with self._lock:
            if project_id in self._owners:
                return False
            self._readers.setdefault(project_id, set()).add(owner)
            return True

    def release_reader(self, project_id: int, owner: object) -> None:
        with self._lock:
            readers = self._readers.get(project_id)
            if readers is None:
                return
            readers.discard(owner)
            if not readers:
                self._readers.pop(project_id, None)

    def admit_reader(self, project_id: int, prepare: Callable[[], bool]) -> bool:
        """Run the reader's fixed preparation admission while writers are excluded."""
        owner = object()
        if not self.try_acquire_reader(project_id, owner):
            return False
        try:
            return prepare()
        finally:
            self.release_reader(project_id, owner)


@dataclass
class TaskContext:
    task_id: int
    project_id: int
    project_path: Path
    params: dict
    cancellation_event: threading.Event
    _progress_callback: Callable[[int, str], None]

    @property
    def id(self) -> int:
        return self.task_id

    def progress(self, percent: int, detail: str = "") -> None:
        self._progress_callback(percent, detail)


class TaskAdapter(Protocol):
    def run(self, context: TaskContext, prepared_input: object = None) -> dict: ...


@dataclass
class _ActiveTask:
    task_id: int
    project_id: int
    thread: threading.Thread
    adapter: TaskAdapter
    cancellation_event: threading.Event
    writer_owner: object | None
    reader_owner: object | None


class WorkbenchTaskManager:
    def __init__(
        self,
        db: Database,
        hub: BroadcastHub,
        adapters: dict[str, object],
        run_manager=None,
        *,
        poll_interval: float = 0.05,
    ):
        self.db = db
        self.hub = hub
        self.adapters = dict(adapters)
        self.run_manager = run_manager
        self.writer_coordinator = (
            getattr(run_manager, "writer_coordinator", None) or WriterCoordinator()
        )
        self.poll_interval = poll_interval
        self._active: dict[int, _ActiveTask] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._scheduler = threading.Thread(
            target=self._schedule_loop, daemon=True, name="workbench-scheduler"
        )
        self._scheduler.start()

    def create(self, project_id: int, task_type: str, params: dict) -> dict:
        if self._shutdown.is_set():
            raise RuntimeError("workbench manager is shut down")
        project = self.db.get_project(project_id)
        if not project:
            raise KeyError(f"project {project_id} not found")
        if task_type not in self.adapters:
            raise ValueError(f"no adapter registered for task type: {task_type}")
        if not isinstance(params, dict):
            raise ValueError("task params must be an object")
        task = self.db.create_task(
            project_id, task_type, dict(params), project_revisions(Path(project["path"]))
        )
        self._emit(task, "task_queued")
        self._wake.set()
        return self._serialize(task)

    def list(self, project_id: int, *, revisions: dict | None = None) -> list[dict]:
        return [self._serialize(task, revisions=revisions) for task in self.db.list_tasks(project_id)]

    def get(self, task_id: int) -> dict | None:
        task = self.db.get_task(task_id)
        return self._serialize(task) if task else None

    def cancel(self, task_id: int) -> dict:
        adapter = None
        with self._lock:
            task = self.db.get_task(task_id)
            if not task:
                raise KeyError(f"task {task_id} not found")
            if task["status"] in TERMINAL_TASK_STATUSES:
                return self._serialize(task)
            self.db.request_task_cancel(task_id)
            active = self._active.get(task_id)
            if task["status"] == "queued" and active is None:
                task = self.db.update_task(
                    task_id, status="cancelled", cancel_requested=True, ended_at=_now_iso()
                )
                self._emit(task, "task_cancelled")
                return self._serialize(task)
            if active is not None:
                active.cancellation_event.set()
                adapter = active.adapter
        cancel_method = getattr(adapter, "cancel", None)
        if callable(cancel_method):
            try:
                cancel_method()
            except Exception:
                pass
        self._wake.set()
        return self._serialize(self.db.get_task(task_id))

    def retry(self, task_id: int) -> dict:
        if self._shutdown.is_set():
            raise RuntimeError("workbench manager is shut down")
        original = self.db.get_task(task_id)
        if not original:
            raise KeyError(f"task {task_id} not found")
        if original["status"] not in TERMINAL_TASK_STATUSES:
            raise ValueError("only terminal tasks can be retried")
        project = self.db.get_project(original["project_id"])
        if not project:
            raise KeyError(f"project {original['project_id']} not found")
        task = self.db.create_task(
            original["project_id"],
            original["task_type"],
            original["params"],
            project_revisions(Path(project["path"])),
            retry_of=task_id,
        )
        self._emit(task, "task_queued")
        self._wake.set()
        return self._serialize(task)

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self._wake.set()
        self._scheduler.join(timeout=2.0)
        with self._lock:
            task_ids = list(self._active)
        for task_id in task_ids:
            self.cancel(task_id)
        with self._lock:
            threads = [active.thread for active in self._active.values()]
        for thread in threads:
            thread.join(timeout=2.0)

    def _serialize(self, task: dict, *, revisions: dict | None = None) -> dict:
        project = self.db.get_project(task["project_id"])
        if revisions is not None:
            current = revisions
        else:
            current = project_revisions(Path(project["path"])) if project else {}
        return {**task, "stale": task_is_stale(task, current)}

    def _queued_tasks(self) -> list[dict]:
        return self.db.list_queued_tasks()

    def _schedule_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._admit_queued()
            except Exception:
                # A transient database/query failure must not terminate the
                # persistent scheduler thread.  The next poll retries it.
                pass
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def _admit_queued(self) -> None:
        blocked_projects = set()
        for task in self._queued_tasks():
            if self._shutdown.is_set():
                return
            project_id = task["project_id"]
            if project_id in blocked_projects:
                continue
            if task["cancel_requested"]:
                with self._lock:
                    current = self.db.get_task(task["id"])
                    if current and current["status"] == "queued":
                        current = self.db.update_task(
                            task["id"], status="cancelled", ended_at=_now_iso()
                        )
                        self._emit(current, "task_cancelled")
                continue
            try:
                admitted = self._try_admit(task)
            except Exception as exc:
                self._fail_admission(task, exc)
                continue
            if not admitted:
                blocked_projects.add(project_id)

    def _fail_admission(self, task: dict, exc: Exception) -> None:
        with self._lock:
            self._active.pop(task["id"], None)
            current = self.db.get_task(task["id"])
            if not current or current["status"] not in {"queued", "running"}:
                return
            failed = self.db.update_task(
                task["id"], status="failed", error=str(exc), ended_at=_now_iso()
            )
        self._emit(failed, "task_failed", error=str(exc), result=None)

    def _legacy_running(self, project_id: int) -> bool:
        return bool(self.run_manager and self.run_manager.is_running(project_id))

    def _try_admit(self, task: dict) -> bool:
        project_id = task["project_id"]
        if self._legacy_running(project_id):
            return False
        if task["task_type"] in WRITER_TASK_TYPES:
            owner = object()
            if not self.writer_coordinator.try_acquire(project_id, owner):
                return False
            if self._legacy_running(project_id):
                self.writer_coordinator.release(project_id, owner)
                return False
            try:
                launched = self._launch(task, writer_owner=owner)
            except Exception:
                self.writer_coordinator.release(project_id, owner)
                raise
            if not launched:
                self.writer_coordinator.release(project_id, owner)
            return launched
        reader_owner = object()
        if not self.writer_coordinator.try_acquire_reader(project_id, reader_owner):
            return False
        try:
            launched = self._launch(task, reader_owner=reader_owner)
        except Exception:
            self.writer_coordinator.release_reader(project_id, reader_owner)
            raise
        if not launched:
            self.writer_coordinator.release_reader(project_id, reader_owner)
        return launched

    def _adapter_for(self, task_type: str):
        provider = self.adapters[task_type]
        return provider if hasattr(provider, "run") else provider()

    def _launch(
        self,
        task: dict,
        writer_owner: object | None = None,
        reader_owner: object | None = None,
    ) -> bool:
        with self._lock:
            current = self.db.get_task(task["id"])
            if self._shutdown.is_set() or not current or current["status"] != "queued":
                return False
            adapter = self._adapter_for(task["task_type"])
            cancellation_event = threading.Event()
            thread = threading.Thread(
                target=self._run_task,
                args=(current, adapter, cancellation_event, writer_owner, reader_owner),
                daemon=True,
                name=f"workbench-{task['id']}",
            )
            current = self.db.update_task(
                task["id"], status="running", started_at=_now_iso()
            )
            active = _ActiveTask(
                task["id"], task["project_id"], thread, adapter,
                cancellation_event, writer_owner, reader_owner,
            )
            self._active[task["id"]] = active
            self._emit(current, "task_started")
            thread.start()
            return True

    def _run_task(
        self, task, adapter, cancellation_event, writer_owner, reader_owner
    ) -> None:
        reader_released = False
        try:
            project = self.db.get_project(task["project_id"])
            if not project:
                raise KeyError(f"project {task['project_id']} not found")
            context = TaskContext(
                task_id=task["id"],
                project_id=task["project_id"],
                project_path=Path(project["path"]),
                params=task["params"],
                cancellation_event=cancellation_event,
                _progress_callback=lambda percent, detail: self._progress(
                    task["id"], percent, detail
                ),
            )
            prepare = getattr(adapter, "prepare", None)
            if callable(prepare):
                prepared_input = prepare(context)
                self.writer_coordinator.release_reader(task["project_id"], reader_owner)
                reader_released = True
                self._wake.set()
                result = adapter.run(context, prepared_input)
            else:
                result = adapter.run(context)
            if not isinstance(result, dict):
                raise TypeError("task adapter result must be a dict")
            if cancellation_event.is_set() or self.db.get_task(task["id"])["cancel_requested"]:
                saved = self.db.update_task(
                    task["id"], status="cancelled", result=result, ended_at=_now_iso()
                )
                self._emit(saved, "task_cancelled")
            else:
                saved = self.db.update_task(
                    task["id"], status="done", result=result, ended_at=_now_iso()
                )
                self._emit(saved, "task_done", result=result)
        except Exception as exc:
            if cancellation_event.is_set() or self.db.get_task(task["id"])["cancel_requested"]:
                saved = self.db.update_task(
                    task["id"], status="cancelled", ended_at=_now_iso()
                )
                self._emit(saved, "task_cancelled")
            else:
                evidence = getattr(exc, "result", None)
                saved = self.db.update_task(
                    task["id"], status="failed", result=evidence,
                    error=str(exc), ended_at=_now_iso(),
                )
                self._emit(saved, "task_failed", error=str(exc), result=evidence)
        finally:
            with self._lock:
                self._active.pop(task["id"], None)
            if writer_owner is not None:
                self.writer_coordinator.release(task["project_id"], writer_owner)
            if reader_owner is not None and not reader_released:
                self.writer_coordinator.release_reader(task["project_id"], reader_owner)
            self._wake.set()

    def _progress(self, task_id: int, percent: int, detail: str) -> None:
        task = self.db.get_task(task_id)
        if task and task["status"] == "running":
            self._emit(task, "task_progress", percent=percent, detail=detail)

    def _emit(self, task: dict, event_type: str, **fields) -> None:
        timestamp = time.time()
        payload = {
            "type": event_type,
            "task_id": task["id"],
            "project_id": task["project_id"],
            "ts": timestamp,
            **fields,
        }
        detail = str(fields.get("detail", fields.get("error", "")))
        data = {"task_id": task["id"], "project_id": task["project_id"], **fields}
        self.db.add_event(
            None, task["project_id"], event_type,
            detail=detail, data=data, ts=str(timestamp),
        )
        self.hub.broadcast(payload)
