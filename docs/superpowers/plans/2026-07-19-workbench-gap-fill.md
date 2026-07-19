# Workbench Gap-Fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the 4 remaining engine capabilities (snapshot recovery, chronology migration, on-demand finalization, normalize, repair) as workbench writer tasks behind a new "维护" tab, and document the `custom` dimension's intentional exclusion from the auto pipeline.

**Architecture:** Add 5 new `TaskAdapter` writer types to `backend/task_adapters.py` that call existing engine functions (no prompt/schema/merge/validation duplication); register them in `create_app` and `WRITER_TASK_TYPES`; add a read-only `GET /recover/snapshots` preview endpoint for recover-story; extend `artifact_status` to treat the new maintenance types as no-card (they affect staleness via revisions, like append/governance); add a 7th React tab `维护` with 5 actions. JSON stays authoritative; `current_stage` semantics untouched.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, pytest; Electron 31, React 18, TypeScript 5.5, Vitest, Testing Library.

## Global Constraints

- Do not rewrite existing prompts, schemas, deterministic merge logic, or validators. Adapters call existing engine functions only.
- JSON remains authoritative; Markdown/Obsidian are read-only mirrors.
- Never run two project-fact JSON writers for the same project concurrently; the 5 new task types are writers (added to `WRITER_TASK_TYPES`).
- Workbench task states are exactly `queued`, `running`, `done`, `failed`, `cancelled`, `interrupted`.
- Do not change `current_stage` semantics or the automatic pipeline state machine.
- `custom` analysis dimension stays workbench-only and is documented as by-design excluded from `FULLBOOK_TASKS` (no code change for `custom`).
- Python interpreter: `runtime\venv\Scripts\python.exe` from repo root; `cd engine` + `..\runtime\venv\Scripts\python.exe` for engine tests; `cd desktop` for npm.
- TDD per task: failing test first → verify RED → implement → verify GREEN → commit.
- Preserve the four pre-existing fixes in `backend/app.py`, `desktop/src/main/index.ts`, `engine/scripts/run_pipeline.py`, `engine/tools/agent_worker.py`.

---

### Task 1: recover_story Adapter and Snapshot Preview Endpoint

**Files:**
- Modify: `backend/task_adapters.py` (add `RecoverStoryAdapter` + `list_recover_snapshots`)
- Modify: `backend/app.py` (add `GET /projects/{pid}/recover/snapshots`)
- Modify: `backend/models.py` (add `RecoverSnapshot` DTO)
- Create: `backend/tests/test_recover_story_task.py`

**Interfaces:**
- Consumes: `recover_story_from_snapshot(project_dir: Path, snapshot_seq: Optional[int] = None, to_seq: Optional[int] = None) -> int`, `latest_snapshot_seq(project_dir: Path) -> Optional[int]`, `max_available_delta_seq(project_dir: Path) -> int` (all in `engine/scripts/run_pipeline.py`); `_result_json`, `_str_param`, `_int_param` from `backend/task_adapters.py`; `TaskContext` from `backend/workbench.py`.
- Produces: `RecoverStoryAdapter` (writer, no `prepare`); `list_recover_snapshots(project_dir: Path) -> list[dict]`; `GET /projects/{pid}/recover/snapshots`.

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_recover_story_task.py`:

```python
#!/usr/bin/env python3
"""recover_story 适配器与快照预览端点测试。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "engine" / "scripts"
sys.path.insert(0, str(ROOT))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from task_adapters import RecoverStoryAdapter, list_recover_snapshots
from run_pipeline import recover_story_from_snapshot
from workbench import TaskContext
import threading


def _seed_project(tmp: Path) -> Path:
    project = tmp / "拆书_测试"
    sdir = project / "原文拆解"
    sdir.mkdir(parents=True)
    (sdir / "_索引.json").write_text(json.dumps({
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "source_title": "第001章"}],
        "pattern": "", "chapter_count": 1, "source_file": "book.txt",
    }, ensure_ascii=False), encoding="utf-8")
    (sdir / "第001章_测试.md").write_text("# 第001章\n正文。\n", encoding="utf-8")
    proc = project / "故事结构_增量.json"
    proc.write_text(json.dumps({"介绍": {"标题": "测试"}, "角色集": []}, ensure_ascii=False), encoding="utf-8")
    # One after-snapshot covering seq 1.
    (project / "story_after_ch001.json").write_text(
        json.dumps({"介绍": {"标题": "测试"}, "角色集": [{"名称": "主角"}]}, ensure_ascii=False), encoding="utf-8")
    return project


def test_list_recover_snapshots_is_readonly_and_reports_seq(tmp_path):
    project = _seed_project(tmp_path)
    snaps = list_recover_snapshots(project)
    assert isinstance(snaps, list) and len(snaps) == 1
    assert snaps[0]["snapshot_seq"] == 1
    # Read-only: process JSON unchanged.
    before = (project / "故事结构_增量.json").read_text(encoding="utf-8")
    list_recover_snapshots(project)
    after = (project / "故事结构_增量.json").read_text(encoding="utf-8")
    assert before == after


def test_recover_story_adapter_replays_and_records_revisions(tmp_path):
    project = _seed_project(tmp_path)
    ctx = TaskContext(
        task_id=1, project_id=1, project_path=project,
        params={"snapshot_seq": 1},
        cancellation_event=threading.Event(),
        _progress_callback=lambda *_: None,
    )
    result = RecoverStoryAdapter().run(ctx)
    assert isinstance(result, dict)
    assert "artifacts" in result and "revision" in result
    # The process JSON now reflects the snapshot's content (主角 present).
    proc = json.loads((project / "故事结构_增量.json").read_text(encoding="utf-8"))
    assert any(c.get("名称") == "主角" for c in proc.get("角色集", []))


def test_recover_story_adapter_rejects_missing_snapshot(tmp_path):
    project = _seed_project(tmp_path)
    ctx = TaskContext(
        task_id=1, project_id=1, project_path=project,
        params={"snapshot_seq": 999},
        cancellation_event=threading.Event(),
        _progress_callback=lambda *_: None,
    )
    try:
        RecoverStoryAdapter().run(ctx)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for missing snapshot")


if __name__ == "__main__":
    test_list_recover_snapshots_is_readonly_and_reports_seq(__import__("tempfile").mkdtemp())
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_recover_story_task.py -q`
Expected: FAIL — `ImportError: cannot import name 'RecoverStoryAdapter'` / `list_recover_snapshots`.

- [ ] **Step 3: Implement the helper and adapter**

In `backend/task_adapters.py`, after the existing helpers and imports, add:

```python
from run_pipeline import (
    recover_story_from_snapshot, latest_snapshot_seq, max_available_delta_seq,
)
```
(merge into the existing `from run_pipeline import (...)` block — do not duplicate imports already present).

Then add the helper and adapter:

```python
def list_recover_snapshots(project_dir: Path) -> list[dict]:
    """Read-only list of available story_after_chNNN snapshots for the recover UI."""
    project_dir = Path(project_dir)
    max_delta = max_available_delta_seq(project_dir)
    snaps: list[dict] = []
    for p in sorted(project_dir.glob("story_after_ch*.json")):
        seq = latest_snapshot_seq(project_dir.parent / p.name) if False else None
        # Derive seq from the filename (story_after_chNNN.json).
        stem = p.stem  # story_after_chNNN
        try:
            seq = int(stem.rsplit("ch", 1)[1])
        except (ValueError, IndexError):
            continue
        snaps.append({
            "snapshot_seq": seq,
            "path": str(p.relative_to(project_dir)) if p.is_relative_to(project_dir) else str(p),
            "size": p.stat().st_size,
            "replayable_to": max_delta,
        })
    return snaps


class RecoverStoryAdapter:
    """Writer adapter: roll the process library back to a story_after_chNNN
    snapshot and replay later deltas via recover_story_from_snapshot."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        snapshot_seq = _int_param(params, "snapshot_seq", 0)
        if snapshot_seq <= 0:
            raise ValueError("recover_story requires 'snapshot_seq' (a story_after_chNNN seq)")
        available = {s["snapshot_seq"] for s in list_recover_snapshots(project_dir)}
        if snapshot_seq not in available:
            raise RuntimeError(f"snapshot seq {snapshot_seq} not found in project")

        before = (project_dir / "故事结构_增量.json")
        before_bytes = before.read_bytes() if before.is_file() else b""
        context.progress(20, f"recovering to snapshot seq={snapshot_seq}")
        rc = recover_story_from_snapshot(project_dir, snapshot_seq=snapshot_seq)
        if rc != 0:
            # Restore before on failure.
            if before_bytes:
                before.write_bytes(before_bytes)
            raise RuntimeError(f"recover_story failed rc={rc}")
        context.progress(90, "recovery complete")
        return _result_json(
            artifacts=[str(before)],
            manifest={"snapshot_seq": snapshot_seq, "replayed_to": max_available_delta_seq(project_dir)},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
            snapshot_seq=snapshot_seq,
        )
