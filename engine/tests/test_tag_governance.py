#!/usr/bin/env python3
"""标签治理的守恒、迁移和详情校验测试。"""

import copy
import json

from apply_governance_ops import apply_ops
from story_schema_rules import validate_exact_story_schema
from validate_delta import validate_tag_governance_ops


def story_with_tags(tags=None, supplementary=None):
    detail = {"首次章节": "0001", "最近章节": "0001"}
    if supplementary is not None:
        detail["补充标签"] = supplementary
    return {
        "介绍": {"标题": "测试", "描述": "测试"},
        "角色集": [{
            "名称": "张三", "是否主角": True, "性别": 0, "年龄": 20,
            "生日": "0001-01-01T00:00:00", "所属阵营": [], "关系": [],
            "分组": "主角团", "别名": [], "标签集": tags or ["人物类型:主角", "热血", "初入江湖"],
            "介绍": "故事主角。", "详情": detail,
        }],
        "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }


def tag_op(retained=None, demoted=None):
    return {
        "类型": "角色", "名称": "张三",
        "保留标签": retained if retained is not None else ["人物类型:主角"],
        "降级标签": demoted if demoted is not None else ["热血", "初入江湖"],
        "理由": "后两项仅描述当前阶段。",
    }


def patch(op):
    empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    return {"章节范围": "第001章-第005章", "新增元素": empty, "修改元素": empty, "治理操作": {"治理标签": [op]}}


def test_tag_governance_moves_tags_losslessly_and_records_reason():
    story = story_with_tags()
    result, logs, warnings = apply_ops(copy.deepcopy(story), patch(tag_op()))
    item = result["角色集"][0]
    assert warnings == []
    assert item["标签集"] == ["人物类型:主角"]
    assert item["详情"]["补充标签"] == '["热血","初入江湖"]'
    assert "理由:后两项仅描述当前阶段。" in logs[0]


def test_tag_governance_merges_existing_supplementary_tags_without_duplicates():
    story = story_with_tags(supplementary='["既有补充","热血"]')
    result, _, warnings = apply_ops(copy.deepcopy(story), patch(tag_op()))
    assert warnings == []
    assert result["角色集"][0]["详情"]["补充标签"] == '["既有补充","热血","初入江湖"]'


def test_append_detail_keeps_relation_clues_as_json_string_array():
    story = story_with_tags()
    story["角色集"][0]["详情"]["关系线索"] = '["父亲:郭巨侠；来源:第003章"]'
    patch_doc = {
        "章节范围": "第001章-第005章",
        "新增元素": {},
        "修改元素": {},
        "治理操作": {
            "追加详情": [{
                "类型": "角色",
                "名称": "张三",
                "详情键": "关系线索",
                "值": '["父亲:郭巨侠；来源:第003章","哥哥:莫小宝；来源:第003章"]',
            }]
        },
    }
    result, _, warnings = apply_ops(copy.deepcopy(story), patch_doc)
    assert warnings == []
    assert json.loads(result["角色集"][0]["详情"]["关系线索"]) == [
        "父亲:郭巨侠；来源:第003章",
        "哥哥:莫小宝；来源:第003章",
    ]


def test_append_detail_keeps_archive_fields_as_json_string_arrays():
    story = story_with_tags()
    story["角色集"][0]["详情"]["人物轨迹"] = '["第001章：首次出场"]'
    patch_doc = {
        "章节范围": "第001章-第005章",
        "新增元素": {},
        "修改元素": {},
        "治理操作": {
            "追加详情": [{
                "类型": "角色",
                "名称": "张三",
                "详情键": "人物轨迹",
                "值": '["第001章：首次出场","第002章：主动推动冲突"]',
            }, {
                "类型": "角色",
                "名称": "张三",
                "详情键": "入库依据",
                "值": "第001章：明确姓名并持续参与剧情",
            }]
        },
    }

    result, _, warnings = apply_ops(copy.deepcopy(story), patch_doc)

    assert warnings == []
    assert json.loads(result["角色集"][0]["详情"]["人物轨迹"]) == [
        "第001章：首次出场",
        "第002章：主动推动冲突",
    ]
    assert json.loads(result["角色集"][0]["详情"]["入库依据"]) == ["第001章：明确姓名并持续参与剧情"]


def test_merge_element_keeps_archive_detail_fields_as_json_string_arrays():
    story = story_with_tags()
    story["角色集"][0]["详情"]["人物轨迹"] = '["第001章：首次出场"]'
    story["角色集"].append({
        "名称": "张三别称", "是否主角": True, "性别": 0, "年龄": 20,
        "生日": "0001-01-01T00:00:00", "所属阵营": [], "关系": [],
        "分组": "主角团", "别名": [], "标签集": [],
        "介绍": "张三的重复条目。",
        "详情": {
            "首次章节": "0002",
            "最近章节": "0002",
            "人物轨迹": '["第001章：首次出场","第002章：以别称再次出场"]',
            "物品轨迹": "第002章：佩剑成为身份标识",
        },
    })
    patch_doc = {
        "章节范围": "第001章-第005章",
        "治理操作": {
            "合并元素": [{
                "类型": "角色",
                "主": "张三",
                "并入": ["张三别称"],
            }]
        },
    }

    result, _, warnings = apply_ops(copy.deepcopy(story), patch_doc)

    assert warnings == []
    assert [item["名称"] for item in result["角色集"]] == ["张三"]
    assert json.loads(result["角色集"][0]["详情"]["人物轨迹"]) == [
        "第001章：首次出场",
        "第002章：以别称再次出场",
    ]
    assert json.loads(result["角色集"][0]["详情"]["物品轨迹"]) == ["第002章：佩剑成为身份标识"]


def test_tag_governance_requires_full_conservation_and_no_overlap():
    story = story_with_tags()
    missing = tag_op(demoted=["热血"])
    overlap = tag_op(retained=["人物类型:主角", "热血"])
    assert any("完整覆盖" in error for error in validate_tag_governance_ops(story, patch(missing), "governance"))
    assert any("不得重叠" in error for error in validate_tag_governance_ops(story, patch(overlap), "governance"))
    result, _, warnings = apply_ops(copy.deepcopy(story), patch(missing))
    assert warnings
    assert result == story


def test_tag_governance_is_governance_only_and_keeps_controlled_validation():
    story = story_with_tags()
    assert validate_tag_governance_ops(story, patch(tag_op()), "process") == ["治理操作.治理标签 仅允许在 governance 模式使用"]
    invalid = tag_op(retained=["人物类型:不存在"], demoted=["热血", "初入江湖"])
    assert any("未知受控标签" in error for error in validate_tag_governance_ops(story, patch(invalid), "governance"))


def test_supplementary_tags_reject_invalid_encoded_content_and_overlap():
    malformed = story_with_tags(supplementary="热血、初入江湖")
    errors, _ = validate_exact_story_schema(malformed, mode="final")
    assert any("必须是可解析的 JSON 字符串数组" in error for error in errors)
    overlap = story_with_tags(supplementary='["热血"]')
    errors, _ = validate_exact_story_schema(overlap, mode="final")
    assert any("不得重复" in error for error in errors)


def test_empty_tag_set_passes_final_schema_validation():
    story = story_with_tags(tags=[])
    errors, warnings = validate_exact_story_schema(story, mode="final")
    assert errors == []
    assert not any("标签集 为空" in warning for warning in warnings)


def test_event_archive_detail_keys_can_preserve_rich_scene_labels_without_punctuation():
    story = {
        "介绍": {"标题": "测试", "描述": "测试"},
        "角色集": [],
        "事件集": [{
            "名称": "名场面对峙",
            "发生地点": "",
            "参与成员": [],
            "重量级": 80,
            "目标事件": [],
            "分组": "00000010-名场面对峙冲突升级身份揭示",
            "时间": "0001-01-01T00:00:00",
            "别名": [],
            "标签集": ["身份线+冲突升级+爆笑+信息差"],
            "介绍": "角色围绕身份误会展开对峙。",
            "详情": {
                "涉及章节": "0001",
                "名场面台词女扮男装误判": "废话当然是雄的。",
                "动作链条压制与围观": "追问身份，然后点穴制住，众人围观。",
            },
        }],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [],
        "其他事项集": [],
    }

    errors, warnings = validate_exact_story_schema(story, mode="process")

    assert errors == []
    assert warnings == []


def test_plain_archive_detail_keys_with_punctuation_warn_but_do_not_block():
    story = story_with_tags()
    story["角色集"][0]["详情"]["动作链条：赖床->装病"] = '["赖床","装病","咳血"]'
    story["角色集"][0]["详情"]["这句台词是不是太长但仍需保留？"] = "废话当然是雄的。"

    errors, warnings = validate_exact_story_schema(story, mode="process")

    assert errors == []
    assert any("详情 键名[动作链条：赖床->装病]包含标点" in warning for warning in warnings)
    assert any("详情 键名[这句台词是不是太长但仍需保留？]" in warning for warning in warnings)


def test_machine_detail_fields_still_use_strict_keys():
    story = story_with_tags()
    story["角色集"][0]["详情"]["关联线索：错误"] = ["线索:资金缺口"]

    errors, _ = validate_exact_story_schema(story, mode="process")

    assert any("机器字段和明确引用字段键名不可包含标点" in error for error in errors)


def test_detail_arrays_are_only_for_typed_references_or_encoded_supplementary_tags():
    story = story_with_tags(supplementary='["人物类型:核心主角"]')
    story["角色集"][0]["详情"]["关联线索"] = ["线索:资金缺口"]
    story["角色集"][0]["详情"]["动作链条"] = '["赖床","装病","咳血","被送医威胁"]'

    errors, _ = validate_exact_story_schema(story, mode="process")

    assert errors == []

    story["角色集"][0]["详情"]["动作链条"] = ["出场", "交锋"]

    errors, _ = validate_exact_story_schema(story, mode="process")

    assert any("详情[动作链条][0] 数组项必须是 类型:名称 格式" in error for error in errors)
