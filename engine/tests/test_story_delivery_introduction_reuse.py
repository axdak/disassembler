"""Offline regressions for preserving a trusted delivery synopsis."""

import hashlib
import json
import threading

import pytest

import story_delivery
from local_json_patch import final_repair_scope
from merge_delta import save_json
from project_lock import project_write_lock
from story_schema_rules import validate_exact_story_schema


def story(description="周期治理维护的当前简介。", title="测试故事"):
    """Keep the delivery fixture complete while varying only the intro."""
    return {
        "介绍": {"标题": title, "描述": description},
        **{name: [] for name in (
            "角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"
        )},
    }


def prepare_files(project, value, *, source_receipt=False):
    """Write real delivery inputs; the caller may then make a repaired draft."""
    source = project / "故事结构_增量.json"
    draft = project / "故事结构_草稿.json"
    root = project / "质量治理/最终审计/交付"
    save_json(source, value)
    draft.write_bytes(source.read_bytes())
    root.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_receipt:
        save_json(root / "source.json", {"source": signature})
    return source, draft, root, signature


def fake_generation(calls):
    """Supply complete offline tree responses if generation is necessary."""
    def worker(command, project, kind, pack, output, **options):
        calls.append(kind)
        indexing = "content 必须为 null" in pack.read_text(encoding="utf-8")
        save_json(output, {
            "overview": "已完成范围的完整证据索引",
            "content": None if indexing else {"标题": "有据标题", "描述": "缺失后补齐的正式简介。"},
        })
        return 0
    return worker


@pytest.mark.parametrize("trust", ["source_receipt", "identical_bytes"])
@pytest.mark.parametrize("command", ["", "offline"])
def test_trusted_description_needs_no_model_and_keeps_draft_bytes(tmp_path, trust, command):
    """Regenerating an accepted synopsis would overwrite user-visible content."""
    source, draft, root, signature = prepare_files(
        tmp_path, story(), source_receipt=trust == "source_receipt"
    )
    if trust == "source_receipt":
        # A valid receipt also covers normalization and accepted draft-only edits.
        value = json.loads(draft.read_text(encoding="utf-8"))
        value["介绍"]["描述"] = "已经定点修正过的当前简介。"
        save_json(draft, value)
    before = draft.read_bytes()
    calls = []

    assert story_delivery.finish_introduction(tmp_path, command, fake_generation(calls)) == 0
    assert calls == []
    assert draft.read_bytes() == before
    assert source.read_bytes() != b""
    assert json.loads((root / "introduction.json").read_text(encoding="utf-8")) == {
        "source": signature, "introduction": json.loads(before)["介绍"]
    }


def test_missing_title_stays_a_strict_local_repair_without_rewriting_description(tmp_path):
    """A missing title must not turn synopsis preservation into final acceptance."""
    _, draft, _, _ = prepare_files(tmp_path, story(title=""))
    before = draft.read_bytes()
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "", fake_generation(calls)) == 0
    assert calls == []
    assert draft.read_bytes() == before
    errors, _ = validate_exact_story_schema(json.loads(before), mode="final")
    assert any("[质量字段:/介绍/标题]" in error for error in errors)
    assert list(final_repair_scope(draft, "\n".join(errors))) == ["/介绍/标题"]


def test_repaired_draft_resume_keeps_current_intro_and_other_repaired_fields(tmp_path):
    """A current source receipt must not restore the older process synopsis."""
    _, draft, root, signature = prepare_files(tmp_path, story("旧版简介。"), source_receipt=True)
    save_json(root / "introduction.json", {"source": signature, "introduction": story("旧版简介。")["介绍"]})
    value = json.loads(draft.read_text(encoding="utf-8"))
    value["介绍"]["描述"] = "玉佩能力若暴露才会招致夺宝的有据简介。"
    value["其他事项集"] = [{
        "名称": "已补齐档案", "分组": "设定", "别名": [], "标签集": [],
        "介绍": "局部修复后保留的正确内容。", "详情": {},
    }]
    save_json(draft, value)
    before = draft.read_bytes()
    calls = []

    for _ in range(2):
        assert story_delivery.finish_introduction(tmp_path, "offline", fake_generation(calls)) == 0
        assert draft.read_bytes() == before
    assert calls == []
    assert json.loads((root / "introduction.json").read_text(encoding="utf-8"))["introduction"] == value["介绍"]


