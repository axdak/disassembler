import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import create_app
from db import Database


TASK_TYPES = (
    "analysis",
    "chapter_visual",
    "chapter_structure",
    "governance",
    "export",
    "append_chapters",
    "chapter_analysis_rerun",
    "chapter_delta_rerun",
)


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class ApiAdapter:
    def __init__(self, state):
        self.state = state
        self.cancelled = threading.Event()

    def run(self, context):
        context.progress(40, "prepared")
        mode = context.params.get("mode", "done")
        if mode == "failed":
            error = RuntimeError("api adapter failed")
            error.result = {"evidence": "preserved"}
            raise error
        if mode == "block":
            gate = self.state["gates"].setdefault(context.task_id, threading.Event())
            while not gate.wait(0.01):
                if context.cancellation_event.is_set():
                    return {"cancelled": True}
        return {"mode": mode, "task_id": context.task_id}

    def cancel(self):
        self.cancelled.set()


@pytest.fixture
def api(tmp_path):
    state = {"gates": {}, "instances": []}

    def factory():
        adapter = ApiAdapter(state)
        state["instances"].append(adapter)
        return adapter

    app = create_app(
        str(tmp_path / "api.db"),
        "sekret",
        task_adapters={task_type: factory for task_type in TASK_TYPES},
    )
    events = []
    app.state.hub.subscribe(events.append)
    client = TestClient(app)
    yield client, state, events, tmp_path
    for gate in state["gates"].values():
        gate.set()
    app.state.workbench.shutdown()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer sekret"}


def create_project(client, auth, path):
    path.mkdir(parents=True, exist_ok=True)
    return client.post("/projects", json={"path": str(path)}, headers=auth).json()


def task_status(client, auth, pid, task_id):
    response = client.get(f"/projects/{pid}/tasks/{task_id}", headers=auth)
    return response.json()["status"] if response.status_code == 200 else None


def test_task_routes_require_authentication(api, auth):
    client, _, _, tmp_path = api
    project = create_project(client, auth, tmp_path / "project")

    assert client.post(
        f"/projects/{project['id']}/tasks", json={"type": "analysis", "params": {}}
    ).status_code == 401
    assert client.get(f"/projects/{project['id']}/tasks").status_code == 401
    assert client.get(f"/projects/{project['id']}/tasks/1").status_code == 401
    assert client.post(f"/projects/{project['id']}/tasks/1/cancel").status_code == 401
    assert client.post(f"/projects/{project['id']}/tasks/1/retry").status_code == 401


def test_create_list_detail_cancel_and_retry(api, auth):
    client, state, _, tmp_path = api
    project = create_project(client, auth, tmp_path / "project")
    pid = project["id"]

    created = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}},
        headers=auth,
    )
    assert created.status_code == 200, created.text
    task = created.json()
    assert task["task_type"] == "governance"
    assert task["stale"] is False
    wait_for(lambda: task_status(client, auth, pid, task["id"]) == "running")

    listed = client.get(f"/projects/{pid}/tasks", headers=auth).json()
    assert [item["id"] for item in listed] == [task["id"]]
    assert client.get(f"/projects/{pid}/tasks/{task['id']}", headers=auth).json()["params"] == {
        "mode": "block"
    }

    cancelled = client.post(
        f"/projects/{pid}/tasks/{task['id']}/cancel", headers=auth
    )
    assert cancelled.status_code == 200
    wait_for(lambda: task_status(client, auth, pid, task["id"]) == "cancelled")
    assert any(instance.cancelled.is_set() for instance in state["instances"])

    retried = client.post(
        f"/projects/{pid}/tasks/{task['id']}/retry", headers=auth
    )
    assert retried.status_code == 200
    retry = retried.json()
    assert retry["retry_of"] == task["id"]
    assert retry["id"] != task["id"]
    wait_for(lambda: task_status(client, auth, pid, retry["id"]) == "running")
    wait_for(lambda: retry["id"] in state["gates"])
    state["gates"][retry["id"]].set()
    wait_for(lambda: task_status(client, auth, pid, retry["id"]) == "done")


def test_project_task_ownership_is_hidden_with_404(api, auth):
    client, _, _, tmp_path = api
    p1 = create_project(client, auth, tmp_path / "p1")
    p2 = create_project(client, auth, tmp_path / "p2")
    task = client.post(
        f"/projects/{p1['id']}/tasks",
        json={"type": "analysis", "params": {}},
        headers=auth,
    ).json()

    for method, suffix in (
        (client.get, ""),
        (client.post, "/cancel"),
        (client.post, "/retry"),
    ):
        response = method(
            f"/projects/{p2['id']}/tasks/{task['id']}{suffix}", headers=auth
        )
        assert response.status_code == 404
    assert client.get("/projects/99999/tasks", headers=auth).status_code == 404
    assert client.post(
        "/projects/99999/tasks",
        json={"type": "analysis", "params": {}},
        headers=auth,
    ).status_code == 404


@pytest.mark.parametrize("task_type", TASK_TYPES)
def test_all_exact_task_type_literals_are_accepted(api, auth, task_type, tmp_path):
    client, _, _, _ = api
    project = create_project(client, auth, tmp_path / task_type)
    response = client.post(
        f"/projects/{project['id']}/tasks",
        json={"type": task_type, "params": {}},
        headers=auth,
    )
    assert response.status_code == 200, response.text


