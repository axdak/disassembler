"""交付步骤的唯一编排：供自动阶段续接和手动整条执行共同调用。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

# 持久化阶段名称保持兼容；状态机和整条执行都引用此序列，不能分别维护顺序。
# 顶层仅依赖标准库，允许 pipeline_state 导入本模块而不循环导入 run_pipeline。
DELIVERY_STAGES = ("final_pack", "commit_final_draft", "finalize")


def run_delivery_stage(
    project: Path, stage: str, *, end_chapter_seq=0, repair_history=False,
    command="", timeout=1200, retries=0, worker_window="hidden", repair_limit=3,
    commit=None, publish=None, worker=None, repair_worker=None,
    progress: Callable[[int, str], None] | None = None,
) -> int:
    """执行一个可恢复阶段，原码返回；内容前置检查在每次阶段入口重新执行。

    默认只执行本地操作。模型参数仅供显式历史维护；回调复用既有引擎函数，
    调用方负责持久化阶段状态、任务进度和写者锁，不再自行拼接交付步骤。
    """
    if stage not in DELIVERY_STAGES:
        raise ValueError(f"未知交付阶段：{stage}")
    import delivery_gate
    import run_pipeline
    import story_delivery

    rc = delivery_gate.check_delivery_prerequisites(project, end_chapter_seq=end_chapter_seq)
    if rc:
        return rc

    def report(percent: int, detail: str) -> None:
        if progress:
            progress(percent, detail)

    if stage == "final_pack":
        report(10, "准备正式故事交付")
        rc = story_delivery.prepare_delivery(project)
        if rc or not repair_history:
            return rc
        report(30, "显式维护：补齐历史简介与缺项")
        return story_delivery.finish_introduction(
            project, command, worker if worker is not None else run_pipeline.run_generation_worker,
            timeout=timeout, retries=retries, worker_window=worker_window,
        )
    if stage == "commit_final_draft":
        report(60, "验收并整理交付草稿")
        return story_delivery.commit_delivery(
            project, command if repair_history else "", repair_history=repair_history,
            timeout=timeout, retries=retries, worker_window=worker_window, repair_limit=repair_limit,
            commit=commit, repair_worker=repair_worker,
            observe=(lambda detail, data: report(65, detail)) if progress else None,
        )
    report(80, "检查交付内容与来源")
    rc = delivery_gate.report_delivery_quality(project)
    if rc:
        return rc
    report(95, "发布正式故事")
    # cmd_finalize 内置严格校验、同字节发布和版本保护，无需额外独立校验进程。
    return (publish if publish is not None else run_pipeline.cmd_finalize)(project)


def run_delivery(project: Path, **options) -> int:
    """按统一顺序运行交付，任一步失败立即停止；自动续接使用单阶段入口。"""
    for stage in DELIVERY_STAGES:
        rc = run_delivery_stage(project, stage, **options)
        if rc:
            return rc
    progress = options.get("progress")
    if progress:
        progress(100, "正式故事交付完成")
    return 0
