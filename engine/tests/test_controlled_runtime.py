"""Offline integration of thin prompts, frozen evidence and candidate MCP."""
import json
import sys
import subprocess
from pathlib import Path

import pytest
from test_architecture_recovery import chapter_project
from test_audit_pack import empty_delta
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def test_controlled_delta_keeps_full_sources_without_embedding_history(tmp_path):
    from controlled_runtime import build_controlled_pack
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    data = json.loads(story.read_text(encoding="utf-8"))
    data["角色集"] = [{"名称": "主角", "详情": {"0001-旧事": "很久以前的承诺" * 15000}}]
    story.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack = build_controlled_pack(tmp_path, "delta", paths["delta_task"], paths["delta"], 1)
    text = pack.read_text(encoding="utf-8")
    assert chapter.read_text(encoding="utf-8") in text
    assert paths["analysis"].read_text(encoding="utf-8") in text
    assert "很久以前的承诺" not in text
    assert len(text) < 20000
    sources = json.loads(pack.with_suffix(".context-inputs.json").read_text(encoding="utf-8"))
    assert str(story.resolve()) in sources
    assert build_controlled_pack(tmp_path, "delta", paths["delta_task"], paths["delta"], 1) == pack


def test_candidate_mcp_rejects_invalid_candidate_then_finishes_same_session(tmp_path):
    from controlled_runtime import build_controlled_pack
    from worker_context import snapshot_context
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack = build_controlled_pack(tmp_path, "delta", paths["delta_task"], paths["delta"], 1)
    frozen = tmp_path / "frozen"; frozen.mkdir()
    manifest = snapshot_context(tmp_path, pack, [], frozen)
    config = json.loads(pack.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    data = json.loads(manifest.read_text(encoding="utf-8")); data["candidate"] = config
    manifest.write_text(json.dumps(data), encoding="utf-8")
    calls = [
        ("candidate_submit", {"operation_id": "start", "expected_revision": 0, "delta": empty_delta(1)}),
        ("candidate_validate", {"expected_revision": 1}),
        ("candidate_finish", {"expected_revision": 1}),
        ("candidate_patch", {"operation_id": "escape", "expected_revision": 1,
                              "changes": [{"path": "/../../story.json", "value": "bad"}]}),
    ]
    requests = [{"id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}
                for i, (name, args) in enumerate(calls)]
    result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "tools/worker_context.py"),
                             str(manifest), "--story-tools", "--candidate-tools"],
                            input="\n".join(map(json.dumps, requests)) + "\n", capture_output=True,
                            text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(row)["result"] for row in result.stdout.splitlines()]
    assert json.loads(responses[1]["content"][0]["text"])["passed"]
    assert json.loads(responses[2]["content"][0]["text"])["revision"] == 1
    assert responses[3]["isError"]
    assert json.loads(story.read_text(encoding="utf-8"))["角色集"] == []


def test_runtime_budget_survives_reconnect_and_reserves_before_dispatch(tmp_path):
    from controlled_runtime import RuntimeBudget
    budget = RuntimeBudget(tmp_path, max_dispatches=2, max_tools=2, max_tokens=100)
    budget.dispatch(10)
    budget.tool(10)
    restarted = RuntimeBudget(tmp_path, max_dispatches=2, max_tools=2, max_tokens=100)
    restarted.dispatch(10)
    restarted.tool(10)
    with pytest.raises(ValueError, match="预算"):
        restarted.dispatch(10)
    with pytest.raises(ValueError, match="预算"):
        restarted.tool(10)


def test_runtime_rejects_stale_completion_and_missing_tool_receipt(tmp_path):
    from controlled_runtime import accept_completion
    from candidate_session import CandidateSession
    _, _, story = chapter_project(tmp_path)
    session = CandidateSession(tmp_path / "session", story, "delta", "第001章")
    with pytest.raises(ValueError):
        accept_completion(session, json.dumps(empty_delta(1)))
    session.stage("one", 0, empty_delta(1)); receipt = session.finish(1)
    assert accept_completion(session, json.dumps({"structured_output": receipt})) == empty_delta(1)
    assert session.status()["accepted"]
    session.patch("two", 1, [{"path": "/处理备注", "value": "后续改动"}])
    assert not session.status()["accepted"]
    with pytest.raises(ValueError):
        accept_completion(session, json.dumps({"structured_output": receipt}))


