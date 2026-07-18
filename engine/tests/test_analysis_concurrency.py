import json
import sys
from pathlib import Path

import run_pipeline
from run_pipeline import cmd_run_analysis


def _write_project(project: Path, n: int = 3):
    (project / "原文拆解").mkdir(parents=True)
    (project / "章节处理").mkdir(parents=True)
    chapters = [{"seq": i + 1, "filename": f"第{i+1:03d}章_c{i}.md", "title": f"c{i}"} for i in range(n)]
    (project / "原文拆解" / "_索引.json").write_text(
        json.dumps({"chapters": chapters}, ensure_ascii=False), encoding="utf-8"
    )
    for ch in chapters:
        (project / "原文拆解" / ch["filename"]).write_text(f"# {ch['title']}\n", encoding="utf-8")


def _write_worker(path: Path):
    path.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['ND_EXPECTED_OUTPUT']).write_text('# analysis\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def test_cmd_run_analysis_parallel(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write_project(project, 3)
    worker = tmp_path / "wk.py"
    _write_worker(worker)
    cmd = f'"{sys.executable}" "{worker}"'
    monkeypatch.setattr(run_pipeline, "validate_chapter_analysis_file", lambda p: (True, []))
    rc = cmd_run_analysis(project, chapter_command=cmd, worker_retries=0,
                          worker_timeout_seconds=30, worker_loop="continuous", concurrency=2)
    assert rc == 0
    for i in range(3):
        assert (project / "章节处理" / f"第{i+1:03d}章_c{i}.md").is_file()


def test_cmd_run_analysis_serial_unchanged(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write_project(project, 2)
    worker = tmp_path / "wk.py"
    _write_worker(worker)
    cmd = f'"{sys.executable}" "{worker}"'
    monkeypatch.setattr(run_pipeline, "validate_chapter_analysis_file", lambda p: (True, []))
    rc = cmd_run_analysis(project, chapter_command=cmd, worker_retries=0,
                          worker_timeout_seconds=30, worker_loop="continuous", concurrency=1)
    assert rc == 0


def test_cmd_run_interleaved(tmp_path, monkeypatch):
    from run_pipeline import cmd_run_interleaved
    project = tmp_path / "proj"
    _write_project(project, 3)
    worker = tmp_path / "wk.py"
    _write_worker(worker)
    cmd = f'"{sys.executable}" "{worker}"'
    monkeypatch.setattr(run_pipeline, "validate_chapter_analysis_file", lambda p: (True, []))
    monkeypatch.setattr(run_pipeline, "commit_chapter", lambda project_dir, seq: 0)
    rc = cmd_run_interleaved(project, run_mode="worker", worker_provider="manual",
                             chapter_command=cmd, audit_command=cmd, worker_retries=0,
                             worker_timeout_seconds=30, concurrency=2, audit_interval=0)
    assert rc == 0
    for i in range(3):
        assert (project / "章节处理" / f"第{i+1:03d}章_c{i}.md").is_file()
        assert (project / "章节处理" / f"第{i+1:03d}章_c{i}.json").is_file()


def test_stream_shell_command_cancel(tmp_path):
    import os
    import threading
    import run_pipeline
    ev = threading.Event()
    ev.set()  # 预先取消，避免定时器线程跨测试泄漏
    run_pipeline.set_cancel_event(ev)
    try:
        cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        rc, _ = run_pipeline.stream_shell_command(cmd, tmp_path, dict(os.environ), 30.0)
        assert rc == 130
    finally:
        run_pipeline.set_cancel_event(threading.Event())
