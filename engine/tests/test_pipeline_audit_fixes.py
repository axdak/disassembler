"""体检问题回归：真实提交入口、预算传播与契约失效，全部离线。"""
import json
from pathlib import Path

import pytest

import engine_contract
import run_pipeline as pipeline
import synopsis_contract
from merge_delta import save_json
from test_audit_pack import empty_story
from test_chronology import event


@pytest.mark.parametrize("multiple", [False, True])
def test_output_truncation_does_not_repeat_identical_request(tmp_path, monkeypatch, multiple):
    pack = tmp_path / "task.md"
    pack.write_text("完整任务材料", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pipeline, "wait_before_worker_retry", lambda _: False)
    monkeypatch.setattr(pipeline, "stream_shell_command", lambda *args: (calls.append("request") or 67, "truncated"))
    output = tmp_path / "output.json"
    runner = pipeline.run_multi_output_worker if multiple else pipeline.run_worker
    assert runner("offline", tmp_path, "delta", pack, [output] if multiple else output, retries=2) == 67
    assert len(calls) == 1
    assert not output.exists()


@pytest.mark.parametrize("filename", ["chronology.py", "element_identity.py"])
def test_semantic_dependencies_invalidate_submission_contract(tmp_path, filename):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dependency = scripts / filename
    dependency.write_text("old", encoding="utf-8")
    before = engine_contract.contract_version(tmp_path)
    dependency.write_text("new", encoding="utf-8")
    assert engine_contract.contract_version(tmp_path) != before


def test_governance_relocates_fields_without_losing_raw_patch(tmp_path):
    story = empty_story()
    story["事件集"] = [event("约定", "0001", "开篇")]
    save_json(tmp_path / "故事结构_增量.json", story)
    patch = {"章节范围": "第001章-第001章", "新增元素": {},
             "修改元素": {"事件集": [{"名称": "约定", "详情": {
                 "重量级": 9, "提取理由": "原文明确表现为关键转折", "原始证据": "独有原文保留"}}]}}
    target = tmp_path / "patch.json"
    save_json(target, patch)
    original = target.read_bytes()
    assert pipeline.commit_governance(tmp_path, str(target)) == 0
    result = json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8"))
    assert result["事件集"][0]["重量级"] == 9
    assert result["事件集"][0]["详情"]["原始证据"] == "独有原文保留"
    backups = list((tmp_path / "质量治理" / "按需治理").glob("original_input_*.json"))
    assert any(path.read_bytes() == original for path in backups)


def test_governance_unknown_modification_is_retained_on_repeated_failure(tmp_path):
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    item = {"名称": "未知角色", "详情": {"提取理由": "原文依据", "证据": "不能丢的原文"}}
    patch = {"章节范围": "第001章-第001章", "新增元素": {}, "修改元素": {"角色集": [item]}}
    target = tmp_path / "patch.json"
    save_json(target, patch)
    for _ in range(2):
        assert pipeline.commit_governance(tmp_path, str(target)) == 1
        assert json.loads(target.read_text(encoding="utf-8"))["修改元素"]["角色集"] == [item]
        assert not target.with_suffix(".commit.json").exists()
        assert "不能丢的原文" in target.with_suffix(".repair.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("root", [[], None, 7, "错误根节点"])
def test_non_object_governance_patch_still_enters_repair(tmp_path, root):
    """合法 JSON 的错误根类型必须生成修复上下文，不能在位置纠错层抛异常。"""
    save_json(tmp_path / "故事结构_增量.json", empty_story())
    story = pipeline.ensure_story(tmp_path)
    before = story.read_bytes()
    target = tmp_path / "patch.json"
    save_json(target, root)
    original = target.read_bytes()
    assert pipeline.commit_governance(tmp_path, str(target)) == 1
    assert target.with_suffix(".repair.md").is_file()
    assert not target.with_suffix(".commit.json").exists()
    assert story.read_bytes() == before
    assert any(path.read_bytes() == original for path in
               (tmp_path / "质量治理" / "按需治理").glob("original_input_*.json"))


def test_governance_repairs_legacy_supplementary_without_emptying_evidence(tmp_path):
    story = empty_story()
    story["事件集"] = [event("约定", "0001", "开篇")]
    raw = '["标签", {"原文":"独有证据"}, 7]'
    story["事件集"][0]["详情"]["补充标签"] = raw
    save_json(tmp_path / "故事结构_增量.json", story)
    patch = {"章节范围": "第001章-第001章", "新增元素": {}, "修改元素": {},
             "治理类型": "no_change", "质量说明": "逐项核对无新增治理", "证据范围": ["第001章"]}
    target = tmp_path / "patch.json"
    save_json(target, patch)
    assert pipeline.commit_governance(tmp_path, str(target)) == 0
    final = json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8"))
    assert raw in json.loads(final["事件集"][0]["详情"]["补充标签"])


def test_relocation_does_not_bypass_governance_immutable_event_fields(tmp_path):
    story = empty_story()
    story["事件集"] = [event("约定", "0001", "开篇")]
    save_json(tmp_path / "故事结构_增量.json", story)
    source = pipeline.ensure_story(tmp_path)
    original = source.read_bytes()
    patch = {"章节范围": "第001章-第001章", "新增元素": {}, "修改元素": {
        "事件集": [{"名称": "约定", "详情": {"时间": "0002-01-01T00:00:00", "提取理由": "不能擅改事实"}}]}}
    target = tmp_path / "patch.json"
    save_json(target, patch)
    assert pipeline.commit_governance(tmp_path, str(target)) == 1
    assert source.read_bytes() == original
    assert not target.with_suffix(".commit.json").exists()


def test_audit_prompt_uses_runtime_synopsis_contract(monkeypatch):
    monkeypatch.setattr(synopsis_contract, "SYNOPSIS_MAX_CHARS", 1234)
    monkeypatch.setattr(synopsis_contract, "SYNOPSIS_SECTIONS", ("甲", "乙", "丙", "丁"))
    text = pipeline.render_audit_prompt(start=1, end=1, correction_path="patch.json",
        run_pipeline_path="run_pipeline.py", project_dir="project", chapters=[],
        full_story_json="", story_index="", relevant_details="", previous_correction=None,
        taxonomy_json="")
    assert "上限 1234 字" in text
    # The real periodic prompt must not narrow the existing factual audit to formatting.
    assert "只修正结构质量问题" not in text
    assert "同时核对事实与结构质量" in text
    assert "不能仅完成标签、分组和引用整理就认为内容已审计" in text
    assert "`【甲】`、`【乙】`、`【丙】`、`【丁】`" in text


def test_synopsis_change_invalidates_audit_prompt_cache(tmp_path, monkeypatch):
    import prompt_loader
    prompts = tmp_path / "prompts"
    (prompts / "流水线").mkdir(parents=True)
    (prompts / "流水线" / "周期审计.j2").write_text("same template", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dependency = scripts / "synopsis_contract.py"
    dependency.write_text("old", encoding="utf-8")
    # 该夹具模拟提示词的全部文件依赖，只有简介契约在本例发生变化。
    for filename in ("generation_contract.py", "story_schema_rules.py"):
        (scripts / filename).write_text("unchanged contract", encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(prompt_loader, "__file__", str(scripts / "prompt_loader.py"))
    before = prompt_loader.prompt_fingerprint("流水线/周期审计.j2")
    dependency.write_text("new", encoding="utf-8")
    assert prompt_loader.prompt_fingerprint("流水线/周期审计.j2") != before
