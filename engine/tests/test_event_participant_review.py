"""空参与成员只触发现有治理中的原文核对，不据此推断人物或新增模型轮次。"""
import copy
import json
from pathlib import Path

import pytest

import run_pipeline as pipeline
import story_schema_rules as rules
import validate_structure
from chronology import chapter_time
from local_json_patch import final_repair_scope
from merge_delta import save_json
from test_audit_pack import empty_story, write_completed_chapter


def event(name="递水", chapter=6, *, participants=None, candidates=None):
    """包含合法标准字段；空成员可表示尚待核实，也可确实无人参与。"""
    item = copy.deepcopy(rules.DEFAULTS["事件集"])
    item.update(名称=name, 分组="00000010-求水救助线索揭示", 时间=chapter_time(chapter),
                标签集=["求水救助+线索揭示+紧张+生存困境"], 介绍="林舟递水给求救者。",
                参与成员=[] if participants is None else participants,
                详情={"涉及章节": f"{chapter:04d}", "动作链条": "林舟递水。"})
    if candidates is not None:
        item["详情"]["关联角色"] = candidates
    return item


def story_with_event(**kwargs):
    story = empty_story()
    role = copy.deepcopy(rules.DEFAULTS["角色集"])
    role.update(名称="林舟", 介绍="本章递水的人。", 分组="求水救助",
                详情={"首次章节": "0006", "最近章节": "0006"})
    story["角色集"] = [role]
    story["事件集"] = [event(**kwargs)]
    return story


def pending(warnings):
    return [warning for warning in warnings if warning.startswith("[内容待核对:")]


@pytest.mark.parametrize("mode", ["process", "governance", "final"])
def test_empty_participants_are_locatable_nonblocking_content_checks(mode):
    story = story_with_event(candidates=["角色:林舟"])
    before = copy.deepcopy(story)
    errors, warnings = rules.validate_exact_story_schema(story, mode=mode)
    assert errors == []
    checks = pending(warnings)
    assert len(checks) == 1
    assert "/事件集/0/参与成员" in checks[0]
    assert all(value in checks[0] for value in ("递水", "0006", "角色:林舟", "候选"))
    assert "[质量字段:" not in checks[0]
    assert story == before


def test_actorless_environment_event_remains_legal_without_invented_people():
    story = story_with_event()
    item = story["事件集"][0]
    item.update(名称="夜间降雪", 介绍="夜间山谷降雪。", 详情={"涉及章节": "0006", "天气": "降雪"})
    errors, warnings = rules.validate_exact_story_schema(story, mode="final")
    assert errors == []
    assert len(pending(warnings)) == 1  # 待核对不等于内容错误，不能据此自动填人。
    assert item["参与成员"] == []
    assert "林舟" not in pending(warnings)[0]


def test_nonempty_participants_do_not_produce_the_empty_value_check():
    story = story_with_event(participants=["林舟"], candidates=["角色:林舟"])
    assert pending(rules.validate_exact_story_schema(story, mode="final")[1]) == []


@pytest.mark.parametrize("value", [None, "[]", {}, ["林舟"]])
def test_only_a_real_empty_array_receives_the_content_reminder(value):
    story = story_with_event()
    story["事件集"][0]["参与成员"] = value
    assert pending(rules.validate_exact_story_schema(story, mode="process")[1]) == []


def test_cycle_checks_only_explicit_chapter_intersection_and_preserves_global_pointers():
    story = story_with_event(candidates=["角色:林舟"])
    story["事件集"].insert(0, event("旧章对话", 1))
    story["事件集"].append(event("无法定位的事件", 20))
    story["事件集"][-1]["详情"].pop("涉及章节")
    story["事件集"].append(event("未来事件", 9))
    collect = getattr(rules, "event_participant_review_warnings")
    before = copy.deepcopy(story)
    scoped = collect(story, chapter_range=(6, 7))
    assert len(scoped) == 1 and "/事件集/1/参与成员" in scoped[0]
    assert "旧章对话" not in scoped[0] and "未来事件" not in scoped[0]
    assert len(collect(story)) == 4  # 全库报告保留无法定位项，不从名称猜章节。
    assert story == before


def test_existing_role_candidates_are_hints_and_not_canonicalized_or_applied():
    story = story_with_event(candidates=["角色:路过的陌生人", "角色:林舟"])
    collect = getattr(rules, "event_participant_review_warnings")
    checks = collect(story, chapter_range=(6, 6))
    assert "角色:路过的陌生人" in checks[0]
    assert story["事件集"][0]["参与成员"] == []
    assert story["事件集"][0]["详情"]["关联角色"] == ["角色:路过的陌生人", "角色:林舟"]


def test_warning_does_not_open_final_repair_scope(tmp_path):
    story = story_with_event(candidates=["角色:林舟"])
    path = tmp_path / "story.json"
    save_json(path, story)
    errors, warnings = rules.validate_exact_story_schema(story, mode="final")
    assert pending(warnings)
    assert final_repair_scope(path, "\n".join(errors + warnings), all_fields=True) == {}


def test_validator_reports_structural_success_and_pending_content_without_protocol_change(tmp_path, monkeypatch, capsys):
    path, report = tmp_path / "story.json", tmp_path / "report.json"
    save_json(path, story_with_event(candidates=["角色:林舟"]))
    monkeypatch.setattr("sys.argv", ["validate_structure", "--strict", "--report-json", str(report), str(path)])
    assert validate_structure.main() == 0
    output = capsys.readouterr().out
    assert "结构校验通过" in output and "内容待核对 1 项" in output
    assert "不代表内容完整" in output
    state = json.loads(report.read_text(encoding="utf-8"))
    assert set(state) == {"passed", "mode", "errors", "warnings"}
    assert state["passed"] is True and state["errors"] == [] and len(pending(state["warnings"])) == 1


