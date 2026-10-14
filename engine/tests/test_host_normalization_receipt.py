"""Host-owned normalization must not turn a content error into paid regeneration."""
import hashlib
import json

import pytest
import controlled_runtime as runtime
import run_pipeline as pipeline
from test_architecture_recovery import chapter_project
from test_audit_pack import empty_delta, write_no_change_correction


@pytest.mark.parametrize("kind", ["chapter", "governance"])
def test_failed_validation_keeps_authenticated_normalized_artifact(tmp_path, kind):
    _, paths, story = chapter_project(tmp_path)
    if kind == "chapter":
        output = paths["delta"]
        payload = empty_delta(1)
    else:
        write_no_change_correction(tmp_path, 1, 1)
        output = tmp_path / "质量治理/周期审计/correction_001-001.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
    payload["修改元素"] = {"角色集": [{"名称": "未知目标", "生日": "0001-01-01T00:00:00",
                                  "详情": {"提取理由": "原文变化，不得丢弃"}}]}
    runtime.publish_generated_artifact(tmp_path, output, json.dumps(payload, ensure_ascii=False).encode(),
                                      hashlib.sha256(story.read_bytes()).hexdigest())
    before_story = story.read_bytes()
    for _ in range(2):
        rc = pipeline.commit_chapter(tmp_path, 1) if kind == "chapter" else pipeline.commit_governance(tmp_path, str(output))
        assert rc != 0
        assert story.read_bytes() == before_story
        assert runtime.generation_is_current(output, story), "host normalization lost its generation receipt"
    output.write_bytes(output.read_bytes().replace("未知目标".encode(), "外部改写".encode()))
    assert not runtime.generation_is_current(output, story)


@pytest.mark.parametrize("crash_at", ["before_output", "after_output"])
def test_host_transformation_crash_keeps_either_exact_version_recoverable(tmp_path, monkeypatch, crash_at):
    _, paths, story = chapter_project(tmp_path)
    output = paths["delta"]
    runtime.publish_generated_artifact(tmp_path, output, b'{"old":1}', hashlib.sha256(story.read_bytes()).hexdigest())
    real_write = runtime.atomic_write_bytes

    def crash(path, data):
        if path == output:
            if crash_at == "after_output":
                real_write(path, data)
            raise RuntimeError("normalization interrupted")
        real_write(path, data)

    monkeypatch.setattr(runtime, "atomic_write_bytes", crash)
    def transform(candidate):
        candidate.write_bytes(b'{"old":1,"normalized":true}')
        return 0
    with pytest.raises(RuntimeError, match="interrupted"):
        runtime.transform_generated_artifact(tmp_path, output, transform)
    assert runtime.generation_is_current(output, story)
    output.write_bytes(b'{"old":2}')
    assert not runtime.generation_is_current(output, story)


def test_failed_transform_does_not_modify_or_authorize_output(tmp_path):
    _, paths, story = chapter_project(tmp_path)
    output = paths["delta"]
    original = output.read_bytes()
    runtime.publish_generated_artifact(tmp_path, output, original, hashlib.sha256(story.read_bytes()).hexdigest())
    receipt = output.with_suffix(".generation.json").read_bytes()
    def failed(candidate):
        candidate.write_bytes(b'{"partial":true}')
        return 7
    assert runtime.transform_generated_artifact(tmp_path, output, failed) == 7
    assert output.read_bytes() == original
    assert output.with_suffix(".generation.json").read_bytes() == receipt


def test_legacy_formatting_requires_hash_authenticated_original(tmp_path):
    _, _, story = chapter_project(tmp_path)
    output = tmp_path / "质量治理/周期审计/correction_001-001.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"value":1}'
    runtime.publish_generated_artifact(tmp_path, output, original, hashlib.sha256(story.read_bytes()).hexdigest())
    output.write_bytes(b'{\n "value": 1\n}')
    assert not runtime.generation_is_current(output, story)
    backup = output.parent / "original_input_20261014_test.json"
    backup.write_bytes(original)
    assert runtime.generation_is_current(output, story)
    output.write_bytes(b'{"value":true}')
    assert not runtime.generation_is_current(output, story), "JSON bool and number must not be treated as equal"


def test_explicit_force_revalidates_signed_chapter_after_story_advanced(tmp_path):
    _, paths, story = chapter_project(tmp_path)
    runtime.publish_generated_artifact(tmp_path, paths["delta"], paths["delta"].read_bytes(),
                                      hashlib.sha256(story.read_bytes()).hexdigest())
    advanced = json.loads(story.read_text(encoding="utf-8"))
    advanced["介绍"]["标题"] = "新版标题"
    story.write_text(json.dumps(advanced, ensure_ascii=False, indent=2), encoding="utf-8")
    assert pipeline.commit_chapter(tmp_path, 1) != 0
    assert pipeline.commit_chapter(tmp_path, 1, force=True) == 0


def test_stale_malformed_governance_returns_recoverable_conflict(tmp_path):
    _, _, story = chapter_project(tmp_path)
    output = tmp_path / "质量治理/周期审计/correction_001-001.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = b'```json\n{"pending":'
    runtime.publish_generated_artifact(tmp_path, output, raw, "0" * 64)
    before = story.read_bytes()
    assert pipeline.commit_governance(tmp_path, str(output)) != 0
    assert output.read_bytes() == raw and story.read_bytes() == before
    assert "版本" in output.with_suffix(".repair.md").read_text(encoding="utf-8")
