"""Canonical project-input revisions for persistent workbench tasks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


_CHAPTER_TASK_NAMES = frozenset({"chapter", "chapter_analysis", "chapter_structure", "visual_assets"})
_SCOPED_CHAPTER_TASK_TYPES = frozenset({
    "chapter_visual",
    "chapter_structure",
    "chapter_analysis_rerun",
    "chapter_delta_rerun",
})


def _digest(parts: list[bytes]) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return hasher.hexdigest()


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return _digest([b"missing"])
    return _digest([b"file", path.read_bytes()])


def _chapter_records(index_path: Path) -> list[dict[str, Any]]:
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    chapters = index.get("chapters", []) if isinstance(index, dict) else index
    return chapters if isinstance(chapters, list) else []


def _chapter_metadata(source_dir: Path, chapter: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    filename = chapter.get("filename")
    relative = str(filename).replace("\\", "/") if isinstance(filename, str) else ""
    path = source_dir / relative
    try:
        stat = path.stat()
        metadata = {"path": relative, "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        metadata = {"path": relative, "exists": False, "size": None, "mtime_ns": None}
    return relative, metadata


def project_revisions(project_dir: Path) -> dict[str, str]:
    """Return revisions for chapters, the process story, and the final story.

    The global ``chapter`` hash includes raw index bytes and metadata for every
    chapter it references.  Per-chapter entries make append-only growth safe for
    existing per-chapter artifacts.
    """
    project_dir = Path(project_dir)
    source_dir = project_dir / "原文拆解"
    index_path = source_dir / "_索引.json"
    try:
        index_bytes = index_path.read_bytes()
    except FileNotFoundError:
        index_bytes = b""

    metadata: list[dict[str, Any]] = []
    revisions: dict[str, str] = {}
    for chapter in _chapter_records(index_path):
        if not isinstance(chapter, dict):
            continue
        relative, item = _chapter_metadata(source_dir, chapter)
        metadata.append(item)
        chapter_bytes = json.dumps(
            {"seq": chapter.get("seq"), "metadata": item},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        revision = _digest([b"chapter-entry", chapter_bytes])
        seq = chapter.get("seq")
        if seq is not None:
            revisions[f"chapter:{seq}"] = revision
        if relative:
            revisions[f"chapter:{relative}"] = revision

    metadata_bytes = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    revisions["chapter"] = _digest([b"chapter-index", index_bytes, metadata_bytes])
    revisions["process"] = _file_hash(project_dir / "故事结构_增量.json")
    revisions["final"] = _file_hash(project_dir / "故事结构.json")
    return revisions


def _analysis_task_name(params: dict[str, Any]) -> str:
    value = params.get("analysis_task", params.get("task", params.get("name", "")))
    return str(value).strip().lower().replace("-", "_")


def _chapter_key(params: dict[str, Any], current: dict[str, str]) -> str:
    for name in ("chapter_seq", "seq", "chapter", "chapter_id", "filename"):
        value = params.get(name)
        if value is not None:
            key = f"chapter:{value}"
            if key in current:
                return key
    return "chapter"


def _task_dependencies(task: dict[str, Any], current: dict[str, str]) -> list[str]:
    task_type = str(task.get("task_type", task.get("type", ""))).strip().lower().replace("-", "_")
    params = task.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if task_type == "export":
        source = str(params.get("source", params.get("export_source", "final"))).lower()
        return ["process" if source == "process" else "final"]
    if task_type in {"chapter", "chapter_analysis", "chapter_task"} | _SCOPED_CHAPTER_TASK_TYPES:
        return [_chapter_key(params, current)]
    if task_type == "analysis":
        name = _analysis_task_name(params)
        if name in _CHAPTER_TASK_NAMES:
            return [_chapter_key(params, current)]
        return ["chapter"]
    if task_type in {"process", "delta", "chapter_delta"}:
        return ["chapter", "process"]
    if task_type in {"final", "finalize"}:
        return ["process"]
    return ["chapter", "process", "final"]


def task_is_stale(task: dict, current: dict[str, str]) -> bool:
    """Whether a task's captured inputs no longer match its live dependencies."""
    captured = task.get("input_revision", task.get("input_revisions", {})) or {}
    if not isinstance(captured, dict):
        return True
    return any(captured.get(key) != current.get(key) for key in _task_dependencies(task, current))
