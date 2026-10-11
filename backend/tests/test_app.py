import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from db import Database
from events import Event
from models import RunConfig


class FakeOrchestrator:
    """可注入的假编排器：发几个事件后返回，避免真实 Agy/引擎执行。"""

    mode = "ok"  # "ok" | "cancel"

    def __init__(self, project_dir, config, bus):
        self.project_dir = project_dir
        self.config = config
        self.bus = bus
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        self.bus.emit(Event("pipeline_start", stage="chapter_analysis"))
        if self.mode == "cancel":
            self._cancel.wait(timeout=5.0)
            self.bus.emit(Event("pipeline_cancelled"))
            return 2
        self.bus.emit(Event("stage_done", stage="chapter_analysis"))
        self.bus.emit(Event("pipeline_done"))
        return 0


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "app.db"), "sekret", FakeOrchestrator)
    return TestClient(app)


@pytest.fixture
def auth():
    return {"Authorization": "Bearer sekret"}


def _wait_done(client, pid, auth, timeout=5.0):
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = client.get(f"/projects/{pid}", headers=auth).json()
        if not st["active_run"]:
            return st
        time.sleep(0.05)
    return st


def test_health_no_auth(client):
    assert client.get("/health").json() == {"ok": True}


def test_auth_required(client, auth):
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers=auth).status_code == 200


def test_project_crud(client, auth, tmp_path):
    r = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth)
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.get("/projects", headers=auth).json()[0]["id"] == pid
    st = client.get(f"/projects/{pid}", headers=auth).json()
    assert st["project"]["id"] == pid
    assert st["current_stage"]  # 新项目 -> 第一阶段
    assert client.delete(f"/projects/{pid}", headers=auth).status_code == 200
    assert client.get(f"/projects/{pid}", headers=auth).status_code == 404


def test_run_range_is_saved_per_project_and_survives_database_reopen(client, auth, tmp_path):
    first = client.post("/projects", json={"path": str(tmp_path / "first")}, headers=auth).json()["id"]
    second = client.post("/projects", json={"path": str(tmp_path / "second")}, headers=auth).json()["id"]

    saved = client.put(
        f"/projects/{first}/run-range", json={"end_chapter_seq": 12}, headers=auth
    )
    assert saved.status_code == 200
    assert saved.json()["run_end_chapter_seq"] == 12
    assert client.get(f"/projects/{first}", headers=auth).json()["project"]["run_end_chapter_seq"] == 12
    assert client.get(f"/projects/{second}", headers=auth).json()["project"]["run_end_chapter_seq"] == 0
    assert client.put(f"/projects/{first}/run-range", json={"end_chapter_seq": -1}, headers=auth).status_code == 422
    assert client.put("/projects/999999/run-range", json={"end_chapter_seq": 1}, headers=auth).status_code == 404

    # A fresh connection models an app restart without interrupting the test backend.
    reopened = Database(client.app.state.db.path)
    try:
        assert reopened.get_project(first)["run_end_chapter_seq"] == 12
    finally:
        reopened.close()


def test_existing_project_table_gains_run_range_column(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
            "path TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO projects(name, path, created_at, updated_at) VALUES ('旧项目', 'book', '', '')"
        )
        conn.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
            "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, rc INTEGER, config_json TEXT)"
        )
        conn.execute(
            "INSERT INTO runs(id, project_id, status, started_at, config_json) "
            "VALUES (1, 1, 'done', '', '{\"end_chapter_seq\": 7}')"
        )
    migrated = Database(db_path)
    try:
        assert migrated.get_project(1)["run_end_chapter_seq"] == 7
        assert migrated.set_project_run_end_chapter_seq(1, 9)["run_end_chapter_seq"] == 9
    finally:
        migrated.close()


def test_run_lifecycle_and_events(client, auth, tmp_path):
    pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]
    rid = client.post(f"/projects/{pid}/run", json={}, headers=auth).json()["run_id"]
    assert rid > 0
    st = _wait_done(client, pid, auth)
    assert st["active_run"] is None
    events = client.get(f"/projects/{pid}/events", headers=auth).json()
    types = [e["type"] for e in events]
    assert "pipeline_start" in types
    assert "pipeline_done" in types
    assert "run_status" in types


