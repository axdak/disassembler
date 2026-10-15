"""范围由宿主决定；大书仍可按需查证完整冻结档案，旧父周期不能跳审。"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

import run_pipeline as pipeline
import generation_contract as contract
from story_schema_rules import DEFAULTS, make_empty_structure
from test_audit_pack import write_completed_chapter, write_json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worker_context import snapshot_context, ContextReader
from local_json_patch import apply_scoped_patch, delta_repair_scope


def test_large_baseline_delta_can_query_complete_frozen_story(tmp_path):
    story = make_empty_structure()
    role = copy.deepcopy(DEFAULTS["角色集"])
    role.update(名称="历史人物", 介绍="完整历史" * 3000)
    role["详情"] = {"首次章节": "0001", "最近章节": "0001", "特殊秘密": "用于查证的唯一内容"}
    story["角色集"] = [role]
    write_json(tmp_path / "故事结构_增量.json", story)
    chapter = write_completed_chapter(tmp_path, 2)
    assert pipeline.prepare_chapter_delta(tmp_path, chapter, full_story_kb=1) == 0
    pack = pipeline.artifact_paths(tmp_path, chapter)["delta_task"]
    directory = tmp_path / "frozen"
    directory.mkdir()
    manifest = snapshot_context(tmp_path, pack, [], directory, inline_prompt=pack.read_text(encoding="utf-8"))
    reader = ContextReader(manifest)
    result = reader.story_get_units([{"collection": "角色集", "name": "历史人物"}], level="L2")
    assert "用于查证的唯一内容" in json.dumps(result, ensure_ascii=False)
    # 运行时实际注册对应工具，不能只提供不可调用的工具说明。
    import agent_worker
    assert "delta" in agent_worker.BROAD_READ_TASKS


def scope_errors(data, **kwargs):
    assert hasattr(contract, "generation_scope_errors"), "新增宿主范围验收缺失"
    return contract.generation_scope_errors(data, **kwargs)


def test_delta_top_chapter_and_event_source_must_match_dispatch():
    data = {"章节": "0009", "新增元素": {}, "修改元素": {}}
    assert any("/章节" in error for error in scope_errors(data, chapter_seq=1))
    data["章节"] = "0001"
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="事件", 详情={"涉及章节": "0009", "提取理由": "错误来源"})
    data["新增元素"]["事件集"] = [event]
    assert any("涉及章节" in error for error in scope_errors(data, chapter_seq=1))
    event["详情"]["涉及章节"] = "0001"
    assert scope_errors(data, chapter_seq=1) == []


def test_periodic_no_change_cannot_claim_other_or_incomplete_chapters():
    data = {"章节范围": "第001章-第005章", "新增元素": {}, "修改元素": {}, "治理类型": "no_change",
            "质量说明": "无需修改", "证据范围": ["第999章"]}
    assert scope_errors(data, start=1, end=5)
    data["证据范围"] = [f"第{i:03d}章" for i in range(1, 6)]
    assert scope_errors(data, start=1, end=5) == []
    data["章节范围"] = "第999章-第999章"
    assert scope_errors(data, start=1, end=5)


def test_legacy_parent_status_requires_cross_range_review(tmp_path, monkeypatch):
    paths = pipeline.audit_paths(tmp_path, 1, 5)
    write_json(paths["status"], {"status": "covered_by_children", "child_ranges": [
        {"start": 1, "end": 2}, {"start": 3, "end": 5}]})
    monkeypatch.setattr(pipeline, "latest_snapshot_seq", lambda *_: 5)
    seen = []
    monkeypatch.setattr(pipeline, "run_child_periodic_governance", lambda *args: seen.append(args[3]) or 2)
    assert pipeline.run_periodic_governance(tmp_path, 1, 5) == 2
    assert seen == [[(1, 2), (3, 5)]]


def test_unresolved_scan_retains_legacy_parent_without_cross_range_receipt(tmp_path):
    paths = pipeline.audit_paths(tmp_path, 1, 5)
    write_json(paths["status"], {"status": "covered_by_children", "range": "001-005",
                               "child_ranges": [{"start": 1, "end": 2}, {"start": 3, "end": 5}]})
    assert [(item["_start"], item["_end"]) for item in pipeline.unresolved_audits(tmp_path)] == [(1, 5)]


@pytest.mark.parametrize("field,bad,good,host", [
    ("章节", "0099", "0001", {"chapter_seq": 1}),
    ("章节范围", "第099章-第100章", "第001章-第002章", {"start": 1, "end": 2}),
    ("证据范围", ["第099章"], ["第001章", "第002章"], {"start": 1, "end": 2}),
])
def test_scope_metadata_error_repairs_only_that_field(tmp_path, field, bad, good, host):
    """来源元数据笔误复用字段补丁，不能触发整份事实重新生成。"""
    data = {"章节": "0001", "章节范围": "第001章-第002章", "证据范围": ["第001章", "第002章"],
            "新增元素": {}, "修改元素": {}, "质量说明": "原有事实和说明不得改写"}
    data[field] = bad
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    errors = scope_errors(data, **host)
    scope = delta_repair_scope(source, "\n".join(errors), make_empty_structure())
    assert set(scope) == {"/" + field}
    apply_scoped_patch(source, output, {"base_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "changes": [{"path": "/" + field, "value": good}]}, scope)
    data[field] = good
    assert json.loads(output.read_text(encoding="utf-8")) == data
    assert scope_errors(data, **host) == []
