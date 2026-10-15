"""Settings expose deployment policy without persisting runtime-only fields."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
from worker_admission import WorkerAdmission


@pytest.fixture
def client(tmp_path):
    return TestClient(app_module.create_app(str(tmp_path / "app.db"), "local-test"))


AUTH = {"Authorization": "Bearer local-test"}


def test_concurrency_policy_is_authenticated_and_read_only(client, monkeypatch):
    monkeypatch.setattr(app_module, "worker_admission", WorkerAdmission(2), raising=False)
    assert client.get("/settings/concurrency").status_code == 401
    response = client.get("/settings/concurrency", headers=AUTH)
    assert response.status_code == 200
    policy = response.json()
    assert policy["deployment_limit"] == 2
    assert policy["limit_source"] == "environment"
    assert policy["scope"] == "backend_process"
    assert policy["max_task_concurrency"] == 32
    assert client.get("/settings", headers=AUTH).json() == {}


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5", 33, None])
@pytest.mark.parametrize("field", ["analysis_concurrency", "per_chapter_concurrency"])
def test_invalid_concurrency_is_rejected_atomically(client, field, bad):
    initial = {"analysis_concurrency": 5, "per_chapter_concurrency": 10}
    assert client.put("/settings", json={"settings": initial}, headers=AUTH).status_code == 200
    response = client.put("/settings", json={"settings": {field: bad}}, headers=AUTH)
    assert response.status_code == 422
    assert client.get("/settings", headers=AUTH).json() == initial
    # Direct run/queue validation must enforce the same rule as saved settings.
    from models import RunConfig
    with pytest.raises(ValueError):
        RunConfig(**{field: bad})


def test_valid_concurrency_boundary_and_no_implicit_deployment_cap(client, monkeypatch):
    monkeypatch.setattr(app_module, "worker_admission", WorkerAdmission(), raising=False)
    response = client.put("/settings", json={"settings": {
        "analysis_concurrency": 32, "per_chapter_concurrency": 1,
    }}, headers=AUTH)
    assert response.status_code == 200
    policy = client.get("/settings/concurrency", headers=AUTH).json()
    assert policy["deployment_limit"] is None
    assert policy["limit_source"] == "task_config"
