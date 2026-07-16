"""Tests for scripts/coerce_delta.py — 入库前的机械纠错层。

设计契约：
- 只做确定性类型/格式纠错，不改语义、不改键名、不评判内容。
- 复用 normalize_story_schema.py 已有的 coerce_* 工具，不重复造轮子。
- 输入是 LLM 写的 Delta JSON dict，输出是修正后的 Delta JSON dict。
- 修正项记录到 list[str] 报告里，方便审计。

每个测试针对一类高频 LLM 错误。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

# scripts 目录加进 sys.path 以便 import
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from coerce_delta import coerce_delta_in_place  # noqa: E402


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
