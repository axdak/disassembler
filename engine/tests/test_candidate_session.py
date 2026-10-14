"""Candidate operations are durable and never publish to the authoritative story."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_audit_pack import empty_delta, empty_story, write_json


def session(tmp_path):
    from candidate_session import CandidateSession
    story = tmp_path / "故事结构_增量.json"
    write_json(story, empty_story())
    return CandidateSession(tmp_path / "candidate", story, "delta", chapter="第001章"), story


def test_stage_retry_is_idempotent_and_survives_process_reconstruction(tmp_path):
    from candidate_session import CandidateSession
    candidate, story = session(tmp_path)
    original = story.read_bytes()
    first = candidate.stage("op-1", 0, empty_delta(1))
    restarted = CandidateSession(candidate.directory, story, "delta", chapter="第001章")
    assert restarted.stage("op-1", 0, empty_delta(1)) == first
    assert restarted.status()["revision"] == 1
    changed = empty_delta(1); changed["处理备注"] = "不同参数"
    with pytest.raises(ValueError, match="操作ID"):
        restarted.stage("op-1", 0, changed)
    with pytest.raises(ValueError, match="版本"):
        restarted.stage("op-2", 0, changed)
    assert story.read_bytes() == original


def test_candidate_validation_rejects_unknown_target_and_accepts_corrected_candidate(tmp_path):
    candidate, story = session(tmp_path)
    bad = empty_delta(1)
    bad["修改元素"] = {"角色集": [{"名称": "不存在", "详情": {"提取理由": "本章提及"}}]}
    candidate.stage("bad", 0, bad)
    feedback = candidate.validate(1)
    assert not feedback["passed"]
    assert any("修改目标" in item for item in feedback["errors"])
    with pytest.raises(ValueError, match="校验"):
        candidate.finish(1)
    candidate.stage("correct", 1, empty_delta(1))
    receipt = candidate.finish(2)
    assert receipt["revision"] == 2
    assert candidate.completed_payload(receipt) == empty_delta(1)
    assert json.loads(story.read_text(encoding="utf-8")) == empty_story()


def test_candidate_patch_is_atomic_and_invalidates_old_completion(tmp_path):
    candidate, _ = session(tmp_path)
    candidate.stage("start", 0, empty_delta(1))
    receipt = candidate.finish(1)
    candidate.patch("note", 1, [{"path": "/处理备注", "value": "已核对本章原文"}])
    with pytest.raises(ValueError, match="版本|凭证"):
        candidate.completed_payload(receipt)
    before = candidate.status()
    with pytest.raises(ValueError):
        candidate.patch("escape", 2, [{"path": "/新增元素", "value": {}},
                                      {"path": "/../../story.json", "value": {}}])
    assert candidate.status() == before


def test_candidate_scope_and_corruption_fail_closed(tmp_path):
    candidate, story = session(tmp_path)
    with pytest.raises(ValueError, match="章节"):
        candidate.stage("wrong-chapter", 0, empty_delta(2))
    candidate.stage("start", 0, empty_delta(1))
    state = json.loads(candidate.state_path.read_text(encoding="utf-8"))
    state["delta"]["处理备注"] = "被外部改动"
    write_json(candidate.state_path, state)
    with pytest.raises(ValueError, match="损坏"):
        candidate.status()


@pytest.mark.parametrize("label", ["0001", "第001章", "001章", "第001章 测试标题"])
def test_chapter_scope_accepts_template_machine_id_and_display_label(tmp_path, label):
    """The generation template emits four-digit IDs, matching normal Delta input."""
    candidate, _ = session(tmp_path)
    delta = empty_delta(1)
    delta["章节"] = label
    candidate.stage("chapter", 0, delta)
    assert candidate.validate(1)["passed"]


@pytest.mark.parametrize("label", ["0002", "0001-0002", "0001junk", "", 1, True])
def test_machine_chapter_id_does_not_relax_scope_or_string_type(tmp_path, label):
    candidate, _ = session(tmp_path)
    delta = empty_delta(1)
    delta["章节"] = label
    with pytest.raises(ValueError, match="章节"):
        candidate.stage("chapter", 0, delta)
    assert candidate.status()["revision"] == 0


def test_project_lock_excludes_another_process_and_releases_after_exception(tmp_path):
    from project_lock import project_write_lock
    script = "from pathlib import Path; from project_lock import project_write_lock\nwith project_write_lock(Path(__import__('sys').argv[1]), timeout=0.1): print('acquired')"
    kwargs = dict(args=[sys.executable, "-X", "utf8", "-c", script, str(tmp_path)],
                  cwd=str(Path(__file__).resolve().parents[1] / "scripts"),
                  capture_output=True, text=True, encoding="utf-8", timeout=10)
    with pytest.raises(RuntimeError):
        with project_write_lock(tmp_path):
            with project_write_lock(tmp_path):  # Same owner may call a nested commit.
                blocked = subprocess.run(**kwargs)
                assert blocked.returncode != 0
                assert "TimeoutError" in blocked.stderr
            raise RuntimeError("interrupted")
    assert subprocess.run(**kwargs).stdout.strip() == "acquired"


def test_governance_candidate_pins_window_and_full_evidence_coverage(tmp_path):
    from candidate_session import CandidateSession
    story = tmp_path / "story.json"; write_json(story, empty_story())
    candidate = CandidateSession(tmp_path / "audit", story, "audit_correction", "第001章-第002章")
    patch = {"章节范围": "第001章-第003章", "治理类型": "no_change", "质量说明": "已核对",
             "证据范围": ["第001章", "第002章"], "新增元素": {}, "修改元素": {}}
    with pytest.raises(ValueError, match="范围"):
        candidate.stage("wrong", 0, patch)
    patch["章节范围"] = "第001-002章"
    candidate.stage("right", 0, patch)
    assert candidate.validate(1)["passed"]
    candidate.patch("missing", 1, [{"path": "/证据范围", "value": ["第001章"]}])
    assert not candidate.validate(2)["passed"]


def test_submit_validates_and_completes_in_one_idempotent_operation(tmp_path):
    candidate, _ = session(tmp_path)
    result = candidate.submit("complete", 0, empty_delta(1))
    assert result["passed"] and result["completion"]["revision"] == 1
    assert candidate.completed_payload(result["completion"]) == empty_delta(1)
    assert candidate.submit("complete", 0, empty_delta(1)) == result
    assert candidate.status()["operations_used"] == 1


def test_submit_returns_validation_errors_without_signing_invalid_candidate(tmp_path):
    candidate, _ = session(tmp_path)
    delta = empty_delta(1)
    delta["修改元素"] = {"角色集": [{"名称": "未登记", "详情": {"状态": "等待"}}]}
    result = candidate.submit("invalid", 0, delta)
    assert not result["passed"] and result["errors"]
    assert result["completion"] is None and not candidate.status()["completed"]
    assert candidate.submit("invalid", 0, delta) == result
    fixed = candidate.submit("corrected", 1, empty_delta(1))
    assert fixed["passed"] and fixed["completion"]["revision"] == 2
