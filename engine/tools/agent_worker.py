#!/usr/bin/env python3
"""Generic external-agent worker for novel-disassembler task packages.

The pipeline supplies ND_* environment variables. This wrapper adapts each
headless CLI provider to the pipeline artifact contract.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prompt_loader import render_prompt


PROVIDER_ORDER = ["agy", "codebuddy"]
UNSUPPORTED_NODE_OPTIONS = ("--use-system-ca",)


def default_agy_bin() -> str:
    found = shutil.which("agy")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    return str(local) if local.is_file() else "agy"


def default_codebuddy_bin() -> str:
    found = shutil.which("codebuddy")
    if found:
        return found
    return "codebuddy"


def split_command(command: str) -> List[str]:
    return shlex.split(command, posix=False)


def command_available(parts: List[str]) -> bool:
    if not parts:
        return False
    executable = parts[0].strip('"')
    if Path(executable).is_file():
        return True
    return shutil.which(executable) is not None


def provider_command(provider: str, project_dir: Path, timeout: str) -> List[str]:
    model = os.environ.get("ND_AGENT_MODEL", "").strip()
    if provider == "auto":
        provider = select_provider()
    if provider == "codebuddy":
        parts = split_command(os.environ.get("ND_CODEBUDDY_BIN", default_codebuddy_bin())) + [
            "-p",
            "--output-format",
            "text",
            "--permission-mode",
            "dontAsk",
            "--add-dir",
            str(project_dir),
        ]
        extra_args = os.environ.get("ND_CODEBUDDY_ARGS", "").strip()
        if extra_args:
            parts.extend(split_command(extra_args))
        if os.environ.get("ND_CODEBUDDY_SKIP_PERMISSIONS", "1") != "0":
            parts.append("--dangerously-skip-permissions")
        if model:
            parts.extend(["--model", model])
        return parts
    if provider == "agy":
        parts = split_command(os.environ.get("ND_AGY_BIN", default_agy_bin())) + [
            "--print-timeout",
            timeout,
            "--add-dir",
            str(project_dir),
        ]
        # 让 agy 把内部日志（含被它吞掉的真实错误，如 429 RESOURCE_EXHAUSTED）
        # 写到指定文件，失败时由 main 转发尾部到 worker 日志，便于排查。
        agy_log = os.environ.get("ND_AGY_LOG_FILE", "").strip()
        if agy_log:
            parts.extend(["--log-file", agy_log])
        if os.environ.get("ND_AGY_SKIP_PERMISSIONS", "1") != "0":
            parts.append("--dangerously-skip-permissions")
        if model:
            parts.extend(["--model", model])
        return parts
    raise ValueError(f"unsupported provider: {provider}")


def provider_captures_stdout(provider: str) -> bool:
    return provider == "codebuddy"


def provider_uses_task_pack_path(provider: str) -> bool:
    return provider == "agy"


def select_provider() -> str:
    requested = os.environ.get("ND_AGENT_PROVIDER_ORDER", "")
    order = [p.strip() for p in requested.split(",") if p.strip()] or PROVIDER_ORDER
    for provider in order:
        if provider not in PROVIDER_ORDER:
            continue
        command = provider_command(provider, Path.cwd(), os.environ.get("ND_AGENT_PRINT_TIMEOUT", "30m"))
        if command_available(command):
            print(f"已选择 provider: {provider}", file=sys.stderr)
            return provider
    raise SystemExit("未找到支持的 provider CLI;请安装 agy/codebuddy 或设置 ND_AGY_BIN/ND_CODEBUDDY_BIN")


def provider_order(provider: str) -> List[str]:
    if provider != "auto":
        return [provider]
    requested = os.environ.get("ND_AGENT_PROVIDER_ORDER", "")
    return [p.strip() for p in requested.split(",") if p.strip()] or PROVIDER_ORDER


def task_guidance(task_type: str) -> str:
    guidance = {
        "analysis": "任务重点:按任务包要求写章节分析 Markdown。根据任务包,只提取和组织本章的剧情、人物、冲突、信息增量、结构和叙事信号。",
        "analysis_regenerate": "任务重点:因校验失败重新生成章节分析 Markdown。使用任务包内的校验反馈,重写完整的目标 Markdown。",
        "delta": "任务重点:写本章 Delta JSON。使用任务包的章节分析、当前结构上下文和规则,只输出要求的增量 JSON。",
        "repair_chapter": "任务重点:根据任务包内的校验报告修复本章 Delta JSON。保留有效语义;只改达成目标所需的。",
        "audit_correction": "任务重点:写周期治理补丁 JSON。审查审计任务包,只产出任务包要求的真实 no_change/add/modify/delete 治理操作。",
        "audit_repair": "任务重点:根据校验报告修复周期治理补丁 JSON。保留有效审计意图;只改达成目标所需的。",
    }
    return guidance.get(task_type, "任务重点:严格遵循任务包,只写要求的目标产物。")


def build_prompt(task_type: str, task_pack: Path, expected_outputs: List[Path], capture_stdout: bool) -> str:
    task_text = task_pack.read_text(encoding="utf-8", errors="ignore")
    if capture_stdout and len(expected_outputs) == 1:
        expected_output_text = str(expected_outputs[0])
        output_kind = "JSON" if expected_outputs[0].suffix.lower() == ".json" else "Markdown"
        output_rule = f"只输出期望 {output_kind} 产物的完整内容。不要直接编辑文件;stdout 会被管线捕获。"
    else:
        expected_output_text = "\n".join(f"- {path}" for path in expected_outputs)
        output_kind = "项目文件"
        output_rule = "直接写入上方列出的每个期望项目文件。stdout 只能含简短进度;不会作为产物内容。"
    return render_prompt(
        "工作包/worker_内容.j2",
        task_type=task_type,
        task_pack=task_pack,
        expected_output_text=expected_output_text,
        output_kind=output_kind,
        output_rule=output_rule,
        guidance=task_guidance(task_type),
        task_text=task_text,
    )


def build_path_prompt(task_type: str, task_pack: Path, expected_outputs: List[Path]) -> str:
    expected_output_text = "\n".join(f"- {path}" for path in expected_outputs)
    if len(expected_outputs) == 1:
        output_kind = "JSON" if expected_outputs[0].suffix.lower() == ".json" else "Markdown"
    else:
        output_kind = "项目文件"
    return render_prompt(
        "工作包/worker_路径.j2",
        task_type=task_type,
        task_pack=task_pack,
        expected_output_text=expected_output_text,
        output_kind=output_kind,
        guidance=task_guidance(task_type),
    )


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"缺少必需的环境变量: {name}")
    return value


def expected_outputs_from_env() -> tuple[List[Path], bool]:
    multi = os.environ.get("ND_EXPECTED_OUTPUTS", "").strip()
    single = os.environ.get("ND_EXPECTED_OUTPUT", "").strip()
    if multi:
        try:
            values = json.loads(multi)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"ND_EXPECTED_OUTPUTS JSON 无效: {exc}") from exc
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise SystemExit("ND_EXPECTED_OUTPUTS 必须是路径的 JSON 列表")
        return [Path(item) for item in values], False
    if single:
        return [Path(single)], True
    raise SystemExit("缺少必需的环境变量: ND_EXPECTED_OUTPUT 或 ND_EXPECTED_OUTPUTS")


def is_unsupported_node_option(option: str) -> bool:
    return any(option == unsupported or option.startswith(f"{unsupported}=") for unsupported in UNSUPPORTED_NODE_OPTIONS)


def provider_env() -> dict[str, str]:
    env = dict(os.environ)
    node_options = env.get("NODE_OPTIONS", "")
    if node_options:
        options = split_command(node_options)
        filtered = [option for option in options if not is_unsupported_node_option(option)]
        if filtered != options:
            if filtered:
                env["NODE_OPTIONS"] = subprocess.list2cmdline(filtered)
            else:
                env.pop("NODE_OPTIONS", None)
            removed = ", ".join(option for option in options if option not in filtered)
            print(f"已忽略不支持的 worker CLI NODE_OPTIONS: {removed}", file=sys.stderr)
    return env


def run_provider_command(command: List[str], prompt: str, project_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=prompt,
        cwd=str(project_dir),
        env=provider_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=None,
    )


def run_provider(
    provider: str,
    command: List[str],
    prompt: str,
    project_dir: Path,
) -> subprocess.CompletedProcess[str]:
    if provider_captures_stdout(provider):
        return run_provider_command(command, prompt, project_dir)
    return run_provider_command([*command, "-p", prompt], "", project_dir)


def _dump_provider_log_tail(log_path: str) -> None:
    """agy 失败时把其内部日志尾部追加到 worker 日志（stderr，会被 stream_shell_command 合并进 worker log）。"""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    # 优先抛出真正的错误行（E 开头 = agy 的 error 级日志）。
    errors = [ln for ln in lines if ln.startswith("E") or "error" in ln.lower()]
    tail = errors[-20:] if errors else lines[-40:]
    if not tail:
        return
    print("---- provider 日志尾部 ----", file=sys.stderr)
    for ln in tail:
        print(ln, file=sys.stderr)
    print("---- provider 日志尾部结束 ----", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="作为受控任务包 worker 运行 agy/codebuddy。")
    parser.add_argument(
        "--provider",
        choices=["auto", "agy", "codebuddy"],
        default=os.environ.get("ND_AGENT_PROVIDER", "auto"),
    )
    parser.add_argument("--print-timeout", default=os.environ.get("ND_AGENT_PRINT_TIMEOUT", "30m"))
    args = parser.parse_args()

    task_type = required_env("ND_TASK_TYPE")
    project_dir = Path(required_env("ND_PROJECT_DIR"))
    task_pack = Path(required_env("ND_TASK_PACK"))
    expected_outputs, env_capture_stdout = expected_outputs_from_env()
    if not task_pack.is_file():
        print(f"任务包不存在: {task_pack}", file=sys.stderr)
        return 1

    last_returncode = 1
    attempted = False
    # agy 默认把真实异常吞掉（只吐 "Agent execution terminated due to error."）。
    # 强制 --log-file 写到临时文件，失败时把尾部追加进 worker 日志，便于排查 429 等。
    agy_log_fd, agy_log_path = tempfile.mkstemp(suffix=".log", prefix="agy_diag_")
    os.close(agy_log_fd)
    os.environ.setdefault("ND_AGY_LOG_FILE", agy_log_path)
    try:
        for provider in provider_order(args.provider):
            if provider not in PROVIDER_ORDER:
                continue
            capture_stdout = env_capture_stdout and provider_captures_stdout(provider)
            if provider_uses_task_pack_path(provider):
                prompt = build_path_prompt(task_type, task_pack, expected_outputs)
            else:
                prompt = build_prompt(task_type, task_pack, expected_outputs, capture_stdout)
            command = provider_command(provider, project_dir, args.print_timeout)
            if not command_available(command):
                continue
            attempted = True
            if args.provider == "auto":
                print(f"已选择 provider: {provider}", file=sys.stderr)
            result = run_provider(provider, command, prompt, project_dir)
            last_returncode = result.returncode
            if result.returncode != 0:
                if result.stdout:
                    print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, file=sys.stderr, end="")
                _dump_provider_log_tail(agy_log_path)
                if args.provider == "auto":
                    continue
                return result.returncode

            if capture_stdout:
                content = (result.stdout or "").strip()
                if not content:
                    print(f"provider 输出为空: {provider}", file=sys.stderr)
                    if args.provider == "auto":
                        last_returncode = 1
                        continue
                    return 1
                expected_output = expected_outputs[0]
                expected_output.parent.mkdir(parents=True, exist_ok=True)
                expected_output.write_text(content + "\n", encoding="utf-8")
                return 0
            if result.stdout:
                print(result.stdout, end="")
            missing = [path for path in expected_outputs if not path.is_file() or path.stat().st_size == 0]
            if missing:
                print("provider 未写入期望的输出文件:", file=sys.stderr)
                for path in missing:
                    print(f"- {path}", file=sys.stderr)
                if args.provider == "auto":
                    last_returncode = 1
                    continue
                return 1
            return 0
    finally:
        os.environ.pop("ND_AGY_LOG_FILE", None)
        try:
            os.unlink(agy_log_path)
        except OSError:
            pass

    if not attempted:
        print("未找到支持的 provider CLI;请安装 agy/codebuddy 或设置 ND_AGY_BIN/ND_CODEBUDDY_BIN", file=sys.stderr)
    return last_returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
