"""Reentrant project writer admission shared by CLI and desktop processes.

OS locks are released when a process dies; a stale file is not a stale lock.
The lock protects cooperating writers. Hash comparisons additionally reject
external edits that do not participate in this protocol.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import errno
import os
import threading
import time
from functools import wraps

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_owners = threading.local()


def serialized_story_write(function):
    """Serialize public writer entrypoints accepting a project Path or CLI args."""
    @wraps(function)
    def guarded(project_dir, *args, **kwargs):
        project = getattr(project_dir, "project_dir", project_dir)
        with project_write_lock(Path(project)):
            return function(project_dir, *args, **kwargs)
    return guarded


@contextmanager
def project_write_lock(project: Path, timeout: float = 10):
    """Hold a process and thread exclusive lock, allowing nested same-owner calls."""
    path = project.resolve() / "质量治理" / ".writer.lock"
    key = os.path.normcase(str(path))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=max(0, timeout)):
        raise TimeoutError("项目写入占用，保留候选后重试")
    held = getattr(_owners, "held", set())
    _owners.held = held
    stream = None
    acquired = False
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("项目写入占用，保留候选后重试") from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
    finally:
        if stream is not None:
            try:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        lock.release()
