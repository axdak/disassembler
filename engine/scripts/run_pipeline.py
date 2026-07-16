#!/usr/bin/env python3
"""
拆书主控器（可断点、可恢复、不会假装自动调用大模型）。

它负责：
- 初始化/拆章/目录/进度；
- 分别生成章节分析任务与Delta提取任务，让模型先写MD、再写JSON；
- 对已经填写好的章节产物自动校验、合并、规范化、快照、diff、更新进度；
- 失败时回滚并生成 repair_prompt；
- 全书/局部分析任务包生成。

重要：脚本默认不直接调用大模型。未配置 worker 命令时，遇到缺章节分析或 Delta 会生成任务包并明确暂停点；
配置 worker 命令时，脚本只调用外部 worker 写当前任务包指定产物，再由本脚本验收、校验、合并和回滚。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chronology import chapter_time
from story_schema_rules import COLLECTION_KEYS
from validate_chapter_analysis import validate_chapter_analysis_file
from validate_structure import validate_structure_file

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认 GBK，遇到中文/校验报告中的 \\ufffd 替换字符会抛
    UnicodeEncodeError。reconfigure 是 Python 3.7+ 的标准做法；在 Linux/macOS
    上等价于 no-op（本来就是 UTF-8）。模块导入时立即生效，确保 main() 入口、
    被测试直接调用的 cmd_* 函数、以及任何 print 调用都受保护。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_ensure_utf8_console()

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(PROMPTS_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render_prompt(name: str, **context: Any) -> str:
    """渲染 prompts/<name>.j2 任务包模板。

    所有任务包都从 prompts/ 单一来源生成，避免脚本和模板漂移。
    缺变量时 StrictUndefined 立刻报错，不静默渲染成空。
    """
    return _JINJA_ENV.get_template(name).render(**context)

REQUIRED_DIRS = [
    "原文", "原文拆解", "章节处理", "章节处理/_任务包", "章节处理/_修复任务",
    "质量治理/delta校验", "质量治理/章节校验", "质量治理/规范化", "质量治理/周期审计", "质量治理/按需治理", "质量治理/最终审计",
    "故事结构版本", "结构变更日志",
    "全书分析/_任务包", "全书分析/剧情结构", "全书分析/人物分析", "全书分析/风格分析", "全书分析/世界观", "全书分析/视觉资产", "全书分析/故事结构", "全书分析/迭代版本", "全书分析/局部分析",
]
ANALYSIS_OUTPUTS = {
    "summary": ["全书分析/剧情结构/章节梗概汇总.md"],
    "characters": ["全书分析/人物分析/人物档案.md", "全书分析/人物分析/人物关系.md", "全书分析/人物分析/人物提及.json"],
    "plot": ["全书分析/剧情结构/剧情线索.md", "全书分析/剧情结构/伏笔追踪.md", "全书分析/剧情结构/冲突图谱.md"],
    "style": ["全书分析/风格分析/文风分析.md", "全书分析/风格分析/节奏分析.md", "全书分析/风格分析/高光场景.md"],
    "report": ["全书分析/拆书总报告.md"],
    "worldview": ["全书分析/世界观/世界观档案.md"],
    "settings": ["全书分析/世界观/设定档案.md"],
    "plotlines": ["全书分析/剧情结构/剧情线总表.md", "全书分析/剧情结构/剧情线交汇矩阵.md"],
    "outline": ["全书分析/剧情结构/全书大纲.md"],
    "detailed_outline": ["全书分析/剧情结构/章节细纲.md"],
    "narrative_structure": [
        "全书分析/故事结构/三幕式结构图.md",
        "全书分析/故事结构/Brooks四部分结构图.md",
        "全书分析/故事结构/Freytag五段结构图.md",
        "全书分析/故事结构/故事七要素档案.md",
        "全书分析/故事结构/故事力学评估.md",
        "全书分析/故事结构/故事工程学评估.md",
        "全书分析/故事结构/小说骨架.md",
    ],
    "visual_assets": ["全书分析/视觉资产/视觉资产清单.md", "全书分析/视觉资产/关键场景分镜表.md", "全书分析/视觉资产/AI绘图提示词素材.md", "全书分析/视觉资产/角色外观一致性表.md", "全书分析/视觉资产/场景氛围表.md"],
}
MULTI_UNIT_ARTIFACT_RE = re.compile(r"^第\d+\s*(?:-|—|–|~|～|至|到)\s*\d+章_.*\.(?:md|json)$")
# 单次子进程调用超时（秒）。生产场景默认 600；测试可用 ND_COMMAND_TIMEOUT=10
# 之类的更短值，让卡死的子进程更快暴露而不是等满 10 分钟。
INTERNAL_COMMAND_TIMEOUT_SECONDS = int(os.getenv("ND_COMMAND_TIMEOUT", "600"))
DEFAULT_WORKER_TIMEOUT_SECONDS = int(os.getenv("ND_WORKER_TIMEOUT", "1800"))
DEFAULT_WORKER_RETRIES = int(os.getenv("ND_WORKER_RETRIES", "2"))
RUN_MODES = ("auto", "subagent", "serial", "worker")
WORKER_PROVIDERS = ("manual", "auto", "agy", "codebuddy")
WORKER_WINDOWS = ("hidden", "powershell", "powershell-keep")
WORKER_LOOPS = ("supervised", "continuous")
DEFAULT_WORKER_WINDOW = os.getenv("ND_WORKER_WINDOW", "hidden")
if DEFAULT_WORKER_WINDOW not in WORKER_WINDOWS:
    DEFAULT_WORKER_WINDOW = "hidden"
DEFAULT_WORKER_LOOP = os.getenv("ND_WORKER_LOOP", "supervised")
if DEFAULT_WORKER_LOOP not in WORKER_LOOPS:
    DEFAULT_WORKER_LOOP = "supervised"

# 周期审计由外部 Agent 执行，脚本无法得知实际模型 tokenizer，故采用可配置的
# 字符预算而不伪造精确 token 计数。默认值为 256k 上下文模型预留输出、系统提示和
# 工具调用后的保守输入上限；调用方可按实际模型覆盖。
DEFAULT_AUDIT_MAX_CONTEXT_CHARS = 160_000
AUDIT_CONTEXT_FORMAT_VERSION = 2
AUDIT_RESOLVED_STATUSES = {"committed", "covered_by_children"}


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def script_path(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def timeout_output(exc: subprocess.TimeoutExpired, timeout_seconds: float) -> str:
    """Return any partial output together with a stable timeout diagnostic."""
    output = exc.stdout or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return f"{output}\n命令执行超时（{timeout_seconds:g} 秒），已终止。\n"


def stream_shell_command(
    command: str,
    cwd: Path,
    env: Dict[str, str],
    timeout: float,
) -> Tuple[int, str]:
    """Run a shell command while forwarding combined stdout/stderr live."""
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output_queue: "queue.Queue[str]" = queue.Queue()

    def read_output() -> None:
        assert proc.stdout is not None
        try:
            for line in iter(proc.stdout.readline, ""):
                output_queue.put(line)
        finally:
            proc.stdout.close()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output_parts: List[str] = []
    deadline = time.monotonic() + timeout if timeout else None

    def drain_output() -> None:
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                break
            output_parts.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()

    while True:
        drain_output()
        if proc.poll() is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            proc.kill()
            proc.wait()
            reader.join(timeout=1)
            drain_output()
            message = timeout_output(subprocess.TimeoutExpired(command, timeout, output=""), timeout)
            output_parts.append(message)
            sys.stdout.write(message)
            sys.stdout.flush()
            return 124, "".join(output_parts)
        time.sleep(0.1)

    reader.join(timeout=1)
    drain_output()
    return proc.returncode or 0, "".join(output_parts)


def ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_visible_powershell_command(
    command: str,
    cwd: Path,
    env: Dict[str, str],
    timeout: float,
    log_path: Path,
    keep_open: bool = False,
) -> Tuple[int, str]:
    """Run a command in a visible PowerShell window and mirror output to a log."""
    if os.name != "nt":
        return stream_shell_command(command, cwd, env, timeout)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env_lines = [
        f"$env:{key} = {ps_single_quote(value)}"
        for key, value in sorted(env.items())
        if key.startswith("ND_") or key in {"PYTHONIOENCODING", "NODE_OPTIONS"}
    ]
    close_lines = [
        "Write-Host ''",
        "Write-Host ('worker exited with code: ' + $rc)",
    ]
    if keep_open:
        close_lines.append("Read-Host 'Press Enter to close this worker window' | Out-Null")
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Continue'",
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
            "$Host.UI.RawUI.WindowTitle = 'novel-disassembler worker: ' + $env:ND_TASK_TYPE",
            f"Set-Location -LiteralPath {ps_single_quote(str(cwd))}",
            *env_lines,
            f"$logPath = {ps_single_quote(str(log_path))}",
            "$rcPath = [System.IO.Path]::ChangeExtension($logPath, '.rc')",
            f"$cmd = {ps_single_quote(command)}",
            "Write-Host ('worker cwd: ' + (Get-Location).Path)",
            "Write-Host ('worker visible command: ' + $cmd)",
            "& cmd.exe /d /s /c $cmd 2>&1 | Tee-Object -FilePath $logPath -Append",
            "$rc = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }",
            "Set-Content -LiteralPath $rcPath -Value $rc -Encoding UTF8",
            *close_lines,
            "exit $rc",
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tmp:
        tmp.write(script)
        script_path = Path(tmp.name)
    try:
        args = [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        proc = subprocess.Popen(
            ["powershell.exe", *args],
            cwd=str(cwd),
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        try:
            proc.wait(timeout=timeout + 10 if timeout else None)
            launcher_output = ""
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            launcher_output = ""
            message = timeout_output(subprocess.TimeoutExpired(command, timeout, output=launcher_output), timeout)
            log_path.write_text((log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "") + message, encoding="utf-8")
            return 124, (launcher_output or "") + message
        output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else (launcher_output or "")
        if launcher_output:
            output = launcher_output + output
        rc_path = log_path.with_suffix(".rc")
        if rc_path.exists():
            try:
                returncode = int(rc_path.read_text(encoding="utf-8", errors="replace").strip())
            except ValueError:
                returncode = proc.returncode or 1
            try:
                rc_path.unlink()
            except OSError:
                pass
        else:
            returncode = proc.returncode or 0
        return returncode, output
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def run_cmd(
    cmd: List[str],
    report_path: Optional[Path] = None,
    timeout: float = INTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> int:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output = proc.stdout or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = timeout_output(exc, timeout)
        returncode = 124
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output or "", encoding="utf-8")
    else:
        print(output)
    return returncode


def worker_log_path(project_dir: Path, task_type: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_type)
    return project_dir / "质量治理" / "worker日志" / f"{safe_task}_{ts}.txt"


def effective_run_mode(
    run_mode: str,
    chapter_command: str = "",
    audit_command: str = "",
    worker_provider: str = "manual",
) -> str:
    if not run_mode:
        run_mode = "auto"
    if run_mode not in RUN_MODES:
        raise ValueError(f"unsupported run mode: {run_mode}")
    if run_mode != "auto":
        return run_mode
    has_worker_config = bool(chapter_command or audit_command or (worker_provider and worker_provider != "manual"))
    return "worker" if has_worker_config else "subagent"


def builtin_agent_worker_command(provider: str) -> str:
    if provider not in WORKER_PROVIDERS or provider == "manual":
        raise ValueError(f"unsupported worker provider: {provider}")
    wrapper = Path(__file__).resolve().parent.parent / "tools" / "agent_worker.py"
    return subprocess.list2cmdline([sys.executable, str(wrapper), "--provider", provider])


def resolve_worker_commands(
    chapter_command: str,
    audit_command: str,
    worker_provider: str,
    run_mode: str = "worker",
) -> Tuple[str, str]:
    if effective_run_mode(run_mode, chapter_command, audit_command, worker_provider) != "worker":
        return "", ""
    if not worker_provider or worker_provider == "manual":
        return chapter_command, audit_command
    command = builtin_agent_worker_command(worker_provider)
    return chapter_command or command, audit_command or command


def run_worker(
    command: str,
    project_dir: Path,
    task_type: str,
    task_pack: Path,
    expected_output: Path,
    chapter_seq: int = 0,
    timeout: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    retries: int = 0,
    worker_window: str = DEFAULT_WORKER_WINDOW,
) -> int:
    """Run an external model/agent worker for exactly one task package."""
    if not command:
        return 2
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "ND_TASK_TYPE": task_type,
            "ND_PROJECT_DIR": str(project_dir),
            "ND_TASK_PACK": str(task_pack),
            "ND_EXPECTED_OUTPUT": str(expected_output),
            "ND_CHAPTER_SEQ": str(chapter_seq or ""),
            "ND_WORKER_ATTEMPT": str(attempt),
        })
        log_path = worker_log_path(project_dir, task_type)
        print(f"worker start: task={task_type} attempt={attempt}/{attempts} log={log_path}")
        if worker_window in ("powershell", "powershell-keep"):
            if worker_window == "powershell-keep":
                print("worker window: opening visible PowerShell; press Enter in that window after it exits.")
            else:
                print("worker window: opening visible PowerShell; it will close when the worker exits.")
            returncode, output = run_visible_powershell_command(
                command,
                project_dir,
                env,
                timeout,
                log_path,
                keep_open=worker_window == "powershell-keep",
            )
        else:
            returncode, output = stream_shell_command(command, project_dir, env, timeout)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
        if returncode != 0:
            print(f"worker失败: task={task_type} attempt={attempt}/{attempts} rc={returncode} log={log_path}")
            if attempt < attempts:
                continue
            return returncode
        if not expected_output.is_file() or expected_output.stat().st_size == 0:
            print(f"worker未写出期望产物: {expected_output}")
            print(f"worker日志: {log_path}")
            if attempt < attempts:
                continue
            return 1
        print(f"worker完成: task={task_type} output={expected_output} log={log_path}")
        return 0
    return 1


def display_shell_command(args: List[str]) -> str:
    return subprocess.list2cmdline([str(arg) for arg in args if str(arg) != ""])


def build_run_foreground_command(
    project_dir: Path,
    *,
    phase: str = "",
    max_chapters: int = 0,
    audit_interval: int = 5,
    audit_max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    run_mode: str = "worker",
    worker_provider: str = "manual",
    chapter_command: str = "",
    audit_command: str = "",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
) -> str:
    args: List[str] = [sys.executable, str(Path(__file__).resolve()), "run", str(project_dir)]
    if max_chapters:
        args.extend(["--max-chapters", str(max_chapters)])
    if audit_interval != 5:
        args.extend(["--audit-interval", str(audit_interval)])
    if phase:
        args.extend(["--phase", phase])
    if audit_max_context_chars != DEFAULT_AUDIT_MAX_CONTEXT_CHARS:
        args.extend(["--audit-max-context-chars", str(audit_max_context_chars)])
    args.extend(["--run-mode", run_mode])
    if worker_provider and worker_provider != "manual":
        args.extend(["--worker-provider", worker_provider])
    else:
        if chapter_command:
            args.extend(["--chapter-command", chapter_command])
        if audit_command:
            args.extend(["--audit-command", audit_command])
    if worker_timeout_seconds != DEFAULT_WORKER_TIMEOUT_SECONDS:
        args.extend(["--worker-timeout-seconds", str(worker_timeout_seconds)])
    if worker_retries != DEFAULT_WORKER_RETRIES:
        args.extend(["--worker-retries", str(worker_retries)])
    if worker_loop != DEFAULT_WORKER_LOOP:
        args.extend(["--worker-loop", worker_loop])
    if worker_window != DEFAULT_WORKER_WINDOW:
        args.extend(["--worker-window", worker_window])
    return display_shell_command(args)


def build_analysis_auto_foreground_command(args: argparse.Namespace, task: str, run_mode: str) -> str:
    command_name = "visual-assets-auto" if task == "visual_assets" else "chapter-structure-auto"
    command: List[str] = [sys.executable, str(Path(__file__).resolve()), command_name, str(args.project_dir)]
    for name, flag, default in [
        ("include_original", "--include-original", "sample"),
        ("max_pack_chars", "--max-pack-chars", 70000),
        ("max_original_chars", "--max-original-chars", 6000),
        ("max_analysis_chars", "--max-analysis-chars", 12000),
    ]:
        value = getattr(args, name, default)
        if value != default:
            command.extend([flag, str(value)])
    if task == "visual_assets" and getattr(args, "force_aggregate", False):
        command.append("--force-aggregate")
    command.extend(["--run-mode", run_mode])
    worker_provider = getattr(args, "worker_provider", "manual")
    worker_command = getattr(args, "worker_command", "")
    if worker_provider and worker_provider != "manual":
        command.extend(["--worker-provider", worker_provider])
    elif worker_command:
        command.extend(["--worker-command", worker_command])
    if getattr(args, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS) != DEFAULT_WORKER_TIMEOUT_SECONDS:
        command.extend(["--worker-timeout-seconds", str(args.worker_timeout_seconds)])
    if getattr(args, "worker_retries", DEFAULT_WORKER_RETRIES) != DEFAULT_WORKER_RETRIES:
        command.extend(["--worker-retries", str(args.worker_retries)])
    if getattr(args, "worker_loop", DEFAULT_WORKER_LOOP) != DEFAULT_WORKER_LOOP:
        command.extend(["--worker-loop", str(args.worker_loop)])
    if getattr(args, "worker_window", DEFAULT_WORKER_WINDOW) != DEFAULT_WORKER_WINDOW:
        command.extend(["--worker-window", str(args.worker_window)])
    return display_shell_command(command)


def worker_supervision_checkpoint(worker_loop: str, message: str, rerun_command: str = "") -> bool:
    if worker_loop != "supervised":
        return False
    print(f"WORKER_SUPERVISION_CHECKPOINT: {message}")
    print("This checkpoint is not completion.")
    print("Do not wait for task-notification or treat a background task id as done.")
    print("Main agent must inspect status/logs, wait for the foreground return code, and rerun the same route.")
    if rerun_command:
        print("NEXT_FOREGROUND_COMMAND:")
        print(rerun_command)
    return True


def run_multi_output_worker(
    command: str,
    project_dir: Path,
    task_type: str,
    task_pack: Path,
    expected_outputs: List[Path],
    chapter_seq: int = 0,
    timeout: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    retries: int = 0,
    worker_window: str = DEFAULT_WORKER_WINDOW,
) -> int:
    """Run an external worker for a task package that owns several artifacts."""
    if not command:
        return 2
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "ND_TASK_TYPE": task_type,
            "ND_PROJECT_DIR": str(project_dir),
            "ND_TASK_PACK": str(task_pack),
            "ND_EXPECTED_OUTPUTS": json.dumps([str(path) for path in expected_outputs], ensure_ascii=False),
            "ND_EXPECTED_OUTPUT": str(expected_outputs[0]) if expected_outputs else "",
            "ND_CHAPTER_SEQ": str(chapter_seq or ""),
            "ND_WORKER_ATTEMPT": str(attempt),
        })
        log_path = worker_log_path(project_dir, task_type)
        print(f"worker start: task={task_type} attempt={attempt}/{attempts} log={log_path}")
        if worker_window in ("powershell", "powershell-keep"):
            if worker_window == "powershell-keep":
                print("worker window: opening visible PowerShell; press Enter in that window after it exits.")
            else:
                print("worker window: opening visible PowerShell; it will close when the worker exits.")
            returncode, output = run_visible_powershell_command(
                command,
                project_dir,
                env,
                timeout,
                log_path,
                keep_open=worker_window == "powershell-keep",
            )
        else:
            returncode, output = stream_shell_command(command, project_dir, env, timeout)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
        if returncode != 0:
            print(f"worker failed: task={task_type} attempt={attempt}/{attempts} rc={returncode} log={log_path}")
            if attempt < attempts:
                continue
            return returncode
        missing = [path for path in expected_outputs if not path.is_file() or path.stat().st_size == 0]
        if missing:
            print("worker did not write expected artifact(s):")
            for path in missing:
                print(f"  - {path}")
            print(f"worker log: {log_path}")
            if attempt < attempts:
                continue
            return 1
        print(f"worker complete: task={task_type} outputs={len(expected_outputs)} log={log_path}")
        return 0
    return 1


def repair_llm_json(path: Path, report_path: Path, backup: bool = False) -> int:
    """修复/格式化 LLM 产出的 JSON 语法，保证后续脚本读取严格 JSON。"""
    cmd = [
        sys.executable,
        str(script_path("repair_llm_json.py")),
        str(path),
        "--report",
        str(report_path),
    ]
    if backup:
        cmd.append("--backup")
    return run_cmd(cmd)


def init_dirs(project_dir: Path) -> None:
    for d in REQUIRED_DIRS:
        (project_dir / d).mkdir(parents=True, exist_ok=True)


def find_multi_unit_artifacts(project_dir: Path) -> List[Path]:
    """查找把多个拆分单元合并成一个产物的非法章节处理文件。"""
    chapter_dir = project_dir / "章节处理"
    if not chapter_dir.is_dir():
        return []
    return sorted(
        p for p in chapter_dir.iterdir()
        if p.is_file() and MULTI_UNIT_ARTIFACT_RE.match(p.name)
    )


def guard_single_unit_artifacts(project_dir: Path) -> bool:
    bad = find_multi_unit_artifacts(project_dir)
    if not bad:
        return True
    print("发现非法多切片合并产物，已停止。步骤2必须逐切片处理，每个Delta只能对应 _索引.json 中一个 seq。")
    for path in bad:
        print(f"  - {path}")
    print("请删除或拆分这些文件，再使用 run_pipeline.py prepare-chapter/run 生成并提交单切片产物。")
    return False


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 进程内 normalize 去抖缓存：project_dir → (mtime_ns, size) of story after last normalize。
# 同一进程里 ensure_story 会被反复调用（测试、GUI、连续提交），每次都拉起
# normalize_story_schema 子进程在 Windows 上要 ~2 秒冷启动。只要 story 自上次
# normalize 后没有被任何 in-place 子进程改动（merge_delta、apply_governance 等
# 改完后 mtime 变化会自动让缓存失效），就跳过这次 subprocess。
_ENSURE_STORY_NORMALIZE_CACHE: Dict[str, Tuple[int, int]] = {}


def _story_signature(story: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = story.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def ensure_story(project_dir: Path) -> Path:
    init_dirs(project_dir)
    story = project_dir / "故事结构_增量.json"
    if not story.is_file():
        run_cmd([sys.executable, str(script_path("progress_manager.py")), "init-incremental", str(project_dir)])
    cache_key = str(project_dir.resolve())
    current_sig = _story_signature(story)
    if current_sig is not None and _ENSURE_STORY_NORMALIZE_CACHE.get(cache_key) == current_sig:
        return story
    # 保全式规范化，避免历史字段被强 schema 卡死。
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place"], project_dir / "质量治理" / "规范化" / "normalize_latest.txt")
    post_sig = _story_signature(story)
    if post_sig is not None:
        _ENSURE_STORY_NORMALIZE_CACHE[cache_key] = post_sig
    return story


def chapter_files(project_dir: Path) -> List[Path]:
    split_dir = project_dir / "原文拆解"
    index_path = split_dir / "_索引.json"
    if index_path.is_file():
        index = load_json(index_path, {})
        records = index.get("chapters", []) if isinstance(index, dict) else []
        indexed: List[Tuple[int, Path]] = []
        seen = set()
        for record in records:
            if not isinstance(record, dict) or record.get("is_preface"):
                continue
            seq, filename = record.get("seq"), record.get("filename")
            if not isinstance(seq, int) or seq < 1 or not isinstance(filename, str) or not filename:
                raise ValueError("_索引.json 包含无效章节记录；seq 和 filename 必须有效")
            if seq in seen:
                raise ValueError(f"_索引.json 存在重复seq[{seq}]，无法作为唯一章节依据")
            seen.add(seq)
            path = split_dir / filename
            if path.is_file():
                indexed.append((seq, path))
        if indexed:
            return [path for _, path in sorted(indexed)]
    files = [p for p in split_dir.glob("第*章_*.md") if p.is_file()]
    def key(p: Path):
        m = re.match(r"第(\d+)章", p.name)
        return int(m.group(1)) if m else 10**9
    return sorted(files, key=key)


def seq_from_file(path: Path) -> int:
    m = re.match(r"第(\d+)章", path.name)
    if not m:
        raise ValueError(f"无法从文件名提取章节号: {path.name}")
    return int(m.group(1))


def chapter_by_seq(project_dir: Path, seq: int) -> Optional[Path]:
    index_path = project_dir / "原文拆解" / "_索引.json"
    if index_path.is_file():
        index = load_json(index_path, {})
        records = index.get("chapters", []) if isinstance(index, dict) else []
        matches = [record for record in records if isinstance(record, dict) and not record.get("is_preface") and record.get("seq") == seq]
        if len(matches) > 1:
            raise ValueError(f"_索引.json 存在重复seq[{seq}]，已停止章节处理")
        if len(matches) == 1:
            filename = matches[0].get("filename")
            if isinstance(filename, str) and filename:
                path = project_dir / "原文拆解" / filename
                return path if path.is_file() else None
            raise ValueError(f"_索引.json 的seq[{seq}]缺少filename")
    for p in chapter_files(project_dir):
        if seq_from_file(p) == seq:
            return p
    return None


def artifact_paths(project_dir: Path, chapter: Path) -> Dict[str, Path]:
    seq = seq_from_file(chapter)
    base = chapter.stem
    task_dir = project_dir / "章节处理" / "_任务包"
    return {
        "chapter": chapter,
        "analysis": project_dir / "章节处理" / chapter.name,
        "delta": project_dir / "章节处理" / f"{base}.json",
        "delta_report": project_dir / "质量治理" / "delta校验" / f"{base}.json",
        "chapter_report": project_dir / "质量治理" / "章节校验" / f"{base}.json",
        "before": project_dir / "故事结构版本" / f"story_before_ch{seq:03d}.json",
        "after": project_dir / "故事结构版本" / f"story_after_ch{seq:03d}.json",
        "diff": project_dir / "结构变更日志" / f"diff_ch{seq:03d}.json",
        "analysis_task": task_dir / f"task_ch{seq:03d}_analysis.md",
        "analysis_regenerate_task": task_dir / f"task_ch{seq:03d}_analysis_regenerate.md",
        "delta_task": task_dir / f"task_ch{seq:03d}_delta.md",
        "repair": project_dir / "章节处理" / "_修复任务" / f"repair_ch{seq:03d}.md",
    }


def is_chapter_complete(project_dir: Path, chapter: Path) -> bool:
    p = artifact_paths(project_dir, chapter)
    required = ["analysis", "delta", "delta_report", "chapter_report", "before", "after", "diff"]
    if not all(p[k].is_file() and p[k].stat().st_size > 0 for k in required):
        return False

    try:
        for report_key in ("delta_report", "chapter_report"):
            report = load_json(p[report_key])
            if not isinstance(report, dict) or report.get("passed") is not True or report.get("mode") != "process":
                return False
            if not isinstance(report.get("errors"), list) or not isinstance(report.get("warnings"), list):
                return False
        diff = load_json(p["diff"])
        if not isinstance(diff, dict):
            return False
        after_ok, _, _ = validate_structure_file(str(p["after"]), mode="process")
        return after_ok
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def next_incomplete(project_dir: Path) -> Optional[Path]:
    for ch in chapter_files(project_dir):
        if not is_chapter_complete(project_dir, ch):
            return ch
    return None


def latest_snapshot_seq(project_dir: Path) -> Optional[int]:
    version_dir = project_dir / "故事结构版本"
    if not version_dir.is_dir():
        return None
    seqs = []
    for path in version_dir.glob("story_after_ch*.json"):
        m = re.match(r"story_after_ch(\d+)\.json$", path.name)
        if m:
            seqs.append(int(m.group(1)))
    return max(seqs) if seqs else None


def max_available_delta_seq(project_dir: Path) -> int:
    seqs = []
    for path in (project_dir / "章节处理").glob("第*章_*.json"):
        m = re.match(r"第(\d+)章", path.name)
        if m:
            seqs.append(int(m.group(1)))
    return max(seqs) if seqs else 0


def replay_one_delta(project_dir: Path, seq: int) -> int:
    story = project_dir / "故事结构_增量.json"
    chapter = chapter_by_seq(project_dir, seq)
    if not chapter:
        print(f"重放失败: 找不到第{seq:03d}章原文拆解文件")
        return 1
    p = artifact_paths(project_dir, chapter)
    if not p["delta"].is_file():
        print(f"重放失败: 缺少第{seq:03d}章Delta: {p['delta']}")
        return 1

    repair_json_report = project_dir / "质量治理" / "delta校验" / f"repair_json_ch{seq:03d}.json"
    rc = repair_llm_json(p["delta"], repair_json_report)
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章 LLM JSON 语法修复失败，见: {repair_json_report}")
        return 1

    coerce_delta_report = project_dir / "质量治理" / "delta校验" / f"coerce_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("coerce_delta.py")), str(p["delta"]), "--report", str(coerce_delta_report)])
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章 Delta 机械纠错失败，见: {coerce_delta_report}")
        return 1

    compress_delta_report = project_dir / "质量治理" / "delta校验" / f"compress_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(p["delta"]), "--report", str(compress_delta_report)])
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章 Delta 标签压缩失败，见: {compress_delta_report}")
        return 1

    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "process", "--report-json", str(p["delta_report"]), str(story), str(p["delta"])])
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章Delta校验失败，见: {p['delta_report']}")
        return 1

    shutil.copy2(story, p["before"])
    rollback_tmp = project_dir / "故事结构版本" / f"_replay_rollback_ch{seq:03d}.json"
    shutil.copy2(story, rollback_tmp)

    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(p["delta"])], project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt")
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并失败，已回滚")
        return 1

    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--report", str(project_dir / "质量治理" / "规范化" / f"normalize_ch{seq:03d}.txt")])
    compress_struct_report = project_dir / "质量治理" / "规范化" / f"compress_after_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--structure", str(story), "--report", str(compress_struct_report)])
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并后标签压缩失败，已回滚，见: {compress_struct_report}")
        return 1
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--chapter-check", "--mode", "process", "--report-json", str(p["chapter_report"]), str(story), str(p["delta"])])
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并后结构校验失败，已回滚，见: {p['chapter_report']}")
        return 1

    shutil.copy2(story, p["after"])
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(p["before"]), str(p["after"]), str(p["diff"])])
    print(f"第{seq:03d}章Delta重放完成。")
    return 0


def recover_story_from_snapshot(project_dir: Path, snapshot_seq: Optional[int] = None, to_seq: Optional[int] = None) -> int:
    """从 story_after_chNNN 快照恢复过程库，并重放后续章节 Delta。"""
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    if snapshot_seq is None:
        snapshot_seq = latest_snapshot_seq(project_dir)
    if snapshot_seq is None:
        print("恢复失败: 没有可用的 story_after_chNNN.json 快照")
        return 1
    snapshot = project_dir / "故事结构版本" / f"story_after_ch{snapshot_seq:03d}.json"
    if not snapshot.is_file():
        print(f"恢复失败: 快照不存在: {snapshot}")
        return 1

    if to_seq is None:
        to_seq = max_available_delta_seq(project_dir)
    if to_seq < snapshot_seq:
        print(f"恢复失败: 目标章节 {to_seq:03d} 早于快照章节 {snapshot_seq:03d}")
        return 1

    story = project_dir / "故事结构_增量.json"
    shutil.copy2(snapshot, story)
    print(f"已从快照恢复过程库: story_after_ch{snapshot_seq:03d}.json -> 故事结构_增量.json")

    due_audits = committed_audits_due_for_recovery(project_dir, snapshot_seq, to_seq)
    rc = replay_committed_audits_after_recovery(project_dir, snapshot_seq, due_audits)
    if rc != 0:
        return rc

    for seq in range(snapshot_seq + 1, to_seq + 1):
        rc = replay_one_delta(project_dir, seq)
        if rc != 0:
            return rc
        rc = replay_committed_audits_after_recovery(project_dir, seq, due_audits)
        if rc != 0:
            return rc

    progress_path = project_dir / "进度.json"
    progress = load_json(progress_path, None)
    if isinstance(progress, dict):
        step2 = progress.setdefault("步骤状态", {}).setdefault("2", {"状态": "in_progress", "详情": {}, "完成时间": None})
        details = step2.setdefault("详情", {})
        done = set(details.setdefault("已完成章节", []))
        for ch in chapter_files(project_dir):
            seq = seq_from_file(ch)
            if seq <= to_seq and is_chapter_complete(project_dir, ch):
                done.add(ch.name)
        details["已完成章节"] = sorted(done)
        details["总章节数"] = len(chapter_files(project_dir))
        progress["当前步骤"] = "2"
        progress["更新时间"] = datetime.now().isoformat()
        save_json(progress_path, progress)

    print(f"恢复完成: 基线第{snapshot_seq:03d}章，重放至第{to_seq:03d}章。")
    return 0


def build_lightweight_index(story_path: Path, max_names: int = 80) -> str:
    data = load_json(story_path, {}) or {}
    lines = []
    intro = data.get("介绍", {}) if isinstance(data, dict) else {}
    if isinstance(intro, dict):
        if intro.get("标题"):
            lines.append(f"标题：{intro.get('标题')}")
    for key in COLLECTION_KEYS:
        items = data.get(key, []) if isinstance(data, dict) else []
        names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if not name:
                continue
            aliases = item.get("别名", []) or []
            if aliases:
                names.append(f"{name}[{'/'.join(str(a) for a in aliases[:3])}]")
            else:
                names.append(name)
        lines.append(f"{key}({len(names)}): " + "、".join(names[:max_names]))
        if len(names) > max_names:
            lines[-1] += f" ……另有{len(names)-max_names}个"
    return "\n".join(lines)


def story_summary(story_path: Path, max_names: int = 80) -> str:
    """Backward-compatible name for the story index used by older callers.

    ``build_lightweight_index`` is the current descriptive API name, but the
    test and any external automation that still imports ``story_summary``
    should receive the identical serialized index.
    """
    return build_lightweight_index(story_path, max_names=max_names)


def parse_chapter_range(spec: str) -> Tuple[int, int]:
    text = (spec or "").strip()
    m = re.match(r"^(\d+)\s*(?:-|—|–|~|～|至|到)\s*(\d+)$", text)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
    elif re.match(r"^\d+$", text):
        start = end = int(text)
    else:
        raise ValueError(f"章节范围格式错误: {spec}，示例: 1-5")
    if start <= 0 or end <= 0 or start > end:
        raise ValueError(f"章节范围无效: {spec}")
    return start, end


def audit_paths(project_dir: Path, start: int, end: int) -> Dict[str, Path]:
    base = f"{start:03d}-{end:03d}"
    audit_dir = project_dir / "质量治理" / "周期审计"
    return {
        "dir": audit_dir,
        "pack": audit_dir / f"audit_{base}.md",
        "correction": audit_dir / f"correction_{base}.json",
        "status": audit_dir / f"audit_{base}.status.json",
        "feedback": audit_dir / f"audit_{base}.feedback.md",
    }


def split_audit_range(start: int, end: int) -> List[Tuple[int, int]]:
    if start >= end:
        return []
    mid = (start + end) // 2
    return [(start, mid), (mid + 1, end)]


def audit_range_label(start: int, end: int) -> str:
    return f"{start:03d}-{end:03d}"


def audit_child_ranges_from_status(data: Dict[str, Any]) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    raw_ranges = data.get("child_ranges", [])
    if not isinstance(raw_ranges, list):
        return ranges
    for item in raw_ranges:
        if not isinstance(item, dict):
            continue
        try:
            child_start = int(item.get("start"))
            child_end = int(item.get("end"))
        except (TypeError, ValueError):
            continue
        if child_start > 0 and child_end >= child_start:
            ranges.append((child_start, child_end))
    return ranges


def audit_status(project_dir: Path, start: int, end: int) -> Dict[str, Any]:
    paths = audit_paths(project_dir, start, end)
    data = load_json(paths["status"], {}) if paths["status"].is_file() else {}
    return data if isinstance(data, dict) else {}


def audit_range_is_resolved(project_dir: Path, start: int, end: int) -> bool:
    return audit_status(project_dir, start, end).get("status") in AUDIT_RESOLVED_STATUSES


def audit_children_resolved(project_dir: Path, child_ranges: List[Tuple[int, int]]) -> bool:
    return bool(child_ranges) and all(audit_range_is_resolved(project_dir, s, e) for s, e in child_ranges)


def mark_audit_covered_by_children(project_dir: Path, start: int, end: int, child_ranges: List[Tuple[int, int]]) -> None:
    paths = audit_paths(project_dir, start, end)
    update_audit_status(
        paths,
        status="covered_by_children",
        range=audit_range_label(start, end),
        child_ranges=[
            {
                "start": child_start,
                "end": child_end,
                "range": audit_range_label(child_start, child_end),
            }
            for child_start, child_end in child_ranges
        ],
        covered_at=datetime.now().isoformat(),
        action="父周期审计已由全部子区间真实审计补丁覆盖；恢复时只重放子区间 correction。",
    )


def read_excerpt(path: Path, limit: int = 12000) -> str:
    if not path.is_file():
        return f"[缺失] {path}"
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) > limit:
        return text[:limit] + f"\n\n...[已截断，原文件 {len(text)} 字符]"
    return text


def read_audit_evidence(path: Path) -> str:
    """读取周期审计的当前证据，缺失时显式保留缺失信息而不静默置空。"""
    if not path.is_file():
        return f"[缺失] {path}"
    return path.read_text(encoding="utf-8", errors="ignore")


def get_previous_correction(project_dir: Path, current_start: int) -> Optional[Dict[str, str]]:
    """返回上一周期 correction JSON 的 {name, content}，无则 None。"""
    if current_start <= 1:
        return None
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return None
    for p in sorted(audit_dir.glob("correction_*.json"), reverse=True):
        m = re.match(r"correction_(\d+)-(\d+)\.json", p.name)
        if m and int(m.group(2)) < current_start:
            return {"name": p.name, "content": read_excerpt(p, 5000)}
    return None


def extract_relevant_elements_for_audit(story_data: dict, project_dir: Path, start: int, end: int) -> str:
    names_mentioned = set()
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if not chapter:
            continue
        p = artifact_paths(project_dir, chapter)
        if p["delta"].is_file():
            delta_data = load_json(p["delta"], {})
            for key in ("新增元素", "修改元素"):
                bucket = delta_data.get(key, {})
                if not isinstance(bucket, dict):
                    continue
                for c_key in COLLECTION_KEYS:
                    for item in bucket.get(c_key, []):
                        if isinstance(item, dict) and item.get("名称"):
                            names_mentioned.add(item["名称"])

    if not names_mentioned:
        return "无"

    relevant = []
    for key in COLLECTION_KEYS:
        for item in story_data.get(key, []):
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if name in names_mentioned:
                relevant.append(json.dumps(item, ensure_ascii=False, indent=2))
                continue
            aliases = item.get("别名", [])
            if isinstance(aliases, list) and any(a in names_mentioned for a in aliases):
                relevant.append(json.dumps(item, ensure_ascii=False, indent=2))

    if not relevant:
        return "无"
    res = "\n\n".join(relevant)
    if len(res) > 80000:
        res = res[:80000] + "\n...[内容过长截断]"
    return res


def write_audit_pack(
    project_dir: Path,
    start: int,
    end: int,
    force: bool = False,
    max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
) -> Optional[Path]:
    """生成一个周期治理包，必要时只降载历史结构；绝不截断本周期证据。"""
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    paths = audit_paths(project_dir, start, end)
    if paths["pack"].exists() and not force:
        existing_status = load_json(paths["status"], {}) if paths["status"].is_file() else {}
        existing_context = existing_status.get("context", {}) if isinstance(existing_status, dict) else {}
        if (
            isinstance(existing_context, dict)
            and existing_context.get("format_version") == AUDIT_CONTEXT_FORMAT_VERSION
            and existing_context.get("max_context_chars") == max_context_chars
        ):
            print(f"周期审计任务包已存在: {paths['pack']}")
            return paths["pack"]
        print("周期审计任务包上下文格式或预算已变化，重新生成。")

    story_data = load_json(story, {})
    full_story_json = json.dumps(story_data, ensure_ascii=False, indent=2)
    story_index = build_lightweight_index(story)
    relevant_details = extract_relevant_elements_for_audit(story_data, project_dir, start, end)

    chapters: List[Dict[str, Any]] = []
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if not chapter:
            chapters.append({"seq": seq, "missing": True})
            continue
        p = artifact_paths(project_dir, chapter)
        chapters.append({
            "seq": seq,
            "missing": False,
            "chapter_path": p["chapter"],
            "analysis_path": p["analysis"],
            "delta_path": p["delta"],
            "chapter_text": read_audit_evidence(p["chapter"]),
            "analysis_text": read_audit_evidence(p["analysis"]),
            "delta_text": read_audit_evidence(p["delta"]),
        })

    taxonomy_path = Path(__file__).resolve().parent.parent / "references" / "narrative_taxonomy.json"
    taxonomy_json = taxonomy_path.read_text(encoding="utf-8", errors="ignore") if taxonomy_path.is_file() else ""
    previous_correction = get_previous_correction(project_dir, start)
    current_evidence_chars = sum(
        len(ch.get("chapter_text", ""))
        + len(ch.get("analysis_text", ""))
        + len(ch.get("delta_text", ""))
        for ch in chapters
    )

    mode_contexts = {
        "full_story": (full_story_json, "", ""),
        "story_index": ("", story_index, ""),
        "story_index_relevant": ("", story_index, relevant_details),
    }
    fallback_modes = ["full_story", "story_index_relevant", "story_index"]
    attempts: List[Dict[str, Any]] = []
    selected_content = ""
    selected_mode = ""
    for mode in fallback_modes:
        candidate_full_story, candidate_index, candidate_details = mode_contexts[mode]
        candidate = render_prompt(
            "audit.j2",
            start=start,
            end=end,
            correction_path=paths["correction"],
            run_pipeline_path=script_path("run_pipeline.py"),
            project_dir=project_dir,
            full_story_json=candidate_full_story,
            story_index=candidate_index,
            relevant_details=candidate_details,
            previous_correction=previous_correction,
            chapters=chapters,
            taxonomy_json=taxonomy_json,
        )
        attempts.append({"render_mode": mode, "rendered_chars": len(candidate)})
        if len(candidate) <= max_context_chars:
            selected_content = candidate
            selected_mode = mode
            break

    context = {
        "format_version": AUDIT_CONTEXT_FORMAT_VERSION,
        "max_context_chars": max_context_chars,
        "current_evidence_chars": current_evidence_chars,
        "attempts": attempts,
    }
    if not selected_content:
        context["render_mode"] = "blocked_context"
        save_json(paths["status"], {
            "status": "blocked_context",
            "range": f"{start:03d}-{end:03d}",
            "audit_pack": str(paths["pack"]),
            "correction_path": str(paths["correction"]),
            "context": context,
            "action": "提高 --audit-max-context-chars，或减小 --audit-interval 后重新生成；不得截断本周期原文、分析或 Delta。",
            "created_at": datetime.now().isoformat(),
        })
        print(f"周期审计 {start:03d}-{end:03d} 上下文超限，未生成不完整任务包。见: {paths['status']}")
        return None

    context["render_mode"] = selected_mode
    context["rendered_chars"] = len(selected_content)
    paths["pack"].parent.mkdir(parents=True, exist_ok=True)
    paths["pack"].write_text(selected_content, encoding="utf-8")
    save_json(paths["status"], {
        "status": "queued",
        "range": f"{start:03d}-{end:03d}",
        "audit_pack": str(paths["pack"]),
        "correction_path": str(paths["correction"]),
        "context": context,
        "created_at": datetime.now().isoformat(),
    })
    print(f"已生成周期审计任务包: {paths['pack']}")
    print(f"请产出治理补丁: {paths['correction']}")
    return paths["pack"]


def unresolved_audits(project_dir: Path) -> List[Dict[str, Any]]:
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return []
    audits = []
    for path in sorted(audit_dir.glob("audit_*.status.json")):
        data = load_json(path, None)
        if isinstance(data, dict) and data.get("status") not in AUDIT_RESOLVED_STATUSES:
            data["_status_path"] = str(path)
            m = re.match(r"^(\d{3})-(\d{3})$", str(data.get("range", "")))
            if m:
                data["_start"] = int(m.group(1))
                data["_end"] = int(m.group(2))
                audits.append(data)
    return audits


def committed_audits_due_for_recovery(project_dir: Path, snapshot_seq: int, to_seq: int) -> List[Dict[str, Any]]:
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return []
    due: List[Dict[str, Any]] = []
    for path in sorted(audit_dir.glob("audit_*.status.json")):
        data = load_json(path, None)
        if not isinstance(data, dict) or data.get("status") != "committed":
            continue
        m = re.match(r"^(\d{3})-(\d{3})$", str(data.get("range", "")))
        if not m:
            m = re.match(r"^audit_(\d{3})-(\d{3})\.status\.json$", path.name)
        if not m:
            continue
        start = int(m.group(1))
        end = int(m.group(2))
        if snapshot_seq <= end <= to_seq:
            data["_start"] = start
            data["_end"] = end
            data["_status_path"] = str(path)
            due.append(data)
    return due


def replay_committed_audits_after_recovery(project_dir: Path, seq: int, due_audits: List[Dict[str, Any]]) -> int:
    for audit in due_audits:
        if audit.get("_end") != seq or audit.get("_replayed"):
            continue
        start = int(audit["_start"])
        end = int(audit["_end"])
        paths = audit_paths(project_dir, start, end)
        correction = Path(str(audit.get("correction_path") or paths["correction"]))
        if not correction.is_file() or correction.stat().st_size == 0:
            update_audit_status(
                paths,
                status="awaiting_agent",
                action="recover-story 已回退到该周期治理之前的章节快照，但找不到已提交的 correction；请补齐治理补丁后重新运行 run。",
            )
            print(f"恢复暂停: 缺少需重放的周期治理补丁 {correction}")
            return 2
        rc = commit_governance(project_dir, str(correction))
        if rc != 0:
            return rc
        audit["_replayed"] = True
    return 0


def update_audit_status(paths: Dict[str, Path], **updates: Any) -> None:
    data = load_json(paths["status"], {}) if paths["status"].is_file() else {}
    if not isinstance(data, dict):
        data = {}
    data.update(updates)
    data["updated_at"] = datetime.now().isoformat()
    save_json(paths["status"], data)


def run_child_periodic_governance(
    project_dir: Path,
    start: int,
    end: int,
    child_ranges: List[Tuple[int, int]],
    max_context_chars: int,
    audit_command: str,
    worker_timeout_seconds: int,
    worker_retries: int,
    worker_window: str,
    worker_loop: str,
    rerun_command: str = "",
) -> int:
    if audit_children_resolved(project_dir, child_ranges):
        mark_audit_covered_by_children(project_dir, start, end, child_ranges)
        print(f"周期审计 {start:03d}-{end:03d} 已由子区间覆盖完成。")
        return 0

    for child_start, child_end in child_ranges:
        if audit_range_is_resolved(project_dir, child_start, child_end):
            continue
        rc = run_periodic_governance(
            project_dir,
            child_start,
            child_end,
            max_context_chars=max_context_chars,
            audit_command=audit_command,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_retries=worker_retries,
            worker_window=worker_window,
            worker_loop=worker_loop,
            rerun_command=rerun_command,
        )
        if rc != 0:
            return rc

    if audit_children_resolved(project_dir, child_ranges):
        mark_audit_covered_by_children(project_dir, start, end, child_ranges)
        print(f"周期审计 {start:03d}-{end:03d} 已由子区间覆盖完成。")
        return 0
    return 2


def prepare_audit_pack_or_split(
    project_dir: Path,
    start: int,
    end: int,
    force: bool = False,
    max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
) -> int:
    paths = audit_paths(project_dir, start, end)
    pack = write_audit_pack(project_dir, start, end, force=force, max_context_chars=max_context_chars)
    if pack is not None:
        return 0

    child_ranges = split_audit_range(start, end)
    if not child_ranges:
        return 1

    update_audit_status(
        paths,
        status="split_pending",
        range=audit_range_label(start, end),
        child_ranges=[
            {
                "start": child_start,
                "end": child_end,
                "range": audit_range_label(child_start, child_end),
            }
            for child_start, child_end in child_ranges
        ],
        action="父周期审计包超出上下文预算，已自动拆分为子区间；子区间全部提交后父区间自动视为覆盖完成。",
    )
    print(
        f"周期审计 {start:03d}-{end:03d} 上下文超限，自动拆分为: "
        + ", ".join(audit_range_label(s, e) for s, e in child_ranges)
    )

    rc = 0
    for child_start, child_end in child_ranges:
        child_rc = prepare_audit_pack_or_split(
            project_dir,
            child_start,
            child_end,
            force=force,
            max_context_chars=max_context_chars,
        )
        if child_rc != 0:
            rc = child_rc
    return rc


def run_periodic_governance(
    project_dir: Path,
    start: int,
    end: int,
    max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    audit_command: str = "",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
    rerun_command: str = "",
) -> int:
    paths = audit_paths(project_dir, start, end)
    existing_status = load_json(paths["status"], {}) if paths["status"].is_file() else {}
    existing_status = existing_status if isinstance(existing_status, dict) else {}
    child_ranges = audit_child_ranges_from_status(existing_status)
    if child_ranges:
        return run_child_periodic_governance(
            project_dir,
            start,
            end,
            child_ranges,
            max_context_chars,
            audit_command,
            worker_timeout_seconds,
            worker_retries,
            worker_window,
            worker_loop,
        )

    pack = write_audit_pack(project_dir, start, end, max_context_chars=max_context_chars)
    if pack is None:
        child_ranges = split_audit_range(start, end)
        if child_ranges:
            update_audit_status(
                paths,
                status="split_pending",
                range=audit_range_label(start, end),
                child_ranges=[
                    {
                        "start": child_start,
                        "end": child_end,
                        "range": audit_range_label(child_start, child_end),
                    }
                    for child_start, child_end in child_ranges
                ],
                action="父周期审计包超出上下文预算，已自动拆分为子区间；子区间全部提交后父区间自动视为覆盖完成。",
            )
            print(
                f"周期审计 {start:03d}-{end:03d} 上下文超限，自动拆分为: "
                + ", ".join(audit_range_label(s, e) for s, e in child_ranges)
            )
            return run_child_periodic_governance(
                project_dir,
                start,
                end,
                child_ranges,
                max_context_chars,
                audit_command,
                worker_timeout_seconds,
                worker_retries,
                worker_window,
                worker_loop,
                rerun_command,
            )
        return 1
    for attempt in range(worker_retries + 1):
        if not paths["correction"].is_file() or paths["correction"].stat().st_size == 0:
            update_audit_status(
                paths,
                status="awaiting_agent",
                action="读取审计任务包，产出真实 correction Delta，然后再次运行 run_pipeline.py run。",
            )
            print(f"周期审计 {start:03d}-{end:03d} 待Agent处理：{paths['pack']}")
            print(f"请产出真实治理补丁：{paths['correction']}")
            if not audit_command:
                return 2
            rc = run_worker(
                audit_command,
                project_dir,
                "audit_correction",
                paths["pack"],
                paths["correction"],
                timeout=worker_timeout_seconds,
                retries=0,
                worker_window=worker_window,
            )
            if rc != 0:
                update_audit_status(paths, status="worker_failed", action="审计 worker 失败；查看 worker 日志后重试。")
                return rc
            if worker_supervision_checkpoint(worker_loop, f"audit correction written for {start:03d}-{end:03d}", rerun_command):
                return 2

        rc = commit_governance(project_dir, str(paths["correction"]))
        if rc == 0:
            print(f"周期审计 {start:03d}-{end:03d} 治理补丁已提交并通过校验。")
            return 0

        update_audit_status(
            paths,
            status="awaiting_agent",
            action="读取治理校验报告，修复 correction Delta，然后再次运行 run_pipeline.py run。",
        )
        print(f"周期审计 {start:03d}-{end:03d} 治理补丁未通过；待Agent修复后重试。")
        if not audit_command or attempt >= worker_retries:
            return 2
        rc = run_worker(
            audit_command,
            project_dir,
            "audit_repair",
            paths["pack"],
            paths["correction"],
            timeout=worker_timeout_seconds,
            retries=0,
            worker_window=worker_window,
        )
        if rc != 0:
            update_audit_status(paths, status="worker_failed", action="审计修复 worker 失败；查看 worker 日志后重试。")
            return rc
        if worker_supervision_checkpoint(worker_loop, f"audit repair written for {start:03d}-{end:03d}", rerun_command):
            return 2
    return 2


def mark_periodic_audit_committed(project_dir: Path, patch: Path, before: Path, after: Path, diff: Path) -> None:
    resolved_patch = patch.resolve()
    audit_dir = (project_dir / "质量治理" / "周期审计").resolve()
    if resolved_patch.parent != audit_dir:
        return
    m = re.match(r"^correction_(\d{3})-(\d{3})\.json$", resolved_patch.name)
    if not m:
        return
    status_path = audit_dir / f"audit_{m.group(1)}-{m.group(2)}.status.json"
    data = load_json(status_path, {}) if status_path.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    data.update({
        "status": "committed",
        "range": f"{m.group(1)}-{m.group(2)}",
        "correction_path": str(resolved_patch),
        "committed_at": datetime.now().isoformat(),
        "before": str(before),
        "after": str(after),
        "diff": str(diff),
    })
    save_json(status_path, data)


def prepare_chapter_analysis(project_dir: Path, chapter: Path, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    p = artifact_paths(project_dir, chapter)
    if p["analysis_task"].exists() and not force:
        print(f"章节分析任务包已存在: {p['analysis_task']}")
        print(f"请只填写章节分析MD: {p['analysis']}")
        return 0
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    chapter_seq = seq_from_file(chapter)
    content = render_prompt(
        "chapter_analysis.j2",
        chapter_seq=chapter_seq,
        analysis_path=p["analysis"],
        chapter_text=chapter_text,
    )
    p["analysis_task"].parent.mkdir(parents=True, exist_ok=True)
    p["analysis_task"].write_text(content, encoding="utf-8")
    print(f"已生成章节分析任务包: {p['analysis_task']}")
    print(f"请填写章节分析MD: {p['analysis']}")
    return 0


def prepare_chapter_delta(project_dir: Path, chapter: Path, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    story = ensure_story(project_dir)
    p = artifact_paths(project_dir, chapter)
    if not p["analysis"].is_file():
        print(f"缺章节分析MD: {p['analysis']}")
        return 2
    analysis_ok, analysis_errors = validate_chapter_analysis_file(p["analysis"])
    if not analysis_ok:
        write_analysis_regenerate_task(project_dir, chapter, analysis_errors)
        p["delta_task"].unlink(missing_ok=True)
        print("章节分析MD结构不合格，已交接重新生成；不会生成Delta任务。")
        return 2
    p["analysis_regenerate_task"].unlink(missing_ok=True)
    if p["delta_task"].exists() and not force:
        print(f"Delta提取任务包已存在: {p['delta_task']}")
        print(f"请只填写本章Delta JSON: {p['delta']}")
        return 0
    chapter_seq = seq_from_file(chapter)
    chapter_id = str(chapter_seq).zfill(4)
    chapter_time_str = chapter_time(chapter_seq)
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    analysis_text = p["analysis"].read_text(encoding="utf-8", errors="ignore")
    summary = build_lightweight_index(story)

    content = render_prompt(
        "delta_extract.j2",
        chapter_seq=chapter_seq,
        chapter_id=chapter_id,
        delta_path=p["delta"],
        summary=summary,
        analysis_text=analysis_text,
        chapter_text=chapter_text,
        chapter_time=chapter_time_str,
    )

    p["delta_task"].parent.mkdir(parents=True, exist_ok=True)
    p["delta_task"].write_text(content, encoding="utf-8")
    print(f"已生成Delta提取任务包: {p['delta_task']}")
    print(f"请填写本章Delta JSON: {p['delta']}")
    return 0


def prepare_chapter(project_dir: Path, seq: Optional[int] = None, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    chapter = chapter_by_seq(project_dir, seq) if seq else next_incomplete(project_dir)
    if not chapter:
        print("没有待处理章节。")
        return 0
    p = artifact_paths(project_dir, chapter)
    if not p["analysis"].is_file():
        return prepare_chapter_analysis(project_dir, chapter, force)
    if not p["delta"].is_file():
        return prepare_chapter_delta(project_dir, chapter, force)
    print(f"第{seq_from_file(chapter):03d}章的章节分析MD和Delta JSON均已存在。")
    return 0


def write_repair_pack(project_dir: Path, chapter: Path, reason: str, report_paths: List[Path]) -> None:
    p = artifact_paths(project_dir, chapter)
    reports: List[Dict[str, str]] = []
    for rp in report_paths:
        if rp.is_file():
            reports.append({
                "name": rp.name,
                "excerpt": rp.read_text(encoding="utf-8", errors="ignore")[:30000],
            })
    content = render_prompt(
        "repair_chapter.j2",
        chapter_seq=seq_from_file(chapter),
        reason=reason,
        reports=reports,
    )
    p["repair"].parent.mkdir(parents=True, exist_ok=True)
    p["repair"].write_text(content, encoding="utf-8")
    print(f"已生成修复任务包: {p['repair']}")


def write_analysis_regenerate_task(project_dir: Path, chapter: Path, errors: List[str]) -> None:
    """Ask the agent to regenerate a shrunken analysis from source, never from the bad MD."""
    p = artifact_paths(project_dir, chapter)
    content = render_prompt(
        "chapter_analysis_regenerate.j2",
        chapter_seq=seq_from_file(chapter),
        analysis_path=p["analysis"],
        chapter_text=chapter.read_text(encoding="utf-8", errors="ignore"),
        errors=errors,
    )
    p["analysis_regenerate_task"].parent.mkdir(parents=True, exist_ok=True)
    p["analysis_regenerate_task"].write_text(content, encoding="utf-8")
    print(f"已生成章节分析重新生成任务: {p['analysis_regenerate_task']}")


def commit_chapter(project_dir: Path, seq: int, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    story = ensure_story(project_dir)
    chapter = chapter_by_seq(project_dir, seq)
    if not chapter:
        print(f"找不到第{seq:03d}章")
        return 1
    p = artifact_paths(project_dir, chapter)
    if is_chapter_complete(project_dir, chapter) and not force:
        print(f"第{seq:03d}章已完成，跳过。使用 --force 可重新提交。")
        return 0
    if not p["analysis"].is_file():
        print(f"缺章节分析MD: {p['analysis']}")
        prepare_chapter(project_dir, seq)
        print("已交接：等待章节分析MD。")
        return 2
    analysis_ok, analysis_errors = validate_chapter_analysis_file(p["analysis"])
    if not analysis_ok:
        write_analysis_regenerate_task(project_dir, chapter, analysis_errors)
        print("已交接：章节分析MD需要从原文重新生成。")
        return 2
    if not p["delta"].is_file():
        print(f"缺Delta JSON: {p['delta']}")
        rc = prepare_chapter(project_dir, seq)
        if rc != 0:
            return rc
        if p["analysis_regenerate_task"].is_file():
            print("已交接：等待从原文重新生成完整章节分析MD。")
            return 2
        print("已交接：等待基于章节分析的Delta JSON。")
        return 2

    # 入库前先修复 LLM JSON 语法：Markdown 代码块、单引号、尾逗号等先转成严格 JSON，
    # 避免后续 coerce/validate 因 json.load 直接失败。
    repair_json_report = project_dir / "质量治理" / "delta校验" / f"repair_json_ch{seq:03d}.json"
    rc = repair_llm_json(p["delta"], repair_json_report)
    if rc != 0:
        print(f"Delta LLM JSON 语法修复失败，见: {repair_json_report}")
        write_repair_pack(project_dir, chapter, "repair_llm_json.py失败", [repair_json_report])
        return 1

    # 入库前先做机械纠错：把 LLM 写错的类型/格式（bool/int、数组↔标量、章节号宽度、
    # 顶层追溯字段位置等）确定性地修好。这一步不需要 LLM 介入，能把大半 repair 循环
    # 提前消解。失败（理论上只在 JSON 损坏时）才落 repair 包。
    coerce_delta_report = project_dir / "质量治理" / "delta校验" / f"coerce_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("coerce_delta.py")), str(p["delta"]), "--report", str(coerce_delta_report)])
    if rc != 0:
        print(f"Delta 机械纠错失败，见: {coerce_delta_report}")
        write_repair_pack(project_dir, chapter, "coerce_delta.py失败", [coerce_delta_report])
        return 1

    # 入库前再做确定性标签压缩，把受控前缀标签和超限自由标签搬到 详情.补充标签，
    # 让原本因 taxonomy 失配进 repair 循环的报错根本不发生。
    compress_delta_report = project_dir / "质量治理" / "delta校验" / f"compress_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(p["delta"]), "--report", str(compress_delta_report)])
    if rc != 0:
        print(f"Delta 标签压缩失败，见: {compress_delta_report}")
        write_repair_pack(project_dir, chapter, "compress_tags.py(Delta)失败", [compress_delta_report])
        return 1

    # 入库前校验
    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "process", "--report-json", str(p["delta_report"]), str(story), str(p["delta"])])
    if rc != 0:
        print(f"Delta校验失败，见: {p['delta_report']}")
        write_repair_pack(project_dir, chapter, "Delta校验失败", [p["delta_report"]])
        return 1

    shutil.copy2(story, p["before"])
    backup_tmp = project_dir / "故事结构版本" / f"_rollback_ch{seq:03d}.json"
    shutil.copy2(story, backup_tmp)

    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(p["delta"])], project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt")
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print("合并失败，已回滚。")
        write_repair_pack(project_dir, chapter, "merge_delta.py合并失败", [project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt"])
        return 1

    # 合并后保全式规范化，不隔离前向引用，避免过程信息丢失。
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--report", str(project_dir / "质量治理" / "规范化" / f"normalize_ch{seq:03d}.txt")])

    # 合并后再压一次：跨章累积的标签集即使每章入口已合规，也可能因新 Delta 与既有元素合并而越过上限。
    compress_struct_report = project_dir / "质量治理" / "规范化" / f"compress_after_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--structure", str(story), "--report", str(compress_struct_report)])
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print(f"合并后标签压缩失败，已回滚，见: {compress_struct_report}")
        write_repair_pack(project_dir, chapter, "compress_tags.py(Structure)失败", [compress_struct_report])
        return 1

    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--chapter-check", "--mode", "process", "--report-json", str(p["chapter_report"]), str(story), str(p["delta"])])
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print(f"章节合并后校验失败，已回滚，见: {p['chapter_report']}")
        write_repair_pack(project_dir, chapter, "合并后结构校验失败", [p["delta_report"], p["chapter_report"]])
        return 1

    shutil.copy2(story, p["after"])
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(p["before"]), str(p["after"]), str(p["diff"])])

    # 更新进度
    detail = json.dumps({"已完成章节": [chapter.name]}, ensure_ascii=False)
    # 这里不用直接覆盖列表，调用 progress_manager update 不适合追加；手动合并更稳。
    progress_path = project_dir / "进度.json"
    progress = load_json(progress_path, None)
    if isinstance(progress, dict):
        step2 = progress.setdefault("步骤状态", {}).setdefault("2", {"状态": "in_progress", "详情": {}, "完成时间": None})
        details = step2.setdefault("详情", {})
        done = details.setdefault("已完成章节", [])
        if chapter.name not in done:
            done.append(chapter.name)
        done.sort()
        details["总章节数"] = len(chapter_files(project_dir))
        step2["状态"] = "in_progress"
        progress["当前步骤"] = "2"
        progress["更新时间"] = datetime.now().isoformat()
        save_json(progress_path, progress)

    print(f"第{seq:03d}章提交完成。")
    return 0


def cmd_resume(project_dir: Path) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    files = chapter_files(project_dir)
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    done = [p for p in files if is_chapter_complete(project_dir, p)]
    nxt = next_incomplete(project_dir)
    print(f"章节总数: {len(files)}")
    print(f"可信完成: {len(done)}")
    if nxt:
        p = artifact_paths(project_dir, nxt)
        print(f"下一章: 第{seq_from_file(nxt):03d}章 {nxt.name}")
        for key in ["analysis", "delta", "delta_report", "chapter_report", "before", "after", "diff"]:
            status = "[x]" if p[key].is_file() and p[key].stat().st_size > 0 else "[ ]"
            print(f"  {status} {key}: {p[key]}")
        if not p["analysis"].is_file():
            print(f"章节分析任务包: {p['analysis_task']}")
        elif not p["delta"].is_file():
            print(f"Delta提取任务包: {p['delta_task']}")
    else:
        print("所有章节均已可信完成。")
    return 0


def cmd_run_analysis(
    project_dir: Path,
    chapter_command: str = "",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
    rerun_command: str = "",
) -> int:
    """Create and validate every chapter-analysis MD without touching Delta flow."""
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    files = chapter_files(project_dir)
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    for chapter in files:
        p = artifact_paths(project_dir, chapter)
        if not p["analysis"].is_file():
            prepare_chapter_analysis(project_dir, chapter)
            print("已交接：等待章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "analysis",
                p["analysis_task"],
                p["analysis"],
                seq_from_file(chapter),
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"analysis artifact written for ch{seq_from_file(chapter):03d}", rerun_command):
                return 2
            continue
        analysis_ok, analysis_errors = validate_chapter_analysis_file(p["analysis"])
        if not analysis_ok:
            write_analysis_regenerate_task(project_dir, chapter, analysis_errors)
            print("已交接：等待从原文重新生成完整章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "analysis_regenerate",
                p["analysis_regenerate_task"],
                p["analysis"],
                seq_from_file(chapter),
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"analysis artifact regenerated for ch{seq_from_file(chapter):03d}", rerun_command):
                return 2
            continue
        p["analysis_regenerate_task"].unlink(missing_ok=True)
    print("所有章节分析MD均已通过校验。")
    return 0


def cmd_run_delta(
    project_dir: Path,
    max_chapters: int = 0,
    audit_interval: int = 5,
    audit_max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    chapter_command: str = "",
    audit_command: str = "",
    run_mode: str = "auto",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
    rerun_command: str = "",
) -> int:
    """Require all chapter analyses before entering the existing serial Delta flow."""
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    files = chapter_files(project_dir)
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    for chapter in files:
        p = artifact_paths(project_dir, chapter)
        if not p["analysis"].is_file():
            prepare_chapter_analysis(project_dir, chapter)
            print("Delta阶段已交接：等待全部章节分析MD通过校验。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "analysis",
                p["analysis_task"],
                p["analysis"],
                seq_from_file(chapter),
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"analysis artifact written for ch{seq_from_file(chapter):03d}", rerun_command):
                return 2
            continue
        analysis_ok, analysis_errors = validate_chapter_analysis_file(p["analysis"])
        if not analysis_ok:
            write_analysis_regenerate_task(project_dir, chapter, analysis_errors)
            print("Delta阶段已交接：等待全部章节分析MD通过校验。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "analysis_regenerate",
                p["analysis_regenerate_task"],
                p["analysis"],
                seq_from_file(chapter),
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"analysis artifact regenerated for ch{seq_from_file(chapter):03d}", rerun_command):
                return 2
            continue
        p["analysis_regenerate_task"].unlink(missing_ok=True)
    return cmd_run(
        project_dir,
        max_chapters=max_chapters,
        audit_interval=audit_interval,
        audit_max_context_chars=audit_max_context_chars,
        chapter_command=chapter_command,
        audit_command=audit_command,
        run_mode=run_mode,
        worker_provider="manual",
        worker_timeout_seconds=worker_timeout_seconds,
        worker_retries=worker_retries,
        worker_window=worker_window,
        worker_loop=worker_loop,
        rerun_command=rerun_command,
    )


def cmd_run(
    project_dir: Path,
    max_chapters: int = 0,
    audit_interval: int = 5,
    phase: str = "",
    audit_max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    chapter_command: str = "",
    audit_command: str = "",
    run_mode: str = "auto",
    worker_provider: str = "manual",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
) -> int:
    requested_chapter_command = chapter_command
    requested_audit_command = audit_command
    mode = effective_run_mode(run_mode, chapter_command, audit_command, worker_provider)
    rerun_command = build_run_foreground_command(
        project_dir,
        phase=phase,
        max_chapters=max_chapters,
        audit_interval=audit_interval,
        audit_max_context_chars=audit_max_context_chars,
        run_mode=mode,
        worker_provider=worker_provider,
        chapter_command=requested_chapter_command,
        audit_command=requested_audit_command,
        worker_timeout_seconds=worker_timeout_seconds,
        worker_retries=worker_retries,
        worker_window=worker_window,
        worker_loop=worker_loop,
    )
    chapter_command, audit_command = resolve_worker_commands(chapter_command, audit_command, worker_provider, mode)
    print(f"run route: mode={mode} worker_provider={worker_provider}")
    if phase == "analysis":
        return cmd_run_analysis(
            project_dir,
            chapter_command=chapter_command,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_retries=worker_retries,
            worker_window=worker_window,
            worker_loop=worker_loop,
            rerun_command=rerun_command,
        )
    if phase == "delta":
        return cmd_run_delta(
            project_dir,
            max_chapters=max_chapters,
            audit_interval=audit_interval,
            audit_max_context_chars=audit_max_context_chars,
            chapter_command=chapter_command,
            audit_command=audit_command,
            run_mode=mode,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_retries=worker_retries,
            worker_window=worker_window,
            worker_loop=worker_loop,
            rerun_command=rerun_command,
        )
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    for audit in unresolved_audits(project_dir):
        rc = run_periodic_governance(
            project_dir,
            audit["_start"],
            audit["_end"],
            audit_max_context_chars,
            audit_command=audit_command,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_retries=worker_retries,
            worker_window=worker_window,
            worker_loop=worker_loop,
            rerun_command=rerun_command,
        )
        if rc != 0:
            return rc
    processed = 0
    while True:
        nxt = next_incomplete(project_dir)
        if not nxt:
            print("所有章节均已完成。")
            return 0
        seq = seq_from_file(nxt)
        p = artifact_paths(project_dir, nxt)
        if not p["analysis"].is_file():
            prepare_chapter(project_dir, seq)
            print("已交接：等待章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "analysis",
                p["analysis_task"],
                p["analysis"],
                seq,
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"analysis artifact written for ch{seq:03d}", rerun_command):
                return 2
            continue
        if not p["delta"].is_file():
            rc = prepare_chapter(project_dir, seq)
            if rc != 0:
                return rc
            if p["analysis_regenerate_task"].is_file():
                print("已交接：等待从原文重新生成完整章节分析MD。")
                if not chapter_command:
                    return 2
                rc = run_worker(
                    chapter_command,
                    project_dir,
                    "analysis_regenerate",
                    p["analysis_regenerate_task"],
                    p["analysis"],
                    seq,
                    timeout=worker_timeout_seconds,
                    retries=worker_retries,
                    worker_window=worker_window,
                )
                if rc != 0:
                    return rc
                if worker_supervision_checkpoint(worker_loop, f"analysis artifact regenerated for ch{seq:03d}", rerun_command):
                    return 2
                continue
            print("已交接：等待基于章节分析的Delta JSON。")
            if not chapter_command:
                return 2
            rc = run_worker(
                chapter_command,
                project_dir,
                "delta",
                p["delta_task"],
                p["delta"],
                seq,
                timeout=worker_timeout_seconds,
                retries=worker_retries,
                worker_window=worker_window,
            )
            if rc != 0:
                return rc
            if worker_supervision_checkpoint(worker_loop, f"delta artifact written for ch{seq:03d}", rerun_command):
                return 2
            continue
        rc = commit_chapter(project_dir, seq)
        if rc != 0:
            if chapter_command and p["repair"].is_file():
                worker_rc = run_worker(
                    chapter_command,
                    project_dir,
                    "repair_chapter",
                    p["repair"],
                    p["delta"],
                    seq,
                    timeout=worker_timeout_seconds,
                    retries=worker_retries,
                    worker_window=worker_window,
                )
                if worker_rc != 0:
                    return worker_rc
                if worker_supervision_checkpoint(worker_loop, f"chapter repair artifact written for ch{seq:03d}", rerun_command):
                    return 2
                continue
            return rc
        processed += 1
        if audit_interval and seq % audit_interval == 0:
            start = seq - audit_interval + 1
            rc = run_periodic_governance(
                project_dir,
                start,
                seq,
                audit_max_context_chars,
                audit_command=audit_command,
                worker_timeout_seconds=worker_timeout_seconds,
                worker_retries=worker_retries,
                worker_window=worker_window,
                worker_loop=worker_loop,
                rerun_command=rerun_command,
            )
            if rc != 0:
                return rc
        if max_chapters and processed >= max_chapters:
            print(f"已达到本次最大提交章节数: {max_chapters}")
            return 0
        if mode == "worker" and worker_supervision_checkpoint(worker_loop, f"chapter committed for ch{seq:03d}", rerun_command):
            return 2



def commit_governance(project_dir: Path, patch_path: str) -> int:
    """提交按需/周期治理补丁：合并新增修改 + 执行治理操作 + 规范化 + governance校验 + diff。"""
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    patch = Path(patch_path)
    if not patch.is_file():
        print(f"治理补丁不存在: {patch}")
        return 1
    audit_dir = (project_dir / "质量治理" / "周期审计").resolve()
    is_periodic_patch = (
        patch.resolve().parent == audit_dir
        and re.match(r"^correction_\d{3}-\d{3}\.json$", patch.name) is not None
    )
    governance_dir = audit_dir if is_periodic_patch else project_dir / "质量治理" / "按需治理"
    governance_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    before = project_dir / "故事结构版本" / f"story_before_governance_{ts}.json"
    after = project_dir / "故事结构版本" / f"story_after_governance_{ts}.json"
    diff = project_dir / "结构变更日志" / f"diff_governance_{ts}.json"
    report_delta = governance_dir / f"validate_delta_governance_{ts}.txt"
    report_schema = governance_dir / f"validate_schema_governance_{ts}.txt"
    shutil.copy2(story, before)
    report_repair_json = governance_dir / f"repair_json_governance_{ts}.json"
    rc = repair_llm_json(patch, report_repair_json, backup=True)
    if rc != 0:
        print(f"治理补丁 LLM JSON 语法修复失败: {report_repair_json}")
        return 1
    report_compress = governance_dir / f"compress_governance_{ts}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(patch), "--report", str(report_compress)])
    if rc != 0:
        print(f"治理补丁标签压缩失败: {report_compress}")
        return 1
    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "governance", str(story), str(patch)], report_delta)
    if rc != 0:
        print(f"治理补丁Delta校验失败: {report_delta}")
        return 1
    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(patch)], governance_dir / f"merge_governance_{ts}.txt")
    if rc != 0:
        shutil.copy2(before, story); print("治理新增/修改合并失败，已回滚。") ; return 1
    rc = run_cmd([sys.executable, str(script_path("apply_governance_ops.py")), str(story), str(patch), "--backup"], governance_dir / f"apply_ops_{ts}.txt")
    if rc not in (0,2):
        shutil.copy2(before, story); print("治理操作失败，已回滚。") ; return 1
    normalize_cmd = [sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place"]
    if is_periodic_patch:
        normalize_cmd.append("--quarantine-invalid-refs")
    normalize_cmd.extend(["--report", str(project_dir / "质量治理" / "规范化" / f"normalize_governance_{ts}.txt")])
    run_cmd(normalize_cmd)
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--mode", "governance", str(story)], report_schema)
    if rc != 0:
        shutil.copy2(before, story); print(f"治理后结构校验失败，已回滚: {report_schema}") ; return 1
    shutil.copy2(story, after)
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(before), str(after), str(diff)])
    mark_periodic_audit_committed(project_dir, patch, before, after, diff)
    print(f"治理补丁提交完成。before={before} after={after} diff={diff}")
    return 0


def cmd_final_pack(project_dir: Path, force: bool = False) -> int:
    """生成最终结构整理任务包：复制增量为草稿，运行 final 校验，打包所有参考材料。"""
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if draft.is_file() and not force:
        print(f"故事结构_草稿.json 已存在，跳过复制。使用 --force 重新从增量复制。")
    else:
        shutil.copy2(story, draft)
        print(f"已复制 故事结构_增量.json → 故事结构_草稿.json")

    # 规范化
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")),
             str(draft), "--in-place", "--backup",
             "--report", str(audit_dir / "normalize_final_init.txt")])

    # 运行 final 校验
    report = audit_dir / "validate_report.txt"
    run_cmd([sys.executable, str(script_path("validate_structure.py")),
             "--mode", "final", str(draft)], report)
    # 收集材料生成任务包
    task_pack = audit_dir / "final_task_pack.md"

    report_excerpt = read_excerpt(report, 30000) if report.is_file() else ""

    analysis_specs = [
        ("全书分析/剧情结构/章节梗概汇总.md", "章节梗概汇总"),
        ("全书分析/人物分析/人物档案.md", "人物档案"),
        ("全书分析/人物分析/人物关系.md", "人物关系"),
        ("全书分析/剧情结构/伏笔追踪.md", "伏笔追踪"),
        ("全书分析/剧情结构/冲突图谱.md", "冲突图谱"),
        ("全书分析/视觉资产/视觉资产清单.md", "视觉资产清单"),
        ("全书分析/视觉资产/关键场景分镜表.md", "关键场景分镜表"),
        ("全书分析/故事结构/三幕式结构图.md", "三幕式结构图"),
        ("全书分析/故事结构/Brooks四部分结构图.md", "Brooks四部分结构图"),
        ("全书分析/故事结构/Freytag五段结构图.md", "Freytag五段结构图"),
        ("全书分析/故事结构/故事七要素档案.md", "故事七要素档案"),
        ("全书分析/故事结构/故事力学评估.md", "故事力学评估"),
        ("全书分析/故事结构/故事工程学评估.md", "故事工程学评估"),
        ("全书分析/故事结构/小说骨架.md", "小说骨架"),
    ]
    analysis_refs: List[Dict[str, Any]] = []
    for rel, title in analysis_specs:
        path = project_dir / rel
        if path.is_file() and path.stat().st_size > 0:
            analysis_refs.append({
                "title": title,
                "path": path,
                "excerpt": read_excerpt(path, 15000),
            })

    audit_refs: List[Dict[str, Any]] = []
    pa_dir = project_dir / "质量治理" / "周期审计"
    if pa_dir.is_dir():
        for md in sorted(pa_dir.glob("audit_*.md")):
            audit_refs.append({"name": md.name, "lang": "markdown",
                               "excerpt": read_excerpt(md, 8000)})
        for cj in sorted(pa_dir.glob("correction_*.json")):
            audit_refs.append({"name": cj.name, "lang": "json",
                               "excerpt": read_excerpt(cj, 8000)})

    content = render_prompt(
        "final_draft.j2",
        run_pipeline_path=script_path("run_pipeline.py"),
        project_dir=project_dir,
        report_excerpt=report_excerpt,
        story_index=build_lightweight_index(draft),
        analysis_refs=analysis_refs,
        audit_refs=audit_refs,
    )

    task_pack.write_text(content, encoding="utf-8")
    print(f"已生成最终整理任务包: {task_pack}")
    print(f"校验报告: {report}")
    print(f"草稿文件: {draft}")
    print(f"\n请根据任务包修改 故事结构_草稿.json，完成后运行:")
    print(f"  python {script_path('run_pipeline.py')} commit-final-draft {project_dir}")
    return 0


def cmd_commit_final_draft(project_dir: Path) -> int:
    """提交模型修好的最终草稿：规范化 → strict 校验 → 失败生成修复包。"""
    init_dirs(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if not draft.is_file():
        print("故事结构_草稿.json 不存在。请先运行 final-pack 生成草稿。")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")),
             str(draft), "--in-place",
             "--report", str(audit_dir / f"normalize_final_{ts}.txt")])

    report = audit_dir / "validate_report.txt"
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(draft)], report)

    if rc != 0:
        print(f"最终校验失败，见: {report}")
        repair = audit_dir / "repair_final.md"
        content = render_prompt(
            "repair_final.j2",
            report_excerpt=read_excerpt(report, 30000),
            run_pipeline_path=script_path("run_pipeline.py"),
            project_dir=project_dir,
        )
        repair.write_text(content, encoding="utf-8")
        print(f"已生成修复任务包: {repair}")
        return 1

    print("最终校验通过！")
    print(f"校验报告: {report}")
    print(f"\n可以运行以下命令完成最终交付:")
    print(f"  python {script_path('run_pipeline.py')} finalize {project_dir}")
    return 0


def cmd_validate_final(project_dir: Path, file: str = "") -> int:
    """独立最终校验：定位目标文件 → strict 校验 → 输出报告。"""
    target = project_dir / (file or "故事结构_草稿.json")
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)
    report = audit_dir / "validate_report.txt"

    if not target.is_file():
        print(f"目标文件不存在: {target}")
        return 1

    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(target)], report)
    print(f"校验报告: {report}")
    if rc == 0:
        print("最终校验通过。")
    else:
        print("最终校验失败。")
    return rc


def cmd_finalize(project_dir: Path) -> int:
    """最终交付：前置 strict 校验 → 复制草稿为最终 JSON → 完成步骤4 → 审计日志。"""
    init_dirs(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    final = project_dir / "故事结构.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if not draft.is_file():
        print("故事结构_草稿.json 不存在。请先运行 final-pack 生成草稿。")
        return 1

    report = audit_dir / "validate_report.txt"
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(draft)], report)
    if rc != 0:
        print(f"最终校验未通过，无法 finalize。见: {report}")
        print("请先修正草稿并运行 commit-final-draft。")
        return 1

    shutil.copy2(draft, final)
    print(f"已生成最终交付: {final}")

    run_cmd([sys.executable, str(script_path("progress_manager.py")),
             "complete", str(project_dir), "4"])

    finalize_log = {
        "finalize_time": datetime.now().isoformat(),
        "draft": str(draft),
        "final": str(final),
        "validate_report": str(report),
        "draft_size_bytes": draft.stat().st_size,
        "final_size_bytes": final.stat().st_size,
    }
    save_json(audit_dir / "finalize_log.json", finalize_log)
    print(f"交付日志: {audit_dir / 'finalize_log.json'}")
    print(f"\n步骤4已完成。最终故事结构: {final}")
    return 0


def cmd_audit_pack(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    try:
        start, end = parse_chapter_range(args.chapters)
    except ValueError as exc:
        print(exc)
        return 1
    return prepare_audit_pack_or_split(project_dir, start, end, force=args.force, max_context_chars=args.max_context_chars)

def cmd_split(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    cmd = [sys.executable, str(script_path("split_chapters.py")), args.source_file, str(project_dir / "原文拆解")]
    if args.pattern:
        cmd.extend(["--pattern", args.pattern])
    if args.preface_mode:
        cmd.extend(["--preface-mode", args.preface_mode])
    rc = run_cmd(cmd)
    if rc == 0:
        ensure_story(project_dir)
    return rc


def cmd_analysis_pack(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    cmd = [sys.executable, str(script_path("analysis_context_pack.py")), str(project_dir), "--task", args.task, "--chapters", args.chapters, "--include-original", args.include_original, "--max-pack-chars", str(args.max_pack_chars), "--max-original-chars", str(args.max_original_chars), "--max-analysis-chars", str(args.max_analysis_chars)]
    if args.targets:
        cmd.extend(["--targets", *args.targets])
    if args.question:
        cmd.extend(["--question", args.question])
    if args.out_dir:
        cmd.extend(["--out-dir", args.out_dir])
    if getattr(args, "per_chapter", False):
        cmd.append("--per-chapter")
    if getattr(args, "aggregate", False):
        cmd.append("--aggregate")
    return run_cmd(cmd)


def visual_assets_per_chapter_relpaths(seq: int) -> List[str]:
    """与 analysis_context_pack.visual_assets_per_chapter_outputs 保持同步的相对路径。"""
    base = f"全书分析/视觉资产/分章/ch{seq:03d}"
    return [
        f"{base}/视觉资产清单.md",
        f"{base}/关键场景分镜表.md",
        f"{base}/AI绘图提示词素材.md",
        f"{base}/角色外观一致性表.md",
        f"{base}/场景氛围表.md",
    ]


def chapter_has_per_chapter_visual(project_dir: Path, seq: int) -> bool:
    for rel in visual_assets_per_chapter_relpaths(seq):
        path = project_dir / rel
        if not (path.is_file() and path.stat().st_size > 0):
            return False
    return True


def chapters_eligible_for_visual(project_dir: Path) -> List[int]:
    """已经有章节分析MD的章节才能跑视觉资产。"""
    result: List[int] = []
    for ch in chapter_files(project_dir):
        seq = seq_from_file(ch)
        analysis = project_dir / "章节处理" / ch.name
        if analysis.is_file() and analysis.stat().st_size > 0:
            result.append(seq)
    return result


def chapter_structure_per_chapter_relpaths(seq: int) -> List[str]:
    base = f"全书分析/故事结构/分章/ch{seq:03d}"
    return [f"{base}/章节结构.md"]


def per_chapter_analysis_relpaths(task: str, seq: int) -> List[str]:
    if task == "visual_assets":
        return visual_assets_per_chapter_relpaths(seq)
    if task == "chapter_structure":
        return chapter_structure_per_chapter_relpaths(seq)
    raise ValueError(f"unsupported per-chapter analysis task: {task}")


def chapter_has_per_chapter_analysis(project_dir: Path, task: str, seq: int) -> bool:
    for rel in per_chapter_analysis_relpaths(task, seq):
        path = project_dir / rel
        if not (path.is_file() and path.stat().st_size > 0):
            return False
    return True


def resolve_analysis_worker_command(worker_command: str, worker_provider: str, run_mode: str) -> Tuple[str, str]:
    mode = effective_run_mode(run_mode, worker_command, "", worker_provider)
    if mode != "worker":
        return mode, ""
    if worker_command:
        return mode, worker_command
    if not worker_provider or worker_provider == "manual":
        return mode, ""
    return mode, builtin_agent_worker_command(worker_provider)


def task_package_root(project_dir: Path) -> Path:
    return project_dir / "全书分析" / "_任务包"


def latest_analysis_manifest(project_dir: Path, suffix: str) -> Optional[Path]:
    root = task_package_root(project_dir)
    candidates = [path / "manifest.json" for path in root.glob(f"*_{suffix}") if (path / "manifest.json").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_analysis_worker_pack(project_dir: Path, manifest_path: Path, record: Dict[str, Any]) -> Path:
    parts: List[str] = []
    for key in ("pack", "inventory", "reduce_prompt"):
        rel = record.get(key)
        if not rel:
            continue
        path = project_dir / rel
        if path.is_file():
            parts.append(f"# {key}: {rel}\n\n{path.read_text(encoding='utf-8', errors='ignore')}")
    if not parts:
        raise ValueError(f"manifest record has no readable task material: {manifest_path}")
    seq = record.get("seq")
    name = f"worker_pack_ch{int(seq):03d}.md" if isinstance(seq, int) else "worker_pack.md"
    out = manifest_path.parent / name
    out.write_text("\n\n".join(parts), encoding="utf-8")
    return out


def run_analysis_context_pack_for_auto(args: argparse.Namespace, task: str, seq: int = 0, aggregate: bool = False) -> int:
    project_dir = Path(args.project_dir)
    cmd = [
        sys.executable,
        str(script_path("analysis_context_pack.py")),
        str(project_dir),
        "--task", task,
    ]
    if aggregate:
        cmd.append("--aggregate")
    else:
        cmd.extend([
            "--chapters", str(seq),
            "--per-chapter",
            "--include-original", args.include_original,
            "--max-pack-chars", str(args.max_pack_chars),
            "--max-original-chars", str(args.max_original_chars),
            "--max-analysis-chars", str(args.max_analysis_chars),
        ])
    return run_cmd(cmd)


def load_latest_pack_record(project_dir: Path, suffix: str, seq: int = 0) -> Tuple[Path, Dict[str, Any]]:
    manifest_path = latest_analysis_manifest(project_dir, suffix)
    if manifest_path is None:
        raise FileNotFoundError(f"no analysis manifest found for {suffix}")
    manifest = load_json(manifest_path, {})
    if seq:
        for record in manifest.get("packs", []):
            if isinstance(record, dict) and int(record.get("seq", 0) or 0) == seq:
                return manifest_path, record
        raise FileNotFoundError(f"manifest has no pack record for ch{seq:03d}: {manifest_path}")
    return manifest_path, manifest


def cmd_per_chapter_analysis_auto(args: argparse.Namespace, task: str) -> int:
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    mode, worker_command = resolve_analysis_worker_command(
        getattr(args, "worker_command", ""),
        getattr(args, "worker_provider", "manual"),
        getattr(args, "run_mode", "auto"),
    )
    rerun_command = build_analysis_auto_foreground_command(args, task, mode)
    print(f"{task} route: mode={mode} worker_provider={getattr(args, 'worker_provider', 'manual')}")
    eligible = chapters_eligible_for_visual(project_dir)
    if not eligible:
        print("尚无任何章节具备章节分析MD；请先完成章节分析后再运行分章分析自动流程。")
        return 1

    suffix = f"{task}_per_chapter"
    while True:
        pending = [seq for seq in eligible if not chapter_has_per_chapter_analysis(project_dir, task, seq)]
        if not pending:
            break
        next_seq = pending[0]
        rc = run_analysis_context_pack_for_auto(args, task, seq=next_seq)
        if rc != 0:
            return rc
        manifest_path, record = load_latest_pack_record(project_dir, suffix, next_seq)
        expected_outputs = [project_dir / rel for rel in record.get("outputs", [])]
        task_pack = write_analysis_worker_pack(project_dir, manifest_path, record)
        remaining = len(pending) - 1
        print(f"generated {task} task pack for ch{next_seq:03d}; remaining chapters: {remaining}")
        if not worker_command:
            print("handoff: complete the task pack, write expected artifact(s), then rerun the same command.")
            return 2
        rc = run_multi_output_worker(
            worker_command,
            project_dir,
            task,
            task_pack,
            expected_outputs,
            next_seq,
            timeout=getattr(args, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS),
            retries=getattr(args, "worker_retries", DEFAULT_WORKER_RETRIES),
            worker_window=getattr(args, "worker_window", DEFAULT_WORKER_WINDOW),
        )
        if rc != 0:
            return rc
        if worker_supervision_checkpoint(getattr(args, "worker_loop", DEFAULT_WORKER_LOOP), f"{task} artifacts written for ch{next_seq:03d}", rerun_command):
            return 2

    if task != "visual_assets":
        print(f"all per-chapter {task} artifacts are complete.")
        return 0

    aggregate_marker = project_dir / "全书分析" / "视觉资产" / "视觉资产清单.md"
    if aggregate_marker.is_file() and aggregate_marker.stat().st_size > 0 and not getattr(args, "force_aggregate", False):
        print("all per-chapter visual assets and top-level aggregate already exist.")
        return 0
    rc = run_analysis_context_pack_for_auto(args, "visual_assets", aggregate=True)
    if rc != 0:
        return rc
    manifest_path, record = load_latest_pack_record(project_dir, "visual_assets_aggregate")
    expected_outputs = [project_dir / rel for rel in record.get("target_outputs", [])]
    task_pack = write_analysis_worker_pack(project_dir, manifest_path, record)
    print("generated visual_assets aggregate task pack.")
    if not worker_command:
        print("handoff: complete the aggregate task pack, write top-level artifact(s), then rerun the same command.")
        return 2
    rc = run_multi_output_worker(
        worker_command,
        project_dir,
        "visual_assets_aggregate",
        task_pack,
        expected_outputs,
        0,
        timeout=getattr(args, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS),
        retries=getattr(args, "worker_retries", DEFAULT_WORKER_RETRIES),
        worker_window=getattr(args, "worker_window", DEFAULT_WORKER_WINDOW),
    )
    if rc != 0:
        return rc
    if worker_supervision_checkpoint(getattr(args, "worker_loop", DEFAULT_WORKER_LOOP), "visual_assets aggregate artifacts written", rerun_command):
        return 2
    return 0


def cmd_visual_assets_auto(args: argparse.Namespace) -> int:
    return cmd_per_chapter_analysis_auto(args, "visual_assets")


def cmd_chapter_structure_auto(args: argparse.Namespace) -> int:
    return cmd_per_chapter_analysis_auto(args, "chapter_structure")


def cmd_visual_assets_auto_legacy(args: argparse.Namespace) -> int:
    """按章自主推进视觉资产生成；遇到缺产物的章节返回 2 等主控Agent写入。"""
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    eligible = chapters_eligible_for_visual(project_dir)
    if not eligible:
        print("尚无任何章节具备章节分析MD；请先完成章节分析后再运行 visual-assets-auto。")
        return 1

    pending = [seq for seq in eligible if not chapter_has_per_chapter_visual(project_dir, seq)]
    if pending:
        # 取第一个待办章节生成单章任务包
        next_seq = pending[0]
        cmd = [
            sys.executable,
            str(script_path("analysis_context_pack.py")),
            str(project_dir),
            "--task", "visual_assets",
            "--chapters", str(next_seq),
            "--per-chapter",
            "--include-original", args.include_original,
            "--max-pack-chars", str(args.max_pack_chars),
            "--max-original-chars", str(args.max_original_chars),
            "--max-analysis-chars", str(args.max_analysis_chars),
        ]
        rc = run_cmd(cmd)
        if rc != 0:
            return rc
        remaining = len(pending) - 1
        print(f"已生成第{next_seq:03d}章视觉资产任务包；剩余 {remaining} 章待生成。")
        print("请主控Agent按 reduce_prompt 写入分章五件套后再次运行。")
        return 2

    # 全部分章产物齐全，生成 aggregate 任务包
    aggregate_marker = project_dir / "全书分析" / "视觉资产" / "视觉资产清单.md"
    if aggregate_marker.is_file() and aggregate_marker.stat().st_size > 0 and not getattr(args, "force_aggregate", False):
        print("所有章节分章视觉资产已生成，且顶层汇总已存在。使用 --force-aggregate 重新生成 aggregate 任务包。")
        return 0

    cmd = [
        sys.executable,
        str(script_path("analysis_context_pack.py")),
        str(project_dir),
        "--task", "visual_assets",
        "--aggregate",
    ]
    rc = run_cmd(cmd)
    if rc != 0:
        return rc
    print("所有章节分章视觉资产已生成；已交接：等待主控Agent按 aggregate 任务包写入顶层五件套。")
    return 2


def cmd_analysis_status(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    print("=== 全书/局部分析状态 ===")
    for task, outputs in ANALYSIS_OUTPUTS.items():
        print(f"\n[{task}]")
        for rel in outputs:
            path = project_dir / rel
            status = "[x]" if path.is_file() and path.stat().st_size > 0 else "[ ]"
            size = path.stat().st_size if path.is_file() else 0
            print(f"  {status} {rel} ({size} bytes)")
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    story = project_dir / (args.file or "故事结构_增量.json")
    cmd = [sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--backup", "--report", str(project_dir / "质量治理" / "规范化" / "normalize_manual.txt")]
    if args.quarantine_invalid_refs:
        cmd.append("--quarantine-invalid-refs")
    return run_cmd(cmd)


def cmd_migrate_chronology(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    cmd = [sys.executable, str(script_path("migrate_chronology.py")), str(project_dir)]
    if args.file:
        cmd.extend(["--file", args.file])
    if args.dry_run:
        cmd.append("--dry-run")
    return run_cmd(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="拆书主控器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dirs = sub.add_parser("init-dirs", help="补齐项目目录结构")
    p_dirs.add_argument("project_dir")

    p_split = sub.add_parser("split", help="拆分原文并初始化故事结构")
    p_split.add_argument("project_dir")
    p_split.add_argument("source_file")
    p_split.add_argument("--pattern", default="")
    p_split.add_argument("--preface-mode", choices=["separate", "attach", "drop"], default="")

    p_prepare = sub.add_parser("prepare-chapter", help="根据当前产物状态生成章节分析或Delta提取任务包")
    p_prepare.add_argument("project_dir")
    p_prepare.add_argument("--chapter", type=int, default=0)
    p_prepare.add_argument("--force", action="store_true")

    p_commit = sub.add_parser("commit-chapter", help="提交已填写好的章节分析MD和Delta")
    p_commit.add_argument("project_dir")
    p_commit.add_argument("--chapter", type=int, required=True)
    p_commit.add_argument("--force", action="store_true")

    p_run = sub.add_parser("run", help="持续提交已具备产物的章节；周期审计任务由主控Agent处理后续提交")
    p_run.add_argument("project_dir")
    p_run.add_argument("--max-chapters", type=int, default=0)
    p_run.add_argument("--audit-interval", type=int, default=5, help="每N章执行一次自动周期治理；0表示关闭")
    p_run.add_argument("--audit-max-context-chars", type=positive_int, default=DEFAULT_AUDIT_MAX_CONTEXT_CHARS, help="周期审计单包最大字符数；默认按256k上下文模型预留输出和安全余量")
    p_run.add_argument("--phase", choices=["auto", "analysis", "delta"], default="auto", help="运行阶段：auto=旧逐章流程；analysis=只循环生成/校验章节分析MD；delta=先要求全部MD合格，再循环生成/提交结构JSON")
    p_run.add_argument("--run-mode", choices=RUN_MODES, default="auto", help="routing mode: subagent=main agent dispatches a child agent, serial=main agent handles return-2 task packs then reruns, worker=external CLI worker, auto=worker when configured otherwise subagent")
    p_run.add_argument("--chapter-command", default="", help="外部章节 worker 命令；由 run 传入当前任务包并验收章节分析/Delta/修复产物")
    p_run.add_argument("--audit-command", default="", help="外部审计 worker 命令；由 run 传入周期审计任务包并验收 correction 产物")
    p_run.add_argument("--worker-provider", choices=WORKER_PROVIDERS, default="manual", help="built-in worker provider; only used by --run-mode worker or auto worker routing")
    p_run.add_argument("--worker-timeout-seconds", type=positive_int, default=DEFAULT_WORKER_TIMEOUT_SECONDS, help="单次外部 worker 命令超时秒数")
    p_run.add_argument("--worker-retries", type=non_negative_int, default=DEFAULT_WORKER_RETRIES, help="外部 worker 失败或未写出产物后的重试次数；默认 2，表示总共执行 3 次")
    p_run.add_argument("--worker-loop", choices=WORKER_LOOPS, default=DEFAULT_WORKER_LOOP, help="worker loop mode: supervised=return after each worker task/chapter checkpoint by default, continuous=run until completion or failure")

    p_resume = sub.add_parser("resume", help="查看下一步缺什么")
    p_run.add_argument("--worker-window", choices=WORKER_WINDOWS, default=DEFAULT_WORKER_WINDOW, help="worker display mode: hidden=current terminal/log by default, powershell=visible auto-close window, powershell-keep=visible window waits for Enter")

    p_resume.add_argument("project_dir")

    p_recover = sub.add_parser("recover-story", help="从 story_after_chNNN 快照恢复过程库，并重放后续Delta")
    p_recover.add_argument("project_dir")
    p_recover.add_argument("--snapshot", type=int, default=0, help="作为基线的快照章节号；默认使用最新 story_after_chNNN")
    p_recover.add_argument("--to", type=int, default=0, help="重放到的章节号；默认使用已有Delta的最大章节号")

    p_norm = sub.add_parser("normalize", help="保全式规范化故事结构JSON")
    p_norm.add_argument("project_dir")
    p_norm.add_argument("--file", default="")
    p_norm.add_argument("--quarantine-invalid-refs", action="store_true")

    p_migrate = sub.add_parser("migrate-chronology", help="按_索引.json回填章节时间、紧凑追溯字段并重排存量事件")
    p_migrate.add_argument("project_dir")
    p_migrate.add_argument("--file", default="")
    p_migrate.add_argument("--dry-run", action="store_true")

    p_gov = sub.add_parser("commit-governance", help="提交按需/周期治理补丁")
    p_gov.add_argument("project_dir")
    p_gov.add_argument("patch_json")

    p_audit = sub.add_parser("audit-pack", help="生成周期结构审计任务包")
    p_audit.add_argument("project_dir")
    p_audit.add_argument("--chapters", required=True, help="章节范围，例如 1-5 或 1-10")
    p_audit.add_argument("--force", action="store_true")
    p_audit.add_argument("--max-context-chars", type=positive_int, default=DEFAULT_AUDIT_MAX_CONTEXT_CHARS, help="周期审计单包最大字符数；默认按256k上下文模型预留输出和安全余量")

    p_pack = sub.add_parser("analysis-pack", help="生成全书/局部分析任务包")
    p_pack.add_argument("project_dir")
    p_pack.add_argument("--task", choices=["summary", "characters", "plot", "style", "visual_assets", "report", "custom", "worldview", "plotlines", "outline", "detailed_outline", "chapter_structure", "narrative_structure", "settings"], required=True)
    p_pack.add_argument("--chapters", default="all")
    p_pack.add_argument("--targets", nargs="*", default=[])
    p_pack.add_argument("--question", default="")
    p_pack.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_pack.add_argument("--max-pack-chars", type=int, default=70000)
    p_pack.add_argument("--max-original-chars", type=int, default=6000)
    p_pack.add_argument("--max-analysis-chars", type=int, default=12000)
    p_pack.add_argument("--out-dir", default="")
    p_pack.add_argument("--per-chapter", action="store_true", help="每章一份独立任务包（visual_assets / chapter_structure），产物写入对应的 全书分析/<维度>/分章/chNNN/ 目录")
    p_pack.add_argument("--aggregate", action="store_true", help="（仅 visual_assets）生成顶层汇总任务包，从 分章/chNNN/ 合并顶层五件套")

    p_status = sub.add_parser("analysis-status", help="查看全书/局部分析状态")
    p_status.add_argument("project_dir")

    p_vauto = sub.add_parser("visual-assets-auto", help="按章自主推进视觉资产生成（每次返回2交接一章，全部完成后交接aggregate）")
    p_vauto.add_argument("project_dir")
    p_vauto.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_vauto.add_argument("--max-pack-chars", type=int, default=70000)
    p_vauto.add_argument("--max-original-chars", type=int, default=6000)
    p_vauto.add_argument("--max-analysis-chars", type=int, default=12000)
    p_vauto.add_argument("--force-aggregate", action="store_true", help="即便顶层汇总已存在，也重新生成 aggregate 任务包")

    p_vauto.add_argument("--run-mode", choices=RUN_MODES, default="auto", help="routing mode: subagent, serial, worker, or auto")
    p_vauto.add_argument("--worker-provider", choices=WORKER_PROVIDERS, default="manual", help="built-in worker provider for worker route")
    p_vauto.add_argument("--worker-command", default="", help="external multi-output analysis worker command")
    p_vauto.add_argument("--worker-timeout-seconds", type=positive_int, default=DEFAULT_WORKER_TIMEOUT_SECONDS)
    p_vauto.add_argument("--worker-retries", type=non_negative_int, default=DEFAULT_WORKER_RETRIES)
    p_vauto.add_argument("--worker-loop", choices=WORKER_LOOPS, default=DEFAULT_WORKER_LOOP, help="worker loop mode: supervised by default, or continuous")

    p_sauto = sub.add_parser("chapter-structure-auto", help="按章自主推进故事结构章节结构生成")
    p_vauto.add_argument("--worker-window", choices=WORKER_WINDOWS, default=DEFAULT_WORKER_WINDOW, help="worker display mode: hidden by default, powershell, or powershell-keep")

    p_sauto.add_argument("project_dir")
    p_sauto.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_sauto.add_argument("--max-pack-chars", type=int, default=70000)
    p_sauto.add_argument("--max-original-chars", type=int, default=6000)
    p_sauto.add_argument("--max-analysis-chars", type=int, default=12000)
    p_sauto.add_argument("--force-aggregate", action="store_true", help=argparse.SUPPRESS)
    p_sauto.add_argument("--run-mode", choices=RUN_MODES, default="auto", help="routing mode: subagent, serial, worker, or auto")
    p_sauto.add_argument("--worker-provider", choices=WORKER_PROVIDERS, default="manual", help="built-in worker provider for worker route")
    p_sauto.add_argument("--worker-command", default="", help="external multi-output analysis worker command")
    p_sauto.add_argument("--worker-timeout-seconds", type=positive_int, default=DEFAULT_WORKER_TIMEOUT_SECONDS)
    p_sauto.add_argument("--worker-retries", type=non_negative_int, default=DEFAULT_WORKER_RETRIES)
    p_sauto.add_argument("--worker-loop", choices=WORKER_LOOPS, default=DEFAULT_WORKER_LOOP, help="worker loop mode: supervised by default, or continuous")
    p_sauto.add_argument("--worker-window", choices=WORKER_WINDOWS, default=DEFAULT_WORKER_WINDOW, help="worker display mode: hidden by default, powershell, or powershell-keep")

    p_fpack = sub.add_parser("final-pack", help="生成最终结构整理任务包（复制增量为草稿+校验+打包参考材料）")
    p_fpack.add_argument("project_dir")
    p_fpack.add_argument("--force", action="store_true", help="强制重新从增量复制草稿")

    p_cfd = sub.add_parser("commit-final-draft", help="提交模型修好的最终草稿（规范化+strict校验）")
    p_cfd.add_argument("project_dir")

    p_vf = sub.add_parser("validate-final", help="独立最终强校验")
    p_vf.add_argument("project_dir")
    p_vf.add_argument("--file", default="", help="校验目标文件名，默认 故事结构_草稿.json")

    p_fin = sub.add_parser("finalize", help="通过校验后复制草稿为最终 故事结构.json")
    p_fin.add_argument("project_dir")

    args = parser.parse_args()
    project_dir = Path(getattr(args, "project_dir", "."))

    if args.cmd == "init-dirs":
        init_dirs(project_dir); print(f"目录已补齐: {project_dir}"); return 0
    if args.cmd == "split":
        return cmd_split(args)
    if args.cmd == "prepare-chapter":
        return prepare_chapter(project_dir, args.chapter or None, args.force)
    if args.cmd == "commit-chapter":
        return commit_chapter(project_dir, args.chapter, args.force)
    if args.cmd == "run":
        return cmd_run(
            project_dir,
            args.max_chapters,
            args.audit_interval,
            phase=args.phase,
            audit_max_context_chars=args.audit_max_context_chars,
            chapter_command=args.chapter_command,
            audit_command=args.audit_command,
            run_mode=args.run_mode,
            worker_provider=args.worker_provider,
            worker_timeout_seconds=args.worker_timeout_seconds,
            worker_retries=args.worker_retries,
            worker_window=args.worker_window,
            worker_loop=args.worker_loop,
        )
    if args.cmd == "resume":
        return cmd_resume(project_dir)
    if args.cmd == "recover-story":
        return recover_story_from_snapshot(project_dir, args.snapshot or None, args.to or None)
    if args.cmd == "normalize":
        return cmd_normalize(args)
    if args.cmd == "migrate-chronology":
        return cmd_migrate_chronology(args)
    if args.cmd == "commit-governance":
        return commit_governance(project_dir, args.patch_json)
    if args.cmd == "audit-pack":
        return cmd_audit_pack(args)
    if args.cmd == "analysis-pack":
        return cmd_analysis_pack(args)
    if args.cmd == "analysis-status":
        return cmd_analysis_status(args)
    if args.cmd == "visual-assets-auto":
        return cmd_visual_assets_auto(args)
    if args.cmd == "chapter-structure-auto":
        return cmd_chapter_structure_auto(args)
    if args.cmd == "final-pack":
        return cmd_final_pack(project_dir, getattr(args, "force", False))
    if args.cmd == "commit-final-draft":
        return cmd_commit_final_draft(project_dir)
    if args.cmd == "validate-final":
        return cmd_validate_final(project_dir, getattr(args, "file", ""))
    if args.cmd == "finalize":
        return cmd_finalize(project_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
