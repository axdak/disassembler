#!/usr/bin/env python3
"""章节分析与 Delta 两阶段交接测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ROOT))

from run_pipeline import artifact_paths, cmd_run, effective_run_mode, resolve_worker_commands


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_worker_provider_supplies_builtin_worker_commands():
    chapter_command, audit_command = resolve_worker_commands("", "", "agy", "worker")

    assert "tools" in chapter_command
    assert "agent_worker.py" in chapter_command
    assert "--provider agy" in chapter_command
    assert audit_command == chapter_command


def test_codebuddy_worker_provider_supplies_builtin_worker_commands():
    chapter_command, audit_command = resolve_worker_commands("", "", "codebuddy", "worker")

    assert "agent_worker.py" in chapter_command
    assert "--provider codebuddy" in chapter_command
    assert audit_command == chapter_command


def test_non_worker_routes_do_not_inject_external_worker_commands():
    chapter_command, audit_command = resolve_worker_commands("", "", "codebuddy", "serial")

    assert chapter_command == ""
    assert audit_command == ""
    assert effective_run_mode("serial", worker_provider="codebuddy") == "serial"
    assert effective_run_mode("subagent", worker_provider="codebuddy") == "subagent"
    assert effective_run_mode("auto", worker_provider="codebuddy") == "worker"


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": "两阶段章节任务测试"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }


def empty_delta(seq):
    empty = {
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    return {"章节": f"第{seq:03d}章", "新增元素": empty, "修改元素": empty}


def valid_analysis(seq=1):
    return f"""# 第{seq:03d}章分析

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


def test_run_hands_off_analysis_then_delta_before_committing():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["analysis_task"].is_file()
        assert not paths["delta_task"].is_file()
        assert not paths["delta"].is_file()

        paths["analysis"].write_text(valid_analysis(), encoding="utf-8")

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["delta_task"].is_file()
        delta_task = paths["delta_task"].read_text(encoding="utf-8")
        assert "## 1. 剧情梗概" in delta_task
        assert not paths["delta"].is_file()

        write_json(paths["delta"], empty_delta(1))

        assert cmd_run(project, audit_interval=0) == 0
        assert paths["after"].is_file()

    print("[OK] run 依次交接章节分析和Delta任务后才提交章节")


def test_run_regenerates_structurally_shrunken_analysis_before_delta_handoff():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)

        assert cmd_run(project, audit_interval=0) == 2
        paths["analysis"].write_text("# 第001章分析\n本章发生了一件事。\n", encoding="utf-8")

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["analysis_regenerate_task"].is_file()
        assert not paths["delta_task"].is_file()
        regenerate_task = paths["analysis_regenerate_task"].read_text(encoding="utf-8")
        assert "重新生成" in regenerate_task
        assert "不要参考当前不合格的章节分析MD" in regenerate_task

        paths["analysis"].write_text(valid_analysis(), encoding="utf-8")
        assert cmd_run(project, audit_interval=0) == 2
        assert paths["delta_task"].is_file()

    print("[OK] 缩水章节分析会重新生成，不能直接交接Delta")


def test_analysis_phase_completes_all_valid_md_without_creating_delta_artifacts():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapters = []
        for seq in (1, 2):
            chapter = project / "原文拆解" / f"第{seq:03d}章_测试.md"
            chapter.parent.mkdir(parents=True, exist_ok=True)
            chapter.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
            chapters.append((seq, chapter, artifact_paths(project, chapter)))

        assert cmd_run(project, audit_interval=0, phase="analysis") == 2
        assert chapters[0][2]["analysis_task"].is_file()
        assert not chapters[0][2]["delta_task"].exists()

        chapters[0][2]["analysis"].write_text(valid_analysis(1), encoding="utf-8")
        assert cmd_run(project, audit_interval=0, phase="analysis") == 2
        assert chapters[1][2]["analysis_task"].is_file()
        assert not chapters[1][2]["delta_task"].exists()

        chapters[1][2]["analysis"].write_text(valid_analysis(2), encoding="utf-8")
        assert cmd_run(project, audit_interval=0, phase="analysis") == 0
        assert not any(paths["delta_task"].exists() for _, _, paths in chapters)
        assert not any(paths["delta"].exists() for _, _, paths in chapters)
        assert not list((project / "故事结构版本").glob("story_*"))


