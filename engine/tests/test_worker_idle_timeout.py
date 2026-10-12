"""无进展（idle）超时判据的离线回归测试。

判据语义：worker 连续 idle_limit 秒没有任何输出即终止并返回 124（可重试），
且诊断必须与墙钟超时区分。测试全部用秒级数值 + monkeypatch，不做真实长等待。
"""
import os
import sys
import time
from pathlib import Path

import pytest
import run_pipeline as pipeline


@pytest.fixture(autouse=True)
def fast_idle_thresholds(monkeypatch):
    """把默认上限与下限都调到秒级，避免测试真的等 900 秒。"""
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 2)
    monkeypatch.setattr(pipeline, "MIN_WORKER_IDLE_SECONDS", 0.1)


def helper_command(tmp_path: Path, body: str) -> str:
    """把脚本写到磁盘再用解释器调用，避免 shell 引号转义带来的假失败。"""
    script = tmp_path / "helper.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def run_stream(command: str, tmp_path: Path, timeout: float = 120.0):
    return pipeline.stream_shell_command(command, tmp_path, dict(os.environ), timeout)


# ---- 判据本身 ----

def test_idle_limit_defaults_to_cap_and_floor(monkeypatch):
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 900)
    monkeypatch.setattr(pipeline, "MIN_WORKER_IDLE_SECONDS", 60.0)
    assert pipeline.worker_idle_limit(1200) == 900   # 低于墙钟，保留默认值
    assert pipeline.worker_idle_limit(120) == 120    # 不超过墙钟
    assert pipeline.worker_idle_limit(0) == 900      # 未提供墙钟时按默认值


def test_tiny_configuration_is_raised_to_floor(monkeypatch):
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 5)
    monkeypatch.setattr(pipeline, "MIN_WORKER_IDLE_SECONDS", 60.0)
    assert pipeline.worker_idle_limit() == 60.0


def test_idle_can_be_disabled(monkeypatch):
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 0)
    assert pipeline.worker_idle_limit(1200) == 0.0


# ---- 真实子进程行为 ----

def test_silent_worker_is_killed_long_before_wall_timeout(tmp_path):
    """零输出即无进展：应在上限处终止，而不是等到墙钟。"""
    command = helper_command(tmp_path, "import time\ntime.sleep(120)\n")
    started = time.monotonic()
    rc, output = run_stream(command, tmp_path, timeout=120)
    elapsed = time.monotonic() - started
    assert rc == 124
    assert elapsed < 30, f"应在秒级被终止，实际 {elapsed:.1f}s"
    assert "无进展" in output
    assert "命令执行超时" not in output, "这两种终止必须能区分"
    assert "最长无输出窗口" in output


def test_progressing_worker_is_never_killed(tmp_path):
    """持续有输出就是有进展：不得被无进展判据误杀。"""
    command = helper_command(
        tmp_path,
        "import time\nfor index in range(6):\n    print('tick', index, flush=True)\n    time.sleep(0.5)\n",
    )
    rc, output = run_stream(command, tmp_path, timeout=60)
    assert rc == 0
    assert "tick 5" in output
    assert "无进展" not in output


def test_wall_timeout_still_reported_when_idle_disabled(tmp_path, monkeypatch):
    """关闭无进展判据后，墙钟超时行为必须保持原样。"""
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 0)
    command = helper_command(tmp_path, "import time\ntime.sleep(120)\n")
    rc, output = run_stream(command, tmp_path, timeout=1)
    assert rc == 124
    assert "命令执行超时" in output
    assert "无进展" not in output


def test_silent_then_progressing_worker_survives(tmp_path):
    """先沉默后恢复输出的 worker 属于有进展，不应被提前判死。"""
    command = helper_command(
        tmp_path,
        "import time\n"
        "time.sleep(1)\n"
        "for index in range(6):\n"
        "    print('late', index, flush=True)\n"
        "    time.sleep(0.5)\n",
    )
    rc, output = run_stream(command, tmp_path, timeout=60)
    assert rc == 0
    assert "late 5" in output
