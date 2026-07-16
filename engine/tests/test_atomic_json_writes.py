#!/usr/bin/env python3
"""Atomic JSON write regressions for story-structure mutators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import apply_governance_ops
import merge_delta
import normalize_story_schema


ORIGINAL = {"介绍": {"标题": "旧", "描述": "原始内容"}, "角色集": []}
UPDATED = {"介绍": {"标题": "新", "描述": "更新内容"}, "角色集": [{"名称": "甲"}]}


def _assert_preserves_existing_file_when_dump_fails(tmp_path: Path, module, save_func) -> None:
    target = tmp_path / "故事结构_增量.json"
    target.write_text(json.dumps(ORIGINAL, ensure_ascii=False, indent=2), encoding="utf-8")

    real_dump = module.json.dump

    def failing_dump(data, handle, *args, **kwargs):
        handle.write("{\"partial\": ")
        raise OSError("simulated write failure")

    module.json.dump = failing_dump
    try:
        with pytest.raises(OSError, match="simulated write failure"):
            save_func(target, UPDATED)
    finally:
        module.json.dump = real_dump

    assert json.loads(target.read_text(encoding="utf-8")) == ORIGINAL


def test_normalize_save_json_preserves_existing_file_when_write_fails(tmp_path):
    _assert_preserves_existing_file_when_dump_fails(
        tmp_path,
        normalize_story_schema,
        lambda path, data: normalize_story_schema.save_json(str(path), data),
    )


def test_merge_delta_save_json_preserves_existing_file_when_write_fails(tmp_path):
    _assert_preserves_existing_file_when_dump_fails(tmp_path, merge_delta, merge_delta.save_json)


def test_governance_save_preserves_existing_file_when_write_fails(tmp_path):
    _assert_preserves_existing_file_when_dump_fails(tmp_path, apply_governance_ops, apply_governance_ops.save)
