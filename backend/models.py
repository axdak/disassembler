#!/usr/bin/env python3
"""FastAPI 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_FULLBOOK_TASKS = [
    "summary", "characters", "plot", "style", "worldview", "settings",
    "plotlines", "outline", "detailed_outline", "narrative_structure", "report",
]

TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled", "interrupted")
TaskType = Literal[
    "analysis",
    "chapter_visual",
    "chapter_structure",
    "governance",
    "export",
    "append_chapters",
    "chapter_analysis_rerun",
    "chapter_delta_rerun",
    "recover_story",
    "migrate_chronology",
    "finalize",
    "normalize",
    "repair",
]


class ProjectCreate(BaseModel):
    path: str
    name: Optional[str] = None


class Project(BaseModel):
    id: int
    name: str
    path: str
    run_end_chapter_seq: int = 0  # 项目级自动运行截止章；0 表示全书
    created_at: str
    updated_at: str


class RunConfig(BaseModel):
    """Validated configuration shared by direct runs, queues, and saved settings."""

    model_config = ConfigDict(extra="forbid")

    worker_provider: Literal["manual", "auto", "agy", "codebuddy"] = "agy"
    worker_command: str = ""
    worker_loop: Literal["supervised", "continuous"] = "continuous"
    worker_window: Literal["hidden", "powershell", "powershell-keep"] = "hidden"
    worker_retries: int = Field(default=2, ge=0)
    worker_timeout_seconds: int = Field(default=1200, gt=0)
    # Token 容量与下面的字符分包阈值独立；运行时固定快照，不热改活跃任务。
    model_input_tokens: int = Field(default=300000, gt=0, strict=True)
    # 单次输出上限必须容纳单章 Delta/分析（含推理型模型的思维链），20000 会导致长输出被截断。
    model_output_tokens: int = Field(default=64000, gt=0, strict=True)
    model_context_tokens: int = Field(default=300000, ge=0, strict=True)
    model_reserve_tokens: int = Field(default=30000, ge=0, strict=True)
    # 分片定位索引的请求上限；引擎仍按输入/输出容量计算实际安全额度。
    model_overview_tokens: int = Field(default=4000, gt=0, strict=True)
    max_attempts: int = Field(default=10, gt=0)
    stagnant_limit: int = Field(default=3, gt=0)
    max_repair_rounds: int = Field(default=3, ge=0)
    analysis_concurrency: int = Field(default=1, gt=0)
    per_chapter_concurrency: int = Field(default=2, gt=0)  # 分章任务并行度；1=逐章串行
    chapter_mode: Literal["two_phase", "interleaved"] = "two_phase"
    end_chapter_seq: int = Field(default=0, ge=0)  # 0=全书；正数为本次运行包含的最后章节序号
    fullbook_tasks: list[str] = Field(default_factory=lambda: list(DEFAULT_FULLBOOK_TASKS))
    export_source: Literal["final", "process"] = "final"
    # 任务包上下文压缩策略（可经设置页配置）
    include_original: Literal["auto", "none", "sample", "full"] = "auto"
    max_original_chars: int = Field(default=6000, gt=0)
    max_analysis_chars: int = Field(default=12000, gt=0)
    story_summary_max_items: int = Field(default=0, ge=0)  # 0=不限制（全名称索引）
    context_budget_chars: int = Field(default=160000, gt=0)
    delta_full_story_kb: int = Field(default=256, gt=0)
    audit_max_context_chars: int = Field(default=524288, gt=0)
    max_pack_chars: int = Field(default=70000, gt=0)  # 全书分析任务包单包最大字符数

    @model_validator(mode="after")
    def validate_capacity(self):
        """Reject impossible budgets before saving settings or scheduling a run."""
        shared = (self.model_context_tokens - self.model_output_tokens
                  if self.model_context_tokens else self.model_input_tokens)
        if min(self.model_input_tokens, shared) <= self.model_reserve_tokens:
            raise ValueError("模型容量不足：扣除输出和安全预留后，可用输入必须大于零")
        return self


class RunStart(BaseModel):
    # Only overrides are supplied here; validate after merging saved settings.
    config: dict = Field(default_factory=dict)


class RunRangeUpdate(BaseModel):
    end_chapter_seq: int = Field(ge=0)


class Run(BaseModel):
    id: int
    project_id: int
    status: str
    started_at: str
    ended_at: Optional[str] = None
    rc: Optional[int] = None
    config: dict = {}


class WorkbenchTask(BaseModel):
    id: int
    project_id: int
    task_type: str
    status: Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]
    params: dict = {}
    input_revision: dict = {}
    result: Optional[dict] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    retry_of: Optional[int] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    stale: bool = False


class TaskCreate(BaseModel):
    type: TaskType
    params: dict


class StageEvent(BaseModel):
    id: int
    run_id: Optional[int] = None
    project_id: Optional[int] = None
    type: str
    stage: Optional[str] = None
    detail: Optional[str] = None
    data: dict = {}
    ts: str


class SettingsUpdate(BaseModel):
    # PATCH 语义：先与已保存设置合并，再用 RunConfig 校验，避免拿默认窗口误判局部更新。
    settings: dict


class QueueAdd(BaseModel):
    project_id: int
    config: Optional[dict] = None


class FromBook(BaseModel):
    source_file: str
    project_dir: str = ""


class AppendPreview(BaseModel):
    source_file: str
    pattern: str = ""
    increment_only: bool = False


class AppendApply(BaseModel):
    plan: dict


class Status(BaseModel):
    project: Project
    active_run: Optional[Run] = None
    current_stage: Optional[str] = None
    completed: list[str] = []
    last_error: Optional[str] = None
    recent_events: list[StageEvent] = []
    revisions: dict[str, str] = {}
    task_summary: dict[str, int] = {}
    stale_task_count: int = 0


CardState = Literal[
    "missing", "latest", "stale", "queued", "running", "failed", "cancelled"
]


class CardStatus(BaseModel):
    status: CardState
    artifact_present: bool = False
    latest_task_id: Optional[int] = None
    latest_task_status: Optional[str] = None
    error: Optional[str] = None
    paths: list[str] = []


class ChapterCardStatus(BaseModel):
    seq: int
    filename: str
    title: str = ""
    cards: dict[str, CardStatus] = {}
    stale: bool = False
    latest_task: Optional[dict] = None
    error: Optional[str] = None


class ArtifactStatusResponse(BaseModel):
    dimensions: dict[str, CardStatus] = {}
    chapters: list[ChapterCardStatus] = []
    exports: dict[str, CardStatus] = {}
    counts: dict[str, int] = {}


class RecoverSnapshot(BaseModel):
    snapshot_seq: int
    path: str
    size: int
    replayable_to: int
