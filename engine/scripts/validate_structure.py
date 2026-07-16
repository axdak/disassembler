#!/usr/bin/env python3
"""
故事结构 JSON 校验器。

三档模式：
  --mode process    逐章过程库：卡结构/字段/类型；前向引用、数量不足为 warning。
  --mode governance 周期/按需治理后：引用错误为 error；事件发生地点的未注册细粒度子空间为 warning；数量不足为 warning。
  --mode final      最终交付：强 Schema、引用、最低数量、介绍完整性均为 error。

兼容：
  --strict 等价于 --mode final
  --chapter-check <story.json> <delta.json> 默认按 process 模式校验合并后的全量过程库，
  同时报告本章 Delta 触碰元素数量；它不是仅检查本章触碰元素的局部校验。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

from story_schema_rules import COLLECTION_KEYS, validate_exact_story_schema


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_stdio()


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_structure_file(path: str, mode: str = "process") -> Tuple[bool, List[str], List[str]]:
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return False, [f"JSON格式错误: {exc}"], []
    except OSError as exc:
        return False, [f"读取失败: {exc}"], []
    errors, warnings = validate_exact_story_schema(data, mode=mode)
    return not errors, errors, warnings


def touched_names_from_delta(delta: Dict[str, Any]) -> Dict[str, set]:
    touched = {k: set() for k in COLLECTION_KEYS}
    for section in ["新增元素", "修改元素"]:
        bucket = delta.get(section, {}) if isinstance(delta, dict) else {}
        if not isinstance(bucket, dict):
            continue
        for key in COLLECTION_KEYS:
            for item in bucket.get(key, []) or []:
                if isinstance(item, dict) and isinstance(item.get("名称"), str) and item["名称"].strip():
                    touched[key].add(item["名称"].strip())
    return touched


def print_report(title: str, path: str, mode: str, errors: List[str], warnings: List[str]) -> None:
    print(title)
    print(f"文件: {path}")
    print(f"模式: {mode}")
    print("=" * 70)
    if errors:
        print(f"\n发现 {len(errors)} 个错误：")
        for i, e in enumerate(errors, 1):
            print(f"  {i}. {e}")
    if warnings:
        print(f"\n发现 {len(warnings)} 个警告：")
        for i, w in enumerate(warnings, 1):
            print(f"  {i}. {w}")
    if not errors and not warnings:
        print("\n校验完全通过。")
    elif not errors:
        print(f"\n校验通过，但有 {len(warnings)} 个警告。")
    else:
        print("\n校验失败。")


def write_json_report(path: str, passed: bool, mode: str, errors: List[str], warnings: List[str]) -> None:
    """Persist validator state for recovery and completion checks."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"passed": passed, "mode": mode, "errors": errors, "warnings": warnings},
            f,
            ensure_ascii=False,
            indent=2,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="故事结构JSON校验器")
    parser.add_argument("--mode", choices=["process", "governance", "final"], default="process")
    parser.add_argument("--strict", action="store_true", help="等价于 --mode final")
    parser.add_argument("--chapter-check", action="store_true", help="章节合并后全量过程库校验；参数为 story.json delta.json")
    parser.add_argument("--report-json", help="write machine-readable validation status to this JSON file")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    mode = "final" if args.strict else args.mode

    if args.chapter_check:
        if len(args.paths) != 2:
            print("用法: python validate_structure.py --chapter-check <故事结构JSON> <章节Delta.json> [--mode process|governance]")
            return 1
        story_path, delta_path = args.paths
        ok, errors, warnings = validate_structure_file(story_path, mode=mode)
        try:
            delta = load_json(delta_path)
            touched = touched_names_from_delta(delta)
            touched_total = sum(len(v) for v in touched.values())
            warnings.insert(0, f"章节Delta触碰元素数: {touched_total}；本模式执行合并后全量过程库校验，不是仅检查本章触碰元素；过程阶段前向引用仅警告")
        except Exception as exc:
            warnings.insert(0, f"章节Delta读取失败，仅完成合并后全量过程库校验: {exc}")
        if args.report_json:
            write_json_report(args.report_json, ok, mode, errors, warnings)
        print_report("=== 章节合并后全量过程库校验 ===", story_path, mode, errors, warnings)
        return 0 if ok else 1

    if len(args.paths) != 1:
        print("用法: python validate_structure.py [--mode process|governance|final] <故事结构JSON>")
        return 1

    path = args.paths[0]
    ok, errors, warnings = validate_structure_file(path, mode=mode)
    if args.report_json:
        write_json_report(args.report_json, ok, mode, errors, warnings)
    print_report("=== 故事结构校验 ===", path, mode, errors, warnings)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
