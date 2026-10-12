"""Bounded composition must preserve evidence, schemas and restart semantics."""
import json
import re
from pathlib import Path

import pytest

from capacity_tree import CapacityTree, TreeFailure, merge_json, merge_markdown
from context_budget import CONTEXT_EXCEEDED, OUTPUT_TRUNCATED, estimate_tokens
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "7000")
    monkeypatch.setenv("ND_CONTEXT_CONTEXT_TOKENS", "10000")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "1000")
    monkeypatch.setenv("ND_CONTEXT_OUTPUT_TOKENS", "1200")


def review_response(prompt, addition=None):
    """Fake only the internal receipt; never call a live reviewer in tests."""
    match = re.search(r"复核材料指纹：([0-9a-f]{64})", prompt)
    return {"overview": "已逐项核对原件和跨片关系", "content": {
        "checked": match[1], "unresolved": [], "addition": addition}} if match else None


def fake_worker(calls, *, cancel_at=None):
    def worker(command, root, kind, pack, output, **options):
        assert kind == "bounded_fragment"
        assert options["retries"] == 0
        if cancel_at is not None and len(calls) == cancel_at:
            return 130
        prompt = pack.read_text(encoding="utf-8")
        calls.append(prompt)
        assert estimate_tokens(prompt) <= 6000
        span = re.search(r"字符区间：(\d+)-(\d+)", prompt)
        content = "# 原报告\n\n## 分析\n" + "保留完整发现 " + span[0] if span else None
        save_json(output, {"overview": "角色变化、证据区间见节点原件", "content": content})
        return 0
    return worker


