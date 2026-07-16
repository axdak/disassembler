#!/usr/bin/env python3
"""线程安全的事件广播 hub。

runner 线程把编排器事件 broadcast 给所有订阅者；WebSocket 协程订阅后用
loop.call_soon_threadsafe 推入各自的 asyncio 队列。
"""
from __future__ import annotations

import threading
from typing import Callable


class BroadcastHub:
    def __init__(self) -> None:
        self._subs: set[Callable[[dict], None]] = set()
        self._lock = threading.Lock()

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.add(cb)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.discard(cb)

    def broadcast(self, event_dict: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event_dict)
            except Exception:
                pass
