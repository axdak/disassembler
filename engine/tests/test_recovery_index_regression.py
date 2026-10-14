"""Chapter 37 regressions: index limits, durable preparation and lossless moves."""
import copy
import json
import re

import pytest

from capacity_tree import CapacityTree, TreeFailure
from context_budget import ContextBudget, CONTEXT_EXCEEDED, capacity_scope
from fault_recovery import preservation_errors
from local_json_patch import delta_repair_scope
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED


def test_configured_overview_budget_reaches_tree_and_child_environment(tmp_path):
    with capacity_scope({'model_overview_tokens': 6000}):
        policy = ContextBudget.from_env()
        tree = CapacityTree(tmp_path, 'offline', None, '索引', 'index.md', [])
        assert tree.overview_budget == 6000
        assert policy.to_env()['ND_CONTEXT_OVERVIEW_TOKENS'] == '6000'
    with capacity_scope({'model_overview_tokens': 90000}):
        tree = CapacityTree(tmp_path, 'offline', None, '索引', 'index.md', [])
        assert tree.overview_budget == min(90000, tree.policy.output_tokens // 5,
                                          tree.policy.usable_input // 16)


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_invalid_overview_setting_is_rejected(value):
    with pytest.raises(ValueError):
        ContextBudget.from_settings({'model_overview_tokens': value})


def test_index_admission_failure_identifies_node_and_capacity(tmp_path):
    with capacity_scope(dict(model_input_tokens=7000, model_context_tokens=10000,
                             model_output_tokens=1200, model_reserve_tokens=1000)):
        tree = CapacityTree(tmp_path, 'offline', None, '索引', 'index.md', [])
        with pytest.raises(TreeFailure) as failure:
            tree.request('证据' * 10000, '完整证据分片', overview_only=True)
    assert failure.value.code == CONTEXT_EXCEEDED
    assert failure.value.node
    assert '估算' in str(failure.value) and '6000' in str(failure.value)


def test_overview_overflow_splits_evidence_and_reuses_successful_nodes(tmp_path):
    source = tmp_path / 'evidence.md'
    source.write_text('证' * 800, encoding='utf-8')
    calls = []

    def worker(command, root, kind, pack, output, **kwargs):
        prompt = pack.read_text(encoding='utf-8')
        calls.append(prompt)
        span = re.search(r'字符区间：(\d+)-(\d+)', prompt)
        # The complete source needs more routing detail than the requested limit.
        long = span and int(span[2]) - int(span[1]) > 400
        save_json(output, {'overview': '证' * 1100 if long else '完整证据位置见原件', 'content': None})
        return 0

    with capacity_scope({'model_overview_tokens': 2000}):
        tree = CapacityTree(tmp_path, 'offline', worker, '索引', 'index.md', [(source, '原文')],
                            adaptive_overviews=True)
        tree.run(overview_only=True)
        assert [(s['start'], s['end']) for s in tree.coverage] == [(0, 400), (400, 800)]
        count = len(calls)
        tree.run(overview_only=True)
        assert len(calls) == count
        assert source.read_text(encoding='utf-8') == '证' * 800


def test_index_failure_does_not_spend_repair_strategies_and_setting_change_reopens(tmp_path):
    from test_fault_recovery import scenario, invoke, state
    output, pack, source, calls, published, options = scenario(tmp_path)
    source.write_text('原始证据' * 2000, encoding='utf-8')

    def invalid(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        save_json(expected, {'invalid': True})
        return 0

    options['worker'] = invalid
    settings = dict(model_input_tokens=7000, model_context_tokens=10000,
                    model_output_tokens=1200, model_reserve_tokens=1000)
    with capacity_scope(settings):
        assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
        record = state(tmp_path)
        assert record['status'] == 'preparation_failed'
        assert record['next_strategy'] == 0
        assert len(record['attempts']) == 1
        assert record['calls'][0]['dispatched'] is False
        assert record['calls'][0]['phase'] == 'evidence_index'
        assert record['calls'][0]['error']
        assert record['calls'][0]['node']
        assert calls == ['bounded_fragment'] * 3
        assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
        assert len(calls) == 3
    with capacity_scope({**settings, 'model_overview_tokens': 8000}):
        assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
        assert state(tmp_path)['generation'] == record['generation'] + 1
        assert len(calls) == 6
    assert not published


def test_failure_before_guarded_dispatch_is_not_a_repair_attempt(tmp_path, monkeypatch):
    """Preparing read-only worker inputs can fail before the worker is called."""
    import bounded_final
    from test_fault_recovery import scenario, invoke, state
    output, pack, source, calls, _, options = scenario(tmp_path)
    source.write_text('原始证据' * 2000, encoding='utf-8')

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        assert kind == 'bounded_fragment'
        save_json(expected, {'overview': '证据见原件', 'content': None})
        return 0

    def failed_guard(*args, **kwargs):
        raise OSError('无法准备只读材料快照')

    monkeypatch.setattr(bounded_final, 'guarded_worker', failed_guard)
    options['worker'] = worker
    with capacity_scope(dict(model_input_tokens=7000, model_context_tokens=10000,
                             model_output_tokens=1200, model_reserve_tokens=1000)):
        assert invoke(tmp_path, output, pack, options) == 1
    record = state(tmp_path)
    assert record['next_strategy'] == 0
    assert record['status'] == 'preparation_failed'
    assert record['calls'][0]['dispatched'] is False
    assert '无法准备只读材料快照' in record['calls'][0]['error']


def migration_sample():
    """Two registered meanings require evidence-based selection, never guessing."""
    story = {'角色集': [{'名称': '族长'}], '地点集': [{'名称': '测试家族'}],
             '阵营集': [{'名称': '测试家族'}]}
    item = {'名称': '测试家族', '生日': '0001-01-01T00:00:00',
            '详情': {'提取理由': '原文描述家族邀请友好势力参加仪式。', '格局': '全部原始分析'}}
    old = {'新增元素': {}, '修改元素': {'角色集': [item]}}
    new = copy.deepcopy(old)
    moved = new['修改元素']['角色集'].pop()
    moved['详情']['生日'] = moved.pop('生日')
    new['修改元素']['阵营集'] = [moved]
    return story, old, new


@pytest.mark.parametrize('empty_roles', [False, True])
def test_cross_category_identity_selects_migration_not_similar_person(tmp_path, empty_roles):
    story, old, _ = migration_sample()
    if empty_roles:
        story['角色集'] = []
    old['修改元素']['角色集'].append({'名称': '族长', '详情': {}})
    source = tmp_path / 'old.json'
    save_json(source, old)
    scope = delta_repair_scope(source, '修改目标[测试家族]不存在', story)
    assert scope['/修改元素/角色集/0']['kind'] == 'move'
    assert '阵营集' in scope['/修改元素/角色集/0']['allowed_collections']
    assert '/修改元素/角色集/0/名称' not in scope
    assert '/修改元素/角色集/1/详情/提取理由' in scope


@pytest.mark.parametrize('damage', [None, 'addition', 'reason_append', 'reason_rewrite', 'list_append', 'list_replace',
                                  'birthday', 'text', 'duplicate', 'collision', 'unregistered'])
def test_preservation_recognizes_only_complete_one_to_one_migration(tmp_path, damage):
    story, old, new = migration_sample()
    target = new['修改元素']['阵营集'][0]
    if damage == 'addition':
        target['详情']['归类依据'] = '原文描述家族作为势力邀请其他组织。'
    elif damage == 'reason_append':
        target['详情']['提取理由'] += '；核对原文后将完整条目迁入阵营集。'
    elif damage == 'reason_rewrite':
        target['详情']['提取理由'] = '重新编写并丢弃原始理由'
    elif damage in {'list_append', 'list_replace'}:
        old['修改元素']['角色集'][0]['详情']['关联阵营'] = ['阵营:甲']
        target['详情']['关联阵营'] = ['阵营:甲', '阵营:乙'] if damage == 'list_append' else ['阵营:乙']
    elif damage == 'birthday':
        target['详情'].pop('生日')
    elif damage == 'text':
        target['详情']['格局'] = '删改'
    elif damage == 'duplicate':
        old['修改元素']['角色集'].append(copy.deepcopy(old['修改元素']['角色集'][0]))
    elif damage == 'collision':
        old['修改元素']['阵营集'] = [copy.deepcopy(target)]
    elif damage == 'unregistered':
        story['阵营集'] = []
        story['地点集'] = []
    before, after = tmp_path / 'old.json', tmp_path / 'new.json'
    save_json(before, old)
    save_json(after, new)
    errors = preservation_errors(before, after, story)
    assert bool(errors) is (damage not in {None, 'addition', 'reason_append', 'list_append'})


@pytest.mark.parametrize('accepted', [True, False])
def test_ambiguous_migration_requires_independent_review_before_publication(tmp_path, accepted):
    """Exercise routing, complete migration, preservation and the publication gate."""
    from fault_recovery import recover_artifact, REVIEW_CHECKS, digest
    story, old, new = migration_sample()
    output, pack = tmp_path / 'delta.json', tmp_path / 'repair.md'
    save_json(tmp_path / '故事结构_增量.json', story)
    save_json(output, old)
    pack.write_text('请依据原文修复归类，完整保留字段。', encoding='utf-8')
    calls, published = [], []

    def validate(root):
        data = json.loads((root / output.name).read_text(encoding='utf-8'))
        return (0, '') if data['修改元素'].get('阵营集') else (1, '修改目标[测试家族]不存在')

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        if kind == 'recovery_patch':
            save_json(expected, {'base_sha256': digest(root / 'reference.json'), 'changes': [],
                'moves': [{'path': '/修改元素/角色集/0', 'target_collection': '阵营集',
                           'evidence': '原文描述家族邀请友好势力参加仪式。'}]})
        elif kind == 'recovery_review':
            save_json(expected, {'passed': accepted, 'candidate_sha256': digest(root / output.name),
                                 'checks': dict.fromkeys(REVIEW_CHECKS, accepted),
                                 'issues': [] if accepted else ['原文无法确认类别'], 'reason': '离线原文复核'})
        else:
            pytest.fail(f'错误地使用了窄修复：{kind}')
        return 0

    def publish(candidate):
        published.append(True)
        output.write_bytes(candidate.read_bytes())
        return 0

    rc = recover_artifact(tmp_path, 'repair_chapter', pack, output, command='offline', worker=worker,
                          validate=validate, publish=publish, strategies=1, retries=0)
    assert calls == ['recovery_patch', 'recovery_review']
    assert rc == (0 if accepted else REPAIR_EXHAUSTED)
    assert bool(published) is accepted
    actual = json.loads(output.read_text(encoding='utf-8'))
    if accepted:
        assert actual['修改元素']['角色集'] == []
        moved = actual['修改元素']['阵营集'][0]
        assert moved['名称'] == '测试家族'
        assert moved['详情']['格局'] == '全部原始分析'
        assert moved['详情']['提取理由'].startswith(old['修改元素']['角色集'][0]['详情']['提取理由'])
    else:
        assert actual == old
