"""Interrupted deliveries retain their original validation and publication gates."""
import hashlib
from pathlib import Path

import pytest

from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_architecture_recovery import chapter_project
from test_audit_pack import empty_delta
from test_fault_recovery import invoke, scenario, state


def test_recovery_freeze_rejects_evidence_changed_after_copy(tmp_path, monkeypatch):
    """A frozen old source must never be authenticated by the new source hash."""
    import fault_recovery as recovery
    output, pack, source, _, published, options = scenario(tmp_path)
    options["validate"] = lambda root: (0, "")
    options["publish"] = lambda candidate: published.append(candidate) or 0
    original_copy, original_write = recovery.shutil.copy2, recovery.atomic_write_bytes
    changed = []

    def replace_source_after_freeze(target):
        target = Path(target)
        if (not changed and target.name == source.name
                and any(part.startswith("mechanical_") for part in target.parts)):
            assert target.read_bytes() == source.read_bytes()
            source.write_text("冻结期间外部任务提交的新原文。", encoding="utf-8")
            changed.append(True)

    def copy(source_path, target, *args, **kwargs):
        result = original_copy(source_path, target, *args, **kwargs)
        replace_source_after_freeze(target)
        return result

    def write(target, content):
        original_write(target, content)
        replace_source_after_freeze(target)

    monkeypatch.setattr(recovery.shutil, "copy2", copy)
    monkeypatch.setattr(recovery, "atomic_write_bytes", write)
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert changed and not published
    assert state(tmp_path)["mechanical"]["status"] == "inputs_changed"


@pytest.mark.parametrize("repair_task", ["repair_chapter", "audit_repair"])
def test_same_baseline_pending_candidate_uses_existing_acceptance(tmp_path, monkeypatch, repair_task):
    """An interrupted host receipt is repair input, never a completed receipt."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    output, pack = paths["delta"], paths["repair"]
    if repair_task == "audit_repair":
        from test_audit_pack import write_no_change_correction
        write_no_change_correction(tmp_path, 1, 1)
        output = pipeline.audit_paths(tmp_path, 1, 1)["correction"]
        pack = output.with_suffix(".repair.md")
    runtime.mark_generation_pending(tmp_path, output, hashlib.sha256(story.read_bytes()).hexdigest())
    assert not runtime.generation_is_current(output, story)
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: pytest.fail("完整候选只需原校验，不得整份重生成"))
    assert pipeline.run_repair_worker("", tmp_path, repair_task, pack, output, 1) == 0
    if repair_task == "repair_chapter":
        assert pipeline.is_chapter_complete(tmp_path, chapter)
    else:
        assert pipeline.audit_status(tmp_path, 1, 1)["status"] == "committed"
    assert state(tmp_path)["attempts"] == []
    assert state(tmp_path)["mechanical"]["status"] == "accepted"


@pytest.mark.parametrize("worker_rc", [124, 67])
def test_baseline_interrupted_complete_artifact_enters_original_acceptance(tmp_path, monkeypatch, worker_rc):
    """Timeout/truncation may leave complete bytes, but cannot issue a receipt."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    chapter, paths, story = chapter_project(tmp_path)
    monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)
    calls = []

    def worker(command, root, task, pack, output, *args, **kwargs):
        calls.append(task)
        assert task == "delta"
        value = empty_delta(1)
        value["处理备注"] = "完整候选已经落盘，进程随后中断。"
        save_json(output, value)
        assert not runtime.generation_is_current(output, story)
        return worker_rc

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
        "delta", paths["delta_task"], paths["delta"], 1) == 0
    assert calls == ["delta"]
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert state(tmp_path)["mechanical"]["status"] == "accepted"


