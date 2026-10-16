"""Only an exact duplicate of one formally registered record can be removed."""
import copy
import hashlib
import json

import pytest

from coerce_delta import coerce_delta_in_place
from fault_recovery import preservation_errors
from governance_contract import normalize_governance_patch
from registered_types import align_registered_types
from story_schema_rules import DEFAULTS, make_empty_structure
from validate_delta import delta_errors


def mirror_fixture():
    """Reproduce the category mirror without retaining private source text."""
    current = make_empty_structure()
    current["介绍"] = {"标题": "镜像验证", "描述": "正式类别中的完整档案必须保留。"}
    location = copy.deepcopy(DEFAULTS["地点集"])
    location.update(名称="谷中祭台", 介绍="旧档案描述祭台位于山谷中央。")
    location["详情"] = {"首次章节": "0001", "最近章节": "0001"}
    current["地点集"] = [location]
    item = {"名称": "谷中祭台", "介绍": "补充描述祭台的完整结构与用途，错误类别镜像不得导致这些原始分析内容丢失。",
            "详情": {"提取理由": "本章描述了山谷内祭台的完整结构，按正式地点登记补充原档案。"}}
    patch = {"章节范围": "第161-165章", "新增元素": {},
             "修改元素": {"物品集": [copy.deepcopy(item)], "地点集": [item]}}
    return current, patch


def test_exact_mirror_keeps_destination_object_and_logs_idempotently():
    current, patch = mirror_fixture()
    destination = patch["修改元素"]["地点集"][0]
    original = copy.deepcopy(destination)
    # JSON object key order has no meaning; array order and scalar types do.
    patch["修改元素"]["物品集"][0] = dict(reversed(list(destination.items())))
    report = align_registered_types(patch, current)
    assert patch["修改元素"]["物品集"] == []
    assert patch["修改元素"]["地点集"][0] is destination
    assert destination == original
    assert len(report) == 1 and "重复镜像" in report[0]
    assert align_registered_types(patch, current) == []


def test_real_coerce_validation_and_preservation_entry(tmp_path):
    current, reference = mirror_fixture()
    repaired = copy.deepcopy(reference)
    before, after = tmp_path / "reference.json", tmp_path / "candidate.json"
    before.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
    report = coerce_delta_in_place(repaired, current)
    after.write_text(json.dumps(repaired, ensure_ascii=False), encoding="utf-8")
    assert report
    assert delta_errors(current, repaired, "governance")[0] == []
    assert preservation_errors(before, after, current) == []
    assert json.loads(before.read_text(encoding="utf-8")) == reference


@pytest.mark.parametrize("difference", ["extra_field", "reason", "array_order", "bool_number", "float_number"])
def test_nonidentical_same_name_proposals_are_untouched(difference):
    current, patch = mirror_fixture()
    source = patch["修改元素"]["物品集"][0]
    destination = patch["修改元素"]["地点集"][0]
    if difference == "extra_field":
        source["详情"]["旧事实"] = "保留"
    elif difference == "reason":
        source["详情"]["提取理由"] += "额外依据。"
    elif difference == "array_order":
        source["详情"]["原列表"] = ["甲", "乙"]
        destination["详情"]["原列表"] = ["乙", "甲"]
    else:
        source["详情"]["数值"] = 1
        destination["详情"]["数值"] = True if difference == "bool_number" else 1.0
    original = copy.deepcopy(patch)
    assert align_registered_types(patch, current) == []
    assert patch == original
    assert delta_errors(current, patch, "governance")[0]
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized["修改元素"]["物品集"] == original["修改元素"]["物品集"]


@pytest.mark.parametrize("ambiguity", ["two_destinations", "same_category_registry", "cross_category_registry", "new_category", "malformed_destination"])
def test_ambiguous_registration_or_destination_never_deduplicates(ambiguity):
    current, patch = mirror_fixture()
    if ambiguity == "two_destinations":
        patch["修改元素"]["地点集"].append(copy.deepcopy(patch["修改元素"]["地点集"][0]))
    elif ambiguity == "same_category_registry":
        current["地点集"].append(copy.deepcopy(current["地点集"][0]))
    elif ambiguity == "cross_category_registry":
        current["物品集"] = [{"名称": "谷中祭台"}]
    elif ambiguity == "new_category":
        patch["新增元素"]["物品集"] = [{"名称": "谷中祭台"}]
    else:
        patch["修改元素"]["地点集"] = {"名称": "谷中祭台"}
    original = copy.deepcopy(patch)
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized["修改元素"]["物品集"] == original["修改元素"]["物品集"]


