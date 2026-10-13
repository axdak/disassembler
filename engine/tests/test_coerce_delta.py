"""Tests for scripts/coerce_delta.py — 入库前的机械纠错层。

设计契约：
- 只做确定性类型/格式及位置纠错，不改语义、不评判内容。
- 复用 normalize_story_schema.py 已有的 coerce_* 工具，不重复造轮子。
- 输入是 LLM 写的 Delta JSON dict，输出是修正后的 Delta JSON dict。
- 修正项记录到 list[str] 报告里，方便审计。

每个测试针对一类高频 LLM 错误。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

import pytest

# scripts 目录加进 sys.path 以便 import
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from coerce_delta import coerce_delta_in_place  # noqa: E402


def test_nonstandard_field_relocation_is_lossless_and_idempotent():
    """Wrong-layer fields are deterministic repairs, not a reason to call a model."""
    from coerce_delta import relocate_element_fields
    item = {"名称": "主角", "人物轨迹": '["完整原文轨迹"]', "关联物品": ["物品:古戒"],
            "提取理由": "原文证据", "详情": {"提取理由": "原文证据"}}
    delta = {"修改元素": {"角色集": [item]}}
    assert len(relocate_element_fields(delta)) == 3
    assert item == {"名称": "主角", "详情": {"人物轨迹": '["完整原文轨迹"]',
                                          "关联物品": ["物品:古戒"], "提取理由": "原文证据"}}
    assert relocate_element_fields(delta) == []


@pytest.mark.parametrize("field", ["人物轨迹", "提取理由"])
def test_relocation_conflict_preserves_both_values_for_validation(field):
    item = {"名称": "主角", field: "顶层分析", "详情": {field: "详情中的不同分析"}}
    delta = {"修改元素": {"角色集": [item]}}
    report = coerce_delta_in_place(delta)
    assert item[field] == "顶层分析"
    assert item["详情"][field] == "详情中的不同分析"
    assert any("冲突" in line for line in report)


def test_invalid_detail_keeps_original_analysis():
    item = {"名称": "主角", "详情": "不能用空对象覆盖这段分析", "人物轨迹": "顶层轨迹"}
    coerce_delta_in_place({"修改元素": {"角色集": [item]}})
    assert item["详情"] == "不能用空对象覆盖这段分析"
    assert item["人物轨迹"] == "顶层轨迹"


def test_program_relocation_keeps_process_and_governance_boundary():
    """Relocating a field must not invent evidence/trace data or relax governance."""
    from test_validate_delta import base_story, bad_delta_missing_trace_for_new_element
    from validate_delta import validate_elements
    delta = bad_delta_missing_trace_for_new_element()
    item = delta["新增元素"]["角色集"][0]
    item["本章状态"] = "前往山门"
    coerce_delta_in_place(delta)
    assert validate_elements(base_story(), delta, "process")[0] == []
    assert validate_elements(base_story(), delta, "governance")[0]
    item["详情"].pop("提取理由")
    assert validate_elements(base_story(), delta, "process")[0]
    assert validate_elements(base_story(), delta, "governance")[0]


def _empty_delta() -> dict:
    return {
        "章节": "第001章",
        "新增元素": {
            "角色集": [],
            "事件集": [],
            "地点集": [],
            "线索集": [],
            "阵营集": [],
            "物品集": [],
            "其他事项集": [],
        },
        "修改元素": {
            "角色集": [],
            "事件集": [],
            "地点集": [],
            "线索集": [],
            "阵营集": [],
            "物品集": [],
            "其他事项集": [],
        },
    }


# ---- 角色集 ----


def test_是否主角_整数1_转bool_true():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "是否主角": 1})
    report = coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["是否主角"] is True
    assert any("是否主角" in line for line in report)


def test_是否主角_整数0_转bool_false():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "是否主角": 0})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["是否主角"] is False


def test_是否主角_字符串主角_转bool_true():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "是否主角": "主角"})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["是否主角"] is True


def test_性别_字符串男_转int_0():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "性别": "男"})
    report = coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["性别"] == 0
    assert any("性别" in line for line in report)


def test_性别_字符串女_转int_1():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "性别": "女"})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["性别"] == 1


def test_性别_未知词_转int_2():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "性别": "未知"})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["性别"] == 2


def test_年龄_字符串数字_转int():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "年龄": "28"})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["年龄"] == 28


def test_关系_对象转字符串数组():
    """LLM 常把 关系 写成 {"师徒": "某某"} 对象，需要转成 ["师徒:某某"]。"""
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "关系": {"师徒": "某某", "对手": "另一人"}}
    )
    report = coerce_delta_in_place(delta)
    rels = delta["新增元素"]["角色集"][0]["关系"]
    assert isinstance(rels, list)
    assert "师徒:某某" in rels
    assert "对手:另一人" in rels
    assert any("关系" in line for line in report)


def test_生日_空字符串_补默认ISO():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲", "生日": ""})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["生日"] == "0001-01-01T00:00:00"


def test_生日_缺失_补默认ISO():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append({"名称": "甲"})
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["角色集"][0]["生日"] == "0001-01-01T00:00:00"


# ---- 事件集 ----


def test_发生地点_单元素数组_转字符串():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "发生地点": ["同福客栈"], "时间": "0001-01-01T00:00:00"}
    )
    report = coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["发生地点"] == "同福客栈"
    assert any("发生地点" in line for line in report)


def test_发生地点_多元素数组_合并为顿号字符串():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "发生地点": ["甲地", "乙地"], "时间": "0001-01-01T00:00:00"}
    )
    coerce_delta_in_place(delta)
    # 多元素必须合并为单个字符串（schema 强制 scalar）。
    val = delta["新增元素"]["事件集"][0]["发生地点"]
    assert isinstance(val, str)
    assert "甲地" in val and "乙地" in val


def test_目标事件_字符串_转单元素数组():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "目标事件": "后续事件", "时间": "0001-01-01T00:00:00"}
    )
    report = coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["目标事件"] == ["后续事件"]
    assert any("目标事件" in line for line in report)


def test_标准字段_匹配类型前缀_自动剥离():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "发生地点": "地点:约翰娜住处",
            "参与成员": ["角色:约翰娜·奥尔森多", "角色:木女王"],
            "目标事件": ["事件:浓雾中贾孤零零出现浑身是血"],
            "时间": "0001-01-01T00:00:00",
        }
    )
    delta["新增元素"]["线索集"].append(
        {"名称": "伏笔", "涉及事件": ["事件:浓雾中贾孤零零出现浑身是血"]}
    )

    report = coerce_delta_in_place(delta)

    event = delta["新增元素"]["事件集"][0]
    assert event["发生地点"] == "约翰娜住处"
    assert event["参与成员"] == ["约翰娜·奥尔森多", "木女王"]
    assert event["目标事件"] == ["浓雾中贾孤零零出现浑身是血"]
    assert delta["新增元素"]["线索集"][0]["涉及事件"] == ["浓雾中贾孤零零出现浑身是血"]
    assert any("标准字段前缀" in line for line in report)


def test_标准字段_不匹配类型前缀_移入待确认引用():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "参与成员": ["角色:约翰娜·奥尔森多", "地点:城堡"],
            "目标事件": ["事件:后续", "角色:误写成角色的事件"],
            "时间": "0001-01-01T00:00:00",
            "详情": {},
        }
    )

    report = coerce_delta_in_place(delta)

    event = delta["新增元素"]["事件集"][0]
    assert event["参与成员"] == ["约翰娜·奥尔森多"]
    assert event["目标事件"] == ["后续"]
    pending = json.loads(event["详情"]["待确认引用"])
    assert "地点:城堡" in pending
    assert "角色:误写成角色的事件" in pending
    assert any("移入详情.待确认引用" in line for line in report)


def test_重量级_字符串数字_转int():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "重量级": "80", "时间": "0001-01-01T00:00:00"}
    )
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["重量级"] == 80


def test_重量级_超过100_截断():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "重量级": 150, "时间": "0001-01-01T00:00:00"}
    )
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["重量级"] == 100


def test_涉及章节_顶层_搬到详情():
    """LLM 常把涉及章节写顶层，schema 要求它在详情里。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "涉及章节": "0001",
            "时间": "0001-01-01T00:00:00",
            "详情": {"提取理由": "..."},
        }
    )
    report = coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert event["详情"]["涉及章节"] == "0001"
    assert "涉及章节" not in event
    assert any("涉及章节" in line for line in report)