@pytest.mark.parametrize("receipt_state", ["missing", "stale"])
def test_untrusted_different_draft_does_not_skip_generation(tmp_path, receipt_state):
    """A nonempty stale draft alone cannot authorize a successful fast return."""
    _, draft, root, _ = prepare_files(tmp_path, story("当前来源简介。"))
    value = json.loads(draft.read_text(encoding="utf-8"))
    value["介绍"]["描述"] = "没有当前来源凭据的旧草稿。"
    save_json(draft, value)
    if receipt_state == "stale":
        save_json(root / "source.json", {"source": "outdated-source"})
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "offline", fake_generation(calls)) == 0
    assert calls
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"]["描述"] == "缺失后补齐的正式简介。"


def test_existing_intro_receipt_shortcut_still_reuses_completed_generation(tmp_path):
    """The prior source-bound generation receipt remains a valid resume point."""
    _, draft, root, signature = prepare_files(tmp_path, story("当前来源简介。"))
    value = json.loads(draft.read_text(encoding="utf-8"))
    value["介绍"]["描述"] = "已经生成并发布过的正式简介。"
    save_json(draft, value)
    save_json(root / "introduction.json", {"source": signature, "introduction": value["介绍"]})
    before = draft.read_bytes()
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "", fake_generation(calls)) == 0
    assert calls == []
    assert draft.read_bytes() == before


@pytest.mark.parametrize("description", ["", " \t\n ", None, 42])
def test_missing_blank_or_wrong_type_description_keeps_full_generation(tmp_path, description):
    """Only a nonempty string can reuse a source-bound synopsis."""
    _, draft, _, _ = prepare_files(tmp_path, story(description), source_receipt=True)
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "offline", fake_generation(calls)) == 0
    assert calls
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"] == {
        "标题": "有据标题", "描述": "缺失后补齐的正式简介。"
    }


def test_reuse_receipt_is_written_under_writer_lock_without_draft_publication(tmp_path, monkeypatch):
    """Receipt publication must exclude a competing accepted project writer."""
    _, draft, root, signature = prepare_files(tmp_path, story())
    before = draft.read_bytes()
    observed = []
    original_save = story_delivery.save_json

    def inspect_receipt(path, value):
        if path == root / "introduction.json":
            def competing_writer():
                try:
                    with project_write_lock(tmp_path, timeout=0.02):
                        observed.append("unlocked")
                except TimeoutError:
                    observed.append("locked")
            contender = threading.Thread(target=competing_writer)
            contender.start()
            contender.join(timeout=1)
            assert not contender.is_alive()
        return original_save(path, value)

    monkeypatch.setattr(story_delivery, "save_json", inspect_receipt)
    assert story_delivery.finish_introduction(tmp_path, "", fake_generation([])) == 0
    assert observed == ["locked"]
    assert draft.read_bytes() == before
    assert json.loads((root / "introduction.json").read_text(encoding="utf-8"))["source"] == signature


@pytest.mark.parametrize("description", [42, ["不是", "字符串简介"]])
def test_preparation_cannot_promote_wrong_type_source_into_reusable_description(tmp_path, description):
    """Normalization must not turn malformed source data into an accepted synopsis."""
    source = tmp_path / "故事结构_增量.json"
    save_json(source, story(description))
    assert story_delivery.prepare_delivery(tmp_path) == 0
    draft = tmp_path / "故事结构_草稿.json"
    normalized = json.loads(draft.read_text(encoding="utf-8"))["介绍"]["描述"]
    assert isinstance(normalized, str) and normalized.strip()
    source_after_prepare = source.read_bytes()
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "offline", fake_generation(calls)) == 0
    assert calls
    assert json.loads(draft.read_text(encoding="utf-8"))["介绍"]["描述"] == "缺失后补齐的正式简介。"
    assert source.read_bytes() == source_after_prepare


def test_completed_intro_receipt_preserves_repaired_draft_with_wrong_type_source(tmp_path):
    """An exact current completion receipt remains authoritative after local repair."""
    source = tmp_path / "故事结构_增量.json"
    save_json(source, story(42))
    assert story_delivery.prepare_delivery(tmp_path) == 0
    draft = tmp_path / "故事结构_草稿.json"
    value = json.loads(draft.read_text(encoding="utf-8"))
    value["介绍"]["描述"] = "已有原文依据并完成局部修正的正式简介。"
    save_json(draft, value)
    root = tmp_path / "质量治理/最终审计/交付"
    save_json(root / "introduction.json", {
        "source": hashlib.sha256(source.read_bytes()).hexdigest(), "introduction": value["介绍"]
    })
    before = draft.read_bytes()
    calls = []

    assert story_delivery.finish_introduction(tmp_path, "", fake_generation(calls)) == 0
    assert calls == []
    assert draft.read_bytes() == before

