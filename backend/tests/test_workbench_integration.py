"""End-to-end workbench integration tests (Task 8 Step 1).

Drives the REAL stack end-to-end:
- real ``Database`` (SQLite, on-disk)
- real ``WorkbenchTaskManager`` scheduler thread
- real FastAPI app via ``TestClient``
- real ``project_revisions`` / ``task_is_stale``
- real on-disk project artifacts under ``tmp_path``
- fake worker adapters only where the production adapter would invoke a real
  LLM worker — deterministic engine functions (append / governance-with-
  pre-populated-correction / export) run against the REAL adapters.

Covers the nine required scenarios from the task brief:
1. automatic run + queued workbench writer (writer waits, then runs)
2. cross-project concurrent execution
3. restart recovery (queued persists; running -> interrupted)
4. force rerun preserves prior task history
5. append -> new-chapter chain -> stale whole-book outputs
6. governance rollback (post-apply structure failure restores before snapshot)
7. export task produces a readable artifact + result_json
8. task event payloads carry task_id + project_id
9. Chinese project names + Chinese log content round-trip without mojibake
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from db import Database
from task_adapters import (
    AppendChaptersTaskAdapter,
    ExportAdapter,
    GovernanceTaskAdapter,
)
from workbench import TaskContext

AUTH = {"Authorization": "Bearer sekret"}


# --- shared helpers --------------------------------------------------------

def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def _progress(*args, **kwargs):
    pass


def _make_context(project_path: Path, params: dict, task_id: int = 1,
                  project_id: int = 1) -> TaskContext:
    return TaskContext(
        task_id=task_id,
        project_id=project_id,
        project_path=Path(project_path),
        params=params,
        cancellation_event=threading.Event(),
        _progress_callback=_progress,
    )


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_chapters(project: Path, count: int = 3) -> Path:
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
    _write_json(project / "故事结构_增量.json", {})
    return out_dir


def _seed_minimal_story(project: Path) -> None:
    """Seed a minimal but valid 故事结构.json so export can run."""
    story = {
        "介绍": {"标题": "测试故事", "描述": "集成测试用最小故事结构。"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    _write_json(project / "故事结构.json", story)


# --- fake worker adapters --------------------------------------------------

class BlockingFakeAdapter:
    """Fake adapter driven by ``params['mode']``:

    - ``"done"`` (default): record start, emit Chinese progress, return immediately.
    - ``"block"``: record start, emit progress, block on a per-task gate until
      released or cancelled.
    - ``"fail"``: raise immediately with a partial result (evidence preserved).

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
            context.progress(50, "工作台任务运行中")
            if mode == "fail":
                error = RuntimeError("集成失败证据")
                error.result = {"evidence": "preserved", "task_id": context.task_id}
                raise error
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
    """Real app + TestClient with fake worker adapters (no LLM)."""
    state = {"lock": threading.Lock(), "started": [], "active_by_project": {},
             "max_by_project": {}, "gates": {}}
    BlockingFakeOrchestrator.instances.clear()
    task_adapters = {
        "analysis": _fake_factory(state),
        "chapter_visual": _fake_factory(state),
        "chapter_structure": _fake_factory(state),
        "governance": _fake_factory(state),
        "chapter_analysis_rerun": _fake_factory(state),
        "chapter_delta_rerun": _fake_factory(state),
    }
    db_path = str(tmp_path / "integration.db")
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


# === Scenario 1: automatic run + queued workbench writer ===================

def test_automatic_run_blocks_queued_workbench_writer_then_runs(app_client):
    client, app, state, tmp_path = app_client
    project = _create_project(client, tmp_path / "测试书")
    pid = project["id"]
    _seed_chapters(tmp_path / "测试书")

    # Start the automatic (legacy) pipeline run with the blocking orchestrator.
    run_resp = client.post(f"/projects/{pid}/run", json={"config": {}}, headers=AUTH)
    assert run_resp.status_code == 200, run_resp.text
    _wait(lambda: BlockingFakeOrchestrator.instances)

    # Queue a workbench writer (governance). It must wait behind the auto writer.
    queued = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    time.sleep(0.1)
    assert _task_status(client, pid, queued["id"]) == "queued"
    assert state["started"] == []

    # Cancel the auto run; the writer lock releases and the workbench runs.
    assert client.post(f"/projects/{pid}/cancel", headers=AUTH).json()["cancelled"] is True
    _wait(lambda: _task_status(client, pid, queued["id"]) == "running")
    _wait(lambda: queued["id"] in state["gates"])

    state["gates"][queued["id"]].set()
    _wait(lambda: _task_status(client, pid, queued["id"]) == "done")


