#!/usr/bin/env python3
"""
保全式故事结构规范化脚本。

它解决 v5 强 Schema 的信息丢失风险：
- 不删除非标准字段，而是迁移到 详情。
- 不删除详情里的复杂值，而是转成字符串。
- 缺失标准字段自动补默认值。
- 尝试把别名引用规范化为正式名称。
- 可把不存在的结构引用移入 详情.待确认引用，避免过程库被硬 Schema 卡死，同时保留信息。

用法：
  python normalize_story_schema.py <input.json> --out <output.json>
  python normalize_story_schema.py <input.json> --in-place --backup
  python normalize_story_schema.py <input.json> --mode process|final
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Tuple

from story_schema_rules import (
    COLLECTION_KEYS, TOP_LEVEL_KEYS, DEFAULTS, VALID_FIELDS, TRACEABLE_COLLECTION_KEYS,
    TYPE_TO_COLLECTION, COLLECTION_TO_TYPE, DETAIL_REF_RE, DETAIL_KEY_RE,
    DETAIL_REF_ARRAY_KEYS, STRUCTURAL_REF_FIELDS,
    DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS, is_cumulative_detail_field,
    make_empty_structure, exact_name_sets, alias_to_name_maps,
    SUPPLEMENTARY_TAGS_DETAIL_KEY, canonicalize_supplementary_tags,
    exclude_primary_supplementary_tags, dump_supplementary_tags,
)
from chronology import (
    BASE_TIME,
    is_iso_time,
    normalize_chapter_value,
    normalize_event_temporal_fields,
    order_events_by_chapter,
    parse_chapter_numbers,
    assign_event_group_orders,
)

LIST_FIELDS = {"所属阵营", "关系", "参与成员", "目标事件", "涉及事件", "别名", "标签集"}
STRING_FIELDS = {"名称", "分组", "介绍", "发生地点", "父级地点", "座落地点", "父级阵营", "生日", "时间"}
BASE_LEGACY_TRACE_DETAIL_FIELDS = {"来源章节", "首次出现章节", "最近更新章节"}
DELTA_ONLY_DETAIL_FIELDS = {"提取理由"}
REF_LIST_FIELDS = {
    "角色集": {"所属阵营": "阵营集"},
    "事件集": {"参与成员": "角色集", "目标事件": "事件集"},
    "线索集": {"涉及事件": "事件集"},
}
REF_SCALAR_FIELDS = {
    "事件集": {"发生地点": "地点集"},
    "地点集": {"父级地点": "地点集"},
    "阵营集": {"父级阵营": "阵营集", "座落地点": "地点集"},
}
RELATION_FIELD = "关系"
CUMULATIVE_DETAIL_ARRAY_FIELDS = DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json_array(value: Any) -> Any:
    """仅解开一层 JSON 数组编码；无效文本和非数组值逐字保留。"""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(decoded, list):
            return decoded
    return value


def is_detail_ref_array_field(key: Any) -> bool:
    """明确的关联字段才采用引用数组契约，普通叙述不猜测类型。"""
    return isinstance(key, str) and (key in DETAIL_REF_ARRAY_KEYS or key.startswith("关联"))


def canonicalize_detail_ref_array(value: Any) -> Any:
    """规范确定的引用容器和类型拼写；未知类型与错配目标留待原校验。"""
    def canonical(entry):
        if not isinstance(entry, str):
            return entry
        prefix, separator, name = entry.partition(":")
        if separator and prefix.strip() in COLLECTION_TO_TYPE and name.strip():
            # 集合名到类型是一一映射；仅修拼写，不猜人物身份或跨类别改目标。
            return f"{COLLECTION_TO_TYPE[prefix.strip()]}:{name.strip()}"
        return entry
    decoded = decode_json_array(value)
    if isinstance(decoded, list):
        return [canonical(entry) for entry in decoded]
    decoded = canonical(decoded)
    if isinstance(decoded, str) and is_typed_ref(decoded):
        return [decoded]
    return decoded


def strip_matching_ref_prefix(value: Any, target_collection: str) -> Any:
    """标准引用使用裸正式名；错误类型前缀保留在原字段供校验拦截。"""
    if isinstance(value, str):
        match = DETAIL_REF_RE.match(value.strip())
        if match and TYPE_TO_COLLECTION.get(match.group(1)) == target_collection:
            return match.group(2).strip()
    return value


def _standard_field_comparison(collection: str, field: str, value: Any) -> Any:
    """比较只忽略可逆数组编码和正确的类型前缀，不猜测业务等价关系。"""
    if isinstance(DEFAULTS[collection].get(field), list):
        value = decode_json_array(value)
    for ref_field, target, kind in STRUCTURAL_REF_FIELDS.get(collection, []):
        if field == ref_field and kind != "relation_list":
            if kind == "list" and isinstance(value, list):
                return [strip_matching_ref_prefix(entry, target) for entry in value]
            return strip_matching_ref_prefix(value, target)
    return value


def restore_standard_detail_fields(item: Dict[str, Any], collection: str, logs: List[str]) -> None:
    """按真实类型归位同名标准字段（含明确空值）；有差异时保留双方证据。"""
    detail = item.get("详情")
    if not isinstance(detail, dict):
        return
    for field in list(detail):
        if field not in VALID_FIELDS[collection] or field in {"名称", "详情"}:
            continue
        value = detail[field]
        default = DEFAULTS[collection].get(field)
        if isinstance(default, list):
            value = decode_json_array(value)
        # 使用默认值的真实类型作为形状契约；bool 不能冒充 int。
        fits = (
            (isinstance(default, bool) and isinstance(value, bool))
            or (type(default) is int and type(value) is int)
            or (isinstance(default, str) and isinstance(value, str))
            or (isinstance(default, list) and isinstance(value, list)
                and all(isinstance(entry, str) for entry in value))
        )
        if not fits:
            continue
        top = item.get(field)
        if isinstance(default, list):
            top = decode_json_array(top)
        empty = top is None or top == [] or top == {} or (isinstance(top, str) and not top.strip())
        label = f"{collection}[{item.get('名称', '')}].{field}"
        if field not in item or empty:
            item[field] = value
            detail.pop(field)
            logs.append(f"{label} 从详情迁回顶层（按声明契约保全原值）")
        elif _standard_field_comparison(collection, field, top) == _standard_field_comparison(collection, field, value):
            detail.pop(field)
            logs.append(f"{label} 已归位，移除详情内等值副本")
        else:
            logs.append(f"{label} 与详情同名标准字段冲突，保留两处原值待修复")


def sanitize_key(key: Any, fallback_prefix: str = "迁移字段") -> str:
    raw = str(key)
    clean = raw.strip()
    return clean or fallback_prefix


def ensure_unique_key(detail: Dict[str, Any], key: str) -> str:
    if key not in detail:
        return key
    i = 2
    while f"{key}{i}" in detail:
        i += 1
    return f"{key}{i}"


def append_detail(detail: Dict[str, Any], key: str, value: Any, logs: List[str]) -> None:
    safe_key = sanitize_key(key)
    val = normalize_detail_value(value, logs)
    if is_cumulative_detail_field(safe_key):
        detail[safe_key] = merge_cumulative_detail_array(detail.get(safe_key), val)
        return
    if safe_key in detail:
        old = detail[safe_key]
        if isinstance(old, str) and isinstance(val, str):
            if val and val not in old:
                detail[safe_key] = old + "\n" + val if old else val
        elif isinstance(old, list) and isinstance(val, list):
            for x in val:
                if x not in old:
                    old.append(x)
        else:
            new_key = ensure_unique_key(detail, safe_key)
            detail[new_key] = val
    else:
        detail[safe_key] = val


def parse_json_string_array_or_lines(value: Any) -> List[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        text = str(value).strip()
        return [text] if text else []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [text]


def merge_cumulative_detail_array(old_value: Any, new_value: Any) -> str:
    items: List[str] = []
    for item in parse_json_string_array_or_lines(old_value) + parse_json_string_array_or_lines(new_value):
        if item and item not in items:
            items.append(item)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def is_typed_ref(value: str) -> bool:
    return bool(isinstance(value, str) and DETAIL_REF_RE.match(value.strip()))


def detail_fields_to_skip(collection_key: str) -> set[str]:
    fields = set(BASE_LEGACY_TRACE_DETAIL_FIELDS)
    if collection_key != "事件集":
        fields |= DELTA_ONLY_DETAIL_FIELDS
    return fields


def normalize_detail_value(value: Any, logs: List[str]) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # 只有全是 类型:名称 的 string[] 才保留数组；普通多值档案写成可逆 JSON 字符串数组。
        if value and all(isinstance(x, str) and is_typed_ref(x) for x in value):
            result = []
            for x in value:
                if x not in result:
                    result.append(x)
            return result
        return json.dumps(
            [stringify(x).strip() for x in value if stringify(x).strip()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return stringify(value)


def normalize_intro(data: Dict[str, Any], logs: List[str]) -> Dict[str, str]:
    intro = data.get("介绍")
    if not isinstance(intro, dict):
        logs.append("介绍不是对象，已重建为标准对象并将原值迁入描述")
        return {"标题": "", "描述": stringify(intro)}
    result = {"标题": stringify(intro.get("标题", "")), "描述": stringify(intro.get("描述", ""))}
    extra = {k: v for k, v in intro.items() if k not in {"标题", "描述"}}
    if extra:
        parts = [result["描述"]] if result["描述"] else []
        for k, v in extra.items():
            parts.append(f"【迁移自介绍.{k}】{stringify(v)}")
            logs.append(f"介绍.{k} 为规范外字段，已迁入介绍.描述")
        result["描述"] = "\n".join(parts)
    return result


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是", "主角"}
    return bool(value)


def coerce_int(value: Any, default: int = 0, min_value: int | None = None, max_value: int | None = None) -> int:
    if isinstance(value, bool):
        n = int(value)
    elif isinstance(value, int):
        n = value
    elif isinstance(value, float):
        n = int(value)
    elif isinstance(value, str):
        m = re.search(r"-?\d+", value)
        n = int(m.group(0)) if m else default
    else:
        n = default
    if min_value is not None:
        n = max(min_value, n)
    if max_value is not None:
        n = min(max_value, n)
    return n


def coerce_string_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        result = []
        for x in value:
            s = stringify(x).strip()
            if s and s not in result:
                result.append(s)
        return result
    s = stringify(value).strip()
    if not s:
        return []
    # 逗号/顿号切分仅作为容错；关系字段里的冒号不会被破坏。
    parts = re.split(r"[，,、；;]\s*", s)
    result = []
    for p in parts:
        p = p.strip()
        if p and p not in result:
            result.append(p)
    return result


def build_alias_maps(data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    return alias_to_name_maps(data)


def resolve_name(name: str, target_collection: str, name_sets: Dict[str, set], alias_maps: Dict[str, Dict[str, str]], logs: List[str]) -> str:
    if not isinstance(name, str):
        return stringify(name)
    clean = name.strip()
    if clean in name_sets.get(target_collection, set()):
        return clean
    if clean in alias_maps.get(target_collection, {}):
        formal = alias_maps[target_collection][clean]
        logs.append(f"别名引用[{clean}]已规范化为正式名称[{formal}]")
        return formal
    return clean


def quarantine_ref(item: Dict[str, Any], field: str, value: str, target_collection: str, logs: List[str]) -> None:
    detail = item.setdefault("详情", {})
    label = f"{field}疑似{COLLECTION_TO_TYPE[target_collection]}"
    append_detail(detail, "待确认引用", f"{label}:{value}", logs)
    logs.append(f"{item.get('名称','?')}.{field} 的无效/前向引用[{value}]已移入详情.待确认引用")


def normalize_refs(item: Dict[str, Any], collection_key: str, name_sets: Dict[str, set], alias_maps: Dict[str, Dict[str, str]], logs: List[str], quarantine_invalid_refs: bool) -> None:
    # 标量引用
    for field, target_collection in REF_SCALAR_FIELDS.get(collection_key, {}).items():
        value = item.get(field, "")
        if not value:
            continue
        match = DETAIL_REF_RE.match(value.strip()) if isinstance(value, str) else None
        if match and TYPE_TO_COLLECTION.get(match.group(1)) != target_collection:
            # 类型错配是硬错误；即使启用不存在引用隔离，也不能清空错误字段。
            continue
        value = strip_matching_ref_prefix(value, target_collection)
        resolved = resolve_name(stringify(value), target_collection, name_sets, alias_maps, logs)
        if resolved not in name_sets.get(target_collection, set()) and quarantine_invalid_refs:
            quarantine_ref(item, field, resolved, target_collection, logs)
            item[field] = ""
        else:
            item[field] = resolved

    # 列表引用
    for field, target_collection in REF_LIST_FIELDS.get(collection_key, {}).items():
        values = item.get(field, [])
        if not isinstance(values, list):
            values = coerce_string_list(values)
        keep = []
        for value in values:
            if not isinstance(value, str):
                # 错误数组项不可转字符串或丢弃，否则类型校验失去原始证据。
                keep.append(value)
                continue
            match = DETAIL_REF_RE.match(value.strip())
            if match and TYPE_TO_COLLECTION.get(match.group(1)) != target_collection:
                keep.append(value)
                continue
            value = strip_matching_ref_prefix(value, target_collection)
            resolved = resolve_name(value, target_collection, name_sets, alias_maps, logs)
            if resolved not in name_sets.get(target_collection, set()) and quarantine_invalid_refs:
                quarantine_ref(item, field, resolved, target_collection, logs)
            elif resolved not in keep:
                keep.append(resolved)
        item[field] = keep

    # 角色关系：关系类型:角色名
    if collection_key == "角色集":
        rels = coerce_string_list(item.get("关系", []))
        keep = []
        for rel in rels:
            if ":" not in rel:
                if quarantine_invalid_refs:
                    append_detail(item.setdefault("详情", {}), "待确认关系", rel, logs)
                    logs.append(f"{item.get('名称','?')}.关系 非标准关系[{rel}]已移入详情.待确认关系")
                else:
                    keep.append(rel)
                continue
            rel_type, ref = rel.split(":", 1)
            rel_type, ref = rel_type.strip(), ref.strip()
            resolved = resolve_name(ref, "角色集", name_sets, alias_maps, logs)
            new_rel = f"{rel_type}:{resolved}"
            if resolved not in name_sets.get("角色集", set()) and quarantine_invalid_refs:
                append_detail(item.setdefault("详情", {}), "待确认关系", new_rel, logs)
                logs.append(f"{item.get('名称','?')}.关系 无效/前向关系[{new_rel}]已移入详情.待确认关系")
            elif new_rel not in keep:
                keep.append(new_rel)
        item["关系"] = keep


def normalize_detail_refs(detail: Dict[str, Any], name_sets: Dict[str, set], alias_maps: Dict[str, Dict[str, str]], logs: List[str], quarantine_invalid_refs: bool) -> None:
    # 详情的引用保持 类型:名称；数组项开启 quarantine 时，不存在引用下沉到待确认引用。
    for key, value in list(detail.items()):
        if is_detail_ref_array_field(key):
            value = canonicalize_detail_ref_array(value)
            detail[key] = value
        if isinstance(value, str):
            m = DETAIL_REF_RE.match(value.strip())
            if m:
                typ, ref = m.group(1), m.group(2).strip()
                target = TYPE_TO_COLLECTION[typ]
                resolved = resolve_name(ref, target, name_sets, alias_maps, logs)
                display_type = "物品" if typ == "道具" else typ
                detail[key] = f"{display_type}:{resolved}"
        elif isinstance(value, list):
            new = []
            invalid = []
            expected_collection = (TYPE_TO_COLLECTION.get(key.removeprefix("关联"))
                                   if key in DETAIL_REF_ARRAY_KEYS else None)
            for elem in value:
                if not isinstance(elem, str):
                    new.append(elem)
                    continue
                m = DETAIL_REF_RE.match(elem.strip())
                if not m:
                    new.append(elem)
                    continue
                typ, ref = m.group(1), m.group(2).strip()
                target = TYPE_TO_COLLECTION[typ]
                if expected_collection and target != expected_collection:
                    # 错类型是字段契约错误，不是前向引用；保留原字段才能继续拦截。
                    new.append(elem)
                    continue
                resolved = resolve_name(ref, target, name_sets, alias_maps, logs)
                display_type = "物品" if typ == "道具" else typ
                new_elem = f"{display_type}:{resolved}"
                if quarantine_invalid_refs and resolved not in name_sets.get(target, set()):
                    if new_elem not in invalid:
                        invalid.append(new_elem)
                    append_detail(detail, "待确认引用", new_elem, logs)
                    logs.append(f"详情.{key} 的无效/前向引用[{new_elem}]已移入详情.待确认引用")
                elif new_elem not in new:
                    new.append(new_elem)
            if new:
                detail[key] = new
            elif invalid:
                detail[key] = []
            else:
                detail[key] = []


def normalize_item(raw: Any, collection_key: str, logs: List[str]) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULTS[collection_key])
    if not isinstance(raw, dict):
        logs.append(f"{collection_key}中存在非对象元素，已转为默认元素并把原值写入详情.原始值")
        defaults["详情"]["原始值"] = stringify(raw)
        return defaults

    # 先处理历史错层字段，再补默认值；否则默认空数组会掩盖原始质量内容。
    raw = copy.deepcopy(raw)
    restore_standard_detail_fields(raw, collection_key, logs)

    item = copy.deepcopy(defaults)
    detail = {}
    raw_detail_value = raw.get("详情", {})
    raw_detail = raw_detail_value if isinstance(raw_detail_value, dict) else {}
    legacy_sources = raw_detail.get("来源章节", "")
    legacy_first = raw_detail.get("首次章节") or raw.get("首次章节") or raw_detail.get("首次出现章节", "")
    legacy_recent = raw_detail.get("最近章节") or raw.get("最近章节") or raw_detail.get("最近更新章节", "")
    if isinstance(raw_detail_value, dict):
        skip_detail_fields = detail_fields_to_skip(collection_key)
        for k, v in raw_detail.items():
            if k in skip_detail_fields:
                logs.append(f"{raw.get('名称','?')}.详情.{k} 已移出最终元素；紧凑追溯字段保留在详情")
                continue
            safe_key = sanitize_key(k)
            if safe_key == SUPPLEMENTARY_TAGS_DETAIL_KEY:
                # 专用规则必须先于普通数组转换，避免混合 JSON 证据在转字符串时丢失。
                normalized_value = canonicalize_supplementary_tags(v)
                if normalized_value != v:
                    logs.append(f"{raw.get('名称','?')}.详情.{safe_key} 已按原内容规范化")
                if not normalized_value:
                    continue
            elif is_detail_ref_array_field(safe_key):
                normalized_value = canonicalize_detail_ref_array(v)
            elif safe_key in VALID_FIELDS[collection_key]:
                # 同名标准字段仍在详情表示冲突或类型错误，不能串化掩盖原值。
                normalized_value = copy.deepcopy(v)
            else:
                normalized_value = normalize_detail_value(v, logs)
            if safe_key in detail:
                safe_key = ensure_unique_key(detail, safe_key)
            detail[safe_key] = normalized_value
    elif raw_detail_value not in (None, "", {}):
        detail["原详情"] = stringify(raw_detail_value)
        logs.append(f"{raw.get('名称','?')}.详情 非对象，已转成详情.原详情")

    for field, value in raw.items():
        if field == "详情":
            continue
        if field not in VALID_FIELDS[collection_key]:
            append_detail(detail, field, value, logs)
            logs.append(f"{raw.get('名称','?')} 的规范外字段[{field}]已迁入详情")
            continue
        if field in LIST_FIELDS:
            decoded = decode_json_array(value)
            item[field] = copy.deepcopy(decoded) if isinstance(decoded, list) else coerce_string_list(decoded)
        elif field in STRING_FIELDS:
            item[field] = stringify(value).strip()
        elif field == "是否主角":
            item[field] = coerce_bool(value)
        elif field == "性别":
            item[field] = coerce_int(value, default=2, min_value=0, max_value=2)
        elif field == "年龄":
            item[field] = coerce_int(value, default=0, min_value=0)
        elif field == "重量级":
            item[field] = coerce_int(value, default=0, min_value=0, max_value=100)

    if collection_key == "角色集" and not is_iso_time(item.get("生日")):
        item["生日"] = BASE_TIME
        logs.append(f"{raw.get('名称','?')}.生日 不合规，已回填未知生日{BASE_TIME}")

    item["详情"] = detail
    if SUPPLEMENTARY_TAGS_DETAIL_KEY in detail:
        # 与压缩入口共用跨区去重规则；带空白的原始证据归档后再移除重复标签。
        tags = json.loads(detail[SUPPLEMENTARY_TAGS_DETAIL_KEY])
        retained = exclude_primary_supplementary_tags(tags, item["标签集"], detail)
        if retained != tags:
            if retained:
                detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = dump_supplementary_tags(retained)
            else:
                detail.pop(SUPPLEMENTARY_TAGS_DETAIL_KEY)
            logs.append(f"{raw.get('名称','?')}.详情.补充标签 已去除主标签副本并保全原文差异")
    if collection_key == "事件集":
        if not detail.get("涉及章节") and legacy_sources:
            detail["涉及章节"] = legacy_sources
        normalize_event_temporal_fields(item)
    elif collection_key in TRACEABLE_COLLECTION_KEYS:
        source_numbers = sorted(set(parse_chapter_numbers(legacy_sources)))
        first = normalize_chapter_value(legacy_first) or (str(source_numbers[0]).zfill(max(4, len(str(source_numbers[0])))) if source_numbers else "")
        recent = normalize_chapter_value(legacy_recent) or (str(source_numbers[-1]).zfill(max(4, len(str(source_numbers[-1])))) if source_numbers else "")
        item["详情"]["首次章节"] = first
        item["详情"]["最近章节"] = recent or first
    return item


def normalize_structure(data: Any, *, quarantine_invalid_refs: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    logs: List[str] = []
    if not isinstance(data, dict):
        logs.append("顶层不是对象，已重建为空故事结构，并把原始值放入介绍.描述")
        result = make_empty_structure()
        result["介绍"]["描述"] = stringify(data)
        return result, logs

    result = make_empty_structure()
    result["介绍"] = normalize_intro(data, logs)

    # 顶层额外字段保全到介绍.描述
    extra_top = [k for k in data.keys() if k not in TOP_LEVEL_KEYS]
    if extra_top:
        parts = [result["介绍"].get("描述", "")] if result["介绍"].get("描述") else []
        for k in extra_top:
            parts.append(f"【迁移自顶层.{k}】{stringify(data[k])}")
            logs.append(f"顶层规范外字段[{k}]已迁入介绍.描述")
        result["介绍"]["描述"] = "\n".join(parts)

    for collection_key in COLLECTION_KEYS:
        raw_items = data.get(collection_key, [])
        if not isinstance(raw_items, list):
            logs.append(f"{collection_key}不是数组，已转为空数组；原值迁入介绍.描述")
            if raw_items not in (None, "", []):
                suffix = f"\n【迁移自{collection_key}】{stringify(raw_items)}"
                result["介绍"]["描述"] = (result["介绍"].get("描述", "") + suffix).strip()
            raw_items = []
        normalized_items = []
        for raw in raw_items:
            normalized_items.append(normalize_item(raw, collection_key, logs))
        result[collection_key] = normalized_items

    # 第二轮：基于名称/别名规范化引用。
    name_sets = exact_name_sets(result)
    alias_maps = build_alias_maps(result)
    for collection_key in COLLECTION_KEYS:
        for item in result[collection_key]:
            normalize_refs(item, collection_key, name_sets, alias_maps, logs, quarantine_invalid_refs)
            normalize_detail_refs(item.get("详情", {}), name_sets, alias_maps, logs, quarantine_invalid_refs)

    # 事件数组顺序是派生不变量（"数组顺序即章节逻辑顺序"），不是创作内容：模型既不能
    # 移动既有事件、也不能改写 详情.涉及章节，而 chronological_event_order_errors 在
    # 所有阶段都是硬 error。逐章解析形成的自然顺序已合规，此处不会改动；只有在顺序
    # 确实被破坏（如治理合并并集了 详情.涉及章节）时才重排，并把变更写进日志。
    logs.extend(order_events_by_chapter(result["事件集"]))
    # 与 merge_delta 保持一致：开启重名拆分，避免产出的分组名被
    # event_group_order_errors 判为"同章节内非连续复用"而无法消除。
    assign_event_group_orders(result["事件集"], split_repeated=True)

    return result, logs


def main() -> int:
    parser = argparse.ArgumentParser(description="保全式规范化故事结构JSON")
    parser.add_argument("input_json")
    parser.add_argument("--out", default="", help="输出路径；不填且非 --in-place 时输出到 <input>.normalized.json")
    parser.add_argument("--in-place", action="store_true", help="原地覆盖输入文件")
    parser.add_argument("--backup", action="store_true", help="原地覆盖前创建时间戳备份")
    parser.add_argument("--quarantine-invalid-refs", action="store_true", help="把不存在的结构引用移入详情.待确认引用，适合最终修复前保全信息")
    parser.add_argument("--report", default="", help="变更日志输出路径")
    args = parser.parse_args()

    if not os.path.isfile(args.input_json):
        print(f"错误: 文件不存在: {args.input_json}")
        return 1
    try:
        data = load_json(args.input_json)
    except json.JSONDecodeError as exc:
        print(f"错误: JSON格式错误: {exc}")
        return 1

    normalized, logs = normalize_structure(data, quarantine_invalid_refs=args.quarantine_invalid_refs)

    if args.in_place:
        out_path = args.input_json
        if args.backup:
            # 备份统一放到 源同级/故事结构版本/备份/，避免根目录被 .bak 堆积
            _bak_dir = os.path.join(os.path.dirname(os.path.abspath(args.input_json)), "故事结构版本", "备份")
            os.makedirs(_bak_dir, exist_ok=True)
            backup = os.path.join(_bak_dir, os.path.basename(args.input_json)+f".bak_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(args.input_json, backup)
            print(f"已备份: {backup}")
    else:
        out_path = args.out or f"{args.input_json}.normalized.json"

    save_json(out_path, normalized)
    print(f"规范化完成: {out_path}")
    print(f"保全/修复记录: {len(logs)} 条")
    for line in logs[:80]:
        print(f"  - {line}")
    if len(logs) > 80:
        print(f"  ... 另有 {len(logs)-80} 条")

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            for line in logs:
                f.write(line + "\n")
        print(f"报告已写入: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
