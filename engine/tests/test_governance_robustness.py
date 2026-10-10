"""Regression tests for real failure patterns, without invoking a model."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from apply_governance_ops import apply_ops
from element_identity import build_lookup_index, find_index
from governance_contract import normalize_governance_patch
from merge_delta import merge_delta
from repair_budget import begin_retry_session, reserve_repair, finish_repair, REPAIR_EXHAUSTED
from story_schema_rules import alias_to_name_maps
from validate_delta import validate_elements
import run_pipeline
from orchestrator import Orchestrator


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def merge_fixture():
    """Reproduce reciprocal aliases with the source preceding the target."""
    return {"角色集": [
        {"名称": "萧家族长", "别名": ["萧战", "萧炎之父"], "介绍": "父亲档案", "详情": {
            "首次章节": "0001", "最近章节": "0003", "补充标签": '["父亲"]',
            "人物轨迹": '["首次出场"]', "旧档案": "不可丢失的内容"}},
        {"名称": "萧战", "别名": ["萧家族长", "萧炎之父"], "介绍": "族长档案", "详情": {
            "首次章节": "0002", "最近章节": "0005", "补充标签": '["族长"]',
            "人物轨迹": '["族长登场"]'}},
        {"名称": "萧炎", "关系": ["父亲:萧家族长"], "详情": {"关联角色": ["角色:萧家族长"], "回忆": "萧家族长"}},
    ], "地点集": [{"名称": "萧家族长", "介绍": "跨类型同名档案"}],
        "事件集": [{"名称": "来访", "参与成员": ["萧家族长"], "发生地点": "萧家族长"}]}


def test_canonical_merge_preserves_metadata_narrative_and_reference_types():
    story = merge_fixture()
    before = copy.deepcopy(story)
    patch = {"治理操作": {"合并元素": [{"类型": "角色", "主": "萧战", "并入": ["萧家族长"]}]}}
    result, logs, errors = apply_ops(story, patch)
    assert not errors
    assert story == before  # Caller state remains untouched until publication.
    assert [r["名称"] for r in result["角色集"]] == ["萧战", "萧炎"]
    target, child = result["角色集"]
    assert target["详情"]["首次章节"] == "0001"
    assert target["详情"]["最近章节"] == "0005"
    assert set(json.loads(target["详情"]["补充标签"])) == {"族长", "父亲"}
    assert json.loads(target["详情"]["人物轨迹"]) == ["族长登场", "首次出场"]
    assert target["详情"]["旧档案"] == "不可丢失的内容"
    assert "父亲档案" in target["介绍"] and "族长档案" in target["介绍"]
    assert child["关系"] == ["父亲:萧战"]
    assert child["详情"]["关联角色"] == ["角色:萧战"]
    assert child["详情"]["回忆"] == "萧家族长"
    assert result["事件集"][0]["参与成员"] == ["萧战"]
    assert result["事件集"][0]["发生地点"] == "萧家族长"
    assert result["地点集"] == story["地点集"]


def test_failed_operation_aborts_entire_patch():
    story = merge_fixture()
    patch = {"治理操作": {"合并元素": [
        {"类型": "角色", "主": "萧战", "并入": ["萧家族长"]},
        {"类型": "角色", "主": "萧战", "并入": ["不存在"]},
    ]}}
    result, logs, errors = apply_ops(story, patch)
    assert errors and not logs
    assert result == story


@pytest.mark.parametrize("name", ["萧炎之父", "重复正式名"])
def test_ambiguous_identity_is_not_first_match(name):
    items = merge_fixture()["角色集"] if name == "萧炎之父" else [{"名称": name}, {"名称": name}]
    with pytest.raises(ValueError, match="匹配多个元素"):
        find_index(*build_lookup_index(items), name)


def test_reference_alias_map_cannot_shadow_canonical_names():
    aliases = alias_to_name_maps(merge_fixture())["角色集"]
    assert "萧战" not in aliases and "萧家族长" not in aliases and "萧炎之父" not in aliases


def test_unknown_modification_fails_before_any_story_write(tmp_path):
    story = {"线索集": [{"名称": "双层地下城与最终BOSS", "详情": {"首次章节": "0001", "最近章节": "0006"}}]}
    patch = {"章节": "0007", "新增元素": {}, "修改元素": {"线索集": [
        {"名称": "双重地下城与最终BOSS", "详情": {"提取理由": "本章线索变化"}}]}}
    errors, _ = validate_elements(story, patch, "process")
    assert any("修改目标" in e and "双层地下城与最终BOSS" in e for e in errors)
    source, delta = tmp_path / "story.json", tmp_path / "delta.json"
    write_json(source, story)
    write_json(delta, patch)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="禁止自动转为新增"):
        merge_delta(source, delta)
    assert source.read_bytes() == before


def test_sparse_governance_modification_inherits_metadata_but_requires_evidence():
    story = {"角色集": [{"名称": "甲", "详情": {"首次章节": "0001", "最近章节": "0005"}}]}
    item = {"名称": "甲", "介绍": "完整的新档案", "详情": {"提取理由": "补充已知身份"}}
    patch = {"新增元素": {}, "修改元素": {"角色集": [item]}}
    errors, _ = validate_elements(story, patch, "governance")
    assert errors == []
    item["详情"]["首次章节"] = "错误"
    assert any("首次章节" in e for e in validate_elements(story, patch, "governance")[0])
    item["详情"] = {}
    assert any("提取理由" in e for e in validate_elements(story, patch, "governance")[0])


def test_shape_normalization_is_lossless_and_idempotent():
    modification = {"角色集": [{"名称": "甲", "详情": {"首次章节": "第001章", "简介": '{"这仍然是档案字符串":true}'}}]}
    patch = {"修改元素": json.dumps(modification), "治理操作": '{"删除元素":[]}', "证据范围": '["0001"]'}
    fixed, changes = normalize_governance_patch(patch, "第001章-第005章")
    assert changes and isinstance(patch["修改元素"], str)
    assert fixed["修改元素"]["角色集"][0]["详情"] == {"首次章节": "0001", "简介": '{"这仍然是档案字符串":true}'}
    assert fixed["章节范围"] == "第001章-第005章"
    assert normalize_governance_patch(fixed) == (fixed, [])
    assert "提取理由" not in fixed["修改元素"]["角色集"][0]["详情"]


def test_nested_worker_retries_share_budget_and_explicit_resume_preserves_history(tmp_path, monkeypatch):
    output = tmp_path / "failed.json"
    write_json(output, {"version": 0})
    calls = []

    def worker(command, project, env, timeout):
        calls.append(env["ND_TASK_TYPE"])
        write_json(output, {"version": len(calls)})
        return 0, "fake worker"

    monkeypatch.setattr(run_pipeline, "stream_shell_command", worker)
    begin_retry_session(tmp_path)
    invoke = lambda: run_pipeline.run_worker("fake", tmp_path, "repair_chapter", tmp_path / "task.md", output, retries=1)
    assert invoke() == 0 and invoke() == 0
    assert invoke() == REPAIR_EXHAUSTED
    assert len(calls) == 2
    journal_path = next((tmp_path / "质量治理/重试记录").glob("repair_chapter_*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert json.loads(Path(journal["attempts"][0]["snapshot"]).read_text(encoding="utf-8")) == {"version": 0}
    begin_retry_session(tmp_path)
    assert invoke() == 0 and len(calls) == 3
    assert len(json.loads(journal_path.read_text(encoding="utf-8"))["attempts"]) == 3


def test_repair_cycle_stops_but_transport_failure_can_retry(tmp_path):
    output = tmp_path / "failed.json"
    write_json(output, {"version": 0})
    first = reserve_repair(tmp_path, output, "audit_repair", 4)
    finish_repair(first, 1)
    second = reserve_repair(tmp_path, output, "audit_repair", 4)
    assert second is not None  # Same input after transport failure is retryable.
    finish_repair(second, 0)
    assert reserve_repair(tmp_path, output, "audit_repair", 4) is None


def test_outer_orchestrator_does_not_redispatch_exhausted_repair(tmp_path, monkeypatch):
    calls = []
    orch = Orchestrator(tmp_path)
    monkeypatch.setattr(orch, "_dispatch", lambda stage: calls.append(stage) or REPAIR_EXHAUSTED)
    assert orch._run_stage_with_retries("chapter_delta") == 1
    assert calls == ["chapter_delta"]
    assert "修复" in orch.state.last_error


def test_governance_failure_keeps_story_and_supplies_current_patch_and_report(tmp_path):
    story = {"介绍": {"标题": "测试", "描述": ""}, "角色集": []}
    write_json(tmp_path / "故事结构_增量.json", story)
    # Normalize before capturing the authoritative bytes, as ordinary startup does.
    source = run_pipeline.ensure_story(tmp_path)
    before = source.read_bytes()
    patch = tmp_path / "质量治理/周期审计/correction_001-005.json"
    write_json(patch, {"章节范围": "第001章-第005章", "新增元素": {}, "修改元素": {},
                       "治理操作": {"删除元素": [{"类型": "角色", "名称": "不存在"}]}})
    assert run_pipeline.commit_governance(tmp_path, str(patch)) == 1
    assert source.read_bytes() == before
    repair = patch.with_suffix(".repair.md").read_text(encoding="utf-8")
    assert "找不到删除元素" in repair and "不存在" in repair
    assert not list((tmp_path / "故事结构版本").glob(".governance_candidate*"))


def test_serial_chapter_validation_loop_is_bounded(tmp_path, monkeypatch):
    from test_audit_pack import write_chapter_source, valid_analysis, empty_story

    write_json(tmp_path / "故事结构_增量.json", empty_story())
    chapter = write_chapter_source(tmp_path, 1)
    run_pipeline.init_dirs(tmp_path)
    paths = run_pipeline.artifact_paths(tmp_path, chapter)
    paths["analysis"].write_text(valid_analysis(1), encoding="utf-8")
    paths["repair"].write_text("修复当前失败文件", encoding="utf-8")
    write_json(paths["delta"], {"attempt": 0})
    calls = []

    def worker(command, project, env, timeout):
        calls.append(env["ND_TASK_TYPE"])
        write_json(paths["delta"], {"attempt": len(calls)})
        return 0, "local stub"

    monkeypatch.setattr(run_pipeline, "stream_shell_command", worker)
    monkeypatch.setattr(run_pipeline, "commit_chapter", lambda *args: 1)
    rc = run_pipeline.cmd_run(tmp_path, chapter_command="fake", audit_interval=0,
                              worker_retries=1, worker_loop="continuous", run_mode="worker")
    assert rc == REPAIR_EXHAUSTED
    assert calls == ["repair_chapter", "repair_chapter"]
    assert paths["analysis"].read_text(encoding="utf-8") == valid_analysis(1)


def test_old_cached_delta_contract_is_refreshed_without_rewriting_analysis(tmp_path):
    from test_audit_pack import write_chapter_source, valid_analysis, empty_story

    write_json(tmp_path / "故事结构_增量.json", empty_story())
    chapter = write_chapter_source(tmp_path, 1)
    run_pipeline.init_dirs(tmp_path)
    paths = run_pipeline.artifact_paths(tmp_path, chapter)
    paths["analysis"].write_text(valid_analysis(1), encoding="utf-8")
    paths["delta_task"].write_text("旧冲突提示词", encoding="utf-8")
    write_json(paths["delta"], {"现有详细内容": "保持"})
    run_pipeline.write_repair_pack(tmp_path, chapter, "缺少字段", [])
    repair = paths["repair"].read_text(encoding="utf-8")
    assert "旧冲突提示词" not in repair
    assert "不是创作故事" in repair and "现有详细内容" in repair
    assert paths["analysis"].read_text(encoding="utf-8") == valid_analysis(1)


def test_native_governance_schema_uses_real_contract():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools/agent_worker.py"
    spec = importlib.util.spec_from_file_location("worker_schema_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = json.loads(module.structured_output_schema("audit_repair", [Path("correction.json")]))
    assert schema["properties"]["修改元素"]["type"] == "object"
    assert schema["properties"]["治理操作"]["type"] == "object"
    assert schema["properties"]["证据范围"]["type"] == "array"
    assert schema["anyOf"] == [{"required": ["章节范围"]}, {"required": ["章节"]}]


def test_resume_after_publication_does_not_repeat_governance(tmp_path, monkeypatch):
    from test_audit_pack import empty_story

    write_json(tmp_path / "故事结构_增量.json", empty_story())
    patch = tmp_path / "质量治理/周期审计/correction_001-005.json"
    write_json(patch, {"章节范围": "第001章-第005章", "新增元素": {}, "修改元素": {},
                       "治理类型": "no_change", "质量说明": "本周期已完整检查，无需变更",
                       "证据范围": ["0001", "0002", "0003", "0004", "0005"]})
    mark = run_pipeline.mark_periodic_audit_committed

    def interrupted(*args):
        raise RuntimeError("模拟落盘后中断")

    monkeypatch.setattr(run_pipeline, "mark_periodic_audit_committed", interrupted)
    with pytest.raises(RuntimeError, match="模拟落盘后中断"):
        run_pipeline.commit_governance(tmp_path, str(patch))
    assert patch.with_suffix(".commit.json").is_file()
    monkeypatch.setattr(run_pipeline, "mark_periodic_audit_committed", mark)
    run = run_pipeline.run_cmd
    commands = []

    def collect(args, *rest, **kwargs):
        commands.append(args)
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(run_pipeline, "run_cmd", collect)
    assert run_pipeline.commit_governance(tmp_path, str(patch)) == 0
    assert not any("merge_delta.py" in " ".join(cmd) for cmd in commands)
    status = json.loads((patch.parent / "audit_001-005.status.json").read_text(encoding="utf-8"))
    assert status["status"] == "committed"
