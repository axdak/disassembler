"""正常 Delta 和身份治理必须保全事实；纠错通过明确证据协议表达。"""
import copy
import json

import pytest

from apply_governance_ops import apply_ops
from generation_contract import chapter_quality_errors
from governance_contract import governance_quality_errors
from merge_delta import deep_merge_item
from normalize_story_schema import normalize_structure
from story_schema_rules import DEFAULTS, make_empty_structure, validate_exact_story_schema


def story_fixture():
    story = make_empty_structure()
    story["介绍"] = {"标题": "验证", "描述": "治理保全验证。"}
    for name in ("主角", "证人"):
        record = copy.deepcopy(DEFAULTS["角色集"])
        record.update(名称=name, 介绍=name + "的档案")
        record["详情"] = {"首次章节": "0001", "最近章节": "0001"}
        if name == "证人":
            record["别名"] = ["目击者"]
            record["关系"] = ["朋友:主角"]
            record["详情"].update(秘密="密钥藏在钟楼", 人物轨迹='["第001章：目睹密谋"]')
        story["角色集"].append(record)
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="救援", 介绍="记录救援", 分组="00000010-救援脱险",
                 标签集=["救援+转折+紧张+生存冲突"], 参与成员=["主角", "证人"])
    event["详情"] = {"涉及章节": "0001", "提取理由": "原文救援", "关键抉择": "甲放弃追击，转身救人。"}
    story["事件集"] = [event]
    return normalize_structure(story)[0]


def test_free_event_detail_update_retains_original_fact():
    before = story_fixture()
    update = {"名称": "救援", "详情": {"关键抉择": "甲离开现场。", "提取理由": "本章补充离场"}}
    after = copy.deepcopy(before)
    deep_merge_item(after["事件集"][0], update, "事件集", "0002")
    text = after["事件集"][0]["详情"]["关键抉择"]
    assert "甲放弃追击，转身救人。" in text
    assert "甲离开现场。" in text
    assert validate_exact_story_schema(after, mode="process")[0] == []