@pytest.mark.parametrize("worker_rc", [1, 124, 67])
def test_baseline_truncated_artifact_stays_pending_until_repaired_and_reviewed(tmp_path, monkeypatch, worker_rc):
    """Provider truncation must use the established repair and independent review."""
    import controlled_runtime as runtime
    import run_pipeline as pipeline
    from fault_recovery import REVIEW_CHECKS, digest
    chapter, paths, story = chapter_project(tmp_path)
    calls = []
    partial = '{"章节": "", "新增元素": {}, "修改元素": {}'
    monkeypatch.delenv("ND_CONTROLLED_RUNTIME", raising=False)

    def worker(command, root, task, pack, output, *args, **kwargs):
        calls.append(task)
        if task == "delta":
            output.write_text(partial, encoding="utf-8")
            return worker_rc
        assert not runtime.generation_is_current(paths["delta"], story)
        assert paths["delta"].read_text(encoding="utf-8") == partial
        if task == "recovery_patch":
            save_json(output, {"base_sha256": digest(root / "reference.json"),
                "changes": [{"path": "/章节", "value": "第001章"}], "moves": []})
        elif task == "recovery_review":
            save_json(output, {"passed": True, "candidate_sha256": digest(root / paths["delta"].relative_to(tmp_path)),
                "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "离线核验完整章节与原始候选。"})
        else:
            pytest.fail(task)
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_generation_worker("python agent_worker.py --provider codebuddy", tmp_path,
        "delta", paths["delta_task"], paths["delta"], 1) == 0
    assert calls == ["delta", "recovery_patch", "recovery_review"]
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert state(tmp_path)["attempts"][-1]["status"] == "accepted"


@pytest.mark.parametrize("worker_rc", [124, 67, 130])
def test_interrupted_generation_never_accepts_unchanged_old_artifact(tmp_path, monkeypatch, worker_rc):
    """A failed dispatch cannot turn yesterday's valid artifact into today's result."""
    import run_pipeline as pipeline
    _, paths, _ = chapter_project(tmp_path)
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: worker_rc)
    monkeypatch.setattr(pipeline, "run_repair_worker", lambda *a, **kw: pytest.fail("不得验收未更新的旧产物"))
    assert pipeline.run_generation_worker("local-fake", tmp_path, "delta", paths["delta_task"], paths["delta"], 1) == worker_rc


def test_cancelled_generation_keeps_new_candidate_without_repair(tmp_path, monkeypatch):
    """Cancellation leaves evidence available and never starts another model task."""
    import run_pipeline as pipeline
    _, paths, _ = chapter_project(tmp_path)

    def worker(*args, **kwargs):
        paths["delta"].write_text('{"章节":', encoding="utf-8")
        return 130

    monkeypatch.setattr(pipeline, "run_worker", worker)
    monkeypatch.setattr(pipeline, "run_repair_worker", lambda *a, **kw: pytest.fail("取消后不得继续修复模型"))
    assert pipeline.run_generation_worker("local-fake", tmp_path, "delta", paths["delta_task"], paths["delta"], 1) == 130


@pytest.mark.parametrize("receipt_change", [
    {"base_revision": "0" * 64},
    {"pending": "true"},
    {"pending": False, "artifact_sha256": "0" * 64},
])
def test_pending_recovery_does_not_relax_conflict_fences(tmp_path, monkeypatch, receipt_change):
    """Only the host's exact pending marker on the current story may recover."""
    import run_pipeline as pipeline
    _, paths, story = chapter_project(tmp_path)
    original = paths["delta"].read_bytes()
    story_original = story.read_bytes()
    save_json(paths["delta"].with_suffix(".generation.json"), {
        "base_revision": hashlib.sha256(story_original).hexdigest(),
        "pending": True, "artifact_sha256": "", **receipt_change})
    monkeypatch.setattr(pipeline, "run_worker", lambda *a, **kw: pytest.fail("无模型命令时保留冲突候选"))
    assert pipeline.run_repair_worker("", tmp_path, "repair_chapter", paths["repair"], paths["delta"], 1) == 65
    assert paths["delta"].read_bytes() == original
    assert story.read_bytes() == story_original
    assert not (tmp_path / "质量治理/自动恢复").exists()
