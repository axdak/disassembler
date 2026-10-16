"""Bound cleanup with fake processes and one explicitly owned local Job child."""
import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_pipeline as pipeline


class FakeProcess:
    """Model delayed/failed termination without spawning or killing anything."""

    pid = 424242

    def __init__(self, waits=(0,), *, exited=False, kill_error=None, output=""):
        self.returncode = 0 if exited else None
        self.outcomes = list(waits)
        self.wait_timeouts = []
        self.kills = 0
        self.kill_error = kill_error
        self.stdout = io.StringIO(output)
        self.stdin = io.StringIO()

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        if self.kill_error:
            raise self.kill_error

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        assert timeout is not None, "a running fake process must never receive unbounded wait"
        outcome = self.outcomes.pop(0) if self.outcomes else "timeout"
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("fake worker", timeout)
        self.returncode = outcome
        return outcome


def platform(monkeypatch, name="win32", stdout=None):
    """Change only this module's platform view, not Python/pytest globals."""
    monkeypatch.setattr(pipeline, "sys", SimpleNamespace(platform=name, stdout=stdout or io.StringIO()))


def taskkill(monkeypatch, outcome=0, events=None):
    calls = []

    def run(command, **options):
        assert command == ["taskkill", "/F", "/T", "/PID", "424242"]
        calls.append(options)
        if events is not None:
            events.append("taskkill")
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(command, outcome, b"", b"")

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    return calls


def assert_bounded(proc, calls):
    assert calls and all(0 < call.get("timeout", 0) <= 5 for call in calls)
    assert proc.wait_timeouts and all(timeout is not None and 0 < timeout <= 5 for timeout in proc.wait_timeouts)


def test_windows_tree_success_has_fixed_taskkill_and_wait_bounds(monkeypatch):
    platform(monkeypatch)
    calls = taskkill(monkeypatch)
    proc = FakeProcess()
    pipeline._kill_tree(proc)
    assert proc.poll() == 0 and proc.kills == 0
    assert_bounded(proc, calls)


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("taskkill", 5), 128, OSError("taskkill unavailable"),
], ids=["taskkill-timeout", "taskkill-failed", "taskkill-unavailable"])
def test_failed_tree_cleanup_releases_root_but_never_reports_success(monkeypatch, failure):
    platform(monkeypatch)
    calls = taskkill(monkeypatch, failure)
    proc = FakeProcess()
    with pytest.raises(RuntimeError, match="清理"):
        pipeline._kill_tree(proc)
    assert proc.kills == 1 and proc.poll() == 0
    assert_bounded(proc, calls)


def test_successful_taskkill_with_slow_wait_uses_bounded_direct_kill_fallback(monkeypatch):
    platform(monkeypatch)
    calls = taskkill(monkeypatch)
    proc = FakeProcess(("timeout", 0))
    pipeline._kill_tree(proc)
    assert proc.kills == 1 and proc.poll() == 0
    assert len(proc.wait_timeouts) == 2
    assert_bounded(proc, calls)


def test_unconfirmed_exit_after_fallback_is_an_error(monkeypatch):
    platform(monkeypatch)
    calls = taskkill(monkeypatch)
    proc = FakeProcess(("timeout", "timeout"))
    with pytest.raises(RuntimeError, match="清理"):
        pipeline._kill_tree(proc)
    assert proc.kills == 1 and proc.poll() is None
    assert_bounded(proc, calls)


def test_failed_direct_kill_does_not_hide_a_running_process(monkeypatch):
    platform(monkeypatch)
    taskkill(monkeypatch, 128)
    proc = FakeProcess(kill_error=OSError("termination denied"))
    with pytest.raises(RuntimeError, match="清理"):
        pipeline._kill_tree(proc)
    assert proc.kills == 1 and proc.poll() is None


def test_already_exited_cleanup_is_idempotent(monkeypatch):
    platform(monkeypatch)
    calls = taskkill(monkeypatch)
    proc = FakeProcess(exited=True)
    pipeline._kill_tree(proc)
    pipeline._kill_tree(proc)
    assert not calls and not proc.kills and not proc.wait_timeouts


