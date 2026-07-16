#!/usr/bin/env python3
"""
Delta 入库前校验器。

模式：
  --mode process    逐章入库：字段/类型/详情硬卡；前向引用为 warning。
  --mode governance 治理补丁：引用错误为 error；事件发生地点的未注册细粒度子空间为 warning。
  --mode final      最终修复补丁：引用错误为 error，warning 也更严格。

注意：Delta 允许额外顶层“治理操作”等字段，但 merge_delta.py 只合并 介绍/新增元素/修改元素；治理操作应由 apply_governance_ops.py 或 run_pipeline commit-governance 执行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Set, Tuple

from story_schema_rules import (
    COLLECTION_KEYS, DEFAULTS, VALID_FIELDS, TYPE_TO_COLLECTION, COLLECTION_TO_TYPE, TRACEABLE_COLLECTION_KEYS,
    exact_name_sets, alias_to_name_maps, validate_controlled_tags, validate_detail_schema,
)
from chronology import BASE_TIME, chapter_time, first_involved_chapter, is_iso_time, normalize_chapter_value, normalize_involved_chapters


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_stdio()

REQUIRED_TOP_KEYS = {"新增元素", "修改元素"}
OPTIONAL_TOP_KEYS = {"章节", "章节范围", "介绍", "冲突声明", "质量说明", "处理备注", "治理操作", "待人工确认", "证据范围", "治理类型", "治理目标"}
ALLOWED_TOP_KEYS = REQUIRED_TOP_KEYS | OPTIONAL_TOP_KEYS
LIST_FIELDS = {"所属阵营", "关系", "参与成员", "目标事件", "涉及事件", "别名", "标签集"}
STRING_FIELDS = {"名称", "分组", "介绍", "发生地点", "父级地点", "座落地点", "父级阵营", "生日", "时间"}
DISPLAY = {"角色集": "角色", "事件集": "事件", "地点集": "地点", "线索集": "线索", "阵营集": "阵营", "物品集": "物品", "其他事项集": "其他事项"}
REQUIRED_EVIDENCE_DETAIL_FIELDS = ("提取理由",)
TRACE_DETAIL_FIELDS = ("首次章节", "最近章节")

STRUCTURAL_REFS = {
    "角色集": [("所属阵营", "阵营集", "list"), ("关系", "角色集", "relation_list")],
    "事件集": [("发生地点", "地点集", "scalar"), ("参与成员", "角色集", "list"), ("目标事件", "事件集", "list")],
    "地点集": [("父级地点", "地点集", "scalar")],
    "线索集": [("涉及事件", "事件集", "list")],
    "阵营集": [("父级阵营", "阵营集", "scalar"), ("座落地点", "地点集", "scalar")],
    "物品集": [], "其他事项集": [],
}

UNIT_NUM = r"[零〇一二三四五六七八九十百千万\d]+"
UNIT_WORD = r"[章节回卷集部篇节幕]"
CHAPTER_RANGE_RE = re.compile(
    rf"第\s*{UNIT_NUM}\s*(?:-|—|–|~|～|至|到)\s*{UNIT_NUM}\s*{UNIT_WORD}"
)
MULTI_CHAPTER_RE = re.compile(
    rf"第\s*{UNIT_NUM}\s*{UNIT_WORD}\s*(?:[,，、/＋+]|和|及)\s*(?:第\s*)?{UNIT_NUM}"
)


def is_multi_unit_chapter_label(label: str) -> bool:
    """判断 Delta.章节 是否像一个多切片范围，而非单个内部序号。"""
    text = label.strip()
    return bool(CHAPTER_RANGE_RE.search(text) or MULTI_CHAPTER_RE.search(text))


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_delta_new_names(delta: Dict[str, Any]) -> Dict[str, Set[str]]:
    result = {k: set() for k in COLLECTION_KEYS}
    new_elements = delta.get("新增元素", {}) if isinstance(delta, dict) else {}
    if not isinstance(new_elements, dict):
        return result
    for key in COLLECTION_KEYS:
        for item in new_elements.get(key, []) or []:
            if isinstance(item, dict) and isinstance(item.get("名称"), str) and item["名称"].strip():
                result[key].add(item["名称"].strip())
    return result


def merge_name_sets(current: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Set[str]]:
    sets = exact_name_sets(current)
    new = collect_delta_new_names(delta)
    return {k: sets.get(k, set()) | new.get(k, set()) for k in COLLECTION_KEYS}


def validate_top(delta: Any, mode: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(delta, dict):
        return ["Delta顶层必须是对象"], warnings
    required = set(REQUIRED_TOP_KEYS)
    if mode == "governance":
        if "章节" not in delta and "章节范围" not in delta:
            errors.append("治理Delta必须包含顶层字段[章节]或[章节范围]")
    else:
        required.add("章节")
    for key in required:
        if key not in delta:
            errors.append(f"Delta缺少顶层字段[{key}]")
    for key in delta.keys():
        if key not in ALLOWED_TOP_KEYS:
            warnings.append(f"Delta包含未知顶层字段[{key}]，不会被标准合并器处理")
    if "章节" in delta:
        if not isinstance(delta["章节"], str):
            errors.append("Delta.章节 必须是字符串")
        elif is_multi_unit_chapter_label(delta["章节"]):
            errors.append("Delta.章节 表示多个切片；单个Delta只能对应一个切片/seq，不能使用章节范围")
    if "章节范围" in delta:
        if mode != "governance":
            errors.append("Delta.章节范围 仅允许治理补丁使用")
        elif not isinstance(delta["章节范围"], str) or not delta["章节范围"].strip():
            errors.append("Delta.章节范围 必须是非空字符串")
    for section in ["新增元素", "修改元素"]:
        bucket = delta.get(section, {})
        if not isinstance(bucket, dict):
            errors.append(f"Delta.{section} 必须是对象")
            continue
        for k, v in bucket.items():
            if k not in COLLECTION_KEYS:
                errors.append(f"Delta.{section} 包含未知集合[{k}]")
            elif not isinstance(v, list):
                errors.append(f"Delta.{section}.{k} 必须是数组")
    return errors, warnings


def issue_ref(
    errors: List[str],
    warnings: List[str],
    mode: str,
    label: str,
    field: str,
    ref: str,
    target: str,
    name_sets: Dict[str, Set[str]],
    alias_maps: Dict[str, Dict[str, str]],
    soft_governance: bool = False,
) -> None:
    if not isinstance(ref, str) or not ref.strip():
        return
    clean = ref.strip()
    if clean in name_sets.get(target, set()):
        return
    if clean in alias_maps.get(target, {}):
        msg = f"{label}.{field} 使用别名引用[{clean}]，建议改为正式名称[{alias_maps[target][clean]}]"
        if mode == "process" or (mode == "governance" and soft_governance):
            warnings.append(msg)
        else:
            errors.append(msg)
        return
    msg = f"{label}.{field} 引用了不存在的{COLLECTION_TO_TYPE[target]}[{clean}]"
    if mode == "process":
        warnings.append(msg + "；过程阶段允许作为前向引用，但最终前必须显式新增/合并目标或移入详情.关系线索/待确认信息")
    elif mode == "governance" and soft_governance:
        warnings.append(msg + "；治理阶段允许事件发生地点保留细粒度子空间，最终交付前建议收敛为已注册父级地点或正式地点")
    else:
        errors.append(msg)


def validate_types(collection_key: str, item: Dict[str, Any], label: str, errors: List[str], warnings: List[str]) -> None:
    valid = VALID_FIELDS[collection_key]
    for field in item.keys():
        if field not in valid:
            errors.append(f"{label} 包含规范外字段[{field}]；非标准信息必须放入详情")
    for field in LIST_FIELDS:
        if field in item:
            if not isinstance(item[field], list):
                errors.append(f"{label}.{field} 必须是数组")
            else:
                for i, elem in enumerate(item[field]):
                    if not isinstance(elem, str):
                        errors.append(f"{label}.{field}[{i}] 必须是字符串")
    for field in STRING_FIELDS:
        if field in item and not isinstance(item[field], str):
            errors.append(f"{label}.{field} 必须是字符串")
    if "标签集" in item:
        for error in validate_controlled_tags(collection_key, item["标签集"]):
            errors.append(f"{label}.{error.split('.', 1)[1]}")
    if "是否主角" in item and not isinstance(item["是否主角"], bool):
        errors.append(f"{label}.是否主角 必须是布尔值")
    if "性别" in item and (not isinstance(item["性别"], int) or item["性别"] not in (0, 1, 2)):
        errors.append(f"{label}.性别 必须是整数0/1/2")
    if "年龄" in item and not isinstance(item["年龄"], int):
        errors.append(f"{label}.年龄 必须是整数；未知填0")
    if "重量级" in item and (not isinstance(item["重量级"], int) or item["重量级"] < 0 or item["重量级"] > 100):
        errors.append(f"{label}.重量级 必须是0-100之间整数")
    if collection_key == "角色集" and "生日" in item and not is_iso_time(item["生日"]):
        errors.append(f"{label}.生日 必须是合法ISO时间；未知固定填{BASE_TIME}")
    detail = item.get("详情", {})
    if collection_key in TRACEABLE_COLLECTION_KEYS:
        if not isinstance(detail, dict):
            errors.append(f"{label}.详情 必须包含首次章节和最近章节")
        else:
            for field in TRACE_DETAIL_FIELDS:
                value = detail.get(field)
                if not isinstance(value, str) or not value or normalize_chapter_value(value) != value:
                    errors.append(f"{label}.详情.{field} 必须是固定宽度章节序号，例如0001")
            first = detail.get("首次章节")
            recent = detail.get("最近章节")
            if (
                isinstance(first, str)
                and isinstance(recent, str)
                and normalize_chapter_value(first) == first
                and normalize_chapter_value(recent) == recent
                and int(first) > int(recent)
            ):
                errors.append(f"{label}.详情.首次章节 不得晚于 最近章节")
    if collection_key == "事件集":
        involved = detail.get("涉及章节") if isinstance(detail, dict) else None
        if not isinstance(involved, str) or not involved or normalize_involved_chapters(involved) != involved:
            errors.append(f"{label}.详情.涉及章节 必须为固定宽度、升序、去重的字符串")
        else:
            expected_time = chapter_time(first_involved_chapter(item))
            if item.get("时间") != expected_time:
                errors.append(f"{label}.时间 必须等于最小涉及章节对应章节时间[{expected_time}]")


def validate_evidence_detail(item: Dict[str, Any], label: str, errors: List[str]) -> None:
    detail = item.get("详情")
    if not isinstance(detail, dict):
        errors.append(f"{label}.详情 必须包含提取理由")
        return
    for field in REQUIRED_EVIDENCE_DETAIL_FIELDS:
        value = detail.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}.详情.{field} 必须是非空字符串，用于Delta、章节分析和治理补丁追溯")


def process_issue(warnings: List[str], label: str, message: str) -> None:
    warnings.append(f"{label} {message}；过程阶段允许先合并，后续由 coerce/normalize/治理收敛")


def validate_process_patch_item(
    collection_key: str,
    item: Dict[str, Any],
    label: str,
    name_sets: Dict[str, Set[str]],
    alias_maps: Dict[str, Dict[str, str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """Process-mode Delta is a mergeable patch, not a complete final element."""
    valid = VALID_FIELDS[collection_key]
    for field in item.keys():
        if field not in valid:
            errors.append(f"{label} 包含规范外字段[{field}]；非标准信息必须放入详情")

    for field in LIST_FIELDS:
        if field in item:
            value = item[field]
            if not isinstance(value, list):
                process_issue(warnings, label, f".{field} 建议为数组")
            else:
                for i, elem in enumerate(value):
                    if not isinstance(elem, str):
                        errors.append(f"{label}.{field}[{i}] 必须是字符串")

    if "标签集" in item:
        for error in validate_controlled_tags(collection_key, item["标签集"]):
            errors.append(f"{label}.{error.split('.', 1)[1]}")

    for field in STRING_FIELDS:
        if field in item and not isinstance(item[field], str):
            process_issue(warnings, label, f".{field} 建议为字符串")

    if "是否主角" in item and not isinstance(item["是否主角"], bool):
        process_issue(warnings, label, ".是否主角 建议为布尔值")
    if "性别" in item and (not isinstance(item["性别"], int) or item["性别"] not in (0, 1, 2)):
        process_issue(warnings, label, ".性别 建议为整数0/1/2")
    if "年龄" in item and not isinstance(item["年龄"], int):
        process_issue(warnings, label, ".年龄 建议为整数")
    if "重量级" in item and (not isinstance(item["重量级"], int) or item["重量级"] < 0 or item["重量级"] > 100):
        process_issue(warnings, label, ".重量级 建议为0-100之间整数")
    if collection_key == "角色集" and "生日" in item and not is_iso_time(item["生日"]):
        process_issue(warnings, label, f".生日 建议为合法ISO时间，未知可用{BASE_TIME}")

    detail = item.get("详情")
    if detail is not None:
        validate_detail_schema(detail, label, name_sets, errors, warnings, mode="delta", alias_maps=alias_maps)
    else:
        process_issue(warnings, label, ".详情 缺失，normalize_story_schema.py 将补为空对象")

    if collection_key in TRACEABLE_COLLECTION_KEYS:
        if not isinstance(detail, dict):
            return
        for field in TRACE_DETAIL_FIELDS:
            value = detail.get(field)
            if value is None:
                process_issue(warnings, label, f".详情.{field} 缺失")
            elif not isinstance(value, str) or not value or normalize_chapter_value(value) != value:
                process_issue(warnings, label, f".详情.{field} 建议为固定宽度章节序号，例如0001")
        first = detail.get("首次章节")
        recent = detail.get("最近章节")
        if (
            isinstance(first, str)
            and isinstance(recent, str)
            and normalize_chapter_value(first) == first
            and normalize_chapter_value(recent) == recent
            and int(first) > int(recent)
        ):
            errors.append(f"{label}.详情.首次章节 不得晚于 最近章节")

    if collection_key == "事件集":
        detail_obj = detail if isinstance(detail, dict) else {}
        involved = detail_obj.get("涉及章节")
        if involved is None:
            process_issue(warnings, label, ".详情.涉及章节 缺失")
        elif not isinstance(involved, str) or not involved or normalize_involved_chapters(involved) != involved:
            process_issue(warnings, label, ".详情.涉及章节 建议为固定宽度、升序、去重的字符串")
        else:
            expected_time = chapter_time(first_involved_chapter(item))
            if item.get("时间") != expected_time:
                process_issue(warnings, label, f".时间 建议等于最小涉及章节对应章节时间[{expected_time}]")

    validate_refs(collection_key, item, label, name_sets, alias_maps, errors, warnings, "process")


def existing_event_by_name_or_alias(current: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    for event in current.get("事件集", []) or []:
        if not isinstance(event, dict):
            continue
        if event.get("名称") == name or name in (event.get("别名") or []):
            return event
    return None


def validate_refs(collection_key: str, item: Dict[str, Any], label: str, name_sets: Dict[str, Set[str]], alias_maps: Dict[str, Dict[str, str]], errors: List[str], warnings: List[str], mode: str) -> None:
    for field, target, kind in STRUCTURAL_REFS[collection_key]:
        value = item.get(field)
        soft_governance = collection_key == "事件集" and field == "发生地点" and target == "地点集"
        if kind == "scalar":
            issue_ref(errors, warnings, mode, label, field, value, target, name_sets, alias_maps, soft_governance=soft_governance)
        elif kind == "list":
            if isinstance(value, list):
                for ref in value:
                    issue_ref(errors, warnings, mode, label, field, ref, target, name_sets, alias_maps)
        elif kind == "relation_list":
            if isinstance(value, list):
                for rel in value:
                    if not isinstance(rel, str) or ":" not in rel:
                        errors.append(f"{label}.关系 项[{rel}]必须是 关系类型:角色名")
                    else:
                        _, ref = rel.split(":", 1)
                        issue_ref(errors, warnings, mode, label, "关系", ref.strip(), target, name_sets, alias_maps)
    # 详情引用规则复用 schema 规则
    if "详情" in item:
        validate_detail_schema(item["详情"], label, name_sets, errors, warnings, mode=("delta" if mode == "process" else mode), alias_maps=alias_maps)


def validate_elements(current: Dict[str, Any], delta: Dict[str, Any], mode: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    name_sets = merge_name_sets(current, delta)
    alias_maps = alias_to_name_maps(current)
    existing_name_or_alias = {k: set(v) for k, v in name_sets.items()}
    for k, mapping in alias_maps.items():
        existing_name_or_alias[k].update(mapping.keys())

    seen_in_delta = {k: set() for k in COLLECTION_KEYS}
    for section in ["新增元素", "修改元素"]:
        bucket = delta.get(section, {}) or {}
        if not isinstance(bucket, dict):
            continue
        for collection_key in COLLECTION_KEYS:
            items = bucket.get(collection_key, []) or []
            for idx, item in enumerate(items):
                label = f"{section}.{DISPLAY[collection_key]}[{idx}]"
                if not isinstance(item, dict):
                    errors.append(f"{label} 必须是对象")
                    continue
                name = item.get("名称")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"{label}.名称 必须是非空字符串")
                    continue
                label = f"{section}.{DISPLAY[collection_key]}[{name.strip()}]"
                if name.strip() in seen_in_delta[collection_key]:
                    warnings.append(f"Delta内部{DISPLAY[collection_key]}名称重复[{name.strip()}]")
                seen_in_delta[collection_key].add(name.strip())

                if section == "新增元素":
                    missing = sorted(set(DEFAULTS[collection_key].keys()) - set(item.keys()))
                    if missing:
                        if mode == "process":
                            process_issue(warnings, label, f"新增元素缺少标准字段: {', '.join(missing)}；normalize 将补齐默认值")
                        else:
                            errors.append(f"{label} 新增元素缺少标准字段: {', '.join(missing)}")
                    if name.strip() in exact_name_sets(current).get(collection_key, set()):
                        warnings.append(f"{label} 已存在于当前结构，建议放入修改元素")
                else:
                    if name.strip() not in existing_name_or_alias.get(collection_key, set()) and mode != "process":
                        warnings.append(f"{label} 在当前结构中不存在，merge后会变成新增元素")

                if mode == "process":
                    validate_process_patch_item(collection_key, item, label, name_sets, alias_maps, errors, warnings)
                else:
                    validate_types(collection_key, item, label, errors, warnings)
                    validate_evidence_detail(item, label, errors)
                    validate_refs(collection_key, item, label, name_sets, alias_maps, errors, warnings, mode)
                if mode == "governance" and collection_key == "事件集":
                    existing_event = existing_event_by_name_or_alias(current, name.strip())
                    if existing_event is not None:
                        if "时间" in item and item.get("时间") != existing_event.get("时间"):
                            errors.append(f"{label}.时间 不得在普通治理中修改；治理只能重分类，时序字段和数组位置必须保持")
                        incoming_detail = item.get("详情") if isinstance(item.get("详情"), dict) else {}
                        existing_detail = existing_event.get("详情") if isinstance(existing_event.get("详情"), dict) else {}
                        if "涉及章节" in incoming_detail and incoming_detail.get("涉及章节") != existing_detail.get("涉及章节"):
                            errors.append(f"{label}.详情.涉及章节 不得在普通治理中修改；治理只能重分类，时序字段和数组位置必须保持")
    return errors, warnings


def _validate_tag_list(value: Any, field: str, label: str, errors: List[str]) -> List[str] | None:
    if not isinstance(value, list):
        errors.append(f"{label}.{field} 必须是数组")
        return None
    if any(not isinstance(tag, str) or not tag.strip() for tag in value):
        errors.append(f"{label}.{field} 必须全部是非空字符串")
        return None
    tags = [tag.strip() for tag in value]
    if len(set(tags)) != len(tags):
        errors.append(f"{label}.{field} 不得包含重复标签")
        return None
    return tags


def _find_governed_item(current: Dict[str, Any], collection_key: str, name: str) -> Dict[str, Any] | None:
    for item in current.get(collection_key, []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("名称") == name or name in (item.get("别名") or []):
            return item
    return None


def validate_tag_governance_ops(current: Dict[str, Any], delta: Dict[str, Any], mode: str) -> List[str]:
    """校验治理标签操作的对象、集合守恒和受控标签合法性。"""
    ops = delta.get("治理操作", {}) if isinstance(delta, dict) else {}
    if not isinstance(ops, dict) or "治理标签" not in ops:
        return []
    errors: List[str] = []
    tag_ops = ops.get("治理标签")
    if mode != "governance":
        return ["治理操作.治理标签 仅允许在 governance 模式使用"]
    if not isinstance(tag_ops, list):
        return ["治理操作.治理标签 必须是数组"]
    for index, op in enumerate(tag_ops):
        label = f"治理操作.治理标签[{index}]"
        if not isinstance(op, dict):
            errors.append(f"{label} 必须是对象")
            continue
        type_name, name = op.get("类型"), op.get("名称")
        if not isinstance(type_name, str) or type_name not in TYPE_TO_COLLECTION:
            errors.append(f"{label}.类型 必须是受支持的元素类型")
            continue
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}.名称 必须是非空字符串")
            continue
        reason = op.get("理由")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{label}.理由 必须是非空字符串")
        retained = _validate_tag_list(op.get("保留标签"), "保留标签", label, errors)
        demoted = _validate_tag_list(op.get("降级标签"), "降级标签", label, errors)
        if retained is None or demoted is None:
            continue
        overlap = set(retained) & set(demoted)
        if overlap:
            errors.append(f"{label}.保留标签 与降级标签 不得重叠: {sorted(overlap)}")
        collection_key = TYPE_TO_COLLECTION[type_name]
        item = _find_governed_item(current, collection_key, name.strip())
        if item is None:
            errors.append(f"{label} 找不到元素[{type_name}:{name.strip()}]")
            continue
        current_tags = item.get("标签集")
        if not isinstance(current_tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in current_tags):
            errors.append(f"{label} 目标元素的标签集不是有效字符串数组")
            continue
        current_normalized = [tag.strip() for tag in current_tags]
        if len(set(current_normalized)) != len(current_normalized):
            errors.append(f"{label} 目标元素的标签集包含重复标签，需先规范化")
            continue
        if set(current_normalized) != set(retained) | set(demoted):
            errors.append(f"{label} 保留标签与降级标签必须完整覆盖治理前标签集，且不得凭空新增")
        for error in validate_controlled_tags(collection_key, retained):
            errors.append(f"{label}.{error.split('.', 1)[1]}")
    return errors


def _is_empty_governance_workload(delta: Dict[str, Any]) -> bool:
    """治理Delta是否完全没有作业：无新增/修改元素，且无任何非空治理操作。"""
    for section in ("新增元素", "修改元素"):
        bucket = delta.get(section, {})
        if isinstance(bucket, dict):
            for value in bucket.values():
                if isinstance(value, list) and value:
                    return False
    ops = delta.get("治理操作", {})
    if isinstance(ops, dict):
        for value in ops.values():
            if isinstance(value, list) and value:
                return False
    return True


def validate_no_change_metadata(delta: Dict[str, Any]) -> List[str]:
    """治理Delta若整体为空，必须显式声明[治理类型=no_change]并附质量说明、证据范围。"""
    errors: List[str] = []
    if not _is_empty_governance_workload(delta):
        return errors
    if delta.get("治理类型") != "no_change":
        errors.append("治理Delta无任何新增/修改元素和治理操作时，必须显式声明[治理类型=no_change]；禁止提交空补丁跳过审计")
    note = delta.get("质量说明")
    if not isinstance(note, str) or not note.strip():
        errors.append("空治理补丁(治理类型=no_change)必须提供非空[质量说明]，记录已检查范围与无需修改的理由")
    evidence = delta.get("证据范围")
    if not isinstance(evidence, list) or not evidence:
        errors.append("空治理补丁(治理类型=no_change)必须提供非空数组[证据范围]，列举已审计章节")
    elif any(not isinstance(x, str) or not x.strip() for x in evidence):
        errors.append("空治理补丁[证据范围]每项必须是非空字符串")
    elif len({x.strip() for x in evidence}) != len(evidence):
        errors.append("空治理补丁[证据范围]不得包含重复项")
    return errors


def write_json_report(path: str, passed: bool, mode: str, errors: List[str], warnings: List[str]) -> None:
    """Write the stable, machine-readable companion to the console report."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"passed": passed, "mode": mode, "errors": errors, "warnings": warnings},
            f,
            ensure_ascii=False,
            indent=2,
        )