@pytest.mark.parametrize("encoded_collections", [False, True])
def test_governance_normalization_aligns_decoded_mirror_without_mutating_input(encoded_collections):
    current, patch = mirror_fixture()
    if encoded_collections:
        patch["修改元素"] = {key: json.dumps(value, ensure_ascii=False)
                          for key, value in patch["修改元素"].items()}
    original = copy.deepcopy(patch)
    normalized, changes = normalize_governance_patch(patch, story=current)
    assert normalized["修改元素"]["物品集"] == []
    assert any("重复镜像" in change for change in changes)
    assert patch == original
    assert normalize_governance_patch(normalized, story=current) == (normalized, [])
    without_story, _ = normalize_governance_patch(patch)
    assert len(without_story["修改元素"]["物品集"]) == 1


@pytest.mark.parametrize("source_value", [161, "第161章"])
def test_normalization_does_not_deduplicate_different_raw_trace_values(source_value):
    current, patch = mirror_fixture()
    patch["修改元素"]["物品集"][0]["详情"]["首次章节"] = source_value
    patch["修改元素"]["地点集"][0]["详情"]["首次章节"] = "0161"
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert len(normalized["修改元素"]["物品集"]) == 1
    assert delta_errors(current, normalized, "governance")[0]


@pytest.mark.parametrize("conflict", [None, "different_reason", "new_category", "two_destinations"])
def test_raw_mirror_actual_commit_accepts_only_exact_unambiguous_duplicate(tmp_path, conflict):
    """Commit raw input directly: no pre-coercion may hide a missing host entry."""
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    current, patch = mirror_fixture()
    pipeline.init_dirs(tmp_path)
    write_completed_chapter(tmp_path, 165)
    story_path = tmp_path / "故事结构_增量.json"
    write_json(story_path, current)
    pipeline.ensure_story(tmp_path)
    base = story_path.read_bytes()
    if conflict == "different_reason":
        patch["修改元素"]["物品集"][0]["详情"]["提取理由"] += "另一份独立理由。"
    elif conflict == "new_category":
        patch["新增元素"]["物品集"] = [copy.deepcopy(patch["修改元素"]["物品集"][0])]
    elif conflict == "two_destinations":
        patch["修改元素"]["地点集"].append(copy.deepcopy(patch["修改元素"]["地点集"][0]))
    raw_input = copy.deepcopy(patch)
    output = tmp_path / "质量治理/按需治理/correction_mirror_probe.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(base).hexdigest())
    rc = pipeline.commit_governance(tmp_path, str(output))
    originals = list(output.parent.glob("original_input_*.json"))
    assert len(originals) == 1
    assert json.loads(originals[0].read_text(encoding="utf-8")) == raw_input
    if conflict:
        assert rc != 0
        assert story_path.read_bytes() == base
        assert not output.with_suffix(".commit.json").exists()
        assert json.loads(output.read_text(encoding="utf-8"))["修改元素"]["物品集"]
        return
    assert rc == 0
    updated = json.loads(story_path.read_text(encoding="utf-8"))
    assert updated["物品集"] == []
    assert len(updated["地点集"]) == 1
    assert patch["修改元素"]["地点集"][0]["介绍"] in updated["地点集"][0]["介绍"]
    assert output.with_suffix(".commit.json").is_file()

def registered_alias_fixture(*, ambiguous=False, destination_present=True):
    """A same spelling can name a role by alias and an item canonically."""
    current = make_empty_structure()
    current["介绍"] = {"标题": "别名身份验证", "描述": "不同类别各自保留合法身份。"}
    for name in (["守卫甲", "守卫乙"] if ambiguous else ["守卫甲"]):
        role = copy.deepcopy(DEFAULTS["角色集"])
        role.update(名称=name, 别名=["凤凰"], 介绍=name + "的原始完整档案。")
        role["详情"] = {"首次章节": "0001", "最近章节": "0001"}
        current["角色集"].append(role)
    item = copy.deepcopy(DEFAULTS["物品集"])
    item.update(名称="凤凰", 介绍="以凤凰命名的物品原档案。")
    item["详情"] = {"首次章节": "0001", "最近章节": "0001"}
    current["物品集"] = [item]
    proposal = {"名称": "凤凰", "介绍": "角色与物品均有独立证据支持的完整补充描述，不得因同名而静默丢弃任意一份。",
                "详情": {"提取理由": "本章分别补充了使用凤凰别名的守卫和正式名为凤凰的物品。"}}
    patch = {"章节范围": "第161-165章", "新增元素": {},
             "修改元素": {"角色集": [copy.deepcopy(proposal)],
                          "物品集": [copy.deepcopy(proposal)] if destination_present else []}}
    return current, patch


