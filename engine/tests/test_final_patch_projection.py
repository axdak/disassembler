"""Pure-superset recovery keeps the original patch strict and fully reviewable."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import local_json_patch as patches
from fault_recovery import digest
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_final_scope_context import final_case, run


RAW = "candidate_patch.json"
PROJECTED = "candidate_patch.projected.json"
AUDIT = "candidate_patch.projection.json"


def batch(tmp_path):
    """Thirty-two real final field rules plus one unrelated existing profile."""
    from story_schema_rules import DEFAULTS, make_empty_structure
    story = make_empty_structure()
    story["介绍"] = {"标题": "原题", "描述": "原故事完整背景。"}
    for i in range(32):
        item = copy.deepcopy(DEFAULTS["角色集"])
        item.update(名称=f"人物{i}", 介绍=f"人物{i}的旧介绍。")
        story["角色集"].append(item)
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="救援", 标签集=["旧标签"], 介绍="救援尚未完成。")
    story["事件集"].append(event)
    reference = tmp_path / "reference.json"; save_json(reference, story)
    values = {f"/角色集/{i}/介绍": f"人物{i}见证救援，未决结果保留。" for i in range(31)}
    values["/事件集/0/标签集"] = ["救援+转折+紧张+未决危机"]
    scope = patches.final_repair_scope(reference, "\n".join(f"[质量字段:{p}]" for p in values))
    assert len(scope) == 32
    patch = {"base_sha256": digest(reference), "changes": [
        {"path": p, "value": v} for p, v in values.items()] + [
        {"path": "/角色集/31/介绍", "value": "禁止应用的额外介绍"}], "moves": []}
    return reference, tmp_path / "candidate.json", scope, patch, story


def test_complete_32_plus_one_is_projected_without_relaxing_original_applier(tmp_path):
    reference, candidate, scope, patch, story = batch(tmp_path)
    candidate.write_bytes(b"untouched")
    with pytest.raises(ValueError, match="必须且只能覆盖"):
        patches.apply_scoped_patch(reference, candidate, patch, scope)
    assert candidate.read_bytes() == b"untouched"
    original = copy.deepcopy(patch)
    projector = getattr(patches, "project_complete_final_patch", None)
    assert callable(projector), "The host's narrow final projection is missing"
    projected = projector(reference, patch, scope)
    assert projected == {"base_sha256": patch["base_sha256"], "changes": patch["changes"][:32], "moves": []}
    assert patch == original
    patches.apply_scoped_patch(reference, candidate, projected, scope)
    actual = json.loads(candidate.read_bytes())
    assert actual["角色集"][31] == story["角色集"][31]
    for i in range(31):
        assert actual["角色集"][i]["介绍"] == f"人物{i}见证救援，未决结果保留。"
        assert f"人物{i}的旧介绍。" in actual["角色集"][i]["详情"]["字段修复原文"]
    assert actual["事件集"][0]["标签集"] == ["救援+转折+紧张+未决危机"]
    assert "旧标签" in actual["事件集"][0]["详情"]["字段修复原文"]


@pytest.mark.parametrize("failure", ["missing", "duplicate_allowed", "duplicate_extra", "base", "moves",
                                     "shape", "identity", "not_final", "promotion", "empty_scope", "no_extra"])
def test_projection_declines_every_non_pure_superset(tmp_path, failure):
    reference, _, scope, patch, _ = batch(tmp_path)
    if failure == "missing": patch["changes"].pop(0)
    elif failure == "duplicate_allowed": patch["changes"].append(copy.deepcopy(patch["changes"][0]))
    elif failure == "duplicate_extra": patch["changes"].append(copy.deepcopy(patch["changes"][-1]))
    elif failure == "base": patch["base_sha256"] = "0" * 64
    elif failure == "moves": patch["moves"] = [{"path": "/角色集/31", "target_collection": "事件集", "evidence": "原文"}]
    elif failure == "shape": patch["changes"][-1]["other"] = True
    elif failure == "identity": next(iter(scope.values()))["kind"] = "identity"
    elif failure == "not_final": next(iter(scope.values()))["final"] = False
    elif failure == "promotion": next(iter(scope.values()))["promotion"] = {}
    elif failure == "empty_scope": scope = {}
    else: patch["changes"].pop()
    projector = getattr(patches, "project_complete_final_patch", None)
    assert callable(projector), "The host's narrow final projection is missing"
    assert projector(reference, patch, scope) is None


def superset_case(tmp_path):
    """Use the original owner and validators; replace only its external worker."""
    case = final_case(tmp_path)
    worker = case.options["worker"]
    case.raw = None; case.workspace = None

    def extra(command, root, kind, task, expected, *args, **kwargs):
        rc = worker(command, root, kind, task, expected, *args, **kwargs)
        case.workspace = root
        if kind == "recovery_patch":
            patch = json.loads(expected.read_bytes())
            patch["changes"].append({"path": "/角色集/2/介绍", "value": "不应应用的越界介绍"})
            # Preserve a distinctive original byte representation, not reserialization.
            expected.write_bytes(("  " + json.dumps(patch, ensure_ascii=False) + "\n").encode())
            case.raw = expected.read_bytes()
        return rc

    case.options["worker"] = extra
    return case


def test_owner_projects_only_allowed_values_then_validates_and_reviews_original_sources(tmp_path):
    case = superset_case(tmp_path)
    worker = case.options["worker"]

    def inspect(command, root, kind, task, expected, *args, **kwargs):
        if kind == "recovery_review":
            assert (root / RAW).read_bytes() == case.raw
            projected = json.loads((root / PROJECTED).read_bytes())
            assert {x["path"] for x in projected["changes"]} == set(case.values)
            audit = json.loads((root / AUDIT).read_bytes())
            assert audit["raw_patch_sha256"] == hashlib.sha256(case.raw).hexdigest()
            assert audit["projected_patch_sha256"] == digest(root / PROJECTED)
            assert audit["discarded_changes"] == [{"path": "/角色集/2/介绍", "value": "不应应用的越界介绍"}]
            assert "必须且只能覆盖" in audit["original_rejection"]
            catalogue = json.loads((root / "materials.json").read_bytes())
            assert {RAW, PROJECTED, AUDIT} <= {x["path"] for x in catalogue["materials"]}
            inputs = json.loads(task.with_suffix(".context-inputs.json").read_bytes())
            assert {RAW, PROJECTED, AUDIT} <= {Path(p).name for p in inputs}
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options["worker"] = inspect
    assert run(case) == 0
    assert case.calls == ["recovery_patch", "recovery_review"] and len(case.published) == 1
    assert (case.workspace / RAW).read_bytes() == case.raw
    assert json.loads(case.output.read_bytes())["角色集"][2] == case.story["角色集"][2]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "moves", "base", "type", "empty", "strict", "review"])
def test_projection_cannot_bypass_patch_rules_strict_or_independent_review(tmp_path, failure):
    case = superset_case(tmp_path)
    worker, validate = case.options["worker"], case.options["validate"]

    def invalid(command, root, kind, task, expected, *args, **kwargs):
        rc = worker(command, root, kind, task, expected, *args, **kwargs)
        if kind == "recovery_patch":
            patch = json.loads(expected.read_bytes())
            if failure == "missing": patch["changes"].pop(0)
            elif failure == "duplicate": patch["changes"].append(copy.deepcopy(patch["changes"][0]))
            elif failure == "moves": patch["moves"] = [{"path": "/角色集/2", "target_collection": "事件集", "evidence": "原文"}]
            elif failure == "base": patch["base_sha256"] = "0" * 64
            elif failure == "type": patch["changes"][0]["value"] = ["不能把介绍改成数组"]
            elif failure == "empty": patch["changes"][0]["value"] = " "
            save_json(expected, patch); case.raw = expected.read_bytes()
        elif failure == "review":
            verdict = json.loads(expected.read_bytes()); verdict.update(passed=False, issues=["原文不支持"]); save_json(expected, verdict)
        return rc

    def strict(root):
        result = validate(root)
        return (1, "原strict拒绝") if failure == "strict" and result == (0, "") else result

    case.options.update(worker=invalid, validate=strict)
    assert run(case) == REPAIR_EXHAUSTED
    assert case.output.read_bytes() == case.original and not case.published
    assert case.calls == (["recovery_patch", "recovery_review"] if failure == "review" else ["recovery_patch"])


@pytest.mark.parametrize("stage", ["generated", "validated"])
def test_projected_candidate_resumes_original_checkpoint_without_regeneration(tmp_path, stage):
    case = superset_case(tmp_path)
    worker, validate = case.options["worker"], case.options["validate"]
    stopped = False

    def validation(root):
        nonlocal stopped
        result = validate(root)
        if stage == "generated" and result == (0, "") and not stopped:
            stopped = True; raise RuntimeError("投影生成后中断")
        return result

    def transient(command, root, kind, task, expected, *args, **kwargs):
        nonlocal stopped
        if stage == "validated" and kind == "recovery_review" and not stopped:
            stopped = True; case.calls.append(kind); return 130
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options.update(worker=transient, validate=validation)
    if stage == "generated":
        with pytest.raises(RuntimeError, match="投影生成后"): run(case)
    else: assert run(case) == 130
    assert not case.published and case.output.read_bytes() == case.original
    assert run(case) == 0
    assert case.calls.count("recovery_patch") == 1 and len(case.published) == 1
    assert (case.workspace / RAW).read_bytes() == case.raw


@pytest.mark.parametrize("name", [RAW, PROJECTED, AUDIT])
def test_projection_evidence_tamper_invalidates_saved_checkpoint(tmp_path, name):
    case = superset_case(tmp_path); worker = case.options["worker"]

    def interrupt(command, root, kind, task, expected, *args, **kwargs):
        if kind == "recovery_review":
            case.workspace = root; case.calls.append(kind); return 130
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options["worker"] = interrupt
    assert run(case) == 130
    ledger = next((tmp_path / "质量治理/自动恢复").glob("*/state.json"))
    checkpoint = json.loads(ledger.read_bytes())["checkpoint"]
    assert {RAW, PROJECTED, AUDIT} <= {Path(p).name for p in checkpoint["files"]}
    path = case.workspace / name; path.write_bytes(path.read_bytes() + b" ")
    case.options["worker"] = worker
    assert run(case) == REPAIR_EXHAUSTED
    assert case.calls == ["recovery_patch", "recovery_review"] and not case.published
    assert case.output.read_bytes() == case.original


@pytest.mark.parametrize("name", [RAW, PROJECTED, AUDIT])
def test_review_cannot_modify_projection_evidence_before_approving(tmp_path, name):
    case = superset_case(tmp_path); worker = case.options["worker"]
    original_evidence = None

    def tamper(command, root, kind, task, expected, *args, **kwargs):
        nonlocal original_evidence
        rc = worker(command, root, kind, task, expected, *args, **kwargs)
        if kind == "recovery_review":
            path = root / name; original_evidence = path.read_bytes()
            path.write_bytes(original_evidence + b" ")
        return rc

    case.options["worker"] = tamper
    assert run(case) == REPAIR_EXHAUSTED
    assert case.calls == ["recovery_patch", "recovery_review"] and not case.published
    assert (case.workspace / name).read_bytes() == original_evidence
    assert case.output.read_bytes() == case.original


def test_non_final_delta_keeps_rejecting_complete_supersets(tmp_path):
    from fault_recovery import recover_artifact
    from test_local_json_patch import example
    reference, _, scope, patch, original = example(tmp_path)
    before = reference.read_bytes()
    pack = tmp_path / "repair.md"; pack.write_text("补齐原文提取理由。", encoding="utf-8")
    calls, published = [], []

    def validate(root):
        data = json.loads((root / reference.name).read_bytes())
        return (0, "") if data["修改元素"]["角色集"][0]["详情"].get("提取理由") else (1, "缺少提取理由")

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind); assert kind == "recovery_patch"
        raw = copy.deepcopy(patch); raw["base_sha256"] = digest(root / "reference.json")
        raw["changes"].append({"path": "/质量说明", "value": "不应应用的范围外字段"})
        save_json(expected, raw)
        return 0

    assert recover_artifact(tmp_path, "repair_chapter", pack, reference, command="local-fake",
        worker=worker, validate=validate, publish=lambda candidate: published.append(candidate) or 0,
        strategies=1, retries=0) == REPAIR_EXHAUSTED
    assert calls == ["recovery_patch"] and not published
    assert reference.read_bytes() == before


@pytest.mark.parametrize("name", [RAW, PROJECTED, AUDIT])
def test_projection_paths_stay_fenced_when_material_text_is_deduplicated(tmp_path, monkeypatch, name):
    from recovery_materials import RecoveryMaterials
    add = RecoveryMaterials.add_source

    def identical_material(self, path, role):
        if path.name == name:
            alias = self.workspace / ("same_" + name)
            alias.write_bytes(path.read_bytes())
            add(self, alias, "另一份同文材料")
        add(self, path, role)

    monkeypatch.setattr(RecoveryMaterials, "add_source", identical_material)
    case = superset_case(tmp_path); worker = case.options["worker"]

    def interrupt(command, root, kind, task, expected, *args, **kwargs):
        if kind == "recovery_review":
            inputs = json.loads(task.with_suffix(".context-inputs.json").read_bytes())
            assert name in {Path(p).name for p in inputs}
            case.calls.append(kind)
            return 130
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options["worker"] = interrupt
    assert run(case) == 130
    ledger = next((tmp_path / "质量治理/自动恢复").glob("*/state.json"))
    checkpoint = json.loads(ledger.read_bytes())["checkpoint"]
    assert name in {Path(p).name for p in checkpoint["files"]}
    assert case.output.read_bytes() == case.original and not case.published
