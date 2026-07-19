"""Artifact freshness collector: card status per dimension and per chapter.

For each analysis dimension (whole-book) and each chapter (per-chapter), the
collector derives a card status from three inputs:

1. artifact-on-disk presence (canonical paths come from the engine manifests
   in ``run_pipeline`` — ``ANALYSIS_OUTPUTS``, ``artifact_paths``,
   ``visual_assets_per_chapter_relpaths``, ``chapter_structure_per_chapter_relpaths``);
2. revision freshness — the latest task for the card is stale when its captured
   ``input_revision`` no longer matches the current ``project_revisions`` for the
   dependency keys that dimension relies on (design §6);
3. the latest task status — ``queued`` / ``running`` / ``failed`` / ``cancelled``
   (``interrupted`` maps to ``cancelled``) override presence/staleness.

Card states are exactly: ``missing``, ``latest``, ``stale``, ``queued``,
``running``, ``failed``, ``cancelled``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from models import (
    ArtifactStatusResponse,
    CardStatus,
    ChapterCardStatus,
)

# engine/scripts holds the authoritative output-path manifests; mirror runner.py.
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))

from run_pipeline import (  # noqa: E402
    ANALYSIS_OUTPUTS,
    artifact_paths,
    chapter_structure_per_chapter_relpaths,
    visual_assets_per_chapter_relpaths,
)
from revisions import project_revisions  # noqa: E402

# Whole-book analysis dimensions whose outputs depend on the process story
# structure (design §6: governance invalidates "reports depending on story
# structure").  engine/scripts/analysis_context_pack.py:build_pack loads
# 故事结构_增量.json and appends summarize_story_structure(story) into EVERY
# whole-book analysis pack, so every whole-book analysis dimension consumes
# the process story JSON — not just narrative_structure.  Governance mutates
# 故事结构_增量.json, so every whole-book analysis card goes stale.
_STRUCTURE_DEPENDENT_DIMENSIONS = frozenset(ANALYSIS_OUTPUTS)

# Revision keys each card kind depends on.  Keys map directly to the entries in
# ``project_revisions``: ``chapter`` (global), ``chapter:<seq>`` (per-chapter),
# ``process`` (故事结构_增量.json), ``final`` (故事结构.json).
def _whole_book_deps(dimension: str) -> tuple[str, ...]:
    if dimension in _STRUCTURE_DEPENDENT_DIMENSIONS:
        return ("chapter", "process")
    return ("chapter",)


def _export_deps(source: str) -> tuple[str, ...]:
    # Exports read the story JSON, which is derived from the process JSON, which
    # is derived from chapter text.  Append (chapter change) or governance
    # (process change) both invalidate exports (design §6).
    if source == "process":
        return ("chapter", "process")
    return ("chapter", "process", "final")


_QUEUED_STATES = frozenset({"queued"})
_RUNNING_STATES = frozenset({"running"})
_FAILED_STATES = frozenset({"failed"})
_CANCELLED_STATES = frozenset({"cancelled", "interrupted"})


def _task_type(task: dict) -> str:
    return str(task.get("task_type", task.get("type", ""))).strip().lower().replace("-", "_")


def _analysis_task_name(params: dict) -> str:
    value = params.get("analysis_task", params.get("task", params.get("name", "")))
    return str(value).strip().lower().replace("-", "_")


def _task_param_seq(params: dict) -> Optional[int]:
    for name in ("chapter_seq", "seq", "chapter", "chapter_id"):
        value = params.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _task_param_seqs(params: dict) -> list[int]:
    seqs = params.get("seqs")
    if isinstance(seqs, list):
        out = []
        for value in seqs:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
        return out
    single = _task_param_seq(params)
    return [single] if single is not None else []


def _normalize_task(task: dict) -> dict:
    """Normalize a task dict (handles DB-decoded and workbench-serialized shapes)."""
    captured = task.get("input_revision", task.get("input_revisions", task.get("input_revision_json")))
    if isinstance(captured, str):
        try:
            captured = json.loads(captured)
        except json.JSONDecodeError:
            captured = {}
    if not isinstance(captured, dict):
        captured = {}
    return {
        "id": task.get("id"),
        "task_type": _task_type(task),
        "status": str(task.get("status", "")),
        "params": task.get("params") if isinstance(task.get("params"), dict) else {},
        "input_revision": captured,
        "error": task.get("error"),
        "result": task.get("result") if isinstance(task.get("result"), dict) else {},
    }


def _is_stale(captured: dict, current: dict, deps) -> bool:
    if not captured:
        return True
    for key in deps:
        if captured.get(key) != current.get(key):
            return True
    return False


def _files_present(paths: list[Path]) -> bool:
    if not paths:
        return False
    return all(p.is_file() and p.stat().st_size > 0 for p in paths)


def _whole_book_paths(project_dir: Path, dimension: str) -> list[Path]:
    return [project_dir / rel for rel in ANALYSIS_OUTPUTS.get(dimension, [])]


def _per_chapter_paths(project_dir: Path, chapter: dict, kind: str) -> list[Path]:
    filename = chapter.get("filename", "")
    if kind in ("analysis", "delta"):
        # Reuse the engine's authoritative chapter artifact map.
        if not filename:
            return []
        source = project_dir / "原文拆解" / filename
        paths = artifact_paths(project_dir, source)
        return [paths["analysis"]] if kind == "analysis" else [paths["delta"]]
    if kind == "visual":
        seq = chapter.get("seq")
        if not isinstance(seq, int):
            return []
        return [project_dir / rel for rel in visual_assets_per_chapter_relpaths(seq)]
    if kind == "structure":
        seq = chapter.get("seq")
        if not isinstance(seq, int):
            return []
        return [project_dir / rel for rel in chapter_structure_per_chapter_relpaths(seq)]
    return []


def _export_paths(project_dir: Path, source: str) -> list[Path]:
    # Export adapter writes the obsidian vault + an export_report under _meta.
    out_dir = project_dir / "obsidian-export"
    return [out_dir / "_meta" / "export_report.json"]


# Maps a task to the (dimension_kind, dimension_key, seqs) cards it targets.
# Returns a list of ("dimension", name) or ("per_chapter", kind, seq) or
# ("export", source).
def _task_targets(task: dict) -> list[tuple]:
    ttype = task["task_type"]
    params = task["params"]
    if ttype == "analysis":
        name = _analysis_task_name(params)
        if not name:
            return []
        return [("dimension", name)]
    if ttype == "chapter_visual":
        if params.get("aggregate"):
            return [("dimension", "visual_assets")]
        return [("per_chapter", "visual", s) for s in _task_param_seqs(params)]
    if ttype == "chapter_structure":
        return [("per_chapter", "structure", s) for s in _task_param_seqs(params)]
    if ttype == "chapter_analysis_rerun":
        return [("per_chapter", "analysis", s) for s in _task_param_seqs(params)]
    if ttype == "chapter_delta_rerun":
        return [("per_chapter", "delta", s) for s in _task_param_seqs(params)]
    if ttype == "export":
        source = str(params.get("source", params.get("export_source", "final"))).lower()
        return [("export", source)]
    return []  # append_chapters / governance / recover_story / migrate_chronology / finalize / normalize / repair: no direct card (affect staleness via revisions)


def _card_for(target, current, latest_task, paths_for_target):
    """Compute a CardStatus for a target with its latest task and on-disk paths."""
    paths = paths_for_target(target)
    artifact_present = _files_present(paths)
    path_strings = [str(p) for p in paths]

    if latest_task is None:
        # No task covers this card: presence alone decides.
        status = "latest" if artifact_present else "missing"
        return CardStatus(
            status=status,
            artifact_present=artifact_present,
            paths=path_strings,
        )

    tstatus = latest_task["status"]
    if tstatus in _QUEUED_STATES:
        state = "queued"
    elif tstatus in _RUNNING_STATES:
        state = "running"
    elif tstatus in _FAILED_STATES:
        state = "failed"
    elif tstatus in _CANCELLED_STATES:
        state = "cancelled"
    else:  # done (or unknown terminal) -> check freshness + presence
        deps = _deps_for_target(target, latest_task["params"])
        stale = _is_stale(latest_task["input_revision"], current, deps)
        if stale:
            state = "stale" if artifact_present else "missing"
        else:
            state = "latest" if artifact_present else "missing"

    return CardStatus(
        status=state,
        artifact_present=artifact_present,
        latest_task_id=latest_task["id"],
        latest_task_status=tstatus,
        error=latest_task["error"],
        paths=path_strings,
    )


def _deps_for_target(target, params) -> tuple[str, ...]:
    kind = target[0]
    if kind == "dimension":
        return _whole_book_deps(target[1])
    if kind == "per_chapter":
        _, _kind, seq = target
        key = f"chapter:{seq}"
        return (key,)
    if kind == "export":
        return _export_deps(target[1])
    return ("chapter", "process", "final")


def _paths_for_target(project_dir, target, chapter_lookup) -> list[Path]:
    kind = target[0]
    if kind == "dimension":
        return _whole_book_paths(project_dir, target[1])
    if kind == "per_chapter":
        _, kdim, seq = target
        chapter = chapter_lookup.get(seq, {"filename": "", "seq": seq})
        return _per_chapter_paths(project_dir, chapter, kdim)
    if kind == "export":
        return _export_paths(project_dir, target[1])
    return []


def collect_artifact_status(project_dir, tasks, *, revisions: dict | None = None) -> ArtifactStatusResponse:
    """Build the artifact status response for a project and its task list.

    ``tasks`` is the list of task dicts (as returned by ``Database.list_tasks``
    or ``WorkbenchTaskManager.list`` — both decoded shapes are accepted).

    ``revisions`` is an optional precomputed ``project_revisions(project_dir)``
    result; when supplied it is used instead of recomputing, so callers that
    also call ``WorkbenchTaskManager.list(..., revisions=...)`` can compute it
    once per request.  When None (default) it is computed here, preserving the
    original behavior for callers that don't pass it.
    """
    project_dir = Path(project_dir)
    current = revisions if revisions is not None else project_revisions(project_dir)

    normalized = [_normalize_task(t) for t in tasks]

    # Index chapters from the canonical chapter index.
    index_path = project_dir / "原文拆解" / "_索引.json"
    chapters: list[dict] = []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        raw = data.get("chapters", []) if isinstance(data, dict) else data
        if isinstance(raw, list):
            chapters = [c for c in raw if isinstance(c, dict)]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        chapters = []

    chapter_lookup = {c.get("seq"): c for c in chapters if c.get("seq") is not None}

    # Build target -> latest task mapping (max task id wins; force rerun creates
    # a newer row, so the newest run is reflected — design §6).
    latest_by_target: dict[tuple, dict] = {}
    for task in normalized:
        for target in _task_targets(task):
            existing = latest_by_target.get(target)
            if existing is None or (task["id"] or 0) > (existing["id"] or 0):
                latest_by_target[target] = task

    def paths_for(target):
        return _paths_for_target(project_dir, target, chapter_lookup)

    # Whole-book dimensions: every declared dimension gets a card.
    dimensions: dict[str, CardStatus] = {}
    for dim in ANALYSIS_OUTPUTS:
        target = ("dimension", dim)
        latest = latest_by_target.get(target)
        dimensions[dim] = _card_for(target, current, latest, paths_for)

    # Per-chapter cards.
    chapter_cards: list[ChapterCardStatus] = []
    for ch in chapters:
        seq = ch.get("seq")
        if seq is None:
            continue
        cards: dict[str, CardStatus] = {}
        per_chapter_latest: list[dict] = []
        for kind in ("analysis", "delta", "visual", "structure"):
            target = ("per_chapter", kind, seq)
            latest = latest_by_target.get(target)
            card = _card_for(target, current, latest, paths_for)
            cards[kind] = card
            if latest is not None:
                per_chapter_latest.append(latest)
        stale = any(c.status == "stale" for c in cards.values())
        latest_task = max(per_chapter_latest, key=lambda t: t["id"] or 0) if per_chapter_latest else None
        error = latest_task["error"] if latest_task and latest_task.get("error") else None
        chapter_cards.append(ChapterCardStatus(
            seq=seq,
            filename=ch.get("filename", ""),
            title=ch.get("title", "") or "",
            cards=cards,
            stale=stale,
            latest_task={
                "id": latest_task["id"],
                "status": latest_task["status"],
                "task_type": latest_task["task_type"],
            } if latest_task else None,
            error=error,
        ))

    # Export cards.
    exports: dict[str, CardStatus] = {}
    for source in ("final", "process"):
        target = ("export", source)
        latest = latest_by_target.get(target)
        exports[source] = _card_for(target, current, latest, paths_for)

    # Counts across every card.
    counts: dict[str, int] = {state: 0 for state in (
        "missing", "latest", "stale", "queued", "running", "failed", "cancelled"
    )}
    all_cards = list(dimensions.values())
    for ch in chapter_cards:
        all_cards.extend(ch.cards.values())
    all_cards.extend(exports.values())
    for card in all_cards:
        counts[card.status] = counts.get(card.status, 0) + 1

    # spec §3.1: overview surfaces "已过期分析和导出数量". These are derived
    # summary counts (whole-book analysis dimension cards + export cards in
    # stale state); they are NOT card states and excluded from the total-card
    # invariant asserted by tests.
    counts["stale_analysis"] = sum(1 for c in dimensions.values() if c.status == "stale")
    counts["stale_export"] = sum(1 for c in exports.values() if c.status == "stale")

    return ArtifactStatusResponse(
        dimensions=dimensions,
        chapters=chapter_cards,
        exports=exports,
        counts=counts,
    )


# Helper retained for /chapters endpoint: build the per-chapter card map only.
def chapter_cards(project_dir, tasks, *, revisions: dict | None = None) -> list[ChapterCardStatus]:
    return collect_artifact_status(project_dir, tasks, revisions=revisions).chapters


# Helper retained for the project status endpoint: summary counts.
def task_summary(tasks) -> dict[str, int]:
    summary: dict[str, int] = {}
    for raw in tasks:
        status = str(raw.get("status", ""))
        if not status:
            continue
        summary[status] = summary.get(status, 0) + 1
    return summary


def stale_task_count(project_dir, tasks, *, revisions: dict | None = None) -> int:
    project_dir = Path(project_dir)
    current = revisions if revisions is not None else project_revisions(project_dir)
    from revisions import task_is_stale
    count = 0
    for raw in tasks:
        if task_is_stale(raw, current):
            count += 1
    return count
