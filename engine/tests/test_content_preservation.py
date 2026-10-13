"""机械规范化必须保全非标准证据，并在重复执行后保持稳定。"""

import json
from copy import deepcopy

import pytest

from chronology import assign_event_group_orders, event_group_order_errors
from coerce_delta import coerce_delta_in_place
from compress_tags import compress_item
from normalize_story_schema import normalize_structure
from story_schema_rules import make_empty_structure, validate_exact_story_schema
from story_schema_rules import canonicalize_supplementary_tags, parse_supplementary_tags
from test_chronology import event


@pytest.mark.parametrize("groups", [
    ["经济线", "转场", "经济线", "经济线续2"],
    ["甲" * 38 + "乙丙", "转场", "甲" * 38 + "乙丙",
     "甲" * 38 + "丁戊", "转场二", "甲" * 38 + "丁戊"],
])
def test_group_split_reserves_authored_and_generated_names(groups):
    """后文原名和截断后的同前缀都不能与新分段碰撞。"""
    events = [event(str(index), "0001", group) for index, group in enumerate(groups)]
    original = deepcopy(events)
    assign_event_group_orders(events, split_repeated=True)
    assert event_group_order_errors(events) == []
    assert [item["名称"] for item in events] == [item["名称"] for item in original]
    assert all(len(item["分组"].split("-", 1)[1]) <= 40 for item in events)
    for before, after in zip(original, events):
        if before["分组"] not in after["分组"]:
            assert before["分组"] in after["详情"].values()
        assert after["介绍"] == before["介绍"]
    once = deepcopy(events)
    assign_event_group_orders(events, split_repeated=True)
    assert events == once


@pytest.mark.parametrize("value, expected", [
    (None, ["null"]),
    (["标签", {"证据": "原文"}, 7, None, False],
     ['["标签",{"证据":"原文"},7,null,false]']),
    ([None], ["[null]"]),
    ({"证据": [False, 0]}, ['{"证据":[false,0]}']),
    (False, ["false"]),
    (0, ["0"]),
    (' ["标签", {"证据":"原文"}, 7, null] \n',
     [' ["标签", {"证据":"原文"}, 7, null] \n']),
    ("  原文,不可猜测分割\n", ["  原文,不可猜测分割\n"]),
    ('["正常标签","既有标签"]', ["正常标签", "既有标签"]),
    ('[" 保留空白 "]', [" 保留空白 "]),
    (["甲", " 甲 "], ['["甲"," 甲 "]']),
    (["甲", "甲", ""], ["甲"]),
    ([], None),
])
@pytest.mark.parametrize("entrypoint", ["coerce", "compress", "normalize"])
def test_supplementary_evidence_survives_each_repair_entrypoint(value, expected, entrypoint):
    """真实规范化、纠错及压缩入口应共享保全规则，并通过治理校验。"""
    item = event("原事件", "0001", "00000010-剧情线")
    item["详情"]["补充标签"] = deepcopy(value)

    def repair(source):
        if entrypoint == "coerce":
            coerce_delta_in_place({"修改元素": {"事件集": [source]}})
            return source
        if entrypoint == "compress":
            compress_item(source, 8)
            return source
        story = make_empty_structure()
        story["事件集"] = [source]
        result, _ = normalize_structure(story)
        return result["事件集"][0]

    repaired = repair(item)
    if expected is None:
        assert "补充标签" not in repaired["详情"]
    else:
        assert json.loads(repaired["详情"]["补充标签"]) == expected
    assert repair(deepcopy(repaired)) == repaired
    story = make_empty_structure()
    story["事件集"] = [repaired]
    assert validate_exact_story_schema(story, mode="governance")[0] == []


def test_truncated_group_preserves_existing_archive_and_chapter_scope():
    """原始分组档案不覆盖作者字段，另章同名不影响当前章的续号选择。"""
    full_name = "甲" * 38 + "结尾"
    events = [event("一", "0001", full_name), event("二", "0001", "转场"),
              event("三", "0001", full_name), event("四", "0002", "甲" * 38 + "续2")]
    events[2]["详情"]["原始分组"] = "更早的作者内容"
    assign_event_group_orders(events, split_repeated=True)
    assert events[2]["分组"] == "00000030-" + "甲" * 38 + "续2"
    assert events[2]["详情"]["原始分组"] == "更早的作者内容"
    assert events[2]["详情"]["原始分组2"] == full_name
    assert event_group_order_errors(events) == []
    once = deepcopy(events)
    assign_event_group_orders(events, split_repeated=True)
    assert events == once


