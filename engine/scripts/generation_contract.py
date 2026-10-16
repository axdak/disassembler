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
from chronology import normalize_chapter_value, normalize_involved_chapters, parse_chapter_numbers

from story_schema_rules import (
    COLLECTION_KEYS, DEFAULTS, DETAIL_REF_ARRAY_KEYS, DETAIL_REF_RE, TYPE_TO_COLLECTION,
    TRACEABLE_COLLECTION_KEYS, CHAPTER_TRACE_FIELDS, is_event_compound_tag,
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
        "每个新增事件的标签集必须包含主题+叙事功能+情绪+冲突四维复合主标签；禁止因精简或标签标准化迁入详情.补充标签。",
        "普通修改中的介绍、事件详情和数组按追加或并集合并，不能依赖它消除旧误。确认旧主标签、介绍、状态或事件事实有误时，周期治理必须使用治理操作.设置字段纠正：声明字段、原值、值、理由及证据数组[{章节,原文}]；原句必须可在已处理原文中找到。仅替换错误字段，旧值由程序留档；事件详情使用详情/字段逐项纠错，不整体覆盖详情，不用普通追加造成新旧矛盾并存。",
        "当前状态与累计事实分别处理：章节Delta记录身份、阵营、关系等本章变化及证据，不提交治理操作；周期治理使用有原文证据的设置字段更新当前状态，并保留旧状态及发生章节，不把已退出阵营或已结束关系盲目并入当前值。新增事实和未证实的状态仍按普通追加处理，不能根据猜测删除旧事实。纠错记录和字段归档中的旧值仅供追溯，不是当前有效事实，不能从中复活已纠正错误。",
        "标准字段写在元素顶层，不放进详情。关联角色等明确关联字段使用真正的类型:名称数组，单个引用也使用数组；禁止将数组编码成字符串。",
        "补充标签、轨迹等既有JSON字符串数组约定保持不变。未知事实按既有未知值表达，不虚构人物、地点或关系。",
        "角色、地点、线索、阵营、物品的详情/首次章节及详情/最近章节可用设置字段有据纠错，须提供操作前精确原值、理由及已处理原文证据，值用规范正数章节序号如0157；旧值由宿主归档，最终首次不得晚于最近。首次/最近不是正式顶层字段，不得省略详情/绕过纠错。事件的时间与章节追溯、治理档案记录、治理纠错记录、字段修复原文和引用修复原文等宿主归档禁止直接设置。同补丁先合并再纠错；仅合并已达成目标且原值匹配操作前快照时可幂等验收，其余原值失配仍拒绝。",
        # A genuine occurrence quote proves presence, not absence in earlier chapters.
        "首次指已处理全书中同一实体（含已确认别名及合并档案）的最早出现，不是本周期首见。拟后移首次时，先核对旧首次及更早同实体记录，并按需查对应原文；本轮真实引文不能证明此前未出现。不能确认旧值错误或历史材料不足时保持旧值，不据本轮首见后移。",
    ])
    return "\n".join(lines)


def quality_marker(path: str) -> str:
    """机器可定位的路径供字段补丁使用，不依赖解析自然语言错误。"""
    return f"[质量字段:{path}]"


