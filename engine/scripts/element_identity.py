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
