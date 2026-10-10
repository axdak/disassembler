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
from worker_artifacts import (
    capture_output_signatures,
    invalid_output_artifacts,
    output_signature,
    unchanged_existing_outputs,
)


PROVIDER_ORDER = ["agy", "codebuddy"]
UNSUPPORTED_NODE_OPTIONS = ("--use-system-ca",)
JSON_TASK_TYPES = {"delta", "repair_chapter", "audit_correction", "audit_repair"}
JSON_OBJECT_SCHEMA = json.dumps({"type": "object"}, separators=(",", ":"))
DELTA_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "delta.schema.json"


def structured_output_schema(task_type: str, expected_outputs: List[Path]) -> str:
    """Return the strictest native schema available for one JSON artifact."""
    if len(expected_outputs) != 1 or expected_outputs[0].suffix.lower() != ".json":
        return ""
    if task_type not in {"delta", "repair_chapter"}:
        return JSON_OBJECT_SCHEMA
    try:
        schema = json.loads(DELTA_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return JSON_OBJECT_SCHEMA
    # Provider validation does not need documentation-only metadata, and
    # omitting it keeps the Windows command line compact.
    schema.pop("$schema", None)
    schema.pop("$comment", None)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


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


def provider_command(
    provider: str,
    project_dir: Path,
    timeout: str,
    json_schema: str = "",
) -> List[str]:
    """Build a provider command, including native structured-output constraints."""
    model = os.environ.get("ND_AGENT_MODEL", "").strip()
    if provider == "auto":
        provider = select_provider()
    if provider == "codebuddy":
        output_format = "json" if json_schema else "text"
        parts = split_command(os.environ.get("ND_CODEBUDDY_BIN", default_codebuddy_bin())) + [
            "-p",
            "--output-format",
            output_format,
            # bypassPermissions: 非交互(-p)模式下必须真正绕过权限检查,worker 才能用 Write 落盘产物。
            # dontAsk 仅表示"不弹窗",询问不了时会直接拒绝 Write/Bash,导致模型准备好内容却写不进文件。
            "--permission-mode",
            "bypassPermissions",
            "--add-dir",
            str(project_dir),
        ]
        extra_args = os.environ.get("ND_CODEBUDDY_ARGS", "").strip()
        if extra_args:
            parts.extend(split_command(extra_args))
        if os.environ.get("ND_CODEBUDDY_SKIP_PERMISSIONS", "1") != "0":
            parts.append("--dangerously-skip-permissions")
        if json_schema:
            # CodeBuddy validates the model response against this schema before
            # returning it as structured_output in the JSON result envelope.
            parts.extend(["--json-schema", json_schema])
            # Some provider/model combinations need more than two turns to
            # submit StructuredOutput. Only apply a turn cap when deployment
            # configuration explicitly requests one; an implicit low cap can
            # otherwise terminate with an empty result before tool submission.
            structured_max_turns = os.environ.get(
                "ND_CODEBUDDY_STRUCTURED_MAX_TURNS", ""
            ).strip()
            if structured_max_turns and "--max-turns" not in parts:
                parts.extend(["--max-turns", structured_max_turns])
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
        if output_kind == "JSON":
            output_rule = (
                "必须调用 CodeBuddy 提供的 StructuredOutput 工具一次，提交期望 JSON 产物的完整对象；"
                "顶层必须是单个对象，禁止提交数组、字符串或 null。"
                "不要在普通回复中打印 JSON、Markdown 代码块或解释文字，也不要直接编辑文件；"
                "管线只会提取 StructuredOutput 的结构化参数。"
            )
        else:
            output_rule = "只输出期望 Markdown 产物的完整内容。不要直接编辑文件;stdout 会被管线捕获。"
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
    if env.get("ND_TASK_TYPE") in JSON_TASK_TYPES and not env.get(
        "CODEBUDDY_CODE_MAX_OUTPUT_TOKENS"
    ):
        # Custom CodeBuddy models may omit maxOutputTokens from their catalog
        # entry, which makes the CLI fall back to a small server-side default
        # and can truncate a valid Delta halfway through a JSON string.
        env["CODEBUDDY_CODE_MAX_OUTPUT_TOKENS"] = env.get(
            "ND_CODEBUDDY_JSON_MAX_OUTPUT_TOKENS", "32768"
        )
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


def _captured_output_is_valid(path: Path, content: str) -> bool:
    """Validate captured stdout before it may replace an output artifact."""
    if not content.strip():
        return False
    if path.suffix.lower() != ".json":
        return True
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict)


