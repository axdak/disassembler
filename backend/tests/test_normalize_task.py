"""NormalizeAdapter tests (writer).

The normalize adapter runs the preserving-style normalization on the
process story JSON (故事结构_增量.json) in place under the writer lock.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from task_adapters import NormalizeAdapter
from workbench import TaskContext


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
    _write_json(p / "原文拆解" / "_索引.json", {"chapters": [
        {"seq": 1, "filename": "第001章_测试.md", "title": "测试"}
    ]})
    (p / "原文拆解" / "第001章_测试.md").write_text(
        "# 第001章 测试\n原文内容\n", encoding="utf-8"
    )
    _write_json(p / "故事结构_增量.json", {
        "介绍": {"标题": "测试"},
        "角色集": [],
        "事件集": [],
    })
    return p


def test_normalize_adapter_rewrites_process_story_json(project):
    adapter = NormalizeAdapter()
    ctx = _make_context(project, {})
    result = adapter.run(ctx)

    # Result is a dict carrying the project revision snapshot.
    assert isinstance(result, dict)
    assert "revision" in result
    assert "chapter" in result["revision"]
    assert "process" in result["revision"]
    assert "final" in result["revision"]

    # The process story JSON is rewritten in place and still parses.
    story_path = project / "故事结构_增量.json"
    assert story_path.is_file()
    rewritten = json.loads(story_path.read_text(encoding="utf-8"))
    assert isinstance(rewritten, dict)
    # Normalization preserves the intro title.
    assert rewritten["介绍"]["标题"] == "测试"
