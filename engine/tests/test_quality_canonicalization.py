"""质量字段按声明契约归位；异常证据必须留给校验器处理。"""

from copy import deepcopy

import pytest


def test_chapter_prefixed_standard_reference_is_restored_in_one_pass():
    """真实模型把涉及事件加章节前缀后错放详情，一次机械处理应完成归位。"""
    from coerce_delta import coerce_delta_in_place
    patch = {"修改元素": {"线索集": [{"名称": "顽劣行径", "详情": {
        "提取理由": "本章出现后续事件。", "0003-涉及事件": '["事件:雪天游戏"]'}}]}}
    coerce_delta_in_place(patch)
    item = patch["修改元素"]["线索集"][0]
    assert item["涉及事件"] == ["雪天游戏"]
    assert "涉及事件" not in item["详情"]
    assert coerce_delta_in_place(patch) == []

from coerce_delta import coerce_delta_in_place
from normalize_story_schema import normalize_structure


def _event_delta(event):
    """仅构造内存 Delta，避免测试接触运行中的书籍。"""
    return {"新增元素": {"事件集": [event]}}


@pytest.mark.parametrize("top", [{}, {"标签集": [], "参与成员": [], "分组": "", "介绍": None}])
def test_nested_quality_fields_restore_without_inventing_content(top):
    event = {"名称": "诊病", **top, "详情": {
        "标签集": '["主题+功能+情绪+冲突"]',
        "参与成员": '["角色:徐六","角色:史大嘴"]',
        "分组": "诊病风波", "介绍": "原文中的诊病经过",
        "人物轨迹": '["原文轨迹"]', "关联角色": '["角色:徐六"]',
    }}
    delta = _event_delta(event)
    coerce_delta_in_place(delta)
    assert event["标签集"] == ["主题+功能+情绪+冲突"]
    assert event["参与成员"] == ["徐六", "史大嘴"]
    assert event["分组"] == "诊病风波"
    assert event["介绍"] == "原文中的诊病经过"
    assert set(event["详情"]) == {"人物轨迹", "关联角色"}
    assert event["详情"]["关联角色"] == ["角色:徐六"]
    assert event["详情"]["人物轨迹"] == '["原文轨迹"]'
    first = deepcopy(delta)
    coerce_delta_in_place(delta)
    assert delta == first


def test_conflicting_quality_fields_preserve_both_sources():
    event = {"名称": "诊病", "标签集": ["顶层标签"], "分组": "顶层分组",
             "详情": {"标签集": '["详情标签"]', "分组": "详情分组"}}
    coerce_delta_in_place(_event_delta(event))
    assert event["标签集"] == ["顶层标签"]
    assert event["分组"] == "顶层分组"
    assert event["详情"]["标签集"] == '["详情标签"]'
    assert event["详情"]["分组"] == "详情分组"


@pytest.mark.parametrize("value", ['["角色:徐六"]', "角色:徐六", ["角色:徐六"], "[]", []])
def test_declared_detail_references_have_real_arrays(value):
    event = {"名称": "诊病", "详情": {"关联角色": deepcopy(value)}}
    coerce_delta_in_place(_event_delta(event))
    expected = [] if value in ("[]", []) else ["角色:徐六"]
    assert event["详情"]["关联角色"] == expected
    assert "参与成员" not in event


@pytest.mark.parametrize("value", [["角色:徐六", 7, "未经确认"], '["角色:徐六",7,"未经确认"]'])
def test_invalid_detail_reference_entries_survive_both_paths(value):
    event = {"名称": "诊病", "详情": {"关联角色": deepcopy(value)}}
    coerce_delta_in_place(_event_delta(event))
    expected = ["角色:徐六", 7, "未经确认"]
    assert event["详情"]["关联角色"] == expected
    normalized, _ = normalize_structure({"事件集": [event]})
    assert normalized["事件集"][0]["详情"]["关联角色"] == expected


def test_mismatched_structural_prefix_remains_at_original_field():
    event = {"名称": "诊病", "参与成员": '["角色:徐六","地点:医馆"]',
             "目标事件": '["事件:争执","角色:史大嘴"]', "发生地点": "角色:徐六"}
    coerce_delta_in_place(_event_delta(event))
    assert event["参与成员"] == ["徐六", "地点:医馆"]
    assert event["目标事件"] == ["争执", "角色:史大嘴"]
    assert event["发生地点"] == "角色:徐六"


