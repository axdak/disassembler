import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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
        "output = Path(os.environ['ND_EXPECTED_OUTPUT'])\n"
        "content = '{}\\n' if output.suffix.lower() == '.json' else '# analysis\\n'\n"
        "output.write_text(content, encoding='utf-8')\n",
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
        assert (project / "章节处理" / "Delta" / f"第{i+1:03d}章_c{i}.json").is_file()


def test_stream_shell_command_cancel(tmp_path):
    import threading
    ev = threading.Event()
    ev.set()  # 预先取消，避免定时器线程跨测试泄漏
    run_pipeline.set_cancel_event(ev)
    try:
        cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        rc, _ = run_pipeline.stream_shell_command(cmd, tmp_path, dict(os.environ), 30.0)
        assert rc == 130
    finally:
        run_pipeline.set_cancel_event(threading.Event())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object regression")
def test_worker_descendants_die_when_controller_is_terminated(tmp_path):
    """Abrupt backend exit must not leave a provider able to write late output."""
    marker = tmp_path / "late-output.txt"
    ready = tmp_path / "child-ready.txt"
    child = tmp_path / "child.py"
    launcher = tmp_path / "launcher.py"
    controller = tmp_path / "controller.py"
    scripts_dir = Path(run_pipeline.__file__).resolve().parent

    child.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "ready, marker = map(Path, sys.argv[1:3])\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "time.sleep(1.0)\n"
        "marker.write_text('late', encoding='utf-8')\n",
        encoding="utf-8",
    )
    launcher.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, *sys.argv[1:]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    # Keep the controller source explicit so quoting remains stable on Windows.
    controller.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(scripts_dir)!r})\n"
        "import run_pipeline\n"
        "run_pipeline.stream_shell_command(sys.argv[1], Path(sys.argv[2]), dict(os.environ), 30)\n",
        encoding="utf-8",
    )
    command = subprocess.list2cmdline(
        [sys.executable, str(launcher), str(child), str(ready), str(marker)]
    )
    proc = subprocess.Popen([sys.executable, str(controller), command, str(tmp_path)])
    deadline = time.time() + 5
    while time.time() < deadline and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), "provider child did not start"

    proc.terminate()
    proc.wait(timeout=5)
    time.sleep(1.3)

    assert not marker.exists(), "provider descendant survived the controller process"
