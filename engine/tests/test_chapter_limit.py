"""截止章对章节阶段与编排续跑点的约束。"""

import json

import analysis_context_pack
import orchestrator
import pytest
from orchestrator import Config, Orchestrator
from pipeline_state import PipelineState, STAGES
import run_pipeline


def _chapters(project, count=2):
    root = project / "原文拆解"
    root.mkdir(parents=True)
    for seq in range(1, count + 1):
        (root / f"第{seq:03d}章_测试.md").write_text("# 测试\n正文", encoding="utf-8")


def test_analysis_stops_at_inclusive_chapter_limit(tmp_path, monkeypatch):
    _chapters(tmp_path)
    first, second = run_pipeline.chapter_files(tmp_path)
    first_paths = run_pipeline.artifact_paths(tmp_path, first)
    second_paths = run_pipeline.artifact_paths(tmp_path, second)
    first_paths["analysis"].parent.mkdir(parents=True, exist_ok=True)
    first_paths["analysis"].write_text("已分析", encoding="utf-8")
    monkeypatch.setattr(run_pipeline, "ensure_story", lambda project: None)
    monkeypatch.setattr(run_pipeline, "validate_chapter_analysis_file", lambda path: (True, []))

    assert run_pipeline.cmd_run(tmp_path, phase="analysis", end_chapter_seq=1) == 0
    assert not second_paths["analysis_task"].exists()


def test_delta_finishes_scope_without_touching_next_chapter(tmp_path, monkeypatch):
    _chapters(tmp_path)
    second = run_pipeline.chapter_files(tmp_path)[1]
    monkeypatch.setattr(run_pipeline, "ensure_story", lambda project: None)
    monkeypatch.setattr(run_pipeline, "unresolved_audits", lambda project: [])
    monkeypatch.setattr(run_pipeline, "next_incomplete", lambda project: second)
    monkeypatch.setattr(run_pipeline, "run_final_partial_governance", lambda *args, **kwargs: 0)

    assert run_pipeline.cmd_run(tmp_path, end_chapter_seq=1) == 0
    assert not run_pipeline.artifact_paths(tmp_path, second)["analysis_task"].exists()


def test_changing_limit_rewinds_completed_pipeline(tmp_path):
    state = PipelineState.load(tmp_path)
    state.end_chapter_seq = 1
    state.fullbook_completed_tasks = ["summary"]
    for stage in STAGES:
        state.mark_completed(stage)
    state.save()

    orch = Orchestrator(tmp_path, config=Config(end_chapter_seq=2))
    orch._reconcile_chapter_scope()

    saved = json.loads((tmp_path / "质量治理" / "编排" / "状态.json").read_text(encoding="utf-8"))
    assert saved["current_stage"] == "chapter_analysis"
    assert saved["completed"] == []
    assert saved["fullbook_completed_tasks"] == []
    assert saved["end_chapter_seq"] == 2


def test_visual_aggregate_uses_only_selected_chapters(tmp_path, monkeypatch):
    split = tmp_path / "原文拆解"
    split.mkdir()
    (split / "_索引.json").write_text(json.dumps({"chapters": [
        {"seq": 1, "filename": "第001章_测试.md"},
        {"seq": 2, "filename": "第002章_测试.md"},
    ]}, ensure_ascii=False), encoding="utf-8")
    visuals = tmp_path / "全书分析" / "视觉资产" / "分章"
    visuals.mkdir(parents=True)
    for seq in (1, 2):
        (visuals / f"ch{seq:03d}.md").write_text("视觉产物", encoding="utf-8")
    monkeypatch.setattr(analysis_context_pack, "_visual_complete", lambda project, seq: True)
    monkeypatch.setattr(analysis_context_pack, "_existing_visual_paths",
                        lambda project, seq: [visuals / f"ch{seq:03d}.md"])

    assert analysis_context_pack.main([
        str(tmp_path), "--task", "visual_assets", "--aggregate", "--chapters", "1-1",
    ]) == 0
    manifests = list((tmp_path / "全书分析" / "_任务包").glob("*_visual_assets_aggregate/manifest.json"))
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["chapters_available"] == [1]
    assert manifest["chapters_missing"] == []


def test_limit_cannot_hide_already_committed_later_chapter(tmp_path, monkeypatch):
    _chapters(tmp_path)
    monkeypatch.setattr(orchestrator, "is_chapter_complete",
                        lambda project, chapter: run_pipeline.seq_from_file(chapter) == 2)
    orch = Orchestrator(tmp_path, config=Config(end_chapter_seq=1))

    with pytest.raises(ValueError, match="第002章已提交"):
        orch._reconcile_chapter_scope()
    assert orch.state.end_chapter_seq == 0
