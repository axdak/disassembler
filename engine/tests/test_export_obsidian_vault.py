#!/usr/bin/env python3
"""Export story structure JSON as an Obsidian-friendly Markdown vault."""

from __future__ import annotations

import json
from pathlib import Path

from export_obsidian_vault import export_obsidian_markdown


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sample_story() -> dict:
    return {
        "介绍": {"标题": "火星", "描述": "火星危机中的家族与战争故事。"},
        "角色集": [{
            "名称": "约翰娜·奥尔森多",
            "是否主角": False,
            "性别": 1,
            "年龄": 0,
            "生日": "0001-01-01T00:00:00",
            "所属阵营": ["奥尔森多家族"],
            "关系": ["弟弟:杰弗里·奥尔森多"],
            "分组": "奥尔森多家族",
            "别名": ["约翰娜"],
            "标签集": ["温柔", "有责任感"],
            "介绍": "奥尔森多家族成员，担忧家人卷入火星危机。",
            "详情": {
                "首次章节": "0001",
                "最近章节": "0002",
                "人物轨迹": "[\"第001章：劝阻弟弟前往危险地区\",\"第002章：继续关注战争风险\"]",
            },
        }, {
            "名称": "杰弗里·奥尔森多",
            "是否主角": False,
            "性别": 0,
            "年龄": 0,
            "生日": "0001-01-01T00:00:00",
            "所属阵营": ["奥尔森多家族"],
            "关系": ["姐姐:约翰娜·奥尔森多"],
            "分组": "奥尔森多家族",
            "别名": [],
            "标签集": ["好奇心强"],
            "介绍": "奥尔森多家族成员，对火星行动保持好奇。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        }],
        "事件集": [{
            "名称": "兄弟理念对立与战争危机铺垫",
            "发生地点": "火星长城",
            "参与成员": ["约翰娜·奥尔森多", "杰弗里·奥尔森多"],
            "重量级": 70,
            "目标事件": [],
            "分组": "00000010-兄弟理念对立与战争危机铺垫",
            "时间": "0001-01-01T00:00:00",
            "别名": [],
            "标签集": ["家族理念+冲突铺垫+担忧+战争危机"],
            "介绍": "姐弟围绕是否卷入战争危机产生理念分歧。",
            "详情": {
                "涉及章节": "0001",
                "关联线索": ["线索:火星危机"],
                "动作链条": "[\"争论\",\"劝阻\",\"危机铺垫\"]",
            },
        }],
        "地点集": [{
            "名称": "火星长城",
            "父级地点": "",
            "分组": "火星",
            "别名": [],
            "标签集": ["军事边境"],
            "介绍": "火星上重要的战争防线。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        }],
        "线索集": [{
            "名称": "火星危机",
            "涉及事件": ["兄弟理念对立与战争危机铺垫"],
            "分组": "战争线",
            "别名": [],
            "标签集": ["战争危机"],
            "介绍": "围绕火星战争风险持续推进的线索。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        }],
        "阵营集": [{
            "名称": "奥尔森多家族",
            "父级阵营": "",
            "座落地点": "火星长城",
            "分组": "家族",
            "别名": [],
            "标签集": ["家族势力"],
            "介绍": "卷入火星危机的家族。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        }],
        "物品集": [],
        "其他事项集": [],
    }


def test_export_obsidian_vault_and_single_markdown(tmp_path: Path) -> None:
    story_path = tmp_path / "故事结构.json"
    out_dir = tmp_path / "obsidian-vault"
    big_md = tmp_path / "故事结构总览.md"
    write_json(story_path, sample_story())

    report = export_obsidian_markdown(
        story_path,
        out_dir,
        single_file=big_md,
        clean=True,
        include_dataview=True,
        include_canvas=True,
        filename_mode="name",
    )

    home_md = out_dir / "00-总览" / "故事总览.md"
    timeline_md = out_dir / "00-总览" / "时间线.md"
    character_index_md = out_dir / "00-总览" / "角色索引.md"
    faction_index_md = out_dir / "00-总览" / "阵营索引.md"
    dataview_md = out_dir / "_dataview" / "查询-按阵营角色.md"
    canvas_path = out_dir / "00-总览" / "故事图谱.canvas"
    character_md = out_dir / "角色" / "约翰娜·奥尔森多.md"
    event_md = out_dir / "事件" / "兄弟理念对立与战争危机铺垫.md"
    mapping_path = out_dir / "_meta" / "story_markdown_map.json"
    graph_path = out_dir / "_meta" / "graph_edges.json"

    assert report["concept_count"] == 6
    assert home_md.is_file()
    assert timeline_md.is_file()
    assert character_index_md.is_file()
    assert faction_index_md.is_file()
    assert dataview_md.is_file()
    assert canvas_path.is_file()
    assert character_md.is_file()
    assert event_md.is_file()
    assert mapping_path.is_file()
    assert graph_path.is_file()
    assert big_md.is_file()

    home_text = home_md.read_text(encoding="utf-8")
    assert "[[时间线]]" in home_text
    assert "[[角色索引]]" in home_text
    assert "[[查询-按阵营角色]]" in home_text

    dataview_text = dataview_md.read_text(encoding="utf-8")
    assert "TABLE group, first_chapter, recent_chapter" in dataview_text
    assert 'WHERE type = "角色"' in dataview_text

    character_text = character_md.read_text(encoding="utf-8")
    assert "type: 角色" in character_text
    assert "- [[奥尔森多家族]]" in character_text
    assert "- [[杰弗里·奥尔森多]]" in character_text
    assert "第001章：劝阻弟弟前往危险地区" in character_text

    event_text = event_md.read_text(encoding="utf-8")
    assert "- [[火星长城]]" in event_text
    assert "- [[约翰娜·奥尔森多]]" in event_text
    assert "- [[火星危机]]" in event_text

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    character_entry = next(item for item in mapping["concepts"] if item["name"] == "约翰娜·奥尔森多")
    assert character_entry["markdown_path"] == "角色/约翰娜·奥尔森多.md"
    assert character_entry["obsidian_link"] == "[[约翰娜·奥尔森多]]"
    assert character_entry["json_pointer"] == "/角色集/0"
    assert any(edge["target_name"] == "奥尔森多家族" for edge in character_entry["links_out"])
    assert mapping["vault_layout"] == "obsidian_vault"
    assert mapping["dataview_enabled"] is True
    assert mapping["canvas_enabled"] is True

    big_text = big_md.read_text(encoding="utf-8")
    assert "# 火星" in big_text
    assert "## 角色集" in big_text
    assert "### [[约翰娜·奥尔森多]]" in big_text
    assert "### [[兄弟理念对立与战争危机铺垫]]" in big_text
