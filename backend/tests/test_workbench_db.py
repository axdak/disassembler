import pytest
from pydantic import ValidationError

from db import Database
from models import RunConfig, WorkbenchTask


def make_db(tmp_path):
    return Database(tmp_path / "workbench.db")


def test_task_json_fields_round_trip_chinese_content(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "项目"))

    task = db.create_task(
        project["id"],
        "analysis",
        {"任务": "人物分析", "章节": ["第001章_相遇.md"]},
        {"chapter": "章节版本"},
    )
    db.update_task(task["id"], status="done", result={"结论": "主角成长明显"})

    saved = db.get_task(task["id"])
    assert saved["params"] == {"任务": "人物分析", "章节": ["第001章_相遇.md"]}
    assert saved["input_revision"] == {"chapter": "章节版本"}
    assert saved["result"] == {"结论": "主角成长明显"}


def test_tasks_are_listed_in_fifo_id_order(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    created = [
        db.create_task(project["id"], "analysis", {"order": n}, {})
        for n in range(3)
    ]

    tasks = db.list_tasks(project["id"], limit=10)

    assert [task["id"] for task in tasks] == [task["id"] for task in created]


def test_task_list_limit_keeps_latest_rows_in_timeline_order(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    created = [
        db.create_task(project["id"], "analysis", {"order": n}, {})
        for n in range(105)
    ]

    tasks = db.list_tasks(project["id"], limit=10)

    assert [task["id"] for task in tasks] == [task["id"] for task in created[-10:]]


def test_requesting_task_cancel_sets_persistent_cancel_flag(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    task = db.create_task(project["id"], "analysis", {}, {})

    assert db.request_task_cancel(task["id"]) is True
    assert db.get_task(task["id"])["cancel_requested"] is True


def test_force_rerun_creates_a_new_task_without_replacing_history(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    original = db.create_task(project["id"], "analysis", {"force": False}, {})

    rerun = db.create_task(project["id"], "analysis", {"force": True}, {}, retry_of=original["id"])

    assert rerun["id"] != original["id"]
    assert rerun["retry_of"] == original["id"]
    assert db.get_task(original["id"])["params"] == {"force": False}


def test_task_model_rejects_states_outside_the_workbench_lifecycle():
    with pytest.raises(ValidationError):
        WorkbenchTask(
            id=1,
            project_id=1,
            task_type="analysis",
            status="waiting",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )


def test_update_task_can_clear_a_previous_result(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    task = db.create_task(project["id"], "analysis", {}, {})
    db.update_task(task["id"], result={"answer": "present"})

    db.update_task(task["id"], result=None)

    assert db.get_task(task["id"])["result"] is None


def test_deleting_project_cascades_to_its_workbench_tasks(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    task = db.create_task(project["id"], "analysis", {}, {})

    db.delete_project(project["id"])

    assert db.get_task(task["id"]) is None


def test_restart_marks_running_tasks_interrupted_and_keeps_queued(tmp_path):
    db = make_db(tmp_path)
    project = db.create_project(str(tmp_path / "project"))
    running = db.create_task(project["id"], "analysis", {}, {})
    queued = db.create_task(project["id"], "analysis", {}, {})
    db.update_task(running["id"], status="running")

    interrupted = db.mark_running_tasks_interrupted()

    assert interrupted == [(running["id"], project["id"])]
    assert db.get_task(running["id"])["status"] == "interrupted"
    assert db.get_task(queued["id"])["status"] == "queued"


def test_automatic_run_queue_survives_database_reopen(tmp_path):
    db_path = tmp_path / "workbench.db"
    db = Database(db_path)
    project = db.create_project(str(tmp_path / "project"))
    config = RunConfig(worker_provider="codebuddy", worker_retries=4)
    created = db.create_queue_item(project["id"], config.model_dump())
    db.close()

    reopened = Database(db_path)
    items = reopened.list_queue_items()

    assert items == [{
        "id": created["id"],
        "project_id": project["id"],
        "config": config.model_dump(),
    }]
    reopened.close()
