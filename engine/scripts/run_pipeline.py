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

from context_budget import CONTEXT_EXCEEDED, ContextBudget, admit_pack, estimate_tokens, inspect_pack

import argparse
import hashlib
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
from contextvars import copy_context
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from chronology import chapter_time
from governance_contract import normalize_governance_patch, periodic_introduction_errors
from merge_delta import save_json as save_atomic_json
from story_schema_rules import COLLECTION_KEYS
from validate_chapter_analysis import validate_chapter_analysis_file
from validate_structure import validate_structure_file

from prompt_loader import render_prompt, prompt_fingerprint
from worker_admission import worker_admission
from fault_recovery import recover_artifact
from worker_checkpoint import atomic_write_bytes
from repair_budget import REPAIR_EXHAUSTED, REPAIR_TASKS, reserve_repair, finish_repair
from worker_artifacts import (
    capture_output_signatures,
    confined_project_path,
    invalid_output_artifacts,
    unchanged_existing_outputs,
)


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

REQUIRED_DIRS = [
    "原文", "原文拆解", "章节处理", "章节处理/_任务包", "章节处理/_修复任务", "章节处理/Delta",
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
DEFAULT_WORKER_TIMEOUT_SECONDS = int(os.getenv("ND_WORKER_TIMEOUT", "1200"))
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
# 字符预算而不伪造精确 token 计数。默认 524288 = 512×1024，对应 512K 字符预算，
# 适配长上下文模型；调用方可按实际模型覆盖。
DEFAULT_AUDIT_MAX_CONTEXT_CHARS = 524_288
AUDIT_CONTEXT_FORMAT_VERSION = 5
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


# 可取消的 worker：每个运行上下文（编排器/工作台任务）通过 set_cancel_event 把自己的
# threading.Event 注入线程本地存储；stream_shell_command 轮询它，被取消时 kill 子进程
# 并返回 130（用户取消）。用 threading.local 而非模块全局，避免并发运行互相串取消信号。
_cancel_local = threading.local()


def set_cancel_event(event: "threading.Event") -> None:
    _cancel_local.event = event


def set_recovery_observer(observer: Optional[Callable]) -> None:
    """Bind recovery events to this run, avoiding project polling or shared state."""
    _cancel_local.recovery_observer = observer


def _get_cancel_event() -> "Optional[threading.Event]":
    return getattr(_cancel_local, "event", None)


def _create_windows_kill_on_close_job(proc: "subprocess.Popen") -> object | None:
    """Attach a worker launcher to a Windows job that dies with this process.

    ``taskkill /T`` handles ordinary timeout paths, but it cannot run when the
    desktop backend itself is terminated or restarted.  A kill-on-close job is
    enforced by Windows, so provider descendants cannot survive that abrupt
    shutdown and write stale artifacts later.
    """
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(proc._handle)  # noqa: SLF001 - required Windows process handle
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return job


def _close_windows_job(job: object | None) -> None:
    """Close a Windows job handle, terminating any descendants still in it."""
    if job is None or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(job)


def _kill_tree(proc: "subprocess.Popen") -> None:
    """杀掉 proc 及其整棵子进程树。

    Windows 上 Popen.kill()（= TerminateProcess）只杀直接子进程；经 shell=True
    启动的 worker 实为 cmd.exe -> python -> agy/node，单杀 cmd.exe 会让真正在
    干活的 agy 成孤儿、继续占锁，导致续跑时新 worker 启动即报错。故取消/
    超时统一用 taskkill /T 递归杀整棵树。
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.kill()
    proc.wait()


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
    # The OS closes this handle automatically even if the backend is killed,
    # which prevents a detached provider from completing against stale state.
    windows_job = _create_windows_kill_on_close_job(proc)
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
    started = time.monotonic()
    heartbeat = started + 30
    deadline = started + timeout if timeout else None

    def forward(line: str) -> None:
        output_parts.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
        # Persist as output arrives, so a backend interruption does not erase
        # the diagnostic evidence accumulated before the worker returns.
        if env.get("ND_WORKER_LOG_PATH"):
            with Path(env["ND_WORKER_LOG_PATH"]).open("a", encoding="utf-8") as stream:
                stream.write(line)

    def drain_output() -> None:
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                break
            forward(line)

    try:
        while True:
            drain_output()
            _ce = _get_cancel_event()
            if _ce is not None and _ce.is_set():
                _close_windows_job(windows_job)
                windows_job = None
                _kill_tree(proc)
                reader.join(timeout=1)
                drain_output()
                message = "[cancelled by user]\n"
                forward(message)
                return 130, "".join(output_parts)
            if proc.poll() is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                _close_windows_job(windows_job)
                windows_job = None
                _kill_tree(proc)
                reader.join(timeout=1)
                drain_output()
                message = timeout_output(subprocess.TimeoutExpired(command, timeout, output=""), timeout)
                forward(message)
                return 124, "".join(output_parts)
            if time.monotonic() >= heartbeat:
                message = f"[任务仍在运行，已等待 {int(time.monotonic() - started)} 秒；尚未收到完成结果]\n"
                forward(message)
                heartbeat = time.monotonic() + 30
            time.sleep(0.1)

        reader.join(timeout=1)
        drain_output()
        return proc.returncode or 0, "".join(output_parts)
    finally:
        _close_windows_job(windows_job)


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
        windows_job = _create_windows_kill_on_close_job(proc)
        deadline = time.monotonic() + timeout + 10 if timeout else None
        try:
            while proc.poll() is None:
                cancel_event = _get_cancel_event()
                if cancel_event is not None and cancel_event.is_set():
                    _close_windows_job(windows_job)
                    windows_job = None
                    _kill_tree(proc)
                    proc.wait()
                    return 130, "[cancelled by user]\n"
                if deadline is not None and time.monotonic() >= deadline:
                    _close_windows_job(windows_job)
                    windows_job = None
                    _kill_tree(proc)
                    proc.wait()
                    launcher_output = ""
                    message = timeout_output(
                        subprocess.TimeoutExpired(command, timeout, output=launcher_output), timeout
                    )
                    previous = (
                        log_path.read_text(encoding="utf-8", errors="replace")
                        if log_path.exists() else ""
                    )
                    log_path.write_text(previous + message, encoding="utf-8")
                    return 124, message
                time.sleep(0.1)
            launcher_output = ""
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
            _close_windows_job(windows_job)
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
    env.update(ContextBudget.from_env().to_env())
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


def worker_log_path(project_dir: Path, task_type: str, seq: int = 0) -> Path:
    # seq 非零时文件名带 _ch{seq:03d}：分章任务并行时避免同秒同 task_type 的日志互相覆盖，
    # 同时让日志文件名自带章号，便于按阶段/章分类展示。
    # 微秒后缀避免并行任务或快速重试在同一秒覆盖彼此日志。
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_type)
    suffix = f"_ch{seq:03d}" if seq else ""
    return project_dir / "质量治理" / "worker日志" / f"{safe_task}{suffix}_{ts}.txt"


def wait_before_worker_retry(attempt: int) -> bool:
    """Apply bounded exponential backoff; return True when cancelled while waiting."""
    if attempt <= 1:
        return False
    try:
        base = max(0.0, float(os.environ.get("ND_WORKER_RETRY_DELAY_SECONDS", "1")))
    except ValueError:
        base = 1.0
    delay = min(30.0, base * (2 ** (attempt - 2)))
    if delay <= 0:
        return False
    print(f"worker retry backoff: {delay:g}s")
    cancel_event = _get_cancel_event()
    if cancel_event is not None:
        return cancel_event.wait(delay)
    time.sleep(delay)
    return False


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


def _admit_worker(function):
    """Share model capacity across both worker APIs, including cancellable waits."""
    @wraps(function)
    def admitted(*args, **kwargs):
        with worker_admission.slot(_get_cancel_event()) as acquired:
            if not acquired:
                return 130
            return function(*args, **kwargs)
    return admitted


def needs_capacity_tree(pack: Path) -> bool:
    """Dispatch outside worker admission so child calls never hold a parent slot."""
    if not pack.with_suffix(".hierarchy.json").is_file():
        return False
    if load_json(pack.with_suffix(".hierarchy.json"), {}).get("require_review"):
        return True
    report = inspect_pack(pack)
    return not report["passed"] or report["estimated_input_tokens"] > ContextBudget.from_env().output_tokens * 3


def run_worker(command, project_dir, task_type, task_pack, expected_output, chapter_seq=0, **options):
    """Keep ordinary calls unchanged; page explicitly declared report workloads."""
    if command and needs_capacity_tree(task_pack):
        from bounded_generation import run_hierarchical_reports
        return run_hierarchical_reports(command, project_dir, task_pack, [expected_output], run_worker, **options)
    rc = _run_worker(command, project_dir, task_type, task_pack, expected_output, chapter_seq, **options)
    if rc == CONTEXT_EXCEEDED and task_pack.with_suffix(".hierarchy.json").is_file():
        from bounded_generation import run_hierarchical_reports
        return run_hierarchical_reports(command, project_dir, task_pack, [expected_output], run_worker, **options)
    return rc


@_admit_worker
def _run_worker(
    command: str,
    project_dir: Path,
    task_type: str,
    task_pack: Path,
    expected_output: Path,
    chapter_seq: int = 0,
    timeout: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    retries: int = 0,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    repair_limit: Optional[int] = None,
    defer_artifact_repair: bool = False,
) -> int:
    """Run an external model/agent worker for exactly one task package."""
    if not command:
        return 2
    if not admit_pack(task_pack):
        return CONTEXT_EXCEEDED
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        if wait_before_worker_retry(attempt):
            return 130
        ticket = None
        if task_type in REPAIR_TASKS:
            ticket = reserve_repair(project_dir, expected_output, task_type,
                                    repair_limit if repair_limit is not None else attempts)
            if ticket is None:
                return REPAIR_EXHAUSTED
        before_outputs = capture_output_signatures([expected_output])
        env = dict(os.environ)
        env.update(ContextBudget.from_env().to_env())
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "ND_TASK_TYPE": task_type,
            "ND_PROJECT_DIR": str(project_dir),
            "ND_TASK_PACK": str(task_pack),
            "ND_EXPECTED_OUTPUT": str(expected_output),
            "ND_CHAPTER_SEQ": str(chapter_seq or ""),
            "ND_WORKER_ATTEMPT": str(attempt),
        })
        log_path = worker_log_path(project_dir, task_type, chapter_seq)
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
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            env["ND_WORKER_LOG_PATH"] = str(log_path.resolve())
            returncode, output = stream_shell_command(command, project_dir, env, timeout)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
        if returncode != 0:
            finish_repair(ticket, returncode)
            print(f"worker失败: task={task_type} attempt={attempt}/{attempts} rc={returncode} log={log_path}")
            if returncode in {130, CONTEXT_EXCEEDED}:
                return returncode  # Capacity and user cancellation have different recovery routes.
            if (defer_artifact_repair and returncode == 1 and expected_output.is_file()
                    and expected_output.stat().st_size and invalid_output_artifacts([expected_output])):
                return 1  # Give deterministic repair the malformed artifact first.
            if attempt < attempts:
                continue
            return returncode
        if not expected_output.is_file() or expected_output.stat().st_size == 0:
            finish_repair(ticket, 1)
            print(f"worker未写出期望产物: {expected_output}")
            print(f"worker日志: {log_path}")
            if attempt < attempts:
                continue
            return 1
        invalid = invalid_output_artifacts([expected_output])
        if invalid:
            finish_repair(ticket, 1)
            print(f"worker写出的产物格式无效: {expected_output}")
            print(f"worker日志: {log_path}")
            if defer_artifact_repair:
                return 1
            if attempt < attempts:
                continue
            return 1
        unchanged = (
            unchanged_existing_outputs(before_outputs, [expected_output])
            if os.environ.get("ND_WORKER_REQUIRE_FRESH_OUTPUT", "1") != "0"
            else []
        )
        if unchanged:
            finish_repair(ticket, 1)
            print(f"worker未更新已存在的期望产物: {expected_output}")
            print(f"worker日志: {log_path}")
            if attempt < attempts:
                continue
            return 1
        finish_repair(ticket, 0)
        print(f"worker完成: task={task_type} output={expected_output} log={log_path}")
        return 0
    return 1


def run_repair_worker(
    command: str, project_dir: Path, task_type: str, task_pack: Path,
    expected_output: Path, chapter_seq: int = 0,
    timeout: float = DEFAULT_WORKER_TIMEOUT_SECONDS, retries: int = 0,
    worker_window: str = DEFAULT_WORKER_WINDOW, repair_limit: Optional[int] = None,
    observe: Optional[Callable] = None,
) -> int:
    """Resolve generated-content failures without granting models commit authority."""
    project_dir = project_dir.resolve()
    relative = expected_output.resolve().relative_to(project_dir)
    pack_relative = task_pack.resolve().relative_to(project_dir)

    def validate(root: Path) -> Tuple[int, str]:
        if task_type == "repair_chapter":
            rc = commit_chapter(root, chapter_seq)
        elif task_type == "audit_repair":
            rc = commit_governance(root, str(root / relative))
        elif task_type == "repair_final":
            rc = cmd_commit_final_draft(root)
        elif task_type == "analysis_regenerate":
            ok, errors = validate_chapter_analysis_file(root / relative)
            if not ok:
                chapter = chapter_by_seq(root, chapter_seq)
                if chapter:
                    write_analysis_regenerate_task(root, chapter, errors)
            return (0 if ok else 1), "\n".join(errors)
        else:
            raise ValueError(f"不支持的自动修复任务：{task_type}")
        report = root / pack_relative
        return rc, report.read_text(encoding="utf-8") if report.is_file() else f"原校验器返回 {rc}"

    def publish(candidate: Path) -> int:
        # Publish the generated artifact first; existing chapter/governance commit
        # receipts protect the subsequent formal structure commit against replay.
        atomic_write_bytes(expected_output, candidate.read_bytes())
        rc, _ = validate(project_dir)
        return rc

    return recover_artifact(
        project_dir, task_type, task_pack, expected_output, command=command,
        worker=run_worker, validate=validate, publish=publish,
        cancel=_get_cancel_event(), chapter_seq=chapter_seq, timeout=timeout,
        retries=retries, worker_window=worker_window,
        strategies=3 if repair_limit is None else repair_limit,
        observe=observe or getattr(_cancel_local, "recovery_observer", None),
    )



def run_generation_worker(
    command: str, project_dir: Path, task_type: str, task_pack: Path,
    expected_output: Path, chapter_seq: int = 0, **worker_options,
) -> int:
    """Route malformed generated artifacts into recovery after releasing the slot.

    A missing artifact or cancelled worker is not semantic repair input. Existing
    partial artifacts remain inspectable, and original validators retain authority.
    """
    if command and task_type == "audit_correction" and task_pack.with_suffix(".governance.json").is_file():
        from bounded_generation import run_bounded_governance
        return run_bounded_governance(command, project_dir, task_pack, expected_output, run_worker, **worker_options)
    if command and task_type in {"analysis", "delta"} and task_pack.is_file():
        report = inspect_pack(task_pack)
        chapter = chapter_by_seq(project_dir, chapter_seq)
        output_risk = chapter is not None and estimate_tokens(chapter.read_text(encoding="utf-8")) > ContextBudget.from_env().output_tokens * 3
        if not report["passed"] or output_risk:
            from bounded_generation import run_bounded_chapter
            return run_bounded_chapter(command, project_dir, task_type, expected_output, chapter_seq, run_worker, **worker_options)
    if task_type == "final_draft" and expected_output.is_file() and task_pack.is_file():
        policy = ContextBudget.from_env()
        if (estimate_tokens(expected_output.read_text(encoding="utf-8")) > policy.output_tokens
                or not inspect_pack(task_pack)["passed"]):
            from bounded_final import run_bounded_final
            return run_bounded_final(command, project_dir, task_pack, expected_output, run_worker, **worker_options)
    repair_task = {"analysis": "analysis_regenerate", "delta": "repair_chapter",
                   "audit_correction": "audit_repair", "final_draft": "repair_final"}.get(task_type)
    final_before = expected_output.read_bytes() if task_type == "final_draft" and expected_output.is_file() else None
    before_outputs = capture_output_signatures([expected_output])
    rc = run_worker(command, project_dir, task_type, task_pack, expected_output,
                    chapter_seq, defer_artifact_repair=bool(repair_task), **worker_options)
    if rc == CONTEXT_EXCEEDED and task_type == "final_draft" and final_before is not None:
        # Provider/tool usage may exceed the preflight estimate. Continue through
        # the same checkpointed final-governance path instead of redispatching
        # the unchanged oversized request.
        from bounded_final import run_bounded_final
        atomic_write_bytes(expected_output, final_before)
        return run_bounded_final(command, project_dir, task_pack, expected_output, run_worker, **worker_options)
    if rc == CONTEXT_EXCEEDED and task_type in {"analysis", "delta"}:
        from bounded_generation import run_bounded_chapter
        return run_bounded_chapter(command, project_dir, task_type, expected_output, chapter_seq, run_worker, **worker_options)
    if rc == CONTEXT_EXCEEDED and task_type == "audit_correction":
        match = re.fullmatch(r"correction_(\d+)-(\d+)\.json", expected_output.name)
        if match:
            first, last = int(match[1]), int(match[2])
            # Never mistake a truncated provider artifact for a finished patch.
            if expected_output.is_file():
                atomic_write_bytes(expected_output.with_suffix(".capacity-partial.json"), expected_output.read_bytes())
                expected_output.unlink()
            children = split_audit_range(first, last)
            if children:
                paths = audit_paths(project_dir, first, last)
                previous = audit_status(project_dir, first, last)
                limit = previous.get("context", {}).get("max_context_chars", DEFAULT_AUDIT_MAX_CONTEXT_CHARS)
                update_audit_status(paths, status="split_pending", range=audit_range_label(first, last),
                                    child_ranges=[{"start": a, "end": b, "range": audit_range_label(a, b)}
                                                  for a, b in children],
                                    capacity_reason="模型生成长度耗尽，原请求不再重试；已提交子区间直接复用")
                return run_child_periodic_governance(
                    project_dir, first, last, children, limit, command,
                    worker_options.get("timeout", DEFAULT_WORKER_TIMEOUT_SECONDS),
                    worker_options.get("retries", 0), worker_options.get("worker_window", DEFAULT_WORKER_WINDOW),
                    "continuous")
            write_governance_route(project_dir, first, last, task_pack)
            from bounded_generation import run_bounded_governance
            return run_bounded_governance(command, project_dir, task_pack, expected_output, run_worker, **worker_options)
    if rc != 1 or not repair_task or not expected_output.is_file() or not expected_output.stat().st_size:
        return rc
    if unchanged_existing_outputs(before_outputs, [expected_output]) and not invalid_output_artifacts([expected_output]):
        # A pre-existing valid draft is not proof that a failed generation ran.
        # Do not turn an unchanged artifact into successful task completion.
        return rc
    if task_type in {"analysis", "delta"}:
        chapter = chapter_by_seq(project_dir, chapter_seq)
        if chapter is None:
            return rc
        paths = artifact_paths(project_dir, chapter)
        repair_pack = paths["analysis_regenerate_task" if task_type == "analysis" else "repair"]
    elif task_type == "audit_correction":
        repair_pack = expected_output.with_suffix(".repair.md")
    else:
        repair_pack = project_dir / "质量治理/最终审计/repair_final.md"
    return run_repair_worker(command, project_dir, repair_task, repair_pack,
                             expected_output, chapter_seq, **worker_options)


def display_shell_command(args: List[str]) -> str:
    return subprocess.list2cmdline([str(arg) for arg in args if str(arg) != ""])


def build_run_foreground_command(
    project_dir: Path,
    *,
    phase: str = "",
    max_chapters: int = 0,
    end_chapter_seq: int = 0,
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
    if end_chapter_seq:
        args.extend(["--end-chapter-seq", str(end_chapter_seq)])
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
        ("end_chapter_seq", "--end-chapter-seq", 0),
        ("include_original", "--include-original", "auto"),
        ("max_pack_chars", "--max-pack-chars", 70000),
        ("max_original_chars", "--max-original-chars", 6000),
        ("max_analysis_chars", "--max-analysis-chars", 12000),
        ("story_summary_max_items", "--story-summary-max-items", 0),
        ("context_budget_chars", "--context-budget-chars", 160000),
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


def run_multi_output_worker(command, project_dir, task_type, task_pack, expected_outputs, chapter_seq=0, **options):
    """Select a reduction route before acquiring an execution slot."""
    if command and needs_capacity_tree(task_pack):
        from bounded_generation import run_hierarchical_reports
        return run_hierarchical_reports(command, project_dir, task_pack, expected_outputs, run_worker, **options)
    rc = _run_multi_output_worker(command, project_dir, task_type, task_pack, expected_outputs, chapter_seq, **options)
    if rc == CONTEXT_EXCEEDED and task_pack.with_suffix(".hierarchy.json").is_file():
        from bounded_generation import run_hierarchical_reports
        return run_hierarchical_reports(command, project_dir, task_pack, expected_outputs, run_worker, **options)
    return rc


@_admit_worker
def _run_multi_output_worker(
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
    if not admit_pack(task_pack):
        return CONTEXT_EXCEEDED
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        if wait_before_worker_retry(attempt):
            return 130
        before_outputs = capture_output_signatures(expected_outputs)
        env = dict(os.environ)
        env.update(ContextBudget.from_env().to_env())
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
        log_path = worker_log_path(project_dir, task_type, chapter_seq)
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
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            env["ND_WORKER_LOG_PATH"] = str(log_path.resolve())
            returncode, output = stream_shell_command(command, project_dir, env, timeout)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
        if returncode != 0:
            print(f"worker failed: task={task_type} attempt={attempt}/{attempts} rc={returncode} log={log_path}")
            if returncode in {130, CONTEXT_EXCEEDED}:
                return returncode
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
        invalid = invalid_output_artifacts(expected_outputs)
        if invalid:
            print("worker wrote invalid artifact(s):")
            for path in invalid:
                print(f"  - {path}")
            print(f"worker log: {log_path}")
            if attempt < attempts:
                continue
            return 1
        unchanged = (
            unchanged_existing_outputs(before_outputs, expected_outputs)
            if os.environ.get("ND_WORKER_REQUIRE_FRESH_OUTPUT", "1") != "0"
            else []
        )
        if unchanged:
            print("worker did not refresh pre-existing artifact(s):")
            for path in unchanged:
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
    _ensure_delta_layout(project_dir)


def _ensure_delta_layout(project_dir: Path) -> None:
    """自愈：把 章节处理/ 根目录残留的 delta JSON 移入 Delta/ 子目录。

    防止从旧备份恢复后，新代码只在 Delta/ 找 delta 而判定"无 delta"（系统失忆、
    可能从第1章重跑烧钱）。幂等：已迁移或新项目无操作。
    """
    src_dir = project_dir / "章节处理"
    if not src_dir.is_dir():
        return
    dst_dir = src_dir / "Delta"
    moved = 0
    for p in src_dir.glob("第*章_*.json"):
        if not p.is_file():
            continue
        target = dst_dir / p.name
        if target.is_file():
            p.unlink(missing_ok=True)  # Delta/ 已有同名，删根目录残留
        else:
            dst_dir.mkdir(parents=True, exist_ok=True)
            p.rename(target)
        moved += 1
    if moved:
        print(f"已自愈：{moved} 个 delta JSON 从 章节处理/ 根目录移入 Delta/。")


def find_multi_unit_artifacts(project_dir: Path) -> List[Path]:
    """查找把多个拆分单元合并成一个产物的非法章节处理文件。

    分析MD在 章节处理/ 根目录、Delta JSON在 章节处理/Delta/，两处都要查。
    """
    chapter_dir = project_dir / "章节处理"
    if not chapter_dir.is_dir():
        return []
    candidates = list(chapter_dir.iterdir())
    delta_dir = chapter_dir / "Delta"
    if delta_dir.is_dir():
        candidates.extend(delta_dir.iterdir())
    return sorted(
        p for p in candidates
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
    # State/receipts must survive interruption just like story JSON files.
    save_atomic_json(path, data)


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
        "delta": project_dir / "章节处理" / "Delta" / f"{base}.json",
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


# Cache only validated immutable snapshots; any artifact replacement invalidates it.
_chapter_validation_cache: Dict[str, tuple] = {}


def _chapter_receipt_matches(paths: Dict[str, Path], receipt: dict) -> bool:
    """Verify the complete publication evidence, never just a snapshot filename."""
    try:
        expected = {"analysis", "delta", "before", "diff", "delta_report", "chapter_report"}
        hashes = receipt.get("artifacts", {})
        return (set(hashes) == expected
                and all(hashlib.sha256(paths[key].read_bytes()).hexdigest() == value for key, value in hashes.items())
                and hashlib.sha256(paths["after"].read_bytes()).hexdigest() == receipt.get("story_sha256"))
    except (OSError, TypeError, ValueError, AttributeError):
        return False


def is_chapter_complete(project_dir: Path, chapter: Path) -> bool:
    p = artifact_paths(project_dir, chapter)
    required = ["analysis", "delta", "delta_report", "chapter_report", "before", "after", "diff"]
    if not all(p[k].is_file() and p[k].stat().st_size > 0 for k in required):
        return False

    receipt_path = p["after"].with_suffix(".commit.json")
    try:
        paths = [p[key] for key in required] + ([receipt_path] if receipt_path.exists() else [])
        signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_ctime_ns, path.stat().st_size) for path in paths)
        cache_key = str(p["after"].resolve())
        if _chapter_validation_cache.get(cache_key) == signature:
            return True
        if receipt_path.exists():
            receipt = load_json(receipt_path, {})
            if not receipt.get("published") or not _chapter_receipt_matches(p, receipt):
                return False
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
        if after_ok:
            _chapter_validation_cache[cache_key] = signature
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
            receipt = load_json(path.with_suffix(".commit.json"), None)
            if receipt is not None and not receipt.get("published"):
                continue
            seqs.append(int(m.group(1)))
    return max(seqs) if seqs else None


def max_available_delta_seq(project_dir: Path) -> int:
    seqs = []
    # 兼容扫描 Delta/（新）与 章节处理/ 根目录（旧备份未迁移），任一处有 delta 都算
    for d in (project_dir / "章节处理" / "Delta", project_dir / "章节处理"):
        if not d.is_dir():
            continue
        for path in d.glob("第*章_*.json"):
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
    rc = run_cmd([sys.executable, str(script_path("coerce_delta.py")), str(p["delta"]), "--current", str(story), "--report", str(coerce_delta_report)])
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


def build_lightweight_index(story_path: Path, max_names: int = 0) -> str:
    """全部元素名称索引。max_names=0 表示不限制（给全部名称）。"""
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
                names.append(f"{name}[{'/'.join(str(a) for a in aliases)}]")
            else:
                names.append(name)
        if max_names > 0:
            lines.append(f"{key}({len(names)}): " + "、".join(names[:max_names]))
            if len(names) > max_names:
                lines[-1] += f" ……另有{len(names)-max_names}个"
        else:
            lines.append(f"{key}({len(names)}): " + "、".join(names))
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
        status="committed" if audit_status(project_dir, start, end).get("cross_range_committed") else "covered_by_children",
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
        action="父周期由子区间审计及跨区间复核共同覆盖；恢复时先重放子补丁，再重放父复核补丁。",
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
            return {"name": p.name, "content": p.read_text(encoding="utf-8")}
    return None


def extract_relevant_elements_for_audit(story_data: dict, project_dir: Path, start: int, end: int) -> str:
    """Select current formal records using Delta names only as typed routing hints."""
    names_mentioned: Dict[str, set[str]] = {key: set() for key in COLLECTION_KEYS}
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
                            names_mentioned[c_key].add(item["名称"])

    if not any(names_mentioned.values()):
        return "无"

    relevant: Dict[str, List[dict]] = {key: [] for key in COLLECTION_KEYS}
    for key in COLLECTION_KEYS:
        for item in story_data.get(key, []):
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if name in names_mentioned[key]:
                relevant[key].append(item)
                continue
            aliases = item.get("别名", [])
            if isinstance(aliases, list) and any(a in names_mentioned[key] for a in aliases):
                relevant[key].append(item)

    relevant = {key: items for key, items in relevant.items() if items}
    if not relevant:
        return "无"
    return json.dumps(relevant, ensure_ascii=False, indent=2)


def extract_relevant_elements_for_material(story_data: dict, material: str) -> str:
    """Return current formal records whose name or alias occurs in task material.

    Historical Delta files are merge provenance, while ``story_data`` is the
    current single source of truth. One-character aliases are excluded from
    automatic matching because they create broad false positives in Chinese
    prose; their formal records remain visible in the complete name index.
    """
    selected: Dict[str, List[dict]] = {key: [] for key in COLLECTION_KEYS}
    for collection in COLLECTION_KEYS:
        for item in story_data.get(collection, []) or []:
            if not isinstance(item, dict):
                continue
            names = [item.get("名称"), *(item.get("别名", []) or [])]
            mentions = [name.strip() for name in names if isinstance(name, str) and len(name.strip()) >= 2]
            if any(name in material for name in mentions):
                selected[collection].append(item)
    selected = {key: items for key, items in selected.items() if items}
    return json.dumps(selected, ensure_ascii=False, indent=2) if selected else "无"


# 历史表示分界，单位 KB；统一 Token 容量可以提前选择索引档，原档案不删除。
DELTA_FULL_STORY_KB = 256


def build_delta_story_context(story: Path, project_dir: Path, chapter_seq: int,
                              full_story_kb: int = DELTA_FULL_STORY_KB, *, current_material: str = "") -> str:
    """Select the authoritative current story representation for one chapter.

    Small stories use the complete current JSON. Large stories use the complete
    formal name/alias index plus current records mentioned by this chapter. Raw
    historical Deltas are audit provenance and never re-enter generation context.
    """
    full_json = story.read_text(encoding="utf-8")
    policy = ContextBudget.from_env()
    if len(full_json.encode("utf-8")) < full_story_kb * 1024 and policy.fits(current_material + full_json):
        return full_json
    index = build_lightweight_index(story)
    details = extract_relevant_elements_for_material(load_json(story, {}) or {}, current_material)
    base = ("## 当前故事全部正式名称与别名索引\n" + index
            + "\n\n以上索引来自当前故事结构，是名称和分类的权威依据。")
    with_details = base + "\n\n## 当前章命中的正式档案\n" + details
    if details != "无" and policy.fits(current_material + with_details):
        return with_details
    return base + "\n\n当前章没有命中可在预算内提供的正式档案；不得从历史 Delta 猜测。"



def write_governance_route(project_dir, start, end, pack, extra_sources=()):
    """Prepare complete bounded evidence without inventing subchapter identities."""
    paths = audit_paths(project_dir, start, end)
    contract = render_prompt("流水线/周期审计.j2", start=start, end=end,
                             correction_path=paths["correction"], project_dir=project_dir,
                             run_pipeline_path=script_path("run_pipeline.py"), full_story_json="",
                             story_index="", relevant_details="", previous_correction=None,
                             chapters=[], taxonomy_json="")
    contract = re.sub(r"```bash.*?```", "程序在全部分片与独立复核通过后统一校验提交。", contract, flags=re.S)
    story = ensure_story(project_dir)
    history = pack.with_suffix(".history.md")
    details = extract_relevant_elements_for_audit(load_json(story, {}), project_dir, start, end)
    # Synopsis is needed even in the lightest historical tier; never truncate it.
    current_intro = json.dumps(load_json(story, {}).get("介绍", {}), ensure_ascii=False)
    atomic_write_bytes(history, ("## 当前故事简介（需核对的旧版，不是原文证据）\n" + current_intro + "\n"
                                + build_lightweight_index(story) + "\n## 本周期相关完整档案\n" + details).encode())
    sources = [(history, "全局索引及本周期相关完整档案")]
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if chapter is not None:
            sources.append((chapter, f"第{seq:03d}章完整原文；本周期事实依据"))
    taxonomy = Path(__file__).resolve().parent.parent / "schemas/narrative_taxonomy.json"
    sources += [(taxonomy, "完整标签规则；作为约束，不是故事事实")]
    sources += list(extra_sources)
    save_json(pack.with_suffix(".governance.json"), {
        "contract": contract, "range": f"第{start:03d}章-第{end:03d}章",
        "references": [[str(story.resolve()), "当前完整故事；已包含本周期 Delta，按需核对历史档案"]],
        "sources": [[str(path.resolve()), role] for path, role in dict.fromkeys(sources)],
    })
    return contract + "\n完整材料已登记为连续分片任务；程序负责覆盖校验、独立复核和严格提交。\n"


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
    capacity = ContextBudget.from_env()
    if paths["pack"].exists() and not force:
        existing_status = load_json(paths["status"], {}) if paths["status"].is_file() else {}
        existing_context = existing_status.get("context", {}) if isinstance(existing_status, dict) else {}
        if (
            isinstance(existing_context, dict)
            and existing_context.get("format_version") == AUDIT_CONTEXT_FORMAT_VERSION
            and existing_context.get("max_context_chars") == max_context_chars
            and existing_context.get("prompt_fingerprint") == prompt_fingerprint("流水线/周期审计.j2")
            and existing_context.get("capacity_policy") == capacity.__dict__
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
            "chapter_text": read_audit_evidence(p["chapter"]),
        })

    taxonomy_path = Path(__file__).resolve().parent.parent / "schemas" / "narrative_taxonomy.json"
    taxonomy_json = taxonomy_path.read_text(encoding="utf-8", errors="ignore") if taxonomy_path.is_file() else ""
    current_evidence_chars = sum(
        len(ch.get("chapter_text", ""))
        for ch in chapters
    )

    mode_contexts = {
        "full_story": (full_story_json, "", ""),
        "story_index": ("", story_index, ""),
        "story_index_relevant": ("", story_index, relevant_details),
    }
    # Index tiers otherwise carry only the title, making synopsis updates blind
    # to the existing description. Include it in the normal capacity accounting.
    intro_context = "\n## 当前故事简介（需核对的旧版，不是原文证据）\n" + json.dumps(
        story_data.get("介绍", {}), ensure_ascii=False)
    for mode in ("story_index", "story_index_relevant"):
        full, index, details = mode_contexts[mode]
        mode_contexts[mode] = (full, index + intro_context, details)
    fallback_modes = ["full_story", "story_index_relevant", "story_index"]
    attempts: List[Dict[str, Any]] = []
    selected_content = ""
    selected_mode = ""
    for mode in fallback_modes:
        candidate_full_story, candidate_index, candidate_details = mode_contexts[mode]
        candidate = render_prompt(
            "流水线/周期审计.j2",
            start=start,
            end=end,
            correction_path=paths["correction"],
            run_pipeline_path=script_path("run_pipeline.py"),
            project_dir=project_dir,
            full_story_json=candidate_full_story,
            story_index=candidate_index,
            relevant_details=candidate_details,
            previous_correction=None,
            chapters=chapters,
            taxonomy_json=taxonomy_json,
        )
        candidate += f"\n完整历史档案：{story}；索引只定位，身份、合并和冲突须读取对应完整档案核实。\n"
        attempts.append({"render_mode": mode, "rendered_chars": len(candidate),
                         "estimated_tokens": estimate_tokens(candidate)})
        if len(candidate) <= max_context_chars and capacity.fits(candidate):
            selected_content = candidate
            selected_mode = mode
            break

    context = {
        "format_version": AUDIT_CONTEXT_FORMAT_VERSION,
        "prompt_fingerprint": prompt_fingerprint("流水线/周期审计.j2"),
        "max_context_chars": max_context_chars,
        "current_evidence_chars": current_evidence_chars,
        "capacity_policy": capacity.__dict__,
        "attempts": attempts,
        "fact_sources": ["current_story", "chapter_originals"],
        "routing_sources": ["typed_delta_names"],
        "excluded_from_model_facts": ["chapter_analysis", "raw_delta", "previous_correction"],
    }
    if not selected_content and start == end:
        selected_content = write_governance_route(project_dir, start, end, paths["pack"])
        selected_mode = "tiered_evidence_tree"
    elif selected_content:
        paths["pack"].with_suffix(".governance.json").unlink(missing_ok=True)
    if not selected_content:
        context["render_mode"] = "blocked_context"
        save_json(paths["status"], {
            "status": "blocked_context",
            "range": f"{start:03d}-{end:03d}",
            "audit_pack": str(paths["pack"]),
            "correction_path": str(paths["correction"]),
            "context": context,
            "action": "历史各档仍超限，自动拆分周期证据；单章内可继续分片，不截断材料。",
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
            m = re.match(r"^(\d{3,})-(\d{3,})$", str(data.get("range", "")))
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
        m = re.match(r"^(\d{3,})-(\d{3,})$", str(data.get("range", "")))
        if not m:
            m = re.match(r"^audit_(\d{3,})-(\d{3,})\.status\.json$", path.name)
        if not m:
            continue
        start = int(m.group(1))
        end = int(m.group(2))
        if snapshot_seq <= end <= to_seq:
            data["_start"] = start
            data["_end"] = end
            data["_status_path"] = str(path)
            due.append(data)
    # Children with the same end chapter must replay before their parent reconciliation.
    return sorted(due, key=lambda item: (item["_end"], item["_end"] - item["_start"]))


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


def run_cross_range_governance(project_dir: Path, start: int, end: int,
                               child_ranges: List[Tuple[int, int]], max_context_chars: int,
                               command: str, timeout: float, retries: int, window: str) -> int:
    """Reconcile child decisions before declaring their parent cycle complete.

    Leaf audits have read complete chapter evidence. This pass checks cross-range
    identities/conflicts against the resulting story and preserves links to every
    source for factual verification; it never replays child operations blindly.
    """
    paths = audit_paths(project_dir, start, end)
    status = audit_status(project_dir, start, end)
    if status.get("cross_range_committed"):
        return 0
    story = ensure_story(project_dir)
    blocks = ["# 跨子区间治理复核", f"范围：第{start:03d}章-第{end:03d}章",
              "子区间分别通过不代表本周期整体通过。核查跨区间重复实体、别名、引用和矛盾。",
              "当前结构已经应用子补丁，禁止重复执行子操作；只输出新增发现的治理补丁。",
              "补丁沿用原治理契约，新增/修改须有详情.提取理由；无修改时须输出 no_change、完整证据范围和非空质量说明。",
              "完整证据保存在原文件，不得依据摘要补造事实；涉及合并或矛盾时须补读对应原文。",
              "只写目标补丁，程序负责验证与提交，禁止直接修改故事库。",
              f"目标：{paths['correction']}", f"完整故事结构：{story}",
              "## 全部正式名称与别名", build_lightweight_index(story)]
    def add_children(ranges):
        for first, last in ranges:
            child = audit_paths(project_dir, first, last)
            if child["correction"].is_file():
                blocks.extend([f"## 子区间结论 {first:03d}-{last:03d}",
                               child["correction"].read_text(encoding="utf-8")])
            else:
                nested = audit_child_ranges_from_status(audit_status(project_dir, first, last))
                if nested:
                    add_children(nested)
                else:
                    raise ValueError("子区间缺少真实治理产物，不能标记整体完成")
    add_children(child_ranges)
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if chapter:
            materials = artifact_paths(project_dir, chapter)
            blocks.append(f"第{seq:03d}章原文：{chapter}；分析：{materials['analysis']}；Delta：{materials['delta']}")
    content = "\n\n".join(blocks)
    paths["pack"].write_text(content, encoding="utf-8")
    # Cross-range review must actually process originals, not merely offer links.
    child_records = [(path, "已提交子区间补丁；只作复核，禁止再次应用")
                     for first, last in child_ranges
                     for path in [audit_paths(project_dir, first, last)["correction"]] if path.is_file()]
    route = write_governance_route(project_dir, start, end, paths["pack"], child_records)
    metadata = load_json(paths["pack"].with_suffix(".governance.json"), {})
    metadata["contract"] += "\n当前故事已应用全部子区间补丁，仅生成新增发现；禁止重复执行历史操作。"
    save_json(paths["pack"].with_suffix(".governance.json"), metadata)
    paths["pack"].write_text(route, encoding="utf-8")
    update_audit_status(paths, status="cross_range_pending", cross_range_required=True)
    if not command:
        return 2
    # A crash after model output reuses it; commit_governance's receipt makes
    # publication idempotent. No completion flag is written before validation.
    if not paths["correction"].is_file():
        rc = run_generation_worker(command, project_dir, "audit_correction", paths["pack"],
                                   paths["correction"], timeout=timeout, retries=retries, worker_window=window)
        if rc != 0:
            return rc
    rc = commit_governance(project_dir, str(paths["correction"]))
    if rc != 0:
        rc = run_repair_worker(command, project_dir, "audit_repair", paths["correction"].with_suffix(".repair.md"),
                               paths["correction"], timeout=timeout, retries=retries, worker_window=window)
    if rc == 0:
        update_audit_status(paths, cross_range_committed=True)
    return rc


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
        rc = run_cross_range_governance(
            project_dir, start, end, child_ranges, max_context_chars, audit_command,
            worker_timeout_seconds, worker_retries, worker_window)
        if rc == 0:
            mark_audit_covered_by_children(project_dir, start, end, child_ranges)
        return rc

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
        rc = run_cross_range_governance(
            project_dir, start, end, child_ranges, max_context_chars, audit_command,
            worker_timeout_seconds, worker_retries, worker_window)
        if rc == 0:
            mark_audit_covered_by_children(project_dir, start, end, child_ranges)
        return rc
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
    # 周期审计的职责是审计已提交章节的结构。零已提交章节（无 story_after_chNNN.json
    # 快照）时无内容可审，跑治理只会刷出空 no_change 快照；此时整体跳过，不建
    # pack、不调 commit、不标记 committed。这与 governance.md「禁止跳过真实审计」
    # 不冲突：那是禁止对有内容的审计偷懒，这里根本没有已提交章节可审。
    if latest_snapshot_seq(project_dir) is None:
        print(f"周期审计 {start:03d}-{end:03d} 跳过：尚无已提交章节（无 story_after_chNNN.json），无内容可审。")
        return 0
    paths = audit_paths(project_dir, start, end)
    existing_status = load_json(paths["status"], {}) if paths["status"].is_file() else {}
    existing_status = existing_status if isinstance(existing_status, dict) else {}
    if audit_range_is_resolved(project_dir, start, end):
        return 0  # Completed receipts are authoritative, including after restart.
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
        rc = run_generation_worker(
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
    print(f"周期审计 {start:03d}-{end:03d} 治理补丁未通过；准备自动修复与重新验收。")
    if not audit_command:
        return 2
    rc = run_repair_worker(
        audit_command,
        project_dir,
        "audit_repair",
        paths["correction"].with_suffix(".repair.md"),
        paths["correction"],
        timeout=worker_timeout_seconds,
        retries=worker_retries,
        worker_window=worker_window,
    )
    if rc != 0:
        update_audit_status(paths, status="worker_failed", action="审计修复 worker 失败；查看 worker 日志后重试。")
        return rc
    if worker_supervision_checkpoint(worker_loop, f"audit repair written for {start:03d}-{end:03d}", rerun_command):
        return 2
    return 0


def mark_periodic_audit_committed(project_dir: Path, patch: Path, before: Path, after: Path, diff: Path) -> None:
    resolved_patch = patch.resolve()
    audit_dir = (project_dir / "质量治理" / "周期审计").resolve()
    if resolved_patch.parent != audit_dir:
        return
    m = re.match(r"^correction_(\d{3,})-(\d{3,})\.json$", resolved_patch.name)
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


MAX_SINGLE_CHAPTER_SOURCE_BYTES = 256 * 1024
EMBEDDED_CHAPTER_HEADING_RE = re.compile(
    r"^\s*第[一二两三四五六七八九十百千万零〇\d]+[章节回卷集部篇]\s*.*$"
)


def validate_chapter_source(chapter: Path) -> List[str]:
    """Reject malformed split units before they consume a model call.

    A legitimate split file owns one Markdown H1 followed by its body. Embedded
    chapter headings indicate that the source splitter merged multiple chapters;
    an empty body usually comes from duplicate consecutive source headings.
    """
    text = chapter.read_text(encoding="utf-8", errors="ignore")
    errors: List[str] = []
    if len(text.encode("utf-8")) > MAX_SINGLE_CHAPTER_SOURCE_BYTES:
        errors.append(
            f"单章原文超过 {MAX_SINGLE_CHAPTER_SOURCE_BYTES // 1024}KB，疑似合并了多个章节"
        )
    lines = text.splitlines()
    body = "\n".join(lines[1:]).strip() if lines else ""
    if not body:
        errors.append("章节正文为空，疑似重复标题产生的空章节")
    embedded = [line.strip() for line in lines[1:] if EMBEDDED_CHAPTER_HEADING_RE.match(line)]
    if embedded:
        sample = "、".join(embedded[:3])
        errors.append(f"章节正文内发现 {len(embedded)} 个额外章节标题：{sample}")
    return errors


def preflight_chapter_source(project_dir: Path, chapter: Path) -> int:
    """Persist one actionable source report and stop before either model stage."""
    source_errors = validate_chapter_source(chapter)
    if not source_errors:
        return 0
    seq = seq_from_file(chapter)
    report = project_dir / "质量治理" / "章节校验" / f"source_input_ch{seq:03d}.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(f"- {error}" for error in source_errors) + "\n", encoding="utf-8")
    print(f"第{seq:03d}章原文拆分异常，已停止模型调用。见: {report}")
    return 1


def prepare_chapter_analysis(project_dir: Path, chapter: Path, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    if preflight_chapter_source(project_dir, chapter) != 0:
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
        "流水线/章节分析.j2",
        chapter_seq=chapter_seq,
        analysis_path=p["analysis"],
        chapter_text=chapter_text,
    )
    p["analysis_task"].parent.mkdir(parents=True, exist_ok=True)
    p["analysis_task"].write_text(content, encoding="utf-8")
    print(f"已生成章节分析任务包: {p['analysis_task']}")
    print(f"请填写章节分析MD: {p['analysis']}")
    return 0


def prepare_chapter_delta(project_dir: Path, chapter: Path, force: bool = False,
                          full_story_kb: int = DELTA_FULL_STORY_KB) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    if preflight_chapter_source(project_dir, chapter) != 0:
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
    contract_marker = f"<!-- authoritative-story-v4 delta-contract:{prompt_fingerprint('流水线/单章增量提取.j2')} -->"
    context_metadata = load_json(p["delta_task"].with_suffix(".context.json"), {}) or {}
    from fault_recovery import digest
    inputs = {key: digest(p[key]) for key in ("chapter", "analysis")}
    inputs["story"] = digest(story)
    if (p["delta_task"].exists() and not force
            and context_metadata.get("full_story_kb") == full_story_kb
            and context_metadata.get("capacity_policy") == ContextBudget.from_env().__dict__
            and context_metadata.get("inputs") == inputs
            and p["delta_task"].read_text(encoding="utf-8").startswith(contract_marker)):
        print(f"Delta提取任务包已存在: {p['delta_task']}")
        print(f"请只填写本章Delta JSON: {p['delta']}")
        return 0
    chapter_seq = seq_from_file(chapter)
    chapter_id = str(chapter_seq).zfill(4)
    chapter_time_str = chapter_time(chapter_seq)
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    analysis_text = p["analysis"].read_text(encoding="utf-8", errors="ignore")
    base_contract = render_prompt(
        "流水线/单章增量提取.j2", chapter_seq=chapter_seq, chapter_id=chapter_id,
        delta_path=p["delta"], summary="", analysis_text=analysis_text,
        chapter_text=chapter_text, chapter_time=chapter_time_str,
    )
    summary = build_delta_story_context(story, project_dir, chapter_seq, full_story_kb=full_story_kb,
                                        current_material=base_contract)
    history_path = p["delta_task"].with_suffix(".history.md")
    atomic_write_bytes(history_path, summary.encode())
    save_json(p["delta_task"].with_suffix(".context.json"), {
        "version": 4, "full_story_kb": full_story_kb,
        "history_mode": "story_index_relevant" if "## 当前故事全部正式名称与别名索引" in summary else "full_story",
        "inputs": inputs, "history_sha256": digest(history_path),
        "capacity_policy": ContextBudget.from_env().__dict__,
    })

    content = render_prompt(
        "流水线/单章增量提取.j2",
        chapter_seq=chapter_seq,
        chapter_id=chapter_id,
        delta_path=p["delta"],
        summary=summary,
        analysis_text=analysis_text,
        chapter_text=chapter_text,
        chapter_time=chapter_time_str,
    )

    p["delta_task"].parent.mkdir(parents=True, exist_ok=True)
    p["delta_task"].write_text(contract_marker + "\n" + content, encoding="utf-8")
    print(f"已生成Delta提取任务包: {p['delta_task']}")
    print(f"请填写本章Delta JSON: {p['delta']}")
    return 0


def prepare_chapter(project_dir: Path, seq: Optional[int] = None, force: bool = False,
                    full_story_kb: int = DELTA_FULL_STORY_KB) -> int:
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
        return prepare_chapter_delta(project_dir, chapter, force, full_story_kb=full_story_kb)
    print(f"第{seq_from_file(chapter):03d}章的章节分析MD和Delta JSON均已存在。")
    return 0


def write_repair_pack(project_dir: Path, chapter: Path, reason: str, report_paths: List[Path]) -> None:
    p = artifact_paths(project_dir, chapter)
    # Refresh cached contracts after an upgrade, keeping valid analysis intact.
    prepare_chapter_delta(project_dir, chapter)
    reports: List[Dict[str, str]] = []
    for rp in report_paths:
        if rp.is_file():
            reports.append({
                "name": rp.name,
                "excerpt": rp.read_text(encoding="utf-8", errors="ignore"),
            })
    # Repair workers need the original chapter material and Delta contract, not
    # only a terse validator report.  Without this context a worker can produce
    # a syntactically valid but meaningless fragment and repeat forever.
    base_task = (
        p["delta_task"].read_text(encoding="utf-8", errors="ignore")
        if p["delta_task"].is_file()
        else ""
    )
    content = render_prompt(
        "流水线/章节修复.j2",
        chapter_seq=seq_from_file(chapter),
        reason=reason,
        reports=reports,
        base_task=base_task,
        failed_delta=p["delta"].read_text(encoding="utf-8") if p["delta"].is_file() else "",
    )
    p["repair"].parent.mkdir(parents=True, exist_ok=True)
    p["repair"].write_text(content, encoding="utf-8")
    print(f"已生成修复任务包: {p['repair']}")


def write_analysis_regenerate_task(project_dir: Path, chapter: Path, errors: List[str]) -> None:
    """Repair analysis from complete source while preserving existing content."""
    p = artifact_paths(project_dir, chapter)
    content = render_prompt(
        "流水线/章节分析_重生成.j2",
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
    receipt_path = p["after"].with_suffix(".commit.json")
    receipt = load_json(receipt_path, {})
    if not force and receipt and _chapter_receipt_matches(p, receipt):
        if receipt.get("published") or receipt.get("story_sha256") == hashlib.sha256(story.read_bytes()).hexdigest():
            result = _record_chapter_progress(project_dir, chapter, seq)
            receipt["published"] = True
            save_json(receipt_path, receipt)
            return result
    if is_chapter_complete(project_dir, chapter) and not force:
        print(f"第{seq:03d}章已完成，跳过。使用 --force 可重新提交。")
        return 0
    if not p["analysis"].is_file():
        print(f"缺章节分析MD: {p['analysis']}")
        rc = prepare_chapter(project_dir, seq)
        if rc != 0:
            return rc
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

    # JSON arrays/strings are syntactically legal, so repair_llm_json cannot
    # reject them.  Delta is contractually an object; fail here with an explicit
    # report before generic coercion/compression obscures the real cause.
    delta_payload = load_json(p["delta"], None)
    if not isinstance(delta_payload, dict):
        save_json(
            p["delta_report"],
            {
                "passed": False,
                "mode": "process",
                "errors": [
                    f"Delta顶层必须是对象，当前为{type(delta_payload).__name__}"
                ],
                "warnings": [],
            },
        )
        print(f"Delta 顶层类型无效，见: {p['delta_report']}")
        write_repair_pack(project_dir, chapter, "Delta顶层必须是对象", [p["delta_report"]])
        return 1

    # 入库前先做机械纠错：把 LLM 写错的类型/格式（bool/int、数组↔标量、章节号宽度、
    # 顶层追溯字段位置等）确定性地修好。这一步不需要 LLM 介入，能把大半 repair 循环
    # 提前消解；正式索引唯一确定的类别与关联引用也在此完整迁移，避免模型反复修复。
    coerce_delta_report = project_dir / "质量治理" / "delta校验" / f"coerce_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("coerce_delta.py")), str(p["delta"]), "--current", str(story), "--report", str(coerce_delta_report)])
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

    # All transformations run against a private candidate. The live structure is
    # published once, after validation and diff succeed; no rollback window exists.
    # Invalidate old evidence before touching snapshots, including legacy --force
    # submissions whose historical snapshots predate publication receipts.
    save_json(receipt_path, {"published": False})
    shutil.copy2(story, p["before"])
    candidate = p["before"].with_name(f".chapter_candidate_{seq:03d}.json")
    shutil.copy2(story, candidate)
    merge_log = project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt"
    normalize_report = project_dir / "质量治理" / "规范化" / f"normalize_ch{seq:03d}.txt"
    compress_report = project_dir / "质量治理" / "规范化" / f"compress_after_ch{seq:03d}.json"
    try:
        steps = [
            ([sys.executable, str(script_path("merge_delta.py")), str(candidate), str(p["delta"])], merge_log, "章节合并失败"),
            ([sys.executable, str(script_path("normalize_story_schema.py")), str(candidate), "--in-place", "--report", str(normalize_report)], None, "章节结构规范化失败"),
            ([sys.executable, str(script_path("compress_tags.py")), "--structure", str(candidate), "--report", str(compress_report)], None, "章节标签处理失败"),
            ([sys.executable, str(script_path("validate_structure.py")), "--chapter-check", "--mode", "process", "--report-json", str(p["chapter_report"]), str(candidate), str(p["delta"])], None, "章节结构校验失败"),
            ([sys.executable, str(script_path("diff_structure.py")), str(p["before"]), str(candidate), str(p["diff"])], None, "章节变更记录生成失败"),
        ]
        for command, log, reason in steps:
            rc = run_cmd(command, log) if log is not None else run_cmd(command)
            if rc != 0:
                write_repair_pack(project_dir, chapter, reason, [merge_log, normalize_report, compress_report, p["chapter_report"]])
                return rc
        # A pending receipt prevents prepublication snapshots from looking complete.
        # Hashes also allow recovery after replace but before the progress update.
        receipt = {
            "published": False,
            "story_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "artifacts": {key: hashlib.sha256(p[key].read_bytes()).hexdigest()
                          for key in ("analysis", "delta", "before", "diff", "delta_report", "chapter_report")},
        }
        save_json(receipt_path, receipt)
        shutil.copy2(candidate, p["after"])
        os.replace(candidate, story)
        # Leave the receipt pending until progress is durable, so the next run
        # returns here for reconciliation rather than skipping an unfinished step.
        result = _record_chapter_progress(project_dir, chapter, seq)
        receipt["published"] = True
        save_json(receipt_path, receipt)
        return result
    finally:
        candidate.unlink(missing_ok=True)


def _record_chapter_progress(project_dir: Path, chapter: Path, seq: int) -> int:
    """Idempotently update progress after a validated chapter has been published."""
    # 更新进度
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
    concurrency: int = 1,
    cancel_event: Optional["threading.Event"] = None,
    include_original: str = "auto",
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    delta_full_story_kb: int = DELTA_FULL_STORY_KB,
    end_chapter_seq: int = 0,
) -> int:
    """Create and validate every chapter-analysis MD without touching Delta flow.

    concurrency>1 时并行生成各章分析MD（分析MD互相独立、不碰 故事结构_增量.json，可并行）；
    delta 阶段需合并同一 故事结构_增量.json 仍保持串行。
    """
    if cancel_event is not None:
        set_cancel_event(cancel_event)
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    files = [ch for ch in chapter_files(project_dir)
             if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    if concurrency <= 1:
        return _cmd_run_analysis_serial(
            project_dir, files, chapter_command, worker_timeout_seconds,
            worker_retries, worker_window, worker_loop, rerun_command,
        )
    return _cmd_run_analysis_parallel(
        project_dir, files, chapter_command, worker_timeout_seconds,
        worker_retries, worker_window, concurrency, cancel_event,
    )


def _cmd_run_analysis_serial(project_dir, files, chapter_command, worker_timeout_seconds,
                              worker_retries, worker_window, worker_loop, rerun_command) -> int:
    for chapter in files:
        p = artifact_paths(project_dir, chapter)
        if not p["analysis"].is_file():
            rc = prepare_chapter_analysis(project_dir, chapter)
            if rc != 0:
                return rc
            print("已交接：等待章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_generation_worker(
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
        analysis_ok, analysis_errors = validate_chapter_analysis_file(p["analysis"])
        if not analysis_ok:
            write_analysis_regenerate_task(project_dir, chapter, analysis_errors)
            print("已交接：等待从原文重新生成完整章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_repair_worker(
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


def _cmd_run_analysis_parallel(project_dir, files, chapter_command, worker_timeout_seconds,
                               worker_retries, worker_window, concurrency, cancel_event=None) -> int:
    from concurrent.futures import ThreadPoolExecutor
    recovery_observer = getattr(_cancel_local, "recovery_observer", None)

    def _do_one(item):
        set_recovery_observer(recovery_observer)
        if cancel_event is not None:
            set_cancel_event(cancel_event)  # 线程池线程需各自设线程本地取消事件
        _chapter, ttype, task_pack, expected, seq = item
        execute = run_repair_worker if ttype == "analysis_regenerate" else run_generation_worker
        return execute(
            chapter_command, project_dir, ttype, task_pack, expected, seq,
            timeout=worker_timeout_seconds, retries=worker_retries, worker_window=worker_window,
        )

    for _ in range(5):  # 最多 5 轮（含失败重生成）
        pending = []  # (chapter, task_type, task_pack, expected_output, seq)
        for chapter in files:
            p = artifact_paths(project_dir, chapter)
            if not p["analysis"].is_file():
                rc = prepare_chapter_analysis(project_dir, chapter)
                if rc != 0:
                    return rc
                pending.append((chapter, "analysis", p["analysis_task"], p["analysis"], seq_from_file(chapter)))
            else:
                ok, errs = validate_chapter_analysis_file(p["analysis"])
                if not ok:
                    write_analysis_regenerate_task(project_dir, chapter, errs)
                    pending.append((chapter, "analysis_regenerate", p["analysis_regenerate_task"], p["analysis"], seq_from_file(chapter)))
                else:
                    p["analysis_regenerate_task"].unlink(missing_ok=True)
        if not pending:
            print("所有章节分析MD均已通过校验。")
            return 0
        if not chapter_command:
            print("已交接：等待章节分析MD。")
            return 2
        print(f"analysis 并行: {len(pending)} 章, concurrency={concurrency}")
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            # Each child gets a separate context copy of the same immutable budget.
            futures = [ex.submit(copy_context().run, _do_one, item) for item in pending]
            rcs = [future.result() for future in futures]
        failed = [r for r in rcs if r != 0]
        if failed:
            return failed[0]
    print("analysis: 达到最大重试轮次仍有未通过章节。")
    return 1


def run_final_partial_governance(
    project_dir: Path,
    audit_interval: int,
    audit_max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    audit_command: str = "",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
    rerun_command: str = "",
    end_chapter_seq: int = 0,
) -> int:
    """全书提交后只补审不足一个周期的尾段，避免大书产生无界审计包。"""
    if audit_interval <= 0:
        return 0
    chapters = [ch for ch in chapter_files(project_dir)
                if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    if not chapters:
        return 0
    end = seq_from_file(chapters[-1])
    remainder = end % audit_interval
    if remainder == 0:
        return 0
    start = end - remainder + 1
    if audit_range_is_resolved(project_dir, start, end):
        return 0
    return run_periodic_governance(
        project_dir, start, end, audit_max_context_chars,
        audit_command=audit_command,
        worker_timeout_seconds=worker_timeout_seconds,
        worker_retries=worker_retries,
        worker_window=worker_window,
        worker_loop=worker_loop,
        rerun_command=rerun_command,
    )


def cmd_run_interleaved(
    project_dir: Path,
    run_mode: str = "auto",
    worker_provider: str = "manual",
    chapter_command: str = "",
    audit_command: str = "",
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_retries: int = DEFAULT_WORKER_RETRIES,
    worker_window: str = DEFAULT_WORKER_WINDOW,
    worker_loop: str = DEFAULT_WORKER_LOOP,
    concurrency: int = 2,
    audit_interval: int = 5,
    audit_max_context_chars: int = DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    emit: Optional[Callable] = None,
    cancel_event: Optional["threading.Event"] = None,
    include_original: str = "auto",
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    delta_full_story_kb: int = DELTA_FULL_STORY_KB,
    end_chapter_seq: int = 0,
) -> int:
    """交错流水线：N 个并行分析 worker 产出分析MD；1 个串行 delta worker 按章序消费。

    每章 delta 依赖前序章节已合并进 故事结构_增量.json 的结构，故 delta 必须按序串行；
    分析MD 互相独立可并行。分析时间被藏到 delta 串行时间之后。
    完成后所有章节分析+delta 均已提交；后续 chapter_delta 阶段为空跑。
    emit: 可选事件回调 emit(type, stage, detail, data)，逐章完成时发射进度事件。
    """
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    files = [ch for ch in chapter_files(project_dir)
             if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    mode = effective_run_mode(run_mode, chapter_command, audit_command, worker_provider)
    chapter_command, audit_command = resolve_worker_commands(
        chapter_command, audit_command, worker_provider, mode,
    )
    if not chapter_command:
        print("已交接：交错流水线需要 worker。")
        return 2

    seq_to_file = {seq_from_file(f): f for f in files}
    seqs = sorted(seq_to_file)
    total = len(seqs)
    ready: dict = {}
    cond = threading.Condition()
    err = {"rc": 0}
    next_pos = [0]

    def fail(rc: int) -> None:
        with cond:
            if not err["rc"]:
                err["rc"] = rc
            cond.notify_all()

    recovery_observer = getattr(_cancel_local, "recovery_observer", None)

    def producer() -> None:
        set_recovery_observer(recovery_observer)
        if cancel_event is not None:
            set_cancel_event(cancel_event)
        while True:
            with cond:
                if err["rc"]:
                    return
                if next_pos[0] >= total:
                    return
                pos = next_pos[0]
                next_pos[0] += 1
            seq = seqs[pos]
            chapter = seq_to_file[seq]
            p = artifact_paths(project_dir, chapter)
            try:
                if not p["analysis"].is_file():
                    rc = prepare_chapter_analysis(project_dir, chapter)
                    if rc != 0:
                        fail(rc); return
                    rc = run_generation_worker(chapter_command, project_dir, "analysis", p["analysis_task"],
                                    p["analysis"], seq, timeout=worker_timeout_seconds,
                                    retries=worker_retries, worker_window=worker_window)
                    if rc != 0:
                        fail(rc); return
                ok, errs = validate_chapter_analysis_file(p["analysis"])
                if not ok:
                    write_analysis_regenerate_task(project_dir, chapter, errs)
                    rc = run_repair_worker(chapter_command, project_dir, "analysis_regenerate",
                                    p["analysis_regenerate_task"], p["analysis"], seq,
                                    timeout=worker_timeout_seconds, retries=worker_retries,
                                    worker_window=worker_window)
                    if rc != 0:
                        fail(rc); return
                else:
                    p["analysis_regenerate_task"].unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                print(f"analysis producer error ch{seq:03d}: {exc}")
                fail(1); return
            with cond:
                ready[seq] = True
                cond.notify_all()
            if emit:
                emit("chapter_progress", stage="chapter_analysis",
                     detail=f"ch{seq:03d}/{total} 分析完成", data={"seq": seq, "total": total})

    def consumer() -> None:
        set_recovery_observer(recovery_observer)
        if cancel_event is not None:
            set_cancel_event(cancel_event)
        for seq in seqs:
            with cond:
                while not ready.get(seq) and not err["rc"]:
                    cond.wait()
                if err["rc"]:
                    return
            chapter = seq_to_file[seq]
            p = artifact_paths(project_dir, chapter)
            try:
                if is_chapter_complete(project_dir, chapter):
                    continue
                if not p["delta"].is_file():
                    rc = prepare_chapter(project_dir, seq, full_story_kb=delta_full_story_kb)
                    if rc != 0:
                        fail(rc); return
                    rc = run_generation_worker(chapter_command, project_dir, "delta", p["delta_task"],
                                    p["delta"], seq, timeout=worker_timeout_seconds,
                                    retries=worker_retries, worker_window=worker_window)
                    if rc != 0:
                        if p["repair"].is_file():
                            rc2 = run_repair_worker(chapter_command, project_dir, "repair_chapter",
                                             p["repair"], p["delta"], seq, timeout=worker_timeout_seconds,
                                             retries=worker_retries, worker_window=worker_window)
                            if rc2 != 0:
                                fail(rc2); return
                        else:
                            fail(rc); return
                rc = commit_chapter(project_dir, seq)
                if rc != 0:
                    if p["repair"].is_file():
                        rc2 = run_repair_worker(chapter_command, project_dir, "repair_chapter",
                                         p["repair"], p["delta"], seq, timeout=worker_timeout_seconds,
                                         retries=worker_retries, worker_window=worker_window)
                        if rc2 != 0:
                            fail(rc2); return
                        rc = commit_chapter(project_dir, seq)
                        if rc != 0:
                            fail(rc); return
                    else:
                        fail(rc); return
                if emit:
                    emit("chapter_progress", stage="chapter_delta",
                         detail=f"ch{seq:03d}/{total} delta完成", data={"seq": seq, "total": total})
                if audit_interval and seq % audit_interval == 0:
                    start = seq - audit_interval + 1
                    rc = run_periodic_governance(
                        project_dir, start, seq, audit_max_context_chars,
                        audit_command=audit_command, worker_timeout_seconds=worker_timeout_seconds,
                        worker_retries=worker_retries, worker_window=worker_window,
                        worker_loop="continuous", rerun_command="",
                    )
                    if rc != 0:
                        fail(rc); return
            except Exception as exc:  # noqa: BLE001
                print(f"delta consumer error ch{seq:03d}: {exc}")
                fail(1); return

    n_workers = min(max(1, concurrency), total)
    producers = [threading.Thread(target=copy_context().run, args=(producer,),
                                  name=f"analysis-{i}") for i in range(n_workers)]
    cons = threading.Thread(target=copy_context().run, args=(consumer,), name="delta")
    for t in producers:
        t.start()
    cons.start()
    for t in producers:
        t.join()
    cons.join()
    if err["rc"]:
        print(f"交错流水线失败 rc={err['rc']}")
        return err["rc"]
    # 满周期在逐章提交时审计；不足一周期的末尾章节在全书提交后补审。
    rc = run_final_partial_governance(
        project_dir, audit_interval, audit_max_context_chars,
        end_chapter_seq=end_chapter_seq,
        audit_command=audit_command,
        worker_timeout_seconds=worker_timeout_seconds,
        worker_retries=worker_retries,
        worker_window=worker_window,
        worker_loop="continuous",
    )
    if rc != 0:
        return rc
    print("交错流水线完成：所有章节分析+delta 提交完成。")
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
    cancel_event: Optional["threading.Event"] = None,
    delta_full_story_kb: int = DELTA_FULL_STORY_KB,
    end_chapter_seq: int = 0,
) -> int:
    """Require all chapter analyses before entering the existing serial Delta flow."""
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    files = [ch for ch in chapter_files(project_dir)
             if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    for chapter in files:
        p = artifact_paths(project_dir, chapter)
        if not p["analysis"].is_file():
            rc = prepare_chapter_analysis(project_dir, chapter)
            if rc != 0:
                return rc
            print("Delta阶段已交接：等待全部章节分析MD通过校验。")
            if not chapter_command:
                return 2
            rc = run_generation_worker(
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
            rc = run_repair_worker(
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
        end_chapter_seq=end_chapter_seq,
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
        delta_full_story_kb=delta_full_story_kb,
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
    concurrency: int = 1,
    cancel_event: Optional["threading.Event"] = None,
    include_original: str = "auto",
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    delta_full_story_kb: int = DELTA_FULL_STORY_KB,
    end_chapter_seq: int = 0,
) -> int:
    if cancel_event is not None:
        set_cancel_event(cancel_event)
    requested_chapter_command = chapter_command
    requested_audit_command = audit_command
    mode = effective_run_mode(run_mode, chapter_command, audit_command, worker_provider)
    rerun_command = build_run_foreground_command(
        project_dir,
        phase=phase,
        max_chapters=max_chapters,
        end_chapter_seq=end_chapter_seq,
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
            end_chapter_seq=end_chapter_seq,
            chapter_command=chapter_command,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_retries=worker_retries,
            worker_window=worker_window,
            worker_loop=worker_loop,
            rerun_command=rerun_command,
            concurrency=concurrency,
            cancel_event=cancel_event,
        )
    if phase == "delta":
        return cmd_run_delta(
            project_dir,
            end_chapter_seq=end_chapter_seq,
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
            cancel_event=cancel_event,
            delta_full_story_kb=delta_full_story_kb,
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
        if not nxt or (end_chapter_seq and seq_from_file(nxt) > end_chapter_seq):
            # 本次范围处理完毕后，只补审范围内不足一个周期的尾段。
            rc = run_final_partial_governance(
                project_dir, audit_interval, audit_max_context_chars,
                end_chapter_seq=end_chapter_seq,
                audit_command=audit_command,
                worker_timeout_seconds=worker_timeout_seconds,
                worker_retries=worker_retries,
                worker_window=worker_window,
                worker_loop=worker_loop,
                rerun_command=rerun_command,
            )
            if rc != 0:
                return rc
            print("本次范围内章节均已完成。")
            return 0
        seq = seq_from_file(nxt)
        p = artifact_paths(project_dir, nxt)
        if not p["analysis"].is_file():
            rc = prepare_chapter(project_dir, seq, full_story_kb=delta_full_story_kb)
            if rc != 0:
                return rc
            print("已交接：等待章节分析MD。")
            if not chapter_command:
                return 2
            rc = run_generation_worker(
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
            rc = prepare_chapter(project_dir, seq, full_story_kb=delta_full_story_kb)
            if rc != 0:
                return rc
            if p["analysis_regenerate_task"].is_file():
                print("已交接：等待从原文重新生成完整章节分析MD。")
                if not chapter_command:
                    return 2
                rc = run_repair_worker(
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
            rc = run_generation_worker(
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
                worker_rc = run_repair_worker(
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
                # Recovery has already validated and committed this chapter.
                # Keep normal bookkeeping so its periodic audit is not skipped.
            else:
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
        # :03d is a minimum width; large books continue beyond chapter 999.
        and re.match(r"^correction_\d{3,}-\d{3,}\.json$", patch.name) is not None
    )
    governance_dir = audit_dir if is_periodic_patch else project_dir / "质量治理" / "按需治理"
    governance_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    before = project_dir / "故事结构版本" / f"story_before_governance_{ts}.json"
    after = project_dir / "故事结构版本" / f"story_after_governance_{ts}.json"
    diff = project_dir / "结构变更日志" / f"diff_governance_{ts}.json"
    report_delta = governance_dir / f"validate_delta_governance_{ts}.txt"
    report_schema = governance_dir / f"validate_schema_governance_{ts}.txt"
    report_repair_json = governance_dir / f"repair_json_governance_{ts}.json"
    def failed(reason: str, report: Path) -> int:
        # Each repair sees this attempt's actual failure, never an old audit
        # task alone or a glob of unrelated historical validation reports.
        base_pack = governance_dir / f"audit_{patch.stem.removeprefix('correction_')}.md"
        # audit_paths is the authoritative naming convention for periodic packs.
        match = re.match(r"^correction_(\d+)-(\d+)\.json$", patch.name)
        if is_periodic_patch and match:
            base_pack = audit_paths(project_dir, int(match[1]), int(match[2]))["pack"]
        content = render_prompt(
            "流水线/治理修复.j2", reason=reason,
            report=report.read_text(encoding="utf-8", errors="replace") if report.is_file() else reason,
            failed_patch=patch.read_text(encoding="utf-8", errors="replace"),
            base_task=base_pack.read_text(encoding="utf-8") if base_pack.is_file() else "",
            output_path=patch,
        )
        patch.with_suffix(".repair.md").write_text(content, encoding="utf-8")
        return 1

    rc = repair_llm_json(patch, report_repair_json, backup=True)
    if rc != 0:
        print(f"治理补丁 LLM JSON 语法修复失败: {report_repair_json}")
        return failed("治理补丁 JSON 语法修复失败", report_repair_json)
    patch_doc = load_json(patch)
    range_match = re.fullmatch(r"correction_(\d+)-(\d+)\.json", patch.name) if is_periodic_patch else None
    known_range = f"第{range_match[1]}章-第{range_match[2]}章" if range_match else ""
    normalized, changes = normalize_governance_patch(patch_doc, known_range)
    if changes:
        # Keep the complete original before any reversible shape correction.
        shutil.copy2(patch, governance_dir / f"original_contract_{ts}.json")
        save_atomic_json(patch, normalized)
    save_json(governance_dir / f"normalize_contract_{ts}.json", {"changes": changes})
    receipt_path = patch.with_suffix(".commit.json")
    receipt = load_json(receipt_path, {})
    patch_digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    # Recover a crash after publishing the story but before recording success.
    # Both content hashes must match; stale receipts cannot skip new work.
    if (isinstance(receipt, dict) and receipt.get("patch_sha256") == patch_digest
            and receipt.get("story_sha256") == hashlib.sha256(story.read_bytes()).hexdigest()):
        recovered = [confined_project_path(project_dir, receipt[key]) for key in ("before", "after", "diff")]
        if all(path.is_file() for path in recovered):
            mark_periodic_audit_committed(project_dir, patch, *recovered)
            print("治理内容已完整落盘，已恢复提交状态，无需重复合并。")
            return 0
    report_compress = governance_dir / f"compress_governance_{ts}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(patch), "--report", str(report_compress)])
    if rc != 0:
        print(f"治理补丁标签压缩失败: {report_compress}")
        return failed("治理补丁标签处理失败", report_compress)
    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "governance", str(story), str(patch)], report_delta)
    if rc != 0:
        print(f"治理补丁Delta校验失败: {report_delta}")
        return failed("治理补丁入库前校验失败", report_delta)
    if is_periodic_patch:
        intro_errors = periodic_introduction_errors(load_json(story), load_json(patch))
        if intro_errors:
            report_delta.write_text("\n".join(intro_errors), encoding="utf-8")
            return failed("周期治理故事简介缺失或更新不合法", report_delta)
    # validate_delta is read-only — only persist the before snapshot once it
    # has passed, so a validation-only failure leaves no snapshot artifacts.
    shutil.copy2(story, before)
    candidate = before.with_name(f".governance_candidate_{ts}.json")
    shutil.copy2(story, candidate)
    try:
        merge_command = [sys.executable, str(script_path("merge_delta.py")), str(candidate), str(patch)]
        if is_periodic_patch:
            merge_command.append("--replace-description")
        rc = run_cmd(merge_command, governance_dir / f"merge_governance_{ts}.txt")
        if rc != 0:
            return failed("治理新增/修改合并失败，原结构未修改", governance_dir / f"merge_governance_{ts}.txt")
        rc = run_cmd([sys.executable, str(script_path("apply_governance_ops.py")), str(candidate), str(patch)], governance_dir / f"apply_ops_{ts}.txt")
        if rc != 0:
            return failed("治理操作失败，原结构未修改", governance_dir / f"apply_ops_{ts}.txt")
        normalize_cmd = [sys.executable, str(script_path("normalize_story_schema.py")), str(candidate), "--in-place"]
        if is_periodic_patch:
            normalize_cmd.append("--quarantine-invalid-refs")
        normalize_cmd.extend(["--report", str(project_dir / "质量治理" / "规范化" / f"normalize_governance_{ts}.txt")])
        rc = run_cmd(normalize_cmd)
        if rc != 0:
            return failed("治理结构规范化失败，原结构未修改", project_dir / "质量治理" / "规范化" / f"normalize_governance_{ts}.txt")
        rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--mode", "governance", str(candidate)], report_schema)
        if rc != 0:
            return failed("治理后结构校验失败，原结构未修改", report_schema)
        shutil.copy2(candidate, after)
        rc = run_cmd([sys.executable, str(script_path("diff_structure.py")), str(before), str(after), str(diff)])
        if rc != 0:
            return failed("治理变更记录生成失败，原结构未修改", diff)
        save_json(receipt_path, {
            "patch_sha256": hashlib.sha256(json.dumps(load_json(patch), ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "story_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "before": str(before.resolve()), "after": str(after.resolve()), "diff": str(diff.resolve()),
        })
        os.replace(candidate, story)  # Publish only the fully validated result.
        mark_periodic_audit_committed(project_dir, patch, before, after, diff)
        print(f"治理补丁提交完成。before={before} after={after} diff={diff}")
        return 0

    finally:
        candidate.unlink(missing_ok=True)


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

    report_excerpt = report.read_text(encoding="utf-8") if report.is_file() else ""
    # The draft is the only writable candidate and already starts as a copy of
    # the current story. Do not require the worker to reread the duplicate process
    # JSON; reports and historical corrections remain optional lookup evidence.
    source_files = [draft]

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
        path = confined_project_path(project_dir, rel)
        if path.is_file() and path.stat().st_size > 0:
            analysis_refs.append({
                "title": title,
                "path": path,
            })

    audit_refs: List[Dict[str, Any]] = []
    pa_dir = project_dir / "质量治理" / "周期审计"
    if pa_dir.is_dir():
        # Audit task packs repeat raw material; the actual accepted corrections
        # and original sources are authoritative and are included in full once.
        for cj in sorted(pa_dir.glob("correction_*.json")):
            audit_refs.append({"name": cj.name, "path": cj})
    # Final governance audits the candidate, reports and accepted corrections.
    # Chapter originals remain complete lookup evidence, not another book rerun.
    reference_files = [path.resolve() for folder in ("原文拆解", "章节处理", "章节处理/Delta")
                       for path in sorted((project_dir / folder).glob("第*章*")) if path.is_file()]
    reference_files.extend(ref["path"].resolve() for ref in analysis_refs)
    reference_files.extend(ref["path"].resolve() for ref in audit_refs)
    reference_files.append(story.resolve())
    source_files = list(dict.fromkeys(path.resolve() for path in source_files if path.is_file()))
    source_manifest = task_pack.with_suffix(task_pack.suffix + ".sources.json")
    save_json(source_manifest, [str(path) for path in source_files])
    reference_manifest = task_pack.with_suffix(".references.json")
    save_json(reference_manifest, [str(path) for path in dict.fromkeys(reference_files) if path.is_file()])

    content = render_prompt(
        "流水线/最终整理.j2",
        run_pipeline_path=script_path("run_pipeline.py"),
        project_dir=project_dir,
        report_excerpt=report_excerpt,
        story_index=build_lightweight_index(draft),
        analysis_refs=analysis_refs,
        audit_refs=audit_refs,
    )

    content = (f"本次治理必读材料：{source_manifest}。\n按需核对原文、章节分析、Delta、全书报告与历史治理记录：{reference_manifest}。"
               "档案保留全文；身份合并、事实矛盾和证据不足时读取对应原件，不得据索引猜测。\n\n" + content)
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
            "流水线/终稿修复.j2",
            report_excerpt=report.read_text(encoding="utf-8"),
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
    cmd = [sys.executable, str(script_path("analysis_context_pack.py")), str(project_dir), "--task", args.task, "--chapters", args.chapters, "--include-original", args.include_original, "--max-pack-chars", str(args.max_pack_chars), "--max-original-chars", str(args.max_original_chars), "--max-analysis-chars", str(args.max_analysis_chars), "--story-summary-max-items", str(getattr(args, "story_summary_max_items", 0)), "--context-budget-chars", str(getattr(args, "context_budget_chars", 160000))]
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


# 视觉资产分章五件套文件名主干（与 analysis_context_pack.VISUAL_ASSETS_PER_CHAPTER_FILES 同步）
VISUAL_PER_CHAPTER_STEMS = ["视觉资产清单", "关键场景分镜表", "AI绘图提示词素材", "角色外观一致性表", "场景氛围表"]


def visual_assets_per_chapter_relpaths(seq: int, name: str = "") -> List[str]:
    """与 analysis_context_pack.visual_assets_per_chapter_outputs 保持同步的相对路径（扁平：ch{seq:03d}_{标题}_{维度}.md）。"""
    prefix = f"全书分析/视觉资产/分章/ch{seq:03d}_{name}" if name else f"全书分析/视觉资产/分章/ch{seq:03d}"
    return [f"{prefix}_{stem}.md" for stem in VISUAL_PER_CHAPTER_STEMS]


def chapter_has_per_chapter_visual(project_dir: Path, seq: int) -> bool:
    """单章视觉资产五件套是否齐全且非空（按 seq 前缀 glob，标题无关，抗章节重命名）。"""
    base = project_dir / "全书分析" / "视觉资产" / "分章"
    if not base.is_dir():
        return False
    found = set()
    for p in base.glob(f"ch{seq:03d}_*.md"):
        if p.is_file() and p.stat().st_size > 0:
            found.add(p.name.rsplit("_", 1)[-1][:-3])
    return all(stem in found for stem in VISUAL_PER_CHAPTER_STEMS)


def chapters_eligible_for_visual(project_dir: Path) -> List[int]:
    """已经有章节分析MD的章节才能跑视觉资产。"""
    result: List[int] = []
    for ch in chapter_files(project_dir):
        seq = seq_from_file(ch)
        analysis = project_dir / "章节处理" / ch.name
        if analysis.is_file() and analysis.stat().st_size > 0:
            result.append(seq)
    return result


def chapter_structure_per_chapter_relpaths(seq: int, name: str = "") -> List[str]:
    """与 analysis_context_pack.chapter_structure_per_chapter_outputs 保持同步的相对路径（扁平）。"""
    if name:
        return [f"全书分析/故事结构/分章/ch{seq:03d}_{name}_章节结构.md"]
    return [f"全书分析/故事结构/分章/ch{seq:03d}_章节结构.md"]


def per_chapter_analysis_relpaths(task: str, seq: int, name: str = "") -> List[str]:
    if task == "visual_assets":
        return visual_assets_per_chapter_relpaths(seq, name)
    if task == "chapter_structure":
        return chapter_structure_per_chapter_relpaths(seq, name)
    raise ValueError(f"unsupported per-chapter analysis task: {task}")


def chapter_has_per_chapter_analysis(project_dir: Path, task: str, seq: int) -> bool:
    """单章分章产物是否齐全且非空（按 seq 前缀 glob，标题无关，抗章节重命名）。"""
    if task == "visual_assets":
        return chapter_has_per_chapter_visual(project_dir, seq)
    if task == "chapter_structure":
        base = project_dir / "全书分析" / "故事结构" / "分章"
        if not base.is_dir():
            return False
        for p in base.glob(f"ch{seq:03d}_*_章节结构.md"):
            if p.is_file() and p.stat().st_size > 0:
                return True
        return False
    raise ValueError(f"unsupported per-chapter analysis task: {task}")


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
        path = confined_project_path(project_dir, rel)
        if path.is_file():
            parts.append(f"# {key}: {rel}\n\n{path.read_text(encoding='utf-8', errors='ignore')}")
    if not parts:
        raise ValueError(f"manifest record has no readable task material: {manifest_path}")
    seq = record.get("seq")
    name = f"worker_pack_ch{int(seq):03d}.md" if isinstance(seq, int) else "worker_pack.md"
    out = manifest_path.parent / name
    out.write_text("\n\n".join(parts), encoding="utf-8")
    # Per-chapter structure/visual tasks also need a capacity fallback.
    from analysis_context_pack import output_contract
    manifest = load_json(manifest_path, {}) or {}
    save_json(out.with_suffix(".hierarchy.json"), {
        "sources": [str(confined_project_path(project_dir, record[key]).resolve())
                    for key in ("pack", "inventory") if record.get(key)],
        "require_review": manifest.get("mode") == "aggregate",
        "contract": output_contract(manifest.get("task", "custom")) + fullbook_scope_contract(manifest)
                    + (f"\n仅处理第{seq}章，不得跨章生成结论。" if isinstance(seq, int) else ""),
    })
    return out


def fullbook_scope_contract(manifest: dict) -> str:
    """Repeat user scope at every hierarchy level, not just the first leaf."""
    return ("\n用户指定问题：" + str(manifest.get("question") or "无")
            + "\n章节范围：" + str(manifest.get("chapter_range") or "以材料范围为准")
            + "\n指定目标：" + json.dumps(manifest.get("targets") or [], ensure_ascii=False)
            + "\n所有结论须保留证据来源；冲突标为待确认，不凭空新增。")


def write_fullbook_partial_worker_pack(
    project_dir: Path,
    manifest_path: Path,
    pack_rel: str,
    index: int,
    total: int,
    partial_path: Path,
) -> Path:
    """包装全书 analysis 的单个分包为 partial 执行的 worker pack。

    全书 analysis 多包模式下，每个 pack_NNN.md 的“目标输出文件”段列的是最终
    target_outputs（如 章节梗概汇总.md），但本阶段只要求 worker 产出本包的分批
    结果 partial_result_NNN.md，最终汇总由 reduce 阶段完成。若不覆盖，worker 会在
    “task pack 是权威”和“ND_EXPECTED_OUTPUT 指向 partial”之间产生歧义，可能直接
    写最终目标文件而漏掉其余分包内容。本函数在原 pack 前加一段覆盖说明，明确告诉
    worker 只写 partial_path。
    """
    pack_path = confined_project_path(project_dir, pack_rel)
    if not pack_path.is_file():
        raise FileNotFoundError(f"analysis pack not found: {pack_path}")
    header = (
        f"# 分批分析任务（第 {index}/{total} 包）\n\n"
        f"本任务包是全书分析的**第 {index}/{total} 包**，只覆盖部分章节。"
        f"请只产出**本包的分批结果**，写入：\n\n{partial_path}\n\n"
        f"**不要**写最终目标文件（任务包内“目标输出文件”段列出的路径），"
        f"那是 reduce 阶段的工作。reduce 会合并所有分包结果后再写最终目标文件。\n\n"
        f"## 原任务包内容\n\n"
    )
    content = pack_path.read_text(encoding="utf-8", errors="ignore")
    out = manifest_path.parent / f"worker_pack_{index:03d}.md"
    out.write_text(header + content, encoding="utf-8")
    manifest = load_json(manifest_path, {}) or {}
    from analysis_context_pack import output_contract
    save_json(out.with_suffix(".hierarchy.json"), {
        "sources": [str(pack_path.resolve())],
        "contract": output_contract(manifest.get("task", "custom"))
                    + fullbook_scope_contract(manifest)
                    + "\n仅产出本分包覆盖范围的完整分批分析结果，不写最终报告。",
    })
    return out


def write_fullbook_reduce_worker_pack(
    project_dir: Path,
    manifest_path: Path,
    partial_paths: List[Path],
    target_outputs: List[Path],
) -> Path:
    """拼接 reduce_prompt + 所有 partial_result 为 reduce 阶段的 worker pack。

    reduce 阶段把各分包的分批结果合并为最终报告，写入 target_outputs。worker pack
    开头列出所有目标文件，随后附 reduce_prompt.md 契约和各 partial_result 内容。
    """
    manifest = load_json(manifest_path, {}) or {}
    reduce_rel = manifest.get("reduce_prompt")
    target_list = "\n".join(f"- {p}" for p in target_outputs)
    parts: List[str] = [
        "# Reduce 汇总任务\n\n",
        "下面是 reduce_prompt 契约和各分包的分批结果。请合并为**最终报告**，"
        f"分别写入以下目标文件：\n\n{target_list}\n\n",
        "某项无内容时写“本书无对应内容”而非省略文件；不要凭空新增分批结果里"
        "没有依据的内容；冲突结论标“待确认”。\n\n",
    ]
    if reduce_rel:
        rp = confined_project_path(project_dir, reduce_rel)
        if rp.is_file():
            parts.append(f"## reduce_prompt\n\n{rp.read_text(encoding='utf-8', errors='ignore')}\n\n")
    for i, p in enumerate(partial_paths, 1):
        parts.append(f"## 分批结果 {i:03d}\n\n{p.read_text(encoding='utf-8', errors='ignore')}\n\n")
    out = manifest_path.parent / "worker_pack_reduce.md"
    out.write_text("".join(parts), encoding="utf-8")
    from analysis_context_pack import output_contract, reduce_contract
    task = manifest.get("task", "custom")
    # Store the contract separately from evidence: the tree repeats the complete
    # contract at every node without repeating every partial in one root request.
    contract = output_contract(task) + "\n" + reduce_contract(task) + fullbook_scope_contract(manifest)
    if reduce_rel and (project_dir / reduce_rel).is_file():
        contract += "\n" + (project_dir / reduce_rel).read_text(encoding="utf-8")
    save_json(out.with_suffix(".hierarchy.json"), {
        "sources": [str(path.resolve()) for path in partial_paths], "contract": contract,
        "require_review": True,
        "evidence": [str(confined_project_path(project_dir, path).resolve())
                     for path in manifest.get("packs", []) if isinstance(path, str)],
    })
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
        end_seq = getattr(args, "end_chapter_seq", 0)
        if end_seq:
            cmd.extend(["--chapters", f"1-{end_seq}"])
    else:
        cmd.extend([
            "--chapters", str(seq),
            "--per-chapter",
            "--include-original", args.include_original,
            "--max-pack-chars", str(args.max_pack_chars),
            "--max-original-chars", str(args.max_original_chars),
            "--max-analysis-chars", str(args.max_analysis_chars),
            "--story-summary-max-items", str(getattr(args, "story_summary_max_items", 0)),
            "--context-budget-chars", str(getattr(args, "context_budget_chars", 160000)),
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


def _cleanup_stale_per_chapter_packs(project_dir: Path, task: str) -> None:
    """清理该 task 未用过（无对应产物）的 per-chapter 任务包目录，保留已用过的供追溯。

    pack 目录是 worker 的临时输入（产物写在 全书分析/<维度>/分章/ 下，不在这里）。
    - 未用过：本目录所有 pack_chNNN 都没产出对应产物（中断/失败留下的）-> 删除，避免堆积。
    - 已用过：至少一个 pack_chNNN 已产出产物 -> 保留，便于追溯"这份产物由哪个任务包生成"。
    这样既防止失败重跑导致的目录无限堆积，又保留成功产物的任务包供审计。
    """
    root = task_package_root(project_dir)
    if not root.is_dir():
        return
    suffix = f"{task}_per_chapter"
    removed = 0
    kept = 0
    for path in root.glob(f"*_{suffix}"):
        if not path.is_dir():
            continue
        # 目录里任一 pack_chNNN 的产物已存在即视为"用过"，保留
        used = False
        for pack in path.glob("pack_ch*.md"):
            m = re.match(r"pack_ch(\d+)\.md", pack.name)
            if m and chapter_has_per_chapter_analysis(project_dir, task, int(m.group(1))):
                used = True
                break
        if used:
            kept += 1
        else:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed or kept:
        print(f"任务包目录：清理 {removed} 个未用过的，保留 {kept} 个已产出产物的（可追溯）。")


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
    end_seq = getattr(args, "end_chapter_seq", 0)
    eligible = [seq for seq in chapters_eligible_for_visual(project_dir)
                if not end_seq or seq <= end_seq]
    if not eligible:
        print("尚无任何章节具备章节分析MD；请先完成章节分析后再运行分章分析自动流程。")
        return 1

    suffix = f"{task}_per_chapter"
    concurrency = max(1, getattr(args, "per_chapter_concurrency", 1))
    cancel_event = getattr(args, "cancel_event", None)
    # supervised 模式为主控 agent 逐章交接，不并行；无 worker 命令时走 handoff 串行。
    use_parallel = (
        concurrency > 1
        and bool(worker_command)
        and getattr(args, "worker_loop", DEFAULT_WORKER_LOOP) != "supervised"
    )
    # 续跑前清理该 task 的旧 per-chapter 任务包目录，避免中断再继续时 pack 目录无限堆积。
    _cleanup_stale_per_chapter_packs(project_dir, task)
    while True:
        pending = [seq for seq in eligible if not chapter_has_per_chapter_analysis(project_dir, task, seq)]
        if not pending:
            break

        if use_parallel:
            # 并行：先串行生成所有 pending 章的任务包（analysis_context_pack 每次新建带时间戳目录，
            # 串行生成可规避 latest_analysis_manifest 的 glob-竞态），再并行跑 worker。
            # worker 调用线程安全（独立子进程），各章产物按 ch{seq:03d} 隔离，日志带 seq 防覆盖。
            jobs = []  # (seq, task_pack, expected_outputs)
            for next_seq in pending:
                rc = run_analysis_context_pack_for_auto(args, task, seq=next_seq)
                if rc != 0:
                    return rc
                manifest_path, record = load_latest_pack_record(project_dir, suffix, next_seq)
                expected_outputs = [project_dir / rel for rel in record.get("outputs", [])]
                task_pack = write_analysis_worker_pack(project_dir, manifest_path, record)
                jobs.append((next_seq, task_pack, expected_outputs))
            print(f"{task} 并行: {len(jobs)} 章, concurrency={concurrency}")
            from concurrent.futures import ThreadPoolExecutor
            wk_timeout = getattr(args, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)
            wk_retries = getattr(args, "worker_retries", DEFAULT_WORKER_RETRIES)
            wk_window = getattr(args, "worker_window", DEFAULT_WORKER_WINDOW)

            def _do_one(job):
                if cancel_event is not None:
                    set_cancel_event(cancel_event)  # 线程池线程需各自设线程本地取消事件
                seq_j, task_pack_j, expected_j = job
                return run_multi_output_worker(
                    worker_command, project_dir, task, task_pack_j, expected_j, seq_j,
                    timeout=wk_timeout, retries=wk_retries, worker_window=wk_window,
                )
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = [ex.submit(copy_context().run, _do_one, job) for job in jobs]
                rcs = [future.result() for future in futures]
            failed = [r for r in rcs if r != 0]
            if failed:
                return failed[0]
            # continuous 模式下 worker_supervision_checkpoint 为 no-op，无 rc=2 handoff；
            # 本轮全成功后重新检查 pending，若某章产物仍缺失（worker 未写出）下轮重试。
        else:
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
        # 仅复用同一章节范围且晚于任务包生成的汇总；范围变化须重新汇总。
        manifest_path = latest_analysis_manifest(project_dir, "visual_assets_aggregate")
        desired_range = f"1-{end_seq}" if end_seq else "all"
        if manifest_path is None:
            reusable = not end_seq  # 兼容旧版全书产物未附带 manifest 的项目
        else:
            manifest = load_json(manifest_path, {}) or {}
            reusable = (
                isinstance(manifest, dict)
                and manifest.get("chapter_range", "all") == desired_range
                and aggregate_marker.stat().st_mtime_ns >= manifest_path.stat().st_mtime_ns
            )
        if reusable:
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
            "--story-summary-max-items", str(getattr(args, "story_summary_max_items", 0)),
            "--context-budget-chars", str(getattr(args, "context_budget_chars", 160000)),
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


# === Scoped engine entry points for workbench task adapters ====================
#
# 这些函数把 analysis_context_pack / prepare_chapter_* / commit_chapter 等可重用
# 入口包装成"按章节范围过滤、产出 manifest 信息"的窄接口，供 backend.task_adapters
# 调用。它们不复制 prompt / schema / merge / validation 逻辑，只调用既有函数并加上
# sequence 过滤。CLI 行为不变。

def _copy_stable_inputs(src_project: Path, dst_project: Path) -> None:
    """把项目稳定输入（章节原文/分析/故事结构/已有全书分析产物）复制到 dst_project。

    读类型 workbench 任务（analysis / chapter_visual / chapter_structure / export）
    需要在 prepare 阶段（持有 reader admission，writers 被排斥）固化一份输入快照，
    再在 run 阶段（锁外）让 engine 函数基于快照运行，从而避免与并发 writer 互相竞态。
    """
    src_project = Path(src_project)
    dst_project = Path(dst_project)
    dst_project.mkdir(parents=True, exist_ok=True)

    src_split = src_project / "原文拆解"
    if src_split.is_dir():
        dst_split = dst_project / "原文拆解"
        dst_split.mkdir(parents=True, exist_ok=True)
        index_src = src_split / "_索引.json"
        if index_src.is_file():
            shutil.copy2(index_src, dst_split / "_索引.json")
        for p in src_split.glob("第*章_*.md"):
            if p.is_file():
                shutil.copy2(p, dst_split / p.name)

    src_chap = src_project / "章节处理"
    if src_chap.is_dir():
        dst_chap = dst_project / "章节处理"
        dst_chap.mkdir(parents=True, exist_ok=True)
        for p in src_chap.iterdir():
            if p.is_file() and p.suffix in (".md", ".json"):
                shutil.copy2(p, dst_chap / p.name)
        # Delta JSON 现位于 Delta/ 子目录，单独复制
        src_delta = src_chap / "Delta"
        if src_delta.is_dir():
            dst_delta = dst_chap / "Delta"
            dst_delta.mkdir(parents=True, exist_ok=True)
            for p in src_delta.iterdir():
                if p.is_file() and p.suffix == ".json":
                    shutil.copy2(p, dst_delta / p.name)

    for name in ("故事结构_增量.json", "故事结构.json"):
        src = src_project / name
        if src.is_file():
            shutil.copy2(src, dst_project / name)

    # 全书分析下既有 .md 产物（narrative_structure 背景、visual aggregate 源），
    # 但排除 _任务包/ —— 那是输出，由 scoped 函数本次生成，不是输入。
    src_analysis = src_project / "全书分析"
    if src_analysis.is_dir():
        for p in src_analysis.rglob("*.md"):
            if "_任务包" in p.parts:
                continue
            rel = p.relative_to(src_analysis)
            target = dst_project / "全书分析" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def snapshot_project_inputs(project_dir: Path) -> Path:
    """生成项目稳定输入的临时快照目录，供读类型 adapter 在 prepare 阶段固化输入。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="nd_snapshot_"))
    _copy_stable_inputs(Path(project_dir), tmp)
    return tmp


