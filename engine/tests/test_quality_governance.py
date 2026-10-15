"""Protect useful existing story fields while allowing explicit entity governance."""
import copy

import pytest

import governance_contract
from apply_governance_ops import apply_ops


COMPOUND = "身份线+冲突升级+爆笑+信息差"


def quality_story():
    """Small connected graph, with one deliberately incomplete historical record."""
    return {
        "介绍": {"标题": "旧书", "描述": "完整的故事介绍。"},
        "角色集": [{"名称": "主角", "别名": [], "介绍": "主角介绍", "标签集": [], "详情": {}}],
        "事件集": [{"名称": "对峙", "别名": [], "分组": "00000010-身份对峙",
                    "标签集": [COMPOUND, "临时气氛"], "参与成员": ["主角"],
                    "发生地点": "大厅", "目标事件": [], "介绍": "对峙场面介绍。",
                    "详情": {"关联角色": ["角色:主角"]}},
                   {"名称": "历史缺字段", "标签集": [], "详情": {}}],
        "地点集": [{"名称": "大厅", "别名": [], "介绍": "大厅介绍", "标签集": [], "详情": {}}],
        "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }


def demotion_patch():
    return {"治理操作": {"治理标签": [{"类型": "事件", "名称": "对峙", "保留标签": [],
                                     "降级标签": [COMPOUND, "临时气氛"], "理由": "误认为都是场面标签。"}]}}


def quality_errors(before, after, patch=None):
    return governance_contract.governance_quality_errors(before, after, patch or {})


def test_registered_scalar_reference_cannot_silently_change_identity():
    before = quality_story()
    before["地点集"].append({"名称": "医馆", "别名": [], "详情": {}})
    after = copy.deepcopy(before)
    after["事件集"][0]["发生地点"] = "医馆"
    assert any("发生地点" in error for error in quality_errors(before, after))


def test_normalizer_restores_compound_and_preserves_legal_demotion_and_audit():
    story, patch = quality_story(), demotion_patch()
    original = copy.deepcopy(patch)
    patch["治理操作"]["设置字段"] = [{"类型": "事件", "名称": "对峙", "字段": "介绍", "值": "修订介绍"}]
    normalized, changes = governance_contract.normalize_governance_patch(patch, story=story)
    op = normalized["治理操作"]["治理标签"][0]
    assert op["保留标签"] == [COMPOUND]
    assert op["降级标签"] == ["临时气氛"]
    assert op["理由"] == original["治理操作"]["治理标签"][0]["理由"]
    assert normalized["治理操作"]["设置字段"] == patch["治理操作"]["设置字段"]
    assert any(COMPOUND in change and "原始意图" in change for change in changes)
    assert patch["治理操作"]["治理标签"] == original["治理操作"]["治理标签"]
    assert governance_contract.normalize_governance_patch(normalized, story=story) == (normalized, [])


@pytest.mark.parametrize("problem", ["new", "missing", "duplicate", "overlap", "ambiguous", "alias", "duplicate_op"])
def test_normalizer_does_not_hide_ambiguous_or_invalid_tag_partition(problem):
    story, patch = quality_story(), demotion_patch()
    op = patch["治理操作"]["治理标签"][0]
    if problem == "new": op["降级标签"].append("新标签")
    if problem == "missing": op["降级标签"].remove("临时气氛")
    if problem == "duplicate": op["降级标签"].append(COMPOUND)
    if problem == "overlap": op["保留标签"].append(COMPOUND)
    if problem == "ambiguous": story["事件集"].append(copy.deepcopy(story["事件集"][0]))
    if problem == "alias":
        story["事件集"][0]["别名"] = ["别称"]
        op["名称"] = "别称"
    if problem == "duplicate_op": patch["治理操作"]["治理标签"].append(copy.deepcopy(op))
    normalized, changes = governance_contract.normalize_governance_patch(patch, story=story)
    assert normalized == patch
    assert changes == []


def test_executor_rejects_direct_compound_demotion_before_other_operations():
    story, patch = quality_story(), demotion_patch()
    original = copy.deepcopy(story)
    patch["治理操作"]["设置字段"] = [{"类型": "事件", "名称": "对峙", "字段": "介绍", "值": "已修改"}]
    with pytest.raises(ValueError, match="四维复合标签"):
        apply_ops(story, patch)
    assert story == original


@pytest.mark.parametrize("field,value", [("分组", ""), ("分组", "00000010-0001"),
                                       ("标签集", ["临时气氛"]), ("参与成员", []),
                                       ("介绍", ""), ("发生地点", "")])
def test_quality_gate_rejects_useful_event_field_moved_into_detail(field, value):
    before = quality_story()
    after = copy.deepcopy(before)
    event = after["事件集"][0]
    event["详情"][field] = event[field]
    event[field] = value
    assert any(field in error for error in quality_errors(before, after))


def test_quality_gate_rejects_clear_introduction_and_explicit_detail_references():
    before = quality_story()
    after = copy.deepcopy(before)
    after["介绍"]["描述"] = ""
    after["事件集"][0]["详情"]["关联角色"] = []
    errors = quality_errors(before, after)
    assert any("介绍.描述" in error for error in errors)
    assert any("关联角色" in error for error in errors)


def test_quality_gate_ignores_unchanged_historical_gaps_and_allows_text_revision():
    before = quality_story()
    after = copy.deepcopy(before)
    after["事件集"][0]["介绍"] = "更清楚的场面介绍。"
    assert quality_errors(before, after) == []


def test_quality_gate_accepts_rename_merge_and_reference_redirection():
    before = quality_story()
    before["角色集"].append({"名称": "旧称", "别名": [], "介绍": "另一条介绍", "标签集": [], "详情": {}})
    before["事件集"][0]["参与成员"].append("旧称")
    patch = {"治理操作": {"合并元素": [{"类型": "角色", "主": "主角", "并入": ["旧称"]}],
                        "重命名元素": [{"类型": "角色", "原名称": "主角", "新名称": "新主角"}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["事件集"][0]["参与成员"] == ["新主角"]
    assert quality_errors(before, after, patch) == []
    after["事件集"][0]["参与成员"] = []
    assert any("参与成员" in error for error in quality_errors(before, after, patch))


@pytest.mark.parametrize("action", ["删除元素", "降级元素"])
def test_quality_gate_accepts_explicit_removal_and_its_reference_cleanup(action):
    before = quality_story()
    patch = {"治理操作": {action: [{"类型": "角色", "名称": "主角", "原因": "实体不成立"}]}}
    if action == "降级元素":
        # 降级必须完整挂载档案；无挂载属于删除，不能再以降级静默执行。
        before["其他事项集"].append({"名称": "归档", "详情": {}})
        patch["治理操作"][action][0]["挂载到"] = {"类型": "其他事项", "名称": "归档"}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert quality_errors(before, after, patch) == []


def test_quality_gate_reports_ambiguous_changed_identity_scope():
    before = quality_story()
    before["事件集"].append(copy.deepcopy(before["事件集"][0]))
    after = copy.deepcopy(before)
    after["事件集"][0]["介绍"] = ""
    assert any("scope" in error and "对峙" in error for error in quality_errors(before, after))


def test_quality_gate_protects_relationship_prefixes_that_are_not_entity_types():
    before = quality_story()
    before["角色集"].append({"名称": "同伴", "关系": ["盟友:主角"], "详情": {}})
    after = copy.deepcopy(before)
    after["角色集"][-1]["关系"] = []
    assert any("关系" in error for error in quality_errors(before, after))


def test_quality_gate_allows_explicit_reference_replacement():
    before = quality_story()
    before["角色集"].append({"名称": "同伴", "详情": {}})
    patch = {"治理操作": {"替换引用": [{"类型": "角色", "原名称": "主角", "新名称": "同伴"}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["事件集"][0]["详情"]["关联角色"] == ["角色:同伴"]
    assert quality_errors(before, after, patch) == []


def test_quality_gate_does_not_promote_dangling_historical_refs_into_requirements():
    before = quality_story()
    before["事件集"][0]["参与成员"] = ["历史不存在"]
    before["事件集"][0]["详情"]["关联角色"] = ["角色:历史不存在"]
    after = copy.deepcopy(before)
    after["事件集"][0]["参与成员"] = []
    after["事件集"][0]["详情"]["关联角色"] = []
    assert quality_errors(before, after) == []


def test_normalizer_preserves_raw_valid_compound_text_without_trimming():
    story, patch = quality_story(), demotion_patch()
    raw = " 身份线+冲突升级+爆笑+信息差 "
    story["事件集"][0]["标签集"][0] = raw
    patch["治理操作"]["治理标签"][0]["降级标签"][0] = raw
    normalized, changes = governance_contract.normalize_governance_patch(patch, story=story)
    assert normalized["治理操作"]["治理标签"][0]["保留标签"] == [raw]
    assert changes


def test_quality_gate_accepts_whitespace_and_alias_reference_canonicalization():
    before = quality_story()
    before["角色集"][0]["别名"] = ["旧主角"]
    before["事件集"][0]["标签集"][0] = " " + COMPOUND + " "
    before["事件集"][0]["参与成员"] = ["旧主角"]
    before["事件集"][0]["详情"]["关联角色"] = ["角色:旧主角"]
    before["角色集"].append({"名称": "同伴", "关系": ["盟友:旧主角"], "详情": {}})
    after = copy.deepcopy(before)
    after["事件集"][0]["标签集"][0] = COMPOUND
    after["事件集"][0]["参与成员"] = ["主角"]
    after["事件集"][0]["详情"]["关联角色"] = ["角色:主角"]
    after["角色集"][-1]["关系"] = ["盟友:主角"]
    assert quality_errors(before, after) == []
