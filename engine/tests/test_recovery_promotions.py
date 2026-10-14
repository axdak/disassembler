"""Chapter 71: a verified new fact must survive modify-to-add relocation."""
import copy
import json

import pytest
from fault_recovery import digest, preservation_errors, recover_artifact, REVIEW_CHECKS
from local_json_patch import apply_scoped_patch, delta_repair_scope
from merge_delta import save_json

NAME = '坊市利润为家族收入大头设定'
EVIDENCE = '第071章三长老明确说坊市利润占家族收入大头，正式档案没有同一设定。'


def example(tmp_path, known=True):
    reference = tmp_path/'delta.json'
    item = {'名称': NAME, '详情': {'规则线索': '坊市利润占萧家收入大头，损失巨大。',
            '首次章节': '0071', '最近章节': '0071', '提取理由': '本章明确交代该设定，故新增。'}}
    before = {'新增元素': {'其他事项集': []}, '修改元素': {'其他事项集': [item,
        {'名称': '炼药师影响坊市竞争', '详情': {'提取理由': '已有机制本章继续产生影响。'}}]}}
    story = {'其他事项集': [{'名称': '炼药师影响坊市竞争'}]} if known else {}
    save_json(reference, before)
    scope = delta_repair_scope(reference, f'修改目标[{NAME}]不存在', story)
    patch = {'base_sha256': digest(reference), 'changes': [], 'moves': [{
        'path': '/修改元素/其他事项集/0', 'target_section': '新增元素',
        'target_collection': '其他事项集', 'evidence': EVIDENCE}]}
    return reference, tmp_path/'candidate.json', before, story, scope, patch


@pytest.mark.parametrize('known', [True, False])
def test_evidence_backed_new_fact_can_be_promoted_without_rewriting(tmp_path, known):
    reference, candidate, before, story, scope, patch = example(tmp_path, known)
    apply_scoped_patch(reference, candidate, patch, scope)
    after = json.loads(candidate.read_text(encoding='utf-8'))
    assert after['修改元素']['其他事项集'] == before['修改元素']['其他事项集'][1:]
    new = after['新增元素']['其他事项集'][0]
    old = before['修改元素']['其他事项集'][0]
    assert new['名称'] == old['名称']
    assert all(new['详情'][key] == value for key,value in old['详情'].items() if key != '提取理由')
    assert new['详情']['提取理由'].startswith(old['详情']['提取理由']) and EVIDENCE in new['详情']['提取理由']
    assert preservation_errors(reference, candidate, story) == []


def test_full_candidate_lossless_promotion_is_not_mistaken_for_deletion(tmp_path):
    reference,candidate,before,story,_,_=example(tmp_path)
    after=copy.deepcopy(before)
    moved=after['修改元素']['其他事项集'].pop(0)
    moved['详情']['入库依据']=EVIDENCE
    after['新增元素']['其他事项集'].append(moved)
    save_json(candidate,after)
    assert preservation_errors(reference,candidate,story)==[]


@pytest.mark.parametrize('reason', ['',None])
def test_promotion_can_fill_missing_reason_without_false_deletion(tmp_path,reason):
    reference,candidate,before,story,_,patch=example(tmp_path)
    before['修改元素']['其他事项集'][0]['详情']['提取理由']=reason
    save_json(reference,before)
    scope=delta_repair_scope(reference,f'修改目标[{NAME}]不存在',story)
    patch['base_sha256']=digest(reference)
    patch['changes']=[{'path':'/修改元素/其他事项集/0/详情/提取理由','value':EVIDENCE}]
    apply_scoped_patch(reference,candidate,patch,scope)
    assert preservation_errors(reference,candidate,story)==[]


@pytest.mark.parametrize('damage', ['delete','rewrite_short','rewrite_reason','registered','alias','collision','duplicate','wrong_section','wrong_collection'])
def test_promotion_never_bypasses_preservation_or_scope(tmp_path, damage):
    reference, candidate, before, story, scope, patch = example(tmp_path)
    if damage in {'wrong_section', 'wrong_collection', 'collision', 'registered', 'alias', 'duplicate'}:
        if damage == 'wrong_section': patch['moves'][0]['target_section'] = '修改元素'
        if damage == 'wrong_collection': patch['moves'][0]['target_collection'] = '角色集'
        if damage == 'registered': story['角色集'] = [{'名称': NAME}]
        if damage == 'alias': story['其他事项集'][0]['别名'] = [NAME]
        if damage == 'collision': before['新增元素']['其他事项集'] = [copy.deepcopy(before['修改元素']['其他事项集'][0])]
        if damage == 'duplicate': before['修改元素']['其他事项集'].append(copy.deepcopy(before['修改元素']['其他事项集'][0]))
        save_json(reference, before)
        patch['base_sha256'] = digest(reference)
        scope = delta_repair_scope(reference, f'修改目标[{NAME}]不存在', story)
        candidate.write_text('untouched',encoding='utf-8')
        with pytest.raises(ValueError): apply_scoped_patch(reference,candidate,patch,scope)
        assert candidate.read_text(encoding='utf-8') == 'untouched'
    else:
        after = copy.deepcopy(before)
        moved = after['修改元素']['其他事项集'].pop(0)
        if damage != 'delete':
            if damage == 'rewrite_short': moved['详情']['最近章节'] = '0070'
            if damage == 'rewrite_reason': moved['详情']['提取理由'] = '换了理由'
            after['新增元素']['其他事项集'].append(moved)
        save_json(candidate,after)
        assert preservation_errors(reference,candidate,story)


@pytest.mark.parametrize('task', ['repair_chapter','audit_repair'])
def test_promotion_uses_local_patch_then_validators_and_review(tmp_path, task):
    output, _, _, story, _, patch = example(tmp_path)
    save_json(tmp_path/'故事结构_增量.json',story)
    pack = tmp_path/'repair.md'
    pack.write_text('只能核对本章原文修复错分区，不得删内容。',encoding='utf-8')
    kinds = []
    def validate(root):
        doc=json.loads((root/output.name).read_text(encoding='utf-8'))
        if any(x['名称']==NAME for x in doc['修改元素']['其他事项集']):
            return 1,f'修改目标[{NAME}]不存在'
        return 0,''
    def worker(command,root,kind,task_path,expected,*args,**kwargs):
        kinds.append(kind)
        if kind=='recovery_patch':
            patch['base_sha256']=digest(root/'reference.json')
            save_json(expected,patch)
        else:
            assert kind=='recovery_review'
            save_json(expected,{'passed':True,'candidate_sha256':digest(root/output.name),
                'checks':dict.fromkeys(REVIEW_CHECKS,True),'issues':[],'reason':'原文与原字段已核对。'})
        return 0
    def publish(candidate):
        output.write_bytes(candidate.read_bytes())
        return validate(tmp_path)[0]
    assert recover_artifact(tmp_path,task,pack,output,command='fake',worker=worker,
                            validate=validate,publish=publish)==0
    assert kinds==['recovery_patch','recovery_review']
