"""Bounded process cache for engine code only; never cache project evidence."""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path


_CACHE = OrderedDict()
_LOCK = threading.RLock()
_MAX_ENTRIES = 512


def _signature(path: Path):
    """Detect edits and atomic replacements, including equal-length rewrites."""
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def code_digest(path: Path) -> str:
    """Hash an engine file once per observed version, with serialized LRU access.

    Callers must pass code, prompt or schema files, never mutable project inputs.
    A file changing during a read is not memoized; the next call reads it again.
    Missing files retain the existing empty-digest behavior and are not cached.
    """
    path = path.resolve()
    with _LOCK:
        try:
            before = _signature(path)
        except FileNotFoundError:
            _CACHE.pop(path, None)
            return hashlib.sha256(b"").hexdigest()
        previous = _CACHE.get(path)
        if previous is not None and previous[0] == before:
            _CACHE.move_to_end(path)
            return previous[1]
        value = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            after = _signature(path)
        except FileNotFoundError:
            after = None
        if before == after:
            _CACHE[path] = (before, value)
            _CACHE.move_to_end(path)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        else:
            _CACHE.pop(path, None)
        return value
