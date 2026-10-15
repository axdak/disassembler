"""生成字段、提示说明与新增档案质量门禁的共同来源。

集合骨架及类型复用 story_schema_rules.DEFAULTS。新增是完整档案，修改是
稀疏补丁；历史过程库不因本模块新增约束而被逐章强制重新生成。
治理操作的独立协议仍维护在 audit_correction.schema.json，本模块只展开
两类 Delta 共有的元素字段，避免把不同业务操作强行合并。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from chronology import normalize_chapter_value, normalize_involved_chapters

from story_schema_rules import (
    COLLECTION_KEYS, DEFAULTS, DETAIL_REF_ARRAY_KEYS, DETAIL_REF_RE, TYPE_TO_COLLECTION,
    TRACEABLE_COLLECTION_KEYS, is_event_compound_tag,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
COMPOUND_PATTERN = "^" + r"\+".join([r"[^+:：]*[^+:：\s][^+:：]*"] * 4) + "$"


def required_element_fields(collection: str) -> tuple[str, ...]:
    """章节时间由程序推导，其余标准字段由新增档案明确交付。"""
    return tuple(key for key in DEFAULTS[collection] if key != "时间")


def field_type(value: Any) -> str:
    """严格区分 bool/int，供 Schema 和局部补丁共同使用。"""
    return ("boolean" if isinstance(value, bool) else "integer" if isinstance(value, int)
            else "array" if isinstance(value, list) else "object" if isinstance(value, dict)
            else "string" if isinstance(value, str) else "null" if value is None else "number")


def detail_schema(collection: str, partial: bool) -> dict:
    """明确引用使用真正数组；普通档案及轨迹字符串保持既有协议。"""
    properties = {"提取理由": {"type": "string", "minLength": 1, "pattern": r"\S"}}
    for key in sorted(DETAIL_REF_ARRAY_KEYS):
        kind = key.removeprefix("关联")
        prefix = "(?:物品|道具)" if kind == "物品" else kind
        properties[key] = {"type": "array", "items": {"type": "string", "pattern": f"^{prefix}:.+"}}
    for key in ("首次章节", "最近章节", "涉及章节"):
        properties[key] = {"type": "string", "pattern": r"^\d{4,}(?:，\d{4,})*$" if key == "涉及章节" else r"^\d{4,}$"}
    required = ["提取理由"]
    if not partial:
        if collection == "事件集":
            required.append("涉及章节")
        elif collection in TRACEABLE_COLLECTION_KEYS:
            required.extend(["首次章节", "最近章节"])
    return {"type": "object", "required": required, "properties": properties,
            "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "array", "items": {
                "type": "string", "pattern": "^(" + "|".join(TYPE_TO_COLLECTION) + "):.+"}}]}}


def element_schema(collection: str, *, partial: bool) -> dict:
    """每类元素展示完整字段；修改只要求身份和本次更新依据。"""
    properties = {}
    for key, default in DEFAULTS[collection].items():
        entry = {"type": field_type(default)}
        if isinstance(default, list):
            entry["items"] = {"type": "string"}
        if key == "名称" or (not partial and (key == "介绍" or key == "分组" and collection == "事件集")):
            entry.update(minLength=1, pattern=r"\S")
        if key == "详情":
            entry = detail_schema(collection, partial)
        if not partial and collection == "事件集" and key == "标签集":
            entry.update(minItems=1, contains={"type": "string", "pattern": COMPOUND_PATTERN})
        properties[key] = entry
    return {"type": "object", "properties": properties,
            "required": ["名称", "详情"] if partial else list(required_element_fields(collection)),
            "additionalProperties": False}


def build_generation_schema(kind: str = "delta") -> dict:
    """从同一字段契约展开可直接交付给 provider 的原生 Schema。

    文件保留各阶段独有的顶层和治理操作协议；只覆盖共享元素部分。
    因此反复生成是幂等的，回归测试同时检查落盘快照与展开结果一致。
    """
    name = "delta.schema.json" if kind == "delta" else "audit_correction.schema.json"
    schema = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    # 四种详情复用本地引用，完整展示字段同时控制原生工具的输入和命令行体积。
    schema["definitions"] = {
        "detail_patch": detail_schema("事件集", True),
        "detail_event": detail_schema("事件集", False),
        "detail_trace": detail_schema("角色集", False),
        "detail_other": detail_schema("其他事项集", False),
    }
    for section in ("新增元素", "修改元素"):
        bucket = schema["properties"][section]
        bucket["properties"] = {}
        for key in COLLECTION_KEYS:
            partial = section == "修改元素"
            element = element_schema(key, partial=partial)
            detail = ("detail_patch" if partial else "detail_event" if key == "事件集"
                      else "detail_trace" if key in TRACEABLE_COLLECTION_KEYS else "detail_other")
            element["properties"]["详情"] = {"$ref": "#/definitions/" + detail}
            bucket["properties"][key] = {"type": "array", "items": element}
        bucket["additionalProperties"] = False
    schema["$comment"] = "元素字段由 scripts/generation_contract.py 根据正式骨架展开；新增完整，修改局部。"
    return schema


def generation_contract_text() -> str:
    """嵌入章节与周期任务包的同一说明，防止治理反向破坏生成要求。"""
    lines = ["新增元素必须交付完整标准字段；修改元素只提交发生变化的字段并保留详情.提取理由。"]
    for collection in COLLECTION_KEYS:
        lines.append(f"新增{collection}必填：{'、'.join(required_element_fields(collection))}。")
    lines.extend([
        "事件分组和介绍必须有原文依据且非空；参与成员使用真实字符串数组，只有确无人物参与才可为空。",
        "每个新增事件的标签集必须包含主题+叙事功能+情绪+冲突四维复合主标签；治理必须保留，禁止降级到详情.补充标签。",
        "标准字段写在元素顶层，不放进详情。关联角色等明确关联字段使用真正的类型:名称数组，单个引用也使用数组；禁止将数组编码成字符串。",
        "补充标签、轨迹等既有JSON字符串数组约定保持不变。未知事实按既有未知值表达，不虚构人物、地点或关系。",
    ])
    return "\n".join(lines)


def quality_marker(path: str) -> str:
    """机器可定位的路径供字段补丁使用，不依赖解析自然语言错误。"""
    return f"[质量字段:{path}]"


def new_element_quality_errors(collection: str, item: dict, path: str) -> list[str]:
    """只检查本次真实新增的档案，不追溯强制修复所有历史空值。"""
    errors = []
    for key in required_element_fields(collection):
        value = item.get(key)
        expected = field_type(DEFAULTS[collection][key])
        message = None
        if key not in item:
            message = "新增档案缺少标准字段，不能用默认空值视为已完成"
        elif field_type(value) != expected or value is None:
            message = f"字段必须为{expected}"
        elif key == "介绍" and not value.strip():
            message = "新增档案介绍必须依据原文填写，不能留空"
        elif key == "分组" and collection == "事件集" and not value.strip():
            message = "新增事件必须填写有依据的分组"
        elif key == "标签集" and collection == "事件集" and not any(is_event_compound_tag(tag) for tag in value):
            message = "新增事件必须包含四维复合标签，不得放入详情或补充标签"
        if message:
            errors.append(f"{quality_marker(path + '/' + key)} {message}")
    detail = item.get("详情")
    if isinstance(detail, dict):
        required = (["涉及章节"] if collection == "事件集" else ["首次章节", "最近章节"]
                    if collection in TRACEABLE_COLLECTION_KEYS else [])
        for key in required:
            value = detail.get(key)
            normalize = normalize_involved_chapters if key == "涉及章节" else normalize_chapter_value
            if not isinstance(value, str) or not value or normalize(value) != value:
                errors.append(f"{quality_marker(path + '/详情/' + key)} 新增档案必须提供规范章节来源，不得缺失或编造")
    return errors


def misplaced_field_errors(collection: str, item: dict) -> list[str]:
    """确定性归位后仍残留的同名标准字段必须显式处理冲突。"""
    detail = item.get("详情")
    if not isinstance(detail, dict):
        return []
    return [f"详情.{key} 是顶层标准字段；请按原值归位，冲突时核对原文，禁止覆盖或删除有效内容"
            for key in detail if key in DEFAULTS[collection] and key not in {"名称", "详情"}]


def reference_quality_errors(item: dict, path: str) -> list[str]:
    """为可定位的关联类型错误提供字段补丁路径，不开放整个详情对象。"""
    detail = item.get("详情")
    if not isinstance(detail, dict):
        return []
    errors = []
    for key in DETAIL_REF_ARRAY_KEYS & detail.keys():
        values = detail[key]
        expected = TYPE_TO_COLLECTION[key.removeprefix("关联")]
        invalid = not isinstance(values, list)
        if not invalid:
            for value in values:
                match = DETAIL_REF_RE.fullmatch(value.strip()) if isinstance(value, str) else None
                invalid |= not match or TYPE_TO_COLLECTION[match[1]] != expected
        if invalid:
            errors.append(f"{quality_marker(path + '/详情/' + key)} {key}必须为匹配类型的真正引用数组；仅修此字段，保留原引用证据")
    return errors


def chapter_quality_errors(before: dict, after: dict, patch: dict) -> list[str]:
    """在合并及规范化之后复验真正的新档案，保护正式发布边界。"""
    from element_identity import build_lookup_index, find_index
    errors = []
    # 已有事件也必须受保护：机器追溯与引用另有规则，普通叙述不得被局部更新抹去。
    previous_events = {item.get("名称"): item for item in before.get("事件集", []) if isinstance(item, dict)}
    current_events = {item.get("名称"): item for item in after.get("事件集", []) if isinstance(item, dict)}
    from story_schema_rules import STRICT_DETAIL_KEY_FIELDS
    from information_preservation import field_correction
    for name, old in previous_events.items():
        new = current_events.get(name)
        if not isinstance(new, dict):
            continue  # 身份操作由治理门禁单独对齐。
        for field, value in old.get("详情", {}).items():
            if (field in STRICT_DETAIL_KEY_FIELDS or field == "提取理由" or field.startswith("关联")
                    or not isinstance(value, str) or not value.strip()):
                continue
            proposed = new.get("详情", {}).get(field)
            def entries(text):
                try:
                    array = json.loads(text)
                    return array if isinstance(array, list) and all(isinstance(x, str) for x in array) else [text]
                except (ValueError, TypeError):
                    return [text]
            if not all(entry in entries(proposed) or isinstance(proposed, str) and entry in proposed for entry in entries(value)):
                if not field_correction(patch, "事件", name, "详情/" + field, value, proposed):
                    errors.append(f"事件集[{name}].详情.{field} 原有事实被覆盖；应追加或通过有来源的治理纠错")
    additions = patch.get("新增元素", {})
    for collection in COLLECTION_KEYS:
        previous = build_lookup_index(before.get(collection) or [])
        records = after.get(collection) or []
        current = build_lookup_index(records)
        for index, item in enumerate(additions.get(collection, []) or []):
            if not isinstance(item, dict) or not isinstance(item.get("名称"), str):
                continue  # 形状错误由补丁校验器负责，本门禁不以异常掩盖它。
            path = f"/新增元素/{collection}/{index}"
            try:
                if find_index(*previous, item["名称"]) >= 0:
                    continue
                target = find_index(*current, item["名称"])
                if target < 0:
                    errors.append(f"{quality_marker(path)} 新增档案在合并候选中缺失")
                    continue
                errors.extend(new_element_quality_errors(collection, records[target], path))
                errors.extend(misplaced_field_errors(collection, records[target]))
            except ValueError as exc:
                errors.append(f"{quality_marker(path)} 候选身份歧义：{exc}")
    return errors
