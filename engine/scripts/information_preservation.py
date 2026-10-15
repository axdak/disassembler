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
    """检查纠错声明的形状；提交入口另核对实际原文，模型负责判断语义。"""
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


def correction_evidence_errors(patch: dict, chapter_texts: dict[int, str], max_chapter: int = 0) -> list[str]:
    """仅核对本次纠错引用的原文章节和原句，不增加模型调用或另建审查状态。"""
    from generation_contract import quality_marker
    errors = []
    operations = patch.get("治理操作", {}) if isinstance(patch, dict) else {}
    setters = operations.get("设置字段", []) if isinstance(operations, dict) else []
    if not isinstance(setters, list):
        return errors  # 操作形状由既有校验器负责。
    for index, op in enumerate(setters):
        if not isinstance(op, dict) or "原值" not in op:
            continue
        evidence = op.get("证据", [])
        if not isinstance(evidence, list):
            continue  # 缺失纠错协议会由保护字段和详情纠错门禁拒绝。
        for number, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                continue
            prefix = f"/治理操作/设置字段/{index}/证据/{number}"
            chapter, quote = entry.get("章节"), entry.get("原文")
            seq = int(chapter) if isinstance(chapter, str) and chapter.isdecimal() else 0
            if seq <= 0 or max_chapter and seq > max_chapter or seq not in chapter_texts:
                errors.append(f"{quality_marker(prefix + '/章节')} 纠错证据必须来自已处理且存在的原文章节")
            elif (not isinstance(quote, str) or not quote.strip()
                  or ''.join(quote.split()) not in ''.join(chapter_texts[seq].split())):
                errors.append(f"{quality_marker(prefix + '/原文')} 纠错引文在所述原文中不存在；保留原字段并核对来源")
    return errors


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
