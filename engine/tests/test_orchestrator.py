import json
import sys
from pathlib import Path
from unittest.mock import patch

import orchestrator
from events import EventBus
from orchestrator import Config, Orchestrator
from pipeline_state import PipelineState

def collect(bus):
    out = []
    bus.subscribe(lambda e: out.append(e))
    return out


def test_chapter_phases_advance_then_block_on_stub(tmp_path):
    bus = EventBus()
    ev = collect(bus)
    with patch.object(orchestrator, "cmd_run", side_effect=[0, 0]) as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=bus)
        rc = orch.run()
    # chapter_analysis(0) -> chapter_delta(0) -> chapter_structure 未实现 -> 阻塞
    assert rc == 1
    assert m.call_count == 2
    types = [(e.type, e.stage) for e in ev]
    assert ("stage_done", "chapter_analysis") in types
    assert ("stage_done", "chapter_delta") in types
    assert ("stage_fail", "chapter_structure") in types
    assert orch.state.current_stage == "chapter_structure"


def test_retry_then_success(tmp_path):
    bus = EventBus()
    with patch.object(orchestrator, "cmd_run", side_effect=[1, 1, 0, 0]) as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=10, stagnant_limit=5), bus=bus)
        rc = orch.run()
    # chapter_analysis: 1,1,0（第3次成功）；chapter_delta: 0；chapter_structure 未实现 -> 阻塞
    assert m.call_count == 4
    assert rc == 1
    assert "chapter_analysis" in orch.state.completed
    assert "chapter_delta" in orch.state.completed


def test_hard_block_stagnant(tmp_path):
    bus = EventBus()
    hard = []
    bus.subscribe(lambda e: hard.append(e) if e.type == "hard_block" else None)
    with patch.object(orchestrator, "cmd_run", return_value=1) as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=10, stagnant_limit=3), bus=bus)
        rc = orch.run()
    assert rc == 1
    assert len(hard) == 1
    assert hard[0].stage == "chapter_analysis"
    # 基线 + 3 次无变化 -> 第 3 次尝试触发硬阻塞
    assert m.call_count == 3


def test_recovery_resumes_at_state(tmp_path):
    st = PipelineState.load(tmp_path)
    st.mark_completed("chapter_analysis")
    st.save()
    bus = EventBus()
    with patch.object(orchestrator, "cmd_run", return_value=0) as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=bus)
        rc = orch.run()
    # chapter_analysis 已完成被跳过；chapter_delta 执行一次(0)；chapter_structure 未实现
    assert m.call_count == 1
    assert rc == 1
    assert "chapter_analysis" in orch.state.completed


def test_state_persisted_between_runs(tmp_path):
    bus = EventBus()
    with patch.object(orchestrator, "cmd_run", side_effect=[0, 0]) as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=bus)
        orch.run()
    # 第二次运行：状态已到 chapter_structure，cmd_run 不应再被调用
    with patch.object(orchestrator, "cmd_run") as m2:
        orch2 = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=EventBus())
        orch2.run()
    assert m2.call_count == 0


def test_structure_and_visual_stages_dispatch(tmp_path):
    bus = EventBus()
    with patch.object(orchestrator, "cmd_run", side_effect=[0, 0]), \
         patch.object(orchestrator, "cmd_chapter_structure_auto", return_value=0) as cs, \
         patch.object(orchestrator, "cmd_visual_assets_auto", return_value=0) as va:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1, worker_provider="agy"), bus=bus)
        rc = orch.run()
    assert cs.call_count == 1
    assert va.call_count == 1
    ns = cs.call_args[0][0]
    assert ns.run_mode == "worker"
    assert ns.worker_provider == "agy"
    assert ns.worker_loop == "continuous"
    assert ns.worker_command == ""  # 由 resolve 按 provider 填充
    assert ns.project_dir == str(tmp_path)
    assert "chapter_structure" in orch.state.completed
    assert "visual_assets" in orch.state.completed
    assert rc == 1  # fullbook_analysis 未实现


def test_final_pack_stage(tmp_path):
    orch = Orchestrator(tmp_path, config=Config(worker_provider="agy"))
    with patch.object(orchestrator, "cmd_final_pack", return_value=0) as fp, \
         patch.object(orchestrator, "run_worker", return_value=0) as rw:
        rc = orch._stage_final_pack()
    assert rc == 0
    assert fp.call_count == 1
    assert rw.call_count == 1
    args = rw.call_args[0]  # command, project_dir, task_type, task_pack, expected_output
    assert args[2] == "final_draft"
    assert args[3].name == "final_task_pack.md"
    assert args[4].name == "故事结构_草稿.json"


