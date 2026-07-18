# Independent Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent independent-task workbench beside the existing automatic pipeline, including scoped analysis, chapter jobs, governance, append-only chapter updates, exports, stale detection, desktop UI, and UTF-8 Windows output.

**Architecture:** Keep `RunManager` and the legacy `/run`/`/queue` APIs for the automatic pipeline. Add a separate SQLite-backed `WorkbenchTaskManager`; it runs adapters against existing engine functions, emits task events through `BroadcastHub`, and shares a per-project writer admission rule with the automatic pipeline. JSON remains authoritative; task history and revision hashes describe execution and staleness but never replace artifact validation.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, pytest, Electron 31, React 18, TypeScript 5.5, Vitest, Testing Library.

## Global Constraints

- Do not rewrite existing prompts, schemas, deterministic merge logic, or validators.
- Do not turn Markdown or Obsidian output into a writable data source; JSON remains authoritative.
- Never run two project-fact JSON writers for the same project concurrently; different projects may run concurrently.
- Keep the existing automatic pipeline and its `/run` and `/queue` interfaces operational.
- Workbench task states are exactly `queued`, `running`, `done`, `failed`, `cancelled`, and `interrupted`.
- Workbench WebSocket events are `task_queued`, `task_started`, `task_progress`, `task_done`, `task_failed`, and `task_cancelled`, each with `task_id` and `project_id`.
- Restart keeps `queued` tasks and changes residual `running` tasks to `interrupted`.
- Governance may apply automatically only after governance delta validation; if post-apply structure validation fails, restore the before snapshot.
- Append preview must be read-only; apply must reject old-title prefix conflicts and write only new chapters plus the updated index.
- Force rerun may replace canonical artifacts but must preserve prior task history.
- Windows dev startup and all Python/backend/worker process boundaries use UTF-8.
- Preserve the four pre-existing uncommitted fixes in `backend/app.py`, `desktop/src/main/index.ts`, `engine/scripts/run_pipeline.py`, and `engine/tools/agent_worker.py`.

---

### Task 1: Persistent Tasks and Revision Semantics

**Files:**
- Modify: `backend/db.py`
- Modify: `backend/models.py`
- Create: `backend/revisions.py`
- Create: `backend/tests/test_workbench_db.py`
- Create: `backend/tests/test_revisions.py`

**Interfaces:**
- Produces: `Database.create_task(project_id, task_type, params, input_revision) -> dict`
- Produces: `Database.get_task(task_id)`, `list_tasks(project_id, limit)`, `update_task(...)`, `request_task_cancel(task_id)`, `mark_running_tasks_interrupted()`.
- Produces: `project_revisions(project_dir: Path) -> dict[str, str]` with `chapter`, `process`, and `final` hashes.
- Produces: `task_is_stale(task: dict, current: dict[str, str]) -> bool`.

- [ ] **Step 1: Add failing database tests**

  Test JSON round-trips for Chinese params/results, FIFO list ordering, cancel flag updates, project deletion cascade, and startup conversion from `running` to `interrupted` while `queued` remains unchanged.

- [ ] **Step 2: Verify database tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_db.py -q`
  Expected: FAIL because task persistence methods and table do not exist.

- [ ] **Step 3: Implement the schema and CRUD**

  Add `workbench_tasks` with the design fields plus `updated_at` and `retry_of`; add `(project_id,status,id)` index; enable `PRAGMA foreign_keys=ON`; decode `params_json`, `input_revision_json`, and `result_json` in one row converter.

- [ ] **Step 4: Add failing revision tests**

  Create projects with `_索引.json`, Chinese chapter filenames, `故事结构_增量.json`, and `故事结构.json`. Assert chapter metadata changes only alter `chapter`; process/final content changes alter only their matching hash. Assert whole-book analysis/export depends on the relevant revision while old per-chapter artifacts remain current after append-only growth.

- [ ] **Step 5: Verify revision tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_revisions.py -q`
  Expected: FAIL because `backend/revisions.py` does not exist.

- [ ] **Step 6: Implement canonical revisions and dependency mapping**

  Hash canonical index bytes plus referenced chapter relative path, existence, size, and `mtime_ns`; hash process/final JSON bytes separately. Use task type and analysis task name to select chapter/story dependencies dynamically rather than storing a stale boolean.

