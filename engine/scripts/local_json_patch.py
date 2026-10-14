"""Narrow field repair and evidence-backed record moves; validators stay authoritative.

The model supplies values, never arbitrary executable operations. A versioned
allowlist fences all writes so unrelated narrative bytes/values cannot drift.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from story_schema_rules import COLLECTION_KEYS
from registered_types import is_unregistered_name

PATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["base_sha256", "changes"],
    "properties": {
        "base_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "value"],
            "properties": {"path": {"type": "string"}, "value": {"type": "string", "minLength": 1}}}},
        "moves": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "target_collection", "evidence"],
            "properties": {"path": {"type": "string"},
                           "target_collection": {"type": "string", "enum": list(COLLECTION_KEYS)},
                           "target_section": {"type": "string", "enum": ["新增元素", "修改元素"]},
                           "evidence": {"type": "string", "minLength": 1}}}},
    },
}


def patch_shape_errors(value) -> list[str]:
    """Validate locally even when the provider ignores its structured schema."""
    if (not isinstance(value, dict) or not {"base_sha256", "changes"} <= set(value)
            or set(value) - {"base_sha256", "changes", "moves"}):
        return ["局部修复必须包含 base_sha256 和 changes"]
    changes, moves = value["changes"], value.get("moves", [])
    if not isinstance(changes, list) or not isinstance(moves, list) or not (changes or moves):
        return ["局部修复 changes/moves 必须是数组且至少有一项修改"]
    if any(not isinstance(item, dict) or set(item) != {"path", "value"}
           or not isinstance(item["path"], str) or not isinstance(item["value"], str)
           or not item["value"].strip() for item in changes):
        return ["局部修复每项必须提供 path 和非空字符串 value"]
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
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                prefix = f"/{section}/{collection}/{index}"
                detail = item.get("详情", {})
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
        if "allowed_values" in rule and change["value"] not in rule["allowed_values"]:
            raise ValueError("修改名称必须复用已有正式名称")
        parts = change["path"].strip("/").split("/")
        node = result
        for part in parts[:-1]:
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node.setdefault(part, {})
        node[parts[-1]] = change["value"]
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
