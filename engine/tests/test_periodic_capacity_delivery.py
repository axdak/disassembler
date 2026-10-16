"""Offline regressions for exhausted CLI turns and story-first delivery."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import run_pipeline as pipeline
from context_budget import CONTEXT_EXCEEDED, OUTPUT_TRUNCATED
from merge_delta import save_json
from provider_stream import CodeBuddyCapacityLog
from story_delivery import ensure_delivery_governance, prepare_delivery, finish_introduction
from test_audit_pack import empty_story, write_completed_chapter
from test_agent_worker import run_worker as invoke_cli


def test_stream_length_stops_before_consuming_fake_success():
    events = [
        {"type": "stream_event", "_messageId": "model-request-1", "event": {
            "type": "message_delta", "delta": {"stop_reason": "max_tokens"}}},
        {"type": "result", "structured_output": {"章节范围": "001-005"}},
    ]
    rc, artifact, args = invoke_cli("codebuddy", "ND_CODEBUDDY_BIN",
                                    {"FAKE_CLI_STDOUT": "\n".join(json.dumps(e) for e in events)},
                                    output_name="correction.json", task_type="audit_correction")
    # 输出被截断只说明输出预算不足，不能报成"输入材料超容量"（那会让上层不断拆分材料）。
    assert rc.returncode == OUTPUT_TRUNCATED != CONTEXT_EXCEEDED
    assert artifact == ""
    assert "不是输入材料超容量" in rc.stdout
    tools = args["argv"][args["argv"].index("--tools") + 1]
    assert tools == "StructuredOutput"
    assert "mcp__nd_context__read_context" in args["argv"][args["argv"].index("--allowedTools") + 1]


@pytest.mark.parametrize("task_type", ["audit_correction", "bounded_fragment"])
def test_stream_allows_more_than_eight_requests_with_restricted_tools(task_type):
    """Evidence-reading turns must not be mistaken for an exhausted response."""
    patch = fake_patch(1, 5)
    expected = patch if task_type == "audit_correction" else {"overview": "完整证据索引", "content": patch}
    events = [{"type": "stream_event", "_requestId": "same-conversation",
               "_messageId": f"model-{i}", "event": {"type": "message_start"}}
              for i in range(12)]
    events.append({"type": "result", "structured_output": expected})
    rc, artifact, args = invoke_cli("codebuddy", "ND_CODEBUDDY_BIN",
                                   {"FAKE_CLI_STDOUT": "\n".join(json.dumps(e) for e in events),
                                    "ND_CODEBUDDY_STRUCTURED_MAX_TURNS": "", "ND_CODEBUDDY_ARGS": ""},
                                   output_name="correction.json", task_type=task_type)
    assert rc.returncode == 0 and json.loads(artifact) == expected
    assert "CLI 模型请求第 12 轮" in rc.stdout
    assert "--max-turns" not in args["argv"]
    assert args["argv"][args["argv"].index("--tools") + 1] == "StructuredOutput"
    assert "mcp__nd_context__read_context" in args["argv"][args["argv"].index("--allowedTools") + 1]


def test_explicit_provider_turn_limit_is_not_clamped_or_treated_as_capacity():
    """Legacy opt-in CLI limits retain their own protocol error classification."""
    event = {"type": "result", "subtype": "error_max_turns", "is_error": True}
    rc, artifact, args = invoke_cli("codebuddy", "ND_CODEBUDDY_BIN",
                                   {"FAKE_CLI_STDOUT": json.dumps(event),
                                    "ND_CODEBUDDY_STRUCTURED_MAX_TURNS": "20", "ND_CODEBUDDY_ARGS": ""},
                                   output_name="correction.json", task_type="audit_correction")
    assert args["argv"][args["argv"].index("--max-turns") + 1] == "20"
    assert rc.returncode == 66 and artifact == ""


def test_stream_does_not_treat_evidence_text_as_capacity_metadata():
    patch = fake_patch(1, 5)
    events = [{"type": "stream_event", "event": {"type": "content_block_delta",
                "choices": None, "delta": {"type": "text_delta", "text": 'finish_reason="length"'}}},
              {"type": "result", "structured_output": patch}]
    result, artifact, _ = invoke_cli("codebuddy", "ND_CODEBUDDY_BIN",
                                   {"FAKE_CLI_STDOUT": "\n".join(json.dumps(x) for x in events)},
                                   output_name="correction.json", task_type="audit_correction")
    assert result.returncode == 0 and json.loads(artifact) == patch


def test_codebuddy_masked_length_uses_matching_diagnostic_id(tmp_path):
    """Replay 2.148's end_turn conversion; the actual length event stays in logs."""
    event = {"type": "stream_event", "_requestId": "conversation-root",
             "_messageId": "model-request-1", "event": {
                 "type": "message_delta", "delta": {"stop_reason": "end_turn"}}}
    result, artifact, _ = invoke_cli("codebuddy", "ND_CODEBUDDY_BIN",
                                   {"FAKE_CLI_STDOUT": json.dumps(event),
                                    "FAKE_CLI_CAPACITY_LOG": "1", "CODEBUDDY_CONFIG_DIR": str(tmp_path)},
                                   output_name="correction.json", task_type="audit_correction")
    assert result.returncode == OUTPUT_TRUNCATED and artifact == ""


