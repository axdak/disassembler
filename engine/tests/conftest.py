import sys
from pathlib import Path
import pytest

# Add scripts directory to sys.path so tests can import modules from it
project_root = Path(__file__).resolve().parent.parent
scripts_dir = project_root / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))


@pytest.fixture(autouse=True)
def prevent_live_model_workers(monkeypatch, local_provider_commands):
    """Keep regression suites offline even when a new recovery branch is added."""
    import run_pipeline
    original = run_pipeline.stream_shell_command

    def offline_only(command, *args, **kwargs):
        env = args[1] if len(args) > 1 else kwargs.get("env", {})
        if "agent_worker.py" in command and env.get("ND_AGY_BIN") not in local_provider_commands:
            raise RuntimeError("离线测试禁止调用真实模型；请显式提供本地假 worker")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(run_pipeline, "stream_shell_command", offline_only)


@pytest.fixture
def local_provider_commands():
    """Explicit allowlist for tests exercising the wrapper with a local fake CLI."""
    return set()
