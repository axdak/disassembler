#!/usr/bin/env python3
"""全流程编排状态机：阶段序列、尝试/停滞计数、持久化。

恢复语义（根据实际产物判定续跑点）由 orchestrator.py 负责；本模块只做状态持久化
与阶段流转辅助。状态文件写入项目目录 质量治理/编排/状态.json，是崩溃恢复的依据之一，
但最终以实际产物为准（SQLite 百分比不作数）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 全流程阶段顺序。项目初始化与拆章（init-dirs/split）由桌面端“接管/新建”负责，
# 编排器从 chapter_analysis 起跑。
STAGES = [
    "chapter_analysis",
    "chapter_delta",
    "chapter_structure",
    "visual_assets",
    "fullbook_analysis",
    "final_pack",
    "commit_final_draft",
    "finalize",
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
            self.current_stage = STAGES[idx + 1] if idx + 1 < len(STAGES) else DONE
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
            return st
        st = cls(project_dir=str(project_dir))
        st.started_at = time.time()
        return st

    def save(self) -> None:
        self.updated_at = time.time()
        path = Path(self.project_dir) / STATE_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
