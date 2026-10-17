"""简介的确定性展示排版：保留正文和次序，只整理已有标题、换行及空行。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from project_lock import project_write_lock
from synopsis_contract import synopsis_section_headings
from worker_checkpoint import atomic_write_bytes


def format_synopsis_text(description: str) -> str:
    """整理已有分节；不分类情节、不补齐四节、不截短或改写自然段。"""
    if not isinstance(description, str):
        raise TypeError("简介必须为字符串")
    headings = "|".join(re.escape(value) for value in synopsis_section_headings())
    heading_line = re.compile(
        rf"^(?:\#{{1,6}}[ \t]+)?(?:\*\*(?P<bold>{headings})\*\*|(?P<plain>{headings}))[ \t]*(?P<body>.*)$"
    )
    lines: list[str] = []
    for original in description.splitlines():
        line = original.strip()
        match = heading_line.fullmatch(line)
        if match:
            if lines and lines[-1]:
                lines.append("")
            lines.append(match["bold"] or match["plain"])
            if match["body"]:
                lines.append(match["body"])
        elif line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def format_delivery_synopsis(project: Path) -> bool:
    """仅在交付成功后整理草稿简介；原过程库不动，写入前保留原稿。"""
    project = Path(project)
    with project_write_lock(project):
        draft = project / "故事结构_草稿.json"
        raw = draft.read_bytes()
        story = json.loads(raw)
        intro = story.get("介绍") if isinstance(story, dict) else None
        description = intro.get("描述") if isinstance(intro, dict) else None
        if not isinstance(description, str):
            return False  # 类型与缺项仍由原严格校验报告，排版不替它补默认值。
        formatted = format_synopsis_text(description)
        if formatted == description:
            return False
        backup = project / "质量治理/最终审计/交付" / f"synopsis_before_{hashlib.sha256(raw).hexdigest()}.json"
        atomic_write_bytes(backup, raw)
        intro["描述"] = formatted
        atomic_write_bytes(draft, (json.dumps(story, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return True
