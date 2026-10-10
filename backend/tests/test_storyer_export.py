"""完整故事家 JSON 应保留原结构，并按章节索引合并正文和分析态。"""

import json

from storyer_export import build_storyer_document, export_storyer_document


def test_storyer_export_combines_chapters_without_changing_final_structure(tmp_path):
    (tmp_path / "原文拆解").mkdir()
    (tmp_path / "章节处理").mkdir()
    original = {"介绍": {"标题": "测试故事", "描述": ""}, "角色集": []}
    structure_path = tmp_path / "故事结构.json"
    structure_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    index = {"chapters": [
        {"seq": 2, "filename": "第二章.md", "title": "第二章"},
        {"seq": 1, "filename": "第一章.md", "title": "第一章"},
    ]}
    (tmp_path / "原文拆解" / "_索引.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "原文拆解" / "第一章.md").write_text("# 第一章\n\n正文一\n", encoding="utf-8")
    (tmp_path / "原文拆解" / "第二章.md").write_text("正文二\n", encoding="utf-8")
    (tmp_path / "章节处理" / "第一章.md").write_text("## 1. 剧情梗概\n分析一", encoding="utf-8")

    document, missing = build_storyer_document(tmp_path)
    assert [chapter["序号"] for chapter in document["章节集"]] == [1, 2]
    assert document["章节集"][0]["正文"] == "正文一\n"
    assert document["章节集"][0]["分析态"].startswith("## 1.")
    assert document["章节集"][1]["分析态"] == ""
    assert missing == 1

    result = export_storyer_document(tmp_path)
    assert result["chapter_count"] == 2
    assert result["missing_analysis"] == 1
    assert json.loads((tmp_path / "导出" / "故事家完整.json").read_text(encoding="utf-8")) == document
    assert json.loads(structure_path.read_text(encoding="utf-8")) == original
