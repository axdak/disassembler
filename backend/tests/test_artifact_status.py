"""TDD tests for artifact status collector and DTOs (Task 5)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# engine/scripts must be importable for the canonical path helpers.
_BACKEND = Path(__file__).resolve().parent.parent
_ENGINE_SCRIPTS = _BACKEND.parent / "engine" / "scripts"
for _p in (_BACKEND, _ENGINE_SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from run_pipeline import (  # noqa: E402
    ANALYSIS_OUTPUTS,
    artifact_paths,
    chapter_structure_per_chapter_relpaths,
    visual_assets_per_chapter_relpaths,
)
from revisions import project_revisions  # noqa: E402

from artifact_status import (  # noqa: E402  -- RED: module absent until Step 3
    ArtifactStatusResponse,
    ChapterCardStatus,
    collect_artifact_status,
)


CARD_STATES = ("missing", "latest", "stale", "queued", "running", "failed", "cancelled")


# ---- fixtures ----------------------------------------------------------

def _write_index(project: Path, chapters: list[dict]) -> None:
    src = project / "原文拆解"
    src.mkdir(parents=True, exist_ok=True)
    for ch in chapters:
        (src / ch["filename"]).write_text(ch.get("content", "body"), encoding="utf-8")
    (src / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )


def _chapter(seq, title="章", content="正文"):
    return {"seq": seq, "title": title, "filename": f"第{seq:03d}章_{title}.md", "content": content}


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _touch_md(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# content", encoding="utf-8")


def _task(tid, task_type, status, params, revision, *, error=None, result=None):
    return {
        "id": tid,
        "task_type": task_type,
        "status": status,
        "params": params,
        "input_revision": dict(revision),
        "error": error,
        "result": result,
    }


def _seed_project(project: Path, chapters):
    _write_index(project, chapters)
    (project / "故事结构_增量.json").write_text('{"阶段":"过程"}', encoding="utf-8")
    (project / "故事结构.json").write_text('{"阶段":"最终"}', encoding="utf-8")
    return project_revisions(project)


def _seed_per_chapter_artifacts(project: Path, chapter: dict) -> None:
    seq = chapter["seq"]
    fn = chapter["filename"]
    stem = Path(fn).stem
    _touch_md(project / "章节处理" / fn)  # analysis MD
    _touch(project / "章节处理" / f"{stem}.json")  # delta JSON
    for rel in visual_assets_per_chapter_relpaths(seq):
        _touch_md(project / rel)
    for rel in chapter_structure_per_chapter_relpaths(seq):
        _touch_md(project / rel)


def _seed_whole_book_artifact(project: Path, dimension: str) -> None:
    for rel in ANALYSIS_OUTPUTS[dimension]:
        _touch_md(project / rel)


# ---- Step 1: every card state is reachable -----------------------------

def test_all_seven_card_states_reachable(tmp_path):
    project = tmp_path / "项目"
    old_rev = _seed_project(project, [_chapter(1)])
    # whole-book artifacts on disk
    _seed_whole_book_artifact(project, "summary")
    _seed_whole_book_artifact(project, "characters")
    _seed_whole_book_artifact(project, "report")  # no task -> latest (artifact only)
    # no artifact, no task for detailed_outline -> missing

    # mutate chapter content; tasks captured before the mutation go stale,
    # tasks captured after stay latest.
    (project / "原文拆解" / _chapter(1)["filename"]).write_text("changed", encoding="utf-8")
    current = project_revisions(project)

    tasks = [
        _task(1, "analysis", "done", {"task": "summary"}, current),        # latest
        _task(2, "analysis", "done", {"task": "characters"}, old_rev),      # stale
        _task(3, "analysis", "queued", {"task": "plot"}, current),         # queued
        _task(4, "analysis", "running", {"task": "worldview"}, current),   # running
        _task(5, "analysis", "failed", {"task": "settings"}, current, error="boom"),  # failed
        _task(6, "analysis", "cancelled", {"task": "outline"}, current),   # cancelled
    ]
    resp = collect_artifact_status(project, tasks)
    assert isinstance(resp, ArtifactStatusResponse)
    assert resp.dimensions["summary"].status == "latest"
    assert resp.dimensions["characters"].status == "stale"
    assert resp.dimensions["plot"].status == "queued"
    assert resp.dimensions["worldview"].status == "running"
    assert resp.dimensions["settings"].status == "failed"
    assert resp.dimensions["outline"].status == "cancelled"
    assert resp.dimensions["report"].status == "latest"  # artifact, no task
    assert resp.dimensions["detailed_outline"].status == "missing"
    # all 7 states observed across the cards
    observed = {c.status for c in resp.dimensions.values()}
    observed |= {c.status for ch in resp.chapters for c in ch.cards.values()}
    observed |= {c.status for c in resp.exports.values()}
    assert set(CARD_STATES) <= observed, observed


# ---- Step 1: paths come from engine manifests -------------------------

def test_card_paths_come_from_engine_manifests(tmp_path):
    project = tmp_path / "项目"
    rev = _seed_project(project, [_chapter(1)])
    _seed_whole_book_artifact(project, "summary")
    _seed_per_chapter_artifacts(project, _chapter(1))

    resp = collect_artifact_status(project, [_task(1, "analysis", "done", {"task": "summary"}, rev)])
    # whole-book card paths equal engine ANALYSIS_OUTPUTS entries
    assert resp.dimensions["summary"].paths == [
        str(project / rel) for rel in ANALYSIS_OUTPUTS["summary"]
    ]
    # per-chapter card paths equal engine per-chapter relpaths
    ch = resp.chapters[0]
    assert ch.cards["analysis"].paths == [str(project / "章节处理" / "第001章_章.md")]
    assert ch.cards["delta"].paths == [str(project / "章节处理" / "第001章_章.json")]
    assert ch.cards["visual"].paths == [
        str(project / rel) for rel in visual_assets_per_chapter_relpaths(1)
    ]
    assert ch.cards["structure"].paths == [
        str(project / rel) for rel in chapter_structure_per_chapter_relpaths(1)
    ]


# ---- Step 1: append invalidation mapping -------------------------------

def test_append_invalidates_whole_book_and_new_chapters_only(tmp_path):
    project = tmp_path / "项目"
    chapters = [_chapter(1), _chapter(2)]
    rev = _seed_project(project, chapters)
    for ch in chapters:
        _seed_per_chapter_artifacts(project, ch)
    # whole-book artifacts present, captured at the pre-append revision
    _seed_whole_book_artifact(project, "summary")
    _seed_whole_book_artifact(project, "narrative_structure")
    _seed_whole_book_artifact(project, "visual_assets")
    _touch_md(project / "obsidian-export" / "_meta" / "export_report.json")

    tasks = [
        _task(1, "analysis", "done", {"task": "summary"}, rev),
        _task(2, "analysis", "done", {"task": "narrative_structure"}, rev),
        _task(3, "chapter_visual", "done", {"seqs": [1, 2], "aggregate": True}, rev),
        _task(4, "export", "done", {"source": "final"}, rev),
    ]

    # append a 3rd chapter
    chapters.append(_chapter(3))
    _write_index(project, chapters)
    (project / "原文拆解" / chapters[-1]["filename"]).write_text(chapters[-1]["content"], encoding="utf-8")
    current = project_revisions(project)

    resp = collect_artifact_status(project, tasks)
    # new chapter's per-chapter artifacts = missing (no artifacts, no tasks)
    ch3 = next(c for c in resp.chapters if c.seq == 3)
    assert ch3.cards["analysis"].status == "missing"
    assert ch3.cards["delta"].status == "missing"
    assert ch3.cards["visual"].status == "missing"
    assert ch3.cards["structure"].status == "missing"
    # unmodified old chapters' per-chapter artifacts = latest (per-chapter rev unchanged)
    ch1 = next(c for c in resp.chapters if c.seq == 1)
    assert ch1.cards["analysis"].status == "latest"
    assert ch1.cards["delta"].status == "latest"
    assert ch1.cards["visual"].status == "latest"
    assert ch1.cards["structure"].status == "latest"
    # all whole-book analysis reports = stale
    assert resp.dimensions["summary"].status == "stale"
    assert resp.dimensions["narrative_structure"].status == "stale"
    assert resp.dimensions["visual_assets"].status == "stale"  # whole-book visual aggregate
    # exports = stale
    assert resp.exports["final"].status == "stale"
    # stale + missing counts
    assert resp.counts["stale"] >= 3
    assert resp.counts["missing"] >= 4
    # spec §3.1 overview derived counts: stale whole-book analysis + stale export
    assert resp.counts["stale_analysis"] >= 3
    assert resp.counts["stale_export"] >= 1


# ---- Step 1: governance invalidation mapping ---------------------------

def test_governance_invalidates_all_whole_book_reports_and_exports_only(tmp_path):
    project = tmp_path / "项目"
    rev = _seed_project(project, [_chapter(1)])
    _seed_per_chapter_artifacts(project, _chapter(1))
    # Seed every whole-book analysis artifact so each has a card to mark stale.
    for dim in ANALYSIS_OUTPUTS:
        _seed_whole_book_artifact(project, dim)
    _touch_md(project / "obsidian-export" / "_meta" / "export_report.json")

    # Give every whole-book analysis dimension a task captured at the
    # pre-governance revision; governance then mutates the process story JSON,
    # and every whole-book analysis pack consumes that JSON (engine build_pack
    # appends summarize_story_structure into every whole-book pack), so every
    # whole-book analysis card must go stale.
    tasks = [_task(i + 1, "analysis", "done", {"task": dim}, rev)
             for i, dim in enumerate(ANALYSIS_OUTPUTS)]
    tasks.append(_task(len(tasks) + 1, "chapter_visual", "done", {"seqs": [1], "aggregate": True}, rev))
    tasks.append(_task(len(tasks) + 2, "export", "done", {"source": "final"}, rev))
    tasks.append(_task(len(tasks) + 3, "export", "done", {"source": "process"}, rev))

    # governance changes the process story JSON only
    (project / "故事结构_增量.json").write_text('{"阶段":"过程已治理"}', encoding="utf-8")

    resp = collect_artifact_status(project, tasks)
    # chapter analysis / per-chapter NOT invalidated (governance edits process JSON only)
    assert resp.chapters[0].cards["analysis"].status == "latest"
    assert resp.chapters[0].cards["delta"].status == "latest"
    assert resp.chapters[0].cards["visual"].status == "latest"
    assert resp.chapters[0].cards["structure"].status == "latest"
    # engine build_pack appends the process story summary into EVERY whole-book
    # analysis pack, so governance marks ALL whole-book analysis cards stale.
    for dim in ANALYSIS_OUTPUTS:
        assert resp.dimensions[dim].status == "stale", dim
    # final JSON + all exports = stale
    assert resp.exports["final"].status == "stale"
    assert resp.exports["process"].status == "stale"


# ---- Step 1: force rerun preserves task history, card reflects newest --

def test_force_rerun_newest_run_wins_and_history_preserved(tmp_path):
    project = tmp_path / "项目"
    rev = _seed_project(project, [_chapter(1)])
    _seed_whole_book_artifact(project, "summary")

    # old failed run, then a fresh successful run at current revision
    tasks = [
        _task(1, "analysis", "failed", {"task": "summary"}, rev, error="boom"),
        _task(2, "analysis", "done", {"task": "summary", "force": True}, rev),
    ]
    resp = collect_artifact_status(project, tasks)
    assert resp.dimensions["summary"].status == "latest"
    assert resp.dimensions["summary"].latest_task_id == 2


def test_counts_total_cards_correctly(tmp_path):
    project = tmp_path / "项目"
    rev = _seed_project(project, [_chapter(1)])
    _seed_whole_book_artifact(project, "summary")
    tasks = [_task(1, "analysis", "done", {"task": "summary"}, rev)]
    resp = collect_artifact_status(project, tasks)
    # Card-state counts (the seven states) sum to total cards. The derived
    # stale_analysis/stale_export summary keys (spec §3.1) are excluded.
    card_states = ("missing", "latest", "stale", "queued", "running", "failed", "cancelled")
    total = sum(resp.counts.get(s, 0) for s in card_states)
    # dimensions (12) + per-chapter cards (4) + exports (2) = 18 cards
    assert total == len(resp.dimensions) + sum(len(c.cards) for c in resp.chapters) + len(resp.exports)
    # Derived overview counts are present (spec §3.1).
    assert "stale_analysis" in resp.counts
    assert "stale_export" in resp.counts


def test_chapter_row_exposes_latest_task_and_error(tmp_path):
    project = tmp_path / "项目"
    rev = _seed_project(project, [_chapter(1)])
    _seed_per_chapter_artifacts(project, _chapter(1))
    tasks = [
        _task(1, "chapter_analysis_rerun", "failed", {"seqs": [1]}, rev, error="rerun boom"),
    ]
    resp = collect_artifact_status(project, tasks)
    ch = resp.chapters[0]
    assert ch.latest_task["id"] == 1
    assert ch.latest_task["status"] == "failed"
    assert ch.error == "rerun boom"
    assert ch.cards["analysis"].status == "failed"
