"""Governance task adapter tests (Task 4 Step 4).

Asserts:
- correction validation failure leaves process JSON unchanged (rolled back)
- success records before/after/diff in the audit status
- post-apply structure failure restores the before snapshot
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from task_adapters import GovernanceTaskAdapter
from workbench import TaskContext

# Make engine/scripts importable so the adapter and its helpers run.
_ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "engine" / "scripts"
if str(_ENGINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ENGINE_SCRIPTS))


AUTH = {"Authorization": "Bearer sekret"}


def _progress(*args, **kwargs):
    pass


def _make_context(project_path: Path, params: dict) -> TaskContext:
    return TaskContext(
        task_id=1,
        project_id=1,
        project_path=Path(project_path),
        params=params,
        cancellation_event=threading.Event(),
        _progress_callback=_progress,
    )


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_story() -> dict:
    return {
        "介绍": {"标题": "治理适配器测试", "描述": "周期治理回归检查。"},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [],
        "其他事项集": [],
    }


def _empty_buckets() -> dict:
    keys = ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]
    return {k: [] for k in keys}


def _empty_delta(seq: int) -> dict:
    return {"章节": f"第{seq:03d}章", "新增元素": _empty_buckets(), "修改元素": _empty_buckets()}


_VALID_ANALYSIS_TEMPLATE = """# 第{seq:03d}章分析

## 1. 剧情梗概
本章围绕主角接到意外来信后前往约定地点展开。

## 2. 出场人物
- 主角：阅读来信后从迟疑转为主动求证。

## 3. 核心冲突
主角需要在相信旧承诺和面对新证据之间作出选择。

## 4. 信息增量
来信说明旧日承诺存在未公开条件。

## 5. 伏笔与悬念
陌生人的身份及其真实目的尚待后续揭示。

## 6. 爽点 / 虐点 / 情绪点
主动追查带来期待、紧张与危机感。

## 7. 章节功能判断
本章承担冲突启动和悬念铺垫功能。

## 8. 事件分组与标签建议
来信求证承诺线冲突启动主动追查身份悬念。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 |
|----------|----------|----------|
| V01 | 主角阅读来信 | 中 |

