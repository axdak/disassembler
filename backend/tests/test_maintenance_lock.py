"""Workbench maintenance shares the engine lock through rollback completion."""
import json
import threading

import pytest
import task_adapters as adapters
from project_lock import project_write_lock
from test_task_adapters import _make_context, _write_json, SAMPLE_STORY as STORY


@pytest.mark.parametrize("name", ["NormalizeAdapter", "MigrateChronologyAdapter", "RecoverStoryAdapter"])
def test_maintenance_adapter_waits_for_engine_writer(tmp_path, monkeypatch, name):
    _write_json(tmp_path / "故事结构_增量.json", STORY)
    _write_json(tmp_path / "故事结构版本/story_after_ch001.json", STORY)
    entered, done, errors = threading.Event(), threading.Event(), []
    monkeypatch.setattr(adapters, "run_migrate", lambda *a, **k: 0)
    monkeypatch.setattr(adapters, "recover_story_from_snapshot", lambda *a, **k: 0)

    def invoke():
        entered.set()
        try:
            getattr(adapters, name)().run(_make_context(tmp_path, {"snapshot_seq": 1}))
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=invoke)
    with project_write_lock(tmp_path):
        thread.start(); assert entered.wait(2)
        blocked = not done.wait(0.15)
    thread.join(5)
    assert blocked, f"{name} bypassed engine writer"
    assert done.is_set() and not errors


@pytest.mark.parametrize("raises", [False, True])
def test_recovery_keeps_writer_lock_until_failed_attempt_is_rolled_back(tmp_path, monkeypatch, raises):
    original = tmp_path / "故事结构_增量.json"
    _write_json(original, STORY)
    _write_json(tmp_path / "故事结构版本/story_after_ch001.json", STORY)
    admitted = []

    def writer():
        try:
            with project_write_lock(tmp_path, timeout=0.1):
                admitted.append(True)
        except TimeoutError:
            admitted.append(False)

    def fail_recovery(*a, **k):
        original.write_text('{}', encoding='utf-8')
        thread = threading.Thread(target=writer)
        thread.start(); thread.join(2)
        assert admitted == [False]
        if raises:
            raise OSError("recovery interrupted")
        return 1

    monkeypatch.setattr(adapters, "recover_story_from_snapshot", fail_recovery)
    with pytest.raises((RuntimeError, OSError)):
        adapters.RecoverStoryAdapter().run(_make_context(tmp_path, {"snapshot_seq": 1}))
    assert json.loads(original.read_text(encoding="utf-8")) == STORY
