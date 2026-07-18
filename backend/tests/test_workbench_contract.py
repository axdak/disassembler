"""Workbench param-contract alignment check.

Locks the public param shapes the frontend sends (spec §8) to the backend
adapters' accepted param names, so the per-side fakes that masked the
param-contract drift in the per-task reviews cannot recur. This is a
param-acceptance check only — it does NOT execute engine work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from task_adapters import (
    AnalysisAdapter,
    ExportAdapter,
    GovernanceTaskAdapter,
)

_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))


# --- Analysis (spec §8/§3.3) -------------------------------------------------

def test_analysis_accepts_spec_param_shape():
    params = {
        "task": "plotlines",
        "chapters": "1-120",
        "targets": ["角色:林婉"],
        "question": "",
        "original_text": "none",
        "force": False,
    }
    parsed = AnalysisAdapter._parse_params(params)
    assert parsed["task"] == "plotlines"
    assert parsed["chapters"] == "1-120"
    assert parsed["targets"] == ["角色:林婉"]
    assert parsed["question"] == ""
    # original_text (the UI's name) must flow through to the engine param.
    assert parsed["include_original"] == "none"
    assert parsed["force"] is False


def test_analysis_original_text_alias_overrides_default():
    parsed = AnalysisAdapter._parse_params({"original_text": "full"})
    assert parsed["include_original"] == "full"


def test_analysis_legacy_include_original_alias_still_accepted():
    parsed = AnalysisAdapter._parse_params({"include_original": "summary"})
    assert parsed["include_original"] == "summary"


# --- Export (spec §3.5/§8) ----------------------------------------------------

def test_export_translate_kinds_maps_spec_kinds():
    flags = ExportAdapter._translate_kinds(["single", "vault", "dataview", "canvas"])
    assert flags == {"single": True, "dataview": True, "canvas": True}


def test_export_translate_kinds_empty_is_all_false():
    flags = ExportAdapter._translate_kinds(None)
    assert flags == {"single": False, "dataview": False, "canvas": False}


def test_export_translate_kinds_vault_has_no_flag():
    # "vault" is always produced (the default Obsidian vault output); it must
    # not raise and must not toggle single/dataview/canvas.
    flags = ExportAdapter._translate_kinds(["vault"])
    assert flags == {"single": False, "dataview": False, "canvas": False}


# --- Governance (spec §8) ----------------------------------------------------

def _seed_index(project: Path, seqs: list[int]) -> None:
    (project / "原文拆解").mkdir(parents=True, exist_ok=True)
    chapters = [
        {"seq": s, "filename": f"第{s:03d}章_测试.md", "title": f"第{s}章"} for s in seqs
    ]
    (project / "原文拆解" / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )


def test_governance_chapters_range_string_parsed(tmp_path):
    project = tmp_path / "proj"
    _seed_index(project, [1, 2, 3])
    start, end = GovernanceTaskAdapter._resolve_range(project, {"chapters": "101-150"})
    assert (start, end) == (101, 150)


def test_governance_chapters_all_derives_from_index(tmp_path):
    project = tmp_path / "proj"
    _seed_index(project, [5, 10, 1])
    start, end = GovernanceTaskAdapter._resolve_range(project, {"chapters": "all"})
    assert (start, end) == (1, 10)


def test_governance_chapters_recent_falls_back_to_index_when_resolved(tmp_path):
    # No unresolved audit status files → "recent" falls back to whole-book range.
    project = tmp_path / "proj"
    _seed_index(project, [1, 2])
    start, end = GovernanceTaskAdapter._resolve_range(project, {"chapters": "recent"})
    assert (start, end) == (1, 2)


def test_governance_start_end_fallback_still_accepted(tmp_path):
    project = tmp_path / "proj"
    _seed_index(project, [1, 2])
    start, end = GovernanceTaskAdapter._resolve_range(project, {"start": 5, "end": 8})
    assert (start, end) == (5, 8)


def test_governance_chapters_missing_without_start_end_raises(tmp_path):
    project = tmp_path / "proj"
    _seed_index(project, [1, 2])
    with pytest.raises(ValueError):
        GovernanceTaskAdapter._resolve_range(project, {})


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
