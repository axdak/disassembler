"""Bound model work across projects within the desktop backend process."""
from __future__ import annotations

import os
import threading
from collections import deque
from contextlib import contextmanager


class WorkerAdmission:
    """FIFO admission with cancellation; queue time does not consume model timeout."""

    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = 0

    @contextmanager
    def slot(self, cancel=None):
        ticket = object()
        admitted = False
        with self._condition:
            self._waiting.append(ticket)
            announced = False
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        break
                    if self._waiting[0] is ticket and self._active < self.limit:
                        self._waiting.popleft()
                        self._active += 1
                        admitted = True
                        self._condition.notify_all()
                        break
                    if not announced:
                        print(f"模型任务正在排队，当前并发上限 {self.limit}；可随时停止。", flush=True)
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


def _configured_limit() -> int:
    try:
        return max(1, int(os.environ.get("ND_MAX_CONCURRENT_WORKERS", "2")))
    except ValueError:
        return 2


worker_admission = WorkerAdmission(_configured_limit())
