"""Offline recovery acceptance: real disk checkpoints and deterministic fake models."""
import json
import shutil
import threading
from pathlib import Path

import pytest

from fault_recovery import REVIEW_CHECKS, digest, preservation_errors, preserve_quality_notes, recover_artifact
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED, begin_retry_session


def scenario(tmp_path):
    """Keep the authoritative artifact/evidence observable throughout repairs."""
    output = tmp_path / "result.json"
    pack = tmp_path / "repair.md"
    source = tmp_path / "原文拆解/第001章_测试.md"
    source.parent.mkdir()
    source.write_text("原文事实：主角收到来信后前往约定地点。", encoding="utf-8")
    save_json(output, {"名称": "原名称", "详情": "这是必须完整保留的长篇分析内容，不可以通过删去原有叙述来规避校验。"})
    pack.write_text("保留原格式、全部分析与真实证据。", encoding="utf-8")
    initial = output.read_bytes()
    calls, publications = [], []

    def validate(root):
        value = json.loads((root / output.name).read_text(encoding="utf-8"))
        # A worker-written success receipt must never bypass this check.
        assert not (root / "forged_success.json").exists()
        return (0, "") if value.get("提取理由") else (1, "缺少提取理由")

    def worker(command, root, kind, task, expected, *args, **kwargs):
        assert output.read_bytes() == initial
        assert kwargs["retries"] == 0
        calls.append(kind)
        if kind == "recovery_diagnosis":
            save_json(expected, {"diagnosis": "未提供原文依据", "steps": ["补充来信证据"]})
        elif kind == "recovery_review":
            save_json(expected, {"passed": True, "candidate_sha256": digest(root / output.name),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [],
                                 "reason": "原文出现来信和约定地点，原有分析全部保留。"})
        else:
            value = json.loads(expected.read_text(encoding="utf-8"))
            value["提取理由"] = "原文中收到来信并前往约定地点。"
            save_json(expected, value)
        return 0

    def publish(candidate):
        publications.append(candidate)
        shutil.copy2(candidate, output)
        return validate(tmp_path)[0]

    options = dict(command="local-fake", worker=worker, validate=validate, publish=publish,
                   retries=0, chapter_seq=1)
    return output, pack, source, calls, publications, options


def invoke(tmp_path, output, pack, options):
    return recover_artifact(tmp_path, "repair_chapter", pack, output, **options)


def state(tmp_path):
    return json.loads(next((tmp_path / "质量治理/自动恢复").glob("*/state.json")).read_text(encoding="utf-8"))


def test_switch_strategy_validate_then_publish_once(tmp_path):
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options["worker"]

    def worker(*args, **kwargs):
        if not calls:
            calls.append(args[2])
            return 1
        return original(*args, **kwargs)

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ["auto_repair_chapter", "auto_repair_chapter", "recovery_review"]
    assert len(published) == 1
    assert state(tmp_path)["attempts"][-1]["strategy"] == "evidence"
    assert invoke(tmp_path, output, pack, options) == 0
    assert len(published) == 1  # Completed recovery is idempotent.


def test_exhaustion_survives_restart_and_legacy_retry_reset(tmp_path):
    output, pack, source, calls, published, options = scenario(tmp_path)
    options["worker"] = lambda *args, **kw: calls.append(args[2]) or 1
    for _ in range(2):
        assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
        begin_retry_session(tmp_path)
    assert calls == ["auto_repair_chapter", "auto_repair_chapter", "recovery_diagnosis"]
    assert not published
    source.write_text("新增了真实原文证据", encoding="utf-8")
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert len(calls) == 6
    assert state(tmp_path)["generation"] == 2


@pytest.mark.parametrize("mode", ["delete_content", "edit_evidence", "edit_reference", "reject_review", "bad_review", "edit_during_review"])
def test_bad_candidates_never_publish(tmp_path, mode):
    output, pack, source, calls, published, options = scenario(tmp_path)
    original = options["worker"]
    options["strategies"] = 1

    def worker(*args, **kwargs):
        rc = original(*args, **kwargs)
        _, root, kind, _, expected, *_ = args
        if kind == "auto_repair_chapter":
            if mode == "delete_content":
                save_json(expected, {"提取理由": "假装修好了"})
            if mode == "edit_evidence":
                (root / source.relative_to(tmp_path)).write_text("篡改原文", encoding="utf-8")
            if mode == "edit_reference":
                save_json(root / "reference.json", {})
            (root / "forged_success.json").write_text("true", encoding="utf-8")
        elif kind == "recovery_review":
            if mode == "reject_review":
                save_json(expected, {"passed": False, "issues": ["缺少证据"]})
            if mode == "bad_review":
                expected.write_text("bad json", encoding="utf-8")
            if mode == "edit_during_review":
                save_json(root / output.name, {})
        return rc

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published
    assert "提取理由" not in json.loads(output.read_text(encoding="utf-8"))


