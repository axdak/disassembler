#!/usr/bin/env python3
"""
Delta合并脚本 - 将单章Delta合并到增量JSON（字段级深度合并）

这是实现"边分析边提取边修改"的核心工具。
主控Agent每完成一章子Agent调用后，运行此脚本将该章Delta合并到增量JSON。

用法:
    python merge_delta.py <增量JSON路径> <Delta JSON路径>

合并策略（字段级深度合并，只增不删）:
    - 元素匹配: 优先按"名称"匹配，找不到再按"别名"匹配（别名命中时合并到主名称元素）
    - 新增元素: 与已有元素同名/别名命中 → 字段级深度合并；否则追加
    - 修改元素: 同上（语义上"修改"只需包含要更新的字段，但合并函数对两者一视同仁）
    - 字段级合并规则:
        * 介绍 (string): 追加，去重，以换行分隔
        * 列表字段 (标签集/别名/关系/所属阵营/参与成员/目标事件/涉及事件): 去重合并，保持原顺序
        * 详情 (dict): 键级递归合并；证据追踪字段按过程库语义追加/保留/更新，其它同名键按原规则合并
        * 标量字段 (性别/年龄/是否主角/重量级/分组/发生地点/父级地点/父级阵营/座落地点):
          - Delta中该字段为非空值 → 覆盖
          - Delta中该字段为空值/缺省 → 跳过，保留原值
    - 顶层"介绍"字段: 标题非空覆盖，描述追加去重
    - 增量JSON不存在或缺骨架时: 自动初始化顶层骨架（介绍 + 七类空集）

输出示例:
    === Delta合并结果 ===
    章节: 第3章 风起
    角色: +2 新增, 1 修改 (当前共 5)
    事件: +1 新增, 0 修改 (当前共 3)
    ...
"""

import sys
import os
import json
import tempfile
import re