def prepare_cycle(project, mode):
    """用真实任务包降载路径触发四种材料视图，不调用模型。"""
    chapter = write_completed_chapter(project, 6)
    story = story_with_event(candidates=["角色:林舟"])
    if mode == "story_index_relevant":
        story["其他事项集"] = [{"名称": "远古档案", "介绍": "旧档案" * 30000}]
    elif mode == "story_index":
        story["事件集"][0]["介绍"] += "本章已有完整动作。" * 18000
        delta_path = pipeline.artifact_paths(project, chapter)["delta"]
        save_json(delta_path, {"章节": "0006", "新增元素": {},
                             "修改元素": {"事件集": [{"名称": "递水"}]}})
    elif mode == "tiered_evidence_tree":
        chapter.write_text("完整原文证据。" * 20000, encoding="utf-8")
    save_json(project / "故事结构_增量.json", story)
    return chapter


@pytest.mark.parametrize("mode", ["full_story", "story_index_relevant", "story_index", "tiered_evidence_tree"])
def test_every_existing_cycle_material_route_receives_the_same_concrete_check(tmp_path, mode):
    chapter = prepare_cycle(tmp_path, mode)
    story = json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8"))
    before = chapter.read_bytes()
    pack = pipeline.write_audit_pack(tmp_path, 6, 6, max_context_chars=18000)
    assert pack is not None
    status = json.loads(pack.with_suffix(".status.json").read_text(encoding="utf-8"))
    assert status["context"]["render_mode"] == mode
    if mode == "tiered_evidence_tree":
        route = json.loads(pack.with_suffix(".governance.json").read_text(encoding="utf-8"))
        history = pack.with_suffix(".history.md")
        assert str(history) in route["contract"]
        assert any(Path(row[0]) == history.resolve() for row in route["sources"])
        material = history.read_text(encoding="utf-8")
    else:
        material = pack.read_text(encoding="utf-8")
    checks = getattr(rules, "event_participant_review_warnings")(story, chapter_range=(6, 6))
    assert checks and all(check in material for check in checks)
    assert str(chapter) in material
    contract = route["contract"] if mode == "tiered_evidence_tree" else material
    for instruction in ("仅提及", "受事", "心理", "无人参与", "保留[]", "候选"):
        assert instruction in contract
    assert chapter.read_bytes() == before


def test_pending_checks_are_capacity_accounted_and_never_truncated(tmp_path):
    chapter = write_completed_chapter(tmp_path, 6)
    story = story_with_event()
    candidates = ["角色:" + "原有关联候选" * 7000 + "尾部保留"]
    story["事件集"][0]["详情"]["关联角色"] = candidates
    save_json(tmp_path / "故事结构_增量.json", story)
    pack = pipeline.write_audit_pack(tmp_path, 6, 6, max_context_chars=18000)
    status = json.loads(pack.with_suffix(".status.json").read_text(encoding="utf-8"))
    assert status["context"]["render_mode"] == "tiered_evidence_tree"
    history = pack.with_suffix(".history.md").read_text(encoding="utf-8")
    check = getattr(rules, "event_participant_review_warnings")(story, chapter_range=(6, 6))[0]
    assert check in history and candidates[0] in history
    route = json.loads(pack.with_suffix(".governance.json").read_text(encoding="utf-8"))
    assert candidates[0] not in route["contract"]  # 固定任务约束不能膨胀成不可分片的整份清单。
    assert any(Path(row[0]) == chapter.resolve() for row in route["sources"])


def test_nonempty_and_out_of_cycle_events_do_not_become_a_whole_book_check(tmp_path):
    chapter = write_completed_chapter(tmp_path, 6)
    story = story_with_event(participants=["林舟"])
    story["事件集"].insert(0, event("旧章空成员", 1))
    save_json(tmp_path / "故事结构_增量.json", story)
    pack = pipeline.write_audit_pack(tmp_path, 6, 6)
    text = pack.read_text(encoding="utf-8")
    assert "[内容待核对:" not in text
    assert chapter.read_text(encoding="utf-8") in text


def test_final_success_message_is_limited_to_structure(tmp_path, capsys):
    save_json(tmp_path / "故事结构_草稿.json", story_with_event())
    assert pipeline.cmd_validate_final(tmp_path) == 0
    assert "最终结构校验通过" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["full_story", "tiered_evidence_tree"])
def test_audit_reuses_one_story_snapshot_for_index_and_pending_checks(tmp_path, monkeypatch, mode):
    prepare_cycle(tmp_path, mode)
    source = tmp_path / "故事结构_增量.json"
    loads = []
    original = pipeline.load_json

    def read(path, *args, **kwargs):
        if Path(path) == source:
            loads.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "load_json", read)
    assert pipeline.write_audit_pack(tmp_path, 6, 6, max_context_chars=18000) is not None
    assert len(loads) == 1


def test_existing_periodic_worker_consumes_hint_once_and_can_keep_unresolved_empty(tmp_path, monkeypatch):
    prepare_cycle(tmp_path, "full_story")
    calls = []

    def worker(command, project, kind, pack, output, *args, **kwargs):
        calls.append(kind)
        assert "[内容待核对:/事件集/0/参与成员]" in pack.read_text(encoding="utf-8")
        save_json(output, {"章节范围": "第006章-第006章", "新增元素": {}, "修改元素": {},
                           "治理类型": "no_change", "质量说明": "逐项核对，仍无法确认身份，保留空数组待确认。",
                           "证据范围": ["第006章"]})
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_periodic_governance(tmp_path, 6, 6, audit_command="offline", worker_loop="continuous") == 0
    assert calls == ["audit_correction"]
    assert json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8"))["事件集"][0]["参与成员"] == []
