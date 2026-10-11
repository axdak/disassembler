"""Field-level repair cannot overwrite correct prose or stale candidates."""
import hashlib
import json

import pytest

from local_json_patch import apply_scoped_patch, delta_repair_scope


def example(tmp_path):
    reference = tmp_path / "result.json"
    value = {"章节": "0008", "新增元素": {}, "修改元素": {"角色集": [
        {"名称": "萧炎", "详情": {"当前修为": "原文完整细节，不能重写或删减。"}}]}}
    reference.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    scope = delta_repair_scope(reference, "缺少提取理由", {})
    patch = {"base_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
             "changes": [{"path": next(iter(scope)), "value": "本章原文明确写出修为变化。"}]}
    return reference, tmp_path / "candidate.json", scope, patch, value


def test_patch_preserves_unrelated_values(tmp_path):
    reference, candidate, scope, patch, original = example(tmp_path)
    apply_scoped_patch(reference, candidate, patch, scope)
    result = json.loads(candidate.read_text(encoding="utf-8"))
    result["修改元素"]["角色集"][0]["详情"].pop("提取理由")
    assert result == original


def test_identity_scope_requires_exact_diagnostic_and_formal_collection(tmp_path):
    reference, _, _, _, value = example(tmp_path)
    story = {"角色集": [{"名称": "正式角色"}, {"名称": ["错误类型"]}]}
    assert not any(key.endswith("/名称") for key in delta_repair_scope(reference, "[萧炎]信息未知", story))
    scope = delta_repair_scope(reference, "修改目标[萧炎]不存在", story)
    assert scope["/修改元素/角色集/0/名称"]["allowed_values"] == ["正式角色"]
    value["修改元素"]["角色集"][0]["名称"] = ["错误类型"]
    value["修改元素"]["未知集合"] = [{"名称": "伪元素", "详情": {}}]
    reference.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    scope = delta_repair_scope(reference, "未知名称", story)
    assert not any(key.endswith("/名称") or "未知集合" in key for key in scope)


def test_recovery_scheduler_uses_patch_then_full_validation_and_review(tmp_path):
    from fault_recovery import recover_artifact, digest, REVIEW_CHECKS
    from merge_delta import save_json
    reference, _, scope, patch, original = example(tmp_path)
    pack = tmp_path / "repair.md"
    pack.write_text("保持完整内容，补齐有原文依据的理由", encoding="utf-8")
    calls = []

    def validate(root):
        data = json.loads((root / reference.name).read_text(encoding="utf-8"))
        detail = data["修改元素"]["角色集"][0]["详情"]
        assert detail["当前修为"] == original["修改元素"]["角色集"][0]["详情"]["当前修为"]
        return (0, "") if detail.get("提取理由") else (1, "缺少提取理由")

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        if kind == "recovery_patch":
            patch["base_sha256"] = digest(root / "reference.json")
            save_json(expected, patch)
        else:
            assert kind == "recovery_review"
            save_json(expected, {"passed": True, "candidate_sha256": digest(root / reference.name),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [],
                                 "reason": "离线模拟原文复核"})
        return 0

    def publish(candidate):
        reference.write_bytes(candidate.read_bytes())
        return validate(tmp_path)[0]

    assert recover_artifact(tmp_path, "repair_chapter", pack, reference, command="fake",
                            worker=worker, validate=validate, publish=publish) == 0
    assert calls == ["recovery_patch", "recovery_review"]


@pytest.mark.parametrize("failure", ["stale", "extra", "duplicate", "empty", "missing"])
def test_patch_rejects_unsafe_mutations_atomically(tmp_path, failure):
    reference, candidate, scope, patch, _ = example(tmp_path)
    candidate.write_text("untouched", encoding="utf-8")
    if failure == "stale":
        patch["base_sha256"] = "0" * 64
    elif failure == "extra":
        patch["changes"].append({"path": "/质量说明", "value": "不相关修改"})
    elif failure == "duplicate":
        patch["changes"] *= 2
    elif failure == "empty":
        patch["changes"][0]["value"] = " "
    else:
        patch["changes"] = []
    with pytest.raises(ValueError):
        apply_scoped_patch(reference, candidate, patch, scope)
    assert candidate.read_text(encoding="utf-8") == "untouched"
