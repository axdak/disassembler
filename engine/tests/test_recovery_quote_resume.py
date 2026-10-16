"""错误治理长引文的端到端恢复：真实保全、原文校验、断点与发布均在线下执行。"""
import copy
import json
from types import SimpleNamespace

import pytest

from fault_recovery import REVIEW_CHECKS, digest, recover_artifact
from information_preservation import correction_evidence_errors
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_recovery_quote_preservation import OLD, TRUE, candidate


QUOTE_PATH = "/治理操作/设置字段/0/证据/0/原文"
INVENTED = "这是一段并不存在于第157章真实原文中的长篇引文，不能因为局部修复声明而成为有效证据。"


def quote_recovery(tmp_path, replacement=TRUE):
    """只替换模型边界，保留宿主原文读取、保全、阶段持久化与发布流程。"""
    output = tmp_path / "correction_156-160.json"
    pack = tmp_path / "repair.md"
    source = tmp_path / "原文拆解/第157章_测试.md"
    source.parent.mkdir()
    source.write_text("# 第157章\n\n" + TRUE, encoding="utf-8")
    save_json(tmp_path / "故事结构_增量.json", {
        "角色集": [{"名称": "凤栖梧", "详情": {"首次章节": "0156"}}],
    })
    save_json(output, candidate())
    pack.write_text("核对156至160章；纠正错误引文，保留人物首次章节纠错及全部其他字段。", encoding="utf-8")
    initial, source_bytes = output.read_bytes(), source.read_bytes()
    calls, publications, validations = [], [], []

    def validate(root):
        # 使用提交门同一原文检查器，不由假 worker 或测试中的相等判断批准候选。
        value = json.loads((root / output.name).read_text(encoding="utf-8"))
        chapter = (root / source.relative_to(tmp_path)).read_text(encoding="utf-8")
        errors = correction_evidence_errors(value, {157: chapter}, 160)
        validations.append((root, errors))
        return (1, "\n".join(errors)) if errors else (0, "")

    def worker(command, root, kind, task, expected, *args, **kwargs):
        assert output.read_bytes() == initial
        assert source.read_bytes() == source_bytes
        calls.append(kind)
        if kind == "recovery_patch":
            save_json(expected, {
                "base_sha256": digest(root / "reference.json"),
                "changes": [{"path": QUOTE_PATH, "value": replacement}],
            })
        elif kind == "recovery_review":
            # 独立复核读取的是已完成真实原文校验的候选和冻结原文。
            assert validate(root) == (0, "")
            save_json(expected, {
                "passed": True,
                "candidate_sha256": digest(root / output.name),
                "checks": dict.fromkeys(REVIEW_CHECKS, True),
                "issues": [],
                "reason": "逐字引文与冻结第157章原文一致，其他纠错字段及分析未改动。",
            })
        else:
            pytest.fail(f"单个有定位的引文错误不应升级整份重写或诊断：{kind}")
        return 0

    def publish(path):
        # 回调落盘前再次走真实来源检查，防止只凭复核回执发布。
        code, _ = validate(path.parent)
        if code:
            return code
        publications.append(path.read_bytes())
        output.write_bytes(path.read_bytes())
        return 0

    options = dict(command="local-fake", worker=worker, validate=validate, publish=publish,
                   chapter_seq=160, retries=0, strategies=1)
    return SimpleNamespace(root=tmp_path, output=output, pack=pack, source=source,
                           initial=initial, source_bytes=source_bytes, calls=calls,
                           publications=publications, validations=validations, options=options)


def run_recovery(case):
    return recover_artifact(case.root, "repair_chapter", case.pack, case.output, **case.options)


def recovery_state(case):
    ledger = next((case.root / "质量治理/自动恢复").glob("*/state.json"))
    return json.loads(ledger.read_text(encoding="utf-8"))


def assert_published_quote(case):
    """完整交付只允许引文字段变化，旧引文和宿主原文均不得被伪造。"""
    expected = copy.deepcopy(candidate())
    expected["治理操作"]["设置字段"][0]["证据"][0]["原文"] = TRUE
    actual = json.loads(case.output.read_text(encoding="utf-8"))
    assert actual == expected
    assert OLD not in actual["治理操作"]["设置字段"][0]["证据"][0]["原文"]
    assert case.source.read_bytes() == case.source_bytes
    assert len(case.publications) == 1
    assert case.options["validate"](case.root) == (0, "")
    assert recovery_state(case)["status"] == "succeeded"


def test_true_long_quote_publishes_after_one_targeted_patch_and_review(tmp_path):
    """丢失 scoped metadata 授权会把已纠正的长引文误判为删改，阻止发布。"""
    case = quote_recovery(tmp_path)

    assert run_recovery(case) == 0

    assert_published_quote(case)
    assert case.calls == ["recovery_patch", "recovery_review"]
    state = recovery_state(case)
    assert [entry["task"] for entry in state["resource_calls"]] == ["recovery_patch", "recovery_review"]
    assert len(state["attempts"]) == 1
    assert state["attempts"][0]["strategy"] == "targeted"


def test_invented_long_quote_reaches_source_validation_and_never_publishes(tmp_path):
    """元数据保全放行不能替代原文真实性检查，也不能触发无效候选复核或发布。"""
    case = quote_recovery(tmp_path, INVENTED)

    assert run_recovery(case) == REPAIR_EXHAUSTED

    assert case.output.read_bytes() == case.initial
    assert case.source.read_bytes() == case.source_bytes
    assert not case.publications
    assert case.calls == ["recovery_patch"]
    state = recovery_state(case)
    assert state["attempts"][0]["status"] == "validation_failed"
    feedback_path = case.root / "质量治理/自动恢复" / next(
        (case.root / "质量治理/自动恢复").iterdir()
    ).name / state["attempts"][0]["feedback_file"]
    assert "纠错引文在所述原文中不存在" in feedback_path.read_text(encoding="utf-8")


def test_generated_quote_checkpoint_resumes_without_repeating_patch(tmp_path):
    """生成后校验崩溃须保留授权范围，续接同一候选而非再生成补丁。"""
    case = quote_recovery(tmp_path)
    validate = case.options["validate"]

    def crash_after_generated_quote(root):
        result = validate(root)
        if result == (0, ""):
            raise RuntimeError("模拟真实长引文生成后宿主校验退出")
        return result

    case.options["validate"] = crash_after_generated_quote
    with pytest.raises(RuntimeError, match="生成后宿主校验退出"):
        run_recovery(case)

    pending = recovery_state(case)
    assert pending["checkpoint"]["stage"] == "generated"
    assert pending["checkpoint"]["validation_inflight"] is True
    assert pending["attempts"][0]["editable_metadata_paths"] == [QUOTE_PATH]
    assert case.output.read_bytes() == case.initial
    assert not case.publications
    assert case.calls == ["recovery_patch"]

    case.options["validate"] = validate
    assert run_recovery(case) == 0

    assert_published_quote(case)
    assert case.calls == ["recovery_patch", "recovery_review"]
    completed = recovery_state(case)
    assert len(completed["attempts"]) == 1
    assert [entry["task"] for entry in completed["resource_calls"]] == ["recovery_patch", "recovery_review"]

