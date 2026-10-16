"""Bind legacy detail-array wire encoding without editing model-supplied facts."""
import copy
import hashlib
import json

import pytest

from apply_governance_ops import apply_ops
from fault_recovery import REVIEW_CHECKS, digest, preservation_errors
from governance_contract import normalize_governance_patch
from merge_delta import save_json
from story_schema_rules import DEFAULTS, make_empty_structure


OLD = ["旧记录第一项包含尚未执行的限制条件，不能改变内容或字符。", "第二项必须保留的尾部空格  "]
NEW = ["原文明确的更正仍是尚未执行的限制条件，不能改成已经完成。", OLD[1]]
QUOTE = "甲只是离村办事，稍后才回家。"


def fixture():
    """Keep an escaped, indented old string so byte-exact preimages matter."""
    story = make_empty_structure()
    story["介绍"] = {"标题": "测试故事", "描述": "已有完整简介。"}
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="甲", 别名=["旧称"], 介绍="甲的既有完整档案。", 分组="主角")
    role["详情"] = {"首次章节": "0001", "最近章节": "0001",
                  "人物轨迹": json.dumps(OLD, ensure_ascii=True, indent=2) + "\n"}
    story["角色集"] = [role]
    op = {"类型": "角色", "名称": "甲", "字段": "详情/人物轨迹", "原值": copy.deepcopy(OLD),
          "值": copy.deepcopy(NEW), "理由": "根据已登记的完整原文章节纠正这一处历史记录，必须保留准确旧值、全部原文限制和不相关的叙述内容。",
          "证据": [{"章节": "0001", "原文": QUOTE}]}
    patch = {"章节范围": "第001章", "新增元素": {}, "修改元素": {}, "治理操作": {"设置字段": [op]}}
    return story, patch


@pytest.mark.parametrize("alias", [False, True])
def test_exact_native_arrays_restore_old_string_and_keep_all_new_text(alias):
    story, patch = fixture()
    if alias:
        patch["治理操作"]["设置字段"][0]["名称"] = "旧称"
    before_story, before_patch = copy.deepcopy(story), copy.deepcopy(patch)
    fixed, changes = normalize_governance_patch(patch, story=story)
    op = fixed["治理操作"]["设置字段"][0]
    assert op["原值"] == story["角色集"][0]["详情"]["人物轨迹"]
    assert isinstance(op["值"], str) and json.loads(op["值"]) == NEW
    assert changes and all("设置字段[0]" in message for message in changes)
    assert story == before_story and patch == before_patch
    assert normalize_governance_patch(fixed, story=story) == (fixed, [])
    applied, _, errors = apply_ops(story, fixed)
    assert not errors
    assert json.loads(applied["角色集"][0]["详情"]["人物轨迹"]) == NEW
    archives = json.loads(applied["角色集"][0]["详情"]["治理纠错记录"])
    assert json.loads(archives[-1])["原值"] == op["原值"]


@pytest.mark.parametrize("problem", ["old_text", "new_text", "old_changed", "old_reordered", "old_trimmed",
    "invalid_json", "current_native", "current_scalar", "current_mixed", "old_mixed", "new_mixed",
    "formal_detail_array", "formal_root_array", "host_archive", "unknown", "ambiguous", "duplicate",
    "alias_duplicate", "same_record_modification", "identity_operation", "malformed_alias", "missing_canonical",
    "append_detail", "supplementary_tags", "malformed_kind"])
