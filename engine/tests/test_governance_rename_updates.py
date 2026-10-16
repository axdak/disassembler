"""Explicit same-patch renames retain one existing identity through governance."""
import copy
import json

import pytest

from apply_governance_ops import apply_ops
from local_json_patch import delta_repair_scope
from merge_delta import merge_delta
from validate_delta import delta_errors


OLD = "霍然升任副班长享受权力并扮演孔震镇压班级"
NEW = "霍然升任班长享受权力并扮演孔震镇压班级"


def rename_fixture():
    """Mirror the real sparse update without depending on a live benchmark case."""
    story = {"介绍": {"标题": "童年", "描述": "童年故事"}, "角色集": [], "地点集": [],
             "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
             "事件集": [{"名称": OLD, "别名": [], "分组": "00000010-掌权施威",
                        "时间": "0001-01-15T00:00:00", "发生地点": "", "参与成员": [],
                        "目标事件": [], "重量级": 0, "标签集": [], "介绍": "掌权事件",
                        "详情": {"涉及章节": "0015", "起因": "旧档案事实", "动作链条": "完整旧动作"}}]}
    patch = {"章节范围": "第011章-第015章", "新增元素": {},
             "修改元素": {"事件集": [{"名称": NEW, "分组": "00000010-班长掌权",
                                    "详情": {"提取理由": "原文明示为班长", "起因": "本次补充事实"}}]},
             "治理操作": {"重命名元素": [{"类型": "事件", "原名称": OLD, "新名称": NEW}]}}
    return story, patch


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_governance_validates_sparse_update_under_explicit_new_name():
    story, patch = rename_fixture()
    original = copy.deepcopy((story, patch))
    errors, _ = delta_errors(story, patch, "governance")
    assert errors == []
    assert (story, patch) == original


def test_merge_resolves_new_name_before_official_rename_without_creating_item(tmp_path):
    story, patch = rename_fixture()
    story["角色集"] = [{"名称": "霍然", "详情": {"关联事件": ["事件:" + OLD]}}]
    source, delta = tmp_path / "story.json", tmp_path / "patch.json"
    write_json(source, story)
    write_json(delta, patch)
    stats = merge_delta(source, delta)
    merged = json.loads(source.read_text(encoding="utf-8"))
    assert stats["事件集"]["新增"] == 0 and stats["事件集"]["修改"] == 1
    assert len(merged["事件集"]) == 1 and merged["事件集"][0]["名称"] == OLD
    after, _, errors = apply_ops(merged, patch)
    assert errors == []
    event = after["事件集"][0]
    assert event["名称"] == NEW and event["别名"] == [OLD]
    assert event["详情"]["涉及章节"] == "0015"
    assert event["时间"] == "0001-01-15T00:00:00"
    assert event["详情"]["起因"] == "旧档案事实\n本次补充事实"
    assert event["详情"]["动作链条"] == "完整旧动作"
    assert after["角色集"][0]["详情"]["关联事件"] == ["事件:" + NEW]


def test_repair_scope_never_promotes_a_declared_rename_target(tmp_path):
    story, patch = rename_fixture()
    reference = tmp_path / "patch.json"
    write_json(reference, patch)
    scope = delta_repair_scope(reference, f"修改目标[{NEW}]不存在", story)
    assert scope == {}


@pytest.mark.parametrize("field,value,diagnostic", [
    ("时间", "0001-01-16T00:00:00", ".时间 不得"),
    ("涉及章节", "0016", ".详情.涉及章节 不得"),
])
def test_new_name_update_cannot_bypass_existing_event_temporal_guards(field, value, diagnostic):
    story, patch = rename_fixture()
    item = patch["修改元素"]["事件集"][0]
    if field == "涉及章节":
        item["详情"][field] = value
    else:
        item[field] = value
    assert any(diagnostic in error for error in delta_errors(story, patch, "governance")[0])


def test_old_name_update_remains_legal_in_same_rename_patch():
    story, patch = rename_fixture()
    patch["修改元素"]["事件集"][0]["名称"] = OLD
    assert delta_errors(story, patch, "governance")[0] == []


@pytest.mark.parametrize("problem", ["undeclared", "missing_source", "ambiguous_source",
                                    "target_collision", "duplicate_target", "duplicate_source",
                                    "added_target", "alias_collision", "removed_source", "rename_chain"])
def test_unsafe_rename_cannot_authorize_new_name_modification(tmp_path, problem):
    story, patch = rename_fixture()
    operations = patch["治理操作"]
    if problem == "undeclared":
        operations.clear()
    elif problem == "missing_source":
        operations["重命名元素"][0]["原名称"] = "不存在的旧名"
    elif problem == "ambiguous_source":
        story["事件集"].append(copy.deepcopy(story["事件集"][0]))
    elif problem == "target_collision":
        event = copy.deepcopy(story["事件集"][0])
        event["名称"] = NEW
        story["事件集"].append(event)
    elif problem == "duplicate_target":
        operations["重命名元素"].append({"类型": "事件", "原名称": "另一个旧名", "新名称": NEW})
    elif problem == "duplicate_source":
        operations["重命名元素"].append({"类型": "事件", "原名称": OLD, "新名称": "另一个新名"})
    elif problem == "added_target":
        patch["新增元素"]["事件集"] = [{"名称": NEW}]
    elif problem == "alias_collision":
        story["事件集"].append({"名称": "别的事件", "别名": [NEW]})
    elif problem == "removed_source":
        operations["删除元素"] = [{"类型": "事件", "名称": OLD}]
    else:
        operations["重命名元素"].append({"类型": "事件", "原名称": NEW, "新名称": "最终新名"})
    assert delta_errors(story, patch, "governance")[0]
    source, delta = tmp_path / "story.json", tmp_path / "patch.json"
    write_json(source, story)
    write_json(delta, patch)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        merge_delta(source, delta)
    assert source.read_bytes() == before


