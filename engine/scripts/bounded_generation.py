"""Capacity fallback adapters; the normal chapter/report contracts stay in charge."""
from __future__ import annotations

import json
import re
from pathlib import Path

from capacity_tree import CapacityTree, TreeFailure, merge_json, merge_markdown
from fault_recovery import digest
from merge_delta import save_json
from coerce_delta import relocate_element_fields
from repair_budget import REPAIR_EXHAUSTED
from story_schema_rules import COLLECTION_KEYS
from worker_checkpoint import atomic_write_bytes


def assemble(contents, json_output=False, resolve=None):
    """Use deterministic composition so reduction cannot silently omit children."""
    if not contents:
        raise ValueError("所有分片均无产物，不能视为任务完成")
    if json_output:
        value = None
        for content in contents:
            value = merge_json(value, content, resolve=resolve)
        return json.dumps(value, ensure_ascii=False, indent=2)
    return merge_markdown(contents)


def review_complete_candidate(tree, text, sources, background, **options):
    """Re-read every original and the complete candidate before allowing publication.

    A separate task/ledger owns the review. Every page and cross-page node must
    acknowledge its exact input and report no unresolved issue; overviews alone
    cannot satisfy source coverage. Returned additions retain the original format.
    """
    candidate = tree.root / ("pre_review.json" if tree.target.endswith(".json") else "pre_review.md")
    atomic_write_bytes(candidate, text.encode())
    sources = list(dict.fromkeys(sources)) + [(candidate, "完整待复核候选；必须与原始证据区分")]
    review = CapacityTree(tree.root, tree.command, tree.worker,
                          tree.contract + "\n独立完整复核本次必读材料与候选。查证档案按关联读取，不逐章重读全部历史。尤其核对跨片名称、时序、伏笔、引用与遗漏。"
                          "区分历史背景、分析判断与本次新增事实。原文优先，不能用派生产物证明自己正确。",
                          tree.target, sources, **{**options, "reference_sources": tree.reference_sources})
    _, additions, _ = review.run(background=background, review=True)
    if not tree.unchanged() or not review.unchanged():
        raise TreeFailure(REPAIR_EXHAUSTED)
    return additions


def combine_delta(contents, story, seq, resolve=None):
    """Reconcile exact formal identities across pieces of the same real chapter.

    Fragment boundaries never become chapter IDs. New/modified classification is
    derived from the original story, preventing later fragments from modifying a
    newly proposed item that has not yet been committed.
    """
    result = {"章节": str(seq).zfill(4), "新增元素": {key: [] for key in COLLECTION_KEYS},
              "修改元素": {key: [] for key in COLLECTION_KEYS}, "冲突声明": [], "质量说明": ""}
    proposed_names = {key: set() for key in COLLECTION_KEYS}
    for content in contents:
        bucket = content.get("新增元素", {}) if isinstance(content, dict) else {}
        if isinstance(bucket, dict):
            for collection in COLLECTION_KEYS:
                items = bucket.get(collection, [])
                if isinstance(items, list):
                    proposed_names[collection].update(item["名称"] for item in items if isinstance(item, dict)
                                                      and isinstance(item.get("名称"), str))
    for content in contents:
        if not isinstance(content, dict):
            raise ValueError("Delta 分片必须是原格式 JSON 对象")
        relocate_element_fields(content)
        if content.get("章节") != result["章节"]:
            raise ValueError("分片不得改变原章节编号")
        if set(content) - set(result):
            raise ValueError("Delta 分片包含额外顶层字段，不能丢弃后冒充成功")
        for section in ("新增元素", "修改元素"):
            bucket = content.get(section)
            if not isinstance(bucket, dict) or set(bucket) - set(COLLECTION_KEYS):
                raise ValueError("Delta 分片集合不合格")
            for collection, items in bucket.items():
                if not isinstance(items, list):
                    raise ValueError("Delta 分片集合必须为数组")
                known = {item.get("名称") for item in story.get(collection, []) if isinstance(item, dict)
                         and isinstance(item.get("名称"), str)}
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("名称"), str):
                        raise ValueError("Delta 分片缺少元素正式名称")
                    # An unknown modified name may be a typo, not a new entity.
                    # Only an explicit new proposal in this chapter can justify
                    # folding its later modifications into the new-item bucket.
                    destination = ("修改元素" if item["名称"] in known else
                                   "新增元素" if item["名称"] in proposed_names[collection] else section)
                    result[destination][collection] = merge_json(result[destination][collection], [item], resolve=resolve)
        for key in ("冲突声明", "质量说明"):
            result[key] = merge_json(result[key], content.get(key), "/" + key)
    return result


