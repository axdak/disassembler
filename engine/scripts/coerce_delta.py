#!/usr/bin/env python3
"""入库前 Delta 机械纠错层。

设计契约：
- 只做确定性类型/格式及字段位置修复，不改语义、不评判 LLM 内容质量。
- 无法唯一归属的修改必须保留，由校验器阻止入库；不得删除内容换取通过。
- 复用 normalize_story_schema.py 已有的 coerce_* 工具，不重复造轮子。
- 输入是 LLM 写的 Delta dict（in-place 修改）；返回纠错操作 list[str]。
- 修复点覆盖 validate_delta.py 中"机械可推断"的硬约束错误，
  把无需 LLM 判断的修复在校验之前完成，大幅降低 repair 循环触发率。

CLI:
    python coerce_delta.py <delta.json> [--current <story.json>] [--report <report.json>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from chronology import (
    BASE_TIME,
    chapter_time,
    normalize_chapter_value,
    normalize_involved_chapters,
    parse_chapter_numbers,
    parse_involved_chapters,
)
from normalize_story_schema import (
    coerce_bool,
    coerce_int,
    coerce_string_list,
    canonicalize_detail_ref_array,
    decode_json_array,
    is_detail_ref_array_field,
    is_iso_time,
    restore_standard_detail_fields,
    stringify,
)
from registered_types import align_registered_types
from story_schema_rules import (
    COLLECTION_KEYS,
    DEFAULTS,
    VALID_FIELDS,
    TRACEABLE_COLLECTION_KEYS,
    DETAIL_REF_RE,
    STRUCTURAL_REF_FIELDS,
    STRICT_DETAIL_KEY_FIELDS,
    SUPPLEMENTARY_TAGS_DETAIL_KEY,
    TYPE_TO_COLLECTION,
    canonicalize_supplementary_tags,
    is_cumulative_detail_field,
)


CHAPTER_TRACE_FIELDS = ("首次章节", "最近章节")
DETAIL_KEY_CHAPTER_WITH_SEPARATOR_RE = re.compile(r"^(?:第?\d{1,6}章?)[\-_=：:，,、\s]+")
DETAIL_KEY_CHAPTER_WITHOUT_SEPARATOR_RE = re.compile(r"^(?:第?(\d{1,6})章)")
DETAIL_KEY_FOUR_DIGIT_CHAPTER_RE = re.compile(r"^(\d{4})(?=\D)")


def _ensure_detail(item: Dict[str, Any]) -> Dict[str, Any]:
    """保证 item['详情'] 是 dict，缺失/类型错则替换为空 dict。"""
    detail = item.get("详情")
    if not isinstance(detail, dict):
        item["详情"] = {}
    return item["详情"]


def relocate_element_fields(delta: Dict[str, Any]) -> List[str]:
    """按同一字段契约无损迁移位置；冲突原样保留，交原校验器/模型处理。

    保全比较也使用此函数，仅在内存中的副本上归一化位置和声明数组编码，
    不改变文本或数组内容。不能用其他字段中的同文替代本字段。

    双向迁移：
    - 顶层 → 详情：顶层出现规范外字段时迁入详情（既有行为）。
    - 详情 → 顶层：同名标准字段符合声明类型（含明确空值）时补回缺失/空值顶层字段。
      列表只安全解开 JSON 数组；两处非空且不同的内容保留供校验器处理。
    """
    report = []
    for section_name in ("新增元素", "修改元素"):
        section = delta.get(section_name)
        if not isinstance(section, dict):
            continue
        for collection, allowed in VALID_FIELDS.items():
            items = section.get(collection)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("详情", {}), dict):
                    continue
                for field in list(item):
                    if field in allowed:
                        continue
                    detail = item.setdefault("详情", {})
                    label = f"{section_name}.{collection}[{item.get('名称', '')}].{field}"
                    if field in detail and detail[field] != item[field]:
                        report.append(f"{label} 与详情同名字段冲突，保留两处原值待修复")
                        continue
                    detail[field] = item.pop(field)
                    report.append(f"{label} 从顶层迁回详情（原值完整保留）")
                restore_standard_detail_fields(item, collection, report)
                for field, default in DEFAULTS[collection].items():
                    if isinstance(default, list) and field in item:
                        original = item[field]
                        item[field] = decode_json_array(original)
                        if item[field] != original:
                            report.append(f"{collection}[{item.get('名称', '')}].{field} JSON字符串数组 → 数组")
    return report


def _coerce_chapter_trace(detail: Dict[str, Any], label: str, report: List[str]) -> None:
    """归一化 详情.首次章节 / 详情.最近章节 到四位补零字符串。"""
    for field in CHAPTER_TRACE_FIELDS:
        if field not in detail:
            continue
        original = detail[field]
        normalized = normalize_chapter_value(original)
        if normalized and normalized != original:
            detail[field] = normalized
            report.append(f"{label}.详情.{field} {original!r} → {normalized!r}")


def _coerce_involved_chapters(detail: Dict[str, Any], label: str, report: List[str]) -> None:
    """归一化 详情.涉及章节 到四位补零、中文全角逗号、升序去重字符串。"""
    if "涉及章节" not in detail:
        return
    original = detail["涉及章节"]
    normalized = normalize_involved_chapters(original)
    if normalized and normalized != original:
        detail["涉及章节"] = normalized
        report.append(f"{label}.详情.涉及章节 {original!r} → {normalized!r}")


def _coerce_supplementary_tags(detail: Dict[str, Any], label: str, report: List[str]) -> None:
    """把 详情.补充标签 统一成可逆的紧凑 JSON 字符串数组。

    数组、被写坏的字符串、以及其它类型都走同一套抢救规则：能解析的按标签
    规范化，不能解析的把原文逐字保留为单个标签，确实不含内容的键直接移除。
    校验器对"补充标签不可解析"是硬 error，而既有事件既不能改 详情 整块、
    也不能改数组位置，所以必须在纠错层把它变成确定性可消除的问题。
    """
    if SUPPLEMENTARY_TAGS_DETAIL_KEY not in detail:
        return
    original = detail[SUPPLEMENTARY_TAGS_DETAIL_KEY]
    canonical = canonicalize_supplementary_tags(original)
    if not canonical:
        detail.pop(SUPPLEMENTARY_TAGS_DETAIL_KEY, None)
        report.append(f"{label}.详情.{SUPPLEMENTARY_TAGS_DETAIL_KEY} 不含有效标签 → 已移除")
        return
    if canonical == original:
        return
    detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = canonical
    report.append(f"{label}.详情.{SUPPLEMENTARY_TAGS_DETAIL_KEY} 非法值 → 按原内容重建为可解析标签")


def _coerce_archive_arrays(collection_key: str, detail: Dict[str, Any], label: str, report: List[str]) -> None:
    """普通档案数组转可逆字符串；引用、冲突标准字段保留真实类型。"""
    special_keys = {
        SUPPLEMENTARY_TAGS_DETAIL_KEY,
        "首次章节",
        "最近章节",
        "涉及章节",
    }
    for key, value in list(detail.items()):
        if is_detail_ref_array_field(key):
            normalized = canonicalize_detail_ref_array(value)
            if normalized != value:
                detail[key] = normalized
                report.append(f"{label}.详情.{key} 按引用契约归一为数组")
            continue
        if key in VALID_FIELDS[collection_key]:
            # 合法同名字段已归位；残留意味着冲突或类型错误，不应串化掩盖。
            continue
        if key in special_keys or not isinstance(value, list):
            continue
        if value and all(isinstance(item, str) and DETAIL_REF_RE.match(item.strip()) for item in value):
            continue
        encoded = json.dumps(
            [stringify(item).strip() for item in value if stringify(item).strip()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        detail[key] = encoded
        report.append(f"{label}.详情.{key} 普通数组 → JSON字符串数组")


def _strip_one_detail_key_chapter_marker(key: str) -> str:
    for pattern in (DETAIL_KEY_CHAPTER_WITH_SEPARATOR_RE, DETAIL_KEY_CHAPTER_WITHOUT_SEPARATOR_RE):
        stripped = pattern.sub("", key, count=1).strip()
        if stripped != key:
            return stripped
    match = DETAIL_KEY_FOUR_DIGIT_CHAPTER_RE.match(key)
    if match and 0 < int(match.group(1)) <= 1000:
        return key[4:].strip()
    return key


def _strip_detail_key_chapter_markers(key: str) -> str:
    stripped = key
    while True:
        next_key = _strip_one_detail_key_chapter_marker(stripped)
        if next_key == stripped:
            return stripped
        stripped = next_key


def _unique_detail_key(detail: Dict[str, Any], key: str) -> str:
    if key not in detail:
        return key
    index = 2
    while f"{key}{index}" in detail:
        index += 1
    return f"{key}{index}"


def _coerce_detail_key_chapter_prefixes(
    collection_key: str, detail: Dict[str, Any], label: str, report: List[str]
) -> None:
    """非事件自由快照 key 不带章节号；合并器会按 Delta.章节统一加前缀。"""
    if collection_key == "事件集":
        return
    for key in list(detail.keys()):
        if not isinstance(key, str) or not key:
            continue
        if key in STRICT_DETAIL_KEY_FIELDS or key.startswith("关联") or is_cumulative_detail_field(key):
            continue
        normalized = _strip_detail_key_chapter_markers(key)
        if not normalized or normalized == key:
            continue
        target = _unique_detail_key(detail, normalized)
        detail[target] = detail.pop(key)
        report.append(f"{label}.详情键章节前缀[{key}]已剥离为[{target}]")


def _coerce_structural_ref_prefixes(
    collection_key: str, item: Dict[str, Any], label: str, report: List[str]
) -> None:
    """标准引用仅剥离匹配前缀；错配值留在原字段，避免掩盖入库错误。"""
    for field, target_collection, kind in STRUCTURAL_REF_FIELDS.get(collection_key, []):
        if kind == "relation_list" or field not in item:
            continue

        def coerce_one(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            clean = value.strip()
            match = DETAIL_REF_RE.match(clean)
            if not match:
                return value
            ref_type, ref_name = match.group(1), match.group(2).strip()
            if TYPE_TO_COLLECTION.get(ref_type) == target_collection:
                report.append(f"{label}.{field} 标准字段前缀[{ref_type}:]已剥离")
                return ref_name
            report.append(f"{label}.{field} 不匹配类型前缀[{clean}]，保留原值待修复")
            return value

        value = item.get(field)
        if kind == "scalar":
            item[field] = coerce_one(value)
        elif kind == "list" and isinstance(value, list):
            item[field] = [coerce_one(elem) for elem in value]


def _coerce_role(item: Dict[str, Any], label: str, report: List[str]) -> None:
    """角色集元素纠错。"""
    if "是否主角" in item and not isinstance(item["是否主角"], bool):
        original = item["是否主角"]
        item["是否主角"] = coerce_bool(original)
        report.append(f"{label}.是否主角 {original!r} → {item['是否主角']!r}")

    if "性别" in item and not (isinstance(item["性别"], int) and item["性别"] in (0, 1, 2)):
        original = item["性别"]
        # 字符串映射：男/女/未知 → 0/1/2
        if isinstance(original, str):
            stripped = original.strip()
            mapping = {"男": 0, "M": 0, "male": 0, "女": 1, "F": 1, "female": 1}
            if stripped in mapping:
                item["性别"] = mapping[stripped]
            elif stripped.lower() in mapping:
                item["性别"] = mapping[stripped.lower()]
            else:
                item["性别"] = coerce_int(original, default=2, min_value=0, max_value=2)
        else:
            item["性别"] = coerce_int(original, default=2, min_value=0, max_value=2)
        report.append(f"{label}.性别 {original!r} → {item['性别']!r}")

    if "年龄" in item and not isinstance(item["年龄"], int):
        original = item["年龄"]
        item["年龄"] = coerce_int(original, default=0, min_value=0)
        report.append(f"{label}.年龄 {original!r} → {item['年龄']!r}")

    if "生日" not in item or not is_iso_time(item.get("生日")):
        original = item.get("生日")
        item["生日"] = BASE_TIME
        if original not in (None, ""):
            report.append(f"{label}.生日 {original!r} → {BASE_TIME!r}")
        else:
            report.append(f"{label}.生日 缺失，补默认 {BASE_TIME!r}")

    if "关系" in item and isinstance(item["关系"], dict):
        original = item["关系"]
        item["关系"] = [f"{k}:{v}" for k, v in original.items() if k and v]
        report.append(f"{label}.关系 对象 → 字符串数组")
    elif "关系" in item and not isinstance(item["关系"], list):
        original = item["关系"]
        item["关系"] = coerce_string_list(original)
        report.append(f"{label}.关系 {type(original).__name__} → 字符串数组")


def _coerce_event(item: Dict[str, Any], label: str, report: List[str]) -> None:
    """事件集元素纠错。"""
    # 发生地点：数组 → 字符串
    if "发生地点" in item and isinstance(item["发生地点"], list):
        original = item["发生地点"]
        if len(original) == 0:
            item["发生地点"] = ""
        elif len(original) == 1:
            item["发生地点"] = str(original[0])
        else:
            # 多元素无法判断主地点；用顿号合并，让 LLM repair 时知道这里有多地点
            item["发生地点"] = "、".join(str(x) for x in original)
        report.append(f"{label}.发生地点 数组 → 字符串")

    # 目标事件：字符串 → 数组
    if "目标事件" in item and not isinstance(item["目标事件"], list):
        original = item["目标事件"]
        item["目标事件"] = coerce_string_list(original)
        report.append(f"{label}.目标事件 {type(original).__name__} → 字符串数组")

    # 参与成员：标量 → 数组
    if "参与成员" in item and not isinstance(item["参与成员"], list):
        original = item["参与成员"]
        item["参与成员"] = coerce_string_list(original)
        report.append(f"{label}.参与成员 {type(original).__name__} → 字符串数组")

    # 重量级：转 int 并限制 0-100
    if "重量级" in item and not (isinstance(item["重量级"], int) and 0 <= item["重量级"] <= 100):
        original = item["重量级"]
        item["重量级"] = coerce_int(original, default=0, min_value=0, max_value=100)
        report.append(f"{label}.重量级 {original!r} → {item['重量级']!r}")

    # 时间：从 详情.涉及章节 自动推导，LLM 不需要填写
    detail = item.get("详情", {})
    if isinstance(detail, dict):
        involved = detail.get("涉及章节")
        chapters = parse_involved_chapters(involved)
        if chapters:
            expected = chapter_time(chapters[0])
            current = item.get("时间")
            if current != expected:
                item["时间"] = expected
                if current:
                    report.append(f"{label}.时间 {current!r} → {expected!r} (从涉及章节推导)")
                else:
                    report.append(f"{label}.时间 缺失，从涉及章节推导为 {expected!r}")
        elif "时间" not in item:
            item["时间"] = BASE_TIME
            report.append(f"{label}.时间 缺失且无涉及章节，补默认 {BASE_TIME!r}")


def _coerce_item(collection_key: str, item: Any, idx: int, report: List[str]) -> None:
    """单个元素纠错入口。"""
    if not isinstance(item, dict):
        return
    name = item.get("名称") if isinstance(item.get("名称"), str) else idx
    label = f"{collection_key}[{name}]"

    # 非对象详情可能包含有效分析，不能用空对象替换来掩盖错误。
    if "详情" in item and not isinstance(item["详情"], dict):
        report.append(f"{label}.详情 类型错误，保留原值待修复")
        return
    detail = _ensure_detail(item)
    _coerce_detail_key_chapter_prefixes(collection_key, detail, label, report)
    # 章节前缀还原可能刚暴露同名标准字段，必须在本次校验前归位，
    # 再交类型专属规则处理引用前缀，避免正确内容被迫调用模型重写。
    restore_standard_detail_fields(item, collection_key, report)
    _coerce_supplementary_tags(detail, label, report)
    _coerce_archive_arrays(collection_key, detail, label, report)

    # 元素类型专属纠错
    if collection_key == "角色集":
        _coerce_role(item, label, report)
    elif collection_key == "事件集":
        _coerce_event(item, label, report)

    _coerce_structural_ref_prefixes(collection_key, item, label, report)

    # 章节追溯字段（角色/地点/线索/阵营/物品共享）
    if collection_key in TRACEABLE_COLLECTION_KEYS:
        _coerce_chapter_trace(detail, label, report)

    # 事件涉及章节字段
    if collection_key == "事件集":
        _coerce_involved_chapters(detail, label, report)


def coerce_delta_in_place(delta: Dict[str, Any], current: Dict[str, Any] | None = None) -> List[str]:
    """机械纠错并按可选正式索引更正唯一类别，返回完整迁移报告。"""
    report: List[str] = []
    if not isinstance(delta, dict):
        return report

    if current is not None:
        # 只迁移正式索引能唯一确定的类别。其余条目完整保留，让校验器结合
        # 正式档案和本批新增判定；跨类别歧义、未知目标及迁移冲突不能靠删除消除。
        report.extend(align_registered_types(delta, current))
    report.extend(relocate_element_fields(delta))

    for section_key in ("新增元素", "修改元素"):
        section = delta.get(section_key)
        if not isinstance(section, dict):
            continue
        for collection_key in COLLECTION_KEYS:
            items = section.get(collection_key)
            if not isinstance(items, list):
                continue
            for idx, item in enumerate(items):
                _coerce_item(collection_key, item, idx, report)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delta 入库前机械纠错：把类型/格式硬约束自动修好，减少 repair 循环。"
    )
    parser.add_argument("delta_path", help="Delta JSON 路径")
    parser.add_argument("--report", help="纠错报告 JSON 输出路径")
    parser.add_argument("--current", help="正式故事结构；只按唯一正式名称修正类别与引用")
    args = parser.parse_args()

    delta_path = Path(args.delta_path)
    try:
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取 Delta 失败: {exc}", file=sys.stderr)
        return 1

    try:
        current = json.loads(Path(args.current).read_text(encoding="utf-8")) if args.current else None
        if current is not None and not isinstance(current, dict):
            raise ValueError("正式故事结构必须是对象")
    except (OSError, ValueError) as exc:
        print(f"读取正式故事结构失败: {exc}", file=sys.stderr)
        return 1
    report = coerce_delta_in_place(delta, current)

    delta_path.write_text(
        json.dumps(delta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Delta 已纠错: {delta_path} （共 {len(report)} 项）")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps({"corrections": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
