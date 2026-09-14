#!/usr/bin/env python3
"""SQLite 持久化：projects / runs / events / settings。

仅作 UI 缓存与历史查询；运行真实状态以项目目录下的实际产物与
质量治理/编排/状态.json 为准（SQLite 百分比不作完成依据）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    rc INTEGER,
    config_json TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    project_id INTEGER,
    type TEXT NOT NULL,
    stage TEXT,
    detail TEXT,
    data_json TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'interrupted')),
    params_json TEXT NOT NULL,
    input_revision_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(retry_of) REFERENCES workbench_tasks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_tasks_project_status_id
    ON workbench_tasks(project_id, status, id);
CREATE TABLE IF NOT EXISTS run_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_queue_id ON run_queue(id);
"""

TASK_STATUSES = frozenset({"queued", "running", "done", "failed", "cancelled", "interrupted"})
_UNSET = object()


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()  # 可重入：create_project/create_run 持锁时调用 get_project/get_run
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- projects ----

    def create_project(self, path: str, name: str = "") -> dict:
        ts = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO projects(name, path, created_at, updated_at) VALUES(?,?,?,?)",
                (name or Path(path).name, path, ts, ts),
            )
            self._conn.commit()
            return self.get_project(cur.lastrowid)

    def list_projects(self) -> list:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_project(self, project_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def get_project_by_path(self, path: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
            return dict(row) if row else None

    def delete_project(self, project_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            self._conn.commit()

    def has_nonterminal_tasks(self, project_id: int) -> bool:
        """Return whether queued/running work still references the project."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM workbench_tasks "
                "WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
                (project_id,),
            ).fetchone()
            return row is not None

    # ---- persistent automatic-run queue ----

    def create_queue_item(self, project_id: int, config: dict) -> dict:
        """Persist one automatic run request before it can be scheduled."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO run_queue(project_id, config_json, created_at) VALUES(?,?,?)",
                (project_id, json.dumps(config, ensure_ascii=False), _now()),
            )
            self._conn.commit()
            return {
                "id": cur.lastrowid,
                "project_id": project_id,
                "config": dict(config),
            }

    def list_queue_items(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM run_queue ORDER BY id ASC").fetchall()
            return [
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "config": json.loads(row["config_json"] or "{}"),
                }
                for row in rows
            ]

    def delete_queue_item(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM run_queue WHERE id=?", (item_id,))
            self._conn.commit()

    def clear_queue(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM run_queue")
            self._conn.commit()

    # ---- runs ----

    def create_run(self, project_id: int, config: dict) -> dict:
        ts = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(project_id, status, started_at, config_json) VALUES(?,?,?,?)",
                (project_id, "running", ts, json.dumps(config, ensure_ascii=False)),
            )
            self._conn.commit()
            return self.get_run(cur.lastrowid)

    def update_run(self, run_id: int, status: Optional[str] = None,
                   rc: Optional[int] = None, ended_at: Optional[str] = None) -> None:
        fields, vals = [], []
        if status is not None:
            fields.append("status=?")
            vals.append(status)
        if rc is not None:
            fields.append("rc=?")
            vals.append(rc)
        if ended_at is not None:
            fields.append("ended_at=?")
            vals.append(ended_at)
        if not fields:
            return
        vals.append(run_id)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {','.join(fields)} WHERE id=?", vals)
            self._conn.commit()

    def get_run(self, run_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["config"] = json.loads(d.pop("config_json") or "{}")
            return d

    def get_active_run(self, project_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE project_id=? AND status='running' ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["config"] = json.loads(d.pop("config_json") or "{}")
            return d

    def mark_running_runs_interrupted(self) -> list:
        """上一会话残留的 running 运行（崩溃/关闭未正常结束）标记为 interrupted。

        返回 [(run_id, project_id), ...]，供调用方追加 interrupted 事件。
        """
        ts = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, project_id FROM runs WHERE status='running'"
            ).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'",
                    (ts,),
                )
                self._conn.commit()
        return [(r["id"], r["project_id"]) for r in rows]

    # ---- workbench tasks ----

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> dict:
        task = dict(row)
        task["params"] = json.loads(task.pop("params_json") or "{}")
        task["input_revision"] = json.loads(task.pop("input_revision_json") or "{}")
        task["result"] = json.loads(task.pop("result_json") or "null")
        task["cancel_requested"] = bool(task["cancel_requested"])
        return task

    def create_task(self, project_id: int, task_type: str, params: dict,
                    input_revision: dict, retry_of: Optional[int] = None) -> dict:
        """Persist a queued workbench task; reruns are new rows linked by retry_of."""
        ts = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO workbench_tasks("
                "project_id, task_type, status, params_json, input_revision_json, retry_of, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    project_id, task_type, "queued", json.dumps(params, ensure_ascii=False),
                    json.dumps(input_revision, ensure_ascii=False), retry_of, ts, ts,
                ),
            )
            self._conn.commit()
            return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workbench_tasks WHERE id=?", (task_id,)
            ).fetchone()
            return self._decode_task(row) if row else None

    def list_tasks(self, project_id: int, limit: int = 100) -> list:
        with self._lock:
            rows = self._conn.execute(
                # 先截取最新记录，再按时间正序返回，兼顾 UI 时间线顺序和长期使用时
                # “最新任务不能被最早 100 条挤掉”的要求。
                "SELECT * FROM ("
                "SELECT * FROM workbench_tasks WHERE project_id=? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (project_id, limit),
            ).fetchall()
            return [self._decode_task(row) for row in rows]

    def list_queued_tasks(self) -> list:
        """Return every queued task in global FIFO order for scheduler admission."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workbench_tasks WHERE status='queued' ORDER BY id ASC"
            ).fetchall()
            return [self._decode_task(row) for row in rows]

    def update_task(self, task_id: int, *, status: Optional[str] = None,
                    result: Any = _UNSET, error: Any = _UNSET,
                    cancel_requested: Optional[bool] = None,
                    retry_of: Any = _UNSET,
                    started_at: Any = _UNSET, ended_at: Any = _UNSET) -> Optional[dict]:
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"无效的任务状态: {status}")
        fields, values = [], []
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if result is not _UNSET:
            fields.append("result_json=?")
            values.append(json.dumps(result, ensure_ascii=False))
        if error is not _UNSET:
            fields.append("error=?")
            values.append(error)
        if cancel_requested is not None:
            fields.append("cancel_requested=?")
            values.append(int(cancel_requested))
        if retry_of is not _UNSET:
            fields.append("retry_of=?")
            values.append(retry_of)
        if started_at is not _UNSET:
            fields.append("started_at=?")
            values.append(started_at)
        if ended_at is not _UNSET:
            fields.append("ended_at=?")
            values.append(ended_at)
        if not fields:
            return self.get_task(task_id)
        fields.append("updated_at=?")
        values.extend([_now(), task_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE workbench_tasks SET {', '.join(fields)} WHERE id=?", values
            )
            self._conn.commit()
        return self.get_task(task_id)

    def request_task_cancel(self, task_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE workbench_tasks SET cancel_requested=1, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_running_tasks_interrupted(self) -> list:
        """Mark residual workbench tasks interrupted after a process restart."""
        ts = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, project_id FROM workbench_tasks WHERE status='running' ORDER BY id"
            ).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE workbench_tasks SET status='interrupted', ended_at=?, updated_at=? "
                    "WHERE status='running'",
                    (ts, ts),
                )
                self._conn.commit()
        return [(row["id"], row["project_id"]) for row in rows]

    # ---- events ----

    def add_event(self, run_id: Optional[int], project_id: Optional[int], type: str,
                  stage: str = "", detail: str = "", data: Optional[dict] = None, ts: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(run_id, project_id, type, stage, detail, data_json, ts) VALUES(?,?,?,?,?,?,?)",
                (run_id, project_id, type, stage, detail,
                 json.dumps(data or {}, ensure_ascii=False), ts or _now()),
            )
            self._conn.commit()

    def list_events(self, project_id: Optional[int] = None, run_id: Optional[int] = None,
                    limit: int = 200) -> list:
        q = "SELECT * FROM events"
        conds, vals = [], []
        if project_id is not None:
            conds.append("project_id=?")
            vals.append(project_id)
        if run_id is not None:
            conds.append("run_id=?")
            vals.append(run_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        vals.append(limit)
        with self._lock:
            rows = self._conn.execute(q, vals).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json") or "{}")
            out.append(d)
        out.reverse()  # 时间正序
        return out

    # ---- settings ----

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            return json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()

    def all_settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value_json FROM settings").fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}