- [ ] **Step 7: Run Task 1 tests and backend regression**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_db.py backend\tests\test_revisions.py backend\tests\test_app.py -q`
  Expected: PASS.

- [ ] **Step 8: Commit Task 1**

  Commit only Task 1 files with message `feat(workbench): persist tasks and input revisions`.

### Task 2: Task Manager, Project Writer Admission, and REST Events

**Files:**
- Create: `backend/workbench.py`
- Modify: `backend/runner.py`
- Modify: `backend/app.py`
- Modify: `backend/models.py`
- Create: `backend/tests/test_workbench_manager.py`
- Create: `backend/tests/test_workbench_api.py`

**Interfaces:**
- Produces: `TaskAdapter.run(context: TaskContext) -> dict` and `TaskAdapter.cancel()`.
- Produces: `WorkbenchTaskManager.create/list/get/cancel/retry/shutdown`.
- Produces: the six task REST endpoints from the design and task events on the existing hub.
- Consumes: Task 1 database/revision APIs.

- [ ] **Step 1: Write failing scheduler tests**

  Use controllable fake adapters and threading events. Assert FIFO per project, one writer per project, parallel writers across projects, automatic-pipeline writer conflict remains queued, read-only work can run from a fixed task package, cancel event propagation, and retry creates a new row with `retry_of` while retaining the old row.

- [ ] **Step 2: Verify scheduler tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_manager.py -q`
  Expected: FAIL because `WorkbenchTaskManager` does not exist.

- [ ] **Step 3: Implement minimal persistent scheduler**

  Poll queued rows in id order, maintain active handles keyed by task id, use project writer locks, record start/end/error/result, and broadcast exact task events. On application creation, mark residual running rows interrupted, then resume polling queued rows.

- [ ] **Step 4: Write failing REST tests**

  Cover create/list/detail/cancel/retry authentication, 404s, invalid type/params, status response shape, and task event payloads containing `task_id` and `project_id`.

- [ ] **Step 5: Verify REST tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_api.py -q`
  Expected: FAIL with missing routes.

- [ ] **Step 6: Add Pydantic DTOs and routes**

  Define literal task types and statuses, keep params as validated per-type dictionaries at manager admission, initialize the manager in `create_app`, and close it in the FastAPI shutdown hook.

- [ ] **Step 7: Run Task 2 and existing API tests**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_manager.py backend\tests\test_workbench_api.py backend\tests\test_app.py -q`
  Expected: PASS.

- [ ] **Step 8: Commit Task 2**

  Commit Task 2 files with message `feat(workbench): schedule persistent project tasks`.

### Task 3: Analysis, Chapter, and Export Adapters

**Files:**
- Create: `backend/task_adapters.py`
- Modify: `engine/scripts/run_pipeline.py`
- Create: `backend/tests/test_task_adapters.py`
- Create: `engine/tests/test_workbench_task_adapters.py`

**Interfaces:**
- Produces adapters for `analysis`, `chapter_visual`, `chapter_structure`, `chapter_analysis_rerun`, `chapter_delta_rerun`, and `export`.
- Consumes engine task manifests as the authoritative output list.

- [ ] **Step 1: Write failing scoped-adapter tests**

  Assert analysis passes task, chapters (`all`, range, and discrete), targets, question, original-text policy, and force. Assert chapter adapters touch only requested seq values. Assert visual/structure per-chapter outputs and aggregate outputs match engine helpers. Assert export selects process/final and passes single-file, Dataview, Canvas, clean, and filename mode.

- [ ] **Step 2: Verify adapter tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_task_adapters.py engine\tests\test_workbench_task_adapters.py -q`
  Expected: FAIL because adapters and scoped entry points do not exist.

- [ ] **Step 3: Expose narrow engine functions**

  Reuse `analysis_context_pack`, `prepare_chapter_analysis`, `prepare_chapter_delta`, `commit_chapter`, the per-chapter visual/structure output helpers, and `export_obsidian_markdown`. Add sequence filters around existing loops; do not duplicate prompts, schemas, merge, or validation code.

- [ ] **Step 4: Implement adapters and artifact results**

  Validate paths and parameters, call engine functions, derive outputs from the generated manifest/helpers, preserve worker logs and partial outputs on failure, and return `result_json` with artifacts, manifest, logs, and actual revision.

- [ ] **Step 5: Run focused and engine regression tests**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_task_adapters.py -q`
  Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests\test_workbench_task_adapters.py tests\test_analysis_context_pack.py tests\test_export_obsidian_vault.py tests\test_chapter_staging.py -q`
  Expected: PASS.

- [ ] **Step 6: Commit Task 3**

  Commit Task 3 files with message `feat(workbench): adapt scoped analysis chapter and export jobs`.

### Task 4: Governance and Append-Only Chapter Updates

**Files:**
- Modify: `engine/scripts/split_chapters_append.py`
- Modify: `backend/task_adapters.py`
- Modify: `backend/app.py`
- Modify: `backend/models.py`
- Create: `engine/tests/test_split_chapters_append.py`
- Create: `backend/tests/test_governance_task.py`
- Create: `backend/tests/test_append_chapters_task.py`

**Interfaces:**
- Produces: `plan_append(source_file, split_dir, pattern='', increment_only=False) -> AppendPlan`.
- Produces: `apply_append(plan) -> dict`.
- Produces governance and append task adapters and the two append endpoints.

- [ ] **Step 1: Write failing append engine tests**

  Assert preview writes nothing, complete-book title prefix must match, no-new returns an empty plan, conflict reports the first mismatch, explicit increment-only accepts a fragment, and apply writes only new Markdown files plus the index while updating `source_file`.

- [ ] **Step 2: Verify append tests fail**

  Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests\test_split_chapters_append.py -q`
  Expected: FAIL because stable planning/apply functions do not exist.

