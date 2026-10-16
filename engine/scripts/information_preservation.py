"""身份治理与字段纠错的可逆档案记录，不推断原文事实或裁决冲突。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from story_schema_rules import (COLLECTION_KEYS, STRUCTURAL_REF_FIELDS, TYPE_TO_COLLECTION,
                                CHAPTER_TRACE_FIELDS, TRACEABLE_COLLECTION_KEYS)

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


def is_trace_field_correction(op: dict) -> bool:
    """识别可有据纠错的非事件追溯字段；原值和引文仍须独立核验。"""
    kind = op.get("类型") if isinstance(op, dict) else None
    field = op.get("字段") if isinstance(op, dict) else None
    return (isinstance(kind, str) and TYPE_TO_COLLECTION.get(kind) in TRACEABLE_COLLECTION_KEYS
            and isinstance(field, str) and field in {"详情/" + key for key in CHAPTER_TRACE_FIELDS})


def set_field_operation_errors(op: dict, patch: dict | None = None, *, max_chapter: int = 0) -> list[str]:
    """生成校验与执行共用字段边界，不把模型档案追溯误当宿主不可变记录。"""
    from chronology import normalize_chapter_value, parse_chapter_numbers
    # 字段/引用修复模块也依赖本契约；延迟导入只复用现有宿主归档常量。
    from local_json_patch import FIELD_REPAIR_ARCHIVE, REFERENCE_REPAIR_ARCHIVE
    field = op.get("字段")
    if not isinstance(field, str) or not field:
        return ["字段必须是非空字符串"]
    if field in CHAPTER_TRACE_FIELDS:
        return ["首次/最近章节不是正式顶层字段；必须使用详情/首次章节或详情/最近章节的有据纠错协议"]
    if field == "详情":
        return ["不得整体替换详情；请使用追加详情或详情/字段纠错"]
    if op.get("类型") == "事件" and field in {
            "时间", "涉及章节", "首次章节", "最近章节", "详情/涉及章节", "详情/首次章节", "详情/最近章节"}:
        return [f"不得设置事件[{op.get('名称')}]的 {field}；时间/涉及章节/数组位置不可变"]
    leaf = field.removeprefix("详情/")
    if (field.startswith("详情/") and not leaf) or leaf in {ARCHIVE_FIELD, CORRECTION_FIELD, FIELD_REPAIR_ARCHIVE, REFERENCE_REPAIR_ARCHIVE, "涉及章节"}:
        return ["不能通过设置字段改变机器追溯或宿主归档字段"]
    if not field.startswith("详情/") or leaf not in CHAPTER_TRACE_FIELDS:
        return []
    if not is_trace_field_correction(op):
        return ["只有角色、地点、线索、阵营、物品的首次/最近章节允许有据纠错"]
    errors = []
    if not evidenced_correction(op, op.get("原值"), op.get("值")):
        errors.append("追溯纠错必须声明精确原值、非空理由和原文章节引文")
    value = op.get("值")
    valid_value = (isinstance(value, str) and value.isascii() and value.isdigit()
                   and int(value) > 0 and normalize_chapter_value(value) == value)
    if not valid_value:
        errors.append("首次/最近章节必须是规范正数章节序号，例如0157")
    if not max_chapter and isinstance(patch, dict):
        numbers = parse_chapter_numbers(patch.get("章节范围", patch.get("章节")))
        max_chapter = max(numbers, default=0)
    if max_chapter:
        if valid_value and int(value) > max_chapter:
            errors.append("追溯纠错值必须属于已处理章节，不能指向未来章节")
        for evidence in op.get("证据", []) if isinstance(op.get("证据"), list) else []:
            chapter = evidence.get("章节") if isinstance(evidence, dict) else None
            if isinstance(chapter, str) and chapter.isdecimal() and int(chapter) > max_chapter:
                errors.append("追溯纠错证据必须来自已处理章节，不能引用未来章节")
    return errors


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
        if not isinstance(op, dict):
            continue
        field = op.get("字段")
        top_level_trace = isinstance(field, str) and field in CHAPTER_TRACE_FIELDS
        if is_trace_field_correction(op) or top_level_trace:
            # 非周期提交也必须采用宿主已处理上限；顶层错层键不能借规范化绕过此门禁。
            leaf = "字段" if top_level_trace else "值"
            prefix = f"/治理操作/设置字段/{index}/{leaf}"
            errors.extend(f"{quality_marker(prefix)} {message}"
                          for message in set_field_operation_errors(op, max_chapter=max_chapter))
        if "原值" not in op:
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


def project_correction_evidence_errors(project: Path, patch: dict, max_chapter: int = 0) -> list[str]:
    """复用宿主章节定位和原文核验；已处理上限由调用方传入，不信模型自报范围。"""
    # 主控器本身也调用此入口，延迟导入仅复用现有章节解析，避免模块循环初始化。
    from run_pipeline import chapter_by_seq
    operations = patch.get("治理操作", {}) if isinstance(patch, dict) else {}
    setters = operations.get("设置字段", []) if isinstance(operations, dict) else []
    chapter_texts = {}
    for op in setters if isinstance(setters, list) else []:
        evidence = op.get("证据", []) if isinstance(op, dict) and "原值" in op else []
        for entry in evidence if isinstance(evidence, list) else []:
            chapter = entry.get("章节") if isinstance(entry, dict) else None
            if isinstance(chapter, str) and chapter.isdecimal() and int(chapter) not in chapter_texts:
                source = chapter_by_seq(project, int(chapter))
                if source is not None:
                    chapter_texts[int(chapter)] = source.read_text(encoding="utf-8-sig", errors="replace")
    return correction_evidence_errors(patch, chapter_texts, max_chapter)


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