def generation_scope_errors(patch: dict, *, chapter_seq: int = 0, start: int = 0, end: int = 0) -> list[str]:
    """将模型声明绑定宿主范围；格式规范不等于来源正确，禁止未来章节混入。"""
    if not isinstance(patch, dict):
        return []  # 根类型由原校验器报告。
    errors = []
    if chapter_seq:
        declared = parse_chapter_numbers(patch.get("章节"))
        if not declared or declared[0] != chapter_seq:
            errors.append(f"{quality_marker('/章节')} 必须对应宿主第{chapter_seq:04d}章，不得采用其他章节来源")
        start = end = chapter_seq
    elif start and end:
        declared = parse_chapter_numbers(patch.get("章节范围", patch.get("章节")))
        if not declared or (declared[0], declared[-1]) != (start, end):
            errors.append(f"{quality_marker('/章节范围')} 必须对应宿主范围{start:04d}-{end:04d}")
        evidence = patch.get("证据范围")
        if patch.get("治理类型") == "no_change" or evidence is not None:
            chapters = [parse_chapter_numbers(entry) for entry in evidence] if isinstance(evidence, list) else []
            if (not chapters or any(len(entry) != 1 for entry in chapters)
                    or sorted(entry[0] for entry in chapters) != list(range(start, end + 1))):
                errors.append(f"{quality_marker('/证据范围')} 必须逐章完整覆盖宿主范围{start:04d}-{end:04d}，不能以自报其他章节跳过审计")
    else:
        return []
    for section in ("新增元素", "修改元素"):
        bucket = patch.get(section, {})
        if not isinstance(bucket, dict):
            continue
        for collection, items in bucket.items():
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                detail = item.get("详情", {}) if isinstance(item, dict) else {}
                if not isinstance(detail, dict):
                    continue
                for field in ("首次章节", "最近章节", "涉及章节"):
                    if field not in detail:
                        continue
                    numbers = parse_chapter_numbers(detail[field])
                    invalid = not numbers or max(numbers) > end
                    if chapter_seq and section == "新增元素" and collection == "事件集" and field == "涉及章节":
                        invalid |= chapter_seq not in numbers
                    if invalid:
                        path = f"/{section}/{collection}/{index}/详情/{field}"
                        errors.append(f"{quality_marker(path)} 来源必须属于已处理章节，不能错用或包含第{end + 1:04d}章以后的材料")
    # setter 的来源值同样受宿主范围约束；模型扩大自报范围不能放行未来追溯。
    from information_preservation import is_trace_field_correction, set_field_operation_errors
    operations = patch.get("治理操作", {})
    setters = operations.get("设置字段", []) if isinstance(operations, dict) else []
    for index, op in enumerate(setters if isinstance(setters, list) else []):
        field = op.get("字段") if isinstance(op, dict) else None
        if isinstance(field, str) and field in CHAPTER_TRACE_FIELDS:
            path = f"/治理操作/设置字段/{index}/字段"
            errors.extend(f"{quality_marker(path)} {message}" for message in set_field_operation_errors(op))
        elif is_trace_field_correction(op):
            numbers = parse_chapter_numbers(op.get("值"))
            if not numbers or max(numbers) > end:
                path = f"/治理操作/设置字段/{index}/值"
                errors.append(f"{quality_marker(path)} 追溯纠错值必须属于已处理章节，不能指向第{end + 1:04d}章以后")
    return errors


def element_field_quality_errors(collection: str, item: dict, path: str) -> list[str]:
    """新增与终稿共用字段要求；章节追溯要求由各阶段单独处理。"""
    errors = []
    for key in required_element_fields(collection):
        value = item.get(key)
        expected = field_type(DEFAULTS[collection][key])
        message = None
        if key not in item:
            message = "档案缺少标准字段，不能用默认空值视为已完成"
        elif field_type(value) != expected or value is None:
            message = f"字段必须为{expected}"
        elif key == "介绍" and not value.strip():
            message = "档案介绍必须依据原文填写，不能留空"
        elif key == "分组" and collection == "事件集" and not value.strip():
            message = "事件必须填写有依据的分组"
        elif key == "标签集" and collection == "事件集" and not any(is_event_compound_tag(tag) for tag in value):
            message = "事件必须包含四维复合标签，不得放入详情或补充标签"
        if message:
            errors.append(f"{quality_marker(path + '/' + key)} {message}")
    return errors


def new_element_quality_errors(collection: str, item: dict, path: str) -> list[str]:
    """新增档案还需提供章节来源；终稿不重新引入已清理的过程字段。"""
    errors = element_field_quality_errors(collection, item, path)
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
    from governance_contract import governance_identity_baseline
    # 合法重命名会改写任意详情里的显式引用；先对齐，不能误报为事实丢失。
    try:
        before = governance_identity_baseline(before, patch)
    except (ValueError, TypeError, KeyError) as exc:
        return [f"治理质量 scope：无法确定身份和引用变换：{exc}"]
    errors = []
    # 已有事件也必须受保护：机器追溯与引用另有规则，普通叙述不得被局部更新抹去。
    previous_events = {item.get("名称"): item for item in before.get("事件集", []) if isinstance(item, dict)}
    current_events = {item.get("名称"): item for item in after.get("事件集", []) if isinstance(item, dict)}
    from story_schema_rules import STRICT_DETAIL_KEY_FIELDS
    from information_preservation import ARCHIVE_FIELD, CORRECTION_FIELD, field_correction
    for name, old in previous_events.items():
        new = current_events.get(name)
        if not isinstance(new, dict):
            continue  # 身份操作由治理门禁单独对齐。
        for field, value in old.get("详情", {}).items():
            if (field in STRICT_DETAIL_KEY_FIELDS or field in {"提取理由", ARCHIVE_FIELD, CORRECTION_FIELD}
                    or field.startswith("关联")
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
