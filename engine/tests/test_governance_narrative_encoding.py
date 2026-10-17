"""Exercise lossless narrative-array encoding through governance validation/merge."""
import copy
import json

import pytest

from apply_governance_ops import apply_ops
from fault_recovery import preservation_errors
from governance_contract import normalize_governance_patch
from information_preservation import ARCHIVE_FIELD, CORRECTION_FIELD
from local_json_patch import FIELD_REPAIR_ARCHIVE, REFERENCE_REPAIR_ARCHIVE
from merge_delta import merge_delta, save_json
from story_schema_rules import DEFAULTS, make_empty_structure
from validate_delta import delta_errors


VALUES = ["  前后空白  ", "重复", "重复", '换行\n与引号"及反斜线\\', ""]


def fixture(section="修改元素", *, field="线索轨迹", value=None):
    """Use a registered record and complete additions; only wire shape is wrong."""
    story = make_empty_structure()
    story["介绍"] = {"标题": "测试故事", "描述": "已有简介。"}
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="甲", 介绍="甲的既有档案。")
    role["详情"] = {"首次章节": "0001", "最近章节": "0001"}
    story["角色集"] = [role]
    clue = copy.deepcopy(DEFAULTS["线索集"])
    clue.update(名称="已有线索", 介绍="已有线索的完整介绍。")
    clue["详情"] = {"首次章节": "0001", "最近章节": "0001",
                  "线索轨迹": '["旧条目"]'}
    story["线索集"] = [clue]
    item = copy.deepcopy(clue) if section == "新增元素" else {"名称": "已有线索"}
    if section == "新增元素":
        item["名称"] = "新线索"
    item["详情"] = {"提取理由": "原文明示的线索进展，保持原词。", field: copy.deepcopy(VALUES if value is None else value)}
    if section == "新增元素":
        item["详情"].update(首次章节="0001", 最近章节="0001")
    patch = {"章节范围": "第001章", "新增元素": {}, "修改元素": {}}
    patch[section] = {"线索集": [item]}
    return story, patch


@pytest.mark.parametrize("section", ["新增元素", "修改元素"])
def test_narrative_encoding_preserves_every_character_order_and_duplicate(section):
    story, patch = fixture(section)
    old_story, old_patch = copy.deepcopy(story), copy.deepcopy(patch)
    assert delta_errors(story, patch, "governance")[0]
    fixed, changes = normalize_governance_patch(patch, story=story)
    value = fixed[section]["线索集"][0]["详情"]["线索轨迹"]
    assert isinstance(value, str)
    assert json.loads(value) == VALUES
    assert delta_errors(story, fixed, "governance")[0] == []
    assert changes
    assert story == old_story and patch == old_patch
    assert normalize_governance_patch(fixed, story=story) == (fixed, [])


def test_empty_known_narrative_array_uses_the_existing_string_contract():
    story, patch = fixture(value=[])
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed["修改元素"]["线索集"][0]["详情"]["线索轨迹"] == "[]"


def test_wire_encoding_does_not_require_guessing_a_story_identity():
    _, patch = fixture()
    fixed, _ = normalize_governance_patch(patch)
    value = fixed["修改元素"]["线索集"][0]["详情"]["线索轨迹"]
    assert isinstance(value, str) and json.loads(value) == VALUES


@pytest.mark.parametrize("field", ["关联角色", "关联事件", "未知轨迹", "随意列表", "别名",
    ARCHIVE_FIELD, CORRECTION_FIELD, FIELD_REPAIR_ARCHIVE, REFERENCE_REPAIR_ARCHIVE])
def test_reference_unknown_standard_and_host_archive_fields_are_not_reencoded(field):
    values = ["角色:甲"] if field.startswith("关联") else ["原样保留的普通条目"]
    story, patch = fixture(field=field, value=values)
    original = copy.deepcopy(patch)
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed == original and patch == original


@pytest.mark.parametrize("values", [[1], [True], [None], [["嵌套"]], [{"正文": "甲"}], ["叙事", 1]])
def test_non_string_and_nested_lists_keep_original_validator_rejection(values):
    story, patch = fixture(value=values)
    original = copy.deepcopy(patch)
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed == original
    assert delta_errors(story, fixed, "governance")[0]


@pytest.mark.parametrize("values", [["角色:甲"], [" 角色:甲 "], ["叙事条目", "角色:甲"]])
def test_explicit_reference_or_mixed_lists_are_not_reinterpreted_as_narrative(values):
    story, patch = fixture(value=values)
    original = copy.deepcopy(patch)
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed == original
    if values == ["叙事条目", "角色:甲"]:
        assert delta_errors(story, fixed, "governance")[0]
    else:
        assert delta_errors(story, fixed, "governance")[0] == []


def test_already_encoded_text_is_not_reformatted():
    text = '[\n  "原有空格与编码", "重复", "重复"\n]\n'
    story, patch = fixture(value=text)
    fixed, changes = normalize_governance_patch(patch, story=story)
    assert fixed == patch and changes == []


def test_unknown_modification_target_is_not_hidden_by_encoding():
    story, patch = fixture(value=["甲条目"])
    patch["修改元素"]["线索集"][0]["名称"] = "没有登记的线索"
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert any("修改目标[没有登记的线索]不存在" in error
               for error in delta_errors(story, fixed, "governance")[0])


@pytest.mark.parametrize("old", [None, "错误旧值"])
def test_setter_missing_or_stale_preimage_is_not_repaired(old):
    story, _ = fixture()
    op = {"类型": "线索", "名称": "已有线索", "字段": "详情/线索轨迹",
          "值": ["更正条目"], "理由": "原文有据但必须仍检查原值。",
          "证据": [{"章节": "0001", "原文": "完整原句"}]}
    if old is not None:
        op["原值"] = old
    patch = {"章节范围": "第001章", "新增元素": {}, "修改元素": {},
             "治理操作": {"设置字段": [op]}}
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed == patch
    result, _, errors = apply_ops(story, fixed)
    assert errors and any("原值" in error for error in errors)
    assert result == story


def test_real_merge_preserves_old_facts_and_raw_artifact(tmp_path):
    story, patch = fixture(value=["新事实一保持完整且不改写任何限制。", "新事实二仍是尚未发生的条件。"])
    old_values = ["旧事实一保持完整且不改写任何限制。", "旧事实二仍是尚未发生的条件。"]
    story["线索集"][0]["详情"]["线索轨迹"] = json.dumps(old_values, ensure_ascii=False)
    before, merged, raw, normalized = [tmp_path / name for name in ("before.json", "story.json", "raw.json", "normalized.json")]
    for path, value in ((before, story), (merged, story), (raw, patch)):
        save_json(path, value)
    raw_bytes = raw.read_bytes()
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert isinstance(fixed["修改元素"]["线索集"][0]["详情"]["线索轨迹"], str)
    assert delta_errors(story, fixed, "governance")[0] == []
    save_json(normalized, fixed)
    merge_delta(merged, normalized)
    final = json.loads(merged.read_text(encoding="utf-8"))
    values = json.loads(final["线索集"][0]["详情"]["线索轨迹"])
    assert values == old_values + ["新事实一保持完整且不改写任何限制。", "新事实二仍是尚未发生的条件。"]
    assert preservation_errors(before, merged) == []
    assert raw.read_bytes() == raw_bytes
