"""Offline complete-source regressions for the existing final field owner."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from context_budget import capacity_scope
from fault_recovery import REVIEW_CHECKS, _inline_final_scope, digest, recover_artifact
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from story_schema_rules import DEFAULTS, make_empty_structure
from test_final_scope_context import INLINE_HEADER, inline_payload


def reference_file(root, *, chapters="0001，0003"):
    """Use explicit trace metadata and deliberately misleading free text."""
    record = {"名称": "第002章并非来源", "标签集": [], "介绍": "第004章也不是来源。",
              "详情": {"涉及章节": chapters, "自由说明": "第005章，金额100元"}}
    path = root / "reference.json"
    save_json(path, {"事件集": [record], "介绍": {"标题": "旧题", "描述": "已有简介"},
                     "角色集": [{"名称": "甲", "介绍": "", "详情": {"首次章节": "0001", "最近章节": "0003"}}]})
    return path


def source_file(root, number, body, *, suffix="原文"):
    """Persist bytes exactly, including CRLF and trailing source whitespace."""
    path = root / "原文拆解" / f"第{number:03d}章_{suffix}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def render(reference, sources, pointers=None):
    return _inline_final_scope("原有额外语义约束必须保留。", reference,
                               pointers or ["/事件集/0/标签集"], source_paths=sources)


def test_complete_multichapter_sources_are_deduplicated_without_losing_crlf_tail(tmp_path):
    reference = reference_file(tmp_path)
    first = "# 第一章\r\n\r\n完整首句。\r\n实体&#039;字&#039;与星星🌟。\r\n尾句一  \r\n\r\n".encode()
    third = "# 第三章\n全文三，不能节选。\n独有尾句三\n".encode()
    one, three = source_file(tmp_path, 1, first), source_file(tmp_path, 3, third)
    prompt = render(reference, [three, one, one], ["/事件集/0/标签集", "/事件集/0/介绍"])
    assert first.decode().replace("\r\n", "\n") in prompt
    assert third.decode() in prompt
    assert prompt.count("尾句一") == prompt.count("独有尾句三") == 1
    assert hashlib.sha256(first).hexdigest() in prompt
    assert hashlib.sha256(third).hexdigest() in prompt
    assert "基础证据" in prompt and "历史" in prompt and "查证" in prompt
    assert len(inline_payload(prompt)["records"]) == 1
    assert one.read_bytes() == first and three.read_bytes() == third


@pytest.mark.parametrize("same_bytes", [True, False])
def test_multiple_registered_paths_for_one_sequence_never_choose_conflicting_first(tmp_path, same_bytes):
    reference = reference_file(tmp_path, chapters="0001")
    body = "# 一\n同章完整内容甲\n尾甲\n".encode()
    first = source_file(tmp_path, 1, body, suffix="甲")
    second = source_file(tmp_path, 1, body if same_bytes else "# 一\n冲突版本乙\n尾乙\n".encode(), suffix="乙")
    prompt = render(reference, [second, first])
    if same_bytes:
        assert prompt.count("同章完整内容甲") == 1
        assert hashlib.sha256(body).hexdigest() in prompt
    else:
        assert "同章完整内容甲" not in prompt and "冲突版本乙" not in prompt
        assert "冲突" in prompt and "0001" in prompt and "完整材料" in prompt


def test_missing_chapter_and_unregistered_neighbor_remain_explicit_mcp_work(tmp_path):
    reference = reference_file(tmp_path)
    one = source_file(tmp_path, 1, "# 一\n已登记完整原文甲\n".encode())
    source_file(tmp_path, 2, "不可猜中间章或相邻章".encode())
    source_file(tmp_path, 3, "存在但未登记原文三".encode())
    prompt = render(reference, [one])
    assert "已登记完整原文甲" in prompt
    assert "不可猜中间章" not in prompt and "存在但未登记原文三" not in prompt
    assert "0003" in prompt and "未登记" in prompt and "完整材料" in prompt


@pytest.mark.parametrize("pointer", ["/角色集/0/介绍", "/介绍/描述"])
def test_non_event_first_recent_and_root_intro_never_imply_whole_book_sources(tmp_path, pointer):
    reference = reference_file(tmp_path)
    one = source_file(tmp_path, 1, "不应自动读取的人物首次原文".encode())
    three = source_file(tmp_path, 3, "不应自动读取的人物最近原文".encode())
    prompt = render(reference, [one, three], [pointer])
    assert "不应自动读取" not in prompt
    assert inline_payload(prompt)["selected_fields"] == [pointer]


def test_event_name_description_and_history_numbers_cannot_supply_missing_trace(tmp_path):
    reference = reference_file(tmp_path, chapters="")
    sources = [source_file(tmp_path, n, f"不应猜出的第{n}段正文".encode()) for n in (2, 4, 5, 100)]
    prompt = render(reference, sources)
    assert "不应猜出的" not in prompt
    assert INLINE_HEADER in prompt


def test_full_source_group_exceeding_budget_falls_back_without_partial_chapters(tmp_path):
    reference = reference_file(tmp_path, chapters="0001")
    one = source_file(tmp_path, 1, ("# 一\n" + "甲" * 1000 + "完整尾甲\n").encode())
    three = source_file(tmp_path, 3, ("# 三\n" + "乙" * 3000 + "完整尾乙\n").encode())
    with capacity_scope(dict(model_input_tokens=10000, model_context_tokens=12000,
                             model_output_tokens=2000, model_reserve_tokens=2000)):
        assert "完整尾甲" in render(reference, [one])
        document = json.loads(reference.read_bytes())
        document["事件集"][0]["详情"]["涉及章节"] = "0001，0003"
        save_json(reference, document)
        prompt = render(reference, [one, three])
    assert INLINE_HEADER not in prompt
    assert "完整尾甲" not in prompt and "完整尾乙" not in prompt
    assert "未内联" in prompt and "完整文件" in prompt


def test_source_hash_and_full_text_come_from_the_same_byte_read(tmp_path, monkeypatch):
    reference = reference_file(tmp_path, chapters="0001")
    initial, changed = "# 一\n同次读取初始正文\n".encode(), "# 一\n之后读取的错误正文\n".encode()
    source = source_file(tmp_path, 1, changed)
    actual_read = Path.read_bytes
    reads = []

    def alternating(path):
        if path == source:
            reads.append(path)
            return initial if len(reads) == 1 else changed
        return actual_read(path)

    monkeypatch.setattr(Path, "read_bytes", alternating)
    prompt = render(reference, [source, source])
    assert len(reads) == 1
    assert initial.decode() in prompt and changed.decode() not in prompt
    assert hashlib.sha256(initial).hexdigest() in prompt


def test_existing_context_marks_complete_inline_sources_as_already_provided(tmp_path):
    from test_worker_context import snapshot_context
    reference = reference_file(tmp_path, chapters="0001")
    source = source_file(tmp_path, 1, "# 一\r\n完整&#039;证据&#039;🌟\r\n尾证据\r\n".encode())
    other = source_file(tmp_path, 2, "# 二\n仍可按需查询的其他章节\n".encode())
    prompt = render(reference, [source, other])
    pack = tmp_path / "task.md"
    pack.write_text(prompt, encoding="utf-8")
    save_json(pack.with_suffix(".context-inputs.json"), [str(source), str(other)])
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    manifest = json.loads(snapshot_context(tmp_path, pack, [], snapshots, inline_prompt=prompt).read_text(encoding="utf-8"))
    registered = {Path(item["source"]): item for item in manifest["files"]}
    assert registered[source]["provided_in_prompt"] is True
    assert not registered[other].get("provided_in_prompt", False)


def event_case(tmp_path):
    """Exercise the real repair owner while substituting only external workers."""
    story = make_empty_structure()
    story["介绍"] = {"标题": "测试故事", "描述": "已核实的背景。"}
    event = copy.deepcopy(DEFAULTS["事件集"])
    event.update(名称="未完救援", 介绍="甲正在救援，尚未完成。")
    event["详情"] = {"涉及章节": "0001", "提取理由": "保留行动未完成边界。"}
    story["事件集"] = [event]
    output, pack = tmp_path / "故事结构_草稿.json", tmp_path / "repair.md"
    save_json(output, story)
    pack.write_text("只补本批标签，保留尚未完成的条件。", encoding="utf-8")
    source = source_file(tmp_path, 1, "# 一\r\n甲正在救援，行动尚未完成。\r\n冻结完整尾句\r\n".encode())
    calls, prompts, published = [], [], []

    def validate(root):
        candidate = json.loads((root / output.name).read_bytes())
        return (0, "") if candidate["事件集"][0]["标签集"] else (1, "[质量字段:/事件集/0/标签集] 缺四维标签")

    def worker(command, root, kind, task, expected, *args, **options):
        calls.append(kind)
        prompt = task.read_text(encoding="utf-8")
        prompts.append((kind, prompt))
        assert "冻结完整尾句" in prompt and "行动尚未完成" in prompt
        if kind == "recovery_patch":
            save_json(expected, {"base_sha256": digest(root / "reference.json"), "changes": [
                {"path": "/事件集/0/标签集", "value": ["救援+情节推进+紧张+生死危机"]}], "moves": []})
        elif kind == "recovery_review":
            save_json(expected, {"passed": True, "candidate_sha256": digest(root / output.name),
                                 "checks": dict.fromkeys(REVIEW_CHECKS, True), "issues": [], "reason": "核对本批原文与实际变化。"})
        else:
            pytest.fail(kind)
        return 0

    def publish(path):
        published.append(path.read_bytes())
        output.write_bytes(path.read_bytes())
        return 0

    options = dict(command="local-fake", worker=worker, validate=validate, publish=publish, strategies=1, retries=0)
    return SimpleNamespace(root=tmp_path, output=output, pack=pack, source=source,
                           calls=calls, prompts=prompts, published=published, options=options)


def run(case):
    return recover_artifact(case.root, "repair_final", case.pack, case.output, **case.options)


def test_actual_owner_uses_only_frozen_sources_for_patch_and_independent_review(tmp_path):
    case = event_case(tmp_path)
    assert run(case) == 0
    assert case.calls == ["recovery_patch", "recovery_review"] and len(case.published) == 1
    for _, prompt in case.prompts:
        assert hashlib.sha256(case.source.read_bytes()).hexdigest() in prompt


def test_live_source_edit_cannot_replace_frozen_review_evidence_or_publish(tmp_path):
    case = event_case(tmp_path)
    worker = case.options["worker"]

    def concurrent(command, root, kind, task, expected, *args, **options):
        result = worker(command, root, kind, task, expected, *args, **options)
        if kind == "recovery_patch":
            case.source.write_bytes("活动源被独立修改，不能作为旧批证据。".encode())
        return result

    case.options["worker"] = concurrent
    assert run(case) == REPAIR_EXHAUSTED
    assert case.calls == ["recovery_patch", "recovery_review"] and not case.published
    assert all("活动源被独立修改" not in prompt for _, prompt in case.prompts)


@pytest.mark.parametrize("stage", ["generated", "validated"])
def test_source_inline_survives_existing_generated_and_validated_resume(tmp_path, stage):
    case = event_case(tmp_path)
    worker, validate = case.options["worker"], case.options["validate"]
    interrupted = False

    def validation(root):
        nonlocal interrupted
        result = validate(root)
        if stage == "generated" and result == (0, "") and not interrupted:
            interrupted = True
            raise RuntimeError("生成后中断")
        return result

    def wrapped(command, root, kind, task, expected, *args, **options):
        nonlocal interrupted
        if stage == "validated" and kind == "recovery_review" and not interrupted:
            interrupted = True
            return 130
        return worker(command, root, kind, task, expected, *args, **options)

    case.options.update(worker=wrapped, validate=validation)
    if stage == "generated":
        with pytest.raises(RuntimeError, match="生成后中断"):
            run(case)
    else:
        assert run(case) == 130
    assert run(case) == 0
    assert case.calls.count("recovery_patch") == 1 and len(case.published) == 1
    assert all("冻结完整尾句" in prompt for _, prompt in case.prompts)
