#!/usr/bin/env python3
"""全流程编排状态机：阶段序列、尝试/停滞计数、持久化。

恢复语义（根据实际产物判定续跑点）由 orchestrator.py 负责；本模块只做状态持久化
与阶段流转辅助。状态文件写入项目目录 质量治理/编排/状态.json，是崩溃恢复的依据之一，
但最终以实际产物为准（SQLite 百分比不作数）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from delivery_workflow import DELIVERY_STAGES

# 全流程阶段顺序。项目初始化与拆章（init-dirs/split）由桌面端“接管/新建”负责，
# 编排器从 chapter_analysis 起跑。
STAGES = [
    "chapter_analysis",
    "chapter_delta",
    *DELIVERY_STAGES,
    "chapter_structure",
    "visual_assets",
    "fullbook_analysis",
    "export",
]
DONE = "done"

STATE_REL = "质量治理/编排/状态.json"


@dataclass
class PipelineState:
    project_dir: str
    current_stage: str = STAGES[0]
    completed: list[str] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""
    # 下游报告按分析维度落细粒度断点；崩溃恢复时只重跑缺失/损坏的维度。
    fullbook_completed_tasks: list[str] = field(default_factory=list)
    end_chapter_seq: int = 0  # 上次运行范围；变更时编排器需从章节阶段续跑
    flow_version: int = 2  # JSON delivery precedes optional downstream reports.

    def _stage(self, stage: str) -> dict[str, Any]:
        return self.stages.setdefault(
            stage, {"attempts": 0, "stagnant_count": 0, "last_signature": ""}
        )

    def attempts(self, stage: str) -> int:
        return self._stage(stage).get("attempts", 0)

    def bump_attempt(self, stage: str) -> int:
        s = self._stage(stage)
        s["attempts"] = s.get("attempts", 0) + 1
        return s["attempts"]

    def reset_stage(self, stage: str) -> None:
        self.stages[stage] = {"attempts": 0, "stagnant_count": 0, "last_signature": ""}

    def stagnant(self, stage: str) -> int:
        return self._stage(stage).get("stagnant_count", 0)

    def record_signature(self, stage: str, signature: str) -> int:
        """记录阶段关键产物签名；与上次相同则递增停滞计数，否则清零。返回当前停滞计数。"""
        s = self._stage(stage)
        if signature and signature == s.get("last_signature"):
            s["stagnant_count"] = s.get("stagnant_count", 0) + 1
        else:
            s["stagnant_count"] = 0
        s["last_signature"] = signature
        return s["stagnant_count"]

    def mark_completed(self, stage: str) -> None:
        if stage not in self.completed:
            self.completed.append(stage)
        if stage in STAGES:
            idx = STAGES.index(stage)
            self.current_stage = next((item for item in STAGES[idx + 1:] if item not in self.completed), DONE)
        self.reset_stage(stage)

    def is_done(self) -> bool:
        return self.current_stage == DONE

    @classmethod
    def load(cls, project_dir: Path) -> "PipelineState":
        path = project_dir / STATE_REL
        if path.is_file():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                d = {}
            st = cls(project_dir=str(project_dir))
            st.current_stage = d.get("current_stage", STAGES[0])
            st.completed = list(d.get("completed", []))
            st.stages = dict(d.get("stages", {}))
            st.started_at = d.get("started_at", 0.0)
            st.updated_at = d.get("updated_at", 0.0)
            st.last_error = d.get("last_error", "")
            st.fullbook_completed_tasks = list(d.get("fullbook_completed_tasks", []))
            st.end_chapter_seq = d.get("end_chapter_seq", 0)
            st.flow_version = d.get("flow_version", 1)
            return st
        st = cls(project_dir=str(project_dir))
        st.started_at = time.time()
        return st

    def save(self) -> None:
        self.updated_at = time.time()
        path = Path(self.project_dir) / STATE_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        # 状态文件是自动恢复的依据；先完整写入同目录临时文件，再原子替换，
        # 避免断电或进程退出留下半截 JSON，使后续运行错误地从头开始。
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(self), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
