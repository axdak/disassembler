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
"""


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()  # 可重入：create_project/create_run 持锁时调用 get_project/get_run
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
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
