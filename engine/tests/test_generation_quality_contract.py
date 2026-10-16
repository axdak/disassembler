"""生成、入库与局部修复应遵守同一份可用性契约。"""
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from local_json_patch import apply_scoped_patch, delta_repair_scope
from story_schema_rules import DEFAULTS, make_empty_structure, validate_detail_schema
from validate_delta import delta_errors, validate_tag_governance_ops


def event():
    """包含可验证事实的完整事件；不依赖真实模型或外部书籍。"""
    item = copy.deepcopy(DEFAULTS["事件集"])
    item.update(名称="雪天游戏", 分组="雪天游戏与童年生活", 标签集=["童年生活+场景铺垫+温暖+环境氛围"],
                介绍="孩子们在雪天一起游戏。", 详情={"涉及章节": "0001", "提取理由": "原文描写了雪天游戏。"})
    return item


def delta(item):
    return {"章节": "0001", "新增元素": {"事件集": [item]}, "修改元素": {}}


@pytest.mark.parametrize("field", ["标签集", "分组", "参与成员", "介绍"])
def test_new_event_missing_formal_field_is_repairable_error(field):
    item = event()
    item.pop(field)
    errors, _ = delta_errors(make_empty_structure(), delta(item))
    assert any(f"/新增元素/事件集/0/{field}" in error for error in errors)


@pytest.mark.parametrize("field,value", [("标签集", []), ("分组", " "), ("介绍", "")])
def test_empty_critical_content_cannot_be_normalized_into_success(field, value):
    item = event()
    item[field] = value
    errors, _ = delta_errors(make_empty_structure(), delta(item))
    assert any(f"/新增元素/事件集/0/{field}" in error for error in errors)


def test_sparse_update_does_not_revalidate_inherited_legacy_defects():
    story = make_empty_structure()
    item = event()
    item.update(标签集=[], 分组="", 介绍="")
    story["事件集"] = [item]
    patch = {"章节": "0001", "新增元素": {}, "修改元素": {"事件集": [
        {"名称": item["名称"], "详情": {"提取理由": "本章补充有依据的动作。", "动作链条": "孩子们继续玩耍。"}}]}}
    assert delta_errors(story, patch)[0] == []


def test_conflicting_nested_standard_field_cannot_hide_from_validation():
    item = event()
    item["详情"]["标签集"] = '["另一主题+冲突升级+压抑+人际冲突"]'
    errors, _ = delta_errors(make_empty_structure(), delta(item))
    assert any("详情.标签集" in error and "顶层" in error for error in errors)


@pytest.mark.parametrize("value", ['["角色:不存在"]', "角色:不存在", ["地点:村庄"]])
def test_explicit_role_reference_field_checks_container_and_target_type(value):
    errors, warnings = [], []
    validate_detail_schema({"关联角色": value}, "事件", {"地点集": {"村庄"}}, errors, warnings, mode="final")
    assert errors


def test_event_compound_tag_cannot_be_demoted_even_when_text_is_conserved():
    story = make_empty_structure()
    story["事件集"] = [event()]
    patch = {"治理操作": {"治理标签": [{"类型": "事件", "名称": "雪天游戏", "保留标签": [],
            "降级标签": event()["标签集"], "理由": "误把复合标签当成描述。"}]}}
    assert any("复合标签" in error for error in validate_tag_governance_ops(story, patch, "governance"))


def test_generation_schemas_and_prompt_contract_have_one_source():
    from generation_contract import build_generation_schema, generation_contract_text
    root = Path(__file__).resolve().parents[1]
    for kind, name in [("delta", "delta.schema.json"), ("governance", "audit_correction.schema.json")]:
        persisted = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert persisted == build_generation_schema(kind)
        addition = persisted["properties"]["新增元素"]["properties"]["事件集"]["items"]
        assert set(DEFAULTS["事件集"]) - {"时间"} <= set(addition["required"])
        modification = persisted["properties"]["修改元素"]["properties"]["事件集"]["items"]
        assert set(modification["required"]) == {"名称", "详情"}
    text = generation_contract_text()
    assert all(field in text for field in ["关联角色", "标签集", "分组", "参与成员"])
    # A legacy derived tag is a hypothesis until checked against the original.
    assert "旧标签、补充标签及档案概括不构成事实依据" in text
    assert "不得把猜测、传闻、条件或未完成行动改写为已证结论" in text


