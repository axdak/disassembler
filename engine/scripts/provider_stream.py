"""Read request-scoped capacity metadata without exposing model reasoning.

CodeBuddy 2.148 converts provider ``length`` into ``end_turn`` in stream-json.
Its local diagnostic log retains the true reason. Only newly appended lines
whose request IDs were observed on this worker's stream may affect this worker.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


# CLI 会话日志里的终局标记：正常一轮结束写 reason=completed，
# 只有连接层失败或会话中止才会写成 reason=failed。
FAILURE_TERMINAL = b"executeFinalStopHooks entry"
FAILURE_REASON = b"reason=failed"


def codebuddy_log_dir(logs: Path | None = None) -> Path:
    """CLI 本地诊断日志根目录；测试与自定义布局可通过 CODEBUDDY_CONFIG_DIR 覆盖。"""
    if logs is not None:
        return logs
    home = Path(os.environ.get("CODEBUDDY_CONFIG_DIR") or Path.home() / ".codebuddy")
    return home / "logs"


def worker_session_logs(prefix: str, logs: Path) -> list[Path]:
    """当天、文件名以 prefix（形如 ``nd_worker_xxx__``）开头的 CLI 会话日志。"""
    directory = codebuddy_log_dir(logs) / datetime.now().strftime("%Y-%m-%d")
    try:
        return [p for p in directory.iterdir() if p.name.startswith(prefix) and p.suffix == ".log"]
    except OSError:
        return []


class CodeBuddyCapacityLog:
    def __init__(self, project: Path, logs: Path | None = None):
        self.logs = codebuddy_log_dir(logs)
        self.prefix = project.name + "__"
        self.offsets = {}
        for path in self.paths():
            try:
                self.offsets[path] = path.stat().st_size
            except OSError:
                pass  # The CLI may rotate a log between listing and stat.
        self.exhausted = set()

    def paths(self):
        return worker_session_logs(self.prefix, self.logs)

    def check(self, request_ids):
        for path in self.paths():
            try:
                offset = self.offsets.get(path, 0)
                if path.stat().st_size < offset:
                    offset = 0  # Rotated log; still require this invocation's ID.
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break  # Revisit an unfinished write on the next event.
                        offset += len(line)
                        if b'finish_reason="length"' in line:
                            match = re.search(rb"requestId=([A-Za-z0-9_-]+)", line)
                            if match:
                                self.exhausted.add(match[1].decode("ascii"))
                self.offsets[path] = offset
            except OSError:
                continue  # Stream protocol remains the primary signal.
        return bool(self.exhausted.intersection(request_ids))


class CodeBuddyFailureLog:
    """检测 CLI 会话是否已经以失败终局。

    连接层失败（ECONNRESET / TLS 握手被中断）时 CLI 会写下
    ``FinalStop ... reason=failed``，随后既不重试也不退出：stdout 不会再有流事件，
    进程只留内存心跳。此时等下去只能靠 idle 判据兜底，白耗一整个窗口；识别该信号
    即可立刻收手并交由上层重试。正常结束写的是 ``reason=completed``，不会触发。
    """

    def __init__(self, project: Path, logs: Path | None = None):
        self.logs = codebuddy_log_dir(logs)
        self.prefix = project.name + "__"
        self.offsets = {}

    def check(self) -> bool:
        for path in worker_session_logs(self.prefix, self.logs):
            try:
                if path not in self.offsets:
                    # 本 worker 的会话日志由本次 CLI 进程新建，id 唯一，可从头读。
                    self.offsets[path] = 0
                offset = self.offsets[path]
                if path.stat().st_size < offset:
                    offset = 0
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break  # Revisit an unfinished write on the next tick.
                        offset += len(line)
                        if FAILURE_TERMINAL in line and FAILURE_REASON in line:
                            self.offsets[path] = offset
                            return True
                self.offsets[path] = offset
            except OSError:
                continue  # 会话日志只是加速判据，读不到就退回 idle/墙钟。
        return False
