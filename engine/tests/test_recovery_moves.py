"""模型只选择已登记的迁移目标，宿主保留全部值并拒绝覆盖与越界。"""
import copy
import json

import pytest

from fault_recovery import digest, preservation_errors
from local_json_patch import delta_repair_scope, apply_scoped_patch, patch_shape_errors
from merge_delta import save_json


def migration(tmp_path):
    """复现萧家同时登记为地点和阵营、错误落入角色集的歧义。"""
    reference, candidate = tmp_path/'reference.json', tmp_path/'candidate.json'
    value = {'章节': '0037', '修改元素': {'角色集': [
        {'名称': '萧家', '详情': {'提取理由': '原文记载萧家邀请各方势力参加成人仪式，体现家族的社会地位。', '状态': '举办仪式'}}]},
        '新增元素': {'事件集': [{'名称': '仪式前', '时间': '0001-02-06T00:00:00', '详情': {'提取理由': '原文中的仪式场景'}}]}}
    story = {'角色集': [{'名称':'萧家大长老'}], '地点集': [{'名称':'萧家'}], '阵营集': [{'名称':'萧家'}]}
    save_json(reference, value)
    scope = delta_repair_scope(reference, '修改目标[萧家]不存在', story)
    patch = {'base_sha256': digest(reference), 'changes': [], 'moves': [
        {'path':'/修改元素/角色集/0', 'target_collection':'阵营集', 'evidence':'第37章原文称萧家为乌坦城三大家族之一，此条描述家族举办仪式。'}]}
    return reference, candidate, value, story, scope, patch


def test_registered_move_preserves_non_target_fields(tmp_path):
    reference, candidate, value, story, scope, patch = migration(tmp_path)
    assert '/修改元素/角色集/0' in scope
    assert not patch_shape_errors(patch)
    apply_scoped_patch(reference, candidate, patch, scope)
    result = json.loads(candidate.read_text(encoding='utf-8'))
    assert result['新增元素'] == value['新增元素']
    assert result['修改元素']['角色集'] == []
    moved = result['修改元素']['阵营集'][0]
    assert moved['名称'] == '萧家' and moved['详情']['状态'] == '举办仪式'
    assert moved['详情']['提取理由'].startswith(value['修改元素']['角色集'][0]['详情']['提取理由'])
    assert patch['moves'][0]['evidence'] in moved['详情']['提取理由']
    assert preservation_errors(reference, candidate, story) == []


@pytest.mark.parametrize('damage', ['wrong_target','outside_path','stale','duplicate','extra_value','empty_evidence'])
def test_move_rejects_unapproved_or_stale_edits_atomically(tmp_path, damage):
    reference, candidate, _, _, scope, patch = migration(tmp_path)
    assert scope, '必须先识别迁移范围，才能验证拒绝越界'
    if damage == 'wrong_target': patch['moves'][0]['target_collection'] = '物品集'
    if damage == 'outside_path': patch['moves'][0]['path'] = '/新增元素/事件集/0'
    if damage == 'stale': patch['base_sha256'] = '0'*64
    if damage == 'duplicate': patch['moves'].append(copy.deepcopy(patch['moves'][0]))
    if damage == 'extra_value': patch['moves'][0]['value'] = {}
    if damage == 'empty_evidence': patch['moves'][0]['evidence'] = ' '
    candidate.write_text('unchanged', encoding='utf-8')
    with pytest.raises(ValueError): apply_scoped_patch(reference, candidate, patch, scope)
    assert candidate.read_text(encoding='utf-8') == 'unchanged'


def test_existing_destination_patch_is_not_overwritten(tmp_path):
    reference, _, value, story, _, _ = migration(tmp_path)
    value['修改元素']['阵营集'] = [{'名称':'萧家', '详情':{'状态':'原有状态'}}]
    save_json(reference, value)
    assert delta_repair_scope(reference, '修改目标[萧家]不存在', story) == {}


def test_duplicate_registry_does_not_become_similar_person_rename(tmp_path):
    """重复登记无法稳定定位时不得退化为将家族改名成人物。"""
    reference, _, _, story, _, _ = migration(tmp_path)
    story.pop('地点集')
    story['阵营集'].append({'名称': '萧家'})
    assert delta_repair_scope(reference, '修改目标[萧家]不存在', story) == {}


def test_mixed_errors_can_escalate_beyond_partial_patch(tmp_path):
    """局部补丁不覆盖其他硬错误时，后续策略必须允许完整修复并继续复核。"""
    from fault_recovery import recover_artifact, REVIEW_CHECKS
    output, pack = tmp_path/'delta.json', tmp_path/'repair.md'
    save_json(output, {'修改元素': {'角色集': [{'名称': '萧炎', '性别': 8, '详情': {}}]}})
    save_json(tmp_path/'故事结构_增量.json', {'角色集': [{'名称': '萧炎'}]})
    pack.write_text('核对性别和原文依据。', encoding='utf-8')
    kinds = []

    def validate(root):
        value = json.loads((root/output.name).read_text(encoding='utf-8'))['修改元素']['角色集'][0]
        errors = ([] if value['性别'] == 0 else ['性别只能是有效枚举']) + ([] if value['详情'].get('提取理由') else ['缺少提取理由'])
        return (1, '\n'.join(errors)) if errors else (0, '')

    def worker(command, root, kind, task, expected, *args, **kwargs):
        kinds.append(kind)
        if kind == 'recovery_patch':
            save_json(expected, {'base_sha256': digest(root/'reference.json'),
                                'changes': [{'path': '/修改元素/角色集/0/详情/提取理由', 'value': '原文确认为男性。'}]})
        elif kind == 'auto_repair_chapter':
            save_json(expected, {'修改元素': {'角色集': [{'名称': '萧炎', '性别': 0, '详情': {'提取理由': '原文确认为男性。'}}]}})
        elif kind == 'recovery_review':
            save_json(expected, {'passed': True, 'candidate_sha256': digest(root/output.name),
                                 'checks': dict.fromkeys(REVIEW_CHECKS, True), 'issues': [], 'reason': '已核对原文。'})
        else:
            pytest.fail('第二策略应能处理范围外错误，无需耗尽诊断策略')
        return 0

    def publish(candidate):
        output.write_bytes(candidate.read_bytes())
        return validate(tmp_path)[0]

    assert recover_artifact(tmp_path, 'repair_chapter', pack, output, command='offline',
                            worker=worker, validate=validate, publish=publish, retries=0) == 0
    assert kinds == ['recovery_patch', 'auto_repair_chapter', 'recovery_review']
