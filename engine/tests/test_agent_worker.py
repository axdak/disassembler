import json
import os
import subprocess
import sys
import tempfile
import pytest
from pathlib import Path

from context_budget import ContextBudget


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tools" / "agent_worker.py"


@pytest.mark.parametrize("task_type,value", [
    ("recovery_diagnosis", {"difficulty": "easy"}),
    ("recovery_diagnosis", {"diagnosis": "问题", "steps": [""]}),
    ("recovery_review", {"passed": True}),
])
@pytest.mark.parametrize("direct", [False, True])
def test_internal_contract_rejects_valid_json_with_wrong_shape(task_type, value, direct):
    """Enforce contracts even when a CLI ignores native structured output."""
    env = {"FAKE_CLI_STDOUT": json.dumps(value)}
    if direct:
        env.update(FAKE_CLI_DIRECT_WRITE="1", FAKE_CLI_DIRECT_CONTENT=json.dumps(value))
    result, output, _ = run_worker("codebuddy", "ND_CODEBUDDY_BIN", env,
                                 task_type=task_type, output_name="out.json")
    assert result.returncode == 66, result.stdout
    if not direct:
        assert output == ""  # Rejected stdout never becomes an accepted artifact.


def test_diagnosis_native_schema_requires_actionable_nonempty_steps():
    plan = {"diagnosis": "字段位置错误", "steps": ["原值迁入详情"]}
    result, output, payload = run_worker("codebuddy", "ND_CODEBUDDY_BIN",
                                        {"FAKE_CLI_STDOUT": json.dumps(plan)},
                                        task_type="recovery_diagnosis", output_name="out.json")
    assert result.returncode == 0, result.stdout
    assert json.loads(output) == plan
    schema = json.loads(payload["argv"][payload["argv"].index("--json-schema") + 1])
    assert schema["required"] == ["diagnosis", "steps"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["steps"]["minItems"] == 1


@pytest.mark.parametrize("content", ["", "not json", '{"diagnosis":'])
def test_invalid_diagnosis_syntax_uses_protocol_failure_code(content):
    result, output, _ = run_worker("codebuddy", "ND_CODEBUDDY_BIN", {"FAKE_CLI_STDOUT": content},
                                 task_type="recovery_diagnosis", output_name="out.json")
    assert result.returncode == 66, result.stdout
    assert output == ""


def write_fake_cli(path: Path) -> None:
    path.write_text(
        """
import json
import os
import sys
from pathlib import Path

record = Path(os.environ["FAKE_CLI_RECORD"])
record.write_text(json.dumps({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "node_options": os.environ.get("NODE_OPTIONS", ""),
    "max_output_tokens": os.environ.get("CODEBUDDY_CODE_MAX_OUTPUT_TOKENS", ""),
    "task_pack": os.environ.get("ND_TASK_PACK", ""),
    "expected_output": os.environ.get("ND_EXPECTED_OUTPUT", ""),
    "cwd": str(Path.cwd()),
    "mcp": json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")) if "--mcp-config" in sys.argv else None,
}, ensure_ascii=False, indent=2), encoding="utf-8")
if os.environ.get("FAKE_CLI_CAPACITY_LOG"):
    from datetime import datetime
    logdir = Path(os.environ["CODEBUDDY_CONFIG_DIR"]) / "logs" / datetime.now().strftime("%Y-%m-%d")
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / (Path.cwd().name + "__fake.log")).write_text(
        'finish_reason="length" received, requestId=model-request-1, chunks=6934\\n', encoding="utf-8")
if os.environ.get("FAKE_CLI_DIRECT_WRITE") == "1":
    output = Path(os.environ["ND_EXPECTED_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    direct_content = os.environ.get(
        "FAKE_CLI_DIRECT_CONTENT",
        "# generated direct artifact\\n" + os.environ.get("ND_TASK_TYPE", "") + "\\n",
    )
    output.write_text(direct_content, encoding="utf-8")
    if os.environ.get("FAKE_CLI_DIRECT_AND_STDOUT") != "1":
        raise SystemExit(0)
print(os.environ.get("FAKE_CLI_STDOUT", "# generated artifact\\n" + os.environ.get("ND_TASK_TYPE", "")))
""",
        encoding="utf-8",
    )


def run_worker(
    provider: str,
    provider_env: str,
    extra_env=None,
    task_text: str | None = None,
    existing_output: str | None = None,
    output_name: str = "out.md",
    task_type: str = "analysis",
):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "task.md"
        task.write_text(task_text or "# Task\nWrite the target file.\n", encoding="utf-8")
        output = root / output_name
        if existing_output is not None:
            output.write_text(existing_output, encoding="utf-8")
            # 固定为旧时间，确保测试能区分“重写相同内容”和“完全没有触碰文件”。
            os.utime(output, ns=(1_000_000_000, 1_000_000_000))
        fake_cli = root / "fake_cli.py"
        record = root / "record.json"
        write_fake_cli(fake_cli)

        env = dict(os.environ)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ND_TASK_TYPE": task_type,
                "ND_PROJECT_DIR": str(project),
                "ND_TASK_PACK": str(task),
                "ND_EXPECTED_OUTPUT": str(output),
                "ND_CHAPTER_SEQ": "1",
                "FAKE_CLI_RECORD": str(record),
                provider_env: f"{sys.executable} {fake_cli}",
            }
        )
        if extra_env:
            env.update(extra_env)

        result = subprocess.run(
            [sys.executable, str(WORKER), "--provider", provider],
            cwd=str(project),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        output_text = output.read_text(encoding="utf-8") if output.is_file() else ""
        payload = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        return result, output_text, payload


def test_codebuddy_provider_writes_stdout_to_expected_output():
    result, output_text, payload = run_worker("codebuddy", "ND_CODEBUDDY_BIN")

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated artifact")
    assert "-p" in payload["argv"]
    assert "--output-format" in payload["argv"]
    assert "Write the target file." in payload["stdin"]


def test_codebuddy_only_exposes_scoped_context_without_inherited_capabilities():
    """The real process arguments must enforce the boundary, not just the prompt."""
    result, _, payload = run_worker("codebuddy", "ND_CODEBUDDY_BIN")
    assert result.returncode == 0, result.stdout
    argv = payload["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--add-dir" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv
    assert set(payload["mcp"]["mcpServers"]) == {"nd_context"}
    assert Path(payload["cwd"]).name.startswith("nd_worker_")


@pytest.mark.parametrize("broad_task", ["audit_correction", "fullbook_analysis", "fullbook_analysis_reduce"])
def test_story_domain_tools_are_exposed_only_for_broad_read_tasks(broad_task):
    chapter_result, _, chapter_payload = run_worker("codebuddy", "ND_CODEBUDDY_BIN", task_type="delta", output_name="out.json")
    assert chapter_result.returncode != 0  # Empty fake output is not a valid Delta; arguments were still captured.
    chapter_allowed = chapter_payload["argv"][chapter_payload["argv"].index("--allowedTools") + 1]
    assert "story_search" not in chapter_allowed
    assert "--story-tools" not in chapter_payload["mcp"]["mcpServers"]["nd_context"]["args"]

    broad_result, _, broad_payload = run_worker(
        "codebuddy", "ND_CODEBUDDY_BIN", task_type=broad_task, output_name="out.json"
    )
    assert broad_result.returncode != 0
    broad_allowed = broad_payload["argv"][broad_payload["argv"].index("--allowedTools") + 1]
    assert "mcp__nd_context__story_search" in broad_allowed
    assert "--story-tools" in broad_payload["mcp"]["mcpServers"]["nd_context"]["args"]


@pytest.mark.parametrize("extra", ["--tools Bash", "--permission-mode bypassPermissions", "--add-dir C:/", "--settings unsafe.json"])
def test_codebuddy_deployment_args_cannot_restore_filesystem_or_command_access(extra):
    result, output, payload = run_worker("codebuddy", "ND_CODEBUDDY_BIN", {"ND_CODEBUDDY_ARGS": extra})
    assert result.returncode != 0
    assert output == "" and payload == {}


@pytest.mark.parametrize("failure", [None, "extra", "invalid"])
def test_multifile_bodies_are_checked_before_publishing(tmp_path, failure):
    """Reject an extra destination or bad second body without touching either old file."""
    sys.path.insert(0, str(ROOT / "tools"))
    from agent_worker import _write_multi_captured_artifacts

    markdown, data = tmp_path / "analysis.md", tmp_path / "result.json"
    markdown.write_text("old analysis", encoding="utf-8")
    data.write_text('{"old":true}', encoding="utf-8")
    bodies = {str(markdown): "# analysis", str(data): '{"passed":true}'}
    if failure == "extra":
        bodies[str(tmp_path / "source.py")] = "unapproved change"
    if failure == "invalid":
        bodies[str(data)] = "broken json"
    if failure:
        with pytest.raises(ValueError):
            _write_multi_captured_artifacts(json.dumps(bodies), [markdown, data])
        assert markdown.read_text(encoding="utf-8") == "old analysis"
        assert json.loads(data.read_text(encoding="utf-8")) == {"old": True}
        assert not (tmp_path / "source.py").exists()
    else:
        _write_multi_captured_artifacts(json.dumps(bodies), [markdown, data])
        assert markdown.read_text(encoding="utf-8").strip() == "# analysis"
        assert json.loads(data.read_text(encoding="utf-8")) == {"passed": True}


def test_codebuddy_preserves_fresh_valid_direct_json_over_stdout_summary():
    """A provider completion summary must not replace JSON it wrote directly."""
    result, output_text, payload = run_worker(
        "codebuddy",
        "ND_CODEBUDDY_BIN",
        {
            "FAKE_CLI_DIRECT_WRITE": "1",
            "FAKE_CLI_DIRECT_AND_STDOUT": "1",
            "FAKE_CLI_DIRECT_CONTENT": '{"新增元素": {}, "修改元素": {}}',
            "FAKE_CLI_STDOUT": "修复完成，文件已写入期望路径且为合法 JSON。",
        },
        output_name="out.json",
        task_type="repair_chapter",
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(output_text) == {"新增元素": {}, "修改元素": {}}
    assert "忽略 stdout 摘要" in result.stdout
    schema = json.loads(payload["argv"][payload["argv"].index("--json-schema") + 1])
    assert schema["type"] == "object"
    assert schema["required"] == ["章节", "新增元素", "修改元素"]
    assert "--max-turns" not in payload["argv"]
    assert "StructuredOutput" in payload["stdin"]
    # 单次输出上限必须容纳单章 Delta（含思维链），不能用 20000 这类过小默认值。
    assert payload["max_output_tokens"] == str(ContextBudget().output_tokens)


def test_codebuddy_rejects_invalid_json_stdout_without_overwriting_existing_file():
    """Invalid captured text must leave the previous artifact byte-for-byte intact."""
    previous = '{"新增元素": {}, "修改元素": {}}\n'
    result, output_text, _payload = run_worker(
        "codebuddy",
        "ND_CODEBUDDY_BIN",
        {"FAKE_CLI_STDOUT": "修复完成，文件已写入期望路径且为合法 JSON。"},
        existing_output=previous,
        output_name="out.json",
    )

    assert result.returncode != 0
    assert output_text == previous
    assert "JSON 格式无效" in result.stdout


def test_codebuddy_rejects_top_level_json_array():
    """Syntactically valid JSON arrays do not satisfy the Delta artifact contract."""
    result, output_text, payload = run_worker(
        "codebuddy",
        "ND_CODEBUDDY_BIN",
        {"FAKE_CLI_STDOUT": '["修改元素"]'},
        output_name="out.json",
    )

    assert result.returncode != 0
    assert output_text == ""
    assert "JSON 格式无效" in result.stdout
    assert "--json-schema" in payload["argv"]


def test_codebuddy_extracts_structured_output_from_json_result_envelope():
    """CodeBuddy JSON mode wraps schema output; only structured_output is the artifact."""
    structured = {"章节": "0001", "新增元素": {}, "修改元素": {}}
    result, output_text, payload = run_worker(
        "codebuddy",
        "ND_CODEBUDDY_BIN",
        {
            "FAKE_CLI_STDOUT": json.dumps(
                [
                    {"type": "message", "role": "user", "content": []},
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": "",
                        "structured_output": structured,
                    },
                ],
                ensure_ascii=False,
            )
        },
        output_name="out.json",
        task_type="repair_chapter",
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(output_text) == structured
    output_format_index = payload["argv"].index("--output-format") + 1
    assert payload["argv"][output_format_index] == "stream-json"
    assert "--max-turns" not in payload["argv"]


def test_agy_provider_uses_print_mode_and_writes_output():
    result, output_text, payload = run_worker(
        "agy",
        "ND_AGY_BIN",
        {"FAKE_CLI_DIRECT_WRITE": "1"},
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated direct artifact")
    assert "-p" in payload["argv"] or "--print" in payload["argv"]
    assert "--print-timeout" in payload["argv"]
    assert "--add-dir" in payload["argv"]
    assert payload["stdin"] == ""
    prompt = payload["argv"][payload["argv"].index("-p") + 1]
    assert payload["task_pack"] in prompt
    assert payload["expected_output"] in prompt
    assert "Write the target file." not in prompt


def test_agy_provider_passes_task_pack_path_instead_of_full_task_text():
    sentinel = "LONG_TASK_BODY_SENTINEL"
    long_task_text = "# Task\n" + sentinel + "\n" + ("x" * 50000)
    result, output_text, payload = run_worker(
        "agy",
        "ND_AGY_BIN",
        {"FAKE_CLI_DIRECT_WRITE": "1"},
        task_text=long_task_text,
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated direct artifact")
    argv = payload["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert len(prompt) < 8000
    assert sentinel not in prompt
    assert payload["task_pack"] in prompt
    assert payload["expected_output"] in prompt


def test_agy_provider_rejects_unchanged_preexisting_output():
    result, output_text, _payload = run_worker(
        "agy",
        "ND_AGY_BIN",
        existing_output="# stale artifact\n",
    )

    assert result.returncode != 0
    assert output_text == "# stale artifact\n"
    assert "provider 未更新已存在的期望输出文件" in result.stdout


def test_agy_provider_accepts_refreshed_preexisting_output():
    result, output_text, _payload = run_worker(
        "agy",
        "ND_AGY_BIN",
        {"FAKE_CLI_DIRECT_WRITE": "1"},
        existing_output="# stale artifact\n",
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated direct artifact")


def test_auto_provider_selects_first_available_cli():
    result, output_text, payload = run_worker(
        "auto",
        "ND_AGY_BIN",
        {"ND_AGENT_PROVIDER_ORDER": "agy", "FAKE_CLI_DIRECT_WRITE": "1"},
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated direct artifact")
    assert "-p" in payload["argv"] or "--print" in payload["argv"]
    assert "已选择 provider: agy" in result.stdout


def test_worker_ignores_unsupported_node_system_ca_option():
    result, output_text, payload = run_worker(
        "codebuddy",
        "ND_CODEBUDDY_BIN",
        {"NODE_OPTIONS": "--use-system-ca --max-old-space-size=4096"},
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated artifact")
    assert "--use-system-ca" not in payload["node_options"]
    assert "--max-old-space-size=4096" in payload["node_options"]
    assert "已忽略不支持的 worker CLI NODE_OPTIONS" in result.stdout


def test_auto_provider_falls_back_when_first_provider_fails():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "task.md"
        task.write_text("# Task\nWrite the target file.\n", encoding="utf-8")
        output = root / "out.md"
        failing_agy = root / "failing_agy.py"
        working_codebuddy = root / "working_codebuddy.py"
        record = root / "codebuddy_record.json"
        failing_agy.write_text(
            """
import sys
sys.stdin.read()
print("agy failed", file=sys.stderr)
raise SystemExit(9)
""",
            encoding="utf-8",
        )
        working_codebuddy.write_text(
            """
import json
import os
import sys
from pathlib import Path

record = Path(os.environ["FAKE_CLI_RECORD"])
record.write_text(json.dumps({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print("# generated by codebuddy fallback")
print(os.environ.get("ND_TASK_TYPE", ""))
""",
            encoding="utf-8",
        )

        env = dict(os.environ)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ND_TASK_TYPE": "analysis",
                "ND_PROJECT_DIR": str(project),
                "ND_TASK_PACK": str(task),
                "ND_EXPECTED_OUTPUT": str(output),
                "ND_CHAPTER_SEQ": "1",
                "ND_AGENT_PROVIDER_ORDER": "agy,codebuddy",
                "ND_AGY_BIN": f"{sys.executable} {failing_agy}",
                "ND_CODEBUDDY_BIN": f"{sys.executable} {working_codebuddy}",
                "FAKE_CLI_RECORD": str(record),
            }
        )

        result = subprocess.run(
            [sys.executable, str(WORKER), "--provider", "auto"],
            cwd=str(project),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        output_text = output.read_text(encoding="utf-8") if output.is_file() else ""
        payload = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        assert result.returncode == 0, result.stdout
        assert output_text.startswith("# generated by codebuddy fallback")
        assert "已选择 provider: agy" in result.stdout
        assert "已选择 provider: codebuddy" in result.stdout
        assert "-p" in payload["argv"]


def test_provider_launch_error_is_reported_without_traceback():
    """CLI 路径存在但不可执行时，应输出可诊断错误而不是泄漏 Python 堆栈。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "task.md"
        task.write_text("# Task\n", encoding="utf-8")
        output = root / "out.md"
        invalid_cli = root / "invalid-cli"
        invalid_cli.write_text("not an executable\n", encoding="utf-8")

        env = dict(os.environ)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ND_TASK_TYPE": "analysis",
                "ND_PROJECT_DIR": str(project),
                "ND_TASK_PACK": str(task),
                "ND_EXPECTED_OUTPUT": str(output),
                "ND_CHAPTER_SEQ": "1",
                "ND_AGY_BIN": str(invalid_cli),
            }
        )
        result = subprocess.run(
            [sys.executable, str(WORKER), "--provider", "agy"],
            cwd=str(project),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        assert result.returncode == 1
        assert "provider 启动失败: agy" in result.stdout
        assert "Traceback" not in result.stdout


def test_claude_provider_is_not_supported():
    result, output_text, payload = run_worker("claude", "ND_CLAUDE_BIN")

    assert result.returncode != 0
    assert output_text == ""
    assert payload == {}
    assert "invalid choice" in result.stdout
