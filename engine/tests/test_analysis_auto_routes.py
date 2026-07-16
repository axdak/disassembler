import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ROOT))

from run_pipeline import cmd_chapter_structure_auto, cmd_visual_assets_auto  # noqa: E402


SRC_DIR = "\u539f\u6587\u62c6\u89e3"
ANALYSIS_DIR = "\u7ae0\u8282\u5904\u7406"
INDEX_FILE = "_\u7d22\u5f15.json"
CHAPTER_FILE = "\u7b2c001\u7ae0_test.md"


def write_project(project: Path) -> None:
    (project / SRC_DIR).mkdir(parents=True)
    (project / ANALYSIS_DIR).mkdir(parents=True)
    (project / SRC_DIR / INDEX_FILE).write_text(
        json.dumps(
            {
                "chapters": [
                    {"seq": 1, "filename": CHAPTER_FILE, "title": "test"}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (project / SRC_DIR / CHAPTER_FILE).write_text("# chapter 001\nsource text.\n", encoding="utf-8")
    (project / ANALYSIS_DIR / CHAPTER_FILE).write_text("# analysis\nready.\n", encoding="utf-8")


def write_multi_output_worker(path: Path) -> None:
    path.write_text(
        """
import json
import os
from pathlib import Path

task_type = os.environ["ND_TASK_TYPE"]
print(f"VISIBLE_ANALYSIS_WORKER {task_type}")
outputs = json.loads(os.environ["ND_EXPECTED_OUTPUTS"])
for item in outputs:
    target = Path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# generated {task_type}\\n{target.name}\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )


def auto_args(project: Path, worker: Path, run_mode: str = "worker") -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project),
        include_original="sample",
        max_pack_chars=70000,
        max_original_chars=6000,
        max_analysis_chars=12000,
        force_aggregate=False,
        run_mode=run_mode,
        worker_provider="manual",
        worker_command=f'"{sys.executable}" "{worker}"',
        worker_timeout_seconds=30,
        worker_retries=0,
        worker_window="hidden",
        worker_loop="continuous",
    )


def test_visual_assets_auto_worker_route_runs_per_chapter_and_aggregate(tmp_path):
    project = tmp_path / "project"
    write_project(project)
    worker = tmp_path / "multi_output_worker.py"
    write_multi_output_worker(worker)

    rc = cmd_visual_assets_auto(auto_args(project, worker))

    assert rc == 0
    assert (project / "\u5168\u4e66\u5206\u6790/\u89c6\u89c9\u8d44\u4ea7/\u5206\u7ae0/ch001/\u89c6\u89c9\u8d44\u4ea7\u6e05\u5355.md").is_file()
    assert (project / "\u5168\u4e66\u5206\u6790/\u89c6\u89c9\u8d44\u4ea7/\u89c6\u89c9\u8d44\u4ea7\u6e05\u5355.md").is_file()


def test_chapter_structure_auto_worker_route_runs_to_completion(tmp_path):
    project = tmp_path / "project"
    write_project(project)
    worker = tmp_path / "multi_output_worker.py"
    write_multi_output_worker(worker)

    rc = cmd_chapter_structure_auto(auto_args(project, worker))

    assert rc == 0
    assert (project / "\u5168\u4e66\u5206\u6790/\u6545\u4e8b\u7ed3\u6784/\u5206\u7ae0/ch001/\u7ae0\u8282\u7ed3\u6784.md").is_file()


def test_visual_assets_auto_serial_route_hands_off_without_calling_provider(tmp_path):
    project = tmp_path / "project"
    write_project(project)
    worker = tmp_path / "must_not_run.py"
    worker.write_text("raise SystemExit('worker should not be called')\n", encoding="utf-8")
    args = auto_args(project, worker, run_mode="serial")

    rc = cmd_visual_assets_auto(args)

    assert rc == 2
    assert not (project / "\u5168\u4e66\u5206\u6790/\u89c6\u89c9\u8d44\u4ea7/\u5206\u7ae0/ch001/\u89c6\u89c9\u8d44\u4ea7\u6e05\u5355.md").exists()
