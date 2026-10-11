"""Lossless shape corrections for legacy governance output.

Decode containers only where the existing contract requires containers. Do
not parse narrative strings, invent evidence, resolve near names or drop data.
"""
from __future__ import annotations

import copy
import json
import re

from story_schema_rules import COLLECTION_KEYS


def periodic_introduction_errors(story, patch):
    """Reject missing/invalid synopses without inventing narrative in program repair.

    An omitted field retains the reviewed existing value; an explicit blank or
    wrong type is an invalid update. This does not tighten chapter Delta rules.
    """
    previous = story.get("介绍", {}) if isinstance(story, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    proposed = patch.get("介绍", {}) if isinstance(patch, dict) else {}
    if not isinstance(proposed, dict):
        return ["周期治理的介绍必须为对象，包含有依据的标题和完整故事描述"]
    errors = []
    for key in ("标题", "描述"):
        value = proposed.get(key, previous.get(key))
        if not isinstance(value, str) or not value.strip():
            errors.append(f"周期治理后介绍.{key}必须为非空字符串；依据已处理原文生成完整简介，不得用占位文字或分析正文填充")
    return errors


def normalize_governance_patch(patch, chapter_range: str = ""):
    """Return a corrected copy plus an auditable list of mechanical changes."""
    result = copy.deepcopy(patch)
    changes = []
    if not isinstance(result, dict):
        return result, changes

    def decode(parent, key, kind, label):
        value = parent.get(key)
        if not isinstance(value, str):
            return
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(decoded, kind):
            parent[key] = decoded
            changes.append(f"{label}：还原为{kind.__name__}")

    for key in ("介绍", "新增元素", "修改元素", "治理操作"):
        decode(result, key, dict, key)
    decode(result, "证据范围", list, "证据范围")
    if chapter_range and "章节" not in result and "章节范围" not in result:
        result["章节范围"] = chapter_range
        changes.append("章节范围：采用调度器已确定的周期范围")
    for section in ("新增元素", "修改元素"):
        bucket = result.get(section)
        if not isinstance(bucket, dict):
            continue
        for collection in COLLECTION_KEYS:
            decode(bucket, collection, list, f"{section}.{collection}")
            items = bucket.get(collection)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                detail = item.get("详情")
                if not isinstance(detail, dict):
                    continue
                for field in ("首次章节", "最近章节"):
                    value = detail.get(field)
                    # Only a single explicit sequence is losslessly normalizable.
                    match = re.fullmatch(r"(?:第)?(\d+)(?:章)?", str(value))
                    if match and int(match[1]) > 0:
                        normalized = str(int(match[1])).zfill(4)
                        if value != normalized:
                            detail[field] = normalized
                            changes.append(f"{section}.{collection}[{item.get('名称')}].{field}：规范章节序号")
    operations = result.get("治理操作")
    if isinstance(operations, dict):
        for key in ("合并元素", "重命名元素", "替换引用", "设置字段", "追加详情", "治理标签", "降级元素", "删除元素"):
            decode(operations, key, list, f"治理操作.{key}")
    return result, changes
