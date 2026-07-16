#!/usr/bin/env python3
"""validate_structure.py 模式语义测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
VALIDATOR = ROOT / "validate_structure.py"


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


def empty_delta():
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {"章节": "第001章", "新增元素": empty, "修改元素": empty}


def story_with_event_location(location):
    return {
        "介绍": {"标题": "测试故事", "描述": ""},
        "角色集": [
            {
                "名称": "张三",
                "是否主角": True,
                "性别": 0,
                "年龄": 20,
                "生日": "0001-01-01T00:00:00",
                "所属阵营": [],
                "关系": [],
                "分组": "主角团",
                "别名": [],
                "标签集": [],
                "介绍": "主角。",
                "详情": {"首次章节": "0001", "最近章节": "0001"},
            }
        ],
        "事件集": [
            {
                "名称": "山门冲突",
                "发生地点": location,
                "参与成员": ["张三"],
                "重量级": 20,
                "目标事件": [],
                "分组": "00000010-山门主线冲突遭遇紧张悬念",
                "时间": "0001-01-01T00:00:00",
                "别名": [],
                "标签集": [],
                "介绍": "张三在山门附近遇到冲突。",
                "详情": {"涉及章节": "0001"},
            }
        ],
        "地点集": [
            {
                "名称": "青云山",
                "父级地点": "",
                "分组": "地理",
                "别名": [],
                "标签集": [],
                "介绍": "宗门所在。",
                "详情": {"首次章节": "0001", "最近章节": "0001"},
            }
        ],
        "线索集": [],
        "阵营集": [],
        "物品集": [],
        "其他事项集": [],
    }


def run(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_chapter_check_reports_full_process_store_check():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, empty_story())
        write_json(delta, empty_delta())

        result = run([sys.executable, str(VALIDATOR), "--chapter-check", str(story), str(delta)])
        assert result.returncode == 0, result.stdout
        assert "合并后全量过程库校验" in result.stdout, result.stdout
        assert "不是仅检查本章触碰元素" in result.stdout, result.stdout

    print("[OK] chapter-check 报告明确为全量过程库校验")


def test_governance_structure_allows_unregistered_event_location_as_warning():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        write_json(story, story_with_event_location("青云山山门口"))

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story)])

        assert result.returncode == 0, result.stdout
        assert "青云山山门口" in result.stdout, result.stdout
        assert "警告" in result.stdout, result.stdout


def test_final_structure_still_rejects_unregistered_event_location():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        write_json(story, story_with_event_location("青云山山门口"))

        result = run([sys.executable, str(VALIDATOR), "--mode", "final", str(story)])

        assert result.returncode != 0, result.stdout
        assert "青云山山门口" in result.stdout, result.stdout


def test_governance_structure_accepts_registered_child_location():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        data = story_with_event_location("青云山山门口")
        data["地点集"].append({
            "名称": "青云山山门口",
            "父级地点": "青云山",
            "分组": "地理/入口",
            "别名": [],
            "标签集": [],
            "介绍": "青云山入口。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        })
        write_json(story, data)

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story)])

        assert result.returncode == 0, result.stdout
        assert "引用了不存在" not in result.stdout, result.stdout


if __name__ == "__main__":
    test_chapter_check_reports_full_process_store_check()
    test_governance_structure_allows_unregistered_event_location_as_warning()
    test_final_structure_still_rejects_unregistered_event_location()
    test_governance_structure_accepts_registered_child_location()
    print("\n全部测试通过 [PASS]")
