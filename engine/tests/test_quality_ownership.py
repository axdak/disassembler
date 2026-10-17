"""责任阶段使用相同字段规则；收尾不能隐式扩大为模型补写。"""
import copy
import json

import pytest

from generation_contract import chapter_quality_errors
from merge_delta import save_json
from test_final_field_repair import complete_story


def modification(story):
    return {"新增元素": {}, "修改元素": {"事件集": [{
        "名称": story["事件集"][0]["名称"],
        "详情": {"提取理由": "核对本周期救援原文。", "动作链条": "甲离开现场。"}}]}}


def test_cycle_checks_touched_legacy_fields_but_chapter_keeps_sparse_updates():
    before = complete_story()
    before["事件集"][0].update(介绍="", 分组="", 标签集=[])
    after = copy.deepcopy(before)
    patch = modification(before)
    assert chapter_quality_errors(before, after, patch) == []
    errors = chapter_quality_errors(before, after, patch, governance=True)
    assert all(any(f"/修改元素/事件集/0/{field}" in error for error in errors)
               for field in ("介绍", "分组", "标签集"))
    assert chapter_quality_errors(before, after, {}, governance=True) == []


def test_sourced_setter_cannot_remove_last_compound_tag_and_is_locally_repairable(tmp_path):
    from local_json_patch import delta_repair_scope, apply_scoped_patch
    from fault_recovery import digest
    before = complete_story()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "甲救人", "字段": "标签集",
        "原值": before["事件集"][0]["标签集"], "值": ["救援"], "理由": "原文纠错",
        "证据": [{"章节": "0001", "原文": "甲救下受困的人。"}]}]}}
    after = copy.deepcopy(before)
    after["事件集"][0]["标签集"] = ["救援"]
    errors = chapter_quality_errors(before, after, patch, governance=True)
    pointer = "/治理操作/设置字段/0/值"
    assert any(pointer in error for error in errors)
    reference, candidate = tmp_path / "correction.json", tmp_path / "candidate.json"
    save_json(reference, patch)
    scope = delta_repair_scope(reference, "\n".join(errors), before)
    assert set(scope) == {pointer}
    apply_scoped_patch(reference, candidate, {"base_sha256": digest(reference), "changes": [
        {"path": pointer, "value": ["救援+转折+紧张+生存冲突"]}]}, scope)
    fixed = json.loads(candidate.read_bytes())
    assert fixed["治理操作"]["设置字段"][0]["证据"] == patch["治理操作"]["设置字段"][0]["证据"]
    after["事件集"][0]["标签集"] = fixed["治理操作"]["设置字段"][0]["值"]
    assert chapter_quality_errors(before, after, fixed, governance=True) == []


def test_non_event_empty_group_and_tags_remain_valid():
    before = complete_story()
    patch = {"修改元素": {"角色集": [{"名称": "甲", "详情": {"提取理由": "核对救援。"}}]}}
    assert chapter_quality_errors(before, before, patch, governance=True) == []


def test_default_delivery_reports_source_fields_without_model(tmp_path):
    from story_delivery import commit_delivery
    from delivery_gate import QUALITY_PENDING, delivery_report_path
    value = complete_story()
    value["事件集"][0]["介绍"] = ""
    save_json(tmp_path / "故事结构_增量.json", value)
    save_json(tmp_path / "故事结构_草稿.json", value)
    calls = []
    assert commit_delivery(tmp_path, "offline", repair_worker=lambda *a, **k: calls.append(a)) == QUALITY_PENDING
    assert calls == []
    assert not (tmp_path / "故事结构.json").exists()
    report = json.loads(delivery_report_path(tmp_path).read_bytes())
    issue = next(i for i in report["issues"] if i["path"] == "/事件集/0/介绍")
    assert issue["owner"] == "historical_maintenance"
    assert issue["name"] == "甲救人"
    assert issue["declared_chapters"] == [1]


def test_pending_cycles_route_to_content_without_running_workers(tmp_path, monkeypatch):
    import run_pipeline
    from delivery_gate import check_delivery_prerequisites, CONTENT_PENDING, delivery_report_path
    chapter = tmp_path / "chapter.md"
    monkeypatch.setattr(run_pipeline, "chapter_files", lambda p: [chapter])
    monkeypatch.setattr(run_pipeline, "seq_from_file", lambda p: 1)
    monkeypatch.setattr(run_pipeline, "is_chapter_complete", lambda *a: True)
    monkeypatch.setattr(run_pipeline, "audit_range_is_resolved", lambda *a: False)
    monkeypatch.setattr(run_pipeline, "run_periodic_governance", lambda *a, **k: pytest.fail("收尾不能跑模型"))
    assert check_delivery_prerequisites(tmp_path) == CONTENT_PENDING
    report = json.loads(delivery_report_path(tmp_path).read_bytes())
    assert report["resume_stage"] == "chapter_delta"
    assert report["issues"][0]["range"] == [1, 1]


