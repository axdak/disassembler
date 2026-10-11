#!/usr/bin/env python3
"""周期审计任务包与 run 自动暂停行为测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
RUNNER = ROOT / "run_pipeline.py"
sys.path.insert(0, str(ROOT))

from run_pipeline import (
    artifact_paths,
    build_delta_story_context,
    cmd_run,
    cmd_run_interleaved,
    commit_governance,
    prepare_audit_pack_or_split,
    run_periodic_governance,
    write_audit_pack,
)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": "周期审计测试"},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def empty_delta(seq):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {"章节": f"第{seq:03d}章", "新增元素": empty, "修改元素": empty}


def valid_analysis(seq):
    return f"""# 第{seq:03d}章分析

## 1. 剧情梗概
本章围绕主角收到一封来信后前往约定地点展开。来信揭示旧承诺存在漏洞，主角决定主动求证，陌生人的出现又为后续留下悬念。

## 2. 出场人物
- 主角：从犹豫转为主动追查来信真相。

## 3. 核心冲突
主角必须在相信旧承诺和面对新证据之间作出选择。

## 4. 信息增量
来信说明旧日约定含有未公开条件。

## 5. 伏笔与悬念
陌生人的身份和真实目的尚待后续揭示。

## 6. 爽点 / 虐点 / 情绪点
主动追查带来期待、紧张与危机感。

## 7. 章节功能判断
本章承担冲突启动和悬念铺垫功能。

## 8. 事件分组与标签建议
来信求证承诺线冲突启动主动追查身份悬念。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 |
|----------|----------|----------|
| V01 | 主角阅读来信 | 中 |

