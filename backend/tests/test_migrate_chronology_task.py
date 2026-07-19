"""MigrateChronologyAdapter task adapter test.

Asserts the writer adapter:
- calls engine ``run_migrate`` in-process (no subprocess),
- produces the ``.before_chronology_migration.bak`` backup next to the
  process story JSON,
- emits the chronology migration report under 质量治理/规范化/, and
- returns a ``_result_json`` dict with a populated ``revision``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from task_adapters import MigrateChronologyAdapter
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
    # Minimal seed: normalize_structure fills defaults for every section.
    _write_json(p / "故事结构_增量.json", {
        "介绍": {"标题": "测试"},
        "角色集": [],
        "事件集": [],
    })
    return p


def test_migrate_chronology_adapter_runs_and_produces_backup_and_report(project):
    adapter = MigrateChronologyAdapter()
    ctx = _make_context(project, {})
    result = adapter.run(ctx)

    # Standard adapter result shape with a populated revision.
    assert isinstance(result, dict)
    assert "revision" in result
    assert "chapter" in result["revision"]
    assert "process" in result["revision"]

    # A .before_chronology_migration.bak backup is produced next to the story.
    backup = project / "故事结构_增量.json.before_chronology_migration.bak"
    assert backup.is_file()

    # The migration report lands at the canonical governance path.
    report = project / "质量治理" / "规范化" / "chronology_migration.txt"
    assert report.is_file()
    # The adapter surfaces the report as an artifact (list of path strings).
    assert str(report) in result["artifacts"]

    # The migrated story JSON was rewritten (now normalized/migrated).
    migrated = json.loads(
        (project / "故事结构_增量.json").read_text(encoding="utf-8")
    )
    assert "事件集" in migrated
