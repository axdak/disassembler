"""非事件档案追溯可有据纠错；同补丁合并不能解除普通原值冲突保护。"""
import copy
import json

import pytest

from apply_governance_ops import apply_ops
from generation_contract import generation_scope_errors
from information_preservation import ARCHIVE_FIELD, CORRECTION_FIELD, correction_evidence_errors
from story_schema_rules import DEFAULTS, TYPE_TO_COLLECTION, make_empty_structure, validate_exact_story_schema
from validate_delta import validate_set_field_governance_ops


def record(kind, name, first, recent):
    """使用正式骨架，保留与真实故障相同的追溯位置。"""
    value = copy.deepcopy(DEFAULTS[TYPE_TO_COLLECTION[kind]])
    value.update(名称=name, 介绍=name + "的完整档案")
    value["详情"] = {"首次章节": first, "最近章节": recent}
    return value


def correction(kind="角色", name="凤栖梧", field="首次章节", old="0156", new="0157"):
    return {"类型": kind, "名称": name, "字段": "详情/" + field, "原值": old, "值": new,
            "理由": "原档案的章节来源有误，按已处理原文纠正并保留旧值。",
            "证据": [{"章节": "0157", "原文": "朱雀族，凤栖梧！"}]}


def workload(operations):
    return {"章节范围": "第156-160章", "新增元素": {}, "修改元素": {}, "治理操作": operations}


def replay_fixture():
    """保留实际三条纠错和三次身份合并的关键值、名称与执行先后关系。"""
    story = make_empty_structure()
    for kind, name, first, recent in [
        ("角色", "凤栖梧", "0156", "0159"),
        ("阵营", "朱雀族", "0156", "0158"), ("阵营", "南荒朱雀族", "0162", "0162"),
        ("地点", "小山谷祭台", "0162", "0162"), ("地点", "无名山谷祭台", "0160", "0161"),
        ("物品", "火红长枪", "0157", "0159"), ("物品", "烈焰神枪", "0158", "0159"),
    ]:
        story[TYPE_TO_COLLECTION[kind]].append(record(kind, name, first, recent))
    setters = [correction(), correction("阵营", "朱雀族"),
               correction("地点", "小山谷祭台", old="0162", new="0160")]
    setters[2]["证据"] = [{"章节": "0160", "原文": "在小山谷的腹地之中，有一座高大的祭台。"}]
    patch = workload({"设置字段": setters, "合并元素": [
        {"类型": "阵营", "主": "朱雀族", "并入": ["南荒朱雀族"]},
        {"类型": "物品", "主": "火红长枪", "并入": ["烈焰神枪"]},
        {"类型": "地点", "主": "小山谷祭台", "并入": ["无名山谷祭台"]},
    ]})
    return story, patch


def test_real_three_trace_corrections_survive_same_patch_merges_and_archive_originals():
    before, patch = replay_fixture()
    assert validate_set_field_governance_ops(before, patch, "governance") == []
    assert correction_evidence_errors(patch, {157: "朱雀族，凤栖梧！", 160: "在小山谷的腹地之中，有一座高大的祭台。"}, 160) == []
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    for collection, name, old, new in [("角色集", "凤栖梧", "0156", "0157"),
                                       ("阵营集", "朱雀族", "0156", "0157"),
                                       ("地点集", "小山谷祭台", "0162", "0160")]:
        item = next(x for x in after[collection] if x["名称"] == name)
        assert item["详情"]["首次章节"] == new
        archived = [json.loads(entry) for entry in json.loads(item["详情"][CORRECTION_FIELD])]
        assert any(entry["字段"] == "详情/首次章节" and entry["原值"] == old and entry["新值"] == new for entry in archived)
    assert "无名山谷祭台" in after["地点集"][0]["详情"][ARCHIVE_FIELD]
    assert before["地点集"][0]["详情"]["首次章节"] == "0162"


