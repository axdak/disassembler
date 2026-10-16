"""终稿与新增共用字段契约，修复限定具体字段和有进展的有限批次。"""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from story_schema_rules import DEFAULTS, make_empty_structure, validate_exact_story_schema
from local_json_patch import apply_scoped_patch


def complete_story():
    story = make_empty_structure()
    story["介绍"] = {"标题": "救援故事", "描述": "甲救人后离开现场。"}
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="甲", 介绍="甲是救援行动的参与者。", 详情={"首次章节":"0001","最近章节":"0001"})
    story["角色集"] = [role]
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="甲救人", 参与成员=["甲"], 分组="00000010-救援脱险", 标签集=["救援+转折+紧张+生存冲突"],
                 介绍="甲救下受困的人后离开。", 详情={"涉及章节": "0001", "提取理由": "原文明确救人。"})
    story["事件集"] = [event]
    return story


@pytest.mark.parametrize("field,value", [("分组", ""), ("标签集", [])])
def test_final_rejects_critical_empty_event_field_with_repair_pointer(field, value):
    story = complete_story(); story["事件集"][0][field] = value
    errors, _ = validate_exact_story_schema(story, mode="final")
    assert any(f"[质量字段:/事件集/0/{field}]" in error for error in errors)
    assert validate_exact_story_schema(story, mode="process")[0] == []


def test_final_flags_conflicting_misplaced_standard_field():
    story = complete_story(); story["事件集"][0]["详情"]["参与成员"] = '["乙"]'
    assert any("[质量字段:/事件集/0/参与成员]" in e for e in validate_exact_story_schema(story, mode="final")[0])


def test_final_field_patch_preserves_unrelated_content_and_conflict_evidence(tmp_path):
    from local_json_patch import final_repair_scope
    story = complete_story(); event = story["事件集"][0]
    event["分组"] = ""; event["详情"]["参与成员"] = '["乙"]'
    source, target = tmp_path/"draft.json", tmp_path/"candidate.json"
    source.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    feedback = "\n".join(validate_exact_story_schema(story,mode="final")[0])
    scope = final_repair_scope(source, feedback)
    assert set(scope) == {"/事件集/0/分组", "/事件集/0/参与成员"}
    patch = {"base_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "changes": [
        {"path":"/事件集/0/分组","value":"00000010-救援脱险"}, {"path":"/事件集/0/参与成员","value":["甲"]}]}
    apply_scoped_patch(source,target,patch,scope)
    current=json.loads(target.read_text(encoding="utf-8"))
    assert current["事件集"][0]["介绍"] == event["介绍"]
    assert "参与成员" not in current["事件集"][0]["详情"]
    assert "乙" in current["事件集"][0]["详情"]["字段修复原文"]
    assert validate_exact_story_schema(current,mode="final")[0] == []
    from fault_recovery import preservation_errors
    assert preservation_errors(source,target) == []


def test_final_partial_progress_only_accepts_its_actual_batch(tmp_path, monkeypatch):
    import local_json_patch as patcher
    monkeypatch.setattr(patcher,"FINAL_REPAIR_BATCH_FIELDS",1)
    story=complete_story(); story["角色集"][0]["介绍"]="";story["事件集"][0]["介绍"]=""
    source,target=tmp_path/"draft.json",tmp_path/"candidate.json"
    source.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    story["角色集"][0]["介绍"]="甲是救援行动的参与者。"
    target.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    assert patcher.final_repair_progress(source,target)
    story["事件集"][0]["参与成员"]=["不存在"]
    target.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    assert not patcher.final_repair_progress(source,target)


def test_final_scope_marks_reference_errors_and_limits_large_backlogs(tmp_path, monkeypatch):
    import local_json_patch as patcher
    monkeypatch.setattr(patcher,"FINAL_REPAIR_BATCH_FIELDS",2)
    story=complete_story(); story["事件集"][0]["参与成员"]=["不存在"]
    for index in range(5):
        role=copy.deepcopy(DEFAULTS["角色集"]);role["名称"]=f"路人{index}";story["角色集"].append(role)
    source=tmp_path/"draft.json";source.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    errors,_=validate_exact_story_schema(story,mode="final")
    assert any("[质量字段:/事件集/0/参与成员]" in e for e in errors)
    assert len(patcher.final_repair_scope(source,"\n".join(errors))) == 2


