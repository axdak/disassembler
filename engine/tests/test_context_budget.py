"""Capacity boundaries are offline and never depend on a live model tokenizer."""
import json

import pytest

from context_budget import ContextBudget, admit_pack, inspect_pack


def test_shared_window_reserves_output_and_overhead():
    budget = ContextBudget(input_tokens=300000, context_tokens=300000)
    assert budget.usable_input == 250000
    assert ContextBudget().usable_input == 270000


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