def test_涉及章节_英文逗号_转中文全角():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "时间": "0001-01-01T00:00:00",
            "详情": {"涉及章节": "0001,0002"},
        }
    )
    report = coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["详情"]["涉及章节"] == "0001，0002"
    assert any("涉及章节" in line for line in report)


def test_涉及章节_位数不足_补零():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "时间": "0001-01-01T00:00:00",
            "详情": {"涉及章节": "1，2"},
        }
    )
    coerce_delta_in_place(delta)
    assert delta["新增元素"]["事件集"][0]["详情"]["涉及章节"] == "0001，0002"


def test_涉及章节_数组_合并成字符串():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "X",
            "时间": "0001-01-01T00:00:00",
            "详情": {"涉及章节": ["0002", "0001"]},
        }
    )
    coerce_delta_in_place(delta)
    # 应排序去重并用中文全角逗号拼接
    assert delta["新增元素"]["事件集"][0]["详情"]["涉及章节"] == "0001，0002"


# ---- 角色/地点/线索/阵营/物品 的章节追溯字段 ----


def test_角色_首次章节_顶层_搬到详情():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "首次章节": "0001", "详情": {"提取理由": "..."}}
    )
    coerce_delta_in_place(delta)
    role = delta["新增元素"]["角色集"][0]
    assert role["详情"]["首次章节"] == "0001"
    assert "首次章节" not in role