from chronology import (
    assign_event_group_orders,
    insert_event_in_chapter_order,
    normalize_chapter_value,
    normalize_event_temporal_fields,
)
from story_schema_rules import (
    COLLECTION_KEYS,
    DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS,
    STRICT_DETAIL_KEY_FIELDS,
    SUPPLEMENTARY_TAGS_DETAIL_KEY,
    dump_supplementary_tags,
    is_cumulative_detail_field,
    parse_supplementary_tags,
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_stdio()


NAME_FIELD = "名称"
ALIAS_FIELD = "别名"

# 列表型字段（去重合并，保持顺序）
LIST_FIELDS = {
    "标签集", "别名", "关系", "所属阵营",
    "参与成员", "目标事件", "涉及事件",
}

# 标量型字段（非空覆盖、空值跳过）。性别/年龄/是否主角/重量级/分组/发生地点/父级地点/父级阵营/座落地点
SCALAR_FIELDS = {
    "性别", "年龄", "是否主角", "重量级", "分组",
    "发生地点", "父级地点", "父级阵营", "座落地点",
    "生日", "时间",
}

# 介绍型字符串字段（追加去重）
INTRO_FIELD = "介绍"
BASE_LEGACY_TRACE_DETAIL_FIELDS = {"来源章节", "首次出现章节", "最近更新章节"}
DELTA_ONLY_DETAIL_FIELDS = {"提取理由"}
CUMULATIVE_DETAIL_ARRAY_FIELDS = DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS
CHAPTER_PREFIXED_DETAIL_KEY_RE = re.compile(r"^\d{4,}-")
LEADING_CHAPTER_MARKER_RE = re.compile(r"^\s*(?:第?\d{1,6}\s*章|\d{4,})\s*[:：,，、\-]\s*")

# 顶层骨架与统一 Schema 共用集合定义，避免新增集合时合并器遗漏。
TOPLEVEL_SKELETON = {"介绍": {"标题": "", "描述": ""}, **{key: [] for key in COLLECTION_KEYS}}


def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def ensure_skeleton(incremental):
    """确保增量JSON包含完整顶层骨架，缺失的键补上。原地修改并返回。"""
    if not isinstance(incremental, dict):
        return dict(TOPLEVEL_SKELETON)
    for key, default in TOPLEVEL_SKELETON.items():
        if key not in incremental:
            incremental[key] = json.loads(json.dumps(default))  # 深拷贝
        elif key == "介绍" and not isinstance(incremental[key], dict):
            incremental[key] = {"标题": "", "描述": ""}
        elif key != "介绍" and not isinstance(incremental[key], list):
            incremental[key] = []
    return incremental


def build_lookup_index(existing_items):
    """
    构建 名称 + 别名 → 索引 的查找表。
    返回:
        name_index: {名称: 索引}
        alias_index: {别名: 索引}  (别名命中时也指向同一元素)
    同一个别名若被多个元素声明，保留第一个。
    """
    name_index = {}
    alias_index = {}
    for i, item in enumerate(existing_items):
        name = item.get(NAME_FIELD, "")
        if name and name not in name_index:
            name_index[name] = i
        for alias in item.get(ALIAS_FIELD, []) or []:
            if alias and alias not in alias_index and alias not in name_index:
                alias_index[alias] = i
    return name_index, alias_index


def find_index(name_index, alias_index, name):
    """按名称→别名顺序查找元素索引，找不到返回 -1。"""
    if name in name_index:
        return name_index[name]
    if name in alias_index:
        return alias_index[name]
    return -1


def merge_list_field(existing_list, new_list):
    """列表字段去重合并，保持原顺序在前。"""
    if not isinstance(existing_list, list):
        existing_list = []
    if not isinstance(new_list, list):
        new_list = []
    merged = list(existing_list)
    seen = set()
    for x in merged:
        try:
            seen.add(x)
        except TypeError:
            pass  # 不可哈希的元素跳过去重
    for x in new_list:
        try:
            if x not in seen:
                merged.append(x)
                seen.add(x)
        except TypeError:
            merged.append(x)
    return merged


def detail_fields_to_skip(collection_key=""):
    """最终结构中应从详情过滤掉的Delta/旧版追溯字段。"""
    fields = set(BASE_LEGACY_TRACE_DETAIL_FIELDS)
    if collection_key != "事件集":
        fields |= DELTA_ONLY_DETAIL_FIELDS
    return fields


def should_prefix_detail_key(key, collection_key="", chapter_prefix=""):
    """Only free-form non-event snapshot keys receive a chapter prefix."""
    if not chapter_prefix or collection_key == "事件集":
        return False
    if not isinstance(key, str) or not key:
        return False
    if key in STRICT_DETAIL_KEY_FIELDS or key.startswith("关联"):
        return False
    if key in BASE_LEGACY_TRACE_DETAIL_FIELDS or is_cumulative_detail_field(key):
        return False
    if CHAPTER_PREFIXED_DETAIL_KEY_RE.match(key):
        return False
    return True


def strip_leading_chapter_marker(text):
    if not isinstance(text, str):
        return text
    return LEADING_CHAPTER_MARKER_RE.sub("", text, count=1).strip()


def merge_detail_field(existing_detail, new_detail, collection_key="", chapter_prefix=""):
    """合并最终详情，过程追溯字段仅留在Delta/分析/治理补丁。"""
    if not isinstance(existing_detail, dict):
        existing_detail = {}
    if not isinstance(new_detail, dict):
        new_detail = {}
    skip_fields = detail_fields_to_skip(collection_key)
    merged = {k: v for k, v in existing_detail.items() if k not in skip_fields}
    for k, new_v in new_detail.items():
        if k in skip_fields:
            continue

        actual_key = k
        if should_prefix_detail_key(k, collection_key, chapter_prefix):
            actual_key = f"{chapter_prefix}-{k}"
        strip_event_chapter = collection_key == "事件集" and is_cumulative_detail_field(k)

        if actual_key not in merged:
            if is_cumulative_detail_field(k):
                merged[actual_key] = merge_json_string_array_field(
                    None, new_v, strip_chapter_marker=strip_event_chapter
                )
            else:
                merged[actual_key] = new_v
            continue

        old_v = merged[actual_key]
        if isinstance(old_v, list) and isinstance(new_v, list):
            merged[actual_key] = merge_list_field(old_v, new_v)
        elif k == SUPPLEMENTARY_TAGS_DETAIL_KEY:
            merged[actual_key] = merge_supplementary_tags_field(old_v, new_v)
        elif is_cumulative_detail_field(k):
            merged[actual_key] = merge_json_string_array_field(
                old_v, new_v, strip_chapter_marker=strip_event_chapter
            )
        else:
            # string/其它: 新值非空则覆盖，空则保留旧值
            if new_v in (None, "", [], {}):
                merged[actual_key] = old_v
            else:
                merged[actual_key] = new_v
    return merged


def merge_scalar_field(old_v, new_v):
    """标量字段: 新值非空覆盖，空值跳过。"""
    if new_v in (None, "", [], {}):
        return old_v
    return new_v


def merge_intro_string(old_intro, new_intro):
    """介绍型字符串: 追加去重，以换行分隔。"""
    if not isinstance(new_intro, str) or not new_intro.strip():
        return old_intro
    new_intro = new_intro.strip()
    if not isinstance(old_intro, str) or not old_intro.strip():
        return new_intro
    old_intro = old_intro.strip()
    if new_intro in old_intro:
        return old_intro
    return old_intro + "\n" + new_intro


def parse_json_string_array_or_lines(value):
    """累计型详情字段兼容旧换行文本，新格式统一输出 JSON 字符串数组。"""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return [str(value).strip()] if str(value).strip() else []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [text]


def dump_json_string_array(items):
    return json.dumps(merge_list_field([], items), ensure_ascii=False, separators=(",", ":"))


def merge_json_string_array_field(old_v, new_v, *, strip_chapter_marker=False):
    """详情中的长期累计字段：JSON 字符串数组追加去重。"""
    items = parse_json_string_array_or_lines(old_v) + parse_json_string_array_or_lines(new_v)
    if strip_chapter_marker:
        items = [strip_leading_chapter_marker(item) for item in items]
    return dump_json_string_array(items)


def merge_supplementary_tags_field(old_v, new_v):
    """详情.补充标签 是 JSON 字符串数组，语义上必须追加去重。"""
    old_tags = [] if old_v in (None, "") else parse_supplementary_tags(old_v)
    new_tags = [] if new_v in (None, "") else parse_supplementary_tags(new_v)
    return dump_supplementary_tags(merge_list_field(old_tags, new_tags))


def deep_merge_item(existing_item, delta_item, collection_key="", chapter_prefix=""):
    """
    将 delta_item 字段级深度合并进 existing_item，返回合并后的元素（原地修改 existing_item）。
    语义: 只增不删。Delta未提供的字段不影响已有值。
    """
    # 名称: 保留已有名称（避免别名命中时把主名称改掉）
    # 不动 existing_item["名称"]
    for field, new_v in delta_item.items():
        if field == NAME_FIELD:
            continue
        if field == ALIAS_FIELD or field in LIST_FIELDS:
            existing_item[field] = merge_list_field(
                existing_item.get(field, []), new_v
            )
        elif field == INTRO_FIELD:
            existing_item[field] = merge_intro_string(
                existing_item.get(field, ""), new_v
            )
        elif field == "详情":
            existing_item[field] = merge_detail_field(
                existing_item.get(field, {}), new_v, collection_key=collection_key, chapter_prefix=chapter_prefix
            )
        elif field in SCALAR_FIELDS:
            existing_item[field] = merge_scalar_field(
                existing_item.get(field), new_v
            )
        else:
            # 未知字段: 非空覆盖、空值跳过（保守策略）
            if new_v not in (None, "", [], {}):
                existing_item[field] = new_v
    if collection_key == "事件集":
        normalize_event_temporal_fields(existing_item)
    return existing_item


def merge_collection(existing_items, new_items, modified_items, collection_key="", chapter_prefix=""):
    """
    合并单个元素集（字段级深度合并 + 别名匹配）。

    返回: (合并后列表, 新增计数, 修改计数, 跳过计数)
    """
    name_index, alias_index = build_lookup_index(existing_items)

    added = 0
    updated = 0
    skipped = 0

    # 新增元素和修改元素走同一深度合并路径，只是统计口径不同
    for bucket, is_modified_bucket in [
        (new_items, False),
        (modified_items, True),
    ]:
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if not isinstance(item, dict):
                skipped += 1
                continue
            name = item.get(NAME_FIELD, "")
            if not name:
                skipped += 1
                continue
            idx = find_index(name_index, alias_index, name)
            if idx >= 0:
                # 命中已有元素 → 字段级深度合并
                deep_merge_item(existing_items[idx], item, collection_key, chapter_prefix)
                # 合并后把可能新增的别名也登记进 alias_index
                for alias in item.get(ALIAS_FIELD, []) or []:
                    if alias and alias not in alias_index and alias not in name_index:
                        alias_index[alias] = idx
                updated += 1
            else:
                # 真正的新元素 → 追加
                # 先做一次自洽清洗（保证结构完整）
                clean = {}
                for k, v in item.items():
                    clean[k] = v
                clean["详情"] = merge_detail_field({}, clean.get("详情", {}), collection_key=collection_key, chapter_prefix=chapter_prefix)
                if collection_key == "事件集":
                    normalize_event_temporal_fields(clean)
                    new_idx = insert_event_in_chapter_order(existing_items, clean)
                    # 插入会改变后续索引，重建映射才能继续安全合并同一批事件。
                    name_index, alias_index = build_lookup_index(existing_items)
                else:
                    existing_items.append(clean)
                    new_idx = len(existing_items) - 1
                if name not in name_index:
                    name_index[name] = new_idx
                for alias in clean.get(ALIAS_FIELD, []) or []:
                    if alias and alias not in alias_index and alias not in name_index:
                        alias_index[alias] = new_idx
                added += 1

    return existing_items, added, updated, skipped


def merge_toplevel_intro(incremental, delta_intro):
    """合并顶层"介绍"字段: 标题非空覆盖, 描述追加去重。"""
    if not isinstance(delta_intro, dict):
        return
    incremental.setdefault("介绍", {"标题": "", "描述": ""})
    if not isinstance(incremental["介绍"], dict):
        incremental["介绍"] = {"标题": "", "描述": ""}
    new_title = delta_intro.get("标题", "")
    if isinstance(new_title, str) and new_title.strip():
        incremental["介绍"]["标题"] = new_title.strip()
    new_desc = delta_intro.get("描述", "")
    if isinstance(new_desc, str) and new_desc.strip():
        incremental["介绍"]["描述"] = merge_intro_string(
            incremental["介绍"].get("描述", ""), new_desc
        )


def merge_delta(incremental_path, delta_path):
    """执行合并。增量JSON不存在或缺骨架时自动初始化。"""
    # 增量JSON: 不存在则用骨架初始化；存在则补全缺失的顶层键
    if not os.path.isfile(incremental_path):
        incremental = json.loads(json.dumps(TOPLEVEL_SKELETON))
        os.makedirs(os.path.dirname(os.path.abspath(incremental_path)), exist_ok=True)
    else:
        try:
            incremental = load_json(incremental_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"增量JSON已存在但格式损坏，已停止合并以避免覆盖过程库: {incremental_path}\n"
                f"请先用 run_pipeline.py recover-story 从最近可信快照恢复并重放后续Delta。\n"
                f"JSON错误: {exc}"
            )
    ensure_skeleton(incremental)

    delta = load_json(delta_path)

    chapter_prefix = normalize_chapter_value(delta.get("章节", ""))

    stats = {}
    new_elements = delta.get("新增元素", {}) or {}
    modified_elements = delta.get("修改元素", {}) or {}

    for key in COLLECTION_KEYS:
        existing = incremental.get(key, [])
        if not isinstance(existing, list):
            existing = []
        new_items = new_elements.get(key, []) or []
        mod_items = modified_elements.get(key, []) or []

        merged, added, updated, skipped = merge_collection(
            existing, new_items, mod_items, key, chapter_prefix=chapter_prefix
        )
        incremental[key] = merged
        stats[key] = {
            "新增": added,
            "修改": updated,
            "跳过": skipped,
            "当前总数": len(merged),
        }

    assign_event_group_orders(incremental["事件集"])

    # 顶层介绍
    if "介绍" in delta:
        merge_toplevel_intro(incremental, delta["介绍"])

    save_json(incremental_path, incremental)
    return stats


def print_stats(stats, chapter_name=""):
    """打印合并统计"""
    print(f"=== Delta合并结果 ===")
    if chapter_name:
        print(f"章节: {chapter_name}")
    print()

    type_names = {
        "角色集": "角色",
        "事件集": "事件",
        "地点集": "地点",
        "线索集": "线索",
        "阵营集": "阵营",
        "物品集": "物品",
    }

    total_added = 0
    total_updated = 0

    for key in COLLECTION_KEYS:
        s = stats.get(key, {})
        added = s.get("新增", 0)
        updated = s.get("修改", 0)
        total = s.get("当前总数", 0)
        skipped = s.get("跳过", 0)

        total_added += added
        total_updated += updated

        name = type_names.get(key, key)
        parts = [f"+{added} 新增"] if added else []
        if updated:
            parts.append(f"{updated} 修改")
        if skipped:
            parts.append(f"{skipped} 跳过")

        detail = ", ".join(parts) if parts else "无变更"
        print(f"  {name}: {detail} (当前共 {total})")

    print(f"\n  合计: +{total_added} 新增, {total_updated} 修改")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python merge_delta.py <增量JSON路径> <Delta JSON路径>")
        print()
        print("功能: 将单章Delta字段级深度合并到增量JSON，实现边分析边提取边修改")
        print("      合并策略: 只增不删; 介绍追加, 列表去重合并, 详情键级合并, 标量非空覆盖")
        print("      增量JSON不存在时自动初始化顶层骨架")
        sys.exit(1)

    incremental_path = sys.argv[1]
    delta_path = sys.argv[2]

    if not os.path.isfile(delta_path):
        print(f"错误: Delta JSON不存在: {delta_path}")
        sys.exit(1)

    # 读取章节名（从Delta文件中）
    try:
        delta = load_json(delta_path)
        chapter_name = delta.get("章节", "")
    except Exception:
        chapter_name = ""

    try:
        stats = merge_delta(incremental_path, delta_path)
    except RuntimeError as exc:
        print(f"错误: {exc}")
        sys.exit(1)
    print_stats(stats, chapter_name)
