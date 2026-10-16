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
    operations = result.get("治理操作")
    if isinstance(operations, dict):
        for key in ("合并元素", "重命名元素", "替换引用", "设置字段", "追加详情", "治理标签", "降级元素", "删除元素"):
            decode(operations, key, list, f"治理操作.{key}")
    if isinstance(story, dict):
        # Reuse the exact same registry rules as chapter coercion/preservation.
        # Compare mirrors before trace formatting can make different raw values equal.
        from registered_types import align_registered_types
        changes.extend(align_registered_types(result, story))
    for section in ("新增元素", "修改元素"):
        bucket = result.get(section)
        if not isinstance(bucket, dict):
            continue
        for collection in COLLECTION_KEYS:
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
    if isinstance(story, dict):
        changes.extend(_retain_event_compound_tags(result, story))
        changes.extend(_relocate_additive_tags(result, story))
    return result, changes


def _retain_event_compound_tags(patch, story):
    """Correct only an exact, complete old-tag partition; never repair ambiguity.

    Four-dimensional event tags already carry formal narrative meaning. Moving
    them back to the retained partition is deterministic and preserves both the
    original reason and every unrelated operation. The change report records
    the rejected original intent without adding unsupported patch fields.
    """
    from story_schema_rules import is_event_compound_tag

    operations = patch.get("治理操作", {})
    tag_ops = operations.get("治理标签") if isinstance(operations, dict) else None
    if not isinstance(tag_ops, list):
        return []
    changes = []
    for op in tag_ops:
        if not isinstance(op, dict) or set(op) != {"类型", "名称", "保留标签", "降级标签", "理由"}:
            continue
        name, retained, demoted = op.get("名称"), op.get("保留标签"), op.get("降级标签")
        if (op.get("类型") != "事件" or not isinstance(name, str)
                or not isinstance(retained, list) or not isinstance(demoted, list)
                or not isinstance(op.get("理由"), str) or not op["理由"].strip()):
            continue
        records = story.get("事件集", [])
        matches = [item for item in records if isinstance(item, dict) and item.get("名称") == name] if isinstance(records, list) else []
        if len(matches) != 1 or sum(isinstance(item, dict) and item.get("类型") == "事件" and item.get("名称") == name for item in tag_ops) != 1:
            continue
        old = matches[0].get("标签集")
        if (not isinstance(old, list)
                or any(not isinstance(tag, str) or not tag.strip() for tag in old + retained + demoted)
                or len({tag.strip() for tag in old}) != len(old)
                or len({tag.strip() for tag in retained + demoted}) != len(retained + demoted)
                or set(old) != set(retained + demoted)):
            continue
        protected = [tag for tag in demoted if is_event_compound_tag(tag)]
        if not protected:
            continue
        op["保留标签"] = retained + protected
        op["降级标签"] = [tag for tag in demoted if tag not in protected]
        intent = json.dumps({"保留标签": retained, "降级标签": demoted, "理由": op["理由"]}, ensure_ascii=False)
        changes.append(f"治理标签[事件:{name}]：保留有效四维复合标签{json.dumps(protected, ensure_ascii=False)}；原始意图:{intent}")
    return changes


def governance_identity_baseline(before, patch):
    """只对齐显式身份和引用操作，供字段与事实保全检查共用同一基线。"""
    from apply_governance_ops import apply_ops
    ops = patch.get("治理操作", {}) if isinstance(patch, dict) else {}
    if not isinstance(ops, dict):
        raise ValueError("治理操作必须为对象")
    identity_ops = {key: ops[key] for key in ("合并元素", "重命名元素", "替换引用", "降级元素", "删除元素") if key in ops}
    if not identity_ops:
        return before
    baseline, _, warnings = apply_ops(before, {"治理操作": identity_ops})
    if warnings:
        raise ValueError("；".join(warnings))
    return baseline


