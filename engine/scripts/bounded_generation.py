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
    """Read all history in bounded pages, then analyze the one original chapter."""
    from run_pipeline import chapter_by_seq, artifact_paths, render_prompt, chapter_time, seq_from_file
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
        if task_type == "delta" and story.is_file():
            recent = sorted((project / "章节处理/Delta").glob("第*章_*.json"))
            recent = [path for path in recent if seq_from_file(path) < seq][-10:]
            history_tree = CapacityTree(project, command, worker,
                                        "完整阅读历史档案，索引关键进展、正式名称与别名、未决线索和来源位置。禁止生成新事实。",
                                        "历史关联索引.md", [(story, "完整历史故事结构")]
                                        + [(path, "最近章节完整 Delta") for path in recent], **options)
            background, _, refs = history_tree.run(overview_only=True)
            background += f"\n完整历史入口：{refs[-1]}；原件目录：{history_tree.root / 'materials'}"
        protected = list(history_tree.root.rglob("*.json")) if history_tree else []
        tree = CapacityTree(project, command, worker, contract, output.name, sources,
                            protected_materials=protected, **options)
        # The history identity is part of task text, so a changed story invalidates
        # content checkpoints even when the chapter itself stayed unchanged.
        _, contents, _ = tree.run(background=background)
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
            _, contents, _ = tree.run()
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