@pytest.mark.parametrize("outcome", [0, "timeout"], ids=["confirmed", "unconfirmed"])
def test_non_windows_keeps_direct_kill_and_bounds_wait(monkeypatch, outcome):
    platform(monkeypatch, "linux")
    calls = taskkill(monkeypatch)
    proc = FakeProcess((outcome,))
    if outcome == "timeout":
        with pytest.raises(RuntimeError, match="清理"):
            pipeline._kill_tree(proc)
    else:
        pipeline._kill_tree(proc)
    assert proc.kills == 1 and not calls
    assert proc.wait_timeouts and all(timeout is not None and 0 < timeout <= 5 for timeout in proc.wait_timeouts)


@pytest.mark.parametrize("trigger", ["deadline", "idle", "cancel"])
def test_stream_stop_closes_its_job_before_bounded_tree_cleanup(tmp_path, monkeypatch, trigger):
    """Exercise real stream stop branches, with every process operation faked."""
    platform(monkeypatch)
    events = []
    calls = taskkill(monkeypatch, events=events)
    proc = FakeProcess()
    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(pipeline, "_create_windows_kill_on_close_job", lambda _: "owned-job")
    monkeypatch.setattr(pipeline, "_close_windows_job", lambda job: events.append("close-job") if job else None)
    monkeypatch.setattr(pipeline, "_get_cancel_event", lambda: SimpleNamespace(is_set=lambda: trigger == "cancel"))
    monkeypatch.setattr(pipeline, "worker_idle_limit", lambda timeout: 0.01 if trigger == "idle" else 0)
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: next(ticks))
    rc, _ = pipeline.stream_shell_command("fake command", tmp_path, {}, 0.01 if trigger == "deadline" else 1200)
    assert rc == (130 if trigger == "cancel" else 124)
    assert events[:2] == ["close-job", "taskkill"]
    assert_bounded(proc, calls)


def test_stream_never_returns_success_when_tree_cleanup_is_unconfirmed(tmp_path, monkeypatch):
    platform(monkeypatch)
    taskkill(monkeypatch, 128)
    proc = FakeProcess()
    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(pipeline, "_create_windows_kill_on_close_job", lambda _: None)
    monkeypatch.setattr(pipeline, "_close_windows_job", lambda _: None)
    monkeypatch.setattr(pipeline, "_get_cancel_event", lambda: SimpleNamespace(is_set=lambda: True))
    with pytest.raises(RuntimeError, match="清理"):
        pipeline.stream_shell_command("fake command", tmp_path, {}, 1200)


@pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed-exit", "unconfirmed-exit"])
def test_agent_terminal_result_requires_confirmed_exit_before_success(tmp_path, monkeypatch, confirmed):
    """Only confirmed exit makes the existing final no-argument wait safe."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import agent_worker
    import provider_stream

    platform(monkeypatch)
    monkeypatch.setattr(pipeline, "_create_windows_kill_on_close_job", lambda _: None)
    monkeypatch.setattr(pipeline, "_close_windows_job", lambda _: False)
    calls = taskkill(monkeypatch)
    terminal = json.dumps({"type": "result", "subtype": "success", "structured_output": {"value": 1}}) + "\n"
    proc = FakeProcess((0,) if confirmed else ("timeout", "timeout"), output=terminal)
    monkeypatch.setattr(agent_worker.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(pipeline, "worker_idle_limit", lambda *args: 0)
    monkeypatch.setattr(provider_stream.CodeBuddyCapacityLog, "check", lambda *a, **k: False)
    monkeypatch.setattr(provider_stream.CodeBuddyFailureLog, "check", lambda *a, **k: False)
    command = ["fake-cli", "--output-format", "json", "--json-schema", json.dumps({"type": "object", "required": ["value"]})]
    if confirmed:
        result = agent_worker.run_structured_stream(command, "private task", tmp_path)
        assert result.returncode == 0 and proc.poll() == 0
        # The fake rejects no-argument wait until a bounded wait confirms exit.
        assert proc.wait_timeouts[-1] is None
        assert all(value is not None and 0 < value <= 5 for value in proc.wait_timeouts[:-1])
        assert calls and all(0 < call.get("timeout", 0) <= 5 for call in calls)
    else:
        with pytest.raises(RuntimeError, match="清理"):
            agent_worker.run_structured_stream(command, "private task", tmp_path)
        assert proc.poll() is None
        assert_bounded(proc, calls)

@pytest.mark.parametrize("closed", [True, False])
def test_close_job_returns_actual_closehandle_success(monkeypatch, closed):
    import ctypes
    platform(monkeypatch)
    seen = []

    def close(handle):
        seen.append(handle)
        return closed

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(CloseHandle=close), raising=False)
    assert pipeline._close_windows_job("owned-job") is closed
    assert seen == ["owned-job"]
    assert pipeline._close_windows_job(None) is False
    assert seen == ["owned-job"]


def test_failed_tree_cleanup_is_not_hidden_by_a_later_exited_poll(monkeypatch):
    platform(monkeypatch)
    calls = taskkill(monkeypatch, 128)
    proc = FakeProcess()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="清理"):
            pipeline._kill_tree(proc)
    assert proc.poll() == 0 and proc.kills == 1 and len(calls) == 1


def test_concurrent_cleanup_reuses_only_confirmed_same_process_tree(monkeypatch):
    platform(monkeypatch)
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []
    proc = FakeProcess()

    def run(command, **options):
        calls.append(options)
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(command, 0)

    def cleanup(mark=False):
        if mark:
            second.set()
        try:
            pipeline._kill_tree(proc)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    one = threading.Thread(target=cleanup)
    two = threading.Thread(target=lambda: cleanup(True))
    one.start()
    assert entered.wait(2)
    two.start()
    assert second.wait(2)
    release.set()
    one.join(2); two.join(2)
    assert not one.is_alive() and not two.is_alive() and not errors
    assert len(calls) == 1 and proc.poll() == 0
    assert_bounded(proc, calls)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job ownership regression")
def test_real_owned_job_close_race_needs_no_second_taskkill(tmp_path, monkeypatch):
    """Create only this test's child; a successful Job close already owns its tree."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job = pipeline._create_windows_kill_on_close_job(proc)
    try:
        if job is None:
            pytest.skip("host does not allow assigning this test child to a Job")
        assert pipeline._close_windows_job(job) is True
        job = None
        real_poll = proc.poll
        polls = iter([None])
        monkeypatch.setattr(proc, "poll", lambda: next(polls, real_poll()))
        monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: pytest.fail("closed owned Job must not race another taskkill"))
        pipeline._kill_tree(proc, job_closed=True)
        assert real_poll() is not None
    finally:
        pipeline._close_windows_job(job)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("capacity", [False, True], ids=["terminal", "capacity-and-finally"])
def test_native_provider_closes_one_owned_job_and_reuses_proof(tmp_path, monkeypatch, capacity):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import agent_worker
    import provider_stream
    platform(monkeypatch)
    closed = []
    calls = taskkill(monkeypatch, 128)
    monkeypatch.setattr(pipeline, "_create_windows_kill_on_close_job", lambda _: "native-owned-job")

    def close(job):
        if job:
            closed.append(job)
        return job is not None

    monkeypatch.setattr(pipeline, "_close_windows_job", close)
    events = [{"type": "stream_event", "_messageId": "req", "event": {"type": "message_start"}},
              {"type": "result", "subtype": "success", "structured_output": {"value": 1}}]
    proc = FakeProcess(output="".join(json.dumps(event) + "\n" for event in events))
    monkeypatch.setattr(agent_worker.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(pipeline, "worker_idle_limit", lambda *a: 0)
    checks = iter([False, True] if capacity else [False, False])
    monkeypatch.setattr(provider_stream.CodeBuddyCapacityLog, "check", lambda *a, **k: next(checks, False))
    monkeypatch.setattr(provider_stream.CodeBuddyFailureLog, "check", lambda *a, **k: False)
    command = ["fake-cli", "--output-format", "json", "--json-schema", json.dumps({"type": "object", "required": ["value"]})]
    result = agent_worker.run_structured_stream(command, "private task", tmp_path)
    assert result.returncode == (67 if capacity else 0)
    assert closed == ["native-owned-job"] and not calls
    assert proc.poll() == 0
