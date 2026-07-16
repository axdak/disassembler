#!/usr/bin/env python3
"""FastAPI 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

DEFAULT_FULLBOOK_TASKS = [
    "summary", "characters", "plot", "worldview", "settings",
    "plotlines", "outline", "detailed_outline", "narrative_structure", "report",
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


class Status(BaseModel):
    project: Project
    active_run: Optional[Run] = None
    current_stage: Optional[str] = None
    completed: list[str] = []
    last_error: Optional[str] = None
    recent_events: list[StageEvent] = []
