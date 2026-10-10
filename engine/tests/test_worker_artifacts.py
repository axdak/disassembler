import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from worker_artifacts import confined_project_path, invalid_output_artifacts  # noqa: E402


def test_confined_project_path_accepts_project_child(tmp_path):
    assert confined_project_path(tmp_path, "全书分析/结果.md") == (
        tmp_path / "全书分析" / "结果.md"
    ).resolve()


def test_confined_project_path_rejects_parent_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes project"):
        confined_project_path(tmp_path, "../outside.json")


def test_invalid_output_artifacts_validates_json_syntax(tmp_path):
    valid = tmp_path / "valid.json"
    invalid = tmp_path / "invalid.json"
    array = tmp_path / "array.json"
    markdown = tmp_path / "report.md"
    valid.write_text(json.dumps({"ok": True}), encoding="utf-8")
    invalid.write_text("{broken", encoding="utf-8")
    array.write_text(json.dumps(["修改元素"], ensure_ascii=False), encoding="utf-8")
    markdown.write_text("# result\n", encoding="utf-8")

    assert invalid_output_artifacts([valid, invalid, array, markdown]) == [invalid, array]
