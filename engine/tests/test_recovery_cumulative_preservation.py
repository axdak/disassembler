"""连续修复追加宿主档案时按记录保全，不按整个JSON字符串的括号位置误判。"""
import json
import pytest
from fault_recovery import preservation_errors


OLD = "这条历史记录必须完整保留，不能在后续局部修复中删减、改写或被其他记录替代。"
NEW = "这是下一次修复新增的记录，与前一条共同留存。"


def check(tmp_path, field, old, new):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    for path, value in ((before, old), (after, new)):
        path.write_text(json.dumps({"角色集": [{"名称": "角色", "详情": {field: value}}]}, ensure_ascii=False), encoding="utf-8")
    return preservation_errors(before, after)


@pytest.mark.parametrize("field", ["治理纠错记录", "治理档案记录", "字段修复原文", "人物轨迹"])
def test_appending_serialized_records_preserves_complete_old_entries(tmp_path, field):
    old = json.dumps([OLD], ensure_ascii=False, indent=2)
    new = json.dumps([OLD, NEW], ensure_ascii=False, separators=(",", ":"))
    assert check(tmp_path, field, old, new) == []


@pytest.mark.parametrize("field", ["治理纠错记录", "字段修复原文", "人物轨迹"])
@pytest.mark.parametrize("new_entries", [[NEW], [OLD[:-8], NEW]])
def test_record_append_never_authorizes_deleting_or_rewriting_old_entries(tmp_path, field, new_entries):
    assert check(tmp_path, field, json.dumps([OLD], ensure_ascii=False), json.dumps(new_entries, ensure_ascii=False))


def test_plain_story_prose_is_not_interpreted_as_an_archive_protocol(tmp_path):
    assert check(tmp_path, "原文引文", json.dumps([OLD], ensure_ascii=False), json.dumps([OLD, NEW], ensure_ascii=False))


def test_appending_records_preserves_existing_relative_order(tmp_path):
    old = json.dumps([OLD, NEW], ensure_ascii=False)
    reordered = json.dumps([NEW, OLD], ensure_ascii=False)
    assert check(tmp_path, "人物轨迹", old, reordered)
