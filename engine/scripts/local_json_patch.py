"""Narrow field repair and evidence-backed record moves; validators stay authoritative.

The model supplies values, never arbitrary executable operations. A versioned
allowlist fences all writes so unrelated narrative bytes/values cannot drift.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from story_schema_rules import COLLECTION_KEYS, DEFAULTS, DETAIL_REF_ARRAY_KEYS, DETAIL_REF_RE, TYPE_TO_COLLECTION
from registered_types import is_unregistered_name
from generation_contract import field_type, quality_marker

PATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["base_sha256", "changes"],
    "properties": {
        "base_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "value"],
            "properties": {"path": {"type": "string"}, "value": {
                "type": ["string", "array", "integer", "boolean"],
                "items": {"type": "string"}}}}},
        "moves": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "target_collection", "evidence"],
            "properties": {"path": {"type": "string"},
                           "target_collection": {"type": "string", "enum": list(COLLECTION_KEYS)},
                           "target_section": {"type": "string", "enum": ["新增元素", "修改元素"]},
                           "evidence": {"type": "string", "minLength": 1}}}},
    },
}

REFERENCE_REPAIR_ARCHIVE = "引用修复原文"

# 复用现有字段补丁协议，限制单次交付体积；成功一批后外层仍须完整校验。
FINAL_REPAIR_BATCH_FIELDS = 32
FIELD_REPAIR_ARCHIVE = "字段修复原文"


def _archive_field(detail: dict, field: str, value) -> None:
    """宿主记录精确旧值；不能让模型以自由文字声称已经保全。"""
    raw = detail.get(FIELD_REPAIR_ARCHIVE, "[]")
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            entries = [raw]
    except (ValueError, TypeError):
        entries = [str(raw)]
    entry = json.dumps({"字段": field, "原值": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if entry not in entries:
        entries.append(entry)
    detail[FIELD_REPAIR_ARCHIVE] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))


def archived_field_preserved(before: dict, after: dict, field: str) -> bool:
    """只接受同一元素/详情内的精确字段档案，不允许异处同文抵消丢失。"""
    detail = after.get("详情", after)
    if not isinstance(detail, dict):
        return False
    try:
        entries = json.loads(detail.get(FIELD_REPAIR_ARCHIVE, "[]"))
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            return False
        if field == FIELD_REPAIR_ARCHIVE:
            old_entries = json.loads(before[field])
            return isinstance(old_entries, list) and all(entry in entries for entry in old_entries)
        permitted = set().union(*(set(defaults) for defaults in DEFAULTS.values())) | DETAIL_REF_ARRAY_KEYS
        if (field not in permitted - {"名称", "详情", "时间"} and not field.startswith("关联")
                and not isinstance(before.get(field), list)):
            return False
        for entry in entries:
            try:
                if json.loads(entry) == {"字段": field, "原值": before[field]}:
                    return True
            except ValueError:
                continue
        return False
    except (ValueError, TypeError, KeyError):
        return False


def final_repair_scope(path: Path, feedback: str, *, all_fields: bool = False) -> dict:
    """终稿只开放真实报错的标准字段，不允许整条或整份故事改写。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    scope = {}
    intro = data.get("介绍")
    if isinstance(intro, dict):
        for field in ("标题", "描述"):
            pointer = "/介绍/" + field
            if quality_marker(pointer) in feedback:
                scope[pointer] = {"kind": "quality", "value_type": "string", "collection": "",
                                  "field": field, "metadata": True, "final": True}
    for collection in COLLECTION_KEYS:
        records = data.get(collection)
        if not isinstance(records, list):
            continue
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                continue
            detail = item.get("详情", {})
            fields = [(field, field_type(default)) for field, default in DEFAULTS[collection].items()
                      if field not in {"名称", "详情", "时间"}]
            if isinstance(detail, dict):
                fields.extend(("详情/" + field, "array") for field in sorted(detail) if field.startswith("关联") or isinstance(detail[field], list))
            for field, value_type in fields:
                pointer = f"/{collection}/{index}/{field}"
                if quality_marker(pointer) not in feedback:
                    continue
                scope[pointer] = {"kind": "quality", "value_type": value_type, "collection": collection,
                                  "field": field.split("/")[-1], "name": item.get("名称"), "final": True}
    return scope if all_fields else dict(list(scope.items())[:FINAL_REPAIR_BATCH_FIELDS])


