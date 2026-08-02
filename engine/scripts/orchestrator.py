#!/usr/bin/env python3
"""全流程编排器。

把现有引擎命令串成一键全流程（章节分析/Delta -> 章节结构 -> 视觉资产 -> 全书分析
-> 最终整理 -> 导出），加状态机、worker 重试、硬阻塞判定与结构化事件。

设计要点：
- 直接 import 调用 run_pipeline 的 cmd_*（显式参数），不经过 CLI 翻译层。
- worker 阶段使用 worker_loop="continuous"，由 cmd_run / cmd_*_auto 内部管理 worker
  子进程、重试与验收；编排器只处理阶段级返回码。
- 阶段级事件（开始/结束/失败/硬阻塞）经 EventBus 发射；章节级细进度由后端轮询
  进度.json / _索引.json（里程碑3）。
- 崩溃恢复：加载状态文件，从 current_stage 续跑；各 cmd 自身按实际产物幂等续跑。
- SQLite 百分比不作完成依据，以实际产物为准。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from events import Event, EventBus
from pipeline_state import PipelineState
from run_pipeline import (
    builtin_agent_worker_command,
    cmd_chapter_structure_auto,
    cmd_commit_final_draft,
    cmd_final_pack,
    cmd_finalize,
    cmd_run,
    cmd_run_interleaved,
    cmd_visual_assets_auto,
    latest_analysis_manifest,
    run_cmd,
    run_multi_output_worker,
    run_worker,
    script_path,
    set_cancel_event,
)

# 全书分析任务依赖顺序（阶段5）
FULLBOOK_TASKS = (
    "summary", "characters", "plot", "worldview", "settings",
    "plotlines", "outline", "detailed_outline", "narrative_structure", "report",
)


@dataclass
class Config:
    worker_provider: str = "agy"
    worker_command: str = ""  # 非空时作为显式 worker 命令（provider 视为 manual），用于假 worker 测试与里程碑4
    worker_loop: str = "continuous"
    worker_retries: int = 2
    worker_timeout_seconds: int = 1200
    worker_window: str = "hidden"
    max_attempts: int = 10  # 安全上限；真正硬阻塞由 stagnant_limit 触发
    stagnant_limit: int = 3
    max_repair_rounds: int = 3
    analysis_concurrency: int = 1  # 分析阶段并行度（delta 仍串行）
    per_chapter_concurrency: int = 2  # 分章任务（章节结构/视觉资产）并行度；1=逐章串行（兼容旧行为）
    chapter_mode: str = "two_phase"  # two_phase | interleaved
    fullbook_tasks: tuple = FULLBOOK_TASKS
    export_source: str = "final"
    # 任务包上下文压缩策略（可经设置页配置）
    include_original: str = "auto"  # auto | none | sample | full
    max_original_chars: int = 6000
    max_analysis_chars: int = 12000
    story_summary_max_items: int = 80
    context_budget_chars: int = 160000
    delta_full_story_kb: int = 256
    audit_max_context_chars: int = 524288


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
        """运行全流程。返回 0=全部完成，1=硬阻塞/致命，2=已取消。"""
        set_cancel_event(self._cancel)
        try:
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
            max_pack_chars=70000,
            force_aggregate=False,
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

    # ---- 阶段实现 ----

    def _stage_chapter_analysis(self) -> int:
        if self.config.chapter_mode == "interleaved":
            return cmd_run_interleaved(
                self.project_dir, concurrency=self.config.analysis_concurrency,
                emit=self.emit, **self._run_worker_kwargs(),
            )
        return cmd_run(self.project_dir, phase="analysis",
                        concurrency=self.config.analysis_concurrency, **self._run_worker_kwargs())

    def _stage_chapter_delta(self) -> int:
        return cmd_run(self.project_dir, phase="delta", **self._run_worker_kwargs())

    def _stage_chapter_structure(self) -> int:
        return cmd_chapter_structure_auto(self._auto_namespace())

    def _stage_visual_assets(self) -> int:
        return cmd_visual_assets_auto(self._auto_namespace())

    def _stage_fullbook_analysis(self) -> int:
        for task in self.config.fullbook_tasks:
            self.emit("progress", stage="fullbook_analysis", detail=f"task={task}")
            rc = self._run_fullbook_task(task)
            if rc != 0:
                return rc
        return 0

    def _run_fullbook_task(self, task: str) -> int:
        # 1. 生成全书分析任务包（packs + manifest + reduce_prompt）
        rc = run_cmd([
            sys.executable, str(script_path("analysis_context_pack.py")), str(self.project_dir),
            "--task", task, "--chapters", "all", "--include-original", "sample",
            "--max-pack-chars", "70000", "--max-original-chars", "6000", "--max-analysis-chars", "12000",
        ])
        if rc != 0:
            return rc
        # 2. 读取 manifest
        manifest_path = latest_analysis_manifest(self.project_dir, task)
        if manifest_path is None:
            return 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        packs = manifest.get("packs", [])
        target_outputs = [self.project_dir / rel for rel in manifest.get("target_outputs", [])]
        pack_count = manifest.get("pack_count", len(packs))
        if not packs or not target_outputs:
            return 1
        out_dir = manifest_path.parent
        wk = dict(
            timeout=self.config.worker_timeout_seconds,
            retries=self.config.worker_retries,
            worker_window=self.config.worker_window,
        )
        if pack_count == 1:
            # 单包：worker 直接写目标产物
            return run_worker(
                self._worker_command(), self.project_dir, task,
                self.project_dir / packs[0], target_outputs[0], **wk,
            )
        # 多包：逐包出 partial_result，再 reduce 到 target_outputs
        partials: list = []
        for i, pack_rel in enumerate(packs, 1):
            partial = out_dir / f"partial_result_{i:03d}.md"
            rc = run_worker(
                self._worker_command(), self.project_dir, task,
                self.project_dir / pack_rel, partial, **wk,
            )
            if rc != 0:
                return rc
            partials.append(partial)
        # reduce 任务包 = reduce_prompt + 所有 partial
        reduce_prompt = self.project_dir / manifest.get("reduce_prompt", "")
        parts: list = []
        if reduce_prompt.is_file():
            parts.append(reduce_prompt.read_text(encoding="utf-8"))
        for p in partials:
            parts.append(f"# partial: {p.name}\n\n{p.read_text(encoding='utf-8', errors='ignore')}")
        reduce_pack = out_dir / "reduce_task_pack.md"
        reduce_pack.write_text("\n\n".join(parts), encoding="utf-8")
        return run_multi_output_worker(
            self._worker_command(), self.project_dir, f"{task}_reduce",
            reduce_pack, target_outputs, **wk,
        )

    def _stage_final_pack(self) -> int:
        # final-pack 生成草稿与最终整理任务包；随后让 worker 按任务包修改草稿。
        # 草稿最终是否合格由 commit_final_draft 阶段的 strict 校验把关。
        rc = cmd_final_pack(self.project_dir)
        if rc != 0:
            return rc
        task_pack = self.project_dir / "质量治理" / "最终审计" / "final_task_pack.md"
        draft = self.project_dir / "故事结构_草稿.json"
        return run_worker(
            self._worker_command(), self.project_dir, "final_draft",
            task_pack, draft,
            timeout=self.config.worker_timeout_seconds,
            retries=self.config.worker_retries,
            worker_window=self.config.worker_window,
        )

    def _stage_commit_final_draft(self) -> int:
        # commit-final-draft 做 strict 校验；失败时生成 repair_final.md，让 worker
        # 修复草稿后重试，最多 max_repair_rounds 轮。
        draft = self.project_dir / "故事结构_草稿.json"
        repair = self.project_dir / "质量治理" / "最终审计" / "repair_final.md"
        for _ in range(self.config.max_repair_rounds):
            rc = cmd_commit_final_draft(self.project_dir)
            if rc == 0:
                return 0
            if not repair.is_file():
                return 1
            wrc = run_worker(
                self._worker_command(), self.project_dir, "repair_final",
                repair, draft,
                timeout=self.config.worker_timeout_seconds,
                retries=self.config.worker_retries,
                worker_window=self.config.worker_window,
            )
            if wrc != 0:
                return 1
        return 1

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
        """阶段关键产物签名，用于停滞检测。按阶段选取关键路径，递归统计文件数/大小/最新mtime。"""
        rels = self._SIG_PATHS.get(stage, ("进度.json",))
        files = size = max_mtime = 0
        for rel in rels:
            p = self.project_dir / rel
            if not p.exists():
                continue
            candidates = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
            for f in candidates:
                st = f.stat()
                size += st.st_size
                if st.st_mtime > max_mtime:
                    max_mtime = st.st_mtime
                files += 1
        return f"{stage}|files={files}|size={size}|mtime={int(max_mtime)}"
