"""Workbench task adapters: bridge persistent scheduler to engine scoped entry points.

Six adapters back the workbench task types introduced in Task 3:

- ``analysis`` (snapshot + writer) — builds a pack and writes canonical analysis outputs.
- ``chapter_visual`` (snapshot + writer) — per-chapter visual_assets packs or aggregate.
- ``chapter_structure`` (snapshot + writer) — per-chapter chapter_structure packs.
- ``chapter_analysis_rerun`` (writer, no prepare) — regenerate chapter analysis task packs.
- ``chapter_delta_rerun`` (writer, no prepare) — regenerate chapter delta task packs.
- ``export`` (snapshot + writer) — export story JSON to an Obsidian vault.

Snapshot adapters copy stable inputs in ``prepare``. Adapters that later write canonical
project artifacts declare ``writes_project_artifacts`` so the scheduler keeps a project
writer lease for the full task; truly read-only adapters may still run concurrently.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# 把 engine/scripts 加入 sys.path 以便导入 scoped entry points（与 runner.py 一致）。
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))

from revisions import project_revisions  # noqa: E402
from workbench import TaskContext  # noqa: E402
from worker_checkpoint import run_checkpointed_partial  # noqa: E402

from run_pipeline import (  # noqa: E402
    DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    DEFAULT_WORKER_RETRIES,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    DEFAULT_WORKER_WINDOW,
    audit_paths,
    audit_status,
    builtin_agent_worker_command,
    cmd_commit_final_draft,
    cmd_finalize,
    cmd_validate_final,
    commit_governance,
    copy_analysis_task_packages,
    latest_analysis_manifest,
    latest_snapshot_seq,
    load_json,
    max_available_delta_seq,
    prepare_audit_pack_or_split,
    run_periodic_governance,
    recover_story_from_snapshot,
    repair_llm_json,
    artifact_paths,
    chapter_files,
    seq_from_file,
    run_generation_worker as run_worker,
    run_repair_worker,
    run_multi_output_worker,
    scoped_analysis_pack,
    scoped_chapter_analysis_rerun,
    scoped_chapter_delta_rerun,
    scoped_chapter_structure_pack,
    scoped_chapter_visual_aggregate,
    scoped_chapter_visual_pack,
    snapshot_project_inputs,
    unresolved_audits,
    write_analysis_worker_pack,
    write_fullbook_partial_worker_pack,
    write_fullbook_reduce_worker_pack,
)
from export_obsidian_vault import export_obsidian_markdown  # noqa: E402
from migrate_chronology import run_migrate  # noqa: E402
from normalize_story_schema import normalize_structure, save_json  # noqa: E402
from worker_artifacts import confined_project_path  # noqa: E402
from split_chapters_append import apply_append  # noqa: E402


def _result_json(*, artifacts, manifest, manifest_path, logs, revision, **extra) -> dict:
    """Standard adapter result shape: artifacts / manifest / logs / revision."""
    return {
        "artifacts": artifacts,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "logs": logs,
        "revision": revision,
        **extra,
    }


def _str_param(params: dict, *names: str, default: str = "") -> str:
    for name in names:
        value = params.get(name)
        if value is not None and value != "":
            return str(value)
    return default


def _bool_param(params: dict, *names: str, default: bool = False) -> bool:
    for name in names:
        if name in params and params[name] is not None:
            return bool(params[name])
    return default


def _int_param(params: dict, name: str, default: int) -> int:
    value = params.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _setting(context: TaskContext, name: str, default: Any) -> Any:
    """Read one value from the task-start settings snapshot."""
    value = (context.settings or {}).get(name, default)
    return default if value is None or value == "" else value


def _resolve_worker(params: dict, context: TaskContext) -> tuple[str, int, int, str]:
    """Resolve worker command + tuning from params and the global worker_provider.

    Returns (worker_command, timeout_seconds, retries, window). worker_command is
    empty when no explicit command is given and provider is manual/unset; callers
    raise if they require a worker.
    """
    command = _str_param(params, "worker_command", default="")
    provider = _str_param(params, "worker_provider", default="") or context.worker_provider
    if not command and provider and provider != "manual":
        command = builtin_agent_worker_command(provider)
    timeout = _int_param(
        params, "worker_timeout_seconds",
        int(_setting(context, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)),
    )
    retries = _int_param(
        params, "worker_retries",
        int(_setting(context, "worker_retries", DEFAULT_WORKER_RETRIES)),
    )
    window = _str_param(
        params, "worker_window",
        default=str(_setting(context, "worker_window", DEFAULT_WORKER_WINDOW)),
    )
    return command, timeout, retries, window


def _chapter_index_seq_range(project_dir: Path) -> tuple[int, int]:
    """Derive (min_seq, max_seq) from the project's ``_索引.json`` chapters."""
    index_path = Path(project_dir) / "原文拆解" / "_索引.json"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return (1, 0)
    raw = data.get("chapters", []) if isinstance(data, dict) else data
    seqs = [int(c["seq"]) for c in raw if isinstance(c, dict) and c.get("seq") is not None]
    if not seqs:
        return (1, 0)
    return (min(seqs), max(seqs))


