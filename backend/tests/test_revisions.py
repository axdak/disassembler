import json

from revisions import project_revisions, task_is_stale


def write_project(project, chapters):
    source = project / "原文拆解"
    source.mkdir(parents=True, exist_ok=True)
    (source / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )
    for chapter in chapters:
        (source / chapter["filename"]).write_text(chapter["content"], encoding="utf-8")
    (project / "故事结构_增量.json").write_text('{"阶段":"过程"}', encoding="utf-8")
    (project / "故事结构.json").write_text('{"阶段":"最终"}', encoding="utf-8")


def chapter(seq, title, content):
    return {"seq": seq, "title": title, "filename": f"第{seq:03d}章_{title}.md", "content": content}


def test_chapter_revision_hashes_chinese_index_and_referenced_chapter_metadata(tmp_path):
    project = tmp_path / "项目"
    chapters = [chapter(1, "初见", "第一章正文")]
    write_project(project, chapters)
    original = project_revisions(project)

    chapters[0]["title"] = "重逢"
    (project / "原文拆解" / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )
    changed = project_revisions(project)

    assert changed["chapter"] != original["chapter"]
    assert changed["process"] == original["process"]
    assert changed["final"] == original["final"]


def test_process_and_final_story_content_change_only_their_matching_revision(tmp_path):
    project = tmp_path / "项目"
    write_project(project, [chapter(1, "初见", "第一章正文")])
    original = project_revisions(project)

    (project / "故事结构_增量.json").write_text('{"阶段":"过程已更新"}', encoding="utf-8")
    process_changed = project_revisions(project)
    (project / "故事结构.json").write_text('{"阶段":"最终已更新"}', encoding="utf-8")
    final_changed = project_revisions(project)

    assert process_changed["process"] != original["process"]
    assert process_changed["chapter"] == original["chapter"]
    assert process_changed["final"] == original["final"]
    assert final_changed["final"] != process_changed["final"]
    assert final_changed["chapter"] == process_changed["chapter"]
    assert final_changed["process"] == process_changed["process"]


def test_whole_book_analysis_and_exports_use_their_mapped_revisions(tmp_path):
    project = tmp_path / "项目"
    write_project(project, [chapter(1, "初见", "第一章正文")])
    original = project_revisions(project)
    whole_book_analysis = {
        "task_type": "analysis",
        "params": {"analysis_task": "summary"},
        "input_revision": original,
    }
    process_export = {
        "task_type": "export",
        "params": {"source": "process"},
        "input_revision": original,
    }
    final_export = {
        "task_type": "export",
        "params": {"source": "final"},
        "input_revision": original,
    }

    (project / "原文拆解" / "第001章_初见.md").write_text("修改后的正文", encoding="utf-8")
    chapter_changed = project_revisions(project)
    assert task_is_stale(whole_book_analysis, chapter_changed) is True
    assert task_is_stale(process_export, chapter_changed) is False
    assert task_is_stale(final_export, chapter_changed) is False

    (project / "故事结构_增量.json").write_text('{"阶段":"过程已更新"}', encoding="utf-8")
    process_changed = project_revisions(project)
    assert task_is_stale(process_export, process_changed) is True
    assert task_is_stale(final_export, process_changed) is False

    (project / "故事结构.json").write_text('{"阶段":"最终已更新"}', encoding="utf-8")
    final_changed = project_revisions(project)
    assert task_is_stale(final_export, final_changed) is True


def test_append_only_growth_keeps_old_per_chapter_analysis_current(tmp_path):
    project = tmp_path / "项目"
    chapters = [chapter(1, "初见", "第一章正文")]
    write_project(project, chapters)
    original = project_revisions(project)
    per_chapter = {
        "task_type": "analysis",
        "params": {"analysis_task": "chapter_structure", "chapter_seq": 1},
        "input_revision": original,
    }

    chapters.append(chapter(2, "同行", "第二章正文"))
    source = project / "原文拆解"
    (source / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )
    (source / chapters[-1]["filename"]).write_text(chapters[-1]["content"], encoding="utf-8")
    appended = project_revisions(project)

    assert appended["chapter"] != original["chapter"]
    assert task_is_stale(per_chapter, appended) is False