def test_角色_首次章节_位数不足_补零():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"首次章节": "1", "最近章节": "1"}}
    )
    coerce_delta_in_place(delta)
    detail = delta["新增元素"]["角色集"][0]["详情"]
    assert detail["首次章节"] == "0001"
    assert detail["最近章节"] == "0001"


def test_提取理由_顶层_搬到详情():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "提取理由": "本章首次出场", "详情": {}}
    )
    coerce_delta_in_place(delta)
    role = delta["新增元素"]["角色集"][0]
    assert role["详情"]["提取理由"] == "本章首次出场"
    assert "提取理由" not in role


def test_详情_缺失_自动补空对象():
    """LLM 漏写详情时不报错，给个空对象等后续 normalize 接手。"""
    delta = _empty_delta()
    delta["新增元素"]["地点集"].append({"名称": "X"})
    coerce_delta_in_place(delta)
    assert isinstance(delta["新增元素"]["地点集"][0]["详情"], dict)


def test_非事件详情自由键_剥离开头章节号():
    delta = _empty_delta()
    delta["章节"] = "第017章"
    delta["新增元素"]["角色集"].append(
        {
            "名称": "甲",
            "详情": {
                "0017心理状态": "紧张",
                "0017-当前处境": "被困",
                "第017章身体状态": "受伤",
                "关联线索": ["线索:资金缺口"],
            },
        }
    )

    report = coerce_delta_in_place(delta)

    detail = delta["新增元素"]["角色集"][0]["详情"]
    assert detail["心理状态"] == "紧张"
    assert detail["当前处境"] == "被困"
    assert detail["身体状态"] == "受伤"
    assert detail["关联线索"] == ["线索:资金缺口"]
    assert "0017心理状态" not in detail
    assert "0017-当前处境" not in detail
    assert "第017章身体状态" not in detail
    assert any("详情键章节前缀" in line for line in report)


# ---- 不该动的情况（确定性的反测试）----


def test_合规Delta_不变():
    """完全合规的 Delta，经过 coerce 后内容不变（除了可能补默认字段）。"""
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {
            "名称": "甲",
            "是否主角": True,
            "性别": 0,
            "年龄": 28,
            "生日": "0001-01-01T00:00:00",
            "所属阵营": ["阵营A"],
            "关系": ["朋友:某某"],
            "分组": "G",
            "别名": [],
            "标签集": ["tag1"],
            "介绍": "intro",
            "详情": {"提取理由": "r", "首次章节": "0001", "最近章节": "0001"},
        }
    )
    before = json.dumps(delta, ensure_ascii=False)
    report = coerce_delta_in_place(delta)
    after = json.dumps(delta, ensure_ascii=False)
    assert before == after
    assert report == []


def test_未修改时返回空报告():
    delta = _empty_delta()
    report = coerce_delta_in_place(delta)
    assert report == []


def test_修改元素也走纠错():
    """修改元素和新增元素同样走 coerce。"""
    delta = _empty_delta()
    delta["修改元素"]["角色集"].append({"名称": "甲", "是否主角": 1})
    coerce_delta_in_place(delta)
    assert delta["修改元素"]["角色集"][0]["是否主角"] is True


