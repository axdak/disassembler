"""Offline acceptance of cycle/tail synopsis replacement and complete evidence."""
import json
import re
from pathlib import Path

import pytest

import engine_contract
import run_pipeline as pipeline
from capacity_tree import CapacityTree
from governance_contract import periodic_introduction_errors
from governance_introduction import combine_introductions
from merge_delta import merge_delta, save_json
from story_schema_rules import COLLECTION_KEYS
from synopsis_contract import (SYNOPSIS_MAX_CHARS, SYNOPSIS_SECTIONS,
                               SYNOPSIS_SYNTHESIS_BUDGETS, synopsis_section_headings,
                               synopsis_section_spec)
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


def test_story_without_title_is_not_a_periodic_deadlock(tmp_path):
    """真实故障回归：历史故事的 介绍.标题 长期为空（process 阶段合法）。

    旧实现把 标题 当硬必填，而修复提示词又要求"有依据才补齐、不虚构"，模型永远无法满足，
    于是每个周期都卡死，并把一次内容缺口放大成一串无效的容量分层修复。标题留空必须放行。
    """
    story = empty_story()
    story["介绍"]["标题"] = ""  # 现实形态：标题从未被填充
    target = tmp_path / "故事结构_增量.json"
    save_json(target, story)
    # 标题缺失/留空都不是缺口；描述仍必须非空（这才是周期治理的真实职责）
    assert not periodic_introduction_errors(story, {"介绍": {"描述": "据原文形成的完整简介。"}})
    assert not periodic_introduction_errors(story, {"介绍": {"标题": "", "描述": "据原文形成的完整简介。"}})
    assert not periodic_introduction_errors(story, fake_patch(1, 5))
    blank = empty_story()
    blank["介绍"] = {"标题": "", "描述": ""}
    assert periodic_introduction_errors(blank, {"介绍": {"标题": ""}})  # 描述为空仍判错
    # 端到端：与真实 011-015 补丁同形（有 描述、无 标题）必须能提交
    paths = pipeline.audit_paths(tmp_path, 1, 5)
    patch = fake_patch(1, 5)
    patch["介绍"] = {"描述": "据当前原文形成的完整简介。"}
    save_json(paths["correction"], patch)
    assert pipeline.commit_governance(tmp_path, str(paths["correction"])) == 0
    assert read(target)["介绍"]["描述"] == "据当前原文形成的完整简介。"
    # 留空不被发明；补齐由最终交付（final 模式）负责
    assert read(target)["介绍"]["标题"] == ""


def test_explicitly_blanking_an_existing_title_is_still_rejected():
    """只拦截显式破坏：已有非空标题被改成空白/非字符串仍是 error。"""
    story = empty_story()  # 夹具标题为 "测试故事"
    assert periodic_introduction_errors(story, {"介绍": {"标题": ""}})
    assert not periodic_introduction_errors(story, {"介绍": {"标题": "新书名", "描述": "简介"}})


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


ENGINE_ROOT = Path(__file__).resolve().parents[1]


def test_periodic_prompt_states_the_char_budget():
    """篇幅契约必须写在生成端提示词里——那才是真正约束模型的一层。

    实测背景：《斗破苍穹》简介随周期增长 0 → 257 → 376 → 510 字，每周期约 +120~134 字，
    当时提示词对篇幅完全没有要求，程序侧也没有任何上限。
    """
    text = (ENGINE_ROOT / "prompts" / "流水线" / "周期审计.j2").read_text(encoding="utf-8")
    assert f"上限 {SYNOPSIS_MAX_CHARS} 字" in text
    assert "不得逐周期累加" in text and "入口而不是索引" in text
    # 既有语义不能被这次改动挤掉
    assert "每次周期治理都检查" in text and "不足五章的尾段" in text


