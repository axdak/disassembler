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

# Invalid-for-strict seed: 介绍.{标题,描述} empty -> validate_structure --strict
# fails with "介绍.标题 为空" / "介绍.描述 为空", so cmd_commit_final_draft writes
# repair_final.md and returns 1 (i.e. the draft needs the worker repair loop).
INVALID_FOR_STRICT_STORY = {
    "介绍": {"标题": "", "描述": ""},
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


def _seed_project(tmp_path: Path, story: dict | None = None) -> Path:
    project = tmp_path / "拆书_finalize"
    _write_json(project / "原文拆解" / "_索引.json", {
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "title": "测试"}]
    })
    chapter = project / "原文拆解" / "第001章_测试.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
    _write_json(project / "故事结构_增量.json", story if story is not None else EMPTY_STORY)
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


def test_finalize_adapter_raises_when_draft_needs_repair_and_no_worker(tmp_path):
    """Regression guard for the no-worker failure path (FinalizeAdapter fix).

    Seed a draft that FAILS ``cmd_commit_final_draft``'s strict validation
    (empty 介绍.标题/描述) so the repair loop is required. With
    ``worker_provider="manual"`` and no explicit ``worker_command``, the
    adapter's auto-resolve is suppressed (no worker available), so the run
    must raise RuntimeError citing "commit_final_draft 失败 ... 且无
    worker_command" - the exact path MaintenanceTab would hit in production
    without the auto-resolve fix.
    """
    project = _seed_project(tmp_path, story=INVALID_FOR_STRICT_STORY)
    adapter = FinalizeAdapter()
    ctx = _make_context(project, {"worker_command": "", "worker_provider": "manual"})
    with pytest.raises(RuntimeError) as excinfo:
        adapter.run(ctx)
    msg = str(excinfo.value)
    assert "commit_final_draft 失败" in msg
    assert "无 worker_command" in msg


def test_finalize_adapter_caps_transport_retries_by_repair_allowance(tmp_path, monkeypatch):
    """Desktop maintenance uses the same budget as the pipeline entry point."""
    import task_adapters
    import run_pipeline
    project = _seed_project(tmp_path)
    repair = project / "质量治理/最终审计/repair_final.md"
    repair.parent.mkdir(parents=True)
    repair.write_text("repair", encoding="utf-8")
    calls = []
    monkeypatch.setattr(task_adapters, "cmd_final_pack", lambda *a: 0)
    monkeypatch.setattr(task_adapters, "cmd_commit_final_draft", lambda *a: 1)
    monkeypatch.setattr(run_pipeline, "wait_before_worker_retry", lambda attempt: False)

    def worker(command, directory, env, timeout):
        if env["ND_TASK_TYPE"] == "final_draft":
            _write_json(Path(env["ND_EXPECTED_OUTPUT"]), EMPTY_STORY)
            return 0, "local initial draft"
        calls.append(env["ND_TASK_TYPE"])
        return 9, "local transport failure"

    monkeypatch.setattr(run_pipeline, "stream_shell_command", worker)
    context = _make_context(project, {"worker_command": "local-fake", "worker_retries": 5,
                                      "max_repair_rounds": 2})
    with pytest.raises(RuntimeError, match="rc=65"):
        FinalizeAdapter().run(context)
    assert calls == ["repair_final", "repair_final"]
