# Two-Pass Chapter Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require each chapter to complete a dedicated analysis-MD task before a separate Delta-extraction task is issued.

**Architecture:** Keep `run_pipeline.py run` as the entrypoint. It will inspect the current chapter artifacts and return task handoffs in order: analysis task, Delta task, then deterministic commit. The two task packs have separate paths and instructions so an Agent cannot satisfy both outputs in one model call.

**Tech Stack:** Python 3 standard library, existing pipeline scripts, standalone Python regression tests.

---

### Task 1: Prove Phase Handoffs

**Files:**
- Create: `scripts/test_chapter_staging.py`
- Test: `scripts/test_chapter_staging.py`

- [ ] **Step 1: Write the failing test**

```python
rc = cmd_run(project, audit_interval=0)
assert rc == 2
assert paths["analysis_task"].is_file()
assert not paths["delta_task"].is_file()

paths["analysis"].write_text("# 第001章分析\n", encoding="utf-8")
rc = cmd_run(project, audit_interval=0)
assert rc == 2
assert paths["delta_task"].is_file()
assert paths["analysis"].read_text(encoding="utf-8") in paths["delta_task"].read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python scripts/test_chapter_staging.py`

Expected: FAIL because `artifact_paths()` has no `analysis_task` key and the pipeline produces one combined task.

- [ ] **Step 3: Add the valid Delta and complete the test**

```python
write_json(paths["delta"], empty_delta(1))
assert cmd_run(project, audit_interval=0) == 0
assert paths["after"].is_file()
```

### Task 2: Split Chapter Task Generation

**Files:**
- Modify: `scripts/run_pipeline.py`
- Test: `scripts/test_chapter_staging.py`

- [ ] **Step 1: Add separate task paths**

```python
"analysis_task": project_dir / "章节处理" / "_任务包" / f"task_ch{seq:03d}_analysis.md",
"delta_task": project_dir / "章节处理" / "_任务包" / f"task_ch{seq:03d}_delta.md",
```

- [ ] **Step 2: Generate the analysis task only when MD is missing**

```python
if not p["analysis"].is_file():
    return prepare_chapter_analysis(project_dir, chapter, force)
if not p["delta"].is_file():
    return prepare_chapter_delta(project_dir, chapter, force)
```

- [ ] **Step 3: Generate Delta task only after MD exists**

```python
analysis_text = p["analysis"].read_text(encoding="utf-8", errors="ignore")
# Build task_chNNN_delta.md with the chapter, current story index, analysis_text, and Delta-only rules.
```

- [ ] **Step 4: Update `cmd_run()` and `commit_chapter()` handoff messages**

```python
if not p["analysis"].is_file():
    prepare_chapter(project_dir, seq)
    print("已交接：等待章节分析MD。")
    return 2
if not p["delta"].is_file():
    prepare_chapter(project_dir, seq)
    print("已交接：等待基于章节分析的Delta JSON。")
    return 2
```

- [ ] **Step 5: Run the staging test**

Run: `python scripts/test_chapter_staging.py`

Expected: PASS with two return-code-2 handoffs followed by a committed snapshot.

### Task 3: Update Agent Instructions

**Files:**
- Modify: `SKILL.md`
- Modify: `references/process_overview.md`
- Modify: `prompts/autonomous_run.j2`

- [ ] **Step 1: Describe task order**

```text
task_chNNN_analysis.md -> only write the chapter analysis MD
task_chNNN_delta.md -> read the completed MD and only write the chapter Delta JSON
```

- [ ] **Step 2: Explicitly prohibit producing Delta during the analysis phase**

```text
Analysis task must not create or modify the Delta JSON. Delta task must not rewrite the analysis MD.
```

- [ ] **Step 3: Update autonomous task-priority instructions**

```text
When an analysis task exists, complete it and rerun. When a Delta task exists, complete it and rerun.
```

### Task 4: Verify Regression Surface

**Files:**
- Test: `scripts/test_chapter_staging.py`
- Test: `scripts/test_audit_pack.py`
- Test: `scripts/test_replay_recovery.py`
- Test: `scripts/test_validate_delta.py`
- Test: `scripts/test_merge_delta.py`

- [ ] **Step 1: Run focused and regression tests**

Run: `python scripts/test_chapter_staging.py`, `python scripts/test_audit_pack.py`, `python scripts/test_replay_recovery.py`, `python scripts/test_validate_delta.py`, and `python scripts/test_merge_delta.py`.

Expected: every command exits `0`.

- [ ] **Step 2: Check command interface and diff**

Run: `python scripts/run_pipeline.py run --help` and `git diff --check`.

Expected: command exits `0`, CLI remains usable, and no whitespace errors are reported.
