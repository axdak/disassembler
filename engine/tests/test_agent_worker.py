import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tools" / "agent_worker.py"


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
    "task_pack": os.environ.get("ND_TASK_PACK", ""),
    "expected_output": os.environ.get("ND_EXPECTED_OUTPUT", ""),
}, ensure_ascii=False, indent=2), encoding="utf-8")
if os.environ.get("FAKE_CLI_DIRECT_WRITE") == "1":
    output = Path(os.environ["ND_EXPECTED_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("# generated direct artifact\\n" + os.environ.get("ND_TASK_TYPE", "") + "\\n", encoding="utf-8")
    raise SystemExit(0)
print("# generated artifact")
print(os.environ.get("ND_TASK_TYPE", ""))
""",
        encoding="utf-8",
    )


def run_worker(provider: str, provider_env: str, extra_env=None, task_text: str | None = None):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "task.md"
        task.write_text(task_text or "# Task\nWrite the target file.\n", encoding="utf-8")
        output = root / "out.md"
        fake_cli = root / "fake_cli.py"
        record = root / "record.json"
        write_fake_cli(fake_cli)

        env = dict(os.environ)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ND_TASK_TYPE": "analysis",
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


def test_auto_provider_selects_first_available_cli():
    result, output_text, payload = run_worker(
        "auto",
        "ND_AGY_BIN",
        {"ND_AGENT_PROVIDER_ORDER": "agy", "FAKE_CLI_DIRECT_WRITE": "1"},
    )

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated direct artifact")
    assert "-p" in payload["argv"] or "--print" in payload["argv"]
    assert "selected provider: agy" in result.stdout


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
    assert "ignored unsupported NODE_OPTIONS" in result.stdout


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
        assert "selected provider: agy" in result.stdout
        assert "selected provider: codebuddy" in result.stdout
        assert "-p" in payload["argv"]


def test_claude_provider_is_not_supported():
    result, output_text, payload = run_worker("claude", "ND_CLAUDE_BIN")

    assert result.returncode != 0
    assert output_text == ""
    assert payload == {}
    assert "invalid choice" in result.stdout
