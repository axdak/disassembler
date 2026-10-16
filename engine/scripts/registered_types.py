"""按正式索引无损更正 Delta 类别；不猜测别名，不合并歧义元素。"""
from __future__ import annotations

import copy
import json

from story_schema_rules import COLLECTION_KEYS, COLLECTION_TO_TYPE, DETAIL_REF_ARRAY_KEYS, DETAIL_REF_RE, TYPE_TO_COLLECTION


def is_unregistered_name(current: dict, name: str) -> bool:
    """Check every formal name/alias before permitting an evidence-reviewed add.

    Absence is permission to ask the model, never a program decision to create.
    Malformed registry collections cannot establish absence safely.
    """
    for collection in COLLECTION_KEYS:
        entries = current.get(collection, [])
        if not isinstance(entries, list):
            return False
        for item in entries:
            if not isinstance(item, dict):
                continue
            aliases = item.get("别名", [])
            if item.get("名称") == name or isinstance(aliases, list) and name in aliases:
                return False
    return True


def _values_retained(original, revised, path=()):
    """Exact old values, with append-only lists and intact reason prefixes."""
    # Empty/null evidence is a known repairable gap, not narrative to preserve.
    # Filling it must work together with a relocation; substantive reasons still
    # require the complete original prefix and original-evidence review.
    if path[-2:] == ("详情", "提取理由") and (original is None or isinstance(original, str) and not original.strip()):
        return isinstance(revised, str) and bool(revised.strip())
    if type(original) is not type(revised):
        return False
    if isinstance(original, dict):
        return all(key in revised and _values_retained(value, revised[key], (*path, key))
                   for key, value in original.items())
    if isinstance(original, list):
        return len(original) <= len(revised) and all(
            _values_retained(a, b, (*path, index)) for index, (a, b) in enumerate(zip(original, revised)))
    if path[-2:] == ("详情", "提取理由") and isinstance(original, str) and original:
        return revised.startswith(original)
    return original == revised


def align_preserved_additions(before: dict, after: dict, current: dict) -> None:
    """Align complete modify-to-add moves only in preservation comparison copies.

    Require no formal name/alias, exactly one same-name proposal in each whole
    candidate, the same collection and all original values. Source/destination
    collisions, changed facts, missing fields or deletions stay errors. Existing
    validators and independent original-evidence review decide whether to add.
    """
    from coerce_delta import relocate_element_fields
    def matches(document, name):
        return [(section, collection, item) for section in ("新增元素", "修改元素")
                if isinstance(document.get(section), dict) for collection in COLLECTION_KEYS
                for item in (document[section].get(collection) if isinstance(document[section].get(collection), list) else [])
                if isinstance(item, dict) and item.get("名称") == name]
    modifications = before.get("修改元素")
    additions = before.get("新增元素", {})
    if not isinstance(modifications, dict) or not isinstance(additions, dict):
        return
    for collection in COLLECTION_KEYS:
        items = modifications.get(collection)
        if not isinstance(items, list):
            continue
        for item in list(items):
            name = item.get("名称") if isinstance(item, dict) else None
            if not isinstance(name, str) or not is_unregistered_name(current, name):
                continue
            destinations = matches(after, name)
            if len(matches(before, name)) != 1 or len(destinations) != 1:
                continue
            section, target, candidate = destinations[0]
            if section != "新增元素" or target != collection or not isinstance(additions.get(target, []), list):
                continue
            expected = {"新增元素": {target: [copy.deepcopy(item)]}}
            actual = {"新增元素": {target: [copy.deepcopy(candidate)]}}
            relocate_element_fields(expected)
            relocate_element_fields(actual)
            if not _values_retained(expected, actual):
                continue
            items.remove(item)
            additions.setdefault(target, []).append(expected["新增元素"][target][0])
            before["新增元素"] = additions