def test_补充标签_数组_转json字符串():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"补充标签": ["莽撞", "客栈"]}}
    )
    report = coerce_delta_in_place(delta)
    value = delta["新增元素"]["角色集"][0]["详情"]["补充标签"]
    assert value == '["莽撞","客栈"]'
    assert any("补充标签" in line for line in report)


def test_补充标签_损坏字符串_按原内容重建():
    """损坏的补充标签必须在纠错层被确定性消除，否则相关元素永远无法提交。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "赖床装病", "详情": {"补充标签": "不是JSON"}}
    )
    report = coerce_delta_in_place(delta)
    value = delta["新增元素"]["事件集"][0]["详情"]["补充标签"]
    assert json.loads(value) == ["不是JSON"]
    assert any("补充标签" in line for line in report)


def test_补充标签_空数组_移除该键():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"补充标签": []}}
    )
    report = coerce_delta_in_place(delta)
    assert "补充标签" not in delta["新增元素"]["角色集"][0]["详情"]
    assert any("补充标签" in line for line in report)


def test_补充标签_已合规_不产生纠错记录():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"补充标签": '["莽撞"]'}}
    )
    report = coerce_delta_in_place(delta)
    assert not any("补充标签" in line for line in report)


def test_普通详情数组_转json字符串数组():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "赖床装病", "详情": {"动作链条": ["赖床", "装病", "咳血", "被送医威胁"]}}
    )

    report = coerce_delta_in_place(delta)

    value = delta["新增元素"]["事件集"][0]["详情"]["动作链条"]
    assert value == '["赖床","装病","咳血","被送医威胁"]'
    assert any("动作链条" in line for line in report)


def test_详情元素引用数组_保持真实数组():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"关联线索": ["线索:资金缺口"]}}
    )

    report = coerce_delta_in_place(delta)

    assert delta["新增元素"]["角色集"][0]["详情"]["关联线索"] == ["线索:资金缺口"]
    assert not any("关联线索" in line for line in report)


# ---- 详情 → 顶层 反向搬迁（模型把整组标准字段写进详情）----


def test_详情内整组标准字段被搬回顶层():
    """模型把事件集标准字段整组写进详情时，程序按顶层契约搬回，无需模型重写。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {
            "名称": "萧炎退回鉴宝室静候拍卖款项",
            "时间": "0001-01-01T00:00:00",
            "详情": {
                "涉及章节": "0024",
                "发生地点": "鉴宝室",
                "参与成员": ["角色:萧炎"],
                "重量级": 20,
                "分组": "筑基灵液交易收官",
                "动作链条": "回到鉴宝室等待款项",
            },
        }
    )

    report = coerce_delta_in_place(delta)

    event = delta["新增元素"]["事件集"][0]
    assert event["发生地点"] == "鉴宝室"
    assert event["参与成员"] == ["萧炎"]
    assert event["重量级"] == 20
    assert event["分组"] == "筑基灵液交易收官"
    assert set(event["详情"]) == {"涉及章节", "动作链条"}
    assert sum("迁回顶层" in line for line in report) == 4
    from test_validate_delta import base_story
    from validate_delta import validate_elements
    assert not any("详情[重量级]" in line for line in validate_elements(base_story(), delta, "process")[0])


def test_详情内标准字段为空值不搬迁():
    """空字符串/空数组不携带信息，搬上去只会掩盖"缺少标准字段"，交给 normalize。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00",
         "详情": {"发生地点": "", "参与成员": [], "分组": "   "}}
    )
    report = coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert "发生地点" not in event and "参与成员" not in event and "分组" not in event
    assert not any("迁回顶层" in line for line in report)


def test_详情内标准字段类型不符则不搬迁():
    """标签集顶层契约是字符串数组；详情里是字符串时不搬，交给校验器报错。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00",
         "详情": {"标签集": '["宝物交易","冲突升级"]'}}
    )
    coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert "标签集" not in event
    assert event["详情"]["标签集"] == '["宝物交易","冲突升级"]'


def test_布尔值不会被当成重量级搬迁():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00", "详情": {"重量级": True}}
    )
    coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert "重量级" not in event
    assert event["详情"]["重量级"] is True