def test_invalid_type_and_non_object_params_are_rejected(api, auth):
    client, _, _, tmp_path = api
    project = create_project(client, auth, tmp_path / "project")
    url = f"/projects/{project['id']}/tasks"

    assert client.post(
        url, json={"type": "unknown", "params": {}}, headers=auth
    ).status_code == 422
    assert client.post(
        url, json={"type": "analysis", "params": ["not", "an", "object"]}, headers=auth
    ).status_code == 422


def test_task_stale_is_computed_from_current_project_revision(api, auth):
    client, _, _, tmp_path = api
    project_path = tmp_path / "project"
    source = project_path / "\u539f\u6587\u62c6\u89e3"
    source.mkdir(parents=True)
    chapter = source / "chapter-1.md"
    chapter.write_text("first", encoding="utf-8")
    index = source / "_\u7d22\u5f15.json"
    index.write_text(
        json.dumps({"chapters": [{"seq": 1, "filename": chapter.name}]}), encoding="utf-8"
    )
    project = create_project(client, auth, project_path)
    task = client.post(
        f"/projects/{project['id']}/tasks",
        json={"type": "analysis", "params": {}},
        headers=auth,
    ).json()
    wait_for(lambda: task_status(client, auth, project["id"], task["id"]) == "done")
    assert client.get(
        f"/projects/{project['id']}/tasks/{task['id']}", headers=auth
    ).json()["stale"] is False

    index.write_text(
        json.dumps({"chapters": [{"seq": 1, "filename": chapter.name, "title": "changed"}]}),
        encoding="utf-8",
    )
    assert client.get(
        f"/projects/{project['id']}/tasks/{task['id']}", headers=auth
    ).json()["stale"] is True


def test_task_events_cover_done_failed_queued_and_cancelled(api, auth):
    client, _, hub_events, tmp_path = api
    project = create_project(client, auth, tmp_path / "project")
    pid = project["id"]
    done = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "analysis", "params": {}},
        headers=auth,
    ).json()
    failed = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "analysis", "params": {"mode": "failed"}},
        headers=auth,
    ).json()
    blocking = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"mode": "block"}},
        headers=auth,
    ).json()
    wait_for(lambda: task_status(client, auth, pid, blocking["id"]) == "running")
    queued = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {}},
        headers=auth,
    ).json()
    client.post(f"/projects/{pid}/tasks/{queued['id']}/cancel", headers=auth)
    client.post(f"/projects/{pid}/tasks/{blocking['id']}/cancel", headers=auth)

    wait_for(lambda: task_status(client, auth, pid, done["id"]) == "done")
    wait_for(lambda: task_status(client, auth, pid, failed["id"]) == "failed")
    wait_for(lambda: task_status(client, auth, pid, queued["id"]) == "cancelled")
    wait_for(lambda: task_status(client, auth, pid, blocking["id"]) == "cancelled")
    wait_for(lambda: any(
        event.get("task_id") == blocking["id"] and event["type"] == "task_cancelled"
        for event in hub_events
    ))

    by_task = {
        task_id: [event["type"] for event in hub_events if event.get("task_id") == task_id]
        for task_id in (done["id"], failed["id"], queued["id"], blocking["id"])
    }
    assert by_task[done["id"]] == [
        "task_queued", "task_started", "task_progress", "task_done"
    ]
    assert by_task[failed["id"]][-1] == "task_failed"
    assert by_task[queued["id"]] == ["task_queued", "task_cancelled"]
    assert by_task[blocking["id"]][-1] == "task_cancelled"
    assert all(
        event.get("task_id") and event.get("project_id") == pid
        for event in hub_events
        if event["type"].startswith("task_")
    )

    persisted = client.get(f"/projects/{pid}/events", headers=auth).json()
    task_events = [event for event in persisted if event["type"].startswith("task_")]
    assert task_events
    assert all(event["data"]["task_id"] and event["data"]["project_id"] == pid for event in task_events)


def test_restart_interrupts_running_tasks_and_keeps_queued_eligible(tmp_path, auth):
    db_path = str(tmp_path / "restart.db")
    state = {"gates": {}, "instances": []}

    def factory():
        adapter = ApiAdapter(state)
        state["instances"].append(adapter)
        return adapter

    project_path = tmp_path / "project"
    project_path.mkdir()
    old_db = Database(db_path)
    project = old_db.create_project(str(project_path))
    running = old_db.create_task(project["id"], "governance", {}, {})
    queued = old_db.create_task(project["id"], "governance", {}, {})
    old_db.update_task(running["id"], status="running")
    old_db.close()

    app2 = create_app(db_path, "sekret", task_adapters={"governance": factory})
    client2 = TestClient(app2)
    try:
        assert app2.state.db.get_task(running["id"])["status"] == "interrupted"
        wait_for(lambda: app2.state.db.get_task(queued["id"])["status"] == "running")
    finally:
        for gate in state["gates"].values():
            gate.set()
        app2.state.workbench.shutdown()
