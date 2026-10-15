"""Regression coverage for redundant evidence reads and effective concurrency."""
import sys
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def test_inline_evidence_is_not_resent_but_missing_evidence_remains_readable(tmp_path):
    from worker_context import ContextReader, snapshot_context

    book = tmp_path / "book"
    book.mkdir()
    source = book / "chapter.md"
    source.write_bytes("完整原文\r\n第二行".encode("utf-8"))
    extra = book / "archive.md"
    extra.write_text("需要按需核对的旧档案", encoding="utf-8")
    pack = book / "task.md"
    pack.write_text(f"已有原文：\n完整原文\n第二行\n档案 `{extra}`；原文 `{source}`", encoding="utf-8")
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    prompt = "宿主规则\n" + pack.read_text(encoding="utf-8")
    manifest = snapshot_context(book, pack, [], frozen, inline_prompt=prompt)
    reader = ContextReader(manifest)
    # No second copy or pagination loop for evidence already in the user prompt.
    for path in [pack, source]:
        receipt = reader.read(str(path))
        assert receipt["provided_in_prompt"] is True
        assert receipt["next_offset"] is None
        assert "text" not in receipt
        assert receipt["sha256"]
    assert reader.read(str(extra))["text"] == "需要按需核对的旧档案"
    # A later context compaction can explicitly restore the frozen evidence.
    assert reader.read(str(source), reread=True)["text"] == "完整原文\n第二行"
    # Coverage is frozen at dispatch and cannot be inferred from an old prompt.
    assert ContextReader(snapshot_context(book, pack, [], frozen)).read(str(pack))["text"]


def test_inline_marker_requires_complete_evidence_not_a_partial_match(tmp_path):
    from worker_context import ContextReader, snapshot_context

    pack = tmp_path / "task.md"
    pack.write_text("完整材料A\n完整材料B", encoding="utf-8")
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    reader = ContextReader(snapshot_context(tmp_path, pack, [], frozen, inline_prompt="完整材料A"))
    assert reader.read(str(pack))["text"] == "完整材料A\n完整材料B"


def test_configured_concurrency_reaches_five_without_cross_context_mutation(monkeypatch):
    import worker_admission as admission

    monkeypatch.delenv("ND_MAX_CONCURRENT_WORKERS", raising=False)
    gate = admission.WorkerAdmission()
    release = threading.Event()
    lock = threading.Lock()
    entered = []
    ready = threading.Event()

    def worker():
        with gate.slot() as acquired:
            assert acquired
            with lock:
                entered.append(1)
                if len(entered) == 5:
                    ready.set()
            release.wait(5)

    try:
        with admission.worker_concurrency_scope(5):
            assert gate.effective_limit() == 5
            with ThreadPoolExecutor(max_workers=5) as pool:
                jobs = [pool.submit(copy_context().run, worker) for _ in range(5)]
                try:
                    assert ready.wait(2), "Configured parallelism is still silently capped at two"
                finally:
                    release.set()
                for job in jobs:
                    job.result()
    finally:
        release.set()
    assert gate.effective_limit() == 2  # Standalone fallback remains bounded.


def test_explicit_operator_cap_and_context_reset_are_preserved(monkeypatch):
    import worker_admission as admission

    gate = admission.WorkerAdmission(2)
    with pytest.raises(RuntimeError):
        with admission.worker_concurrency_scope(5):
            assert gate.effective_limit() == 2
            raise RuntimeError("cancelled")
    assert gate.effective_limit() == 2


