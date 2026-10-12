"""Chapter splitting must reject duplicate headers and recognize colloquial numerals."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from split_chapters import split_novel  # noqa: E402
from run_pipeline import preflight_chapter_source, validate_chapter_source  # noqa: E402


def test_split_handles_liang_hundreds_and_duplicate_consecutive_titles(tmp_path):
    source = tmp_path / "novel.txt"
    output = tmp_path / "split"
    source.write_text(
        "第两百三十一章 修补与强化\n"
        "第两百三十一章 修补与强化\n"
        "这是第一章正文。\n"
        "------------\n"
        "第两百三十二章 青莲地心火\n"
        "第两百三十二章 青莲地心火\n"
        "这是第二章正文。\n",
        encoding="utf-8",
    )

    index = split_novel(str(source), str(output))

    assert [item["source_order"] for item in index] == [231, 232]
    assert len(list(output.glob("第*章_*.md"))) == 2
    stored = json.loads((output / "_索引.json").read_text(encoding="utf-8"))
    assert stored["chapter_count"] == 2
    assert all("正文" in (output / item["filename"]).read_text(encoding="utf-8") for item in index)


def test_chapter_preflight_blocks_old_merged_split_files(tmp_path):
    chapter = tmp_path / "原文拆解" / "第298章_修补与强化.md"
    chapter.parent.mkdir()
    chapter.write_text(
        "# 第298章 修补与强化\n本章正文\n------------\n第两百三十二章 青莲地心火\n后续章正文",
        encoding="utf-8",
    )

    errors = validate_chapter_source(chapter)

    assert any("额外章节标题" in error for error in errors)
    assert preflight_chapter_source(tmp_path, chapter) == 1
    report = tmp_path / "质量治理" / "章节校验" / "source_input_ch298.txt"
    assert "额外章节标题" in report.read_text(encoding="utf-8")