- [ ] **Step 3: Extract pure planning and atomic apply functions**

  Keep CLI behavior by making `main()` call these functions. Build all output data before writes; use temporary files plus replace for the index and never overwrite old chapter files.

- [ ] **Step 4: Write failing governance/append backend tests**

  Governance: correction validation failure leaves process JSON unchanged; success records before/after/diff; post-apply structure failure restores before. Append: endpoint preview is read-only; apply queues a writer task; success changes chapter revision, leaves old chapter artifacts current, marks whole-book outputs stale, and queues only new chapter production work.

- [ ] **Step 5: Verify backend tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_governance_task.py backend\tests\test_append_chapters_task.py -q`
  Expected: FAIL with missing adapters/routes.

- [ ] **Step 6: Implement governance and append adapters**

  Governance reuses audit pack creation, worker correction, `commit_governance`, and its validators/rollback. Append executes a previously previewed plan under the project writer lock, refreshes revisions, and creates scoped new-chapter tasks.

- [ ] **Step 7: Run focused and governance regressions**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_governance_task.py backend\tests\test_append_chapters_task.py -q`
  Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests\test_split_chapters_append.py tests\test_audit_pack.py tests\test_validate_delta.py tests\test_validate_structure_modes.py -q`
  Expected: PASS.

- [ ] **Step 8: Commit Task 4**

  Commit Task 4 files with message `feat(workbench): add governed fixes and append-only updates`.

### Task 5: Artifact Status and Chapter Workbench Data

**Files:**
- Create: `backend/artifact_status.py`
- Modify: `backend/app.py`
- Modify: `backend/models.py`
- Create: `backend/tests/test_artifact_status.py`

**Interfaces:**
- Produces: `collect_artifact_status(project_dir, tasks) -> ArtifactStatusResponse`.
- Extends chapter rows with analysis, delta, visual, structure, stale, latest task, and error status.

- [ ] **Step 1: Write failing status tests**

  Seed mixed existing/missing artifacts and tasks at old/current revisions. Assert cards return `missing/latest/stale/queued/running/failed/cancelled`, paths come from engine manifests/helpers, append and governance invalidate only the mapped downstream outputs, and stale/export counts are correct.

- [ ] **Step 2: Verify tests fail**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_artifact_status.py -q`
  Expected: FAIL because artifact status API is absent.

- [ ] **Step 3: Implement collector and endpoints**

  Add `GET /projects/{id}/artifacts/status`; extend `/chapters`; add revision/task summaries to project status without changing `current_stage` semantics.

- [ ] **Step 4: Run status and backend regression**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_artifact_status.py backend\tests -q`
  Expected: PASS.

- [ ] **Step 5: Commit Task 5**

  Commit Task 5 files with message `feat(workbench): report artifact freshness and chapter jobs`.

### Task 6: Electron Bridge, Renderer Test Harness, and UTF-8 Boundaries

**Files:**
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `desktop/src/main/index.ts`
- Modify: `desktop/src/preload/index.ts`
- Modify: `desktop/src/renderer/src/global.d.ts`
- Modify: `desktop/src/renderer/src/types.ts`
- Modify: `backend/main.py`
- Create: `desktop/src/main/backend-process.ts`
- Create: `desktop/src/main/backend-process.test.ts`
- Create: `desktop/src/renderer/src/test/setup.ts`
- Create: `backend/tests/test_encoding_windows.py`

**Interfaces:**
- Produces typed IPC methods for tasks, artifacts/status, append preview/apply, and path selection.
- Produces one reusable backend spawn/UTF-8 decoder helper.

- [ ] **Step 1: Add Vitest and failing bridge/encoding tests**

  Assert Chinese stdout/stderr split across Buffer chunks decodes without replacement characters; backend spawn env includes `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`; Python stdio configuration and a Chinese project/log round-trip remain intact.

- [ ] **Step 2: Verify tests fail**

  Run from `desktop`: `npm test -- --run src/main/backend-process.test.ts`
  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_encoding_windows.py -q`
  Expected: FAIL because helpers/test configuration do not exist.

- [ ] **Step 3: Implement process boundary and IPC methods**

  Move spawn/stream decoding into the helper, use `StringDecoder('utf8')` to preserve split multibyte characters, configure backend stdio, add all REST-to-IPC methods, and keep bearer token/path safety behavior unchanged.

