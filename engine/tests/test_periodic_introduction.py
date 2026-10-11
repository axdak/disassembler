"""Offline acceptance of cycle/tail synopsis replacement and complete evidence."""
import json
import re

import pytest

import run_pipeline as pipeline
from capacity_tree import CapacityTree
from governance_contract import periodic_introduction_errors
from governance_introduction import combine_introductions
from merge_delta import merge_delta, save_json
from test_audit_pack import empty_story, write_completed_chapter
from test_periodic_capacity_delivery import fake_patch


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cycle_then_short_tail_replaces_synopsis_and_keeps_analysis(tmp_path, monkeypatch):
    story = empty_story()
    story["介绍"]["描述"] = ""
    target = tmp_path / "故事结构_增量.json"
    save_json(target, story)
    for seq in range(1, 8):
        write_completed_chapter(tmp_path, seq)
    materials = {path: path.read_bytes() for folder in ("原文拆解", "章节处理")
                 for path in (tmp_path / folder).rglob("*") if path.is_file()}
    calls = []

    def worker(command, root, kind, pack, output, *args, **kwargs):
        calls.append(kind)
        assert kind == "audit_correction"
        text = pack.read_text(encoding="utf-8")
        assert "每次周期治理都检查" in text and "不足五章的尾段" in text
        first, last = (1, 5) if "001-005" in output.name else (6, 7)
        patch = fake_patch(first, last)
        patch["介绍"] = {"标题": "测试故事", "描述": "初期主线简介。" if last == 5 else "包含初期主线与尾段发展的完整简介。"}
        save_json(output, patch)
        return 0

    monkeypatch.setattr(pipeline, "run_worker", worker)
    assert pipeline.run_periodic_governance(tmp_path, 1, 5, audit_command="fake", worker_loop="continuous") == 0
    assert read(target)["介绍"]["描述"] == "初期主线简介。"
    assert pipeline.run_final_partial_governance(tmp_path, 5, audit_command="fake", worker_loop="continuous") == 0
    final = read(target)
    assert final["介绍"]["描述"] == "包含初期主线与尾段发展的完整简介。"
    assert {k: v for k, v in final.items() if k != "介绍"} == {k: v for k, v in story.items() if k != "介绍"}
    assert all(path.read_bytes() == body for path, body in materials.items())
    before = list((tmp_path / "故事结构版本").glob("story_before_governance_*.json"))
    assert any(read(path)["介绍"]["描述"] == "初期主线简介。" for path in before)
    assert pipeline.run_final_partial_governance(tmp_path, 5, audit_command="fake", worker_loop="continuous") == 0
    assert calls == ["audit_correction", "audit_correction"]


def test_missing_intro_produces_repair_without_publishing_or_inventing_text(tmp_path):
    story = empty_story()
    story["介绍"]["描述"] = ""
    target = tmp_path / "故事结构_增量.json"
    save_json(target, story)
    patch_path = pipeline.audit_paths(tmp_path, 1, 5)["correction"]
    patch = fake_patch(1, 5)
    save_json(patch_path, patch)
    before = target.read_bytes()
    assert pipeline.commit_governance(tmp_path, str(patch_path)) == 1
    assert target.read_bytes() == before
    assert "介绍.描述" in patch_path.with_suffix(".repair.md").read_text(encoding="utf-8")
    patch["介绍"] = {"描述": "据当前原文形成的完整简介。"}
    save_json(patch_path, patch)
    assert pipeline.commit_governance(tmp_path, str(patch_path)) == 0
    assert read(target)["介绍"]["描述"] == patch["介绍"]["描述"]


def test_tail_above_chapter_999_uses_synopsis_replacement_and_receipt(tmp_path):
    target = tmp_path / "故事结构_增量.json"
    save_json(target, empty_story())
    paths = pipeline.audit_paths(tmp_path, 1001, 1003)
    patch = fake_patch(1001, 1003)
    patch["介绍"] = {"描述": "千章之后尾段的完整简介。"}
    save_json(paths["correction"], patch)
    assert pipeline.commit_governance(tmp_path, str(paths["correction"])) == 0
    assert read(target)["介绍"]["描述"] == patch["介绍"]["描述"]
    assert pipeline.audit_range_is_resolved(tmp_path, 1001, 1003)
    assert not pipeline.unresolved_audits(tmp_path)


@pytest.mark.parametrize("intro", [None, {"描述": ""}, {"描述": []}, {"标题": 1}])
def test_invalid_explicit_intro_is_not_silently_ignored(intro):
    assert periodic_introduction_errors(empty_story(), {"介绍": intro})


