"""Final governance: lossless coverage, real strict validation and durable restart."""
import json
import re
from pathlib import Path

from bounded_final import run_bounded_final, split_text
from context_budget import estimate_tokens
from merge_delta import save_json
from story_schema_rules import COLLECTION_KEYS
from repair_budget import REPAIR_EXHAUSTED


def project_files(tmp_path, monkeypatch, valid=True):
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "10000")
    monkeypatch.setenv("ND_CONTEXT_OUTPUT_TOKENS", "2000")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "2000")
    story = {key: [] for key in COLLECTION_KEYS}
    story["介绍"] = {"标题": "测试书" if valid else "", "描述": "完整的故事描述。"}
    draft = tmp_path / "故事结构_草稿.json"
    save_json(draft, story)
    pack = tmp_path / "final.md"
    pack.write_text("完整终稿治理要求", encoding="utf-8")
    source = tmp_path / "历史原文.md"
    source.write_text("全部历史证据。" * 600 + "结尾关键证据", encoding="utf-8")
    save_json(pack.with_suffix(".md.sources.json"), [str(source)])
    return pack, draft, source


def unchanged_worker(calls):
    def worker(command, project, kind, task, output, **kwargs):
        calls.append(task.read_text(encoding="utf-8"))
        if kind == "bounded_fragment":
            fingerprint = re.search(r"复核材料指纹：([0-9a-f]{64})", calls[-1])
            assert fingerprint
            save_json(output, {"overview": "离线复核完整证据及候选，无跨片遗漏",
                               "content": {"checked": fingerprint[1], "unresolved": [], "addition": None}})
            return 0
        assert kind == "audit_correction"
        assert kwargs["retries"] == 0
        save_json(output, {"章节范围": "全书", "新增元素": {}, "修改元素": {},
                           "治理类型": "no_change", "质量说明": "离线模拟：已核查本片段及跨区间关联，无需修改。",
                           "证据范围": ["第001章"]})
        return 0
    return worker


def test_lossless_split_including_json_and_unicode_boundaries():
    text = '{"详情":"' + "重要历史😀\n" * 100 + '"}'
    parts = split_text(text, 99)
    assert "".join(parts) == text
    assert all(estimate_tokens(part) <= 99 for part in parts)


