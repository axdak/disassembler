#!/usr/bin/env python3
"""FastAPI 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

DEFAULT_FULLBOOK_TASKS = [
    "summary", "characters", "plot", "worldview", "settings",
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
    worker_provider: str = "agy"
    worker_command: str = ""
    worker_loop: str = "continuous"
    worker_retries: int = 2
    worker_timeout_seconds: int = 1800
    max_attempts: int = 10
    stagnant_limit: int = 3
    max_repair_rounds: int = 3
    analysis_concurrency: int = 1
    chapter_mode: str = "two_phase"
    fullbook_tasks: list[str] = DEFAULT_FULLBOOK_TASKS
    export_source: str = "final"


class RunStart(BaseModel):
    config: RunConfig = RunConfig()


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
    settings: dict[str, Any]


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
