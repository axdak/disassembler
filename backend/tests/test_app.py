import threading
import time

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
