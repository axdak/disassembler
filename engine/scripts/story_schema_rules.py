#!/usr/bin/env python3
"""
故事结构 JSON Schema 与校验规则。

目标：
1. 最终故事结构.json 必须严格符合用户指定的顶层结构和七类元素结构。
2. 过程库 故事结构_增量.json 也保持同一骨架，但允许“未完成叙事”产生的前向引用、数量不足、介绍不完整等 warning。
3. 强 Schema 不负责删信息；信息保全由 normalize_story_schema.py 完成。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from chronology import (
    BASE_TIME,
    chapter_time,
    chronological_event_order_errors,
    event_group_name_errors,
    event_group_order_errors,
    first_involved_chapter,
    is_iso_time,
    normalize_chapter_value,
    normalize_involved_chapters,
    split_event_group,
)

TOP_LEVEL_KEYS = ["介绍", "角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]
COLLECTION_KEYS = ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]
REQUIRED_TOP_LEVEL_KEYS = {"介绍", "角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"}
OPTIONAL_TOP_LEVEL_KEYS = {"其他事项集"}
COMMON_FIELDS = {"别名", "标签集", "介绍", "详情"}
CHAPTER_TRACE_FIELDS = {"首次章节", "最近章节"}
TRACEABLE_COLLECTION_KEYS = {"角色集", "地点集", "线索集", "阵营集", "物品集"}

VALID_FIELDS: Dict[str, Set[str]] = {
    "角色集": {"名称", "是否主角", "性别", "年龄", "生日", "所属阵营", "关系", "分组", *COMMON_FIELDS},
    "事件集": {"名称", "发生地点", "参与成员", "重量级", "目标事件", "分组", "时间", *COMMON_FIELDS},
    "地点集": {"名称", "父级地点", "分组", *COMMON_FIELDS},
    "线索集": {"名称", "涉及事件", "分组", *COMMON_FIELDS},
    "阵营集": {"名称", "父级阵营", "座落地点", "分组", *COMMON_FIELDS},
    "物品集": {"名称", "分组", *COMMON_FIELDS},
    "其他事项集": {"名称", "分组", *COMMON_FIELDS},
}

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "角色集": {"名称": "", "是否主角": False, "性别": 2, "年龄": 0, "生日": BASE_TIME, "所属阵营": [], "关系": [], "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "事件集": {"名称": "", "发生地点": "", "参与成员": [], "重量级": 0, "目标事件": [], "分组": "", "时间": BASE_TIME, "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "地点集": {"名称": "", "父级地点": "", "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "线索集": {"名称": "", "涉及事件": [], "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "阵营集": {"名称": "", "父级阵营": "", "座落地点": "", "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "物品集": {"名称": "", "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
    "其他事项集": {"名称": "", "分组": "", "别名": [], "标签集": [], "介绍": "", "详情": {}},
}

TYPE_TO_COLLECTION = {
    "角色": "角色集",
    "事件": "事件集",
    "地点": "地点集",
    "线索": "线索集",
    "阵营": "阵营集",
    "物品": "物品集",
    "其他事项": "其他事项集",
    "道具": "物品集",  # 输入容错；最终规范建议统一写“物品”。
}
COLLECTION_TO_TYPE = {
    "角色集": "角色", "事件集": "事件", "地点集": "地点", "线索集": "线索", "阵营集": "阵营", "物品集": "物品", "其他事项集": "其他事项"
}

MIN_COUNTS = {"角色集": 0, "事件集": 0, "地点集": 0, "线索集": 0, "阵营集": 0, "物品集": 0, "其他事项集": 0}
DETAIL_KEY_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9\-]+$")
DETAIL_REF_RE = re.compile(r"^(角色|事件|地点|线索|阵营|物品|道具):(.+)$")
LEGACY_FINAL_TRACE_DETAIL_FIELDS = {"来源章节", "首次出现章节", "最近更新章节"}
DELTA_ONLY_DETAIL_FIELDS = {"提取理由"}
SUPPLEMENTARY_TAGS_DETAIL_KEY = "补充标签"
DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS = {
    "待确认信息", "疑似信息", "冲突声明", "关系线索", "待确认关系", "待确认引用",
    "入库依据", "档案线索", "证据摘录", "变化轨迹", "状态变化", "关键表现", "关键行为",
    "动作链条", "冲突变化", "信息揭示", "情绪转折", "喜剧机制", "关键台词", "成员状态变化",
    "人物轨迹", "性格线索", "动机线索", "关系变化", "人物证据",
    "地点轨迹", "空间线索", "氛围线索", "场景变化", "地点证据",
    "线索轨迹", "推进记录", "暗示证据", "回收记录", "线索证据",
    "阵营轨迹", "立场变化", "成员变化", "势力变化", "阵营证据",
    "物品轨迹", "用途线索", "持有变化", "物品状态", "物品证据",
    "事项轨迹", "事项证据",
}
DETAIL_REF_ARRAY_KEYS = {"关联线索", "关联角色", "关联事件", "关联地点", "关联阵营", "关联物品"}
DETAIL_CUMULATIVE_KEY_SUFFIXES = (
    "线索", "轨迹", "证据", "记录", "变化", "状态", "表现", "行为",
    "台词", "依据", "摘录", "链条", "机制", "用途", "细节",
)


def is_cumulative_detail_field(key: Any) -> bool:
    """Whether a detail key should be merged as a JSON string array."""
    if not isinstance(key, str):
        return False
    if key in DETAIL_REF_ARRAY_KEYS or key.startswith("关联"):
        return False
    return key in DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS


STRICT_DETAIL_KEY_FIELDS = {
    "首次章节", "最近章节", "涉及章节", SUPPLEMENTARY_TAGS_DETAIL_KEY, *DETAIL_REF_ARRAY_KEYS,
}


def detail_key_warnings(key: str) -> List[str]:
    warnings: List[str] = []
    if len(key) > 24:
        warnings.append("键名过长，建议压缩为稳定短键")
    punctuation_count = sum(1 for ch in key if not re.match(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]", ch))
    if punctuation_count >= 3:
        warnings.append("键名包含较多标点，建议只在确有保真价值时保留")
    if len(key) > 12 and any(mark in key for mark in "，。？！；：,.?!;:"):
        warnings.append("键名看起来像句子，建议把完整句子放入值中")
    return warnings

STRING_FIELDS = {"名称", "分组", "介绍", "发生地点", "父级地点", "座落地点", "父级阵营", "生日", "首次章节", "最近章节", "时间", "涉及章节"}
LIST_STRING_FIELDS = {"所属阵营", "关系", "参与成员", "目标事件", "涉及事件", "别名", "标签集"}

STRUCTURAL_REF_FIELDS = {
    "角色集": [("所属阵营", "阵营集", "list"), ("关系", "角色集", "relation_list")],
    "事件集": [("发生地点", "地点集", "scalar"), ("参与成员", "角色集", "list"), ("目标事件", "事件集", "list")],
    "地点集": [("父级地点", "地点集", "scalar")],
    "线索集": [("涉及事件", "事件集", "list")],
    "阵营集": [("父级阵营", "阵营集", "scalar"), ("座落地点", "地点集", "scalar")],
    "物品集": [],
    "其他事项集": [],
}

TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "references" / "narrative_taxonomy.json"
with TAXONOMY_PATH.open("r", encoding="utf-8") as f:
    NARRATIVE_TAXONOMY: Dict[str, List[str]] = json.load(f)

CONTROLLED_TAG_PREFIXES = {
    "角色集": {"人物类型"},
    "事件集": {
        "剧情母题", "叙事功能", "冲突对象", "冲突议题", "悬念问题", "悬念风险", "悬念机制",
        "剧情线主题", "爽点情绪点", "冲突悬念类型", "画面类型", "视觉用途",
    },
    "其他事项集": {"自由主题"},
}


def make_empty_structure() -> Dict[str, Any]:
    return {"介绍": {"标题": "", "描述": ""}, "角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}


def validate_controlled_tags(collection_key: str, tags: Any) -> List[str]:
    """仅校验本集合允许的带前缀标签；无前缀普通标签保持兼容。"""
    if not isinstance(tags, list):
        return []
    allowed_prefixes = CONTROLLED_TAG_PREFIXES.get(collection_key, set())
    errors: List[str] = []
    for tag in tags:
        if not isinstance(tag, str) or ":" not in tag:
            continue
        prefix, value = tag.split(":", 1)
        if prefix not in NARRATIVE_TAXONOMY:
            continue
        if prefix not in allowed_prefixes:
            errors.append(f"{collection_key}.标签集 不允许使用受控标签前缀[{prefix}]")
            continue
        if prefix != "自由主题" and value not in NARRATIVE_TAXONOMY[prefix]:
            errors.append(f"{collection_key}.标签集 包含未知受控标签[{tag}]")
    return errors


def parse_supplementary_tags(value: Any) -> List[str]:
    """解析详情.补充标签的可逆 JSON 字符串表示。"""
    if not isinstance(value, str):
        raise ValueError("必须是 JSON 字符串")
    try:
        tags = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("必须是可解析的 JSON 字符串数组") from exc
    if not isinstance(tags, list) or not tags:
        raise ValueError("必须是非空字符串数组")
    if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("数组项必须是非空字符串")
    normalized = [tag.strip() for tag in tags]
    if len(set(normalized)) != len(normalized):
        raise ValueError("数组项不得重复")
    return normalized


def dump_supplementary_tags(tags: List[str]) -> str:
    """输出稳定、紧凑且可逆的详情.补充标签 JSON 字符串。"""
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def final_disallowed_detail_fields(collection_key: str) -> Set[str]:
    fields = set(LEGACY_FINAL_TRACE_DETAIL_FIELDS)
    if collection_key != "事件集":
        fields |= DELTA_ONLY_DETAIL_FIELDS
    return fields


def exact_name_sets(data: Dict[str, Any]) -> Dict[str, Set[str]]:
    sets: Dict[str, Set[str]] = {k: set() for k in COLLECTION_KEYS}
    if not isinstance(data, dict):
        return sets
    for collection_key in COLLECTION_KEYS:
        items = data.get(collection_key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = item.get("名称")
                if isinstance(name, str) and name.strip():
                    sets[collection_key].add(name.strip())
    return sets


def alias_to_name_maps(data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    maps: Dict[str, Dict[str, str]] = {k: {} for k in COLLECTION_KEYS}
    if not isinstance(data, dict):
        return maps
    for collection_key in COLLECTION_KEYS:
        for item in data.get(collection_key, []) or []:
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if not isinstance(name, str) or not name.strip():
                continue
            for alias in item.get("别名", []) or []:
                if isinstance(alias, str) and alias.strip() and alias.strip() not in maps[collection_key]:
                    maps[collection_key][alias.strip()] = name.strip()
    return maps


def name_alias_sets(data: Dict[str, Any]) -> Dict[str, Set[str]]:
    sets = exact_name_sets(data)
    for key, mapping in alias_to_name_maps(data).items():
        sets[key].update(mapping.keys())
    return sets


def _issue_ref(
    errors: List[str],
    warnings: List[str],
    mode: str,
    label: str,
    field: str,
    ref: Any,
    target_collection: str,
    name_sets: Dict[str, Set[str]],
    alias_maps: Optional[Dict[str, Dict[str, str]]] = None,
    soft_governance: bool = False,
) -> None:
    if ref in (None, ""):
        return
    if not isinstance(ref, str):
        errors.append(f"{label}.{field} 引用值必须是字符串，实际为{type(ref).__name__}")
        return
    clean = ref.strip()
    if not clean:
        return
    if clean in name_sets.get(target_collection, set()):
        return
    if alias_maps and clean in alias_maps.get(target_collection, {}):
        # 过程阶段允许别名先通过，但提示应规范化；最终必须使用正式名称。
        msg = f"{label}.{field} 使用了别名引用[{clean}]，应规范化为正式名称[{alias_maps[target_collection][clean]}]"
        if mode == "final":
            errors.append(msg)
        else:
            warnings.append(msg)
        return
    msg = f"{label}.{field} 引用了不存在的{COLLECTION_TO_TYPE[target_collection]}[{clean}]"
    if mode in ("process", "delta"):
        warnings.append(msg + "；过程阶段可能是前向引用，最终交付前必须显式新增/合并目标或移入详情.关系线索/待确认信息")
    elif mode == "governance" and soft_governance:
        warnings.append(msg + "；治理阶段允许事件发生地点保留细粒度子空间，最终交付前建议收敛为已注册父级地点或正式地点")
    else:
        errors.append(msg)


def validate_detail_schema(
    detail: Any,
    label: str,
    name_sets: Dict[str, Set[str]],
    errors: List[str],
    warnings: List[str],
    *,
    mode: str = "process",
    alias_maps: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    if not isinstance(detail, dict):
        errors.append(f"{label}.详情 必须是对象")
        return
    for key, value in detail.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{label}.详情 键名[{key}]不合规：键名必须是非空字符串")
            continue
        if key in STRICT_DETAIL_KEY_FIELDS or key.startswith("关联"):
            if not DETAIL_KEY_RE.match(key):
                errors.append(f"{label}.详情 键名[{key}]不合规：机器字段和明确引用字段键名不可包含标点符号、空格或下划线")
        elif not DETAIL_KEY_RE.match(key):
            warnings.append(f"{label}.详情 键名[{key}]包含标点/空格/下划线；普通档案字段允许保真，但建议优先使用稳定短键")
        for warning in detail_key_warnings(key):
            warnings.append(f"{label}.详情 键名[{key}] {warning}")
        if key == SUPPLEMENTARY_TAGS_DETAIL_KEY:
            try:
                parse_supplementary_tags(value)
            except ValueError as exc:
                errors.append(f"{label}.详情.{SUPPLEMENTARY_TAGS_DETAIL_KEY} {exc}")
            continue
        if isinstance(value, str):
            m = DETAIL_REF_RE.match(value.strip())
            if m:
                ref_type, ref_name = m.group(1), m.group(2).strip()
                target_collection = TYPE_TO_COLLECTION[ref_type]
                _issue_ref(errors, warnings, mode, label, f"详情.{key}", ref_name, target_collection, name_sets, alias_maps)
            continue
        if isinstance(value, list):
            if not value:
                warnings.append(f"{label}.详情[{key}] 是空数组；数组仅应在指代多个故事元素时使用")
            for i, elem in enumerate(value):
                if not isinstance(elem, str):
                    errors.append(f"{label}.详情[{key}][{i}] 必须是字符串")
                    continue
                m = DETAIL_REF_RE.match(elem.strip())
                if not m:
                    errors.append(f"{label}.详情[{key}][{i}] 数组项必须是 类型:名称 格式，实际为[{elem}]")
                    continue
                ref_type, ref_name = m.group(1), m.group(2).strip()
                target_collection = TYPE_TO_COLLECTION[ref_type]
                _issue_ref(errors, warnings, mode, label, f"详情.{key}[{i}]", ref_name, target_collection, name_sets, alias_maps)
            continue
        errors.append(f"{label}.详情[{key}] 值类型必须是 string 或 string[]，实际为{type(value).__name__}")


def validate_item_schema(
    collection_key: str,
    item: Any,
    idx: int,
    name_sets: Dict[str, Set[str]],
    alias_maps: Dict[str, Dict[str, str]],
    errors: List[str],
    warnings: List[str],
    *,
    mode: str = "process",
) -> None:
    type_name = COLLECTION_TO_TYPE[collection_key]
    label = f"{type_name}[{item.get('名称', idx) if isinstance(item, dict) else idx}]"
    if not isinstance(item, dict):
        errors.append(f"{collection_key}[{idx}] 必须是对象")
        return

    valid = VALID_FIELDS[collection_key]
    missing = sorted(valid - set(item.keys()))
    extra = sorted(set(item.keys()) - valid)
    for field in missing:
        errors.append(f"{label} 缺少必需字段[{field}]；请先运行 normalize_story_schema.py 补齐默认字段")
    for field in extra:
        errors.append(f"{label} 包含规范外字段[{field}]；请先运行 normalize_story_schema.py 迁移到详情")

    name = item.get("名称")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{label}.名称 必须是非空字符串")

    for field in STRING_FIELDS:
        if field in item and not isinstance(item[field], str):
            errors.append(f"{label}.{field} 必须是字符串，实际为{type(item[field]).__name__}")

    for field in LIST_STRING_FIELDS:
        if field in item:
            value = item[field]
            if not isinstance(value, list):
                errors.append(f"{label}.{field} 必须是数组")
            else:
                for i, elem in enumerate(value):
                    if not isinstance(elem, str):
                        errors.append(f"{label}.{field}[{i}] 必须是字符串")

    if "标签集" in item:
        for error in validate_controlled_tags(collection_key, item["标签集"]):
            errors.append(f"{label}.{error.split('.', 1)[1]}")

    if collection_key == "角色集":
        if "是否主角" in item and not isinstance(item["是否主角"], bool):
            errors.append(f"{label}.是否主角 必须是布尔值")
        if "性别" in item and (not isinstance(item["性别"], int) or item["性别"] not in (0, 1, 2)):
            errors.append(f"{label}.性别 必须是整数0/1/2")
        if "年龄" in item and not isinstance(item["年龄"], int):
            errors.append(f"{label}.年龄 必须是整数；未知年龄填0")
        if not is_iso_time(item.get("生日")):
            errors.append(f"{label}.生日 必须是合法ISO时间；未知固定填{BASE_TIME}")

    detail = item.get("详情", {})
    if not isinstance(detail, dict):
        detail = {}

    if collection_key in TRACEABLE_COLLECTION_KEYS:
        for field in ("首次章节", "最近章节"):
            value = detail.get(field)
            if not isinstance(value, str) or not value or normalize_chapter_value(value) != value:
                errors.append(f"{label}.详情.{field} 必须是固定宽度章节序号，例如0001")
        first = detail.get("首次章节", "")
        recent = detail.get("最近章节", "")
        if isinstance(first, str) and isinstance(recent, str) and first and recent and int(first) > int(recent):
            errors.append(f"{label}.详情.首次章节 不得晚于 最近章节")

    if collection_key == "事件集":
        if "重量级" in item and (not isinstance(item["重量级"], int) or item["重量级"] < 0 or item["重量级"] > 100):
            errors.append(f"{label}.重量级 必须是0-100之间的整数")
        if isinstance(item.get("分组"), str) and "/" in item.get("分组", ""):
            warnings.append(f"{label}.分组 包含层级分隔符/；事件分组不建议分层")
        involved = detail.get("涉及章节")
        normalized_involved = normalize_involved_chapters(involved)
        if not isinstance(involved, str) or not involved or normalized_involved != involved:
            errors.append(f"{label}.详情.涉及章节 必须为固定宽度、升序、去重的字符串，例如0043，0044")
        else:
            first_ch = first_involved_chapter(item)
            if first_ch is not None:
                expected_time = chapter_time(first_ch)
                if item.get("时间") != expected_time:
                    errors.append(f"{label}.时间 必须等于最小涉及章节的章节时间[{expected_time}]")
        group = item.get("分组")
        if isinstance(group, str) and group:
            rank, group_name = split_event_group(group)
            if rank is None or not group_name:
                errors.append(f"{label}.分组 必须为8位段号-40字以内复合剧情段短名，例如00000010-退婚尊严线冲突爆发羞辱反击身份尊严")
            else:
                for error in event_group_name_errors(group_name):
                    errors.append(f"{label}.分组 {error}")

    # 结构引用
    for field, target_collection, kind in STRUCTURAL_REF_FIELDS[collection_key]:
        value = item.get(field)
        soft_governance = collection_key == "事件集" and field == "发生地点" and target_collection == "地点集"
        if kind == "scalar":
            _issue_ref(errors, warnings, mode, label, field, value, target_collection, name_sets, alias_maps, soft_governance=soft_governance)
        elif kind == "list":
            if isinstance(value, list):
                for ref in value:
                    _issue_ref(errors, warnings, mode, label, field, ref, target_collection, name_sets, alias_maps)
        elif kind == "relation_list":
            if isinstance(value, list):
                for rel in value:
                    if not isinstance(rel, str) or ":" not in rel:
                        errors.append(f"{label}.关系 项[{rel}]必须是 关系类型:角色名 格式")
                    else:
                        rel_type, ref_name = rel.split(":", 1)
                        if not rel_type.strip() or not ref_name.strip():
                            errors.append(f"{label}.关系 项[{rel}]关系类型或角色名为空")
                        else:
                            _issue_ref(errors, warnings, mode, label, "关系", ref_name.strip(), target_collection, name_sets, alias_maps)

    if collection_key == "地点集":
        parent = item.get("父级地点", "")
        if parent and parent == item.get("名称"):
            errors.append(f"{label}.父级地点 不可指向自身")
    if collection_key == "阵营集":
        parent = item.get("父级阵营", "")
        if parent and parent == item.get("名称"):
            errors.append(f"{label}.父级阵营 不可指向自身")

    if "详情" in item:
        if isinstance(item.get("详情"), dict):
            for field in final_disallowed_detail_fields(collection_key) & set(item["详情"]):
                errors.append(f"{label}.详情.{field} 不得存在于最终元素；请保留在Delta、章节分析、治理补丁或变更日志")
        validate_detail_schema(item.get("详情"), label, name_sets, errors, warnings, mode=mode, alias_maps=alias_maps)
        detail_tags = item.get("详情", {}).get(SUPPLEMENTARY_TAGS_DETAIL_KEY) if isinstance(item.get("详情"), dict) else None
        if detail_tags is not None:
            try:
                overlap = set(parse_supplementary_tags(detail_tags)) & set(item.get("标签集", []))
                if overlap:
                    errors.append(f"{label}.标签集 与详情.{SUPPLEMENTARY_TAGS_DETAIL_KEY} 不得重复: {sorted(overlap)}")
            except ValueError:
                # 具体格式错误已由 validate_detail_schema 输出。
                pass

    if mode == "final":
        intro = item.get("介绍")
        if not isinstance(intro, str) or not intro.strip():
            errors.append(f"{label}.介绍 为空；最终交付必须补充其在故事中的作用和意义")
        group = item.get("分组")
        if not isinstance(group, str) or not group.strip():
            warnings.append(f"{label}.分组 为空；建议补充分组")


def validate_parent_cycles(data: Dict[str, Any], errors: List[str]) -> None:
    for collection_key, parent_field in [("地点集", "父级地点"), ("阵营集", "父级阵营")]:
        mapping = {}
        for item in data.get(collection_key, []) or []:
            if isinstance(item, dict) and isinstance(item.get("名称"), str):
                mapping[item["名称"]] = item.get(parent_field, "")
        for name in list(mapping):
            seen = set()
            cur = name
            while cur:
                if cur in seen:
                    errors.append(f"{COLLECTION_TO_TYPE[collection_key]}[{name}] 存在父级循环引用")
                    break
                seen.add(cur)
                cur = mapping.get(cur, "")


def validate_exact_story_schema(data: Any, *, mode: str = "process", enforce_counts: Optional[bool] = None) -> Tuple[List[str], List[str]]:
    """返回 (errors, warnings)。

    mode:
      - process: 逐章过程库。硬卡格式/字段/类型；前向引用、数量不足、内容深度不足为 warning。
      - governance: 治理后阶段。引用错误升级为 error；事件发生地点的未注册细粒度子空间保留为 warning；数量不足仍为 warning。
      - final: 最终交付。引用、数量、介绍等核心质量要求均为 error。
    """
    if mode not in {"process", "governance", "final", "delta"}:
        raise ValueError(f"未知校验模式: {mode}")
    if enforce_counts is None:
        enforce_counts = mode == "final"

    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return ["顶层必须是JSON对象"], warnings

    missing_top = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in data]
    extra_top = [k for k in data.keys() if k not in TOP_LEVEL_KEYS]
    for key in missing_top:
        errors.append(f"缺少顶层字段[{key}]")
    for key in extra_top:
        errors.append(f"顶层包含规范外字段[{key}]")

    intro = data.get("介绍")
    if not isinstance(intro, dict):
        errors.append("介绍 必须是对象")
    else:
        intro_extra = sorted(set(intro.keys()) - {"标题", "描述"})
        intro_missing = sorted({"标题", "描述"} - set(intro.keys()))
        for key in intro_missing:
            errors.append(f"介绍 缺少字段[{key}]")
        for key in intro_extra:
            errors.append(f"介绍 包含规范外字段[{key}]")
        for key in ["标题", "描述"]:
            if key in intro and not isinstance(intro[key], str):
                errors.append(f"介绍.{key} 必须是字符串")
        if mode == "final":
            if not isinstance(intro.get("标题"), str) or not intro.get("标题", "").strip():
                errors.append("介绍.标题 为空；最终交付必须填写故事名称")
            if not isinstance(intro.get("描述"), str) or not intro.get("描述", "").strip():
                errors.append("介绍.描述 为空；最终交付必须填写故事简介")
        else:
            if isinstance(intro.get("标题"), str) and not intro.get("标题", "").strip():
                warnings.append("介绍.标题 为空；过程阶段允许，最终交付前必须补充")

    name_sets = exact_name_sets(data)
    alias_maps = alias_to_name_maps(data)

    for collection_key in COLLECTION_KEYS:
        if collection_key in OPTIONAL_TOP_LEVEL_KEYS and collection_key not in data:
            continue
        items = data.get(collection_key)
        if not isinstance(items, list):
            errors.append(f"{collection_key} 必须是数组")
            continue
        if len(items) < MIN_COUNTS[collection_key]:
            msg = f"{collection_key}元素数量偏少：建议/最终要求 >= {MIN_COUNTS[collection_key]}，实际 {len(items)}。拆书过程不得为达标编造原文不存在的元素。"
            if enforce_counts:
                errors.append(msg)
            else:
                warnings.append(msg)
        seen = set()
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                name = item.get("名称")
                if isinstance(name, str) and name.strip():
                    if name.strip() in seen:
                        errors.append(f"{collection_key}中存在重复名称[{name.strip()}]")
                    seen.add(name.strip())
            validate_item_schema(collection_key, item, idx, name_sets, alias_maps, errors, warnings, mode=mode)

    validate_parent_cycles(data, errors)
    events = data.get("事件集", [])
    if isinstance(events, list):
        errors.extend(chronological_event_order_errors(events))
        errors.extend(event_group_order_errors([event for event in events if isinstance(event, dict)]))
    return errors, warnings