def test_analysis_phase_hands_off_regeneration_for_invalid_md_only():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)
        paths["analysis"].parent.mkdir(parents=True, exist_ok=True)
        paths["analysis"].write_text("# 第001章分析\n本章发生了一件事。\n", encoding="utf-8")

        assert cmd_run(project, audit_interval=0, phase="analysis") == 2
        assert paths["analysis_regenerate_task"].is_file()
        assert not paths["delta_task"].exists()
        assert not paths["delta"].exists()


def test_run_cli_phase_analysis_generates_analysis_task_without_delta_task():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)

        rc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_pipeline.py"),
                "run",
                str(project),
                "--audit-interval",
                "0",
                "--phase",
                "analysis",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        assert rc.returncode == 2
        assert paths["analysis_task"].is_file()
        assert not paths["delta_task"].exists()
        assert not paths["delta"].exists()


def test_delta_phase_requires_every_analysis_md_before_first_delta_task():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapters = []
        for seq in (1, 2):
            chapter = project / "原文拆解" / f"第{seq:03d}章_测试.md"
            chapter.parent.mkdir(parents=True, exist_ok=True)
            chapter.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
            paths = artifact_paths(project, chapter)
            paths["analysis"].parent.mkdir(parents=True, exist_ok=True)
            chapters.append(paths)
        chapters[0]["analysis"].write_text(valid_analysis(1), encoding="utf-8")

        assert cmd_run(project, audit_interval=0, phase="delta") == 2
        assert chapters[1]["analysis_task"].is_file()
        assert not chapters[0]["delta_task"].exists()
        assert not chapters[0]["delta"].exists()
        assert not list((project / "故事结构版本").glob("story_*"))


def write_simple_chapter_worker(worker: Path, seq: int = 1) -> None:
    analysis_text = valid_analysis(seq)
    delta_text = json.dumps(empty_delta(seq), ensure_ascii=False, indent=2)
    worker.write_text(
        f"""
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
output.parent.mkdir(parents=True, exist_ok=True)

if task_type in ("analysis", "analysis_regenerate"):
    output.write_text({analysis_text!r}, encoding="utf-8")
elif task_type in ("delta", "repair_chapter"):
    output.write_text({delta_text!r}, encoding="utf-8")
else:
    raise SystemExit(f"unexpected task type: {{task_type}}")
""",
        encoding="utf-8",
    )


def test_worker_route_defaults_to_supervised_handoffs_after_each_worker_task():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        write_json(project / "\u6545\u4e8b\u7ed3\u6784_\u589e\u91cf.json", empty_story())
        chapter = project / "\u539f\u6587\u62c6\u89e3" / "\u7b2c001\u7ae0_test.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# chapter 001\nsource text.\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)
        worker = root / "chapter_worker.py"
        write_simple_chapter_worker(worker)
        command = f'"{sys.executable}" "{worker}"'

        rc = cmd_run(
            project,
            audit_interval=0,
            chapter_command=command,
            run_mode="worker",
            worker_window="hidden",
        )
        assert rc == 2
        assert paths["analysis"].is_file()
        assert not paths["delta"].is_file()
        assert not paths["after"].is_file()

        rc = cmd_run(
            project,
            audit_interval=0,
            chapter_command=command,
            run_mode="worker",
            worker_window="hidden",
        )
        assert rc == 2
        assert paths["delta"].is_file()
        assert not paths["after"].is_file()

        rc = cmd_run(
            project,
            audit_interval=0,
            chapter_command=command,
            run_mode="worker",
            worker_window="hidden",
        )
        assert rc == 2
        assert paths["after"].is_file()

        assert cmd_run(
            project,
            audit_interval=0,
            chapter_command=command,
            run_mode="worker",
            worker_window="hidden",
        ) == 0


def test_run_with_chapter_worker_continuous_loop_completes_analysis_and_delta():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)
        worker = root / "chapter_worker.py"
        worker.write_text(
            """
import json
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
output = Path(os.environ["ND_EXPECTED_OUTPUT"])
output.parent.mkdir(parents=True, exist_ok=True)

if task_type in ("analysis", "analysis_regenerate"):
    output.write_text('''# 第001章分析

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
''', encoding="utf-8")
elif task_type in ("delta", "repair_chapter"):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    output.write_text(json.dumps({"章节": "第001章", "新增元素": empty, "修改元素": empty}, ensure_ascii=False, indent=2), encoding="utf-8")
else:
    raise SystemExit(f"unexpected task type: {task_type}")
""",
            encoding="utf-8",
        )

        rc = cmd_run(
            project,
            audit_interval=0,
            chapter_command=f'"{sys.executable}" "{worker}"',
            worker_window="hidden",
            worker_loop="continuous",
        )

        assert rc == 0
        assert paths["analysis"].is_file()
        assert paths["delta"].is_file()
        assert paths["after"].is_file()


