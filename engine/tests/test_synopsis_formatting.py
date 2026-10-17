"""简介排版只调整展示，不补写事实，也不增加模型调用。"""
import json

import pytest

from synopsis_formatting import format_synopsis_text


@pytest.mark.parametrize("source,expected", [
    ("  原有简介只有一段，事实保持原样。  ", "原有简介只有一段，事实保持原样。"),
    ("【主线】甲寻找乙。\r\n\r\n\r\n【当前进展】\r\n仍未找到。",
     "【主线】\n甲寻找乙。\n\n【当前进展】\n仍未找到。"),
    ("## 【主线】 甲寻找乙。\n**【核心冲突】** 乙的去向不明。",
     "【主线】\n甲寻找乙。\n\n【核心冲突】\n乙的去向不明。"),
    ("甲说：‘【主线】只是一个标记。’\n\n【附注】未知。",
     "甲说：‘【主线】只是一个标记。’\n\n【附注】未知。"),
    ("\n\t第一段。\n\n\n第二段。\n", "第一段。\n\n第二段。"),
    ("", ""),
])
def test_layout_preserves_body_and_only_formats_existing_sections(source, expected):
    """不能为缺少四节的旧简介分配情节或创建占位段落。"""
    assert format_synopsis_text(source) == expected
    assert format_synopsis_text(expected) == expected


@pytest.mark.parametrize("value", [None, 42, [], {}])
def test_invalid_description_is_not_silently_coerced(value):
    with pytest.raises(TypeError):
        format_synopsis_text(value)


def test_final_delivery_formats_without_model_and_preserves_process(tmp_path):
    """自动收尾与手工调用同一规则，正式来源凭据仍可验证。"""
    import story_delivery
    from run_pipeline import cmd_finalize
    from story_source import select_analysis_story_source
    story = {"介绍": {"标题": "测试", "描述": "【主线】甲寻找乙。\n【当前进展】尚未找到。"},
             **{key: [] for key in ("角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集")}}
    source = tmp_path / "故事结构_增量.json"
    source.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    assert story_delivery.prepare_delivery(tmp_path) == 0
    # 既有 prepare 会规范化过程库；这里只核验新增交付排版不再修改它。
    before = source.read_bytes()
    assert story_delivery.commit_delivery(tmp_path, "") == 0
    draft = tmp_path / "故事结构_草稿.json"
    expected = "【主线】\n甲寻找乙。\n\n【当前进展】\n尚未找到。"
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"]["描述"] == expected
    assert source.read_bytes() == before
    assert list((tmp_path / "质量治理/最终审计/交付").glob("synopsis_before_*.json"))
    once = draft.read_bytes()
    assert story_delivery.commit_delivery(tmp_path, "") == 0
    assert draft.read_bytes() == once
    assert cmd_finalize(tmp_path) == 0
    selected, raw, _ = select_analysis_story_source(tmp_path)
    assert selected.name == "故事结构.json"
    assert json.loads(raw)["介绍"]["描述"] == expected
