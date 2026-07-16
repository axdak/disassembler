# Two-Phase Chapter Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit `run --phase analysis|delta` modes without changing the default chapter-by-chapter run behavior.

**Architecture:** Add phase-specific orchestration functions around existing analysis preparation and chapter commit functions. Phase state is derived only from chapter files and `validate_chapter_analysis_file`; the existing Delta commit and governance paths remain authoritative.

**Tech Stack:** Python 3, argparse, pytest.

---

### Task 1: Lock the analysis-phase contract with failing tests

**Files:**
- Modify: `tests/test_chapter_staging.py`
- Modify: `scripts/run_pipeline.py`

- [ ] **Step 1: Write failing analysis-phase tests**

Add tests that create two source chapters and assert `cmd_run(..., phase="analysis")` first produces only chapter 1 analysis handoff, then, after a valid MD is supplied, produces only chapter 2 analysis handoff, and finally returns `0` without any Delta task, Delta file, story snapshot, or completed-chapter progress change. Add a separate test with an invalid MD asserting that the regeneration task exists and no Delta task exists.

- [ ] **Step 2: Run the focused tests and verify they fail because `cmd_run` has no `phase` argument**

Run: `python -m pytest tests/test_chapter_staging.py -q`

Expected: failure reporting that `cmd_run()` does not accept `phase`.

- [ ] **Step 3: Add the minimal analysis-phase runner**

Add `cmd_run_analysis(project_dir: Path) -> int`. Iterate `chapter_files(project_dir)` in order; use `prepare_chapter_analysis` for a missing MD, use `validate_chapter_analysis_file` for a present MD, and use `write_analysis_regenerate_task` for an invalid MD. Return `2` at the first handoff and `0` only after all MD files pass. Extend `cmd_run` with `phase: str = ""` and dispatch `analysis` to this helper before its existing logic.

- [ ] **Step 4: Re-run the focused tests and verify they pass**

Run: `python -m pytest tests/test_chapter_staging.py -q`

Expected: PASS.

### Task 2: Lock Delta preflight and sequential governance behavior with failing tests

**Files:**
- Modify: `tests/test_chapter_staging.py`
- Modify: `tests/test_audit_pack.py`
- Modify: `scripts/run_pipeline.py`

- [ ] **Step 1: Write failing Delta-phase tests**

Add a test asserting `cmd_run(..., phase="delta", audit_interval=0)` returns `2` and creates an analysis handoff when a later chapter lacks an MD, even if an earlier chapter has a valid MD. Assert no Delta task or snapshot exists for the earlier chapter. Add an audit test that uses fully valid MDs and Deltas for chapters 1 through 6, runs `phase="delta"`, and asserts the existing `audit_001-005.status.json` reaches `awaiting_agent` before chapter 6 commits.

- [ ] **Step 2: Run the focused tests and verify they fail because Delta phase dispatch does not exist**

Run: `python -m pytest tests/test_chapter_staging.py tests/test_audit_pack.py -q`

Expected: failure reporting unsupported `phase="delta"` behavior.

- [ ] **Step 3: Add the minimal Delta preflight runner**

Add `cmd_run_delta(project_dir: Path, max_chapters: int = 0, audit_interval: int = 5) -> int`. First scan all `chapter_files(project_dir)` and require every MD to exist and pass `validate_chapter_analysis_file`; use existing preparation/regeneration handoffs and return `2` on the first failure. On success, call the pre-existing default run loop logic so chapter commits, periodic governance, reports, snapshots, and progress updates keep their current behavior. Dispatch `phase="delta"` from `cmd_run`.

- [ ] **Step 4: Re-run the focused tests and verify they pass**

Run: `python -m pytest tests/test_chapter_staging.py tests/test_audit_pack.py -q`

Expected: PASS.

### Task 3: Expose the CLI and progress visibility

**Files:**
- Modify: `scripts/run_pipeline.py`
- Modify: `tests/test_chapter_staging.py`

- [ ] **Step 1: Write failing CLI and resume-output tests**

Add a CLI-help assertion that `run --help` contains `--phase {analysis,delta}`. Add a resume assertion that valid MD count and trusted committed count are both printed separately for a project containing one valid analysis MD and no committed Delta.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_chapter_staging.py -q`

Expected: assertion failure because phase help and analysis-ready summary are absent.

- [ ] **Step 3: Add CLI and resume support**

Add `p_run.add_argument("--phase", choices=["analysis", "delta"], default="", help="...")` and forward it to `cmd_run`. In `cmd_resume`, count chapter MDs that exist and pass `validate_chapter_analysis_file`, then print that analysis-ready count independently from `可信完成`.

- [ ] **Step 4: Re-run the focused tests and verify they pass**

Run: `python -m pytest tests/test_chapter_staging.py -q`

Expected: PASS.

### Task 4: Align operational documentation and regression coverage

**Files:**
- Modify: `SKILL.md`
- Modify: `references/commands_and_resources.md`
- Modify: `references/autonomous_loop.md`
- Modify: `prompts/autonomous_run.j2`

- [ ] **Step 1: Document both explicit phases**

Describe `run --phase analysis` as MD-only work that returns `2` per MD handoff and does not trigger governance. Describe `run --phase delta` as requiring all MDs to pass before it issues Delta work, then preserving serial commit and periodic governance behavior. Keep unqualified `run` documented as the legacy/default per-chapter interleaved flow.

- [ ] **Step 2: Run the full relevant regression set**

Run: `python -m pytest tests/test_chapter_staging.py tests/test_audit_pack.py tests/test_replay_recovery.py tests/test_chronology.py tests/test_compress_tags.py -q`

Expected: PASS.

- [ ] **Step 3: Run static CLI and formatting checks**

Run: `python -m py_compile scripts/run_pipeline.py; python scripts/run_pipeline.py run --help; git diff --check`

Expected: successful compilation, phase option visible in help, and no whitespace errors.