def test_tree_covers_full_source_and_reuses_all_levels(tmp_path, limits):
    source = tmp_path / "long.md"
    source.write_text("原文必须完整保留。" * 1500 + "最后一句", encoding="utf-8")
    calls = []
    tree = CapacityTree(tmp_path, "fake", fake_worker(calls), "保留报告结构和全部证据", "报告.md", [(source, "完整历史")])
    _, contents, _ = tree.run()
    count = len(calls)
    assert count > 5
    coverage = json.loads((tree.root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage[0]["start"] == 0 and coverage[-1]["end"] == len(source.read_text(encoding="utf-8"))
    assert all(a["end"] == b["start"] for a, b in zip(coverage, coverage[1:]))
    combined = merge_markdown(contents)
    assert combined.count("# 原报告") == 1
    assert all(f"字符区间：{part['start']}-{part['end']}" in combined for part in coverage)
    tree.run()
    assert len(calls) == count


def test_cancel_resumes_without_repeating_successful_leaf(tmp_path, limits):
    source = tmp_path / "chapter.md"
    source.write_text("原始材料" * 1500, encoding="utf-8")
    calls = []
    args = (tmp_path, "fake", fake_worker(calls, cancel_at=2), "完整报告", "报告.md", [(source, "章节")])
    with pytest.raises(TreeFailure) as failure:
        CapacityTree(*args).run()
    assert failure.value.code == 130
    before = calls.copy()
    tree = CapacityTree(tmp_path, "fake", fake_worker(calls), "完整报告", "报告.md", [(source, "章节")])
    tree.run()
    assert all(calls.count(text) == 1 for text in before)


def test_semantic_failure_does_not_exponentially_split_or_reset_budget(tmp_path, limits):
    source = tmp_path / "source.md"
    source.write_text("完整长原文" * 300, encoding="utf-8")
    calls = []
    def invalid(command, root, kind, pack, output, **kwargs):
        calls.append(1)
        save_json(output, {"difficulty": "很难"})
        return 0
    tree = CapacityTree(tmp_path, "fake", invalid, "契约", "报告.md", [(source, "原文")])
    for _ in range(2):
        with pytest.raises(TreeFailure) as failure:
            tree.run()
        assert failure.value.code == REPAIR_EXHAUSTED
    assert len(calls) == 3


def test_provider_capacity_splits_leaf_without_becoming_cancellation(tmp_path, limits):
    source = tmp_path / "source.md"
    source.write_text("甲" * 1000, encoding="utf-8")
    calls = []
    normal = fake_worker(calls)
    def limited(command, root, kind, pack, output, **kwargs):
        prompt = pack.read_text(encoding="utf-8")
        span = re.search(r"字符区间：(\d+)-(\d+)", prompt)
        if span and int(span[2]) - int(span[1]) > 350:
            return CONTEXT_EXCEEDED
        return normal(command, root, kind, pack, output, **kwargs)
    tree = CapacityTree(tmp_path, "fake", limited, "契约", "报告.md", [(source, "原文")])
    tree.run()
    assert calls
    assert any(part["end"] - part["start"] <= 350 for part in tree.coverage)


def test_output_truncation_is_not_recorded_as_split_evidence(tmp_path, limits):
    """输出被截断说明输出额度不足；拆分材料无用，因此不得记为继续分包的证据。"""
    source = tmp_path / "source.md"
    source.write_text("甲" * 400, encoding="utf-8")

    def worker(command, root, kind, pack, output, **options):
        return OUTPUT_TRUNCATED

    tree = CapacityTree(tmp_path, "fake", worker, "契约", "报告.md", [(source, "原文")])
    with pytest.raises(TreeFailure) as failure:
        tree.run()
    assert failure.value.code == OUTPUT_TRUNCATED
    ledger = json.loads(next((tmp_path / "质量治理/容量分层").rglob("state.json")).read_text(encoding="utf-8"))
    assert not ledger.get("split_required")
    assert ledger["attempts"] == 0  # 输出截断不是语义失败，不消耗该节点的策略预算


def test_json_merge_preserves_mentions_and_rejects_scalar_conflicts():
    value = merge_json({"人物提及": [{"人物": "甲", "章节": "第001章"}]},
                       {"人物提及": [{"人物": "甲", "章节": "第002章"}]})
    assert len(value["人物提及"]) == 2
    with pytest.raises(ValueError):
        merge_json({"年龄": 12}, {"年龄": 13})


def test_markdown_fences_are_not_reinterpreted_as_sections():
    original = "# 报告\n\n## 分析\n```markdown\n# 示例标题\n```\n正文和证据。"
    result = merge_markdown([original, "# 报告\n\n## 分析\n后续证据。"])
    assert "```markdown\n# 示例标题\n```\n正文和证据。" in result
    assert "后续证据。" in result


def test_delta_fragments_keep_original_chapter_and_exact_identity():
    from bounded_generation import combine_delta
    a = {"章节": "0008", "新增元素": {"角色集": [{"名称": "萧炎", "详情": {"提取理由": "首段证据"}}]}, "修改元素": {}}
    b = {"章节": "0008", "新增元素": {}, "修改元素": {"角色集": [{"名称": "萧炎", "详情": {"提取理由": "末段证据"}}]}}
    result = combine_delta([a, b], {}, 8)
    assert result["修改元素"]["角色集"] == []
    assert len(result["新增元素"]["角色集"]) == 1
    assert result["新增元素"]["角色集"][0]["详情"]["提取理由"] == "首段证据\n末段证据"
    b["章节"] = "0009"
    with pytest.raises(ValueError):
        combine_delta([a, b], {}, 8)


def test_report_tree_keeps_full_content_and_json_format(tmp_path, limits):
    from bounded_generation import run_hierarchical_reports
    source = tmp_path / "partial.md"
    source.write_text("已有分析证据" * 500, encoding="utf-8")
    pack = tmp_path / "reduce.md"
    pack.write_text("完整汇总任务", encoding="utf-8")
    save_json(pack.with_suffix(".hierarchy.json"), {"contract": "保持所有证据章节", "sources": [str(source)]})
    outputs = [tmp_path / "报告.md", tmp_path / "人物提及.json"]
    counts = []
    def worker(command, root, kind, task, output, **kwargs):
        text = task.read_text(encoding="utf-8")
        if review_response(text):
            save_json(output, review_response(text))
            return 0
        span = re.search(r"字符区间：(\d+)-(\d+)", text)
        content = None
        if span:
            counts.append(span[0])
            content = {"人物提及": [{"人物": "原人物", "证据": span[0]}]} if "本次目标：人物提及.json" in text else "# 报告\n\n## 分析\n" + span[0]
        save_json(output, {"overview": "完整证据见分片位置", "content": content})
        return 0
    assert run_hierarchical_reports("fake", tmp_path, pack, outputs, worker) == 0
    report = outputs[0].read_text(encoding="utf-8")
    mentions = json.loads(outputs[1].read_text(encoding="utf-8"))["人物提及"]
    assert len(mentions) > 1
    assert all(item["证据"] in report for item in mentions)
    count = len(counts)
    assert run_hierarchical_reports("fake", tmp_path, pack, outputs, worker) == 0
    assert len(counts) == count


def test_failed_second_report_does_not_replace_first(tmp_path, limits):
    from bounded_generation import run_hierarchical_reports
    source = tmp_path / "partial.md"
    source.write_text("完整证据", encoding="utf-8")
    pack = tmp_path / "reduce.md"
    save_json(pack.with_suffix(".hierarchy.json"), {"contract": "原格式", "sources": [str(source)]})
    outputs = [tmp_path / "first.md", tmp_path / "second.md"]
    for output in outputs:
        output.write_text("正式原报告", encoding="utf-8")
    def worker(command, root, kind, task, output, **kwargs):
        second = "本次目标：second.md" in task.read_text(encoding="utf-8")
        save_json(output, {"overview": "来源索引", "content": None if second else "# 合格报告\n全文"})
        return 0
    assert run_hierarchical_reports("fake", tmp_path, pack, outputs, worker) == REPAIR_EXHAUSTED
    assert all(output.read_text(encoding="utf-8") == "正式原报告" for output in outputs)


def test_transport_capacity_code_is_not_reported_as_user_cancel(tmp_path, monkeypatch):
    import run_pipeline as pipeline
    pack = tmp_path / "task.md"
    pack.write_text("小任务包", encoding="utf-8")
    monkeypatch.setattr(pipeline, "stream_shell_command", lambda *a: (64, "超出提供方容量"))
    assert pipeline.run_worker("fake", tmp_path, "analysis", pack, tmp_path / "out.md") == 64
    assert pipeline.run_multi_output_worker("fake", tmp_path, "report", pack, [tmp_path / "out.md"]) == 64


def test_oversized_repair_evidence_is_paged_without_changing_patch_contract(tmp_path, limits):
    from bounded_generation import run_paged_recovery
    source = tmp_path / "evidence.md"
    source.write_text("完整原文证据" * 1000, encoding="utf-8")
    pack = tmp_path / "repair.md"
    pack.write_text("只修复 /修改元素/角色集/0/详情/提取理由，保持 base_sha256。", encoding="utf-8")
    save_json(pack.with_suffix(".md.sources.json"), [str(source)])
    output = tmp_path / "patch.json"
    calls = []
    def worker(command, root, kind, task, result, chapter_seq=0, **kwargs):
        calls.append(kind)
        if kind == "bounded_fragment":
            save_json(result, {"overview": "原文证据位于材料原件；不能把待修值当事实", "content": None})
        else:
            assert kind == "recovery_patch"
            assert "/修改元素/角色集/0/详情/提取理由" in task.read_text(encoding="utf-8")
            assert chapter_seq == 8
            save_json(result, {"base_sha256": "a" * 64, "changes": [{"path": "/修改元素/角色集/0/详情/提取理由", "value": "原文依据"}]})
        return 0
    assert run_paged_recovery(worker, "fake", tmp_path, "recovery_patch", pack, output, 8) == 0
    assert calls.count("bounded_fragment") > 1 and calls[-1] == "recovery_patch"
    assert json.loads(output.read_text(encoding="utf-8"))["base_sha256"] == "a" * 64


def test_conflicting_scalar_uses_scoped_model_decision(tmp_path, limits):
    source = tmp_path / "source.md"
    source.write_text("人物在本章过生日，年龄从12变为13。", encoding="utf-8")
    calls = []
    def worker(command, root, kind, task, result, **kwargs):
        calls.append(task.read_text(encoding="utf-8"))
        save_json(result, {"overview": "原文生日后的年龄", "content": {"choice": "right", "reason": "原文明确生日后为13岁"}})
        return 0
    tree = CapacityTree(tmp_path, "fake", worker, "原JSON契约", "delta.json", [(source, "原文")])
    assert merge_json({"年龄": 12}, {"年龄": 13}, resolve=tree.resolve) == {"年龄": 13}
    assert len(calls) == 1 and "left=12" in calls[0] and "right=13" in calls[0]


def test_unknown_modified_identity_is_not_silently_reclassified_as_new():
    from bounded_generation import combine_delta
    patch = {"章节": "0007", "新增元素": {}, "修改元素": {"线索集": [
        {"名称": "双重地下城与最终BOSS", "详情": {"提取理由": "本章有变化"}}]}}
    result = combine_delta([patch], {"线索集": [{"名称": "双层地下城与最终BOSS"}]}, 7)
    assert result["新增元素"]["线索集"] == []
    assert result["修改元素"]["线索集"][0]["名称"] == "双重地下城与最终BOSS"


def test_fragment_native_schema_keeps_internal_envelope_out_of_final_contract():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tools/agent_worker.py"
    spec = importlib.util.spec_from_file_location("fragment_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = json.loads(module.structured_output_schema("bounded_fragment", [Path("result.json")]))
    assert schema["required"] == ["overview", "content"]
    assert schema["additionalProperties"] is False


def test_long_truncated_output_requests_split_once_and_remembers_decision(tmp_path, limits):
    source = tmp_path / "source.md"
    source.write_text("完整原件", encoding="utf-8")
    calls = []
    def truncated(command, root, kind, task, result, **kwargs):
        calls.append(1)
        result.write_text('{"overview":"' + "中文" * 500, encoding="utf-8")
        return 1
    tree = CapacityTree(tmp_path, "fake", truncated, "契约", "报告.md", [(source, "原文")])
    for _ in range(2):
        with pytest.raises(TreeFailure) as failure:
            tree.request("相同材料", "输出截断测试")
        assert failure.value.code == CONTEXT_EXCEEDED
    assert len(calls) == 1