def test_final_cross_review_failure_keeps_draft_and_durable_budget(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    before = draft.read_bytes()
    worker = unchanged_worker([])
    rejected = []

    def reject(command, root, kind, task, output, **kwargs):
        rc = worker(command, root, kind, task, output, **kwargs)
        if kind == 'bounded_fragment':
            verdict = json.loads(output.read_text(encoding='utf-8'))
            verdict['content']['unresolved'] = ['跨片身份与原文不一致，尚未解决']
            save_json(output, verdict)
            rejected.append(1)
        return rc

    assert run_bounded_final('fake', tmp_path, pack, draft, reject) == REPAIR_EXHAUSTED
    assert run_bounded_final('fake', tmp_path, pack, draft, reject) == REPAIR_EXHAUSTED
    assert len(rejected) == 3
    assert draft.read_bytes() == before


def test_final_cross_review_addition_reenters_strict_governance(tmp_path, monkeypatch):
    from fault_recovery import digest, REVIEW_CHECKS
    pack, draft, source = project_files(tmp_path, monkeypatch)
    source.write_text('完整的故事描述。补充跨片关联证据。', encoding='utf-8')
    normal = unchanged_worker([])
    repaired, proposed, accepted = [], [], []
    patch = {'章节范围': '全书', '新增元素': {}, '修改元素': {},
             '介绍': {'描述': '完整的故事描述。补充跨片关联证据。'}}

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        if kind == 'audit_correction' and '本次材料：独立跨片复核' in prompt:
            save_json(output, patch)
            repaired.append(1)
            return 0
        if kind == 'recovery_review':
            save_json(output, {'passed': True, 'candidate_sha256': digest(root / '故事结构_增量.json'),
                               'checks': dict.fromkeys(REVIEW_CHECKS, True), 'issues': [],
                               'reason': '离线检查：原文支持补充且原描述完整保留'})
            accepted.append(1)
            return 0
        rc = normal(command, root, kind, task, output, **kwargs)
        if kind == 'bounded_fragment' and not repaired and not proposed:
            value = json.loads(output.read_text(encoding='utf-8'))
            value['content']['addition'] = patch
            save_json(output, value)
            proposed.append(1)
        return rc

    assert run_bounded_final('fake', tmp_path, pack, draft, worker) == 0
    assert proposed and repaired and accepted
    description = json.loads(draft.read_text(encoding='utf-8'))['介绍']['描述']
    assert patch['介绍']['描述'] in description  # Original merge appends evidence conservatively.
    assert list(tmp_path.rglob('review_coverage.json'))


def test_all_evidence_covered_and_success_reused(tmp_path, monkeypatch):
    pack, draft, source = project_files(tmp_path, monkeypatch)
    original = source.read_bytes()
    calls = []
    assert run_bounded_final("fake", tmp_path, pack, draft, unchanged_worker(calls)) == 0
    assert any("结尾关键证据" in call for call in calls)
    count = len(calls)
    assert count > 2
    assert run_bounded_final("fake", tmp_path, pack, draft, unchanged_worker(calls)) == 0
    assert len(calls) == count
    assert source.read_bytes() == original
    coverage = json.loads(next((tmp_path / "质量治理").rglob("coverage.json")).read_text(encoding="utf-8"))
    material = [item for item in coverage if item["source"].endswith(".md")]
    assert material[0]["start"] == 0
    assert material[-1]["end"] == len(source.read_text(encoding="utf-8"))
    assert all(a["end"] == b["start"] for a, b in zip(material, material[1:]))


def test_cancel_resumes_only_unfinished_unit(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    calls = []
    original = unchanged_worker(calls)

    def cancel_second(*args, **kwargs):
        return 130 if len(calls) == 1 else original(*args, **kwargs)

    assert run_bounded_final("fake", tmp_path, pack, draft, cancel_second) == 130
    completed = calls[0]
    assert run_bounded_final("fake", tmp_path, pack, draft, original) == 0
    assert calls.count(completed) == 1


def test_strict_failure_does_not_publish_or_reset_attempts(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch, valid=False)
    before = draft.read_bytes()
    calls = []
    worker = unchanged_worker(calls)
    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == REPAIR_EXHAUSTED
    count = len(calls)
    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == REPAIR_EXHAUSTED
    assert len(calls) == count
    assert draft.read_bytes() == before


def test_declared_capacity_overflow_routes_final_to_bounded_runner(tmp_path, monkeypatch):
    import bounded_final
    import run_pipeline
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    monkeypatch.setattr(run_pipeline, "inspect_pack", lambda *a: {"passed": False})
    calls = []
    monkeypatch.setattr(bounded_final, "run_bounded_final", lambda *a, **k: calls.append(a) or 0)
    assert run_pipeline.run_generation_worker("fake", tmp_path, "final_draft", pack, draft) == 0
    assert len(calls) == 1


def test_modified_evidence_is_restored_and_candidate_not_published(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    before = draft.read_bytes()
    calls = []
    worker = unchanged_worker(calls)

    def malicious(command, root, kind, task, output, **kwargs):
        rc = worker(command, root, kind, task, output, **kwargs)
        (root / "故事结构_增量.json").write_text("{}", encoding="utf-8")
        return rc

    assert run_bounded_final("fake", tmp_path, pack, draft, malicious) == REPAIR_EXHAUSTED
    assert draft.read_bytes() == before
    assert len(calls) == 3


def test_source_change_fences_publication(tmp_path, monkeypatch):
    pack, draft, source = project_files(tmp_path, monkeypatch)
    before = draft.read_bytes()
    worker = unchanged_worker([])

    def concurrent_edit(*args, **kwargs):
        # Simulate an independent user edit, outside the isolated model workspace.
        source.write_text("用户更新后的原文", encoding="utf-8")
        return worker(*args, **kwargs)

    assert run_bounded_final("fake", tmp_path, pack, draft, concurrent_edit) == REPAIR_EXHAUSTED
    assert draft.read_bytes() == before
    assert source.read_text(encoding="utf-8") == "用户更新后的原文"


def test_real_patch_requires_independent_review_before_publication(tmp_path, monkeypatch):
    from fault_recovery import digest, REVIEW_CHECKS
    pack, draft, source = project_files(tmp_path, monkeypatch, valid=False)
    source.write_text("原文书名：测试书。", encoding="utf-8")
    before = draft.read_bytes()
    reviews = []
    unchanged = unchanged_worker([])

    def worker(command, root, kind, task, output, **kwargs):
        assert draft.read_bytes() == before  # Formal draft stays private until strict acceptance.
        if kind == "recovery_review":
            reviews.append(kind)
            save_json(output, {"passed": True, "candidate_sha256": digest(root / "故事结构_增量.json"),
                               "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [],
                               "reason": "离线模拟：标题可由原文确认"})
        elif kind == "bounded_fragment":
            return unchanged(command, root, kind, task, output, **kwargs)
        elif not json.loads((root / "故事结构_增量.json").read_text(encoding="utf-8"))["介绍"]["标题"]:
            save_json(output, {"章节范围": "全书", "介绍": {"标题": "测试书"},
                               "新增元素": {}, "修改元素": {}, "治理类型": "governance"})
        else:
            return unchanged(command, root, kind, task, output, **kwargs)
        return 0

    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == 0
    assert reviews == ["recovery_review"]
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"]["标题"] == "测试书"


def test_empty_or_unknown_intro_cannot_bypass_no_change_contract():
    from validate_delta import validate_no_change_metadata
    for intro in ({}, {"标题": " "}, {"unknown": "pretend work"}):
        assert validate_no_change_metadata({"介绍": intro, "新增元素": {}, "修改元素": {}})


def test_merge_preservation_accepts_archives_moved_to_target_but_rejects_loss(tmp_path):
    from bounded_final import governance_preservation_errors
    from apply_governance_ops import apply_ops
    before, after, patch, baseline = [tmp_path / name for name in ("before.json", "after.json", "patch.json", "baseline.json")]
    story = {"角色集": [{"名称": "主角色", "详情": {"经历": "主角色的完整经历。"}},
                         {"名称": "旧别名", "详情": {"补充经历": "别名下的重要历史资料。"}}]}
    proposal = {"治理操作": {"合并元素": [{"类型": "角色", "主": "主角色", "并入": ["旧别名"]}]}}
    save_json(before, story)
    save_json(patch, proposal)
    merged, _, warnings = apply_ops(story, proposal)
    assert not warnings
    save_json(after, merged)
    assert governance_preservation_errors(before, after, patch, baseline) == []
    del merged["角色集"][0]["详情"]["补充经历"]
    save_json(after, merged)
    assert governance_preservation_errors(before, after, patch, baseline)


def test_program_normalization_precedes_model_retry(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    calls = []
    original = unchanged_worker(calls)

    def legacy_output(*args, **kwargs):
        rc = original(*args, **kwargs)
        if args[2] == "bounded_fragment":
            return rc
        output = args[4]
        patch = json.loads(output.read_text(encoding="utf-8"))
        patch["新增元素"] = "{}"  # Losslessly repairable legacy container.
        save_json(output, patch)
        return rc

    assert run_bounded_final("fake", tmp_path, pack, draft, legacy_output) == 0
    assert list((tmp_path / "质量治理").rglob("*.normalization.json"))
    state = json.loads(next((tmp_path / "质量治理").rglob("state.json")).read_text(encoding="utf-8"))
    assert all(attempts == 1 for attempts in state["attempts"].values())


def test_transport_exception_restores_readonly_material(tmp_path):
    from bounded_final import guarded_worker
    source = tmp_path / "source.md"
    source.write_text("完整证据", encoding="utf-8")

    def broken(*args, **kwargs):
        source.write_text("错误改动", encoding="utf-8")
        raise TimeoutError("offline injected timeout")

    rc, changed = guarded_worker(broken, "fake", tmp_path, "test", source,
                                 tmp_path / "output.json", [source], {})
    assert rc == 124 and changed
    assert source.read_text(encoding="utf-8") == "完整证据"


def test_changed_contract_or_model_invalidates_success_receipt(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    calls = []
    worker = unchanged_worker(calls)
    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == 0
    count = len(calls)
    pack.write_text("新增了明确的完整治理要求", encoding="utf-8")
    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == 0
    assert len(calls) > count
    count = len(calls)
    monkeypatch.setenv("ND_AGENT_MODEL", "offline-new-model")
    assert run_bounded_final("fake", tmp_path, pack, draft, worker) == 0
    assert len(calls) > count


def test_identical_evidence_has_one_canonical_read_with_all_roles(tmp_path, monkeypatch):
    pack, draft, source = project_files(tmp_path, monkeypatch)
    duplicate = tmp_path / "重复引用.md"
    duplicate.write_bytes(source.read_bytes())
    save_json(pack.with_suffix(".md.sources.json"), [str(source), str(duplicate)])
    assert run_bounded_final("fake", tmp_path, pack, draft, unchanged_worker([])) == 0
    catalog = json.loads(next((tmp_path / "质量治理").rglob("catalog.json")).read_text(encoding="utf-8"))
    assert len(catalog) == 2  # draft plus one full evidence file, not three copies
    assert set(catalog[1]["roles"]) == {source.name, duplicate.name}
