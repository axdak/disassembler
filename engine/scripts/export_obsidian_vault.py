#!/usr/bin/env python3
"""Export story structure JSON to an Obsidian-friendly Markdown vault.

The story JSON remains the source of truth. This exporter creates a readable
Markdown mirror for Obsidian graph/backlink workflows and AI plugins.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from story_schema_rules import COLLECTION_KEYS


COLLECTION_INFO: Dict[str, Dict[str, str]] = {
    "角色集": {"type": "角色", "dir": "角色", "index": "00-总览/角色索引.md"},
    "事件集": {"type": "事件", "dir": "事件", "index": "00-总览/时间线.md"},
    "地点集": {"type": "地点", "dir": "地点", "index": "00-总览/地点索引.md"},
    "线索集": {"type": "线索", "dir": "线索", "index": "00-总览/线索索引.md"},
    "阵营集": {"type": "阵营", "dir": "阵营", "index": "00-总览/阵营索引.md"},
    "物品集": {"type": "物品", "dir": "物品", "index": "00-总览/物品索引.md"},
    "其他事项集": {"type": "其他事项", "dir": "其他事项", "index": "00-总览/其他事项索引.md"},
}

DATAVIEW_QUERIES: Dict[str, str] = {
    "查询-主角.md": """# 查询-主角

```dataview
TABLE group, first_chapter, recent_chapter, tags
FROM "角色"
WHERE type = "角色" AND contains(tags, "主角")
SORT first_chapter ASC, name ASC
```
""",
    "查询-未回收线索.md": """# 查询-未回收线索

```dataview
TABLE group, first_chapter, recent_chapter, tags
FROM "线索"
WHERE type = "线索" AND (contains(tags, "未回收") OR contains(tags, "伏笔"))
SORT recent_chapter DESC, name ASC
```
""",
    "查询-按章节事件.md": """# 查询-按章节事件

```dataview
TABLE group, time, involved_chapters, tags
FROM "事件"
WHERE type = "事件"
SORT time ASC, file.name ASC
```
""",
    "查询-按阵营角色.md": """# 查询-按阵营角色

