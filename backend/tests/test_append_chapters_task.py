"""Append-only chapter update endpoint + adapter tests (Task 4 Step 4).

Asserts:
- preview endpoint is read-only (returns plan, writes nothing)
- apply endpoint queues an ``append_chapters`` writer task
- successful apply changes the chapter revision, leaves old chapter artifacts
  current, marks whole-book analysis outputs stale, and queues only the new
  chapter production work (chapter_analysis_rerun + chapter_delta_rerun) for
  the new seqs.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from db import Database
from task_adapters import AppendChaptersTaskAdapter
from workbench import TaskContext


AUTH = {"Authorization": "Bearer sekret"}


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def _progress(*args, **kwargs):
    pass


def _make_context(project_path: Path, params: dict) -> TaskContext:
    return TaskContext(
        task_id=1,
        project_id=1,
        project_path=Path(project_path),
        params=params,
        cancellation_event=threading.Event(),
        _progress_callback=_progress,
    )


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create a project with three already-split chapters."""
    project = tmp_path / "拆书_测试"
    out_dir = project / "原文拆解"
    out_dir.mkdir(parents=True)
    chapters = [(1, "序章"), (2, "相遇"), (3, "冲突")]
    index = {"chapters": []}
    for seq, title in chapters:
        fname = f"第{seq:03d}章_{title}.md"
        (out_dir / fname).write_text(f"# 第{seq}章 {title}\n内容{seq}\n", encoding="utf-8")
        index["chapters"].append({
            "seq": seq, "filename": fname, "title": f"第{seq}章 {title}",
            "source_title": f"第{seq}章 {title}",
        })
    index["source_file"] = str(tmp_path / "old_book.txt")
    index["pattern"] = r"^第\d+章"
    index["chapter_count"] = 3
    (out_dir / "_索引.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (project / "故事结构_增量.json").write_text("{}", encoding="utf-8")
    return project, out_dir


# --- Adapter level (real on-disk behavior) --------------------------------

def test_append_adapter_writes_only_new_chapters_and_enqueues_followups(tmp_path):
    project, out_dir = _seed_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 相遇\n内容2\n第3章 冲突\n内容3\n"
        "第4章 续写\n内容4\n第5章 续写二\n内容5\n",
        encoding="utf-8",
    )

    from split_chapters_append import plan_append
    plan = plan_append(str(new_src), str(out_dir))

    enqueued: list[tuple[int, str, dict]] = []

    class FakeManager:
        def create(self, project_id, task_type, params):
            enqueued.append((project_id, task_type, dict(params)))
            return {"id": len(enqueued), "project_id": project_id, "task_type": task_type,
                    "params": params, "status": "queued"}

    adapter = AppendChaptersTaskAdapter(manager=FakeManager())
    ctx = _make_context(project, {"plan": plan})
    result = adapter.run(ctx)

    # Only the new md files exist; old md untouched.
    assert (out_dir / "第004章_续写.md").is_file() or any(
        p.name.startswith("第004章") for p in out_dir.glob("*.md")
    )
    for seq in (1, 2, 3):
        names = [p.name for p in out_dir.glob(f"第{seq:03d}章_*.md")]
        assert names, f"old chapter {seq} md must still exist"

    # Index updated with new source_file + 5 chapters.
    new_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    assert new_index["source_file"] == str(new_src.resolve())
    assert [c["seq"] for c in new_index["chapters"]] == [1, 2, 3, 4, 5]

    # Follow-up work queued only for new seqs (4, 5), analysis + delta each.
    assert sorted(e[1] for e in enqueued) == [
        "chapter_analysis_rerun", "chapter_analysis_rerun",
        "chapter_delta_rerun", "chapter_delta_rerun",
    ]
    new_seqs = sorted(
        e[2]["seqs"][0] for e in enqueued if e[1] == "chapter_analysis_rerun"
    )
    assert new_seqs == [4, 5]
    # Revision captured reflects the new chapter index.
    assert "chapter" in result["revision"]


# --- Endpoint level -------------------------------------------------------

@pytest.fixture
def app_client(tmp_path):
    """Create the real app with default (real) adapters."""
    db_path = str(tmp_path / "append.db")
    app = create_app(db_path, "sekret")
    client = TestClient(app)
    yield client, tmp_path, app
    app.state.workbench.shutdown()


def _create_project(client, path: Path) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    return client.post("/projects", json={"path": str(path)}, headers=AUTH).json()