@pytest.mark.parametrize("kind", ["角色", "地点", "线索", "阵营", "物品"])
@pytest.mark.parametrize("field,old,new", [("首次章节", "0156", "0157"), ("最近章节", "0159", "0158")])
def test_all_traceable_non_events_accept_evidenced_trace_corrections(kind, field, old, new):
    before = make_empty_structure()
    before[TYPE_TO_COLLECTION[kind]] = [record(kind, "档案", "0156", "0159")]
    patch = workload({"设置字段": [correction(kind, "档案", field, old, new)]})
    assert validate_set_field_governance_ops(before, patch, "governance") == []
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    assert after[TYPE_TO_COLLECTION[kind]][0]["详情"][field] == new


@pytest.mark.parametrize("missing", ["原值", "理由", "证据"])
def test_trace_correction_requires_full_evidence_protocol_in_validator_and_executor(missing):
    before, _ = replay_fixture()
    op = correction()
    op.pop(missing)
    patch = workload({"设置字段": [op]})
    assert validate_set_field_governance_ops(before, patch, "governance")
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before


@pytest.mark.parametrize("value", ["0000", "157", "00157", 157, "0161"])
def test_invalid_or_future_trace_value_fails_before_execution(value):
    before, _ = replay_fixture()
    patch = workload({"设置字段": [correction(new=value)]})
    assert validate_set_field_governance_ops(before, patch, "governance")
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before


def test_host_scope_rejects_future_trace_setter_even_when_declared_range_is_expanded():
    patch = workload({"设置字段": [correction(new="0161")]})
    patch["章节范围"] = "第156-161章"
    errors = generation_scope_errors(patch, start=156, end=160)
    assert any("/治理操作/设置字段/0/值" in error for error in errors)


@pytest.mark.parametrize("kind,field", [("事件", "时间"), ("事件", "详情/涉及章节"),
                                        ("事件", "详情/首次章节"), ("事件", "详情/最近章节"),
                                        ("角色", "详情/治理档案记录"), ("角色", "详情/治理纠错记录")])
def test_event_chronology_and_host_archives_remain_protected(kind, field):
    before = make_empty_structure()
    before[TYPE_TO_COLLECTION[kind]] = [record(kind, "受保护档案", "0156", "0159")]
    op = correction(kind, "受保护档案")
    op["字段"] = field
    patch = workload({"设置字段": [op]})
    assert validate_set_field_governance_ops(before, patch, "governance")
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before


def test_equal_target_is_not_global_permission_to_ignore_stale_original():
    before, _ = replay_fixture()
    before["角色集"][0]["详情"]["首次章节"] = "0157"
    patch = workload({"设置字段": [correction()]})
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before


def test_same_patch_merge_does_not_authorize_wrong_original_or_unattained_value():
    before, patch = replay_fixture()
    patch["治理操作"]["设置字段"] = [correction("地点", "小山谷祭台", old="0161", new="0160")]
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before
    patch["治理操作"]["设置字段"][0].update(原值="0162", 值="0159")
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before


def test_corrected_trace_range_still_reaches_final_order_validation():
    before, _ = replay_fixture()
    patch = workload({"设置字段": [correction(new="0160")]})
    after, _, warnings = apply_ops(before, patch)
    assert warnings == []
    errors, _ = validate_exact_story_schema(after, mode="governance")
    assert any("首次章节 不得晚于 最近章节" in error for error in errors)

@pytest.mark.parametrize("kind", [None, [], {}, "missing"])
def test_malformed_trace_type_never_crashes_prevalidation(kind):
    from information_preservation import is_trace_field_correction
    op = correction()
    if kind == "missing":
        op.pop("类型")
    else:
        op["类型"] = kind
    assert is_trace_field_correction(op) is False
    patch = workload({"设置字段": [op]})
    assert generation_scope_errors(patch, start=156, end=160) == []
    assert validate_set_field_governance_ops({}, patch, "governance")


