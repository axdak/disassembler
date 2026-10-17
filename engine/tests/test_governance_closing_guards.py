"""Keep repair metadata editable while preserving real narrative and parent links."""
import copy

import pytest

from apply_governance_ops import apply_ops
from fault_recovery import preservation_errors
from governance_contract import governance_quality_errors
from merge_delta import save_json
from story_schema_rules import make_empty_structure


@pytest.mark.parametrize("section", ["新增元素", "修改元素"])
def test_delta_extraction_reason_can_be_corrected_without_rewriting_narrative(tmp_path, section):
    """A repair may correct its justification; the submitted facts remain intact."""
    old = {section: {"事件集": [{"名称": "救援",
        "详情": {"提取理由": "旧修复说明把旁观者误写成了实际行动者，须按原文核实后纠正。",
                 "发展": "甲听到呼救后立即赶到河边，找到绳索将乙救上岸，随后等待医者到场。"}}]}}
    new = copy.deepcopy(old)
    new[section]["事件集"][0]["详情"]["提取理由"] = "按原文更正为甲实施救援，乙是被救者。"
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    save_json(before, old)
    save_json(after, new)
    raw = before.read_bytes()
    assert preservation_errors(before, after) == []
    assert before.read_bytes() == raw

    new[section]["事件集"][0]["详情"]["发展"] = "甲救乙。"
    save_json(after, new)
    assert any("发展" in error for error in preservation_errors(before, after))


@pytest.mark.parametrize("value", ["", None])
def test_delta_reason_cannot_be_erased(tmp_path, value):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    old = {"修改元素": {"事件集": [{"名称": "救援", "详情": {
        "提取理由": "旧理由仍是需要核对的完整说明，不能用空值或错误类型静默清除。"}}]}}
    new = copy.deepcopy(old)
    new["修改元素"]["事件集"][0]["详情"]["提取理由"] = value
    save_json(before, old)
    save_json(after, new)
    assert preservation_errors(before, after)


def test_story_reason_is_not_delta_metadata(tmp_path):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    old = {"事件集": [{"名称": "救援", "详情": {
        "提取理由": "正式档案的旧文本仍受内容保全约束，不因同名键而扩大补丁元数据豁免。"}}]}
    new = copy.deepcopy(old)
    new["事件集"][0]["详情"]["提取理由"] = "替换说明"
    save_json(before, old)
    save_json(after, new)
    assert preservation_errors(before, after)


@pytest.mark.parametrize("collection,kind,parent", [
    ("地点集", "地点", "父级地点"), ("阵营集", "阵营", "父级阵营")])
def test_explicit_merge_can_remove_the_resulting_invalid_self_parent(collection, kind, parent):
    """Use the real merge/setter path; contracting identities must not require a cycle."""
    old = make_empty_structure()
    old[collection] = [{"名称": "主档", parent: "", "别名": [], "详情": {}},
                       {"名称": "重复档", parent: "主档", "别名": [], "详情": {}}]
    patch = {"治理操作": {
        "合并元素": [{"类型": kind, "主": "主档", "并入": ["重复档"]}],
        "设置字段": [{"类型": kind, "名称": "重复档", "字段": parent,
                    "原值": "主档", "值": "", "理由": "合并同一实体，解除由旧重复档产生的自身父级。"}]}}
    after, _, errors = apply_ops(old, patch)
    assert not errors
    assert len(after[collection]) == 1 and after[collection][0][parent] == ""
    assert governance_quality_errors(old, after, patch) == []

    old[collection] = [{"名称": "主档", parent: "独立上级", "别名": [], "详情": {}},
                       {"名称": "独立上级", parent: "", "别名": [], "详情": {}}]
    after = copy.deepcopy(old)
    after[collection][0][parent] = ""
    assert any(parent in error for error in governance_quality_errors(old, after, {}))