def _extract_captured_artifact(
    provider: str,
    raw_content: str,
    *,
    structured: bool,
) -> str:
    """Extract the artifact body from a provider-specific result envelope."""
    if provider != "codebuddy" or not structured:
        return raw_content
    try:
        envelope = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content
    if isinstance(envelope, list):
        # Some CodeBuddy builds emit the complete message transcript even for
        # --output-format json.  The result message containing structured_output
        # is the last matching object in that transcript.
        result_messages = [
            item
            for item in envelope
            if isinstance(item, dict)
            and ("structured_output" in item or item.get("type") == "result")
        ]
        if not result_messages:
            return raw_content
        envelope = result_messages[-1]
    if not isinstance(envelope, dict):
        return raw_content
    structured_output = envelope.get("structured_output")
    if structured_output is not None:
        return json.dumps(structured_output, ensure_ascii=False, indent=2)
    # Older CodeBuddy builds may serialize structured JSON into result while
    # omitting structured_output; accept that field only as a string candidate.
    result = envelope.get("result")
    return result if isinstance(result, str) else raw_content


def _structured_result_diagnostic(raw_content: str) -> str:
    """Summarize a structured provider envelope without logging prompt content."""
    try:
        envelope = json.loads(raw_content)
    except json.JSONDecodeError:
        return f"raw_chars={len(raw_content)} envelope=invalid_json"
    if isinstance(envelope, list):
        candidates = [
            item
            for item in envelope
            if isinstance(item, dict)
            and ("structured_output" in item or item.get("type") == "result")
        ]
        if not candidates:
            return f"raw_chars={len(raw_content)} envelope=list messages={len(envelope)} result=missing"
        result = candidates[-1]
        prefix = f"raw_chars={len(raw_content)} envelope=list messages={len(envelope)}"
    elif isinstance(envelope, dict):
        result = envelope
        prefix = f"raw_chars={len(raw_content)} envelope=object"
    else:
        return f"raw_chars={len(raw_content)} envelope={type(envelope).__name__}"
    fields = []
    for key in ("subtype", "is_error", "num_turns", "duration_ms", "duration_api_ms"):
        if key in result:
            fields.append(f"{key}={result[key]!r}")
    structured_output = result.get("structured_output")
    result_text = result.get("result")
    fields.append(f"structured_output={type(structured_output).__name__}")
    if isinstance(result_text, str):
        fields.append(f"result_chars={len(result_text)}")
    return " ".join([prefix, *fields])