def test_uncertain_or_nonlegacy_shapes_are_not_reencoded(problem):
    story, patch = fixture()
    detail, op = story["角色集"][0]["详情"], patch["治理操作"]["设置字段"][0]
    if problem == "old_text": op["原值"] = detail["人物轨迹"]
    elif problem == "new_text": op["值"] = json.dumps(NEW, ensure_ascii=False)
    elif problem == "old_changed": op["原值"][0] += "无据改字"
    elif problem == "old_reordered": op["原值"].reverse()
    elif problem == "old_trimmed": op["原值"][1] = op["原值"][1].strip()
    elif problem == "invalid_json": detail["人物轨迹"] += "trailing"
    elif problem == "current_native": detail["人物轨迹"] = copy.deepcopy(OLD)
    elif problem == "current_scalar": detail["人物轨迹"] = json.dumps("不是数组")
    elif problem == "current_mixed": detail["人物轨迹"] = json.dumps([OLD[0], True])
    elif problem == "old_mixed": op["原值"][1] = True
    elif problem == "new_mixed": op["值"][1] = 1
    elif problem == "formal_detail_array":
        detail["关联角色"] = json.dumps(OLD); op["字段"] = "详情/关联角色"
    elif problem == "formal_root_array":
        story["角色集"][0]["关系"] = copy.deepcopy(OLD); op["字段"] = "关系"
    elif problem == "host_archive":
        detail["治理纠错记录"] = detail["人物轨迹"]; op["字段"] = "详情/治理纠错记录"
    elif problem == "supplementary_tags":
        detail["补充标签"] = detail["人物轨迹"]; op["字段"] = "详情/补充标签"
    elif problem == "append_detail":
        patch["治理操作"]["追加详情"] = [{"类型": "角色", "名称": "甲", "字段": "人物轨迹", "值": "竞争追加"}]
    elif problem == "missing_canonical":
        story["角色集"][0].pop("名称"); op["名称"] = "旧称"
    elif problem == "malformed_alias": story["角色集"][0]["别名"] = None
    elif problem == "unknown": op["名称"] = "未登记"
    elif problem == "ambiguous":
        story["角色集"].append(copy.deepcopy(story["角色集"][0]))
    elif problem in {"duplicate", "alias_duplicate"}:
        other = copy.deepcopy(op)
        if problem == "alias_duplicate": other["名称"] = "旧称"
        patch["治理操作"]["设置字段"].append(other)
    elif problem == "same_record_modification":
        patch["修改元素"] = {"角色集": [{"名称": "甲", "详情": {"人物轨迹": "同批冲突"}}]}
    elif problem == "identity_operation":
        patch["治理操作"]["重命名元素"] = [{"类型": "角色", "原名称": "甲", "新名称": "乙"}]
    else: op["类型"] = []
    before_story, before_patch = copy.deepcopy(story), copy.deepcopy(patch)
    fixed, _ = normalize_governance_patch(patch, story=story)
    assert fixed["治理操作"]["设置字段"] == before_patch["治理操作"]["设置字段"]
    assert story == before_story and patch == before_patch


@pytest.mark.parametrize("reverse", [False, True])
def test_preservation_compares_only_equivalent_governance_encoding_views(tmp_path, reverse):
    story, native = fixture()
    encoded = copy.deepcopy(native)
    op = encoded["治理操作"]["设置字段"][0]
    op["原值"] = story["角色集"][0]["详情"]["人物轨迹"]
    op["值"] = json.dumps(NEW, ensure_ascii=False)
    before, after = (encoded, native) if reverse else (native, encoded)
    paths = [tmp_path / "before.json", tmp_path / "after.json"]
    for path, value in zip(paths, (before, after)): save_json(path, value)
    original = [p.read_bytes() for p in paths]
    assert preservation_errors(*paths, story) == []
    assert [p.read_bytes() for p in paths] == original
    assert preservation_errors(*paths), "without current-story identity/preimage, no encoding exemption is authorized"
    after = copy.deepcopy(after)
    after["治理操作"]["设置字段"][0]["理由"] = "删减"
    save_json(paths[1], after)
    assert preservation_errors(*paths, story), "encoding equivalence must not relax other prose"


@pytest.mark.parametrize("problem", [None, "missing_evidence", "false_quote", "stale_generation"])
def test_real_commit_normalizes_authenticated_raw_and_keeps_all_original_gates(tmp_path, problem):
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact, generation_is_current
    from test_audit_pack import write_completed_chapter
    story, patch = fixture()
    pipeline.init_dirs(tmp_path)
    write_completed_chapter(tmp_path, 1)
    for source in (tmp_path / "原文拆解").glob("*.md"): source.write_text(QUOTE, encoding="utf-8")
    target = tmp_path / "故事结构_增量.json"
    save_json(target, story)
    pipeline.ensure_story(tmp_path)
    original_story = target.read_bytes()
    if problem == "missing_evidence": patch["治理操作"]["设置字段"][0].pop("证据")
    if problem == "false_quote": patch["治理操作"]["设置字段"][0]["证据"][0]["原文"] = "原文没有这句话。"
    raw = json.dumps(patch, ensure_ascii=False).encode()
    output = tmp_path / "质量治理/按需治理/correction_encoding.json"
    base = "0" * 64 if problem == "stale_generation" else hashlib.sha256(original_story).hexdigest()
    publish_generated_artifact(tmp_path, output, raw, base)
    assert pipeline.commit_governance(tmp_path, str(output)) == (0 if problem is None else 1)
    assert next(output.parent.glob("original_input_*.json")).read_bytes() == raw
    if problem is None:
        current = json.loads(target.read_bytes())["角色集"][0]["详情"]
        assert json.loads(current["人物轨迹"]) == NEW
        fixed = json.loads(output.read_bytes())["治理操作"]["设置字段"][0]
        assert isinstance(fixed["原值"], str) and isinstance(fixed["值"], str)
        receipt = json.loads(output.with_suffix(".generation.json").read_bytes())
        assert receipt["artifact_sha256"] == digest(output) and receipt["base_revision"] == base
        assert json.loads(next(output.parent.glob("original_contract_*.json")).read_bytes()) == patch
    else:
        assert target.read_bytes() == original_story
        assert not output.with_suffix(".commit.json").exists()
        if problem == "stale_generation": assert output.read_bytes() == raw
        else: assert generation_is_current(output, target)


