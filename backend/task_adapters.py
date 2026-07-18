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
    copy_analysis_task_packages,
    latest_analysis_manifest,
    load_json,
    scoped_analysis_pack,
    scoped_chapter_analysis_rerun,
    scoped_chapter_delta_rerun,
    scoped_chapter_structure_pack,
    scoped_chapter_visual_aggregate,
    scoped_chapter_visual_pack,
    snapshot_project_inputs,
)
from export_obsidian_vault import export_obsidian_markdown  # noqa: E402


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

    def run(self, context: TaskContext, prepared_input: object = None) -> dict:
        params = context.params or {}
        task = _str_param(params, "task", "analysis_task", "name", default="summary")
        chapters = _str_param(params, "chapters", "chapter_range", default="all")
        targets = params.get("targets") or None
        question = _str_param(params, "question", default="")
        include_original = _str_param(
            params, "include_original", "original", default="sample"
        )
        force = _bool_param(params, "force", default=False)
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

        single_file = params.get("single_file")
        single_file_path: Optional[Path] = None
        if single_file:
            single_file_path = Path(single_file)
            if not single_file_path.is_absolute():
                single_file_path = (project_dir / single_file_path).resolve()

        clean = _bool_param(params, "clean", default=False)
        include_dataview = _bool_param(
            params, "include_dataview", "dataview", default=False
        )
        include_canvas = _bool_param(params, "include_canvas", "canvas", default=False)
        filename_mode = _str_param(params, "filename_mode", default="name")

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
        )