@pytest.mark.parametrize("future", [False, True])
def test_nonperiodic_commit_binds_trace_value_to_host_processed_chapters(tmp_path, future):
    """使用真实提交入口；模型自报9999范围不能把未来recent值写进已处理165章的书。"""
    import hashlib
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    from test_information_preservation import story_fixture
    pipeline.init_dirs(tmp_path)
    source = write_completed_chapter(tmp_path, 157)
    source.write_text("证人出现在大殿。", encoding="utf-8")
    write_completed_chapter(tmp_path, 165)
    story = story_fixture()
    story["角色集"][1]["详情"].update(首次章节="0156", 最近章节="0159")
    story_path = tmp_path / "故事结构_增量.json"
    write_json(story_path, story)
    pipeline.ensure_story(tmp_path)
    original = story_path.read_bytes()
    op = correction("角色", "证人", "最近章节", "0159", "9999" if future else "0158")
    op["证据"] = [{"章节": "0157", "原文": "证人出现在大殿。"}]
    patch = workload({"设置字段": [op]})
    if future:
        patch["章节范围"] = "第156-9999章"
    output = tmp_path / "质量治理/按需治理/correction_trace_probe.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(original).hexdigest())
    rc = pipeline.commit_governance(tmp_path, str(output))
    if future:
        assert rc != 0
        assert story_path.read_bytes() == original
        report = output.with_suffix(".repair.md").read_text(encoding="utf-8")
        assert "/治理操作/设置字段/0/值" in report and "已处理章节" in report
    else:
        assert rc == 0
        updated = json.loads(story_path.read_text(encoding="utf-8"))["角色集"][1]["详情"]
        assert updated["最近章节"] == "0158" and "0159" in updated[CORRECTION_FIELD]


@pytest.mark.parametrize("field", ["首次章节", "最近章节"])
def test_top_level_trace_setter_cannot_bypass_evidenced_detail_correction(field):
    """历史详情缺口不能让规范外顶层setter经normalizer回填并绕过追溯证据。"""
    before = make_empty_structure()
    item = record("角色", "旧档案", "0001", "0001")
    item["详情"] = {}
    before["角色集"] = [item]
    patch = workload({"设置字段": [{"类型": "角色", "名称": "旧档案", "字段": field, "值": "0999"}]})
    assert validate_set_field_governance_ops(before, patch, "governance")
    assert generation_scope_errors(patch, start=156, end=160)
    assert correction_evidence_errors(patch, {157: "原文"}, 160)
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before



def test_nonperiodic_commit_rejects_top_level_trace_without_correction_protocol(tmp_path):
    """真实提交必须拒绝规范外顶层键，不允许normalizer把它回填为正式追溯。"""
    import hashlib
    import run_pipeline as pipeline
    from controlled_runtime import publish_generated_artifact
    from test_audit_pack import write_completed_chapter, write_json
    from test_information_preservation import story_fixture
    pipeline.init_dirs(tmp_path)
    write_completed_chapter(tmp_path, 165)
    story = story_fixture()
    story["角色集"][1]["详情"] = {}
    story_path = tmp_path / "故事结构_增量.json"
    write_json(story_path, story)
    pipeline.ensure_story(tmp_path)
    original = story_path.read_bytes()
    patch = workload({"设置字段": [{"类型": "角色", "名称": "证人", "字段": "首次章节", "值": "0999"}]})
    output = tmp_path / "质量治理/按需治理/correction_top_trace_probe.json"
    publish_generated_artifact(tmp_path, output, json.dumps(patch, ensure_ascii=False).encode(),
                               hashlib.sha256(original).hexdigest())
    assert pipeline.commit_governance(tmp_path, str(output)) != 0
    assert story_path.read_bytes() == original
    assert "详情/" in output.with_suffix(".repair.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("archive", ["字段修复原文", "引用修复原文"])
def test_evidence_cannot_replace_existing_host_repair_archives(archive):
    before, _ = replay_fixture()
    original = '["宿主保留的旧字段或引用原文"]'
    before["角色集"][0]["详情"][archive] = original
    op = correction(field=archive, old=original, new='["替换后的内容"]')
    patch = workload({"设置字段": [op]})
    assert validate_set_field_governance_ops(before, patch, "governance")
    after, _, warnings = apply_ops(before, patch)
    assert warnings and after == before
