"""Registered category corrections must retain content and reject ambiguity."""
import copy
import json

from coerce_delta import coerce_delta_in_place
from fault_recovery import preservation_errors, preserve_quality_notes
from registered_types import align_registered_types
from story_schema_rules import DETAIL_REF_RE, make_empty_structure


def sample():
    """Reproduce chapter 11 without storing private book text in the repository."""
    current = make_empty_structure()
    current["其他事项集"] = [{"名称": "隐秘情愫"}]
    narrative = "这份完整分析描述了角色在告别之后流露的情感，修正类别不能删减这段叙述。"
    delta = {"新增元素": {"事件集": [{"名称": "告别", "详情": {
        "关联线索": ["线索:隐秘情愫"], "内容": narrative}}]},
        "修改元素": {"线索集": [{"名称": "隐秘情愫", "详情": {"本章进展": narrative}}]},
        "冲突声明": ["这份旧的说明错误地将已经登记的情愫事项描述为线索，修复时应更正该说明。"]}
    return current, delta


def test_registered_migration_retains_record_and_reference_and_is_idempotent():
    current, delta = sample()
    original = copy.deepcopy(delta["修改元素"]["线索集"][0])
    assert align_registered_types(delta, current)
    assert delta["修改元素"]["线索集"] == []
    assert delta["修改元素"]["其他事项集"] == [original]
    detail = delta["新增元素"]["事件集"][0]["详情"]
    assert detail["关联线索"] == []
    assert detail["关联其他事项"] == ["其他事项:隐秘情愫"]
    assert align_registered_types(delta, current) == []


def test_ambiguous_or_conflicting_categories_are_never_guessed():
    current, delta = sample()
    current["物品集"] = [{"名称": "隐秘情愫"}]
    original = copy.deepcopy(delta)
    assert align_registered_types(delta, current) == []
    assert delta == original
    current["物品集"] = []
    delta["修改元素"]["其他事项集"] = [{"名称": "隐秘情愫", "详情": {"冲突": "另一份补丁"}}]
    align_registered_types(delta, current)
    assert delta["修改元素"]["线索集"]  # Do not silently merge two patches.


def test_preservation_accepts_proven_migration_but_rejects_deleted_narrative(tmp_path):
    current, old = sample()
    new = copy.deepcopy(old)
    align_registered_types(new, current)
    new["冲突声明"] = ["按正式索引更正为其他事项。"]
    before, after = tmp_path / "old.json", tmp_path / "new.json"
    before.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    after.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    assert preservation_errors(before, after, current)  # Changed metadata needs history.
    preserve_quality_notes(before, after)
    assert preservation_errors(before, after, current) == []
    snapshot = after.read_bytes()
    preserve_quality_notes(before, after)
    assert after.read_bytes() == snapshot
    new = json.loads(after.read_text(encoding="utf-8"))
    new["修改元素"]["其他事项集"][0]["详情"]["本章进展"] = "删减分析"
    after.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    assert preservation_errors(before, after, current)


def test_other_item_reference_survives_mechanical_coercion():
    current, delta = sample()
    assert DETAIL_REF_RE.fullmatch("其他事项:隐秘情愫")
    coerce_delta_in_place(delta, current)
    detail = delta["新增元素"]["事件集"][0]["详情"]
    assert detail["关联其他事项"] == ["其他事项:隐秘情愫"]


def test_malformed_names_remain_for_validation_instead_of_crashing():
    current, delta = sample()
    delta["修改元素"]["线索集"] = [{"名称": ["非法数组"]}]
    align_registered_types(delta, current)
    assert delta["修改元素"]["线索集"] == [{"名称": ["非法数组"]}]


def test_new_same_name_in_other_category_prevents_automatic_reclassification():
    current, delta = sample()
    delta["新增元素"]["线索集"] = [{"名称": "隐秘情愫", "介绍": "同名但另有含义，待核实"}]
    before = copy.deepcopy(delta)
    assert align_registered_types(delta, current) == []
    assert delta == before


def test_preservation_allows_tag_deduplication_but_rejects_lost_unique_content(tmp_path):
    """Chapter 11 contains identical main/supplementary tags; both are evidence."""
    from compress_tags import compress_item

    item = {"名称": "测试角色", "标签集": ["可靠", "人物类型:主角"],
            "详情": {"补充标签": json.dumps(["可靠", "旧标签"], ensure_ascii=False),
                     "分析": "完整分析不能随着标签归位而删改，这是独立于标签集合的业务内容。"}}
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    compress_item(item, limit=8)
    after.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    assert preservation_errors(before, after) == []
    item["详情"]["补充标签"] = json.dumps(["人物类型:主角"], ensure_ascii=False)
    after.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    assert any("旧标签" in error for error in preservation_errors(before, after))
    item["详情"]["补充标签"] = json.dumps(["人物类型:主角", "旧标签"], ensure_ascii=False)
    item["详情"]["分析"] = "被删减"
    after.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    assert any("原叙述" in error for error in preservation_errors(before, after))


def test_preservation_does_not_drop_unparseable_supplementary_tags(tmp_path):
    item = {"名称": "测试角色", "标签集": ["可靠"], "详情": {"补充标签": "无法解析的原有标签"}}
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    del item["详情"]["补充标签"]
    after.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    assert preservation_errors(before, after)