```

(`project_revisions` is already imported at the top of `task_adapters.py`.)

- [ ] **Step 4: Add the preview endpoint and DTO**

In `backend/models.py`, add:

```python
class RecoverSnapshot(BaseModel):
    snapshot_seq: int
    path: str
    size: int
    replayable_to: int
```

In `backend/app.py`, after the append endpoints, add:

```python
@app.get(
    "/projects/{pid}/recover/snapshots",
    dependencies=[Depends(require_token)],
    response_model=list[RecoverSnapshot],
)
def recover_snapshots(pid: int):
    project = _project_or_404(pid)
    from task_adapters import list_recover_snapshots
    return list_recover_snapshots(Path(project["path"]))
```

Add `RecoverSnapshot` to the `from models import (...)` block in `app.py`.

- [ ] **Step 5: Run tests, verify GREEN**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_recover_story_task.py -q`
Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add backend/task_adapters.py backend/app.py backend/models.py backend/tests/test_recover_story_task.py
git commit -m "feat(workbench): add recover_story snapshot adapter and preview endpoint"
```

### Task 2: migrate_chronology Adapter

**Files:**
- Modify: `engine/scripts/migrate_chronology.py` (extract `run_migrate` from `main`)
- Modify: `backend/task_adapters.py` (add `MigrateChronologyAdapter`)
- Create: `backend/tests/test_migrate_chronology_task.py`

**Interfaces:**
- Consumes: `migrate_chronology.index_sequences(project_dir: Path) -> list[int]`, `migrate_chronology.migrate_story(data: Any) -> tuple[dict, list[str]]` (in `engine/scripts/migrate_chronology.py`); `_result_json`, `project_revisions`, `TaskContext`.
- Produces: `run_migrate(project_dir: Path, file: str = "故事结构_增量.json", dry_run: bool = False) -> int` (extracted from `main`); `MigrateChronologyAdapter` (writer).

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_migrate_chronology_task.py`:

