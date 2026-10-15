#!/usr/bin/env python3
"""compress_tags 行为测试。"""

import json
from pathlib import Path

import pytest

from compress_tags import (
    FALLBACK_LIMIT,
    compress_delta,
    compress_item,
    compress_structure,
    load_limits,
)
from story_schema_rules import (
    SUPPLEMENTARY_TAGS_DETAIL_KEY,
    parse_supplementary_tags,
    salvage_supplementary_tags,
)


def _event(name, tags, supplementary=None, detail_extra=None):
    detail = {"涉及章节": "0001", "提取理由": "测试"}
    if supplementary is not None:
        detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = supplementary
    if detail_extra:
        detail.update(detail_extra)
    return {
        "名称": name,
        "发生地点": "",
        "参与成员": [],
        "重量级": 0,
        "目标事件": [],
        "分组": "",
        "时间": "0001-01-01T00:00:00",
        "别名": [],
        "标签集": list(tags),
        "介绍": "",
        "详情": detail,
    }


def _character(name, tags, supplementary=None):
    detail = {"首次章节": "0001", "最近章节": "0001", "提取理由": "测试"}
    if supplementary is not None:
        detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = supplementary
    return {
        "名称": name,
        "是否主角": False,
        "性别": 2,
        "年龄": 0,
        "生日": "0001-01-01T00:00:00",
        "所属阵营": [],
        "关系": [],
        "分组": "",
        "别名": [],
        "标签集": list(tags),
        "介绍": "",
        "详情": detail,
    }


def _place(name, tags):
    return {
        "名称": name,
        "父级地点": "",
        "分组": "",
        "别名": [],
        "标签集": list(tags),
        "介绍": "",
        "详情": {"首次章节": "0001", "最近章节": "0001", "提取理由": "测试"},
    }


