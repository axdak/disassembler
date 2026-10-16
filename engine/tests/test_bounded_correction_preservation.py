"""有据纠错在容量治理中只对齐声明字段，原文与无关档案仍独立受保护。"""
import copy
import json
from pathlib import Path

import pytest

from apply_governance_ops import apply_ops
from bounded_final import governance_preservation_errors, run_bounded_final
from information_preservation import CORRECTION_FIELD
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from story_schema_rules import DEFAULTS, make_empty_structure


OLD = "甲在原记录中被写成已经死亡并永远不再出现的角色状态，这一叙述包含错误的事实结论。"
NEW = "原文明示甲仍然活着，只是暂时离开村子处理家事，旧记录中的死亡判断应据此纠正。"
HISTORY = "这段与本次纠错无关的重要历史经历必须完整保留，不能借有据纠错的声明一并抹去。"


def story():
    """保留真实纠错相同的长叙述、独立历史字段和正式追溯位置。"""
    value = make_empty_structure()
    value["介绍"] = {"标题": "测试书", "描述": "已有完整故事简介。"}
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="甲", 介绍=OLD, 分组="主角", 详情={
        "首次章节": "0001", "最近章节": "0001", "当前状态": OLD, "历史经历": HISTORY})
    value["角色集"] = [role]
    return value


def correction(field="详情/当前状态", old=OLD, new=NEW):
    return {"类型": "角色", "名称": "甲", "字段": field, "原值": old, "值": new,
            "理由": "原文明确否定死亡结论，纠正旧误并保留旧值。",
            "证据": [{"章节": "0001", "原文": "甲还活着，只是离村办事。"}]}


def proposal(*setters, **operations):
    return {"章节范围": "第001章", "新增元素": {}, "修改元素": {},
            "治理操作": {**operations, "设置字段": list(setters)}}


def documents(tmp_path, before, after, patch):
    paths = [tmp_path / name for name in ("before.json", "after.json", "patch.json", "baseline.json")]
    for path, value in zip(paths, (before, after, patch)):
        save_json(path, value)
    return paths


@pytest.mark.parametrize("field", ["介绍", "详情/当前状态"])
def test_evidenced_exact_field_replacement_aligns_baseline_and_archive(tmp_path, field):
    before = story()
    patch = proposal(correction(field))
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    paths = documents(tmp_path, before, after, patch)
    assert governance_preservation_errors(*paths) == []
    baseline = json.loads(paths[-1].read_text(encoding="utf-8"))
    assert baseline["角色集"][0]["详情"][CORRECTION_FIELD] == after["角色集"][0]["详情"][CORRECTION_FIELD]


@pytest.mark.parametrize("missing", ["原值", "值", "理由", "证据"])
def test_incomplete_protocol_cannot_authorize_replacement_even_with_correction_archive(tmp_path, missing):
    before = story()
    patch = proposal(correction())
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    patch["治理操作"]["设置字段"][0].pop(missing)
    assert governance_preservation_errors(*documents(tmp_path, before, after, patch))


def test_stale_preimage_is_rejected_instead_of_trusting_existing_archive(tmp_path):
    before = story()
    patch = proposal(correction())
    after, _, _ = apply_ops(before, patch)
    patch["治理操作"]["设置字段"][0]["原值"] = "与真实旧值不一致的声明。"
    assert governance_preservation_errors(*documents(tmp_path, before, after, patch))


def test_archiving_old_fact_without_a_declared_setter_does_not_authorize_its_rewrite(tmp_path):
    before = story()
    after, _, _ = apply_ops(before, proposal(correction()))
    assert CORRECTION_FIELD in after["角色集"][0]["详情"]
    assert governance_preservation_errors(*documents(tmp_path, before, after, proposal()))


def test_valid_correction_cannot_hide_loss_of_unrelated_history(tmp_path):
    before = story()
    patch = proposal(correction())
    after, _, _ = apply_ops(before, patch)
    after["角色集"][0]["详情"]["历史经历"] = "已删减"
    assert governance_preservation_errors(*documents(tmp_path, before, after, patch))


def test_candidate_must_contain_declared_new_value_even_when_it_is_short(tmp_path):
    before = story()
    patch = proposal(correction(new="仍然活着"))
    after, _, _ = apply_ops(before, patch)
    after["角色集"][0]["详情"]["当前状态"] = "已经死亡"
    assert governance_preservation_errors(*documents(tmp_path, before, after, patch))