def test_顶层已有同名值时详情内副本原样保留():
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00", "分组": "顶层分组",
         "详情": {"分组": "详情里的另一种说法"}}
    )
    report = coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert event["分组"] == "顶层分组"
    assert event["详情"]["分组"] == "详情里的另一种说法"
    assert not any("迁回顶层" in line for line in report)


def test_名称不从详情搬迁():
    """名称由身份对齐负责，不能把详情里的同名键当成正式名称。"""
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00", "详情": {"名称": "另一种说法"}}
    )
    coerce_delta_in_place(delta)
    event = delta["新增元素"]["事件集"][0]
    assert event["名称"] == "X"
    assert event["详情"]["名称"] == "另一种说法"


def test_反向搬迁幂等():
    from coerce_delta import relocate_element_fields
    delta = _empty_delta()
    delta["新增元素"]["事件集"].append(
        {"名称": "X", "时间": "0001-01-01T00:00:00", "详情": {"重量级": 30, "发生地点": "广场"}}
    )
    assert len(relocate_element_fields(delta)) == 2
    assert relocate_element_fields(delta) == []


# ---------------------------------------------------------------------------
# 修改目标无法唯一归属时保留业务内容，由校验器/合并端阻止错误入库。
# 本批新增后的合法修改仍可合入；未知或冲突条目不得通过删除换取成功。
# ---------------------------------------------------------------------------


def _mod_delta(name: str) -> dict:
    delta = _empty_delta()
    # 提供已有规范化默认值，让完整性断言只检测条目/内容丢失。
    delta["修改元素"]["角色集"].append({"名称": name, "生日": "0001-01-01T00:00:00",
                                    "详情": {"提取理由": "本章变化"}})
    return delta


def test_档案中不存在的修改目标保留并阻止入库():
    """无法归属不代表内容无效，必须保留完整补丁交给校验器。"""
    from validate_delta import validate_elements

    delta = _mod_delta("不存在的角色")
    original = deepcopy(delta["修改元素"]["角色集"])
    story = {"角色集": [{"名称": "萧炎"}]}
    coerce_delta_in_place(delta, story)
    assert delta["修改元素"]["角色集"] == original
    assert any("修改目标[不存在的角色]不存在" in e for e in validate_elements(story, delta, "process")[0])


def test_跨集合歧义的修改目标完整保留():
    """第037章事故：不能确定萧家的类别时不得丢弃本章格局。"""
    from validate_delta import validate_elements

    delta = _mod_delta("萧家")
    delta["修改元素"]["角色集"][0]["详情"]["本章格局"] = "族内势力发生变化，必须保留以便核对原文。"
    original = deepcopy(delta["修改元素"]["角色集"])
    story = {"角色集": [{"名称": "萧炎"}],
             "地点集": [{"名称": "萧家"}],
             "阵营集": [{"名称": "萧家"}]}
    coerce_delta_in_place(delta, story)
    assert delta["修改元素"]["角色集"] == original
    assert any("修改目标[萧家]不存在" in e for e in validate_elements(story, delta, "process")[0])


def test_档案中存在的修改目标被保留():
    delta = _mod_delta("萧炎")
    coerce_delta_in_place(delta, {"角色集": [{"名称": "萧炎"}]})
    assert [item["名称"] for item in delta["修改元素"]["角色集"]] == ["萧炎"]


def test_按别名命中的修改目标被保留():
    delta = _mod_delta("萧家族长")
    coerce_delta_in_place(delta, {"角色集": [{"名称": "萧战", "别名": ["萧家族长"]}]})
    assert [item["名称"] for item in delta["修改元素"]["角色集"]] == ["萧家族长"]


def test_未提供正式结构时不做移除():
    """没有可依据的正式档案，保持既有行为不变。"""
    delta = _mod_delta("任意名称")
    report = coerce_delta_in_place(delta)
    assert not any("已移除" in line for line in report)
    assert len(delta["修改元素"]["角色集"]) == 1


def test_正式结构缺少该集合时不做移除():
    """目标集合缺失说明传入的结构不完整，交给原校验器而不是代为丢弃。"""
    delta = _mod_delta("任意名称")
    coerce_delta_in_place(delta, {"事件集": []})
    assert len(delta["修改元素"]["角色集"]) == 1