def test_capacity_log_ignores_old_and_other_worker_requests(tmp_path):
    folder = tmp_path / datetime.now().strftime("%Y-%m-%d")
    folder.mkdir()
    log = folder / "book__123.log"
    line = 'finish_reason="length" received, requestId=own-request, chunks=6934\n'
    log.write_text(line, encoding="utf-8")
    watcher = CodeBuddyCapacityLog(Path("book"), tmp_path)
    assert not watcher.check({"own-request"})
    with log.open("a", encoding="utf-8") as f:
        f.write(line.replace("own-request", "another-worker"))
    assert not watcher.check({"own-request"})
    with log.open("a", encoding="utf-8") as f:
        f.write(line)
    assert watcher.check({"own-request"})


def fake_patch(first, last):
    return {"章节范围": f"第{first:03d}章-第{last:03d}章", "新增元素": {}, "修改元素": {},
            "治理类型": "no_change", "质量说明": "完整核对原文和各集合，未发现需改动事项。",
            "证据范围": [f"第{i:03d}章" for i in range(first, last + 1)]}


def test_runtime_split_resumes_children_without_replaying_parent(tmp_path, monkeypatch):
    for i in range(1, 6):
        write_completed_chapter(tmp_path, i)
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    calls = []
    bounded_calls = []
    cancel_once = [True]

    def worker(command, root, kind, pack, output, *args, **kwargs):
        text = pack.read_text(encoding="utf-8")
        if kind == "audit_correction":
            calls.append(output.name)
            if output.name == "correction_001-005.json":
                return 64
            if output.name == "correction_004-005.json" and cancel_once[0]:
                cancel_once[0] = False
                return 130
            a, b = map(int, re.findall(r"\d+", output.stem))
            save_json(output, fake_patch(a, b))
        else:
            assert kind == "bounded_fragment"
            bounded_calls.append(str(pack))
            review = re.search(r"复核材料指纹：([0-9a-f]{64})", text)
            content = {"checked": review[1], "unresolved": [], "addition": None} if review else fake_patch(1, 5)
            save_json(output, {"overview": "完整证据索引", "content": content})
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_periodic_governance(tmp_path, 1, 5, audit_command="fake", worker_loop="continuous") == 130
    assert pipeline.audit_range_is_resolved(tmp_path, 1, 3)
    assert pipeline.run_periodic_governance(tmp_path, 1, 5, audit_command="fake", worker_loop="continuous") == 0
    assert pipeline.audit_range_is_resolved(tmp_path, 1, 5)
    assert calls.count("correction_001-005.json") == 1
    assert calls.count("correction_001-003.json") == 1
    assert calls.count("correction_004-005.json") == 2  # Cancelled, then completed.
    assert len(bounded_calls) < 10  # Small sources share groups instead of per-file calls.
    evidence = tmp_path / "质量治理/周期审计/audit_001-005.evidence.md"
    body = evidence.read_text(encoding="utf-8")
    for row in json.loads(evidence.with_suffix(".json").read_text(encoding="utf-8")):
        assert body[row["start"]:row["end"]] == Path(row["source"]).read_text(encoding="utf-8")
    before = calls[:]
    assert pipeline.run_periodic_governance(tmp_path, 1, 5, audit_command="fake", worker_loop="continuous") == 0
    assert calls == before


