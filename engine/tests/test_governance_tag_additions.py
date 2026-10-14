"""Move unambiguous additive tags to modifications without losing old tags/evidence."""
import copy

import pytest
from governance_contract import normalize_governance_patch
from test_tag_governance import story_with_tags, patch, tag_op
from validate_delta import validate_tag_governance_ops


def normalize(document, story):
    return normalize_governance_patch(document, "第001章-第005章", story=story)


@pytest.mark.parametrize("old", [[], ["热血"]])
def test_additive_tag_operation_is_relocated_losslessly(old):
    story = story_with_tags()
    story["角色集"][0]["标签集"] = old
    proposal = patch(tag_op(old + ["人物类型:竞争者"], []))
    original = copy.deepcopy(proposal)
    result, changes = normalize(proposal, story)
    assert proposal == original
    assert changes
    assert result["治理操作"]["治理标签"] == []
    record = result["修改元素"]["角色集"][0]
    assert record["名称"] == "张三"
    assert record["标签集"] == old + ["人物类型:竞争者"]
    assert record["详情"]["提取理由"] == original["治理操作"]["治理标签"][0]["理由"]
    assert not validate_tag_governance_ops(story, result, "governance")
    again, changes = normalize(result, story)
    assert again == result and changes == []


@pytest.mark.parametrize("problem", ["missing", "demotion", "invalid_tag", "duplicate_target", "collision", "extra_field"])
def test_ambiguous_tag_operations_still_fail_original_validation(problem):
    story = story_with_tags()
    story["角色集"][0]["标签集"] = ["热血"]
    proposal = patch(tag_op(["热血", "人物类型:竞争者"], []))
    op = proposal["治理操作"]["治理标签"][0]
    if problem == "missing": op["保留标签"].remove("热血")
    if problem == "demotion": op.update(保留标签=["人物类型:竞争者"], 降级标签=["热血"])
    if problem == "invalid_tag": op["保留标签"].append("人物类型:不存在的标签")
    if problem == "duplicate_target": story["角色集"].append(copy.deepcopy(story["角色集"][0]))
    if problem == "collision": proposal["修改元素"] = {"角色集": [{"名称": "张三", "标签集": ["热血"]}]}
    if problem == "extra_field": op["原文注记"] = "不允许静默丢失"
    result, changes = normalize(proposal, story)
    assert result == proposal and changes == []
    assert validate_tag_governance_ops(story, result, "governance")


def test_existing_modification_fields_and_reason_survive_additive_relocation():
    story = story_with_tags()
    story["角色集"][0]["标签集"] = []
    proposal = patch(tag_op(["人物类型:竞争者"], []))
    proposal["修改元素"] = {"角色集": [{"名称": "张三", "介绍": "原有修订",
                                     "详情": {"提取理由": "原有理由", "状态": "原有状态"}}]}
    result, _ = normalize(proposal, story)
    record = result["修改元素"]["角色集"][0]
    assert record["介绍"] == "原有修订"
    assert record["详情"]["状态"] == "原有状态"
    assert record["详情"]["提取理由"].startswith("原有理由")
    assert proposal["治理操作"]["治理标签"][0]["理由"] in record["详情"]["提取理由"]