@pytest.mark.parametrize("entrypoint", ["compress", "normalize"])
def test_compression_preserves_whitespace_evidence_without_main_tag_overlap(entrypoint):
    """主标签的规范文本已存在时，带空白原文仍须可逆保留且不得形成跨区重复。"""
    item = event("一", "0001", "00000010-剧情线")
    item["标签集"] = ["甲"]
    item["详情"]["补充标签"] = " 甲 "
    story = make_empty_structure()
    story["事件集"] = [item]
    if entrypoint == "compress":
        compress_item(item, 8)
    else:
        story, _ = normalize_structure(story)
        item = story["事件集"][0]
    assert validate_exact_story_schema(story, mode="governance")[0] == []
    assert " 甲 " in item["详情"]["补充标签原文"]
    once = deepcopy(item)
    compress_item(item, 8)
    assert item == once
    assert normalize_structure(story)[0] == story


@pytest.mark.parametrize("primary, limit, padded", [
    (["甲", "乙"], 1, " 乙 "),
    (["剧情母题:复仇"], 8, " 剧情母题:复仇 "),
])
def test_compression_preserves_padded_evidence_when_matching_tag_is_demoted(primary, limit, padded):
    """新降级标签与既有补充标签仅空白不同，也必须保全并通过严格校验。"""
    item = event("一", "0001", "00000010-剧情线")
    item["标签集"] = primary
    item["详情"]["补充标签"] = json.dumps([padded], ensure_ascii=False)
    compress_item(item, limit)
    story = make_empty_structure()
    story["事件集"] = [item]
    assert validate_exact_story_schema(story, mode="governance")[0] == []
    assert padded in item["详情"]["补充标签原文"]
    assert json.loads(item["详情"]["补充标签"]) == [padded.strip()]
    once = deepcopy(item)
    compress_item(item, limit)
    assert item == once
    assert normalize_structure(story)[0] == story


@pytest.mark.parametrize("entrypoint", ["delta", "governance"])
def test_supplementary_merge_preserves_canonical_raw_evidence(entrypoint):
    """合并不得使用校验器返回的裁剪文本替代已经保全的原始证据。"""
    from merge_delta import merge_supplementary_tags_field
    from apply_governance_ops import append_detail_value
    merge = (merge_supplementary_tags_field if entrypoint == "delta" else
             lambda old, new: append_detail_value(old, new, "补充标签"))
    raw = "  独有原文,不可猜测分割\n"
    canonical = canonicalize_supplementary_tags(raw)
    merged = merge("", canonical)
    assert json.loads(merged) == [raw]
    assert merge(merged, canonical) == merged
    # 两个各自合法的数组合并后，也不能因裁剪而丢掉不同空白写法的证据。
    merged = merge('["甲"]', '[" 甲 "]')
    parse_supplementary_tags(merged)
    assert " 甲 " in merged
    assert merge(merged, '[" 甲 "]') == merged
    with pytest.raises(ValueError):
        merge(merged, "broken JSON")


def test_governance_tag_demotion_preserves_raw_supplementary_evidence():
    """真实治理标签操作保留既有证据空白，同时合并降级标签。"""
    from apply_governance_ops import apply_ops
    raw = "  独有原文,不可猜测分割\n"
    item = event("一", "0001", "00000010-剧情线")
    item["标签集"] = ["甲"]
    item["详情"]["补充标签"] = canonicalize_supplementary_tags(raw)
    story = make_empty_structure()
    story["事件集"] = [item]
    story, _, warnings = apply_ops(story, {"治理操作": {"治理标签": [{"类型": "事件", "名称": "一", "保留标签": [],
                                 "降级标签": ["甲"], "理由": "归档标签"}]}})
    item = story["事件集"][0]
    assert warnings == []
    assert json.loads(item["详情"]["补充标签"]) == [raw, "甲"]
    assert item["标签集"] == []
    assert validate_exact_story_schema(story, mode="governance")[0] == []