def test_delivery_cycles_include_tail_and_reject_incomplete_chapters(tmp_path, monkeypatch):
    for i in range(1, 8):
        write_completed_chapter(tmp_path, i)
    seen = []
    monkeypatch.setattr(pipeline, "run_periodic_governance", lambda p, a, b, **kw: seen.append((a, b)) or 0)
    assert ensure_delivery_governance(tmp_path, "fake") == 0
    assert seen == [(1, 5), (6, 7)]
    monkeypatch.setattr(pipeline, "is_chapter_complete", lambda *a: False)
    assert ensure_delivery_governance(tmp_path, "fake") == 1
    assert len(seen) == 2


def test_introduction_changes_only_intro_and_reuses_completed_work(tmp_path):
    source = tmp_path / "故事结构_增量.json"
    save_json(source, empty_story())
    assert prepare_delivery(tmp_path) == 0
    draft = tmp_path / "故事结构_草稿.json"
    before = json.loads(draft.read_text(encoding="utf-8"))
    calls = []

    def worker(command, root, kind, pack, output, **kwargs):
        text = pack.read_text(encoding="utf-8")
        calls.append(text)
        intro = None if "content 必须为 null" in text else {"标题": "测试故事", "描述": "完整范围的正式简介。"}
        save_json(output, {"overview": "完整来源及主线索引", "content": intro})
        return 0

    assert finish_introduction(tmp_path, "fake", worker) == 0
    after = json.loads(draft.read_text(encoding="utf-8"))
    assert {k: v for k, v in before.items() if k != "介绍"} == {k: v for k, v in after.items() if k != "介绍"}
    count = len(calls)
    assert prepare_delivery(tmp_path) == 0
    assert finish_introduction(tmp_path, "fake", worker) == 0
    assert len(calls) == count
    assert pipeline.cmd_finalize(tmp_path) == 0
    import hashlib
    receipt = json.loads((tmp_path / "质量治理/最终审计/finalize_log.json").read_text(encoding="utf-8"))
    for key, path in [("process_sha256", source), ("draft_sha256", draft), ("final_sha256", tmp_path / "故事结构.json")]:
        assert receipt[key] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_wrong_intro_contract_uses_bounded_repair_and_keeps_draft(tmp_path):
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    prepare_delivery(tmp_path)
    draft = tmp_path / "故事结构_草稿.json"
    before = draft.read_bytes()
    calls = []

    def worker(command, root, kind, pack, output, **kwargs):
        text = pack.read_text(encoding="utf-8")
        calls.append(text)
        save_json(output, {"overview": "证据位置", "content": None if "content 必须为 null" in text else {"角色集": []}})
        return 0

    assert finish_introduction(tmp_path, "fake", worker) == 65
    assert draft.read_bytes() == before
    count = len(calls)
    assert finish_introduction(tmp_path, "fake", worker) == 65
    assert len(calls) == count


def test_legacy_report_resume_delivers_json_first_without_losing_reports(tmp_path):
    from pipeline_state import PipelineState, STAGES
    from orchestrator import Orchestrator
    state = PipelineState.load(tmp_path)
    state.flow_version = 1
    state.current_stage = "fullbook_analysis"
    state.completed = ["chapter_analysis", "chapter_delta", "chapter_structure", "visual_assets"]
    state.fullbook_completed_tasks = ["summary", "characters"]
    state.save()
    orch = Orchestrator(tmp_path)
    orch._reconcile_recovery_state()
    assert orch.state.current_stage == "final_pack"
    assert orch.state.fullbook_completed_tasks == ["summary", "characters"]
    assert "visual_assets" in orch.state.completed
    orch.state.mark_completed("finalize")
    assert orch.state.current_stage == "fullbook_analysis"
    assert STAGES.index("finalize") < STAGES.index("fullbook_analysis")


