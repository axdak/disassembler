#!/usr/bin/env python3
"""FastAPI 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

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
    max_attempts: int = Field(default=10, gt=0)
    stagnant_limit: int = Field(default=3, gt=0)
    max_repair_rounds: int = Field(default=3, ge=0)
    analysis_concurrency: int = Field(default=1, gt=0)
    per_chapter_concurrency: int = Field(default=2, gt=0)  # 分章任务并行度；1=逐章串行
    chapter_mode: Literal["two_phase", "interleaved"] = "two_phase"
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


class RunStart(BaseModel):
    config: RunConfig = Field(default_factory=RunConfig)


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
    # RunConfig 全部字段都有默认值；接口使用 exclude_unset，仅保存调用方显式传入项。
    settings: RunConfig


class QueueAdd(BaseModel):
    project_id: int
    config: Optional[RunConfig] = None


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