def final_repair_progress(before: Path, after: Path) -> bool:
    """仅承认本批全部字段修好且没有新增错误，不能把部分进度当成最终通过。"""
    from story_schema_rules import validate_exact_story_schema
    try:
        old = json.loads(before.read_text(encoding="utf-8"))
        new = json.loads(after.read_text(encoding="utf-8"))
        old_errors = validate_exact_story_schema(old, mode="final")[0]
        new_errors = validate_exact_story_schema(new, mode="final")[0]
    except (OSError, ValueError, TypeError, KeyError):
        return False
    scope = final_repair_scope(before, "\n".join(old_errors))
    if not scope:
        return False
    # 未选中的错误须逐字保持；新引用/跨字段错误也会被此集合比较拒绝。
    remaining = {error for error in old_errors if not any(quality_marker(path) in error for path in scope)}
    return (len(new_errors) < len(old_errors) and set(new_errors) <= remaining
            and not any(quality_marker(path) in error for path in scope for error in new_errors))


def _reference_values(value):
    """只为证据比较解开关联值的一层数组编码，不解释普通档案。"""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded if isinstance(decoded, list) else [value]
        except ValueError:
            value = [value]
    return value if isinstance(value, list) else []


def _correct_reference_values(field, value):
    target = TYPE_TO_COLLECTION[field.removeprefix("关联")]
    return [entry for entry in _reference_values(value) if isinstance(entry, str)
            and (match := DETAIL_REF_RE.fullmatch(entry.strip())) and TYPE_TO_COLLECTION[match[1]] == target]


def archived_reference_preserved(before: dict, after: dict, field: str) -> bool:
    """仅接受同一详情内逐值归档的错误引用，不允许档案别处同文替代证据。"""
    if field == REFERENCE_REPAIR_ARCHIVE:
        try:
            old_entries, new_entries = json.loads(before[field]), json.loads(after.get(field, "[]"))
            return (isinstance(old_entries, list) and isinstance(new_entries, list)
                    and all(isinstance(entry, str) and entry in new_entries for entry in old_entries))
        except (ValueError, TypeError):
            return False
    if field not in DETAIL_REF_ARRAY_KEYS or field not in after or not isinstance(after[field], list):
        return False
    if len(_correct_reference_values(field, after[field])) != len(after[field]):
        return False
    if any(value not in after[field] for value in _correct_reference_values(field, before[field])):
        return False
    try:
        entries = json.loads(after.get(REFERENCE_REPAIR_ARCHIVE, "[]"))
        if not isinstance(entries, list):
            return False
        for entry in entries:
            try:
                if isinstance(entry, str) and json.loads(entry) == {"字段": field, "原值": before[field]}:
                    return True
            except ValueError:
                continue  # 原有非JSON证据也保留，不影响后续合法条目的逐值匹配。
        return False
    except (ValueError, TypeError):
        return False


