"""Bound model repair calls across nested pipeline retries and keep recovery evidence.

An explicit run starts a new allowance; automatic stage redispatches share it.
Journals and pre-repair artifacts persist on disk, outside the story schema.
No semantic content is truncated or synthesized by this policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

REPAIR_EXHAUSTED = 65
REPAIR_TASKS = {"repair_chapter", "audit_repair", "analysis_regenerate", "repair_final"}
_sessions: dict[str, str] = {}
_lock = threading.RLock()


def begin_retry_session(project_dir: Path) -> None:
    """Called only by the outer user-run entry, never by an automatic retry."""
    with _lock:
        _sessions[str(project_dir.resolve())] = uuid.uuid4().hex


def _save(path: Path, data: dict) -> None:
    """Reserve attempts durably before launching a possibly failing worker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reserve_repair(project_dir: Path, output: Path, task: str, limit: int):
    """Return a journal ticket, or None for exhausted/cyclic repairs.

Transport failures still receive their bounded retry allowance. A previously
accepted worker result leading back to the same failed input signals a cycle.
"""
    project_key = str(project_dir.resolve())
    key = hashlib.sha256(f"{task}:{output.resolve()}".encode()).hexdigest()[:20]
    path = project_dir / "质量治理" / "重试记录" / f"{task}_{key}.json"
    with _lock:
        session = _sessions.setdefault(project_key, uuid.uuid4().hex)
        if path.exists():
            # A corrupt journal is not silently treated as a fresh allowance.
            journal = json.loads(path.read_text(encoding="utf-8"))
        else:
            journal = {"output": str(output), "task": task, "attempts": []}
        records = [r for r in journal["attempts"] if r["session"] == session]
        content = output.read_bytes() if output.is_file() else b""
        digest = hashlib.sha256(content).hexdigest()
        cycle = any(r["input_sha256"] == digest and r.get("returncode") == 0 for r in records)
        if len(records) >= max(1, limit) or cycle:
            journal["blocked_reason"] = "相同失败产物重复修复" if cycle else "本次运行修复次数已耗尽"
            _save(path, journal)
            print(f"{journal['blocked_reason']}；已保留断点与修复历史：{path}")
            return None
        snapshot = path.with_name(f"{path.stem}_before_{len(journal['attempts']) + 1:04d}{output.suffix}")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if content:
            snapshot.write_bytes(content)
        journal.pop("blocked_reason", None)
        journal["attempts"].append({
            "session": session, "input_sha256": digest, "snapshot": str(snapshot),
            "started_at": datetime.now().isoformat(), "limit": max(1, limit),
        })
        _save(path, journal)
        print(f"自动修复进度：{len(records) + 1}/{max(1, limit)}；产物：{output.name}")
        return path, len(journal["attempts"]) - 1


def finish_repair(ticket, returncode: int) -> None:
    """Complete a reserved attempt without changing its allowance."""
    if ticket is None:
        return
    path, index = ticket
    with _lock:
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["attempts"][index].update(returncode=returncode, finished_at=datetime.now().isoformat())
        _save(path, journal)
