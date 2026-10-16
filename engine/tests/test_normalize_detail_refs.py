from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from coerce_delta import coerce_delta_in_place  # noqa: E402
from normalize_story_schema import normalize_detail_refs, normalize_structure  # noqa: E402
from story_schema_rules import COLLECTION_KEYS, make_empty_structure, validate_detail_schema  # noqa: E402


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


def normalized_reference_detail(value, field="关联事件", *, route="structure", quarantine=False):
    """Exercise actual normalization entry points with the same explicit field."""
    detail = {field: copy.deepcopy(value)}
    names = {key: set() for key in COLLECTION_KEYS}
    names["事件集"] = {"晚归燕子相依", "甲、乙/丙"}
    names["角色集"] = {"甲"}
    if route == "detail":
        normalize_detail_refs(detail, names, {}, [], quarantine)
        return detail
    if route == "coerce":
        item = {"名称": "暗恋线", "详情": detail}
        coerce_delta_in_place({"修改元素": {"线索集": [item]}})
        return item["详情"]
    story = {"事件集": [{"名称": name} for name in names["事件集"]],
             "角色集": [{"名称": "甲"}], "线索集": [{"名称": "暗恋线", "详情": detail}]}
    normalized, _ = normalize_structure(story, quarantine_invalid_refs=quarantine)
    return normalized["线索集"][0]["详情"]


def detail_errors(detail):
    """Known registries make wrong types and nonexistent targets observable."""
    names = {key: set() for key in COLLECTION_KEYS}
    names["事件集"] = {"晚归燕子相依", "甲、乙/丙"}
    names["角色集"] = {"甲"}
    errors = []
    validate_detail_schema(detail, "线索[暗恋线]", names, errors, [], mode="governance")
    return errors


@pytest.mark.parametrize("route", ["structure", "detail", "coerce"])
@pytest.mark.parametrize("value", [["晚归燕子相依"], '["晚归燕子相依"]', "晚归燕子相依"])
def test_explicit_field_supplies_type_for_bare_reference_without_guessing_target(route, value):
    detail = normalized_reference_detail(value, route=route)
    assert detail["关联事件"] == ["事件:晚归燕子相依"]
    assert detail_errors(detail) == []
    assert normalized_reference_detail(detail["关联事件"], route=route) == detail


@pytest.mark.parametrize("route", ["structure", "detail", "coerce"])
def test_unknown_bare_reference_gains_declared_type_but_still_fails_strict_target_validation(route):
    detail = normalized_reference_detail(["未登记的事件"], route=route)
    assert detail["关联事件"] == ["事件:未登记的事件"]
    assert any("不存在的事件[未登记的事件]" in error for error in detail_errors(detail))


@pytest.mark.parametrize("value", ["角色:甲", "未知类型:晚归燕子相依", "角色：甲", "事件:"])
@pytest.mark.parametrize("route", ["structure", "detail", "coerce"])
def test_existing_colon_never_reclassifies_wrong_or_unknown_type(value, route):
    detail = normalized_reference_detail([value], route=route, quarantine=True)
    assert detail["关联事件"] == [value]
    assert "待确认引用" not in detail
    assert detail_errors(detail)


@pytest.mark.parametrize("value", [["晚归燕子相依"], '["晚归燕子相依"]', "晚归燕子相依"])
@pytest.mark.parametrize("route", ["structure", "detail", "coerce"])
def test_generic_association_never_infers_an_element_type(value, route):
    detail = normalized_reference_detail(value, field="关联对象", route=route)
    expected = ["晚归燕子相依"] if isinstance(value, list) or value.startswith("[") else value
    assert detail["关联对象"] == expected


@pytest.mark.parametrize("route", ["structure", "detail", "coerce"])
def test_declared_reference_keeps_whole_name_and_empty_or_nonstring_evidence(route):
    detail = normalized_reference_detail(["甲、乙/丙", "", " ", 7, {"错误": "对象"}], route=route)
    assert detail["关联事件"] == ["事件:甲、乙/丙", "", " ", 7, {"错误": "对象"}]
    assert detail_errors(detail)


@pytest.mark.parametrize("route", ["structure", "detail"])
def test_unknown_declared_bare_reference_uses_existing_quarantine_and_preserves_name(route):
    detail = normalized_reference_detail(["晚归燕子相依", "未登记的事件"], route=route, quarantine=True)
    assert detail["关联事件"] == ["事件:晚归燕子相依"]
    assert json.loads(detail["待确认引用"]) == ["事件:未登记的事件"]


@pytest.mark.parametrize("field,expected", [("关联角色", "角色:原名"), ("关联事件", "事件:原名"),
    ("关联地点", "地点:原名"), ("关联线索", "线索:原名"), ("关联阵营", "阵营:原名"),
    ("关联物品", "物品:原名"), ("关联其他事项", "其他事项:原名")])
def test_helper_uses_only_the_type_declared_by_each_formal_reference_field(field, expected):
    from normalize_story_schema import canonicalize_detail_ref_array
    assert canonicalize_detail_ref_array(["原名"], field_key=field) == [expected]


@pytest.mark.parametrize("field", [None, "关联对象", "关联成员", "关联道具", "普通叙述"])
def test_helper_without_a_formal_type_declaration_does_not_prefix_bare_names(field):
    from normalize_story_schema import canonicalize_detail_ref_array
    assert canonicalize_detail_ref_array(["原名"], field_key=field) == ["原名"]


def test_quarantined_bare_reference_is_idempotent_without_duplicate_evidence():
    detail = normalized_reference_detail(["未登记的事件"], route="detail", quarantine=True)
    original = copy.deepcopy(detail)
    normalize_detail_refs(detail, {key: set() for key in COLLECTION_KEYS}, {}, [], True)
    assert detail == original
    assert json.loads(detail["待确认引用"]) == ["事件:未登记的事件"]
