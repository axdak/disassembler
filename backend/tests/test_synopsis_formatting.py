"""手动简介排版沿用任务队列，仅生成文本副本，不改故事及交付回执。"""
import json
import threading
import hashlib
import time
from pathlib import Path

import pytest

from models import TaskCreate
from task_adapters import FormatSynopsisAdapter
from workbench import TaskContext, WRITER_TASK_TYPES


def context(project):
    """使用真实任务上下文，模型调用由全局离线夹具阻止。"""
    return TaskContext(task_id=1, project_id=1, project_path=project, params={},
                       cancellation_event=threading.Event(), _progress_callback=lambda *args: None)


def test_manual_layout_creates_preview_and_leaves_sources_unchanged(tmp_path):
    source = tmp_path / "故事结构_增量.json"
    source.write_text(json.dumps({"介绍": {"标题": "旧故事", "描述": "【主线】甲找乙。\n【当前进展】尚未找到。"}},
                                 ensure_ascii=False), encoding="utf-8")
    before = source.read_bytes()
    result = FormatSynopsisAdapter().run(context(tmp_path))
    artifact = Path(result["artifacts"][0])
    assert artifact.read_text(encoding="utf-8") == "【主线】\n甲找乙。\n\n【当前进展】\n尚未找到。\n"
    assert result["task"] == "format_synopsis"
    assert source.read_bytes() == before
    assert not (tmp_path / "故事结构.json").exists()
    assert TaskCreate(type="format_synopsis", params={}).type == "format_synopsis"
    assert "format_synopsis" in WRITER_TASK_TYPES


@pytest.mark.parametrize("description", ["", "  ", None, 42])
def test_manual_layout_rejects_missing_or_invalid_content(tmp_path, description):
    source = tmp_path / "故事结构_增量.json"
    source.write_text(json.dumps({"介绍": {"描述": description}}, ensure_ascii=False), encoding="utf-8")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="简介"):
        FormatSynopsisAdapter().run(context(tmp_path))
    assert source.read_bytes() == before
    assert not list(tmp_path.rglob("*.txt"))


def test_manual_layout_prefers_receipt_bound_final_without_modifying_it(tmp_path):
    """终稿存在本身不够；只有三份版本摘要一致才作为权威简介。"""
    files = {}
    for key, name, text in (
        ("process_sha256", "故事结构_增量.json", "旧版简介。"),
        ("draft_sha256", "故事结构_草稿.json", "【主线】已纠正的简介。"),
        ("final_sha256", "故事结构.json", "【主线】已纠正的简介。"),
    ):
        path = tmp_path / name
        path.write_text(json.dumps({"介绍": {"描述": text}}, ensure_ascii=False), encoding="utf-8")
        files[path] = path.read_bytes()
    receipt = tmp_path / "质量治理/最终审计/finalize_log.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({key: hashlib.sha256(raw).hexdigest()
                                  for key, raw in zip(("process_sha256", "draft_sha256", "final_sha256"), files.values())}), encoding="utf-8")
    original_receipt = receipt.read_bytes()
    result = FormatSynopsisAdapter().run(context(tmp_path))
    assert Path(result["artifacts"][0]).read_text(encoding="utf-8") == "【主线】\n已纠正的简介。\n"
    assert all(path.read_bytes() == raw for path, raw in files.items())
    assert receipt.read_bytes() == original_receipt


def test_manual_button_task_uses_default_registered_adapter(tmp_path):
    """实际 API 入队并完成排版，防止按钮只注册类型却没有执行入口。"""
    from app import create_app
    from fastapi.testclient import TestClient
    project = tmp_path / "project"
    project.mkdir()
    source = project / "故事结构_增量.json"
    source.write_text('{"介绍":{"描述":"【主线】原有内容。"}}', encoding="utf-8")
    before = source.read_bytes()
    app = create_app(str(tmp_path / "format.db"), "format-token")
    client = TestClient(app)
    auth = {"Authorization": "Bearer format-token"}
    try:
        pid = client.post("/projects", headers=auth, json={"path": str(project)}).json()["id"]
        response = client.post(f"/projects/{pid}/tasks", headers=auth,
                               json={"type": "format_synopsis", "params": {}})
        assert response.status_code == 200
        task_id = response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = client.get(f"/projects/{pid}/tasks/{task_id}", headers=auth).json()
            if task["status"] in {"done", "failed"}:
                break
            time.sleep(0.01)
        assert task["status"] == "done", task
        artifact = Path(task["result"]["artifacts"][0])
        assert artifact.read_text(encoding="utf-8") == "【主线】\n原有内容。\n"
        assert source.read_bytes() == before
    finally:
        app.state.workbench.shutdown()
        client.close()
