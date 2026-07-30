"""Tests for the recover_story snapshot adapter (GF1).

These tests stay light on purpose: the full happy-path replay (schema-valid
delta + source chapter replayed via replay_one_delta) is deferred to GF8
integration with a realistic seed. Here we only assert the read-only snapshot
listing helper and the adapter's param-validation guards.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from task_adapters import RecoverStoryAdapter, list_recover_snapshots
from workbench import TaskContext


SAMPLE_STORY = {
    "介绍": {"标题": "火星", "描述": "恢复测试故事结构。"},
    "角色集": [],
    "事件集": [],
    "地点集": [],
    "线索集": [],
    "阵营集": [],
    "物品集": [],
    "其他事项集": [],
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
    (p / "章节处理" / "Delta").mkdir(parents=True, exist_ok=True)
    _write_json(p / "原文拆解" / "_索引.json", {"chapters": [
        {"seq": 1, "filename": "第001章_测试.md", "title": "测试"}
    ]})
    (p / "原文拆解" / "第001章_测试.md").write_text(
        "# 第001章 测试\n原文\n", encoding="utf-8"
        )
    _write_json(p / "故事结构_增量.json", {})
    _write_json(p / "故事结构版本" / "story_after_ch001.json", SAMPLE_STORY)
    # A chapter-2 delta file makes max_available_delta_seq() return 2.
    (p / "章节处理" / "Delta" / "第002章_测试.json").write_text("{}", encoding="utf-8")
    return p


# --- list_recover_snapshots -------------------------------------------------

def test_list_recover_snapshots_is_readonly_and_reports_seq(project):
    process_path = project / "故事结构_增量.json"
    before_bytes = process_path.read_bytes()

    snaps = list_recover_snapshots(project)

    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["snapshot_seq"] == 1
    assert snap["path"].replace("\\", "/").endswith("故事结构版本/story_after_ch001.json")
    assert snap["replayable_to"] == 2
    assert snap["size"] > 0

    # The helper is read-only: the process JSON bytes are unchanged.
    assert process_path.read_bytes() == before_bytes


# --- RecoverStoryAdapter guards --------------------------------------------

def test_recover_story_adapter_rejects_missing_snapshot(project):
    adapter = RecoverStoryAdapter()
    ctx = _make_context(project, {"snapshot_seq": 999})
    with pytest.raises(RuntimeError, match="snapshot seq 999 未找到"):
        adapter.run(ctx)


def test_recover_story_adapter_rejects_zero_seq(project):
    adapter = RecoverStoryAdapter()
    with pytest.raises(ValueError, match="snapshot_seq"):
        adapter.run(_make_context(project, {}))
    with pytest.raises(ValueError, match="snapshot_seq"):
        adapter.run(_make_context(project, {"snapshot_seq": 0}))
