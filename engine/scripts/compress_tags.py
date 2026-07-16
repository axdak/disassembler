#!/usr/bin/env python3
"""
标签自动压缩：把所有带受控前缀（前缀出现在 narrative_taxonomy.json 顶层键中）
的标签从 `标签集` 机械降级到 `详情.补充标签`，并对剩余自由标签按集合上限做
FIFO 截断（截断部分一并降级）。

设计原则：
- 单一职责：只搬运标签，不评判 LLM 的内容质量；不读写章节分析或原文。
- 确定性：同一输入两次执行结果完全一致。
- 只增不删：已有 `详情.补充标签` 永远保留，新降级项追加去重。
- 复用现有零件：`SUPPLEMENTARY_TAGS_DETAIL_KEY`、parse/dump 补充标签、taxonomy。

CLI:
    python compress_tags.py --delta     <delta.json>      [--limits <path>] [--report <path>]
    python compress_tags.py --structure <structure.json>  [--limits <path>] [--report <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from story_schema_rules import (
    COLLECTION_KEYS,
    NARRATIVE_TAXONOMY,
    SUPPLEMENTARY_TAGS_DETAIL_KEY,
    dump_supplementary_tags,
    parse_supplementary_tags,
)


DEFAULT_LIMITS_PATH = Path(__file__).resolve().parent.parent / "references" / "tag_limits.json"
FALLBACK_LIMIT = 8


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_limits(limits_path: Path | None) -> Dict[str, int]:
    """读取每集合标签上限；缺键回退到 FALLBACK_LIMIT。"""
    limits: Dict[str, int] = {}
    target = limits_path or DEFAULT_LIMITS_PATH
    try:
        raw = load_json(str(target))
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict):
        for key in COLLECTION_KEYS:
            value = raw.get(key)
            limits[key] = value if isinstance(value, int) and value >= 0 else FALLBACK_LIMIT
    else:
        for key in COLLECTION_KEYS:
            limits[key] = FALLBACK_LIMIT
    return limits


def _append_unique(base: List[str], extra: List[str]) -> List[str]:
    """保持 base 原顺序在前，追加 extra 中不重复的项。"""
    seen = set(base)
    merged = list(base)
    for tag in extra:
        if tag in seen:
            continue
        merged.append(tag)
        seen.add(tag)
    return merged


def _is_prefixed(tag: str) -> bool:
    """判断标签是否带受控前缀（前缀出现在 taxonomy 顶层键中）。"""
    if ":" not in tag:
        return False
    prefix = tag.split(":", 1)[0]
    return prefix in NARRATIVE_TAXONOMY


def compress_item(item: Dict[str, Any], limit: int) -> Tuple[int, int]:
    """
    单元素压缩。返回 (降级前缀标签数, 截断自由标签数)。
    原地修改 item.标签集 和 item.详情.补充标签。
    """
    raw = item.get("标签集")
    if not isinstance(raw, list):
        return 0, 0

    # 规整：只保留非空字符串、去重保序
    cleaned: List[str] = []
    seen: set = set()
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or t in seen:
            continue
        cleaned.append(t)
        seen.add(t)

    prefix_tags = [t for t in cleaned if _is_prefixed(t)]
    free_tags = [t for t in cleaned if t not in prefix_tags]
    keep_free = free_tags[: max(limit, 0)]
    extra_free = free_tags[max(limit, 0):]

    demoted_new = prefix_tags + extra_free
    if not demoted_new and keep_free == cleaned:
        # 无任何改动；不要碰详情.补充标签的存在性
        return 0, 0

    # 读现有补充标签
    detail = item.get("详情")
    if not isinstance(detail, dict):
        detail = {}
        item["详情"] = detail

    existing = detail.get(SUPPLEMENTARY_TAGS_DETAIL_KEY)
    existing_tags: List[str] = []
    if isinstance(existing, str) and existing.strip():
        try:
            existing_tags = parse_supplementary_tags(existing)
        except ValueError:
            # 现有内容损坏；按空处理而不是抛错丢数据
            existing_tags = []

    merged = _append_unique(existing_tags, demoted_new)

    item["标签集"] = keep_free
    if merged:
        detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = dump_supplementary_tags(merged)

    return len(prefix_tags), len(extra_free)


def compress_collection(
    items: Any,
    collection_key: str,
    limit: int,
    section_label: str,
    report: List[Dict[str, Any]],
) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        demoted_prefix, demoted_free = compress_item(item, limit)
        if demoted_prefix or demoted_free:
            report.append(
                {
                    "段": section_label,
                    "集合": collection_key,
                    "名称": item.get("名称", ""),
                    "降级前缀标签数": demoted_prefix,
                    "截断自由标签数": demoted_free,
                }
            )


def compress_delta(delta: Dict[str, Any], limits: Dict[str, int]) -> List[Dict[str, Any]]:
    report: List[Dict[str, Any]] = []
    if not isinstance(delta, dict):
        raise ValueError("Delta 顶层必须是对象")
    for section in ("新增元素", "修改元素"):
        bucket = delta.get(section)
        if not isinstance(bucket, dict):
            continue
        for collection_key in COLLECTION_KEYS:
            compress_collection(
                bucket.get(collection_key),
                collection_key,
                limits.get(collection_key, FALLBACK_LIMIT),
                section,
                report,
            )
    return report


def compress_structure(data: Dict[str, Any], limits: Dict[str, int]) -> List[Dict[str, Any]]:
    report: List[Dict[str, Any]] = []
    if not isinstance(data, dict):
        raise ValueError("故事结构顶层必须是对象")
    for collection_key in COLLECTION_KEYS:
        compress_collection(
            data.get(collection_key),
            collection_key,
            limits.get(collection_key, FALLBACK_LIMIT),
            "故事结构",
            report,
        )
    return report


def write_report(report_path: str | None, mode: str, source: str, report: List[Dict[str, Any]]) -> None:
    if not report_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    payload = {
        "模式": mode,
        "目标文件": source,
        "改写元素数": len(report),
        "明细": report,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="标签压缩：受控前缀标签和超限自由标签下层到 详情.补充标签。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--delta", help="处理单章 Delta JSON")
    group.add_argument("--structure", help="处理过程库或最终故事结构 JSON")
    parser.add_argument("--limits", help="覆盖 references/tag_limits.json")
    parser.add_argument("--report", help="写入改写明细到该 JSON 文件")
    args = parser.parse_args()

    target = args.delta or args.structure
    if not os.path.isfile(target):
        print(f"错误: 输入文件不存在: {target}")
        return 1
    try:
        data = load_json(target)
    except json.JSONDecodeError as exc:
        print(f"错误: 输入文件不是合法 JSON: {exc}")
        return 1

    limits = load_limits(Path(args.limits) if args.limits else None)
    try:
        if args.delta:
            report = compress_delta(data, limits)
            mode = "delta"
        else:
            report = compress_structure(data, limits)
            mode = "structure"
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    save_json(target, data)
    write_report(args.report, mode, target, report)

    if report:
        print(f"标签压缩: 改写 {len(report)} 个元素，目标={target}")
    else:
        print(f"标签压缩: 无需改写，目标={target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
