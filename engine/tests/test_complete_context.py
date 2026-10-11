"""Offline full-evidence coverage, independent review and within-chapter governance."""
import json
import re
from pathlib import Path

import pytest

from context_budget import capacity_scope
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_capacity_tree import review_response


@pytest.mark.parametrize('old_policy', ['auto', 'none', 'sample', 'full'])
@pytest.mark.parametrize('task', ['summary', 'report', 'narrative_structure'])
def test_legacy_policies_keep_required_materials_and_complete_lookup_sources(tmp_path, old_policy, task):
    from analysis_context_pack import build_pack, split_rendered_pack
    from test_audit_pack import write_completed_chapter, empty_story
    chapter = write_completed_chapter(tmp_path, 1)
    original = '完整原文' * 4000 + '唯一原文尾部'
    chapter.write_text(original, encoding='utf-8')
    story = empty_story()
    story['介绍']['描述'] = '完整历史' * 3000 + '唯一历史尾部'
    save_json(tmp_path / '故事结构_增量.json', story)
    report = tmp_path / '全书分析/剧情结构/章节梗概汇总.md'
    report.parent.mkdir(parents=True)
    upstream = '全书梗概' * 6000 + '唯一报告尾部'
    report.write_text(upstream, encoding='utf-8')
    content = build_pack(tmp_path, task, [{'seq': 1, 'filename': chapter.name}], [], '',
                         old_policy, 10, 10, 1, 1, story_summary_max_items=1, max_pack_chars=12000)
    assert ('唯一原文尾部' in content) == (task == 'summary')
    assert '唯一历史尾部' not in content  # Story uses the explicit global index.
    assert str(tmp_path / '故事结构_增量.json') in content
    assert chapter.read_text(encoding='utf-8') == original
    if task == 'narrative_structure':
        assert upstream in content
    elif task == 'report':
        assert str(report) in content  # Oversized summary is a lookup reference.
        assert report.read_text(encoding='utf-8') == upstream
    pieces = split_rendered_pack(content, 12000)
    material = content.split('## 章节材料\n', 1)[1]
    assert ''.join(piece.split('## 章节材料\n', 1)[1] for piece in pieces) == material


def test_delta_includes_history_older_than_ten_chapters_without_future_leak(tmp_path):
    from full_context import history_evidence, render_sources
    from test_audit_pack import write_completed_chapter, empty_story
    save_json(tmp_path / '故事结构_增量.json', empty_story())
    for seq in [1, 5, 15, 16]:
        chapter = write_completed_chapter(tmp_path, seq)
        chapter.write_text(f'完整原文标记-{seq:03d}', encoding='utf-8')
    history = render_sources(history_evidence(tmp_path, 16))
    assert all(f'完整原文标记-{seq:03d}' in history for seq in [1, 5, 15])
    assert '完整原文标记-016' not in history


def test_renamed_original_does_not_hide_historical_delta_or_governance_tail(tmp_path):
    from full_context import history_evidence, render_sources
    from run_pipeline import get_previous_correction
    delta = tmp_path / '章节处理/Delta/第001章_原来的标题.json'
    delta.parent.mkdir(parents=True)
    delta.write_text('{"完整历史":"旧标题对应的完整内容"}', encoding='utf-8')
    assert '旧标题对应的完整内容' in render_sources(history_evidence(tmp_path, 2))
    previous = tmp_path / '质量治理/周期审计/correction_001-005.json'
    previous.parent.mkdir(parents=True)
    text = '完整历史治理记录' * 1000 + '末尾跨周期合并依据'
    previous.write_text(text, encoding='utf-8')
    assert get_previous_correction(tmp_path, 6)['content'] == text