def test_delivery_trace_detects_incomplete_submission_without_exposing_values():
    from worker_stream_trace import DeliveryTrace

    trace = DeliveryTrace({"required": ["章节", "新增元素", "修改元素"]}, started=0)
    trace.observe({"type": "system", "_requestId": "conversation"}, now=1)
    trace.observe({"type": "stream_event", "_messageId": "req1", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "StructuredOutput", "id": "call1", "input": {}}}}, now=2)
    trace.observe({"type": "stream_event", "_messageId": "req1", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"章节":"秘密内容"}'}}}, now=3)
    trace.observe({"type": "stream_event", "_messageId": "req1", "event": {
        "type": "content_block_stop", "index": 0}}, now=120)
    report = trace.report(now=121)
    assert report["model_messages"] == 1  # Conversation IDs never add a round.
    assert report["max_event_gap_seconds"] == 117
    assert report["submissions"][0]["missing_required"] == ["新增元素", "修改元素"]
    assert report["submissions"][0]["sha256"]
    assert "秘密内容" not in json.dumps(report, ensure_ascii=False)


def test_delivery_trace_never_treats_unfinished_arguments_as_a_submission():
    from worker_stream_trace import DeliveryTrace

    trace = DeliveryTrace({}, started=0)
    trace.observe({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "StructuredOutput", "id": "x", "input": {}}}}, now=1)
    trace.observe({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"章节":'}}}, now=2)
    assert trace.report(now=3)["submissions"] == []


def test_protocol_heartbeat_does_not_hide_a_silent_delivery_tail():
    from worker_stream_trace import DeliveryTrace

    trace = DeliveryTrace({}, started=0)
    trace.observe({"type":"stream_event", "event":{"type":"message_start"}}, now=1)
    trace.observe({"type":"stream_event", "event":{"type":"ping"}}, now=150)
    assert trace.report(now=151)["silence_seconds"] == 150


def test_terminal_valid_result_does_not_wait_for_cli_shutdown(tmp_path, monkeypatch):
    """A complete native envelope is deliverable even if the CLI never exits."""
    import time
    import agent_worker

    script = tmp_path / "cli.py"
    script.write_text('import json,sys,time\nsys.stdin.read()\n'
                      'print(json.dumps({"type":"result","subtype":"success",'
                      '"structured_output":{"章节":"0001","新增元素":{},"修改元素":{}}}),flush=True)\n'
                      'time.sleep(90)\n', encoding="utf-8")
    started = time.monotonic()
    result = agent_worker.run_structured_stream([sys.executable, str(script), "--output-format", "json",
        "--json-schema", json.dumps({"type": "object", "required": ["章节", "新增元素", "修改元素"]})], "task", tmp_path)
    assert result.returncode == 0
    assert time.monotonic() - started < 15
    assert json.loads(result.stdout)["structured_output"]["章节"] == "0001"


def test_terminal_failure_releases_cli_without_publishing_candidate(tmp_path):
    """Explicit failure metadata must not become a success or wait for cleanup."""
    import time
    import agent_worker

    script = tmp_path / "failed_cli.py"
    script.write_text('import json,sys,time\nsys.stdin.read()\n'
                      'print(json.dumps({"type":"result","subtype":"error_during_execution",'
                      '"is_error":True,"structured_output":{"章节":"0001"}}),flush=True)\n'
                      'time.sleep(20)\n', encoding="utf-8")
    started = time.monotonic()
    result = agent_worker.run_structured_stream([sys.executable, str(script), "--output-format", "json",
        "--json-schema", json.dumps({"type":"object","required":["章节"]})], "task", tmp_path)
    assert result.returncode == 66
    assert not result.stdout
    assert time.monotonic() - started < 10


def test_terminal_envelope_rechecks_late_capacity_metadata(tmp_path, monkeypatch):
    """A final fast-path delivery must not skip a just-written length marker."""
    import agent_worker
    import provider_stream
    from context_budget import OUTPUT_TRUNCATED

    checks = iter([False, True])
    monkeypatch.setattr(provider_stream.CodeBuddyCapacityLog, "check", lambda self, ids: next(checks))
    script = tmp_path / "cli.py"
    script.write_text('import json,sys\nsys.stdin.read()\n'
                      'print(json.dumps({"type":"stream_event","_messageId":"req",'
                      '"event":{"type":"message_start"}}),flush=True)\n'
                      'print(json.dumps({"type":"result","subtype":"success",'
                      '"structured_output":{"章节":"0001"}}),flush=True)\n', encoding="utf-8")
    result = agent_worker.run_structured_stream([sys.executable, str(script), "--output-format", "json",
        "--json-schema", json.dumps({"type":"object","required":["章节"]})], "task", tmp_path)
    assert result.returncode == OUTPUT_TRUNCATED
    assert not result.stdout


@pytest.mark.parametrize("value", [
    {"type": "result", "subtype": "success", "structured_output": {"章节": "0001"}},
    {"type": "result", "is_error": True, "structured_output": {"章节": "0001", "新增元素": {}, "修改元素": {}}},
    {"type": "assistant", "structured_output": {"章节": "0001", "新增元素": {}, "修改元素": {}}},
])
def test_partial_or_failed_delivery_is_never_an_early_success(value):
    from worker_stream_trace import valid_terminal_result
    assert not valid_terminal_result(value, {"required": ["章节", "新增元素", "修改元素"]})


@pytest.mark.parametrize("mode,expected", [("two_phase", 5), ("interleaved", 6)])
def test_desktop_run_propagates_its_settings_to_admission(tmp_path, monkeypatch, mode, expected):
    from orchestrator import Orchestrator, Config
    from worker_admission import worker_admission

    # The real desktop entry point must freeze the setting before thread copies.
    monkeypatch.setattr(worker_admission, "limit", None)
    orch = Orchestrator(tmp_path, Config(analysis_concurrency=5, chapter_mode=mode))
    observed = []
    monkeypatch.setattr(orch, "_run", lambda: observed.append(worker_admission.effective_limit()) or 0)
    assert orch.run() == 0
    assert observed == [expected]
    assert worker_admission.effective_limit() == 2
