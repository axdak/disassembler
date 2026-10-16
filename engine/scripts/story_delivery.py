"""Deliver the accumulated story without an obligatory whole-book model rewrite.

A source-bound synopsis is preserved, including accepted draft-only repairs.
Missing descriptions or untrusted drafts retain read-only evidence generation.
Strict final validation and scoped repair remain the publication gate, including
missing titles; reusing a synopsis does not assert its semantic correctness.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from capacity_tree import CapacityTree, TreeFailure
from context_budget import CONTEXT_EXCEEDED
from fault_recovery import digest
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from project_lock import project_write_lock
from worker_checkpoint import atomic_write_bytes


def validate_introduction(intro):
    """Reject extra fields rather than silently discarding model changes."""
    if (not isinstance(intro, dict) or set(intro) != {"标题", "描述"}
            or any(not isinstance(v, str) or not v.strip() for v in intro.values())):
        raise ValueError("正式简介必须且只能提供非空标题和描述")


def ensure_delivery_governance(project: Path, command: str, *, end_chapter_seq=0, **options) -> int:
    """Require all selected chapters and every cycle, including the short tail."""
    from run_pipeline import (chapter_files, seq_from_file, is_chapter_complete,
                              run_periodic_governance)
    chapters = [ch for ch in chapter_files(project)
                if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    if not chapters or any(not is_chapter_complete(project, ch) for ch in chapters):
        print("正式交付前须完成所选范围的章节分析与 Delta。", flush=True)
        return 1
    last = seq_from_file(chapters[-1])
    for first in range(1, last + 1, 5):
        rc = run_periodic_governance(project, first, min(first + 4, last),
                                    audit_command=command,
                                    worker_timeout_seconds=options.get("timeout", 1200),
                                    worker_retries=options.get("retries", 0),
                                    worker_window=options.get("worker_window", "hidden"),
                                    worker_loop="continuous")
        if rc:
            return rc
    return 0


def prepare_delivery(project: Path) -> int:
    """Refresh a source-bound draft, retaining backups and pre-normalization proof."""
    from run_pipeline import ensure_story, cmd_commit_final_draft
    with project_write_lock(project):
        # ensure_story may coerce malformed descriptions into nonempty strings.
        # Capture only their original shape under the same lock, not another
        # synopsis copy; this is type/nonempty evidence, not semantic approval.
        source = project / "故事结构_增量.json"
        try:
            original = json.loads(source.read_bytes())
        except (OSError, ValueError):
            original = {}
        intro = original.get("介绍") if isinstance(original, dict) else None
        description = intro.get("描述") if isinstance(intro, dict) else None
        description_valid = isinstance(description, str) and bool(description.strip())
        source = ensure_story(project)
        draft = project / "故事结构_草稿.json"
        root = project / "质量治理/最终审计/交付"
        root.mkdir(parents=True, exist_ok=True)
        receipt = root / "source.json"
        state = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
        signature = digest(source)
        if state.get("source") != signature or not draft.is_file():
            if draft.is_file():
                atomic_write_bytes(root / f"previous_draft_{digest(draft)}.json", draft.read_bytes())
            atomic_write_bytes(draft, source.read_bytes())
            save_json(receipt, {"source": signature, "source_description_valid": description_valid})
        # Run deterministic normalization first. A failed validation is retained
        # for subsequent scoped repair; it never authorizes skipping final checks.
        cmd_commit_final_draft(project)
    return 0


def finish_introduction(project: Path, command: str, worker, **options) -> int:
    """Reuse a trusted synopsis; otherwise synthesize from complete evidence."""
    source = project / "故事结构_增量.json"
    draft = project / "故事结构_草稿.json"
    root = project / "质量治理/最终审计/交付"
    receipt = root / "introduction.json"
    try:
        # Capture the publication baseline under the same writer protocol used
        # below. Model work runs outside the lock and cannot authorize stale writes.
        with project_write_lock(project):
            state = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
            source_signature = digest(source)
            draft_bytes = draft.read_bytes()
            draft_signature = hashlib.sha256(draft_bytes).hexdigest()
            value = json.loads(draft_bytes)
            if state.get("source") == source_signature and state.get("introduction") == value.get("介绍"):
                return 0
            # The source receipt also covers accepted repairs made only to the
            # draft. Preserve that current value instead of restoring the older
            # process synopsis; missing titles remain strict final-repair work.
            intro = value.get("介绍")
            description = intro.get("描述") if isinstance(intro, dict) else None
            if isinstance(description, str) and description.strip():
                source_receipt = root / "source.json"
                prepared = (json.loads(source_receipt.read_text(encoding="utf-8"))
                            if source_receipt.is_file() else {})
                prepared_source = isinstance(prepared, dict) and prepared.get("source") == source_signature
                if prepared_source and "source_description_valid" in prepared:
                    # Preserve the pre-normalization result across resume; a
                    # normalized "42" must not become evidence on the next run.
                    source_description_valid = prepared["source_description_valid"] is True
                else:
                    source_value = json.loads(source.read_bytes())
                    source_intro = source_value.get("介绍") if isinstance(source_value, dict) else None
                    source_description = source_intro.get("描述") if isinstance(source_intro, dict) else None
                    source_description_valid = isinstance(source_description, str) and bool(source_description.strip())
                if source_description_valid and (draft_signature == source_signature or prepared_source):
                    # Publish the resume receipt under the writer lock, without
                    # rewriting any draft field or changing final schema checks.
                    save_json(receipt, {"source": source_signature, "introduction": intro})
                    return 0
        if not command:
            return 2
        tree = CapacityTree(project, command, worker,
                            "为故事 JSON 收尾正式简介。完整阅读已治理故事的证据，建立主角、主线、核心冲突与发展结果的定位索引。"
                            "仅概括当前已完成范围，不虚构未处理章节，不写阶段性进度、审计笔记。"
                            "以当前有效档案为准，纠错记录和字段归档中的旧值仅用于追溯，不能复活已纠正错误。"
                            "原简介仅供参考，须与当前档案重新核对；保持推测、传闻、条件和未完成行动的限定。"
                            "禁止修改任何角色、事件或其他故事档案。标题须有依据。",
                            "正式简介.json", [(source, "已完成范围的完整故事资料")],
                            **{**options, "adaptive_overviews": True, "protected_materials": [draft, source]})
        overview, _, refs = tree.run(overview_only=True)
        # A provider can exhaust its generation despite a small input. Change
        # the synthesis budget without rereading successful evidence pages.
        for budget in (2000, 1000, 500, 250):
            try:
                result, _ = tree.request(
                    f"完整证据根节点：{refs[-1]}\n关联索引：{overview}\n"
                    f"请生成正式简介，描述控制在约 {budget} 字以内（仍须覆盖核心主线）；"
                    'content 必须仅为 {"标题":"有依据的作品标题","描述":"正式故事简介"}。'
                    "原档案全部保留；此任务只是简介概括，不删减分析产物。",
                    f"正式简介收尾（描述预算 {budget} 字）", validate_content=validate_introduction)
                intro = result["content"]
                validate_introduction(intro)
                break
            except TreeFailure as exc:
                if exc.code != CONTEXT_EXCEEDED:
                    raise
        else:
            return CONTEXT_EXCEEDED
        with project_write_lock(project):
            # Re-read the current draft once for both its fence and merge. Reject
            # either changed input before preserving metadata and writing only intro.
            draft_bytes = draft.read_bytes()
            if (digest(source) != source_signature
                    or hashlib.sha256(draft_bytes).hexdigest() != draft_signature
                    or not tree.unchanged()):
                return REPAIR_EXHAUSTED
            value = json.loads(draft_bytes)
            old_intro = value.get("介绍", {})
            save_json(root / f"introduction_before_{draft_signature}.json", old_intro)
            value["介绍"] = {**old_intro, **intro}
            save_json(draft, value)
            save_json(receipt, {"source": source_signature, "introduction": value["介绍"]})
            return 0
    except TreeFailure as exc:
        return exc.code
    except (OSError, ValueError, TypeError) as exc:
        print(f"正式简介收尾未通过；原档案与断点保留：{exc}", flush=True)
        return REPAIR_EXHAUSTED


def commit_delivery(project: Path, command: str, *, timeout=1200, retries=0,
                    worker_window="hidden", repair_limit=3, observe=None,
                    commit=None, repair_worker=None) -> int:
    """逐批修复历史缺项；每批复用原修复/复核，完成后再严格验收。"""
    from local_json_patch import final_repair_scope, FINAL_REPAIR_BATCH_FIELDS
    from story_schema_rules import validate_exact_story_schema
    draft = project / "故事结构_草稿.json"
    repair = project / "质量治理" / "最终审计" / "repair_final.md"
    from run_pipeline import cmd_commit_final_draft, run_repair_worker
    commit = commit or cmd_commit_final_draft
    repair_worker = repair_worker or run_repair_worker
    rc = commit(project)
    if rc == 0:
        return 0
    if not repair.is_file():
        return 1
    if not command:
        return 2

    def all_scope():
        try:
            data = json.loads(draft.read_text(encoding="utf-8"))
            feedback = "\n".join(validate_exact_story_schema(data, mode="final")[0])
            return final_repair_scope(draft, feedback, all_fields=True)
        except (OSError, ValueError, TypeError):
            return {}

    fields = all_scope()
    # 字段数给出有限上界；重试不扩成全书重写，也不能用部分修好替代最终通过。
    batch_count = (len(fields) + FINAL_REPAIR_BATCH_FIELDS - 1) // FINAL_REPAIR_BATCH_FIELDS if fields else 1
    for _ in range(batch_count):
        before = draft.read_bytes() if draft.is_file() else b""
        if fields:
            binding = {"本批候选摘要": hashlib.sha256(before).hexdigest(),
                       "字段": list(fields)[:FINAL_REPAIR_BATCH_FIELDS]}
            # 与现有恢复指纹绑定；同一批失败预算不刷新，成功后的下一批有独立范围。
            marker = "\n\n<!-- final-field-batch -->\n"
            text = repair.read_text(encoding="utf-8").split(marker, 1)[0]
            repair.write_text(text + marker + json.dumps(binding, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        rc = repair_worker(
            command, project, "repair_final", repair, draft,
            timeout=timeout, retries=retries,
            worker_window=worker_window, repair_limit=repair_limit, observe=observe,
        )
        if rc != 0:
            return rc
        # 兼容不可读取的旧错误：修复入口自行报告失败，不能在这里编造补齐范围。
        if not fields:
            return commit(project) if draft.is_file() else 0
        if not draft.is_file() or draft.read_bytes() == before:
            return 1
        rc = commit(project)
        if rc == 0:
            return 0
        fields = all_scope()
        if not fields:
            return rc
    return rc
