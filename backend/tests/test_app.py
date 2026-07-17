import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from events import Event


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
    while time.time() < deadline and not (run_done(p1) and run_done(p2)):
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