@pytest.mark.parametrize("stale", [False, True])
def test_merge_and_rename_share_one_trace_preimage_during_baseline_alignment(tmp_path, stale):
    before = story()
    before["地点集"] = []
    for name, first, recent in [("旧祭台", "0162", "0162"), ("另一祭台", "0160", "0161")]:
        item = copy.deepcopy(DEFAULTS["地点集"])
        item.update(名称=name, 介绍="原文已登记的完整地点介绍。", 详情={"首次章节": first, "最近章节": recent})
        before["地点集"].append(item)
    op = correction("详情/首次章节", "0162", "0160")
    op.update(类型="地点", 名称="正式祭台", 证据=[{"章节": "0160", "原文": "谷中有祭台。"}])
    patch = proposal(op, 合并元素=[{"类型": "地点", "主": "旧祭台", "并入": ["另一祭台"]}],
                     重命名元素=[{"类型": "地点", "原名称": "旧祭台", "新名称": "正式祭台"}])
    patch["章节范围"] = "第156章-第160章"
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    if stale:
        patch["治理操作"]["设置字段"][0]["原值"] = "0161"
    paths = documents(tmp_path, before, after, patch)
    errors = governance_preservation_errors(*paths)
    if stale:
        assert errors
    else:
        assert errors == []
        baseline = json.loads(paths[-1].read_text(encoding="utf-8"))
        archive = [json.loads(entry) for entry in json.loads(baseline["地点集"][0]["详情"][CORRECTION_FIELD])]
        assert archive[-1]["原值"] == "0162" and archive[-1]["新值"] == "0160"


@pytest.mark.parametrize("problem", ["missing_quote", "future_trace"])
def test_bounded_final_checks_real_host_evidence_before_merge_review_or_publication(tmp_path, monkeypatch, problem):
    """隔离保全比较，直接验证真实容量驱动的原文门禁及失败断点。"""
    import bounded_final
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "10000")
    monkeypatch.setenv("ND_CONTEXT_OUTPUT_TOKENS", "2000")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "2000")
    monkeypatch.setattr(bounded_final, "governance_preservation_errors", lambda *args: [])
    draft = tmp_path / "故事结构_草稿.json"
    value = story()
    save_json(draft, value)
    raw = draft.read_bytes()
    pack = tmp_path / "final.md"
    pack.write_text("按已有原文纠正指定字段，其余档案不变。", encoding="utf-8")
    save_json(pack.with_suffix(".md.sources.json"), [])
    chapters = tmp_path / "原文拆解"
    chapters.mkdir()
    (chapters / "第001章_原文.md").write_text("甲还活着，只是离村办事。", encoding="utf-8")
    versions = tmp_path / "故事结构版本"
    versions.mkdir()
    save_json(versions / "story_after_ch001.json", value)
    op = correction()
    patch = proposal(op)
    if problem == "missing_quote":
        op["证据"][0]["原文"] = "这句引文并不存在于原文之中。"
    else:
        op.update(字段="详情/最近章节", 原值="0001", 值="0002")
        patch["章节范围"] = "第001章-第002章"
    calls = []

    def worker(command, root, kind, task, output, **options):
        calls.append(kind)
        assert kind == "audit_correction", "invalid evidence must not reach independent review"
        save_json(output, patch)
        return 0

    assert run_bounded_final("offline", tmp_path, pack, draft, worker) == REPAIR_EXHAUSTED
    assert calls and set(calls) == {"audit_correction"}
    assert draft.read_bytes() == raw
    assert not pack.with_suffix(".final-receipt.json").is_file()
    ledgers = list((tmp_path / "质量治理/最终审计/分步治理").glob("*/state.json"))
    assert len(ledgers) == 1
    state = json.loads(ledgers[0].read_text(encoding="utf-8"))
    assert state["next"] == 0 and "[质量字段:" in state["feedback"]
    dispatches = len(calls)
    assert run_bounded_final("offline", tmp_path, pack, draft, worker) == REPAIR_EXHAUSTED
    assert len(calls) == dispatches


def test_candidate_cannot_forge_host_field_archive_to_replace_an_undeclared_fact(tmp_path):
    """容量补丁不是宿主字段修复，不能自行制造该流程的归档授权。"""
    from local_json_patch import FIELD_REPAIR_ARCHIVE
    before = story()
    after = copy.deepcopy(before)
    after["角色集"][0]["介绍"] = NEW
    after["角色集"][0]["详情"][FIELD_REPAIR_ARCHIVE] = json.dumps([
        json.dumps({"字段": "介绍", "原值": OLD}, ensure_ascii=False)], ensure_ascii=False)
    assert governance_preservation_errors(*documents(tmp_path, before, after, proposal()))
