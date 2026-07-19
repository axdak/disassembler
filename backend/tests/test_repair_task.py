"""RepairAdapter (writer) tests: repair syntactically broken chapter Delta JSON.

The adapter walks ``chapter_files`` → ``artifact_paths`` → ``p["delta"]`` and
repairs only the deltas that fail to parse as strict JSON, via the engine's
``repair_llm_json`` helper. Already-valid deltas are left untouched.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from task_adapters import RepairAdapter
from workbench import TaskContext


BROKEN_DELTA = '{"章节": "第001章", "新增元素": '  # truncated JSON
VALID_DELTA = {
    "章节": "第001章",
    "新增元素": [],
    "更新元素": [],
    "删除元素": [],
}


def _progress(*args, **kwargs):
    pass


def _make_context(project_path: Path, params: dict) -> TaskContext:
    return TaskContext(
        task_id=1,
        project_id=1,
        project_path=Path(project_path),
        params=params,
        cancellation_event=threading.Event(),
        _progress_callback=_progress,
    )


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "novel"
    (p / "原文拆解").mkdir(parents=True)
    (p / "章节处理").mkdir(parents=True)
    _write_json(p / "原文拆解" / "_索引.json", {"chapters": [
        {"seq": 1, "filename": "第001章_测试.md", "title": "测试"}
    ]})
    (p / "原文拆解" / "第001章_测试.md").write_text(
        "# 第001章 测试\n原文内容\n", encoding="utf-8"
    )
    _write_json(p / "故事结构_增量.json", {"介绍": {"标题": "测试"}, "角色集": []})
    return p


def test_repair_adapter_fixes_broken_delta(project):
    delta = project / "章节处理" / "第001章_测试.json"
    delta.write_text(BROKEN_DELTA, encoding="utf-8")
    original_bytes = delta.read_bytes()

    adapter = RepairAdapter()
    ctx = _make_context(project, {"chapters": "all"})
    result = adapter.run(ctx)

    # The delta now parses as strict JSON and retains its 章节 field.
    data = json.loads(delta.read_text(encoding="utf-8"))
    assert data["章节"] == "第001章"

    # The repair actually changed the file (it was broken before).
    assert delta.read_bytes() != original_bytes

    # Result reports one repaired delta and its report file.
    assert result["manifest"]["repaired"] == 1
    assert len(result["manifest"]["reports"]) == 1
    assert Path(result["manifest"]["reports"][0]).is_file()
    assert str(delta) in result["artifacts"]


def test_repair_adapter_skips_valid_delta(project):
    delta = project / "章节处理" / "第001章_测试.json"
    _write_json(delta, VALID_DELTA)
    original_text = delta.read_text(encoding="utf-8")

    adapter = RepairAdapter()
    ctx = _make_context(project, {"chapters": "all"})
    result = adapter.run(ctx)

    # The valid delta is untouched: same bytes, still parses.
    assert delta.read_text(encoding="utf-8") == original_text
    data = json.loads(delta.read_text(encoding="utf-8"))
    assert data["章节"] == "第001章"

    # Nothing was repaired.
    assert result["manifest"]["repaired"] == 0
    assert result["manifest"]["reports"] == []
    assert result["artifacts"] == []
