"""真实磁盘上的恢复断点：模型仅替换外部边界，校验、预算和发布保持真实。"""
import json
import threading
import shutil

import pytest

from test_fault_recovery import scenario, invoke, state
from repair_budget import REPAIR_EXHAUSTED
from merge_delta import save_json


@pytest.mark.parametrize("code", [130, 124, 1073807364, -1073741510])
def test_review_interruption_resumes_candidate_without_regeneration(tmp_path, code):
    """复核服务中断不应抛弃已校验候选或重新支付生成调用。"""
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options["worker"]
    interrupted = False

    def worker(*args, **kwargs):
        nonlocal interrupted
        if args[2] == "recovery_review" and not interrupted:
            interrupted = True
            calls.append(args[2])
            return code
        return original(*args, **kwargs)

    options["worker"] = worker
    expected = 130 if code in {1073807364, -1073741510} else code
    assert invoke(tmp_path, output, pack, options) == expected
    assert not published
    spent = len(state(tmp_path)["resource_calls"])
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ["auto_repair_chapter", "recovery_review", "recovery_review"]
    assert len(state(tmp_path)["resource_calls"]) == spent + 1
    assert len(published) == 1


def test_validation_crash_resumes_completed_generation(tmp_path):
    """生成返回成功后、校验过程中宿主退出，续接仍使用该完整候选。"""
    output, pack, _, calls, published, options = scenario(tmp_path)
    validate = options["validate"]

    def interrupted(root):
        rc, feedback = validate(root)
        if rc == 0:
            raise RuntimeError("模拟生成后校验进程退出")
        return rc, feedback

    options["validate"] = interrupted
    with pytest.raises(RuntimeError, match="模拟生成后"):
        invoke(tmp_path, output, pack, options)
    options["validate"] = validate
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ["auto_repair_chapter", "recovery_review"]
    assert len(published) == 1


def test_host_termination_keeps_semantic_strategy(tmp_path):
    """原生宿主退出不能消耗定点策略；实际调用仍计入预算。"""
    output, pack, _, calls, _, options = scenario(tmp_path)
    options["worker"] = lambda *a, **kw: calls.append(a[2]) or 1073807364
    assert invoke(tmp_path, output, pack, options) == 130
    assert state(tmp_path)["next_strategy"] == 0
    assert len(state(tmp_path)["resource_calls"]) == 1


@pytest.mark.parametrize("damage", ["candidate", "evidence"])
def test_resumed_review_rejects_changed_checkpoint(tmp_path, damage):
    """保存的候选或冻结证据被修改后，不得用旧断点发布。"""
    output, pack, source, calls, published, options = scenario(tmp_path)
    original = options["worker"]
    workspace = []

    def worker(*args, **kwargs):
        if args[2] == "recovery_review":
            workspace.append(args[1])
            calls.append(args[2])
            return 130
        return original(*args, **kwargs)

    options["worker"] = worker
    assert invoke(tmp_path, output, pack, options) == 130
    target = workspace[0] / (output.name if damage == "candidate" else source.relative_to(tmp_path))
    target.write_text('{}', encoding='utf-8')
    options["worker"] = original
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published


def test_resume_does_not_refund_worker_budget(tmp_path, monkeypatch):
    """待复核断点不能绕过已耗尽的实际调用预算。"""
    monkeypatch.setenv("ND_RECOVERY_MAX_WORKER_CALLS", "2")
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options['worker']
    options['worker'] = lambda *a, **kw: (calls.append(a[2]) or 130) if a[2] == 'recovery_review' else original(*a, **kw)
    assert invoke(tmp_path, output, pack, options) == 130
    options['worker'] = original
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert len(calls) == 2 and not published


def test_accepted_review_resumes_publication_without_model(tmp_path):
    """复核已经成功后取消，续接直接进入相同发布门。"""
    output, pack, _, calls, published, options = scenario(tmp_path)
    original = options['worker']
    stop = threading.Event()
    options['cancel'] = stop

    def worker(*args, **kwargs):
        rc = original(*args, **kwargs)
        if args[2] == 'recovery_review':
            stop.set()
        return rc

    options['worker'] = worker
    assert invoke(tmp_path, output, pack, options) == 130
    assert not published
    stop.clear()
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ['auto_repair_chapter', 'recovery_review']
    assert len(published) == 1


def test_changed_formal_evidence_requires_new_review(tmp_path):
    """正式输入版本变化不能复用前一版本的验收阶段。"""
    output, pack, source, calls, _, options = scenario(tmp_path)
    original = options['worker']
    options['worker'] = lambda *a, **kw: (calls.append(a[2]) or 130) if a[2] == 'recovery_review' else original(*a, **kw)
    assert invoke(tmp_path, output, pack, options) == 130
    source.write_text('修改后的正式原文证据。', encoding='utf-8')
    options['worker'] = original
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ['auto_repair_chapter', 'recovery_review', 'auto_repair_chapter', 'recovery_review']
    assert state(tmp_path)['generation'] == 2


def test_normalization_copy_crash_replays_immutable_generated_candidate(tmp_path, monkeypatch):
    """宿主规范化复制已发生、阶段记录未切换时，恢复生成快照重新验收。"""
    output, pack, _, calls, published, options = scenario(tmp_path)
    validate, copy = options['validate'], shutil.copy2

    def normalize(root):
        rc, feedback = validate(root)
        if rc == 0:
            data = json.loads((root/output.name).read_text(encoding='utf-8'))
            data['normalized'] = True
            save_json(root/output.name, data)
        return rc, feedback

    def interrupted(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        if (str(source.parent.name).startswith('validation_') and target.name == output.name
                and 'attempt_' in str(target) and json.loads(target.read_text(encoding='utf-8')).get('normalized')):
            raise RuntimeError('规范化复制完成后退出')
        return result

    options['validate'] = normalize
    monkeypatch.setattr(shutil, 'copy2', interrupted)
    with pytest.raises(RuntimeError, match='规范化复制'):
        invoke(tmp_path, output, pack, options)
    monkeypatch.setattr(shutil, 'copy2', copy)
    assert invoke(tmp_path, output, pack, options) == 0
    assert calls == ['auto_repair_chapter', 'recovery_review']
    assert len(published) == 1


def test_deduplicated_evidence_still_invalidates_review_checkpoint(tmp_path):
    """读材料可去重，完整性校验必须保护包括同文副本在内的全部证据。"""
    output, pack, source, calls, published, options = scenario(tmp_path)
    analysis = tmp_path/'章节处理/第001章_测试.md'
    analysis.parent.mkdir()
    analysis.write_bytes(source.read_bytes())
    original, stop, workspaces = options['worker'], threading.Event(), []
    options['cancel'] = stop

    def worker(*args, **kwargs):
        rc = original(*args, **kwargs)
        if args[2] == 'recovery_review':
            workspaces.append(args[1])
            stop.set()
        return rc

    options['worker'] = worker
    assert invoke(tmp_path, output, pack, options) == 130
    (workspaces[0]/analysis.relative_to(tmp_path)).write_text('被替换的分析', encoding='utf-8')
    stop.clear()
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert not published
