"""Repository engine validation must match the current packaged layout."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_SCRIPTS = ROOT / "engine" / "scripts"
if str(ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ENGINE_SCRIPTS))


def test_validate_engine_accepts_current_packaged_layout():
    """Removing the retired references tree must not fail runtime validation."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "validate_engine.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "校验通过" in result.stdout