def test_unchanged_synopsis_and_chapter_append_rules_remain(tmp_path):
    assert not periodic_introduction_errors(empty_story(), fake_patch(1, 5))
    target, patch_path = tmp_path / "story.json", tmp_path / "chapter.json"
    save_json(target, empty_story())
    save_json(patch_path, {"介绍": {"描述": "章节补充"}})
    merge_delta(target, patch_path)
    assert read(target)["介绍"]["描述"] == "周期审计测试\n章节补充"


def test_light_tiers_and_paged_evidence_include_complete_previous_synopsis(tmp_path):
    story = empty_story()
    story["其他事项集"] = [{"名称": "历史档案", "介绍": "x" * 100000}]
    story["介绍"]["描述"] = "旧版简介完整标记"
    save_json(tmp_path / "故事结构_增量.json", story)
    write_completed_chapter(tmp_path, 1)
    pack = pipeline.write_audit_pack(tmp_path, 1, 1, max_context_chars=16000)
    assert "旧版简介完整标记" in pack.read_text(encoding="utf-8")
    assert "## 完整故事结构" not in pack.read_text(encoding="utf-8")
    pipeline.write_governance_route(tmp_path, 1, 1, pack)
    assert "旧版简介完整标记" in pack.with_suffix(".history.md").read_text(encoding="utf-8")


def test_fragment_synopses_are_synthesized_once_and_reused(tmp_path):
    source = tmp_path / "raw.md"
    source.write_text("完整主线与冲突原文。", encoding="utf-8")
    calls = []

    def worker(command, root, kind, pack, output, **options):
        text = pack.read_text(encoding="utf-8")
        calls.append(text)
        content = None if "content 必须为 null" in text else {"描述": "主线与冲突相连的一份完整简介。"}
        save_json(output, {"overview": "主线及冲突证据位置", "content": content})
        return 0

    tree = CapacityTree(tmp_path, "fake", worker, "完整治理契约", "patch.json", [(source, "原文")])
    proposals = [{"标题": "测试", "描述": "第一份简介。"}, {"描述": "第二份简介。"}]
    combined = combine_introductions(tree, proposals)
    assert combined == {"标题": "测试", "描述": "主线与冲突相连的一份完整简介。"}
    count = len(calls)
    assert combine_introductions(tree, proposals) == combined
    assert len(calls) == count
    assert read(next(tree.root.glob("synopsis_proposals_*.json"))) == proposals
    assert combine_introductions(tree, [proposals[0], proposals[0]]) == proposals[0]
    assert len(calls) == count


def test_bounded_governance_reviews_one_synopsis_and_resumes(tmp_path):
    from bounded_generation import run_bounded_governance

    story = empty_story()
    save_json(tmp_path / "故事结构_增量.json", story)
    write_completed_chapter(tmp_path, 1)
    paths = pipeline.audit_paths(tmp_path, 1, 1)
    paths["pack"].write_text(pipeline.write_governance_route(tmp_path, 1, 1, paths["pack"]), encoding="utf-8")
    calls, reviewed = [], []

    def worker(command, root, kind, pack, output, **options):
        text = pack.read_text(encoding="utf-8")
        calls.append(str(pack))
        review = re.search(r"复核材料指纹：([0-9a-f]{64})", text)
        if review:
            reviewed.append(text)
            content = {"checked": review[1], "unresolved": [], "addition": None}
        elif "content 必须为 null" in text:
            content = None
        elif "本次目标：周期简介.json" in text:
            content = {"描述": "跨片核对后的一份完整简介。"}
        else:
            span = re.search(r"字符区间：(\d+)-(\d+)", text)
            if span and int(span[2]) - int(span[1]) > 2500:
                return 64
            content = fake_patch(1, 1)
            content["介绍"] = {"描述": f"第{len(calls)}份简介候选。"}
        save_json(output, {"overview": "完整证据与主线定位", "content": content})
        return 0

    assert run_bounded_governance("fake", tmp_path, paths["pack"], paths["correction"], worker) == 0
    result = read(paths["correction"])
    assert result["介绍"]["描述"] == "跨片核对后的一份完整简介。"
    assert any("跨片核对后的一份完整简介。" in text for text in reviewed)
    count = len(calls)
    assert run_bounded_governance("fake", tmp_path, paths["pack"], paths["correction"], worker) == 0
    assert len(calls) == count