def copy_analysis_task_packages(src_project: Path, dst_project: Path) -> None:
    """把 src_project 的 全书分析/_任务包/ 子树合并回 dst_project。

    adapter 在快照上跑完 analysis_context_pack 后，task pack（manifest + pack_*.md）
    只产生在快照目录里；此函数把该子树复制回真实项目目录，使 canonical 产物落到
    正确位置。reader 任务只写 Markdown 任务包，不写 project-fact JSON，与并发 writer
    不冲突。
    """
    src = Path(src_project) / "全书分析" / "_任务包"
    if not src.is_dir():
        return
    dst = Path(dst_project) / "全书分析" / "_任务包"
    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for name in files:
            src_file = Path(root) / name
            target = dst / rel_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)


def _analysis_pack_argv(project_dir, *, task, chapters, targets, question,
                        include_original, max_pack_chars, max_original_chars,
                        max_analysis_chars, story_summary_max_items,
                        context_budget_chars, out_dir) -> List[str]:
    argv = [
        str(project_dir),
        "--task", task,
        "--chapters", str(chapters),
        "--include-original", include_original,
        "--max-pack-chars", str(max_pack_chars),
        "--max-original-chars", str(max_original_chars),
        "--max-analysis-chars", str(max_analysis_chars),
        "--story-summary-max-items", str(story_summary_max_items),
        "--context-budget-chars", str(context_budget_chars),
    ]
    if targets:
        argv.extend(["--targets", *targets])
    if question:
        argv.extend(["--question", question])
    if out_dir:
        argv.extend(["--out-dir", out_dir])
    return argv


