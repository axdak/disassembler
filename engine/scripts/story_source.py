"""Choose one verified story authority for downstream analysis and its tools."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


STORY_NAMES = {"故事结构_增量.json", "故事结构_草稿.json", "故事结构.json"}


def select_analysis_story_source(project: Path) -> tuple[Path, bytes, str]:
    """Use final delivery only while all receipt-bound inputs remain unchanged.

    A file's existence, size or timestamp cannot prove a successful publication.
    Legacy receipts remain readable history but cannot override process facts.
    """
    project = project.resolve()
    process, draft, final = [project / name for name in (
        "故事结构_增量.json", "故事结构_草稿.json", "故事结构.json")]
    process_raw = process.read_bytes() if process.is_file() else b""
    receipt = project / "质量治理/最终审计/finalize_log.json"
    try:
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("交付回执必须为 JSON 对象")
        versions = {"process_sha256": process, "draft_sha256": draft, "final_sha256": final}
        if any(not isinstance(saved.get(key), str) or not saved[key] for key in versions):
            return process, process_raw, "旧交付回执缺少版本哈希，本任务使用当前过程库。"
        # Return the verified bytes themselves: a later file read could race an
        # external replacement and silently package a different, unverified final.
        raw = {"process_sha256": process_raw, "draft_sha256": draft.read_bytes(),
               "final_sha256": final.read_bytes()}
        if all(saved[key] == hashlib.sha256(raw[key]).hexdigest() for key in versions):
            return final, raw["final_sha256"], "最终交付回执与过程库、草稿、终稿版本一致。"
        return process, process_raw, "交付后输入或终稿已变化，本任务使用当前过程库。"
    except (OSError, ValueError, UnicodeError):
        return process, process_raw, "尚无可验证的最终交付回执，本任务使用当前过程库。"


def declared_story_source(project: Path, text: str, *, allowed_sources=()) -> Path | None:
    """Read the host's explicit story reference without inferring authority by name.

    The same declaration is consumed by frozen tools and checkpoint identities.
    Confined paths or host-declared frozen aliases prevent importing unrelated
    files while preserving nested capacity requests outside the book directory.
    """
    match = re.search(r"^当前权威故事档案：`([^`\r\n]+)`", text, re.MULTILINE)
    if match is None:
        return None
    source = Path(match[1])
    if not source.is_absolute():
        source = project / source
    source = source.resolve()
    registered = {Path(value).resolve() for value in allowed_sources}
    if source.name not in STORY_NAMES or (not source.is_relative_to(project.resolve())
                                          and source not in registered):
        raise ValueError("权威故事档案必须是项目内登记的故事结构文件")
    return source
