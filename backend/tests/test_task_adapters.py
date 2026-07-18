"""Workbench task adapter tests.

These tests assert the six adapters produced by backend/task_adapters.py:

- analysis forwards task / chapters (all, range, discrete) / targets / question /
  original-text policy / force and produces a real manifest + pack artifacts.
- chapter_visual / chapter_structure touch only the requested seq values and their
  per-chapter outputs match the engine helpers; the visual aggregate target_outputs
  match the registered TASKS outputs.
- chapter_analysis_rerun / chapter_delta_rerun regenerate task packs only for the
  requested seqs and honor the force flag.
- export selects process / final source and forwards single-file, Dataview,
  Canvas, clean, and filename mode to export_obsidian_markdown.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from task_adapters import (
    AnalysisAdapter,
    ChapterAnalysisRerunAdapter,
    ChapterDeltaRerunAdapter,
    ChapterStructureAdapter,
    ChapterVisualAdapter,
    ExportAdapter,
)
from workbench import TaskContext


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


SAMPLE_STORY = {
    "介绍": {"标题": "火星", "描述": "适配器测试故事结构。"},
    "角色集": [{
        "名称": "约翰娜",
        "是否主角": False,
        "性别": 1,
        "年龄": 0,
        "生日": "0001-01-01T00:00:00",
        "所属阵营": ["奥尔森多家族"],
        "关系": ["弟弟:杰弗里"],
        "分组": "奥尔森多家族",
        "别名": ["约翰娜"],
        "标签集": ["温柔"],
        "介绍": "奥尔森多家族成员。",
        "详情": {"首次章节": "0001", "最近章节": "0002"},
    }],
    "事件集": [{
        "名称": "兄弟理念对立",
        "发生地点": "火星长城",
        "参与成员": ["约翰娜", "杰弗里"],
        "重量级": 70,
        "目标事件": [],
        "分组": "00000010-兄弟理念对立",
        "时间": "0001-01-01T00:00:00",
        "别名": [],
        "标签集": ["家族理念+冲突铺垫"],
        "介绍": "姐弟围绕是否卷入战争产生理念分歧。",
        "详情": {"涉及章节": "0001"},
    }],
    "地点集": [{
        "名称": "火星长城",
        "父级地点": "",
        "分组": "火星",
        "别名": [],
        "标签集": ["军事边境"],
        "介绍": "火星上重要的战争防线。",
        "详情": {"首次章节": "0001", "最近章节": "0001"},
    }],
    "线索集": [],
    "阵营集": [{
        "名称": "奥尔森多家族",
        "父级阵营": "",
        "座落地点": "火星长城",
        "分组": "家族",
        "别名": [],
        "标签集": ["家族势力"],
        "介绍": "卷入火星危机的家族。",
        "详情": {"首次章节": "0001", "最近章节": "0001"},
    }],
    "物品集": [],
    "其他事项集": [],
}


def _progress(*args, **kwargs):
    pass


def _make_context(project_path: Path, params: dict) -> TaskContext:
    return TaskContext(
        task_id=1,
        project_id=1,
        project_path=Path(project_path),
        params=params,
        cancellation_event=threading.Event(),
        _progress_callback=_progress,
    )


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "novel"
    (p / "原文拆解").mkdir(parents=True)
    (p / "章节处理" / "_任务包").mkdir(parents=True)
    (p / "全书分析" / "_任务包").mkdir(parents=True)
    chapters = [(1, "序章"), (2, "相遇"), (3, "冲突")]
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
    _write_json(p / "故事结构_增量.json", SAMPLE_STORY)
    _write_json(p / "故事结构.json", SAMPLE_STORY)
    return p


# --- AnalysisAdapter ------------------------------------------------------

def test_analysis_adapter_passes_task_chapters_targets_question_original_and_force(project):
    adapter = AnalysisAdapter()
    params = {
        "task": "summary",
        "chapters": "all",
        "targets": ["角色:林婉"],
        "question": "谁是主角？",
        "include_original": "none",
        "force": True,
    }
    ctx = _make_context(project, params)
    prepared = adapter.prepare(ctx)
    result = adapter.run(ctx, prepared)

    manifest = result["manifest"]
    assert manifest["task"] == "summary"
    assert manifest["chapter_range"] == "all"
    assert manifest["chapters"] == [1, 2, 3]
    assert manifest["targets"] == ["角色:林婉"]
    assert manifest["question"] == "谁是主角？"
    assert manifest["include_original"] == "none"

    # Adapter records the forwarded force flag.
    assert result["force"] is True
    assert result["task"] == "summary"
    assert result["chapters"] == "all"

    # Manifest and pack artifacts land in the real project dir.
    manifest_path = Path(result["manifest_path"])
    assert manifest_path.is_file()
    assert manifest_path.parent.parent == (project / "全书分析" / "_任务包")
    for art in result["artifacts"]:
        assert Path(art["path"]).is_file()

    # Revision is captured.
    assert "chapter" in result["revision"]
    assert "process" in result["revision"]
    assert "final" in result["revision"]


def test_analysis_adapter_range_chapters(project):
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "1-2", "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["chapter_range"] == "1-2"
    assert result["manifest"]["chapters"] == [1, 2]


def test_analysis_adapter_discrete_chapters(project):
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "1,3", "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["chapter_range"] == "1,3"
    assert result["manifest"]["chapters"] == [1, 3]


def test_analysis_adapter_original_text_policy_full(project):
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "all", "include_original": "full"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["include_original"] == "full"
    assert result["include_original"] == "full"


# --- ChapterVisualAdapter -------------------------------------------------

def test_chapter_visual_adapter_touches_only_requested_seqs(project):
    adapter = ChapterVisualAdapter()
    params = {"seqs": [1, 3], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["task"] == "visual_assets"
    assert result["mode"] == "per_chapter"
    touched = sorted(record["seq"] for record in result["manifest"]["packs"])
    assert touched == [1, 3]

    # The pack md files for the requested seqs exist on disk; seq 2 has none.
    pack_dir = Path(result["manifest_path"]).parent
    names = sorted(p.name for p in pack_dir.glob("pack_ch*.md"))
    assert names == ["pack_ch001.md", "pack_ch003.md"]


def test_chapter_visual_adapter_per_chapter_outputs_match_engine_helper(project):
    from run_pipeline import visual_assets_per_chapter_relpaths

    adapter = ChapterVisualAdapter()
    params = {"seqs": [1], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    record = result["manifest"]["packs"][0]
    assert record["outputs"] == visual_assets_per_chapter_relpaths(1)


def test_chapter_visual_adapter_aggregate_outputs_match_engine_helper(project):
    from analysis_context_pack import TASKS

    # The aggregate pack requires per-chapter visual asset files to exist.
    for seq in (1, 2, 3):
        d = project / "全书分析" / "视觉资产" / "分章" / f"ch{seq:03d}"
        d.mkdir(parents=True, exist_ok=True)
        for name in (
            "视觉资产清单.md",
            "关键场景分镜表.md",
            "AI绘图提示词素材.md",
            "角色外观一致性表.md",
            "场景氛围表.md",
        ):
            (d / name).write_text(f"# {name}\n内容\n", encoding="utf-8")

    adapter = ChapterVisualAdapter()
    params = {"seqs": [1, 2, 3], "aggregate": True, "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["task"] == "visual_assets"
    assert result["mode"] == "aggregate"
    assert result["target_outputs"] == TASKS["visual_assets"]["outputs"]
    assert Path(result["manifest_path"]).is_file()


# --- ChapterStructureAdapter ----------------------------------------------

def test_chapter_structure_adapter_touches_only_requested_seqs(project):
    adapter = ChapterStructureAdapter()
    params = {"seqs": [2], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["task"] == "chapter_structure"
    assert result["mode"] == "per_chapter"
    touched = sorted(record["seq"] for record in result["manifest"]["packs"])
    assert touched == [2]


def test_chapter_structure_adapter_per_chapter_outputs_match_engine_helper(project):
    from run_pipeline import chapter_structure_per_chapter_relpaths

    adapter = ChapterStructureAdapter()
    params = {"seqs": [1, 2], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    for record in result["manifest"]["packs"]:
        seq = record["seq"]
        assert record["outputs"] == chapter_structure_per_chapter_relpaths(seq)


# --- ChapterAnalysisRerunAdapter (writer) ---------------------------------

def test_chapter_analysis_rerun_adapter_regenerates_requested_seqs(project):
    adapter = ChapterAnalysisRerunAdapter()
    params = {"seqs": [1, 3], "force": True}
    ctx = _make_context(project, params)
    result = adapter.run(ctx)

    assert result["task"] == "chapter_analysis_rerun"
    assert result["seqs"] == [1, 3]
    assert result["force"] is True
    touched = sorted(entry["seq"] for entry in result["manifest"]["per_seq"])
    assert touched == [1, 3]
    for entry in result["manifest"]["per_seq"]:
        assert Path(entry["analysis_task"]).is_file()
        # The analysis task pack path follows the engine's stable layout.
        assert f"task_ch{entry['seq']:03d}_analysis.md" in entry["analysis_task"]


def test_chapter_analysis_rerun_adapter_honors_force_false(project):
    adapter = ChapterAnalysisRerunAdapter()
    # First run with force=True to generate the pack.
    adapter.run(_make_context(project, {"seqs": [1], "force": True}))
    # Second run with force=False: the existing pack is kept (rc=0).
    result = adapter.run(_make_context(project, {"seqs": [1], "force": False}))
    assert result["force"] is False
    assert Path(result["manifest"]["per_seq"][0]["analysis_task"]).is_file()


# --- ChapterDeltaRerunAdapter (writer) ------------------------------------

def test_chapter_delta_rerun_adapter_regenerates_requested_seqs(project):
    adapter = ChapterDeltaRerunAdapter()
    params = {"seqs": [1], "force": True}
    ctx = _make_context(project, params)
    result = adapter.run(ctx)

    assert result["task"] == "chapter_delta_rerun"
    assert result["seqs"] == [1]
    assert result["force"] is True
    entry = result["manifest"]["per_seq"][0]
    assert Path(entry["delta_task"]).is_file()
    assert f"task_ch001_delta.md" in entry["delta_task"]


def test_chapter_delta_rerun_adapter_honors_force_false(project):
    adapter = ChapterDeltaRerunAdapter()
    adapter.run(_make_context(project, {"seqs": [1], "force": True}))
    result = adapter.run(_make_context(project, {"seqs": [1], "force": False}))
    assert result["force"] is False
    assert Path(result["manifest"]["per_seq"][0]["delta_task"]).is_file()


# --- ExportAdapter --------------------------------------------------------

def test_export_adapter_selects_process_source_and_forwards_options(project, tmp_path):
    adapter = ExportAdapter()
    out_dir = tmp_path / "vault"
    single_file = tmp_path / "故事结构总览.md"
    params = {
        "source": "process",
        "out_dir": str(out_dir),
        "single_file": str(single_file),
        "include_dataview": True,
        "include_canvas": True,
        "clean": True,
        "filename_mode": "type-name",
    }
    ctx = _make_context(project, params)
    prepared = adapter.prepare(ctx)
    result = adapter.run(ctx, prepared)

    # The source resolved to 故事结构_增量.json (process).
    assert "故事结构_增量.json" in result["manifest"]["source_file"]
    assert result["source"] == "process"

    # All forwarded options are reflected in the export report.
    assert result["manifest"]["single_file"] == str(single_file)
    assert result["manifest"]["dataview_enabled"] is True
    assert result["manifest"]["canvas_enabled"] is True
    assert result["manifest"]["filename_mode"] == "type-name"

    # Artifacts exist on disk.
    assert (out_dir / "_meta" / "export_report.json").is_file()
    assert (out_dir / "00-总览" / "故事总览.md").is_file()
    assert (out_dir / "_dataview" / "查询-主角.md").is_file()
    assert (out_dir / "00-总览" / "故事图谱.canvas").is_file()
    assert single_file.is_file()

    # type-name filename mode produces type-prefixed object pages.
    assert (out_dir / "角色" / "角色-约翰娜.md").is_file()


def test_export_adapter_selects_final_source_by_default(project, tmp_path):
    adapter = ExportAdapter()
    out_dir = tmp_path / "vault-final"
    params = {
        "out_dir": str(out_dir),
        "clean": True,
    }
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["source"] == "final"
    assert "故事结构.json" in result["manifest"]["source_file"]
    assert "故事结构_增量.json" not in result["manifest"]["source_file"]
    # Dataview / canvas default off when not requested.
    assert result["manifest"]["dataview_enabled"] is False
    assert result["manifest"]["canvas_enabled"] is False
    assert result["manifest"]["filename_mode"] == "name"


def test_export_adapter_clean_wipes_existing_vault(project, tmp_path):
    out_dir = tmp_path / "vault-clean"
    out_dir.mkdir(parents=True)
    (out_dir / "stale.md").write_text("stale", encoding="utf-8")

    adapter = ExportAdapter()
    params = {"source": "final", "out_dir": str(out_dir), "clean": True}
    ctx = _make_context(project, params)
    adapter.run(ctx, adapter.prepare(ctx))

    # Stale file is gone after a clean export.
    assert not (out_dir / "stale.md").exists()
    assert (out_dir / "00-总览" / "故事总览.md").is_file()
