"""Offline final-governance capacity regressions; no provider or live project."""
import json
import re
from pathlib import Path

import pytest

from bounded_final import run_bounded_final
from context_budget import ContextBudget, CONTEXT_EXCEEDED, estimate_tokens
from fault_recovery import digest
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED
from test_bounded_final import project_files, unchanged_worker
from validate_structure import validate_structure_file


def state_of(project):
    return json.loads(next((project / '质量治理/最终审计/分步治理').glob('*/state.json')).read_text(encoding='utf-8'))


def test_global_index_larger_than_window_is_fully_paged_and_reviewed(tmp_path, monkeypatch):
    from run_pipeline import build_lightweight_index
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    story = json.loads(draft.read_text(encoding='utf-8'))
    story['其他事项集'] = [{'名称': f'档案{i:03d}' + '长名称' * 20, '分组': '自由主题',
                         '别名': [f'别名{i:03d}'], '标签集': [], '介绍': '容量边界样本',
                         '详情': {'说明': '离线完整名称覆盖验证'}} for i in range(80)]
    save_json(draft, story)
    assert validate_structure_file(str(draft), mode='final')[0]
    full_index = build_lightweight_index(draft)
    assert estimate_tokens(full_index) > ContextBudget.from_env().usable_input
    calls = []
    assert run_bounded_final('offline', tmp_path, pack, draft, unchanged_worker(calls)) == 0
    assert json.loads(draft.read_text(encoding='utf-8')) == story
    assert all(estimate_tokens(prompt) <= ContextBudget.from_env().usable_input for prompt in calls)
    index = next((tmp_path / '质量治理').rglob('name-index/*/index.md'))
    catalog = json.loads(index.with_name('catalog.json').read_text(encoding='utf-8'))
    assert ''.join(Path(item['path']).read_text(encoding='utf-8') for item in catalog) == full_index
    assert any('完整最新正式名称与全部别名' in prompt for prompt in calls)
    assert any('别名079' in prompt for prompt in calls)
    count = len(calls)
    assert run_bounded_final('offline', tmp_path, pack, draft, unchanged_worker(calls)) == 0
    assert len(calls) == count


def test_runtime_overflow_routes_to_bounded_and_restores_partial_draft(tmp_path, monkeypatch):
    import bounded_final
    import run_pipeline
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    before = draft.read_bytes()
    monkeypatch.setattr(run_pipeline, 'inspect_pack', lambda *a: {'passed': True})

    def reject(*a, **k):
        draft.write_text('{"partial":true}', encoding='utf-8')
        return CONTEXT_EXCEEDED

    calls = []
    monkeypatch.setattr(run_pipeline, 'run_worker', reject)
    monkeypatch.setattr(bounded_final, 'run_bounded_final', lambda *a, **k: calls.append(draft.read_bytes()) or 0)
    assert run_pipeline.run_generation_worker('offline', tmp_path, 'final_draft', pack, draft) == 0
    assert calls == [before]


