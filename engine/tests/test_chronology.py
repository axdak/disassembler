#!/usr/bin/env python3
"""章节时间、事件顺序和剧情段分组的回归测试。"""

import json
import os
import sys
import tempfile

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, HERE)

from apply_governance_ops import merge_items
from chronology import (
    BASE_TIME,
    assign_event_group_orders,
    chapter_time,
    is_iso_time,
    is_semantic_event_group_name,
    normalize_involved_chapters,
)
from merge_delta import deep_merge_item, merge_delta
from migrate_chronology import migrate_story
from normalize_story_schema import normalize_detail_value, normalize_structure
from story_schema_rules import validate_exact_story_schema


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def empty_story():
    return {
        "介绍": {"标题": "测试", "描述": ""},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def event(name, chapters, group="0001-开篇"):
    return {
        "名称": name,
        "发生地点": "",
        "参与成员": [],
        "重量级": 1,
        "目标事件": [],
        "分组": group,
        "时间": chapter_time(int(chapters.split("，")[0])),
        "别名": [],
        "标签集": [],
        "介绍": name,
        "详情": {"涉及章节": chapters},
    }


def test_chapter_time_and_involved_chapters_are_canonical():
    assert BASE_TIME == "0001-01-01T00:00:00"
    assert chapter_time(1) == "0001-01-01T00:00:00"
    assert chapter_time(2) == "0001-01-02T00:00:00"
    assert is_iso_time(chapter_time(1))
    assert not is_iso_time("0001-1-1T0:0:0")
    assert normalize_involved_chapters("0045，0043，0043，0044") == "0043，0044，0045"
    print("[OK] 章节时间和涉及章节字符串规范化")


def test_normalize_detail_value_encodes_non_reference_arrays_as_json_strings():
    logs = []

    assert normalize_detail_value(["赖床", "装病", "咳血"], logs) == '["赖床","装病","咳血"]'
    assert normalize_detail_value(["线索:资金缺口"], logs) == ["线索:资金缺口"]


def test_normalization_preserves_plain_archive_detail_keys_with_punctuation():
    normalized, _ = normalize_structure({
        **empty_story(),
        "事件集": [{
            **event("赖床装病", "0001", "赖床装病"),
            "详情": {
                "涉及章节": "0001",
                "动作链条：赖床->装病": ["赖床", "装病", "咳血"],
            },
        }],
    })

    detail = normalized["事件集"][0]["详情"]
    assert detail["动作链条：赖床->装病"] == '["赖床","装病","咳血"]'


def test_normalization_adds_birthday_and_compact_chapter_fields():
    normalized, _ = normalize_structure({
        **empty_story(),
        "角色集": [{"名称": "甲", "详情": {"首次出现章节": "第001章", "最近更新章节": "第008章", "来源章节": "第001章；第008章", "提取理由": "首次登场；身份确认"}}],
    })
    person = normalized["角色集"][0]
    assert person["生日"] == BASE_TIME
    assert person["详情"]["首次章节"] == "0001"
    assert person["详情"]["最近章节"] == "0008"
    assert "来源章节" not in person["详情"]
    assert "提取理由" not in person["详情"]
    print("[OK] 人物默认生日和紧凑章节字段")


def test_merge_inserts_historical_event_by_involved_chapter():
    with tempfile.TemporaryDirectory() as directory:
        story_path = os.path.join(directory, "story.json")
        delta_path = os.path.join(directory, "delta.json")
        story = empty_story()
        story["事件集"] = [
            event("第001章事件", "0001", "0001-开篇"),
            event("第050章事件", "0050", "0050-收束"),
        ]
        write_json(story_path, story)
        write_json(delta_path, {
            "章节": "治理补丁_001-050",
            "新增元素": {"事件集": [event("第043-045章聚合事件", "0043，0044，0045", "成人仪式")]},
            "修改元素": {},
        })

        merge_delta(story_path, delta_path)
        with open(story_path, "r", encoding="utf-8") as handle:
            merged = json.load(handle)

        assert [item["名称"] for item in merged["事件集"]] == ["第001章事件", "第043-045章聚合事件", "第050章事件"]
        assert merged["事件集"][1]["分组"] == "00000020-成人仪式"
    print("[OK] 历史聚合事件按章节位置插入")


def test_group_order_uses_plot_segment_rank_without_reordering_group_members():
    events = [
        event("第050章事件", "0050", "成人仪式"),
        event("第029章事件", "0029", "成人仪式"),
        event("第043章事件", "0043", "成人仪式"),
    ]
    assign_event_group_orders(events)
    assert [item["名称"] for item in events] == ["第050章事件", "第029章事件", "第043章事件"]
    assert [item["分组"] for item in events] == [
        "00000010-成人仪式",
        "00000020-成人仪式",
        "00000030-成人仪式",
    ]
    print("[OK] 剧情段不跨章节且不改事件原顺序")


def test_migration_drops_legacy_trace_and_stably_orders_events():
    story = empty_story()
    story["角色集"] = [{
        "名称": "甲", "是否主角": True, "性别": 0, "年龄": 20,
        "所属阵营": [], "关系": [], "分组": "主角", "别名": [], "标签集": [], "介绍": "甲",
        "详情": {"来源章节": "第001章；第008章", "提取理由": "首次登场；身份确认", "首次出现章节": "第001章", "最近更新章节": "第008章"},
    }]
    story["事件集"] = [
        event("后续事件", "0050", "后续"),
        {**event("成人仪式", "0043，0044，0045", "成人仪式"), "详情": {"来源章节": "第043章；第044章；第045章", "提取理由": "聚合重复事件"}},
    ]
    migrated, _ = migrate_story(story)
    assert [item["名称"] for item in migrated["事件集"]] == ["成人仪式", "后续事件"]
    assert migrated["事件集"][0]["分组"] == "00000010-成人仪式"
    assert migrated["角色集"][0]["生日"] == BASE_TIME
    assert migrated["角色集"][0]["详情"]["首次章节"] == "0001"
    assert migrated["角色集"][0]["详情"]["最近章节"] == "0008"
    assert not {"来源章节", "首次出现章节", "最近更新章节"} & set(migrated["角色集"][0]["详情"])
    assert "提取理由" not in migrated["角色集"][0]["详情"]
    assert migrated["事件集"][0]["详情"]["提取理由"] == "聚合重复事件"
    print("[OK] 存量迁移回填章节字段、清理最终追溯文本并稳定重排事件")


def test_structure_validator_rejects_event_order_and_group_rank_drift():
    story = empty_story()
    story["事件集"] = [
        event("后续", "0050", "0050-主线"),
        event("历史聚合", "0043", "0050-主线"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("早于前一事件" in error for error in errors), errors
    assert any("段号应为00000010" in error for error in errors), errors
    print("[OK] 结构校验器拒绝事件乱序和错误剧情段号")


def test_repeated_trace_updates_do_not_grow_final_element():
    target = {"名称": "甲", "详情": {}}
    for seq in range(1, 1001):
        deep_merge_item(target, {
            "名称": "甲",
            "详情": {"来源章节": f"第{seq:04d}章", "提取理由": f"第{seq}章提取"},
        }, "角色集")
    assert target["详情"] == {}
    assert "来源章节" not in target
    print("[OK] 千次非事件追溯更新不沉淀Delta理由，不累积旧来源章节")


def test_structure_validator_rejects_legacy_trace_in_final_elements():
    story = empty_story()
    story["角色集"] = [{
        "名称": "甲", "是否主角": True, "性别": 0, "年龄": 20, "生日": BASE_TIME,
        "所属阵营": [], "关系": [], "分组": "主角", "首次章节": "0001", "最近章节": "0001",
        "别名": [], "标签集": [], "介绍": "甲", "详情": {"来源章节": "第001章"},
    }]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("不得存在于最终元素" in error for error in errors), errors
    print("[OK] 结构校验器拒绝最终元素中的旧追溯字段")


def test_structure_validator_allows_detail_reason_only_on_events():
    story = empty_story()
    story["角色集"] = [{
        "名称": "甲", "是否主角": True, "性别": 0, "年龄": 20, "生日": BASE_TIME,
        "所属阵营": [], "关系": [], "分组": "主角", "别名": [], "标签集": [], "介绍": "甲",
        "详情": {"首次章节": "0001", "最近章节": "0001", "提取理由": "Delta审计理由"},
    }]
    story["事件集"] = [event("事件甲", "0001", "事件甲")]
    story["事件集"][0]["详情"]["提取理由"] = "最终事件档案理由"

    errors, _ = validate_exact_story_schema(story, mode="process")

    assert any("角色[甲].详情.提取理由 不得存在于最终元素" in error for error in errors), errors
    assert not any("事件[事件甲].详情.提取理由" in error for error in errors), errors


def test_group_order_splits_same_plot_segment_at_chapter_boundary():
    events = [
        event("退婚冲突", "0003", "退婚事件"),
        event("退婚反击", "0004", "退婚事件"),
        event("魔兽山脉历练", "0006", "历练事件"),
    ]
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == [
        "00000010-退婚事件",
        "00000020-退婚事件",
        "00000030-历练事件",
    ]
    print("[OK] 相同剧情短名在章节边界切为新段")


def test_group_order_rebuild_preserves_free_plot_segment_detail():
    events = [
        event("坊市筹资", "0003", "坊市筹资"),
        event("魔兽山脉历练", "0006", "历练事件"),
        event("拍卖会竞价", "0018", "拍卖会事件"),
    ]
    events[0]["详情"]["剧情段"] = "萧炎-坊市-药材出售-资源积累"
    events[0]["详情"]["关联线索"] = ["线索:资金缺口"]
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == [
        "00000010-坊市筹资",
        "00000020-历练事件",
        "00000030-拍卖会事件",
    ]
    assert events[0]["详情"]["剧情段"] == "萧炎-坊市-药材出售-资源积累"
    assert events[0]["详情"]["关联线索"] == ["线索:资金缺口"]
    print("[OK] 段号重排不影响自由剧情段与线索关联")


def test_numeric_only_group_uses_plot_segment_detail_instead_of_number_suffix():
    events = [
        event("坊市筹资", "0003", "0001"),
        event("出售药材", "0003", "0001"),
    ]
    for item in events:
        item["详情"]["剧情段"] = "坊市筹资"
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == ["00000010-坊市筹资", "00000010-坊市筹资"]
    print("[OK] 纯编号分组改用剧情段信息")


def test_structure_validator_rejects_number_only_plot_segment_name():
    story = empty_story()
    story["事件集"] = [event("坊市筹资", "0003", "00000010-0001")]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("不能是纯编号" in error for error in errors), errors
    print("[OK] 结构校验拒绝编号套编号分组")


def test_assigner_does_not_launder_number_only_group_into_ranked_group():
    events = [event("坊市筹资", "0003", "0001")]
    assign_event_group_orders(events)
    assert events[0]["分组"] == "0001"
    print("[OK] 排序器不再把纯编号伪装成剧情分组")


def test_group_order_uses_eight_digit_rank_and_keeps_compact_semantic_name():
    events = [
        event("退婚冲突", "0003", "退婚尊严线冲突爆发羞辱反击身份尊严"),
        event("退婚反击", "0003", "退婚尊严线冲突爆发羞辱反击身份尊严"),
    ]
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == [
        "00000010-退婚尊严线冲突爆发羞辱反击身份尊严",
        "00000010-退婚尊严线冲突爆发羞辱反击身份尊严",
    ]
    assert is_semantic_event_group_name("退婚尊严线冲突爆发羞辱反击身份尊严")
    print("[OK] 分组使用8位段号和40字内复合短名")


def test_structure_validator_rejects_long_plus_event_group_format():
    story = empty_story()
    story["事件集"] = [
        event("退婚冲突", "0003", "00000010-退婚事件+情感尊严线+冲突爆发+羞辱反击+身份与尊严")
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("40字以内" in error for error in errors), errors
    assert any("不得使用+" in error for error in errors), errors
    print("[OK] 结构校验拒绝长格式+分隔事件分组")


def test_structure_validator_rejects_short_rank_width():
    story = empty_story()
    story["事件集"] = [event("退婚冲突", "0003", "0010-退婚尊严线冲突爆发羞辱反击身份尊严")]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("段号必须至少8位" in error for error in errors), errors
    print("[OK] 结构校验拒绝4位事件分组段号")


def test_merge_assigns_new_segment_rank_and_governance_reorders_after_insertion():
    with tempfile.TemporaryDirectory() as directory:
        story_path = os.path.join(directory, "story.json")
        delta_path = os.path.join(directory, "delta.json")
        story = empty_story()
        story["事件集"] = [
            event("退婚冲突", "0003", "00000010-退婚事件"),
            event("拍卖会竞价", "0018", "00000020-拍卖会事件"),
        ]
        write_json(story_path, story)
        write_json(delta_path, {
            "章节范围": "第001章-第020章",
            "新增元素": {"事件集": [event("魔兽山脉历练", "0006", "历练事件")]},
            "修改元素": {},
        })
        merge_delta(story_path, delta_path)
        merged = json.loads(open(story_path, "r", encoding="utf-8").read())
        assert [item["名称"] for item in merged["事件集"]] == ["退婚冲突", "魔兽山脉历练", "拍卖会竞价"]
        assert [item["分组"] for item in merged["事件集"]] == [
            "00000010-退婚事件",
            "00000020-历练事件",
            "00000030-拍卖会事件",
        ]
    print("[OK] 治理插入历史事件后按剧情段顺序全量重排段号")


def test_migration_splits_noncontiguous_legacy_group_runs():
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "0003-经济线"),
        event("魔兽山脉历练", "0006", "0006-历练事件"),
        event("拍卖会竞价", "0018", "0018-经济线"),
    ]
    migrated, _ = migrate_story(story)
    assert [item["分组"] for item in migrated["事件集"]] == [
        "00000010-经济线",
        "00000020-历练事件",
        "00000030-经济线",
    ]
    print("[OK] 存量迁移按章节拆分同名旧分组")


def test_structure_validator_allows_same_plot_segment_name_in_different_chapters():
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "00000010-坊市筹资"),
        event("魔兽山脉历练", "0006", "00000020-历练事件"),
        event("拍卖会竞价", "0018", "00000030-坊市筹资"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert not any("非连续复用" in error for error in errors), errors
    print("[OK] 结构校验允许跨章节复用同一剧情段短名")


def test_production_assignment_splits_same_chapter_noncontiguous_group_name():
    """生产重排必须开启重名拆分，否则产出的分组会被自家校验器判死且无消除路径。"""
    events = [
        event("坊市筹资", "0003", "经济线"),
        event("魔兽山脉历练", "0003", "历练事件"),
        event("拍卖会竞价", "0003", "经济线"),
    ]
    assign_event_group_orders(events, split_repeated=True)
    assert [item["分组"] for item in events] == [
        "00000010-经济线",
        "00000020-历练事件",
        "00000030-经济线续2",
    ]
    story = empty_story()
    story["事件集"] = events
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert not any("非连续复用" in error for error in errors), errors
    print("[OK] 生产重排拆分同章节非连续复用分组名")


def test_structure_validator_rejects_noncontiguous_reuse_of_plot_segment_name():
    """裸名复用仍必须被校验器拦住——这正是生产路径必须拆分的依据。"""
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "00000010-经济线"),
        event("魔兽山脉历练", "0003", "00000020-历练事件"),
        event("拍卖会竞价", "0003", "00000030-经济线"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("非连续复用" in error for error in errors), errors
    print("[OK] 结构校验拒绝同章节内非连续复用剧情段名称")


def test_split_suffix_keeps_group_name_within_length_limit():
    """拆分后缀不得把合法的 40 字剧情段短名挤超上限。"""
    long_name = "退" * 40
    events = [
        event("甲", "0003", long_name),
        event("乙", "0003", "另一段"),
        event("丙", "0003", long_name),
    ]
    assign_event_group_orders(events, split_repeated=True)
    for item in events:
        _, name = item["分组"].split("-", 1)
        assert is_semantic_event_group_name(name), item["分组"]
    print("[OK] 重名拆分后缀不突破剧情段短名长度上限")


def test_merge_and_normalize_split_same_chapter_noncontiguous_group_name():
    """合并与规范化两条生产路径都必须产出校验器接受的分组名（重名拆分已开启）。"""
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "经济线"),
        event("魔兽山脉历练", "0003", "历练事件"),
        event("拍卖会竞价", "0003", "经济线"),
    ]
    normalized, _ = normalize_structure(story)
    assert normalized["事件集"][2]["分组"] == "00000030-经济线续2", normalized["事件集"][2]["分组"]
    errors, _ = validate_exact_story_schema(normalized, mode="process")
    assert not any("非连续复用" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as directory:
        story_path = os.path.join(directory, "story.json")
        delta_path = os.path.join(directory, "delta.json")
        base = empty_story()
        base["事件集"] = [
            event("坊市筹资", "0003", "经济线"),
            event("魔兽山脉历练", "0003", "历练事件"),
        ]
        write_json(story_path, base)
        write_json(delta_path, {
            "章节范围": "第003章-第003章",
            "新增元素": {"事件集": [event("拍卖会竞价", "0003", "经济线")]},
            "修改元素": {},
        })
        merge_delta(story_path, delta_path)
        merged = json.loads(open(story_path, "r", encoding="utf-8").read())
        assert [item["分组"] for item in merged["事件集"]] == [
            "00000010-经济线",
            "00000020-历练事件",
            "00000030-经济线续2",
        ], merged["事件集"]
    print("[OK] 合并与规范化均拆分同章节非连续复用分组名")


def test_normalization_removes_event_order_violation():
    """顺序倒置必须能由程序侧确定性消除：规范化按章节稳定重排。"""
    story = empty_story()
    story["事件集"] = [
        event("后续事件", "0018", "00000010-后续线"),
        event("开篇事件", "0003", "00000020-开篇线"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("早于前一事件" in error for error in errors), errors

    normalized, _ = normalize_structure(story)
    assert [item["名称"] for item in normalized["事件集"]] == ["开篇事件", "后续事件"]
    errors, _ = validate_exact_story_schema(normalized, mode="process")
    assert not any("早于前一事件" in error for error in errors), errors
    print("[OK] 规范化按章节稳定重排事件并消除顺序倒置")


def test_normalization_keeps_authored_order_within_same_chapter():
    """同一章节内的事件必须保持作者给出的相对顺序（稳定排序）。"""
    story = empty_story()
    story["事件集"] = [
        event("乙事件", "0003", "同章线"),
        event("甲事件", "0003", "同章线"),
    ]
    normalized, _ = normalize_structure(story)
    assert [item["名称"] for item in normalized["事件集"]] == ["乙事件", "甲事件"]
    print("[OK] 同章节事件保持原始相对顺序")


def test_normalization_leaves_compliant_event_order_untouched():
    """已合规的数组顺序（逐章解析形成的自然顺序）不得被重排，也不产生重排日志。"""
    story = empty_story()
    story["事件集"] = [
        event("开篇事件", "0003", "00000010-开篇线"),
        event("中期事件", "0010", "00000020-中期线"),
        event("最新事件", "0018", "00000030-最新线"),
    ]
    normalized, logs = normalize_structure(story)
    assert [item["名称"] for item in normalized["事件集"]] == [
        "开篇事件",
        "中期事件",
        "最新事件",
    ], normalized["事件集"]
    assert not any("重排" in line for line in logs), logs
    print("[OK] 已合规的事件顺序不被重排，且不产生重排日志")


def test_governance_merge_reorder_is_repaired_and_reported():
    """治理合并并集 详情.涉及章节 造成的顺序倒置，必须由规范化确定性消除并留下日志。"""
    story = empty_story()
    story["事件集"] = [
        event("早期事件", "0005", "00000010-早期线"),
        event("中期事件", "0010", "00000020-中期线"),
        event("聚合事件", "0020", "00000030-聚合线"),
    ]
    # 真实成因：治理合并把早期事件并入聚合事件，聚合事件首章回到 0005，位置仍在末尾。
    merge_items(story["事件集"][2], story["事件集"][0])
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("早于前一事件" in error for error in errors), errors

    normalized, logs = normalize_structure(story)
    names = [item["名称"] for item in normalized["事件集"]]
    assert names.index("聚合事件") < names.index("中期事件"), names
    errors, _ = validate_exact_story_schema(normalized, mode="process")
    assert not any("早于前一事件" in error for error in errors), errors
    assert any("重排" in line for line in logs), logs
    print("[OK] 治理合并造成的顺序倒置被确定性消除并写入变更日志")


if __name__ == "__main__":
    test_chapter_time_and_involved_chapters_are_canonical()
    test_normalization_adds_birthday_and_compact_chapter_fields()
    test_merge_inserts_historical_event_by_involved_chapter()
    test_group_order_uses_plot_segment_rank_without_reordering_group_members()
    test_migration_drops_legacy_trace_and_stably_orders_events()
    test_structure_validator_rejects_event_order_and_group_rank_drift()
    test_repeated_trace_updates_do_not_grow_final_element()
    test_structure_validator_rejects_legacy_trace_in_final_elements()
    test_group_order_uses_plot_segment_rank_not_chapter_number()
    test_group_order_rebuild_preserves_free_plot_segment_detail()
    test_numeric_only_group_uses_plot_segment_detail_instead_of_number_suffix()
    test_structure_validator_rejects_number_only_plot_segment_name()
    test_assigner_does_not_launder_number_only_group_into_ranked_group()
    test_group_order_uses_eight_digit_rank_and_keeps_compact_semantic_name()
    test_structure_validator_rejects_long_plus_event_group_format()
    test_structure_validator_rejects_short_rank_width()
    test_merge_assigns_new_segment_rank_and_governance_reorders_after_insertion()
    test_migration_splits_noncontiguous_legacy_group_runs()
    test_structure_validator_rejects_noncontiguous_reuse_of_plot_segment_name()
    test_production_assignment_splits_same_chapter_noncontiguous_group_name()
    test_split_suffix_keeps_group_name_within_length_limit()
    test_merge_and_normalize_split_same_chapter_noncontiguous_group_name()
    test_normalization_removes_event_order_violation()
    test_normalization_keeps_authored_order_within_same_chapter()
    test_normalization_leaves_compliant_event_order_untouched()
    test_governance_merge_reorder_is_repaired_and_reported()
    print("\n全部测试通过 [PASS]")
