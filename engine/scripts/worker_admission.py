"""Bound model work across projects within the desktop backend process."""
from __future__ import annotations

import os
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, eq=False)
class _Capacity:
    """Identity follows copied contexts; equal numeric limits are separate tasks."""
    limit: int


@dataclass(eq=False)
class _Ticket:
    capacity: _Capacity
    cancel: object


_requested_capacity = ContextVar("worker_concurrency", default=_Capacity(2))


@contextmanager
def worker_concurrency_scope(limit: int):
    """Freeze a run's requested capacity without changing another run or process."""
    if type(limit) is not int or limit < 1:
        raise ValueError("模型并发必须为正整数")
    token = _requested_capacity.set(_Capacity(limit))
    try:
        yield
    finally:
        _requested_capacity.reset(token)


class WorkerAdmission:
    """Oldest eligible request wins, respecting task quotas and a process ceiling.

    A task that has exhausted its own quota cannot block other tasks. Requests
    within one copied context retain FIFO order. Queue time is outside timeout.
    """

    def __init__(self, limit: int | None = None, *, source: str | None = None, warning: str = ""):
        # An explicitly configured deployment ceiling remains authoritative.
        # Without it, the run settings replace the old hidden fixed cap of two.
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("部署并发上限必须为正整数")
        self.limit = limit
        self._source = source or ("environment" if limit is not None else "task_config")
        self._warning = warning
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = 0
        self._active_by_task: dict[_Capacity, int] = {}

    @classmethod
    def from_environment(cls):
        """Read once at startup; malformed deployment limits fail closed visibly."""
        raw = os.environ.get("ND_MAX_CONCURRENT_WORKERS", "").strip()
        if not raw:
            return cls()
        try:
            limit = int(raw)
            if limit < 1:
                raise ValueError
        except ValueError:
            return cls(1, source="invalid_environment", warning=(
                "ND_MAX_CONCURRENT_WORKERS 无效，已安全降为 1；请设为正整数并重启应用。"
            ))
        return cls(limit)

    def configuration(self) -> dict:
        """Expose this process's frozen policy, never the caller's environment."""
        return {"deployment_limit": self.limit, "limit_source": self._source,
                "warning": self._warning, "scope": "backend_process"}

    def effective_limit(self) -> int:
        requested = _requested_capacity.get().limit
        return min(requested, self.limit) if self.limit is not None else requested

    def _first_eligible(self):
        """Called under the condition lock; skip cancelled or quota-bound tasks."""
        if self.limit is not None and self._active >= self.limit:
            return None
        return next((ticket for ticket in self._waiting
                     if not (ticket.cancel is not None and ticket.cancel.is_set())
                     and self._active_by_task.get(ticket.capacity, 0) < ticket.capacity.limit), None)

    @contextmanager
    def slot(self, cancel=None):
        capacity = _requested_capacity.get()
        ticket = _Ticket(capacity, cancel)
        admitted = False
        limit = self.effective_limit()
        with self._condition:
            self._waiting.append(ticket)
            announced = False
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        break
                    if self._first_eligible() is ticket:
                        self._waiting.remove(ticket)
                        self._active += 1
                        self._active_by_task[capacity] = self._active_by_task.get(capacity, 0) + 1
                        admitted = True
                        self._condition.notify_all()
                        break
                    if not announced:
                        print(f"模型任务正在排队，任务生效上限 {limit}（任务配置 {capacity.limit}，"
                              f"本进程部署总上限 {self.limit if self.limit is not None else '未设置'}）；可随时停止。", flush=True)
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
                    remaining = self._active_by_task[capacity] - 1
                    if remaining:
                        self._active_by_task[capacity] = remaining
                    else:
                        del self._active_by_task[capacity]
                    self._condition.notify_all()


worker_admission = WorkerAdmission.from_environment()
