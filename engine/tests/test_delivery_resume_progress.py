"""Final delivery resumes durable publication without replaying accepted fields."""
import json

import pytest


def test_delivery_continues_after_native_publication_resume(tmp_path, monkeypatch):
    """A real publishing ledger must continue with the next remaining field batch."""
    import local_json_patch as patcher
    import run_pipeline as pipeline
    from fault_recovery import REVIEW_CHECKS, digest
    from merge_delta import save_json
    from story_delivery import commit_delivery
    from story_schema_rules import validate_exact_story_schema
    from test_fault_recovery import state
    from test_final_field_repair import complete_story

    monkeypatch.setattr(patcher, "FINAL_REPAIR_BATCH_FIELDS", 1)
    story = complete_story()
    story["角色集"][0]["介绍"] = ""
    story["事件集"][0]["介绍"] = ""
    draft = tmp_path / "故事结构_草稿.json"
    source = tmp_path / "故事结构_增量.json"
    save_json(source, story)
    save_json(draft, story)
    original_source = source.read_bytes()
    calls = []
    repaired_paths = []

    def worker(command, root, task, pack, output, *args, **options):
        calls.append(task)
        if task == "recovery_patch":
            reference = root / "reference.json"
            errors = validate_exact_story_schema(
                json.loads(reference.read_text(encoding="utf-8")), mode="final"
            )[0]
            scope = patcher.final_repair_scope(reference, "\n".join(errors))
            assert len(scope) == 1
            pointer = next(iter(scope))
            repaired_paths.append(pointer)
            save_json(output, {"base_sha256": digest(reference),
                "changes": [{"path": pointer, "value": "甲参与救援并救下受困的人。"}], "moves": []})
        elif task == "recovery_review":
            save_json(output, {"passed": True, "candidate_sha256": digest(root / draft.name),
                "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [],
                "reason": "离线对照原始档案确认本批字段，其余内容完整保留。"})
        else:
            raise AssertionError(task)
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    original_write = pipeline.atomic_write_bytes

    def interrupted_publication(path, content):
        original_write(path, content)
        if path == draft:
            raise RuntimeError("host stopped after replacing final draft")

    monkeypatch.setattr(pipeline, "atomic_write_bytes", interrupted_publication)
    with pytest.raises(RuntimeError, match="host stopped after replacing"):
        commit_delivery(tmp_path, "offline", repair_limit=1, repair_history=True)
    assert state(tmp_path)["status"] == "publishing"
    assert calls == ["recovery_patch", "recovery_review"]
    assert json.loads(draft.read_text(encoding="utf-8"))["角色集"][0]["介绍"]
    assert not json.loads(draft.read_text(encoding="utf-8"))["事件集"][0]["介绍"]

    monkeypatch.setattr(pipeline, "atomic_write_bytes", original_write)
    assert commit_delivery(tmp_path, "offline", repair_limit=1, repair_history=True) == 0
    assert validate_exact_story_schema(json.loads(draft.read_text(encoding="utf-8")), mode="final")[0] == []
    assert repaired_paths == ["/角色集/0/介绍", "/事件集/0/介绍"]
    assert calls == ["recovery_patch", "recovery_review"] * 2
    assert source.read_bytes() == original_source


def test_delivery_rejects_zero_progress_after_bounded_confirmation(tmp_path):
    """A successful return without field changes cannot loop or pass validation."""
    from merge_delta import save_json
    from story_delivery import commit_delivery
    from story_schema_rules import validate_exact_story_schema
    from test_final_field_repair import complete_story

    story = complete_story()
    story["角色集"][0]["介绍"] = ""
    draft = tmp_path / "故事结构_草稿.json"
    save_json(draft, story)
    original = draft.read_bytes()
    calls = []

    def no_progress(*args, **options):
        calls.append(args[2])
        return 0

    assert commit_delivery(tmp_path, "offline", repair_worker=no_progress, repair_history=True) == 1
    assert 1 <= len(calls) <= 2
    assert draft.read_bytes() == original
    assert validate_exact_story_schema(json.loads(draft.read_text(encoding="utf-8")), mode="final")[0]
    assert not (tmp_path / "故事结构.json").exists()


@pytest.mark.parametrize("failure", [65, 66, 124, 130])
def test_delivery_keeps_original_failure_code_without_retry(tmp_path, failure):
    """Failure is not an idempotent success confirmation and spends no new call."""
    from merge_delta import save_json
    from story_delivery import commit_delivery
    from test_final_field_repair import complete_story

    story = complete_story()
    story["角色集"][0]["介绍"] = ""
    draft = tmp_path / "故事结构_草稿.json"
    save_json(draft, story)
    before = draft.read_bytes()
    calls = []

    def fail(*args, **options):
        calls.append(args[2])
        return failure

    assert commit_delivery(tmp_path, "offline", repair_worker=fail, repair_history=True) == failure
    assert calls == ["repair_final"]
    assert draft.read_bytes() == before


def test_delivery_resume_does_not_refund_failed_native_batch(tmp_path, monkeypatch):
    """The same failed scope retains its native strategy and model-call budget."""
    import run_pipeline as pipeline
    from merge_delta import save_json
    from story_delivery import commit_delivery
    from test_fault_recovery import state
    from test_final_field_repair import complete_story

    story = complete_story()
    story["角色集"][0]["介绍"] = ""
    draft = tmp_path / "故事结构_草稿.json"
    save_json(tmp_path / "故事结构_增量.json", story)
    save_json(draft, story)
    calls = []

    def invalid_patch(command, root, task, pack, output, *args, **options):
        calls.append(task)
        assert task == "recovery_patch"
        save_json(output, {"base_sha256": "0" * 64,
            "changes": [{"path": "/角色集/0/介绍", "value": "未经正确版本授权的值。"}], "moves": []})
        return 0

    monkeypatch.setattr(pipeline, "run_worker", invalid_patch)
    assert commit_delivery(tmp_path, "offline", repair_limit=1, repair_history=True) == 65
    original_ledger = state(tmp_path)
    assert len(original_ledger["calls"]) == 1
    assert commit_delivery(tmp_path, "offline", repair_limit=1, repair_history=True) == 65
    resumed_ledger = state(tmp_path)
    assert resumed_ledger["generation"] == original_ledger["generation"]
    assert len(resumed_ledger["calls"]) == 1
    assert calls == ["recovery_patch"]