```python
#!/usr/bin/env python3
"""migrate_chronology 适配器测试。"""
import json, sys, threading
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from task_adapters import MigrateChronologyAdapter
from workbench import TaskContext


def _seed(tmp: Path) -> Path:
    project = tmp / "拆书_测试"
    sdir = project / "原文拆解"
    sdir.mkdir(parents=True)
    (sdir / "_索引.json").write_text(json.dumps({
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "source_title": "第001章"}],
        "pattern": "", "chapter_count": 1, "source_file": "book.txt",
    }, ensure_ascii=False), encoding="utf-8")
    (sdir / "第001章_测试.md").write_text("# 第001章\n正文。\n", encoding="utf-8")
    (project / "故事结构_增量.json").write_text(json.dumps({
        "介绍": {"标题": "测试"}, "角色集": [], "事件集": [],
    }, ensure_ascii=False), encoding="utf-8")
    return project


def test_migrate_adapter_runs_and_writes_report(tmp_path):
    project = _seed(tmp_path)
    ctx = TaskContext(1, 1, project, {}, threading.Event(), lambda *_: None)
    result = MigrateChronologyAdapter().run(ctx)
    assert isinstance(result, dict) and "revision" in result
    # A backup and a report are produced.
    assert any(p.name.endswith(".before_chronology_migration.bak") for p in project.iterdir())
    assert (project / "质量治理" / "规范化" / "chronology_migration.txt").is_file()


if __name__ == "__main__":
    import tempfile
    test_migrate_adapter_runs_and_writes_report(Path(tempfile.mkdtemp()))
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_migrate_chronology_task.py -q`
Expected: FAIL — `ImportError: cannot import name 'MigrateChronologyAdapter'`.

- [ ] **Step 3: Extract run_migrate from migrate_chronology.main**

In `engine/scripts/migrate_chronology.py`, refactor `main()` so its body (after arg parsing) becomes a reusable function `run_migrate(project_dir, file="故事结构_增量.json", dry_run=False) -> int`, and `main()` parses args then calls `run_migrate`. The extracted function keeps the existing load → index_sequences → migrate_story → backup → save → report logic verbatim. Do not change behavior.

```python
def run_migrate(project_dir: Path, file: str = "故事结构_增量.json", dry_run: bool = False) -> int:
    project_dir = Path(project_dir)
    story_path = project_dir / file
    if not story_path.is_file():
        print(f"故事结构不存在: {story_path}")
        return 1
    try:
        sequences = index_sequences(project_dir)
        source = load_json(story_path)
        migrated, logs = migrate_story(source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"迁移失败: {exc}")
        return 1
    print(f"章节索引已确认: {len(sequences)} 个seq，范围 {sequences[0]}-{sequences[-1]}")
    print(f"迁移记录: {len(logs)} 条")
    if dry_run:
        print("dry-run：未写入文件。")
        return 0
    backup = story_path.with_name(story_path.name + ".before_chronology_migration.bak")
    if not backup.exists():
        shutil.copy2(story_path, backup)
    story_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
    report = project_dir / "质量治理" / "规范化" / "chronology_migration.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(logs) + "\n", encoding="utf-8")
    print(f"迁移完成: {story_path}")
    print(f"备份: {backup}")
    print(f"迁移报告: {report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移故事结构的章节时间和事件分组字段")
    parser.add_argument("project_dir")
    parser.add_argument("--file", default="故事结构_增量.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run_migrate(args.project_dir, file=args.file, dry_run=args.dry_run)
```

- [ ] **Step 4: Implement MigrateChronologyAdapter**

In `backend/task_adapters.py`, add the import and adapter:

```python
from migrate_chronology import run_migrate
```

```python
class MigrateChronologyAdapter:
    """Writer adapter: run chronology migration (time backfill, retrospective
    compaction, event reorder) on the process story JSON."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        dry_run = _bool_param(params, "dry_run", default=False)
        context.progress(30, "migrating chronology")
        rc = run_migrate(project_dir, dry_run=dry_run)
        if rc != 0:
            raise RuntimeError(f"chronology migration failed rc={rc}")
        report = project_dir / "质量治理" / "规范化" / "chronology_migration.txt"
        return _result_json(
            artifacts=[str(report)] if report.is_file() else [],
            manifest={"dry_run": dry_run},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
        )
```

- [ ] **Step 5: Run tests + engine regression**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_migrate_chronology_task.py -q`
Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests\test_migrate_chronology.py -q` (if it exists; otherwise skip)
Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add engine/scripts/migrate_chronology.py backend/task_adapters.py backend/tests/test_migrate_chronology_task.py
git commit -m "feat(workbench): add migrate_chronology adapter"
```

### Task 3: finalize Adapter (composite, worker-backed)

**Files:**
- Modify: `backend/task_adapters.py` (add `FinalizeAdapter`)
- Create: `backend/tests/test_finalize_task.py`

**Interfaces:**
- Consumes: `cmd_final_pack(project_dir: Path, force: bool = False) -> int`, `cmd_commit_final_draft(project_dir: Path) -> int`, `cmd_validate_final(project_dir: Path, file: str = "") -> int`, `cmd_finalize(project_dir: Path) -> int` (all in `engine/scripts/run_pipeline.py`); `run_worker(worker_command, project_dir, task_kind, pack_path, output_path, *, timeout, retries, worker_window)`; `_result_json`, `_str_param`, `_int_param`, `project_revisions`, `TaskContext`; `DEFAULT_WORKER_TIMEOUT_SECONDS`, `DEFAULT_WORKER_WINDOW`, `DEFAULT_WORKER_RETRIES` (constants imported from `run_pipeline` — confirm names exist; if `DEFAULT_WORKER_RETRIES` is not exported, use the literal the orchestrator uses).
- Produces: `FinalizeAdapter` (writer, worker-backed).

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_finalize_task.py`. The test uses a fake worker command (a python one-liner that copies the task pack to the draft) so the composite chain completes without a real LLM. Mirror `engine/tests/test_chapter_staging.py`'s `write_simple_chapter_worker` pattern.