# === Scenario 2: cross-project concurrent execution ========================

def test_two_projects_run_writers_concurrently(app_client):
    client, app, state, tmp_path = app_client
    p1 = _create_project(client, tmp_path / "项目甲")
    p2 = _create_project(client, tmp_path / "项目乙")
    _seed_chapters(tmp_path / "项目甲")
    _seed_chapters(tmp_path / "项目乙")

    t1 = client.post(
        f"/projects/{p1['id']}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    t2 = client.post(
        f"/projects/{p2['id']}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()

    # Both writers run concurrently (different projects, separate writer locks).
    _wait(lambda: _task_status(client, p1["id"], t1["id"]) == "running")
    _wait(lambda: _task_status(client, p2["id"], t2["id"]) == "running")
    assert state["max_by_project"][p1["id"]] == 1
    assert state["max_by_project"][p2["id"]] == 1
    assert set(state["started"]) == {t1["id"], t2["id"]}

    _wait(lambda: t1["id"] in state["gates"] and t2["id"] in state["gates"])
    state["gates"][t1["id"]].set()
    state["gates"][t2["id"]].set()
    _wait(lambda: _task_status(client, p1["id"], t1["id"]) == "done")
    _wait(lambda: _task_status(client, p2["id"], t2["id"]) == "done")


# === Scenario 3: restart recovery ==========================================

def test_restart_interrupts_running_and_resumes_queued(tmp_path):
    db_path = str(tmp_path / "restart.db")
    project_path = tmp_path / "测试书"
    project_path.mkdir(parents=True)
    _seed_chapters(project_path)

    # Session 1: create a project + a running task + a queued task, then close.
    db1 = Database(db_path)
    project = db1.create_project(str(project_path))
    running = db1.create_task(project["id"], "governance", {"mode": "block"}, {})
    queued = db1.create_task(project["id"], "governance", {"mode": "block"}, {})
    db1.update_task(running["id"], status="running")
    db1.close()

    # Session 2: a fresh app start simulates a process restart.
    state = {"lock": threading.Lock(), "started": [], "active_by_project": {},
             "max_by_project": {}, "gates": {}}
    task_adapters = {"governance": _fake_factory(state)}
    app2 = create_app(db_path, "sekret", task_adapters=task_adapters)
    client2 = TestClient(app2)
    try:
        # Residual running task -> interrupted; queued task resumes.
        assert app2.state.db.get_task(running["id"])["status"] == "interrupted"
        _wait(lambda: app2.state.db.get_task(queued["id"])["status"] == "running")
        _wait(lambda: queued["id"] in state["gates"])
        state["gates"][queued["id"]].set()
        _wait(lambda: app2.state.db.get_task(queued["id"])["status"] == "done")
    finally:
        for gate in state["gates"].values():
            gate.set()
        app2.state.workbench.shutdown()


# === Scenario 4: force rerun preserves prior task history ==================

def test_force_rerun_preserves_prior_task_history(app_client):
    client, app, state, tmp_path = app_client
    project = _create_project(client, tmp_path / "测试书")
    pid = project["id"]
    _seed_chapters(tmp_path / "测试书")

    original = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "analysis",
              "params": {"question": "主角为什么犹豫？", "mode": "block"}},
        headers=AUTH,
    ).json()
    _wait(lambda: original["id"] in state["gates"])
    state["gates"][original["id"]].set()
    _wait(lambda: _task_status(client, pid, original["id"]) == "done")

    # Retry (= force rerun) creates a new row; the old row must remain.
    retried = client.post(
        f"/projects/{pid}/tasks/{original['id']}/retry", headers=AUTH
    ).json()
    assert retried["id"] != original["id"]
    assert retried["retry_of"] == original["id"]

    _wait(lambda: retried["id"] in state["gates"])
    state["gates"][retried["id"]].set()
    _wait(lambda: _task_status(client, pid, retried["id"]) == "done")

    # Prior history preserved: both rows exist, old is still done.
    listed = client.get(f"/projects/{pid}/tasks", headers=AUTH).json()
    ids = [t["id"] for t in listed]
    assert original["id"] in ids and retried["id"] in ids
    old = next(t for t in listed if t["id"] == original["id"])
    assert old["status"] == "done"
    assert old["params"]["question"] == "主角为什么犹豫？"
    assert old["params"]["mode"] == "block"