def test_final_driver_completes_more_than_one_field_batch(tmp_path, monkeypatch):
    import local_json_patch as patcher
    import orchestrator
    from orchestrator import Orchestrator, Config
    monkeypatch.setattr(patcher, "FINAL_REPAIR_BATCH_FIELDS", 1)
    story = complete_story()
    story["角色集"][0]["介绍"] = ""; story["事件集"][0]["介绍"] = ""
    draft = tmp_path / "故事结构_草稿.json"
    draft.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    repair = tmp_path / "质量治理/最终审计/repair_final.md"
    repair.parent.mkdir(parents=True)
    def strict(project):
        errors = validate_exact_story_schema(json.loads(draft.read_text(encoding="utf-8")), mode="final")[0]
        repair.write_text("\n".join(errors), encoding="utf-8")
        return int(bool(errors))
    calls = []
    def worker(command, project, task, pack, output, **kwargs):
        scope = patcher.final_repair_scope(draft, pack.read_text(encoding="utf-8"))
        calls.append((list(scope), pack.read_text(encoding="utf-8")))
        assert len(scope) == 1
        pointer = next(iter(scope)); _, collection, index, field = pointer.split("/")
        data = json.loads(draft.read_text(encoding="utf-8"))
        data[collection][int(index)][field] = "经原文确认的救援参与及作用。"
        draft.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return 0
    monkeypatch.setattr(orchestrator, "cmd_commit_final_draft", strict)
    monkeypatch.setattr(orchestrator, "run_repair_worker", worker)
    assert Orchestrator(tmp_path, config=Config())._stage_commit_final_draft() == 0
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert "本批候选摘要" in calls[0][1]


# 已登记角色的裸名现可按明确字段归一；未知目标仍必须留下局部修复定位。
@pytest.mark.parametrize("field,value", [("关联角色", ["未登记角色"]), ("关联事项", ["其他事项集:策略"])])
def test_final_bad_detail_reference_has_local_repair_pointer(tmp_path, field, value):
    from normalize_story_schema import normalize_structure
    from local_json_patch import final_repair_scope
    story = complete_story(); story["角色集"][0]["详情"][field] = value
    story = normalize_structure(story)[0]
    source = tmp_path / "draft.json"
    source.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    errors = validate_exact_story_schema(story, mode="final")[0]
    assert errors
    assert f"/角色集/0/详情/{field}" in final_repair_scope(source, "\n".join(errors))


def test_final_bad_controlled_tag_can_be_archived_and_corrected(tmp_path):
    from local_json_patch import final_repair_scope
    from fault_recovery import preservation_errors
    story = complete_story(); story["角色集"][0]["标签集"] = ["人物类型:无效"]
    source, target = tmp_path / "draft.json", tmp_path / "candidate.json"
    source.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
    feedback = "\n".join(validate_exact_story_schema(story, mode="final")[0])
    scope = final_repair_scope(source, feedback)
    patch = {"base_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
             "changes":[{"path":"/角色集/0/标签集", "value":["人物类型:主角"]}]}
    apply_scoped_patch(source, target, patch, scope)
    assert validate_exact_story_schema(json.loads(target.read_text(encoding="utf-8")), mode="final")[0] == []
    assert preservation_errors(source, target) == []


