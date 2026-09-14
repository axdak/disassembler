"""Shared freshness checks for worker-owned output artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping


OutputSignature = tuple[int, int] | None


def confined_project_path(project_dir: Path, value: str | Path) -> Path:
    """Resolve a manifest path and reject targets outside the project root."""
    base = Path(project_dir).resolve()
    target = (base / value).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"artifact path escapes project directory: {value}")
    return target


def output_signature(path: Path) -> OutputSignature:
    """Return metadata that changes when a worker creates or rewrites a file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def capture_output_signatures(paths: Iterable[Path]) -> dict[Path, OutputSignature]:
    """Snapshot expected outputs immediately before invoking one worker attempt."""
    return {path: output_signature(path) for path in paths}


def unchanged_existing_outputs(
    before: Mapping[Path, OutputSignature], expected_outputs: Iterable[Path]
) -> list[Path]:
    """Return outputs that existed before the attempt and were not refreshed."""
    return [
        path
        for path in expected_outputs
        if before.get(path) is not None and output_signature(path) == before[path]
    ]


def invalid_output_artifacts(expected_outputs: Iterable[Path]) -> list[Path]:
    """Return outputs that are empty/whitespace or invalid JSON by extension."""
    invalid: list[Path] = []
    for path in expected_outputs:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            invalid.append(path)
            continue
        if not content.strip():
            invalid.append(path)
            continue
        if path.suffix.lower() == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError:
                invalid.append(path)
    return invalid
