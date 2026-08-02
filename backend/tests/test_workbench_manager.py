import threading
import time

import pytest

from db import Database
from hub import BroadcastHub
from models import RunConfig
from runner import QueueRunner, RunManager, runconfig_to_config
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


class PreparedAdapter:
    def __init__(self):
        self.prepare_started = threading.Event()
        self.prepare_release = threading.Event()
        self.run_started = threading.Event()
        self.run_release = threading.Event()
        self.received = None

    def prepare(self, context):
        self.prepare_started.set()
        self.prepare_release.wait(3)
        return {"stable_task": context.task_id}

    def run(self, context, prepared_input):
        self.received = prepared_input
        self.run_started.set()
        self.run_release.wait(3)
        return {"prepared": prepared_input}


class ImmediateAdapter:
    def run(self, context):
        return {"task_id": context.task_id}


class FailFirstAdapterFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("adapter construction failed")
        return ImmediateAdapter()


class FailTaskStartedEventDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.fail_task_started_once = True

    def add_event(self, run_id, project_id, type, *args, **kwargs):
        if type == "task_started" and self.fail_task_started_once:
            self.fail_task_started_once = False
            raise RuntimeError("task_started persistence failed")
        return super().add_event(run_id, project_id, type, *args, **kwargs)


class FailRunStatusEventDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.fail_run_status_once = True

    def add_event(self, run_id, project_id, type, *args, **kwargs):
        if type == "run_status" and self.fail_run_status_once:
            self.fail_run_status_once = False
            raise RuntimeError("run_status persistence failed")
        return super().add_event(run_id, project_id, type, *args, **kwargs)


class FailWorkerProjectLookupDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.fail_worker_lookup_once = True

    def get_project(self, project_id):
        if (
            threading.current_thread().name.startswith("workbench-")
            and self.fail_worker_lookup_once
        ):
            self.fail_worker_lookup_once = False
            raise RuntimeError("worker project lookup failed")
        return super().get_project(project_id)


class FailDoneUpdateOnceDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.fail_done_once = True

    def update_run(self, run_id, status=None, rc=None, ended_at=None):
        if status == "done" and self.fail_done_once:
            self.fail_done_once = False
            raise RuntimeError("terminal done update failed")
        return super().update_run(run_id, status=status, rc=rc, ended_at=ended_at)


class FailAllRunUpdatesDatabase(Database):
    def update_run(self, run_id, status=None, rc=None, ended_at=None):
        raise RuntimeError("persistent terminal update failure")


class FailFirstRunUpdateDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.fail_first_update = True

    def update_run(self, run_id, status=None, rc=None, ended_at=None):
        if self.fail_first_update:
            self.fail_first_update = False
            raise RuntimeError("cleanup update failed")
        return super().update_run(run_id, status=status, rc=rc, ended_at=ended_at)


class FailFirstOrchestratorFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self, project_path, config, bus):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("orchestrator setup failed")
        return InstantOrchestrator(project_path, config, bus)


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
        {
            "governance": writer_factory,
            "chapter_analysis_rerun": writer_factory,
            "analysis": reader_factory,
        },
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


def test_chapter_analysis_rerun_cannot_overlap_governance(manager_env):
    db, p1, _, _, _, gates, manager, _, _ = manager_env
    chapter_task = manager.create(p1["id"], "chapter_analysis_rerun", {})
    wait_for(lambda: db.get_task(chapter_task["id"])["status"] == "running")
    governance = manager.create(p1["id"], "governance", {})

    time.sleep(0.05)
    assert db.get_task(governance["id"])["status"] == "queued"

    wait_for(lambda: chapter_task["id"] in gates)
    gates[chapter_task["id"]].set()
    wait_for(lambda: db.get_task(governance["id"])["status"] == "running")
    wait_for(lambda: governance["id"] in gates)
    gates[governance["id"]].set()
    wait_for(lambda: db.get_task(governance["id"])["status"] == "done")


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
        with pytest.raises(RuntimeError, match="writer 活跃中"):
            run_manager.start(project["id"], RunConfig())

        gates[task["id"]].set()
        wait_for(lambda: db.get_task(task["id"])["status"] == "done")
    finally:
        for gate in gates.values():
            gate.set()
        manager.shutdown()
        db.close()