def test_append_preview_endpoint_is_read_only(app_client):
    client, tmp_path, _ = app_client
    project, out_dir = _seed_project(tmp_path / "project")
    project_record = _create_project(client, project)

    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 相遇\n内容2\n第3章 冲突\n内容3\n"
        "第4章 续写\n内容4\n",
        encoding="utf-8",
    )
    before = {p.name: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}

    response = client.post(
        f"/projects/{project_record['id']}/chapters/append-preview",
        json={"source_file": str(new_src)},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["appended"]
    assert [c["seq"] for c in plan["appended"]] == [4]
    assert plan["conflict"] is None

    # Read-only: nothing on disk changed.
    after = {p.name: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}
    assert before == after


def test_append_apply_endpoint_queues_writer_and_updates_index(app_client):
    client, tmp_path, app = app_client
    project, out_dir = _seed_project(tmp_path / "project")
    project_record = _create_project(client, project)

    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 相遇\n内容2\n第3章 冲突\n内容3\n"
        "第4章 续写\n内容4\n",
        encoding="utf-8",
    )
    plan = client.post(
        f"/projects/{project_record['id']}/chapters/append-preview",
        json={"source_file": str(new_src)}, headers=AUTH,
    ).json()

    old_files = {p.name: p.read_bytes() for p in out_dir.glob("*.md")}
    old_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))

    response = client.post(
        f"/projects/{project_record['id']}/chapters/apply",
        json={"plan": plan}, headers=AUTH,
    )
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["task_type"] == "append_chapters"
    assert queued["status"] == "queued"

    _wait(lambda: app.state.db.get_task(queued["id"])["status"] in {"done", "failed"})

    final = app.state.db.get_task(queued["id"])
    assert final["status"] == "done", final.get("error")

    # Old chapter md files untouched.
    for name, content in old_files.items():
        assert (out_dir / name).read_bytes() == content

    # Index source_file updated to the new book; new chapter appended.
    new_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    assert new_index["source_file"] == str(new_src.resolve())
    assert new_index["source_file"] != old_index["source_file"]
    assert [c["seq"] for c in new_index["chapters"]] == [1, 2, 3, 4]

    # Whole-book analysis tasks are now stale (chapter revision changed).
    from revisions import task_is_stale, project_revisions
    current = project_revisions(project)
    # A whole-book analysis task captured against the OLD chapter revision is stale.
    old_chapter_revision = {"chapter": old_index["source_file"]}
    stale_task = {
        "task_type": "analysis",
        "params": {"task": "summary", "chapters": "all"},
        "input_revision": old_chapter_revision,
    }
    assert task_is_stale(stale_task, current) is True
    # A fresh task captured against the NEW revision is not stale.
    fresh_task = {
        "task_type": "analysis",
        "params": {"task": "summary", "chapters": "all"},
        "input_revision": current,
    }
    assert task_is_stale(fresh_task, current) is False

    # Follow-up chapter production work queued for new seqs only.
    followups = [
        t for t in app.state.db.list_tasks(project_record["id"])
        if t["task_type"] in {"chapter_analysis_rerun", "chapter_delta_rerun"}
        and t["id"] != queued["id"]
    ]
    assert followups, "expected follow-up rerun tasks for new seqs"
    new_analysis_seqs = sorted(
        t["params"]["seqs"][0] for t in followups
        if t["task_type"] == "chapter_analysis_rerun"
    )
    assert new_analysis_seqs == [4]


def test_append_adapter_confines_split_dir_to_project(tmp_path):
    """A tampered/stale plan whose split_dir points outside the project must
    NOT escape: the adapter overrides split_dir to the canonical project
    chapter dir so the writer lock actually covers the index being mutated.
    """
    project, out_dir = _seed_project(tmp_path / "project")
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 相遇\n内容2\n第3章 冲突\n内容3\n"
        "第4章 续写\n内容4\n",
        encoding="utf-8",
    )

    from split_chapters_append import plan_append
    # Build the plan against the real out_dir, then tamper: redirect
    # split_dir to a dir OUTSIDE the project.
    plan = plan_append(str(new_src), str(out_dir))
    tampered_dir = tmp_path / "tampered_split_dir"
    tampered_dir.mkdir(parents=True)
    plan["split_dir"] = str(tampered_dir)
    # Sanity: the tampered dir is outside the project.
    assert not Path(tampered_dir).resolve().is_relative_to(
        Path(project).resolve()
    )

    class FakeManager:
        def create(self, project_id, task_type, params):
            return {"id": 1, "project_id": project_id, "task_type": task_type,
                    "params": params, "status": "queued"}

    adapter = AppendChaptersTaskAdapter(manager=FakeManager())
    ctx = _make_context(project, {"plan": plan})
    result = adapter.run(ctx)

    # New chapter md + updated index land under the project's 原文拆解.
    assert (out_dir / "_索引.json").is_file()
    new_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    assert [c["seq"] for c in new_index["chapters"]] == [1, 2, 3, 4]
    assert new_index["source_file"] == str(new_src.resolve())
    assert any(p.name.startswith("第004章") for p in out_dir.glob("*.md"))
    assert "index_path" in result["manifest"]
    assert Path(result["manifest"]["index_path"]).resolve().is_relative_to(
        Path(project).resolve()
    )

    # Nothing was written under the tampered split_dir.
    assert list(tampered_dir.iterdir()) == [], (
        "tampered split_dir must not receive any writes"
    )


def test_append_apply_rejects_conflict_plan(app_client):
    client, tmp_path, app = app_client
    project, out_dir = _seed_project(tmp_path / "project")
    project_record = _create_project(client, project)

    new_src = tmp_path / "book_conflict.txt"
    new_src.write_text(
        "第1章 序章\n内容1\n第2章 改过的相遇\n内容2\n第3章 冲突\n内容3\n",
        encoding="utf-8",
    )
    plan = client.post(
        f"/projects/{project_record['id']}/chapters/append-preview",
        json={"source_file": str(new_src)}, headers=AUTH,
    ).json()
    assert plan["conflict"] is not None

    response = client.post(
        f"/projects/{project_record['id']}/chapters/apply",
        json={"plan": plan}, headers=AUTH,
    )
    assert response.status_code == 422, response.text