def test_auto_final_pack_never_generates_introduction(tmp_path, monkeypatch):
    import story_delivery
    import delivery_gate
    from orchestrator import Orchestrator
    calls = []
    monkeypatch.setattr(delivery_gate, "check_delivery_prerequisites", lambda *a, **k: 0)
    monkeypatch.setattr(story_delivery, "prepare_delivery", lambda *a: calls.append("prepare") or 0)
    monkeypatch.setattr(story_delivery, "finish_introduction", lambda *a, **k: pytest.fail("默认收尾禁止模型简介"))
    monkeypatch.setattr(story_delivery, "ensure_delivery_governance", lambda *a, **k: pytest.fail("周期属于内容阶段"))
    assert Orchestrator(tmp_path)._stage_final_pack() == 0
    assert calls == ["prepare"]


def test_tail_governance_is_owned_by_chapter_delta(tmp_path, monkeypatch):
    import orchestrator
    import story_delivery
    calls = []
    monkeypatch.setattr(orchestrator, "cmd_run", lambda *a, **kw: calls.append("delta") or 0)
    monkeypatch.setattr(story_delivery, "ensure_delivery_governance", lambda *a, **kw: calls.append("cycles") or 0)
    assert orchestrator.Orchestrator(tmp_path)._stage_chapter_delta() == 0
    assert calls == ["delta", "cycles"]


def test_final_quality_failure_is_not_retried_as_finalization(tmp_path, monkeypatch):
    import orchestrator
    from delivery_gate import QUALITY_PENDING
    calls = []
    orch = orchestrator.Orchestrator(tmp_path)
    monkeypatch.setattr(orch, "_dispatch", lambda stage: calls.append(stage) or QUALITY_PENDING)
    assert orch._run_stage_with_retries("commit_final_draft") == QUALITY_PENDING
    assert calls == ["commit_final_draft"]


def test_pending_cycle_handoff_is_bounded_and_persisted(tmp_path, monkeypatch):
    import orchestrator
    from delivery_gate import CONTENT_PENDING, delivery_report_path
    orch = orchestrator.Orchestrator(tmp_path)
    orch.state.current_stage = "final_pack"
    monkeypatch.setattr(orch, "_reconcile_chapter_scope", lambda: None)
    monkeypatch.setattr(orch, "_reconcile_recovery_state", lambda: None)
    save_json(delivery_report_path(tmp_path), {"resume_stage": "chapter_delta", "issues": ["cycle pending"]})
    calls = []
    def dispatch(stage):
        calls.append(stage)
        if stage == "chapter_delta":
            orch.state.current_stage = "final_pack"
            return 0
        return CONTENT_PENDING
    monkeypatch.setattr(orch, "_run_stage_with_retries", dispatch)
    assert orch._run() == 1
    assert calls == ["final_pack", "chapter_delta", "final_pack"]
    calls.clear()
    assert orch._run() == 1
    assert calls == ["final_pack"]


@pytest.mark.parametrize("action", ["rename", "merge"])
def test_cycle_identity_operations_also_check_retained_record(action):
    from apply_governance_ops import apply_ops
    before = complete_story()
    before["角色集"][0]["介绍"] = ""
    if action == "rename":
        patch = {"治理操作": {"重命名元素": [{"类型": "角色", "原名称": "甲", "新名称": "乙", "理由": "核对身份。"}]}}
    else:
        other = copy.deepcopy(before["角色集"][0])
        other["名称"] = "乙"
        before["角色集"].append(other)
        patch = {"治理操作": {"合并元素": [{"类型": "角色", "主": "甲", "并入": ["乙"], "理由": "同一身份。"}]}}
    after, _, warnings = apply_ops(before, patch)
    assert not warnings
    errors = chapter_quality_errors(before, after, patch, governance=True)
    assert any("/治理操作/" in error and "/介绍" in error for error in errors)


