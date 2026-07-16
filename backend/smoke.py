#!/usr/bin/env python3
"""一次性冒烟：启动真实后端，验证 BACKEND_READY + /health + 鉴权。"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# 绕过环境代理对 127.0.0.1 的拦截（部分环境会返回 502）
import os
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

ROOT = Path(__file__).resolve().parent
PY = str(ROOT.parent / "runtime" / "venv" / "Scripts" / "python.exe")
MAIN = str(ROOT / "main.py")
DB = str(Path(tempfile.mkdtemp()) / "app.db")

proc = subprocess.Popen(
    [PY, MAIN, "--db", DB],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
)
try:
    line = proc.stdout.readline()
    m = re.search(r"BACKEND_READY host=(\S+) port=(\d+) token=(\S+)", line)
    if not m:
        print("FAIL: no BACKEND_READY:", line.strip())
        print(proc.stdout.read())
        sys.exit(1)
    host, port, token = m.group(1), int(m.group(2)), m.group(3)
    base = f"http://{host}:{port}"
    # uvicorn 需要一点时间起服；轮询 /health
    ok = False
    for _ in range(40):
        try:
            r = httpx.get(base + "/health", timeout=2)
            if r.status_code == 200 and r.json() == {"ok": True}:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.25)
    if not ok:
        print("FAIL: /health never ready")
        sys.exit(1)
    assert httpx.get(base + "/projects", timeout=5).status_code == 401
    r = httpx.get(base + "/projects", headers={"Authorization": f"Bearer {token}"}, timeout=5)
    assert r.status_code == 200, r.text
    print(f"SMOKE OK host={host} port={port} (token len={len(token)})")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