def governance_quality_errors(before, after, patch):
    """Reject loss of previously useful formal fields after candidate governance.

    Replay only explicit identity/reference operations to align old records with
    the candidate. This permits intentional merges, renames, removals and their
    reference cleanup while preventing field writes from authorizing their own
    destructive result. Historical gaps are ignored; ambiguous changed records
    produce a scoped error instead of guessing which identity lost information.
    """
    from chronology import is_semantic_event_group_name, split_event_group
    from element_identity import build_lookup_index, find_index
    from story_schema_rules import (COLLECTION_TO_TYPE, DETAIL_REF_RE, STRUCTURAL_REF_FIELDS,
                                    TYPE_TO_COLLECTION, is_event_compound_tag)
    from information_preservation import field_correction

    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["治理质量 scope：治理前后故事结构必须为对象"]
    try:
        baseline = governance_identity_baseline(before, patch)
    except (ValueError, TypeError, KeyError) as exc:
        return [f"治理质量 scope：无法确定身份和引用变换：{exc}"]

    def nonblank(value):
        return isinstance(value, str) and bool(value.strip())

    # Resolve references against the transformed baseline; dangling historical
    # references are not promoted into new quality requirements.
    indices = {key: build_lookup_index(baseline.get(key, []) or []) for key in COLLECTION_KEYS}

    def reference_identity(collection, value):
        """Compare resolved identities so alias/whitespace cleanup stays legal."""
        if not nonblank(value):
            return None
        try:
            index = find_index(*indices[collection], value.strip())
            return (collection, index) if index >= 0 else None
        except ValueError:
            return None

    def typed_reference(value):
        match = DETAIL_REF_RE.fullmatch(value.strip()) if isinstance(value, str) else None
        return reference_identity(TYPE_TO_COLLECTION[match[1]], match[2]) if match else None

    def relationship_reference(value, target):
        # The prefix is a relation such as 盟友, not an entity type such as 角色.
        if not isinstance(value, str):
            return None
        prefix, separator, name = value.partition(":")
        identity = reference_identity(target, name) if separator and prefix.strip() else None
        return (prefix.strip(), identity) if identity is not None else None

    errors = []
    previous_intro = baseline.get("介绍", {})
    current_intro = after.get("介绍", {})
    for field in ("标题", "描述"):
        if isinstance(previous_intro, dict) and nonblank(previous_intro.get(field)) and (not isinstance(current_intro, dict) or not nonblank(current_intro.get(field))):
            errors.append(f"治理质量：介绍.{field}已有有效内容，不得清空或迁入详情")
    for collection in COLLECTION_KEYS:
        previous_records = baseline.get(collection, [])
        current_records = after.get(collection, [])
        if not isinstance(previous_records, list) or not isinstance(current_records, list):
            errors.append(f"治理质量 scope：{collection}必须为数组")
            continue
        previous_by_name, current_by_name = {}, {}
        for records, index in ((previous_records, previous_by_name), (current_records, current_by_name)):
            for item in records:
                if isinstance(item, dict) and isinstance(item.get("名称"), str):
                    index.setdefault(item["名称"], []).append(item)
        for name, old_matches in previous_by_name.items():
            new_matches = current_by_name.get(name, [])
            label = f"{collection}[{name}]"
            if len(old_matches) != 1 or len(new_matches) != 1:
                if old_matches != new_matches:
                    errors.append(f"治理质量 scope：{label}身份缺失或不唯一，须用明确合并、重命名、降级或删除操作说明")
                continue
            old, new = old_matches[0], new_matches[0]
            if nonblank(old.get("介绍")) and not nonblank(new.get("介绍")):
                errors.append(f"治理质量：{label}.介绍已有有效内容，不得清空或迁入详情")
            if collection == "事件集":
                _, old_group = split_event_group(old.get("分组"))
                _, new_group = split_event_group(new.get("分组"))
                if is_semantic_event_group_name(old_group) and not is_semantic_event_group_name(new_group):
                    errors.append(f"治理质量：{label}.分组不得丢失已有剧情语义")
                old_tags = old.get("标签集", [])
                new_tags = new.get("标签集", [])
                protected = [tag for tag in old_tags if is_event_compound_tag(tag)] if isinstance(old_tags, list) else []
                current_tags = {tag.strip() for tag in new_tags if isinstance(tag, str)} if isinstance(new_tags, list) else set()
                if (any(tag.strip() not in current_tags for tag in protected)
                        and not field_correction(patch, "事件", name, "标签集", old_tags, new_tags)):
                    errors.append(f"治理质量：{label}.标签集不得移除已有有效四维复合标签或迁入详情")
            for field, target, kind in STRUCTURAL_REF_FIELDS[collection]:
                old_value, new_value = old.get(field), new.get(field)
                if kind == "scalar":
                    old_identity = reference_identity(target, old_value)
                    missing = old_identity is not None and old_identity != reference_identity(target, new_value)
                else:
                    old_refs = old_value if isinstance(old_value, list) else []
                    new_refs = new_value if isinstance(new_value, list) else []
                    resolve = relationship_reference if kind == "relation_list" else lambda value, target: reference_identity(target, value)
                    valid = {identity for value in old_refs if (identity := resolve(value, target)) is not None}
                    current = {identity for value in new_refs if (identity := resolve(value, target)) is not None}
                    missing = bool(valid - current)
                # 目标存在只证明引用格式有效，不证明原提取的关系/地点正确。
                # 有明确原值、理由和原句的纠错复用同一入口；提交时核对原句。
                if missing and not field_correction(patch, COLLECTION_TO_TYPE[collection], name, field, old_value, new_value):
                    errors.append(f"治理质量：{label}.{field}不得丢失已有有效正式引用；引用变更须使用明确治理操作")
            old_detail, new_detail = old.get("详情", {}), new.get("详情", {})
            if isinstance(old_detail, dict):
                for field, value in old_detail.items():
                    old_refs = value if isinstance(value, list) else [value]
                    valid = {identity for ref in old_refs if (identity := typed_reference(ref)) is not None}
                    current = new_detail.get(field) if isinstance(new_detail, dict) else None
                    current_refs = current if isinstance(current, list) else [current]
                    current_identities = {identity for ref in current_refs if (identity := typed_reference(ref)) is not None}
                    if (valid - current_identities and not field_correction(
                            patch, COLLECTION_TO_TYPE[collection], name, "详情/" + field, value, current)):
                        errors.append(f"治理质量：{label}.详情.{field}不得丢失已有明确关联引用")
    return errors


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
