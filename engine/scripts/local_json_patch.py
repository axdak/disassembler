"""Narrow repair of diagnosed Delta fields; full validators remain authoritative.

The model supplies values, never arbitrary executable operations. A versioned
allowlist fences all writes so unrelated narrative bytes/values cannot drift.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from story_schema_rules import COLLECTION_KEYS

PATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["base_sha256", "changes"],
    "properties": {
        "base_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "changes": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "value"],
            "properties": {"path": {"type": "string"}, "value": {"type": "string", "minLength": 1}}}},
    },
}


def patch_shape_errors(value) -> list[str]:
    """Validate locally even when the provider ignores its structured schema."""
    if not isinstance(value, dict) or set(value) != {"base_sha256", "changes"}:
        return ["局部修复必须包含 base_sha256 和 changes"]
    changes = value["changes"]
    if not isinstance(changes, list) or not changes:
        return ["局部修复 changes 必须是非空数组"]
    if any(not isinstance(item, dict) or set(item) != {"path", "value"}
           or not isinstance(item["path"], str) or not isinstance(item["value"], str)
           or not item["value"].strip() for item in changes):
        return ["局部修复每项必须提供 path 和非空字符串 value"]
    return []


def delta_repair_scope(path: Path, feedback: str, story: dict) -> dict:
    """Select only missing reasons and unknown modified identities, never guess values."""
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
                if (section == "修改元素" and known and isinstance(name, str) and name not in known
                        and f"修改目标[{name}]不存在" in feedback):
                    scope[prefix + "/名称"] = {"name": name, "kind": "identity",
                                             "allowed_values": sorted(n for n in known if isinstance(n, str))}
    return scope


def apply_scoped_patch(reference: Path, candidate: Path, patch: dict, scope: dict) -> None:
    """Apply a complete allowlisted patch atomically, rejecting stale/extra edits."""
    errors = patch_shape_errors(patch)
    raw = reference.read_bytes()
    if errors or patch.get("base_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("局部修复协议不合格或原始版本已变化")
    paths = [item["path"] for item in patch["changes"]]
    if len(set(paths)) != len(paths) or set(paths) != set(scope):
        raise ValueError("局部修复必须且只能覆盖本次指定的字段，禁止重复修改")
    result = copy.deepcopy(json.loads(raw))
    for change in patch["changes"]:
        rule = scope[change["path"]]
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
    from worker_checkpoint import atomic_write_bytes
    atomic_write_bytes(candidate, json.dumps(result, ensure_ascii=False, indent=2).encode())
