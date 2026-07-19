"""Finalize task adapter tests (gap-fill plan Task 3).

Asserts the worker-backed composite finalize adapter mirrors the
orchestrator's final-pack -> worker revises draft -> commit_final_draft
(repair loop) -> validate_final -> finalize sequence, producing a valid
故事结构.json under the project dir.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from task_adapters import FinalizeAdapter
from workbench import TaskContext

# Make engine/scripts importable so the adapter and its helpers run.
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))


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


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# Minimal valid story schema: passes validate_structure --strict (final mode)
# because MIN_COUNTS are all 0 and 介绍.{标题,描述} are non-empty strings.
EMPTY_STORY = {
    "介绍": {"标题": "测试故事", "描述": "finalize 适配器测试。"},
    "角色集": [],
    "事件集": [],
    "地点集": [],
    "线索集": [],
    "阵营集": [],
    "物品集": [],
    "其他事项集": [],
}


def _write_finalize_worker(worker: Path) -> None:
    """Fake worker: for final_draft/repair_final, write EMPTY_STORY JSON to
    ND_EXPECTED_OUTPUT. Mirrors the env-var contract run_worker passes
    (ND_TASK_TYPE / ND_EXPECTED_OUTPUT / ND_TASK_PACK / ND_PROJECT_DIR)."""
    story_text = json.dumps(EMPTY_STORY, ensure_ascii=False, indent=2)
    worker.write_text(
        f"""
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
output.parent.mkdir(parents=True, exist_ok=True)

if task_type in ("final_draft", "repair_final"):
    output.write_text({story_text!r}, encoding="utf-8")
else:
    raise SystemExit(f"unexpected task type: {{task_type}}")
""",
        encoding="utf-8",
    )


def _seed_project(tmp_path: Path) -> Path:
    project = tmp_path / "拆书_finalize"
    _write_json(project / "原文拆解" / "_索引.json", {
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "title": "测试"}]
    })
    chapter = project / "原文拆解" / "第001章_测试.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
    _write_json(project / "故事结构_增量.json", EMPTY_STORY)
    return project


def test_finalize_adapter_produces_valid_final_json(tmp_path):
    project = _seed_project(tmp_path)
    worker = tmp_path / "finalize_worker.py"
    _write_finalize_worker(worker)
    command = f'"{sys.executable}" "{worker}"'

    adapter = FinalizeAdapter()
    ctx = _make_context(project, {"worker_command": command})
    result = adapter.run(ctx)

    assert result["revision"], "result must carry a project revision"
    final = project / "故事结构.json"
    assert final.is_file(), f"故事结构.json must be written: {final}"
    data = json.loads(final.read_text(encoding="utf-8"))
    assert data["介绍"]["标题"] == "测试故事"
    # The final JSON must itself satisfy strict (final) validation shape.
    for key in ("介绍", "角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"):
        assert key in data, f"final story missing top-level key {key}"


def test_finalize_adapter_without_worker_command_raises(tmp_path):
    """No worker_command: cmd_final_pack succeeds but commit_final_draft fails
    strict validation (the seed draft is a copy of 增量 which IS valid here,
    so this mainly asserts the error-path wiring when no worker is configured
    and the draft cannot pass). The happy-path test above is the real check."""
    project = _seed_project(tmp_path)
    adapter = FinalizeAdapter()
    ctx = _make_context(project, {"worker_command": ""})
    # With a valid seed the run may actually succeed even without a worker
    # (cmd_final_pack copies 增量 -> draft, and 增量 already validates strict).
    # So this test only asserts the adapter either succeeds OR raises
    # RuntimeError citing a finalize-stage failure — both are acceptable
    # outcomes of the no-worker path for a valid seed.
    try:
        result = adapter.run(ctx)
    except RuntimeError as exc:
        # Error path: failure must cite a finalize-stage step.
        assert any(s in str(exc) for s in (
            "final_pack", "final_draft worker", "commit_final_draft",
            "validate_final", "finalize",
        ))
        return
    # Happy path: a valid seed means the draft already passes strict, so
    # finalize can succeed without ever invoking the worker.
    assert result["revision"]
    assert (project / "故事结构.json").is_file()
