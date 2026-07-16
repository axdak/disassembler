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

from run_pipeline import cmd_run, commit_governance, prepare_audit_pack_or_split, write_audit_pack


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
    write_json(project / "章节处理" / f"{base}.json", empty_delta(seq))
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


def test_audit_pack_falls_back_to_story_index_when_full_story_exceeds_budget():
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


def test_audit_pack_attempts_full_story_before_fallback_for_very_large_file():
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
        assert attempts[:2] == ["full_story", "story_index_relevant"]
        assert status["context"]["render_mode"] == "story_index_relevant"


def test_audit_pack_blocks_instead_of_truncating_current_evidence_when_context_overflows():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = write_completed_chapter(project, 1)
        chapter.write_text("原文证据" * 10000, encoding="utf-8")

        pack = write_audit_pack(project, 1, 1, max_context_chars=12000)

        assert pack is None
        status = load_status(project / "质量治理" / "周期审计" / "audit_001-001.status.json")
        assert status["status"] == "blocked_context"
        assert status["context"]["current_evidence_chars"] >= len("原文证据" * 10000)
        assert not (project / "质量治理" / "周期审计" / "audit_001-001.md").exists()


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
        write_json(project / "章节处理" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))
        rc = cmd_run(project)
        assert rc == 2
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        assert load_status(status)["status"] == "awaiting_agent"
        assert not (project / "故事结构版本" / "story_after_ch006.json").is_file()

        correction = project / "质量治理" / "周期审计" / "correction_001-005.json"
        empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
        write_json(correction, {
            "章节范围": "第001章-第005章",
            "治理类型": "no_change",
            "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
            "证据范围": ["第001章", "第002章", "第003章", "第004章", "第005章"],
            "新增元素": empty,
            "修改元素": empty,
        })

        rc = cmd_run(project)
        assert rc == 0
        assert load_status(status)["status"] == "committed"
        assert list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "按需治理").glob("*governance_*.txt"))
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()

    print("[OK] run 将周期审计交给Agent，提交真实补丁后继续下一章")


def test_run_with_audit_worker_commits_periodic_audit_and_continues():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 5)
        (project / "章节处理" / "第005章_测试.md").write_text(valid_analysis(5), encoding="utf-8")
        write_json(project / "章节处理" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))
        worker = root / "audit_worker.py"
        worker.write_text(
            """
import json
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
if task_type not in ("audit_correction", "audit_repair"):
    raise SystemExit(f"unexpected task type: {task_type}")

empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "章节范围": "第001章-第005章",
    "治理类型": "no_change",
    "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
    "证据范围": ["第001章", "第002章", "第003章", "第004章", "第005章"],
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
        write_json(project / "章节处理" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))

        worker = root / "split_audit_worker.py"
        worker.write_text(
            """
import json
import os
import re
from pathlib import Path

output = Path(os.environ["ND_EXPECTED_OUTPUT"])
match = re.match(r"correction_(\\d{3})-(\\d{3})\\.json", output.name)
if not match:
    raise SystemExit(f"unexpected output: {output}")
start, end = match.groups()
chapters = [f"第{seq:03d}章" for seq in range(int(start), int(end) + 1)]
empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "章节范围": f"第{start}章-第{end}章",
    "治理类型": "no_change",
    "质量说明": "已检查本子区间角色、事件、地点、线索、阵营、物品，无需合并或降级。",
    "证据范围": chapters,
    "新增元素": empty,
    "修改元素": empty,
}, ensure_ascii=False, indent=2), encoding="utf-8")
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
        assert parent["status"] == "covered_by_children"
        assert {item["range"] for item in parent["child_ranges"]} == {"001-003", "004-005"}
        assert load_status(audit_dir / "audit_001-003.status.json")["status"] == "covered_by_children"
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
        (project / "章节处理" / f"{base}.json").write_text(malformed_delta, encoding="utf-8")

        rc = cmd_run(project)

        assert rc == 0
        repaired = json.loads((project / "章节处理" / f"{base}.json").read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    test_audit_pack_cli_creates_review_package()
    test_run_hands_periodic_audit_to_agent_then_continues_after_real_patch()
    test_run_with_audit_worker_commits_periodic_audit_and_continues()
    test_manual_governance_reports_remain_separate()
    test_governance_patch_compresses_tags_before_validation()
    test_chapter_delta_repairs_llm_json_before_validation()
    test_governance_patch_repairs_llm_json_before_validation()
    print("\n全部测试通过 [PASS]")
