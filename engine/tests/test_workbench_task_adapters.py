#!/usr/bin/env python3
"""Scoped engine entry points used by workbench task adapters.

These tests exercise the narrow functions added to run_pipeline.py for the
workbench adapters. They assert the scoped functions:

- forward task / chapters / targets / question / original-text policy / force
  to analysis_context_pack and produce a matching manifest;
- touch only the requested seq values for per-chapter visual / structure packs;
- produce per-chapter outputs that match the engine helpers, and aggregate
  outputs that match the registered TASKS outputs;
- regenerate chapter analysis / delta task packs only for the requested seqs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis_context_pack import (  # noqa: E402
    TASKS,
    chapter_structure_per_chapter_outputs,
    visual_assets_per_chapter_outputs,
)
from run_pipeline import (  # noqa: E402
    artifact_paths,
    chapter_by_seq,
    copy_analysis_task_packages,
    latest_analysis_manifest,
    load_json,
    scoped_analysis_pack,
    scoped_chapter_analysis_rerun,
    scoped_chapter_delta_rerun,
    scoped_chapter_structure_pack,
    scoped_chapter_visual_aggregate,
    scoped_chapter_visual_pack,
    snapshot_project_inputs,
)


VALID_ANALYSIS = """# 第001章分析

## 1. 剧情梗概
本章围绕主角接到意外来信后前往约定地点展开。来信暴露了旧日承诺的漏洞，主角在犹豫后决定当面求证，结尾以陌生人出现留下新的悬念。

## 2. 出场人物
- 主角：阅读来信后从迟疑转为主动求证。

## 3. 核心冲突
主角需要在相信旧承诺和面对新证据之间作出选择。

## 4. 信息增量
来信说明旧日承诺存在未公开的条件。

## 5. 伏笔与悬念
陌生人的身份及其掌握的信息待后续揭示。

## 6. 爽点 / 虐点 / 情绪点
主角主动追查带来期待与紧张。

## 7. 章节功能判断
本章承担冲突启动与悬念铺垫功能。

## 8. 事件分组与标签建议
来信求证承诺线冲突启动主动追查身份悬念。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 |
|----------|----------|----------|
| V01 | 主角阅读来信 | 中 |