def test_chapter_analysis_rerun_blocks_legacy_automatic_run(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    coordinator = WriterCoordinator()
    run_manager = RunManager(db, hub, InstantOrchestrator, coordinator)
    monitor, gates = RunMonitor(), {}
    manager = WorkbenchTaskManager(
        db,
        hub,
        {"chapter_analysis_rerun": AdapterFactory(monitor, gates)},
        run_manager,
        poll_interval=0.01,
    )
    try:
        task = manager.create(project["id"], "chapter_analysis_rerun", {})
        wait_for(lambda: db.get_task(task["id"])["status"] == "running")
        with pytest.raises(RuntimeError, match="writer 活跃中"):
            run_manager.start(project["id"], RunConfig())
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


def test_reader_without_prepare_holds_lease_for_its_whole_run(manager_env):
    db, p1, _, _, _, gates, manager, _, _ = manager_env
    reader = manager.create(p1["id"], "analysis", {})
    wait_for(lambda: db.get_task(reader["id"])["status"] == "running")
    wait_for(lambda: reader["id"] in gates)
    writer = manager.create(p1["id"], "governance", {})

    time.sleep(0.05)
    assert db.get_task(writer["id"])["status"] == "queued"

    gates[reader["id"]].set()
    wait_for(lambda: db.get_task(writer["id"])["status"] == "running")
    wait_for(lambda: writer["id"] in gates)
    gates[writer["id"]].set()
    wait_for(lambda: db.get_task(writer["id"])["status"] == "done")


def test_prepared_reader_releases_lease_after_stable_input_and_not_before(tmp_path):
    db = Database(tmp_path / "tasks.db")
    p1_path, p2_path = tmp_path / "p1", tmp_path / "p2"
    p1_path.mkdir()
    p2_path.mkdir()
    p1 = db.create_project(str(p1_path))
    p2 = db.create_project(str(p2_path))
    hub = BroadcastHub()
    prepared = PreparedAdapter()
    monitor, gates = RunMonitor(), {}
    writer_factory = AdapterFactory(monitor, gates)
    manager = WorkbenchTaskManager(
        db,
        hub,
        {"analysis": prepared, "governance": writer_factory},
        poll_interval=0.01,
    )
    try:
        reader = manager.create(p1["id"], "analysis", {})
        assert prepared.prepare_started.wait(1)
        same_project_writer = manager.create(p1["id"], "governance", {})
        other_project_writer = manager.create(p2["id"], "governance", {})

        wait_for(lambda: db.get_task(other_project_writer["id"])["status"] == "running")
        time.sleep(0.05)
        assert db.get_task(same_project_writer["id"])["status"] == "queued"

        prepared.prepare_release.set()
        assert prepared.run_started.wait(1)
        assert prepared.received == {"stable_task": reader["id"]}
        wait_for(lambda: db.get_task(same_project_writer["id"])["status"] == "running")
        assert db.get_task(reader["id"])["status"] == "running"

        prepared.run_release.set()
        wait_for(lambda: same_project_writer["id"] in gates and other_project_writer["id"] in gates)
        gates[same_project_writer["id"]].set()
        gates[other_project_writer["id"]].set()
        wait_for(lambda: db.get_task(reader["id"])["status"] == "done")
    finally:
        prepared.prepare_release.set()
        prepared.run_release.set()
        for gate in gates.values():
            gate.set()
        manager.shutdown()
        db.close()


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


def test_scheduler_finds_queued_task_after_more_than_100_older_rows(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    for index in range(101):
        older = db.create_task(project["id"], "analysis", {"older": index}, {})
        db.update_task(older["id"], status="done")
    target = db.create_task(project["id"], "analysis", {"target": True}, {})
    manager = WorkbenchTaskManager(
        db, BroadcastHub(), {"analysis": ImmediateAdapter()}, poll_interval=0.01
    )
    try:
        wait_for(lambda: db.get_task(target["id"])["status"] == "done")
    finally:
        manager.shutdown()
        db.close()


def test_adapter_construction_failure_releases_lease_and_scheduler_continues(tmp_path):
    db = Database(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = WorkbenchTaskManager(
        db,
        BroadcastHub(),
        {"governance": FailFirstAdapterFactory()},
        poll_interval=0.01,
    )
    try:
        failed = manager.create(project["id"], "governance", {"order": 1})
        following = manager.create(project["id"], "governance", {"order": 2})

        wait_for(lambda: db.get_task(failed["id"])["status"] == "failed")
        assert db.get_task(failed["id"])["error"] == "adapter construction failed"
        wait_for(lambda: db.get_task(following["id"])["status"] == "done")
    finally:
        manager.shutdown()
        db.close()


def test_task_started_event_failure_cleans_active_lease_and_next_task_runs(tmp_path):
    db = FailTaskStartedEventDatabase(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = WorkbenchTaskManager(
        db, BroadcastHub(), {"governance": ImmediateAdapter()}, poll_interval=0.01
    )
    try:
        failed = manager.create(project["id"], "governance", {"order": 1})
        following = manager.create(project["id"], "governance", {"order": 2})

        wait_for(lambda: db.get_task(failed["id"])["status"] == "failed")
        assert db.get_task(failed["id"])["error"] == "task_started persistence failed"
        wait_for(lambda: db.get_task(following["id"])["status"] == "done")
    finally:
        manager.shutdown()
        db.close()


def test_worker_context_failure_releases_lease_and_next_task_runs(tmp_path):
    db = FailWorkerProjectLookupDatabase(tmp_path / "tasks.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = WorkbenchTaskManager(
        db, BroadcastHub(), {"governance": ImmediateAdapter()}, poll_interval=0.01
    )
    try:
        failed = manager.create(project["id"], "governance", {"order": 1})
        following = manager.create(project["id"], "governance", {"order": 2})

        wait_for(lambda: db.get_task(failed["id"])["status"] == "failed")
        assert db.get_task(failed["id"])["error"] == "worker project lookup failed"
        wait_for(lambda: db.get_task(following["id"])["status"] == "done")
    finally:
        manager.shutdown()
        db.close()


def test_legacy_worker_tail_failure_releases_lease_and_next_run_starts(tmp_path):
    db = FailRunStatusEventDatabase(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = RunManager(db, BroadcastHub(), InstantOrchestrator, WriterCoordinator())

    first = manager.start(project["id"], RunConfig())
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: db.get_run(second)["status"] == "done")

    assert db.get_run(first)["status"] == "done"
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    db.close()


def test_run_status_broadcast_observes_already_persisted_terminal_state(tmp_path):
    db = Database(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    observed = []

    def observe(event):
        if event["type"] == "run_status":
            observed.append((event["detail"], db.get_run(event["run_id"])["status"]))

    hub.subscribe(observe)
    manager = RunManager(db, hub, InstantOrchestrator, WriterCoordinator())
    run_id = manager.start(project["id"], RunConfig())
    wait_for(lambda: manager.active_run_id(project["id"]) is None)

    assert db.get_run(run_id)["status"] == "done"
    assert observed == [("done", "done")]
    db.close()


def test_failed_done_update_falls_back_before_consistent_terminal_broadcast(tmp_path):
    db = FailDoneUpdateOnceDatabase(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    events = []
    hub.subscribe(events.append)
    manager = RunManager(db, hub, InstantOrchestrator, WriterCoordinator())

    first = manager.start(project["id"], RunConfig())
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    first_statuses = [
        event["detail"] for event in events
        if event["type"] == "run_status" and event["run_id"] == first
    ]
    assert db.get_run(first)["status"] == "blocked"
    assert first_statuses == ["blocked"]
    assert any(
        event["type"] == "pipeline_error" and event["run_id"] == first
        for event in events
    )

    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: db.get_run(second)["status"] == "done")
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    db.close()


def test_persistent_terminal_update_failure_never_broadcasts_false_done(tmp_path):
    db = FailAllRunUpdatesDatabase(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    hub = BroadcastHub()
    events = []
    hub.subscribe(events.append)
    manager = RunManager(db, hub, InstantOrchestrator, WriterCoordinator())

    first = manager.start(project["id"], RunConfig())
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    assert not any(
        event["type"] == "run_status" and event["run_id"] == first
        for event in events
    )

    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    assert second != first
    assert db.get_run(first)["status"] == "running"
    assert db.get_run(second)["status"] == "running"
    assert db.mark_running_runs_interrupted() == [
        (first, project["id"]), (second, project["id"])
    ]
    db.close()


def test_legacy_thread_start_failure_cleans_active_and_writer_lease(tmp_path, monkeypatch):
    db = Database(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = RunManager(db, BroadcastHub(), InstantOrchestrator, WriterCoordinator())
    original_start = threading.Thread.start
    failed_once = False

    def fail_first_run_start(thread):
        nonlocal failed_once
        if thread.name.startswith("run-") and not failed_once:
            failed_once = True
            raise RuntimeError("thread start failed")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_first_run_start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        manager.start(project["id"], RunConfig())

    assert manager.active_run_id(project["id"]) is None
    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: db.get_run(second)["status"] == "done")
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    db.close()


def test_thread_start_cleanup_update_failure_preserves_cleanup_and_original_error(
    tmp_path, monkeypatch
):
    db = FailFirstRunUpdateDatabase(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    manager = RunManager(db, BroadcastHub(), InstantOrchestrator, WriterCoordinator())
    original_start = threading.Thread.start
    failed_once = False

    def fail_first_run_start(thread):
        nonlocal failed_once
        if thread.name.startswith("run-") and not failed_once:
            failed_once = True
            raise RuntimeError("thread start failed")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_first_run_start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        manager.start(project["id"], RunConfig())
    assert manager.active_run_id(project["id"]) is None

    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: db.get_run(second)["status"] == "done")
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    db.close()


def test_setup_cleanup_update_failure_releases_lease_and_preserves_setup_error(tmp_path):
    db = FailFirstRunUpdateDatabase(tmp_path / "runs.db")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.create_project(str(project_path))
    factory = FailFirstOrchestratorFactory()
    manager = RunManager(db, BroadcastHub(), factory, WriterCoordinator())

    with pytest.raises(RuntimeError, match="orchestrator setup failed"):
        manager.start(project["id"], RunConfig())
    assert manager.active_run_id(project["id"]) is None

    second = manager.start(project["id"], RunConfig())
    wait_for(lambda: db.get_run(second)["status"] == "done")
    wait_for(lambda: manager.active_run_id(project["id"]) is None)
    db.close()


def test_shutdown_cancels_active_work_and_stops_admission(manager_env):
    db, p1, _, _, _, _, manager, writer_factory, _ = manager_env
    task = manager.create(p1["id"], "governance", {})
    wait_for(lambda: db.get_task(task["id"])["status"] == "running")

    manager.shutdown()

    assert db.get_task(task["id"])["status"] == "cancelled"
    assert writer_factory.instances[0].cancel_called.is_set()
    with pytest.raises(RuntimeError, match="已关闭"):
        manager.create(p1["id"], "governance", {})


def test_runconfig_to_config_passes_compression_fields():
    """RunConfig 的压缩策略字段必须透传到 orchestrator.Config，否则设置页配置静默失效。"""
    rc = RunConfig(
        include_original="full",
        max_original_chars=9999,
        max_analysis_chars=8888,
        story_summary_max_items=77,
        context_budget_chars=222222,
        delta_full_story_kb=66,
        audit_max_context_chars=111111,
    )
    cfg = runconfig_to_config(rc)
    assert cfg.include_original == "full"
    assert cfg.max_original_chars == 9999
    assert cfg.max_analysis_chars == 8888
    assert cfg.story_summary_max_items == 77
    assert cfg.context_budget_chars == 222222
    assert cfg.delta_full_story_kb == 66
    assert cfg.audit_max_context_chars == 111111
