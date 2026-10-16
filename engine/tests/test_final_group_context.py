"""Final group context is exact optional reference data, never mutation authority."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import fault_recovery
from context_budget import estimate_tokens
from fault_recovery import _inline_final_scope
from merge_delta import save_json

HEADER = "## 已有格式合法的分组原值及使用档案"


def block(prompt, header):
    start = prompt.index("```json\n", prompt.index(header)) + len("```json\n")
    return json.JSONDecoder().raw_decode(prompt, start)[0]


def group_case(tmp_path):
    event = {"名称": "本批待补", "分组": "", "标签集": [], "介绍": "完整旧介绍不能删。",
             "详情": {"涉及章节": "0001", "完整历史": "引号“甲”与换行\n条件均完整保留。"}}
    story = {"角色集": [{"名称": "甲", "分组": "00000010-人物原组"}], "事件集": [event]}
    for name, group in [("甲\"/~", "00000010-已有一段"), ("乙", "00000010-已有一段"),
                        ("丙", " 00000020-已有二段 "), ("空", ""), ("短号", "0010-短号"),
                        ("空名", "00000030-"), ("纯编号", "00000040-1234"),
                        ("加号", "00000050-一+二"), ("过长", "00000060-" + "长" * 41),
                        ("非文本", True)]:
        item = copy.deepcopy(event); item.update(名称=name, 分组=group); story["事件集"].append(item)
    reference = tmp_path / "reference.json"
    save_json(reference, story)
    source = tmp_path / "原文拆解/第001章_证据.md"
    source.parent.mkdir(); source.write_bytes("完整原文第一行。\r\n最后一句原文不能被预算挤走。\r\n".encode())
    return reference, source, story


def test_group_registry_keeps_exact_raw_values_all_paths_names_and_source_hash(tmp_path):
    reference, source, story = group_case(tmp_path)
    before = reference.read_bytes()
    candidate = tmp_path / "candidate.json"
    changed = copy.deepcopy(story); changed["事件集"][0]["分组"] = "00000030-候选新段不得变成旧锚点"
    save_json(candidate, changed); candidate_before = candidate.read_bytes()
    prompt = _inline_final_scope("只修分组", reference, ["/事件集/0/分组"], candidate=candidate, source_paths=[source])
    payload = block(prompt, "## 本批完整档案")
    data = block(prompt, HEADER)
    assert data["reference_sha256"] == payload["reference_sha256"] == hashlib.sha256(before).hexdigest()
    assert data["groups"] == [
        {"value": "00000010-已有一段", "records": [{"path": "/事件集/1", "name": "甲\"/~"}, {"path": "/事件集/2", "name": "乙"}]},
        {"value": " 00000020-已有二段 ", "records": [{"path": "/事件集/3", "name": "丙"}]}]
    assert payload["records"] == [{"path": "/事件集/0", "value": story["事件集"][0]}]
    assert payload["actual_new_values"] == {"/事件集/0/分组": changed["事件集"][0]["分组"]}
    assert "不证明剧情分类或全局段号分配正确" in prompt and "不作为原文事实依据" in prompt
    assert "用于定位既有分组及相邻事件" in prompt and "剧情归属仍须核对原文" in prompt
    assert "不得修改本次范围外的分组或段号" in prompt
    assert "不得据此猜分组、生成段号或改号" not in prompt
    assert source.read_bytes().decode().replace("\r\n", "\n") in prompt
    assert reference.read_bytes() == before and candidate.read_bytes() == candidate_before


def test_group_registry_uses_the_same_single_reference_byte_read(tmp_path, monkeypatch):
    reference, source, story = group_case(tmp_path)
    before = reference.read_bytes(); original = Path.read_bytes; reads = 0
    changed = copy.deepcopy(story); changed["事件集"][1]["分组"] = "00000990-后来的版本"
    def read(path):
        nonlocal reads
        result = original(path)
        if path == reference:
            reads += 1
            save_json(reference, changed)
        return result
    monkeypatch.setattr(Path, "read_bytes", read)
    prompt = _inline_final_scope("原任务", reference, ["/事件集/0/分组"], source_paths=[source])
    data = block(prompt, HEADER)
    assert reads == 1
    assert data["reference_sha256"] == hashlib.sha256(before).hexdigest()
    assert data["groups"][0]["value"] == "00000010-已有一段"
    assert "00000990-后来的版本" not in prompt


@pytest.mark.parametrize("pointer", ["/事件集/0/标签集", "/事件集/0/介绍", "/事件集/0/详情/分组", "/角色集/0/分组"])
def test_registry_is_only_for_exact_event_group_scope(tmp_path, pointer):
    reference, source, _ = group_case(tmp_path)
    before = reference.read_bytes()
    prompt = _inline_final_scope("原任务", reference, [pointer], source_paths=[source])
    assert "## 本批完整档案" in prompt and HEADER not in prompt
    assert reference.read_bytes() == before


def test_optional_registry_cannot_displace_complete_records_or_raw_body(tmp_path, monkeypatch):
    reference, source, story = group_case(tmp_path)
    full = _inline_final_scope("原任务", reference, ["/事件集/0/分组"], source_paths=[source])
    assert HEADER in full
    baseline = full.split("\n" + HEADER, 1)[0]
    budget = (estimate_tokens(baseline) * 4 + 2) // 3
    assert estimate_tokens(full) > budget * 3 // 4 >= estimate_tokens(baseline)
    monkeypatch.setattr(fault_recovery.ContextBudget, "from_env", classmethod(lambda cls: SimpleNamespace(usable_input=budget)))
    actual = _inline_final_scope("原任务", reference, ["/事件集/0/分组"], source_paths=[source])
    assert actual == baseline and HEADER not in actual
    assert block(actual, "## 本批完整档案")["records"][0]["value"] == story["事件集"][0]
    assert source.read_bytes().decode().replace("\r\n", "\n") in actual
    assert "本批完整档案未内联" not in actual


def test_empty_legal_group_set_does_not_add_or_invent_registry(tmp_path):
    reference, source, story = group_case(tmp_path)
    story["事件集"] = story["事件集"][:1];save_json(reference, story)
    prompt = _inline_final_scope("原任务", reference, ["/事件集/0/分组"], source_paths=[source])
    assert HEADER not in prompt and "## 本批完整档案" in prompt
    assert source.read_bytes().decode().replace("\r\n", "\n") in prompt



def test_registry_does_not_mark_whole_reference_as_provided_or_change_schema(tmp_path, monkeypatch):
    # Keep this standalone test independent of other tests importing engine/tools.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from worker_context import snapshot_context, ContextReader
    from recovery_contract import RECOVERY_SCHEMAS
    reference, source, _ = group_case(tmp_path)
    original_schemas = copy.deepcopy(RECOVERY_SCHEMAS)
    prompt = _inline_final_scope("原任务", reference, ["/事件集/0/分组"], source_paths=[source])
    pack = tmp_path / "patch.md";pack.write_text(prompt, encoding="utf-8")
    pack.with_suffix(".context-inputs.json").write_text(json.dumps([str(reference), str(source)]), encoding="utf-8")
    frozen = tmp_path / "frozen";frozen.mkdir()
    manifest = snapshot_context(tmp_path, pack, [], frozen, inline_prompt=prompt)
    reader = ContextReader(manifest)
    files = {Path(x["source"]).name: x for x in reader.files}
    assert files[source.name]["provided_in_prompt"] is True
    assert files["reference.json"].get("provided_in_prompt") is not True
    response = reader.read("reference.json", offset=0, limit=100)
    assert response["text"] == reference.read_text(encoding="utf-8")[:100]
    assert response["next_offset"] == 100 and response.get("provided_in_prompt") is not True
    assert RECOVERY_SCHEMAS == original_schemas