def test_historical_structure_restores_quality_and_is_idempotent():
    original = {"角色集": [{"名称": "徐六"}], "事件集": [{"名称": "诊病", "详情": {
        "标签集": '["原文标签"]', "分组": "诊病风波", "介绍": "原文介绍",
        "参与成员": '["角色:徐六"]', "关联角色": '["角色:徐六"]',
        "动作链条": '["不得全局解码"]', "叙述": '["原文叙述"]',
    }}]}
    source = deepcopy(original)
    result, _ = normalize_structure(source)
    event = result["事件集"][0]
    assert event["标签集"] == ["原文标签"]
    # 既有章节顺序前缀仍由 chronology 派生，原分组文字必须完整保留。
    assert event["分组"].endswith("-诊病风波")
    assert event["介绍"] == "原文介绍"
    assert event["参与成员"] == ["徐六"]
    assert event["详情"]["关联角色"] == ["角色:徐六"]
    assert event["详情"]["动作链条"] == '["不得全局解码"]'
    assert event["详情"]["叙述"] == '["原文叙述"]'
    assert source == original
    second, _ = normalize_structure(result)
    assert second == result


def test_unavailable_quality_fields_are_not_fabricated():
    result, _ = normalize_structure({"事件集": [{"名称": "诊病", "详情": {"关联角色": "角色:徐六"}}]})
    event = result["事件集"][0]
    assert event["标签集"] == []
    assert event["分组"] == event["介绍"] == ""
    assert event["参与成员"] == []


def test_encoded_empty_top_array_can_be_recovered_from_detail():
    event = {"名称": "诊病", "参与成员": "[]", "详情": {"参与成员": '["角色:徐六"]'}}
    coerce_delta_in_place(_event_delta(event))
    assert event["参与成员"] == ["徐六"]
    assert "参与成员" not in event["详情"]


def test_invalid_nested_standard_array_keeps_exact_entry_types():
    event = {"名称": "诊病", "详情": {"标签集": ["原文标签", 7]}}
    coerce_delta_in_place(_event_delta(event))
    assert event["详情"]["标签集"] == ["原文标签", 7]
    assert "标签集" not in event


def test_normalizer_preserves_wrong_structural_prefix_even_when_quarantining():
    original = {"事件集": [{"名称": "诊病", "发生地点": "角色:徐六",
                              "参与成员": ["地点:医馆", 7], "详情": {}}]}
    result, _ = normalize_structure(original, quarantine_invalid_refs=True)
    event = result["事件集"][0]
    assert event["发生地点"] == "角色:徐六"
    assert event["参与成员"] == ["地点:医馆", 7]


@pytest.mark.parametrize("collection,field,value", [
    ("物品集", "分组", ""),
    ("角色集", "别名", []),
    ("事件集", "目标事件", "[]"),
])
def test_explicit_empty_nested_standard_fields_restore_without_model(collection, field, value):
    """明确给出的合法空值也是交付内容，不应产生无必要的模型修复。"""
    item = {"名称": "测试档案", "详情": {field: deepcopy(value)}}
    delta = {"新增元素": {collection: [item]}}
    coerce_delta_in_place(delta)
    assert field in item
    assert item[field] == ([] if value == "[]" else value)
    assert field not in item["详情"]
    first = deepcopy(delta)
    coerce_delta_in_place(delta)
    assert delta == first


@pytest.mark.parametrize("field,top,nested", [
    ("分组", "已有分组", ""),
    ("目标事件", ["已有事件"], []),
    ("目标事件", ["已有事件"], "[]"),
])
def test_empty_nested_value_does_not_erase_nonempty_top_or_conflict_evidence(field, top, nested):
    """空值与非空顶层不相等；必须保留双方，不能删除空补丁意图。"""
    event = {"名称": "诊病", field: deepcopy(top), "详情": {field: deepcopy(nested)}}
    delta = _event_delta(event)
    coerce_delta_in_place(delta)
    assert event[field] == top
    assert event["详情"][field] == nested
    first = deepcopy(delta)
    coerce_delta_in_place(delta)
    assert delta == first


def test_wrong_detail_reference_type_survives_quarantine_but_valid_forward_ref_moves():
    """不存在的错误类型不能被隔离清除；同类型前向引用仍沿用隔离策略。"""
    story = {"事件集": [{"名称": "诊病", "详情": {
        "关联角色": ["地点:不存在", "角色:尚未登记"],
        "关联物品": ["道具:尚未登记物品"],
    }}]}
    result, _ = normalize_structure(story, quarantine_invalid_refs=True)
    detail = result["事件集"][0]["详情"]
    assert detail["关联角色"] == ["地点:不存在"]
    assert detail["关联物品"] == []
    assert "角色:尚未登记" in detail["待确认引用"]
    assert "物品:尚未登记物品" in detail["待确认引用"]
    assert "地点:不存在" not in detail["待确认引用"]
    second, _ = normalize_structure(result, quarantine_invalid_refs=True)
    assert second == result


def test_textual_identity_fields_remain_for_local_repair():
    """本次空值归位不扩大为性别、附注年龄或单字符串别名的推断。"""
    detail = {"性别": "男", "年龄": "四岁（比徐六小十岁）", "别名": "小脑袋"}
    item = {"名称": "测试角色", "详情": deepcopy(detail)}
    coerce_delta_in_place({"新增元素": {"角色集": [item]}})
    assert item["详情"] == detail
    assert not any(field in item for field in detail)
