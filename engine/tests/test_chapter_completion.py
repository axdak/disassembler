#!/usr/bin/env python3
"""Completion checks must reject stale or malformed governance artifacts."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from run_pipeline import is_chapter_complete


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def build_project(root: Path) -> Path:
    chapter = root / "原文拆解" / "第001章_测试.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("# 测试", encoding="utf-8")
    (root / "章节处理").mkdir(parents=True, exist_ok=True)
    (root / "章节处理" / chapter.name).write_text("analysis", encoding="utf-8")
    write_json(root / "章节处理" / "第001章_测试.json", {})
    for name in ("story_before_ch001.json", "story_after_ch001.json"):
        write_json(root / "故事结构版本" / name, {})
    write_json(root / "结构变更日志" / "diff_ch001.json", {})
    report = {"passed": True, "mode": "process", "errors": [], "warnings": []}
    write_json(root / "质量治理" / "delta校验" / "第001章_测试.json", report)
    write_json(root / "质量治理" / "章节校验" / "第001章_测试.json", report)
    return chapter


def test_completion_requires_report_status_and_valid_artifacts():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        chapter = build_project(root)
        with patch("run_pipeline.validate_structure_file", return_value=(True, [], [])):
            assert is_chapter_complete(root, chapter)

            report = root / "质量治理" / "delta校验" / "第001章_测试.json"
            write_json(report, {"passed": False, "mode": "process", "errors": ["bad"], "warnings": []})
            assert not is_chapter_complete(root, chapter)

            write_json(report, {"passed": True, "mode": "process", "errors": [], "warnings": []})
            (root / "结构变更日志" / "diff_ch001.json").write_text("not json", encoding="utf-8")
            assert not is_chapter_complete(root, chapter)


def test_completion_revalidates_after_snapshot():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        chapter = build_project(root)
        with patch("run_pipeline.validate_structure_file", return_value=(False, ["invalid"], [])):
            assert not is_chapter_complete(root, chapter)