# === Scenario 5: append -> new-chapter chain -> stale outputs ==============

def test_append_chains_new_chapter_tasks_and_stales_whole_book(app_client, tmp_path):
    client, app, state, _ = app_client
    project_path = tmp_path / "测试书"
    # Register a real append adapter (with manager auto-wired by create_app) by
    # replacing the fake in the live manager's adapter map.  This keeps the
    # other (LLM-backed) adapters fake while exercising the real append wiring.
    real_append = AppendChaptersTaskAdapter()
    real_append.manager = app.state.workbench
    app.state.workbench.adapters["append_chapters"] = real_append

    project = _create_project(client, project_path)
    pid = project["id"]
    out_dir = _seed_chapters(project_path)

    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 相遇\n内容2\n第3章 冲突\n内容3\n"
        "第4章 续写\n内容4\n第5章 续写二\n内容5\n",
        encoding="utf-8",
    )
    plan = client.post(
        f"/projects/{pid}/chapters/append-preview",
        json={"source_file": str(new_src)}, headers=AUTH,
    ).json()
    assert plan["appended"]
    assert [c["seq"] for c in plan["appended"]] == [4, 5]

    old_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    old_chapter_rev = app.state.db.get_task  # noop; capture via revisions below
    from revisions import project_revisions
    pre_rev = project_revisions(project_path)

    apply_resp = client.post(
        f"/projects/{pid}/chapters/apply", json={"plan": plan}, headers=AUTH,
    )
    assert apply_resp.status_code == 200, apply_resp.text
    append_task = apply_resp.json()
    _wait(lambda: app.state.db.get_task(append_task["id"])["status"] == "done")
    append_result = app.state.db.get_task(append_task["id"])["result"]

    # Index updated; new md files written; old md untouched.
    new_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    assert [c["seq"] for c in new_index["chapters"]] == [1, 2, 3, 4, 5]
    assert new_index["source_file"] == str(new_src.resolve())
    assert (out_dir / new_index["chapters"][3]["filename"]).is_file()
    assert (out_dir / new_index["chapters"][4]["filename"]).is_file()

    # Follow-up chapter_analysis_rerun + chapter_delta_rerun queued for new seqs.
    queued_types_seqs = sorted(
        (t["task_type"], t["params"]["seqs"][0])
        for t in app.state.db.list_tasks(pid)
        if t["task_type"] in {"chapter_analysis_rerun", "chapter_delta_rerun"}
        and t["id"] != append_task["id"]
    )
    assert queued_types_seqs == [
        ("chapter_analysis_rerun", 4), ("chapter_analysis_rerun", 5),
        ("chapter_delta_rerun", 4), ("chapter_delta_rerun", 5),
    ]
    assert append_result["new_seqs"] == [4, 5]

    # Whole-book analysis outputs go stale: chapter revision changed.
    post_rev = project_revisions(project_path)
    assert post_rev["chapter"] != pre_rev["chapter"]
    stale_task = {
        "task_type": "analysis",
        "params": {"task": "summary", "chapters": "all"},
        "input_revision": pre_rev,
    }
    from revisions import task_is_stale
    assert task_is_stale(stale_task, post_rev) is True

    # Release any fake follow-up tasks that started.
    for tid in list(state["gates"]):
        state["gates"][tid].set()


