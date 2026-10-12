"""Task-scoped, read-only MCP context: only immutable supplied materials exist.

The agent receives logical source paths, while this server reads private snapshots.
It exposes no filesystem search, command execution, network, or write capability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

TEXT_SUFFIXES = {".md", ".json", ".txt", ".csv", ".tsv"}
SOURCE_ROOT = Path(__file__).resolve().parents[2]
READ_LIMIT = 24000
COLLECTION_KEYS = ("角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集")


def referenced_paths(text: str) -> set[str]:
    """Recognize rendered Markdown and JSON path references, never execute them."""
    values = set(re.findall(r"`([^`\r\n]+)`", text))
    values.update(re.findall(r'(?:[A-Za-z]:[\\/]|/)[^\r\n`"<>]*?\.(?:md|json|txt|csv|tsv)(?=$|[\s`"，。；;)])', text))
    for token in re.findall(r'"(?:\\.|[^"\\])*"', text):
        try:
            value = json.loads(token)
        except ValueError:
            continue
        if isinstance(value, str) and Path(value).suffix.lower() in TEXT_SUFFIXES:
            values.add(value)
    return values


def snapshot_context(project: Path, pack: Path, outputs: list[Path], directory: Path) -> Path:
    """Freeze explicit inputs plus referenced in-project text; reject code and links out.

    Host-declared inputs can be frozen siblings of a capacity request directory.
    Logical original paths map to those frozen copies, never live source files.
    Follow material catalogs but not arbitrary paths in story JSON/novel prose.
    """
    project, pack = project.resolve(), pack.resolve()
    declared = pack.with_suffix(".context-inputs.json")
    aliases = {}
    if declared.is_file():
        entries = json.loads(declared.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("宿主材料清单必须是数组")
        for entry in entries:
            if isinstance(entry, str):
                source = snapshot = Path(entry).resolve()
            elif isinstance(entry, dict) and set(entry) == {"source", "snapshot"}:
                source, snapshot = Path(entry["source"]).resolve(), Path(entry["snapshot"]).resolve()
            else:
                raise ValueError("宿主材料清单条目格式错误")
            if snapshot.suffix.lower() in TEXT_SUFFIXES:
                aliases[source] = snapshot
    pending = [pack, *(path.resolve() for path in outputs if path.is_file()), *aliases]
    seen: set[Path] = set()
    files = []
    while pending:
        source = pending.pop()
        actual = aliases.get(source, source)
        if source in seen or not actual.is_file():
            continue
        seen.add(source)
        content = actual.read_text(encoding="utf-8")
        snapshot = directory / f"context_{len(files):04d}.txt"
        snapshot.write_text(content, encoding="utf-8")
        files.append({"source": str(source), "snapshot": str(snapshot)})
        catalog = (source.name in {"materials.json", "references.json", "sources.json", "manifest.json"}
                   or source.name.endswith((".sources.json", ".references.json", ".hierarchy.json")))
        if (not catalog and (source.suffix.lower() != ".md" or "原文拆解" in source.parts
                             or source.parent.name == "章节处理")):
            continue
        for value in referenced_paths(content):
            candidate = Path(value.replace("\\", os.sep)).expanduser()
            if not candidate.is_absolute():
                candidate = (source.parent if catalog else project) / candidate
            candidate = candidate.resolve()
            if candidate in aliases:
                pending.append(candidate)
                continue
            if (candidate.suffix.lower() not in TEXT_SUFFIXES or not candidate.is_relative_to(project)
                    or candidate.is_relative_to(SOURCE_ROOT)
                    or any(part.startswith(".") for part in candidate.relative_to(project).parts)
                    or candidate.name.upper() in {"AGENTS.MD", "CLAUDE.MD", "PACKAGE.JSON"}):
                continue
            pending.append(candidate)
    manifest = directory / "context_manifest.json"
    manifest.write_text(json.dumps({"project": str(project), "files": files}, ensure_ascii=False), encoding="utf-8")
    return manifest


class ContextReader:
    """Resolve exact manifest paths only; live source files are never read by tools."""

    def __init__(self, manifest: Path):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.project = Path(data["project"])
        self.files = data["files"]
        self.allowed = {self.key(Path(item["source"])): Path(item["snapshot"]) for item in self.files}

    @staticmethod
    def key(path: Path) -> str:
        """Match Windows aliases without making POSIX filenames case-insensitive."""
        return os.path.normcase(str(path.resolve()))

    def read(self, path: str, offset: int = 0, limit: int = READ_LIMIT) -> dict:
        """Page through a supplied snapshot; reject traversal and unknown paths."""
        if not isinstance(path, str) or ".." in path.replace("\\", "/").split("/"):
            raise ValueError("只允许读取任务材料清单中的确切路径")
        candidate = Path(path.replace("\\", os.sep))
        if not candidate.is_absolute():
            candidate = self.project / candidate
        snapshot = self.allowed.get(self.key(candidate))
        if snapshot is None:
            raise ValueError("该路径不在任务材料清单中；禁止读取项目代码或额外文件")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 < limit <= READ_LIMIT:
            raise ValueError(f"offset 必须非负；limit 必须为 1..{READ_LIMIT}")
        text = snapshot.read_text(encoding="utf-8")
        end = min(len(text), offset + limit)
        print(f"worker context read: {candidate.name} offset={offset} end={end} total={len(text)}", file=sys.stderr, flush=True)
        return {"path": path, "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None}

    def _story_snapshot(self) -> tuple[Path, Path, dict]:
        """Load the frozen authoritative story, preferring a final-draft candidate."""
        candidates = []
        for item in self.files:
            source = Path(item["source"])
            if source.name in {"故事结构_草稿.json", "故事结构_增量.json"}:
                priority = 0 if source.name == "故事结构_草稿.json" else 1
                candidates.append((priority, source, Path(item["snapshot"])))
        if not candidates:
            raise ValueError("本任务没有登记可查询的故事结构快照")
        _, source, snapshot = sorted(candidates, key=lambda value: value[0])[0]
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("故事结构快照不是 JSON 对象")
        return source, snapshot, data

    @staticmethod
    def _unit_view(collection: str, item: dict, level: str) -> dict:
        if level == "L2":
            return {"collection": collection, "record": item}
        keys = ("名称", "别名") if level == "L0" else (
            "名称", "别名", "介绍", "分组", "标签集", "首次涉及章节", "最近涉及章节"
        )
        return {"collection": collection, **{key: item[key] for key in keys if key in item}}

    def story_snapshot(self) -> dict:
        source, snapshot, data = self._story_snapshot()
        raw = snapshot.read_bytes()
        return {
            "source": str(source),
            "revision": hashlib.sha256(raw).hexdigest(),
            "introduction": data.get("介绍", {}),
            "counts": {key: len(data.get(key, []) or []) for key in COLLECTION_KEYS},
        }

    def story_search(self, query: str, collection: str = "", limit: int = 20, cursor: int = 0) -> dict:
        """Search frozen current records; return bounded L1 results and a cursor."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须是非空字符串")
        if collection and collection not in COLLECTION_KEYS:
            raise ValueError("collection 不是受支持的故事集合")
        if type(limit) is not int or not 1 <= limit <= 50 or type(cursor) is not int or cursor < 0:
            raise ValueError("limit 必须为 1..50，cursor 必须为非负整数")
        _, _, data = self._story_snapshot()
        found = []
        for key in ([collection] if collection else COLLECTION_KEYS):
            for item in data.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                searchable = json.dumps({field: item.get(field) for field in ("名称", "别名", "介绍", "标签集", "分组")}, ensure_ascii=False)
                if query in searchable:
                    found.append(self._unit_view(key, item, "L1"))
        page = found[cursor:cursor + limit]
        end = cursor + len(page)
        return {"items": page, "total": len(found), "next_cursor": end if end < len(found) else None}

    def story_get_units(self, refs: list, level: str = "L1") -> dict:
        """Read exact formal records by collection/name from the frozen story."""
        if not isinstance(refs, list) or not 1 <= len(refs) <= 50 or level not in {"L0", "L1", "L2"}:
            raise ValueError("refs 必须含 1..50 项，level 必须为 L0/L1/L2")
        _, _, data = self._story_snapshot()
        items, missing = [], []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"collection", "name"}:
                raise ValueError("每个 ref 只能包含 collection 和 name")
            collection, name = ref["collection"], ref["name"]
            if collection not in COLLECTION_KEYS or not isinstance(name, str) or not name.strip():
                raise ValueError("ref 的 collection/name 无效")
            match = next((item for item in data.get(collection, []) or [] if isinstance(item, dict)
                          and (item.get("名称") == name or name in (item.get("别名", []) or []))), None)
            if match is None:
                missing.append(ref)
            else:
                items.append(self._unit_view(collection, match, level))
        return {"items": items, "missing": missing}

    def _chapter_files(self) -> list[tuple[Path, Path]]:
        result = []
        for item in self.files:
            source = Path(item["source"])
            if source.suffix.lower() == ".md" and "原文拆解" in source.parts:
                result.append((source, Path(item["snapshot"])))
        return sorted(result, key=lambda value: value[0].name)

    def story_query_chapters(self, mode: str, query: str = "", chapter: str = "",
                             offset: int = 0, limit: int = 20) -> dict:
        """Index, search, or page only frozen chapter sources registered for this task."""
        chapters = self._chapter_files()
        if mode == "index":
            return {"chapters": [source.name for source, _ in chapters]}
        if mode == "search":
            if not query or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("search 需要 query，limit 必须为 1..20")
            matches = []
            for source, snapshot in chapters:
                text = snapshot.read_text(encoding="utf-8")
                start = 0
                while len(matches) < limit:
                    pos = text.find(query, start)
                    if pos < 0:
                        break
                    matches.append({"chapter": source.name, "offset": pos,
                                    "excerpt": text[max(0, pos - 120):pos + len(query) + 120]})
                    start = pos + len(query)
            return {"matches": matches, "limited": len(matches) == limit}
        if mode == "read":
            if not chapter or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= READ_LIMIT:
                raise ValueError(f"read 需要 chapter；offset 非负；limit 为 1..{READ_LIMIT}")
            match = next(((source, snapshot) for source, snapshot in chapters
                          if source.name == chapter or source.stem == chapter), None)
            if match is None:
                raise ValueError("章节不在本任务冻结材料范围内")
            source, snapshot = match
            text = snapshot.read_text(encoding="utf-8")
            end = min(len(text), offset + limit)
            return {"chapter": source.name, "text": text[offset:end], "total_chars": len(text),
                    "next_offset": end if end < len(text) else None}
        raise ValueError("mode 必须为 index/search/read")