def test_commit_final_draft_repair_loop(tmp_path):
    orch = Orchestrator(tmp_path, config=Config(worker_provider="agy", max_repair_rounds=3))
    repair = tmp_path / "质量治理" / "最终审计" / "repair_final.md"
    repair.parent.mkdir(parents=True, exist_ok=True)
    repair.write_text("repair instructions", encoding="utf-8")
    with patch.object(orchestrator, "cmd_commit_final_draft", side_effect=[1, 1, 0]) as cfd, \
         patch.object(orchestrator, "run_worker", return_value=0) as rw:
        rc = orch._stage_commit_final_draft()
    assert rc == 0
    assert cfd.call_count == 3
    assert rw.call_count == 2  # 两次失败各触发一次修复 worker


def test_commit_final_draft_exhausts_repair_rounds(tmp_path):
    orch = Orchestrator(tmp_path, config=Config(worker_provider="agy", max_repair_rounds=2))
    repair = tmp_path / "质量治理" / "最终审计" / "repair_final.md"
    repair.parent.mkdir(parents=True, exist_ok=True)
    repair.write_text("repair instructions", encoding="utf-8")
    with patch.object(orchestrator, "cmd_commit_final_draft", return_value=1), \
         patch.object(orchestrator, "run_worker", return_value=0) as rw:
        rc = orch._stage_commit_final_draft()
    assert rc == 1
    assert rw.call_count == 2  # 每轮修复一次，共 2 轮


def test_export_stage(tmp_path):
    orch = Orchestrator(tmp_path)
    with patch.object(orchestrator, "run_cmd", return_value=0) as rc_mock:
        rc = orch._stage_export()
    assert rc == 0
    cmd = rc_mock.call_args[0][0]
    assert "export_obsidian_vault.py" in cmd[1]
    assert "--source" in cmd and "final" in cmd
    assert "--include-dataview" in cmd
    assert "--include-canvas" in cmd
    assert "--clean" in cmd


def _write_manifest(project: Path, task: str, pack_count: int, target_outputs: list) -> Path:
    mdir = project / "全书分析" / "_任务包" / f"20260101_000000_{task}"
    mdir.mkdir(parents=True, exist_ok=True)
    packs = []
    for i in range(1, pack_count + 1):
        (mdir / f"pack_{i:03d}.md").write_text(f"pack {i}", encoding="utf-8")
        packs.append(f"全书分析/_任务包/20260101_000000_{task}/pack_{i:03d}.md")
    (mdir / "reduce_prompt.md").write_text("reduce contract", encoding="utf-8")
    manifest = {
        "task": task, "packs": packs, "pack_count": pack_count,
        "reduce_prompt": f"全书分析/_任务包/20260101_000000_{task}/reduce_prompt.md",
        "target_outputs": target_outputs,
    }
    mpath = mdir / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return mpath


def _write_expected_worker(*args, **kwargs):
    """假 worker：按 ND_EXPECTED_OUTPUT 写产物。"""
    expected = args[4]
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text("worker output\n", encoding="utf-8")
    return 0


def test_fullbook_single_pack(tmp_path):
    mpath = _write_manifest(tmp_path, "summary", 1, ["全书分析/剧情结构/章节梗概汇总.md"])
    orch = Orchestrator(tmp_path, config=Config(worker_provider="agy", fullbook_tasks=("summary",)))
    with patch.object(orchestrator, "run_cmd", return_value=0), \
         patch.object(orchestrator, "latest_analysis_manifest", return_value=mpath), \
         patch.object(orchestrator, "run_worker", side_effect=_write_expected_worker) as rw, \
         patch.object(orchestrator, "run_multi_output_worker", return_value=0) as rmw:
        rc = orch._stage_fullbook_analysis()
    assert rc == 0
    assert rw.call_count == 1  # 单包直接写目标
    assert rmw.call_count == 0  # 无 reduce


def test_fullbook_multi_pack_reduce(tmp_path):
    mpath = _write_manifest(
        tmp_path, "characters", 2,
        ["全书分析/人物分析/人物档案.md", "全书分析/人物分析/人物关系.md"],
    )
    orch = Orchestrator(tmp_path, config=Config(worker_provider="agy", fullbook_tasks=("characters",)))
    with patch.object(orchestrator, "run_cmd", return_value=0), \
         patch.object(orchestrator, "latest_analysis_manifest", return_value=mpath), \
         patch.object(orchestrator, "run_worker", side_effect=_write_expected_worker) as rw, \
         patch.object(orchestrator, "run_multi_output_worker", return_value=0) as rmw:
        rc = orch._stage_fullbook_analysis()
    assert rc == 0
    assert rw.call_count == 2  # 2 包 -> 2 partial
    assert rmw.call_count == 1  # 1 次 reduce
    assert (mpath.parent / "reduce_task_pack.md").is_file()


