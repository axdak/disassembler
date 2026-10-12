"""CLI 失败终局判据的离线回归测试。

背景（2026-10-12 实测）：模型请求在连接层失败（ECONNRESET / TLS 握手被中断）后，
CLI 会写下 ``FinalStop ... reason=failed``，随后既不重试也不退出——stdout 不再产生
任何流事件，进程只留内存心跳。旧行为只能靠 idle 判据兜底，白等 915 秒才重试。

本判据的语义：一旦识别到该终局，立即终止本 provider 并返回 124（可重试）。
正常结束（reason=completed）绝不能触发。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
import run_pipeline as pipeline
from provider_stream import CodeBuddyFailureLog

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "scripts", ROOT / "tools", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import agent_worker  # noqa: E402


FAILED_LINE = (
    "[2026/10/12 22:20:32.833] [Info] [pid=65312] [FinalStop] "
    "executeFinalStopHooks entry sessionId=s1 reason=failed\n"
)
COMPLETED_LINE = (
    "[2026/10/12 22:21:04.659] [Info] [pid=52440] [FinalStop] "
    "executeFinalStopHooks entry sessionId=s2 reason=completed\n"
)


def make_session_log(tmp_path: Path, name: str = "nd_worker_abc"):
    """造出与真实布局一致的目录：<config>/logs/<date>/<worker>__*.log。

    返回值中的 logs 是**根目录**（不含日期），与 CodeBuddyFailureLog 的参数语义一致。
    """
    logs = tmp_path / "codebuddy" / "logs"
    day = logs / datetime.now().strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    return project, logs, day / f"{name}__hash.log"


# ---- 判据本身 ----

def test_completed_session_is_not_a_failure(tmp_path):
    """正常结束写 reason=completed，绝不能被判成失败。"""
    project, logs, log = make_session_log(tmp_path)
    log.write_text(COMPLETED_LINE, encoding="utf-8")
    assert CodeBuddyFailureLog(project, logs).check() is False


def test_failed_session_is_detected(tmp_path):
    project, logs, log = make_session_log(tmp_path)
    log.write_text(COMPLETED_LINE, encoding="utf-8")
    probe = CodeBuddyFailureLog(project, logs)
    assert probe.check() is False
    with log.open("a", encoding="utf-8") as handle:
        handle.write(FAILED_LINE)
    assert probe.check() is True


def test_unfinished_line_is_not_judged(tmp_path):
    """写到一半的行必须留到下一次 tick，不能凭半行内容下结论。"""
    project, logs, log = make_session_log(tmp_path)
    log.write_bytes(b"[x] [FinalStop] executeFinalStopHooks entry reason=failed")
    assert CodeBuddyFailureLog(project, logs).check() is False
    log.write_bytes(b"[x] [FinalStop] executeFinalStopHooks entry reason=failed\n")
    assert CodeBuddyFailureLog(project, logs).check() is True


def test_missing_log_directory_is_harmless(tmp_path):
    """日志目录不存在时安静退回（非 Windows / CLI 换布局都不能影响主流程）。"""
    project = tmp_path / "nd_worker_abc"
    project.mkdir()
    assert CodeBuddyFailureLog(project, tmp_path / "nowhere").check() is False


# ---- 端到端：不必再等满 idle 窗口 ----

def write_fake_cli(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_failed_cli_is_released_without_waiting_for_idle(tmp_path, monkeypatch):
    """CLI 失败终局后应立即收手，而不是白等整个 idle 窗口。"""
    monkeypatch.setattr(pipeline, "DEFAULT_WORKER_IDLE_SECONDS", 600)
    monkeypatch.setattr(pipeline, "MIN_WORKER_IDLE_SECONDS", 0.1)
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", str(tmp_path / "codebuddy"))

    project = tmp_path / "nd_worker_live"
    project.mkdir()
    fake_cli = tmp_path / "fake_cli.py"
    write_fake_cli(fake_cli, """
import json, os, sys, time
from datetime import datetime
from pathlib import Path

print(json.dumps({"_messageId": "req-1"}), flush=True)
logdir = Path(os.environ["CODEBUDDY_CONFIG_DIR"]) / "logs" / datetime.now().strftime("%Y-%m-%d")
logdir.mkdir(parents=True, exist_ok=True)
(logdir / (Path.cwd().name + "__fake.log")).write_text(
    "[x] [FinalStop] executeFinalStopHooks entry sessionId=s1 reason=failed\\n", encoding="utf-8")
time.sleep(300)
""")
    command = [sys.executable, str(fake_cli), "-p", "--output-format", "text",
               "--json-schema", json.dumps({"type": "object"})]

    started = time.monotonic()
    result = agent_worker.run_structured_stream(command, "prompt", project)
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert elapsed < 30, f"应在秒级收手，实际 {elapsed:.1f}s"