def patch_shape_errors(value) -> list[str]:
    """Validate locally even when the provider ignores its structured schema."""
    if (not isinstance(value, dict) or not {"base_sha256", "changes"} <= set(value)
            or set(value) - {"base_sha256", "changes", "moves"}):
        return ["局部修复必须包含 base_sha256 和 changes"]
    changes, moves = value["changes"], value.get("moves", [])
    if not isinstance(changes, list) or not isinstance(moves, list) or not (changes or moves):
        return ["局部修复 changes/moves 必须是数组且至少有一项修改"]
    if any(not isinstance(item, dict) or set(item) != {"path", "value"}
           or not isinstance(item["path"], str)
           or not (isinstance(item["value"], str)
                   or isinstance(item["value"], (int, bool))
                   or isinstance(item["value"], list) and all(isinstance(value, str) for value in item["value"]))
           for item in changes):
        return ["局部修复每项必须提供 path 和声明类型的字符串、字符串数组、整数或布尔值"]
    if any(not isinstance(item, dict) or not {"path", "target_collection", "evidence"} <= set(item)
           or set(item) - {"path", "target_collection", "target_section", "evidence"}
           or not isinstance(item["path"], str) or item["target_collection"] not in COLLECTION_KEYS
           or item.get("target_section", "修改元素") not in {"新增元素", "修改元素"}
           or not isinstance(item["evidence"], str) or not item["evidence"].strip() for item in moves):
        return ["迁移必须提供指定路径、目标集合和非空原文依据，禁止携带替换条目"]
    return []