```python
#!/usr/bin/env python3
"""finalize 复合适配器测试（worker-backed，用假 worker）。"""
import json, sys, threading
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from task_adapters import FinalizeAdapter
from workbench import TaskContext


def _write_fake_worker(tmp: Path) -> Path:
    worker = tmp / "fake_final_worker.py"
    worker.write_text(
        'import sys, json, shutil\n'
        'kind, pack, out = sys.argv[1], sys.argv[2], sys.argv[3]\n'
        '# Copy the task pack text as the draft content (good enough to pass strict validate).\n'
        'shutil.copyfile(pack, out)\n',
        encoding="utf-8")
    return worker


def _seed(tmp: Path) -> Path:
    project = tmp / "拆书_测试"
    sdir = project / "原文拆解"
    sdir.mkdir(parents=True)
    (sdir / "_索引.json").write_text(json.dumps({
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "source_title": "第001章"}],
        "pattern": "", "chapter_count": 1, "source_file": "book.txt",
    }, ensure_ascii=False), encoding="utf-8")
    (sdir / "第001章_测试.md").write_text("# 第001章\n正文。\n", encoding="utf-8")
    proc = project / "故事结构_增量.json"
    proc.write_text(json.dumps({
        "介绍": {"标题": "测试"}, "角色集": [], "事件集": [], "地点集": [],
        "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }, ensure_ascii=False), encoding="utf-8")
    return project


def test_finalize_runs_composite_chain_and_writes_final(tmp_path):
    import sys as _sys
    project = _seed(tmp_path)
    worker = _write_fake_worker(tmp_path)
    worker_cmd = f'"{_sys.executable}" "{worker}"'
    ctx = TaskContext(1, 1, project, {"worker_command": worker_cmd},
                      threading.Event(), lambda *_: None)
    result = FinalizeAdapter().run(ctx)
    assert isinstance(result, dict) and "revision" in result
    assert (project / "故事结构.json").is_file()  # final written


if __name__ == "__main__":
    import tempfile
    test_finalize_runs_composite_chain_and_writes_final(Path(tempfile.mkdtemp()))
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_finalize_task.py -q`
Expected: FAIL — `ImportError: cannot import name 'FinalizeAdapter'`.

- [ ] **Step 3: Implement FinalizeAdapter**

In `backend/task_adapters.py`, merge into the `from run_pipeline import (...)` block: `cmd_final_pack`, `cmd_commit_final_draft`, `cmd_validate_final`, `cmd_finalize`, `run_worker` (and the worker default constants if not already imported). Then add:

```python
class FinalizeAdapter:
    """Writer, worker-backed adapter: rerun the finalization sequence on demand.
    Mirrors orchestrator._stage_final_pack / _stage_commit_final_draft /
    _stage_finalize: final_pack → worker revises draft → commit_final_draft
    (with repair loop) → validate_final → finalize."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        worker_command = _str_param(params, "worker_command", default="")
        worker_timeout = _int_param(params, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)
        worker_window = _str_param(params, "worker_window", default=DEFAULT_WORKER_WINDOW)
        worker_retries = _int_param(params, "worker_retries", 0)

        task_pack = project_dir / "质量治理" / "最终审计" / "final_task_pack.md"
        draft = project_dir / "故事结构_草稿.json"
        repair = project_dir / "质量治理" / "最终审计" / "repair_final.md"

        context.progress(10, "final pack")
        rc = cmd_final_pack(project_dir)
        if rc != 0:
            raise RuntimeError(f"final_pack failed rc={rc}")
        if worker_command:
            context.progress(30, "final draft worker")
            rc = run_worker(worker_command, project_dir, "final_draft",
                            task_pack, draft, timeout=worker_timeout,
                            retries=worker_retries, worker_window=worker_window)
            if rc != 0:
                raise RuntimeError(f"final_draft worker failed rc={rc}")

        context.progress(60, "commit final draft")
        # Repair loop mirrors orchestrator._stage_commit_final_draft.
        for _ in range(4):
            rc = cmd_commit_final_draft(project_dir)
            if rc == 0:
                break
            if not repair.is_file():
                raise RuntimeError(f"commit_final_draft failed rc={rc} (no repair task)")
            if not worker_command:
                raise RuntimeError(f"commit_final_draft failed rc={rc} and no worker_command")
            wrc = run_worker(worker_command, project_dir, "repair_final",
                             repair, draft, timeout=worker_timeout,
                             retries=worker_retries, worker_window=worker_window)
            if wrc != 0:
                raise RuntimeError("repair_final worker failed")
        else:
            raise RuntimeError("commit_final_draft did not converge")

        context.progress(80, "validate final")
        rc = cmd_validate_final(project_dir)
        if rc != 0:
            raise RuntimeError(f"validate_final failed rc={rc} (故事结构.json not written)")

        context.progress(95, "finalize")
        rc = cmd_finalize(project_dir)
        if rc != 0:
            raise RuntimeError(f"finalize failed rc={rc}")
        final = project_dir / "故事结构.json"
        return _result_json(
            artifacts=[str(final)],
            manifest={"final": str(final)},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
        )
```

- [ ] **Step 4: Run tests, verify GREEN**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_finalize_task.py -q`
Expected: PASS. (If the fake-worker approach does not satisfy `cmd_commit_final_draft`'s strict validate, adjust the fake worker to emit a minimal valid story schema — but first try the copy approach.)

- [ ] **Step 5: Commit Task 3**

```bash
git add backend/task_adapters.py backend/tests/test_finalize_task.py
git commit -m "feat(workbench): add composite finalize adapter"
```

### Task 4: normalize Adapter

**Files:**
- Modify: `backend/task_adapters.py` (add `NormalizeAdapter`)
- Create: `backend/tests/test_normalize_task.py`

**Interfaces:**
- Consumes: `normalize_structure(data: Any, *, quarantine_invalid_refs: bool = False) -> Tuple[dict, list[str]]` (in `engine/scripts/normalize_story_schema.py`); `_result_json`, `project_revisions`, `TaskContext`.
- Produces: `NormalizeAdapter` (writer).

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_normalize_task.py`:

