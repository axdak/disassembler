"""Public maintenance commands must share the chapter/governance OS lock."""
import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import run_pipeline as pipeline
from test_audit_pack import empty_story, write_json


@pytest.mark.parametrize("entry", ["ensure_story", "replay_one_delta", "recover_story_from_snapshot",
                                  "cmd_commit_final_draft", "cmd_finalize", "cmd_final_pack",
                                  "cmd_normalize", "cmd_migrate_chronology"])
def test_maintenance_waits_for_external_writer_before_mutating(tmp_path, monkeypatch, entry):
    """Use a real OS lock owned by a separate process, including early returns."""
    ready = tmp_path / "lock-ready"
    write_json(tmp_path / "故事结构_增量.json", empty_story())
    code = ("import sys;from pathlib import Path;from project_lock import project_write_lock\n"
            "with project_write_lock(Path(sys.argv[1])):\n"
            " Path(sys.argv[2]).write_text('ready');sys.stdin.readline()\n")
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(tmp_path), str(ready)],
                             cwd=Path(pipeline.__file__).parent, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    finished, entered, errors = threading.Event(), threading.Event(), []
    monkeypatch.setattr(pipeline, "run_cmd", lambda *args, **kwargs: 0)
    # We test entry admission, not every command's independent validation rules.
    monkeypatch.setattr(pipeline, "guard_single_unit_artifacts", lambda *_: False)

    def invoke():
        entered.set()
        try:
            args = argparse.Namespace(project_dir=str(tmp_path), file="", dry_run=True,
                                      quarantine_invalid_refs=False)
            if entry in {"cmd_normalize", "cmd_migrate_chronology"}:
                getattr(pipeline, entry)(args)
            elif entry == "replay_one_delta":
                pipeline.replay_one_delta(tmp_path, 1)
            else:
                getattr(pipeline, entry)(tmp_path)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=invoke)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        thread.start(); assert entered.wait(2)
        assert not finished.wait(0.15), f"{entry} bypassed the external writer"
    finally:
        child.communicate("release\n", timeout=5)
        if thread.ident is not None:
            thread.join(10)
    assert finished.is_set() and not errors


def test_final_publication_interruption_preserves_previous_final(tmp_path, monkeypatch):
    """Final JSON needs the same atomic replacement boundary as chapter commits."""
    final = tmp_path / "故事结构.json"
    final.write_bytes(b'{"old":true}')
    write_json(tmp_path / "故事结构_草稿.json", empty_story())
    old = final.read_bytes()
    real_replace = pipeline.os.replace

    def interrupted(source, destination):
        if Path(destination) == final:
            raise OSError("publication interrupted")
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline.os, "replace", interrupted)
    monkeypatch.setattr(pipeline, "run_cmd", lambda *a, **k: 0)
    with pytest.raises(OSError, match="interrupted"):
        pipeline.cmd_finalize(tmp_path)
    assert final.read_bytes() == old