def test_diagnosis_strategy_and_configured_model_fallback(tmp_path, monkeypatch):
    output, pack, _, calls, published, options = scenario(tmp_path)
    monkeypatch.setenv("ND_RECOVERY_COMMAND", "stronger-local-fake")
    original = options["worker"]

    def worker(*args, **kwargs):
        if len(calls) < 2:
            calls.append(args[2])
            return 1
        assert args[0] == "stronger-local-fake"
        return original(*args, **kwargs)

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls[-3:] == ["recovery_diagnosis", "auto_repair_chapter", "recovery_review"]
    assert len(published) == 1


def test_publication_crash_resumes_without_new_model_calls(tmp_path):
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options["publish"]

    def interrupted(candidate):
        original(candidate)
        raise RuntimeError("模拟写入后进程退出")

    options["publish"] = interrupted
    with pytest.raises(RuntimeError):
        invoke(tmp_path, output, pack, options)
    assert state(tmp_path)["status"] == "publishing"
    options["publish"] = original
    count = len(calls)
    assert invoke(tmp_path, output, pack, options) == 0
    assert len(calls) == count


def test_cancel_retains_strategy_but_does_not_refund_call_budget(tmp_path):
    output, pack, _, calls, _, options = scenario(tmp_path)
    options["worker"] = lambda *a, **kw: calls.append(a[2]) or 130
    assert invoke(tmp_path, output, pack, options) == 130
    assert state(tmp_path)["next_strategy"] == 0
    assert len(state(tmp_path)["calls"]) == 1
    options["cancel"] = threading.Event()
    options["cancel"].set()
    assert invoke(tmp_path, output, pack, options) == 130
    assert len(calls) == 1


def test_transport_retry_is_bounded_and_cancelable(tmp_path, monkeypatch):
    output, pack, _, calls, _, options = scenario(tmp_path)
    monkeypatch.setattr("fault_recovery._sleep", lambda seconds: False)
    options.update(retries=5, strategies=1,
                   worker=lambda *a, **kw: calls.append(a[2]) or 429)
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert len(calls) == 3  # Per-operation cap also applies to configured retries.


def test_preservation_allows_identifier_fix_but_retains_long_analysis(tmp_path):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    narrative = "原章节分析中的完整细节必须保留，名称纠错不能删除已有分析内容。"
    save_json(old, {"名称": "双重地下城与最终BOSS", "内容": narrative})
    save_json(new, {"名称": "双层地下城与最终BOSS", "内容": narrative, "提取理由": "补充证据"})
    assert preservation_errors(old, new) == []
    save_json(new, {"名称": "双层地下城与最终BOSS", "内容": "简化了"})
    assert preservation_errors(old, new)


def test_malformed_generation_enters_repair_without_holding_worker_slot(tmp_path, monkeypatch):
    """A provider's format failure must not escape the automatic recovery path."""
    import run_pipeline as pipeline
    from test_audit_pack import write_chapter_source
    chapter = write_chapter_source(tmp_path, 1)
    pipeline.init_dirs(tmp_path)
    paths = pipeline.artifact_paths(tmp_path, chapter)
    paths["delta"].write_text('{"章节":', encoding="utf-8")
    calls = []
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: 1)
    monkeypatch.setattr(pipeline, "run_repair_worker", lambda *a, **kw: calls.append(a) or 0)
    assert pipeline.run_generation_worker("local", tmp_path, "delta", paths["delta_task"], paths["delta"], 1) == 0
    assert calls[0][2:5] == ("repair_chapter", paths["repair"], paths["delta"])
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: 130)
    assert pipeline.run_generation_worker("local", tmp_path, "delta", paths["delta_task"], paths["delta"], 1) == 130
    assert len(calls) == 1


