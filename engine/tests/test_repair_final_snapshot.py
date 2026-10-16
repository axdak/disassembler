"""The final batch consumer must preserve its baseline across publication."""
import pytest

def test_final_batch_compares_frozen_baseline_after_publication(tmp_path, monkeypatch):
    """Writing the accepted batch must not replace its comparison preimage."""
    import local_json_patch as patcher
    import run_pipeline as pipeline
    draft = tmp_path / "故事结构_草稿.json"
    draft.write_bytes(b'{"repair":"pending","next_batch":"pending"}')
    candidate_bytes = b'{"repair":"complete","next_batch":"pending"}'
    compared_baselines = []
    compared_bytes = []

    def progress(before, after):
        # This fake exposes the helper's before/after contract independently of
        # schema rules, which have their own real-validator integration tests.
        compared_baselines.append(before)
        compared_bytes.append(before.read_bytes())
        return b'"repair":"pending"' in compared_bytes[-1] and b'"repair":"complete"' in after.read_bytes()

    def recover(project, task, pack, output, *, validate, publish, **options):
        workspace = project / "private_candidate"
        workspace.mkdir()
        (workspace / "original.json").write_bytes(output.read_bytes())
        candidate = workspace / output.name
        candidate.write_bytes(candidate_bytes)
        assert validate(workspace)[0] == 0
        return publish(candidate)

    monkeypatch.setattr(patcher, "final_repair_progress", progress, raising=False)
    monkeypatch.setattr(pipeline, "cmd_commit_final_draft", lambda root: 1)
    monkeypatch.setattr(pipeline, "recover_artifact", recover)
    assert pipeline.run_repair_worker("offline", tmp_path, "repair_final", tmp_path / "repair.md", draft) == 0
    assert draft.read_bytes() == candidate_bytes
    assert compared_bytes == [b'{"repair":"pending","next_batch":"pending"}'] * 2
    assert compared_baselines[0] != draft
    assert not compared_baselines[0].exists()


@pytest.mark.parametrize("interruption,tamper", [
    (None, False), ("before_replace", False), ("after_replace", False), ("after_replace", True)])
def test_final_field_batch_publishes_against_original_snapshot(tmp_path, monkeypatch, interruption, tamper):
    """Real strict validation may reject the next batch after this one succeeds."""
    import json
    import local_json_patch as patcher
    import run_pipeline as pipeline
    from fault_recovery import REVIEW_CHECKS, digest
    from merge_delta import save_json
    from test_final_field_repair import complete_story
    from test_fault_recovery import state
    monkeypatch.setattr(patcher, "FINAL_REPAIR_BATCH_FIELDS", 1)
    draft = tmp_path / "故事结构_草稿.json"
    story = complete_story()
    story["角色集"][0]["介绍"] = ""
    story["事件集"][0]["介绍"] = ""
    save_json(tmp_path / "故事结构_增量.json", story)
    save_json(draft, story)
    pack = tmp_path / "质量治理/最终审计/repair_final.md"
    calls = []

    def worker(command, root, task, pack, output, *args, **options):
        calls.append(task)
        if task == "recovery_patch":
            save_json(output, {"base_sha256": digest(root / "reference.json"),
                "changes": [{"path": "/角色集/0/介绍", "value": "甲是救援行动的参与者。"}], "moves": []})
        elif task == "recovery_review":
            save_json(output, {"passed": True, "candidate_sha256": digest(root / draft.name),
                "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "离线核验局部字段补充与原始故事档案。"})
        else:
            raise AssertionError(task)
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    if interruption:
        original_write = pipeline.atomic_write_bytes

        def interrupted_write(path, content):
            if path == draft:
                if interruption == "after_replace":
                    original_write(path, content)
                raise RuntimeError("host interrupted final batch publication")
            return original_write(path, content)

        monkeypatch.setattr(pipeline, "atomic_write_bytes", interrupted_write)
        with pytest.raises(RuntimeError, match="host interrupted"):
            pipeline.run_repair_worker("offline", tmp_path, "repair_final", pack, draft, repair_limit=1)
        assert state(tmp_path)["status"] == "publishing"
        assert calls == ["recovery_patch", "recovery_review"]
        assert not list(tmp_path.glob(".final_repair_baseline_*"))
        monkeypatch.setattr(pipeline, "atomic_write_bytes", original_write)
        if tamper:
            ledger = next((tmp_path / "质量治理/自动恢复").glob("*/state.json"))
            original = (ledger.parent / state(tmp_path)["candidate"]).parent / "original.json"
            damaged = json.loads(original.read_text(encoding="utf-8"))
            # Keep the same schema errors so semantic progress alone cannot
            # detect that the reviewed preimage has been substituted.
            damaged["介绍"]["描述"] = "外部改写了复核时的故事描述。"
            save_json(original, damaged)
            before_resume = draft.read_bytes()
            assert pipeline.run_repair_worker("offline", tmp_path, "repair_final", pack, draft, repair_limit=1) == 65
            assert draft.read_bytes() == before_resume
            assert state(tmp_path)["status"] == "publishing"
            assert calls == ["recovery_patch", "recovery_review"]
            return
    assert pipeline.run_repair_worker("offline", tmp_path, "repair_final", pack, draft, repair_limit=1) == 0
    current = json.loads(draft.read_text(encoding="utf-8"))
    assert current["角色集"][0]["介绍"] == "甲是救援行动的参与者。"
    assert current["事件集"][0]["介绍"] == ""
    assert pipeline.cmd_commit_final_draft(tmp_path) == 1
    assert not (tmp_path / "故事结构.json").exists()
    assert calls == ["recovery_patch", "recovery_review"]
    assert state(tmp_path)["status"] == "succeeded"


def test_mechanical_final_publication_rejects_changed_raw_preimage(tmp_path):
    """A program-only publication authenticates its original without a checkpoint."""
    from fault_recovery import recover_artifact
    from test_fault_recovery import scenario, state
    output, pack, _, calls, published, options = scenario(tmp_path)
    options["validate"] = lambda root: (0, "")

    def interrupted_publish(candidate):
        output.write_bytes(candidate.read_bytes())
        raise RuntimeError("host interrupted mechanical final publication")

    options["publish"] = interrupted_publish
    with pytest.raises(RuntimeError, match="host interrupted"):
        recover_artifact(tmp_path, "repair_final", pack, output, **options)
    assert state(tmp_path)["status"] == "publishing"
    assert "checkpoint" not in state(tmp_path)
    ledger = next((tmp_path / "质量治理/自动恢复").glob("*/state.json"))
    original = (ledger.parent / state(tmp_path)["candidate"]).parent / "original.json"
    original.write_bytes(original.read_bytes() + b"\n")
    options["publish"] = lambda candidate: published.append(candidate) or 0
    assert recover_artifact(tmp_path, "repair_final", pack, output, **options) == 65
    assert not published and not calls
    assert state(tmp_path)["status"] == "publishing"
