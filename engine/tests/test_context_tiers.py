"""Offline acceptance of historical tiers, scoped evidence and reference isolation."""
import json
from pathlib import Path

import pytest

from context_budget import capacity_scope, inspect_pack
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_audit_pack import empty_story, write_completed_chapter, empty_delta
from test_capacity_tree import review_response
import run_pipeline as pipeline


def test_delta_threshold_keeps_global_names_and_excludes_historical_deltas(tmp_path):
    story = empty_story()
    story['角色集'] = [{'名称': f'角色{i}', '介绍': '历史详情' * 300,
                       '别名': [f'别名{i}-{n}' for n in range(5)]} for i in range(100)]
    path = tmp_path / '故事结构_增量.json'
    save_json(path, story)
    for seq in [1, 5, 15, 16]:
        delta = tmp_path / f'章节处理/Delta/第{seq:03d}章_旧标题.json'
        delta.parent.mkdir(parents=True, exist_ok=True)
        delta.write_text(f'历史Delta-{seq}:' + '完整记录' * 4000 + f'唯一尾部-{seq}', encoding='utf-8')
    context = pipeline.build_delta_story_context(path, tmp_path, 16, full_story_kb=10)
    assert '角色99' in context and '别名99-4' in context
    assert '历史详情' not in context
    assert '唯一尾部-15' not in context
    assert all(f'历史Delta-{seq}:' not in context for seq in [1, 5, 16])
    assert json.loads(path.read_text(encoding='utf-8')) == story


def test_delta_token_budget_can_switch_before_kb_threshold(tmp_path):
    path = tmp_path / '故事结构_增量.json'
    story = empty_story()
    story['介绍']['描述'] = '完整历史描述' * 800
    save_json(path, story)
    assert '完整历史描述' in pipeline.build_delta_story_context(path, tmp_path, 1, 1000)
    with capacity_scope(dict(model_input_tokens=10000, model_output_tokens=1000,
                             model_context_tokens=13000, model_reserve_tokens=1000)):
        context = pipeline.build_delta_story_context(path, tmp_path, 1, 1000,
                                                     current_material='本章必要证据' * 600)
    assert '## 当前故事全部正式名称与别名索引' in context and '完整历史描述' not in context


def test_delta_task_refreshes_on_policy_and_input_change(tmp_path):
    path = tmp_path / '故事结构_增量.json'
    story = empty_story()
    story['介绍']['描述'] = '历史档案' * 2000
    save_json(path, story)
    chapter = write_completed_chapter(tmp_path, 1)
    assert pipeline.prepare_chapter_delta(tmp_path, chapter, full_story_kb=1000) == 0
    task = pipeline.artifact_paths(tmp_path, chapter)['delta_task']
    before = task.read_text(encoding='utf-8')
    assert pipeline.prepare_chapter_delta(tmp_path, chapter, full_story_kb=10) == 0
    after = task.read_text(encoding='utf-8')
    assert before != after and '## 当前故事全部正式名称与别名索引' in after
    story['角色集'] = [{'名称': '新增历史正式名'}]
    save_json(path, story)
    assert pipeline.prepare_chapter_delta(tmp_path, chapter, full_story_kb=10) == 0
    assert '新增历史正式名' in task.read_text(encoding='utf-8')