```dataview
TABLE group, first_chapter, recent_chapter, tags
FROM "角色"
WHERE type = "角色"
SORT group ASC, first_chapter ASC, name ASC
```
""",
}

OVERVIEW_LINKS = [
    "故事总览",
    "时间线",
    "角色索引",
    "阵营索引",
    "地点索引",
    "线索索引",
    "物品索引",
    "其他事项索引",
]

STRUCTURAL_REFS: Dict[str, List[Tuple[str, str, str]]] = {
    "角色集": [("所属阵营", "阵营集", "list"), ("关系", "角色集", "relation_list")],
    "事件集": [("发生地点", "地点集", "scalar"), ("参与成员", "角色集", "list"), ("目标事件", "事件集", "list")],
    "地点集": [("父级地点", "地点集", "scalar")],
    "线索集": [("涉及事件", "事件集", "list")],
    "阵营集": [("父级阵营", "阵营集", "scalar"), ("座落地点", "地点集", "scalar")],
    "物品集": [],
    "其他事项集": [],
}

DETAIL_REF_RE = re.compile(r"^(角色|事件|地点|线索|阵营|物品|道具):(.+)$")
TYPE_TO_COLLECTION = {
    "角色": "角色集",
    "事件": "事件集",
    "地点": "地点集",
    "线索": "线索集",
    "阵营": "阵营集",
    "物品": "物品集",
    "道具": "物品集",
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
BARE_YAML_RE = re.compile(r"^[^\s\[\]{}#&*!|>'\"%@`,:][^\[\]{}#&*!|>'\"%@`,:]*$")


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_story_path(input_path: Path, source: str = "final") -> Path:
    if input_path.is_file():
        return input_path
    if source == "process":
        candidate = input_path / "故事结构_增量.json"
    else:
        candidate = input_path / "故事结构.json"
    if not candidate.is_file():
        raise FileNotFoundError(f"未找到故事结构文件: {candidate}")
    return candidate


def sanitize_filename(name: str, fallback: str = "未命名") -> str:
    clean = INVALID_FILENAME_CHARS.sub("_", str(name or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean or fallback


def obsidian_link(name: str, label: Optional[str] = None) -> str:
    clean = str(name or "").strip()
    if not clean:
        return ""
    if label and label != clean:
        return f"[[{clean}|{label}]]"
    return f"[[{clean}]]"


def yaml_string(value: Any) -> str:
    text = str(value if value is not None else "")
    if text and not re.match(r"^[+-]?\d", text) and BARE_YAML_RE.match(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_list(values: Iterable[Any]) -> List[str]:
    result = []
    for value in values or []:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
    return result


def frontmatter(data: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            if value:
                for item in value:
                    lines.append(f"  - {yaml_string(item)}")
            else:
                lines.append("  []")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {yaml_string(value)}")
    lines.append("---")
    return "\n".join(lines)


def parse_json_string_array(value: Any) -> Optional[List[str]]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if str(item).strip()]


def readable_detail_value(value: Any) -> List[str]:
    parsed = parse_json_string_array(value)
    if parsed is not None:
        return parsed
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, "", {}, []):
        return []
    return [str(value)]


def concept_path(collection_key: str, item: Dict[str, Any], used_paths: set[str], filename_mode: str = "name") -> str:
    info = COLLECTION_INFO[collection_key]
    name = sanitize_filename(item.get("名称"), fallback=info["type"])
    if filename_mode == "type-name":
        name = f"{info['type']}-{name}"
    path = f"{info['dir']}/{name}.md"
    if path not in used_paths:
        used_paths.add(path)
        return path
    suffix = 2
    while True:
        candidate = f"{info['dir']}/{name}-{suffix}.md"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        suffix += 1


def build_concept_index(story: Dict[str, Any], filename_mode: str = "name") -> Dict[Tuple[str, str], Dict[str, Any]]:
    used_paths: set[str] = set()
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for collection_key in COLLECTION_KEYS:
        for idx, item in enumerate(story.get(collection_key, []) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("名称", "")).strip()
            if not name:
                continue
            path = concept_path(collection_key, item, used_paths, filename_mode=filename_mode)
            index[(collection_key, name)] = {
                "collection": collection_key,
                "name": name,
                "json_pointer": f"/{collection_key}/{idx}",
                "markdown_path": path,
                "obsidian_link": obsidian_link(name),
                "item": item,
            }
    return index


def add_edge(edges: List[Dict[str, Any]], source: Dict[str, Any], field: str, target_collection: str, target_name: str, target: Optional[Dict[str, Any]]) -> None:
    if not target_name:
        return
    edges.append({
        "source_collection": source["collection"],
        "source_name": source["name"],
        "source_markdown_path": source["markdown_path"],
        "field": field,
        "target_collection": target_collection,
        "target_name": target_name,
        "target_markdown_path": target["markdown_path"] if target else "",
        "target_exists": target is not None,
    })


def extract_links_for_item(concept: Dict[str, Any], concept_index: Dict[Tuple[str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    item = concept["item"]
    collection_key = concept["collection"]
    edges: List[Dict[str, Any]] = []

    for field, target_collection, kind in STRUCTURAL_REFS.get(collection_key, []):
        value = item.get(field)
        if kind == "scalar":
            target_name = value.strip() if isinstance(value, str) else ""
            add_edge(edges, concept, field, target_collection, target_name, concept_index.get((target_collection, target_name)))
        elif kind == "list":
            for target_name in value or []:
                if isinstance(target_name, str):
                    clean = target_name.strip()
                    add_edge(edges, concept, field, target_collection, clean, concept_index.get((target_collection, clean)))
        elif kind == "relation_list":
            for relation in value or []:
                if isinstance(relation, str) and ":" in relation:
                    rel_type, target_name = relation.split(":", 1)
                    clean = target_name.strip()
                    add_edge(edges, concept, f"{field}:{rel_type.strip()}", target_collection, clean, concept_index.get((target_collection, clean)))

    detail = item.get("详情", {})
    if isinstance(detail, dict):
        for key, value in detail.items():
            values = value if isinstance(value, list) else [value]
            for raw in values:
                if not isinstance(raw, str):
                    continue
                match = DETAIL_REF_RE.match(raw.strip())
                if not match:
                    continue
                ref_type, target_name = match.group(1), match.group(2).strip()
                target_collection = TYPE_TO_COLLECTION[ref_type]
                add_edge(edges, concept, f"详情.{key}", target_collection, target_name, concept_index.get((target_collection, target_name)))
    return edges


def render_link_list(edges: List[Dict[str, Any]], field_prefix: str) -> List[str]:
    rows = []
    for edge in edges:
        if edge["field"] == field_prefix or edge["field"].startswith(field_prefix + ":"):
            if edge["target_exists"]:
                label = obsidian_link(edge["target_name"])
            else:
                label = edge["target_name"]
            if edge["field"] != field_prefix:
                relation = edge["field"].split(":", 1)[1]
                rows.append(f"- {label}（{relation}）")
            else:
                rows.append(f"- {label}")
    return rows


def render_detail_section(detail: Any) -> List[str]:
    if not isinstance(detail, dict) or not detail:
        return []
    lines = ["## 详情", ""]
    for key, value in detail.items():
        values = readable_detail_value(value)
        if not values:
            continue
        lines.append(f"### {key}")
        for entry in values:
            match = DETAIL_REF_RE.match(entry.strip())
            if match:
                entry = obsidian_link(match.group(2).strip())
            lines.append(f"- {entry}")
        lines.append("")
    return lines


def render_concept_markdown(concept: Dict[str, Any], edges: List[Dict[str, Any]]) -> str:
    item = concept["item"]
    collection_key = concept["collection"]
    info = COLLECTION_INFO[collection_key]
    detail = item.get("详情", {}) if isinstance(item.get("详情"), dict) else {}
    title = concept["name"]
    fm = {
        "type": info["type"],
        "collection": collection_key,
        "name": title,
        "aliases": yaml_list(item.get("别名", [])),
        "tags": yaml_list(item.get("标签集", [])),
        "group": item.get("分组", ""),
        "first_chapter": detail.get("首次章节", ""),
        "recent_chapter": detail.get("最近章节", ""),
        "involved_chapters": detail.get("涉及章节", ""),
        "source_json": f"story-json://{collection_key}/{title}",
        "json_pointer": concept["json_pointer"],
    }
    lines = [frontmatter(fm), "", f"# {title}", ""]
    intro = item.get("介绍", "")
    if intro:
        lines.extend(["## 简介", "", str(intro).strip(), ""])

    standard_sections = [
        ("所属阵营", render_link_list(edges, "所属阵营")),
        ("关系", render_link_list(edges, "关系")),
        ("发生地点", render_link_list(edges, "发生地点")),
        ("参与成员", render_link_list(edges, "参与成员")),
        ("目标事件", render_link_list(edges, "目标事件")),
        ("父级地点", render_link_list(edges, "父级地点")),
        ("涉及事件", render_link_list(edges, "涉及事件")),
        ("父级阵营", render_link_list(edges, "父级阵营")),
        ("座落地点", render_link_list(edges, "座落地点")),
        ("详情引用", [f"- {obsidian_link(e['target_name']) if e['target_exists'] else e['target_name']}" for e in edges if e["field"].startswith("详情.")]),
    ]
    for heading, rows in standard_sections:
        if rows:
            lines.extend([f"## {heading}", "", *rows, ""])

    tags = yaml_list(item.get("标签集", []))
    if tags:
        lines.extend(["## 属性标签", "", *[f"- {tag}" for tag in tags], ""])

    lines.extend(render_detail_section(detail))
    return "\n".join(lines).rstrip() + "\n"


def render_home(story: Dict[str, Any], mapping: Dict[str, Any], include_dataview: bool = False, include_canvas: bool = False) -> str:
    intro = story.get("介绍", {}) if isinstance(story.get("介绍"), dict) else {}
    title = intro.get("标题") or "故事结构"
    lines = [
        f"# {title}",
        "",
        intro.get("描述", ""),
        "",
        "## 索引",
        "",
    ]
    for link in OVERVIEW_LINKS:
        if link == "故事总览":
            continue
        count = 0
        for collection_key in COLLECTION_KEYS:
            if Path(COLLECTION_INFO[collection_key]["index"]).stem == link:
                count = sum(1 for concept in mapping["concepts"] if concept["collection"] == collection_key)
                break
        suffix = f"：{count}" if count else ""
        lines.append(f"- [[{link}]]{suffix}")
    if include_dataview:
        lines.extend([
            "",
            "## Dataview 查询",
            "",
            "- [[查询-主角]]",
            "- [[查询-未回收线索]]",
            "- [[查询-按章节事件]]",
            "- [[查询-按阵营角色]]",
        ])
    if include_canvas:
        lines.extend(["", "## Canvas", "", "- [[故事图谱.canvas|故事图谱]]"])
    lines.extend(["", "## 元数据", "", "- `_meta/story_markdown_map.json`", "- `_meta/graph_edges.json`"])
    return "\n".join(lines).rstrip() + "\n"


def render_collection_index(collection_key: str, concepts: List[Dict[str, Any]]) -> str:
    info = COLLECTION_INFO[collection_key]
    title = "时间线" if collection_key == "事件集" else f"{info['type']}索引"
    lines = [f"# {title}", ""]
    for concept in concepts:
        item = concept["item"]
        group = item.get("分组", "")
        if collection_key == "事件集":
            time = item.get("时间", "")
            involved = item.get("详情", {}).get("涉及章节", "") if isinstance(item.get("详情"), dict) else ""
            parts = [part for part in [time, involved, group] if part]
            suffix = f" - {' / '.join(parts)}" if parts else ""
        else:
            suffix = f" - {group}" if group else ""
        lines.append(f"- {concept['obsidian_link']}{suffix}")
    return "\n".join(lines).rstrip() + "\n"


def render_dataview_query(name: str, body: str) -> str:
    return body.rstrip() + "\n"


def render_canvas(concepts: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    nodes = []
    node_ids: Dict[str, str] = {}
    columns = {key: i for i, key in enumerate(COLLECTION_KEYS)}
    counters: Dict[str, int] = {key: 0 for key in COLLECTION_KEYS}
    for idx, concept in enumerate(concepts):
        node_id = f"node-{idx}"
        node_ids[concept["markdown_path"]] = node_id
        column = columns.get(concept["collection"], 0)
        row = counters.get(concept["collection"], 0)
        counters[concept["collection"]] = row + 1
        nodes.append({
            "id": node_id,
            "type": "file",
            "file": concept["markdown_path"],
            "x": column * 360,
            "y": row * 180,
            "width": 300,
            "height": 120,
        })
    canvas_edges = []
    for idx, edge in enumerate(edges):
        from_id = node_ids.get(edge["source_markdown_path"])
        to_id = node_ids.get(edge["target_markdown_path"])
        if not from_id or not to_id:
            continue
        canvas_edges.append({
            "id": f"edge-{idx}",
            "fromNode": from_id,
            "fromSide": "right",
            "toNode": to_id,
            "toSide": "left",
            "label": edge["field"],
        })
    return {"nodes": nodes, "edges": canvas_edges}


def render_single_markdown(story: Dict[str, Any], concepts: List[Dict[str, Any]], edges_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]]) -> str:
    intro = story.get("介绍", {}) if isinstance(story.get("介绍"), dict) else {}
    title = intro.get("标题") or "故事结构"
    lines = [f"# {title}", ""]
    if intro.get("描述"):
        lines.extend([str(intro["描述"]).strip(), ""])
    for collection_key in COLLECTION_KEYS:
        section_concepts = [c for c in concepts if c["collection"] == collection_key]
        if not section_concepts:
            continue
        lines.extend([f"## {collection_key}", ""])
        for concept in section_concepts:
            item = concept["item"]
            lines.extend([f"### {concept['obsidian_link']}", ""])
            if item.get("介绍"):
                lines.extend([str(item["介绍"]).strip(), ""])
            tags = yaml_list(item.get("标签集", []))
            if tags:
                lines.append("标签：" + "，".join(tags))
                lines.append("")
            link_rows = []
            for edge in edges_by_key.get((concept["collection"], concept["name"]), []):
                link_rows.append(f"- {edge['field']} -> {obsidian_link(edge['target_name']) if edge['target_exists'] else edge['target_name']}")
            if link_rows:
                lines.extend(["关系：", *link_rows, ""])
    return "\n".join(lines).rstrip() + "\n"


def export_obsidian_markdown(
    story_path: Path | str,
    out_dir: Path | str,
    *,
    single_file: Optional[Path | str] = None,
    clean: bool = False,
    include_dataview: bool = False,
    include_canvas: bool = False,
    filename_mode: str = "name",
) -> Dict[str, Any]:
    story_path = Path(story_path)
    out_dir = Path(out_dir)
    single_file_path = Path(single_file) if single_file else None
    story = load_json(story_path)

    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if filename_mode not in {"name", "type-name"}:
        raise ValueError("filename_mode must be 'name' or 'type-name'")

    concept_index = build_concept_index(story, filename_mode=filename_mode)
    concepts = list(concept_index.values())
    concepts.sort(key=lambda c: (COLLECTION_KEYS.index(c["collection"]), c["json_pointer"]))

    mapping = {
        "export_type": "obsidian_vault",
        "vault_layout": "obsidian_vault",
        "source_file": str(story_path),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "dataview_enabled": include_dataview,
        "canvas_enabled": include_canvas,
        "filename_mode": filename_mode,
        "concepts": [],
    }
    all_edges: List[Dict[str, Any]] = []
    edges_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for concept in concepts:
        edges = extract_links_for_item(concept, concept_index)
        edges_by_key[(concept["collection"], concept["name"])] = edges
        all_edges.extend(edges)
        mapping["concepts"].append({
            "collection": concept["collection"],
            "type": COLLECTION_INFO[concept["collection"]]["type"],
            "name": concept["name"],
            "json_pointer": concept["json_pointer"],
            "markdown_path": concept["markdown_path"],
            "obsidian_link": concept["obsidian_link"],
            "source_json": f"story-json://{concept['collection']}/{concept['name']}",
            "tags": yaml_list(concept["item"].get("标签集", [])),
            "links_out": edges,
        })

    for concept in concepts:
        markdown = render_concept_markdown(concept, edges_by_key[(concept["collection"], concept["name"])])
        target = out_dir / concept["markdown_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")

    for collection_key in COLLECTION_KEYS:
        info = COLLECTION_INFO[collection_key]
        section = [concept for concept in concepts if concept["collection"] == collection_key]
        index_path = out_dir / info["index"]
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(render_collection_index(collection_key, section), encoding="utf-8")

    overview_dir = out_dir / "00-总览"
    overview_dir.mkdir(parents=True, exist_ok=True)
    (overview_dir / "故事总览.md").write_text(render_home(story, mapping, include_dataview=include_dataview, include_canvas=include_canvas), encoding="utf-8")
    if include_dataview:
        dataview_dir = out_dir / "_dataview"
        dataview_dir.mkdir(parents=True, exist_ok=True)
        for name, body in DATAVIEW_QUERIES.items():
            (dataview_dir / name).write_text(render_dataview_query(name, body), encoding="utf-8")
    if include_canvas:
        dump_json(overview_dir / "故事图谱.canvas", render_canvas(concepts, all_edges))
    dump_json(out_dir / "_meta" / "story_markdown_map.json", mapping)
    dump_json(out_dir / "_meta" / "graph_edges.json", all_edges)

    if single_file_path:
        single_file_path.parent.mkdir(parents=True, exist_ok=True)
        single_file_path.write_text(render_single_markdown(story, concepts, edges_by_key), encoding="utf-8")

    report = {
        "source_file": str(story_path),
        "out_dir": str(out_dir),
        "single_file": str(single_file_path) if single_file_path else "",
        "concept_count": len(concepts),
        "edge_count": len(all_edges),
        "dataview_enabled": include_dataview,
        "canvas_enabled": include_canvas,
        "filename_mode": filename_mode,
    }
    dump_json(out_dir / "_meta" / "export_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export story structure JSON to an Obsidian vault.")
    parser.add_argument("input", help="故事结构.json path or project directory")
    parser.add_argument("--out", required=True, help="Output Obsidian vault directory")
    parser.add_argument("--source", choices=["final", "process"], default="final", help="When input is a project directory, choose 故事结构.json or 故事结构_增量.json")
    parser.add_argument("--single-file", help="Also write one large Markdown file")
    parser.add_argument("--clean", action="store_true", help="Remove output directory before export")
    parser.add_argument("--include-dataview", action="store_true", help="Write _dataview query notes for the Dataview plugin")
    parser.add_argument("--include-canvas", action="store_true", help="Write 00-总览/故事图谱.canvas")
    parser.add_argument("--filename-mode", choices=["name", "type-name"], default="name", help="Use bare names or type-prefixed names for object pages")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    story_path = resolve_story_path(Path(args.input), args.source)
    report = export_obsidian_markdown(
        story_path,
        Path(args.out),
        single_file=args.single_file,
        clean=args.clean,
        include_dataview=args.include_dataview,
        include_canvas=args.include_canvas,
        filename_mode=args.filename_mode,
    )
    print("=== Obsidian Vault 导出完成 ===")
    print(f"源文件: {report['source_file']}")
    print(f"输出目录: {report['out_dir']}")
    if report["single_file"]:
        print(f"大 Markdown: {report['single_file']}")
    print(f"对象数: {report['concept_count']}")
    print(f"关系边: {report['edge_count']}")
    print(f"Dataview: {'yes' if report['dataview_enabled'] else 'no'}")
    print(f"Canvas: {'yes' if report['canvas_enabled'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
