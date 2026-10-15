"""Bound model work across projects within the desktop backend process."""
from __future__ import annotations

import os
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar


_requested_limit = ContextVar("worker_concurrency", default=2)


@contextmanager
def worker_concurrency_scope(limit: int):
    """Freeze a run's requested capacity without changing another run or process."""
    if type(limit) is not int or limit < 1:
        raise ValueError("模型并发必须为正整数")
    token = _requested_limit.set(limit)
    try:
        yield
    finally:
        _requested_limit.reset(token)


class WorkerAdmission:
    """FIFO admission with cancellation; queue time does not consume model timeout."""

    def __init__(self, limit: int | None = None):
        # An explicitly configured deployment ceiling remains authoritative.
        # Without it, the run settings replace the old hidden fixed cap of two.
        self.limit = max(1, limit) if limit is not None else None
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = 0

    def effective_limit(self) -> int:
        requested = _requested_limit.get()
        return min(requested, self.limit) if self.limit is not None else requested

    @contextmanager
    def slot(self, cancel=None):
        ticket = object()
        admitted = False
        limit = self.effective_limit()
        with self._condition:
            self._waiting.append(ticket)
            announced = False
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        break
                    if self._waiting[0] is ticket and self._active < limit:
                        self._waiting.popleft()
                        self._active += 1
                        admitted = True
                        self._condition.notify_all()
                        break
                    if not announced:
                        print(f"模型任务正在排队，实际并发上限 {limit}（任务配置 {_requested_limit.get()}）；可随时停止。", flush=True)
                        announced = True
                    self._condition.wait(timeout=0.1)
            finally:
                if not admitted:
                    self._waiting.remove(ticket)
                    self._condition.notify_all()
        try:
            yield admitted
        finally:
            if admitted:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()


def _configured_limit() -> int | None:
    try:
        value = os.environ.get("ND_MAX_CONCURRENT_WORKERS", "").strip()
        return max(1, int(value)) if value else None
    except ValueError:
        return 2


worker_admission = WorkerAdmission(_configured_limit())