def validate_delta(current_path: str, delta_path: str, mode: str = "process", strict: bool = False,
                   report_json: str | None = None) -> bool:
    print(f"验证Delta: {delta_path}")
    print(f"当前结构: {current_path}")
    print(f"模式: {mode}{' + strict' if strict else ''}")
    print("=" * 70)
    try:
        current = load_json(current_path) if os.path.isfile(current_path) else {k: [] for k in COLLECTION_KEYS}
    except json.JSONDecodeError as exc:
        print(f"当前结构JSON格式错误: {exc}")
        if report_json:
            write_json_report(report_json, False, mode, [f"当前结构JSON格式错误: {exc}"], [])
        return False
    try:
        delta = load_json(delta_path)
    except json.JSONDecodeError as exc:
        print(f"Delta JSON格式错误: {exc}")
        if report_json:
            write_json_report(report_json, False, mode, [f"Delta JSON格式错误: {exc}"], [])
        return False

    errors, warnings = validate_top(delta, mode)
    if isinstance(delta, dict):
        e2, w2 = validate_elements(current if isinstance(current, dict) else {}, delta, mode)
        errors.extend(e2)
        warnings.extend(w2)
        errors.extend(validate_tag_governance_ops(current if isinstance(current, dict) else {}, delta, mode))
        if mode == "governance":
            errors.extend(validate_no_change_metadata(delta))
    if strict and warnings:
        errors.extend(warnings)
        warnings = []

    passed = not errors
    if report_json:
        write_json_report(report_json, passed, mode, errors, warnings)

    if errors:
        print(f"\n发现 {len(errors)} 个错误：")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}")
    if warnings:
        print(f"\n发现 {len(warnings)} 个警告：")
        for i, warn in enumerate(warnings, 1):
            print(f"  {i}. {warn}")
    if not errors and not warnings:
        print("\nDelta校验通过。")
        return True
    if not errors:
        print(f"\nDelta可合并，但有 {len(warnings)} 个警告。")
        return True
    print("\nDelta校验未通过。")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Delta入库前校验")
    parser.add_argument("--mode", choices=["process", "governance", "final"], default="process")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--report-json", help="write machine-readable validation status to this JSON file")
    parser.add_argument("current_json")
    parser.add_argument("delta_json")
    args = parser.parse_args()
    return 0 if validate_delta(args.current_json, args.delta_json, mode=args.mode, strict=args.strict,
                               report_json=args.report_json) else 1


if __name__ == "__main__":
    sys.exit(main())