```python
#!/usr/bin/env python3
"""normalize 适配器测试。"""
import json, sys, threading
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from task_adapters import NormalizeAdapter
from workbench import TaskContext


def _seed(tmp: Path) -> Path:
    project = tmp / "拆书_测试"
    sdir = project / "原文拆解"
    sdir.mkdir(parents=True)
    (sdir / "_索引.json").write_text(json.dumps({
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "source_title": "第001章"}],
        "pattern": "", "chapter_count": 1, "source_file": "book.txt",
    }, ensure_ascii=False), encoding="utf-8")
    (sdir / "第001章_测试.md").write_text("# 第001章\n正文。\n", encoding="utf-8")
    (project / "故事结构_增量.json").write_text(json.dumps({
        "介绍": {"标题": "测试"}, "角色集": [], "事件集": [],
    }, ensure_ascii=False), encoding="utf-8")
    return project


def test_normalize_adapter_rewrites_process_json(tmp_path):
    project = _seed(tmp_path)
    before = (project / "故事结构_增量.json").read_text(encoding="utf-8")
    ctx = TaskContext(1, 1, project, {}, threading.Event(), lambda *_: None)
    result = NormalizeAdapter().run(ctx)
    assert isinstance(result, dict) and "revision" in result
    after = (project / "故事结构_增量.json").read_text(encoding="utf-8")
    # Round-trips to valid JSON.
    json.loads(after)


if __name__ == "__main__":
    import tempfile
    test_normalize_adapter_rewrites_process_json(Path(tempfile.mkdtemp()))
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_normalize_task.py -q`
Expected: FAIL — `ImportError: cannot import name 'NormalizeAdapter'`.

- [ ] **Step 3: Implement NormalizeAdapter**

In `backend/task_adapters.py`, add the import and adapter:

```python
from normalize_story_schema import normalize_structure
```

```python
class NormalizeAdapter:
    """Writer adapter: preserving-style normalization of the process story JSON."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        story_path = project_dir / "故事结构_增量.json"
        if not story_path.is_file():
            raise RuntimeError("故事结构_增量.json not found")
        data = json.loads(story_path.read_text(encoding="utf-8"))
        context.progress(40, "normalizing story schema")
        normalized, logs = normalize_structure(data, quarantine_invalid_refs=False)
        story_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return _result_json(
            artifacts=[str(story_path)],
            manifest={"logs": len(logs)},
            manifest_path="",
            logs=logs,
            revision=project_revisions(project_dir),
        )
```

(If `json` is not already imported at the top of `task_adapters.py`, add `import json`.)

- [ ] **Step 4: Run tests, verify GREEN**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_normalize_task.py -q`
Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/task_adapters.py backend/tests/test_normalize_task.py
git commit -m "feat(workbench): add normalize story schema adapter"
```

### Task 5: repair Adapter

**Files:**
- Modify: `backend/task_adapters.py` (add `RepairAdapter`)
- Create: `backend/tests/test_repair_task.py`

**Interfaces:**
- Consumes: `repair_llm_json(path: Path, report_path: Path, backup: bool = False) -> int`, `chapter_files(project_dir: Path) -> List[Path]`, `artifact_paths(project_dir: Path, chapter: Path) -> Dict[str, Path]` (in `engine/scripts/run_pipeline.py`); `_parse_governance_chapters` (for chapter-range selection) or `_chapter_index_seq_range`; `_result_json`, `project_revisions`, `TaskContext`.
- Produces: `RepairAdapter` (writer).

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_repair_task.py`:

```python
#!/usr/bin/env python3
"""repair 适配器测试。"""
import json, sys, threading
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from task_adapters import RepairAdapter
from workbench import TaskContext


def _seed(tmp: Path, broken: bool) -> Path:
    project = tmp / "拆书_测试"
    sdir = project / "原文拆解"
    sdir.mkdir(parents=True)
    (sdir / "_索引.json").write_text(json.dumps({
        "chapters": [{"seq": 1, "filename": "第001章_测试.md", "source_title": "第001章"}],
        "pattern": "", "chapter_count": 1, "source_file": "book.txt",
    }, ensure_ascii=False), encoding="utf-8")
    (sdir / "第001章_测试.md").write_text("# 第001章\n正文。\n", encoding="utf-8")
    ddir = project / "章节处理"
    ddir.mkdir(parents=True)
    delta = ddir / "第001章_测试.json"
    if broken:
        delta.write_text('{"章节": "第001章", "新增元素": ', encoding="utf-8")  # truncated
    else:
        delta.write_text(json.dumps({"章节": "第001章", "新增元素": {}}), encoding="utf-8")
    proc = project / "故事结构_增量.json"
    proc.write_text(json.dumps({"介绍": {"标题": "测试"}, "角色集": []}, ensure_ascii=False), encoding="utf-8")
    return project


def test_repair_adapter_fixes_broken_delta(tmp_path):
    project = _seed(tmp_path, broken=True)
    ctx = TaskContext(1, 1, project, {}, threading.Event(), lambda *_: None)
    result = RepairAdapter().run(ctx)
    assert isinstance(result, dict)
    # The delta now parses as JSON.
    delta = json.loads((project / "章节处理" / "第001章_测试.json").read_text(encoding="utf-8"))
    assert delta["章节"] == "第001章"


def test_repair_adapter_skips_valid_delta(tmp_path):
    project = _seed(tmp_path, broken=False)
    before = (project / "章节处理" / "第001章_测试.json").read_text(encoding="utf-8")
    ctx = TaskContext(1, 1, project, {}, threading.Event(), lambda *_: None)
    RepairAdapter().run(ctx)
    after = (project / "章节处理" / "第001章_测试.json").read_text(encoding="utf-8")
    assert before == after  # untouched


