"""正常 Delta 和身份治理必须保全事实；纠错通过明确证据协议表达。"""
import copy
import json

import pytest

from apply_governance_ops import apply_ops
from generation_contract import chapter_quality_errors
from governance_contract import governance_quality_errors
from merge_delta import deep_merge_item
from normalize_story_schema import normalize_structure
from story_schema_rules import DEFAULTS, make_empty_structure, validate_exact_story_schema


def story_fixture():
    story = make_empty_structure()
    story["介绍"] = {"标题": "验证", "描述": "治理保全验证。"}
    for name in ("主角", "证人"):
        record = copy.deepcopy(DEFAULTS["角色集"])
        record.update(名称=name, 介绍=name + "的档案")
        record["详情"] = {"首次章节": "0001", "最近章节": "0001"}
        if name == "证人":
            record["别名"] = ["目击者"]
            record["关系"] = ["朋友:主角"]
            record["详情"].update(秘密="密钥藏在钟楼", 人物轨迹='["第001章：目睹密谋"]')
        story["角色集"].append(record)
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="救援", 介绍="记录救援", 分组="00000010-救援脱险",
                 标签集=["救援+转折+紧张+生存冲突"], 参与成员=["主角", "证人"])
    event["详情"] = {"涉及章节": "0001", "提取理由": "原文救援", "关键抉择": "甲放弃追击，转身救人。"}
    story["事件集"] = [event]
    return normalize_structure(story)[0]


def test_free_event_detail_update_retains_original_fact():
    before = story_fixture()
    update = {"名称": "救援", "详情": {"关键抉择": "甲离开现场。", "提取理由": "本章补充离场"}}
    after = copy.deepcopy(before)
    deep_merge_item(after["事件集"][0], update, "事件集", "0002")
    text = after["事件集"][0]["详情"]["关键抉择"]
    assert "甲放弃追击，转身救人。" in text
    assert "甲离开现场。" in text
    assert validate_exact_story_schema(after, mode="process")[0] == []


def test_degrade_preserves_complete_archive_and_original_reference_context():
    before = story_fixture()
    patch = {"治理操作": {"降级元素": [{"类型": "角色", "名称": "证人", "原因": "临时人物",
                         "挂载到": {"类型": "角色", "名称": "主角"}}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    host = after["角色集"][0]
    archive = json.loads(host["详情"]["治理档案记录"])
    entry = json.loads(archive[-1])
    assert entry["原档案"] == before["角色集"][1]
    assert any(ref["字段"] == "参与成员" and ref["名称"] == "救援" for ref in entry["原引用"])
    assert validate_exact_story_schema(after, mode="governance")[0] == []


@pytest.mark.parametrize("target", ["不存在", "证人"])
def test_invalid_degrade_target_is_atomic(target):
    before = story_fixture()
    after, _, warnings = apply_ops(before, {"治理操作": {"降级元素": [{
        "类型": "角色", "名称": "证人", "原因": "临时人物", "挂载到": {"类型": "角色", "名称": target}}]}})
    assert warnings
    assert after == before


def test_degrade_requires_destination():
    before = story_fixture()
    after, _, warnings = apply_ops(before, {"治理操作": {"降级元素": [{"类型": "角色", "名称": "证人"}]}})
    assert warnings
    assert after == before


def test_merge_keeps_conflicting_scalar_and_its_source_record():
    before = story_fixture()
    before["角色集"][0]["年龄"] = 16
    before["角色集"][1]["年龄"] = 17
    before["角色集"][1]["详情"]["最近章节"] = "0010"
    after, _, warnings = apply_ops(before, {"治理操作": {"合并元素": [
        {"类型": "角色", "主": "主角", "并入": ["证人"]}]}})
    assert warnings == []
    entries = json.loads(after["角色集"][0]["详情"]["治理档案记录"])
    assert json.loads(entries[-1])["原档案"]["年龄"] == 17
    assert "年龄" in after["角色集"][0]["详情"]["待确认信息"]


def test_evidenced_tag_correction_keeps_old_value_and_can_pass_quality_guard():
    before = story_fixture()
    old = before["事件集"][0]["标签集"]
    new = ["和解+收束+释然+观念冲突"]
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集", "值": new,
        "原值": old, "理由": "原标签误判，原文为和解", "证据": [{"章节": "0001", "原文": "两人握手言和。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert governance_quality_errors(before, after, patch) == []
    assert old[0] in after["事件集"][0]["详情"]["治理纠错记录"]


def test_unexplained_tag_replacement_stays_blocked():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集", "值": ["和解+收束+释然+观念冲突"]}]}}
    after, _, _ = apply_ops(before, patch)
    assert governance_quality_errors(before, after, patch)


def test_correction_rejects_stale_old_value_without_any_mutation():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集",
        "原值": ["过期+标签+不能+覆盖"], "值": ["和解+收束+释然+观念冲突"], "理由": "纠错",
        "证据": [{"章节": "0001", "原文": "两人握手言和。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings
    assert after == before


def test_normal_update_quality_gate_checks_existing_event_prose():
    before = story_fixture()
    after = copy.deepcopy(before)
    after["事件集"][0]["详情"]["关键抉择"] = "离开了。"
    patch = {"新增元素": {}, "修改元素": {"事件集": [{"名称": "救援", "详情": {"关键抉择": "离开了。"}}]}}
    assert any("关键抉择" in error for error in chapter_quality_errors(before, after, patch))


def test_evidenced_detail_correction_is_scoped_and_retains_prior_fact():
    before = story_fixture()
    old = before["事件集"][0]["详情"]["关键抉择"]
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "详情/关键抉择",
        "原值": old, "值": "甲先救人，随后追击。", "理由": "原提取顺序错误",
        "证据": [{"章节": "0001", "原文": "他先救人，随后才追击。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["事件集"][0]["详情"]["关键抉择"] == "甲先救人，随后追击。"
    assert old in after["事件集"][0]["详情"]["治理纠错记录"]
    assert chapter_quality_errors(before, after, patch) == []


def test_correction_cannot_replace_event_trace_via_detail_pointer():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "详情/涉及章节",
        "原值": "0001", "值": "0009", "理由": "错误范围",
        "证据": [{"章节": "0009", "原文": "另一章"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings
    assert after == before