def delta_repair_scope(path: Path, feedback: str, story: dict) -> dict:
    """选择报错身份；登记类别迁移或未登记新档案的显式证据决策，不猜测。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    scope = {}
    # 来源声明也复用既有字段补丁；其正确性仍由宿主范围校验决定。
    # 更正错误章节列表不属于删除故事事实，不套用内容数组的只增保护。
    for field, value_type in (("章节", "string"), ("章节范围", "string"), ("证据范围", "array")):
        if quality_marker("/" + field) in feedback:
            scope["/" + field] = {"kind": "quality", "value_type": value_type,
                                  "collection": "", "field": field, "metadata": True}
    operations = data.get("治理操作", {})
    setters = operations.get("设置字段", []) if isinstance(operations, dict) else []
    for index, op in enumerate(setters if isinstance(setters, list) else []):
        evidence = op.get("证据", []) if isinstance(op, dict) else []
        for number, entry in enumerate(evidence if isinstance(evidence, list) else []):
            if not isinstance(entry, dict):
                continue
            for field in ("章节", "原文"):
                pointer = f"/治理操作/设置字段/{index}/证据/{number}/{field}"
                if quality_marker(pointer) in feedback:
                    scope[pointer] = {"kind": "quality", "value_type": "string",
                                      "collection": "", "field": field, "metadata": True}
    from element_identity import same_patch_rename_targets
    try:
        renamed_targets = same_patch_rename_targets(story, data)
    except ValueError:
        return {}  # A conflicting declared rename must never authorize promotion.
    for section in ("新增元素", "修改元素"):
        bucket = data.get(section)
        if not isinstance(bucket, dict):
            continue
        for collection, items in bucket.items():
            if collection not in COLLECTION_KEYS or not isinstance(items, list):
                continue
            historical = story.get(collection, [])
            known = {item["名称"] for item in historical if isinstance(item, dict)
                     and isinstance(item.get("名称"), str)} if isinstance(historical, list) else set()
            # A validated future name belongs to an existing record; even stale
            # missing-target feedback must not offer a move into 新增元素.
            known.update(renamed_targets.get(collection, {}))
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                prefix = f"/{section}/{collection}/{index}"
                detail = item.get("详情", {})
                # 仅采纳校验器实际标记的字段；不能凭一个泛化错误开放整条档案写入。
                from story_schema_rules import DEFAULTS
                for field, default in DEFAULTS[collection].items():
                    path = prefix + "/" + field
                    if field in {"名称", "详情", "时间"} or quality_marker(path) not in feedback:
                        continue
                    scope[path] = {"name": item.get("名称"), "kind": "quality", "value_type": field_type(default),
                                   "collection": collection, "field": field}
                for field in ("涉及章节", "首次章节", "最近章节"):
                    path = prefix + "/详情/" + field
                    if quality_marker(path) in feedback:
                        scope[path] = {"name": item.get("名称"), "kind": "quality", "value_type": "string",
                                       "collection": collection, "field": field}
                for field in DETAIL_REF_ARRAY_KEYS:
                    path = prefix + "/详情/" + field
                    if quality_marker(path) in feedback:
                        scope[path] = {"name": item.get("名称"), "kind": "quality", "value_type": "array",
                                       "collection": collection, "field": field, "reference": True}
                if isinstance(detail, dict) and (detail.get("提取理由") is None or
                        isinstance(detail.get("提取理由"), str) and not detail["提取理由"].strip()):
                    scope[prefix + "/详情/提取理由"] = {"name": item.get("名称"), "kind": "evidence"}
                name = item.get("名称")
                # The original validator must identify this exact unknown target.
                if (section == "修改元素" and isinstance(name, str) and name not in known
                        and f"修改目标[{name}]不存在" in feedback):
                    # 同名跨类别不能改成相似人物名。模型只选择已登记类别，
                    # 宿主搬移整条原值；目的已有补丁或身份不唯一时拒绝覆盖。
                    counts = {other: sum(
                        isinstance(record, dict) and record.get("名称") == name
                        for record in story[other]) for other in COLLECTION_KEYS if other != collection
                        and isinstance(story.get(other), list)}
                    if any(count > 1 for count in counts.values()):
                        return {}
                    targets = [other for other, count in counts.items() if count == 1]
                    if targets:
                        matches = sum(isinstance(record, dict) and record.get("名称") == name
                                      for records in bucket.values() if isinstance(records, list)
                                      for record in records)
                        if matches != 1 or any(other in bucket and not isinstance(bucket[other], list) for other in targets):
                            return {}
                        scope[prefix] = {"name": name, "kind": "move", "allowed_collections": targets}
                        continue
                    # A genuine new fact may have been put in 修改元素. Offer
                    # promotion alongside renaming; only model evidence review
                    # can choose it. Never infer newness from search misses alone.
                    proposals = [record for sec in ("新增元素", "修改元素")
                                 if isinstance(data.get(sec), dict)
                                 for records in data[sec].values() if isinstance(records, list)
                                 for record in records if isinstance(record, dict) and record.get("名称") == name]
                    can_add = (is_unregistered_name(story, name) and len(proposals) == 1
                               and isinstance(data.get("新增元素", {}), dict)
                               and isinstance(data.get("新增元素", {}).get(collection, []), list))
                    promotion = {"path": prefix, "target_section": "新增元素", "target_collection": collection}
                    if known:
                        scope[prefix + "/名称"] = {"name": name, "kind": "identity",
                                                 "allowed_values": sorted(known)}
                        if can_add:
                            scope[prefix + "/名称"]["promotion"] = promotion
                    elif can_add:
                        scope[prefix] = {"name": name, "kind": "move", "allowed_collections": [collection],
                                         "target_section": "新增元素"}
    return scope


def apply_scoped_patch(reference: Path, candidate: Path, patch: dict, scope: dict) -> None:
    """Apply a complete allowlisted patch atomically, rejecting stale/extra edits."""
    errors = patch_shape_errors(patch)
    raw = reference.read_bytes()
    if errors or patch.get("base_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("局部修复协议不合格或原始版本已变化")
    def move_scope(move):
        field = move["path"] + "/名称"
        if field in scope and scope[field].get("promotion", {}).get("path") == move["path"]:
            return field
        return move["path"]
    paths = [item["path"] for item in patch["changes"]] + [move_scope(item) for item in patch.get("moves", [])]
    if len(set(paths)) != len(paths) or set(paths) != set(scope):
        raise ValueError("局部修复必须且只能覆盖本次指定的字段，禁止重复修改")
    result = copy.deepcopy(json.loads(raw))
    for change in patch["changes"]:
        rule = scope[change["path"]]
        if rule["kind"] == "move":
            raise ValueError("迁移不能使用字符串替换")
        value = change["value"]
        if rule["kind"] == "quality":
            if field_type(value) != rule["value_type"]:
                raise ValueError("局部质量字段类型不符合本次声明契约")
            if (rule["field"] == "介绍" or rule["field"] == "分组" and rule["collection"] == "事件集") and not value.strip():
                raise ValueError("事件分组和档案介绍必须依据原文补齐，不能提交空值")
            if rule["field"] == "标签集" and rule["collection"] == "事件集":
                from story_schema_rules import is_event_compound_tag
                if not any(is_event_compound_tag(tag) for tag in value):
                    raise ValueError("局部事件标签必须包含四维复合主标签")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("身份和提取理由修复必须是非空字符串")
        if "allowed_values" in rule and change["value"] not in rule["allowed_values"]:
            raise ValueError("修改名称必须复用已有正式名称")
        parts = change["path"].strip("/").split("/")
        node = result
        for part in parts[:-1]:
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node.setdefault(part, {})
        previous = node.get(parts[-1]) if isinstance(node, dict) else node[int(parts[-1])]
        if rule.get("final") and not rule.get("metadata"):
            # 只有宿主定位的终稿报错字段允许纠正旧值；其余普通补丁仍执行原保全规则。
            record = result[parts[0]][int(parts[1])]
            detail = record.setdefault("详情", {})
            if not isinstance(detail, dict):
                raise ValueError("原详情异常，禁止覆盖")
            if previous not in (None, "", []):
                _archive_field(detail, rule["field"], previous)
            if len(parts) == 3 and rule["field"] in detail:
                _archive_field(detail, rule["field"], detail.pop(rule["field"]))
        elif rule.get("reference"):
            if (len(_correct_reference_values(rule["field"], value)) != len(value)
                    or any(entry not in value for entry in _correct_reference_values(rule["field"], previous))):
                raise ValueError("关联修复必须匹配引用类型且保留已有正确类型引用")
            # 宿主固定生成逐字段原值档案，模型不能用自由文字假称已保全。
            archived = node.get(REFERENCE_REPAIR_ARCHIVE, "[]")
            try:
                entries = json.loads(archived)
                if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
                    entries = [archived]
            except (ValueError, TypeError):
                entries = [str(archived)]
            entry = json.dumps({"字段": rule["field"], "原值": previous}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if entry not in entries:
                entries.append(entry)
            node[REFERENCE_REPAIR_ARCHIVE] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        elif (rule["kind"] == "quality" and not rule.get("metadata")
              and isinstance(previous, list) and isinstance(value, list)):
            if any(entry not in value for entry in previous):
                raise ValueError("局部质量补齐不得删除已有列表内容")
        node[parts[-1]] = value
    # 先完成原索引上的字段修复，再按对象身份搬移，避免多次pop导致索引漂移。
    selected = []
    for move in patch.get("moves", []):
        rule = scope[move_scope(move)]
        promotion = rule.get("promotion")
        target_section = move.get("target_section", "修改元素")
        if promotion:
            permitted = (target_section == promotion["target_section"] and move["target_collection"] == promotion["target_collection"])
        else:
            permitted = (rule["kind"] == "move" and move["target_collection"] in rule["allowed_collections"]
                         and target_section == rule.get("target_section", "修改元素"))
        if not permitted:
            raise ValueError("迁移目标不在本次已登记类别范围")
        _, section, collection, index = move["path"].split("/")
        item = result[section][collection][int(index)]
        target = result.setdefault(target_section, {}).setdefault(move["target_collection"], [])
        if not isinstance(target, list) or any(record.get("名称") == item["名称"] for record in target if isinstance(record, dict)):
            raise ValueError("迁移目的已有同名补丁，禁止覆盖或隐式合并")
        selected.append((result[section][collection], item, target, move["evidence"]))
    for source, item, target, evidence in selected:
        source.remove(item)
        detail = item.setdefault("详情", {})
        if not isinstance(detail, dict):
            raise ValueError("原详情类型异常，禁止覆盖")
        reason = detail.get("提取理由", "")
        if not isinstance(reason, str):
            raise ValueError("原提取理由类型异常，禁止覆盖")
        detail["提取理由"] = reason + "\n归类依据：" + evidence
        target.append(item)
    from worker_checkpoint import atomic_write_bytes
    atomic_write_bytes(candidate, json.dumps(result, ensure_ascii=False, indent=2).encode())
