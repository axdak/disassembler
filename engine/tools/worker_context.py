"""Task-scoped, read-only MCP context: only immutable supplied materials exist.

The agent receives logical source paths, while this server reads private snapshots.
It exposes no filesystem search, command execution, network, or write capability.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# The standalone stdio process must resolve the same identity rules as commits.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from element_identity import build_lookup_index, find_index
from story_source import declared_story_source

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


def snapshot_context(project: Path, pack: Path, outputs: list[Path], directory: Path,
                     *, inline_prompt: str = "") -> Path:
    """Freeze explicit inputs plus referenced in-project text; reject code and links out.

    Host-declared inputs can be frozen siblings of a capacity request directory.
    Logical original paths map to those frozen copies, never live source files.
    Follow material catalogs but not arbitrary paths in story JSON/novel prose.
    """
    project, pack = project.resolve(), pack.resolve()
    pack_text = pack.read_text(encoding="utf-8")
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
    story_source = declared_story_source(project, pack_text, allowed_sources=aliases)
    story_version = re.search(r"^当前故事版本：([0-9a-f]{64})$", pack_text, re.MULTILINE)
    pending = [pack, *(path.resolve() for path in outputs if path.is_file()), *aliases]
    if story_source is not None:
        pending.append(story_source)
    seen: set[Path] = set()
    files = []
    # Match complete supplied text across Windows newline conventions only.
    # Partial quotations must never suppress the remaining evidence pages.
    inline = inline_prompt.replace("\r\n", "\n")
    while pending:
        source = pending.pop()
        actual = aliases.get(source, source)
        if source in seen or not actual.is_file():
            continue
        seen.add(source)
        # Hashes identify evidence bytes. Text-mode writes would translate LF
        # into CRLF on Windows and invalidate unchanged provider artifacts.
        raw = actual.read_bytes()
        if source == story_source and story_version is not None and (
                hashlib.sha256(raw).hexdigest() != story_version[1]):
            raise ValueError("权威故事档案版本变化，请重新生成分析任务包；未混用新旧事实")
        content = raw.decode("utf-8")
        snapshot = directory / f"context_{len(files):04d}.txt"
        snapshot.write_bytes(raw)
        entry = {"source": str(source), "snapshot": str(snapshot)}
        normalized = content.replace("\r\n", "\n").strip()
        if normalized and normalized in inline:
            entry.update(provided_in_prompt=True, total_chars=len(content),
                         sha256=hashlib.sha256(raw).hexdigest())
        files.append(entry)
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
    metadata = {"project": str(project), "files": files}
    if story_source is not None:
        metadata["story_source"] = str(story_source)
    manifest.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return manifest


class ContextReader:
    """Resolve exact manifest paths only; live source files are never read by tools."""

    def __init__(self, manifest: Path):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.project = Path(data["project"])
        self.files = data["files"]
        self.allowed = {self.key(Path(item["source"])): Path(item["snapshot"]) for item in self.files}
        self.inline = {self.key(Path(item["source"])): item for item in self.files
                       if item.get("provided_in_prompt") is True}
        self.story_source = Path(data["story_source"]) if data.get("story_source") else None
        # A reader owns one immutable session revision; caches never span tasks.
        self._story_cache = None
        self._story_revision = ""
        self._story_indexes = {}

    @staticmethod
    def key(path: Path) -> str:
        """Match Windows aliases without making POSIX filenames case-insensitive."""
        return os.path.normcase(str(path.resolve()))

    def read(self, path: str, offset: int = 0, limit: int = READ_LIMIT, reread: bool = False) -> dict:
        """Reuse inline evidence; page frozen text explicitly after context loss.

        Unknown paths and traversal remain forbidden even for a forced reread.
        """
        if not isinstance(path, str) or ".." in path.replace("\\", "/").split("/"):
            raise ValueError("只允许读取任务材料清单中的确切路径")
        candidate = Path(path.replace("\\", os.sep))
        if not candidate.is_absolute():
            candidate = self.project / candidate
        snapshot = self.allowed.get(self.key(candidate))
        if snapshot is None:
            raise ValueError("该路径不在任务材料清单中；禁止读取项目代码或额外文件")
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 0 < limit <= READ_LIMIT
                or type(reread) is not bool):
            raise ValueError(f"offset 必须非负；limit 必须为 1..{READ_LIMIT}")
        provided = self.inline.get(self.key(candidate))
        if provided is not None and not reread:
            print(f"worker context reused inline: {candidate.name}", file=sys.stderr, flush=True)
            return {"path": path, "provided_in_prompt": True, "total_chars": provided["total_chars"],
                    "sha256": provided["sha256"], "next_offset": None,
                    "instruction": "完整材料已在初始任务提示中提供，请直接使用；无需重复读取或分页。仅在上下文丢失后需要恢复全文时使用reread=true。"}
        text = snapshot.read_text(encoding="utf-8")
        end = min(len(text), offset + limit)
        print(f"worker context read: {candidate.name} offset={offset} end={end} total={len(text)}", file=sys.stderr, flush=True)
        return {"path": path, "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None}

    def _story_snapshot(self) -> tuple[Path, Path, dict]:
        """Parse the host-selected frozen archive once for this reader session."""
        if self._story_cache is not None:
            return self._story_cache
        if self.story_source is not None:
            source = self.story_source
            snapshot = self.allowed.get(self.key(source))
            if snapshot is None:
                raise ValueError("权威故事档案未登记到任务冻结材料")
        else:
            # Legacy final-refinement tasks prefer their draft. A final-only
            # manifest is explicit registration, never a probe of live files.
            candidates = []
            for item in self.files:
                source = Path(item["source"])
                priority = {"故事结构_草稿.json": 0, "故事结构_增量.json": 1, "故事结构.json": 2}.get(source.name)
                if priority is not None:
                    candidates.append((priority, source, Path(item["snapshot"])))
            if not candidates:
                raise ValueError("本任务没有登记可查询的故事结构快照")
            _, source, snapshot = min(candidates, key=lambda value: value[0])
        raw = snapshot.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("故事结构快照不是 JSON 对象")
        self._story_revision = hashlib.sha256(raw).hexdigest()
        self._story_cache = (source, snapshot, data)
        return self._story_cache

    def _story_index(self, collection: str):
        """Reuse one identity index per collection within the frozen revision."""
        _, _, data = self._story_snapshot()
        if collection not in self._story_indexes:
            self._story_indexes[collection] = build_lookup_index(data.get(collection, []) or [])
        return self._story_indexes[collection]

    @staticmethod
    def _unit_view(collection: str, item: dict, level: str) -> dict:
        if level == "L2":
            return {"collection": collection, "record": copy.deepcopy(item)}
        keys = ("名称", "别名") if level == "L0" else (
            "名称", "别名", "介绍", "分组", "标签集", "首次涉及章节", "最近涉及章节"
        )
        return {"collection": collection, **{key: copy.deepcopy(item[key]) for key in keys if key in item}}

    def story_snapshot(self) -> dict:
        source, snapshot, data = self._story_snapshot()
        return {
            "source": str(source),
            "revision": self._story_revision,
            "introduction": copy.deepcopy(data.get("介绍", {})),
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
                # Search historical detail too, but return only a bounded identity
                # view. A missing summary match must not mean a fact never existed.
                searchable = json.dumps(item, ensure_ascii=False)
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
            records = data.get(collection, []) or []
            # Canonical names outrank aliases; ambiguous matches are actionable
            # tool errors, never an arbitrary record selected by storage order.
            names, aliases = self._story_index(collection)
            index = find_index(names, aliases, name)
            if index < 0:
                missing.append(ref)
            else:
                items.append(self._unit_view(collection, records[index], level))
        result = {"items": items, "missing": missing}
        if len(json.dumps(result, ensure_ascii=False)) > READ_LIMIT:
            raise ValueError("档案超过单次回包额度；请使用 story_read_fields 按字段分页读取，数据未截断")
        return result

    def story_read_fields(self, collection: str, name: str, fields: list | None = None,
                          offset: int = 0, limit: int = 8000) -> dict:
        """Page exact JSON field values; an empty selection returns a field index.

        Offsets address the serialized selection, not the live file. JSON Pointer
        escaping permits literal slash/tilde keys without interpreting file paths.
        Every response carries its frozen story revision for explicit provenance.
        """
        if (collection not in COLLECTION_KEYS or not isinstance(name, str) or not name.strip()
                or type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= READ_LIMIT or fields is not None and (
                    not isinstance(fields, list) or len(fields) > 50
                    or any(not isinstance(field, str) or not field.startswith("/") for field in fields))):
            raise ValueError("需要有效集合/名称、最多50个字段路径；offset非负，limit为1..24000")
        _, snapshot, data = self._story_snapshot()
        records = data.get(collection, []) or []
        names, aliases = self._story_index(collection)
        index = find_index(names, aliases, name)
        if index < 0:
            raise ValueError("正式档案不存在；检索其他集合或核对原文，不能据此猜测新增")
        record = records[index]
        if not fields:
            def pointers(value, prefix=""):
                for key, child in value.items():
                    path = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
                    yield path
                    if isinstance(child, dict):
                        yield from pointers(child, path)
            selected = {"fields": list(pointers(record))}
        else:
            selected = {}
            for field in fields:
                value = record
                for part in field[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if not isinstance(value, dict) or part not in value:
                        raise ValueError(f"字段不存在：{field}")
                    value = value[part]
                selected[field] = value
        text = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
        if offset > len(text):
            raise ValueError("offset 超过所选内容长度")
        end = min(len(text), offset + limit)
        return {"collection": collection, "name": record["名称"],
                "revision": self._story_revision,
                "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None}

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


def serve(manifest: Path, story_tools: bool = False, candidate_tools: bool = False) -> None:
    """Serve frozen reads and host-authorized candidate edits; never live writes."""
    reader = ContextReader(manifest)
    candidate, budget = None, None
    if candidate_tools:
        from candidate_session import CandidateSession
        from controlled_runtime import RuntimeBudget
        config = json.loads(manifest.read_text(encoding="utf-8"))["candidate"]
        frozen = reader.allowed[reader.key(Path(config["story"]))]
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != config["base_revision"]:
            raise ValueError("故事版本变化，拒绝启动候选工具")
        candidate = CandidateSession(Path(config["directory"]), frozen, config["task"], config["chapter"])
        budget = RuntimeBudget(Path(config["directory"]))
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
                    {"name": "list_context", "description": "只在任务提示未给出材料路径时列目录；已内嵌的完整材料无需查询。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                    {"name": "read_context", "description": "按需读取提示中未提供的冻结材料。已内嵌全文只返回复用提示；其他大文件按next_offset续读至null。",
                     "inputSchema": {"type": "object", "required": ["path"], "properties": {
                         "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                          "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT},
                          "reread": {"type": "boolean", "description": "仅在上下文丢失后恢复已内嵌全文；正常处理不要使用。"}}, "additionalProperties": False}},
                ]
                if story_tools:
                    definitions.extend([
                        {"name": "story_snapshot", "description": "读取冻结故事版本、简介与各集合计数，不返回完整故事。",
                         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                        {"name": "story_search", "description": "在冻结档案全部字段及历史详情中搜索，分页返回身份摘要；未命中不代表原文不存在该事实。",
                         "inputSchema": {"type": "object", "required": ["query"], "properties": {
                             "query": {"type": "string"}, "collection": {"type": "string"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                             "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
                        {"name": "story_read_fields", "description": "按集合/正式名读取字段目录或选定字段；fields为空返回目录，字段为JSON Pointer，如/详情/0001-状态。text按next_offset续读，完整JSON重组后解释；历史查询不改写原件。",
                         "inputSchema": {"type": "object", "required": ["collection", "name"], "properties": {
                             "collection": {"type": "string"}, "name": {"type": "string"},
                             "fields": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
                             "offset": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT}}, "additionalProperties": False}},
                        {"name": "story_get_units", "description": "按集合和正式名称批量读取档案；大对象使用story_read_fields分页，不能跳过历史核对。",
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
                if candidate is not None:
                    from controlled_runtime import candidate_definitions
                    definitions.extend(candidate_definitions())
                result = {"tools": definitions}
            elif method == "tools/call":
                try:
                    if budget is not None:
                        from context_budget import estimate_tokens
                        budget.tool(estimate_tokens(json.dumps(params, ensure_ascii=False)))
                    if params.get("name") == "list_context":
                        value = {"paths": [item["source"] for item in reader.files],
                                 "provided_in_prompt": [item["source"] for item in reader.files
                                                        if item.get("provided_in_prompt") is True]}
                    elif params.get("name") == "read_context":
                        value = reader.read(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_snapshot":
                        value = reader.story_snapshot()
                    elif story_tools and params.get("name") == "story_search":
                        value = reader.story_search(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_get_units":
                        value = reader.story_get_units(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_read_fields":
                        value = reader.story_read_fields(**params.get("arguments", {}))
                    elif story_tools and params.get("name") == "story_query_chapters":
                        value = reader.story_query_chapters(**params.get("arguments", {}))
                    elif candidate is not None and params.get("name") in {
                            "candidate_status", "candidate_read", "candidate_submit", "candidate_stage", "candidate_patch",
                            "candidate_validate", "candidate_finish"}:
                        function = getattr(candidate, params["name"].removeprefix("candidate_"))
                        value = function(**params.get("arguments", {}))
                    else:
                        raise ValueError("该工具不在本任务只读能力范围内")
                    text = json.dumps(value, ensure_ascii=False)
                    if budget is not None:
                        if len(text) > 32000:
                            raise ValueError("回包超过额度；请缩小批量、字段或分页范围。未截断原始数据。")
                        budget.charge(estimate_tokens(text))
                    result = {"content": [{"type": "text", "text": text}]}
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
    parser.add_argument("--candidate-tools", action="store_true", help="启用宿主登记的候选操作，禁止写正式结构")
    arguments = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(arguments.manifest, story_tools=arguments.story_tools, candidate_tools=arguments.candidate_tools)
