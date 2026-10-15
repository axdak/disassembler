"""Exercise independent task quotas without starting providers or touching books."""
import threading
import time
from contextlib import ExitStack
from contextvars import copy_context

import pytest

from worker_admission import WorkerAdmission, worker_concurrency_scope


def _queued(gate, count):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with gate._condition:
            if len(gate._waiting) >= count:
                return
        time.sleep(0.005)
    raise AssertionError("worker did not join admission queue")


def _start(gate, entered, release, *, cancel=None, results=None):
    def request():
        with gate.slot(cancel) as admitted:
            if results is not None:
                results.append(admitted)
            if admitted:
                entered.set()
                release.wait(3)
    thread = threading.Thread(target=copy_context().run, args=(request,))
    thread.start()
    return thread


def test_independent_small_task_does_not_wait_for_other_task_to_drain():
    gate = WorkerAdmission()
    release, entered = threading.Event(), threading.Event()
    thread = None
    try:
        with ExitStack() as held:
            held.enter_context(worker_concurrency_scope(5))
            for _ in range(3):
                held.enter_context(gate.slot())
            with worker_concurrency_scope(1):
                thread = _start(gate, entered, release)
            assert entered.wait(1), "another task's active workers consumed this task's quota"
    finally:
        release.set()
        if thread:
            thread.join(3)


def test_busy_task_at_queue_head_does_not_block_an_eligible_task():
    gate = WorkerAdmission(4)
    release = threading.Event()
    blocked, eligible = threading.Event(), threading.Event()
    threads = []
    try:
        with worker_concurrency_scope(1), gate.slot():
            threads.append(_start(gate, blocked, release))
            _queued(gate, 1)
            with worker_concurrency_scope(5):
                threads.append(_start(gate, eligible, release))
            assert eligible.wait(1), "an ineligible FIFO head blocked available deployment capacity"
            assert not blocked.is_set(), "one task exceeded its own quota"
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert blocked.is_set()
    assert gate._active == 0


def test_deployment_ceiling_preserves_order_and_cancellation():
    gate = WorkerAdmission(1)
    release, first, second, cancel = [threading.Event() for _ in range(4)]
    threads, cancelled = [], []
    try:
        with gate.slot():
            with worker_concurrency_scope(2):
                threads.append(_start(gate, first, release, cancel=cancel, results=cancelled))
            _queued(gate, 1)
            with worker_concurrency_scope(3):
                threads.append(_start(gate, second, release))
            _queued(gate, 2)
            assert not first.is_set() and not second.is_set()
            cancel.set()
            threads[0].join(2)
            assert cancelled == [False]
            assert not second.is_set(), "deployment ceiling was bypassed"
        assert second.wait(1)
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert gate._active == 0 and not gate._waiting


def test_copied_contexts_share_task_quota_and_release_after_failure():
    gate = WorkerAdmission(4)
    release, first, second = [threading.Event() for _ in range(3)]
    threads = []
    try:
        with worker_concurrency_scope(1):
            threads.append(_start(gate, first, release))
            assert first.wait(1)
            threads.append(_start(gate, second, release))
            _queued(gate, 1)
            assert not second.is_set()
        release.set()
        for thread in threads:
            thread.join(3)
        assert second.is_set()
        try:
            with worker_concurrency_scope(1), gate.slot():
                raise RuntimeError("local worker failed")
        except RuntimeError:
            pass
        with worker_concurrency_scope(1), gate.slot() as admitted:
            assert admitted
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert gate._active == 0


@pytest.mark.parametrize("raw,expected,source", [
    ("", None, "task_config"), (" 5 ", 5, "environment"),
    ("bad", 1, "invalid_environment"), ("0", 1, "invalid_environment"),
    ("-2", 1, "invalid_environment"), ("1.5", 1, "invalid_environment"),
])
def test_environment_policy_is_frozen_and_invalid_cap_is_visible(monkeypatch, raw, expected, source):
    monkeypatch.setenv("ND_MAX_CONCURRENT_WORKERS", raw)
    gate = WorkerAdmission.from_environment()
    monkeypatch.setenv("ND_MAX_CONCURRENT_WORKERS", "99")
    policy = gate.configuration()
    assert policy["deployment_limit"] == expected
    assert policy["limit_source"] == source
    assert bool(policy["warning"]) == (source == "invalid_environment")


def test_waiters_in_one_task_keep_fifo_order():
    gate = WorkerAdmission(3)
    first, second, release_first, release_second = [threading.Event() for _ in range(4)]
    threads = []
    try:
        with worker_concurrency_scope(1), gate.slot():
            threads.append(_start(gate, first, release_first))
            _queued(gate, 1)
            threads.append(_start(gate, second, release_second))
            _queued(gate, 2)
        assert first.wait(1)
        assert not second.is_set()
        release_first.set()
        assert second.wait(1)
    finally:
        release_first.set()
        release_second.set()
        for thread in threads:
            thread.join(3)
    assert not gate._active_by_task


def test_many_copied_tasks_never_exceed_task_or_deployment_limits():
    gate = WorkerAdmission(3)
    release, full = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peaks, errors, threads = {}, {}, [], []
    total = peak_total = completed = 0

    def request(group):
        nonlocal total, peak_total, completed
        try:
            with gate.slot() as acquired:
                assert acquired
                with lock:
                    active[group] = active.get(group, 0) + 1
                    peaks[group] = max(peaks.get(group, 0), active[group])
                    total += 1
                    peak_total = max(peak_total, total)
                    if total == 3:
                        full.set()
                assert release.wait(3)
                with lock:
                    active[group] -= 1
                    total -= 1
                    completed += 1
        except Exception as exc:
            errors.append(exc)

    try:
        for group in range(8):
            with worker_concurrency_scope(2):
                for _ in range(3):
                    thread = threading.Thread(target=copy_context().run, args=(request, group))
                    threads.append(thread)
                    thread.start()
        assert full.wait(1)
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert completed == 24 and peak_total == 3 and max(peaks.values()) <= 2
    assert gate._active == 0 and not gate._active_by_task and not gate._waiting