def test_run_with_worker_provider_uses_builtin_wrapper_for_chapter_flow():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        write_json(project / "\u6545\u4e8b\u7ed3\u6784_\u589e\u91cf.json", empty_story())
        chapter = project / "\u539f\u6587\u62c6\u89e3" / "\u7b2c001\u7ae0_test.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# chapter 001\nsource text.\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)
        fake_cli = root / "fake_agy_cli.py"
        fake_cli.write_text(
            """
import json
import os
import sys
from pathlib import Path

sys.stdin.read()
task_type = os.environ["ND_TASK_TYPE"]
output = Path(os.environ["ND_EXPECTED_OUTPUT"])

def emit(text):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text.strip() + "\\n", encoding="utf-8")

if task_type in ("analysis", "analysis_regenerate"):
    emit('''# 第001章分析
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
应提取来信事件、约定地点与陌生人线索。''')
elif task_type in ("delta", "repair_chapter"):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    emit(json.dumps({"章节": "第001章", "新增元素": empty, "修改元素": empty}, ensure_ascii=False, indent=2))
else:
    raise SystemExit(f"unexpected task type: {task_type}")
""",
            encoding="utf-8",
        )
        old_agy = os.environ.get("ND_AGY_BIN")
        os.environ["ND_AGY_BIN"] = f"{sys.executable} {fake_cli}"
        try:
            rc = cmd_run(
                project,
                audit_interval=0,
                worker_provider="agy",
                run_mode="worker",
                worker_window="hidden",
                worker_loop="continuous",
            )
        finally:
            if old_agy is None:
                os.environ.pop("ND_AGY_BIN", None)
            else:
                os.environ["ND_AGY_BIN"] = old_agy

        assert rc == 0
        assert paths["analysis"].is_file()
        assert paths["delta"].is_file()
        assert paths["after"].is_file()


def test_run_mode_serial_returns_handoff_even_when_worker_provider_is_configured():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        write_json(project / "\u6545\u4e8b\u7ed3\u6784_\u589e\u91cf.json", empty_story())
        chapter = project / "\u539f\u6587\u62c6\u89e3" / "\u7b2c001\u7ae0_test.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# chapter 001\nsource text.\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)
        fake_cli = root / "fake_codebuddy_cli.py"
        fake_cli.write_text(
            """
raise SystemExit("serial route must not call provider")
""",
            encoding="utf-8",
        )
        old_codebuddy = os.environ.get("ND_CODEBUDDY_BIN")
        os.environ["ND_CODEBUDDY_BIN"] = f"{sys.executable} {fake_cli}"
        try:
            rc = cmd_run(
                project,
                audit_interval=0,
                worker_provider="codebuddy",
                run_mode="serial",
                worker_window="hidden",
            )
        finally:
            if old_codebuddy is None:
                os.environ.pop("ND_CODEBUDDY_BIN", None)
            else:
                os.environ["ND_CODEBUDDY_BIN"] = old_codebuddy

        assert rc == 2
        assert paths["analysis_task"].is_file()
        assert not paths["analysis"].is_file()


if __name__ == "__main__":
    test_run_hands_off_analysis_then_delta_before_committing()
    test_run_regenerates_structurally_shrunken_analysis_before_delta_handoff()
    test_analysis_phase_completes_all_valid_md_without_creating_delta_artifacts()
    test_analysis_phase_hands_off_regeneration_for_invalid_md_only()
    test_run_cli_phase_analysis_generates_analysis_task_without_delta_task()
    test_delta_phase_requires_every_analysis_md_before_first_delta_task()
    test_worker_route_defaults_to_supervised_handoffs_after_each_worker_task()
    test_run_with_chapter_worker_continuous_loop_completes_analysis_and_delta()
    print("\n全部测试通过 [PASS]")
