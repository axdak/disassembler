#!/usr/bin/env python3
"""merge_delta.py 深度合并行为验证测试。

覆盖场景:
  1. 增量JSON不存在 → 自动初始化顶层骨架
  2. 增量JSON缺"介绍"键 → 自动补全，不崩
  3. 别名匹配：第2章用别名"林姑娘"修改，应命中第1章建的"林婉"
  4. 介绍追加去重：第二次追加相同内容不重复
  5. 列表字段去重合并：标签集/别名/关系/参与成员合并去重
  6. 详情键级合并：同名键都为list去重合并，都为string保留新值，仅一边有的键合并进来
  7. 标量字段非空覆盖、空值跳过：分组非空覆盖、年龄空值保留原值
  8. 顶层介绍：标题非空覆盖、描述追加去重
  9. 修改元素只含增量字段：不破坏已有字段
"""
import json
import os
import sys
import tempfile

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, HERE)

from merge_delta import (
    merge_delta, merge_list_field, merge_detail_field,
    merge_intro_string, merge_scalar_field, deep_merge_item,
    build_lookup_index, find_index, ensure_skeleton, TOPLEVEL_SKELETON,
)
from story_schema_rules import parse_supplementary_tags


def _write(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def test_auto_init_when_missing():
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        delta = os.path.join(d, "d1.json")
        _write(delta, {
            "章节": "第1章",
            "新增元素": {
                "角色集": [{"名称": "甲", "介绍": "首次出现", "标签集": ["主角"]}]
            },
            "修改元素": {},
        })
        assert not os.path.isfile(inc)
        stats = merge_delta(inc, delta)
        assert os.path.isfile(inc), "增量JSON应被自动创建"
        with open(inc, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert "介绍" in data and data["介绍"] == {"标题": "", "描述": ""}
        for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]:
            assert k in data and isinstance(data[k], list)
        assert data["角色集"][0]["名称"] == "甲"
        assert stats["角色集"]["新增"] == 1
    print("[OK] 增量JSON不存在时自动初始化顶层骨架")


def test_missing_intro_key_no_crash():
    """旧版第128行 incremental["介绍"]["标题"]=... 在无"介绍"键时崩。新版应自动补全。"""
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        _write(inc, {"角色集": []})  # 故意缺"介绍"
        delta = os.path.join(d, "d.json")
        _write(delta, {
            "章节": "第1章",
            "介绍": {"标题": "风起", "描述": "故事开端"},
            "新增元素": {},
            "修改元素": {},
        })
        stats = merge_delta(inc, delta)  # 不应抛异常
        with open(inc, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert data["介绍"]["标题"] == "风起"
        assert data["介绍"]["描述"] == "故事开端"
    print("[OK] 增量JSON缺'介绍'键时自动补全，不崩")


def test_alias_matching():
    """第2章用别名'林姑娘'修改，应命中第1章建的'林婉'，而非新建。"""
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        _write(inc, {
            "介绍": {"标题": "", "描述": ""},
            "角色集": [{
                "名称": "林婉", "别名": ["林姑娘", "婉儿"],
                "介绍": "出身世家，性格隐忍",
                "标签集": ["隐忍", "谋略"], "详情": {"族长": "角色:林父"}
            }],
            "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
        })
        delta = os.path.join(d, "d.json")
        # 后期子Agent只拿到名称摘要，用别名"林姑娘"发起修改，只输出增量字段
        _write(delta, {
            "章节": "第15章",
            "新增元素": {},
            "修改元素": {
                "角色集": [{
                    "名称": "林姑娘",  # 别名！
                    "介绍": "本章与主角对峙，展现果断一面",
                    "标签集": ["果断"],
                    "详情": {"武器": "短剑"},
                    "分组": "世家/林家",
                }]
            },
        })
        stats = merge_delta(inc, delta)
        with open(inc, 'r', encoding='utf-8') as f:
            data = json.load(f)
        chars = data["角色集"]
        assert len(chars) == 1, f"应命中已有元素而非新建，实际 {len(chars)} 个"
        wan = chars[0]
        # 名称保留原名（不被别名覆盖）
        assert wan["名称"] == "林婉", wan["名称"]
        # 介绍追加，不丢失第3章背景
        assert "出身世家" in wan["介绍"]
        assert "对峙" in wan["介绍"]
        # 标签集合并去重
        assert set(wan["标签集"]) == {"隐忍", "谋略", "果断"}, wan["标签集"]
        # 详情键级合并，原有"族长"保留，新增自由快照字段自动带章节前缀
        assert wan["详情"]["族长"] == "角色:林父"
        assert wan["详情"]["0015-武器"] == "短剑"
        # 分组（标量）非空覆盖
        assert wan["分组"] == "世家/林家"
        # 统计：1个修改，0个新增
        assert stats["角色集"]["修改"] == 1
        assert stats["角色集"]["新增"] == 0
    print("[OK] 别名匹配命中已有元素，字段级深度合并不丢信息")


def test_intro_dedup():
    assert merge_intro_string("", "甲") == "甲"
    assert merge_intro_string("甲", "") == "甲"
    assert merge_intro_string("甲", "甲") == "甲"  # 相同不重复
    assert merge_intro_string("甲", "乙") == "甲\n乙"
    assert merge_intro_string("甲\n乙", "甲") == "甲\n乙"  # 子串去重
    print("[OK] 介绍字段追加去重")


def test_list_dedup():
    assert merge_list_field(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert merge_list_field([], ["x"]) == ["x"]
    assert merge_list_field(["x"], []) == ["x"]
    print("[OK] 列表字段去重合并保持顺序")


def test_detail_merge():
    # 同名键都为list → 去重合并
    assert merge_detail_field({"a": ["x"]}, {"a": ["y", "x"]}) == {"a": ["x", "y"]}
    # 同名键都为string → 保留新值
    assert merge_detail_field({"a": "旧"}, {"a": "新"}) == {"a": "新"}
    # 新值为空 → 保留旧值
    assert merge_detail_field({"a": "旧"}, {"a": ""}) == {"a": "旧"}
    # 仅一边有的键合并进来
    assert merge_detail_field({"a": "1"}, {"b": "2"}) == {"a": "1", "b": "2"}
    print("[OK] 详情字段键级合并")


def test_cumulative_detail_fields_append_without_overwriting():
    detail = merge_detail_field(
        {"关系线索": '["父亲:郭巨侠；来源:第003章；目标角色未正式入库"]'},
        {"关系线索": '["哥哥:莫小宝；来源:第003章；目标角色未正式入库"]'},
    )
    assert json.loads(detail["关系线索"]) == [
        "父亲:郭巨侠；来源:第003章；目标角色未正式入库",
        "哥哥:莫小宝；来源:第003章；目标角色未正式入库",
    ]

    unchanged = merge_detail_field(
        {"待确认信息": '["父亲:郭巨侠；来源:第003章；目标角色未正式入库"]'},
        {"待确认信息": '["父亲:郭巨侠；来源:第003章；目标角色未正式入库"]'},
    )
    assert json.loads(unchanged["待确认信息"]) == ["父亲:郭巨侠；来源:第003章；目标角色未正式入库"]

    migrated = merge_detail_field(
        {"待确认关系": "父亲:郭巨侠；来源:第003章\n哥哥:莫小宝；来源:第003章"},
        {"待确认关系": '["父亲:郭巨侠；来源:第003章","嫂子:佟湘玉；来源:第004章"]'},
    )
    assert json.loads(migrated["待确认关系"]) == [
        "父亲:郭巨侠；来源:第003章",
        "哥哥:莫小宝；来源:第003章",
        "嫂子:佟湘玉；来源:第004章",
    ]

    profile = merge_detail_field(
        {
            "人物轨迹": '["第001章：首次出场，表现为冲动好胜"]',
            "入库依据": '["第001章：首次明确姓名并持续参与剧情"]',
            "动作链条": "赖床装病",
        },
        {
            "人物轨迹": '["第001章：首次出场，表现为冲动好胜","第002章：与掌柜发生正面冲突"]',
            "入库依据": '["第001章：首次明确姓名并持续参与剧情","第002章：推动客栈冲突"]',
            "动作链条": '["赖床装病","被送医威胁"]',
            "物品轨迹": '["第002章：账本成为追查欠款的凭证"]',
        },
    )
    assert json.loads(profile["人物轨迹"]) == [
        "第001章：首次出场，表现为冲动好胜",
        "第002章：与掌柜发生正面冲突",
    ]
    assert json.loads(profile["入库依据"]) == [
        "第001章：首次明确姓名并持续参与剧情",
        "第002章：推动客栈冲突",
    ]
    assert json.loads(profile["动作链条"]) == ["赖床装病", "被送医威胁"]
    assert json.loads(profile["物品轨迹"]) == ["第002章：账本成为追查欠款的凭证"]

    natural = merge_detail_field(
        {
            "外貌细节": '["第001章：衣着狼狈"]',
            "口癖台词": "第001章：常用反问压人",
            "经营状态": '["第001章：客栈生意冷清"]',
            "关联线索": ["线索:资金缺口"],
        },
        {
            "外貌细节": '["第001章：衣着狼狈","第002章：动作利落"]',
            "口癖台词": '["第002章：继续用反问制造压迫感"]',
            "经营状态": "第002章：客栈因冲突引来围观",
            "关联线索": ["线索:账本异常"],
        },
    )
    assert json.loads(natural["外貌细节"]) == ["第001章：衣着狼狈", "第002章：动作利落"]
    assert json.loads(natural["口癖台词"]) == ["第002章：继续用反问制造压迫感"]
    assert natural["经营状态"] == "第002章：客栈因冲突引来围观"
    assert natural["关联线索"] == ["线索:资金缺口", "线索:账本异常"]

    tags = merge_detail_field(
        {"补充标签": '["人物类型:掌柜","喜剧包袱"]'},
        {"补充标签": '["喜剧包袱","关系待确认"]'},
    )
    assert parse_supplementary_tags(tags["补充标签"]) == ["人物类型:掌柜", "喜剧包袱", "关系待确认"]
    print("[OK] 累计型详情字段追加去重，不覆盖")


def test_non_event_free_detail_keys_are_chapter_prefixed_without_touching_machine_fields():
    detail = merge_detail_field(
        {},
        {
            "提取理由": "本章更新",
            "首次章节": "0005",
            "最近章节": "0005",
            "当前修为": "练气三层",
            "关联线索": ["线索:神秘小瓶"],
            "人物轨迹": '["第005章：确认修炼瓶颈"]',
            "补充标签": '["人物类型:核心主角"]',
        },
        collection_key="角色集",
        chapter_prefix="0005",
    )
    assert "提取理由" not in detail
    assert detail["首次章节"] == "0005"
    assert detail["最近章节"] == "0005"
    assert detail["0005-当前修为"] == "练气三层"
    assert detail["关联线索"] == ["线索:神秘小瓶"]
    assert json.loads(detail["人物轨迹"]) == ["第005章：确认修炼瓶颈"]
    assert parse_supplementary_tags(detail["补充标签"]) == ["人物类型:核心主角"]


def test_event_detail_keys_and_json_string_array_values_do_not_keep_chapter_noise():
    detail = merge_detail_field(
        {"动作链条": '["第001章：旧动作"]'},
        {
            "涉及章节": "0001",
            "提取理由": "事件本章发生",
            "动作链条": '["第001章：旧动作","第001章-新动作","0001-补充动作"]',
            "冲突变化": "第001章：矛盾升级",
        },
        collection_key="事件集",
        chapter_prefix="0001",
    )
    assert detail["涉及章节"] == "0001"
    assert detail["提取理由"] == "事件本章发生"
    assert "0001-动作链条" not in detail
    assert json.loads(detail["动作链条"]) == ["旧动作", "新动作", "补充动作"]
    assert json.loads(detail["冲突变化"]) == ["矛盾升级"]


def test_detail_reason_is_preserved_only_for_events_while_legacy_trace_is_not_merged():
    old = {
        "来源章节": "第001章",
        "提取理由": "首次进入宗门主线",
        "首次出现章节": "第001章",
        "最近更新章节": "第001章",
    }
    new = {
        "来源章节": "第008章",
        "提取理由": "确认其宗门地位变化",
        "首次出现章节": "第008章",
        "最近更新章节": "第008章",
    }
    event_detail = merge_detail_field(old, new, collection_key="事件集")
    role_detail = merge_detail_field(old, new, collection_key="角色集")
    assert event_detail == {"提取理由": "确认其宗门地位变化"}
    assert role_detail == {}
    print("[OK] 最终详情仅事件保留提取理由，其他元素不累积Delta追溯字段")


def test_scalar_merge():
    assert merge_scalar_field("旧", "新") == "新"
    assert merge_scalar_field("旧", "") == "旧"
    assert merge_scalar_field(20, 25) == 25
    assert merge_scalar_field(20, None) == 20
    print("[OK] 标量字段非空覆盖、空值跳过")


def test_toplevel_intro_merge():
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        _write(inc, {
            "介绍": {"标题": "旧标题", "描述": "旧描述"},
            "角色集": [], "事件集": [], "地点集": [],
            "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
        })
        delta = os.path.join(d, "d.json")
        _write(delta, {
            "章节": "第5章",
            "介绍": {"标题": "新标题", "描述": "新进展"},  # 标题覆盖、描述追加
            "新增元素": {}, "修改元素": {},
        })
        merge_delta(inc, delta)
        with open(inc, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert data["介绍"]["标题"] == "新标题"  # 非空覆盖
        assert data["介绍"]["描述"] == "旧描述\n新进展"  # 追加
    print("[OK] 顶层介绍: 标题非空覆盖、描述追加去重")


def test_modified_only_partial_fields():
    """修改元素只含增量字段时，不破坏已有字段。"""
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        _write(inc, {
            "介绍": {"标题": "", "描述": ""},
            "角色集": [{
                "名称": "甲", "性别": 0, "年龄": 25, "是否主角": True,
                "介绍": "背景", "标签集": ["a"], "详情": {"x": "1"},
                "分组": "G1", "别名": [], "关系": [], "所属阵营": [],
            }],
            "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
        })
        delta = os.path.join(d, "d.json")
        _write(delta, {
            "章节": "第10章",
            "新增元素": {},
            "修改元素": {
                "角色集": [{
                    "名称": "甲",
                    "介绍": "新进展",        # 只更新介绍
                    "标签集": ["b"],         # 只追加标签
                }]
            },
        })
        merge_delta(inc, delta)
        with open(inc, 'r', encoding='utf-8') as f:
            data = json.load(f)
        a = data["角色集"][0]
        # 未提及的字段保持不变
        assert a["性别"] == 0
        assert a["年龄"] == 25
        assert a["是否主角"] is True
        assert a["分组"] == "G1"
        assert a["详情"] == {"x": "1"}
        # 提及的字段按规则合并
        assert a["介绍"] == "背景\n新进展"
        assert set(a["标签集"]) == {"a", "b"}
    print("[OK] 修改元素只含增量字段时不破坏已有字段")


def test_lookup_index():
    items = [
        {"名称": "甲", "别名": ["甲哥"]},
        {"名称": "乙", "别名": ["甲哥"]},  # 别名冲突，保留第一个
    ]
    name_idx, alias_idx = build_lookup_index(items)
    assert find_index(name_idx, alias_idx, "甲") == 0
    assert find_index(name_idx, alias_idx, "甲哥") == 0  # 别名命中第一个
    assert find_index(name_idx, alias_idx, "乙") == 1
    assert find_index(name_idx, alias_idx, "不存在") == -1
    print("[OK] 名称/别名查找索引（冲突别名保留第一个）")


def test_ensure_skeleton():
    data = {"角色集": [{"名称": "甲"}]}  # 缺很多键
    ensure_skeleton(data)
    assert "介绍" in data and data["介绍"] == {"标题": "", "描述": ""}
    for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]:
        assert k in data
    assert data["角色集"] == [{"名称": "甲"}]  # 已有内容保留
    print("[OK] ensure_skeleton 补全缺失键不破坏已有内容")


def test_existing_corrupt_incremental_fails_without_overwrite():
    """已有过程库损坏时必须失败，不能重建空骨架覆盖历史数据。"""
    with tempfile.TemporaryDirectory() as d:
        inc = os.path.join(d, "故事结构_增量.json")
        with open(inc, "w", encoding="utf-8") as f:
            f.write('{"介绍": {"标题": "旧库"}, "角色集": [')
        before = open(inc, "r", encoding="utf-8").read()

        delta = os.path.join(d, "d.json")
        _write(delta, {
            "章节": "第2章",
            "新增元素": {
                "角色集": [{"名称": "乙", "介绍": "新人物"}]
            },
            "修改元素": {},
        })

        try:
            merge_delta(inc, delta)
            assert False, "损坏的已有增量JSON不应被自动初始化"
        except RuntimeError as exc:
            assert "格式损坏" in str(exc)

        after = open(inc, "r", encoding="utf-8").read()
        assert after == before, "损坏文件必须保持原样，等待恢复流程处理"
    print("[OK] 已有增量JSON损坏时拒绝覆盖，保留现场")


if __name__ == '__main__':
    test_auto_init_when_missing()
    test_missing_intro_key_no_crash()
    test_alias_matching()
    test_intro_dedup()
    test_list_dedup()
    test_detail_merge()
    test_cumulative_detail_fields_append_without_overwriting()
    test_non_event_free_detail_keys_are_chapter_prefixed_without_touching_machine_fields()
    test_event_detail_keys_and_json_string_array_values_do_not_keep_chapter_noise()
    test_detail_reason_is_preserved_only_for_events_while_legacy_trace_is_not_merged()
    test_detail_evidence_is_not_merged_into_final_elements()
    test_scalar_merge()
    test_toplevel_intro_merge()
    test_modified_only_partial_fields()
    test_lookup_index()
    test_ensure_skeleton()
    test_existing_corrupt_incremental_fails_without_overwrite()
    print("\n全部测试通过 [PASS]")