def test_cycle_resolves_alias_updates_and_same_patch_rename():
    before = complete_story()
    before["事件集"][0].update(别名=["救援"], 介绍="")
    patch = {"修改元素": {"事件集": [{"名称": "救援", "详情": {"提取理由": "核实救援。"}}]}}
    assert any("/修改元素/事件集/0/介绍" in e for e in chapter_quality_errors(before, before, patch, governance=True))


@pytest.mark.parametrize("field", ["描述", "标题"])
def test_normalization_of_wrong_intro_type_is_not_content_evidence(tmp_path, field):
    from story_delivery import prepare_delivery, commit_delivery
    from delivery_gate import QUALITY_PENDING
    value = complete_story()
    value["介绍"][field] = 42
    save_json(tmp_path / "故事结构_增量.json", value)
    assert prepare_delivery(tmp_path) == 0
    assert commit_delivery(tmp_path, "") == QUALITY_PENDING
    # 续跑不能因过程库已被规范化而遗忘第一次捕获的字段类型。
    assert prepare_delivery(tmp_path) == 0
    assert commit_delivery(tmp_path, "") == QUALITY_PENDING


@pytest.mark.parametrize("stage", ["_stage_commit_final_draft", "_stage_finalize"])
def test_resumed_delivery_stages_cannot_bypass_content_gate(tmp_path, monkeypatch, stage):
    import delivery_gate
    import orchestrator
    monkeypatch.setattr(delivery_gate, "check_delivery_prerequisites", lambda *a, **kw: delivery_gate.CONTENT_PENDING)
    monkeypatch.setattr(orchestrator, "cmd_finalize", lambda *a: pytest.fail("前置未完成不能发布"))
    monkeypatch.setattr(orchestrator, "cmd_commit_final_draft", lambda *a: pytest.fail("前置未完成不能提交草稿"))
    assert getattr(orchestrator.Orchestrator(tmp_path), stage)() == delivery_gate.CONTENT_PENDING


def test_explicit_generated_intro_keeps_evidence_after_local_layout(tmp_path):
    from story_delivery import prepare_delivery, finish_introduction, commit_delivery
    from synopsis_formatting import format_synopsis_text
    value = complete_story()
    value["介绍"]["描述"] = ""
    save_json(tmp_path / "故事结构_增量.json", value)
    assert prepare_delivery(tmp_path) == 0
    description = "【主线】甲救下受困的人。【当前进展】甲离开现场。"
    def worker(command, project, kind, pack, output, **options):
        indexing = "content 必须为 null" in pack.read_text(encoding="utf-8")
        save_json(output, {"overview": "完整原文证据位置", "content": None if indexing else {
            "标题": "救援故事", "描述": description}})
        return 0
    assert finish_introduction(tmp_path, "offline", worker) == 0
    assert commit_delivery(tmp_path, "offline", repair_history=True) == 0
    draft = json.loads((tmp_path / "故事结构_草稿.json").read_bytes())
    assert draft["介绍"]["描述"] == format_synopsis_text(description)
    assert draft["介绍"]["描述"] != description
    assert commit_delivery(tmp_path, "") == 0
    before = (tmp_path / "故事结构_草稿.json").read_bytes()
    assert finish_introduction(tmp_path, "", lambda *a, **kw: pytest.fail("排版后的有效简介应复用")) == 0
    assert (tmp_path / "故事结构_草稿.json").read_bytes() == before


def test_local_validator_failure_does_not_claim_ready_or_offer_model_repair(tmp_path):
    from story_delivery import commit_delivery
    from delivery_gate import QUALITY_PENDING, delivery_report_path, DeliveryBlocked
    save_json(tmp_path / "故事结构_草稿.json", complete_story())
    assert commit_delivery(tmp_path, "offline", commit=lambda p: 1) == QUALITY_PENDING
    report = json.loads(delivery_report_path(tmp_path).read_bytes())
    assert report["kind"] == "local_failure"
    assert report["issues"][0]["owner"] == "final_delivery"
    assert DeliveryBlocked(tmp_path, QUALITY_PENDING).result["repair_history_available"] is False


def test_local_final_commit_does_not_need_worker_configuration(tmp_path, monkeypatch):
    import delivery_gate
    from orchestrator import Orchestrator
    save_json(tmp_path / "故事结构_草稿.json", complete_story())
    monkeypatch.setattr(delivery_gate, "check_delivery_prerequisites", lambda *a, **kw: 0)
    orch = Orchestrator(tmp_path)
    monkeypatch.setattr(orch, "_worker_command", lambda: pytest.fail("本地收尾不应解析模型命令"))
    assert orch._stage_commit_final_draft() == 0