def serve(manifest: Path, story_tools: bool = False) -> None:
    """Serve task snapshots and optional bounded, read-only story-domain queries."""
    reader = ContextReader(manifest)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or "id" not in request:
                continue
            method, params = request.get("method"), request.get("params", {})
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "nd-context", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                definitions = [
                    {"name": "list_context", "description": "列出本任务可读取的材料路径。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                    {"name": "read_context", "description": "只读任务材料。大文件按 next_offset 续读直至 null，禁止遗漏材料。",
                     "inputSchema": {"type": "object", "required": ["path"], "properties": {
                         "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                         "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT}}, "additionalProperties": False}},
                ]
                if story_tools:
                    definitions.extend([
                        {"name": "story_snapshot", "description": "读取冻结故事版本、简介与各集合计数，不返回完整故事。",
                         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                        {"name": "story_search", "description": "在当前正式故事档案中分页搜索名称、别名、介绍、标签和分组。",
                         "inputSchema": {"type": "object", "required": ["query"], "properties": {
                             "query": {"type": "string"}, "collection": {"type": "string"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                             "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
                        {"name": "story_get_units", "description": "按集合和正式名称读取当前档案；仅需要完整详情时使用 L2。",
                         "inputSchema": {"type": "object", "required": ["refs"], "properties": {
                             "refs": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object",
                                 "required": ["collection", "name"], "properties": {"collection": {"type": "string"}, "name": {"type": "string"}},
                                 "additionalProperties": False}},
                             "level": {"type": "string", "enum": ["L0", "L1", "L2"]}}, "additionalProperties": False}},
                        {"name": "story_query_chapters", "description": "仅在本任务冻结的章节范围内列目录、搜索或分页读取原文。",
                         "inputSchema": {"type": "object", "required": ["mode"], "properties": {
                             "mode": {"type": "string", "enum": ["index", "search", "read"]},
                             "query": {"type": "string"}, "chapter": {"type": "string"},
                             "offset": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT}}, "additionalProperties": False}},
                    ])
                result = {"tools": definitions}
            elif method == "tools/call":
                try:
                    if params.get("name") == "list_context":
                        value = {"paths": [item["source"] for item in reader.files]}
                    elif params.get("name") == "read_context":
                        value = reader.read(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_snapshot":
                        value = reader.story_snapshot()
                    elif story_tools and params.get("name") == "story_search":
                        value = reader.story_search(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_get_units":
                        value = reader.story_get_units(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_query_chapters":
                        value = reader.story_query_chapters(**params.get("arguments", {}))
                    else:
                        raise ValueError("该工具不在本任务只读能力范围内")
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                except (ValueError, TypeError, OSError) as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)
        except (ValueError, TypeError) as exc:
            print(f"Invalid MCP request: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Only read frozen task context via MCP")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--story-tools", action="store_true", help="启用只读、分页的故事领域查询")
    arguments = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(arguments.manifest, story_tools=arguments.story_tools)
