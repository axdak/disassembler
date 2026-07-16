#!/usr/bin/env python3
"""“其他事项集”必须保持可选，并贯通校验、规范化、合并和审计。"""

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, HERE)

from analysis_context_pack import summarize_story_structure
from diff_structure import diff
from merge_delta import merge_delta
from normalize_story_schema import normalize_structure
from run_pipeline import story_summary
from story_schema_rules import make_empty_structure, validate_exact_story_schema
from validate_delta import validate_delta


OTHER_KEY = "其他事项集"


def other_item(*, with_evidence: bool = False):
    detail = {"来源": "原文说明"}
    if with_evidence:
        detail["提取理由"] = "本章出现的有效补充信息，应归入其他事项集"
    return {
        "名称": "创作背景注记",
        "分组": "补充资料",
        "别名": [],
        "标签集": ["背景"],
        "介绍": "记录作品外的可追溯补充信息。",
        "详情": detail,
    }


def delta_collections(item):
    collections = {key: [] for key in make_empty_structure() if key != "介绍"}
    collections[OTHER_KEY] = [item]
    return collections


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def test_legacy_story_may_omit_other_items():
    story = make_empty_structure()
    story.pop(OTHER_KEY)
    errors, _warnings = validate_exact_story_schema(story, mode="process")
    assert not errors, errors
    print("[OK] 历史故事结构可省略其他事项集")


def test_other_item_is_valid_without_chapter_trace_fields():
    story = make_empty_structure()
    story[OTHER_KEY] = [other_item()]
    errors, _warnings = validate_exact_story_schema(story, mode="process")
    assert not errors, errors
    print("[OK] 自由主题仅使用公共字段即可通过结构校验")


def test_normalizer_preserves_other_item():
    story = make_empty_structure()
    story[OTHER_KEY] = [{"名称": "仅有名称的自由主题"}]
    normalized, _logs = normalize_structure(story)
    item = normalized[OTHER_KEY][0]
    assert item["名称"] == "仅有名称的自由主题"
    assert item["标签集"] == []
    assert "首次章节" not in item["详情"]
    print("[OK] 规范化保留其他事项且不伪造章节追溯")


def test_legacy_delta_may_omit_other_items():
    with tempfile.TemporaryDirectory() as directory:
        current_path = os.path.join(directory, "current.json")
        delta_path = os.path.join(directory, "legacy-delta.json")
        write_json(current_path, make_empty_structure())
        legacy_collections = {
            key: []
            for key in make_empty_structure()
            if key not in {"介绍", OTHER_KEY}
        }
        write_json(delta_path, {
            "章节": "第001章",
            "新增元素": legacy_collections,
            "修改元素": legacy_collections,
        })
        assert validate_delta(current_path, delta_path, mode="process")
    print("[OK] 历史 Delta 可省略其他事项集")


def test_delta_merge_and_context_include_other_items():
    with tempfile.TemporaryDirectory() as directory:
        incremental_path = os.path.join(directory, "故事结构_增量.json")
        delta_path = os.path.join(directory, "chapter.json")
        current_path = os.path.join(directory, "current.json")

        base = make_empty_structure()
        write_json(incremental_path, base)
        write_json(current_path, base)
        evidence_item = other_item(with_evidence=True)
        delta = {
            "章节": "第001章",
            "新增元素": delta_collections(evidence_item),
            "修改元素": {key: [] for key in delta_collections(evidence_item)},
        }
        write_json(delta_path, delta)

        assert validate_delta(current_path, delta_path, mode="process")
        stats = merge_delta(incremental_path, delta_path)
        with open(incremental_path, "r", encoding="utf-8") as f:
            merged = json.load(f)

        assert stats[OTHER_KEY]["新增"] == 1
        assert merged[OTHER_KEY][0]["名称"] == evidence_item["名称"]
        assert "提取理由" not in merged[OTHER_KEY][0]["详情"]
        assert evidence_item["名称"] in story_summary(Path(incremental_path))
        assert evidence_item["名称"] in summarize_story_structure(merged)

        changed = make_empty_structure()
        changed[OTHER_KEY] = [merged[OTHER_KEY][0]]
        assert evidence_item["名称"] in diff(make_empty_structure(), changed)["元素集"][OTHER_KEY]["新增"]
    print("[OK] Delta、合并、上下文和变更日志均保留其他事项")


if __name__ == "__main__":
    test_legacy_story_may_omit_other_items()
    test_other_item_is_valid_without_chapter_trace_fields()
    test_normalizer_preserves_other_item()
    test_legacy_delta_may_omit_other_items()
    test_delta_merge_and_context_include_other_items()
    print("\n全部测试通过 [PASS]")
