#!/usr/bin/env python3
"""run_pipeline 恢复重放行为测试。"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ROOT))

import run_pipeline
from run_pipeline import audit_paths, recover_story_from_snapshot


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def character(name, seq=1):
    chapter = f"第{seq:03d}章"
    return {
        "名称": name,
        "是否主角": False,
        "性别": 2,
        "年龄": 0,
        "生日": "0001-01-01T00:00:00",
        "所属阵营": [],
        "关系": [],
        "分组": "测试",
        "别名": [],
        "标签集": ["测试"],
        "介绍": f"{name}出现。",
        "详情": {"提取理由": "恢复重放测试角色", "首次章节": f"{seq:04d}", "最近章节": f"{seq:04d}"},
    }


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": "恢复重放测试"},
        "角色集": [character("甲")],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def delta(seq, name):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {
        "章节": f"第{seq:03d}章",
        "新增元素": {**empty, "角色集": [character(name, seq)]},
        "修改元素": empty,
    }


def test_recover_from_snapshot_replays_later_deltas():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        for rel in ["原文拆解", "章节处理", "故事结构版本", "结构变更日志"]:
            (project / rel).mkdir(parents=True, exist_ok=True)
        for seq in [1, 2, 3]:
            (project / "原文拆解" / f"第{seq:03d}章_测试.md").write_text(f"# 第{seq:03d}章\n", encoding="utf-8")

        write_json(project / "故事结构版本" / "story_after_ch001.json", empty_story())
        (project / "故事结构_增量.json").write_text('{"损坏": [', encoding="utf-8")
        write_json(project / "章节处理" / "第002章_测试.json", delta(2, "乙"))
        write_json(project / "章节处理" / "第003章_测试.json", delta(3, "丙"))

        rc = recover_story_from_snapshot(project, snapshot_seq=1, to_seq=3)
        assert rc == 0

        story = json.loads((project / "故事结构_增量.json").read_text(encoding="utf-8"))
        names = [item["名称"] for item in story["角色集"]]
        assert names == ["甲", "乙", "丙"], names
        assert (project / "故事结构版本" / "story_after_ch002.json").is_file()
        assert (project / "故事结构版本" / "story_after_ch003.json").is_file()

    print("[OK] 可从快照恢复并重放后续Delta")


def test_recover_replays_committed_periodic_governance_at_restored_boundary():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        version_dir = project / "故事结构版本"
        version_dir.mkdir(parents=True, exist_ok=True)
        write_json(version_dir / "story_after_ch005.json", {"order": []})

        paths = audit_paths(project, 1, 5)
        write_json(paths["correction"], {"governance": True})
        write_json(paths["status"], {
            "status": "committed",
            "range": "001-005",
            "correction_path": str(paths["correction"]),
        })

        calls = []

        def fake_commit_governance(project_dir, patch_path):
            calls.append(("audit", Path(patch_path).name))
            story_path = Path(project_dir) / "故事结构_增量.json"
            story = json.loads(story_path.read_text(encoding="utf-8"))
            story["order"].append("audit_001-005")
            write_json(story_path, story)
            return 0

        def fake_replay_one_delta(project_dir, seq):
            calls.append(("delta", seq))
            story_path = Path(project_dir) / "故事结构_增量.json"
            story = json.loads(story_path.read_text(encoding="utf-8"))
            story["order"].append(f"ch{seq:03d}")
            write_json(story_path, story)
            return 0

        with patch.object(run_pipeline, "commit_governance", side_effect=fake_commit_governance), patch.object(
            run_pipeline, "replay_one_delta", side_effect=fake_replay_one_delta
        ):
            rc = recover_story_from_snapshot(project, snapshot_seq=5, to_seq=6)

        assert rc == 0
        assert calls == [("audit", "correction_001-005.json"), ("delta", 6)]
        story = json.loads((project / "故事结构_增量.json").read_text(encoding="utf-8"))
        assert story["order"] == ["audit_001-005", "ch006"]


if __name__ == "__main__":
    test_recover_from_snapshot_replays_later_deltas()
    print("\n全部测试通过 [PASS]")
