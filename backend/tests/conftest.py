import sys
from pathlib import Path
import pytest

# 让 backend/ 下的模块可被 import
backend = Path(__file__).resolve().parent.parent
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))


@pytest.fixture(autouse=True)
def prevent_live_model_workers(monkeypatch):
    """Tests may run local fake workers, but must never consume a live model."""
    import run_pipeline
    original = run_pipeline.stream_shell_command

    def offline_only(command, *args, **kwargs):
        if "agent_worker.py" in command:
            raise RuntimeError("离线测试禁止调用真实模型；请显式提供本地假 worker")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(run_pipeline, "stream_shell_command", offline_only)
