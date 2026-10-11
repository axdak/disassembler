"""Deliver the accumulated story without an obligatory whole-book model rewrite.

The only unconditional model task is the formal introduction. It has read-only
evidence and can replace two introduction fields only. Original strict final
validation and the program-first repair scheduler remain the publication gate.
"""
from __future__ import annotations

import json
from pathlib import Path

from capacity_tree import CapacityTree, TreeFailure
from context_budget import CONTEXT_EXCEEDED
from fault_recovery import digest
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
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
    """Refresh a draft only when the accumulated source changes, retaining backups."""
    from run_pipeline import ensure_story, cmd_commit_final_draft
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
        save_json(receipt, {"source": signature})
    # Run deterministic normalization first. A failed validation is retained for
    # the subsequent repair stage; it does not authorize skipping final checks.
    cmd_commit_final_draft(project)
    return 0


def finish_introduction(project: Path, command: str, worker, **options) -> int:
    """Read complete evidence hierarchically and publish title/description only."""
    source = project / "故事结构_增量.json"
    draft = project / "故事结构_草稿.json"
    root = project / "质量治理/最终审计/交付"
    receipt = root / "introduction.json"
    state = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
    value = json.loads(draft.read_text(encoding="utf-8"))
    if state.get("source") == digest(source) and state.get("introduction") == value.get("介绍"):
        return 0
    if not command:
        return 2
    try:
        tree = CapacityTree(project, command, worker,
                            "为故事 JSON 收尾正式简介。完整阅读已治理故事的证据，建立主角、主线、核心冲突与发展结果的定位索引。"
                            "仅概括当前已完成范围，不虚构未处理章节，不写阶段性进度、审计笔记。"
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
        if not tree.unchanged():
            return REPAIR_EXHAUSTED
        # Preserve every other field, including extra introduction metadata.
        old_intro = value.get("介绍", {})
        save_json(root / f"introduction_before_{digest(draft)}.json", old_intro)
        value["介绍"] = {**old_intro, **intro}
        save_json(draft, value)
        save_json(receipt, {"source": digest(source), "introduction": value["介绍"]})
        return 0
    except TreeFailure as exc:
        return exc.code
    except (OSError, ValueError, TypeError) as exc:
        print(f"正式简介收尾未通过；原档案与断点保留：{exc}", flush=True)
        return REPAIR_EXHAUSTED