def test_run_config_merges_settings(client, auth, tmp_path):
    # 前端通常传空 config，settings 必须作为默认值注入，且不被 RunConfig 默认值覆盖。
    client.put("/settings", json={"settings": {
        "chapter_mode": "interleaved",
        "analysis_concurrency": 5,
    }}, headers=auth)
    pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]

    # 空 config：应使用 settings 的 interleaved/5
    rid = client.post(f"/projects/{pid}/run", json={"config": {}}, headers=auth).json()["run_id"]
    _wait_done(client, pid, auth)
    cfg = client.app.state.db.get_run(rid)["config"]
    assert cfg["chapter_mode"] == "interleaved"
    assert cfg["analysis_concurrency"] == 5

    # 显式字段覆盖 settings，未覆盖字段仍沿用 settings
    rid2 = client.post(f"/projects/{pid}/run", json={"config": {"chapter_mode": "two_phase"}}, headers=auth).json()["run_id"]
    _wait_done(client, pid, auth)
    cfg2 = client.app.state.db.get_run(rid2)["config"]
    assert cfg2["chapter_mode"] == "two_phase"
    assert cfg2["analysis_concurrency"] == 5


def test_run_rejects_when_already_running(client, auth, tmp_path):
    FakeOrchestrator.mode = "cancel"
    try:
        pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]
        client.post(f"/projects/{pid}/run", json={}, headers=auth)
        r = client.post(f"/projects/{pid}/run", json={}, headers=auth)
        assert r.status_code == 409
        client.post(f"/projects/{pid}/cancel", headers=auth)
        _wait_done(client, pid, auth)
    finally:
        FakeOrchestrator.mode = "ok"


def test_cancel_run(client, auth, tmp_path):
    FakeOrchestrator.mode = "cancel"
    try:
        pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]
        client.post(f"/projects/{pid}/run", json={}, headers=auth)
        time.sleep(0.1)
        assert client.post(f"/projects/{pid}/cancel", headers=auth).json()["cancelled"] is True
        _wait_done(client, pid, auth)
        events = client.get(f"/projects/{pid}/events", headers=auth).json()
        assert "pipeline_cancelled" in [e["type"] for e in events]
    finally:
        FakeOrchestrator.mode = "ok"


def test_settings(client, auth):
    assert client.get("/settings", headers=auth).json() == {}
    r = client.put("/settings", json={"settings": {"worker_provider": "codebuddy"}}, headers=auth)
    assert r.json()["worker_provider"] == "codebuddy"
    assert client.get("/settings", headers=auth).json()["worker_provider"] == "codebuddy"


def test_model_limits_validate_merged_settings_and_preserve_previous_on_error(client, auth):
    """A partial save must use the stored window and reject the whole bad update."""
    saved = dict(model_input_tokens=800000, model_context_tokens=900000,
                 model_output_tokens=400000, model_reserve_tokens=10000,
                 worker_timeout_seconds=77, worker_retries=0)
    assert client.put('/settings', json={'settings': saved}, headers=auth).status_code == 200
    # This output would be invalid against defaults, but valid against the saved window.
    assert client.put('/settings', json={'settings': {'model_output_tokens': 600000}}, headers=auth).status_code == 200
    before = client.get('/settings', headers=auth).json()
    bad = client.put('/settings', json={'settings': {'model_context_tokens': 500000,
                                                    'worker_retries': 9}}, headers=auth)
    assert bad.status_code == 422
    assert client.get('/settings', headers=auth).json() == before
    assert client.put('/settings', json={'settings': {'model_input_tokens': 1.5}}, headers=auth).status_code == 422
    assert client.put('/settings', json={'settings': {'unknown_capacity': 99}}, headers=auth).status_code == 422


def test_run_capacity_override_uses_saved_window(client, auth, tmp_path):
    client.put('/settings', json={'settings': {'model_context_tokens': 900000}}, headers=auth)
    project = client.post('/projects', json={'path': str(tmp_path / 'large-window')}, headers=auth).json()
    response = client.post(f"/projects/{project['id']}/run",
                           json={'config': {'model_output_tokens': 400000}}, headers=auth)
    assert response.status_code == 200
    _wait_done(client, project['id'], auth)
    response = client.post(f"/projects/{project['id']}/run",
                           json={'config': {'unknown_capacity': 99}}, headers=auth)
    assert response.status_code == 422


def test_settings_reject_invalid_values_without_persisting(client, auth):
    """设置必须在落库前校验，避免无效 provider/负数参数延迟到运行期爆炸。"""
    invalid_provider = client.put(
        "/settings", json={"settings": {"worker_provider": "claude"}}, headers=auth
    )
    assert invalid_provider.status_code == 422
    assert "worker_provider" not in client.get("/settings", headers=auth).json()

    invalid_retries = client.put(
        "/settings", json={"settings": {"worker_retries": -1}}, headers=auth
    )
    assert invalid_retries.status_code == 422
    assert "worker_retries" not in client.get("/settings", headers=auth).json()


