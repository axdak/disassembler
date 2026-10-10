"""把拆书项目的最终结构、原文章节和分析态合成故事家导入 JSON。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


EXPORT_RELATIVE_PATH = Path("导出") / "故事家完整.json"


def _read_chapter_body(path: Path) -> str:
    """原文拆解文件首行是章节标题；故事家的章节名称单独存储。"""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("# "):
        return "".join(lines[1:]).lstrip("\r\n")
    return text


def build_storyer_document(project_dir: Path) -> tuple[dict, int]:
    """按索引顺序合并，不修改拆书引擎维护的故事结构文件。"""
    root = Path(project_dir)
    structure = json.loads((root / "故事结构.json").read_text(encoding="utf-8-sig"))
    index = json.loads((root / "原文拆解" / "_索引.json").read_text(encoding="utf-8-sig"))
    if not isinstance(structure, dict) or not isinstance(index.get("chapters"), list):
        raise ValueError("故事结构或章节索引格式错误")
    if not isinstance(structure.get("介绍"), dict):
        raise ValueError("故事结构缺少介绍")

    chapters = []
    missing_analysis = 0
    seen_sequences = set()
    for row in index["chapters"]:
        if not isinstance(row, dict):
            raise ValueError("章节索引含无效记录")
        sequence = row.get("seq")
        filename = row.get("filename")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1 or sequence in seen_sequences:
            raise ValueError(f"章节序号无效或重复：{sequence}")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError(f"章节文件名无效：{filename}")
        seen_sequences.add(sequence)
        title = str(row.get("title") or row.get("source_title") or f"第{sequence}章").strip()
        if not title:
            raise ValueError(f"第{sequence}章缺少标题")
        source = root / "原文拆解" / filename
        if not source.is_file():
            raise FileNotFoundError(f"第{sequence}章原文不存在：{source}")
        analysis_path = root / "章节处理" / filename
        analysis = analysis_path.read_text(encoding="utf-8-sig") if analysis_path.is_file() else ""
        missing_analysis += not bool(analysis.strip())
        chapters.append({
            "序号": sequence,
            "名称": title,
            "正文": _read_chapter_body(source),
            "分析态": analysis,
        })

    structure["章节集"] = sorted(chapters, key=lambda item: item["序号"])
    return structure, missing_analysis


def export_storyer_document(project_dir: Path) -> dict:
    """只写独立导出文件，使用同目录临时文件避免产生半成品。"""
    root = Path(project_dir)
    document, missing_analysis = build_storyer_document(root)
    destination = root / EXPORT_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".故事家完整-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(destination), "chapter_count": len(document["章节集"]),
            "missing_analysis": missing_analysis}
