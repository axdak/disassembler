#!/usr/bin/env python3
"""后端入口：随机空闲端口 + 随机 session token，启动 uvicorn。

打印 `BACKEND_READY host=<h> port=<p> token=<t>` 到 stdout，供 Electron 主进程解析。
数据库默认写入 %APPDATA%/NovelDisassemblerDesktop/app.db。
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

import uvicorn

from app import create_app


def _configure_stdio_utf8() -> None:
    """Force stdio to UTF-8 so worker subprocess output survives the parent
    pipeline on Windows consoles that default to GBK (code page 936).
    PYTHONUTF8 / PYTHONIOENCODING handle most cases; this is belt-and-suspenders
    for interpreters started before those env vars take effect on stdio buffers.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (TypeError, ValueError, OSError):
                pass


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _default_db_path() -> str:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return str(Path(appdata) / "NovelDisassemblerDesktop" / "app.db")


def main(argv=None) -> int:
    _configure_stdio_utf8()
    parser = argparse.ArgumentParser(description="Novel Disassembler Desktop 后端")
    parser.add_argument("--db", default="", help="SQLite 路径，默认 %APPDATA%/NovelDisassemblerDesktop/app.db")
    parser.add_argument("--port", type=int, default=0, help="端口，0=随机空闲端口")
    parser.add_argument("--token", default="", help="session token，默认随机生成")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    port = args.port or _free_port()
    token = args.token or secrets.token_urlsafe(32)
    db_path = args.db or _default_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    app = create_app(db_path, token)
    print(f"BACKEND_READY host={args.host} port={port} token={token}", flush=True)
    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
