"""Exercise outbound recovery prompts without paid global rereads or weaker evidence."""
import pytest

from bounded_generation import run_paged_recovery
from fault_recovery import recover_artifact
from recovery_materials import read_material_document
from test_final_scope_context import final_case, inline_payload, run


COMMAND = "python agent_worker.py --provider codebuddy"


@pytest.mark.parametrize("command", ["local-fake", COMMAND])
def test_scoped_final_outbound_patch_and_review_do_not_require_global_rereads(tmp_path, command):
    """Catch the old common instruction overriding complete inline batch evidence."""
    case = final_case(tmp_path)
    case.options["command"] = command
    assert run(case) == 0
    assert case.calls == ["recovery_patch", "recovery_review"]
    for kind, prompt in case.prompts:
        assert "从目录读取完整任务要求、完整校验反馈" not in prompt
        assert "本批已授权字段" in prompt
        assert "完整关联档案" in prompt
        assert "必要原文" in prompt
        assert set(inline_payload(prompt)["selected_fields"]) == set(case.values)
        if kind == "recovery_review":
            assert inline_payload(prompt)["actual_new_values"] == case.values
    assert len(case.published) == 1


def test_mcp_routing_does_not_reintroduce_global_feedback_and_draft_requirement(tmp_path):
    """Catch the adapter's appended instruction undoing the owner's scoped prompt."""
    pack, output = tmp_path / "recovery_patch.md", tmp_path / "candidate.json"
    pack.write_text("本批档案与实际待修字段已经完整提供。", encoding="utf-8")
    received = []

    def worker(command, project, kind, task, target, *args, **kwargs):
        received.append(task.read_text(encoding="utf-8"))
        target.write_text('{"ok":true}', encoding="utf-8")
        return 0

    assert run_paged_recovery(worker, COMMAND, tmp_path, "recovery_patch", pack, output) == 0
    assert len(received) == 1
    prompt = received[0]
    assert "先读取本次完整校验反馈和任务约束、待修候选" not in prompt
    assert "已完整内联" in prompt and "未提供" in prompt
    assert "目录或搜索摘要" in prompt
    assert "身份" in prompt and "歧义" in prompt


def test_unprovided_extra_task_constraints_remain_required_and_losslessly_readable(tmp_path):
    """A shared field contract must not hide the semantic task in contract.md."""
    case = final_case(tmp_path)
    extra = "额外语义约束：只证明背书包，不能把吕红玲希望阻止二人写成已经斥退。"
    case.pack.write_text(extra + "\n只修已授权字段，保留所有条件与既有证据。", encoding="utf-8")
    worker = case.options["worker"]
    observed = []

    def inspect(command, root, kind, task, expected, *args, **kwargs):
        prompt = task.read_text(encoding="utf-8")
        observed.append((kind, read_material_document(root / "contract.md")))
        assert extra not in prompt
        assert "共享字段契约不替代完整原任务" in prompt
        assert "未内联的额外任务约束" in prompt and "contract.md" in prompt
        return worker(command, root, kind, task, expected, *args, **kwargs)

    case.options.update(command=COMMAND, worker=inspect)
    assert run(case) == 0
    assert [kind for kind, _ in observed] == ["recovery_patch", "recovery_review"]
    assert all(extra in text for _, text in observed)


def test_nonfinal_keeps_full_original_task_candidate_and_source_review(tmp_path):
    """Only a real final field scope can narrow the existing complete-artifact review."""
    from test_fault_recovery import scenario
    output, pack, _, calls, published, options = scenario(tmp_path)
    worker = options["worker"]
    observed = []

    def inspect(command, root, kind, actual_pack, expected, *args, **kwargs):
        observed.append((kind, actual_pack.read_text(encoding="utf-8")))
        return worker(command, root, kind, actual_pack, expected, *args, **kwargs)

    options.update(command=COMMAND, worker=inspect)
    assert recover_artifact(tmp_path, "repair_chapter", pack, output, **options) == 0
    assert len(published) == 1
    review = next(prompt for kind, prompt in observed if kind == "recovery_review")
    assert "从目录读取完整任务要求、完整校验反馈" in review
    assert "比较原始失败产物与当前候选，并核对完整原文证据" in review
    assert "无正文，不替代全文复核" in review


def test_unscoped_final_still_refuses_whole_story_rewrite_without_worker(tmp_path):
    """No machine-locatable field remains a hard refusal, not a broader prompt."""
    from test_fault_recovery import scenario
    from recovery_contract import CONTRACT_INVALID
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = output.read_bytes()
    assert recover_artifact(tmp_path, "repair_final", pack, output, **options) == CONTRACT_INVALID
    assert calls == [] and published == []
    assert output.read_bytes() == original