## 10. 结构提取提示
应提取来信事件、约定地点与陌生人线索。
"""


def _empty_story() -> dict:
    return {
        "介绍": {"标题": "适配器测试故事", "描述": "scoped engine entry point tests"},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [],
        "其他事项集": [],
    }


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "novel"
    (p / "原文拆解").mkdir(parents=True)
    (p / "章节处理" / "_任务包").mkdir(parents=True)
    (p / "全书分析" / "_任务包").mkdir(parents=True)
    chapters = [
        (1, "序章"),
        (2, "相遇"),
        (3, "冲突"),
    ]
    _write_json(p / "原文拆解" / "_索引.json", {"chapters": [
        {"seq": seq, "filename": f"第{seq:03d}章_{name}.md", "title": name}
        for seq, name in chapters
    ]})
    for seq, name in chapters:
        (p / "原文拆解" / f"第{seq:03d}章_{name}.md").write_text(
            f"# 第{seq:03d}章 {name}\n原文内容\n", encoding="utf-8"
        )
        (p / "章节处理" / f"第{seq:03d}章_{name}.md").write_text(
            VALID_ANALYSIS.replace("001", f"{seq:03d}"), encoding="utf-8"
        )
    _write_json(p / "故事结构_增量.json", _empty_story())
    _write_json(p / "故事结构.json", _empty_story())
    return p


# --- scoped_analysis_pack -------------------------------------------------

def test_scoped_analysis_pack_forwards_all_chapters_targets_question_and_original(project):
    result = scoped_analysis_pack(
        project,
        task="summary",
        chapters="all",
        targets=["角色:林婉"],
        question="谁是主角？",
        include_original="none",
        force=True,
    )
    manifest_path = Path(result["manifest_path"])
    assert manifest_path.is_file()
    manifest = result["manifest"]
    assert manifest["task"] == "summary"
    assert manifest["chapter_range"] == "all"
    assert manifest["chapters"] == [1, 2, 3]
    assert manifest["targets"] == ["角色:林婉"]
    assert manifest["question"] == "谁是主角？"
    assert manifest["include_original"] == "none"
    # The pack files referenced by the manifest exist on disk.
    for rel in manifest["packs"]:
        assert (project / rel).is_file()


def test_scoped_analysis_pack_range_chapters(project):
    result = scoped_analysis_pack(project, task="summary", chapters="1-2", include_original="none")
    assert result["manifest"]["chapters"] == [1, 2]
    assert result["manifest"]["chapter_range"] == "1-2"


def test_scoped_analysis_pack_discrete_chapters(project):
    result = scoped_analysis_pack(project, task="summary", chapters="1,3", include_original="none")
    assert result["manifest"]["chapters"] == [1, 3]
    assert result["manifest"]["chapter_range"] == "1,3"


def test_scoped_analysis_pack_target_outputs_match_task_definition(project):
    result = scoped_analysis_pack(project, task="characters", chapters="all", include_original="none")
    assert result["target_outputs"] == TASKS["characters"]["outputs"]


# --- scoped_chapter_visual_pack / aggregate -------------------------------

def test_scoped_chapter_visual_pack_touches_only_requested_seqs(project):
    result = scoped_chapter_visual_pack(project, seqs=[1, 3], include_original="none")
    assert result["task"] == "visual_assets"
    assert result["mode"] == "per_chapter"
    touched = sorted(record["seq"] for record in result["manifest"]["packs"])
    assert touched == [1, 3]
    # Pack files exist on disk for the touched seqs only.
    for record in result["manifest"]["packs"]:
        assert (project / record["pack"]).is_file()


def test_scoped_chapter_visual_pack_outputs_match_engine_helper(project):
    result = scoped_chapter_visual_pack(project, seqs=[1], include_original="none")
    record = result["manifest"]["packs"][0]
    assert record["outputs"] == visual_assets_per_chapter_outputs(1, "序章")
    assert record["outputs"] == [
        f"全书分析/视觉资产/分章/ch001_序章_{name}"
        for name in (
            "视觉资产清单.md",
            "关键场景分镜表.md",
            "AI绘图提示词素材.md",
            "角色外观一致性表.md",
            "场景氛围表.md",
        )
    ]


def test_scoped_chapter_visual_aggregate_outputs_match_engine_helper(project):
    # The aggregate pack requires per-chapter visual asset files to exist.
    titles = {1: "序章", 2: "相遇", 3: "冲突"}
    for seq in (1, 2, 3):
        d = project / "全书分析" / "视觉资产" / "分章"
        d.mkdir(parents=True, exist_ok=True)
        for name in (
            "视觉资产清单.md",
            "关键场景分镜表.md",
            "AI绘图提示词素材.md",
            "角色外观一致性表.md",
            "场景氛围表.md",
        ):
            (d / f"ch{seq:03d}_{titles[seq]}_{name}").write_text(f"# {name}\n内容\n", encoding="utf-8")
    result = scoped_chapter_visual_aggregate(project)
    assert result["task"] == "visual_assets"
    assert result["mode"] == "aggregate"
    assert result["target_outputs"] == TASKS["visual_assets"]["outputs"]
    assert Path(result["manifest_path"]).is_file()


# --- scoped_chapter_structure_pack -----------------------------------------

def test_scoped_chapter_structure_pack_touches_only_requested_seqs(project):
    result = scoped_chapter_structure_pack(project, seqs=[2], include_original="none")
    assert result["task"] == "chapter_structure"
    assert result["mode"] == "per_chapter"
    touched = sorted(record["seq"] for record in result["manifest"]["packs"])
    assert touched == [2]


def test_scoped_chapter_structure_pack_outputs_match_engine_helper(project):
    result = scoped_chapter_structure_pack(project, seqs=[1, 2], include_original="none")
    titles = {1: "序章", 2: "相遇", 3: "冲突"}
    for record in result["manifest"]["packs"]:
        seq = record["seq"]
        assert record["outputs"] == chapter_structure_per_chapter_outputs(seq, titles[seq])
        assert record["outputs"] == [f"全书分析/故事结构/分章/ch{seq:03d}_{titles[seq]}_章节结构.md"]


# --- scoped_chapter_analysis_rerun / delta_rerun --------------------------

def test_scoped_chapter_analysis_rerun_regenerates_task_packs_for_requested_seqs(project):
    result = scoped_chapter_analysis_rerun(project, seqs=[1, 3], force=True)
    assert result["task"] == "chapter_analysis_rerun"
    assert result["seqs"] == [1, 3]
    assert result["force"] is True
    touched = sorted(entry["seq"] for entry in result["per_seq"])
    assert touched == [1, 3]
    for entry in result["per_seq"]:
        assert Path(entry["analysis_task"]).is_file()
        assert entry["rc"] == 0


def test_scoped_chapter_analysis_rerun_skips_when_force_false_and_pack_exists(project):
    first = scoped_chapter_analysis_rerun(project, seqs=[1], force=True)
    second = scoped_chapter_analysis_rerun(project, seqs=[1], force=False)
    # With force=False the existing pack is reused; rc=0 means "already exists, skipped".
    assert second["per_seq"][0]["rc"] == 0
    assert Path(second["per_seq"][0]["analysis_task"]).is_file()
    # The task pack path is stable per seq.
    assert first["per_seq"][0]["analysis_task"] == second["per_seq"][0]["analysis_task"]


def test_scoped_chapter_delta_rerun_regenerates_task_packs_for_requested_seqs(project):
    result = scoped_chapter_delta_rerun(project, seqs=[1], force=True)
    assert result["task"] == "chapter_delta_rerun"
    assert result["seqs"] == [1]
    assert result["force"] is True
    entry = result["per_seq"][0]
    assert Path(entry["delta_task"]).is_file()
    assert entry["rc"] == 0


def test_scoped_chapter_delta_rerun_skips_when_force_false_and_pack_exists(project):
    scoped_chapter_delta_rerun(project, seqs=[1], force=True)
    second = scoped_chapter_delta_rerun(project, seqs=[1], force=False)
    assert second["per_seq"][0]["rc"] == 0
    assert Path(second["per_seq"][0]["delta_task"]).is_file()


# --- snapshot helpers ------------------------------------------------------

def test_snapshot_project_inputs_copies_chapter_material_and_story(project):
    snapshot = snapshot_project_inputs(project)
    try:
        assert (snapshot / "原文拆解" / "_索引.json").is_file()
        assert (snapshot / "原文拆解" / "第001章_序章.md").is_file()
        assert (snapshot / "章节处理" / "第001章_序章.md").is_file()
        assert (snapshot / "故事结构_增量.json").is_file()
        assert (snapshot / "故事结构.json").is_file()
    finally:
        import shutil
        shutil.rmtree(snapshot, ignore_errors=True)


def test_copy_analysis_task_packages_merges_packs_back(project, tmp_path):
    # Generate a pack in the snapshot, then copy back.
    snapshot = snapshot_project_inputs(project)
    try:
        scoped_analysis_pack(snapshot, task="summary", chapters="all", include_original="none")
        copy_analysis_task_packages(snapshot, project)
        manifest = latest_analysis_manifest(project, "summary")
        assert manifest is not None
        assert manifest.is_file()
        manifest_data = load_json(manifest, {})
        assert manifest_data["task"] == "summary"
    finally:
        import shutil
        shutil.rmtree(snapshot, ignore_errors=True)


def test_latest_analysis_manifest_finds_per_chapter_and_aggregate_suffixes(project):
    scoped_chapter_visual_pack(project, seqs=[1], include_original="none")
    assert latest_analysis_manifest(project, "visual_assets_per_chapter") is not None
    # No aggregate yet.
    assert latest_analysis_manifest(project, "visual_assets_aggregate") is None
