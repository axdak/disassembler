#!/usr/bin/env python3
"""Mechanical quality gate for a single chapter analysis Markdown file."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Tuple


REQUIRED_HEADINGS = [
    "## 1. 剧情梗概",
    "## 2. 出场人物",
    "## 3. 核心冲突",
    "## 4. 信息增量",
    "## 5. 伏笔与悬念",
    "## 6. 爽点 / 虐点 / 情绪点",
    "## 7. 章节功能判断",
    "## 8. 事件分组与标签建议",
    "## 9. 画面 / 分镜 / 视觉资产候选",
    "## 10. 结构提取提示",
]
MIN_ANALYSIS_TEXT_LENGTH = 200


def _section_content(text: str, heading: str, next_heading: str | None) -> str:
    start = text.find(heading) + len(heading)
    end = text.find(next_heading, start) if next_heading else len(text)
    return text[start:end].strip()


def validate_chapter_analysis_text(text: str) -> List[str]:
    """Return structural errors without judging literary claims or inventing content."""
    errors: List[str] = []
    if not text.strip():
        return ["章节分析MD为空"]
    if not text.lstrip().startswith("# "):
        errors.append("缺少一级章节标题")

    positions = [text.find(heading) for heading in REQUIRED_HEADINGS]
    for heading, position in zip(REQUIRED_HEADINGS, positions):
        if position < 0:
            errors.append(f"缺少必填章节[{heading}]")
    present_positions = [position for position in positions if position >= 0]
    if len(present_positions) == len(REQUIRED_HEADINGS) and present_positions != sorted(present_positions):
        errors.append("10个必填章节的顺序错误")

    if errors:
        return errors

    for index, heading in enumerate(REQUIRED_HEADINGS):
        next_heading = REQUIRED_HEADINGS[index + 1] if index + 1 < len(REQUIRED_HEADINGS) else None
        if not _section_content(text, heading, next_heading):
            errors.append(f"章节[{heading}]内容为空；无信息时应说明本章无明确新增及原因")

    visual_section = _section_content(text, REQUIRED_HEADINGS[8], REQUIRED_HEADINGS[9])
    if "|" not in visual_section and "无高价值画面候选" not in visual_section:
        errors.append("章节[## 9. 画面 / 分镜 / 视觉资产候选]必须保留候选表或写明无高价值画面候选")

    compact_length = len("".join(text.split()))
    if compact_length < MIN_ANALYSIS_TEXT_LENGTH:
        errors.append(f"章节分析内容明显过短（至少{MIN_ANALYSIS_TEXT_LENGTH}字，实际{compact_length}字）")
    return errors


def validate_chapter_analysis_file(path: Path) -> Tuple[bool, List[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, [f"读取章节分析MD失败: {exc}"]
    errors = validate_chapter_analysis_text(text)
    return not errors, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验章节分析MD的固定10维结构")
    parser.add_argument("path")
    args = parser.parse_args()
    path = Path(args.path)
    ok, errors = validate_chapter_analysis_file(path)
    if ok:
        print(f"章节分析MD结构通过: {path}")
        return 0
    print(f"章节分析MD结构不通过: {path}")
    for error in errors:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