def test_final_archive_does_not_expand_delta_prose_rewrite_permission(tmp_path):
    from fault_recovery import preservation_errors
    source, target = tmp_path / "delta.json", tmp_path / "candidate.json"
    before = {"新增元素":{"角色集":[{"名称":"甲","介绍":"甲在救援中坚持搜寻被困者，确认危险地点后带着绳索逐个救人，最后才离开现场。","详情":{}}]}}
    after = copy.deepcopy(before)
    role = after["新增元素"]["角色集"][0]
    original = role["介绍"]; role["介绍"] = "甲救人。"
    role["详情"]["字段修复原文"] = json.dumps([json.dumps({"字段":"介绍","原值":original}, ensure_ascii=False)], ensure_ascii=False)
    source.write_text(json.dumps(before,ensure_ascii=False),encoding="utf-8")
    target.write_text(json.dumps(after,ensure_ascii=False),encoding="utf-8")
    assert preservation_errors(source,target)


def test_final_custom_reference_array_keeps_exact_archived_old_value(tmp_path):
    """普通详情数组也遵循类型引用协议，其坏引用可同字段归档后修复。"""
    from local_json_patch import final_repair_scope
    from fault_recovery import preservation_errors
    story = complete_story()
    story["角色集"][0]["详情"]["见证人"] = ["角色:不存在", "角色:甲"]
    source, target = tmp_path/"draft.json", tmp_path/"candidate.json"
    source.write_text(json.dumps(story,ensure_ascii=False),encoding="utf-8")
    scope = final_repair_scope(source,"\n".join(validate_exact_story_schema(story,mode="final")[0]))
    apply_scoped_patch(source,target,{"base_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
        "changes":[{"path":"/角色集/0/详情/见证人","value":["角色:甲"]}]},scope)
    assert validate_exact_story_schema(json.loads(target.read_text(encoding="utf-8")),mode="final")[0] == []
    assert preservation_errors(source,target) == []


def test_finalize_valid_standalone_draft_does_not_fail_after_publication(tmp_path):
    """既有CLI允许独立合法草稿，不能交付后再因过程库不存在抛异常。"""
    import run_pipeline as pipeline
    draft=tmp_path/"故事结构_草稿.json"
    draft.write_text(json.dumps(complete_story(),ensure_ascii=False),encoding="utf-8")
    assert pipeline.cmd_finalize(tmp_path) == 0
    from story_source import select_analysis_story_source
    path, raw, reason=select_analysis_story_source(tmp_path)
    assert path == tmp_path/"故事结构.json"
    assert raw == path.read_bytes()


@pytest.mark.parametrize("changed", ["故事结构_草稿.json", "故事结构_增量.json"])
def test_finalize_rejects_input_changed_during_validation(tmp_path, monkeypatch, changed):
    """交付回执绑定实际受检字节，验证期间变化时保留上一终稿。"""
    import run_pipeline as pipeline
    for filename in ("故事结构_草稿.json", "故事结构_增量.json"):
        (tmp_path/filename).write_text(json.dumps(complete_story(),ensure_ascii=False),encoding="utf-8")
    final=tmp_path/"故事结构.json"; final.write_text('{"old":true}',encoding="utf-8")
    def validate_then_change(*args):
        (tmp_path/changed).write_text('{"external":"change"}',encoding="utf-8")
        return 0
    monkeypatch.setattr(pipeline,"run_cmd",validate_then_change)
    assert pipeline.cmd_finalize(tmp_path) == 65
    assert final.read_text(encoding="utf-8") == '{"old":true}'
    assert not (tmp_path/"质量治理/最终审计/finalize_log.json").exists()


def test_collection_name_reference_prefix_is_canonicalized_without_guessing_identity():
    """真实151章的“其他事项集:名称”属于确定的类型拼写，不需模型重写。"""
    from normalize_story_schema import canonicalize_detail_ref_array
    assert canonicalize_detail_ref_array(["其他事项集:三行合一战技"]) == ["其他事项:三行合一战技"]
    assert canonicalize_detail_ref_array('["角色集:甲"]') == ["角色:甲"]
    assert canonicalize_detail_ref_array(["阵营集:甲"]) == ["阵营:甲"]
    assert canonicalize_detail_ref_array(["未知集:甲"]) == ["未知集:甲"]
