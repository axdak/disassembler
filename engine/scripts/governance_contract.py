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

    介绍.描述 与 介绍.标题 的要求**必须不同**，并与 story_schema_rules 的模式策略一致：
    - 描述可由已处理原文直接生成，周期治理必须给出；缺失/空串/非字符串是一处真实缺口，
      判 error 并要求补齐。
    - 标题需要"有依据"才能补齐，原文没有书名或标题线索时留空是合法的：process 与
      governance 模式下它只是 warning（"最终交付前必须补充"），只有 final 模式才强制。
    历史故事长期 标题 为空时，若这里也判 error，每个周期都会卡死；而修复提示词又明令
    "标题有依据才补齐、不虚构"，模型永远无法满足 -> 死锁，并把一次内容缺口放大成一串
    无效的容量分层修复。因此这里只拦截"显式破坏已有标题"的更新（非字符串，或把已有
    非空标题改成空白），缺失或留空一律放行，由最终交付（final 模式）负责强制补齐。
    这不会放宽最终交付质量：final 模式仍要求标题非空。
    """
    previous = story.get("介绍", {}) if isinstance(story, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    proposed = patch.get("介绍", {}) if isinstance(patch, dict) else {}
    if not isinstance(proposed, dict):
        return ["周期治理的介绍必须为对象，包含有依据的标题和完整故事描述"]
    errors = []
    value = proposed.get("描述", previous.get("描述"))
    if not isinstance(value, str) or not value.strip():
        errors.append("周期治理后介绍.描述必须为非空字符串；依据已处理原文生成完整简介，不得用占位文字或分析正文填充")
    if "标题" in proposed:
        title = proposed.get("标题")
        had_title = isinstance(previous.get("标题"), str) and previous.get("标题", "").strip() != ""
        if not isinstance(title, str) or (not title.strip() and had_title):
            errors.append("周期治理不得把介绍.标题改成空白或非字符串；标题可暂留空，有依据时再补齐，不得破坏已有标题")
    return errors


def normalize_governance_patch(patch, chapter_range: str = "", *, story=None):
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
    if isinstance(story, dict):
        changes.extend(_relocate_additive_tags(result, story))
    return result, changes


def _relocate_additive_tags(patch, story):
    """Translate pure tag additions placed in the old-tag partition operation.

    Only unambiguous exact names, complete old-tag coverage and valid proposed
    tags are eligible. Demotions/missing tags/conflicting writes stay untouched
    for the existing validator; no tag or evidence is deleted to obtain a pass.
    """
    from story_schema_rules import TYPE_TO_COLLECTION, validate_controlled_tags
    operations = patch.get("治理操作", {})
    tag_ops = operations.get("治理标签") if isinstance(operations, dict) else None
    modifications = patch.get("修改元素", {})
    if not isinstance(tag_ops, list) or not isinstance(modifications, dict):
        return []
    changes, remaining = [], []
    for op in tag_ops:
        remaining.append(op)
        if not isinstance(op, dict) or set(op) != {"类型", "名称", "保留标签", "降级标签", "理由"}:
            continue
        collection = TYPE_TO_COLLECTION.get(op.get("类型")) if isinstance(op.get("类型"), str) else None
        name, retained, reason = op.get("名称"), op.get("保留标签"), op.get("理由")
        if (not collection or not isinstance(name, str) or not isinstance(retained, list)
                or op.get("降级标签") != [] or not isinstance(reason, str) or not reason.strip()):
            continue
        records = story.get(collection, [])
        matches = [x for x in records if isinstance(x, dict) and x.get("名称") == name] if isinstance(records, list) else []
        if len(matches) != 1 or sum(isinstance(x, dict) and x.get("类型") == op["类型"] and x.get("名称") == name for x in tag_ops) != 1:
            continue
        old = matches[0].get("标签集")
        if (not isinstance(old, list) or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in old + retained)
                or len(set(old)) != len(old) or len(set(retained)) != len(retained)
                or not set(old) < set(retained) or validate_controlled_tags(collection, retained)):
            continue
        # Other governance operations can rename/remove the target or change its
        # tags. Leave combinations for explicit repair instead of guessing order.
        if any(value for key, value in operations.items() if key != "治理标签"):
            continue
        items = modifications.get(collection, [])
        if not isinstance(items, list):
            continue
        edited = [x for x in items if isinstance(x, dict) and x.get("名称") == name]
        if len(edited) > 1:
            continue
        item = edited[0] if edited else {"名称": name, "详情": {}}
        detail = item.get("详情", {})
        if ("标签集" in item or not isinstance(detail, dict) or "标签集" in detail
                or not isinstance(detail.get("提取理由", ""), str)):
            continue
        old_reason = detail.get("提取理由", "")
        detail["提取理由"] = old_reason + ("\n标签新增依据：" if old_reason else "") + reason
        item["详情"] = detail
        item["标签集"] = list(retained)
        if not edited:
            modifications.setdefault(collection, []).append(item)
        patch["修改元素"] = modifications
        remaining.pop()
        changes.append(f"治理标签[{op['类型']}:{name}]：纯新增标签迁入修改元素，保留全部旧标签与理由")
    operations["治理标签"] = remaining
    return changes
