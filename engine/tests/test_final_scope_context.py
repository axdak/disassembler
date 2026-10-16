"""Offline final batches carry exact local evidence without relaxing acceptance."""
import copy
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from context_budget import capacity_scope, estimate_tokens, ContextBudget
from fault_recovery import REVIEW_CHECKS, digest, recover_artifact
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from story_schema_rules import DEFAULTS, make_empty_structure


INLINE_HEADER = "## 本批完整档案（宿主参考快照）"


def inline_payload(prompt):
    """Read only the explicit host data block, not natural-language instructions."""
    assert INLINE_HEADER in prompt
    block = prompt.split(INLINE_HEADER, 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
    return json.loads(block)


def final_case(tmp_path):
    """Use real scope/application/checkpoints and replace only model execution."""
    story = make_empty_structure()
    story["介绍"] = {"标题": "测试故事", "描述": "已经核实的全书背景。"}
    for name in ("甲", "乙", "旁观者"):
        role = copy.deepcopy(DEFAULTS["角色集"])
        role.update(名称=name, 介绍="" if name != "旁观者" else "旁观者既有完整介绍与限定条件都必须原样保留，不得借修复其他角色而删除。")
        role["详情"] = {"首次章节": "0001", "最近章节": "0001", "完整历史": "原文含引号“甲”、换行\n和未完成条件，完整保留。",
                      "关联角色": ["角色:旁观者"], "人物轨迹": '["旧轨迹一","旧轨迹二"]'}
        story["角色集"].append(role)
    output, pack = tmp_path / "故事结构_草稿.json", tmp_path / "repair.md"
    save_json(output, story)
    pack.write_text("只补本批字段；保留所有未修改事实，仍需独立原文复核。", encoding="utf-8")
    original = output.read_bytes()
    source = tmp_path / "原文拆解/第001章_测试.md"
    source.parent.mkdir()
    source.write_text("甲与乙见证救援，旁观者只提供绳索；行动尚未完成。", encoding="utf-8")
    values = {"/角色集/0/介绍": "甲是救援见证者，行动尚未完成。", "/角色集/0/标签集": ["见证者"],
              "/角色集/1/介绍": "乙见证了救援行动。"}
    calls, prompts, published = [], [], []

    def validate(root):
        data = json.loads((root / output.name).read_text(encoding="utf-8"))
        errors = [f"[质量字段:{pointer}] 字段尚未补齐" for pointer in values
                  if not (data["角色集"][int(pointer.split("/")[2])].get(pointer.split("/")[3]))]
        return (1, "\n".join(errors)) if errors else (0, "")

    def worker(command, root, kind, task, expected, *args, **options):
        calls.append(kind)
        prompt = task.read_text(encoding="utf-8")
        prompts.append((kind, prompt))
        if kind == "recovery_patch":
            save_json(expected, {"base_sha256": digest(root / "reference.json"),
                                 "changes": [{"path": key, "value": value} for key, value in values.items()], "moves": []})
        elif kind == "recovery_review":
            save_json(expected, {"passed": True, "candidate_sha256": digest(root / output.name),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "离线独立核对本批完整档案和原文。"})
        else:
            pytest.fail(f"本批不应扩大任务：{kind}")
        return 0

    def publish(candidate):
        published.append(candidate.read_bytes())
        output.write_bytes(candidate.read_bytes())
        return 0

    options = dict(command="local-fake", worker=worker, validate=validate, publish=publish,
                   strategies=1, retries=0)
    return SimpleNamespace(root=tmp_path, output=output, pack=pack, story=story, original=original,
                           values=values, calls=calls, prompts=prompts, published=published, options=options)


def run(case):
    return recover_artifact(case.root, "repair_final", case.pack, case.output, **case.options)


@pytest.mark.parametrize("command", ["local-fake", "python agent_worker.py --provider codebuddy"])
def test_patch_and_review_inline_complete_deduplicated_reference_records(tmp_path, command):
    case = final_case(tmp_path)
    case.options["command"] = command
    assert run(case) == 0
    assert case.calls == ["recovery_patch", "recovery_review"]
    for kind, prompt in case.prompts:
        data = inline_payload(prompt)
        assert data["reference_sha256"] == digest(Path(data["reference"]))
        assert data["records"] == [{"path": "/角色集/0", "value": case.story["角色集"][0]},
                                   {"path": "/角色集/1", "value": case.story["角色集"][1]}]
        assert set(data["selected_fields"]) == set(case.values)
        assert estimate_tokens(prompt) <= ContextBudget.from_env().usable_input * 3 // 4
        if kind == "recovery_review":
            assert data["actual_new_values"] == case.values
            assert data["candidate_sha256"] == digest(Path(data["candidate"]))
            assert "程序仍对整份候选逐值保全" in prompt
            assert "不必每批遍历整份候选" in prompt
        else:
            assert "actual_new_values" not in data
    actual = json.loads(case.output.read_bytes())
    assert actual["角色集"][2] == case.story["角色集"][2]
    assert len(case.published) == 1


def test_root_intro_inline_is_complete_object_and_preserves_exact_source_hash(tmp_path):
    from fault_recovery import _inline_final_scope
    reference, candidate = tmp_path / "reference.json", tmp_path / "candidate.json"
    raw = '{ "介绍": {"标题":"原题", "描述":"旧说明\\n原样", "历史":"不要删除"} }'.encode("utf-8")
    reference.write_bytes(raw)
    changed = json.loads(raw); changed["介绍"]["描述"] = "新的有据说明"
    save_json(candidate, changed)
    prompt = _inline_final_scope("现有指令", reference, ["/介绍/描述"], candidate=candidate)
    data = inline_payload(prompt)
    assert data["reference_sha256"] == hashlib.sha256(raw).hexdigest()
    assert data["records"] == [{"path": "/介绍", "value": json.loads(raw)["介绍"]}]
    assert data["actual_new_values"] == {"/介绍/描述": "新的有据说明"}
    assert reference.read_bytes() == raw


def test_oversized_inline_group_falls_back_whole_without_truncating_material(tmp_path):
    from fault_recovery import _inline_final_scope
    reference = tmp_path / "reference.json"
    record = {"名称": "甲", "介绍": "", "详情": {"完整证据": "起" + "历史原文" * 1500 + "终"}}
    save_json(reference, {"角色集": [record]})
    before = reference.read_bytes()
    with capacity_scope(dict(model_input_tokens=6000, model_context_tokens=8000,
                             model_output_tokens=1000, model_reserve_tokens=1000)):
        prompt = _inline_final_scope("已有短任务", reference, ["/角色集/0/介绍"])
    assert INLINE_HEADER not in prompt
    assert "未内联" in prompt and str(reference) in prompt
    assert "/角色集/0/介绍" in prompt
    assert "历史原文" not in prompt
    assert reference.read_bytes() == before


@pytest.mark.parametrize("interruption", ["generated", "validated"])
def test_final_scope_resume_reuses_generation_and_keeps_exact_batch_context(tmp_path, interruption):
    case = final_case(tmp_path)
    validate, worker = case.options["validate"], case.options["worker"]
    interrupted = False

    def validation(root):
        nonlocal interrupted
        result = validate(root)
        if interruption == "generated" and result == (0, "") and not interrupted:
            interrupted = True
            raise RuntimeError("生成后中断")
        return result

    def transient(command, root, kind, task, expected, *args, **kwargs):
        nonlocal interrupted
        if interruption == "validated" and kind == "recovery_review" and not interrupted:
            interrupted = True
            case.calls.append(kind); case.prompts.append((kind, task.read_text(encoding="utf-8")))
            return 130
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options.update(validate=validation, worker=transient)
    if interruption == "generated":
        with pytest.raises(RuntimeError, match="生成后中断"):
            run(case)
    else:
        assert run(case) == 130
    assert not case.published and case.output.read_bytes() == case.original
    assert run(case) == 0
    assert case.calls.count("recovery_patch") == 1
    for kind, prompt in case.prompts:
        data = inline_payload(prompt)
        assert set(data["selected_fields"]) == set(case.values)
        if kind == "recovery_review":assert data["actual_new_values"] == case.values
    assert len(case.published) == 1


@pytest.mark.parametrize("failure", ["review_rejected", "validator_damage", "out_of_scope"])
def test_inline_context_cannot_bypass_review_preservation_or_patch_scope(tmp_path, failure):
    case = final_case(tmp_path)
    worker, validate = case.options["worker"], case.options["validate"]

    def wrapped(command, root, kind, task, expected, *args, **kwargs):
        rc = worker(command, root, kind, task, expected, *args, **kwargs)
        if kind == "recovery_patch" and failure == "out_of_scope":
            patch = json.loads(expected.read_bytes());patch["changes"].append({"path": "/角色集/2/介绍", "value": "越界改写"});save_json(expected, patch)
        if kind == "recovery_review" and failure == "review_rejected":
            verdict = json.loads(expected.read_bytes());verdict.update(passed=False, issues=["原文不支持"]);save_json(expected, verdict)
        return rc

    def damaging_validation(root):
        result = validate(root)
        if failure == "validator_damage" and result == (0, ""):
            path = root / case.output.name;data = json.loads(path.read_bytes());data["角色集"][2]["介绍"] = "被删除了限定";save_json(path, data)
        return result

    case.options.update(worker=wrapped, validate=damaging_validation)
    assert run(case) == REPAIR_EXHAUSTED
    assert case.output.read_bytes() == case.original and not case.published
    if failure != "review_rejected":assert "recovery_review" not in case.calls


def test_nonfinal_review_retains_existing_full_artifact_contract(tmp_path):
    from test_fault_recovery import scenario
    output, pack, _, calls, published, options = scenario(tmp_path)
    worker = options["worker"];reviews = []

    def capture(command, root, kind, task, expected, *args, **kwargs):
        if kind == "recovery_review":reviews.append(task.read_text(encoding="utf-8"))
        return worker(command, root, kind, task, expected, *args, **kwargs)

    options["worker"] = capture
    assert recover_artifact(tmp_path, "repair_chapter", pack, output, **options) == 0
    assert len(published) == 1
    assert INLINE_HEADER not in reviews[0]
    assert "无正文，不替代全文复核" in reviews[0]


@pytest.mark.parametrize("field", ["标题", "描述"])
def test_root_intro_replacement_needs_exact_host_authorization_and_final_trust(tmp_path, field):
    from fault_recovery import preservation_errors
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    story = {"介绍": {"标题": "原来的完整书名文本必须精确保留，不允许因其他字段授权而被改变。",
                    "描述": "原来的完整故事描述包含已确认事实与保留条件，不能无授权重写。"},
             "角色集": [{"名称": "甲", "介绍": "甲的完整人物介绍同样不能因为根简介获准纠错而被顺带修改。"}], "事件集": []}
    changed = copy.deepcopy(story); changed["介绍"][field] = "依据原文纠正的完整新值。"
    save_json(before, story); save_json(after, changed)
    pointer = "/介绍/" + field
    assert preservation_errors(before, after)
    assert preservation_errors(before, after, editable_metadata_paths={pointer}, allow_field_archives=False)
    assert preservation_errors(before, after, editable_metadata_paths={pointer}, allow_field_archives=True) == []
    for neighbor in ["intro_neighbor", "profile_neighbor"]:
        damaged = copy.deepcopy(changed)
        if neighbor == "intro_neighbor":
            other = "标题" if field == "描述" else "描述"
            damaged["介绍"][other] = damaged["介绍"][other].replace("原来", "后来", 1)
        else:
            damaged["角色集"][0]["介绍"] = damaged["角色集"][0]["介绍"].replace("甲", "乙", 1)
        save_json(after, damaged)
        assert preservation_errors(before, after, editable_metadata_paths={pointer}, allow_field_archives=True)


@pytest.mark.parametrize("approved", [False, True])
def test_real_final_owner_repairs_only_authorized_nonempty_root_description(tmp_path, monkeypatch, approved):
    """Use the original owner and strict schema validator with an explicit semantic error pointer."""
    import run_pipeline as pipeline
    from test_final_field_repair import complete_story
    old = "甲在救援过程中已经独自完成所有行动，这段无据结果需要精确定点纠正并保留其他事实。"
    new = "甲参与救援，现有原文仅证明行动进行中，尚不能判断已经完成。"
    story = complete_story(); story["介绍"]["描述"] = old
    output = tmp_path / "故事结构_草稿.json"
    save_json(output, story)
    pack = tmp_path / "质量治理/最终审计/repair_final.md"
    pack.parent.mkdir(parents=True)
    pack.write_text("[质量字段:/介绍/描述] 纠正无据完成结果。", encoding="utf-8")
    source = tmp_path / "原文拆解/第001章_测试.md"
    source.parent.mkdir(); source.write_text("甲正在救援，行动尚未完成。", encoding="utf-8")
    strict = pipeline.cmd_commit_final_draft
    calls = []

    def validate(root):
        data = json.loads((root / output.name).read_bytes())
        if data["介绍"]["描述"] == old:
            feedback = root / pack.relative_to(tmp_path)
            feedback.parent.mkdir(parents=True, exist_ok=True)
            feedback.write_text("[质量字段:/介绍/描述] 纠正无据完成结果。", encoding="utf-8")
            return 1
        return strict(root)

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        if kind == "recovery_patch":
            save_json(expected, {"base_sha256": digest(root / "reference.json"),
                                 "changes": [{"path": "/介绍/描述", "value": new}], "moves": []})
        elif kind == "recovery_review":
            context = inline_payload(task.read_text(encoding="utf-8"))
            assert context["selected_fields"] == ["/介绍/描述"]
            assert context["actual_new_values"] == {"/介绍/描述": new}
            save_json(expected, {"passed": approved, "candidate_sha256": digest(root / output.name),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, approved),
                                 "issues": [] if approved else ["独立复核未通过"], "reason": "依据冻结章节核对结果措辞。"})
        else:
            pytest.fail(kind)
        return 0

    monkeypatch.setattr(pipeline, "cmd_commit_final_draft", validate)
    monkeypatch.setattr(pipeline, "run_worker", worker)
    result = pipeline.run_repair_worker("local-fake", tmp_path, "repair_final", pack, output, retries=0, repair_limit=1)
    assert result == (0 if approved else REPAIR_EXHAUSTED)
    assert calls == ["recovery_patch", "recovery_review"]
    actual = json.loads(output.read_bytes())
    assert actual["介绍"]["描述"] == (new if approved else old)
    assert actual["介绍"]["标题"] == story["介绍"]["标题"]
    assert actual["角色集"] == story["角色集"]


def test_final_patch_context_carries_shared_field_contract_even_when_records_fall_back(tmp_path):
    """The real legacy batch emitted concatenated tags after reading only a contract page."""
    from fault_recovery import _inline_final_scope
    from generation_contract import generation_contract_text
    reference = tmp_path / "reference.json"
    save_json(reference, {"事件集": [{"名称": "事件", "标签集": [], "详情": {"正文": "完整原文" * 1000}}]})
    contract = generation_contract_text()
    prompt = _inline_final_scope("只修已授权标签，不扩大修改范围", reference, ["/事件集/0/标签集"])
    assert contract in prompt
    assert "主题+叙事功能+情绪+冲突" in prompt
    assert "8位段号-40字以内复合剧情段短名" in contract
    with capacity_scope(dict(model_input_tokens=6000, model_context_tokens=8000,
                             model_output_tokens=1000, model_reserve_tokens=1000)):
        fallback = _inline_final_scope("只修已授权标签", reference, ["/事件集/0/标签集"])
    assert INLINE_HEADER not in fallback
    assert contract in fallback and "未内联" in fallback