def test_run_reports_historical_invalid_setting_as_422(client, auth, tmp_path):
    """历史数据库中的坏值应给出可诊断响应，而不是触发未处理的服务端异常。"""
    client.app.state.db.set_setting("worker_timeout_seconds", 0)
    pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]

    response = client.post(f"/projects/{pid}/run", json={"config": {}}, headers=auth)

    assert response.status_code == 422
    assert "已保存的运行设置无效" in response.json()["detail"]


def test_hub_broadcasts_events(client, auth, tmp_path):
    pid = client.post("/projects", json={"path": str(tmp_path / "proj")}, headers=auth).json()["id"]
    received = []
    client.app.state.hub.subscribe(lambda e: received.append(e))
    client.post(f"/projects/{pid}/run", json={}, headers=auth)
    _wait_done(client, pid, auth)
    types = [e["type"] for e in received]
    assert "pipeline_start" in types
    assert "run_status" in types


def test_ws_accepts_valid_token(client):
    # 进入 with 即完成握手并被 accept；错误 token 会在进入时抛异常
    with client.websocket_connect("/ws/events?token=sekret"):
        pass


# ---- 章节状态 / 产物预览 / 日志 ----

def _make_project_with_chapters(root):
    (root / "原文拆解").mkdir(parents=True)
    (root / "章节处理").mkdir(parents=True)
    (root / "原文拆解" / "_索引.json").write_text(
        json.dumps({"chapters": [{"seq": 1, "filename": "第001章_测试.md", "title": "测试"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "章节处理" / "第001章_测试.md").write_text("# analysis", encoding="utf-8")


def test_chapters_status(client, auth, tmp_path):
    proj = tmp_path / "proj"
    _make_project_with_chapters(proj)
    pid = client.post("/projects", json={"path": str(proj)}, headers=auth).json()["id"]
    ch = client.get(f"/projects/{pid}/chapters", headers=auth).json()[0]
    assert ch["seq"] == 1
    assert ch["has_analysis"] is True
    assert ch["has_delta"] is False


def test_artifact_preview_and_traversal(client, auth, tmp_path):
    proj = tmp_path / "proj"
    (proj / "全书分析").mkdir(parents=True)
    (proj / "全书分析" / "note.md").write_text("# hi", encoding="utf-8")
    pid = client.post("/projects", json={"path": str(proj)}, headers=auth).json()["id"]
    r = client.get(f"/projects/{pid}/artifact", params={"path": "全书分析/note.md"}, headers=auth)
    assert r.status_code == 200
    assert "# hi" in r.json()["content"]
    # 目录穿越被拒绝
    r = client.get(f"/projects/{pid}/artifact", params={"path": "../../etc/passwd"}, headers=auth)
    assert r.status_code == 404


def test_logs(client, auth, tmp_path):
    proj = tmp_path / "proj"
    ldir = proj / "质量治理" / "worker日志"
    ldir.mkdir(parents=True)
    (ldir / "analysis_20260101_000000.txt").write_text("worker log content", encoding="utf-8")
    pid = client.post("/projects", json={"path": str(proj)}, headers=auth).json()["id"]
    lst = client.get(f"/projects/{pid}/logs", headers=auth).json()
    assert lst and lst[0]["name"].endswith(".txt")
    r = client.get(f"/projects/{pid}/logs/analysis_20260101_000000.txt", headers=auth)
    assert "worker log content" in r.json()["content"]


# ---- 单任务队列 ----

def test_queue_runs_projects_sequentially(client, auth, tmp_path):
    p1 = client.post("/projects", json={"path": str(tmp_path / "p1")}, headers=auth).json()["id"]
    p2 = client.post("/projects", json={"path": str(tmp_path / "p2")}, headers=auth).json()["id"]
    assert client.post("/queue", json={"project_id": p1}, headers=auth).json()["position"] == 1
    assert client.post("/queue", json={"project_id": p2}, headers=auth).json()["position"] == 2
    assert len(client.get("/queue", headers=auth).json()) == 2
    assert client.post("/queue/start", headers=auth).json()["started"] is True

    def run_done(pid):
        ev = client.get(f"/projects/{pid}/events", headers=auth).json()
        return any(e["type"] == "run_status" and e["detail"] == "done" for e in ev)

    deadline = time.time() + 10
    # Run completion and queue consumption are separate asynchronous steps.
    # Wait for both within the same deadline instead of racing the queue thread.
    while time.time() < deadline and not (
        run_done(p1) and run_done(p2) and client.get("/queue", headers=auth).json() == []
    ):
        time.sleep(0.1)
    assert run_done(p1) and run_done(p2)
    assert client.get("/queue", headers=auth).json() == []  # 队列耗尽


def test_queue_remove(client, auth, tmp_path):
    p1 = client.post("/projects", json={"path": str(tmp_path / "p1")}, headers=auth).json()["id"]
    p2 = client.post("/projects", json={"path": str(tmp_path / "p2")}, headers=auth).json()["id"]
    client.post("/queue", json={"project_id": p1}, headers=auth)
    client.post("/queue", json={"project_id": p2}, headers=auth)
    assert client.delete("/queue/1", headers=auth).json()["removed"] is True
    assert client.get("/queue", headers=auth).json()[0]["project_id"] == p2


def test_queue_rejects_missing_project(client, auth):
    response = client.post("/queue", json={"project_id": 999999}, headers=auth)
    assert response.status_code == 404


def test_stale_run_marked_interrupted_on_restart(tmp_path):
    db_path = str(tmp_path / "app.db")
    auth = {"Authorization": "Bearer sekret"}
    app1 = create_app(db_path, "sekret", FakeOrchestrator)
    c1 = TestClient(app1)
    pid = c1.post("/projects", json={"path": str(tmp_path / "p")}, headers=auth).json()["id"]
    # 模拟一条残留的 running 运行（无活动线程，像崩溃/关闭后遗留）
    app1.state.db.create_run(pid, {})
    # 重启：同一 db 新建 app
    app2 = create_app(db_path, "sekret", FakeOrchestrator)
    c2 = TestClient(app2)
    st = c2.get(f"/projects/{pid}", headers=auth).json()
    assert st["active_run"] is None  # 僵尸 running 已清理，UI 不再被锁
    ev = c2.get(f"/projects/{pid}/events", headers=auth).json()
    assert any(e["type"] == "run_status" and e["detail"] == "interrupted" for e in ev)


def test_persisted_automatic_queue_resumes_without_user_action(tmp_path):
    db_path = tmp_path / "app.db"
    project_path = tmp_path / "resume-project"
    project_path.mkdir()
    db = Database(db_path)
    project = db.create_project(str(project_path))
    db.create_queue_item(project["id"], RunConfig().model_dump())
    db.close()

    app = create_app(str(db_path), "sekret", FakeOrchestrator)
    client = TestClient(app)
    auth = {"Authorization": "Bearer sekret"}
    deadline = time.time() + 5
    while time.time() < deadline and client.get("/queue", headers=auth).json():
        time.sleep(0.05)

    assert client.get("/queue", headers=auth).json() == []
    events = client.get(f"/projects/{project['id']}/events", headers=auth).json()
    assert any(e["type"] == "run_status" and e["detail"] == "done" for e in events)


def test_from_book_creates_project(client, auth, tmp_path):
    book = tmp_path / "book.txt"
    book.write_text("第一章 测试\n正文一\n\n第二章 测试二\n正文二\n", encoding="utf-8")
    r = client.post("/projects/from-book", json={"source_file": str(book)}, headers=auth)
    assert r.status_code == 200, r.text
    pdir = Path(r.json()["path"])
    idx = pdir / "原文拆解" / "_索引.json"
    assert idx.is_file()
    chapters = json.loads(idx.read_text(encoding="utf-8")).get("chapters", [])
    assert len(chapters) >= 2


def test_from_book_is_idempotent_for_registered_project(client, auth, tmp_path):
    book = tmp_path / "book.txt"
    book.write_text("第一章 测试\n原始正文\n", encoding="utf-8")
    first = client.post("/projects/from-book", json={"source_file": str(book)}, headers=auth)
    assert first.status_code == 200
    marker = Path(first.json()["path"]) / "用户标记.txt"
    marker.write_text("do not overwrite", encoding="utf-8")

    second = client.post("/projects/from-book", json={"source_file": str(book)}, headers=auth)

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert marker.read_text(encoding="utf-8") == "do not overwrite"


def test_from_book_rejects_unregistered_nonempty_target(client, auth, tmp_path):
    book = tmp_path / "book.txt"
    book.write_text("第一章 测试\n正文\n", encoding="utf-8")
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("keep", encoding="utf-8")

    response = client.post(
        "/projects/from-book",
        json={"source_file": str(book), "project_dir": str(target)},
        headers=auth,
    )

    assert response.status_code == 409
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_delete_rejects_project_queued_for_automatic_run(client, auth, tmp_path):
    pid = client.post(
        "/projects", json={"path": str(tmp_path / "queued-project")}, headers=auth
    ).json()["id"]
    client.post("/queue", json={"project_id": pid}, headers=auth)

    response = client.delete(f"/projects/{pid}", headers=auth)

    assert response.status_code == 409
    assert client.get(f"/projects/{pid}", headers=auth).status_code == 200
