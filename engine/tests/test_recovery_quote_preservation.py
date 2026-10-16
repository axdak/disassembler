"""治理纠错引文属于待核验输入；局部改正不能被旧错误引文保全反向阻断。"""
import copy
import json

import pytest

from fault_recovery import preservation_errors
from information_preservation import correction_evidence_errors


OLD = "话语声还在殿堂之上回荡着，古飞等人便见到一袭火红的身影从大门之外，直接大步走了进来。"
TRUE = "话语声还在殿堂之上回&#039;荡&#039;，古飞等人便见到一袭火红的身影从大门之外，直接大步走了进来。"


def candidate():
    return {"新增元素": {}, "修改元素": {}, "治理操作": {"设置字段": [{
        "类型": "角色", "名称": "凤栖梧", "字段": "详情/首次章节",
        "原值": "0156", "值": "0157", "理由": "原文中该人物首次正式出现应为第157章，旧记录的首次章节偏早一章。",
        "证据": [{"章节": "0157", "原文": OLD}]}]}}


def compare(tmp_path, before, after, *, authorized=True):
    source, target = tmp_path / "before.json", tmp_path / "after.json"
    for path, data in ((source, before), (target, after)):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return preservation_errors(source, target, editable_metadata_paths={"/治理操作/设置字段/0/证据/0/原文"} if authorized else set())


def test_correcting_long_governance_quote_reaches_original_source_check(tmp_path):
    before = candidate()
    after = copy.deepcopy(before)
    after["治理操作"]["设置字段"][0]["证据"][0]["原文"] = TRUE
    assert compare(tmp_path, before, after) == []
    assert correction_evidence_errors(after, {157: TRUE}, 157) == []


def test_quote_preservation_exemption_does_not_accept_invented_source(tmp_path):
    before = candidate()
    after = copy.deepcopy(before)
    after["治理操作"]["设置字段"][0]["证据"][0]["原文"] = "这是一段并不存在于真实章节中的长篇引文，不能通过声明修复而直接成为事实。"
    # 保全检查只负责阻止内容丢失；原文真实性仍由既有提交校验拒绝。
    assert compare(tmp_path, before, after) == []
    assert any("原文中不存在" in error for error in correction_evidence_errors(after, {157: TRUE}, 157))


@pytest.mark.parametrize("field", ["理由", "原值", "值"])
def test_quote_exemption_does_not_weaken_other_operation_fields(tmp_path, field):
    before = candidate()
    before["治理操作"]["设置字段"][0][field] = OLD
    after = copy.deepcopy(before)
    after["治理操作"]["设置字段"][0][field] = "删减"
    assert compare(tmp_path, before, after)


@pytest.mark.parametrize("change", ["remove_evidence", "remove_quote", "non_string"])
def test_quote_fix_cannot_remove_evidence_or_change_its_type(tmp_path, change):
    before = candidate()
    after = copy.deepcopy(before)
    evidence = after["治理操作"]["设置字段"][0]["证据"]
    if change == "remove_evidence":
        evidence.clear()
    elif change == "remove_quote":
        evidence[0].pop("原文")
    else:
        evidence[0]["原文"] = []
    assert compare(tmp_path, before, after)


def test_same_named_quote_in_story_details_still_requires_preservation(tmp_path):
    before = {"事件集": [{"名称": "旧事实", "详情": {"治理操作": {"设置字段": [{"证据": [{"原文": OLD}]}]}}}]}
    after = copy.deepcopy(before)
    after["事件集"][0]["详情"]["治理操作"]["设置字段"][0]["证据"][0]["原文"] = TRUE
    assert compare(tmp_path, before, after)


def test_matching_quote_path_without_host_authorization_remains_protected(tmp_path):
    before = candidate()
    after = copy.deepcopy(before)
    after["治理操作"]["设置字段"][0]["证据"][0]["原文"] = TRUE
    assert compare(tmp_path, before, after, authorized=False)
