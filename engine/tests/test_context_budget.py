"""Capacity boundaries are offline and never depend on a live model tokenizer."""
import json

import pytest

from context_budget import ContextBudget, admit_pack, inspect_pack


def test_shared_window_reserves_output_and_overhead():
    budget = ContextBudget(input_tokens=300000, context_tokens=300000)
    assert budget.usable_input == 250000
    assert ContextBudget().usable_input == 250000
    assert ContextBudget(context_tokens=0).usable_input == 270000


def test_overflow_preserves_complete_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "31000")
    pack = tmp_path / "task.md"
    text = "历史事实不可截断。" * 1000
    pack.write_text(text, encoding="utf-8")
    assert not admit_pack(pack)
    assert pack.read_text(encoding="utf-8") == text
    receipt = json.loads(pack.with_suffix(".md.capacity.json").read_text(encoding="utf-8"))
    assert receipt["measurement"] == "conservative_estimate"
    assert receipt["runtime_tool_usage"] == "not_observable_by_cli_adapter"


def test_linked_material_counts_and_exact_repeats_deduplicate(tmp_path):
    pack, source = tmp_path / "task.md", tmp_path / "evidence.md"
    source.write_text("历史原文" * 100, encoding="utf-8")
    pack.write_text("请读取完整原文", encoding="utf-8")
    small = inspect_pack(pack)["estimated_input_tokens"]
    pack.with_suffix(".md.sources.json").write_text(json.dumps([str(source)]), encoding="utf-8")
    linked = inspect_pack(pack)["estimated_input_tokens"]
    assert linked > small
    pack.write_text("请读取完整原文\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    assert inspect_pack(pack)["estimated_input_tokens"] == linked


def test_invalid_limits_fail_explicitly(monkeypatch):
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "1")
    with pytest.raises(ValueError):
        ContextBudget.from_env()


def test_periodic_pack_uses_token_budget_and_invalidates_old_capacity(tmp_path, monkeypatch):
    from test_audit_pack import write_json, empty_story, write_completed_chapter
    from run_pipeline import write_audit_pack, audit_paths
    write_json(tmp_path / "故事结构_增量.json", empty_story())
    chapter = write_completed_chapter(tmp_path, 1)
    chapter.write_text("章节完整证据" * 3000, encoding="utf-8")
    pack = write_audit_pack(tmp_path, 1, 1)
    assert pack is not None
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "31000")
    assert write_audit_pack(tmp_path, 1, 1).with_suffix(".governance.json").is_file()
    status = json.loads(audit_paths(tmp_path, 1, 1)["status"].read_text(encoding="utf-8"))
    assert status["context"]["render_mode"] == "complete_evidence_tree"
    assert chapter.read_text(encoding="utf-8") == "章节完整证据" * 3000


def test_cross_range_failure_never_marks_parent_complete(tmp_path, monkeypatch):
    import run_pipeline as pipeline
    from test_audit_pack import write_json, empty_story, write_no_change_correction
    write_json(tmp_path / "故事结构_增量.json", empty_story())
    for first, last in [(1, 2), (3, 5)]:
        write_no_change_correction(tmp_path, first, last)
        pipeline.update_audit_status(pipeline.audit_paths(tmp_path, first, last), status="committed")
    monkeypatch.setattr(pipeline, "run_generation_worker", lambda *a, **k: 9)
    rc = pipeline.run_child_periodic_governance(tmp_path, 1, 5, [(1, 2), (3, 5)],
                                              200000, "fake", 10, 0, "hidden", "continuous")
    assert rc == 9
    assert pipeline.audit_status(tmp_path, 1, 5)["status"] == "cross_range_pending"


def test_recovery_replays_children_before_parent(tmp_path):
    import run_pipeline as pipeline
    from test_audit_pack import write_json
    for first, last in [(1, 5), (4, 5), (1, 3)]:
        write_json(pipeline.audit_paths(tmp_path, first, last)["status"],
                   {"status": "committed", "range": f"{first:03d}-{last:03d}"})
    due = pipeline.committed_audits_due_for_recovery(tmp_path, 0, 5)
    assert [(item["_start"], item["_end"]) for item in due] == [(1, 3), (4, 5), (1, 5)]