## 10. 结构提取提示
应提取来信事件、约定地点和陌生人线索。
"""


def write_chapter_source(project, seq):
    path = project / "原文拆解" / f"第{seq:03d}章_测试.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
    return path


def write_completed_chapter(project, seq):
    chapter = write_chapter_source(project, seq)
    base = chapter.stem
    (project / "章节处理").mkdir(parents=True, exist_ok=True)
    (project / "章节处理" / chapter.name).write_text(valid_analysis(seq), encoding="utf-8")
    write_json(project / "章节处理" / "Delta" / f"{base}.json", empty_delta(seq))
    for rel in [
        f"质量治理/delta校验/{base}.json",
        f"质量治理/章节校验/{base}.json",
        f"故事结构版本/story_before_ch{seq:03d}.json",
        f"故事结构版本/story_after_ch{seq:03d}.json",
        f"结构变更日志/diff_ch{seq:03d}.json",
    ]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if "校验" in str(path):
            write_json(path, {"passed": True, "mode": "process", "errors": [], "warnings": []})
        elif path.suffix == ".json":
            write_json(path, empty_story())
        else:
            path.write_text("ok\n", encoding="utf-8")
    return chapter


def write_no_change_correction(project, start, end):
    """为指定已提交区间提供可校验的无变更治理补丁。"""
    empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    write_json(project / "质量治理" / "周期审计" / f"correction_{start:03d}-{end:03d}.json", {
        "章节范围": f"第{start:03d}章-第{end:03d}章",
        "治理类型": "no_change",
        "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
        "证据范围": [f"第{seq:03d}章" for seq in range(start, end + 1)],
        "新增元素": empty,
        "修改元素": empty,
    })


def test_audit_pack_cli_creates_review_package():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 6):
            write_completed_chapter(project, seq)

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(RUNNER), "audit-pack", str(project), "--chapters", "1-5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert result.returncode == 0, result.stdout

        pack = project / "质量治理" / "周期审计" / "audit_001-005.md"
        correction = project / "质量治理" / "周期审计" / "correction_001-005.json"
        assert pack.is_file(), result.stdout
        text = pack.read_text(encoding="utf-8")
        assert "周期结构审计任务包" in text
        assert str(correction) in text
        assert "commit-governance" in text
        assert "第001章_测试.md" in text
        assert "第005章_测试.json" in text
        assert "测试原文。" in text
        assert "顶层介绍" in text
        assert "阶段性故事简介" in text

    print("[OK] audit-pack CLI 生成周期审计任务包")


def test_audit_pack_pages_full_story_when_it_exceeds_budget():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = empty_story()
        story["介绍"]["描述"] = "结构详情" * 6000
        write_json(project / "故事结构_增量.json", story)
        write_completed_chapter(project, 1)

        pack = write_audit_pack(project, 1, 1, max_context_chars=12000)

        assert pack is not None
        text = pack.read_text(encoding="utf-8")
        assert "## 当前故事结构索引" in text
        assert "## 完整故事结构" not in text
        status = load_status(project / "质量治理" / "周期审计" / "audit_001-001.status.json")
        assert status["context"]["render_mode"] == "story_index_relevant"


def test_audit_pack_keeps_full_story_for_large_file_when_budget_allows():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = empty_story()
        story["padding"] = "x" * 150000
        write_json(project / "故事结构_增量.json", story)
        write_completed_chapter(project, 1)

        pack = write_audit_pack(project, 1, 1, max_context_chars=220000)

        assert pack is not None
        status = load_status(next(project.rglob("audit_001-001.status.json")))
        assert status["context"]["render_mode"] == "full_story"
        assert status["context"]["attempts"][0]["render_mode"] == "full_story"


def test_audit_pack_attempts_full_story_before_paging_very_large_file():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = empty_story()
        story["padding"] = "x" * 250000
        write_json(project / "故事结构_增量.json", story)
        write_completed_chapter(project, 1)

        pack = write_audit_pack(project, 1, 1, max_context_chars=12000)

        assert pack is not None
        status = load_status(next(project.rglob("audit_001-001.status.json")))
        attempts = [attempt["render_mode"] for attempt in status["context"]["attempts"]]
        assert attempts == ["full_story", "story_index_relevant"]
        assert status["context"]["render_mode"] == "story_index_relevant"


def test_audit_pack_pages_single_chapter_without_truncating_evidence():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = write_completed_chapter(project, 1)
        chapter.write_text("原文证据" * 10000, encoding="utf-8")

        pack = write_audit_pack(project, 1, 1, max_context_chars=12000)

        assert pack is not None and pack.with_suffix(".governance.json").is_file()
        status = load_status(project / "质量治理" / "周期审计" / "audit_001-001.status.json")
        assert status["context"]["render_mode"] == "tiered_evidence_tree"
        assert status["context"]["current_evidence_chars"] >= len("原文证据" * 10000)
        assert (project / "质量治理" / "周期审计" / "audit_001-001.md").exists()


def test_audit_pack_splits_multi_chapter_overflow_into_child_packs():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 6):
            chapter = write_completed_chapter(project, seq)
            chapter.write_text(f"# 第{seq:03d}章\n" + ("当前周期证据" * 1200), encoding="utf-8")

        rc = prepare_audit_pack_or_split(project, 1, 5, max_context_chars=25000)

        assert rc == 0
        audit_dir = project / "质量治理" / "周期审计"
        parent = load_status(audit_dir / "audit_001-005.status.json")
        assert parent["status"] == "split_pending"
        child_ranges = {item["range"] for item in parent["child_ranges"]}
        assert child_ranges == {"001-003", "004-005"}
        mid = load_status(audit_dir / "audit_001-003.status.json")
        assert mid["status"] == "split_pending"
        assert {item["range"] for item in mid["child_ranges"]} == {"001-002", "003-003"}
        assert (audit_dir / "audit_001-002.md").is_file()
        assert (audit_dir / "audit_003-003.md").is_file()
        assert (audit_dir / "audit_004-005.md").is_file()
        assert not (audit_dir / "audit_001-005.md").exists()


def test_run_hands_periodic_audit_to_agent_then_continues_after_real_patch():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 5)
        (project / "章节处理" / "第005章_测试.md").write_text(valid_analysis(5), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第006章_测试.json", empty_delta(6))
        rc = cmd_run(project)
        assert rc == 2
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        assert load_status(status)["status"] == "awaiting_agent"
        assert not (project / "故事结构版本" / "story_after_ch006.json").is_file()

        write_no_change_correction(project, 1, 5)

        rc = cmd_run(project)
        assert rc == 2
        tail_status = project / "质量治理" / "周期审计" / "audit_006-006.status.json"
        assert load_status(tail_status)["status"] == "awaiting_agent"
        write_no_change_correction(project, 6, 6)
        rc = cmd_run(project)
        assert rc == 0
        assert load_status(status)["status"] == "committed"
        assert load_status(tail_status)["status"] == "committed"
        assert list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "按需治理").glob("*governance_*.txt"))
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()

    print("[OK] run 将周期审计交给Agent，提交真实补丁后继续下一章")


def test_short_book_audits_final_partial_range_once():
    """三章短书补审 001-003 尾段，续跑不重复提交。"""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 4):
            write_completed_chapter(project, seq)

        assert cmd_run(project) == 2
        audit_dir = project / "质量治理" / "周期审计"
        status = audit_dir / "audit_001-003.status.json"
        assert load_status(status)["status"] == "awaiting_agent"

        write_no_change_correction(project, 1, 3)
        assert cmd_run(project) == 0
        assert load_status(status)["status"] == "committed"
        committed_diffs = list((project / "结构变更日志").glob("diff_governance_*.json"))

        assert cmd_run(project) == 0
        assert list((project / "结构变更日志").glob("diff_governance_*.json")) == committed_diffs


def test_interleaved_run_audits_final_partial_range():
    """交错流水线在全部章节提交后也补审不足一周期的尾段。"""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 4):
            write_completed_chapter(project, seq)

        rc = cmd_run_interleaved(
            project, run_mode="worker", worker_provider="manual",
            chapter_command="unused-worker", audit_command="",
        )

        assert rc == 2
        status = project / "质量治理" / "周期审计" / "audit_001-003.status.json"
        assert load_status(status)["status"] == "awaiting_agent"


def test_full_periods_do_not_trigger_unbounded_whole_book_audit():
    """十章书已有两个五章周期审计时，不再生成 001-010 大审计包。"""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 11):
            write_completed_chapter(project, seq)
        audit_dir = project / "质量治理" / "周期审计"
        for start, end in ((1, 5), (6, 10)):
            write_json(audit_dir / f"audit_{start:03d}-{end:03d}.status.json", {
                "status": "committed", "range": f"{start:03d}-{end:03d}",
            })

        assert cmd_run(project) == 0
        assert not (audit_dir / "audit_001-010.status.json").exists()


def test_run_with_audit_worker_commits_periodic_audit_and_continues():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 5)
        (project / "章节处理" / "第005章_测试.md").write_text(valid_analysis(5), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第006章_测试.json", empty_delta(6))
        worker = root / "audit_worker.py"
        worker.write_text(
            """