def test_periodic_prompt_requires_structured_sections():
    """1500 字的简介必须是分节的，而不是一整坨连续长文。

    分节写在提示词里而不是做程序侧硬门禁：篇幅与结构都是软契约
    （见 test_overlong_synopsis_is_never_a_periodic_failure 的既有模式策略）。
    """
    text = (ENGINE_ROOT / "prompts" / "流水线" / "周期审计.j2").read_text(encoding="utf-8")
    for heading in synopsis_section_headings():
        assert heading in text, f"提示词缺少分节标题 {heading}"
    assert "四个分节必须齐全且顺序固定" in text
    assert "不得新增、改名、合并或省略" in text
    assert "节与节之间空一行" in text
    # 分节标题只做纯文本装饰，不带 Markdown 标记。
    # 注意不能断言整个提示词里没有 `*`——别的段落仍在用 `**加粗**` 强调。
    assert not any("*" in heading for heading in synopsis_section_headings())
    assert "不得写成条目清单" in text
    # 旧简介未结构化时必须重写——否则模型会以"内容准确"为由永远保留一整段旧简介
    assert "一律视为需要更新" in text


def test_sections_are_single_sourced_into_the_merge_instruction():
    """归并端必须与生成端共用同一套分节定义，否则冲突归并会把结构弄丢。"""
    import governance_introduction

    assert SYNOPSIS_SECTIONS == ("主线", "主角处境", "核心冲突", "当前进展")
    assert synopsis_section_headings()[0] == "【主线】"
    spec = synopsis_section_spec()
    for heading in synopsis_section_headings():
        assert heading in spec
    assert spec in governance_introduction._SECTION_RULE
    assert "四个分节必须齐全且顺序固定" in governance_introduction._SECTION_RULE


def test_chapter_prompt_no_longer_invites_unbounded_synopsis():
    """章节提示词不再鼓励顶层简介写长；元素级档案的"可写长"保持不变。"""
    text = (ENGINE_ROOT / "prompts" / "流水线" / "单章增量提取.j2").read_text(encoding="utf-8")
    assert "可以写长一点" not in text
    assert "顶层故事简介" in text
    assert "不要为了简短而省略剧情细节" in text


def test_synopsis_budget_is_single_sourced():
    """生成端、归并端、消费端共用同一个上限，避免三处漂移。"""
    assert SYNOPSIS_MAX_CHARS == 1500
    assert SYNOPSIS_SYNTHESIS_BUDGETS[0] == SYNOPSIS_MAX_CHARS
    assert all(0 < budget <= SYNOPSIS_MAX_CHARS for budget in SYNOPSIS_SYNTHESIS_BUDGETS)
    # 降级阶梯必须严格递减，否则"材料超预算时收紧目标"没有意义。
    assert all(a > b for a, b in zip(SYNOPSIS_SYNTHESIS_BUDGETS, SYNOPSIS_SYNTHESIS_BUDGETS[1:]))
    # 分节标题本身不能吃掉太多篇幅预算，否则再宽松的上限也写不下正文。
    heading_chars = sum(len(heading) for heading in synopsis_section_headings())
    assert SYNOPSIS_MAX_CHARS - heading_chars >= 1000


def test_downstream_reads_exactly_the_contract_budget():
    import analysis_context_pack

    story = {"介绍": {"标题": "测试", "描述": "字" * (SYNOPSIS_MAX_CHARS + 300)},
             **{key: [] for key in COLLECTION_KEYS}}
    text = analysis_context_pack.summarize_story_structure(story)
    line = next(item for item in text.splitlines() if item.startswith("- 描述:"))
    assert line.count("字") == SYNOPSIS_MAX_CHARS


def test_overlong_synopsis_is_never_a_periodic_failure():
    """篇幅是软契约：超长不判失败。

    周期治理属于 process 阶段；按既有模式策略，过程阶段不得把内容/篇幅问题升级为
    error——那会让模型"写短"与"写全"两边都做不到，重演标题门禁那类死锁。
    """
    long_synopsis = "简" * (SYNOPSIS_MAX_CHARS + 800)
    assert not periodic_introduction_errors(empty_story(), {"介绍": {"描述": long_synopsis}})


def test_char_budget_is_not_a_validation_fingerprint():
    """调字数不应让历史回执集体失效并触发大规模重算。"""
    names = {str(path.relative_to(path.parents[1])) for path in engine_contract.contract_files()}
    assert not any("synopsis_contract" in name for name in names)