@pytest.mark.parametrize("field", ["原名称", "新名称"])
@pytest.mark.parametrize("value", [["错误类型"], {"错误": "类型"}])
def test_malformed_rename_names_return_validation_errors_without_crashing(field, value):
    story, patch = rename_fixture()
    patch["治理操作"]["重命名元素"][0][field] = value
    assert delta_errors(story, patch, "governance")[0]


def test_malformed_other_rename_cannot_crash_valid_target_validation():
    story, patch = rename_fixture()
    patch["治理操作"]["重命名元素"].append({"类型": "事件", "原名称": ["错误类型"], "新名称": "其他名"})
    assert delta_errors(story, patch, "governance")[0]


@pytest.mark.parametrize("typ,collection", [("角色", "角色集"), ("事件", "事件集"),
    ("地点", "地点集"), ("线索", "线索集"), ("阵营", "阵营集"),
    ("物品", "物品集"), ("道具", "物品集"), ("其他事项", "其他事项集")])
def test_rename_updates_keep_collection_identity_even_with_cross_type_same_name(tmp_path, typ, collection):
    """Renaming a location must never resolve a same-name character or event."""
    other = "角色集" if collection != "角色集" else "地点集"
    story = {collection: [{"名称": "旧名", "别名": [], "详情": {"涉及章节": "0015"}}],
             other: [{"名称": "新名", "别名": [], "详情": {"档案": "其他类型保留"}}]}
    patch = {"章节范围": "第011章-第015章", "新增元素": {},
             "修改元素": {collection: [{"名称": "新名", "详情": {"提取理由": "原文明确", "档案": "补充内容"}}]},
             "治理操作": {"重命名元素": [{"类型": typ, "原名称": "旧名", "新名称": "新名"}]}}
    assert delta_errors(story, patch, "governance")[0] == []
    source, delta = tmp_path / "story.json", tmp_path / "patch.json"
    write_json(source, story)
    write_json(delta, patch)
    assert merge_delta(source, delta)[collection]["新增"] == 0
    merged = json.loads(source.read_text(encoding="utf-8"))
    result, _, errors = apply_ops(merged, patch)
    assert errors == []
    assert len(result[collection]) == 1 and result[collection][0]["名称"] == "新名"
    assert result[other] == story[other]


def test_rename_in_another_collection_cannot_authorize_event_update(tmp_path):
    story, patch = rename_fixture()
    patch["治理操作"]["重命名元素"][0]["类型"] = "角色"
    story["角色集"] = [{"名称": OLD, "别名": []}]
    assert any("修改目标" in error for error in delta_errors(story, patch, "governance")[0])
    source, delta = tmp_path / "story.json", tmp_path / "patch.json"
    write_json(source, story)
    write_json(delta, patch)
    with pytest.raises(ValueError, match="修改目标"):
        merge_delta(source, delta)


@pytest.mark.parametrize("rename_type,other_type", [("物品", "道具"), ("道具", "物品")])
@pytest.mark.parametrize("conflict", ["duplicate_source", "duplicate_target", "chain", "merge", "delete", "demote"])
def test_item_type_aliases_share_one_rename_and_removal_namespace(tmp_path, rename_type, other_type, conflict):
    """物品/道具 are one collection; mixed spellings cannot hide identity conflicts."""
    story = {"物品集": [{"名称": "旧物", "别名": [], "详情": {"旧档案": "保留旧物"}},
                         {"名称": "另一旧物", "别名": [], "详情": {"旧档案": "保留另一物品"}}]}
    operations = {"重命名元素": [{"类型": rename_type, "原名称": "旧物", "新名称": "新甲"}]}
    patch = {"章节范围": "第011章-第015章", "新增元素": {},
             "修改元素": {"物品集": [{"名称": "新甲", "详情": {"提取理由": "原文明确", "补充": "甲更新"}}]},
             "治理操作": operations}
    if conflict == "duplicate_source":
        operations["重命名元素"].append({"类型": other_type, "原名称": "旧物", "新名称": "新乙"})
        patch["修改元素"]["物品集"].append({"名称": "新乙", "详情": {"提取理由": "原文明确", "补充": "乙更新"}})
    elif conflict == "duplicate_target":
        operations["重命名元素"].append({"类型": other_type, "原名称": "另一旧物", "新名称": "新甲"})
    elif conflict == "chain":
        operations["重命名元素"].append({"类型": other_type, "原名称": "新甲", "新名称": "新乙"})
    elif conflict == "merge":
        operations["合并元素"] = [{"类型": other_type, "主": "另一旧物", "并入": ["旧物"]}]
    elif conflict == "delete":
        operations["删除元素"] = [{"类型": other_type, "名称": "旧物"}]
    else:
        operations["降级元素"] = [{"类型": other_type, "名称": "旧物", "挂载到": {"类型": "物品", "名称": "另一旧物"}}]
    assert delta_errors(story, patch, "governance")[0]
    source, delta = tmp_path / "story.json", tmp_path / "patch.json"
    write_json(source, story)
    write_json(delta, patch)
    original = source.read_bytes()
    with pytest.raises(ValueError):
        merge_delta(source, delta)
    assert source.read_bytes() == original


@pytest.mark.parametrize("value", [["道具"], {"类型": "道具"}, None, 7])
def test_malformed_rename_type_returns_missing_target_error_without_type_crash(value):
    story, patch = rename_fixture()
    patch["治理操作"]["重命名元素"][0]["类型"] = value
    assert delta_errors(story, patch, "governance")[0]