## 10. 结构提取提示
应提取来信事件、约定地点和陌生人线索。
"""


def _write_completed_chapter(project: Path, seq: int) -> Path:
    chapter = project / "原文拆解" / f"第{seq:03d}章_测试.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
    base = chapter.stem
    (project / "章节处理").mkdir(parents=True, exist_ok=True)
    (project / "章节处理" / chapter.name).write_text(
        _VALID_ANALYSIS_TEMPLATE.format(seq=seq), encoding="utf-8"
    )
    _write_json(project / "章节处理" / f"{base}.json", _empty_delta(seq))
    for rel in [
        f"质量治理/delta校验/{base}.json",
        f"质量治理/章节校验/{base}.json",
    ]:
        _write_json(project / rel, {"passed": True, "mode": "process",
                                     "errors": [], "warnings": []})
    return chapter


def _seed_project(tmp_path: Path, with_character: bool = False) -> Path:
    project = tmp_path / "拆书_测试"
    story = _empty_story()
    if with_character:
        story["角色集"].append({
            "名称": "约翰娜",
            "是否主角": False,
            "性别": 1,
            "年龄": 0,
            "生日": "0001-01-01T00:00:00",
            "所属阵营": [],
            "关系": [],
            "分组": "测试",
            "别名": [],
            "标签集": [],
            "介绍": "测试角色。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        })
    _write_json(project / "故事结构_增量.json", story)
    _write_completed_chapter(project, 1)
    return project


def _no_change_correction(start: int, end: int) -> dict:
    return {
        "章节范围": f"第{start:03d}章-第{end:03d}章",
        "治理类型": "no_change",
        "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
        "证据范围": [f"第{seq:03d}章" for seq in range(start, end + 1)],
        "新增元素": _empty_buckets(),
        "修改元素": _empty_buckets(),
    }


def _bad_correction_missing_participant(start: int, end: int) -> dict:
    """Fails validate_delta governance: unregistered participant."""
    bucket = _empty_buckets()
    bucket["事件集"] = [{
        "名称": "坏事件",
        "发生地点": "",
        "参与成员": ["未注册角色"],
        "重量级": 20,
        "目标事件": [],
        "分组": "00000010-坏事件",
        "时间": "0001-01-01T00:00:00",
        "别名": [],
        "标签集": [],
        "介绍": "坏事件。",
        "详情": {"涉及章节": "0001", "提取理由": "测试治理校验失败。"},
    }]
    return {
        "章节范围": f"第{start:03d}章-第{end:03d}章",
        "新增元素": bucket,
        "修改元素": _empty_buckets(),
    }


def _structure_breaking_correction(start: int, end: int) -> dict:
    """Passes validate_delta (治理操作 is not validated there) but fails
    validate_structure governance: 设置字段 swaps a character's
    首次章节/最近章节 so first > recent, which normalize preserves and
    validate_structure rejects for TRACEABLE collections.
    """
    return {
        "章节范围": f"第{start:03d}章-第{end:03d}章",
        "新增元素": _empty_buckets(),
        "修改元素": _empty_buckets(),
        "治理操作": {
            "设置字段": [
                {
                    "类型": "角色", "名称": "约翰娜", "字段": "详情",
                    "值": {
                        "首次章节": "0099", "最近章节": "0001",
                        "提取理由": "测试治理结构校验失败。",
                    },
                }
            ]
        },
    }


# --- Adapter level --------------------------------------------------------

def test_governance_adapter_success_records_before_after_diff(tmp_path):
    project = _seed_project(tmp_path)
    start, end = 1, 1
    from run_pipeline import audit_paths
    paths = audit_paths(project, start, end)

    # Pre-write the audit correction so the adapter skips the worker step.
    _write_json(paths["correction"], _no_change_correction(start, end))

    adapter = GovernanceTaskAdapter()
    ctx = _make_context(project, {"start": start, "end": end})
    result = adapter.run(ctx)

    assert result["task"] == "governance"
    assert result["before"] and Path(result["before"]).is_file()
    assert result["after"] and Path(result["after"]).is_file()
    assert result["diff"] and Path(result["diff"]).is_file()
    # Audit status JSON records the commit.
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    assert status["status"] == "committed"
    assert status["before"] == result["before"]
    assert status["after"] == result["after"]
    assert status["diff"] == result["diff"]


def test_governance_adapter_validation_failure_leaves_process_unchanged(tmp_path):
    project = _seed_project(tmp_path)
    start, end = 1, 1
    from run_pipeline import audit_paths
    paths = audit_paths(project, start, end)

    process_path = project / "故事结构_增量.json"
    before_process = process_path.read_text(encoding="utf-8")

    _write_json(paths["correction"], _bad_correction_missing_participant(start, end))

    adapter = GovernanceTaskAdapter()
    ctx = _make_context(project, {"start": start, "end": end})
    with pytest.raises(Exception):
        adapter.run(ctx)

    # Process JSON untouched (commit_governance rolled back to before snapshot).
    assert process_path.read_text(encoding="utf-8") == before_process
    # Audit status is NOT committed.
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    assert status["status"] != "committed"


def test_governance_adapter_structure_failure_restores_before(tmp_path):
    project = _seed_project(tmp_path, with_character=True)
    start, end = 1, 1
    from run_pipeline import audit_paths
    paths = audit_paths(project, start, end)

    process_path = project / "故事结构_增量.json"
    before_obj = json.loads(process_path.read_text(encoding="utf-8"))

    _write_json(paths["correction"], _structure_breaking_correction(start, end))

    adapter = GovernanceTaskAdapter()
    ctx = _make_context(project, {"start": start, "end": end})
    with pytest.raises(Exception):
        adapter.run(ctx)

    # Structure failure rolled the process JSON back to the before snapshot.
    after_obj = json.loads(process_path.read_text(encoding="utf-8"))
    assert after_obj == before_obj
    # The invalid 首次章节/最近章节 swap was not persisted.
    for character in after_obj.get("角色集", []):
        if character.get("名称") == "约翰娜":
            detail = character.get("详情", {})
            assert detail.get("首次章节") != "0099"


# --- Endpoint level (smoke) ----------------------------------------------

@pytest.fixture
def app_client(tmp_path):
    db_path = str(tmp_path / "gov.db")
    app = create_app(db_path, "sekret")
    client = TestClient(app)
    yield client, tmp_path, app
    app.state.workbench.shutdown()


def _create_project(client, path: Path) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    return client.post("/projects", json={"path": str(path)}, headers=AUTH).json()


def _wait(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met before timeout")


def test_governance_endpoint_queues_writer_task(app_client):
    client, tmp_path, app = app_client
    project = _seed_project(tmp_path / "project")
    pid = _create_project(client, project)["id"]
    from run_pipeline import audit_paths
    paths = audit_paths(project, 1, 1)
    _write_json(paths["correction"], _no_change_correction(1, 1))

    response = client.post(
        f"/projects/{pid}/tasks",
        json={"type": "governance", "params": {"start": 1, "end": 1}},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["task_type"] == "governance"
    assert task["status"] == "queued"

    _wait(lambda: app.state.db.get_task(task["id"])["status"] in {"done", "failed"})
    final = app.state.db.get_task(task["id"])
    assert final["status"] == "done", final.get("error")
    assert final["result"]["before"]
    assert final["result"]["after"]
    assert final["result"]["diff"]
