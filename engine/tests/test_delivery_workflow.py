"""交付步骤只有一处定义，整条执行与逐阶段续接具有相同边界。"""
import pytest

import delivery_gate
import delivery_workflow as workflow
import run_pipeline
import story_delivery


@pytest.fixture
def actions(monkeypatch):
    """隔离模型和文件写入，记录具有业务含义的交付动作。"""
    calls = []
    monkeypatch.setattr(delivery_gate, "check_delivery_prerequisites", lambda *a, **k: calls.append("gate") or 0)
    monkeypatch.setattr(story_delivery, "prepare_delivery", lambda *a: calls.append("prepare") or 0)
    monkeypatch.setattr(story_delivery, "commit_delivery", lambda *a, **k: calls.append("commit") or 0)
    monkeypatch.setattr(delivery_gate, "report_delivery_quality", lambda *a: calls.append("quality") or 0)
    monkeypatch.setattr(run_pipeline, "cmd_finalize", lambda *a: calls.append("publish") or 0)
    monkeypatch.setattr(story_delivery, "finish_introduction", lambda *a, **k: pytest.fail("默认收尾禁止生成简介"))
    return calls


def test_full_delivery_and_staged_resume_share_order(tmp_path, actions):
    assert workflow.run_delivery(tmp_path) == 0
    complete = list(actions)
    actions.clear()
    for stage in workflow.DELIVERY_STAGES:
        assert workflow.run_delivery_stage(tmp_path, stage) == 0
    assert actions == complete == ["gate", "prepare", "gate", "commit", "gate", "quality", "publish"]
    from pipeline_state import STAGES
    assert tuple(STAGES[2:5]) == workflow.DELIVERY_STAGES


@pytest.mark.parametrize("stage", ["final_pack", "commit_final_draft", "finalize"])
def test_every_resumable_stage_rechecks_prerequisites(tmp_path, actions, monkeypatch, stage):
    def gate(project, **options):
        assert options["end_chapter_seq"] == 7
        return 68
    monkeypatch.setattr(delivery_gate, "check_delivery_prerequisites", gate)
    assert workflow.run_delivery_stage(tmp_path, stage, end_chapter_seq=7) == 68
    assert actions == []


@pytest.mark.parametrize("name,code,expected", [
    ("prepare_delivery", 11, ["gate"]),
    ("commit_delivery", 65, ["gate", "prepare", "gate"]),
    ("cmd_finalize", 124, ["gate", "prepare", "gate", "commit", "gate", "quality"]),
])
def test_failure_returns_original_code_and_stops_later_actions(tmp_path, actions, monkeypatch, name, code, expected):
    owner = run_pipeline if name == "cmd_finalize" else story_delivery
    monkeypatch.setattr(owner, name, lambda *a, **k: code)
    progress = []
    assert workflow.run_delivery(tmp_path, progress=lambda percent, detail: progress.append(percent)) == code
    assert actions == expected
    assert 100 not in progress


def test_publish_resume_does_not_repeat_preparation(tmp_path, actions):
    assert workflow.run_delivery_stage(tmp_path, "finalize") == 0
    assert actions == ["gate", "quality", "publish"]


def test_default_delivery_does_not_use_configured_model(tmp_path, actions, monkeypatch):
    def local_commit(project, command, **options):
        assert command == ""
        assert options["repair_history"] is False
        return 0
    monkeypatch.setattr(story_delivery, "commit_delivery", local_commit)
    assert workflow.run_delivery(tmp_path, command="configured-model") == 0


def test_explicit_history_maintenance_keeps_worker_options(tmp_path, actions, monkeypatch):
    calls = []
    def introduction(project, command, worker, **options):
        calls.append(("introduction", command, worker, options))
        return 0
    def commit(project, command, **options):
        calls.append(("commit", command, options))
        return 0
    worker, repair = object(), object()
    monkeypatch.setattr(story_delivery, "finish_introduction", introduction)
    monkeypatch.setattr(story_delivery, "commit_delivery", commit)
    assert workflow.run_delivery(tmp_path, repair_history=True, command="offline", worker=worker,
                                 repair_worker=repair, timeout=23, retries=1, repair_limit=2) == 0
    assert calls[0][1:3] == ("offline", worker)
    assert calls[0][3]["timeout"] == 23
    assert calls[1][2]["repair_history"] is True
    assert calls[1][2]["repair_worker"] is repair
    assert calls[1][2]["repair_limit"] == 2
    assert calls[1][2]["retries"] == 1


def test_unknown_stage_fails_before_writing(tmp_path, actions):
    with pytest.raises(ValueError, match="交付阶段"):
        workflow.run_delivery_stage(tmp_path, "unknown")
    assert actions == []
