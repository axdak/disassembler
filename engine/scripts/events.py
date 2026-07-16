#!/usr/bin/env python3
"""事件总线。

编排器在阶段开始/结束/失败/硬阻塞/进度更新时发射事件；订阅者（未来的 Python
后端 / WebSocket 桥接）接收并转发给 Renderer。事件总线线程安全，支持进度轮询
线程在阻塞的 cmd_run 期间并发发射进度事件。
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

Listener = Callable[["Event"], None]


@dataclass
class Event:
    type: str
    stage: str = ""
    detail: str = ""
    data: dict = field(default_factory=dict)
    ts: float = 0.0


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def emit(self, event: Event) -> None:
        event.ts = time.time()
        with self._lock:
            listeners = list(self._listeners)
        for ln in listeners:
            try:
                ln(event)
            except Exception as exc:  # noqa: BLE001
                print(f"[events] listener error: {type(exc).__name__}: {exc}", file=sys.stderr)

    @staticmethod
    def to_dict(event: Event) -> dict:
        return asdict(event)
