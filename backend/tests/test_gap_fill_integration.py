"""GF8 integration tests for the 5 new maintenance writer task types.

Mirrors ``test_workbench_integration.py``'s real-app + fake-adapter pattern to
cover the gap-fill maintenance workflows end-to-end through the FastAPI app:

- The 5 new types (``recover_story`` / ``migrate_chronology`` / ``finalize`` /
  ``normalize`` / ``repair``) queue as WRITERS and respect the project writer
  mutex shared with the auto pipeline (a queued maintenance writer stays
  ``queued`` while a fake auto-pipeline run holds the writer; goes ``running``
  after cancel) - mirrors Scenario 1 of test_workbench_integration.py.
- Cross-project: two maintenance writers on two projects run concurrently.
- Task events (``task_queued`` / ``task_started`` / ``task_done``) carry
  ``task_id`` + ``project_id``.
- ONE real-engine happy-path: ``normalize`` on a seeded project changes the
  ``process`` revision and the task goes ``done``.
- ``repair`` on a project with a broken chapter delta JSON: task goes ``done``
  and the delta now parses.
- ``GET /projects/{pid}/recover/snapshots`` returns a read-only list.

Fake adapters (mode="done") back the mutex/events tests so they stay
deterministic; the real ``NormalizeAdapter`` / ``RepairAdapter`` back the two
real-engine happy-path tests.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from task_adapters import NormalizeAdapter, RepairAdapter
from workbench import TaskContext

AUTH = {"Authorization": "Bearer sekret"}

# Make engine/scripts importable so the real adapters can load their helpers.
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))


MAINTENANCE_TYPES = ("recover_story", "migrate_chronology", "finalize", "normalize", "repair")


# --- shared helpers (mirror test_workbench_integration.py) ------------------

def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_chapters(project: Path, count: int = 1) -> Path:
    """Seed 原文拆解/_索引.json + N chapter md files. Returns the out dir."""
    out_dir = project / "原文拆解"
    out_dir.mkdir(parents=True, exist_ok=True)
    titles = ["序章", "相遇", "冲突", "转折", "高潮", "结局"]
    chapters = []
    for seq in range(1, count + 1):
        title = titles[seq - 1] if seq <= len(titles) else f"第{seq}章"
        fname = f"第{seq:03d}章_{title}.md"
        (out_dir / fname).write_text(f"# 第{seq}章 {title}\n内容{seq}\n", encoding="utf-8")
        chapters.append({
            "seq": seq, "filename": fname, "title": f"第{seq}章 {title}",
            "source_title": f"第{seq}章 {title}",
        })
    index = {
        "chapters": chapters,
        "source_file": str(project.parent / "old_book.txt"),
        "pattern": r"^第\d+章",
        "chapter_count": count,
    }
    _write_json(out_dir / "_索引.json", index)
    return out_dir


def _seed_minimal_process_story(project: Path) -> None:
    """Seed a minimal valid process story JSON for normalize/repair tests."""
    story = {
        "介绍": {"标题": "维护集成测试", "描述": "归一化/修复回归。"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    _write_json(project / "故事结构_增量.json", story)


# --- fake worker adapters ---------------------------------------------------

class BlockingFakeAdapter:
    """Fake adapter driven by ``params['mode']``:

    - ``"done"`` (default): record start, emit Chinese progress, return immediately.
    - ``"block"``: record start, emit progress, block on a per-task gate until
      released or cancelled.

    Records start/active counts so tests can assert ordering/concurrency.
    """
    def __init__(self, state: dict):
        self.state = state
        self.cancel_called = threading.Event()

    def _enter(self, context: TaskContext) -> None:
        with self.state["lock"]:
            self.state["started"].append(context.task_id)
            self.state["active_by_project"][context.project_id] = \
                self.state["active_by_project"].get(context.project_id, 0) + 1
            self.state["max_by_project"][context.project_id] = max(
                self.state["active_by_project"].get(context.project_id, 0),
                self.state["max_by_project"].get(context.project_id, 0),
            )

    def _leave(self, context: TaskContext) -> None:
        with self.state["lock"]:
            self.state["active_by_project"][context.project_id] -= 1

    def run(self, context: TaskContext, prepared_input: Any = None) -> dict:
        mode = (context.params or {}).get("mode", "done")
        self._enter(context)
        try:
            context.progress(50, "维护任务运行中")
            if mode != "block":
                return {"task_id": context.task_id, "done": True, "mode": mode}
            gate = self.state["gates"].setdefault(context.task_id, threading.Event())
            while not gate.wait(0.01):
                if context.cancellation_event.is_set():
                    return {"cancelled": True, "task_id": context.task_id}
            return {"task_id": context.task_id, "done": True, "mode": "block"}
        finally:
            self._leave(context)

    def cancel(self):
        self.cancel_called.set()


def _fake_factory(state: dict):
    def factory():
        return BlockingFakeAdapter(state)
    return factory


class BlockingFakeOrchestrator:
    """Fake auto-pipeline orchestrator that blocks until cancel()."""
    instances: list["BlockingFakeOrchestrator"] = []

    def __init__(self, project_path, config, bus):
        self.cancelled = threading.Event()
        self.project_path = str(project_path)
        BlockingFakeOrchestrator.instances.append(self)

    def run(self) -> int:
        self.cancelled.wait(10)
        return 2  # cancelled

    def cancel(self):
        self.cancelled.set()


@pytest.fixture
def app_client(tmp_path):
    """Real app + TestClient with fake maintenance adapters (no engine/LLM)."""
    state = {"lock": threading.Lock(), "started": [], "active_by_project": {},
             "max_by_project": {}, "gates": {}}
    BlockingFakeOrchestrator.instances.clear()
    task_adapters = {t: _fake_factory(state) for t in MAINTENANCE_TYPES}
    db_path = str(tmp_path / "gf8_integration.db")
    app = create_app(db_path, "sekret",
                     orchestrator_factory=BlockingFakeOrchestrator,
                     task_adapters=task_adapters)
    client = TestClient(app)
    yield client, app, state, tmp_path
    for gate in state["gates"].values():
        gate.set()
    app.state.workbench.shutdown()


def _create_project(client, path: Path, name: str = "") -> dict:
    path.mkdir(parents=True, exist_ok=True)
    return client.post(
        "/projects", json={"path": str(path), "name": name}, headers=AUTH
    ).json()


def _task_status(client, pid, task_id):
    resp = client.get(f"/projects/{pid}/tasks/{task_id}", headers=AUTH)
    return resp.json()["status"] if resp.status_code == 200 else None


# === Scenario 1: maintenance writer respects project writer mutex =========

@pytest.mark.parametrize("task_type", MAINTENANCE_TYPES)
def test_maintenance_writer_waits_for_auto_pipeline_then_runs(app_client, task_type):
    """Each of the 5 maintenance types queues as a writer and waits behind the
    auto pipeline's project writer lock; on cancel it runs. Mirrors Scenario 1
    of test_workbench_integration.py.
    """
    client, app, state, tmp_path = app_client
    project = _create_project(client, tmp_path / "测试书")
    pid = project["id"]
    _seed_chapters(tmp_path / "测试书")

    # Start the automatic (legacy) pipeline run with the blocking orchestrator.
    run_resp = client.post(f"/projects/{pid}/run", json={"config": {}}, headers=AUTH)
    assert run_resp.status_code == 200, run_resp.text
    _wait(lambda: BlockingFakeOrchestrator.instances)

    # Queue a maintenance writer. It must wait behind the auto writer.
    queued = client.post(
        f"/projects/{pid}/tasks",
        json={"type": task_type, "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    time.sleep(0.1)
    assert _task_status(client, pid, queued["id"]) == "queued"
    assert state["started"] == []

    # Cancel the auto run; the writer lock releases and the maintenance task runs.
    assert client.post(f"/projects/{pid}/cancel", headers=AUTH).json()["cancelled"] is True
    _wait(lambda: _task_status(client, pid, queued["id"]) == "running")
    _wait(lambda: queued["id"] in state["gates"])

    state["gates"][queued["id"]].set()
    _wait(lambda: _task_status(client, pid, queued["id"]) == "done")


# === Scenario 2: cross-project concurrent maintenance writers ==============

def test_two_projects_run_maintenance_writers_concurrently(app_client):
    client, app, state, tmp_path = app_client
    p1 = _create_project(client, tmp_path / "项目甲")
    p2 = _create_project(client, tmp_path / "项目乙")
    _seed_chapters(tmp_path / "项目甲")
    _seed_chapters(tmp_path / "项目乙")

    t1 = client.post(
        f"/projects/{p1['id']}/tasks",
        json={"type": "normalize", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    t2 = client.post(
        f"/projects/{p2['id']}/tasks",
        json={"type": "repair", "params": {"mode": "block"}}, headers=AUTH,
    ).json()

    # Both writers run concurrently (different projects, separate writer locks).
    # Wait on the gates (set inside run() after _enter) so the start bookkeeping
    # is guaranteed to be populated before we assert on it.
    _wait(lambda: t1["id"] in state["gates"] and t2["id"] in state["gates"])
    assert state["max_by_project"][p1["id"]] == 1
    assert state["max_by_project"][p2["id"]] == 1
    assert set(state["started"]) == {t1["id"], t2["id"]}

    state["gates"][t1["id"]].set()
    state["gates"][t2["id"]].set()
    _wait(lambda: _task_status(client, p1["id"], t1["id"]) == "done")
    _wait(lambda: _task_status(client, p2["id"], t2["id"]) == "done")


# === Scenario 3: task events carry task_id + project_id ====================

def test_maintenance_task_events_carry_task_id_and_project_id(app_client):
    client, app, state, tmp_path = app_client
    events: list[dict] = []
    app.state.hub.subscribe(events.append)
    project = _create_project(client, tmp_path / "测试书")
    pid = project["id"]
    _seed_chapters(tmp_path / "测试书")

    done = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "normalize", "params": {"mode": "done"}}, headers=AUTH,
    ).json()
    _wait(lambda: _task_status(client, pid, done["id"]) == "done")

    # Persistence precedes event delivery; wait for the observable event too.
    _wait(lambda: any(e["type"] == "task_done" and e.get("task_id") == done["id"] for e in events))
    task_events = [e for e in events if e["type"].startswith("task_")]
    assert task_events
    for e in task_events:
        assert e.get("task_id"), e
        assert e.get("project_id") == pid

    types = [e["type"] for e in task_events if e["task_id"] == done["id"]]
    assert types == ["task_queued", "task_started", "task_progress", "task_done"]

    # Persisted events also carry task_id + project_id in data.
    persisted = client.get(f"/projects/{pid}/events", headers=AUTH).json()
    ptask = [e for e in persisted if e["type"].startswith("task_")]
    assert ptask
    for e in ptask:
        assert e["data"]["task_id"]
        assert e["data"]["project_id"] == pid


# === Scenario 4: real-engine normalize happy path ==========================

def test_normalize_real_engine_changes_process_revision_and_goes_done(tmp_path):
    """Real NormalizeAdapter through the real app: process JSON rewritten,
    process revision changes, task status == done."""
    db_path = str(tmp_path / "gf8_normalize.db")
    # Real normalize adapter; fake everything else (none invoked here).
    task_adapters = {"normalize": NormalizeAdapter()}
    app = create_app(db_path, "sekret", task_adapters=task_adapters)
    client = TestClient(app)
    try:
        project_path = tmp_path / "拆书_归一化"
        _seed_chapters(project_path)
        # Seed a story with a non-canonical top-level field (备注) so
        # normalize_structure migrates it into 介绍.描述, guaranteeing the
        # process bytes (and thus the process revision) actually change.
        _write_json(project_path / "故事结构_增量.json", {
            "介绍": {"标题": "维护集成测试"},
            "角色集": [],
            "事件集": [],
            "备注": "待归一化的非规范顶层字段",
        })
        project = _create_project(client, project_path)

        from revisions import project_revisions
        pre_rev = project_revisions(project_path)["process"]

        response = client.post(
            f"/projects/{project['id']}/tasks",
            json={"type": "normalize", "params": {}}, headers=AUTH,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        _wait(lambda: app.state.db.get_task(task_id)["status"] in {"done", "failed"})
        final = app.state.db.get_task(task_id)
        assert final["status"] == "done", final.get("error")

        # Process JSON still parses and the process revision changed.
        process_path = project_path / "故事结构_增量.json"
        rewritten = json.loads(process_path.read_text(encoding="utf-8"))
        assert isinstance(rewritten, dict)
        post_rev = project_revisions(project_path)["process"]
        assert post_rev != pre_rev
    finally:
        app.state.workbench.shutdown()


# === Scenario 5: real-engine repair happy path ============================

def test_repair_real_engine_fixes_broken_delta_and_goes_done(tmp_path):
    """Real RepairAdapter through the real app: a broken chapter delta JSON is
    repaired in place and the task status == done."""
    db_path = str(tmp_path / "gf8_repair.db")
    task_adapters = {"repair": RepairAdapter()}
    app = create_app(db_path, "sekret", task_adapters=task_adapters)
    client = TestClient(app)
    try:
        project_path = tmp_path / "拆书_修复"
        out_dir = _seed_chapters(project_path)
        _seed_minimal_process_story(project_path)

        # Locate the seeded chapter and write a truncated (broken) delta JSON
        # next to it under 章节处理/.
        index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
        chapter_record = index["chapters"][0]
        chapter_path = out_dir / chapter_record["filename"]
        delta_path = project_path / "章节处理" / "Delta" / f"{chapter_path.stem}.json"
        delta_path.parent.mkdir(parents=True, exist_ok=True)
        delta_path.write_text('{"章节": "第001章", "新增元素": ', encoding="utf-8")  # truncated
        # Confirm it does not parse before the repair.
        with pytest.raises(json.JSONDecodeError):
            json.loads(delta_path.read_text(encoding="utf-8"))

        project = _create_project(client, project_path)
        response = client.post(
            f"/projects/{project['id']}/tasks",
            json={"type": "repair", "params": {"chapters": "all"}}, headers=AUTH,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        _wait(lambda: app.state.db.get_task(task_id)["status"] in {"done", "failed"})
        final = app.state.db.get_task(task_id)
        assert final["status"] == "done", final.get("error")

        # The delta now parses as strict JSON and retains its 章节 field.
        data = json.loads(delta_path.read_text(encoding="utf-8"))
        assert data["章节"] == "第001章"
    finally:
        app.state.workbench.shutdown()


# === Scenario 6: recover preview endpoint is read-only ====================

def test_recover_snapshots_preview_endpoint_returns_list(tmp_path):
    """GET /projects/{pid}/recover/snapshots returns a read-only list of
    story_after_chNNN snapshots."""
    db_path = str(tmp_path / "gf8_recover.db")
    # No maintenance task is invoked; any adapter set works. Use the real
    # normalize adapter purely so create_app has a non-empty adapter map.
    task_adapters = {"normalize": NormalizeAdapter()}
    app = create_app(db_path, "sekret", task_adapters=task_adapters)
    client = TestClient(app)
    try:
        project_path = tmp_path / "拆书_恢复"
        _seed_chapters(project_path)
        _seed_minimal_process_story(project_path)
        snapshot = {
            "介绍": {"标题": "快照测试", "描述": "恢复预览。"},
            "角色集": [], "事件集": [], "地点集": [], "线索集": [],
            "阵营集": [], "物品集": [], "其他事项集": [],
        }
        _write_json(project_path / "故事结构版本" / "story_after_ch001.json", snapshot)

        project = _create_project(client, project_path)
        process_before = (project_path / "故事结构_增量.json").read_bytes()

        resp = client.get(
            f"/projects/{project['id']}/recover/snapshots", headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        snaps = resp.json()
        assert isinstance(snaps, list)
        assert len(snaps) == 1
        assert snaps[0]["snapshot_seq"] == 1
        assert "story_after_ch001.json" in snaps[0]["path"].replace("\\", "/")

        # Read-only: process JSON bytes unchanged.
        assert (project_path / "故事结构_增量.json").read_bytes() == process_before
    finally:
        app.state.workbench.shutdown()


if __name__ == "__main__":
    import tempfile
    test_normalize_real_engine_changes_process_revision_and_goes_done(Path(tempfile.mkdtemp()))
    print("ok")
