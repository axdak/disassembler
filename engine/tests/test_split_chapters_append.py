#!/usr/bin/env python3
"""split_chapters_append.py 纯函数 plan_append / apply_append 测试。

覆盖 Step 1 断言：
- preview 不写盘
- 完整新书前缀必须逐字匹配
- 无新增返回空 plan
- 前缀不一致报告首个冲突
- --increment-only 接受片段
- apply 只写新 md + 更新索引（含 source_file）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from split_chapters_append import apply_append, plan_append  # noqa: E402
from split_chapters import split_novel  # noqa: E402


def _write_index(out_dir: Path, chapters: list[dict], source_file: str,
                 pattern: str = r"^第[一二三四五六七八九十百千万零〇\d]+章") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "source_file": source_file,
        "pattern": pattern,
        "preface_mode": "separate",
        "chapter_count": sum(1 for c in chapters if not c.get("is_preface")),
        "chapters": chapters,
    }
    (out_dir / "_索引.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_old_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Initialize a project with three already-split chapters."""
    project = tmp_path / "拆书_测试"
    out_dir = project / "原文拆解"
    src = tmp_path / "book_old.txt"
    src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n",
        encoding="utf-8",
    )
    # Use split_novel to lay down the canonical index + md files.
    split_novel(str(src), str(out_dir))
    return project, out_dir, src


def _chapter_entry(seq: int, title: str) -> dict:
    from split_chapters import extract_order, normalize_title, safe_name
    return {
        "seq": seq,
        "source_order": extract_order(title) or seq,
        "source_title": title,
        "normalized_title": normalize_title(title),
        "filename": f"第{seq:03d}章_{safe_name(title)}.md",
        "title": title,
        "is_preface": False,
    }


# --- Step 1 assertions ----------------------------------------------------

def test_preview_writes_nothing(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n"
        "第4章 续写\n续写内容\n",
        encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}

    plan = plan_append(str(new_src), str(out_dir))

    assert plan is not None
    after = {p: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}
    assert before == after, "preview must not write anything"
    # Plan identifies the one new chapter.
    assert [c["seq"] for c in plan["appended"]] == [4]
    assert plan["conflict"] is None


def test_complete_book_prefix_must_match(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    # Same first three chapter titles, then a 4th new chapter.
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n"
        "第4章 续写\n续写内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))
    assert plan["prefix_len"] == 3
    assert [c["seq"] for c in plan["appended"]] == [4]
    assert plan["appended"][0]["source_title"] == "第4章 续写"
    assert plan["conflict"] is None


def test_no_new_chapters_returns_empty_plan(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_same.txt"
    # Identical content to the old book: prefix covers everything.
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))
    assert plan["appended"] == []
    assert plan["prefix_len"] == 3
    assert plan["conflict"] is None


def test_conflict_reports_first_mismatch(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_conflict.txt"
    # Chapter 2 title differs -> first mismatch is index 1 (second chapter).
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 改动过的相遇\n相遇内容\n第3章 冲突\n冲突内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))
    assert plan["conflict"] is not None
    assert plan["conflict"]["index"] == 1
    assert "相遇" in plan["conflict"]["old_title"]
    assert "改动过的相遇" in plan["conflict"]["new_title"]
    assert plan["appended"] == []


def test_increment_only_accepts_fragment(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "fragment.txt"
    # Only the new chapters; no prefix overlap expected.
    new_src.write_text(
        "第4章 续写一\n续写一内容\n第5章 续写二\n续写二内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir), increment_only=True)
    assert plan["prefix_len"] == 0
    assert [c["seq"] for c in plan["appended"]] == [4, 5]
    assert plan["conflict"] is None


def test_apply_writes_only_new_md_and_updates_index(tmp_path):
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n"
        "第4章 续写\n续写内容\n",
        encoding="utf-8",
    )

    plan = plan_append(str(new_src), str(out_dir))
    assert [c["seq"] for c in plan["appended"]] == [4]

    old_files = {p.name: p.read_bytes() for p in out_dir.glob("*.md")}
    old_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    old_source_file = old_index["source_file"]
    old_chapter_count = old_index["chapter_count"]

    report = apply_append(plan)

    # New chapter md exists; old md files untouched.
    new_md = out_dir / plan["appended"][0]["filename"]
    assert new_md.is_file()
    assert "续写内容" in new_md.read_text(encoding="utf-8")
    for name, content in old_files.items():
        assert (out_dir / name).read_bytes() == content, f"old file {name} must not change"

    # Index updated: source_file now points at the new book, chapter_count grew.
    new_index = json.loads((out_dir / "_索引.json").read_text(encoding="utf-8"))
    assert new_index["source_file"] == str(new_src.resolve()) or new_index["source_file"] == str(new_src)
    assert new_index["chapter_count"] == old_chapter_count + 1
    assert new_index["source_file"] != old_source_file
    assert [c["seq"] for c in new_index["chapters"]] == [1, 2, 3, 4]
    assert new_index["chapters"][-1]["source_title"] == "第4章 续写"

    # Report mirrors the applied entries.
    assert [c["seq"] for c in report["appended"]] == [4]
    assert report["index_path"] == str(out_dir / "_索引.json")


def test_apply_uses_temp_file_and_replace(tmp_path, monkeypatch):
    """The index must be written via a temp file + os.replace (atomic)."""
    project, out_dir, old_src = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n序章内容\n第2章 相遇\n相遇内容\n第3章 冲突\n冲突内容\n"
        "第4章 续写\n续写内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))

    import os as _os
    import split_chapters_append as mod
    seen: list[str] = []
    real_replace = _os.replace

    def spy_replace(src, dst, *args, **kwargs):
        seen.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)
    monkeypatch.setattr(_os, "replace", spy_replace)
    monkeypatch.setattr(mod.os, "replace", spy_replace, raising=False)

    apply_append(plan)
    assert any(str(out_dir / "_索引.json") == p for p in seen), \
        "index must be replaced atomically via os.replace"


def test_apply_rejects_plan_when_index_changed_after_preview(tmp_path):
    _, out_dir, _ = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容\n第2章 相遇\n内容\n第3章 冲突\n内容\n第4章 续写\n内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))
    index_path = out_dir / "_索引.json"
    index_path.write_text(index_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="索引在预览后已变化"):
        apply_append(plan)

    assert not (out_dir / plan["appended"][0]["filename"]).exists()


def test_apply_rejects_plan_when_source_changed_after_preview(tmp_path):
    _, out_dir, _ = _make_old_project(tmp_path)
    new_src = tmp_path / "book_new.txt"
    new_src.write_text(
        "第1章 序章\n内容\n第2章 相遇\n内容\n第3章 冲突\n内容\n第4章 续写\n内容\n",
        encoding="utf-8",
    )
    plan = plan_append(str(new_src), str(out_dir))
    new_src.write_text(new_src.read_text(encoding="utf-8") + "已变化", encoding="utf-8")

    with pytest.raises(ValueError, match="新原文在预览后已变化"):
        apply_append(plan)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