- [ ] **Step 4: Add UTF-8 dev launcher**

  Make Windows `npm run dev` execute `chcp 65001 >NUL` before `electron-vite dev`; retain a platform-neutral direct script for CI/typecheck/build.

- [ ] **Step 5: Run focused tests, typechecks, and build**

  Run from `desktop`: `npm test -- --run src/main/backend-process.test.ts && npm run typecheck:node && npm run typecheck:web && npm run build`
  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_encoding_windows.py -q`
  Expected: PASS.

- [ ] **Step 6: Commit Task 6**

  Commit Task 6 files with message `feat(desktop): bridge workbench APIs with utf8 streams`.

### Task 7: Six-Tab Desktop Workbench

**Files:**
- Modify: `desktop/src/renderer/src/App.tsx`
- Create: `desktop/src/renderer/src/hooks/useProjectTasks.ts`
- Create: `desktop/src/renderer/src/components/TaskStatus.tsx`
- Create: `desktop/src/renderer/src/components/OverviewTab.tsx`
- Create: `desktop/src/renderer/src/components/ChaptersTab.tsx`
- Create: `desktop/src/renderer/src/components/AnalysisWorkbenchTab.tsx`
- Create: `desktop/src/renderer/src/components/GovernanceTab.tsx`
- Create: `desktop/src/renderer/src/components/ExportTab.tsx`
- Create: `desktop/src/renderer/src/components/LogsTab.tsx`
- Create: `desktop/src/renderer/src/components/ArtifactDrawer.tsx`
- Create: `desktop/src/renderer/src/components/workbench.test.tsx`

**Interfaces:**
- Consumes Task 6 typed `window.api` methods and Task 5 status DTOs.

- [ ] **Step 1: Write failing renderer interaction tests**

  Render with a fake typed API. Assert the six required tabs; analysis dimension cards and configurable scope/targets/question/original policy/force; chapter multi-select and four batch actions; governance range/history; export source/single-file/Vault/Dataview/Canvas; append preview/conflict/apply; task events refresh only the selected project; artifact/log paths open in the drawer.

- [ ] **Step 2: Verify renderer tests fail**

  Run from `desktop`: `npm test -- --run src/renderer/src/components/workbench.test.tsx`
  Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement the typed task hook and shared status component**

  Load tasks/status, subscribe to `task_*` WebSocket events, refresh selected-project data, expose create/cancel/retry actions, and display concise Chinese errors with links to logs/artifacts.

- [ ] **Step 4: Split App and implement six tabs**

  Preserve project selection/settings/tray behavior. Move preview into `ArtifactDrawer`; keep automatic pipeline controls in Overview; add revision/task/stale summaries; implement task forms and chapter selection without introducing a new state-management dependency.

- [ ] **Step 5: Run renderer tests, typechecks, and build**

  Run from `desktop`: `npm test -- --run && npm run typecheck:node && npm run typecheck:web && npm run build`
  Expected: PASS.

- [ ] **Step 6: Commit Task 7**

  Commit Task 7 files with message `feat(desktop): add independent task workbench`.

### Task 8: End-to-End Regression and Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-18-independent-workbench.md`
- Create: `backend/tests/test_workbench_integration.py`

**Interfaces:**
- Verifies all previous task interfaces together.

- [ ] **Step 1: Write integration tests**

  Use fake workers with real database, manager, API, revisions, and temporary project artifacts. Cover automatic run plus queued workbench writer, cross-project execution, restart recovery, force rerun history, append→new-chapter chain→stale outputs, governance rollback, export artifact result, task events, and Chinese names/logs.

- [ ] **Step 2: Verify integration tests fail for any missing wiring**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests\test_workbench_integration.py -q`
  Expected before final wiring: at least one assertion fails; fix production wiring, not expectations.

- [ ] **Step 3: Complete minimal integration wiring and documentation**

  Document the six tabs, task lifecycle, append safety, JSON authority, developer commands, and Windows UTF-8 launcher. Check every plan checkbox against implemented code and remove no-longer-accurate roadmap text.

- [ ] **Step 4: Run complete verification**

  Run: `runtime\venv\Scripts\python.exe -m pytest backend\tests -q`
  Run from `engine`: `..\runtime\venv\Scripts\python.exe -m pytest tests -q`
  Run from `desktop`: `npm test -- --run && npm run typecheck:node && npm run typecheck:web && npm run build`
  Run: `runtime\venv\Scripts\python.exe validate_engine.py`
  Expected: all commands exit 0 with no test failures or type/build errors.

- [ ] **Step 5: Commit Task 8**

  Commit Task 8 files with message `test(workbench): verify independent task workflows`.

