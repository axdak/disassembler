"""身份治理与字段纠错的可逆档案记录，不推断原文事实或裁决冲突。"""
from __future__ import annotations

import copy
import json

from story_schema_rules import COLLECTION_KEYS, STRUCTURAL_REF_FIELDS, TYPE_TO_COLLECTION

ARCHIVE_FIELD = "治理档案记录"
CORRECTION_FIELD = "治理纠错记录"


def append_record(detail: dict, field: str, record: dict) -> None:
    """在既有字符串数组协议内逐条记录；重复恢复不会重复追加。"""
    original = detail.get(field, "[]")
    try:
        entries = json.loads(original) if isinstance(original, str) else copy.deepcopy(original)
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            entries = [json.dumps(original, ensure_ascii=False)]
    except (ValueError, TypeError):
        entries = [str(original)]
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if text not in entries:
        entries.append(text)
    detail[field] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))


def evidenced_correction(op: dict, old, new) -> bool:
    """只认可显式原值及可定位引文的纠错声明；引文真实性由语义验收核对。"""
    if (not isinstance(op, dict) or "原值" not in op or op["原值"] != old or op.get("值") != new
            or not isinstance(op.get("理由"), str) or not op["理由"].strip()):
        return False
    evidence = op.get("证据")
    return (isinstance(evidence, list) and bool(evidence)
            and all(isinstance(entry, dict) and isinstance(entry.get("章节"), str)
                    and entry["章节"].isdigit() and int(entry["章节"]) > 0
                    and isinstance(entry.get("原文"), str) and bool(entry["原文"].strip()) for entry in evidence))


def field_correction(patch: dict, kind: str, name: str, field: str, old, new) -> bool:
    """候选字段必须与同一元素、同一旧值和新值的唯一纠错声明一致。"""
    operations = patch.get("治理操作", {}).get("设置字段", [])
    matches = [op for op in operations if isinstance(op, dict)
               and (op.get("类型"), op.get("名称"), op.get("字段")) == (kind, name, field)]
    return len(matches) == 1 and evidenced_correction(matches[0], old, new)


def reference_context(data: dict, kind: str, names: set[str]) -> list[dict]:
    """降级移除引用前保全引用所在字段，避免关联语境从正式档案消失。"""
    target = TYPE_TO_COLLECTION[kind]
    records = []

    def contains(value, *, relation=False, typed=False):
        if isinstance(value, list):
            return any(contains(item, relation=relation, typed=typed) for item in value)
        if not isinstance(value, str):
            return False
        prefix, separator, name = value.partition(":")
        return (bool(separator) and name in names and (relation or TYPE_TO_COLLECTION.get(prefix) == target)) if relation or typed else value in names

    for collection in COLLECTION_KEYS:
        for item in data.get(collection, []) or []:
            for field, field_target, shape in STRUCTURAL_REF_FIELDS[collection]:
                if field_target == target and contains(item.get(field), relation=shape == "relation_list"):
                    records.append({"类型": collection, "名称": item["名称"], "字段": field, "原值": copy.deepcopy(item[field])})
            detail = item.get("详情", {})
            if isinstance(detail, dict):
                for field, value in detail.items():
                    if contains(value, typed=True):
                        records.append({"类型": collection, "名称": item["名称"], "字段": "详情/" + field, "原值": copy.deepcopy(value)})
    return records
