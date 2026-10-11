"""按正式索引无损更正 Delta 类别；不猜测别名，不合并歧义元素。"""
from __future__ import annotations

from story_schema_rules import COLLECTION_KEYS, COLLECTION_TO_TYPE, DETAIL_REF_ARRAY_KEYS, DETAIL_REF_RE, TYPE_TO_COLLECTION


def align_registered_types(delta: dict, current: dict) -> list[str]:
    """迁移唯一已登记的修改目标及关联引用，保留完整档案与冲突原值。

    只有正式名称在一个类别中唯一存在时才自动修复。目的集合已含同名
    补丁、关联字段类型异常或名称跨类别重复时，留给原校验器处理。
    同一实现也用于保全比较，避免把合法迁移当成删除。
    """
    categories: dict[str, set[str]] = {}
    for collection in COLLECTION_KEYS:
        items = current.get(collection, [])
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("名称"), str):
                categories.setdefault(item["名称"], set()).add(collection)
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
    unique = {name: next(iter(kinds)) for name, kinds in categories.items() if len(kinds) == 1}
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
                if not isinstance(destinations, list) or any(
                    isinstance(other, dict) and other.get("名称") == item["名称"] for other in destinations
                ):
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