def test_bounded_final_uses_each_accepted_snapshot_as_the_next_encoding_preimage(tmp_path, monkeypatch):
    """Two accepted proposals require the second unit's current protected baseline."""
    from bounded_final import run_bounded_final
    from test_bounded_final import project_files
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    story, template = fixture()
    save_json(draft, story)
    chapter = tmp_path / "原文拆解/第001章_依据.md"
    chapter.parent.mkdir(); chapter.write_text(QUOTE, encoding="utf-8")
    versions = tmp_path / "故事结构版本"; versions.mkdir()
    save_json(versions / "story_after_ch001.json", story)
    calls, reviews = [], []
    values = [NEW, ["第二轮已接受的更正，仍保留原文未完成条件。", OLD[1]]]
    def worker(command, root, kind, task, output, **kwargs):
        if kind == "audit_correction":
            calls.append(kind)
            if len(calls) > 2: return 130
            current = json.loads((root / "故事结构_增量.json").read_bytes())
            patch = copy.deepcopy(template)
            op = patch["治理操作"]["设置字段"][0]
            op["原值"] = json.loads(current["角色集"][0]["详情"]["人物轨迹"])
            op["值"] = values[len(calls)-1]
            save_json(output, patch)
        else:
            assert kind == "recovery_review"
            reviews.append(kind)
            save_json(output, {"passed": True, "candidate_sha256": digest(root / "故事结构_增量.json"),
                "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "离线复核：精确旧值和原文均已核验。"})
        return 0
    assert run_bounded_final("offline-fake", tmp_path, pack, draft, worker) == 130
    ledger = json.loads(next((tmp_path / "质量治理/最终审计/分步治理").glob("*/state.json")).read_bytes())
    session = next((tmp_path / "质量治理/最终审计/分步治理").iterdir())
    saved = json.loads((session / ledger["snapshot"]).read_bytes())
    assert ledger["next"] == 2 and len(reviews) == 2
    assert json.loads(saved["角色集"][0]["详情"]["人物轨迹"]) == values[1]
    assert json.loads(draft.read_bytes())["角色集"][0]["详情"]["人物轨迹"] == story["角色集"][0]["详情"]["人物轨迹"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("change", ["delete", "replace", "reorder"])
def test_short_cross_wire_arrays_cannot_lose_or_change_model_new_content(tmp_path, reverse, change):
    """Encoding comparison must retain list checks even below the prose threshold."""
    story, native = fixture()
    native["治理操作"]["设置字段"][0]["值"] = ["甲", "乙"]
    encoded = copy.deepcopy(native)
    op = encoded["治理操作"]["设置字段"][0]
    op["原值"] = story["角色集"][0]["详情"]["人物轨迹"]
    values = {"delete": ["甲"], "replace": ["甲", "丙"], "reorder": ["乙", "甲"]}[change]
    op["值"] = json.dumps(values, ensure_ascii=False)
    before, after = (encoded, native) if reverse else (native, encoded)
    paths = [tmp_path / "before.json", tmp_path / "after.json"]
    for path, value in zip(paths, (before, after)): save_json(path, value)
    assert preservation_errors(*paths, story)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("style", ["compact", "indented"])
def test_short_equivalent_array_encoding_preserves_all_model_text(tmp_path, reverse, style):
    story, native = fixture()
    values = ["甲", "乙  "]
    native["治理操作"]["设置字段"][0]["值"] = values
    encoded = copy.deepcopy(native)
    op = encoded["治理操作"]["设置字段"][0]
    op["原值"] = story["角色集"][0]["详情"]["人物轨迹"]
    kwargs = {"separators": (",", ":")} if style == "compact" else {"indent": 2}
    op["值"] = json.dumps(values, ensure_ascii=False, **kwargs)
    before, after = (encoded, native) if reverse else (native, encoded)
    paths = [tmp_path / "before.json", tmp_path / "after.json"]
    for path, value in zip(paths, (before, after)): save_json(path, value)
    original = [p.read_bytes() for p in paths]
    assert preservation_errors(*paths, story) == []
    assert [p.read_bytes() for p in paths] == original