import json
import os
import re
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
if task_type not in ("audit_correction", "audit_repair"):
    raise SystemExit(f"unexpected task type: {task_type}")

empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
match = re.fullmatch(r"correction_(\\d{3})-(\\d{3})\\.json", output.name)
if match is None:
    raise SystemExit(f"unexpected output: {output}")
start, end = map(int, match.groups())
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "章节范围": f"第{start:03d}章-第{end:03d}章",
    "治理类型": "no_change",
    "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
    "证据范围": [f"第{seq:03d}章" for seq in range(start, end + 1)],
    "新增元素": empty,
    "修改元素": empty,
}, ensure_ascii=False, indent=2), encoding="utf-8")
""",
            encoding="utf-8",
        )

        rc = cmd_run(
            project,
            audit_command=f'"{sys.executable}" "{worker}"',
            worker_window="hidden",
            worker_loop="continuous",
        )

        assert rc == 0
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        assert load_status(status)["status"] == "committed"
        tail_status = project / "质量治理" / "周期审计" / "audit_006-006.status.json"
        assert load_status(tail_status)["status"] == "committed"
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()


def test_run_auto_splits_periodic_audit_and_continues_after_child_patches():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            chapter = write_completed_chapter(project, seq)
            chapter.write_text(f"# 第{seq:03d}章\n" + ("当前周期证据" * 1200), encoding="utf-8")
        ch5 = write_chapter_source(project, 5)
        ch5.write_text("# 第005章\n" + ("当前周期证据" * 1200), encoding="utf-8")
        (project / "章节处理" / "第005章_测试.md").write_text(valid_analysis(5), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "Delta" / "第006章_测试.json", empty_delta(6))

        worker = root / "split_audit_worker.py"
        worker.write_text(
            r"""
import json, os, re
from pathlib import Path
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
prompt = Path(os.environ["ND_TASK_PACK"]).read_text(encoding="utf-8")
review = re.search(r"复核材料指纹：([0-9a-f]{64})", prompt)
match = re.search(r"correction_(\d{3})-(\d{3})\.json", output.name + prompt)
if not match: raise SystemExit("no real chapter range")
start, end = match.groups()
empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
value = {"章节范围": f"第{start}章-第{end}章", "治理类型": "no_change",
         "质量说明": "已完整检查证据、名称和跨片关系，无需变更。",
         "证据范围": [f"第{n:03d}章" for n in range(int(start), int(end)+1)],
         "新增元素": empty, "修改元素": empty}
