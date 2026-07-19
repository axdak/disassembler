#!/usr/bin/env python3
"""GF6 wiring: 5 new task types are writers + registered + admittable via manager + HTTP endpoint."""
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
ENGINE = BACKEND / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))

from app import create_app
from db import Database
from task_adapters import (
    FinalizeAdapter,
    MigrateChronologyAdapter,
    NormalizeAdapter,
    RecoverStoryAdapter,
    RepairAdapter,
)
from workbench import WRITER_TASK_TYPES, WorkbenchTaskManager


NEW_TYPES = ("recover_story", "migrate_chronology", "finalize", "normalize", "repair")


def test_new_types_are_writers():
    for t in NEW_TYPES:
        assert t in WRITER_TASK_TYPES, f"{t} should be a writer task type"


def test_manager_admits_new_types(tmp_path):
    db = Database(tmp_path / "w.db")  # schema runs in __init__; no db.init()
    pid = db.create_project(str(tmp_path / "proj"), "proj")["id"]
    adapters = {
        "recover_story": RecoverStoryAdapter(),
        "migrate_chronology": MigrateChronologyAdapter(),
        "finalize": FinalizeAdapter(),
        "normalize": NormalizeAdapter(),
        "repair": RepairAdapter(),
    }

    class NoHub:
        def broadcast(self, *_):
            pass

    mgr = WorkbenchTaskManager(db, NoHub(), adapters, poll_interval=0.01)
    try:
        for t in adapters:
            task = mgr.create(pid, t, {})
            assert task["status"] == "queued"
            assert task["task_type"] == t
    finally:
        mgr.shutdown()


@pytest.fixture
def app_client(tmp_path):
    app = create_app(str(tmp_path / "gf6.db"), "sekret")
    client = TestClient(app)
    yield client, app
    app.state.workbench.shutdown()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer sekret"}


@pytest.mark.parametrize("task_type", NEW_TYPES)
def test_endpoint_admits_new_types(app_client, auth, task_type, tmp_path):
    client, _ = app_client
    project = client.post(
        "/projects", json={"path": str(tmp_path / task_type)}, headers=auth
    ).json()
    response = client.post(
        f"/projects/{project['id']}/tasks",
        json={"type": task_type, "params": {}},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["task_type"] == task_type
    assert task["status"] == "queued"


if __name__ == "__main__":
    test_new_types_are_writers()
    test_manager_admits_new_types(Path(tempfile.mkdtemp()))
    print("ok")