def align_preserved_moves(before: dict, after: dict, current: dict) -> None:
    """仅为保全比较对齐模型选择的完整迁移，不替模型判定语义或写回产物。

    旧类别无此正式身份、新类别已有登记、旧/新补丁各恰有一个同名项，
    且迁移后所有原值保留时才对齐，允许新增详情字段、在列表末尾补充，
    或在详情.提取理由的完整原文之后追加归类依据；其他原字符串须一致。
    原文依据由后续独立复核验收。
    不接受覆盖既有补丁、合并条目、改名、丢字段或把短文本换掉。
    """
    # Local import avoids a cycle: mechanical coercion also uses this module.
    from coerce_delta import relocate_element_fields

    old, new = before.get("修改元素"), after.get("修改元素")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return

    def records(bucket, name):
        return [(collection, item) for collection in COLLECTION_KEYS
                for item in (bucket.get(collection) if isinstance(bucket.get(collection), list) else [])
                if isinstance(item, dict) and item.get("名称") == name]

    def registered(collection, name):
        entries = current.get(collection)
        return isinstance(entries, list) and any(
            isinstance(item, dict) and item.get("名称") == name for item in entries)

    for collection in COLLECTION_KEYS:
        items = old.get(collection)
        if not isinstance(items, list):
            continue
        for item in list(items):
            name = item.get("名称") if isinstance(item, dict) else None
            if not isinstance(name, str) or registered(collection, name):
                continue
            matches = records(new, name)
            if len(records(old, name)) != 1 or len(matches) != 1:
                continue
            target, candidate = matches[0]
            if target == collection or not registered(target, name):
                continue
            if target in old and not isinstance(old[target], list):
                continue
            expected = {"修改元素": {target: [copy.deepcopy(item)]}}
            actual = {"修改元素": {target: [copy.deepcopy(candidate)]}}
            relocate_element_fields(expected)
            relocate_element_fields(actual)
            if not _values_retained(expected, actual):
                continue
            items.remove(item)
            old.setdefault(target, []).append(expected["修改元素"][target][0])


def align_registered_types(delta: dict, current: dict) -> list[str]:
    """迁移唯一已登记的修改目标及关联引用，保留完整档案与冲突原值。

    只有正式名称在唯一类别恰有一条记录时才自动修复。目的集合恰有
    一条类型敏感 JSON 完全相同的补丁时，仅移除错误类别镜像；其他
    同名冲突、关联字段类型异常或登记歧义仍交原校验器处理。
    同一实现也用于保全比较，避免把无损迁移或精确镜像去重当成删除。
    """
    categories: dict[str, set[str]] = {}
    registrations: dict[str, int] = {}
    for collection in COLLECTION_KEYS:
        items = current.get(collection, [])
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("名称"), str):
                categories.setdefault(item["名称"], set()).add(collection)
                registrations[item["名称"]] = registrations.get(item["名称"], 0) + 1
    # An explicit same-name proposal in another category makes the batch
    # ambiguous even if the old registry alone had exactly one category.
    additions = delta.get("新增元素", {})
    if isinstance(additions, dict):
        for collection in COLLECTION_KEYS:
            items = additions.get(collection, [])
            for item in items if isinstance(items, list) else []:
                name = item.get("名称") if isinstance(item, dict) else None
                if isinstance(name, str) and name in categories:
                    categories[name].add(collection)
    unique = {name: next(iter(kinds)) for name, kinds in categories.items()
              if len(kinds) == 1 and registrations[name] == 1}
    report = []
    modifications = delta.get("修改元素", {})
    if isinstance(modifications, dict):
        for collection in COLLECTION_KEYS:
            items = modifications.get(collection)
            if not isinstance(items, list):
                continue
            for item in list(items):
                name = item.get("名称") if isinstance(item, dict) else None
                target = unique.get(name) if isinstance(name, str) else None
                if not target or target == collection:
                    continue
                destinations = modifications.get(target, [])
                if not isinstance(destinations, list):
                    continue
                matches = [other for other in destinations
                           if isinstance(other, dict) and other.get("名称") == name]
                if matches:
                    # Sorted JSON ignores only object key order: unlike dict equality,
                    # it keeps bool/number distinctions and the complete array order.
                    if len(matches) == 1 and json.dumps(item, sort_keys=True) == json.dumps(matches[0], sort_keys=True):
                        items[:] = [other for other in items if other is not item]
                        report.append(f"修改元素[{name}] {collection} → {target}（移除完全相同的重复镜像，保留目的档案）")
                    continue
                modifications.setdefault(target, destinations).append(item)
                items.remove(item)
                report.append(f"修改元素[{item['名称']}] {collection} → {target}（档案完整迁移）")
    for section_name in ("新增元素", "修改元素"):
        section = delta.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for collection in COLLECTION_KEYS:
            items = section.get(collection, [])
            if not isinstance(items, list):
                continue
            for item in items:
                detail = item.get("详情") if isinstance(item, dict) else None
                if not isinstance(detail, dict):
                    continue
                for field, refs in list(detail.items()):
                    if field not in DETAIL_REF_ARRAY_KEYS or not isinstance(refs, list):
                        continue
                    for ref in list(refs):
                        match = DETAIL_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
                        target = unique.get(match[2]) if match else None
                        if not target or TYPE_TO_COLLECTION[match[1]] == target:
                            continue
                        kind = COLLECTION_TO_TYPE[target]
                        destination = "关联" + kind
                        if destination in detail and not isinstance(detail[destination], list):
                            continue
                        corrected = f"{kind}:{match[2]}"
                        destination_refs = detail.setdefault(destination, [])
                        if corrected not in destination_refs:
                            destination_refs.append(corrected)
                        refs.remove(ref)
                        report.append(f"{section_name}[{item.get('名称', '')}] {ref} → {corrected}")
    return report