def test_repaired_chapter_still_triggers_periodic_audit(tmp_path, monkeypatch):
    """Publishing inside recovery must not skip the chapter's normal audit barrier."""
    import run_pipeline as pipeline
    from test_audit_pack import write_chapter_source, valid_analysis, empty_delta, empty_story
    chapter = write_chapter_source(tmp_path, 1)
    pipeline.init_dirs(tmp_path)
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    paths = pipeline.artifact_paths(tmp_path, chapter)
    paths["analysis"].write_text(valid_analysis(1), encoding="utf-8")
    paths["repair"].write_text("修复任务", encoding="utf-8")
    save_json(paths["delta"], empty_delta(1))
    done, audits = [], []
    monkeypatch.setattr(pipeline, "is_chapter_complete", lambda *a: bool(done))
    monkeypatch.setattr(pipeline, "commit_chapter", lambda *a: 1)
    monkeypatch.setattr(pipeline, "run_repair_worker", lambda *a, **kw: done.append(True) or 0)
    monkeypatch.setattr(pipeline, "run_periodic_governance", lambda *a, **kw: audits.append(a[1:3]) or 0)
    assert pipeline.cmd_run(tmp_path, chapter_command="local", run_mode="worker", audit_interval=1,
                            worker_loop="continuous", max_chapters=1) == 0
    assert audits == [(1, 1)]


def test_changed_authoritative_input_rejects_stale_candidate(tmp_path):
    output, pack, source, calls, published, options = scenario(tmp_path)
    original = options["worker"]

    def worker(*a, **kw):
        rc = original(*a, **kw)
        if a[2] == "recovery_review":
            source.write_text("模拟其他任务修改正式输入", encoding="utf-8")
        return rc

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published
    assert state(tmp_path)["attempts"][-1]["status"] == "inputs_changed"


def test_pending_publication_rejects_unrelated_input_change(tmp_path):
    output, pack, source, calls, published, options = scenario(tmp_path)

    def crashed(candidate):
        raise RuntimeError("模拟发布前中断")

    options["publish"] = crashed
    with pytest.raises(RuntimeError):
        invoke(tmp_path, output, pack, options)
    source.write_text("外部编辑了原文，旧复核已失效", encoding="utf-8")
    options["publish"] = lambda candidate: published.append(candidate) or 0
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published


def test_pending_publication_recognizes_validated_story_change(tmp_path):
    output, pack, _, calls, published, options = scenario(tmp_path)
    story = tmp_path / "故事结构_增量.json"
    save_json(story, {"version": 1})
    validate = options["validate"]

    def validate_and_merge(root):
        rc, feedback = validate(root)
        if rc == 0:
            save_json(root / story.name, {"version": 2})
        return rc, feedback

    def publish(candidate):
        shutil.copy2(candidate, output)
        validate_and_merge(tmp_path)
        raise RuntimeError("合并成功后记账前中断")

    options.update(validate=validate_and_merge, publish=publish)
    with pytest.raises(RuntimeError):
        invoke(tmp_path, output, pack, options)
    count = len(calls)
    options["publish"] = lambda candidate: validate_and_merge(tmp_path)[0]
    assert invoke(tmp_path, output, pack, options) == 0
    assert len(calls) == count


def test_analysis_recovery_receives_complete_fresh_contract(tmp_path, monkeypatch):
    """Regeneration gets the newly generated chapter task, including full source."""
    import run_pipeline as pipeline
    from test_audit_pack import write_chapter_source, valid_analysis
    chapter = write_chapter_source(tmp_path, 1)
    pipeline.init_dirs(tmp_path)
    paths = pipeline.artifact_paths(tmp_path, chapter)
    retained = "短分析内容必须保留。"
    paths["analysis"].write_text("# 旧分析\n" + retained, encoding="utf-8")
    observed = []

    def worker(command, root, kind, pack, expected, *a, **kw):
        if kind == "auto_analysis_regenerate":
            contract = (root / "contract.md").read_text(encoding="utf-8")
            assert "以下10个章节" in contract
            assert "测试原文。" in contract
            expected.write_text(valid_analysis(1) + "\n" + retained, encoding="utf-8")
        elif kind == "recovery_review":
            candidate = root / paths["analysis"].relative_to(tmp_path)
            save_json(expected, {"passed": True, "candidate_sha256": digest(candidate),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "离线复核协议模拟"})
        else:
            pytest.fail(kind)
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_repair_worker("local", tmp_path, "analysis_regenerate", paths["analysis_regenerate_task"],
                                      paths["analysis"], 1, observe=lambda event, fields: observed.append(event)) == 0
    assert retained in paths["analysis"].read_text(encoding="utf-8")
    assert observed[-1] == "修复验收通过，继续流水线"
    assert not (tmp_path / "故事结构_增量.json").exists()


