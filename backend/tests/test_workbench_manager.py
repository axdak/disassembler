import threading
import time

import pytest

from db import Database
from hub import BroadcastHub
from models import RunConfig
from runner import QueueRunner, RunManager
from workbench import WorkbenchTaskManager, WriterCoordinator


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class RunMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = []
        self.active_by_project = {}
        self.max_by_project = {}

    def enter(self, context):
        with self.lock:
            self.started.append(context.task_id)
            count = self.active_by_project.get(context.project_id, 0) + 1
            self.active_by_project[context.project_id] = count
            self.max_by_project[context.project_id] = max(
                count, self.max_by_project.get(context.project_id, 0)
            )

    def leave(self, context):
        with self.lock:
            self.active_by_project[context.project_id] -= 1


class ControlledAdapter:
    def __init__(self, monitor, gates, fail=False, partial=None):
        self.monitor = monitor
        self.gates = gates
        self.fail = fail
        self.partial = partial
        self.cancel_called = threading.Event()

    def run(self, context):
        self.monitor.enter(context)
        try:
            context.progress(25, "package prepared")
            gate = self.gates.setdefault(context.task_id, threading.Event())
            while not gate.wait(0.01):
                if context.cancellation_event.is_set():
                    return {"cancelled_at": "worker"}
            if self.fail:
                error = RuntimeError("adapter exploded")
                error.result = self.partial
                raise error
            return {"task": context.task_id, "path": str(context.project_path)}
        finally:
            self.monitor.leave(context)

    def cancel(self):
        self.cancel_called.set()


class AdapterFactory:
    def __init__(self, monitor, gates, **adapter_options):
        self.monitor = monitor
        self.gates = gates
        self.adapter_options = adapter_options
        self.instances = []

    def __call__(self):
        adapter = ControlledAdapter(self.monitor, self.gates, **self.adapter_options)
        self.instances.append(adapter)
        return adapter


@pytest.fixture
def manager_env(tmp_path):
    db = Database(tmp_path / "tasks.db")
    p1_path = tmp_path / "p1"
    p2_path = tmp_path / "p2"
    p1_path.mkdir()
    p2_path.mkdir()
    p1 = db.create_project(str(p1_path))
    p2 = db.create_project(str(p2_path))
    hub = BroadcastHub()
    monitor = RunMonitor()
    gates = {}
    writer_factory = AdapterFactory(monitor, gates)
    reader_factory = AdapterFactory(monitor, gates)
    manager = WorkbenchTaskManager(
        db,
        hub,
        {"governance": writer_factory, "analysis": reader_factory},
        poll_interval=0.01,
    )
    yield db, p1, p2, hub, monitor, gates, manager, writer_factory, reader_factory
    for gate in gates.values():
        gate.set()
    manager.shutdown()
    db.close()


def test_writer_fifo_per_project_and_parallel_across_projects(manager_env):
    db, p1, p2, _, monitor, gates, manager, _, _ = manager_env
    first = manager.create(p1["id"], "governance", {"order": 1})
    wait_for(lambda: db.get_task(first["id"])["status"] == "running")
    second = manager.create(p1["id"], "governance", {"order": 2})
    other_project = manager.create(p2["id"], "governance", {"order": 1})

    wait_for(lambda: db.get_task(other_project["id"])["status"] == "running")
    assert db.get_task(second["id"])["status"] == "queued"
    assert monitor.max_by_project[p1["id"]] == 1

    gates[first["id"]].set()
    wait_for(lambda: db.get_task(second["id"])["status"] == "running")
    wait_for(lambda: second["id"] in monitor.started)
    assert monitor.started.index(first["id"]) < monitor.started.index(second["id"])

    wait_for(lambda: second["id"] in gates and other_project["id"] in gates)
    gates[second["id"]].set()
    gates[other_project["id"]].set()
    wait_for(lambda: all(db.get_task(tid)["status"] == "done" for tid in (
        first["id"], second["id"], other_project["id"]
    )))


class BlockingOrchestrator:
    instances = []

    def __init__(self, project_path, config, bus):
        self.cancelled = threading.Event()
        self.__class__.instances.append(self)

    def run(self):
        self.cancelled.wait(3)
        return 2

    def cancel(self):
        self.cancelled.set()


class InstantOrchestrator:
    def __init__(self, project_path, config, bus):
        pass

    def run(self):
        return 0

    def cancel(self):
        pass


