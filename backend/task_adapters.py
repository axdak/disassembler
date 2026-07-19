"""Workbench task adapters: bridge persistent scheduler to engine scoped entry points.

Six adapters back the workbench task types introduced in Task 3:

- ``analysis`` (reader, prepare) — builds one analysis task pack for a scope.
- ``chapter_visual`` (reader, prepare) — per-chapter visual_assets packs or aggregate.
- ``chapter_structure`` (reader, prepare) — per-chapter chapter_structure packs.
- ``chapter_analysis_rerun`` (writer, no prepare) — regenerate chapter analysis task packs.
- ``chapter_delta_rerun`` (writer, no prepare) — regenerate chapter delta task packs.
- ``export`` (reader, prepare) — export story JSON to an Obsidian vault.

Readers snapshot stable inputs under reader admission (writers excluded) in ``prepare``,
then run the engine scoped function against the snapshot outside any lock and merge
Markdown task packages back to the real project dir. Writers hold the project writer lock
for the whole run (no prepare) and run directly against the live project dir.
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

from run_pipeline import (  # noqa: E402
    DEFAULT_AUDIT_MAX_CONTEXT_CHARS,
    DEFAULT_WORKER_RETRIES,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    DEFAULT_WORKER_WINDOW,
    audit_paths,
    audit_status,
    commit_governance,
    copy_analysis_task_packages,
    latest_analysis_manifest,
    latest_snapshot_seq,
    load_json,
    prepare_audit_pack_or_split,
    run_worker,
    scoped_analysis_pack,
    scoped_chapter_analysis_rerun,
    scoped_chapter_delta_rerun,
    scoped_chapter_structure_pack,
    scoped_chapter_visual_aggregate,
    scoped_chapter_visual_pack,
    snapshot_project_inputs,
    unresolved_audits,
)
from export_obsidian_vault import export_obsidian_markdown  # noqa: E402
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
            raise ValueError("governance 'all' requires chapters in the project index")
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
            raise ValueError("governance 'recent' has no unaudited chapters and no index")
        return start, end
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", chapters)
    if not m:
        raise ValueError(f"governance 'chapters' must be all/recent/N-M, got: {chapters!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        raise ValueError(f"governance 'chapters' requires start <= end: {chapters!r}")
    return start, end


def _latest_manifest(project_dir: Path, suffix: str) -> Optional[Path]:
    """Locate the most recent manifest matching suffix in the real project dir."""
    path = latest_analysis_manifest(project_dir, suffix)
    return Path(path) if path is not None else None


# === AnalysisAdapter =======================================================

class AnalysisAdapter:
    """Reader adapter: build one analysis task pack for a chapter scope."""

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    @staticmethod
    def _parse_params(params: dict) -> dict:
        """Translate the spec public params to engine call params.

        spec §8/§3.3: ``{task, chapters, targets, question, original_text,
        force}``. The ``original_text`` alias is the canonical UI name; legacy
        ``include_original``/``original`` remain accepted.
        """
        return {
            "task": _str_param(params, "task", "analysis_task", "name", default="summary"),
            "chapters": _str_param(params, "chapters", "chapter_range", default="all"),
            "targets": params.get("targets") or None,
            "question": _str_param(params, "question", default=""),
            "include_original": _str_param(
                params, "include_original", "original", "original_text", default="sample"
            ),
            "force": _bool_param(params, "force", default=False),
            "max_pack_chars": _int_param(params, "max_pack_chars", 70000),
            "max_original_chars": _int_param(params, "max_original_chars", 6000),
            "max_analysis_chars": _int_param(params, "max_analysis_chars", 12000),
        }

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        parsed = self._parse_params(params)
        task = parsed["task"]
        chapters = parsed["chapters"]
        targets = parsed["targets"]
        question = parsed["question"]
        include_original = parsed["include_original"]
        force = parsed["force"]
        max_pack_chars = parsed["max_pack_chars"]
        max_original_chars = parsed["max_original_chars"]
        max_analysis_chars = parsed["max_analysis_chars"]

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

        artifacts = [{"name": "manifest", "path": str(manifest_path)}]
        for index, pack in enumerate(packs):
            if isinstance(pack, str):
                artifacts.append({"name": f"pack_{index + 1:03d}", "path": str(project_dir / pack)})
            elif isinstance(pack, dict):
                rel = pack.get("pack") or pack.get("pack_path")
                if rel:
                    artifacts.append(
                        {"name": f"pack_ch{pack.get('seq', 0):03d}", "path": str(project_dir / rel)}
                    )

        revision = project_revisions(project_dir)
        context.progress(100, "analysis task pack ready")
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
            include_original=include_original,
            force=force,
        )


# === ChapterVisualAdapter ==================================================

class ChapterVisualAdapter:
    """Reader adapter: per-chapter visual_assets packs, or the aggregate pack."""

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_visual requires a non-empty 'seqs' list")
        seqs = [int(s) for s in seqs]
        aggregate = _bool_param(params, "aggregate", default=False)
        include_original = _str_param(params, "include_original", "original", default="sample")
        max_pack_chars = _int_param(params, "max_pack_chars", 70000)
        max_original_chars = _int_param(params, "max_original_chars", 6000)
        max_analysis_chars = _int_param(params, "max_analysis_chars", 12000)
        force_aggregate = _bool_param(params, "force_aggregate", default=False)

        project_dir = Path(context.project_path)
        snapshot_dir = (
            prepared_input["snapshot_dir"]
            if isinstance(prepared_input, dict) and prepared_input.get("snapshot_dir")
            else snapshot_project_inputs(project_dir)
        )

        revision = project_revisions(project_dir)
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
                target_outputs = manifest.get("target_outputs") or []
                artifacts.append({"name": "manifest", "path": str(manifest_path)})
                context.progress(100, "aggregate pack ready")
                return _result_json(
                    artifacts=artifacts,
                    manifest=manifest,
                    manifest_path=str(manifest_path),
                    logs=[],
                    revision=revision,
                    task="visual_assets",
                    mode="aggregate",
                    target_outputs=target_outputs,
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
            )
            context.progress(70, "merging per-chapter packs into project")
            copy_analysis_task_packages(snapshot_dir, project_dir)
            manifest_path = (
                _latest_manifest(project_dir, "visual_assets_per_chapter")
                or Path(scoped["manifest_path"])
            )
            manifest = load_json(manifest_path, {}) or {}
            packs = manifest.get("packs") or []
            for record in packs:
                rel = record.get("pack")
                if rel:
                    artifacts.append(
                        {"name": f"pack_ch{record.get('seq', 0):03d}", "path": str(project_dir / rel)}
                    )
            artifacts.append({"name": "manifest", "path": str(manifest_path)})
            context.progress(100, "per-chapter packs ready")
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
    """Reader adapter: per-chapter chapter_structure task packs."""

    def prepare(self, context: TaskContext) -> dict:
        project_dir = Path(context.project_path)
        snapshot_dir = snapshot_project_inputs(project_dir)
        return {"snapshot_dir": snapshot_dir, "project_dir": project_dir}

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        seqs = params.get("seqs") or []
        if not seqs:
            raise ValueError("chapter_structure requires a non-empty 'seqs' list")
        seqs = [int(s) for s in seqs]
        include_original = _str_param(params, "include_original", "original", default="sample")
        max_pack_chars = _int_param(params, "max_pack_chars", 70000)
        max_original_chars = _int_param(params, "max_original_chars", 6000)
        max_analysis_chars = _int_param(params, "max_analysis_chars", 12000)

        project_dir = Path(context.project_path)
        snapshot_dir = (
            prepared_input["snapshot_dir"]
            if isinstance(prepared_input, dict) and prepared_input.get("snapshot_dir")
            else snapshot_project_inputs(project_dir)
        )
        try:
            context.progress(10, "generating chapter_structure per-chapter packs")
            scoped = scoped_chapter_structure_pack(
                snapshot_dir,
                seqs=seqs,
                include_original=include_original,
                max_pack_chars=max_pack_chars,
                max_original_chars=max_original_chars,
                max_analysis_chars=max_analysis_chars,
            )
            context.progress(70, "merging per-chapter packs into project")
            copy_analysis_task_packages(snapshot_dir, project_dir)
            manifest_path = (
                _latest_manifest(project_dir, "chapter_structure_per_chapter")
                or Path(scoped["manifest_path"])
            )
            manifest = load_json(manifest_path, {}) or {}
            packs = manifest.get("packs") or []
            artifacts = [{"name": "manifest", "path": str(manifest_path)}]
            for record in packs:
                rel = record.get("pack")
                if rel:
                    artifacts.append(
                        {"name": f"pack_ch{record.get('seq', 0):03d}", "path": str(project_dir / rel)}
                    )
            revision = project_revisions(project_dir)
            context.progress(100, "per-chapter packs ready")
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
            raise ValueError("chapter_analysis_rerun requires a non-empty 'seqs' list")
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
            raise ValueError("chapter_delta_rerun requires a non-empty 'seqs' list")
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


# === ExportAdapter (reader) ================================================

class ExportAdapter:
    """Reader adapter: export story structure JSON to an Obsidian vault."""

    def prepare(self, context: TaskContext) -> dict:
        params = context.params or {}
        source = _str_param(params, "source", "export_source", default="final").lower()
        project_dir = Path(context.project_path)
        story_path = self._resolve_story_path(project_dir, source)
        snapshot = Path(tempfile.mktemp(prefix="nd_story_", suffix=".json"))
        shutil.copy2(story_path, snapshot)
        return {"story_snapshot_path": snapshot, "project_dir": project_dir, "source": source}

    @staticmethod
    def _resolve_story_path(project_dir: Path, source: str) -> Path:
        if source == "process":
            candidate = project_dir / "故事结构_增量.json"
        else:
            candidate = project_dir / "故事结构.json"
        if not candidate.is_file():
            raise FileNotFoundError(f"story structure file not found: {candidate}")
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
                "governance requires 'chapters' (all/recent/N-M) or explicit 'start'<='end'"
            )
        return start, end

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        project_dir = Path(context.project_path)
        start, end = self._resolve_range(project_dir, params)
        force = _bool_param(params, "force", default=False)
        max_context_chars = _int_param(
            params, "max_context_chars", DEFAULT_AUDIT_MAX_CONTEXT_CHARS
        )
        worker_command = _str_param(
            params, "worker_command", "audit_command", default=""
        )
        worker_timeout = _int_param(
            params, "worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS
        )
        worker_window = _str_param(params, "worker_window", default=DEFAULT_WORKER_WINDOW)

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

        context.progress(10, "preparing audit pack")
        rc = prepare_audit_pack_or_split(
            project_dir, start, end,
            force=force, max_context_chars=max_context_chars,
        )
        if rc != 0:
            raise RuntimeError(f"audit pack preparation failed rc={rc}")

        paths = audit_paths(project_dir, start, end)
        correction_path = paths["correction"]

        # Worker correction step: only invoke when the correction is missing.
        if not correction_path.is_file() or correction_path.stat().st_size == 0:
            if not worker_command:
                raise RuntimeError(
                    f"governance correction missing and no worker_command provided: {correction_path}"
                )
            context.progress(30, "running audit correction worker")
            rc = run_worker(
                worker_command, project_dir, "audit_correction",
                paths["pack"], correction_path,
                timeout=worker_timeout, retries=0, worker_window=worker_window,
            )
            if rc != 0:
                raise RuntimeError(f"audit correction worker failed rc={rc}")

        context.progress(60, "committing governance patch")
        rc = commit_governance(project_dir, str(correction_path))
        if rc != 0:
            # commit_governance has already restored the before snapshot.
            raise RuntimeError(f"governance commit failed rc={rc}")

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
            raise ValueError("append_chapters requires a 'plan' (preview first)")
        if plan.get("conflict"):
            raise ValueError(
                f"append_chapters plan has a prefix conflict: {plan['conflict'].get('message', '')}"
            )
        if not plan.get("appended"):
            raise ValueError("append_chapters plan has no new chapters to append")

        project_dir = Path(context.project_path)

        # Confine the plan's split_dir to the canonical project chapter dir so
        # the writer lock this adapter holds actually covers the index being
        # mutated — a tampered or stale plan must not escape the project root.
        plan["split_dir"] = str(project_dir / "原文拆解")

        context.progress(20, "appending new chapter files")
        report = apply_append(plan)
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

