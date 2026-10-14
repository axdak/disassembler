"""Large archives must not require paid global indexing before local MCP repair."""
import json
import sys
from pathlib import Path

import pytest

from bounded_generation import run_paged_recovery
from context_budget import inspect_pack
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worker_context import ContextReader, snapshot_context
from agent_worker import provider_command

COMMAND = 'python agent_worker.py --provider codebuddy'
KINDS = ['auto_repair_chapter', 'auto_audit_repair', 'recovery_patch',
         'recovery_diagnosis', 'recovery_review']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('tamper', [False, True])
def test_large_recovery_dispatches_directly_with_full_frozen_evidence(tmp_path, kind, tamper):
    """Keep ambiguous identities and all evidence; fence even failed worker writes."""
    story = tmp_path / '故事结构_增量.json'
    save_json(story, {'地点集': [{'名称': '萧家'}], '阵营集': [{'名称': '萧家'}],
                      '角色集': [{'名称': '无关角色', '详情': {'历史': '旧档案' * 150000}}]})
    before = story.read_bytes()
    pack, output = tmp_path / 'repair.md', tmp_path / 'candidate.json'
    pack.write_text('按完整原文核对萧家归类；不删除事实。', encoding='utf-8')
    save_json(output, {'待修候选': True})
    save_json(tmp_path / 'materials.json', {'materials': [{'path': story.name}]})
    save_json(pack.with_suffix('.context-inputs.json'), [str(story)])
    assert not inspect_pack(pack)['passed']
    calls, diagnostics = [], {}

    def worker(command, project, task, routed, target, *args, **kwargs):
        calls.append(task)
        assert task == kind, 'local recovery unexpectedly invoked an indexing model'
        assert inspect_pack(routed)['passed']
        frozen = tmp_path / 'frozen'
        frozen.mkdir()
        reader = ContextReader(snapshot_context(project, routed, [target], frozen))
        found = reader.story_search('萧家')['items']
        assert {item['collection'] for item in found} == {'地点集', '阵营集'}
        assert reader.allowed[reader.key(story)].read_bytes() == before
        if tamper:
            story.write_text('{}', encoding='utf-8')
        save_json(target, {'candidate': True})
        return 0

    rc = run_paged_recovery(worker, COMMAND, tmp_path, kind, pack, output,
                            evidence_index_root=tmp_path / 'index',
                            immutable_sources=[(story, '完整故事')], diagnostics=diagnostics)
    assert calls == [kind]
    assert rc == (REPAIR_EXHAUSTED if tamper else 0)
    assert story.read_bytes() == before
    assert diagnostics['dispatched'] is True
    assert diagnostics['worker_calls'] == 1
    assert diagnostics['context_mode'] == 'on_demand'


@pytest.mark.parametrize('kind', KINDS)
def test_recovery_provider_exposes_story_queries_without_candidate_mutations(kind):
    args = provider_command('codebuddy', Path('.'), '60', task_type=kind)
    allowed = args[args.index('--allowedTools') + 1]
    assert 'mcp__nd_context__story_search' in allowed
    assert 'mcp__nd_context__story_read_fields' in allowed
    assert 'candidate_submit' not in allowed


def test_other_provider_keeps_existing_full_material_admission(tmp_path):
    """Unknown commands cannot claim the built-in MCP capability boundary."""
    pack = tmp_path / 'task.md'
    pack.write_text('small task', encoding='utf-8')
    calls = []
    def worker(command, project, kind, actual_pack, output, *args, **kwargs):
        calls.append(actual_pack)
        return 0
    assert run_paged_recovery(worker, 'external-worker', tmp_path, 'recovery_patch',
                              pack, tmp_path / 'out.json') == 0
    assert calls == [pack]


@pytest.mark.parametrize('accepted', [False, True])
def test_large_ambiguous_recovery_keeps_review_and_publication_fence(tmp_path, accepted):
    """End-to-end scheduler: zero index calls, repair then independent review."""
    from fault_recovery import recover_artifact, REVIEW_CHECKS, digest
    from test_recovery_index_regression import migration_sample
    story, old, new = migration_sample()
    story['角色集'].append({'名称': '其他人物', '详情': {'历史': '旧记录' * 150000}})
    save_json(tmp_path / '故事结构_增量.json', story)
    output, pack = tmp_path / 'delta.json', tmp_path / 'repair.md'
    save_json(output, old)
    pack.write_text('根据原文核对归类，保留完整内容。', encoding='utf-8')
    calls, published = [], []

    def validate(root):
        candidate = json.loads((root / output.name).read_text(encoding='utf-8'))
        return (0, '') if candidate['修改元素'].get('阵营集') else (1, '修改目标[测试家族]不存在')

    def worker(command, root, kind, task, expected, *args, **kwargs):
        calls.append(kind)
        if kind == 'recovery_patch':
            save_json(expected, {'base_sha256': digest(root / 'reference.json'), 'changes': [],
                'moves': [{'path': '/修改元素/角色集/0', 'target_collection': '阵营集',
                           'evidence': '原文描述家族邀请友好势力参加仪式。'}]})
        elif kind == 'recovery_review':
            save_json(expected, {'passed': accepted, 'candidate_sha256': digest(root / output.name),
                                 'checks': dict.fromkeys(REVIEW_CHECKS, accepted),
                                 'issues': [] if accepted else ['原文无法确认'], 'reason': '离线复核'})
        else:
            pytest.fail(f'不应为局部错误生成全库索引: {kind}')
        return 0

    def publish(candidate):
        published.append(True)
        output.write_bytes(candidate.read_bytes())
        return 0

    assert recover_artifact(tmp_path, 'repair_chapter', pack, output, command=COMMAND,
                            worker=worker, validate=validate, publish=publish,
                            strategies=1, retries=0) == (0 if accepted else REPAIR_EXHAUSTED)
    assert calls == ['recovery_patch', 'recovery_review']
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
