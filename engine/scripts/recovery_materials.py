"""Lossless, file-backed material sharing inside one recovery workspace.

Only complete, identical text blocks are replaced with references. Different
versions remain separate; neither prose summarization nor model judgment is used.
Each independent worker must read the necessary full sources once in its context.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from merge_delta import save_json


def rebase_material_paths(text: str, source: Path, target: Path) -> str:
    """Rebase once, preserving paths already inside the destination workspace.

    The destination is usually a child of source. Sequential str.replace calls
    would replace its prefix again, producing nonexistent doubled paths.
    """
    variants = lambda path: (str(path), path.as_posix(), json.dumps(str(path), ensure_ascii=False)[1:-1])
    replacements = dict(zip(variants(source), variants(target)))
    replacements.update({value: value for value in variants(target)})
    pattern = "|".join(re.escape(value) for value in sorted(replacements, key=len, reverse=True))
    return re.sub(pattern, lambda match: replacements[match[0]], text)


class RecoveryMaterials:
    """A per-attempt catalog; all references resolve to protected local files."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.entries: list[dict] = []
        self.blocks: dict[str, dict] = {}
        self.documents: list[dict] = []

    def add_source(self, path: Path, role: str) -> None:
        """Register immutable evidence; identical contents share one read target."""
        text = path.read_text(encoding="utf-8")
        # Only surrounding newline separators are excluded from matching. All
        # words, indentation and inner whitespace stay in the authoritative file.
        block = text.strip("\n")
        if not block:
            return
        existing = self.blocks.get(block)
        if existing:
            existing["roles"].append(role)
            return
        entry = {"id": f"M{len(self.entries) + 1:03d}", "path": str(path.relative_to(self.workspace)),
                 "roles": [role], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "chars": len(text)}
        self.entries.append(entry)
        self.blocks[block] = entry

    def reference(self, entry: dict) -> str:
        return f"[完整材料 {entry['id']}，须读全文且本次无需重复读取]({self.workspace / entry['path']})"

    def write_document(self, path: Path, text: str, role: str) -> None:
        """Replace only full line-bounded source blocks, longest first.

        The source text remains available in full. Small mentions inside prose
        cannot accidentally turn into document links. A reconstruction assertion
        checks every replacement before writing the compact navigation document.
        """
        rendered = text
        replacements = []
        for block, entry in sorted(self.blocks.items(), key=lambda item: len(item[0]), reverse=True):
            if block not in rendered:
                continue
            marker = self.reference(entry)
            if marker in text:
                continue  # Preserve user-authored links rather than reinterpret them.
            pattern = r"(?m)^" + re.escape(block) + r"(?=\n|$)"
            rendered, count = re.subn(pattern, lambda _: marker, rendered)
            if count:
                replacements.append((marker, block))
        restored = rendered
        for marker, block in reversed(replacements):
            restored = restored.replace(marker, block)
        if restored != text:
            raise ValueError("材料去重无法无损还原，拒绝改写任务要求")
        path.write_text(rendered, encoding="utf-8")
        self.documents.append({"path": str(path.relative_to(self.workspace)), "role": role,
                               "original_chars": len(text), "rendered_chars": len(rendered),
                               "lossless": True})
        # Both the original full document and its compact form may recur as
        # feedback/history. They refer to the same document, never duplicate it.
        self.add_source(path, role)
        if text.strip("\n"):
            self.blocks.setdefault(text.strip("\n"), self.blocks[rendered.strip("\n")])

    def save(self) -> Path:
        """Emit one reading index shared by repair, diagnosis and independent review."""
        path = self.workspace / "materials.json"
        save_json(path, {"instructions": "同一材料ID全文只读一次；引用不代表摘要或可省略内容。"
                        "独立复核是新的上下文，仍须读取原契约与完整事实证据，不得仅凭变更清单判断。",
                        "materials": self.entries, "documents": self.documents})
        return path

    def protected_paths(self) -> list[Path]:
        return [self.workspace / item["path"] for item in self.entries + self.documents]


def read_material_document(path: Path) -> str:
    """Expand our own generated references for lossless history transfer/tests.

    Only cataloged navigation documents are expanded. Novel text is always
    opaque data, even if it contains lookalike links. Old feedback is expanded
    before rebasing, so it cannot accidentally point to a newer error report.
    """
    root = path.parent.resolve()
    catalog_path = root / "materials.json"
    if not catalog_path.is_file():
        return path.read_text(encoding="utf-8")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = {item["id"]: item for item in catalog["materials"]}
    documents = {item["path"] for item in catalog["documents"]}
    cache = {}
    visiting = set()

    def expand(target):
        target = target.resolve()
        relative = str(target.relative_to(root))  # Reject paths outside the old attempt.
        if target in visiting:
            raise ValueError("材料引用成环，拒绝丢弃历史反馈")
        if target in cache:
            return cache[target]
        text = target.read_text(encoding="utf-8")
        visiting.add(target)
        if relative in documents:
            def replace(match):
                entry = entries.get(match[1])
                if not entry or match[2] != str(root / entry["path"]):
                    return match[0]
                return expand(root / entry["path"]).strip("\n")
            text = re.sub(r"(?m)^\[完整材料 (M\d+)，须读全文且本次无需重复读取\]\(([^\n]+)\)$", replace, text)
        visiting.remove(target)
        cache[target] = text
        return text

    return expand(path)


def write_change_index(reference: Path, candidate: Path, target: Path) -> None:
    """Locate changes without duplicating values; full artifacts stay authoritative."""
    changes = []

    def visit(old, new, path):
        if old == new:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in old or key not in new:
                    changes.append({"path": pointer, "change": "added" if key not in old else "removed"})
                else:
                    visit(old[key], new[key], pointer)
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (left, right) in enumerate(zip(old, new)):
                visit(left, right, f"{path}/{index}")
        else:
            changes.append({"path": path or "/", "change": "modified"})

    try:
        visit(json.loads(reference.read_text(encoding="utf-8")), json.loads(candidate.read_text(encoding="utf-8")), "")
    except (ValueError, UnicodeError):
        changes = [{"path": "/", "change": "compare_full_text"}]
    save_json(target, {"basis": "程序纠错后的参考版本与候选；本清单不替代完整证据或内容保全检查",
                       "reference": str(reference), "candidate": str(candidate), "changes": changes})
