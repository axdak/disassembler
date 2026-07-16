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
from pathlib import Path
from typing import List


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
            print(f"selected provider: {provider}", file=sys.stderr)
            return provider
    raise SystemExit(
        "no supported provider CLI found; install agy/codebuddy or set "
        "ND_AGY_BIN or ND_CODEBUDDY_BIN"
    )


def provider_order(provider: str) -> List[str]:
    if provider != "auto":
        return [provider]
    requested = os.environ.get("ND_AGENT_PROVIDER_ORDER", "")
    return [p.strip() for p in requested.split(",") if p.strip()] or PROVIDER_ORDER


def task_guidance(task_type: str) -> str:
    guidance = {
        "analysis": (
            "Task focus: write the chapter analysis Markdown requested by the task pack. "
            "Extract and organize only the current chapter's plot, characters, conflicts, "
            "information increments, structure, and narrative signals according to the pack."
        ),
        "analysis_regenerate": (
            "Task focus: regenerate the chapter analysis Markdown because validation failed. "
            "Use the validation feedback inside the task pack and rewrite the full target Markdown."
        ),
        "delta": (
            "Task focus: write the current chapter Delta JSON. Use the task pack's chapter analysis, "
            "current structure context, and rules to output only the incremental JSON requested."
        ),
        "repair_chapter": (
            "Task focus: repair the current chapter Delta JSON based on the validation report in the "
            "task pack. Preserve valid semantics; change only what is needed for the requested target."
        ),
        "audit_correction": (
            "Task focus: write the periodic governance correction JSON. Review the audit pack and "
            "produce only real no_change/add/modify/delete governance operations requested by the pack."
        ),
        "audit_repair": (
            "Task focus: repair the periodic governance correction JSON based on the validation report. "
            "Preserve valid audit intent; change only what is needed for the correction target."
        ),
    }
    return guidance.get(task_type, "Task focus: follow the task pack exactly and write only the requested target artifact.")


def build_prompt(task_type: str, task_pack: Path, expected_outputs: List[Path], capture_stdout: bool) -> str:
    task_text = task_pack.read_text(encoding="utf-8", errors="ignore")
    if capture_stdout and len(expected_outputs) == 1:
        expected_output_text = str(expected_outputs[0])
        output_kind = "JSON" if expected_outputs[0].suffix.lower() == ".json" else "Markdown"
        output_rule = (
            f"Output only the complete content of the expected {output_kind} artifact. "
            "Do not edit the file directly; stdout will be captured by the pipeline."
        )
    else:
        expected_output_text = "\n".join(f"- {path}" for path in expected_outputs)
        output_kind = "project files"
        output_rule = (
            "Write every expected project file listed above directly. Stdout may contain only brief progress; "
            "it will not be used as artifact content."
        )
    return f"""You are a controlled external worker for novel-disassembler.
Current task type: {task_type}
Task pack path: {task_pack}
Expected output file(s):
{expected_output_text}
Expected output kind: {output_kind}

{task_guidance(task_type)}

Hard rules:
1. Read and follow the task pack below; it is the authority for this task.
2. {output_rule}
3. Do not explain, summarize, wrap artifacts in code fences, or add unrelated prose.
4. Do not edit story_structure_delta.json or any process database.
5. Do not create scripts, batch processors, schedulers, or extra artifacts.
6. Do not process chapters or audit ranges outside the task pack.
7. For repair tasks, rewrite only the expected target artifact content.

Task pack:

{task_text}
"""


def build_path_prompt(task_type: str, task_pack: Path, expected_outputs: List[Path]) -> str:
    expected_output_text = "\n".join(f"- {path}" for path in expected_outputs)
    if len(expected_outputs) == 1:
        output_kind = "JSON" if expected_outputs[0].suffix.lower() == ".json" else "Markdown"
    else:
        output_kind = "project files"
    return f"""You are a controlled external worker for novel-disassembler.
Current task type: {task_type}
Task pack path: {task_pack}
Expected output file(s):
{expected_output_text}
Expected output kind: {output_kind}

{task_guidance(task_type)}

Hard rules:
1. Read the task pack from the exact path above; it is the authority for this task.
2. Write every expected project file listed above directly. Stdout may contain only brief progress; it will not be used as artifact content.
3. Do not explain, summarize, wrap artifacts in code fences, or add unrelated prose.
4. Do not edit story_structure_delta.json or any process database.
5. Do not create scripts, batch processors, schedulers, prompt files, or extra artifacts.
6. Do not process chapters or audit ranges outside the task pack.
7. For repair tasks, rewrite only the expected target artifact content.
"""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def expected_outputs_from_env() -> tuple[List[Path], bool]:
    multi = os.environ.get("ND_EXPECTED_OUTPUTS", "").strip()
    single = os.environ.get("ND_EXPECTED_OUTPUT", "").strip()
    if multi:
        try:
            values = json.loads(multi)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid ND_EXPECTED_OUTPUTS JSON: {exc}") from exc
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise SystemExit("ND_EXPECTED_OUTPUTS must be a JSON list of paths")
        return [Path(item) for item in values], False
    if single:
        return [Path(single)], True
    raise SystemExit("missing required environment variable: ND_EXPECTED_OUTPUT or ND_EXPECTED_OUTPUTS")


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
            print(f"ignored unsupported NODE_OPTIONS for worker CLI: {removed}", file=sys.stderr)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agy/codebuddy as a controlled task-package worker.")
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
        print(f"task pack does not exist: {task_pack}", file=sys.stderr)
        return 1

    last_returncode = 1
    attempted = False
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
            print(f"selected provider: {provider}", file=sys.stderr)
        result = run_provider(provider, command, prompt, project_dir)
        last_returncode = result.returncode
        if result.returncode != 0:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            if args.provider == "auto":
                continue
            return result.returncode

        if capture_stdout:
            content = (result.stdout or "").strip()
            if not content:
                print(f"provider produced empty output: {provider}", file=sys.stderr)
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
            print("provider did not write expected output file(s):", file=sys.stderr)
            for path in missing:
                print(f"- {path}", file=sys.stderr)
            if args.provider == "auto":
                last_returncode = 1
                continue
            return 1
        return 0

    if not attempted:
        print("no supported provider CLI found; install agy/codebuddy or set ND_AGY_BIN or ND_CODEBUDDY_BIN", file=sys.stderr)
    return last_returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
