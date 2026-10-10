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

def test_analysis_adapter_uses_global_pack_settings_as_defaults():
    parsed = AnalysisAdapter._parse_params({}, {
        "include_original": "none",
        "max_pack_chars": 43210,
        "story_summary_max_items": 25,
        "context_budget_chars": 98765,
        "worker_timeout_seconds": 77,
        "worker_retries": 4,
        "worker_window": "powershell",
    })

    assert parsed["include_original"] == "none"
    assert parsed["max_pack_chars"] == 43210
    assert parsed["story_summary_max_items"] == 25
    assert parsed["context_budget_chars"] == 98765
    assert parsed["worker_timeout_seconds"] == 77
    assert parsed["worker_retries"] == 4
    assert parsed["worker_window"] == "powershell"


def test_analysis_adapter_task_params_override_global_settings():
    parsed = AnalysisAdapter._parse_params(
        {"include_original": "full", "max_pack_chars": 12000},
        {"include_original": "none", "max_pack_chars": 43210},
    )

    assert parsed["include_original"] == "full"
    assert parsed["max_pack_chars"] == 12000

def _install_fake_workers(monkeypatch, calls=None):
    """Replace run_worker/run_multi_output_worker with fakes that write placeholder
    artifacts, so AnalysisAdapter can be exercised end-to-end without a real LLM.
    Returns the call log (list of tuples) so tests can assert the execution shape.
    """
    calls = [] if calls is None else calls

    def fake_run_worker(command, project_dir, task_type, task_pack, expected_output,
                        chapter_seq=0, timeout=0, retries=0, worker_window="hidden"):
        calls.append(("run_worker", task_type, str(task_pack), str(expected_output)))
        out = Path(expected_output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# 假分批结果 {task_type}\n", encoding="utf-8")
        return 0

    def fake_run_multi(command, project_dir, task_type, task_pack, expected_outputs,
                       chapter_seq=0, timeout=0, retries=0, worker_window="hidden"):
        calls.append(("run_multi", task_type, str(task_pack), [str(p) for p in expected_outputs]))
        for p in expected_outputs:
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# 假产出 {task_type} {p.name}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("task_adapters.run_worker", fake_run_worker)
    monkeypatch.setattr("task_adapters.run_multi_output_worker", fake_run_multi)
    return calls


def test_analysis_adapter_passes_task_chapters_targets_question_original_and_force(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {
        "task": "summary",
        "chapters": "all",
        "targets": ["角色:林婉"],
        "question": "谁是主角？",
        # Spec §3.3 canonical param name (the UI sends original_text).
        "original_text": "none",
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
    assert result["include_original"] == "none"

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

    # The canonical target output was produced by the (faked) worker.
    assert (project / "全书分析" / "剧情结构" / "章节梗概汇总.md").is_file()

    # Revision is captured.
    assert "chapter" in result["revision"]
    assert "process" in result["revision"]
    assert "final" in result["revision"]


def test_analysis_adapter_range_chapters(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "1-2", "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["chapter_range"] == "1-2"
    assert result["manifest"]["chapters"] == [1, 2]


def test_analysis_adapter_discrete_chapters(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "1,3", "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["chapter_range"] == "1,3"
    assert result["manifest"]["chapters"] == [1, 3]


def test_analysis_adapter_original_text_policy_full(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "all", "include_original": "full"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))
    assert result["manifest"]["include_original"] == "full"
    assert result["include_original"] == "full"


def test_analysis_adapter_single_pack_runs_worker_directly(project, monkeypatch):
    """单包：直接 run_multi_output_worker 产 target_outputs，不走 partial/reduce。"""
    calls = _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    ctx = _make_context(project, {"task": "summary", "chapters": "all", "include_original": "none"})
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["manifest"]["pack_count"] == 1
    assert [c for c in calls if c[0] == "run_worker"] == []
    multi = [c for c in calls if c[0] == "run_multi"]
    assert len(multi) == 1
    assert multi[0][1] == "fullbook_analysis"
    target = project / "全书分析" / "剧情结构" / "章节梗概汇总.md"
    assert target.is_file()
    assert any(a["path"] == str(target) for a in result["artifacts"])


def test_analysis_adapter_multi_pack_runs_partials_then_reduce(project, monkeypatch):
    """多包：逐包 run_worker 产 partial，再 run_multi_output_worker reduce。"""
    calls = _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    # Use real evidence volume to force multiple packs while leaving room for
    # the complete output contract; a 50-character total budget is invalid.
    for path in (project / "章节处理").glob("*.md"):
        path.write_text(path.read_text(encoding="utf-8") + "\n完整分包证据" * 1000, encoding="utf-8")
    params = {"task": "summary", "chapters": "all", "include_original": "none",
              "max_pack_chars": 8000}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    pack_count = result["manifest"]["pack_count"]
    assert pack_count > 1
    worker_calls = [c for c in calls if c[0] == "run_worker"]
    reduce_calls = [c for c in calls if c[0] == "run_multi"]
    assert len(worker_calls) == pack_count
    assert all(c[1] == "fullbook_analysis" for c in worker_calls)
    assert len(reduce_calls) == 1
    assert reduce_calls[0][1] == "fullbook_analysis_reduce"

    pack_dir = Path(result["manifest_path"]).parent
    for i in range(1, pack_count + 1):
        assert (pack_dir / f"partial_result_{i:03d}.md").is_file()
        assert (pack_dir / f"worker_pack_{i:03d}.md").is_file()
    assert (pack_dir / "worker_pack_reduce.md").is_file()
    assert (project / "全书分析" / "剧情结构" / "章节梗概汇总.md").is_file()


def test_analysis_adapter_without_worker_raises(project, monkeypatch):
    """有 target_outputs 但无 worker（provider=manual 且无 worker_command）-> RuntimeError。"""
    _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {"task": "summary", "chapters": "all", "include_original": "none",
              "worker_provider": "manual"}
    ctx = _make_context(project, params)
    with pytest.raises(RuntimeError, match="worker"):
        adapter.run(ctx, adapter.prepare(ctx))


def test_analysis_adapter_custom_skips_worker(project, monkeypatch):
    """custom 无 target_outputs -> 不调 worker，不 raise，保留只生成包的行为。"""
    calls = _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    params = {"task": "custom", "chapters": "all", "include_original": "none",
              "question": "分析主角成长线是否断裂"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert calls == []
    assert result["task"] == "custom"
    assert Path(result["manifest_path"]).is_file()


def test_analysis_adapter_structure_task_single_pack_no_reduce(project, monkeypatch):
    """结构类任务（plot）单包直出：一次 run_multi_output_worker，不调 run_worker（无 partial/reduce）。"""
    calls = _install_fake_workers(monkeypatch)
    adapter = AnalysisAdapter()
    ctx = _make_context(project, {"task": "plot", "chapters": "all", "include_original": "none"})
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["manifest"]["pack_count"] == 1
    assert [c for c in calls if c[0] == "run_worker"] == []
    multi = [c for c in calls if c[0] == "run_multi"]
    assert len(multi) == 1
    assert multi[0][1] == "fullbook_analysis"
    # plot 三件产出落盘
    for rel in ("全书分析/剧情结构/剧情线索.md", "全书分析/剧情结构/伏笔追踪.md", "全书分析/剧情结构/冲突图谱.md"):
        assert (project / rel).is_file()



# --- ChapterVisualAdapter -------------------------------------------------

def test_chapter_visual_adapter_touches_only_requested_seqs(project, monkeypatch):
    _install_fake_workers(monkeypatch)
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


def test_chapter_visual_adapter_per_chapter_outputs_match_engine_helper(project, monkeypatch):
    from run_pipeline import visual_assets_per_chapter_relpaths

    _install_fake_workers(monkeypatch)
    adapter = ChapterVisualAdapter()
    params = {"seqs": [1], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    record = result["manifest"]["packs"][0]
    assert record["outputs"] == visual_assets_per_chapter_relpaths(1, "序章")


def test_chapter_visual_adapter_aggregate_outputs_match_engine_helper(project, monkeypatch):
    from analysis_context_pack import TASKS

    _install_fake_workers(monkeypatch)
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

    adapter = ChapterVisualAdapter()
    params = {"seqs": [1, 2, 3], "aggregate": True, "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["task"] == "visual_assets"
    assert result["mode"] == "aggregate"
    assert result["target_outputs"] == TASKS["visual_assets"]["outputs"]
    assert Path(result["manifest_path"]).is_file()


def test_chapter_visual_adapter_per_chapter_runs_worker_and_writes_outputs(project, monkeypatch):
    calls = _install_fake_workers(monkeypatch)
    from run_pipeline import visual_assets_per_chapter_relpaths

    adapter = ChapterVisualAdapter()
    ctx = _make_context(project, {"seqs": [1, 2], "include_original": "none"})
    result = adapter.run(ctx, adapter.prepare(ctx))

    multi = [c for c in calls if c[0] == "run_multi"]
    assert len(multi) == 2
    assert all(c[1] == "visual_assets" for c in multi)
    titles = {1: "序章", 2: "相遇", 3: "冲突"}
    for seq in (1, 2):
        for rel in visual_assets_per_chapter_relpaths(seq, titles[seq]):
            assert (project / rel).is_file()


def test_chapter_visual_adapter_aggregate_runs_worker(project, monkeypatch):
    calls = _install_fake_workers(monkeypatch)
    from analysis_context_pack import TASKS

    titles = {1: "序章", 2: "相遇", 3: "冲突"}
    for seq in (1, 2, 3):
        d = project / "全书分析" / "视觉资产" / "分章"
        d.mkdir(parents=True, exist_ok=True)
        for name in ("视觉资产清单.md", "关键场景分镜表.md", "AI绘图提示词素材.md",
                     "角色外观一致性表.md", "场景氛围表.md"):
            (d / f"ch{seq:03d}_{titles[seq]}_{name}").write_text(f"# {name}\n", encoding="utf-8")

    adapter = ChapterVisualAdapter()
    ctx = _make_context(project, {"seqs": [1, 2, 3], "aggregate": True, "include_original": "none"})
    adapter.run(ctx, adapter.prepare(ctx))

    multi = [c for c in calls if c[0] == "run_multi"]
    assert len(multi) == 1
    assert multi[0][1] == "visual_assets_aggregate"
    for rel in TASKS["visual_assets"]["outputs"]:
        assert (project / rel).is_file()


def test_chapter_visual_adapter_without_worker_raises(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = ChapterVisualAdapter()
    ctx = _make_context(project, {"seqs": [1], "include_original": "none", "worker_provider": "manual"})
    with pytest.raises(RuntimeError, match="worker"):
        adapter.run(ctx, adapter.prepare(ctx))


# --- ChapterStructureAdapter ----------------------------------------------

def test_chapter_structure_adapter_touches_only_requested_seqs(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = ChapterStructureAdapter()
    params = {"seqs": [2], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    assert result["task"] == "chapter_structure"
    assert result["mode"] == "per_chapter"
    touched = sorted(record["seq"] for record in result["manifest"]["packs"])
    assert touched == [2]


def test_chapter_structure_adapter_per_chapter_outputs_match_engine_helper(project, monkeypatch):
    from run_pipeline import chapter_structure_per_chapter_relpaths

    _install_fake_workers(monkeypatch)
    adapter = ChapterStructureAdapter()
    params = {"seqs": [1, 2], "include_original": "none"}
    ctx = _make_context(project, params)
    result = adapter.run(ctx, adapter.prepare(ctx))

    for record in result["manifest"]["packs"]:
        seq = record["seq"]
        assert record["outputs"] == chapter_structure_per_chapter_relpaths(seq, {1: "序章", 2: "相遇", 3: "冲突"}[seq])


def test_chapter_structure_adapter_runs_worker_and_writes_outputs(project, monkeypatch):
    calls = _install_fake_workers(monkeypatch)
    from run_pipeline import chapter_structure_per_chapter_relpaths

    adapter = ChapterStructureAdapter()
    ctx = _make_context(project, {"seqs": [1, 2], "include_original": "none"})
    adapter.run(ctx, adapter.prepare(ctx))

    multi = [c for c in calls if c[0] == "run_multi"]
    assert len(multi) == 2
    assert all(c[1] == "chapter_structure" for c in multi)
    titles = {1: "序章", 2: "相遇", 3: "冲突"}
    for seq in (1, 2):
        for rel in chapter_structure_per_chapter_relpaths(seq, titles[seq]):
            assert (project / rel).is_file()


def test_chapter_structure_adapter_without_worker_raises(project, monkeypatch):
    _install_fake_workers(monkeypatch)
    adapter = ChapterStructureAdapter()
    ctx = _make_context(project, {"seqs": [1], "include_original": "none", "worker_provider": "manual"})
    with pytest.raises(RuntimeError, match="worker"):
        adapter.run(ctx, adapter.prepare(ctx))


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
    """Canonical spec path (§3.5/§8): {source, kinds, force}.

    kinds translates to engine flags: single → single_file (default path when
    none given), dataview/canvas → on; vault is always produced. force wipes
    the existing export dir before regenerating.
    """
    adapter = ExportAdapter()
    out_dir = tmp_path / "vault"
    params = {
        "source": "process",
        "kinds": ["single", "vault", "dataview", "canvas"],
        "force": True,
        "out_dir": str(out_dir),
        "filename_mode": "type-name",
    }
    ctx = _make_context(project, params)
    prepared = adapter.prepare(ctx)
    result = adapter.run(ctx, prepared)

    # The source resolved to 故事结构_增量.json (process).
    assert "故事结构_增量.json" in result["manifest"]["source_file"]
    assert result["source"] == "process"

    # kinds + force are echoed on the result.
    assert result["kinds"] == ["single", "vault", "dataview", "canvas"]
    assert result["force"] is True

    # "single" with no explicit path → default single-file path beside the vault.
    single_file_path = out_dir.parent / (out_dir.name + "_single.md")
    assert result["manifest"]["single_file"] == str(single_file_path)
    assert result["manifest"]["dataview_enabled"] is True
    assert result["manifest"]["canvas_enabled"] is True
    assert result["manifest"]["filename_mode"] == "type-name"

    # Artifacts exist on disk.
    assert (out_dir / "_meta" / "export_report.json").is_file()
    assert (out_dir / "00-总览" / "故事总览.md").is_file()
    assert (out_dir / "_dataview" / "查询-主角.md").is_file()
    assert (out_dir / "00-总览" / "故事图谱.canvas").is_file()
    assert single_file_path.is_file()

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


def test_export_adapter_force_wipes_existing_vault(project, tmp_path):
    """force wipes an exporter-owned vault before regenerating it."""
    out_dir = tmp_path / "vault-clean"
    (out_dir / "_meta").mkdir(parents=True)
    (out_dir / "_meta" / "export_report.json").write_text("{}", encoding="utf-8")
    (out_dir / "stale.md").write_text("stale", encoding="utf-8")

    adapter = ExportAdapter()
    params = {"source": "final", "kinds": ["vault"], "force": True, "out_dir": str(out_dir)}
    ctx = _make_context(project, params)
    adapter.run(ctx, adapter.prepare(ctx))

    # Stale file is gone after a forced (clean) export.
    assert not (out_dir / "stale.md").exists()
    assert (out_dir / "00-总览" / "故事总览.md").is_file()
