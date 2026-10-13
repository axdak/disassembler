"""Regression coverage for durable failures and meaningful retry progress."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import orchestrator
import progress_manager
from events import EventBus
from orchestrator import Config, Orchestrator
from pipeline_state import PipelineState


@pytest.mark.parametrize("failure", [ValueError("invalid chapter scope"), OSError("disk denied")])
def test_unexpected_stage_failure_is_persisted_and_reported(tmp_path, monkeypatch, failure):
    """Unexpected exceptions stop once and retain a recoverable, diagnostic state."""
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    orch = Orchestrator(tmp_path, bus=bus)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(orchestrator, "cmd_run", fail)
    assert orch.run() == 1
    assert len(calls) == 1
    state = PipelineState.load(tmp_path)
    assert state.current_stage == "chapter_analysis"
    assert str(failure) in state.last_error
    for kind in ("stage_fail", "pipeline_blocked"):
        event = next(event for event in events if event.type == kind)
        assert event.stage == "chapter_analysis"
        assert type(failure).__name__ in event.detail
        assert "Traceback" in event.data["traceback"]
    assert not orch.state.completed


def test_unexpected_failure_reports_even_when_state_cannot_be_saved(tmp_path, monkeypatch):
    """A damaged/unwritable checkpoint must not hide the original diagnostic."""
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    orch = Orchestrator(tmp_path, bus=bus)

    def denied():
        raise OSError("checkpoint disk denied")

    monkeypatch.setattr(orch.state, "save", denied)
    assert orch.run() == 1
    event = next(event for event in events if event.type == "pipeline_blocked")
    assert "checkpoint disk denied" in event.detail
    assert "checkpoint disk denied" in event.data["persistence_error"]


def test_reconcile_exception_is_reported_and_observers_are_reset(tmp_path, monkeypatch):
    """Preparation failures use the same contract and clean up run-local globals."""
    orch = Orchestrator(tmp_path)
    resets = []
    monkeypatch.setattr(orchestrator, "set_cancel_event", lambda value: resets.append(value))
    observers = []
    monkeypatch.setattr(orchestrator, "set_recovery_observer", observers.append)

    def invalid_scope():
        raise ValueError("scope cannot rewind committed chapters")

    monkeypatch.setattr(orch, "_reconcile_chapter_scope", invalid_scope)
    assert orch.run() == 1
    assert "scope cannot rewind" in PipelineState.load(tmp_path).last_error
    assert resets[-1] is not orch._cancel
    assert not resets[-1].is_set()
    assert observers[-1] is None


def test_cancellation_base_exception_is_not_converted_to_failure(tmp_path, monkeypatch):
    """Interpreter interrupts propagate while the normal cleanup still executes."""
    orch = Orchestrator(tmp_path)
    observers = []
    monkeypatch.setattr(orchestrator, "set_recovery_observer", observers.append)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(orchestrator, "cmd_run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        orch.run()
    assert observers[-1] is None


def test_stagnation_ignores_task_rewrites_and_progress_timestamps(tmp_path, monkeypatch):
    """Regenerated prompts and heartbeat metadata cannot renew a failing stage."""
    chapter = tmp_path / "章节处理/第001章.md"
    chapter.parent.mkdir()
    chapter.write_text("unchanged analysis", encoding="utf-8")
    progress = {"更新时间": "old", "步骤状态": {"2": {"状态": "in_progress", "详情": {"已完成章节": []}}}}
    progress_manager.save_progress(tmp_path, progress)
    orch = Orchestrator(tmp_path, config=Config(max_attempts=10, stagnant_limit=3))
    calls = []

    def failed_attempt(*args, **kwargs):
        calls.append(1)
        for folder in ("_任务包", "_修复任务"):
            task = chapter.parent / folder / f"attempt_{len(calls)}.md"
            task.parent.mkdir(exist_ok=True)
            task.write_text("new diagnostic only", encoding="utf-8")
        os.utime(chapter, (2000000000 + len(calls), 2000000000 + len(calls)))
        progress["更新时间"] = str(len(calls))
        progress_manager.save_progress(tmp_path, progress)
        return 1

    monkeypatch.setattr(orchestrator, "cmd_run", failed_attempt)
    assert orch.run() == 1
    assert len(calls) == 3
    assert orch.state.stagnant("chapter_analysis") == 3


def test_signature_detects_equal_size_content_changes_without_mtime_changes(tmp_path):
    """Revisions of the actual analysis count even when file metadata matches."""
    chapter = tmp_path / "章节处理/第001章.md"
    chapter.parent.mkdir()
    chapter.write_bytes(b"first")
    orch = Orchestrator(tmp_path)
    old_stat = chapter.stat()
    original = orch._signature("chapter_analysis")
    chapter.write_bytes(b"other")
    os.utime(chapter, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert orch._signature("chapter_analysis") != original


def test_progress_completion_changes_signature(tmp_path):
    """Only ignoring timestamps must not ignore newly committed progress."""
    progress = {"更新时间": "same", "步骤状态": {"2": {"详情": {"已完成章节": []}}}}
    progress_manager.save_progress(tmp_path, progress)
    orch = Orchestrator(tmp_path)
    before = orch._signature("chapter_delta")
    progress["步骤状态"]["2"]["详情"]["已完成章节"].append("第001章.md")
    progress_manager.save_progress(tmp_path, progress)
    assert orch._signature("chapter_delta") != before


def test_changing_results_still_respect_max_attempts(tmp_path, monkeypatch):
    """Real progress cannot remove the stage's absolute retry bound."""
    chapter = tmp_path / "章节处理/第001章.md"
    chapter.parent.mkdir()
    orch = Orchestrator(tmp_path, config=Config(max_attempts=4, stagnant_limit=2))
    calls = []

    def evolving_failure(*args, **kwargs):
        calls.append(1)
        chapter.write_text(str(len(calls)), encoding="utf-8")
        return 1

    monkeypatch.setattr(orchestrator, "cmd_run", evolving_failure)
    assert orch.run() == 1
    assert len(calls) == 4