def test_legacy_run_and_workbench_writer_exclude_each_other_without_losing_queue(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    coordinator = WriterCoordinator()
    run_manager = RunManager(db, hub, BlockingOrchestrator, coordinator)
    monitor, gates = RunMonitor(), {}
    manager = WorkbenchTaskManager(
        db, hub, {"governance": AdapterFactory(monitor, gates)}, run_manager, poll_interval=0.01
    )
    try:
        run_manager.start(project["id"], RunConfig())
        task = manager.create(project["id"], "governance", {})
        time.sleep(0.05)
        assert db.get_task(task["id"])["status"] == "queued"

        assert run_manager.cancel(project["id"]) is True
        wait_for(lambda: db.get_task(task["id"])["status"] == "running")
        wait_for(lambda: task["id"] in gates)
        with pytest.raises(RuntimeError, match="writer active"):
            run_manager.start(project["id"], RunConfig())

        gates[task["id"]].set()
        wait_for(lambda: db.get_task(task["id"])["status"] == "done")
    finally:
        for gate in gates.values():
            gate.set()
        manager.shutdown()
        db.close()


def test_legacy_queue_retries_after_workbench_writer_releases_project(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    coordinator = WriterCoordinator()
    run_manager = RunManager(db, hub, InstantOrchestrator, coordinator)
    queue = QueueRunner(db, run_manager)
    monitor, gates = RunMonitor(), {}
    manager = WorkbenchTaskManager(
        db, hub, {"governance": AdapterFactory(monitor, gates)}, run_manager,
        poll_interval=0.01,
    )
    try:
        task = manager.create(project["id"], "governance", {})
        wait_for(lambda: db.get_task(task["id"])["status"] == "running")
        queue.add(project["id"], RunConfig())
        queue.start()
        time.sleep(0.05)
        assert db.get_active_run(project["id"]) is None

        gates[task["id"]].set()
        wait_for(lambda: db.get_task(task["id"])["status"] == "done")
        wait_for(lambda: any(
            event["type"] == "run_status" and event["detail"] == "done"
            for event in db.list_events(project["id"])
        ))
    finally:
        queue.stop()
        for gate in gates.values():
            gate.set()
        manager.shutdown()
        db.close()


def test_readers_wait_for_writer_admission_then_run_concurrently(manager_env):
    db, p1, _, _, monitor, gates, manager, _, _ = manager_env
    writer = manager.create(p1["id"], "governance", {})
    wait_for(lambda: db.get_task(writer["id"])["status"] == "running")
    reader1 = manager.create(p1["id"], "analysis", {"scope": "whole"})
    reader2 = manager.create(p1["id"], "analysis", {"scope": "chapter"})
    time.sleep(0.05)
    assert db.get_task(reader1["id"])["status"] == "queued"
    assert db.get_task(reader2["id"])["status"] == "queued"

    gates[writer["id"]].set()
    wait_for(lambda: db.get_task(reader1["id"])["status"] == "running")
    wait_for(lambda: db.get_task(reader2["id"])["status"] == "running")
    wait_for(lambda: reader1["id"] in gates and reader2["id"] in gates)
    wait_for(lambda: monitor.active_by_project.get(p1["id"]) == 2)
    assert monitor.max_by_project[p1["id"]] == 2

    gates[reader1["id"]].set()
    gates[reader2["id"]].set()
    wait_for(lambda: db.get_task(reader1["id"])["status"] == "done")
    wait_for(lambda: db.get_task(reader2["id"])["status"] == "done")


def test_cancel_queued_and_running_tasks_and_retry_keeps_history(manager_env):
    db, p1, _, _, _, gates, manager, writer_factory, _ = manager_env
    running = manager.create(p1["id"], "governance", {})
    wait_for(lambda: db.get_task(running["id"])["status"] == "running")
    queued = manager.create(p1["id"], "governance", {})

    cancelled_queued = manager.cancel(queued["id"])
    assert cancelled_queued["status"] == "cancelled"
    assert cancelled_queued["cancel_requested"] is True

    manager.cancel(running["id"])
    wait_for(lambda: db.get_task(running["id"])["status"] == "cancelled")
    assert writer_factory.instances[0].cancel_called.is_set()

    retry = manager.retry(running["id"])
    assert retry["id"] != running["id"]
    assert retry["retry_of"] == running["id"]
    assert db.get_task(running["id"])["status"] == "cancelled"
    wait_for(lambda: db.get_task(retry["id"])["status"] == "running")
    wait_for(lambda: retry["id"] in gates)
    gates[retry["id"]].set()
    wait_for(lambda: db.get_task(retry["id"])["status"] == "done")


def test_exact_event_order_payload_progress_and_failure_evidence(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    events = []
    hub.subscribe(events.append)
    monitor, gates = RunMonitor(), {}
    ok_factory = AdapterFactory(monitor, gates)
    fail_factory = AdapterFactory(monitor, gates, fail=True, partial={"log": "kept"})
    manager = WorkbenchTaskManager(
        db, hub, {"analysis": ok_factory, "governance": fail_factory}, poll_interval=0.01
    )
    try:
        done = manager.create(project["id"], "analysis", {})
        wait_for(lambda: db.get_task(done["id"])["status"] == "running")
        wait_for(lambda: done["id"] in gates)
        gates[done["id"]].set()
        wait_for(lambda: db.get_task(done["id"])["status"] == "done")

        failed = manager.create(project["id"], "governance", {})
        wait_for(lambda: db.get_task(failed["id"])["status"] == "running")
        wait_for(lambda: failed["id"] in gates)
        gates[failed["id"]].set()
        wait_for(lambda: db.get_task(failed["id"])["status"] == "failed")

        done_types = [e["type"] for e in events if e["task_id"] == done["id"]]
        assert done_types == ["task_queued", "task_started", "task_progress", "task_done"]
        failed_types = [e["type"] for e in events if e["task_id"] == failed["id"]]
        assert failed_types == ["task_queued", "task_started", "task_progress", "task_failed"]
        assert all(e["project_id"] == project["id"] and e["task_id"] for e in events)
        assert next(e for e in events if e["type"] == "task_progress")["percent"] == 25
        saved = db.get_task(failed["id"])
        assert saved["error"] == "adapter exploded"
        assert saved["result"] == {"log": "kept"}
        assert saved["ended_at"] is not None
    finally:
        for gate in gates.values():
            gate.set()
        manager.shutdown()
        db.close()


def test_shutdown_cancels_active_work_and_stops_admission(manager_env):
    db, p1, _, _, _, _, manager, writer_factory, _ = manager_env
    task = manager.create(p1["id"], "governance", {})
    wait_for(lambda: db.get_task(task["id"])["status"] == "running")

    manager.shutdown()

    assert db.get_task(task["id"])["status"] == "cancelled"
    assert writer_factory.instances[0].cancel_called.is_set()
    with pytest.raises(RuntimeError, match="shut down"):
        manager.create(p1["id"], "governance", {})
