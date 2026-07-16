#!/usr/bin/env python3
"""Tests for repair_llm_json.py — LLM JSON syntax repair preflight."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from repair_llm_json import repair_llm_json_file  # noqa: E402


def test_repairs_markdown_fenced_json_with_single_quotes_and_trailing_commas(tmp_path):
    target = tmp_path / "delta.json"
    target.write_text(
        """```json
{
  '章节': '第001章',
  '新增元素': {'角色集': [],},
  '修改元素': {'角色集': [],},
}
```
""",
        encoding="utf-8",
    )

    report = repair_llm_json_file(target)

    assert report["input_was_valid_json"] is False
    assert report["repaired"] is True
    assert report["output_valid_json"] is True
    repaired = json.loads(target.read_text(encoding="utf-8"))
    assert repaired == {
        "章节": "第001章",
        "新增元素": {"角色集": []},
        "修改元素": {"角色集": []},
    }
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_pretty_prints_already_valid_json_without_repair(tmp_path):
    target = tmp_path / "delta.json"
    target.write_text('{"章节":"第001章","新增元素":{},"修改元素":{}}', encoding="utf-8")

    report = repair_llm_json_file(target)

    assert report["input_was_valid_json"] is True
    assert report["repaired"] is False
    assert json.loads(target.read_text(encoding="utf-8"))["章节"] == "第001章"
    assert "\n  \"章节\": \"第001章\"" in target.read_text(encoding="utf-8")


def test_writes_backup_and_report_when_requested(tmp_path):
    target = tmp_path / "delta.json"
    report_path = tmp_path / "report.json"
    target.write_text("{'章节': '第001章', '新增元素': {}, '修改元素': {}}", encoding="utf-8")

    report = repair_llm_json_file(target, report_path=report_path, backup=True)

    backup_path = Path(report["backup_path"])
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8").startswith("{'章节'")
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted_report["repaired"] is True
    assert persisted_report["output_valid_json"] is True


def test_raises_without_modifying_file_when_repair_cannot_produce_json(tmp_path):
    target = tmp_path / "delta.json"
    original = "这完全不是 JSON，也没有对象边界"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        repair_llm_json_file(target)

    assert target.read_text(encoding="utf-8") == original