@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("destination_present", [False, True])
def test_registered_source_alias_blocks_migration_and_mirror_removal(ambiguous, destination_present):
    current, patch = registered_alias_fixture(ambiguous=ambiguous, destination_present=destination_present)
    original = copy.deepcopy(patch)
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, changes = normalize_governance_patch(patch, story=current)
    assert normalized == original and changes == []
    errors, _ = delta_errors(current, normalized, "governance")
    assert bool(errors) is ambiguous


def test_preservation_rejects_deleting_a_valid_source_alias_update(tmp_path):
    current, reference = registered_alias_fixture()
    candidate = copy.deepcopy(reference)
    candidate["修改元素"]["角色集"] = []
    before, after = tmp_path / "reference.json", tmp_path / "candidate.json"
    before.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
    after.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    assert preservation_errors(before, after, current)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_raw_commit_preserves_alias_updates_or_rejects_ambiguous_identity(tmp_path, ambiguous):
    """The real commit updates both namespaces or rejects a conflicting alias."""
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    current, patch = registered_alias_fixture(ambiguous=ambiguous)
    pipeline.init_dirs(tmp_path)
    write_completed_chapter(tmp_path, 165)
    story_path = tmp_path / "故事结构_增量.json"
    write_json(story_path, current)
    pipeline.ensure_story(tmp_path)
    base = story_path.read_bytes()
    output = tmp_path / "质量治理/按需治理/correction_alias_mirror_probe.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(base).hexdigest())
    rc = pipeline.commit_governance(tmp_path, str(output))
    if ambiguous:
        assert rc != 0
        assert story_path.read_bytes() == base
        assert not output.with_suffix(".commit.json").exists()
        assert "匹配多个元素" in output.with_suffix(".repair.md").read_text(encoding="utf-8")
        return
    assert rc == 0
    updated = json.loads(story_path.read_text(encoding="utf-8"))
    addition = patch["修改元素"]["角色集"][0]["介绍"]
    assert updated["角色集"][0]["名称"] == "守卫甲"
    assert addition in updated["角色集"][0]["介绍"]
    assert addition in updated["物品集"][0]["介绍"]

@pytest.mark.parametrize("ambiguous", [False, True])
def test_registered_source_alias_keeps_typed_reference_in_its_namespace(ambiguous):
    current, patch = registered_alias_fixture(ambiguous=ambiguous)
    patch["修改元素"]["角色集"] = []
    detail = patch["修改元素"]["物品集"][0]["详情"]
    detail["关联角色"] = ["角色:凤凰"]
    original = copy.deepcopy(patch)
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, changes = normalize_governance_patch(patch, story=current)
    assert normalized == original and changes == []
    assert bool(delta_errors(current, normalized, "governance")[0]) is ambiguous


@pytest.mark.parametrize("declaration", ["new_alias", "modified_alias"])
def test_declared_source_alias_keeps_update_in_its_category(declaration):
    current, patch = registered_alias_fixture(destination_present=False)
    role = copy.deepcopy(current["角色集"][0])
    if declaration == "new_alias":
        current["角色集"] = []
        role["详情"]["提取理由"] = "原文介绍守卫甲及其凤凰代号。"
        patch["新增元素"]["角色集"] = [role]
    else:
        current["角色集"][0]["别名"] = []
        patch["修改元素"]["角色集"].insert(0, {"名称": "守卫甲", "别名": ["凤凰"],
            "详情": {"提取理由": "明确声明已有角色的凤凰别名。"}})
    original = copy.deepcopy(patch)
    before_errors = delta_errors(current, patch, "governance")[0]
    assert bool(before_errors) is (declaration == "modified_alias")
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, changes = normalize_governance_patch(patch, story=current)
    assert normalized == original and changes == []
    assert bool(delta_errors(current, normalized, "governance")[0]) is bool(before_errors)


@pytest.mark.parametrize("declaration", ["new_alias", "modified_alias"])
def test_declared_source_alias_does_not_reroute_invalid_typed_reference(declaration):
    current, patch = registered_alias_fixture()
    role = copy.deepcopy(current["角色集"][0])
    patch["修改元素"]["角色集"] = []
    if declaration == "new_alias":
        current["角色集"] = []
        role["详情"]["提取理由"] = "原文介绍守卫甲及其凤凰代号。"
        patch["新增元素"]["角色集"] = [role]
    else:
        current["角色集"][0]["别名"] = []
        patch["修改元素"]["角色集"] = [{"名称": "守卫甲", "别名": ["凤凰"],
            "详情": {"提取理由": "明确声明已有角色的凤凰别名。"}}]
    patch["修改元素"]["物品集"][0]["详情"]["关联角色"] = ["角色:凤凰"]
    original = copy.deepcopy(patch)
    assert delta_errors(current, patch, "governance")[0]
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized == original
    assert delta_errors(current, normalized, "governance")[0]