def test_independent_review_can_add_cross_page_findings_and_must_cover_candidate(tmp_path):
    from bounded_generation import run_hierarchical_reports
    source = tmp_path / 'full.md'
    source.write_text('角色甲的旧名在前段。' + '完整原文。' * 500 + '末段揭示角色乙就是甲。', encoding='utf-8')
    pack, output = tmp_path / 'pack.md', tmp_path / 'report.md'
    save_json(pack.with_suffix('.hierarchy.json'), {'sources': [str(source)], 'contract': '原格式报告'})
    seen = []

    def worker(command, root, kind, task, result, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        seen.append(prompt)
        response = review_response(prompt, '# 报告\n\n## 关联\n甲乙为同一人，前后段证据共同支持。')
        save_json(result, response or {'overview': '甲乙身份需要跨片复核', 'content': '# 报告\n\n## 分析\n原始完整发现。'})
        return 0

    with capacity_scope(dict(model_input_tokens=12000, model_output_tokens=2000,
                             model_context_tokens=15000, model_reserve_tokens=1000)):
        assert run_hierarchical_reports('offline', tmp_path, pack, [output], worker) == 0
        count = len(seen)
        assert run_hierarchical_reports('offline', tmp_path, pack, [output], worker) == 0
        assert len(seen) == count
    assert '原始完整发现' in output.read_text(encoding='utf-8')
    assert '甲乙为同一人' in output.read_text(encoding='utf-8')
    reviews = list(tmp_path.rglob('review_coverage.json'))
    assert reviews
    coverage = json.loads(reviews[0].read_text(encoding='utf-8'))
    assert len({row['source'] for row in coverage}) == 2  # Full original AND full candidate.
    assert any('跨区间归并复核' in text and '复核材料指纹' in text for text in seen)


@pytest.mark.parametrize('bad_receipt', ['missing', 'wrong_hash', 'unresolved'])
def test_review_failure_never_overwrites_report(tmp_path, bad_receipt):
    from bounded_generation import run_hierarchical_reports
    source, pack, output = tmp_path / 'source.md', tmp_path / 'pack.md', tmp_path / 'report.md'
    source.write_text('原始证据', encoding='utf-8')
    output.write_text('已发布报告', encoding='utf-8')
    save_json(pack.with_suffix('.hierarchy.json'), {'sources': [str(source)], 'contract': '原契约'})
    calls = []

    def worker(command, root, kind, task, result, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        response = review_response(prompt)
        if response:
            calls.append(1)
            if bad_receipt == 'missing':
                response['content'] = None
            elif bad_receipt == 'wrong_hash':
                response['content']['checked'] = '0' * 64
            else:
                response['content']['unresolved'] = ['两处原文时序矛盾，尚未核实']
        save_json(result, response or {'overview': '来源索引', 'content': '# 候选报告\n证据结论'})
        return 0

    assert run_hierarchical_reports('offline', tmp_path, pack, [output], worker) == REPAIR_EXHAUSTED
    assert run_hierarchical_reports('offline', tmp_path, pack, [output], worker) == REPAIR_EXHAUSTED
    assert len(calls) == 3
    assert output.read_text(encoding='utf-8') == '已发布报告'


@pytest.mark.parametrize('add_correction', [False, True])
def test_single_chapter_governance_pages_then_uses_original_strict_commit(tmp_path, monkeypatch, add_correction):
    import run_pipeline as pipeline
    from test_audit_pack import write_completed_chapter, empty_story, write_no_change_correction
    save_json(tmp_path / '故事结构_增量.json', empty_story())
    chapter = write_completed_chapter(tmp_path, 1)
    original = '全部治理原文证据。' * 3000 + '治理末尾唯一事实'
    chapter.write_text(original, encoding='utf-8')
    write_no_change_correction(tmp_path, 1, 1)
    paths = pipeline.audit_paths(tmp_path, 1, 1)
    golden = json.loads(paths['correction'].read_text(encoding='utf-8'))
    paths['correction'].unlink()
    calls = []

    def worker(command, root, kind, task, result, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        calls.append(prompt)
        assert '本次目标：correction_001-001.json' in prompt
        addition = ({'章节范围': golden['章节范围'], '新增元素': {}, '修改元素': {},
                     '介绍': {'描述': '治理末尾唯一事实'}, '治理类型': 'governance'} if add_correction else None)
        save_json(result, review_response(prompt, addition) or {'overview': '已核对全部原文、档案、时序和引用', 'content': golden})
        return 0

    monkeypatch.setattr(pipeline, 'run_worker', worker)
    with capacity_scope(dict(model_input_tokens=26000, model_output_tokens=4000,
                             model_context_tokens=32000, model_reserve_tokens=2000)):
        assert pipeline.run_periodic_governance(tmp_path, 1, 1, max_context_chars=10000,
                                               audit_command='offline', worker_loop='continuous') == 0
    assert pipeline.audit_status(tmp_path, 1, 1)['status'] == 'committed'
    assert chapter.read_text(encoding='utf-8') == original
    assert any('治理末尾唯一事实' in prompt for prompt in calls)
    assert list(tmp_path.rglob('review_coverage.json'))
    assert list(tmp_path.rglob('validate_schema_governance_*.txt'))
    assert json.loads(paths['correction'].read_text(encoding='utf-8'))['章节范围'] == '第001章-第001章'
    if add_correction:
        assert json.loads(paths['correction'].read_text(encoding='utf-8'))['治理类型'] == 'governance'