def test_full_pipeline_completes(tmp_path):
    bus = EventBus()
    done = []
    bus.subscribe(lambda e: done.append(e.type))
    with patch.object(orchestrator, "cmd_run", side_effect=[0, 0]), \
         patch.object(orchestrator, "cmd_chapter_structure_auto", return_value=0), \
         patch.object(orchestrator, "cmd_visual_assets_auto", return_value=0), \
         patch.object(orchestrator, "cmd_final_pack", return_value=0), \
         patch.object(orchestrator, "run_worker", return_value=0), \
         patch.object(orchestrator, "cmd_commit_final_draft", return_value=0), \
         patch.object(orchestrator, "cmd_finalize", return_value=0), \
         patch.object(orchestrator, "run_cmd", return_value=0):
        orch = Orchestrator(
            tmp_path, config=Config(max_attempts=1, worker_provider="agy", fullbook_tasks=()), bus=bus,
        )
        rc = orch.run()
    assert rc == 0
    assert orch.state.is_done()
    assert "pipeline_done" in done


# ---- 真实引擎接线测试（假 worker 驱动真实 cmd_chapter_structure_auto）----

SRC_DIR = "原文拆解"
ANALYSIS_DIR = "章节处理"
INDEX_FILE = "_索引.json"
CHAPTER_FILE = "第001章_test.md"


def _write_project(project: Path) -> None:
    (project / SRC_DIR).mkdir(parents=True)
    (project / ANALYSIS_DIR).mkdir(parents=True)
    (project / SRC_DIR / INDEX_FILE).write_text(
        json.dumps({"chapters": [{"seq": 1, "filename": CHAPTER_FILE, "title": "test"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (project / SRC_DIR / CHAPTER_FILE).write_text("# chapter 001\nsource text.\n", encoding="utf-8")
    (project / ANALYSIS_DIR / CHAPTER_FILE).write_text("# analysis\nready.\n", encoding="utf-8")


def _write_multi_output_worker(path: Path) -> None:
    path.write_text(
        """
import json
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
outputs = json.loads(os.environ["ND_EXPECTED_OUTPUTS"])
for item in outputs:
    target = Path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# generated {task_type}\\n{target.name}\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )


def test_chapter_phases_pass_correct_args(tmp_path):
    with patch.object(orchestrator, "cmd_run", side_effect=[0, 0]) as m:
        Orchestrator(tmp_path, config=Config(max_attempts=1, worker_provider="agy")).run()
    assert m.call_count == 2
    assert m.call_args_list[0].kwargs["phase"] == "analysis"
    assert m.call_args_list[0].kwargs["run_mode"] == "worker"
    assert m.call_args_list[0].kwargs["worker_provider"] == "agy"
    assert m.call_args_list[0].kwargs["worker_loop"] == "continuous"
    assert m.call_args_list[1].kwargs["phase"] == "delta"


def test_chapter_structure_real_wiring_with_fake_worker(tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    worker = tmp_path / "multi_output_worker.py"
    _write_multi_output_worker(worker)
    cfg = Config(
        worker_provider="manual",
        worker_command=f'"{sys.executable}" "{worker}"',
        worker_timeout_seconds=30,
        worker_retries=0,
    )
    orch = Orchestrator(project, config=cfg)
    rc = orch._stage_chapter_structure()
    assert rc == 0
    assert (project / "全书分析" / "故事结构" / "分章" / "ch001" / "章节结构.md").is_file()


def test_signature_stage_aware_final_pack(tmp_path):
    orch = Orchestrator(tmp_path)
    s1 = orch._signature("final_pack")
    assert "files=0" in s1
    draft = tmp_path / "故事结构_草稿.json"
    draft.write_text("{}", encoding="utf-8")
    s2 = orch._signature("final_pack")
    assert s2 != s1
    assert "files=1" in s2


def test_chapter_analysis_passes_concurrency(tmp_path):
    with patch.object(orchestrator, "cmd_run", return_value=0) as m:
        orch = Orchestrator(tmp_path, config=Config(analysis_concurrency=4))
        orch._stage_chapter_analysis()
    m.assert_called_once()
    assert m.call_args.kwargs["phase"] == "analysis"
    assert m.call_args.kwargs["concurrency"] == 4


def test_cancel_before_run(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e.type))
    with patch.object(orchestrator, "cmd_run") as m:
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=bus)
        orch.cancel()
        rc = orch.run()
    assert rc == 2  # cancelled
    assert m.call_count == 0  # 未进入任何阶段
    assert "pipeline_cancelled" in events


def test_cancel_mid_stage(tmp_path):
    import threading
    import time
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e.type))
    orch_ref: list = []

    def fake_cmd_run(*a, **k):
        o = orch_ref[0]
        for _ in range(100):
            if o._cancel.is_set():
                return 130  # 模拟 worker 被取消
            time.sleep(0.02)
        return 1

    with patch.object(orchestrator, "cmd_run", side_effect=fake_cmd_run):
        orch = Orchestrator(tmp_path, config=Config(max_attempts=1), bus=bus)
        orch_ref.append(orch)
        threading.Timer(0.2, orch.cancel).start()
        rc = orch.run()
    assert rc == 2  # 阶段中途取消
    assert "pipeline_cancelled" in events
