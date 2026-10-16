"""明确历史别名应先归位，再解析引用；不能从叙述推测人物身份。"""
import copy
import json

import pytest

from normalize_story_schema import normalize_structure
from story_schema_rules import DEFAULTS, make_empty_structure


def fixture():
    story = make_empty_structure()
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="孔小生", 别名=[], 详情={"首次章节":"0014", "最近章节":"0023", "0016-别名": '["小生"]'})
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="营救青蛙", 分组="营救青蛙", 参与成员=["小生"], 详情={"涉及章节":"0021"})
    story["角色集"] = [role]
    story["事件集"] = [event]
    return story


def test_declared_historical_alias_restores_formal_reference_before_quarantine():
    source = fixture()
    original = copy.deepcopy(source)
    result, logs = normalize_structure(source, quarantine_invalid_refs=True)
    assert source == original
    assert result["角色集"][0]["别名"] == ["小生"]
    assert result["角色集"][0]["详情"]["0016-别名"] == '["小生"]'
    assert result["事件集"][0]["参与成员"] == ["孔小生"]
    assert "待确认引用" not in result["事件集"][0]["详情"]
    assert any("0016-别名" in entry for entry in logs)
    assert normalize_structure(result, quarantine_invalid_refs=True)[0] == result


@pytest.mark.parametrize("collection", list(DEFAULTS))
def test_explicit_history_alias_is_supported_for_each_collection(collection):
    story = make_empty_structure()
    item = copy.deepcopy(DEFAULTS[collection])
    item.update(名称="正式名", 别名=["旧别名"], 详情={"首次章节":"0001", "最近章节":"0001", "涉及章节":"0001", "0001-别名": '["旧别名","新增别名"]'})
    story[collection] = [item]
    result, _ = normalize_structure(story)
    assert result[collection][0]["别名"] == ["旧别名", "新增别名"]


@pytest.mark.parametrize("key,value", [("0016-别名说明", '["小生"]'), ("别名称呼", '["小生"]'), ("0016-别名", "也许是小生"), ("0016-别名", '["小生",3]')])
def test_prose_or_malformed_declarations_cannot_authorize_identity(key,value):
    story = fixture()
    story["角色集"][0]["详情"] = {key:value}
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    assert result["角色集"][0]["别名"] == []
    assert result["事件集"][0]["参与成员"] == []
    assert "小生" in result["事件集"][0]["详情"]["待确认引用"]


def test_shared_history_alias_remains_ambiguous_instead_of_first_match():
    story = fixture()
    second = copy.deepcopy(story["角色集"][0])
    second["名称"] = "另一人"
    story["角色集"].append(second)
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    assert [item["名称"] for item in result["角色集"]] == ["孔小生", "另一人"]
    assert result["事件集"][0]["参与成员"] == []
    assert "小生" in result["事件集"][0]["详情"]["待确认引用"]


def test_canonical_name_still_wins_over_another_records_historical_alias():
    story = fixture()
    second = copy.deepcopy(DEFAULTS["角色集"])
    second["名称"] = "小生"
    story["角色集"].append(second)
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    assert result["事件集"][0]["参与成员"] == ["小生"]


@pytest.mark.parametrize("value", [["小生",3], ["小生",{}], ["小生",True], ["小生",""], ["小生"," "]])
def test_native_malformed_alias_array_cannot_be_laundered_by_stringification(value):
    story = fixture()
    story["角色集"][0]["详情"]["0016-别名"] = value
    story["事件集"][0]["参与成员"] = ["3", "小生"]
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    assert result["角色集"][0]["别名"] == []
    assert result["事件集"][0]["参与成员"] == []
    assert json.loads(result["角色集"][0]["详情"]["0016-别名"]) == value


def test_valid_native_alias_array_is_restored_without_losing_source():
    story = fixture()
    story["角色集"][0]["详情"]["0016-别名"] = ["小生"]
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    assert result["角色集"][0]["别名"] == ["小生"]
    assert json.loads(result["角色集"][0]["详情"]["0016-别名"]) == ["小生"]


def test_evidenced_alias_correction_is_not_undone_by_old_history():
    from apply_governance_ops import apply_ops
    story = fixture()
    story["角色集"][0]["别名"] = ["小生"]
    patch = {"治理操作":{"设置字段":[{"类型":"角色", "名称":"孔小生", "字段":"别名", "原值":["小生"], "值":[], "理由":"纠正旧别名误认", "证据":[{"章节":"0021", "原文":"这并不是小生。"}]}]}}
    corrected, _, warnings = apply_ops(story,patch)
    assert warnings == []
    result, _ = normalize_structure(corrected,quarantine_invalid_refs=True)
    assert result["角色集"][0]["别名"] == []
    assert result["角色集"][0]["详情"]["0016-别名"] == '["小生"]'


@pytest.mark.parametrize("field,corrected_field", [("字段修复原文","别名"),("治理纠错记录","别名"),("治理纠错记录","年龄")])
def test_host_field_correction_archive_is_respected_by_alias_restoration(field,corrected_field):
    story = fixture()
    story["角色集"][0]["详情"][field] = json.dumps([json.dumps({"字段":corrected_field,"原值":["小生"]},ensure_ascii=False)],ensure_ascii=False)
    result, _ = normalize_structure(story)
    assert result["角色集"][0]["别名"] == ([] if corrected_field == "别名" else ["小生"])