@pytest.mark.parametrize("command,strategies", [("local-fake", 3), ("", 0)])
def test_program_repair_succeeds_without_model_or_model_budget(tmp_path, command, strategies):
    """A deterministic type correction needs no model repair or semantic review."""
    output, pack, _, calls, published, options = scenario(tmp_path)
    before = json.loads(output.read_text(encoding="utf-8"))
    before["数量"] = "2"
    save_json(output, before)

    def normalize_and_validate(root):
        target = root / output.name
        value = json.loads(target.read_text(encoding="utf-8"))
        value["数量"] = int(value["数量"])
        save_json(target, value)
        return 0, "原规则校验通过"

    def publish(candidate):
        published.append(candidate)
        shutil.copy2(candidate, output)
        return 0

    options.update(command=command, strategies=strategies, validate=normalize_and_validate, publish=publish)
    assert invoke(tmp_path, output, pack, options) == 0
    assert not calls
    after = json.loads(output.read_text(encoding="utf-8"))
    assert after == {**before, "数量": 2}
    assert state(tmp_path)["calls"] == []
    assert state(tmp_path)["attempts"] == []
    assert state(tmp_path)["mechanical"]["status"] == "accepted"
    assert invoke(tmp_path, output, pack, options) == 0
    assert len(published) == 1


@pytest.mark.parametrize("provider_rc", [0, 1])
def test_malformed_governance_uses_real_program_repair_before_regeneration(tmp_path, monkeypatch, provider_rc):
    """Exercise the actual JSON repair, normalization, validation and commit path."""
    import run_pipeline as pipeline
    from test_audit_pack import empty_story
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    patch = tmp_path / "质量治理/周期审计/correction_001-001.json"
    patch.parent.mkdir(parents=True)
    pack = patch.with_suffix(".md")
    pack.write_text("按原治理契约生成补丁", encoding="utf-8")
    calls = []

    def worker(command, directory, env, timeout):
        calls.append(env["ND_TASK_TYPE"])
        assert env["ND_TASK_TYPE"] == "audit_correction"
        value = {"章节范围": "第001章-第001章", "新增元素": {}, "修改元素": {},
                 "治理类型": "no_change", "质量说明": "本周期已完整检查，无需变更", "证据范围": ["0001"]}
        text = json.dumps(value, ensure_ascii=False)
        Path(env["ND_EXPECTED_OUTPUT"]).write_text(text[:-1] + ",}", encoding="utf-8")
        return provider_rc, "模拟模型产出的多余尾逗号"

    monkeypatch.setattr(pipeline, "stream_shell_command", worker)
    assert pipeline.run_generation_worker("local-fake", tmp_path, "audit_correction", pack, patch, retries=2) == 0
    assert calls == ["audit_correction"]
    assert state(tmp_path)["calls"] == []
    assert pipeline.audit_status(tmp_path, 1, 1)["status"] == "committed"


def test_failed_generation_cannot_reuse_unchanged_valid_draft(tmp_path, monkeypatch):
    """Passing validation on yesterday's draft cannot hide today's worker failure."""
    import run_pipeline as pipeline
    draft = tmp_path / "故事结构_草稿.json"
    save_json(draft, {"existing": "valid JSON from an earlier task"})
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: 1)
    monkeypatch.setattr(pipeline, "run_repair_worker", lambda *a, **kw: pytest.fail("不应验收未更新草稿"))
    assert pipeline.run_generation_worker("local-fake", tmp_path, "final_draft", tmp_path / "task.md", draft) == 1


def test_schema_relocation_keeps_content_and_rejects_hidden_deletion(tmp_path):
    """Regression for the saved chapter-8 candidates: same values, new location."""
    before, after = tmp_path / "old.json", tmp_path / "new.json"
    narrative = "完整分析必须留在所属元素中，不能借迁移字段的位置把分析内容删掉。"
    save_json(before, {"修改元素": {"角色集": [{"名称": "主角", "人物轨迹": narrative}]}})
    save_json(after, {"修改元素": {"角色集": [{"名称": "主角", "详情": {"人物轨迹": narrative}}]}})
    assert preservation_errors(before, after) == []
    save_json(after, {"修改元素": {"角色集": [{"名称": "主角", "详情": {"人物轨迹": "缩写"}}]}, "质量说明": narrative})
    assert preservation_errors(before, after)  # Same text in metadata cannot replace analysis.


