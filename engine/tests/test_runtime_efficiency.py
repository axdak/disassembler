"""Runtime efficiency must preserve complete evidence and authoritative gates."""
import hashlib
import json
from pathlib import Path

import pytest

from test_architecture_recovery import chapter_project
from test_tag_governance import story_with_tags, tag_op, patch


@pytest.mark.parametrize("task", ["delta", "audit_correction"])
@pytest.mark.parametrize("setting", [None, "0", "1"])
def test_generation_defaults_to_baseline_and_runtime_requires_opt_in(tmp_path, monkeypatch, task, setting):
    """Normal generation must not pay for candidate-tool orchestration by default."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack = paths["delta_task"]
    output = paths["delta"] if task == "delta" else pipeline.audit_paths(tmp_path, 1, 1)["correction"]
    if task == "audit_correction":
        pack = pipeline.write_audit_pack(tmp_path, 1, 1, force=True)
    if setting is None:
        monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("ND_CONTROLLED_RUNTIME", setting)
    selected = []

    def worker(command, project, kind, actual_pack, target, *args, **kwargs):
        selected.append(actual_pack)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"fresh": true}', encoding="utf-8")
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
                                         task, pack, output, 1) == 0
    assert (selected[0] != pack) is (setting == "1")
    if setting != "1":
        assert not selected[0].with_suffix(".runtime.json").exists()
        assert output.with_suffix(".generation.json").exists()
        assert runtime.generation_is_current(output, story)


def test_baseline_generation_keeps_pre_dispatch_revision_on_concurrent_story_edit(tmp_path, monkeypatch):
    """Fast delivery still must not authorize a proposal against a newer story."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)

    def worker(*args, **kwargs):
        paths["delta"].write_text('{"fresh": true}', encoding="utf-8")
        story.write_bytes(story.read_bytes() + b"\n")
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
                                         "delta", paths["delta_task"], paths["delta"], 1) == 0
    assert not runtime.generation_is_current(paths["delta"], story)


@pytest.mark.parametrize("task", ["delta", "audit_correction"])
def test_baseline_refreshes_old_pack_and_fences_worker_crash(tmp_path, monkeypatch, task):
    """A stale prompt and a half-delivered first artifact must never be endorsed."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack = paths["delta_task"]
    output = paths["delta"]
    if task == "audit_correction":
        pack = pipeline.write_audit_pack(tmp_path, 1, 1, force=True)
        output = pipeline.audit_paths(tmp_path, 1, 1)["correction"]
    data = json.loads(story.read_text(encoding="utf-8"))
    data["介绍"]["描述"] = "派发前刚更新的故事事实"
    story.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)

    def interrupted(command, root, kind, actual_pack, target, *args, **kwargs):
        assert "派发前刚更新的故事事实" in actual_pack.read_text(encoding="utf-8")
        target.write_text('{"fresh": true}', encoding="utf-8")
        assert not runtime.generation_is_current(target, story)
        raise RuntimeError("host stopped after worker output")

    monkeypatch.setattr(pipeline, "run_worker", interrupted)
    with pytest.raises(RuntimeError, match="host stopped"):
        pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
                                       task, pack, output, 1)
    assert not runtime.generation_is_current(output, story)


@pytest.mark.parametrize("task", ["delta", "audit_correction"])
@pytest.mark.parametrize("resume", [False, True])
def test_baseline_capacity_handoff_finishes_pending_receipt(tmp_path, monkeypatch, task, resume):
    """Successful bounded delivery must remain committable after timeout/resume."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    import bounded_generation
    chapter, paths, story = chapter_project(tmp_path)
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack, output = paths["delta_task"], paths["delta"]
    if task == "audit_correction":
        pack = pipeline.write_audit_pack(tmp_path, 1, 1, force=True)
        output = pipeline.audit_paths(tmp_path, 1, 1)["correction"]
    monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)
    if resume:
        runtime.mark_generation_pending(tmp_path, output, hashlib.sha256(story.read_bytes()).hexdigest())
        if task == "delta":
            monkeypatch.setattr(pipeline, "inspect_pack", lambda _: {"passed": False})
        else:
            original = runtime.prepare_baseline_dispatch

            def prepare(*args):
                result = original(*args)
                result[0].with_suffix(".governance.json").write_text("{}", encoding="utf-8")
                return result

            monkeypatch.setattr(runtime, "prepare_baseline_dispatch", prepare)

    def bounded(*args, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"bounded": true}', encoding="utf-8")
        return 0

    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: pipeline.CONTEXT_EXCEEDED)
    monkeypatch.setattr(bounded_generation, "run_bounded_chapter", bounded)
    monkeypatch.setattr(bounded_generation, "run_bounded_governance", bounded)
    assert pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
                                         task, pack, output, 1) == 0
    assert runtime.generation_is_current(output, story)


def test_submit_schema_exposes_existing_governance_and_evidence_contract():
    from controlled_runtime import candidate_definitions
    from story_schema_rules import TYPE_TO_COLLECTION, COLLECTION_KEYS
    tool = next(x for x in candidate_definitions() if x["name"] == "candidate_submit")
    schema = tool["inputSchema"]["properties"]["delta"]
    assert set(schema["required"]) >= {"新增元素", "修改元素"}
    buckets = schema["properties"]["修改元素"]["properties"]
    assert set(buckets) == set(COLLECTION_KEYS)
    item = buckets["事件集"]["items"]
    assert set(item["required"]) >= {"名称", "详情"}
    assert "提取理由" in item["properties"]["详情"]["required"]
    tags = schema["properties"]["治理操作"]["properties"]["治理标签"]["items"]
    assert set(tags["properties"]["类型"]["enum"]) == set(TYPE_TO_COLLECTION)
    assert "角色集" not in tags["properties"]["类型"]["enum"]
    assert set(tags["required"]) == {"类型", "名称", "保留标签", "降级标签", "理由"}