@pytest.mark.parametrize("valid_receipt", [True, False])
@pytest.mark.parametrize("interrupted_proposal", [False, True, "malformed"])
def test_codebuddy_adapter_accepts_only_host_verified_candidate(tmp_path, monkeypatch, valid_receipt,
                                                               interrupted_proposal):
    import agent_worker
    import run_pipeline as pipeline
    from controlled_runtime import build_controlled_pack, RECEIPT_SCHEMA
    from candidate_session import CandidateSession
    chapter, paths, story = chapter_project(tmp_path)
    # Production stdout artifacts use LF even on Windows; snapshots must retain
    # the exact bytes for the host's evidence version check.
    agent_worker._write_captured_output_atomically(
        paths["analysis"], paths["analysis"].read_text(encoding="utf-8"))
    pipeline.prepare_chapter_delta(tmp_path, chapter, force=True)
    pack = build_controlled_pack(tmp_path, "delta", paths["delta_task"], paths["delta"], 1)
    config = json.loads(pack.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("ND_TASK_PACK", str(pack))
    monkeypatch.setenv("ND_EXPECTED_OUTPUT", str(paths["delta"]))
    monkeypatch.delenv("ND_EXPECTED_OUTPUTS", raising=False)
    if interrupted_proposal:
        # Tool success alone is not the model's final delivery. A stopped probe
        # must resume generation instead of silently publishing an empty delta.
        pending = CandidateSession(Path(config["directory"]), story, "delta", "第001章")
        if interrupted_proposal == "malformed":
            bad = empty_delta(1)
            bad["新增元素"] = {"角色集": 1}
            pending.stage("probe", 0, bad)
        else:
            pending.submit("probe", 0, empty_delta(1))

    def fake_model(command, prompt, temporary):
        mcp = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        args = mcp["mcpServers"]["nd_context"]["args"]
        assert "--candidate-tools" in args
        manifest = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        frozen = next(Path(item["snapshot"]) for item in manifest["files"] if item["source"] == str(story))
        session = CandidateSession(Path(config["directory"]), frozen, "delta", "第001章")
        assert '"accepted": false' in prompt
        if interrupted_proposal:
            assert '"candidate_validation"' in prompt
            if interrupted_proposal == "malformed":
                assert '"passed": false' in prompt and "候选字段类型错误" in prompt
            else:
                assert '"passed": true' in prompt
        session.stage("model-1", int(bool(interrupted_proposal)), empty_delta(1))
        value = session.finish(1 + int(bool(interrupted_proposal))) if valid_receipt else empty_delta(1)
        return subprocess.CompletedProcess(command, 0, json.dumps({"structured_output": value}), "")

    monkeypatch.setattr(agent_worker, "run_structured_stream", fake_model)
    command = agent_worker.provider_command("codebuddy", tmp_path, "30m", RECEIPT_SCHEMA,
                                            "delta", candidate_tools=True)
    assert "mcp__nd_context__candidate_stage" in command[command.index("--allowedTools") + 1]
    assert "mcp__nd_context__candidate_submit" in command[command.index("--allowedTools") + 1]
    assert "mcp__nd_context__story_read_fields" in command[command.index("--allowedTools") + 1]
    assert "--max-turns" in command
    result = agent_worker.run_provider("codebuddy", command, pack.read_text(encoding="utf-8"),
                                       tmp_path, "delta", candidate_config=config)
    assert result.returncode == (0 if valid_receipt else 66)
    if valid_receipt:
        assert json.loads(result.stdout)["structured_output"] == empty_delta(1)
        # Exercise the CLI main boundary too: the receipt schema must be selected
        # and the host must save the Delta, not mistake the receipt for the Delta.
        monkeypatch.setattr(sys, "argv", ["agent_worker.py", "--provider", "codebuddy"])
        monkeypatch.setenv("ND_TASK_TYPE", "delta")
        monkeypatch.setenv("ND_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_worker, "command_available", lambda _: True)
        # A completed durable candidate is a checkpoint, not a reason to ask the
        # model to restate the same receipt on another paid dispatch.
        monkeypatch.setattr(agent_worker, "run_structured_stream",
                            lambda *a: pytest.fail("completed candidate must resume without model call"))
        assert agent_worker.main() == 0
        assert json.loads(paths["delta"].read_text(encoding="utf-8")) == empty_delta(1)
        receipt = json.loads(paths["delta"].with_suffix(".generation.json").read_text(encoding="utf-8"))
        assert receipt["base_revision"] == config["base_revision"]
        import hashlib
        assert receipt["artifact_sha256"] == hashlib.sha256(paths["delta"].read_bytes()).hexdigest()


def test_generation_fence_rejects_torn_publication_and_resumes(tmp_path, monkeypatch):
    """A fresh receipt must never authorize the old artifact left by a crash."""
    import controlled_runtime as runtime
    _, paths, story = chapter_project(tmp_path)
    original = paths["delta"].read_bytes()
    content = original + b"\n"
    base = runtime.hashlib.sha256(story.read_bytes()).hexdigest()
    real_write = runtime.atomic_write_bytes

    def crash(path, data):
        if path == paths["delta"]:
            raise RuntimeError("publication interrupted")
        real_write(path, data)

    monkeypatch.setattr(runtime, "atomic_write_bytes", crash)
    with pytest.raises(RuntimeError, match="interrupted"):
        runtime.publish_generated_artifact(tmp_path, paths["delta"], content, base)
    assert paths["delta"].read_bytes() == original
    assert not runtime.generation_is_current(paths["delta"], story)
    monkeypatch.setattr(runtime, "atomic_write_bytes", real_write)
    runtime.publish_generated_artifact(tmp_path, paths["delta"], content, base)
    assert runtime.generation_is_current(paths["delta"], story)


def test_controlled_governance_verifies_original_bytes_before_normalizing(tmp_path):
    """Host formatting must not invalidate the receipt it just accepted."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    from test_audit_pack import write_no_change_correction
    _, _, story = chapter_project(tmp_path)
    write_no_change_correction(tmp_path, 1, 1)
    patch = tmp_path / "质量治理/周期审计/correction_001-001.json"
    payload = json.loads(patch.read_text(encoding="utf-8"))
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    runtime.publish_generated_artifact(tmp_path, patch, raw,
                                      runtime.hashlib.sha256(story.read_bytes()).hexdigest())
    assert pipeline.commit_governance(tmp_path, str(patch)) == 0
    assert pipeline.commit_governance(tmp_path, str(patch)) == 0