@pytest.mark.parametrize("entrypoint", ["shared", "chapter", "periodic", "final_repair"])
def test_emitted_contract_requires_literal_plus_in_one_four_dimension_tag(tmp_path, entrypoint):
    """All real prompt consumers must disambiguate the delimiter before generation."""
    from generation_contract import generation_contract_text
    from prompt_loader import render_prompt
    if entrypoint == "shared":
        prompt = generation_contract_text()
    elif entrypoint == "chapter":
        prompt = render_prompt("流水线/单章增量提取.j2", chapter_id="0001",
                               delta_path="delta.json", summary="正式档案索引",
                               analysis_text="章节分析", chapter_text="本章原文")
    elif entrypoint == "periodic":
        prompt = render_prompt("流水线/周期审计.j2", start=1, end=1,
                               correction_path="correction.json", run_pipeline_path="run_pipeline.py",
                               project_dir=str(tmp_path), chapters=[], synopsis_max_chars=1000,
                               synopsis_sections="【主线】【主角处境】【核心冲突】【当前进展】",
                               taxonomy_json="", full_story_json="", story_index="", relevant_details="")
    else:
        from fault_recovery import _inline_final_scope
        reference = tmp_path / "reference.json"
        reference.write_text(json.dumps({"事件集": [event()]}, ensure_ascii=False), encoding="utf-8")
        prompt = _inline_final_scope("只补当前事件标签", reference, ["/事件集/0/标签集"])
    # The validator already requires literal +; every generated task must say so.
    for instruction in ("四个非空维度", "同一个字符串", "半角+连接",
                        "不能用/、|作为维度分隔符", "不能拆成四个独立标签"):
        assert instruction in prompt


@pytest.mark.parametrize("tags,accepted", [
    (["主题+叙事功能+情绪+冲突"], True),
    (["主题/叙事功能/情绪/冲突"], False),
    (["主题|叙事功能|情绪|冲突"], False),
    (["主题", "叙事功能", "情绪", "冲突"], False),
    (["主题+ +情绪+冲突"], False),
])
def test_existing_compound_tag_format_gate_requires_one_nonempty_plus_string(tags, accepted):
    """Clarifying the prompt must not broaden the existing acceptance format."""
    item = event()
    item["标签集"] = tags
    errors, _ = delta_errors(make_empty_structure(), delta(item))
    invalid_tag = any("/新增元素/事件集/0/标签集" in error for error in errors)
    assert invalid_tag is not accepted


def test_quality_field_repair_keeps_unrelated_event_content(tmp_path):
    item = event()
    item.pop("标签集")
    item["介绍"] = "这份已经正确的介绍不能重写。"
    original = delta(item)
    errors, _ = delta_errors(make_empty_structure(), original)
    reference, candidate = tmp_path / "reference.json", tmp_path / "candidate.json"
    reference.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    scope = delta_repair_scope(reference, "\n".join(errors), make_empty_structure())
    path = "/新增元素/事件集/0/标签集"
    assert set(scope) == {path}
    patch = {"base_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
             "changes": [{"path": path, "value": event()["标签集"]}]}
    apply_scoped_patch(reference, candidate, patch, scope)
    fixed = json.loads(candidate.read_text(encoding="utf-8"))
    assert fixed["新增元素"]["事件集"][0]["介绍"] == item["介绍"]
    assert delta_errors(make_empty_structure(), fixed)[0] == []
    patch["changes"][0]["value"] = "伪装成字符串的标签"
    with pytest.raises(ValueError):
        apply_scoped_patch(reference, candidate, patch, scope)


