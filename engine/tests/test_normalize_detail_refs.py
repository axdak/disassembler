from __future__ import annotations

import json
from pathlib import Path
import sys


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from normalize_story_schema import normalize_structure  # noqa: E402
from story_schema_rules import make_empty_structure  # noqa: E402


def test_quarantine_invalid_detail_ref_array_items_preserves_valid_refs():
    story = make_empty_structure()
    story["角色集"].append({"名称": "甲"})
    story["事件集"].append(
        {
            "名称": "事件A",
            "详情": {
                "关联角色": ["角色:甲", "角色:乙"],
                "关联事件": ["事件:不存在事件"],
            },
        }
    )

    normalized, logs = normalize_structure(story, quarantine_invalid_refs=True)

    detail = normalized["事件集"][0]["详情"]
    assert detail["关联角色"] == ["角色:甲"]
    assert detail["关联事件"] == []
    pending = json.loads(detail["待确认引用"])
    assert "角色:乙" in pending
    assert "事件:不存在事件" in pending
    assert any("详情.关联角色" in log and "已移入详情.待确认引用" in log for log in logs)


def test_quarantine_invalid_detail_ref_does_not_apply_to_scalar_string():
    story = make_empty_structure()
    story["角色集"].append({"名称": "甲"})
    story["地点集"].append(
        {
            "名称": "地点A",
            "详情": {
                "族长": "角色:乙",
                "见证者": "角色:甲",
            },
        }
    )

    normalized, _logs = normalize_structure(story, quarantine_invalid_refs=True)

    detail = normalized["地点集"][0]["详情"]
    assert detail["见证者"] == "角色:甲"
    assert detail["族长"] == "角色:乙"
    assert "待确认引用" not in detail