# === Scenario 6: governance rollback =======================================

# Reuses the proven governance fixtures from test_governance_task.py.  The
# adapter is driven through the real workbench manager (not called directly).

_GOV_EMPTY_BUCKETS = {k: [] for k in [
    "角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}


def _gov_empty_story(with_character: bool = False) -> dict:
    story: dict = {
        "介绍": {"标题": "治理集成测试", "描述": "结构校验失败回滚检查。"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    if with_character:
        story["角色集"].append({
            "名称": "约翰娜", "是否主角": False, "性别": 1, "年龄": 0,
            "生日": "0001-01-01T00:00:00", "所属阵营": [], "关系": [],
            "分组": "测试", "别名": [], "标签集": [], "介绍": "测试角色。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        })
    return story


def _gov_structure_breaking_correction(start: int, end: int) -> dict:
    """Passes validate_delta but fails validate_structure: swaps a character's
    首次章节/最近章节 so first > recent."""
    return {
        "章节范围": f"第{start:03d}章-第{end:03d}章",
        "新增元素": _GOV_EMPTY_BUCKETS,
        "修改元素": _GOV_EMPTY_BUCKETS,
        "治理操作": {
            "设置字段": [{
                "类型": "角色", "名称": "约翰娜", "字段": "详情",
                "值": {"首次章节": "0099", "最近章节": "0001",
                       "提取理由": "测试治理结构校验失败。"},
            }]
        },
    }


def _gov_seed_project(project: Path) -> None:
    story = _gov_empty_story(with_character=True)
    _write_json(project / "故事结构_增量.json", story)
    # Commit the seeded story as the seq-1 snapshot so the governance early-skip
    # guard (latest_snapshot_seq is None -> skipped_no_chapters) does not fire:
    # the seed represents "1 chapter already committed" and governance must run.
    _write_json(project / "故事结构版本" / "story_after_ch001.json", story)
    chapter = project / "原文拆解" / "第001章_测试.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
    analysis = ("# 第001章分析\n## 1. 剧情梗概\n本章围绕主角接到意外来信展开。\n"
                "## 2. 出场人物\n- 主角：阅读来信后从迟疑转为主动求证。\n")
    (project / "章节处理").mkdir(parents=True, exist_ok=True)
    (project / "章节处理" / chapter.name).write_text(analysis, encoding="utf-8")
    _write_json(project / "章节处理" / f"{chapter.stem}.json", {
        "章节": "第001章", "新增元素": _GOV_EMPTY_BUCKETS,
        "修改元素": _GOV_EMPTY_BUCKETS})
    for rel in [f"质量治理/delta校验/{chapter.stem}.json",
                f"质量治理/章节校验/{chapter.stem}.json"]:
        _write_json(project / rel, {"passed": True, "mode": "process",
                                    "errors": [], "warnings": []})


def test_governance_rollback_restores_before_snapshot(tmp_path):
    """A governance task whose correction fails post-apply structure validation
    restores the before snapshot; the process JSON is unchanged.  Driven
    through the real workbench manager with the real GovernanceTaskAdapter.
    """
    db_path = str(tmp_path / "gov_integration.db")
    # Real governance adapter + fake everything else.
    task_adapters = {"governance": GovernanceTaskAdapter()}
    app = create_app(db_path, "sekret", task_adapters=task_adapters)
    client = TestClient(app)
    try:
        project_path = tmp_path / "拆书_治理"
        _gov_seed_project(project_path)
        project = _create_project(client, project_path)

        from run_pipeline import audit_paths
        paths = audit_paths(project_path, 1, 1)
        _write_json(paths["correction"], _gov_structure_breaking_correction(1, 1))

        process_path = project_path / "故事结构_增量.json"
        before_obj = json.loads(process_path.read_text(encoding="utf-8"))

        response = client.post(
            f"/projects/{project['id']}/tasks",
            json={"type": "governance", "params": {"start": 1, "end": 1}},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        _wait(lambda: app.state.db.get_task(task_id)["status"] in {"done", "failed"})
        final = app.state.db.get_task(task_id)
        assert final["status"] == "failed", final.get("error")

        # Process JSON restored to the before snapshot.
        after_obj = json.loads(process_path.read_text(encoding="utf-8"))
        assert after_obj == before_obj
        for character in after_obj["角色集"]:
            if character["名称"] == "约翰娜":
                assert character["详情"]["首次章节"] != "0099"
    finally:
        app.state.workbench.shutdown()


# === Scenario 7: export task produces a readable artifact + result_json ====

def test_export_task_produces_readable_artifact_and_result_json(tmp_path):
    db_path = str(tmp_path / "export_integration.db")
    # Real export adapter; deterministic (no LLM).
    task_adapters = {"export": ExportAdapter()}
    app = create_app(db_path, "sekret", task_adapters=task_adapters)
    client = TestClient(app)
    try:
        project_path = tmp_path / "拆书_导出"
        _seed_chapters(project_path)
        _seed_minimal_story(project_path)
        project = _create_project(client, project_path)

        response = client.post(
            f"/projects/{project['id']}/tasks",
            json={"type": "export", "params": {"source": "final", "clean": True}},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        _wait(lambda: app.state.db.get_task(task_id)["status"] in {"done", "failed"})
        final = app.state.db.get_task(task_id)
        assert final["status"] == "done", final.get("error")
        result = final["result"]

        # result_json carries artifact paths + the export report.
        assert result["task"] == "export"
        assert result["source"] == "final"
        artifact_paths = [a["path"] for a in result["artifacts"]]
        assert any(Path(p).is_dir() for p in artifact_paths), artifact_paths
        export_report = Path(result["manifest_path"])
        assert export_report.is_file()
        # The on-disk report records the snapshot path; the adapter overrides
        # the in-memory manifest to point at the canonical project story JSON.
        assert result["manifest"]["source_file"] == str(
            project_path / "故事结构.json")
        assert isinstance(result["manifest"]["concept_count"], int)
        on_disk = json.loads(export_report.read_text(encoding="utf-8"))
        assert isinstance(on_disk["concept_count"], int)

        # The artifact endpoint can read one of the produced files.
        vault_dir = next(Path(p) for p in artifact_paths if Path(p).is_dir())
        sample = next(vault_dir.rglob("*.md"))
        rel = sample.relative_to(project_path).as_posix()
        art = client.get(
            f"/projects/{project['id']}/artifact", params={"path": rel},
            headers=AUTH,
        )
        assert art.status_code == 200, art.text
        assert art.json()["size"] > 0
    finally:
        app.state.workbench.shutdown()


# === Scenario 8: task event payloads carry task_id + project_id ============

def test_task_event_payloads_carry_task_id_and_project_id(app_client):
    client, app, state, tmp_path = app_client
    events: list[dict] = []
    app.state.hub.subscribe(events.append)
    project = _create_project(client, tmp_path / "测试书")
    pid = project["id"]
    _seed_chapters(tmp_path / "测试书")

    # A task that completes (queued -> started -> progress -> done).  Uses the
    # "analysis" reader adapter so it runs concurrently with nothing else here.
    done = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "analysis", "params": {"mode": "done"}}, headers=AUTH,
    ).json()
    _wait(lambda: _task_status(client, pid, done["id"]) == "done")

    # A task that fails immediately (mode="fail"; writer releases on raise).
    failed = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "fail"}}, headers=AUTH,
    ).json()
    _wait(lambda: _task_status(client, pid, failed["id"]) == "failed")

    # A running writer task that gets cancelled while running.
    blocking = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    _wait(lambda: _task_status(client, pid, blocking["id"]) == "running")
    client.post(f"/projects/{pid}/tasks/{blocking['id']}/cancel", headers=AUTH)
    _wait(lambda: _task_status(client, pid, blocking["id"]) == "cancelled")

    # A queued writer task cancelled before it ever starts: keep the writer
    # busy with a blocking task, queue a second one, cancel it while queued.
    running_block = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    _wait(lambda: _task_status(client, pid, running_block["id"]) == "running")
    queued = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}}, headers=AUTH,
    ).json()
    assert _task_status(client, pid, queued["id"]) == "queued"
    client.post(f"/projects/{pid}/tasks/{queued['id']}/cancel", headers=AUTH)
    _wait(lambda: _task_status(client, pid, queued["id"]) == "cancelled")
    client.post(f"/projects/{pid}/tasks/{running_block['id']}/cancel", headers=AUTH)
    _wait(lambda: _task_status(client, pid, running_block["id"]) == "cancelled")
    for gate in state["gates"].values():
        gate.set()

    # Every task_* event carries both task_id and project_id == pid.
    task_events = [e for e in events if e["type"].startswith("task_")]
    assert task_events
    for e in task_events:
        assert e.get("task_id"), e
        assert e.get("project_id") == pid

    done_types = [e["type"] for e in task_events if e["task_id"] == done["id"]]
    assert done_types == ["task_queued", "task_started", "task_progress", "task_done"]
    failed_types = [e["type"] for e in task_events if e["task_id"] == failed["id"]]
    assert failed_types[-1] == "task_failed"
    # The cancelled-while-running task emitted task_cancelled (not task_failed).
    blocking_types = [e["type"] for e in task_events if e["task_id"] == blocking["id"]]
    assert blocking_types[-1] == "task_cancelled"
    # The cancelled-while-queued task only saw queued + cancelled.
    queued_types = [e["type"] for e in task_events if e["task_id"] == queued["id"]]
    assert queued_types == ["task_queued", "task_cancelled"]

    # Events persisted to SQLite also carry task_id + project_id in data.
    persisted = client.get(f"/projects/{pid}/events", headers=AUTH).json()
    ptask = [e for e in persisted if e["type"].startswith("task_")]
    assert ptask
    for e in ptask:
        assert e["data"]["task_id"]
        assert e["data"]["project_id"] == pid