def scoped_analysis_pack(
    project_dir,
    *,
    task,
    chapters="all",
    targets=None,
    question="",
    include_original="sample",
    max_pack_chars: int = 70000,
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    out_dir: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    """生成一次 analysis_context_pack 任务包并返回 manifest 信息。

    chapters 接受 "all" / "1-5" / "1,3,5-7" 任何 analysis_context_pack 已支持的 spec；
    targets / question / include_original 透传给现有打包器，不复制 prompt 或 schema。

    force 当前对 analysis 是显式参数：analysis_context_pack 每次都生成新的时间戳目录，
    pack 永远不覆盖既有 canonical 产物；adapter 把 force 透传并记录到 result_json，
    既满足"force rerun 不得删除既有 task 历史"的全局约束，也让调度层显式表达意图。
    """
    from analysis_context_pack import main as acp_main
    project_dir = Path(project_dir)
    argv = _analysis_pack_argv(
        project_dir,
        task=task, chapters=chapters, targets=targets, question=question,
        include_original=include_original, max_pack_chars=max_pack_chars,
        max_original_chars=max_original_chars, max_analysis_chars=max_analysis_chars,
        story_summary_max_items=story_summary_max_items,
        context_budget_chars=context_budget_chars,
        out_dir=out_dir,
    )
    rc = acp_main(argv)
    if rc != 0:
        raise RuntimeError(f"analysis_context_pack failed (task={task}): rc={rc}")
    manifest_path = latest_analysis_manifest(project_dir, task)
    if manifest_path is None:
        raise RuntimeError(f"analysis manifest not found for task={task}")
    manifest = load_json(manifest_path, {}) or {}
    return {
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "target_outputs": manifest.get("target_outputs") or [],
        "packs": manifest.get("packs") or [],
        "task": task,
        "chapters": chapters,
        "force": force,
    }


def _per_chapter_argv(project_dir, *, task, chapters_spec, include_original,
                      max_pack_chars, max_original_chars, max_analysis_chars,
                      story_summary_max_items, context_budget_chars, out_dir) -> List[str]:
    argv = [
        str(project_dir),
        "--task", task,
        "--per-chapter",
        "--chapters", chapters_spec,
        "--include-original", include_original,
        "--max-pack-chars", str(max_pack_chars),
        "--max-original-chars", str(max_original_chars),
        "--max-analysis-chars", str(max_analysis_chars),
        "--story-summary-max-items", str(story_summary_max_items),
        "--context-budget-chars", str(context_budget_chars),
    ]
    if out_dir:
        argv.extend(["--out-dir", out_dir])
    return argv


def scoped_chapter_visual_pack(
    project_dir,
    *,
    seqs,
    include_original="sample",
    max_pack_chars: int = 70000,
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    out_dir: str = "",
) -> Dict[str, Any]:
    """对请求的 seq 列表生成 visual_assets per-chapter 任务包。

    sequence 过滤靠 --chapters spec 传给 analysis_context_pack，不复制打包逻辑。
    已有完整分章产物的章节会被现有打包器自动跳过（断点续传）。
    """
    from analysis_context_pack import main as acp_main
    project_dir = Path(project_dir)
    seqs = sorted(set(int(s) for s in seqs))
    if not seqs:
        raise ValueError("seqs must be a non-empty list of chapter seqs")
    chapters_spec = ",".join(str(s) for s in seqs)
    argv = _per_chapter_argv(
        project_dir, task="visual_assets", chapters_spec=chapters_spec,
        include_original=include_original, max_pack_chars=max_pack_chars,
        max_original_chars=max_original_chars, max_analysis_chars=max_analysis_chars,
        story_summary_max_items=story_summary_max_items,
        context_budget_chars=context_budget_chars,
        out_dir=out_dir,
    )
    rc = acp_main(argv)
    if rc != 0:
        raise RuntimeError(f"analysis_context_pack (visual per-chapter) failed: rc={rc}")
    manifest_path = latest_analysis_manifest(project_dir, "visual_assets_per_chapter")
    manifest = load_json(manifest_path, {}) if manifest_path else {}
    return {
        "manifest_path": str(manifest_path) if manifest_path else "",
        "manifest": manifest,
        "packs": manifest.get("packs") or [],
        "seqs": seqs,
        "mode": "per_chapter",
        "task": "visual_assets",
    }


def scoped_chapter_visual_aggregate(project_dir, *, out_dir: str = "") -> Dict[str, Any]:
    """生成 visual_assets 顶层汇总任务包。

    需要分章视觉资产文件已存在（由 worker 写入）；aggregate 任务包基于既有分章产物
    合并顶层五件套。不复制 aggregate 逻辑，只调用既有 _build_visual_aggregate_pack。
    """
    from analysis_context_pack import main as acp_main
    project_dir = Path(project_dir)
    argv = [str(project_dir), "--task", "visual_assets", "--aggregate"]
    if out_dir:
        argv.extend(["--out-dir", out_dir])
    rc = acp_main(argv)
    if rc != 0:
        raise RuntimeError(f"analysis_context_pack (visual aggregate) failed: rc={rc}")
    manifest_path = latest_analysis_manifest(project_dir, "visual_assets_aggregate")
    manifest = load_json(manifest_path, {}) if manifest_path else {}
    return {
        "manifest_path": str(manifest_path) if manifest_path else "",
        "manifest": manifest,
        "target_outputs": manifest.get("target_outputs") or [],
        "mode": "aggregate",
        "task": "visual_assets",
    }


def scoped_chapter_structure_pack(
    project_dir,
    *,
    seqs,
    include_original="sample",
    max_pack_chars: int = 70000,
    max_original_chars: int = 6000,
    max_analysis_chars: int = 12000,
    story_summary_max_items: int = 0,
    context_budget_chars: int = 160000,
    out_dir: str = "",
) -> Dict[str, Any]:
    """对请求的 seq 列表生成 chapter_structure per-chapter 任务包。"""
    from analysis_context_pack import main as acp_main
    project_dir = Path(project_dir)
    seqs = sorted(set(int(s) for s in seqs))
    if not seqs:
        raise ValueError("seqs must be a non-empty list of chapter seqs")
    chapters_spec = ",".join(str(s) for s in seqs)
    argv = _per_chapter_argv(
        project_dir, task="chapter_structure", chapters_spec=chapters_spec,
        include_original=include_original, max_pack_chars=max_pack_chars,
        max_original_chars=max_original_chars, max_analysis_chars=max_analysis_chars,
        story_summary_max_items=story_summary_max_items,
        context_budget_chars=context_budget_chars,
        out_dir=out_dir,
    )
    rc = acp_main(argv)
    if rc != 0:
        raise RuntimeError(f"analysis_context_pack (chapter_structure per-chapter) failed: rc={rc}")
    manifest_path = latest_analysis_manifest(project_dir, "chapter_structure_per_chapter")
    manifest = load_json(manifest_path, {}) if manifest_path else {}
    return {
        "manifest_path": str(manifest_path) if manifest_path else "",
        "manifest": manifest,
        "packs": manifest.get("packs") or [],
        "seqs": seqs,
        "mode": "per_chapter",
        "task": "chapter_structure",
    }


def _scoped_chapter_rerun(project_dir, *, seqs, force, prepare_fn, artifact_key, label):
    """共享的 chapter rerun 调度：对每个 seq 调用 prepare_chapter_analysis/delta。"""
    project_dir = Path(project_dir)
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        raise RuntimeError("multi-unit artifacts present; cannot rerun chapter task")
    ensure_story(project_dir)
    seqs = sorted(set(int(s) for s in seqs))
    if not seqs:
        raise ValueError("seqs must be a non-empty list of chapter seqs")
    per_seq: List[Dict[str, Any]] = []
    for seq in seqs:
        chapter = chapter_by_seq(project_dir, seq)
        if not chapter:
            raise FileNotFoundError(f"chapter seq {seq} not found in project")
        rc = prepare_fn(project_dir, chapter, force=force)
        paths = artifact_paths(project_dir, chapter)
        per_seq.append({
            "seq": seq,
            "rc": rc,
            "chapter": chapter.name,
            artifact_key: str(paths[artifact_key]),
        })
        if rc != 0:
            raise RuntimeError(
                f"{label} failed for seq {seq}: rc={rc} (artifact={paths[artifact_key]})"
            )
    return {"seqs": seqs, "force": force, "per_seq": per_seq, "task": label}


def scoped_chapter_analysis_rerun(project_dir, *, seqs, force: bool = False) -> Dict[str, Any]:
    """对请求的 seq 列表重新生成章节分析任务包（章节处理/_任务包/task_chNNN_analysis.md）。

    force=True 时覆盖既有任务包；force=False 时既有任务包被复用（rc=0）。
    """
    return _scoped_chapter_rerun(
        project_dir, seqs=seqs, force=force,
        prepare_fn=prepare_chapter_analysis,
        artifact_key="analysis_task",
        label="chapter_analysis_rerun",
    )


def scoped_chapter_delta_rerun(project_dir, *, seqs, force: bool = False) -> Dict[str, Any]:
    """对请求的 seq 列表重新生成 Delta 提取任务包（章节处理/_任务包/task_chNNN_delta.md）。

    需要本章章节分析 MD 已通过校验；否则 prepare_chapter_delta 会生成重新生成任务并
    返回 rc=2，此函数将其作为失败抛出，adapter 据此返回 failed 任务。
    """
    return _scoped_chapter_rerun(
        project_dir, seqs=seqs, force=force,
        prepare_fn=prepare_chapter_delta,
        artifact_key="delta_task",
        label="chapter_delta_rerun",
    )


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
    p_run.add_argument("--end-chapter-seq", type=non_negative_int, default=0, help="包含的最后章节序号；0 表示全书")
    p_run.add_argument("--audit-interval", type=int, default=5, help="每N章执行一次自动周期治理；0表示关闭")
    p_run.add_argument("--audit-max-context-chars", type=positive_int, default=DEFAULT_AUDIT_MAX_CONTEXT_CHARS, help="周期审计单包最大字符数；默认 524288 = 512×1024，适配长上下文模型")
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
    p_audit.add_argument("--max-context-chars", type=positive_int, default=DEFAULT_AUDIT_MAX_CONTEXT_CHARS, help="周期审计单包最大字符数；默认 524288 = 512×1024，适配长上下文模型")

    p_pack = sub.add_parser("analysis-pack", help="生成全书/局部分析任务包")
    p_pack.add_argument("project_dir")
    p_pack.add_argument("--task", choices=["summary", "characters", "plot", "style", "visual_assets", "report", "custom", "worldview", "plotlines", "outline", "detailed_outline", "chapter_structure", "narrative_structure", "settings"], required=True)
    p_pack.add_argument("--chapters", default="all")
    p_pack.add_argument("--targets", nargs="*", default=[])
    p_pack.add_argument("--question", default="")
    p_pack.add_argument("--include-original", choices=["auto", "none", "sample", "full"], default="auto")
    p_pack.add_argument("--max-pack-chars", type=int, default=70000)
    p_pack.add_argument("--max-original-chars", type=int, default=6000)
    p_pack.add_argument("--max-analysis-chars", type=int, default=12000)
    p_pack.add_argument("--story-summary-max-items", type=int, default=0)
    p_pack.add_argument("--context-budget-chars", type=int, default=160000)
    p_pack.add_argument("--out-dir", default="")
    p_pack.add_argument("--per-chapter", action="store_true", help="每章一份独立任务包（visual_assets / chapter_structure），产物扁平写入 全书分析/<维度>/分章/ 下 chNNN_<标题>_*.md")
    p_pack.add_argument("--aggregate", action="store_true", help="（仅 visual_assets）生成顶层汇总任务包，从 分章/ 合并顶层五件套")

    p_status = sub.add_parser("analysis-status", help="查看全书/局部分析状态")
    p_status.add_argument("project_dir")

    p_vauto = sub.add_parser("visual-assets-auto", help="按章自主推进视觉资产生成（每次返回2交接一章，全部完成后交接aggregate）")
    p_vauto.add_argument("project_dir")
    p_vauto.add_argument("--end-chapter-seq", type=non_negative_int, default=0, help="包含的最后章节序号；0 表示全书")
    p_vauto.add_argument("--include-original", choices=["auto", "none", "sample", "full"], default="auto")
    p_vauto.add_argument("--max-pack-chars", type=int, default=70000)
    p_vauto.add_argument("--max-original-chars", type=int, default=6000)
    p_vauto.add_argument("--max-analysis-chars", type=int, default=12000)
    p_vauto.add_argument("--story-summary-max-items", type=int, default=0)
    p_vauto.add_argument("--context-budget-chars", type=int, default=160000)
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
    p_sauto.add_argument("--end-chapter-seq", type=non_negative_int, default=0, help="包含的最后章节序号；0 表示全书")
    p_sauto.add_argument("--include-original", choices=["auto", "none", "sample", "full"], default="auto")
    p_sauto.add_argument("--max-pack-chars", type=int, default=70000)
    p_sauto.add_argument("--max-original-chars", type=int, default=6000)
    p_sauto.add_argument("--max-analysis-chars", type=int, default=12000)
    p_sauto.add_argument("--story-summary-max-items", type=int, default=0)
    p_sauto.add_argument("--context-budget-chars", type=int, default=160000)
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
            end_chapter_seq=args.end_chapter_seq,
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