def _parse_governance_chapters(project_dir: Path, chapters: str) -> tuple[int, int]:
    """Translate the spec's ``chapters`` param to a (start, end) range.

    - ``"all"`` → min..max seq from ``_索引.json``.
    - ``"recent"`` → the first unresolved periodic-audit range; if none is
      unresolved, fall back to the whole-book range (the book has been fully
      audited, so audit the whole book again on demand).
    - ``"101-150"`` (or any ``"N-M"``) → parsed start/end.
    """
    chapters = (chapters or "").strip().lower()
    if chapters in ("", "all"):
        start, end = _chapter_index_seq_range(project_dir)
        if end < start:
            raise ValueError("governance 'all' 需要项目索引中存在章节")
        return start, end
    if chapters == "recent":
        unresolved = unresolved_audits(Path(project_dir))
        for entry in unresolved:
            start = entry.get("_start")
            end = entry.get("_end")
            if start is not None and end is not None and end >= start:
                return int(start), int(end)
        start, end = _chapter_index_seq_range(project_dir)
        if end < start:
            raise ValueError("governance 'recent' 没有未审计的章节且无索引")
        return start, end
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", chapters)
    if not m:
        raise ValueError(f"governance 'chapters' 必须为 all/recent/N-M，实际为: {chapters!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        raise ValueError(f"governance 'chapters' 要求 start <= end: {chapters!r}")
    return start, end


def _latest_manifest(project_dir: Path, suffix: str) -> Optional[Path]:
    """Locate the most recent manifest matching suffix in the real project dir."""
    path = latest_analysis_manifest(project_dir, suffix)
    return Path(path) if path is not None else None


# === AnalysisAdapter =======================================================

class AnalysisAdapter:
    """Snapshot inputs, build an analysis pack, then write canonical outputs."""

    # prepare 只读快照，但 run 会让 worker 写 canonical 全书分析产物。
    writes_project_artifacts = True

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    @staticmethod
    def _parse_params(params: dict, settings: Optional[dict] = None) -> dict:
        """Translate the spec public params to engine call params.

        spec §8/§3.3: ``{task, chapters, targets, question, original_text,
        force}``. The ``original_text`` alias is the canonical UI name; legacy
        ``include_original``/``original`` remain accepted.
        """
        settings = settings or {}
        return {
            "task": _str_param(params, "task", "analysis_task", "name", default="summary"),
            "chapters": _str_param(params, "chapters", "chapter_range", default="all"),
            "targets": params.get("targets") or None,
            "question": _str_param(params, "question", default=""),
            "include_original": _str_param(
                params, "include_original", "original", "original_text",
                default=str(settings.get("include_original", "sample")),
            ),
            "force": _bool_param(params, "force", default=False),
            "max_pack_chars": _int_param(
                params, "max_pack_chars", int(settings.get("max_pack_chars", 70000))
            ),
            "max_original_chars": _int_param(
                params, "max_original_chars", int(settings.get("max_original_chars", 6000))
            ),
            "max_analysis_chars": _int_param(
                params, "max_analysis_chars", int(settings.get("max_analysis_chars", 12000))
            ),
            "story_summary_max_items": _int_param(
                params, "story_summary_max_items", int(settings.get("story_summary_max_items", 0))
            ),
            "context_budget_chars": _int_param(
                params, "context_budget_chars", int(settings.get("context_budget_chars", 160000))
            ),
            "worker_command": _str_param(params, "worker_command", default=""),
            "worker_provider": _str_param(params, "worker_provider", default=""),
            "worker_timeout_seconds": _int_param(
                params, "worker_timeout_seconds",
                int(settings.get("worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)),
            ),
            "worker_retries": _int_param(
                params, "worker_retries", int(settings.get("worker_retries", DEFAULT_WORKER_RETRIES))
            ),
            "worker_window": _str_param(
                params, "worker_window", default=str(settings.get("worker_window", DEFAULT_WORKER_WINDOW))
            ),
        }

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        parsed = self._parse_params(params, context.settings)
        task = parsed["task"]
        chapters = parsed["chapters"]
        targets = parsed["targets"]
        question = parsed["question"]
        include_original = parsed["include_original"]
        force = parsed["force"]
        max_pack_chars = parsed["max_pack_chars"]
        max_original_chars = parsed["max_original_chars"]
        max_analysis_chars = parsed["max_analysis_chars"]
        story_summary_max_items = parsed["story_summary_max_items"]
        context_budget_chars = parsed["context_budget_chars"]

        project_dir = Path(context.project_path)
        snapshot_dir = (
            prepared_input["snapshot_dir"]
            if isinstance(prepared_input, dict) and prepared_input.get("snapshot_dir")
            else snapshot_project_inputs(project_dir)
        )
        try:
            context.progress(10, "generating analysis task pack")
            scoped = scoped_analysis_pack(
                snapshot_dir,
                task=task,
                chapters=chapters,
                targets=targets,
                question=question,
                include_original=include_original,
                max_pack_chars=max_pack_chars,
                max_original_chars=max_original_chars,
                max_analysis_chars=max_analysis_chars,
                story_summary_max_items=story_summary_max_items,
                context_budget_chars=context_budget_chars,
                force=force,
            )
            context.progress(70, "merging task pack into project")
            copy_analysis_task_packages(snapshot_dir, project_dir)
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

        # Re-locate the manifest in the real project dir so result paths are canonical.
        manifest_path = _latest_manifest(project_dir, task) or Path(scoped["manifest_path"])
        manifest = load_json(manifest_path, {}) or {}
        packs = manifest.get("packs") or []
        target_outputs = [
            confined_project_path(project_dir, rel)
            for rel in (manifest.get("target_outputs") or [])
        ]

        artifacts = [{"name": "manifest", "path": str(manifest_path)}]
        for index, pack in enumerate(packs):
            if isinstance(pack, str):
                artifacts.append({
                    "name": f"pack_{index + 1:03d}",
                    "path": str(confined_project_path(project_dir, pack)),
                })
            elif isinstance(pack, dict):
                rel = pack.get("pack") or pack.get("pack_path")
                if rel:
                    artifacts.append(
                        {
                            "name": f"pack_ch{pack.get('seq', 0):03d}",
                            "path": str(confined_project_path(project_dir, rel)),
                        }
                    )

        # Worker execution: produce the canonical target_outputs by running the
        # task packs through an external worker. ``custom`` has no predefined
        # outputs (gap-fill design §8) and keeps the pack-only behavior.
        worker_command = parsed["worker_command"]
        if target_outputs and not worker_command:
            provider = parsed["worker_provider"] or context.worker_provider
            if provider and provider != "manual":
                worker_command = builtin_agent_worker_command(provider)
        if target_outputs:
            if not worker_command:
                raise RuntimeError(
                    "analysis 执行需要 worker：未配置 worker_command，且 worker_provider 为 "
                    "manual 或未设置。请在设置中选择 worker provider（如 agy）后再运行分析任务。"
                )
            worker_timeout = parsed["worker_timeout_seconds"]
            worker_retries = parsed["worker_retries"]
            worker_window = parsed["worker_window"]
            pack_count = len(packs)
            if pack_count == 0:
                raise RuntimeError(f"analysis manifest 没有 packs: {manifest_path}")
            if pack_count == 1:
                pack_rel = packs[0] if isinstance(packs[0], str) else packs[0].get("pack")
                pack_path = confined_project_path(project_dir, pack_rel)
                context.progress(80, "worker 执行分析任务包 (1/1)")
                rc = run_multi_output_worker(
                    worker_command, project_dir, "fullbook_analysis", pack_path, target_outputs,
                    timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                )
                if rc != 0:
                    raise RuntimeError(f"analysis worker 失败 rc={rc}")
            else:
                partial_paths: list[Path] = []
                for index, pack in enumerate(packs, 1):
                    pack_rel = pack if isinstance(pack, str) else pack.get("pack")
                    partial_path = manifest_path.parent / f"partial_result_{index:03d}.md"
                    worker_pack = write_fullbook_partial_worker_pack(
                        project_dir, manifest_path, pack_rel, index, pack_count, partial_path
                    )
                    context.progress(75, f"worker 执行分包 ({index}/{pack_count})")
                    rc = run_checkpointed_partial(
                        run_worker, worker_command, project_dir, "fullbook_analysis", worker_pack, partial_path,
                        reuse=not force,
                        timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                    )
                    if rc != 0:
                        raise RuntimeError(f"analysis pack {index} worker 失败 rc={rc}")
                    partial_paths.append(partial_path)
                reduce_pack = write_fullbook_reduce_worker_pack(
                    project_dir, manifest_path, partial_paths, target_outputs
                )
                context.progress(90, "worker 汇总分批结果")
                rc = run_multi_output_worker(
                    worker_command, project_dir, "fullbook_analysis_reduce", reduce_pack, target_outputs,
                    timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                )
                if rc != 0:
                    raise RuntimeError(f"analysis reduce worker 失败 rc={rc}")
            for out in target_outputs:
                artifacts.append({"name": out.name, "path": str(out)})

        revision = project_revisions(project_dir)
        context.progress(100, "analysis complete")
        return _result_json(
            artifacts=artifacts,
            manifest=manifest,
            manifest_path=str(manifest_path),
            logs=[],
            revision=revision,
            task=task,
            chapters=chapters,
            targets=targets or [],
            question=question,
            include_original=manifest.get("include_original", "full"),
            force=force,
        )


# === ChapterVisualAdapter ==================================================

class ChapterVisualAdapter:
    """Snapshot-backed writer for per-chapter or aggregate visual assets."""

    writes_project_artifacts = True

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_visual 需要非空 'seqs' 列表")
        seqs = [int(s) for s in seqs]
        aggregate = _bool_param(params, "aggregate", default=False)
        include_original = _str_param(
            params, "include_original", "original",
            default=str(_setting(context, "include_original", "sample")),
        )
        max_pack_chars = _int_param(
            params, "max_pack_chars", int(_setting(context, "max_pack_chars", 70000))
        )
        max_original_chars = _int_param(
            params, "max_original_chars", int(_setting(context, "max_original_chars", 6000))
        )
        max_analysis_chars = _int_param(
            params, "max_analysis_chars", int(_setting(context, "max_analysis_chars", 12000))
        )
        story_summary_max_items = _int_param(
            params, "story_summary_max_items",
            int(_setting(context, "story_summary_max_items", 0)),
        )
        context_budget_chars = _int_param(
            params, "context_budget_chars",
            int(_setting(context, "context_budget_chars", 160000)),
        )
        force_aggregate = _bool_param(params, "force_aggregate", default=False)

        project_dir = Path(context.project_path)
        snapshot_dir = (
            prepared_input["snapshot_dir"]
            if isinstance(prepared_input, dict) and prepared_input.get("snapshot_dir")
            else snapshot_project_inputs(project_dir)
        )

        revision = project_revisions(project_dir)
        worker_command, worker_timeout, worker_retries, worker_window = _resolve_worker(params, context)
        artifacts: list[dict] = []
        try:
            if aggregate:
                context.progress(10, "generating visual_assets aggregate pack")
                scoped = scoped_chapter_visual_aggregate(snapshot_dir)
                context.progress(70, "merging aggregate pack into project")
                copy_analysis_task_packages(snapshot_dir, project_dir)
                manifest_path = (
                    _latest_manifest(project_dir, "visual_assets_aggregate")
                    or Path(scoped["manifest_path"])
                )
                manifest = load_json(manifest_path, {}) or {}
                target_outputs = [
                    confined_project_path(project_dir, rel)
                    for rel in (manifest.get("target_outputs") or [])
                ]
                if not worker_command:
                    raise RuntimeError(
                        "chapter_visual 执行需要 worker：未配置 worker_command，且 worker_provider 为 "
                        "manual 或未设置。请在设置中选择 worker provider（如 agy）后再运行。"
                    )
                if target_outputs:
                    task_pack = write_analysis_worker_pack(project_dir, manifest_path, manifest)
                    context.progress(60, "worker 执行视觉资产汇总")
                    rc = run_multi_output_worker(
                        worker_command, project_dir, "visual_assets_aggregate", task_pack, target_outputs,
                        timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                    )
                    if rc != 0:
                        raise RuntimeError(f"visual_assets aggregate worker 失败 rc={rc}")
                artifacts.append({"name": "manifest", "path": str(manifest_path)})
                for out in target_outputs:
                    artifacts.append({"name": out.name, "path": str(out)})
                context.progress(100, "aggregate complete")
                return _result_json(
                    artifacts=artifacts,
                    manifest=manifest,
                    manifest_path=str(manifest_path),
                    logs=[],
                    revision=revision,
                    task="visual_assets",
                    mode="aggregate",
                    target_outputs=manifest.get("target_outputs") or [],
                    seqs=seqs,
                    force_aggregate=force_aggregate,
                )

            context.progress(10, "generating visual_assets per-chapter packs")
            scoped = scoped_chapter_visual_pack(
                snapshot_dir,
                seqs=seqs,
                include_original=include_original,
                max_pack_chars=max_pack_chars,
                max_original_chars=max_original_chars,
                max_analysis_chars=max_analysis_chars,
                story_summary_max_items=story_summary_max_items,
                context_budget_chars=context_budget_chars,
            )
            context.progress(70, "merging per-chapter packs into project")
            copy_analysis_task_packages(snapshot_dir, project_dir)
            manifest_path = (
                _latest_manifest(project_dir, "visual_assets_per_chapter")
                or Path(scoped["manifest_path"])
            )
            manifest = load_json(manifest_path, {}) or {}
            packs = manifest.get("packs") or []
            if not worker_command:
                raise RuntimeError(
                    "chapter_visual 执行需要 worker：未配置 worker_command，且 worker_provider 为 "
                    "manual 或未设置。请在设置中选择 worker provider（如 agy）后再运行。"
                )
            artifacts.append({"name": "manifest", "path": str(manifest_path)})
            for record in packs:
                seq = int(record.get("seq", 0))
                rel = record.get("pack")
                if rel:
                    artifacts.append({
                        "name": f"pack_ch{seq:03d}",
                        "path": str(confined_project_path(project_dir, rel)),
                    })
                expected_outputs = [
                    confined_project_path(project_dir, rel)
                    for rel in (record.get("outputs") or [])
                ]
                if not expected_outputs:
                    continue
                task_pack = write_analysis_worker_pack(project_dir, manifest_path, record)
                context.progress(80, f"worker 执行第{seq:03d}章视觉资产")
                rc = run_multi_output_worker(
                    worker_command, project_dir, "visual_assets", task_pack, expected_outputs, seq,
                    timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                )
                if rc != 0:
                    raise RuntimeError(f"visual_assets ch{seq:03d} worker 失败 rc={rc}")
                for out in expected_outputs:
                    artifacts.append({"name": out.name, "path": str(out)})
            context.progress(100, "per-chapter complete")
            return _result_json(
                artifacts=artifacts,
                manifest=manifest,
                manifest_path=str(manifest_path),
                logs=[],
                revision=revision,
                task="visual_assets",
                mode="per_chapter",
                seqs=seqs,
                force_aggregate=force_aggregate,
            )
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)


# === ChapterStructureAdapter ===============================================

class ChapterStructureAdapter:
    """Snapshot-backed writer for per-chapter structure artifacts."""

    writes_project_artifacts = True

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_structure 需要非空 'seqs' 列表")
        seqs = [int(s) for s in seqs]
        include_original = _str_param(
            params, "include_original", "original",
            default=str(_setting(context, "include_original", "sample")),
        )
        max_pack_chars = _int_param(
            params, "max_pack_chars", int(_setting(context, "max_pack_chars", 70000))
        )
        max_original_chars = _int_param(
            params, "max_original_chars", int(_setting(context, "max_original_chars", 6000))
        )
        max_analysis_chars = _int_param(
            params, "max_analysis_chars", int(_setting(context, "max_analysis_chars", 12000))
        )
        story_summary_max_items = _int_param(
            params, "story_summary_max_items",
            int(_setting(context, "story_summary_max_items", 0)),
        )
        context_budget_chars = _int_param(
            params, "context_budget_chars",
            int(_setting(context, "context_budget_chars", 160000)),
        )

        project_dir = Path(context.project_path)
        snapshot_dir = (
            prepared_input["snapshot_dir"]
            if isinstance(prepared_input, dict) and prepared_input.get("snapshot_dir")
            else snapshot_project_inputs(project_dir)
        )
        worker_command, worker_timeout, worker_retries, worker_window = _resolve_worker(params, context)
        try:
            context.progress(10, "generating chapter_structure per-chapter packs")
            scoped = scoped_chapter_structure_pack(
                snapshot_dir,
                seqs=seqs,
                include_original=include_original,
                max_pack_chars=max_pack_chars,
                max_original_chars=max_original_chars,
                max_analysis_chars=max_analysis_chars,
                story_summary_max_items=story_summary_max_items,
                context_budget_chars=context_budget_chars,
            )
            context.progress(70, "merging per-chapter packs into project")
            copy_analysis_task_packages(snapshot_dir, project_dir)
            manifest_path = (
                _latest_manifest(project_dir, "chapter_structure_per_chapter")
                or Path(scoped["manifest_path"])
            )
            manifest = load_json(manifest_path, {}) or {}
            packs = manifest.get("packs") or []
            if not worker_command:
                raise RuntimeError(
                    "chapter_structure 执行需要 worker：未配置 worker_command，且 worker_provider 为 "
                    "manual 或未设置。请在设置中选择 worker provider（如 agy）后再运行。"
                )
            artifacts = [{"name": "manifest", "path": str(manifest_path)}]
            for record in packs:
                seq = int(record.get("seq", 0))
                rel = record.get("pack")
                if rel:
                    artifacts.append({
                        "name": f"pack_ch{seq:03d}",
                        "path": str(confined_project_path(project_dir, rel)),
                    })
                expected_outputs = [
                    confined_project_path(project_dir, rel)
                    for rel in (record.get("outputs") or [])
                ]
                if not expected_outputs:
                    continue
                task_pack = write_analysis_worker_pack(project_dir, manifest_path, record)
                context.progress(80, f"worker 执行第{seq:03d}章章节结构")
                rc = run_multi_output_worker(
                    worker_command, project_dir, "chapter_structure", task_pack, expected_outputs, seq,
                    timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                )
                if rc != 0:
                    raise RuntimeError(f"chapter_structure ch{seq:03d} worker 失败 rc={rc}")
                for out in expected_outputs:
                    artifacts.append({"name": out.name, "path": str(out)})
            revision = project_revisions(project_dir)
            context.progress(100, "per-chapter complete")
            return _result_json(
                artifacts=artifacts,
                manifest=manifest,
                manifest_path=str(manifest_path),
                logs=[],
                revision=revision,
                task="chapter_structure",
                mode="per_chapter",
                seqs=seqs,
            )
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)


# === ChapterAnalysisRerunAdapter (writer) ==================================

class ChapterAnalysisRerunAdapter:
    """Writer adapter: regenerate chapter analysis task packs for requested seqs.

    Holds the project writer lock for the whole run (no prepare) because
    prepare_chapter_analysis -> ensure_story may normalize 故事结构_增量.json.
    """

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_analysis_rerun 需要非空 'seqs' 列表")
        seqs = [int(s) for s in seqs]
        force = _bool_param(params, "force", default=False)
        project_dir = Path(context.project_path)

        context.progress(10, "regenerating chapter analysis task packs")
        scoped = scoped_chapter_analysis_rerun(project_dir, seqs=seqs, force=force)
        context.progress(90, "chapter analysis task packs ready")

        artifacts = [
            {"name": f"analysis_task_ch{entry['seq']:03d}", "path": entry["analysis_task"]}
            for entry in scoped.get("per_seq", [])
        ]
        revision = project_revisions(project_dir)
        context.progress(100, "done")
        return _result_json(
            artifacts=artifacts,
            manifest=scoped,
            manifest_path="",
            logs=[],
            revision=revision,
            task="chapter_analysis_rerun",
            seqs=seqs,
            force=force,
        )


# === ChapterDeltaRerunAdapter (writer) ====================================

class ChapterDeltaRerunAdapter:
    """Writer adapter: regenerate chapter delta task packs for requested seqs."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_delta_rerun 需要非空 'seqs' 列表")
        seqs = [int(s) for s in seqs]
        force = _bool_param(params, "force", default=False)
        project_dir = Path(context.project_path)

        context.progress(10, "regenerating chapter delta task packs")
        scoped = scoped_chapter_delta_rerun(project_dir, seqs=seqs, force=force)
        context.progress(90, "chapter delta task packs ready")

        artifacts = [
            {"name": f"delta_task_ch{entry['seq']:03d}", "path": entry["delta_task"]}
            for entry in scoped.get("per_seq", [])
        ]
        revision = project_revisions(project_dir)
        context.progress(100, "done")
        return _result_json(
            artifacts=artifacts,
            manifest=scoped,
            manifest_path="",
            logs=[],
            revision=revision,
            task="chapter_delta_rerun",
            seqs=seqs,
            force=force,
        )


# === RepairAdapter (writer) ================================================

class RepairAdapter:
    """Writer adapter: repair syntactically broken chapter Delta JSON in place.

    Walks ``chapter_files`` → ``artifact_paths`` → ``p["delta"]`` for the
    requested ``chapters`` range (``all``/``recent``/``N-M``) and invokes the
    engine's ``repair_llm_json`` on any delta that fails to parse as strict
    JSON. Deltas that already parse are skipped, leaving them untouched.
    """

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        chapters_spec = _str_param(params, "chapters", default="all")
        all_chapters = chapter_files(project_dir)
        # _parse_governance_chapters handles "all"/"recent"/"N-M" (local helper).
        start, end = _parse_governance_chapters(project_dir, chapters_spec)
        sel = set(range(start, end + 1))
        selected = [c for c in all_chapters if seq_from_file(c) in sel]
        repaired: list[str] = []
        reports: list[str] = []
        context.progress(10, f"scanning {len(selected)} chapter deltas")
        for ch in selected:
            p = artifact_paths(project_dir, ch)
            delta = p["delta"]
            if not delta.is_file():
                continue
            try:
                json.loads(delta.read_text(encoding="utf-8"))
                continue  # already valid — skip
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


# === ExportAdapter (snapshot reader + artifact writer) =====================

class ExportAdapter:
    """Snapshot-backed writer that exports story JSON to an Obsidian vault."""

    writes_project_artifacts = True

    def prepare(self, context: TaskContext) -> dict:
        params = context.params or {}
        source = _str_param(params, "source", "export_source", default="final").lower()
        project_dir = Path(context.project_path)
        story_path = self._resolve_story_path(project_dir, source)
        # NamedTemporaryFile reserves the pathname atomically; tempfile.mktemp
        # has a race where another process can create the file before copy2.
        with tempfile.NamedTemporaryFile(prefix="nd_story_", suffix=".json", delete=False) as tmp:
            snapshot = Path(tmp.name)
        shutil.copy2(story_path, snapshot)
        return {"story_snapshot_path": snapshot, "project_dir": project_dir, "source": source}

    @staticmethod
    def _resolve_story_path(project_dir: Path, source: str) -> Path:
        if source == "process":
            candidate = project_dir / "故事结构_增量.json"
        else:
            candidate = project_dir / "故事结构.json"
        if not candidate.is_file():
            raise FileNotFoundError(f"story structure 文件未找到: {candidate}")
        return candidate

    @staticmethod
    def _translate_kinds(kinds) -> dict:
        """Translate the spec's ``kinds`` array to engine output flags.

        "vault" is always produced (the default Obsidian vault output) and
        therefore has no flag here. Returns a dict with single/dataview/canvas
        bools, all False when ``kinds`` is absent (caller falls back to the
        granular flags).
        """
        flags = {"single": False, "dataview": False, "canvas": False}
        if not kinds:
            return flags
        for item in kinds:
            key = str(item).lower().strip()
            if key in flags:
                flags[key] = True
        return flags

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        source = _str_param(params, "source", "export_source", default="final").lower()
        project_dir = Path(context.project_path)
        canonical_story_path = self._resolve_story_path(project_dir, source)

        if isinstance(prepared_input, dict) and prepared_input.get("story_snapshot_path"):
            story_path = Path(prepared_input["story_snapshot_path"])
        else:
            story_path = canonical_story_path

        out_dir = params.get("out_dir")
        out_dir_path = Path(out_dir) if out_dir else project_dir / "obsidian-export"
        if not out_dir_path.is_absolute():
            out_dir_path = (project_dir / out_dir_path).resolve()

        # spec §3.5: UI sends ``kinds`` (array of single/vault/dataview/canvas)
        # and ``force``. Translate to the engine flags. The granular flags
        # remain accepted as a fallback. "vault" is always produced.
        kind_flags = self._translate_kinds(params.get("kinds"))

        single_file = params.get("single_file")
        single_file_path: Optional[Path] = None
        if single_file:
            single_file_path = Path(single_file)
            if not single_file_path.is_absolute():
                single_file_path = (project_dir / single_file_path).resolve()
        elif kind_flags["single"]:
            # Default single-file Markdown path sits beside the vault export.
            single_file_path = out_dir_path.parent / (out_dir_path.name + "_single.md")

        clean = _bool_param(params, "clean", default=False)
        include_dataview = (
            _bool_param(params, "include_dataview", "dataview", default=False)
            or kind_flags["dataview"]
        )
        include_canvas = (
            _bool_param(params, "include_canvas", "canvas", default=False)
            or kind_flags["canvas"]
        )
        filename_mode = _str_param(params, "filename_mode", default="name")
        # spec §3.5 "强制重新导出": wipe the existing export dir before
        # regenerating. Task history (DB rows) is never touched here.
        force = _bool_param(params, "force", default=False)
        if force:
            clean = True

        context.progress(10, "exporting obsidian vault")
        try:
            report = export_obsidian_markdown(
                story_path,
                out_dir_path,
                single_file=single_file_path,
                clean=clean,
                include_dataview=include_dataview,
                include_canvas=include_canvas,
                filename_mode=filename_mode,
            )
        finally:
            # Remove the temp story snapshot; never touch canonical project JSON.
            if isinstance(prepared_input, dict) and prepared_input.get("story_snapshot_path"):
                try:
                    Path(prepared_input["story_snapshot_path"]).unlink()
                except OSError:
                    pass
        # Report the canonical source path (the snapshot was an internal stable copy).
        report = {**report, "source_file": str(canonical_story_path)}
        context.progress(90, "export complete")

        artifacts = [
            {"name": "vault", "path": str(out_dir_path)},
            {"name": "export_report", "path": str(out_dir_path / "_meta" / "export_report.json")},
        ]
        if single_file_path:
            artifacts.append({"name": "single_file", "path": str(single_file_path)})

        revision = project_revisions(project_dir)
        context.progress(100, "done")
        return _result_json(
            artifacts=artifacts,
            manifest=report,
            manifest_path=str(out_dir_path / "_meta" / "export_report.json"),
            logs=[],
            revision=revision,
            task="export",
            source=source,
            single_file=str(single_file_path) if single_file_path else "",
            include_dataview=include_dataview,
            include_canvas=include_canvas,
            clean=clean,
            filename_mode=filename_mode,
            kinds=list(params.get("kinds") or []),
            force=force,
        )


# === GovernanceTaskAdapter (writer) ======================================

class GovernanceTaskAdapter:
    """Writer adapter: run a periodic governance cycle for a chapter range.

    Sequence mirrors run_pipeline.run_periodic_governance minus the supervision
    loop: build the audit pack → (optional) audit correction worker →
    commit_governance (which performs validate_delta → merge_delta →
    apply_governance_ops → normalize → validate_structure, with before/after/diff
    snapshots and automatic rollback on any step failure).
    """

    def __init__(self, manager: object = None):
        # The adapter does not enqueue follow-up tasks; manager is accepted
        # purely for symmetry with the append adapter and future use.
        self.manager = manager

    @staticmethod
    def _resolve_range(project_dir: Path, params: dict) -> tuple[int, int]:
        """Resolve the audit chapter range from the spec's ``chapters`` param,
        falling back to an explicit ``start``/``end`` for backward compat."""
        chapters = _str_param(params, "chapters", default="")
        if chapters:
            return _parse_governance_chapters(project_dir, chapters)
        start = int(params.get("start", params.get("seq_start", 1)))
        end = int(params.get("end", params.get("seq_end", 0)))
        if end < start:
            raise ValueError(
                "governance 需要 'chapters' (all/recent/N-M) 或显式的 'start'<='end'"
            )
        return start, end

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        start, end = self._resolve_range(project_dir, params)
        force = _bool_param(params, "force", default=False)
        max_context_chars = _int_param(
            params, "max_context_chars",
            int(_setting(context, "audit_max_context_chars", DEFAULT_AUDIT_MAX_CONTEXT_CHARS)),
        )
        worker_command = _str_param(
            params, "worker_command", "audit_command", default=""
        )
        worker_timeout = _int_param(
            params, "worker_timeout_seconds",
            int(_setting(context, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)),
        )
        worker_retries = _int_param(
            params, "worker_retries",
            int(_setting(context, "worker_retries", DEFAULT_WORKER_RETRIES)),
        )
        worker_window = _str_param(
            params, "worker_window",
            default=str(_setting(context, "worker_window", DEFAULT_WORKER_WINDOW)),
        )

        # 周期审计的职责是审计已提交章节。零已提交章节（无 story_after_chNNN.json）
        # 时无内容可审，跑下去只会让 worker 产出空 no_change 并刷出空快照。直接
        # 短路返回，不建 pack、不调 worker/commit。与 governance.md「禁止跳过真实
        # 审计」不冲突：那是禁止对有内容的审计偷懒，这里根本没有已提交章节。
        if latest_snapshot_seq(project_dir) is None:
            revision = project_revisions(project_dir)
            context.progress(100, "skipped: no committed chapters")
            return _result_json(
                artifacts=[],
                manifest={"status": "skipped_no_chapters",
                          "range": f"{start:03d}-{end:03d}"},
                manifest_path="",
                logs=["尚无已提交章节（无 story_after_chNNN.json），周期治理无内容可审，已跳过。"],
                revision=revision,
                task="governance",
                chapters=_str_param(params, "chapters", default=""),
                start=start,
                end=end,
            )

        # The workbench and automatic pipeline share one split/resume owner.
        # Preparing children and then dispatching the absent parent pack would
        # bypass capacity recovery and repeat already committed child audits.
        if force:
            rc = prepare_audit_pack_or_split(project_dir, start, end, force=True,
                                            max_context_chars=max_context_chars)
            if rc:
                raise RuntimeError(f"audit pack 准备失败 rc={rc}")
        if not worker_command:
            provider = _str_param(params, "worker_provider", default="") or context.worker_provider
            if provider and provider != "manual":
                worker_command = builtin_agent_worker_command(provider)
        context.progress(30, "周期治理：程序校验、必要模型处理与断点续跑")
        rc = run_periodic_governance(project_dir, start, end, max_context_chars=max_context_chars,
                                     audit_command=worker_command, worker_timeout_seconds=worker_timeout,
                                     worker_retries=worker_retries, worker_window=worker_window,
                                     worker_loop="continuous")
        if rc:
            raise RuntimeError(f"governance 自动治理未完成 rc={rc}；材料与断点已保留")
        paths = audit_paths(project_dir, start, end)
        correction_path = paths["correction"]

        status = audit_status(project_dir, start, end)
        before = status.get("before", "") or ""
        after = status.get("after", "") or ""
        diff = status.get("diff", "") or ""

        artifacts = [
            {"name": "audit_pack", "path": str(paths["pack"])},
            {"name": "correction", "path": str(correction_path)},
            {"name": "audit_status", "path": str(paths["status"])},
        ]
        if before:
            artifacts.append({"name": "before", "path": before})
        if after:
            artifacts.append({"name": "after", "path": after})
        if diff:
            artifacts.append({"name": "diff", "path": diff})

        revision = project_revisions(project_dir)
        context.progress(100, "governance committed")
        return _result_json(
            artifacts=artifacts,
            manifest=status,
            manifest_path=str(paths["status"]),
            logs=[],
            revision=revision,
            task="governance",
            chapters=_str_param(params, "chapters", default=""),
            start=start,
            end=end,
            before=before,
            after=after,
            diff=diff,
        )


# === NormalizeAdapter (writer) ============================================

class NormalizeAdapter:
    """Writer adapter: preserving-style normalization of the process story JSON."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        story_path = project_dir / "故事结构_增量.json"
        if not story_path.is_file():
            raise RuntimeError("故事结构_增量.json 未找到")
        data = json.loads(story_path.read_text(encoding="utf-8"))
        context.progress(40, "normalizing story schema")
        normalized, logs = normalize_structure(data, quarantine_invalid_refs=False)
        # 规范化直接更新权威过程结构，必须原子替换以保证自动恢复始终可读。
        save_json(str(story_path), normalized)
        return _result_json(
            artifacts=[str(story_path)],
            manifest={"logs": len(logs)},
            manifest_path="",
            logs=logs,
            revision=project_revisions(project_dir),
        )


# === MigrateChronologyAdapter (writer) ===================================

class MigrateChronologyAdapter:
    """Writer adapter: chronology migration (time backfill, retrospective
    compaction, event reorder) on the process story JSON."""

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        dry_run = _bool_param(params, "dry_run", default=False)
        context.progress(30, "migrating chronology")
        rc = run_migrate(project_dir, dry_run=dry_run)
        if rc != 0:
            raise RuntimeError(f"chronology migration 失败 rc={rc}")
        report = project_dir / "质量治理" / "规范化" / "chronology_migration.txt"
        return _result_json(
            artifacts=[str(report)] if report.is_file() else [],
            manifest={"dry_run": dry_run},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
        )


# === RecoverStoryAdapter (writer) ========================================

def list_recover_snapshots(project_dir: Path) -> list[dict]:
    """List ``story_after_chNNN.json`` snapshots in the project's version dir.

    Read-only: globs the engine's own discovery pattern and reports each
    snapshot's seq, relative path, size, and the max delta seq it could be
    replayed up to (``max_available_delta_seq``). Returns ``[]`` when the
    version dir or snapshots are absent.
    """
    project_dir = Path(project_dir)
    version_dir = project_dir / "故事结构版本"
    max_delta = max_available_delta_seq(project_dir)
    snaps: list[dict] = []
    if not version_dir.is_dir():
        return snaps
    for p in sorted(version_dir.glob("story_after_ch*.json")):
        m = re.match(r"story_after_ch(\d+)\.json$", p.name)
        if not m:
            continue
        snaps.append({
            "snapshot_seq": int(m.group(1)),
            "path": str(p.relative_to(project_dir)),
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
            raise ValueError("recover_story 需要 'snapshot_seq'")
        available = {s["snapshot_seq"] for s in list_recover_snapshots(project_dir)}
        if snapshot_seq not in available:
            raise RuntimeError(f"snapshot seq {snapshot_seq} 未找到")
        before = project_dir / "故事结构_增量.json"
        before_bytes = before.read_bytes() if before.is_file() else b""
        context.progress(20, f"recovering to snapshot seq={snapshot_seq}")
        rc = recover_story_from_snapshot(project_dir, snapshot_seq=snapshot_seq)
        if rc != 0:
            if before_bytes:
                before.write_bytes(before_bytes)
            raise RuntimeError(f"recover_story 失败 rc={rc}")
        context.progress(90, "recovery complete")
        return _result_json(
            artifacts=[str(before)],
            manifest={
                "snapshot_seq": snapshot_seq,
                "replayed_to": max_available_delta_seq(project_dir),
            },
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
            snapshot_seq=snapshot_seq,
        )


# === AppendChaptersTaskAdapter (writer) ==================================

class AppendChaptersTaskAdapter:
    """Writer adapter: apply a previewed append plan under the writer lock.

    Holds the project writer lock for the whole run (no prepare) because
    apply_append mutates the chapter index.  After applying the plan it
    refreshes revisions and queues chapter_analysis_rerun + chapter_delta_rerun
    for the new seqs only — via the manager's task-creation path so writer
    admission is preserved (the new tasks stay queued until this run releases
    the lock).
    """

    def __init__(self, manager: object = None):
        self.manager = manager

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        plan = params.get("plan")
        if not isinstance(plan, dict):
            raise ValueError("append_chapters 需要 'plan'（先预览）")
        if plan.get("conflict"):
            raise ValueError(
                f"append_chapters plan 存在前缀冲突: {plan['conflict'].get('message', '')}"
            )
        if not plan.get("appended"):
            raise ValueError("append_chapters plan 没有新章节可追加")

        project_dir = Path(context.project_path)

        # Confine the plan's split_dir to the canonical project chapter dir so
        # the writer lock this adapter holds actually covers the index being
        # mutated — a tampered or stale plan must not escape the project root.
        plan["split_dir"] = str(project_dir / "原文拆解")

        context.progress(20, "appending new chapter files")
        try:
            report = apply_append(plan)
        except SystemExit as exc:
            # CLI helper uses SystemExit for input errors; scheduler threads must
            # convert it to an ordinary task failure instead of exiting silently.
            raise ValueError(str(exc)) from exc
        context.progress(70, "queueing new chapter production work")

        new_seqs = [int(entry["seq"]) for entry in report.get("appended", []) if entry.get("seq") is not None]
        queued_tasks: list[dict] = []
        if self.manager is not None and new_seqs:
            for seq in new_seqs:
                created = self.manager.create(
                    context.project_id, "chapter_analysis_rerun", {"seqs": [seq]}
                )
                queued_tasks.append({"type": "chapter_analysis_rerun", "seq": seq, "id": created.get("id")})
                created = self.manager.create(
                    context.project_id, "chapter_delta_rerun", {"seqs": [seq]}
                )
                queued_tasks.append({"type": "chapter_delta_rerun", "seq": seq, "id": created.get("id")})

        revision = project_revisions(project_dir)
        context.progress(100, "append complete")
        return _result_json(
            artifacts=[{"name": "index", "path": report["index_path"]}],
            manifest=report,
            manifest_path=report["index_path"],
            logs=[],
            revision=revision,
            task="append_chapters",
            appended=report["appended"],
            new_seqs=new_seqs,
            queued_tasks=queued_tasks,
        )


# === FinalizeAdapter (writer, worker-backed) ===============================

class FinalizeAdapter:
    """Writer, worker-backed adapter: rerun finalization on demand.

    Mirrors orchestrator ``_stage_final_pack`` / ``_stage_commit_final_draft``
    / ``_stage_finalize``: completed cycles → program preparation → formal introduction →
    ``cmd_commit_final_draft`` (repair loop on failure) → ``cmd_validate_final``
    → ``cmd_finalize``. Holds the project writer lock for the whole run.
    """

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        worker_command = _str_param(params, "worker_command", default="")
        worker_timeout = _int_param(
            params, "worker_timeout_seconds",
            int(_setting(context, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)),
        )
        worker_window = _str_param(
            params, "worker_window",
            default=str(_setting(context, "worker_window", DEFAULT_WORKER_WINDOW)),
        )
        worker_retries = _int_param(
            params, "worker_retries", int(_setting(context, "worker_retries", DEFAULT_WORKER_RETRIES))
        )
        max_repair_rounds = _int_param(
            params, "max_repair_rounds", int(_setting(context, "max_repair_rounds", 3))
        )
        # 任务参数未显式指定 worker_command 时，按全局 worker_provider 自动解析内置
        # agent_worker 命令（默认 agy），对齐 orchestrator 的 _worker_command() 与
        # GovernanceTaskAdapter/AnalysisAdapter，使 MaintenanceTab 一键 finalize
        # 无需手动配 worker 命令（spec §3.2：worker 步是 finalize 链的一环）。
        if not worker_command:
            provider = _str_param(params, "worker_provider", default="") or context.worker_provider
            if provider and provider != "manual":
                worker_command = builtin_agent_worker_command(provider)

        draft = project_dir / "故事结构_草稿.json"
        repair = project_dir / "质量治理" / "最终审计" / "repair_final.md"

        from story_delivery import prepare_delivery, finish_introduction, ensure_delivery_governance
        rc = ensure_delivery_governance(project_dir, worker_command,
                                        timeout=worker_timeout, retries=worker_retries,
                                        worker_window=worker_window)
        if rc != 0:
            raise RuntimeError(f"正式交付前章节或周期治理未完成 rc={rc}")
        context.progress(10, "准备正式故事交付")
        rc = prepare_delivery(project_dir)
        if rc != 0:
            raise RuntimeError(f"final_pack 失败 rc={rc}")
        context.progress(30, "正式简介收尾")
        rc = finish_introduction(project_dir, worker_command, run_worker,
                                 timeout=worker_timeout, retries=worker_retries,
                                 worker_window=worker_window)
        if rc != 0:
            raise RuntimeError(f"正式简介收尾未完成 rc={rc}；断点已保留")

        context.progress(60, "commit final draft")
        rc = cmd_commit_final_draft(project_dir)
        if rc != 0:
            if not repair.is_file() or not worker_command:
                raise RuntimeError(f"commit_final_draft 失败 rc={rc} 且无 worker_command 或 repair 任务")
            rc = run_repair_worker(
                worker_command, project_dir, "repair_final", repair, draft,
                timeout=worker_timeout, retries=worker_retries, worker_window=worker_window,
                repair_limit=max_repair_rounds,
                observe=lambda detail, data: context.progress(65, detail),
            )
            if rc != 0:
                raise RuntimeError(f"repair_final 自动故障处理未完成 rc={rc}；诊断与断点已保留")

        context.progress(80, "validate final")
        rc = cmd_validate_final(project_dir)
        if rc != 0:
            raise RuntimeError(f"validate_final 失败 rc={rc}")
        context.progress(95, "finalize")
        rc = cmd_finalize(project_dir)
        if rc != 0:
            raise RuntimeError(f"finalize 失败 rc={rc}")
        final = project_dir / "故事结构.json"
        context.progress(100, "finalize complete")
        return _result_json(
            artifacts=[str(final)],
            manifest={"final": str(final)},
            manifest_path="",
            logs=[],
            revision=project_revisions(project_dir),
            task="finalize",
        )

