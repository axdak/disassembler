"""Downstream reports must use the exact delivered story, never stale process facts."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from analysis_context_pack import build_pack, main, split_rendered_pack
from worker_checkpoint import run_checkpointed_partial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worker_context import ContextReader, snapshot_context


def write_json(path, value):
    """Write independent story variants without introducing validation/model work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def delivered_story(root):
    """A receipt binds the delivered repair to its process and draft revisions."""
    process, draft, final = [root / name for name in (
        "故事结构_增量.json", "故事结构_草稿.json", "故事结构.json")]
    write_json(process, {"介绍": {"描述": "过程旧简介"}, "角色集": [{"名称": "过程旧名"}]})
    repaired = {"介绍": {"描述": "交付简介"}, "角色集": [{"名称": "交付新名"}]}
    write_json(draft, repaired)
    write_json(final, repaired)
    receipt = root / "质量治理/最终审计/finalize_log.json"
    write_json(receipt, {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in (
        ("process_sha256", process), ("draft_sha256", draft), ("final_sha256", final))})
    return process, draft, final, receipt


def report_pack(root):
    """Render a report without chapter files so story provenance stays observable."""
    return build_pack(root, "report", [], [], "", "none", 6000, 12000, 1, 1)


def test_report_uses_verified_delivery_instead_of_process_name(tmp_path):
    process, _, final, _ = delivered_story(tmp_path)
    text = report_pack(tmp_path)
    assert "交付新名" in text
    assert "交付简介" in text
    assert "过程旧名" not in text
    assert str(final) in text
    assert str(process) not in text


@pytest.mark.parametrize("changed", ["process", "draft", "final", "legacy"])
def test_unpublished_or_changed_delivery_keeps_process_source(tmp_path, changed):
    process, draft, final, receipt = delivered_story(tmp_path)
    if changed == "legacy":
        write_json(receipt, {"final_size_bytes": final.stat().st_size})
    else:
        path = {"process": process, "draft": draft, "final": final}[changed]
        path.write_bytes(path.read_bytes() + b" ")
    text = report_pack(tmp_path)
    assert "过程旧名" in text
    assert "交付新名" not in text
    assert str(process) in text
    if changed == "legacy":
        assert "旧交付回执" in text


def test_every_split_retains_same_story_source_for_frozen_tools(tmp_path):
    _, _, final, _ = delivered_story(tmp_path)
    text = report_pack(tmp_path) + "完整下游分析材料" * 3000
    for index, piece in enumerate(split_rendered_pack(text, 8000)):
        assert str(final) in piece
        pack = tmp_path / f"pack_{index}.md"
        pack.write_text(piece, encoding="utf-8")
        sandbox = tmp_path / f"sandbox_{index}"
        sandbox.mkdir()
        reader = ContextReader(snapshot_context(tmp_path, pack, [], sandbox))
        assert reader.story_snapshot()["source"] == str(final)
        assert reader.story_get_units([{"collection": "角色集", "name": "交付新名"}])["items"]


def test_checkpoint_tracks_declared_final_detail_changes(tmp_path):
    process, draft, final, receipt = delivered_story(tmp_path)
    pack, output = tmp_path / "pack.md", tmp_path / "partial.md"
    # The index and task text stay identical when a detail field changes.
    pack.write_text(f"{output}\n## 原任务包内容\n\n当前权威故事档案：`{final}`\n同一索引", encoding="utf-8")
    calls = []

    def worker(command, root, task, actual_pack, target, **kwargs):
        calls.append(1)
        target.write_text(f"结果{len(calls)}", encoding="utf-8")
        return 0

    assert run_checkpointed_partial(worker, "offline", tmp_path, "report", pack, output) == 0
    assert run_checkpointed_partial(worker, "offline", tmp_path, "report", pack, output) == 0
    assert len(calls) == 1
    changed = {"角色集": [{"名称": "交付新名", "详情": {"状态": "已修正的新事实"}}]}
    write_json(final, changed)
    assert run_checkpointed_partial(worker, "offline", tmp_path, "report", pack, output) == 0
    assert len(calls) == 2