def test_unknown_value_under_known_prefix_is_demoted():
    """前缀对、值不在字典：剧情母题:武力对峙 → 进补充标签，前缀保留。"""
    item = _event("退婚事件", ["剧情母题:武力对峙", "热血"])
    compress_item(item, limit=8)
    assert item["标签集"] == ["热血"]
    supp = parse_supplementary_tags(item["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY])
    assert supp == ["剧情母题:武力对峙"]


def test_wrong_prefix_for_collection_is_demoted():
    """越权前缀：地点带剧情母题:复仇 → 下层到补充标签。"""
    item = _place("张府", ["剧情母题:复仇", "古典"])
    compress_item(item, limit=8)
    assert item["标签集"] == ["古典"]
    supp = parse_supplementary_tags(item["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY])
    assert supp == ["剧情母题:复仇"]


def test_free_tag_overflow_truncates_to_supplementary():
    """自由标签 10 个、limit=8：前 8 个留标签集，后 2 个进补充标签。"""
    tags = [f"自由{i}" for i in range(10)]
    item = _character("李四", tags)
    compress_item(item, limit=8)
    assert item["标签集"] == tags[:8]
    supp = parse_supplementary_tags(item["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY])
    assert supp == tags[8:]


def test_idempotent_second_run_no_change():
    item = _event("退婚事件", ["剧情母题:武力对峙", "剧情线主题:主线推进", "热血", "初入江湖"])
    compress_item(item, limit=8)
    snapshot = json.dumps(item, ensure_ascii=False, sort_keys=True)
    compress_item(item, limit=8)
    assert json.dumps(item, ensure_ascii=False, sort_keys=True) == snapshot


def test_merge_with_existing_supplementary_preserves_order():
    existing = json.dumps(["热血", "初入江湖"], ensure_ascii=False, separators=(",", ":"))
    item = _character("王五", ["人物类型:主角", "新加标签"], supplementary=existing)
    compress_item(item, limit=8)
    assert item["标签集"] == ["新加标签"]
    supp = parse_supplementary_tags(item["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY])
    assert supp == ["热血", "初入江湖", "人物类型:主角"]


def test_no_change_when_only_free_tags_under_limit():
    """全自由标签且不超限：标签集与详情都不变。"""
    item = _character("赵六", ["热血", "初入江湖"])
    before_detail = json.dumps(item["详情"], ensure_ascii=False, sort_keys=True)
    compress_item(item, limit=8)
    assert item["标签集"] == ["热血", "初入江湖"]
    assert SUPPLEMENTARY_TAGS_DETAIL_KEY not in item["详情"]
    assert json.dumps(item["详情"], ensure_ascii=False, sort_keys=True) == before_detail


def test_merged_primary_and_supplementary_overlap_is_losslessly_removed():
    """合并后跨区重复需整理，否则最终结构校验会阻断章节入库。"""
    item = _character("未明人物", ["身份未明"], supplementary='["身份未明","举止可疑"]')
    report = compress_structure({"角色集": [item]}, {"角色集": 8})
    assert len(report) == 1
    assert item["标签集"] == ["身份未明"]
    assert parse_supplementary_tags(item["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY]) == ["举止可疑"]
    assert compress_structure({"角色集": [item]}, {"角色集": 8}) == []


def test_duplicate_only_supplementary_field_is_removed():
    item = _character("未明人物", ["身份未明"], supplementary='["身份未明"]')
    compress_item(item, limit=8)
    assert item["标签集"] == ["身份未明"]
    assert SUPPLEMENTARY_TAGS_DETAIL_KEY not in item["详情"]


def test_event_four_dimension_compound_tag_stays_in_tag_set():
    """事件四维复合标签不带受控前缀，必须留在标签集供界面直读。"""
    tag = "侠之真义+主题点化+感动+观念冲突"
    item = _event("佟湘玉点化郭芙蓉", [tag, "哲学对话"])
    before_detail = json.dumps(item["详情"], ensure_ascii=False, sort_keys=True)
    compress_item(item, limit=8)
    assert item["标签集"] == [tag, "哲学对话"]
    assert SUPPLEMENTARY_TAGS_DETAIL_KEY not in item["详情"]
    assert json.dumps(item["详情"], ensure_ascii=False, sort_keys=True) == before_detail


def test_delta_prompt_requires_event_four_dimension_compound_tag():
    prompt = Path("prompts/流水线/单章增量提取.j2").read_text(encoding="utf-8")
    assert "剧情线主题+叙事功能+爽点/情绪点+冲突悬念类型" in prompt
    assert "每个事件" in prompt
    assert "不下沉" in prompt


def test_delta_prompt_requires_rich_event_archive_values_with_strict_keys():
    prompt = Path("prompts/流水线/单章增量提取.j2").read_text(encoding="utf-8")
    assert "机器字段和明确引用字段必须严格稳定" in prompt
    assert "普通档案字段优先使用稳定短键" in prompt
    assert "自然生成少量 key" in prompt
    assert "value 要充分承载档案信息" in prompt
    for phrase in ["动作链条", "冲突变化", "信息揭示", "情绪转折", "关键台词"]:
        assert phrase in prompt
    assert "不要只写机器字段" in prompt


def test_compress_delta_walks_both_buckets():
    delta = {
        "章节": "第001章",
        "新增元素": {
            "角色集": [_character("张三", ["人物类型:主角", "热血"])],
            "事件集": [], "地点集": [], "线索集": [],
            "阵营集": [], "物品集": [], "其他事项集": [],
        },
        "修改元素": {
            "角色集": [],
            "事件集": [_event("退婚事件", ["剧情母题:武力对峙", "反转"])],
            "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
        },
    }
    limits = {k: 8 for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    report = compress_delta(delta, limits)
    assert len(report) == 2
    char = delta["新增元素"]["角色集"][0]
    assert char["标签集"] == ["热血"]
    assert parse_supplementary_tags(char["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY]) == ["人物类型:主角"]
    evt = delta["修改元素"]["事件集"][0]
    assert evt["标签集"] == ["反转"]
    assert parse_supplementary_tags(evt["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY]) == ["剧情母题:武力对峙"]


def test_compress_structure_keeps_skeleton():
    data = {
        "介绍": {"标题": "测试", "描述": "测试"},
        "角色集": [_character("张三", ["人物类型:主角", "热血"])],
        "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    limits = {k: 8 for k in data if k != "介绍"}
    report = compress_structure(data, limits)
    assert len(report) == 1
    assert data["介绍"] == {"标题": "测试", "描述": "测试"}
    char = data["角色集"][0]
    assert char["标签集"] == ["热血"]
    assert parse_supplementary_tags(char["详情"][SUPPLEMENTARY_TAGS_DETAIL_KEY]) == ["人物类型:主角"]


def test_compressed_delta_passes_validation(tmp_path):
    """端到端：压缩后用 validate_delta 跑一次，原本会失败的标签不再报错。"""
    from validate_delta import delta_errors, validate_delta

    story = {
        "介绍": {"标题": "测试", "描述": "测试"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    # 完整事件质量是独立前置条件，本例只允许受控标签压缩改变校验结果。
    event = _event("退婚事件", ["婚约尊严+冲突爆发+愤怒+人际冲突", "剧情母题:武力对峙", "反转"])
    event.update(分组="退婚冲突与尊严反击", 介绍="婚约解除引发双方对峙，角色展开尊严反击。")
    delta = {
        "章节": "第001章",
        "新增元素": {
            "角色集": [],
            "事件集": [event],
            "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
        },
        "修改元素": {
            "角色集": [], "事件集": [], "地点集": [], "线索集": [],
            "阵营集": [], "物品集": [], "其他事项集": [],
        },
    }

    story_path = tmp_path / "story.json"
    delta_path = tmp_path / "delta.json"
    story_path.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    delta_path.write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")

    # 压缩前会因「未知受控标签」失败
    assert any("未知受控标签[剧情母题:武力对峙]" in error for error in delta_errors(story, delta)[0])
    assert validate_delta(str(story_path), str(delta_path), mode="process") is False

    # 压缩后通过
    limits = {k: 8 for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    raw_delta = json.loads(delta_path.read_text(encoding="utf-8"))
    compress_delta(raw_delta, limits)
    delta_path.write_text(json.dumps(raw_delta, ensure_ascii=False), encoding="utf-8")
    assert validate_delta(str(story_path), str(delta_path), mode="process") is True


def test_load_limits_falls_back_for_missing_keys(tmp_path):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"事件集": 12}), encoding="utf-8")
    limits = load_limits(p)
    assert limits["事件集"] == 12
    assert limits["角色集"] == FALLBACK_LIMIT


def test_load_limits_handles_missing_file(tmp_path):
    limits = load_limits(tmp_path / "does_not_exist.json")
    for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]:
        assert limits[k] == FALLBACK_LIMIT


def test_corrupted_supplementary_is_rebuilt_keeping_original_content():
    """损坏的补充标签就地重建为可解析标签：原内容逐字保留，不以空数组覆盖。

    结构侧压缩是每次提交都要过的关口，抛异常会让整份结构永久无法提交；而既有
    事件不允许改 详情 整块，模型也修不了，所以必须在这里自愈。
    """
    item = _event("退婚事件", ["剧情母题:武力对峙"], supplementary="not a json")
    compress_item(item, limit=8)
    assert parse_supplementary_tags(item["详情"]["补充标签"]) == ["not a json", "剧情母题:武力对峙"]


def test_corrupted_supplementary_is_repaired_by_structure_pass():
    """结构级压缩不再抛错，修复后幂等，可重新通过解析。"""
    item = _event("退婚事件", ["剧情母题:武力对峙"], supplementary="not a json")
    data = {"事件集": [item]}
    compress_structure(data, {"事件集": 8})
    assert parse_supplementary_tags(item["详情"]["补充标签"]) == ["not a json", "剧情母题:武力对峙"]
    assert compress_structure(data, {"事件集": 8}) == []


def test_salvage_supplementary_tags_keeps_every_shape_of_content():
    """抢救规则覆盖数组 / JSON 字符串 / 纯文本 / 其它类型，且不猜分隔符。"""
    # 混合数组整体保留，null 及原文空白不能因仅提取字符串项而静默丢失。
    assert salvage_supplementary_tags(["甲", " 乙 ", "甲", "", None]) == ['["甲"," 乙 ","甲","",null]']
    assert salvage_supplementary_tags('["甲","乙"]') == ["甲", "乙"]
    assert salvage_supplementary_tags('"甲"') == ["甲"]
    assert salvage_supplementary_tags("甲,乙") == ["甲,乙"]
    assert salvage_supplementary_tags(123) == ["123"]
    assert salvage_supplementary_tags("") == []
    assert salvage_supplementary_tags([]) == []