def test_new_event_cannot_omit_chapter_provenance():
    item = event()
    item["详情"].pop("涉及章节")
    assert any("/详情/涉及章节" in error for error in delta_errors(make_empty_structure(), delta(item))[0])


def test_program_derived_time_is_optional_in_governance_generation():
    item = event()
    item.pop("时间")
    patch = {"章节范围": "第001章", "新增元素": {"事件集": [item]}, "修改元素": {}}
    assert delta_errors(make_empty_structure(), patch, "governance")[0] == []


def test_schema_compound_pattern_rejects_blank_dimension():
    from generation_contract import COMPOUND_PATTERN
    assert not re.fullmatch(COMPOUND_PATTERN, "主题+  +情绪+冲突")


def test_unique_existing_alias_in_addition_is_an_inherited_update():
    story = make_empty_structure()
    item = event()
    item["别名"] = ["旧事件名"]
    story["事件集"] = [item]
    patch = delta({"名称": "旧事件名", "详情": {"提取理由": "原文补充了同一事件的动作。"}})
    assert delta_errors(story, patch)[0] == []


def test_merged_candidate_is_checked_even_if_patch_was_complete():
    from generation_contract import chapter_quality_errors
    before = make_empty_structure()
    after = copy.deepcopy(before)
    item = event()
    after["事件集"] = [copy.deepcopy(item)]
    after["事件集"][0]["标签集"] = []
    assert chapter_quality_errors(before, after, delta(item))
    before["事件集"] = [copy.deepcopy(after["事件集"][0])]
    assert chapter_quality_errors(before, after, delta(item)) == []


def test_wrong_reference_type_is_repaired_locally_with_original_evidence(tmp_path):
    from fault_recovery import preservation_errors
    story = make_empty_structure()
    story["角色集"] = [{"名称": "孩子"}]
    story["地点集"] = [{"名称": "村庄"}]
    item = event()
    item["详情"]["关联角色"] = ["地点:村庄"]
    source = delta(item)
    errors, _ = delta_errors(story, source)
    reference, candidate = tmp_path / "reference.json", tmp_path / "candidate.json"
    reference.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    scope = delta_repair_scope(reference, "\n".join(errors), story)
    field = "/新增元素/事件集/0/详情/关联角色"
    assert set(scope) == {field}
    patch = {"base_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
             "changes": [{"path": field, "value": ["角色:孩子"]}]}
    apply_scoped_patch(reference, candidate, patch, scope)
    fixed = json.loads(candidate.read_text(encoding="utf-8"))
    detail = fixed["新增元素"]["事件集"][0]["详情"]
    assert "地点:村庄" in detail["引用修复原文"]
    assert detail["关联角色"] == ["角色:孩子"]
    assert preservation_errors(reference, candidate, story) == []
    assert delta_errors(story, fixed)[0] == []
    # 再修另一个关联字段时，已归档原文也必须按条目保留，不能误报整段重写。
    detail["关联地点"] = ["角色:孩子"]
    reference.write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
    errors, _ = delta_errors(story, fixed)
    scope = delta_repair_scope(reference, "\n".join(errors), story)
    path = "/新增元素/事件集/0/详情/关联地点"
    apply_scoped_patch(reference, candidate, {"base_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "changes": [{"path": path, "value": ["地点:村庄"]}]}, scope)
    assert preservation_errors(reference, candidate, story) == []


def test_reference_repair_archive_cannot_authorize_removing_valid_links(tmp_path):
    story = make_empty_structure()
    story["角色集"] = [{"名称": "孩子"}]
    item = event()
    item["详情"]["关联角色"] = ["角色:孩子", "地点:村庄"]
    original = delta(item)
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    errors, _ = delta_errors(story, original)
    scope = delta_repair_scope(reference, "\n".join(errors), story)
    assert scope
    patch = {"base_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
             "changes": [{"path": next(iter(scope)), "value": []}]}
    with pytest.raises(ValueError):
        apply_scoped_patch(reference, tmp_path / "candidate.json", patch, scope)
