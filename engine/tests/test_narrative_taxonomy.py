#!/usr/bin/env python3
"""叙事词库与受控标签校验测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from story_schema_rules import NARRATIVE_TAXONOMY, validate_controlled_tags


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
DELTA_VALIDATOR = SCRIPT_DIR / "validate_delta.py"


def test_known_controlled_tags_are_accepted_for_their_collections():
    assert validate_controlled_tags("角色集", ["人物类型:导师", "热血"]) == []
    assert validate_controlled_tags(
        "事件集",
        ["剧情母题:追捕", "叙事功能:艰巨任务", "冲突对象:人与人"],
    ) == []
    assert validate_controlled_tags(
        "事件集",
        ["剧情线主题:主线推进", "爽点情绪点:打脸", "冲突悬念类型:身份与尊严", "画面类型:冲突对峙", "视觉用途:封面候选"],
    ) == []
    assert validate_controlled_tags("其他事项集", ["自由主题:世界规则"]) == []


def test_unknown_controlled_tag_is_rejected_but_unprefixed_tag_is_compatible():
    errors = validate_controlled_tags("事件集", ["叙事功能:瞬移", "热血"])
    assert errors == ["事件集.标签集 包含未知受控标签[叙事功能:瞬移]"]


def test_controlled_tag_prefix_is_rejected_outside_its_collection():
    errors = validate_controlled_tags("角色集", ["剧情母题:追捕"])
    assert errors == ["角色集.标签集 不允许使用受控标签前缀[剧情母题]"]


def test_taxonomy_has_the_requested_fixed_category_sizes():
    assert len(NARRATIVE_TAXONOMY["人物类型"]) == 45
    assert len(NARRATIVE_TAXONOMY["剧情母题"]) == 36
    assert len(NARRATIVE_TAXONOMY["叙事功能"]) == 31
    assert len(NARRATIVE_TAXONOMY["冲突对象"]) + len(NARRATIVE_TAXONOMY["冲突议题"]) == 14
    assert sum(len(NARRATIVE_TAXONOMY[key]) for key in ["悬念问题", "悬念风险", "悬念机制"]) == 12


def _empty_story():
    return {
        "介绍": {"标题": "测试", "描述": "测试结构"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }


def _event_delta(tags):
    empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    event = {
        "名称": "试炼", "发生地点": "", "参与成员": [], "重量级": 20,
        "目标事件": [], "分组": "0001-试炼", "时间": "0001-01-01T00:00:00",
        "别名": [], "标签集": tags, "介绍": "主角参与试炼。",
        "详情": {"涉及章节": "0001", "提取理由": "测试受控叙事标签"},
    }
    return {"章节": "第001章", "新增元素": {**empty, "事件集": [event]}, "修改元素": empty}


def _run_delta_validator(story_path, delta_path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(DELTA_VALIDATOR), str(story_path), str(delta_path)],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


def test_delta_accepts_optional_propp_tag_and_rejects_unknown_prefixed_tag(tmp_path):
    story = tmp_path / "story.json"
    delta = tmp_path / "delta.json"
    story.write_text(json.dumps(_empty_story(), ensure_ascii=False), encoding="utf-8")
    delta.write_text(json.dumps(_event_delta(["叙事功能:艰巨任务"]), ensure_ascii=False), encoding="utf-8")
    assert _run_delta_validator(story, delta).returncode == 0

    delta.write_text(json.dumps(_event_delta(["叙事功能:不存在功能"]), ensure_ascii=False), encoding="utf-8")
    result = _run_delta_validator(story, delta)
    assert result.returncode != 0
    assert "未知受控标签[叙事功能:不存在功能]" in result.stdout


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            if func.__code__.co_argcount == 1:
                with tempfile.TemporaryDirectory() as directory:
                    func(Path(directory))
            else:
                func()
            print(f"PASS {name}")