def test_bounded_delta_does_not_reanalyze_all_old_chapters(tmp_path):
    from bounded_generation import run_bounded_chapter
    save_json(tmp_path / '故事结构_增量.json', empty_story())
    current = write_completed_chapter(tmp_path, 16)
    old = write_completed_chapter(tmp_path, 1)
    old.write_text('不得自动重读的旧原文' * 8000, encoding='utf-8')
    output = pipeline.artifact_paths(tmp_path, current)['delta']
    golden = empty_delta(16)
    golden['章节'] = '0016'  # Bounded fragments retain the canonical chapter identifier.
    calls = []

    def worker(command, root, kind, task, result, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        calls.append(prompt)
        save_json(result, review_response(prompt) or {'overview': '本章证据与正式名称', 'content': golden})
        return 0

    assert run_bounded_chapter('offline', tmp_path, 'delta', output, 16, worker) == 0
    assert not any('不得自动重读的旧原文' in prompt for prompt in calls)
    assert not any('本次只阅读历史背景' in prompt for prompt in calls)
    count = len(calls)
    assert run_bounded_chapter('offline', tmp_path, 'delta', output, 16, worker) == 0
    assert len(calls) == count
    assert list(tmp_path.rglob('references.json'))


def test_periodic_tiers_preserve_current_evidence_and_do_not_truncate_details(tmp_path):
    story = empty_story()
    story['角色集'] = [{'名称': '当期角色', '介绍': '相关完整档案' * 18000 + '相关详情唯一尾部'}]
    save_json(tmp_path / '故事结构_增量.json', story)
    old = write_completed_chapter(tmp_path, 1)
    old.write_text('不属于当前周期的原文', encoding='utf-8')
    current = write_completed_chapter(tmp_path, 6)
    paths = pipeline.artifact_paths(tmp_path, current)
    save_json(paths['delta'], {'新增元素': {}, '修改元素': {'角色集': [{'名称': '当期角色'}]}})
    detail = pipeline.extract_relevant_elements_for_audit(story, tmp_path, 6, 6)
    assert '相关详情唯一尾部' in detail and len(detail) > 80000
    pack = pipeline.write_audit_pack(tmp_path, 6, 6, max_context_chars=14000)
    assert pack
    status = pipeline.audit_status(tmp_path, 6, 6)
    assert status['context']['render_mode'] == 'story_index'
    text = pack.read_text(encoding='utf-8')
    assert current.read_text(encoding='utf-8') in text
    assert paths['analysis'].read_text(encoding='utf-8') not in text
    assert paths['delta'].read_text(encoding='utf-8') not in text
    assert '不属于当前周期的原文' not in text
    assert str(tmp_path / '故事结构_增量.json') in text


def test_singleton_paging_scopes_sources_to_cycle_and_keeps_story_reference(tmp_path):
    save_json(tmp_path / '故事结构_增量.json', empty_story())
    old = write_completed_chapter(tmp_path, 1)
    chapter = write_completed_chapter(tmp_path, 6)
    chapter.write_text('本周期完整原文' * 4000 + '原文唯一末尾', encoding='utf-8')
    pack = pipeline.write_audit_pack(tmp_path, 6, 6, max_context_chars=12000)
    route = json.loads(pack.with_suffix('.governance.json').read_text(encoding='utf-8'))
    primary = [Path(path) for path, _ in route['sources']]
    references = [Path(path) for path, _ in route['references']]
    assert chapter in primary and old not in primary
    assert tmp_path / '故事结构_增量.json' in references
    assert tmp_path / '故事结构_增量.json' not in primary


def test_final_pack_separates_required_sources_from_optional_originals(tmp_path, monkeypatch):
    save_json(tmp_path / '故事结构_增量.json', empty_story())
    chapter = write_completed_chapter(tmp_path, 1)
    chapter.write_text('全书原文仅在需查证时读取' * 50000, encoding='utf-8')
    monkeypatch.setattr(pipeline, 'run_cmd', lambda *a, **k: 0)
    assert pipeline.cmd_final_pack(tmp_path) == 0
    pack = tmp_path / '质量治理/最终审计/final_task_pack.md'
    sources = json.loads(pack.with_suffix('.md.sources.json').read_text(encoding='utf-8'))
    references = json.loads(pack.with_suffix('.references.json').read_text(encoding='utf-8'))
    assert str(chapter.resolve()) not in sources
    assert str(chapter.resolve()) in references
    assert inspect_pack(pack)['estimated_input_tokens'] < 20000
    assert '全书原文仅在需查证时读取' not in pack.read_text(encoding='utf-8')


def test_reference_lookup_does_not_create_leaf_and_remains_guarded(tmp_path):
    from capacity_tree import CapacityTree, TreeFailure
    source, archive = tmp_path / 'current.md', tmp_path / 'archive.json'
    source.write_text('本章唯一证据', encoding='utf-8')
    archive.write_text('{"历史":"需查证的完整档案"}', encoding='utf-8')
    calls = []

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        calls.append(prompt)
        save_json(output, {'overview': '本章证据', 'content': '# 分析\n本章结论'})
        return 0

    tree = CapacityTree(tmp_path, 'offline', worker, '原要求', 'report.md', [(source, '本章')],
                        reference_sources=[(archive, '历史查证')])
    tree.run()
    assert len(tree.coverage) == 1 and len(calls) == 1
    assert '需查证的完整档案' not in calls[0]
    copy = next(iter(tree.reference_paths.values()))
    original = copy.read_bytes()

    def corrupt(*args, **kwargs):
        worker(*args, **kwargs)
        copy.write_text('错误改动', encoding='utf-8')
        return 0

    tree.worker = corrupt
    with pytest.raises(TreeFailure) as failure:
        tree.request('另一次核查', '查证保护')
    assert failure.value.code == REPAIR_EXHAUSTED and copy.read_bytes() == original


def _cycle_event_project(project, *, oversized_event=False):
    """Build host-scoped events plus a large related role, without a model."""
    story = empty_story()
    story["角色集"] = [{"名称": "当期角色", "介绍": "角色历史档案" * 24000}]
    story["事件集"] = [
        {"名称": "旧章事件", "参与成员": ["当期角色"],
         "详情": {"涉及章节": "0001", "动作证据": "旧章唯一动作"}},
        {"名称": "当期事件", "参与成员": ["当期角色"],
         "详情": {"涉及章节": "0006", "动作证据": "完整动作开头" + "行动细节" * (20000 if oversized_event else 8) + "完整动作末尾"}},
        {"名称": "跨章事件", "参与成员": ["当期角色"],
         "详情": {"涉及章节": "0001，0007", "动作证据": "跨章历史与当期动作均须完整保留"}},
        {"名称": "当期未被Delta点名事件", "参与成员": ["当期角色"],
         "详情": {"涉及章节": "0007", "动作证据": "正式档案章节决定范围"}},
        {"名称": "未知章事件", "参与成员": ["当期角色"],
         "详情": {"动作证据": "不能猜测未知章归属"}},
    ]
    sources = {}
    for seq in (1, 6, 7):
        chapter = write_completed_chapter(project, seq)
        chapter.write_text(f"原文{seq}开头\n完整原文证据：行动、结果及未解线索。\n原文{seq}末尾", encoding="utf-8")
        sources[seq] = chapter
        if seq in (6, 7):
            # Delta names are routing hints and may mention an old event.
            save_json(pipeline.artifact_paths(project, chapter)["delta"], {
                "新增元素": {},
                "修改元素": {"角色集": [{"名称": "当期角色"}],
                             "事件集": [{"名称": name} for name in
                                     ("旧章事件", "当期事件", "跨章事件", "未知章事件")]},
            })
    save_json(project / "故事结构_增量.json", story)
    pipeline.ensure_story(project)
    return sources, json.loads((project / "故事结构_增量.json").read_text(encoding="utf-8"))


def _embedded_audit_records(pack, heading):
    """Decode the actual JSON supplied to generation, rather than match markers."""
    import re
    text = pack.read_text(encoding="utf-8")
    match = re.search(re.escape(heading) + r"\s*\x60{3}json\s*\n(.*?)\n\x60{3}", text, re.S)
    return json.loads(match[1]) if match else None


def test_audit_fallback_embeds_complete_host_scoped_events_before_bare_index(tmp_path):
    """A large related role must not discard affordable current event records."""
    sources, current = _cycle_event_project(tmp_path)
    source_bytes = {seq: path.read_bytes() for seq, path in sources.items()}
    story_before = (tmp_path / "故事结构_增量.json").read_bytes()

    pack = pipeline.write_audit_pack(tmp_path, 6, 7)
    assert pack is not None
    supplied = _embedded_audit_records(pack, "## 本周期相关老元素详情")
    assert supplied == {"事件集": [current["事件集"][index] for index in (1, 2, 3)]}
    assert pipeline.audit_status(tmp_path, 6, 7)["context"]["render_mode"] == "story_index_events"
    assert inspect_pack(pack)["passed"] is True
    assert (tmp_path / "故事结构_增量.json").read_bytes() == story_before
    text = pack.read_text(encoding="utf-8")
    for seq in (6, 7):
        assert text.count(sources[seq].read_text(encoding="utf-8")) == 1
    assert sources[1].read_text(encoding="utf-8") not in text
    assert {seq: path.read_bytes() for seq, path in sources.items()} == source_bytes


def test_audit_event_fallback_keeps_bare_index_when_complete_events_exceed_budget(tmp_path):
    """The new tier must never truncate an oversized event to force admission."""
    sources, current = _cycle_event_project(tmp_path, oversized_event=True)
    with capacity_scope(dict(model_input_tokens=30000, model_output_tokens=1000,
                             model_context_tokens=35000, model_reserve_tokens=1000)):
        pack = pipeline.write_audit_pack(tmp_path, 6, 7)
        assert pack is not None and inspect_pack(pack)["passed"] is True
    assert pipeline.audit_status(tmp_path, 6, 7)["context"]["render_mode"] == "story_index"
    assert _embedded_audit_records(pack, "## 本周期相关老元素详情") is None
    assert json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8")) == current
    text = pack.read_text(encoding="utf-8")
    assert all(sources[seq].read_text(encoding="utf-8") in text for seq in (6, 7))


@pytest.mark.parametrize("mode", ["full_story", "story_index_relevant"])
def test_audit_event_fallback_preserves_existing_richer_material_routes(tmp_path, mode):
    """Affordable full story or complete related records retain their priority."""
    sources, story = _cycle_event_project(tmp_path)
    story["角色集"][0]["介绍"] = "相关角色完整介绍"
    if mode == "story_index_relevant":
        story["其他事项集"] = [{"名称": "范围外大型档案", "介绍": "历史背景" * 50000}]
    save_json(tmp_path / "故事结构_增量.json", story)
    pipeline.ensure_story(tmp_path)
    canonical = json.loads((tmp_path / "故事结构_增量.json").read_text(encoding="utf-8"))

    pack = pipeline.write_audit_pack(tmp_path, 6, 7)
    assert pack is not None
    assert pipeline.audit_status(tmp_path, 6, 7)["context"]["render_mode"] == mode
    if mode == "full_story":
        assert _embedded_audit_records(pack, "## 完整故事结构") == canonical
    else:
        supplied = _embedded_audit_records(pack, "## 本周期相关老元素详情")
        assert supplied["角色集"] == canonical["角色集"]
        # The unchanged rich tier includes all explicitly related history.
        assert supplied["事件集"] == [canonical["事件集"][index] for index in (0, 1, 2, 4)]
    assert all(sources[seq].read_text(encoding="utf-8") in pack.read_text(encoding="utf-8") for seq in (6, 7))