@pytest.mark.parametrize("declaration", ["new_alias", "rename", "ref_only_rename"])
def test_raw_commit_keeps_same_patch_declared_role_update(tmp_path, declaration):
    """Existing identity projection keeps sparse updates with the intended role."""
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    current, patch = registered_alias_fixture(destination_present=False)
    if declaration == "new_alias":
        role = current["角色集"].pop()
        role["详情"]["提取理由"] = "原文介绍守卫甲及其凤凰代号。"
        patch["新增元素"]["角色集"] = [role]
    else:
        current["角色集"][0]["别名"] = []
        patch["治理操作"] = {"重命名元素": [{"类型": "角色", "原名称": "守卫甲", "新名称": "凤凰"}]}
        if declaration == "ref_only_rename":
            patch["修改元素"]["角色集"] = []
            patch["修改元素"]["物品集"] = [{"名称": "凤凰", "详情": {
                "提取理由": "物品与更名后的角色有关联。", "关联角色": ["角色:凤凰"]}}]
    pipeline.init_dirs(tmp_path)
    write_completed_chapter(tmp_path, 165)
    story_path = tmp_path / "故事结构_增量.json"
    write_json(story_path, current)
    pipeline.ensure_story(tmp_path)
    base = story_path.read_bytes()
    output = tmp_path / "质量治理/按需治理/correction_declared_alias_probe.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(base).hexdigest())
    rc = pipeline.commit_governance(tmp_path, str(output))
    if declaration == "ref_only_rename":
        assert rc != 0
        assert story_path.read_bytes() == base
        assert not output.with_suffix(".commit.json").exists()
        assert json.loads(output.read_text(encoding="utf-8"))["修改元素"]["物品集"][0]["详情"]["关联角色"] == ["角色:凤凰"]
        return
    assert rc == 0
    updated = json.loads(story_path.read_text(encoding="utf-8"))
    addition = patch["修改元素"]["角色集"][0]["介绍"]
    assert addition in updated["角色集"][0]["介绍"]
    assert addition not in updated["物品集"][0]["介绍"]

@pytest.mark.parametrize("valid", [False, True])
def test_same_patch_rename_keeps_source_namespace_or_original_validation_error(valid):
    current, patch = registered_alias_fixture(destination_present=False)
    current["角色集"][0]["别名"] = []
    patch["治理操作"] = {"重命名元素": [{"类型": "角色", "原名称": "守卫甲" if valid else "不存在", "新名称": "凤凰"}]}
    original = copy.deepcopy(patch)
    assert bool(delta_errors(current, patch, "governance")[0]) is not valid
    assert align_registered_types(patch, current) == []
    assert patch == original
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized == original
    assert bool(delta_errors(current, normalized, "governance")[0]) is not valid


def test_new_formal_source_name_also_blocks_cross_category_alignment():
    current, patch = registered_alias_fixture(destination_present=False)
    role = current["角色集"].pop()
    role.update(名称="凤凰", 别名=[])
    role["详情"]["提取理由"] = "原文明确新增角色的正式名称为凤凰。"
    patch["新增元素"]["角色集"] = [role]
    original = copy.deepcopy(patch)
    assert delta_errors(current, patch, "governance")[0] == []
    assert align_registered_types(patch, current) == []
    assert patch == original


def test_governance_decodes_rename_container_before_identity_alignment():
    current, patch = registered_alias_fixture(destination_present=False)
    current["角色集"][0]["别名"] = []
    renames = [{"类型": "角色", "原名称": "守卫甲", "新名称": "凤凰"}]
    patch["治理操作"] = {"重命名元素": json.dumps(renames, ensure_ascii=False)}
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized["治理操作"]["重命名元素"] == renames
    assert normalized["修改元素"] == patch["修改元素"]
    assert delta_errors(current, normalized, "governance")[0] == []

@pytest.mark.parametrize("encoded_renames", [False, True])
def test_ref_only_rename_declaration_prevents_cross_category_routing(encoded_renames):
    current, patch = registered_alias_fixture()
    current["角色集"][0]["别名"] = []
    patch["修改元素"]["角色集"] = []
    patch["修改元素"]["物品集"][0]["详情"]["关联角色"] = ["角色:凤凰"]
    renames = [{"类型": "角色", "原名称": "守卫甲", "新名称": "凤凰"}]
    patch["治理操作"] = {"重命名元素": json.dumps(renames, ensure_ascii=False) if encoded_renames else renames}
    normalized, _ = normalize_governance_patch(patch, story=current)
    assert normalized["修改元素"] == patch["修改元素"]
    assert delta_errors(current, normalized, "governance")[0]