def test_capacity_split_survives_cancel_without_replaying_completed_batch(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    calls, accepted, rejected = [], [], []
    normal = unchanged_worker(calls)
    cancel = [True]

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        if kind == 'audit_correction':
            if len(accepted) == 1 and not rejected:
                rejected.append(prompt)
                return CONTEXT_EXCEEDED
            if rejected and cancel[0]:
                cancel[0] = False
                return 130
            accepted.append(prompt)
        return normal(command, root, kind, task, output, **kwargs)

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 130
    state = state_of(tmp_path)
    assert state['splits'] and state['next'] == 1
    assert all('content' not in item and 'sha256' in item for item in state['units'])
    completed = accepted[0]
    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 0
    assert accepted.count(completed) == 1
    assert state_of(tmp_path)['splits'] == state['splits']


@pytest.mark.parametrize('truncated', [False, True])
def test_large_output_splits_current_unit_and_retains_failed_response(tmp_path, monkeypatch, truncated):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    normal = unchanged_worker([])
    rejected = []

    def worker(command, root, kind, task, output, **kwargs):
        if kind == 'audit_correction' and not rejected:
            text = '超长返回' * (240 if truncated else 500)
            output.write_text('{"质量说明":"' + text, encoding='utf-8')
            rejected.append(output)
            return 0
        return normal(command, root, kind, task, output, **kwargs)

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 0
    assert state_of(tmp_path)['splits'] and rejected[0].is_file()


@pytest.mark.parametrize('runtime_rejection', [False, True])
def test_large_patch_review_resumes_without_regenerating_proposal(tmp_path, monkeypatch, runtime_rejection):
    pack, draft, _ = project_files(tmp_path, monkeypatch, valid=False)
    value = json.loads(draft.read_text(encoding='utf-8'))
    value['介绍']['描述'] = '完整历史介绍' * (10 if runtime_rejection else 1300) + '必须保留的尾部'
    save_json(draft, value)
    before = draft.read_bytes()
    normal = unchanged_worker([])
    proposals, reviewed, direct_rejections = [], [], []
    cancel = [not runtime_rejection]

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        if kind == 'recovery_review' and runtime_rejection:
            direct_rejections.append(1)
            return CONTEXT_EXCEEDED
        if kind == 'audit_correction' and not json.loads((root / '故事结构_增量.json').read_text(encoding='utf-8'))['介绍']['标题']:
            save_json(output, {'章节范围': '全书', '介绍': {'标题': '测试书'}, '新增元素': {}, '修改元素': {}})
            proposals.append(output)
            return 0
        if kind == 'bounded_fragment' and '独立复核完整治理补丁' in prompt:
            if reviewed and cancel[0]:
                cancel[0] = False
                return 130
            reviewed.append(prompt)
        return normal(command, root, kind, task, output, **kwargs)

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == (0 if runtime_rejection else 130)
    if not runtime_rejection:
        assert draft.read_bytes() == before and state_of(tmp_path)['pending_proposal']['paged_review']
    else:
        assert direct_rejections == [1]
    first = reviewed[0]
    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 0
    assert len(proposals) == 1 and reviewed.count(first) == 1
    assert json.loads(draft.read_text(encoding='utf-8'))['介绍']['描述'] == value['介绍']['描述']


def test_global_review_overflow_only_reduces_parent_routing_indices(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    normal = unchanged_worker([])
    compressed, leaves, rejected = [], [], []

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        if kind == 'bounded_fragment':
            if '阶段：跨区间归并复核\n' in prompt and not rejected:
                rejected.append(prompt)
                return CONTEXT_EXCEEDED
            if '阶段：收紧关联索引' in prompt:
                compressed.append(prompt)
                save_json(output, {'overview': '核对名称与引用', 'content': None})
                return 0
            if '阶段：完整证据分片' in prompt:
                leaves.append(prompt)
        return normal(command, root, kind, task, output, **kwargs)

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 0
    assert rejected and compressed and len(leaves) == len(set(leaves))


def test_irreducible_capacity_rejection_stops_without_resetting_on_resume(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    before, calls = digest(draft), []

    def reject(*a, **k):
        calls.append(1)
        return CONTEXT_EXCEEDED

    assert run_bounded_final('offline', tmp_path, pack, draft, reject) == CONTEXT_EXCEEDED
    count = len(calls)
    assert 1 < count < 10 and state_of(tmp_path)['status'] == 'capacity_exceeded'
    assert run_bounded_final('offline', tmp_path, pack, draft, reject) == CONTEXT_EXCEEDED
    assert len(calls) == count and digest(draft) == before


def test_global_reviewer_cannot_modify_candidate_behind_strict_validation(tmp_path, monkeypatch):
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    before = draft.read_bytes()
    normal = unchanged_worker([])

    def worker(command, root, kind, task, output, **kwargs):
        rc = normal(command, root, kind, task, output, **kwargs)
        if kind == 'bounded_fragment':
            candidate = next((tmp_path / '质量治理/最终审计/分步治理').glob('*/故事结构_增量.json'))
            candidate.write_text('{}', encoding='utf-8')
        return rc

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == REPAIR_EXHAUSTED
    assert draft.read_bytes() == before


def test_global_reconciliation_never_redispatches_a_whole_book_read(tmp_path, monkeypatch):
    """All-book reconciliation belongs to the tree, even if its directive is tiny."""
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    calls = []
    normal = unchanged_worker(calls)

    def worker(command, root, kind, task, output, **kwargs):
        prompt = task.read_text(encoding='utf-8')
        if kind == 'audit_correction' and '本次材料：global_reconciliation' in prompt:
            return CONTEXT_EXCEEDED
        return normal(command, root, kind, task, output, **kwargs)

    assert run_bounded_final('offline', tmp_path, pack, draft, worker) == 0
    assert any('阶段：跨区间归并复核' in prompt for prompt in calls)
    assert not any('本次材料：global_reconciliation' in prompt for prompt in calls)


def test_large_final_diagnostics_are_paged_with_durable_round_limit(tmp_path, monkeypatch):
    import bounded_final
    pack, draft, _ = project_files(tmp_path, monkeypatch)
    error = '必须修正的严格引用错误。' * 1000 + '诊断唯一尾部'
    monkeypatch.setattr(bounded_final, 'validate_structure_file', lambda *a, **k: (False, [error], []))
    calls = []
    normal = unchanged_worker(calls)
    assert run_bounded_final('offline', tmp_path, pack, draft, normal) == REPAIR_EXHAUSTED
    state = state_of(tmp_path)
    assert state['final_validation_rounds'] == 3
    repairs = [item for item in state['units'] if item['source'] == 'final_validation_repair']
    assert len(repairs) > 3
    assert any('诊断唯一尾部' in prompt for prompt in calls)
    assert all(estimate_tokens(prompt) <= ContextBudget.from_env().usable_input for prompt in calls)
    count = len(calls)
    assert run_bounded_final('offline', tmp_path, pack, draft, normal) == REPAIR_EXHAUSTED
    assert len(calls) == count
