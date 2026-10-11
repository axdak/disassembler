"""Task-scoped, read-only MCP context: only immutable supplied materials exist.

The agent receives logical source paths, while this server reads private snapshots.
It exposes no filesystem search, command execution, network, or write capability.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TEXT_SUFFIXES = {".md", ".json", ".txt", ".csv", ".tsv"}
SOURCE_ROOT = Path(__file__).resolve().parents[2]
READ_LIMIT = 24000


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


def serve(manifest: Path) -> None:
    """Serve the MCP JSON-RPC stdio subset needed for these two read-only tools."""
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
                result = {"tools": [
                    {"name": "list_context", "description": "列出本任务可读取的材料路径。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                    {"name": "read_context", "description": "只读任务材料。大文件按 next_offset 续读直至 null，禁止遗漏材料。",
                     "inputSchema": {"type": "object", "required": ["path"], "properties": {
                         "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                         "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT}}, "additionalProperties": False}},
                ]}
            elif method == "tools/call":
                try:
                    if params.get("name") == "list_context":
                        value = {"paths": [item["source"] for item in reader.files]}
                    elif params.get("name") == "read_context":
                        value = reader.read(**params.get("arguments", {}))
                    else:
                        raise ValueError("任务只支持 list_context/read_context，不允许其他操作")
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
    arguments = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(arguments.manifest)
