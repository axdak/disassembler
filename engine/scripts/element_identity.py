"""Shared, deterministic element identity rules for validation and mutation.

Canonical names win over aliases. Ambiguous names/aliases are errors rather
than arbitrary first matches; near names are suggestions only, never merges.
The external story/Delta format remains name-based and unchanged.
"""
from __future__ import annotations

from difflib import get_close_matches


def build_lookup_index(items):
    """Build canonical/alias indexes; None marks a conflicting identity."""
    names, aliases = {}, {}
    for index, item in enumerate(items):
        index_item(names, aliases, item, index)
    return names, aliases


def index_item(names, aliases, item, index):
    """Index one append/union update without rescanning a whole collection."""
    if not isinstance(item, dict):
        return
    name = item.get("名称")
    if isinstance(name, str) and name:
        names[name] = index if name not in names or names[name] == index else None
    values = item.get("别名")
    if isinstance(values, list):
        for alias in values:
            if isinstance(alias, str) and alias:
                aliases[alias] = index if alias not in aliases or aliases[alias] == index else None


def find_index(names, aliases, name):
    """Resolve a name exactly first, then by unique alias; -1 means missing."""
    index = names.get(name, -1) if name in names else aliases.get(name, -1)
    if index is None:
        raise ValueError(f"名称或别名[{name}]匹配多个元素；请指定唯一正式名称")
    return index


def missing_target_message(name, names):
    """Offer bounded candidates without guessing whether two entities coincide."""
    candidates = get_close_matches(name, list(names), n=3, cutoff=0.55)
    hint = f"；相近正式名称：{'、'.join(candidates)}（须依据原文确认）" if candidates else ""
    return f"修改目标[{name}]不存在，禁止自动转为新增{hint}；若确为新元素，请显式放入新增元素并提供来源"


def same_patch_rename_targets(current, delta):
    """Resolve explicitly renamed modification targets to their original names.

    This is a lookup projection, not an operation: merging still precedes the
    official rename. Only direct, unique renames without identity conflicts can
    authorize a new-name update; unknown names never authorize record creation.
    """
    from story_schema_rules import COLLECTION_TO_TYPE, TYPE_TO_COLLECTION

    operations = delta.get("治理操作", {}) if isinstance(delta, dict) else {}
    renames = operations.get("重命名元素", []) if isinstance(operations, dict) else []
    modifications = delta.get("修改元素", {}) if isinstance(delta, dict) else {}
    if not isinstance(renames, list) or not isinstance(modifications, dict):
        return {}
    additions = delta.get("新增元素", {})
    additions = additions if isinstance(additions, dict) else {}
    result = {}
    # Type aliases such as 物品/道具 share one identity namespace. Check each
    # canonical collection once so alias spellings cannot conceal conflicts.
    for collection, typ in COLLECTION_TO_TYPE.items():
        items = modifications.get(collection, [])
        if not isinstance(items, list):
            continue
        requested = {item.get("名称") for item in items if isinstance(item, dict)
                     and isinstance(item.get("名称"), str)}
        declared = [op for op in renames if isinstance(op, dict)
                    and isinstance(op.get("类型"), str)
                    and TYPE_TO_COLLECTION.get(op["类型"]) == collection]
        if any(not isinstance(op.get(field), str) or not op[field].strip()
               or op[field] != op[field].strip()
               for op in declared for field in ("原名称", "新名称")):
            raise ValueError(f"同批重命名[{typ}]原名称和新名称必须是无首尾空白的非空字符串；禁止自动转为新增")
        relevant = [op for op in declared if op["新名称"] in requested]
        if not relevant:
            continue
        records = current.get(collection, []) or []
        names, aliases = build_lookup_index(records)
        for op in relevant:
            old, new = op.get("原名称"), op.get("新名称")

            def reject():
                raise ValueError(f"同批重命名[{typ}:{new}]不能唯一解析到无冲突的原正式档案；请核对治理操作，禁止自动转为新增")

            source = names.get(old, -1)
            if source is None or source < 0:
                reject()
            # Do not infer an order for duplicate, branching or chained renames.
            if any(other is not op and (other.get("原名称") in {old, new}
                       or other.get("新名称") in {old, new}) for other in declared):
                reject()
            destination = find_index(names, aliases, new)
            if destination >= 0 and destination != source:
                reject()
            proposed = additions.get(collection, [])
            if isinstance(proposed, list) and find_index(*build_lookup_index(proposed), new) != -1:
                reject()
            # A merge/removal of the same record changes identity before/after
            # rename. Leave such combinations to explicit correction, not a guess.
            for action in ("合并元素", "降级元素", "删除元素"):
                entries = operations.get(action, [])
                for entry in entries if isinstance(entries, list) else []:
                    if (not isinstance(entry, dict) or not isinstance(entry.get("类型"), str)
                            or TYPE_TO_COLLECTION.get(entry["类型"]) != collection):
                        continue
                    affected = [entry.get("名称"), entry.get("主") or entry.get("保留")]
                    sources = entry.get("并入", [])
                    affected.extend(sources if isinstance(sources, list) else [sources])
                    if any(isinstance(value, str) and (value == new or
                           find_index(names, aliases, value) == source) for value in affected):
                        reject()
            # Another update cannot introduce this future name as its own alias.
            for item in items:
                if (isinstance(item, dict) and item.get("名称") != new
                        and isinstance(item.get("别名"), list) and new in item["别名"]
                        and (not isinstance(item.get("名称"), str)
                             or find_index(names, aliases, item["名称"]) != source)):
                    reject()
            result.setdefault(collection, {})[new] = old
    return result