if os.environ["ND_TASK_TYPE"] == "bounded_fragment":
    value = {"overview": "完整证据及跨片关系已检查", "content":
             {"checked": review[1], "unresolved": [], "addition": None} if review else value}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
""",
            encoding="utf-8",
        )

        rc = cmd_run(
            project,
            audit_max_context_chars=25000,
            audit_command=f'"{sys.executable}" "{worker}"',
            worker_window="hidden",
            worker_loop="continuous",
        )

        assert rc == 0
        audit_dir = project / "质量治理" / "周期审计"
        parent = load_status(audit_dir / "audit_001-005.status.json")
        assert parent["status"] == "committed"
        assert parent["cross_range_committed"] is True
        assert {item["range"] for item in parent["child_ranges"]} == {"001-003", "004-005"}
        assert load_status(audit_dir / "audit_001-003.status.json")["status"] == "committed"
        assert load_status(audit_dir / "audit_001-002.status.json")["status"] == "committed"
        assert load_status(audit_dir / "audit_003-003.status.json")["status"] == "committed"
        assert load_status(audit_dir / "audit_004-005.status.json")["status"] == "committed"
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()


def load_status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manual_governance_reports_remain_separate():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "manual_correction.json"
        write_json(patch, {
            "章节": "手工治理",
            "治理类型": "no_change",
            "质量说明": "手工治理回归检查，无需修改。",
            "证据范围": ["第001章"],
            "新增元素": empty_delta(1)["新增元素"],
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))
        assert rc == 0
        assert list((project / "质量治理" / "按需治理").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))

    print("[OK] 手工治理报告保持在按需治理目录")


def test_periodic_governance_normalize_quarantines_invalid_detail_ref_arrays():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = empty_story()
        story["事件集"].append(
            {
                "名称": "事件A",
                "发生地点": "",
                "参与成员": [],
                "重量级": 10,
                "目标事件": [],
                "分组": "00000010-测试事件",
                "时间": "0001-01-01T00:00:00",
                "别名": [],
                "标签集": [],
                "介绍": "测试事件。",
                "详情": {
                    "涉及章节": "0001",
                    "关联事件": ["事件:不存在事件"],
                },
            }
        )
        write_json(project / "故事结构_增量.json", story)
        patch = project / "质量治理" / "周期审计" / "correction_001-005.json"
        write_json(patch, {
            "章节范围": "第001章-第005章",
            "治理类型": "no_change",
            "质量说明": "周期治理回归检查，无需修改。",
            "证据范围": ["第001章", "第002章", "第003章", "第004章", "第005章"],
            "新增元素": empty_delta(1)["新增元素"],
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))

        assert rc == 0
        normalized = json.loads((project / "故事结构_增量.json").read_text(encoding="utf-8"))
        detail = normalized["事件集"][0]["详情"]
        assert detail["关联事件"] == []
        assert json.loads(detail["待确认引用"]) == ["事件:不存在事件"]

    print("[OK] 周期治理规范化会隔离详情无效引用数组")


def test_governance_patch_compresses_tags_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_json(project / "故事结构_增量.json", empty_story())

        patch = project / "质量治理" / "周期审计" / "correction_001-005.json"
        write_json(patch, {
            "章节范围": "001-005",
            "新增元素": {
                "角色集": [
                    {
                        "名称": "客栈掌柜",
                        "是否主角": False,
                        "性别": 2,
                        "年龄": 0,
                        "生日": "0001-01-01T00:00:00",
                        "所属阵营": [],
                        "关系": [],
                        "分组": "路人",
                        "别名": [],
                        "标签集": ["人物类型:掌柜", "客栈"],
                        "介绍": "客栈掌柜。",
                        "详情": {
                            "首次章节": "0001",
                            "最近章节": "0001",
                            "提取理由": "测试治理补丁标签压缩。",
                        },
                    }
                ],
                "事件集": [],
                "地点集": [],
                "线索集": [],
                "阵营集": [],
                "物品集": [],
                "其他事项集": [],
            },
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))

        assert rc == 0
        patched = json.loads(patch.read_text(encoding="utf-8"))
        character = patched["新增元素"]["角色集"][0]
        assert character["标签集"] == ["客栈"]
        assert json.loads(character["详情"]["补充标签"]) == ["人物类型:掌柜"]
        assert list((project / "质量治理" / "周期审计").glob("compress_governance_*.json"))

    print("[OK] 治理补丁校验前执行标签压缩")


def test_chapter_delta_repairs_llm_json_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = write_chapter_source(project, 1)
        base = chapter.stem
        (project / "章节处理").mkdir(parents=True, exist_ok=True)
        (project / "章节处理" / "Delta").mkdir(parents=True, exist_ok=True)
        (project / "章节处理" / chapter.name).write_text(valid_analysis(1), encoding="utf-8")
        malformed_delta = """```json
{
  '章节': '第001章',
  '新增元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
  '修改元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
}
```
"""
        (project / "章节处理" / "Delta" / f"{base}.json").write_text(malformed_delta, encoding="utf-8")

        # 此用例只校验章节 Delta 的 JSON 修复，不启动独立的周期审计。
        rc = cmd_run(project, audit_interval=0)

        assert rc == 0
        repaired = json.loads((project / "章节处理" / "Delta" / f"{base}.json").read_text(encoding="utf-8"))
        assert repaired["章节"] == "第001章"
        assert list((project / "质量治理" / "delta校验").glob("repair_json_ch001.json"))

    print("[OK] 章节 Delta 校验前修复 LLM JSON 语法")


def test_governance_patch_repairs_llm_json_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "manual_correction.json"
        patch.write_text(
            """```json
{
  '章节': '手工治理',
  '治理类型': 'no_change',
  '质量说明': '手工治理回归检查，无需修改。',
  '证据范围': ['第001章',],
  '新增元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
  '修改元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
}
```
""",
            encoding="utf-8",
        )

        rc = commit_governance(project, str(patch))

        assert rc == 0
        repaired = json.loads(patch.read_text(encoding="utf-8"))
        assert repaired["章节"] == "手工治理"
        assert list((project / "质量治理" / "按需治理").glob("repair_json_governance_*.json"))

    print("[OK] 治理补丁校验前修复 LLM JSON 语法")


def test_periodic_governance_skipped_when_no_committed_chapters():
    # 零已提交章节（无 story_after_chNNN.json）时，周期治理整体跳过：
    # 不建 pack、不 commit、不产生空快照。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())

        rc = run_periodic_governance(project, 1, 5)

        assert rc == 0
        audit_dir = project / "质量治理" / "周期审计"
        assert not list(audit_dir.glob("audit_*.md"))
        versions = project / "故事结构版本"
        assert not list(versions.glob("story_before_governance_*.json"))
        assert not list(versions.glob("story_after_governance_*.json"))

    print("[OK] 零已提交章节时周期治理整体跳过，不产空快照")


def test_commit_governance_rejects_no_change_missing_metadata():
    # no_change 周期补丁缺 质量说明/证据范围 时，validate_no_change_metadata 报错，
    # commit_governance 不接受（不标记 committed）——治理补丁必须真实，禁止空补丁跳过审计。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "质量治理" / "周期审计" / "correction_001-005.json"
        write_json(patch, {
            "章节范围": "第001章-第005章",
            "治理类型": "no_change",
            # 故意缺 质量说明 和 证据范围
            "新增元素": empty_delta(1)["新增元素"],
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))

        assert rc != 0
        versions = project / "故事结构版本"
        assert not list(versions.glob("story_after_governance_*.json"))

    print("[OK] no_change 补丁缺元数据时 commit_governance 拒绝")


def test_delta_story_context_passes_full_story_when_small():
    # < 256KB：完整故事 JSON 直接传给 Delta 提取器。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = project / "故事结构_增量.json"
        data = empty_story()
        data["角色集"] = [
            {"名称": "主角A", "是否主角": True, "介绍": "主角介绍"},
            {"名称": "路人B", "介绍": "FULL_ONLY_MARKER"},
        ]
        write_json(story, data)

        ctx = build_delta_story_context(story, project, chapter_seq=1)

        assert "FULL_ONLY_MARKER" in ctx
        assert "主角介绍" in ctx

    print("[OK] 小故事(<256KB) Delta 上下文传完整 JSON")


def test_delta_story_context_uses_global_index_and_recent_deltas_when_medium():
    # 达到阈值后采用全局索引和近期 Delta，完整档案仍保留。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = project / "故事结构_增量.json"
        data = empty_story()
        data["角色集"] = [
            {"名称": "主角A", "是否主角": True, "介绍": "主角介绍"},
            {"名称": "路人B", "介绍": "FULL_ONLY_MARKER" + "x" * 280000},
        ]
        write_json(story, data)

        ctx = build_delta_story_context(story, project, chapter_seq=1)

        assert "FULL_ONLY_MARKER" not in ctx
        assert "主角A" in ctx
        assert "路人B" in ctx

    print("[OK] 达到阈值使用全局索引，完整详情可查证")


def test_delta_story_context_large_tier_keeps_all_names_and_lookup_path():
    # 超过阈值时恢复全局索引，不限制索引名称数量。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = project / "故事结构_增量.json"
        data = empty_story()
        data["角色集"] = [
            {"名称": "主角A", "是否主角": True, "介绍": "主角介绍"},
            {"名称": "路人B", "介绍": "FULL_ONLY_MARKER" + "x" * 520000},
        ]
        write_json(story, data)

        ctx = build_delta_story_context(story, project, chapter_seq=1)

        assert "FULL_ONLY_MARKER" not in ctx  # 完整档案通过原件入口读取
        assert "主角A" in ctx  # 全部名称都在
        assert "路人B" in ctx  # 全部名称都在（不只是主角）
        assert "故事结构_增量.json" in ctx  # 附文件路径

    print("[OK] 大故事(≥阈值) Delta 上下文传全部名称索引+文件路径")


def test_delta_story_context_large_tier_all_faction_names():
    # ≥阈值：给全部阵营名称（包括从属和无关阵营），不只主要阵营。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = project / "故事结构_增量.json"
        data = empty_story()
        data["角色集"] = [
            {"名称": "主角A", "是否主角": True, "所属阵营": ["大公会"], "介绍": "x" * 280000},
        ]
        data["阵营集"] = [
            {"名称": "大公会", "父级阵营": ""},
            {"名称": "大公会下属小队", "父级阵营": "大公会"},
            {"名称": "无关边缘公会", "父级阵营": ""},
        ]
        write_json(story, data)

        ctx = build_delta_story_context(story, project, chapter_seq=1)

        assert "大公会" in ctx
        assert "无关边缘公会" in ctx
        assert "大公会下属小队" in ctx  # 从属阵营也在（全部名称）

    print("[OK] ≥阈值档给全部阵营名称")


def test_delta_story_context_medium_tier_includes_actual_recent_deltas():
    # ≥256KB（中量）：名称摘要 + 最近10章 Delta 实际内容（不只是名称）。
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        story = project / "故事结构_增量.json"
        data = empty_story()
        data["角色集"] = [
            {"名称": "主角A", "是否主角": True, "介绍": "主角介绍"},
            {"名称": "路人B", "介绍": "FULL_ONLY_MARKER" + "x" * 280000},
        ]
        write_json(story, data)
        # 准备前一章已提交的 Delta，带一个可识别的实际内容标记
        prev_chapter = project / "原文拆解" / "第001章_测试.md"
        prev_chapter.parent.mkdir(parents=True, exist_ok=True)
        prev_chapter.write_text("# 第001章\n原文。\n", encoding="utf-8")
        prev_paths = artifact_paths(project, prev_chapter)
        prev_paths["delta"].parent.mkdir(parents=True, exist_ok=True)
        write_json(prev_paths["delta"], {
            "章节": "第001章",
            "新增元素": {"角色集": [{"名称": "前章角色C"}], "事件集": [], "地点集": [],
                          "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []},
            "修改元素": {"角色集": [], "事件集": [], "地点集": [],
                          "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []},
        })

        ctx = build_delta_story_context(story, project, chapter_seq=2)

        assert "前章角色C" in ctx  # 实际 Delta 内容被嵌入
        assert "FULL_ONLY_MARKER" not in ctx  # 完整档案通过原件入口读取
        assert "第001章 Delta:" in ctx  # 实际 Delta 块标记

    print("[OK] 达到阈值使用全局索引与最近十章完整 Delta")


if __name__ == "__main__":
    test_audit_pack_cli_creates_review_package()
    test_run_hands_periodic_audit_to_agent_then_continues_after_real_patch()
    test_run_with_audit_worker_commits_periodic_audit_and_continues()
    test_manual_governance_reports_remain_separate()
    test_governance_patch_compresses_tags_before_validation()
    test_chapter_delta_repairs_llm_json_before_validation()
    test_governance_patch_repairs_llm_json_before_validation()
    test_periodic_governance_skipped_when_no_committed_chapters()
    test_commit_governance_rejects_no_change_missing_metadata()
    test_delta_story_context_passes_full_story_when_small()
    test_delta_story_context_uses_global_index_and_recent_deltas_when_medium()
    test_delta_story_context_large_tier_keeps_all_names_and_lookup_path()
    test_delta_story_context_medium_tier_includes_actual_recent_deltas()
    print("\n全部测试通过 [PASS]")