def test_唯一类别迁移与无归属保留协同():
    """安全迁移不受影响，仍无法归属的内容留在原集合等待修复。"""
    delta = _empty_delta()
    delta["修改元素"]["角色集"].append({"名称": "某地点", "详情": {"提取理由": "x"}})
    delta["修改元素"]["角色集"].append({"名称": "查无此名", "生日": "0001-01-01T00:00:00", "详情": {"提取理由": "x"}})
    coerce_delta_in_place(delta, {"角色集": [], "地点集": [{"名称": "某地点"}]})
    assert delta["修改元素"]["角色集"] == [{"名称": "查无此名", "生日": "0001-01-01T00:00:00", "详情": {"提取理由": "x"}}]
    assert [item["名称"] for item in delta["修改元素"]["地点集"]] == ["某地点"]


def test_无归属修改重复纠错仍完整保留():
    """重跑不能让待处理条目消失，也不能把原有硬错误洗成成功。"""
    delta = _mod_delta("萧家")
    story = {"角色集": [{"名称": "萧炎"}],
             "地点集": [{"名称": "萧家"}],
             "阵营集": [{"名称": "萧家"}]}
    original = deepcopy(delta)
    coerce_delta_in_place(delta, story)
    coerce_delta_in_place(delta, story)
    assert delta == original


def test_唯一类别目标完整迁移():
    """档案中唯一类别存在的目标仍按正式名称完整迁移。"""
    delta = _mod_delta("萧家")
    report = coerce_delta_in_place(delta, {"角色集": [{"名称": "萧炎"}], "阵营集": [{"名称": "萧家"}]})
    assert not any("已移除" in line for line in report)
    assert delta["修改元素"]["阵营集"]
    assert delta["修改元素"]["角色集"] == []


@pytest.mark.parametrize("target", ["萧玉", "萧玉的别名"])
def test_同章新增后的修改保留并实际合入(target):
    """新增和修改承载不同信息，正式名及新增别名都不能被当成无效修改丢弃。"""
    from merge_delta import merge_collection
    from validate_delta import validate_elements

    delta = _mod_delta(target)
    delta["新增元素"]["角色集"] = [{"名称": "萧玉", "别名": ["萧玉的别名"],
                                    "详情": {"提取理由": "本章首次出场", "原始信息": "回到家族"}}]
    delta["修改元素"]["角色集"][0]["详情"]["补充信息"] = "与萧炎发生争执"
    coerce_delta_in_place(delta, {"角色集": []})
    assert validate_elements({"角色集": []}, delta, "process")[0] == []
    merged, *_ = merge_collection([], delta["新增元素"]["角色集"],
                                  delta["修改元素"]["角色集"], "角色集")
    assert len(merged) == 1
    assert merged[0]["详情"]["原始信息"] == "回到家族"
    assert merged[0]["详情"]["补充信息"] == "与萧炎发生争执"


def test_迁移目的集合已有补丁时保留两处内容():
    """迁移被同名补丁阻止时，不能把另一份独有信息当成重复内容删除。"""
    delta = _mod_delta("萧家")
    delta["修改元素"]["角色集"][0]["详情"]["本章格局"] = "待确认的完整变化"
    delta["修改元素"]["阵营集"] = [{"名称": "萧家", "详情": {"提取理由": "阵营信息"}}]
    original = deepcopy(delta)
    coerce_delta_in_place(delta, {"角色集": [], "阵营集": [{"名称": "萧家"}]})
    assert delta == original


def test_cli重跑保留失败条目且仍无法通过校验(tmp_path, monkeypatch):
    """覆盖写入磁盘及重复运行，防止仅验证内存而漏掉实际 CLI 丢数据。"""
    from coerce_delta import main
    from validate_delta import validate_elements

    delta = _mod_delta("萧家")
    delta["修改元素"]["角色集"][0]["详情"]["本章格局"] = "必须留存的本章独有信息"
    story = {"角色集": [], "地点集": [{"名称": "萧家"}], "阵营集": [{"名称": "萧家"}]}
    delta_path, story_path, report_path = [tmp_path / name for name in ("delta.json", "story.json", "report.json")]
    delta_path.write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")
    story_path.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["coerce_delta", str(delta_path), "--current", str(story_path),
                                    "--report", str(report_path)])
    for _ in range(2):
        assert main() == 0  # 机械纠错完成不代表章节校验通过。
        actual = json.loads(delta_path.read_text(encoding="utf-8"))
        assert actual == delta
        assert any("修改目标[萧家]不存在" in e for e in validate_elements(story, actual, "process")[0])