@pytest.mark.parametrize("destination", ["missing", "unrelated_field", "other_element"])
@pytest.mark.parametrize("sparse", [False, True])
def test_preservation_rejects_unarchived_supplementary_whitespace_loss(tmp_path, destination, sparse):
    """模型不能裁剪补充证据，也不能用其他字段或其他元素的同文冒充原位保全。"""
    from fault_recovery import preservation_errors
    from merge_delta import save_json
    raw = "  原文证据不可裁剪\n"
    item = {"名称": "甲", "标签集": [], "详情": {"补充标签": json.dumps([raw], ensure_ascii=False)}}
    if sparse:
        item.pop("标签集")
    before = {"修改元素": {"角色集": [item]}}
    after = deepcopy(before)
    detail = after["修改元素"]["角色集"][0]["详情"]
    detail["补充标签"] = json.dumps([raw.strip()], ensure_ascii=False)
    if destination == "unrelated_field":
        detail["无关说明"] = raw
    if destination == "other_element":
        after["修改元素"]["角色集"].append({"名称": "乙", "标签集": [],
            "详情": {"补充标签原文": json.dumps([raw], ensure_ascii=False)}})
    old, new = tmp_path / "before.json", tmp_path / "after.json"
    save_json(old, before)
    save_json(new, after)
    assert preservation_errors(old, new)


@pytest.mark.parametrize("destination", ["unchanged", "primary", "archive", "quoted"])
@pytest.mark.parametrize("sparse", [False, True])
def test_preservation_accepts_reversible_supplementary_evidence_relocation(tmp_path, destination, sparse):
    """合法原文归档、跨区搬迁和合并时的 JSON 封存仍可通过内容门禁。"""
    from fault_recovery import preservation_errors
    from merge_delta import save_json
    raw = "  原文证据不可裁剪\n"
    item = {"名称": "甲", "标签集": [], "详情": {"补充标签": json.dumps([raw], ensure_ascii=False)}}
    if sparse:
        item.pop("标签集")
    before = {"修改元素": {"角色集": [item]}}
    after = deepcopy(before)
    target = after["修改元素"]["角色集"][0]
    if destination == "primary":
        target["标签集"] = [raw]
        target["详情"].pop("补充标签")
    if destination == "archive":
        # 使用真实压缩入口，并制造已有档案碰撞，须识别编号后的原文档案。
        target["标签集"] = [raw.strip()]
        target["详情"]["补充标签原文"] = "已有作者内容"
        compress_item(target, 8)
        assert json.loads(target["详情"]["补充标签原文2"]) == [raw]
    if destination == "quoted":
        from story_schema_rules import merge_supplementary_tag_values
        target["详情"]["补充标签"] = merge_supplementary_tag_values(
            json.dumps([raw.strip()], ensure_ascii=False), target["详情"]["补充标签"])
    old, new = tmp_path / "before.json", tmp_path / "after.json"
    save_json(old, before)
    save_json(new, after)
    assert preservation_errors(old, new) == []


def test_recovery_keeps_all_strategies_but_never_publishes_trimmed_evidence(tmp_path):
    """保全检查拦截坏候选后仍走完三策略，不能修改正式原件或误报成功。"""
    from fault_recovery import REVIEW_CHECKS, digest
    from merge_delta import save_json
    from repair_budget import REPAIR_EXHAUSTED
    from test_fault_recovery import scenario, invoke, state
    output, pack, _, calls, published, options = scenario(tmp_path)
    raw = "  原文证据不可裁剪\n"
    save_json(output, {"名称": "甲", "标签集": [],
                       "详情": {"补充标签": json.dumps([raw], ensure_ascii=False)}})
    original = output.read_bytes()

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        if kind == "recovery_diagnosis":
            save_json(expected, {"diagnosis": "应保留原文", "steps": ["补足理由并保全证据"]})
        elif kind == "recovery_review":
            save_json(expected, {"passed": True, "candidate_sha256": digest(root / output.name),
                "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "模拟模型误判通过"})
        else:
            value = json.loads(expected.read_text(encoding="utf-8"))
            value["提取理由"] = "补足理由"
            value["详情"]["补充标签"] = json.dumps([raw.strip()], ensure_ascii=False)
            save_json(expected, value)
        return 0

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published
    assert output.read_bytes() == original
    assert [item["strategy"] for item in state(tmp_path)["attempts"]] == ["targeted", "evidence", "diagnosed"]
    assert calls.count("auto_repair_chapter") == 3