def test_failed_progress_serialization_preserves_previous_bytes(tmp_path):
    """Serialization errors must never truncate the last good checkpoint."""
    path = tmp_path / progress_manager.PROGRESS_FILE
    previous = b'{"previous": "trusted"}\n'
    path.write_bytes(previous)
    with pytest.raises(TypeError):
        progress_manager.save_progress(tmp_path, {"bad": object()})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("damaged", [b'{"cut":', b'\xff', b'[]', b'null'])
def test_corrupt_progress_is_preserved_with_actionable_error(tmp_path, damaged):
    """A corrupt checkpoint must not silently become empty or completed progress."""
    path = tmp_path / progress_manager.PROGRESS_FILE
    path.write_bytes(damaged)
    with pytest.raises(ValueError) as error:
        progress_manager.load_progress(tmp_path)
    assert str(path) in str(error.value)
    assert "恢复" in str(error.value)
    assert path.read_bytes() == damaged


def test_worker_module_import_is_independent_of_test_path_order(tmp_path):
    """Load the real governance schema in a fresh interpreter with isolated paths."""
    worker = Path(__file__).resolve().parents[1] / "tools/agent_worker.py"
    code = "\n".join([
        "import importlib.util, json, sys",
        "from pathlib import Path",
        "spec = importlib.util.spec_from_file_location('isolated_worker', sys.argv[1])",
        "module = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(module)",
        "print(module.structured_output_schema('audit_repair', [Path('correction.json')]))",
    ])
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(worker)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    schema = json.loads(result.stdout)
    assert schema["properties"]["治理操作"]["type"] == "object"
    assert schema["properties"]["修改元素"]["type"] == "object"