def test_main_manifest_records_delivered_source_and_revision(tmp_path):
    _, _, final, _ = delivered_story(tmp_path)
    chapter = tmp_path / "原文拆解/第001章_序章.md"
    chapter.parent.mkdir()
    chapter.write_text("章节证据", encoding="utf-8")
    write_json(chapter.parent / "_索引.json", {"chapters": [{"seq": 1, "filename": chapter.name}]})
    assert main([str(tmp_path), "--task", "report"]) == 0
    manifest = json.loads(next((tmp_path / "全书分析/_任务包").glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["story_source"] == str(final)
    assert manifest["story_revision"] == hashlib.sha256(final.read_bytes()).hexdigest()


def test_reduce_wrapper_registers_the_same_final_as_partials(tmp_path):
    """Reduction keeps the manifest authority even when partial prose omits it."""
    from run_pipeline import write_fullbook_partial_worker_pack, write_fullbook_reduce_worker_pack
    _, _, final, _ = delivered_story(tmp_path)
    pack, partial, target = [tmp_path / name for name in ("original.md", "partial.md", "report.md")]
    pack.write_text(report_pack(tmp_path), encoding="utf-8")
    partial.write_text("分包结论", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"task": "report", "packs": [pack.name], "story_source": str(final),
                          "story_revision": hashlib.sha256(final.read_bytes()).hexdigest()})
    first = write_fullbook_partial_worker_pack(tmp_path, manifest, pack.name, 1, 1, partial)
    reduced = write_fullbook_reduce_worker_pack(tmp_path, manifest, [partial], [target])
    for index, task in enumerate((first, reduced)):
        assert f"当前权威故事档案：`{final}`" in task.read_text(encoding="utf-8")
        frozen = tmp_path / f"worker_{index}"
        frozen.mkdir()
        reader = ContextReader(snapshot_context(tmp_path, task, [], frozen))
        assert reader.story_snapshot()["source"] == str(final)
    hierarchy = json.loads(reduced.with_suffix(".hierarchy.json").read_text(encoding="utf-8"))
    assert str(final) in hierarchy["references"]
    assert str(final) not in hierarchy["evidence"]


def test_snapshot_refuses_source_changed_after_package_build(tmp_path):
    """The header revision and frozen tool bytes must describe one version."""
    _, _, final, _ = delivered_story(tmp_path)
    pack = tmp_path / "task.md"
    pack.write_text(report_pack(tmp_path), encoding="utf-8")
    final.write_bytes(final.read_bytes() + b" ")
    frozen = tmp_path / "worker"
    frozen.mkdir()
    with pytest.raises(ValueError, match="版本变化"):
        snapshot_context(tmp_path, pack, [], frozen)


def test_bounded_request_accepts_only_host_aliased_story_authority(tmp_path):
    """A nested capacity request resolves the logical story to its frozen alias."""
    _, _, final, _ = delivered_story(tmp_path)
    request = tmp_path / "tree/request"
    request.mkdir(parents=True)
    pack = request / "task.md"
    pack.write_text(report_pack(tmp_path), encoding="utf-8")
    snapshot = tmp_path / "tree/frozen_story.json"
    snapshot.write_bytes(final.read_bytes())
    write_json(pack.with_suffix(".context-inputs.json"), [{"source": str(final), "snapshot": str(snapshot)}])
    final.write_text("live source is no longer authoritative", encoding="utf-8")
    frozen = request / "worker"
    frozen.mkdir()
    reader = ContextReader(snapshot_context(request, pack, [], frozen))
    assert reader.story_get_units([{"collection": "角色集", "name": "交付新名"}])["items"]


def test_hierarchical_report_freezes_final_as_lookup_reference(tmp_path):
    """Nested generation/review can query final facts without full archive leaves."""
    from bounded_generation import run_hierarchical_reports
    from context_budget import capacity_scope
    from test_capacity_tree import review_response
    _, _, final, _ = delivered_story(tmp_path)
    material, pack, output = [tmp_path / name for name in ("material.md", "task.md", "report.md")]
    material.write_text("本次完整分析证据", encoding="utf-8")
    contract = f"当前权威故事档案：`{final}`\n当前故事版本：{hashlib.sha256(final.read_bytes()).hexdigest()}\n完整报告"
    pack.write_text(contract, encoding="utf-8")
    write_json(pack.with_suffix(".hierarchy.json"), {"sources": [str(material)],
               "references": [str(final)], "contract": contract})
    queries = []

    def worker(command, root, kind, task, result, **options):
        frozen = root / "reader"
        frozen.mkdir(exist_ok=True)
        reader = ContextReader(snapshot_context(root, task, [], frozen))
        assert reader.story_get_units([{"collection": "角色集", "name": "交付新名"}])["items"]
        queries.append(reader.story_snapshot()["revision"])
        prompt = task.read_text(encoding="utf-8")
        write_json(result, review_response(prompt) or {
            "overview": "原文与交付档案已核对", "content": "# 报告\n交付新名的完整分析。"})
        return 0

    with capacity_scope(dict(model_input_tokens=12000, model_output_tokens=2000,
                             model_context_tokens=15000, model_reserve_tokens=1000)):
        assert run_hierarchical_reports("offline", tmp_path, pack, [output], worker) == 0
    assert queries
    # Generation and review retain lookup archives outside their mandatory leaves.
    for source_list in (tmp_path / "质量治理/容量分层").rglob("sources.json"):
        assert str(final) not in [item[0] for item in json.loads(source_list.read_text(encoding="utf-8"))]


def test_package_consumes_the_same_final_bytes_that_were_verified(tmp_path, monkeypatch):
    """A concurrent replacement cannot slip unverified facts into a new package."""
    import analysis_context_pack as packs
    _, _, final, _ = delivered_story(tmp_path)
    verified = final.read_bytes()
    original = packs.select_analysis_story_source

    def replacement_after_selection(project):
        selected = original(project)
        write_json(final, {"角色集": [{"名称": "并发未交付新名"}]})
        return selected

    monkeypatch.setattr(packs, "select_analysis_story_source", replacement_after_selection)
    text = report_pack(tmp_path)
    assert "  - 交付新名\n" in text
    assert "并发未交付新名" not in text
    assert hashlib.sha256(verified).hexdigest() in text


def test_invalid_process_json_keeps_existing_empty_summary_fallback(tmp_path):
    """Source selection preserves the legacy non-destructive invalid-JSON path."""
    process = tmp_path / "故事结构_增量.json"
    process.write_text('{"incomplete": [', encoding="utf-8")
    assert "角色集: 0" in report_pack(tmp_path)
    assert process.read_text(encoding="utf-8") == '{"incomplete": ['