def test_submit_schema_keeps_standard_fields_outside_freeform_detail():
    """Advertising only name/detail encouraged real outputs to misplace fields."""
    from controlled_runtime import candidate_submit_schema
    from story_schema_rules import VALID_FIELDS
    buckets = candidate_submit_schema()["properties"]["修改元素"]["properties"]
    for collection, fields in VALID_FIELDS.items():
        item = buckets[collection]["items"]
        assert set(item["properties"]) == fields
        assert set(item["required"]) == {"名称", "详情"}  # Sparse patches remain legal.
    event = buckets["事件集"]["items"]["properties"]
    assert event["参与成员"] == {"type": "array", "items": {"type": "string"}}
    assert event["发生地点"]["type"] == "string"
    assert event["重量级"]["type"] == "integer"
    role = buckets["角色集"]["items"]["properties"]
    assert role["是否主角"]["type"] == "boolean"


def test_tag_errors_return_missing_and_unexpected_values_without_changing_rules():
    from validate_delta import validate_tag_governance_ops
    bad = tag_op(retained=["新标签"], demoted=["初入江湖"])
    errors = validate_tag_governance_ops(story_with_tags(), patch(bad), "governance")
    assert len(errors) == 1
    assert '遗漏=["人物类型:主角", "热血"]' in errors[0]
    assert '额外=["新标签"]' in errors[0]
    bad["类型"] = "角色集"
    errors = validate_tag_governance_ops(story_with_tags(), patch(bad), "governance")
    assert "角色" in errors[0] and "可选值" in errors[0]
    assert not validate_tag_governance_ops(story_with_tags(), patch(tag_op()), "governance")


@pytest.mark.parametrize("task", ["delta", "audit_correction"])
def test_small_story_inline_and_tools_modes_keep_same_evidence(tmp_path, monkeypatch, task):
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    _, paths, story = chapter_project(tmp_path)
    data = json.loads(story.read_text(encoding="utf-8"))
    data["介绍"]["描述"] = "不可遗漏的历史身份与事实"
    story.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    output = paths["delta"] if task == "delta" else pipeline.audit_paths(tmp_path, 1, 1)["correction"]
    monkeypatch.setenv("ND_RUNTIME_STORY_CONTEXT", "auto")
    inline = runtime.build_controlled_pack(tmp_path, task, paths["delta_task"], output, 1)
    meta = json.loads(inline.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    assert meta["story_context"] == "inline"
    text = inline.read_text(encoding="utf-8")
    assert runtime.encoded(data).decode("utf-8") in text
    assert "无需重复调用工具读取" in text
    monkeypatch.setenv("ND_RUNTIME_STORY_CONTEXT", "tools")
    tools_pack = runtime.build_controlled_pack(tmp_path, task, paths["delta_task"], output, 1)
    tools_meta = json.loads(tools_pack.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    assert tools_meta["story_context"] == "tools" and inline != tools_pack
    assert meta["inputs"] == tools_meta["inputs"]


def test_inline_respects_story_limit_and_total_prompt_headroom(monkeypatch):
    import controlled_runtime as runtime
    from context_budget import ContextBudget, estimate_tokens
    budget = ContextBudget(input_tokens=10000, output_tokens=1000, context_tokens=0, reserve_tokens=0)
    monkeypatch.delenv("ND_RUNTIME_STORY_CONTEXT", raising=False)
    small = {"介绍": {"描述": "少量历史"}}
    text, mode = runtime.story_context_suffix(small, "简短任务", budget)
    assert mode == "inline" and runtime.encoded(small).decode() in text
    _, mode = runtime.story_context_suffix(small, "长原文" * 1000, budget)
    assert mode == "tools"
    large = {"介绍": {"描述": "历史" * 1000}}
    assert estimate_tokens(runtime.encoded(large).decode()) > budget.usable_input // 8
    assert runtime.story_context_suffix(large, "简短任务", budget)[1] == "tools"
    monkeypatch.setenv("ND_RUNTIME_STORY_CONTEXT", "invalid")
    with pytest.raises(ValueError, match="ND_RUNTIME_STORY_CONTEXT"):
        runtime.story_context_suffix(small, "简短任务", budget)


def test_prompt_and_hash_use_same_story_bytes_during_concurrent_edit(tmp_path, monkeypatch):
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    _, paths, story = chapter_project(tmp_path)
    # Avoid an unrelated normalization step; mutate after the builder has read
    # the story but before it used to calculate the evidence hash a second time.
    monkeypatch.setattr(pipeline, "ensure_story", lambda _: story)
    before = story.read_bytes()
    render = pipeline.render_prompt

    def racing_render(*args, **kwargs):
        text = render(*args, **kwargs)
        data = json.loads(before)
        data["介绍"]["描述"] = "并发写入的新版本"
        story.write_bytes(runtime.encoded(data))
        return text

    monkeypatch.setattr(pipeline, "render_prompt", racing_render)
    pack = runtime.build_controlled_pack(tmp_path, "delta", paths["delta_task"], paths["delta"], 1)
    config = json.loads(pack.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    assert config["base_revision"] == hashlib.sha256(before).hexdigest()
    assert config["base_revision"] != hashlib.sha256(story.read_bytes()).hexdigest()
    assert "并发写入的新版本" not in pack.read_text(encoding="utf-8")
