#!/usr/bin/env python3
"""回填历史 Delta 缺失的 详情.提取理由。

背景：delta_extraction.md 规则5 要求每个 新增元素/修改元素 在 详情 提供非空
提取理由 用于 Delta 审计。validate_delta 现已对 process 模式也强制该校验；
历史 Delta 若缺提取理由会在重放/重校验时失败。本脚本给这类元素补一个**占位**
提取理由（非语义伪造），标记待人工补全，使旧 Delta 通过机械校验。

仅作用于 章节处理/Delta/第NNN章_*.json（Delta 文件）；不读取原文、不生成剧情语义。
不修改 故事结构_增量.json（事件 提取理由 已在合并时落盘，过程库不再回填）。

用法：
    python backfill_extraction_reason.py <project_dir>            # 预览（dry-run）
    python backfill_extraction_reason.py <project_dir> --apply     # 实际回填并备份
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PLACEHOLDER = "（历史回填：提取理由待补，请人工补全该元素为何入库/本章为何更新）"
DELTA_RE = re.compile(r"^第\d+章_.*\.json$")


def _ensure_reason(item: dict) -> bool:
    """给单个元素补 详情.提取理由 占位。返回是否有改动。"""
    if not isinstance(item, dict):
        return False
    detail = item.get("详情")
    if not isinstance(detail, dict):
        detail = {}
        item["详情"] = detail
    reason = detail.get("提取理由")
    if isinstance(reason, str) and reason.strip():
        return False
    detail["提取理由"] = PLACEHOLDER
    return True


def backfill_delta(path: Path) -> int:
    """回填一个 Delta 文件，返回补填的元素个数（0 表示无需改动）。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    changed = 0
    for bucket in ("新增元素", "修改元素"):
        b = data.get(bucket, {})
        if not isinstance(b, dict):
            continue
        for _c_key, items in b.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if _ensure_reason(item):
                    changed += 1
    if changed:
        # 原地改写前先备份。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(path.name + f".bak_reason_{ts}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="回填历史 Delta 缺失的提取理由（默认 dry-run）")
    ap.add_argument("project_dir")
    ap.add_argument("--apply", action="store_true", help="实际回填并备份；缺省仅预览")
    args = ap.parse_args()
    project_dir = Path(args.project_dir).resolve()
    chapter_dir = project_dir / "章节处理" / "Delta"
    if not chapter_dir.is_dir():
        print(f"找不到 章节处理/Delta 目录: {chapter_dir}")
        return 1

    targets = sorted(p for p in chapter_dir.iterdir() if p.is_file() and DELTA_RE.match(p.name))
    if not targets:
        print("未发现 Delta 文件。")
        return 0

    total_changes = 0
    affected_files = 0
    for p in targets:
        if args.apply:
            n = backfill_delta(p)
        else:
            # dry-run：只统计，不写盘。
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            n = 0
            if isinstance(data, dict):
                for bucket in ("新增元素", "修改元素"):
                    b = data.get(bucket, {})
                    if isinstance(b, dict):
                        for items in b.values():
                            if isinstance(items, list):
                                for item in items:
                                    if _ensure_reason(item):
                                        n += 1
        if n:
            affected_files += 1
            total_changes += n
            print(f"  {'[回填]' if args.apply else '[预览]'} {p.name}: 缺提取理由元素 {n} 个")
    if total_changes == 0:
        print(f"所有 {len(targets)} 个 Delta 文件的元素均已含提取理由，无需回填。")
    else:
        print(f"\n共 {'回填' if args.apply else '预览'} {total_changes} 个元素（涉及 {affected_files} 个文件）。")
        if not args.apply:
            print("以上为预览，未改动。加 --apply 实际回填（写盘前自动 .bak_reason 备份）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
