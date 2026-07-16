# Two-Phase Chapter Run Design

## Goal

Keep the current per-chapter `run` behavior unchanged while adding an explicit two-phase mode: complete and validate every chapter-analysis MD first, then generate, validate, and commit chapter Delta JSONs in order.

## Commands

```powershell
python scripts/run_pipeline.py run <project_dir> --phase analysis
python scripts/run_pipeline.py run <project_dir> --phase delta
```

Without `--phase`, `run` preserves its existing analysis-to-Delta per-chapter loop.

## Analysis phase

For every source chapter in sequence:

1. If its analysis MD is absent, create only `task_chNNN_analysis.md` and return `2`.
2. If the MD is present but fails `validate_chapter_analysis_file`, create only the analysis-regeneration task and return `2`.
3. If it passes, continue to the next source chapter.

The phase completes with `0` only after all analysis MDs pass. It never creates Delta tasks, changes the process story, updates completed-chapter progress, creates snapshots, or triggers governance.

Analysis file existence plus its existing structural validation is the complete checkpoint contract. There is deliberately no hash, source-version, prompt-version, or automatic invalidation tracking.

## Delta phase

Before creating or committing any Delta, scan every source chapter and require a present, valid analysis MD. A missing or invalid MD produces the normal analysis/regeneration handoff and returns `2`; no Delta task or story mutation is allowed first.

After that preflight, use the existing sequential commit path unchanged for each chapter: Delta task handoff, Delta validation, merge, normalization/tag compression, process-level full-structure validation, snapshot/diff, and progress update. Periodic governance remains triggered only after a successfully committed chapter reaches its configured interval.

## Recovery and governance boundary

The new analysis phase is recoverable by rescanning MD files and their existing validator. It does not interact with story snapshots. Delta-phase recovery continues to use the established snapshots and Delta replay behavior.

Recovery also replays already-committed periodic governance corrections whose audit boundary falls inside the restored interval. For example, when restoring from `story_after_ch005.json`, a committed `audit_001-005.status.json` correction is replayed before chapter 6 Delta replay. If the correction file is missing or empty, recovery pauses and marks that audit as awaiting agent work instead of silently dropping the governance patch.

## Visibility and documentation

`resume` will expose analysis-ready count separately from trusted committed count. Existing completion semantics remain unchanged: a chapter is complete only when all Delta, report, snapshot, and diff artifacts are trustworthy.

Command reference and autonomous-loop documentation will show the explicit analysis and Delta commands, their return-`2` handoff behavior, and the rule that Delta mode requires every MD to have passed validation.

## Acceptance criteria

1. Default `run` still alternates analysis and Delta for one chapter exactly as before.
2. Analysis mode never creates a Delta task or changes the story, and returns `0` only once every MD passes.
3. Delta mode refuses to create a Delta task when any MD is missing or invalid.
4. Delta mode commits chapters sequentially and still pauses for periodic governance at the original committed-chapter boundary.
5. Existing MD validation, Delta validation, post-merge process validation, snapshots, diffs, and final/governance validation paths remain in force.
6. `recover-story` preserves committed periodic governance effects when the restore point is at or before that audit boundary.
