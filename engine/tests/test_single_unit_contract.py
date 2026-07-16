#!/usr/bin/env python3
"""逐切片产物契约测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ROOT))

from run_pipeline import find_multi_unit_artifacts

VALIDATOR = ROOT / "validate_delta.py"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": ""},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def empty_delta(chapter):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {"章节": chapter, "新增元素": empty, "修改元素": empty}


def run(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env)


def test_validate_delta_rejects_chapter_range():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, empty_story())
        write_json(delta, empty_delta("第003-005章 七玄门-墨大夫"))

        result = run([sys.executable, str(VALIDATOR), str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "单个Delta只能对应一个切片" in result.stdout, result.stdout

    print("[OK] validate_delta 拒绝范围章节Delta")


def test_find_multi_unit_artifacts_detects_range_files():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        chapter_dir = project / "章节处理"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "第003-005章_七玄门-墨大夫.md").write_text("# 合并分析\n", encoding="utf-8")
        (chapter_dir / "第003-005章_七玄门-墨大夫.json").write_text("{}\n", encoding="utf-8")

        bad = find_multi_unit_artifacts(project)
        names = [p.name for p in bad]
        assert "第003-005章_七玄门-墨大夫.md" in names
        assert "第003-005章_七玄门-墨大夫.json" in names

    print("[OK] 主控器可发现多切片合并产物")


if __name__ == "__main__":
    test_validate_delta_rejects_chapter_range()
    test_find_multi_unit_artifacts_detects_range_files()
    print("\n全部测试通过 [PASS]")
