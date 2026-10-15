#!/usr/bin/env python3
"""全流程编排器。

把现有引擎命令串成一键全流程（章节分析/Delta及周期治理 -> 简介与严格交付
-> 章节结构/视觉资产/全书报告 -> 导出），报告不再阻塞正式故事 JSON 产出。

设计要点：
- 直接 import 调用 run_pipeline 的 cmd_*（显式参数），不经过 CLI 翻译层。
- worker 阶段使用 worker_loop="continuous"，由 cmd_run / cmd_*_auto 内部管理 worker
  子进程、重试与验收；编排器只处理阶段级返回码。
- 阶段事件与交错执行的章节进度经 EventBus 发射；界面按事件合并刷新，
  章节列表由用户手动核对，不依赖高频全书扫描。
- 崩溃恢复：加载状态文件，从 current_stage 续跑；各 cmd 自身按实际产物幂等续跑。
- SQLite 百分比不作完成依据，以实际产物为准。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from events import Event, EventBus
from context_budget import OUTPUT_TRUNCATED, capacity_scope
from worker_checkpoint import run_checkpointed_partial
from repair_budget import REPAIR_EXHAUSTED, begin_retry_session
from pipeline_state import STAGES, PipelineState
from run_pipeline import (
    builtin_agent_worker_command,
    cmd_chapter_structure_auto,
    cmd_commit_final_draft,
    cmd_finalize,
    cmd_run,
    cmd_run_interleaved,
    cmd_visual_assets_auto,
    chapter_files,
    is_chapter_complete,
    latest_analysis_manifest,
    run_cmd,
    run_multi_output_worker,
    run_generation_worker as run_worker,
    run_repair_worker,
    script_path,
    seq_from_file,
    set_cancel_event,
    set_recovery_observer,
    write_fullbook_partial_worker_pack,
    write_fullbook_reduce_worker_pack,
)
from worker_artifacts import confined_project_path, invalid_output_artifacts
from worker_admission import worker_concurrency_scope, worker_admission

# 全书分析任务依赖顺序（阶段5）
FULLBOOK_TASKS = (
    "summary", "characters", "plot", "style", "worldview", "settings",
    "plotlines", "outline", "detailed_outline", "narrative_structure", "report",
)


@dataclass
class Config:
    worker_provider: str = "agy"
    worker_command: str = ""  # 非空时作为显式 worker 命令（provider 视为 manual），用于假 worker 测试与里程碑4
    worker_loop: str = "continuous"
    worker_retries: int = 2
    worker_timeout_seconds: int = 1200
    # Immutable per-run Token settings; subprocesses inherit this task's snapshot.
    model_input_tokens: int = 300000
    model_output_tokens: int = 64000  # 单次输出上限；必须容纳单章 Delta/分析（含思维链）
    model_context_tokens: int = 300000
    model_reserve_tokens: int = 30000
    model_overview_tokens: int = 4000  # 证据定位概览，不限制章节正文长度
    worker_window: str = "hidden"
    max_attempts: int = 10  # 安全上限；真正硬阻塞由 stagnant_limit 触发
    stagnant_limit: int = 3
    max_repair_rounds: int = 3
    analysis_concurrency: int = 1  # 分析阶段并行度（delta 仍串行）
    per_chapter_concurrency: int = 2  # 分章任务（章节结构/视觉资产）并行度；1=逐章串行（兼容旧行为）
    chapter_mode: str = "two_phase"  # two_phase | interleaved
    end_chapter_seq: int = 0  # 0=全书；正数表示运行至该章节（含）
    fullbook_tasks: tuple = FULLBOOK_TASKS
    export_source: str = "final"
    # 任务包上下文压缩策略（可经设置页配置）
    include_original: str = "auto"  # auto | none | sample | full
    max_original_chars: int = 6000
    max_analysis_chars: int = 12000
    story_summary_max_items: int = 0  # 0=不限制（全名称索引，默认，避免大书全书分析任务包遗漏元素）
    context_budget_chars: int = 160000
    delta_full_story_kb: int = 256
    audit_max_context_chars: int = 524288
    max_pack_chars: int = 70000  # 全书分析任务包单包最大字符数，超出自动分包；只影响阶段5全书分析任务包


class Orchestrator:
    def __init__(
        self,
        project_dir,
        config: Optional[Config] = None,
        bus: Optional[EventBus] = None,
    ):
        self.project_dir = Path(project_dir)
        self.config = config or Config()
        self.bus = bus or EventBus()
        self.state = PipelineState.load(self.project_dir)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """请求取消：在当前阶段结束后停止（不中断进行中的 worker 子进程）。"""
        self._cancel.set()

    def emit(self, type: str, stage: str = "", detail: str = "", data: Optional[dict] = None) -> None:
        self.bus.emit(Event(type, stage=stage, detail=detail, data=data or {}))

    def run(self) -> int:
        """Freeze capacity and convert unexpected failures into the blocked contract."""
        try:
            concurrency = max(self.config.analysis_concurrency, self.config.per_chapter_concurrency)
            if self.config.chapter_mode == "interleaved":
                concurrency = max(concurrency, self.config.analysis_concurrency + 1)
            with capacity_scope(asdict(self.config)), worker_concurrency_scope(concurrency):
                # Task allowance is distinct from the ceiling shared by this process.
                policy = worker_admission.configuration()
                ceiling = policy["deployment_limit"]
                self.emit("log", detail=f"模型并发：本任务各阶段最大配置 {concurrency}，"
                          f"任务生效上限 {worker_admission.effective_limit()}，"
                          f"本进程部署总上限 {ceiling if ceiling is not None else '未设置'}")
                if policy["warning"]:
                    self.emit("log", detail=policy["warning"])
                return self._run()
        except Exception as exc:
            # Only ordinary failures are converted: cancellation/system-exit
            # BaseExceptions still propagate. Never retry an unexplained crash.
            stage = self.state.current_stage
            diagnostic = traceback.format_exc()
            self.state.last_error = (
                f"{stage}: {type(exc).__name__}: {exc}。"
                "请根据诊断堆栈检查输入文件、配置或目录权限，修复后从当前阶段续跑。"
            )
            data = {"traceback": diagnostic, "exception_type": type(exc).__name__}
            try:
                self.state.save()
            except Exception as persistence_error:
                # Keep the original traceback even when the checkpoint itself
                # cannot be written; the event/log remains actionable.
                data["persistence_error"] = f"{type(persistence_error).__name__}: {persistence_error}"
                self.state.last_error += f" 状态保存失败：{data['persistence_error']}"
            print(diagnostic, file=sys.stderr)
            self.emit("stage_fail", stage=stage, detail=self.state.last_error, data=data)
            self.emit("pipeline_blocked", stage=stage, detail=self.state.last_error, data=data)
            return 1

    def _run(self) -> int:
        """运行全流程。返回 0=全部完成，1=硬阻塞/致命，2=已取消。"""
        try:
            # Preparation also belongs to this lifetime so partially installed
            # cancellation/recovery hooks are cleaned up after setup errors.
            begin_retry_session(self.project_dir)
            set_cancel_event(self._cancel)
            set_recovery_observer(lambda detail, data: self.emit(
                "worker_progress" if data.get("worker") else "recovery_progress",
                stage=self.state.current_stage, detail=detail, data=data))
            self._reconcile_chapter_scope()
            self._reconcile_recovery_state()
            self.emit("pipeline_start", stage=self.state.current_stage)
            while not self.state.is_done():
                if self._cancel.is_set():
                    self.emit("pipeline_cancelled")
                    return 2
                stage = self.state.current_stage
                rc = self._run_stage_with_retries(stage)
                if rc != 0:
                    if rc == 2:
                        self.emit("pipeline_cancelled")
                        return 2
                    self.emit("pipeline_blocked", stage=stage, detail=self.state.last_error)
                    return 1
            self.emit("pipeline_done", detail="全流程完成")
            return 0
        finally:
            set_cancel_event(threading.Event())  # 重置，避免跨运行/跨测试泄漏
            set_recovery_observer(None)

    @staticmethod
    def _valid_json_file(path: Path) -> bool:
        try:
            return path.is_file() and bool(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False

    def _rewind_to(self, stage: str, reason: str) -> None:
        """Rewind persisted completion when authoritative artifacts disappeared."""
        index = STAGES.index(stage)
        self.state.completed = [
            item for item in self.state.completed
            if item in STAGES and STAGES.index(item) < index
        ]
        self.state.current_stage = stage
        self.state.last_error = f"自动恢复回退到 {stage}: {reason}"
        self.state.save()
        self.emit("recovery_rewind", stage=stage, detail=reason)

    def _reconcile_chapter_scope(self) -> None:
        """运行范围变化后重走章节与下游阶段，已有单章产物由各阶段幂等跳过。"""
        if self.state.end_chapter_seq == self.config.end_chapter_seq:
            return
        if self.config.end_chapter_seq:
            # 过程故事结构已合入的后续章节不能仅靠改范围撤回；避免生成误称为
            # “截止至 N 章”但仍包含 N 章以后内容的全书分析。
            beyond = [seq_from_file(ch) for ch in chapter_files(self.project_dir)
                      if seq_from_file(ch) > self.config.end_chapter_seq
                      and is_chapter_complete(self.project_dir, ch)]
            if beyond:
                raise ValueError(
                    f"第{min(beyond):03d}章已提交，无法直接把截止章改为"
                    f"第{self.config.end_chapter_seq:03d}章；请在新项目中运行该范围"
                )
        self.state.end_chapter_seq = self.config.end_chapter_seq
        self.state.fullbook_completed_tasks.clear()
        self._rewind_to("chapter_analysis", "截止章节已变化")

    def _reconcile_recovery_state(self) -> None:
        """Verify terminal artifacts before trusting a late/done state checkpoint."""
        if self.state.flow_version < 2:
            # Upgrade pending legacy report/final stages without replaying chapters
            # or losing completed report dimensions. Existing final files stay valid.
            if self.state.current_stage not in {"chapter_analysis", "chapter_delta"} and "finalize" not in self.state.completed:
                self.state.current_stage = "final_pack"
                self.state.completed = [s for s in self.state.completed
                                        if s not in {"final_pack", "commit_final_draft", "finalize"}]
            self.state.flow_version = 2
            self.state.save()
        current_index = len(STAGES) if self.state.is_done() else (
            STAGES.index(self.state.current_stage)
            if self.state.current_stage in STAGES else 0
        )
        final = self.project_dir / "故事结构.json"
        draft = self.project_dir / "故事结构_草稿.json"
        if current_index > STAGES.index("finalize"):
            if not self._valid_json_file(final):
                target = "finalize" if self._valid_json_file(draft) else "final_pack"
                self._rewind_to(target, "最终故事结构缺失或 JSON 无效")
                return
            export_report = self.project_dir / "导出" / "obsidian-vault" / "_meta" / "export_report.json"
            single = self.project_dir / "导出" / "故事结构总览.md"
            if self.state.is_done() and (
                not self._valid_json_file(export_report)
                or not single.is_file()
                or single.stat().st_size == 0
            ):
                self._rewind_to("export", "导出产物缺失或无效")
                return
        elif current_index > STAGES.index("final_pack") and not self._valid_json_file(draft):
            self._rewind_to("final_pack", "最终草稿缺失或 JSON 无效")

    def _run_stage_with_retries(self, stage: str) -> int:
        self.emit("stage_start", stage=stage)
        self.state.reset_stage(stage)
        # 建立停滞检测基线签名（阶段开始时的产物状态）
        self.state.record_signature(stage, self._signature(stage))
        self.state.save()
        attempt = 0
        while True:
            attempt += 1
            self.state.bump_attempt(stage)
            self.state.save()
            try:
                rc = self._dispatch(stage)
            except NotImplementedError:
                self.state.last_error = f"{stage} not implemented"
                self.state.save()
                self.emit("stage_fail", stage=stage, detail="stage not implemented")
                return 1
            if rc == 0:
                self.state.mark_completed(stage)
                self.state.save()
                self.emit("stage_done", stage=stage, data={"attempts": attempt})
                return 0
            if self._cancel.is_set():
                self.emit("pipeline_cancelled")
                return 2
            if rc == 64:
                # An unchanged context budget cannot be fixed by redispatching it.
                self.state.last_error = (
                    "输入材料超过可用输入预算；完整材料与检查点已保留，"
                    "请查看任务包旁的 capacity.json，提高模型输入容量或继续细分该请求。"
                )
                self.state.save()
                self.emit("hard_block", stage=stage, detail=self.state.last_error)
                return 1
            if rc == OUTPUT_TRUNCATED:
                # 单轮输出被输出上限截断：属于输出预算不足，与输入材料规模无关。
                # 重跑同一请求不会改变输出上限，因此这里不做无效重试；
                # 保留断点，并用与容量阻塞区分开的事件给出可执行的处置指引。
                self.state.last_error = (
                    "单轮模型输出达到“单次输出上限”被截断（输入材料并未超容量）；"
                    "断点与产物已保留。请在设置中提高“单次输出上限”后重跑本章，"
                    "或将该任务细分。"
                )
                self.state.save()
                self.emit("output_truncated", stage=stage, detail=self.state.last_error)
                return 1
            if rc == REPAIR_EXHAUSTED:
                # A failed logical task must not regain its model budget when
                # the outer stage is redispatched or a legacy retry session resets.
                self.state.last_error = "自动故障处理的可用策略已耗尽；断点和诊断记录已保留于质量治理/自动恢复。输入或修复配置变化后可重新评估。"
                self.state.save()
                self.emit("hard_block", stage=stage, detail=self.state.last_error)
                return 1
            if rc == 2:
                # rc=2 是交接/检查点（cmd_run supervised checkpoint、worker 未配置等），
                # 不是失败：产物由 worker/主控在下一次派发前补齐后重跑同一路由。
                # autonomous_loop.md：rc=2 主控必须重跑同一路由，不当作结束/失败。
                # 不计停滞、不发 stage_fail，只受 max_attempts 上限保护，避免死循环。
                self.emit("stage_handoff", stage=stage,
                          detail=f"attempt {attempt} rc=2 (handoff), rerun same stage")
                self.state.last_error = None
                self.state.save()
                if attempt >= self.config.max_attempts:
                    self.emit("hard_block", stage=stage,
                              detail=f"exhausted {attempt} attempts on handoff")
                    return 1
                continue
            sig = self._signature(stage)
            stagnant = self.state.record_signature(stage, sig)
            self.state.last_error = f"{stage} attempt {attempt} rc={rc}"
            self.state.save()
            self.emit(
                "stage_fail", stage=stage,
                detail=f"attempt {attempt} rc={rc}", data={"stagnant": stagnant},
            )
            if stagnant >= self.config.stagnant_limit:
                self.emit("hard_block", stage=stage, detail=f"stagnant {stagnant} 次无变化")
                return 1
            if attempt >= self.config.max_attempts:
                self.emit("hard_block", stage=stage, detail=f"exhausted {attempt} attempts")
                return 1

    def _dispatch(self, stage: str) -> int:
        handler = {
            "chapter_analysis": self._stage_chapter_analysis,
            "chapter_delta": self._stage_chapter_delta,
            "chapter_structure": self._stage_chapter_structure,
            "visual_assets": self._stage_visual_assets,
            "fullbook_analysis": self._stage_fullbook_analysis,
            "final_pack": self._stage_final_pack,
            "commit_final_draft": self._stage_commit_final_draft,
            "finalize": self._stage_finalize,
            "export": self._stage_export,
        }.get(stage)
        if handler is None:
            raise NotImplementedError(stage)
        return handler()

    # ---- worker 调用公共参数 ----

    def _run_worker_kwargs(self) -> dict:
        base = dict(
            run_mode="worker",
            worker_loop=self.config.worker_loop,
            worker_retries=self.config.worker_retries,
            worker_timeout_seconds=self.config.worker_timeout_seconds,
            worker_window=self.config.worker_window,
            cancel_event=self._cancel,
            include_original=self.config.include_original,
            max_original_chars=self.config.max_original_chars,
            max_analysis_chars=self.config.max_analysis_chars,
            story_summary_max_items=self.config.story_summary_max_items,
            context_budget_chars=self.config.context_budget_chars,
            delta_full_story_kb=self.config.delta_full_story_kb,
            audit_max_context_chars=self.config.audit_max_context_chars,
        )
        if self.config.worker_command:
            return {**base, "worker_provider": "manual",
                    "chapter_command": self.config.worker_command,
                    "audit_command": self.config.worker_command}
        return {**base, "worker_provider": self.config.worker_provider}

    def _auto_namespace(self) -> argparse.Namespace:
        """构造 chapter-structure-auto / visual-assets-auto 所需的 argparse.Namespace。

        worker_command 非空时作为显式 worker 命令；否则由 resolve_analysis_worker_command
        按 worker_provider 解析为内置 agent_worker.py 命令（与 cmd_run 的 resolve_worker_commands 一致）。
        """
        return argparse.Namespace(
            project_dir=str(self.project_dir),
            max_pack_chars=self.config.max_pack_chars,
            force_aggregate=False,  # 汇总阶段根据任务包范围与产物新鲜度自行判断复用
            end_chapter_seq=self.config.end_chapter_seq,
            run_mode="worker",
            worker_provider="manual" if self.config.worker_command else self.config.worker_provider,
            worker_command=self.config.worker_command,
            worker_timeout_seconds=self.config.worker_timeout_seconds,
            worker_retries=self.config.worker_retries,
            worker_window=self.config.worker_window,
            worker_loop=self.config.worker_loop,
            per_chapter_concurrency=self.config.per_chapter_concurrency,
            cancel_event=self._cancel,
            include_original=self.config.include_original,
            max_original_chars=self.config.max_original_chars,
            max_analysis_chars=self.config.max_analysis_chars,
            story_summary_max_items=self.config.story_summary_max_items,
            context_budget_chars=self.config.context_budget_chars,
            delta_full_story_kb=self.config.delta_full_story_kb,
            audit_max_context_chars=self.config.audit_max_context_chars,
        )

    def _worker_command(self) -> str:
        """解析内置 agent_worker.py 命令（按 worker_provider）；worker_command 非空时直接用。"""
        if self.config.worker_command:
            return self.config.worker_command
        return builtin_agent_worker_command(self.config.worker_provider)

    def _chapter_spec(self) -> str:
        """分析任务包使用与本次章节运行一致的范围。"""
        return f"1-{self.config.end_chapter_seq}" if self.config.end_chapter_seq else "all"

    # ---- 阶段实现 ----

    def _stage_chapter_analysis(self) -> int:
        if self.config.chapter_mode == "interleaved":
            return cmd_run_interleaved(
                self.project_dir, concurrency=self.config.analysis_concurrency,
                end_chapter_seq=self.config.end_chapter_seq, emit=self.emit, **self._run_worker_kwargs(),
            )
        return cmd_run(self.project_dir, phase="analysis",
                        concurrency=self.config.analysis_concurrency,
                        end_chapter_seq=self.config.end_chapter_seq, **self._run_worker_kwargs())

    def _stage_chapter_delta(self) -> int:
        return cmd_run(self.project_dir, phase="delta", end_chapter_seq=self.config.end_chapter_seq,
                       emit=self.emit, **self._run_worker_kwargs())

    def _stage_chapter_structure(self) -> int:
        return cmd_chapter_structure_auto(self._auto_namespace())

    def _stage_visual_assets(self) -> int:
        return cmd_visual_assets_auto(self._auto_namespace())

    def _stage_fullbook_analysis(self) -> int:
        for task in self.config.fullbook_tasks:
            if task in self.state.fullbook_completed_tasks:
                if self._fullbook_task_outputs_ready(task):
                    self.emit("progress", stage="fullbook_analysis", detail=f"resume task={task}")
                    continue
                self.state.fullbook_completed_tasks.remove(task)
                self.state.save()
            self.emit("progress", stage="fullbook_analysis", detail=f"task={task}")
            rc = self._run_fullbook_task(task)
            if rc != 0:
                return rc
            if task not in self.state.fullbook_completed_tasks:
                self.state.fullbook_completed_tasks.append(task)
                self.state.save()
        return 0

    def _fullbook_task_outputs_ready(self, task: str) -> bool:
        """Check that a saved per-task checkpoint still has valid outputs."""
        manifest_path = latest_analysis_manifest(self.project_dir, task)
        if manifest_path is None:
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            outputs = [
                confined_project_path(self.project_dir, rel)
                for rel in manifest.get("target_outputs", [])
            ]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(outputs) and not invalid_output_artifacts(outputs)

    def _run_fullbook_task(self, task: str) -> int:
        # 1. 生成全书分析任务包（packs + manifest + reduce_prompt）
        rc = run_cmd([
            sys.executable, str(script_path("analysis_context_pack.py")), str(self.project_dir),
            "--task", task, "--chapters", self._chapter_spec(),
            "--include-original", self.config.include_original,
            "--max-pack-chars", str(self.config.max_pack_chars),
            "--max-original-chars", str(self.config.max_original_chars),
            "--max-analysis-chars", str(self.config.max_analysis_chars),
            "--story-summary-max-items", str(self.config.story_summary_max_items),
            "--context-budget-chars", str(self.config.context_budget_chars),
        ])
        if rc != 0:
            return rc
        # 2. 读取 manifest
        manifest_path = latest_analysis_manifest(self.project_dir, task)
        if manifest_path is None:
            return 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        packs = manifest.get("packs", [])
        try:
            target_outputs = [
                confined_project_path(self.project_dir, rel)
                for rel in manifest.get("target_outputs", [])
            ]
            pack_paths = [confined_project_path(self.project_dir, rel) for rel in packs]
        except (TypeError, ValueError):
            return 1
        # packs 是实际执行依据；不信任可能过期的冗余 pack_count，避免清单数字与
        # 文件列表不一致时漏跑或错误进入单包分支。
        pack_count = len(packs)
        if not packs or not target_outputs:
            return 1
        out_dir = manifest_path.parent
        wk = dict(
            timeout=self.config.worker_timeout_seconds,
            retries=self.config.worker_retries,
            worker_window=self.config.worker_window,
        )
        if pack_count == 1:
            # 即使只有一个任务包，也可能拥有多个目标产物（如人物分析三件套、
            # 故事结构七件套），必须统一走多产物契约并逐一验收。
            return run_multi_output_worker(
                self._worker_command(), self.project_dir, task,
                pack_paths[0], target_outputs, **wk,
            )
        # 多包：逐包出 partial_result，再 reduce 到 target_outputs
        partials: list = []
        for i, pack_rel in enumerate(packs, 1):
            partial = out_dir / f"partial_result_{i:03d}.md"
            # 原始 pack 中列的是最终产物；增加明确的 partial 覆盖说明，避免
            # worker 在分包阶段提前覆盖最终文件或遗漏 partial。
            worker_pack = write_fullbook_partial_worker_pack(
                self.project_dir, manifest_path, pack_rel, i, pack_count, partial
            )
            rc = run_checkpointed_partial(
                run_worker, self._worker_command(), self.project_dir, task,
                worker_pack, partial, **wk,
            )
            if rc != 0:
                return rc
            partials.append(partial)
        # 复用工作台同一套 reduce 包装逻辑，显式列出全部最终产物并合并 partial。
        reduce_pack = write_fullbook_reduce_worker_pack(
            self.project_dir, manifest_path, partials, target_outputs
        )
        return run_multi_output_worker(
            self._worker_command(), self.project_dir, f"{task}_reduce",
            reduce_pack, target_outputs, **wk,
        )

    def _stage_final_pack(self) -> int:
        # Formal delivery no longer asks a model to rewrite/re-audit the book.
        # Only the introduction is synthesized; strict validation still follows.
        from story_delivery import prepare_delivery, finish_introduction, ensure_delivery_governance
        rc = ensure_delivery_governance(
            self.project_dir, self._worker_command(), end_chapter_seq=self.config.end_chapter_seq,
            timeout=self.config.worker_timeout_seconds, retries=self.config.worker_retries,
            worker_window=self.config.worker_window)
        if rc != 0:
            return rc
        rc = prepare_delivery(self.project_dir)
        if rc != 0:
            return rc
        return finish_introduction(
            self.project_dir, self._worker_command(), run_worker,
            timeout=self.config.worker_timeout_seconds,
            retries=self.config.worker_retries,
            worker_window=self.config.worker_window,
        )

    def _stage_commit_final_draft(self) -> int:
        # commit-final-draft 做 strict 校验；失败时生成 repair_final.md，让 worker
        # 自动切换修复策略并独立验收，最多 max_repair_rounds 种策略。
        draft = self.project_dir / "故事结构_草稿.json"
        repair = self.project_dir / "质量治理" / "最终审计" / "repair_final.md"
        # 无论允许几轮修复，都先做一次提交校验；随后每完成一轮修复都必须
        # 再校验一次，避免“最后一轮已经修好却未验收就返回失败”的边界错误。
        rc = cmd_commit_final_draft(self.project_dir)
        if rc == 0:
            return 0
        if not repair.is_file():
            return 1
        return run_repair_worker(
            self._worker_command(), self.project_dir, "repair_final", repair, draft,
            timeout=self.config.worker_timeout_seconds, retries=self.config.worker_retries,
            worker_window=self.config.worker_window, repair_limit=self.config.max_repair_rounds,
        )

    def _stage_finalize(self) -> int:
        return cmd_finalize(self.project_dir)

    def _stage_export(self) -> int:
        out = self.project_dir / "导出" / "obsidian-vault"
        single = self.project_dir / "导出" / "故事结构总览.md"
        cmd = [
            sys.executable, str(script_path("export_obsidian_vault.py")), str(self.project_dir),
            "--source", self.config.export_source, "--out", str(out),
            "--single-file", str(single),
            "--include-dataview", "--include-canvas", "--clean",
        ]
        return run_cmd(cmd)

    # ---- 停滞签名 ----

    _SIG_PATHS = {
        "chapter_analysis": ("章节处理", "进度.json"),
        "chapter_delta": ("章节处理", "进度.json"),
        "chapter_structure": ("全书分析",),
        "visual_assets": ("全书分析",),
        "fullbook_analysis": ("全书分析",),
        "final_pack": ("故事结构_草稿.json",),
        "commit_final_draft": ("故事结构_草稿.json",),
        "finalize": ("故事结构.json",),
        "export": ("导出",),
    }

    def _signature(self, stage: str) -> str:
        """Hash result contents and semantic progress, excluding repair diagnostics.

        Task regeneration, file touches, and progress heartbeat timestamps do
        not mean a failed stage advanced. Content hashes also detect equal-size
        revisions made within one filesystem timestamp interval.
        """
        def semantic_progress(value):
            if isinstance(value, dict):
                return {key: semantic_progress(item) for key, item in value.items()
                        if key not in {"创建时间", "更新时间", "完成时间"}}
            if isinstance(value, list):
                return [semantic_progress(item) for item in value]
            return value

        rels = self._SIG_PATHS.get(stage, ("进度.json",))
        digest = hashlib.sha256()
        files = 0
        for rel in rels:
            p = self.project_dir / rel
            if not p.exists():
                continue
            candidates = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
            for f in candidates:
                relative = f.relative_to(self.project_dir)
                if (any(part in {"_任务包", "_修复任务", "_诊断", "自动恢复"} for part in relative.parts)
                        or f.name.startswith(".")
                        or f.name.endswith((".tmp", ".log", ".capacity.json", ".capacity-partial.json", ".context-inputs.json"))):
                    continue
                content = f.read_bytes()
                if f.suffix.lower() == ".json":
                    try:
                        value = json.loads(content)
                        if relative.as_posix() == "进度.json":
                            value = semantic_progress(value)
                        content = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")
                    except (ValueError, UnicodeError):
                        # Malformed output still has meaningful bytes; its
                        # validation belongs to the owning stage.
                        pass
                digest.update(relative.as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(content).digest())
                files += 1
        return f"{stage}|files={files}|sha256={digest.hexdigest()}"