# === Scenario 9: Chinese names + Chinese log content =======================

def test_chinese_project_name_and_log_content_survives_the_stack(app_client):
    client, app, state, tmp_path = app_client
    chinese_name = "测试书·风云录"
    chinese_path = tmp_path / chinese_name
    project = _create_project(client, chinese_path, name=chinese_name)
    pid = project["id"]
    assert project["name"] == chinese_name
    _seed_chapters(chinese_path)

    # Subscribe BEFORE creating the task so we capture the progress event.
    events: list[dict] = []
    app.state.hub.subscribe(events.append)

    chinese_question = "主角为什么在面对旧承诺与新证据时犹豫？"
    task = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "analysis",
              "params": {"question": chinese_question, "mode": "block"}},
        headers=AUTH,
    ).json()
    _wait(lambda: task["id"] in state["gates"])

    progress_events = [e for e in events if e["type"] == "task_progress"
                       and e["task_id"] == task["id"]]
    assert progress_events
    # The fake adapter's Chinese progress detail round-trips through the hub.
    assert "工作台任务运行中" in progress_events[0]["detail"]

    state["gates"][task["id"]].set()
    _wait(lambda: _task_status(client, pid, task["id"]) == "done")

    # Chinese params survive the SQLite round-trip via the API.
    fetched = client.get(
        f"/projects/{pid}/tasks/{task['id']}", headers=AUTH
    ).json()
    assert fetched["params"]["question"] == chinese_question

    # Chinese project name survives list/projects.
    listed = client.get("/projects", headers=AUTH).json()
    assert any(p["name"] == chinese_name for p in listed)

    # Chinese path is preserved byte-for-byte (no mojibake).
    assert any(p["path"] == str(chinese_path.resolve()) for p in listed)

    # Persisted events retain Chinese detail text without replacement chars.
    persisted = client.get(f"/projects/{pid}/events", headers=AUTH).json()
    ptask = [e for e in persisted if e["type"] == "task_progress"
             and e.get("data", {}).get("task_id") == task["id"]]
    assert ptask
    assert "工作台任务运行中" in ptask[0]["detail"]
    assert "�" not in ptask[0]["detail"]