@pytest.mark.parametrize("changed_file", ["故事结构_增量.json", "故事结构_草稿.json"])
@pytest.mark.parametrize("edit_phase", ["before_tree", "after_request"])
def test_introduction_rejects_source_or_draft_changes_before_publication(tmp_path, monkeypatch, changed_file, edit_phase):
    """A concurrent accepted edit must survive a stale introduction candidate."""
    import story_delivery
    from project_lock import project_write_lock
    from repair_budget import REPAIR_EXHAUSTED
    source = tmp_path / "故事结构_增量.json"
    save_json(source, empty_story())
    prepare_delivery(tmp_path)
    draft = tmp_path / "故事结构_草稿.json"
    before = draft.read_bytes()
    changed = tmp_path / changed_file
    updated = json.loads(changed.read_text(encoding="utf-8"))
    updated["介绍"]["并发备注"] = "另一个写入者刚核验的补充"
    original_tree = story_delivery.CapacityTree
    original_request = original_tree.request

    def edit():
        with project_write_lock(tmp_path):
            save_json(changed, updated)

    class ConcurrentTree(original_tree):
        def __init__(self, *args, **kwargs):
            if edit_phase == "before_tree":
                edit()
            super().__init__(*args, **kwargs)

        def request(self, *args, **kwargs):
            result = original_request(self, *args, **kwargs)
            if edit_phase == "after_request" and kwargs.get("validate_content"):
                # Edit after worker protection ends, as an independent host writer.
                edit()
            return result

    def worker(command, root, kind, pack, output, **kwargs):
        prompt = pack.read_text(encoding="utf-8")
        content = None if "content 必须为 null" in prompt else {"标题": "正式标题", "描述": "有依据的正式简介"}
        save_json(output, {"overview": "完整来源索引", "content": content})
        return 0

    monkeypatch.setattr(story_delivery, "CapacityTree", ConcurrentTree)
    assert finish_introduction(tmp_path, "offline", worker) == REPAIR_EXHAUSTED
    assert json.loads(changed.read_text(encoding="utf-8")) == updated
    if changed != draft:
        assert draft.read_bytes() == before
    assert not (tmp_path / "质量治理/最终审计/交付/introduction.json").exists()


def test_introduction_publishes_under_project_lock_and_retains_metadata(tmp_path, monkeypatch):
    """Publication must exclude another host writer for its complete merge."""
    import threading
    import story_delivery
    from project_lock import project_write_lock
    source = tmp_path / "故事结构_增量.json"
    save_json(source, empty_story())
    prepare_delivery(tmp_path)
    draft = tmp_path / "故事结构_草稿.json"
    value = json.loads(draft.read_text(encoding="utf-8"))
    value["介绍"]["备注"] = "既有简介元数据"
    save_json(draft, value)
    observed = []
    original_save = story_delivery.save_json

    def inspect_publication(path, value):
        if path == draft:
            def competing_writer():
                try:
                    with project_write_lock(tmp_path, timeout=0.02):
                        observed.append("unlocked")
                except TimeoutError:
                    observed.append("locked")
            competitor = threading.Thread(target=competing_writer)
            competitor.start()
            competitor.join(timeout=1)
            assert not competitor.is_alive()
        return original_save(path, value)

    def worker(command, root, kind, pack, output, **kwargs):
        prompt = pack.read_text(encoding="utf-8")
        content = None if "content 必须为 null" in prompt else {"标题": "正式标题", "描述": "有依据的正式简介"}
        save_json(output, {"overview": "完整来源索引", "content": content})
        return 0

    monkeypatch.setattr(story_delivery, "save_json", inspect_publication)
    assert finish_introduction(tmp_path, "offline", worker) == 0
    assert observed == ["locked"]
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"] == {
        "标题": "正式标题", "描述": "有依据的正式简介", "备注": "既有简介元数据"}