if __name__ == "__main__":
    import tempfile
    test_repair_adapter_fixes_broken_delta(Path(tempfile.mkdtemp()))
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_repair_task.py -q`
Expected: FAIL — `ImportError: cannot import name 'RepairAdapter'`.

- [ ] **Step 3: Implement RepairAdapter**

In `backend/task_adapters.py`, merge into the `from run_pipeline import (...)` block: `repair_llm_json`, `chapter_files`, `artifact_paths`. Then add:

```python
class RepairAdapter:
    """Writer adapter: repair syntactically broken chapter Delta JSON in place."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        chapters_spec = _str_param(params, "chapters", default="all")
        all_chapters = chapter_files(project_dir)
        # Filter by chapter range if not "all".
        if chapters_spec and chapters_spec != "all":
            start, end = _parse_governance_chapters(project_dir, chapters_spec)
            sel = {s for s in range(start, end + 1)}
            from run_pipeline import seq_from_file
            selected = [c for c in all_chapters if seq_from_file(c) in sel]
        else:
            selected = all_chapters
        repaired: list[str] = []
        reports: list[str] = []
        context.progress(10, f"scanning {len(selected)} chapter deltas")
        for ch in selected:
            p = artifact_paths(project_dir, ch)
            delta = p["delta"]
            if not delta.is_file():
                continue
            # Only repair if it does not parse.
            try:
                json.loads(delta.read_text(encoding="utf-8"))
                continue
            except (json.JSONDecodeError, ValueError):
                pass
            report = delta.with_name(delta.name + ".repair.txt")
            rc = repair_llm_json(delta, report, backup=True)
            if rc == 0:
                repaired.append(str(delta))
                reports.append(str(report))
        context.progress(90, f"repaired {len(repaired)} deltas")
        return _result_json(
            artifacts=repaired,
            manifest={"repaired": len(repaired), "reports": reports},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
        )
```

(Add `import json` if missing. `seq_from_file` is already imported in run_pipeline; add it to the import block if not already present in task_adapters.)

- [ ] **Step 4: Run tests, verify GREEN**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_repair_task.py -q`
Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add backend/task_adapters.py backend/tests/test_repair_task.py
git commit -m "feat(workbench): add repair delta adapter"
```

### Task 6: Wiring — Register Adapters, Writer Types, Staleness Mapping

**Files:**
- Modify: `backend/workbench.py` (`WRITER_TASK_TYPES`)
- Modify: `backend/app.py` (register 5 adapters in `create_app`)
- Modify: `backend/artifact_status.py` (treat new maintenance types as no-card)
- Create: `backend/tests/test_gap_fill_wiring.py`

**Interfaces:**
- Consumes: the 5 adapter classes from Task 1-5.
- Produces: the 5 task types reachable via `POST /projects/{id}/tasks` through the manager; maintenance types do not produce dimension cards but affect staleness via revisions.

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_gap_fill_wiring.py`:

```python
#!/usr/bin/env python3
"""端到端 wiring：5 个新任务类型经 manager 可创建、为 writer。"""
import sys, threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
ENGINE = BACKEND / "engine" / "scripts"
sys.path.insert(0, str(ENGINE))

from workbench import WorkbenchTaskManager, WRITER_TASK_TYPES
from db import Database


def test_new_types_are_writers():
    for t in ("recover_story", "migrate_chronology", "finalize", "normalize", "repair"):
        assert t in WRITER_TASK_TYPES, f"{t} should be a writer task type"


def test_manager_admits_new_types(tmp_path):
    db = Database(tmp_path / "w.db")
    db.init()
    pid = db.create_project(str(tmp_path / "proj"), "proj")
    from task_adapters import (RecoverStoryAdapter, MigrateChronologyAdapter,
        FinalizeAdapter, NormalizeAdapter, RepairAdapter)
    adapters = {
        "recover_story": RecoverStoryAdapter(),
        "migrate_chronology": MigrateChronologyAdapter(),
        "finalize": FinalizeAdapter(),
        "normalize": NormalizeAdapter(),
        "repair": RepairAdapter(),
    }
    class NoHub:
        def broadcast(self, *_): pass
    mgr = WorkbenchTaskManager(db, NoHub(), adapters, poll_interval=0.01)
    try:
        for t in adapters:
            task = mgr.create(pid, t, {})
            assert task["status"] == "queued"
            assert task["task_type"] == t
    finally:
        mgr.shutdown()


if __name__ == "__main__":
    import tempfile
    test_new_types_are_writers()
    test_manager_admits_new_types(Path(tempfile.mkdtemp()))
    print("ok")
```

- [ ] **Step 2: Verify tests fail**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_gap_fill_wiring.py -q`
Expected: FAIL — new types not in `WRITER_TASK_TYPES`; adapters not registered by default.

- [ ] **Step 3: Add new types to WRITER_TASK_TYPES**

In `backend/workbench.py`:

```python
WRITER_TASK_TYPES = frozenset({
    "append_chapters",
    "chapter_analysis_rerun",
    "chapter_delta_rerun",
    "governance",
    "recover_story",
    "migrate_chronology",
    "finalize",
    "normalize",
    "repair",
})
```

- [ ] **Step 4: Register the 5 adapters in create_app**

In `backend/app.py`, extend the `from task_adapters import (...)` block to include `RecoverStoryAdapter`, `MigrateChronologyAdapter`, `FinalizeAdapter`, `NormalizeAdapter`, `RepairAdapter`. In the `default_adapters` dict (around line 94-106), add:

```python
        default_adapters: dict = {
            # ... existing entries ...
            "recover_story": RecoverStoryAdapter(),
            "migrate_chronology": MigrateChronologyAdapter(),
            "finalize": FinalizeAdapter(),
            "normalize": NormalizeAdapter(),
            "repair": RepairAdapter(),
        }
```

- [ ] **Step 5: Extend artifact_status no-card set**

In `backend/artifact_status.py`, find the branch that returns `[]` for `append_chapters`/`governance` (around line 203) and extend it to include the 5 new maintenance types (they affect staleness via revisions, not dimension cards). For example, change the condition to:

```python
if ttype in {"append_chapters", "governance", "recover_story",
             "migrate_chronology", "finalize", "normalize", "repair"}:
    return []  # maintenance ops: no direct card; affect staleness via revisions
```

(Adjust to match the actual structure — the goal is that the 5 new types do not crash card derivation and do not produce dimension cards.)

- [ ] **Step 6: Run wiring tests + backend regression**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_gap_fill_wiring.py backend\tests -q`
Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add backend/workbench.py backend/app.py backend/artifact_status.py backend/tests/test_gap_fill_wiring.py
git commit -m "feat(workbench): wire recover/migrate/finalize/normalize/repair tasks"
```

### Task 7: 维护 Tab UI

**Files:**
- Modify: `desktop/src/renderer/src/global.d.ts` (add 5 typed api methods + recoverSnapshots)
- Modify: `desktop/src/preload/index.ts` (add IPC methods)
- Modify: `desktop/src/main/index.ts` (add IPC handlers)
- Create: `desktop/src/renderer/src/components/MaintenanceTab.tsx`
- Modify: `desktop/src/renderer/src/App.tsx` (add 7th tab)
- Create: `desktop/src/renderer/src/components/workbench.test.tsx` (extend with maintenance tests — or add `maintenance.test.tsx`)

**Interfaces:**
- Consumes: Task 6 typed `window.api` methods; `useProjectTasks` hook; `TaskStatus`, `ArtifactDrawer`.
- Produces: `MaintenanceTab` with 5 actions; 7th tab in App.

- [ ] **Step 1: Write failing renderer tests**

Extend `desktop/src/renderer/src/components/workbench.test.tsx` (or create `maintenance.test.tsx`) with a fake typed API asserting: the 维护 tab renders; the 5 action buttons exist; recover shows snapshots from `recoverSnapshots()` and applies; the other 4 submit tasks with the right `{type, params}`. Mirror the existing fake-API pattern in `workbench.test.tsx`.

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MaintenanceTab } from './MaintenanceTab'

function fakeApi(snapshots = [{ snapshot_seq: 120, path: 'story_after_ch120.json', size: 100, replayable_to: 125 }]) {
  const calls: any[] = []
  return {
    calls,
    api: {
      recoverSnapshots: async () => snapshots,
      createTask: async (_id: number, type: string, params: any) => { calls.push({ type, params }); return { id: 1, status: 'queued' } },
      onEvent: () => () => {},
    } as any,
  }
}

describe('MaintenanceTab', () => {
  it('renders 5 actions and applies recover_story', async () => {
    const { api, calls } = fakeApi()
    render(<MaintenanceTab projectId={1} api={api} />)
    expect(await screen.findByText('快照恢复')).toBeTruthy()
    expect(screen.getByText('时间线迁移')).toBeTruthy()
    expect(screen.getByText('重新收尾')).toBeTruthy()
    expect(screen.getByText('归一化故事结构')).toBeTruthy()
    expect(screen.getByText('修复 Delta')).toBeTruthy()
    fireEvent.click(screen.getByText('应用恢复'))
    await waitFor(() => expect(calls.some(c => c.type === 'recover_story')).toBe(true))
  })

  it('submits migrate/finalize/normalize/repair tasks', async () => {
    const { api, calls } = fakeApi()
    render(<MaintenanceTab projectId={1} api={api} />)
    fireEvent.click(screen.getByText('执行迁移')); 
    fireEvent.click(screen.getByText('重新收尾'));
    fireEvent.click(screen.getByText('归一化故事结构'));
    fireEvent.click(screen.getByText('修复 Delta'));
    await waitFor(() => {
      expect(calls.some(c => c.type === 'migrate_chronology')).toBe(true)
      expect(calls.some(c => c.type === 'finalize')).toBe(true)
      expect(calls.some(c => c.type === 'normalize')).toBe(true)
      expect(calls.some(c => c.type === 'repair')).toBe(true)
    })
  })
})
```

- [ ] **Step 2: Verify tests fail**

Run from `desktop`: `npm test -- --run src/renderer/src/components/maintenance.test.tsx`
Expected: FAIL — `MaintenanceTab` does not exist.

- [ ] **Step 3: Add typed IPC methods + handlers**

In `desktop/src/renderer/src/global.d.ts`, extend `Window.api`:

```typescript
    recoverSnapshots(id: number): Promise<{ snapshot_seq: number; path: string; size: number; replayable_to: number }[]>
    createTask(id: number, type: string, params: Record<string, unknown>): Promise<unknown>
```
(If a generic `createTask` already exists from the workbench, reuse it; otherwise add it.)

In `desktop/src/preload/index.ts`:

```typescript
  recoverSnapshots: (id: number) => ipcRenderer.invoke('nd:projects:recoverSnapshots', id),
  createTask: (id: number, type: string, params: Record<string, unknown>) =>
    ipcRenderer.invoke('nd:projects:tasks:create', id, type, params),
```

In `desktop/src/main/index.ts` `registerIpc()`:

```typescript
  ipcMain.handle('nd:projects:recoverSnapshots', (_e, id: number) => bget(`/projects/${id}/recover/snapshots`))
  ipcMain.handle('nd:projects:tasks:create', (_e, id: number, type: string, params: Record<string, unknown>) =>
    bpost(`/projects/${id}/tasks`, { type, params }))
```

- [ ] **Step 4: Implement MaintenanceTab**

Create `desktop/src/renderer/src/components/MaintenanceTab.tsx`:

```tsx
import { useEffect, useState } from 'react'

type Snapshot = { snapshot_seq: number; path: string; size: number; replayable_to: number }

export function MaintenanceTab({ projectId, api }: { projectId: number; api: any }) {
  const [snaps, setSnaps] = useState<Snapshot[]>([])
  const [chosen, setChosen] = useState<number | null>(null)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (projectId == null) return
    api.recoverSnapshots(projectId).then(setSnaps).catch((e: any) => setMsg(String(e)))
  }, [projectId])

  const submit = async (type: string, params: Record<string, unknown> = {}) => {
    try { await api.createTask(projectId, type, params); setMsg(`已排队：${type}`) }
    catch (e: any) { setMsg(String(e)) }
  }

  return (
    <div style={{ maxWidth: 720 }}>
      <h3>维护</h3>
      <section style={{ border: '1px solid #ddd', padding: 12, marginBottom: 12 }}>
        <h4>快照恢复</h4>
        {snaps.length === 0 ? <span style={{ color: '#888' }}>无可用快照</span> : (
          <select onChange={(e) => setChosen(Number(e.target.value))}>
            <option value="">选择快照…</option>
            {snaps.map((s) => <option key={s.snapshot_seq} value={s.snapshot_seq}>
              seq {s.snapshot_seq}（可重放到 {s.replayable_to}）
            </option>)}
          </select>
        )}
        <div style={{ color: 'crimson', fontSize: 12, marginTop: 4 }}>
          应用将丢弃所选快照之后的所有过程库改动。
        </div>
        <button disabled={chosen == null} onClick={() => chosen != null && submit('recover_story', { snapshot_seq: chosen })}>
          应用恢复
        </button>
      </section>
      <button onClick={() => submit('migrate_chronology')}>执行迁移</button>{' '}
      <button onClick={() => submit('finalize')}>重新收尾</button>{' '}
      <button onClick={() => submit('normalize')}>归一化故事结构</button>{' '}
      <button onClick={() => submit('repair', { chapters: 'all' })}>修复 Delta</button>
      {msg && <div style={{ marginTop: 12, color: '#444' }}>{msg}</div>}
    </div>
  )
}
```

- [ ] **Step 5: Add the 7th tab in App.tsx**

In `desktop/src/renderer/src/App.tsx`, extend the `TABS` array (or tab type) to include `'maintenance'` labeled `维护`, and render `<MaintenanceTab projectId={selected} api={window.api} />` when active. Preserve all existing tabs and behavior.

- [ ] **Step 6: Run renderer tests, typechecks, build**

Run from `desktop`: `npm test -- --run && npm run typecheck:node && npm run typecheck:web && npm run build`
Expected: PASS.

- [ ] **Step 7: Commit Task 7**

```bash
git add desktop/src/renderer/src/global.d.ts desktop/src/preload/index.ts desktop/src/main/index.ts desktop/src/renderer/src/components/MaintenanceTab.tsx desktop/src/renderer/src/App.tsx desktop/src/renderer/src/components/maintenance.test.tsx
git commit -m "feat(desktop): add maintenance tab for recover/migrate/finalize/normalize/repair"
```

### Task 8: Documentation and Full Regression

**Files:**
- Modify: `README.md` (维护 tab + custom exclusion note)
- Modify: `docs/superpowers/plans/2026-07-19-workbench-gap-fill.md` (tick checkboxes)
- Create: `backend/tests/test_gap_fill_integration.py`

**Interfaces:**
- Verifies all previous task interfaces together.

- [ ] **Step 1: Write integration tests**

Create `backend/tests/test_gap_fill_integration.py` using real `Database`/`WorkbenchTaskManager`/`TestClient` + fake workers (mirror `test_workbench_integration.py`). Cover: maintenance tasks queue as writers and respect the project writer mutex with the auto pipeline; recover_story preview is read-only; normalize changes the process revision and marks whole-book analyses stale; finalize writes `故事结构.json` and marks exports stale; repair fixes a broken delta without changing the story revision; task events carry `task_id`+`project_id`.

- [ ] **Step 2: Verify integration tests pass**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_gap_fill_integration.py -q`
Expected: PASS (fix production wiring, not expectations, if any fail).

- [ ] **Step 3: Update README**

In `README.md`, add a "维护" tab section documenting the 5 maintenance actions and their safety semantics (recover-story preview/apply + rollback, finalize composite, normalize before/after, repair target). Add a one-paragraph note under the analysis section: "`custom` 分析维度需用户自定义问题，故仅在工作台手动触发，不进自动流水线 `FULLBOOK_TASKS`。"

- [ ] **Step 4: Tick plan checkboxes**

In `docs/superpowers/plans/2026-07-19-workbench-gap-fill.md`, mark each completed step `[ ]` → `[x]` honestly.

- [ ] **Step 5: Run complete verification**

Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests -q`
Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests -q`
Run from `desktop`: `npm test -- --run && npm run typecheck:node && npm run typecheck:web && npm run build`
Run: `runtime\venv\Scripts\python.exe validate_engine.py`
Expected: all exit 0, no failures.

- [ ] **Step 6: Commit Task 8**

```bash
git add README.md docs/superpowers/plans/2026-07-19-workbench-gap-fill.md backend/tests/test_gap_fill_integration.py
git commit -m "test(workbench): verify gap-fill maintenance workflows"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 = recover_story (spec §3.1, §7); Task 2 = migrate_chronology (§3 table); Task 3 = finalize composite (§3.2); Task 4 = normalize (§3.3); Task 5 = repair (§3.3); Task 6 = wiring + staleness (§5, §6); Task 7 = 维护 tab (§4); Task 8 = custom doc note (§8) + integration (§9). All spec sections covered.
- **Staleness:** recover/migrate/normalize mutate `故事结构_增量.json` → `process` revision changes → done whole-book tasks stale automatically (no new artifact_status logic beyond the no-card classification in Task 6). finalize writes `故事结构.json` → `final` revision changes → exports stale. repair does not change story revisions → no stale. This matches spec §6.
- **Type consistency:** adapter class names (`RecoverStoryAdapter`, `MigrateChronologyAdapter`, `FinalizeAdapter`, `NormalizeAdapter`, `RepairAdapter`) and task type strings (`recover_story`, `migrate_chronology`, `finalize`, `normalize`, `repair`) are used consistently across Tasks 1-7.
- **Worker constants:** Task 3 references `DEFAULT_WORKER_TIMEOUT_SECONDS` / `DEFAULT_WORKER_WINDOW` — confirm these are importable from `run_pipeline` (they are used by the orchestrator and existing adapters). If `DEFAULT_WORKER_RETRIES` is not the exported name, the adapter uses `retries=0` literal (matching GovernanceTaskAdapter).
