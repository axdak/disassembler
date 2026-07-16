#!/usr/bin/env python3
"""将既有故事结构迁移到章节时间、紧凑追溯字段和事件顺序规范。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from chronology import assign_event_group_orders, first_involved_chapter
from normalize_story_schema import normalize_structure


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def index_sequences(project_dir: Path) -> list[int]:
    index_path = project_dir / "原文拆解" / "_索引.json"
    if not index_path.is_file():
        raise ValueError(f"缺少章节索引: {index_path}")
    index = load_json(index_path)
    chapters = index.get("chapters") if isinstance(index, dict) else None
    if not isinstance(chapters, list):
        raise ValueError("_索引.json.chapters 必须是数组")
    sequences = [item.get("seq") for item in chapters if isinstance(item, dict) and not item.get("is_preface")]
    if not sequences or any(not isinstance(seq, int) or seq < 1 for seq in sequences):
        raise ValueError("_索引.json 不包含有效的非前言章节 seq")
    if len(set(sequences)) != len(sequences):
        raise ValueError("_索引.json 存在重复 seq，无法作为章节时间唯一依据")
    return sorted(sequences)


def migrate_story(data: Any) -> tuple[dict[str, Any], list[str]]:
    story, logs = normalize_structure(data)
    events = story["事件集"]
    # 这是一次性迁移：稳定排序建立“数组顺序即章节逻辑顺序”的新约定。
    events.sort(key=lambda event: first_involved_chapter(event) if first_involved_chapter(event) is not None else 10**12)
    assign_event_group_orders(events, split_repeated=True)
    logs.append("事件集已按最小涉及章节稳定重排，并改为剧情段序号分组；后续治理修改既有事件不得移动数组位置")
    return story, logs


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移故事结构的章节时间和事件分组字段")
    parser.add_argument("project_dir")
    parser.add_argument("--file", default="故事结构_增量.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    story_path = project_dir / args.file
    if not story_path.is_file():
        print(f"故事结构不存在: {story_path}")
        return 1
    try:
        sequences = index_sequences(project_dir)
        source = load_json(story_path)
        migrated, logs = migrate_story(source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"迁移失败: {exc}")
        return 1

    print(f"章节索引已确认: {len(sequences)} 个seq，范围 {sequences[0]}-{sequences[-1]}")
    print(f"迁移记录: {len(logs)} 条")
    if args.dry_run:
        print("dry-run：未写入文件。")
        return 0

    backup = story_path.with_name(story_path.name + ".before_chronology_migration.bak")
    if not backup.exists():
        shutil.copy2(story_path, backup)
    story_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
    report = project_dir / "质量治理" / "规范化" / "chronology_migration.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(logs) + "\n", encoding="utf-8")
    print(f"迁移完成: {story_path}")
    print(f"备份: {backup}")
    print(f"迁移报告: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