def test_preservation_reserves_exact_names_before_matching_renamed_records(tmp_path):
    before, after = tmp_path / "old.json", tmp_path / "new.json"
    first = "第一个元素的完整独立分析必须全部保留，不能由另外一个元素替代。"
    second = "第二个元素的完整独立分析必须全部保留，不能由另外一个元素替代。"
    save_json(before, [{"名称": "错别字", "介绍": first}, {"名称": "乙", "介绍": second}])
    save_json(after, [{"名称": "乙", "介绍": second}, {"名称": "正式名称", "介绍": first}])
    assert preservation_errors(before, after) == []
    save_json(before, [{"名称": "重复名称", "介绍": first}, {"名称": "重复名称", "介绍": first}])
    save_json(after, [{"名称": "替代元素", "介绍": "删去分析"}, {"名称": "重复名称", "介绍": first}])
    assert preservation_errors(before, after)


@pytest.mark.parametrize("valid", [True, False])
def test_typed_relation_requires_same_element_complete_detail(tmp_path, valid):
    before, after = tmp_path / "old.json", tmp_path / "new.json"
    detail = {"寄居物品": "古戒（随身携带）", "关联物品": ["物品:古戒"]}
    save_json(before, {"新增元素": {"角色集": [{"名称": "老者", "关系": ["寄居:物品:古戒"], "详情": detail}]}})
    candidate_detail = detail if valid else {**detail, "寄居物品": "古戒二号"}
    save_json(after, {"新增元素": {"角色集": [{"名称": "老者", "关系": [], "详情": candidate_detail}]}})
    assert bool(preservation_errors(before, after)) is not valid


def test_quality_note_is_retained_without_exempting_narrative(tmp_path):
    before, after = tmp_path / "old.json", tmp_path / "new.json"
    old_note = "旧说明曾描述修复提取理由，后续改名不能导致这一份历史说明消失。"
    narrative = "分析正文详细记载了主角第一次收到来信并决定前往约定地点的过程。"
    save_json(before, {"质量说明": old_note, "介绍": narrative})
    save_json(after, {"质量说明": "已按正式索引更正名称。", "介绍": narrative})
    preserve_quality_notes(before, after)
    assert preservation_errors(before, after) == []
    first = after.read_bytes()
    preserve_quality_notes(before, after)
    assert after.read_bytes() == first
    value = json.loads(first)
    assert old_note in value["质量说明"]
    value["介绍"] = "删除细节"
    save_json(after, value)
    preserve_quality_notes(before, after)
    assert preservation_errors(before, after)


@pytest.mark.parametrize("bad_plan", [{"difficulty": "easy"}, {"diagnosis": "根因", "steps": [""]},
                                      {"diagnosis": "根因", "steps": [None]}, {"diagnosis": "根因", "steps": [{}]}])
def test_diagnosis_protocol_retries_once_then_continues(tmp_path, bad_plan):
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options["worker"]

    def worker(*args, **kwargs):
        if len(calls) < 2:
            calls.append(args[2])
            return 1
        if calls.count("recovery_diagnosis") == 0:
            calls.append(args[2])
            save_json(args[4], bad_plan)
            return 0
        return original(*args, **kwargs)

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls.count("recovery_diagnosis") == 2
    assert len(published) == 1
    assert state(tmp_path)["calls"][2]["returncode"] == 66


@pytest.mark.parametrize("native_rc", [0, 66])
def test_invalid_diagnosis_exhausts_bounded_budget_without_publication(tmp_path, native_rc):
    output, pack, _, calls, published, options = scenario(tmp_path)

    def worker(*args, **kwargs):
        calls.append(args[2])
        if args[2] == "recovery_diagnosis":
            save_json(args[4], {"difficulty": "easy"})
            return native_rc
        return 1

    options["worker"] = worker
    for _ in range(2):
        assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert calls == ["auto_repair_chapter", "auto_repair_chapter", "recovery_diagnosis", "recovery_diagnosis"]
    assert not published