def test_degrade_preserves_complete_archive_and_original_reference_context():
    before = story_fixture()
    patch = {"治理操作": {"降级元素": [{"类型": "角色", "名称": "证人", "原因": "临时人物",
                         "挂载到": {"类型": "角色", "名称": "主角"}}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    host = after["角色集"][0]
    archive = json.loads(host["详情"]["治理档案记录"])
    entry = json.loads(archive[-1])
    assert entry["原档案"] == before["角色集"][1]
    assert any(ref["字段"] == "参与成员" and ref["名称"] == "救援" for ref in entry["原引用"])
    assert validate_exact_story_schema(after, mode="governance")[0] == []


@pytest.mark.parametrize("target", ["不存在", "证人"])
def test_invalid_degrade_target_is_atomic(target):
    before = story_fixture()
    after, _, warnings = apply_ops(before, {"治理操作": {"降级元素": [{
        "类型": "角色", "名称": "证人", "原因": "临时人物", "挂载到": {"类型": "角色", "名称": target}}]}})
    assert warnings
    assert after == before


def test_degrade_requires_destination():
    before = story_fixture()
    after, _, warnings = apply_ops(before, {"治理操作": {"降级元素": [{"类型": "角色", "名称": "证人"}]}})
    assert warnings
    assert after == before


def test_merge_keeps_conflicting_scalar_and_its_source_record():
    before = story_fixture()
    before["角色集"][0]["年龄"] = 16
    before["角色集"][1]["年龄"] = 17
    before["角色集"][1]["详情"]["最近章节"] = "0010"
    after, _, warnings = apply_ops(before, {"治理操作": {"合并元素": [
        {"类型": "角色", "主": "主角", "并入": ["证人"]}]}})
    assert warnings == []
    entries = json.loads(after["角色集"][0]["详情"]["治理档案记录"])
    assert json.loads(entries[-1])["原档案"]["年龄"] == 17
    assert "年龄" in after["角色集"][0]["详情"]["待确认信息"]


def test_evidenced_tag_correction_keeps_old_value_and_can_pass_quality_guard():
    before = story_fixture()
    old = before["事件集"][0]["标签集"]
    new = ["和解+收束+释然+观念冲突"]
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集", "值": new,
        "原值": old, "理由": "原标签误判，原文为和解", "证据": [{"章节": "0001", "原文": "两人握手言和。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert governance_quality_errors(before, after, patch) == []
    assert old[0] in after["事件集"][0]["详情"]["治理纠错记录"]


def test_unexplained_tag_replacement_stays_blocked():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集", "值": ["和解+收束+释然+观念冲突"]}]}}
    after, _, _ = apply_ops(before, patch)
    assert governance_quality_errors(before, after, patch)


def test_correction_rejects_stale_old_value_without_any_mutation():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "标签集",
        "原值": ["过期+标签+不能+覆盖"], "值": ["和解+收束+释然+观念冲突"], "理由": "纠错",
        "证据": [{"章节": "0001", "原文": "两人握手言和。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings
    assert after == before


def test_normal_update_quality_gate_checks_existing_event_prose():
    before = story_fixture()
    after = copy.deepcopy(before)
    after["事件集"][0]["详情"]["关键抉择"] = "离开了。"
    patch = {"新增元素": {}, "修改元素": {"事件集": [{"名称": "救援", "详情": {"关键抉择": "离开了。"}}]}}
    assert any("关键抉择" in error for error in chapter_quality_errors(before, after, patch))


def test_evidenced_detail_correction_is_scoped_and_retains_prior_fact():
    before = story_fixture()
    old = before["事件集"][0]["详情"]["关键抉择"]
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "详情/关键抉择",
        "原值": old, "值": "甲先救人，随后追击。", "理由": "原提取顺序错误",
        "证据": [{"章节": "0001", "原文": "他先救人，随后才追击。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["事件集"][0]["详情"]["关键抉择"] == "甲先救人，随后追击。"
    assert old in after["事件集"][0]["详情"]["治理纠错记录"]
    assert chapter_quality_errors(before, after, patch) == []


def test_correction_cannot_replace_event_trace_via_detail_pointer():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "事件", "名称": "救援", "字段": "详情/涉及章节",
        "原值": "0001", "值": "0009", "理由": "错误范围",
        "证据": [{"章节": "0009", "原文": "另一章"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings
    assert after == before


def test_correction_evidence_must_quote_the_original_chapter():
    """声明有证据不能替代检查实际引文；本检查不声称证明语义蕴含。"""
    from information_preservation import correction_evidence_errors
    op = {"类型": "事件", "名称": "救援", "字段": "标签集", "原值": [], "值": [],
          "理由": "纠错", "证据": [{"章节": "0001", "原文": "两人握手言和。"}]}
    patch = {"治理操作": {"设置字段": [op]}}
    assert correction_evidence_errors(patch, {1: "最后，两人握手言和。"}, 1) == []
    errors = correction_evidence_errors(patch, {1: "两人继续争斗。"}, 1)
    assert any("/治理操作/设置字段/0/证据/0/原文" in error for error in errors)
    op["证据"][0]["章节"] = "0002"
    assert correction_evidence_errors(patch, {2: "两人握手言和。"}, 1)


def test_whole_detail_replacement_cannot_erase_correction_archive():
    """整个详情对象替换会同时抹掉普通事实和宿主归档，应使用原有局部接口。"""
    before = story_fixture()
    after, _, warnings = apply_ops(before, {"治理操作": {"设置字段": [
        {"类型": "角色", "名称": "主角", "字段": "详情", "值": {"新字段": "新值"}}]}})
    assert warnings
    assert after == before


def test_source_backed_relationship_correction_is_not_frozen_as_valid_format():
    before = story_fixture()
    patch = {"治理操作": {"设置字段": [{"类型": "角色", "名称": "证人", "字段": "关系",
        "原值": ["朋友:主角"], "值": ["敌人:主角"], "理由": "此前误判朋友，原文为敌对",
        "证据": [{"章节": "0001", "原文": "他是我的敌人。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert governance_quality_errors(before, after, patch) == []
    assert "朋友:主角" in after["角色集"][1]["详情"]["治理纠错记录"]


def test_bad_quote_can_be_repaired_without_rewriting_the_operation(tmp_path):
    from information_preservation import correction_evidence_errors
    from local_json_patch import delta_repair_scope, apply_scoped_patch
    import hashlib
    patch = {"新增元素": {}, "修改元素": {}, "治理操作": {"设置字段": [{
        "类型": "事件", "名称": "救援", "字段": "标签集", "原值": [], "值": [], "理由": "纠错",
        "证据": [{"章节": "0001", "原文": "记错的引文"}]}]}}
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
    errors = correction_evidence_errors(patch, {1: "两人握手言和。"}, 1)
    scope = delta_repair_scope(source, "\n".join(errors), story_fixture())
    pointer = "/治理操作/设置字段/0/证据/0/原文"
    assert set(scope) == {pointer}
    apply_scoped_patch(source, output, {"base_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "changes": [{"path": pointer, "value": "两人握手言和。"}]}, scope)
    patch["治理操作"]["设置字段"][0]["证据"][0]["原文"] = "两人握手言和。"
    assert json.loads(output.read_text(encoding="utf-8")) == patch


@pytest.mark.parametrize("valid_quote", [False, True])
def test_governance_commit_checks_quote_before_publication(tmp_path, valid_quote):
    """真实提交入口必须接上引文检查，不能只给纯函数写通过测试。"""
    import hashlib
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    pipeline.init_dirs(tmp_path)
    source = write_completed_chapter(tmp_path, 1)
    source.write_text("两人握手言和。", encoding="utf-8")
    story_path = tmp_path / "故事结构_增量.json"
    before = story_fixture()
    write_json(story_path, before)
    pipeline.ensure_story(tmp_path)
    original = story_path.read_bytes()
    old = before["事件集"][0]["标签集"]
    patch = {"章节": "0001", "新增元素": {}, "修改元素": {}, "治理操作": {"设置字段": [{
        "类型": "事件", "名称": "救援", "字段": "标签集", "原值": old,
        "值": ["和解+收束+释然+观念冲突"], "理由": "原文为和解",
        "证据": [{"章节": "0001", "原文": "两人握手言和。" if valid_quote else "原文没有的话"}]}]}}
    output = tmp_path / "质量治理" / "按需治理" / "correction_test.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(original).hexdigest())
    rc = pipeline.commit_governance(tmp_path, str(output))
    if valid_quote:
        assert rc == 0
        current = json.loads(story_path.read_text(encoding="utf-8"))
        assert current["事件集"][0]["标签集"] == patch["治理操作"]["设置字段"][0]["值"]
    else:
        assert rc != 0
        assert story_path.read_bytes() == original
        assert "原文中不存在" in output.with_suffix(".repair.md").read_text(encoding="utf-8")


def test_repeated_identity_merges_keep_one_flat_copy_of_each_archive():
    """连续周期中的合并链不得把旧归档再次嵌套序列化，避免指数增长。"""
    from apply_governance_ops import merge_items
    records = []
    for index in range(11):
        role = copy.deepcopy(DEFAULTS["角色集"])
        role.update(名称=f"角色{index}", 介绍=f"原档案{index}", 详情={"秘密": f"信息{index}"})
        records.append(role)
    initial_size = len(json.dumps(records, ensure_ascii=False).encode())
    for index in range(10):
        merge_items(records[index + 1], records[index])
        entries = [json.loads(value) for value in json.loads(records[index + 1]["详情"]["治理档案记录"])]
        assert len(entries) == index + 1
        assert all("治理档案记录" not in entry["原档案"]["详情"] for entry in entries)
        assert len(json.dumps(records[index + 1], ensure_ascii=False).encode()) < initial_size * 4
    assert {entry["原档案"]["名称"] for entry in entries} == {f"角色{i}" for i in range(10)}


def test_fact_guard_aligns_explicit_identity_and_reference_changes():
    before = story_fixture()
    before["事件集"][0]["详情"]["目击者"] = "角色:证人"
    patch = {"治理操作": {"重命名元素": [{"类型": "角色", "原名称": "证人", "新名称": "新姓名"}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["事件集"][0]["详情"]["目击者"] == "角色:新姓名"
    assert governance_quality_errors(before, after, patch) == []
    assert chapter_quality_errors(before, after, patch) == []


def test_update_and_identity_merge_do_not_treat_new_archive_as_old_story_fact():
    before = story_fixture()
    source = copy.deepcopy(before["事件集"][0])
    source["名称"] = "另名救援"
    before["事件集"].append(source)
    change = {"名称": "另名救援", "介绍": "补充原文行动", "详情": {"提取理由": "补充行动"}}
    patch = {"新增元素": {}, "修改元素": {"事件集": [change]}, "治理操作": {"合并元素": [
        {"类型": "事件", "主": "救援", "并入": ["另名救援"]}]}}
    updated = copy.deepcopy(before)
    deep_merge_item(updated["事件集"][1], change, "事件集", "0001")
    after, _, warnings = apply_ops(updated, patch)
    assert warnings == []
    assert chapter_quality_errors(before, after, patch) == []


def test_equal_corrections_on_different_sources_keep_their_ownership():
    """纠错记录随所属档案保存，不能平铺去重成一次无归属的纠错。"""
    from apply_governance_ops import merge_items
    correction = json.dumps([json.dumps({"字段": "年龄", "原值": 16, "新值": 17}, ensure_ascii=False)], ensure_ascii=False)
    target = copy.deepcopy(DEFAULTS["角色集"])
    target["名称"] = "主档案"
    for name in ("来源一", "来源二"):
        source = copy.deepcopy(DEFAULTS["角色集"])
        source.update(名称=name, 详情={"治理纠错记录": correction})
        merge_items(target, source)
    archives = [json.loads(entry)["原档案"] for entry in json.loads(target["详情"]["治理档案记录"])]
    assert {item["名称"] for item in archives} == {"来源一", "来源二"}
    assert all(item["详情"]["治理纠错记录"] == correction for item in archives)


def test_evidenced_faction_transition_replaces_current_value_and_retains_history():
    """当前阵营不应累积已退出阵营；旧事实通过既有纠错记录保全。"""
    before = story_fixture()
    for name in ("旧派", "新派"):
        faction = copy.deepcopy(DEFAULTS["阵营集"])
        faction.update(名称=name, 介绍=name + "阵营档案", 详情={"首次章节":"0001", "最近章节":"0002"})
        before["阵营集"].append(faction)
    before["角色集"][0]["所属阵营"] = ["旧派"]
    patch = {"治理操作":{"设置字段":[{"类型":"角色","名称":"主角","字段":"所属阵营",
        "原值":["旧派"],"值":["新派"],"理由":"主角已经退出旧派加入新派",
        "证据":[{"章节":"0002","原文":"主角退出旧派，加入新派。"}]}]}}
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after["角色集"][0]["所属阵营"] == ["新派"]
    assert "旧派" in after["角色集"][0]["详情"]["治理纠错记录"]
    assert governance_quality_errors(before, after, patch) == []