def _write_captured_output_atomically(path: Path, content: str) -> None:
    """Publish validated stdout while preserving the prior file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            suffix=path.suffix,
            prefix=f".{path.stem}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            stream.write(content.rstrip() + "\n")
            temp_path = Path(stream.name)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


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
    configured_agy_log = os.environ.get("ND_AGY_LOG_FILE", "").strip()
    owns_agy_log = not configured_agy_log
    if owns_agy_log:
        agy_log_fd, agy_log_path = tempfile.mkstemp(suffix=".log", prefix="agy_diag_")
        os.close(agy_log_fd)
        os.environ["ND_AGY_LOG_FILE"] = agy_log_path
    else:
        agy_log_path = configured_agy_log
    try:
        for provider in provider_order(args.provider):
            if provider not in PROVIDER_ORDER:
                continue
            capture_stdout = env_capture_stdout and provider_captures_stdout(provider)
            if provider_uses_task_pack_path(provider):
                prompt = build_path_prompt(task_type, task_pack, expected_outputs)
            else:
                prompt = build_prompt(task_type, task_pack, expected_outputs, capture_stdout)
            json_schema = structured_output_schema(task_type, expected_outputs)
            command = provider_command(
                provider,
                project_dir,
                args.print_timeout,
                json_schema=json_schema,
            )
            if not command_available(command):
                continue
            attempted = True
            if args.provider == "auto":
                print(f"已选择 provider: {provider}", file=sys.stderr)
            before_outputs = capture_output_signatures(expected_outputs)
            try:
                result = run_provider(provider, command, prompt, project_dir)
            except OSError as exc:
                # CLI 文件存在但无法启动（损坏、权限或平台不兼容）时，auto 仍应尝试下一个 provider。
                last_returncode = 1
                print(f"provider 启动失败: {provider}: {exc}", file=sys.stderr)
                if provider == "agy":
                    _dump_provider_log_tail(agy_log_path)
                if args.provider == "auto":
                    continue
                return 1
            last_returncode = result.returncode
            if result.returncode != 0:
                if result.stdout:
                    print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, file=sys.stderr, end="")
                if provider == "agy":
                    _dump_provider_log_tail(agy_log_path)
                if args.provider == "auto":
                    continue
                return result.returncode

            if capture_stdout:
                raw_content = (result.stdout or "").strip()
                content = _extract_captured_artifact(
                    provider,
                    raw_content,
                    structured=bool(json_schema),
                ).strip()
                expected_output = expected_outputs[0]
                direct_output_changed = (
                    output_signature(expected_output) != before_outputs.get(expected_output)
                )
                direct_output_valid = (
                    direct_output_changed
                    and expected_output.is_file()
                    and not invalid_output_artifacts([expected_output])
                )
                if direct_output_valid:
                    # Some CLIs ignore the stdout-only contract, write the
                    # artifact directly, then print a prose completion summary.
                    # The fresh valid file is authoritative in that situation.
                    print(
                        f"provider 已直接写入有效产物，忽略 stdout 摘要: {expected_output.name}",
                        file=sys.stderr,
                    )
                    return 0
                if not content:
                    print(f"provider 输出为空: {provider}", file=sys.stderr)
                    if args.provider == "auto":
                        last_returncode = 1
                        continue
                    return 1
                if not _captured_output_is_valid(expected_output, content):
                    print(f"provider 写出的 JSON 格式无效: {expected_output}", file=sys.stderr)
                    # Keep a bounded diagnostic because structured-output CLIs
                    # may return an envelope or prose despite a JSON schema.
                    # Never persist this preview into the target artifact.
                    preview = content[:2000].replace("\x00", "")
                    if json_schema:
                        print(
                            f"provider 结构化输出诊断: {_structured_result_diagnostic(raw_content)}",
                            file=sys.stderr,
                        )
                    print(f"provider stdout 预览:\n{preview}", file=sys.stderr)
                    if args.provider == "auto":
                        last_returncode = 1
                        continue
                    return 1
                _write_captured_output_atomically(expected_output, content)
                # capture_stdout 模式下 stdout 否则为空（产物已写文件、不回显内容），
                # 留一行摘要给 worker 日志，便于判断该章是否真完成。
                print(f"provider 已通过 stdout 捕获写入: {expected_output.name}（{len(content)} 字符）")
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
            invalid = invalid_output_artifacts(expected_outputs)
            if invalid:
                print("provider 写出的产物格式无效:", file=sys.stderr)
                for path in invalid:
                    print(f"- {path}", file=sys.stderr)
                if args.provider == "auto":
                    last_returncode = 1
                    continue
                return 1
            unchanged = (
                unchanged_existing_outputs(before_outputs, expected_outputs)
                if os.environ.get("ND_WORKER_REQUIRE_FRESH_OUTPUT", "1") != "0"
                else []
            )
            if unchanged:
                print("provider 未更新已存在的期望输出文件:", file=sys.stderr)
                for path in unchanged:
                    print(f"- {path}", file=sys.stderr)
                if args.provider == "auto":
                    last_returncode = 1
                    continue
                return 1
            print(f"provider 已写入产物: {', '.join(p.name for p in expected_outputs if p.is_file())}")
            return 0
    finally:
        if owns_agy_log:
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
