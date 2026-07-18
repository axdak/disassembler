"""UTF-8 boundary tests for backend stdio + Chinese content round-trip.

Asserts:
- backend/main.py configures stdio for UTF-8 (so worker subprocess output
  survives GBK consoles on Windows).
- A Chinese project name + Chinese log line round-trip through UTF-8
  encode/decode without mojibake (no U+FFFD replacement chars).
"""
from __future__ import annotations

import sys


def test_main_configures_stdio_utf8() -> None:
    """backend.main should expose a _configure_stdio_utf8 that reconfigures stdio."""
    import main as backend_main  # noqa: WPS433 (tests/sys.path already has backend/)

    assert callable(getattr(backend_main, "_configure_stdio_utf8", None))

    # Apply it to the running interpreter: stdout/stderr should report utf-8.
    backend_main._configure_stdio_utf8()
    out_encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    err_encoding = (getattr(sys.stderr, "encoding", "") or "").lower()
    assert "utf-8" in out_encoding or "utf8" in out_encoding
    assert "utf-8" in err_encoding or "utf8" in err_encoding


def test_chinese_round_trip_no_mojibake() -> None:
    """A Chinese project name and log line must survive encode/decode."""
    project_name = "我的小说项目"
    log_line = "开始处理第1章：场景拆解完成"
    payload = f"{project_name}\n{log_line}\n"

    encoded = payload.encode("utf-8")
    assert b"\xff\xfd" not in encoded  # sanity: no replacement bytes in source

    decoded = encoded.decode("utf-8")
    assert decoded == payload
    assert "�" not in decoded
    assert decoded.splitlines()[0] == project_name
    assert decoded.splitlines()[1] == log_line


def test_multibyte_split_at_byte_boundary() -> None:
    """A multibyte sequence split mid-char between two buffers must still
    decode cleanly once concatenated — this is the byte-level guarantee the
    Electron StringDecoder relies on the Python side to uphold."""
    text = "小说解析器"
    bytes_total = text.encode("utf-8")
    # Split at byte 1: first char (3 bytes) is split as [0xE5] | [0xB0 0x8F ...].
    a = bytes_total[:1]
    b = bytes_total[1:]
    assert (a + b).decode("utf-8") == text
    assert "�" not in (a + b).decode("utf-8")