def run_bounded_chapter(command, project, task_type, output, seq, worker, **options):
    """Page only this chapter and its selected history tier; archives remain references."""
    from run_pipeline import (chapter_by_seq, artifact_paths, render_prompt, chapter_time,
                              build_delta_story_context, DELTA_FULL_STORY_KB)
    from context_budget import ContextBudget, estimate_tokens
    from validate_chapter_analysis import validate_chapter_analysis_text
    chapter = chapter_by_seq(project, seq)
    if chapter is None:
        return 1
    paths = artifact_paths(project, chapter)
    story = project / "故事结构_增量.json"
    before = digest(output)
    if task_type == "analysis":
        contract = render_prompt("流水线/章节分析.j2", chapter_seq=seq,
                                 analysis_path="由程序合成章节分析.md", chapter_text="由分片材料提供")
        sources = [(chapter, "本章原文")]
    else:
        contract = render_prompt("流水线/单章增量提取.j2", chapter_seq=seq, chapter_id=str(seq).zfill(4),
                                 delta_path="由程序合成章节Delta.json", summary="完整历史以隔离原件和关联索引提供",
                                 analysis_text="由分片材料提供", chapter_text="由分片材料提供",
                                 chapter_time=chapter_time(seq))
        sources = [(chapter, "本章原文"), (paths["analysis"], "本章完整分析")]
    try:
        background = ""
        history_tree = None
        history_sources, references = [], []
        if task_type == "delta" and story.is_file():
            metadata_path = paths["delta_task"].with_suffix(".context.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            history = paths["delta_task"].with_suffix(".history.md")
            # Rebuild the selected representation locally, without a model call:
            # changed chapter/analysis/recent Deltas must invalidate old evidence.
            current = contract + chapter.read_text(encoding="utf-8") + paths["analysis"].read_text(encoding="utf-8")
            background = build_delta_story_context(story, project, seq,
                metadata.get("full_story_kb", DELTA_FULL_STORY_KB), current_material=current)
            atomic_write_bytes(history, background.encode())
            history_sources = [(history, "本章选定的历史上下文；不是本章新增事实")]
            references = [(story, "完整历史故事档案；按正式名称核查"), *sources]
            # Most selected tiers are inlined without another model call. Only
            # an oversized index/recent-Delta package itself needs paging.
            if estimate_tokens(background) > ContextBudget.from_env().usable_input // 3:
                history_tree = CapacityTree(project, command, worker,
                    "读取本章选定的历史上下文，索引身份与关联证据。禁止生成本章新增事实。",
                    "历史关联索引.md", history_sources, reference_sources=references, **options)
                background, _, refs = history_tree.run(overview_only=True)
                background += f"\n选定历史完整入口：{refs[-1]}；原件目录：{history_tree.root / 'materials'}"
        protected = list(history_tree.root.rglob("*.json")) if history_tree else []
        tree = CapacityTree(project, command, worker, contract, output.name, sources,
                            protected_materials=protected, reference_sources=references, **options)
        if str(story.resolve()) in tree.reference_paths:
            background = background.replace(str(story), str(tree.reference_paths[str(story.resolve())]))
        # The history identity is part of task text, so a changed story invalidates
        # content checkpoints even when the chapter itself stayed unchanged.
        overview, contents, _ = tree.run(background=background)
        preliminary = (json.dumps(combine_delta(contents, json.loads(story.read_text(encoding="utf-8")), seq, tree.resolve), ensure_ascii=False)
                       if task_type == "delta" else assemble(contents))
        review_sources = sources + history_sources
        contents += review_complete_candidate(tree, preliminary, review_sources, overview, **options)
        if task_type == "delta":
            value = combine_delta(contents, json.loads(story.read_text(encoding="utf-8")), seq, tree.resolve)
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            # All internal slices belong to the same chapter. Use its file title
            # to prevent ten-section templates being duplicated under invented
            # per-fragment H1 titles during deterministic assembly.
            title = "# " + chapter.stem.replace("_", " ")
            contents = [re.sub(r"^# [^\n]*", title, value, count=1, flags=re.MULTILINE) for value in contents]
            text = assemble(contents)
            errors = validate_chapter_analysis_text(text)
            if errors:
                save_json(tree.root / "assembly_errors.json", errors)
                return REPAIR_EXHAUSTED
        candidate = tree.root / ("candidate.json" if task_type == "delta" else "candidate.md")
        atomic_write_bytes(candidate, text.encode())
        if not tree.unchanged() or history_tree is not None and not history_tree.unchanged() or digest(output) != before:
            return REPAIR_EXHAUSTED
        # This writes only the chapter artifact. commit_chapter still performs its
        # unchanged process-mode validation before touching the formal story.
        atomic_write_bytes(output, candidate.read_bytes())
        return 0
    except TreeFailure as exc:
        return exc.code
    except (ValueError, TypeError, OSError) as exc:
        print(f"分层单章合成失败，完整分片与原产物已保留：{exc}", flush=True)
        return REPAIR_EXHAUSTED


def run_hierarchical_reports(command, project, pack, outputs, worker, **options):
    """Reduce per target through a bounded tree, then assemble complete content."""
    metadata = json.loads(pack.with_suffix(".hierarchy.json").read_text(encoding="utf-8"))
    sources = [(Path(path), "完整分批分析结果") for path in metadata["sources"]]
    signatures = {output: digest(output) for output in outputs}
    staged = []
    try:
        for output in outputs:
            contract = metadata["contract"] + f"\n只组织目标文件：{output.name}。保持该文件原有结构。"
            tree = CapacityTree(project, command, worker, contract, output.name, sources, **options)
            overview, contents, _ = tree.run()
            preliminary = assemble(contents, output.suffix == ".json", tree.resolve)
            evidence = [(Path(path), "完整原始分析材料") for path in metadata.get("evidence", [])]
            contents += review_complete_candidate(tree, preliminary, sources + evidence, overview, **options)
            text = assemble(contents, output.suffix == ".json", tree.resolve)
            candidate = tree.root / ("candidate" + output.suffix)
            atomic_write_bytes(candidate, text.encode())
            staged.append((output, candidate, tree))
        # Validate the entire set before publishing any output; every stage is
        # recoverable from immutable leaf receipts if publication is interrupted.
        from worker_artifacts import invalid_output_artifacts
        if invalid_output_artifacts([candidate for _, candidate, _ in staged]):
            return REPAIR_EXHAUSTED
        if any(not tree.unchanged() or digest(output) != signatures[output] for output, _, tree in staged):
            return REPAIR_EXHAUSTED
        for output, candidate, _ in staged:
            atomic_write_bytes(output, candidate.read_bytes())
        return 0
    except TreeFailure as exc:
        return exc.code
    except (ValueError, TypeError, OSError) as exc:
        print(f"分层汇总失败，完整分片与原报告已保留：{exc}", flush=True)
        return REPAIR_EXHAUSTED


def run_bounded_governance(command, project, pack, output, worker, **options):
    """Split within one real chapter/range; original strict commit remains authoritative."""
    from governance_contract import normalize_governance_patch
    metadata = json.loads(pack.with_suffix(".governance.json").read_text(encoding="utf-8"))
    sources = [(Path(path), role) for path, role in metadata["sources"]]
    before = digest(output)
    try:
        references = [(Path(path), role) for path, role in metadata.get("references", [])]
        tree = CapacityTree(project, command, worker, metadata["contract"], output.name, sources,
                            reference_sources=references, **options)

        def combine(parts):
            result = None
            no_change_seen = False
            for part in parts:
                patch, _ = normalize_governance_patch(part, metadata["range"])
                if not isinstance(patch, dict) or patch.get("章节范围") != metadata["range"]:
                    raise ValueError("治理分片不得改变实际章节范围")
                # no_change describes one slice, not the combined workload.
                # Keep its evidence/rationale but do not concatenate this flag
                # into another slice's actual governance type.
                if patch.get("治理类型") == "no_change":
                    no_change_seen = True
                    patch.pop("治理类型")
                result = merge_json(result, patch, resolve=tree.resolve)
            if not isinstance(result, dict):
                raise ValueError("治理缺少真实补丁或有依据的 no_change")
            if any(not isinstance(result.get(key, {}), dict)
                   for key in ("新增元素", "修改元素", "治理操作")):
                raise ValueError("治理分片的元素与操作必须为对象，不能丢弃异常内容")
            changed = any(any(items for items in result.get(key, {}).values())
                          for key in ("新增元素", "修改元素", "治理操作")) or bool(result.get("介绍"))
            if not changed and no_change_seen and "治理类型" not in result:
                result["治理类型"] = "no_change"
            return json.dumps(result, ensure_ascii=False, indent=2)

        overview, contents, _ = tree.run()
        contents += review_complete_candidate(tree, combine(contents), sources, overview, **options)
        candidate = tree.root / "candidate.json"
        atomic_write_bytes(candidate, combine(contents).encode())
        if not tree.unchanged() or digest(output) != before:
            return REPAIR_EXHAUSTED
        # Only the patch is published. commit_governance still validates, merges,
        # preserves archives and rolls back on any original governance failure.
        atomic_write_bytes(output, candidate.read_bytes())
        return 0
    except TreeFailure as exc:
        return exc.code
    except (ValueError, TypeError, OSError) as exc:
        print(f"分片治理未通过，保留候选与检查点：{exc}", flush=True)
        return REPAIR_EXHAUSTED


def run_paged_recovery(worker, command, project, kind, pack, output, chapter_seq=0, **options):
    """Page oversized repair evidence while retaining the original repair scope.

    The recovery scheduler still owns program repair, scope validation, independent
    review, strategy switching and publication. This adapter only organizes input.
    """
    from context_budget import inspect_pack
    report = inspect_pack(pack)
    if report["passed"]:
        return worker(command, project, kind, pack, output, chapter_seq, **options)
    sources = [(Path(item["path"]), "完整修复材料") for item in report["sources"]]
    try:
        tree = CapacityTree(project, command, worker,
                            "逐片阅读修复材料，索引错误路径、原文证据、候选变化和约束。禁止把待修产物当作原文事实。",
                            "修复材料索引.md", sources, **options)
        overview, _, refs = tree.run(overview_only=True)
        routed = tree.root / "dispatch" / "task.md"
        routed.parent.mkdir(exist_ok=True)
        routed.write_text(pack.read_text(encoding="utf-8") + "\n## 完整材料分层入口\n"
                          + overview + f"\n根节点：{refs[-1]}\n隔离原件目录：{tree.root / 'materials'}\n"
                          "材料已经逐片建立索引。按本次字段和证据位置读取必要原件，禁止把全目录一次性重新拼回上下文。"
                          "索引不是证据，修复值和复核结论必须核对原件。只写原来指定的产物，不改修复范围。",
                          encoding="utf-8")
        from bounded_final import guarded_worker
        protected = [path for path in tree.root.rglob("*") if path.is_file() and path != output]
        rc, changed = guarded_worker(worker, command, project, kind, routed, output, protected,
                                     {**options, "chapter_seq": chapter_seq})
        return rc if not changed and tree.unchanged() else REPAIR_EXHAUSTED
    except TreeFailure as exc:
        return exc.code
