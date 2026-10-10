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
import run_pipeline  # noqa: E402
from prompt_loader import render_prompt  # noqa: E402


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


def test_commit_rejects_top_level_array_with_actionable_repair_context(tmp_path, monkeypatch):
    """A legal JSON array must fail before compression and retain regeneration context."""
    project = tmp_path / "project"
    chapter_dir = project / "原文拆解"
    chapter_dir.mkdir(parents=True)
    chapter = chapter_dir / "第001章_测试.md"
    chapter.write_text("# 测试章节\n", encoding="utf-8")
    (chapter_dir / "_索引.json").write_text(
        json.dumps(
            {"chapters": [{"seq": 1, "filename": chapter.name, "title": "测试"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_pipeline.init_dirs(project)
    paths = run_pipeline.artifact_paths(project, chapter)
    paths["analysis"].write_text("# 合法章节分析\n", encoding="utf-8")
    paths["delta"].write_text('["修改元素"]\n', encoding="utf-8")
    sentinel = "ORIGINAL_DELTA_TASK_CONTEXT"
    paths["delta_task"].write_text(
        f"<!-- delta-contract:{run_pipeline.prompt_fingerprint('流水线/单章增量提取.j2')} -->\n{sentinel}",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_pipeline, "validate_chapter_analysis_file", lambda _path: (True, []))

    rc = run_pipeline.commit_chapter(project, 1)

    assert rc == 1
    report = json.loads(paths["delta_report"].read_text(encoding="utf-8"))
    assert report["errors"] == ["Delta顶层必须是对象，当前为list"]
    repair_pack = paths["repair"].read_text(encoding="utf-8")
    assert sentinel in repair_pack
    assert "Delta顶层必须是对象" in repair_pack
    assert "完整 JSON 对象" in repair_pack


def test_repair_prompt_remains_compatible_with_running_legacy_backend():
    """A hot template update must not crash callers that do not pass base_task yet."""
    rendered = render_prompt(
        "流水线/章节修复.j2",
        chapter_seq=1,
        reason="Delta顶层必须是对象",
        reports=[],
    )

    assert "第001章修复任务" in rendered
    assert "完整 JSON 对象" in rendered
