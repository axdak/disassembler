import io
import sys
import tempfile
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "scripts"
WORKER = Path(__file__).resolve().parent.parent / "tools" / "agent_worker.py"
sys.path.insert(0, str(ROOT))

from run_pipeline import DEFAULT_WORKER_LOOP, DEFAULT_WORKER_WINDOW, run_worker, worker_supervision_checkpoint  # noqa: E402


def test_default_worker_window_is_hidden():
    assert DEFAULT_WORKER_WINDOW == "hidden"


def test_default_worker_loop_is_supervised():
    assert DEFAULT_WORKER_LOOP == "supervised"


def test_worker_supervision_checkpoint_prints_foreground_rerun_contract():
    captured = io.StringIO()

    with redirect_stdout(captured):
        should_stop = worker_supervision_checkpoint(
            "supervised",
            "analysis artifact accepted",
            "python scripts/run_pipeline.py run C:/book --run-mode worker --worker-provider agy",
        )

    output = captured.getvalue()
    assert should_stop is True
    assert "WORKER_SUPERVISION_CHECKPOINT" in output
    assert "not completion" in output
    assert "Do not wait for task-notification" in output
    assert "NEXT_FOREGROUND_COMMAND:" in output
    assert "python scripts/run_pipeline.py run C:/book --run-mode worker --worker-provider agy" in output


def test_run_worker_streams_external_output_to_terminal():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task_pack = root / "task.md"
        task_pack.write_text("# task\n", encoding="utf-8")
        expected_output = root / "out.md"
        worker = root / "worker.py"
        worker.write_text(
            """
import os
from pathlib import Path

print("VISIBLE_WORKER_PROGRESS")
Path(os.environ["ND_EXPECTED_OUTPUT"]).write_text("# output\\n", encoding="utf-8")
print("VISIBLE_WORKER_DONE")
""",
            encoding="utf-8",
        )

        captured = io.StringIO()
        with redirect_stdout(captured):
            rc = run_worker(
                f'"{sys.executable}" "{worker}"',
                project,
                "analysis",
                task_pack,
                expected_output,
                timeout=10,
                worker_window="hidden",
            )

        assert rc == 0
        displayed = captured.getvalue()
        assert "VISIBLE_WORKER_PROGRESS" in displayed
        assert "VISIBLE_WORKER_DONE" in displayed


def test_agent_worker_forwards_codebuddy_stderr_to_terminal():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task_pack = root / "task.md"
        task_pack.write_text("# task\n", encoding="utf-8")
        expected_output = root / "out.md"
        fake_codebuddy = root / "fake_codebuddy.py"
        fake_codebuddy.write_text(
            """
import sys

sys.stdin.read()
print("VISIBLE_PROVIDER_PROGRESS", file=sys.stderr)
print("# generated")
""",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ND_TASK_TYPE": "analysis",
                "ND_PROJECT_DIR": str(project),
                "ND_TASK_PACK": str(task_pack),
                "ND_EXPECTED_OUTPUT": str(expected_output),
                "ND_CHAPTER_SEQ": "1",
                "ND_CODEBUDDY_BIN": f"{sys.executable} {fake_codebuddy}",
            }
        )

        result = subprocess.run(
            [sys.executable, str(WORKER), "--provider", "codebuddy"],
            cwd=str(project),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        assert result.returncode == 0
        assert "VISIBLE_PROVIDER_PROGRESS" in result.stdout
        assert expected_output.read_text(encoding="utf-8") == "# generated\n"
