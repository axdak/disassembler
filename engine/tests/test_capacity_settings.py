"""Offline regression for isolated settings, child workers and capacity splitting."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from context_budget import ContextBudget, capacity_scope


def test_concurrent_budgets_do_not_change_environment_or_each_other():
    """Different books and their nested workers must retain their own settings."""
    before = dict(os.environ)
    barrier = threading.Barrier(2)

    def book(limit):
        with capacity_scope({'model_input_tokens': limit}):
            barrier.wait(timeout=3)
            with ThreadPoolExecutor(max_workers=1) as pool:
                child = pool.submit(copy_context().run, ContextBudget.from_env).result()
            return child.usable_input

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(book, [60000, 90000]))
    assert results == [30000, 60000]
    assert dict(os.environ) == before
    assert ContextBudget.from_env().usable_input == 250000


def test_scope_restored_after_failure():
    with capacity_scope({'model_input_tokens': 60000}):
        with pytest.raises(RuntimeError), capacity_scope({'model_input_tokens': 90000}):
            raise RuntimeError('cancelled')
        assert ContextBudget.from_env().usable_input == 30000


def test_orchestrator_carries_saved_limits(tmp_path, monkeypatch):
    from orchestrator import Orchestrator, Config
    seen = []
    monkeypatch.setattr(Orchestrator, '_run', lambda self: seen.append(ContextBudget.from_env()) or 0)
    config = Config(model_input_tokens=90000, model_output_tokens=10000,
                    model_context_tokens=80000, model_reserve_tokens=5000)
    assert Orchestrator(tmp_path, config).run() == 0
    assert seen[0].usable_input == 65000


@pytest.mark.parametrize('multi', [False, True])
def test_worker_environment_receives_budget_and_call_options(tmp_path, monkeypatch, multi):
    import run_pipeline as pipeline
    pack, output = tmp_path / 'task.md', tmp_path / 'report.md'
    pack.write_text('完整材料', encoding='utf-8')
    calls = []

    def shell(command, root, env, timeout):
        calls.append((env, timeout))
        output.write_text('完整报告', encoding='utf-8')
        return 0, ''

    monkeypatch.setattr(pipeline, 'stream_shell_command', shell)
    settings = dict(model_input_tokens=90000, model_output_tokens=12000,
                    model_context_tokens=100000, model_reserve_tokens=7000)
    with capacity_scope(settings):
        worker = pipeline._run_multi_output_worker if multi else pipeline._run_worker
        assert worker('offline', tmp_path, 'report', pack, [output] if multi else output,
                      timeout=91, retries=0) == 0
    assert len(calls) == 1 and calls[0][1] == 91
    assert calls[0][0]['ND_CONTEXT_INPUT_TOKENS'] == '90000'
    assert calls[0][0]['ND_CONTEXT_OUTPUT_TOKENS'] == '12000'
    assert calls[0][0]['ND_CONTEXT_RESERVE_TOKENS'] == '7000'
    receipt = json.loads(pack.with_suffix('.md.capacity.json').read_text(encoding='utf-8'))
    assert receipt['usable_input_tokens'] == 81000


def test_internal_pack_process_receives_same_capacity(tmp_path, monkeypatch):
    import run_pipeline as pipeline
    seen = []
    monkeypatch.setattr(pipeline.subprocess, 'run', lambda *a, **kw:
                        seen.append(kw['env']) or SimpleNamespace(returncode=0, stdout=''))
    with capacity_scope({'model_input_tokens': 60000}):
        assert pipeline.run_cmd(['offline']) == 0
    assert seen[0]['ND_CONTEXT_INPUT_TOKENS'] == '60000'


def test_saved_small_capacity_splits_without_losing_evidence(tmp_path):
    from capacity_tree import CapacityTree, merge_markdown
    from test_capacity_tree import fake_worker
    source = tmp_path / 'existing.md'
    content = '原始完整分析与历史不能截断。' * 1200
    source.write_text(content, encoding='utf-8')
    calls = []
    with capacity_scope(dict(model_input_tokens=7000, model_context_tokens=10000,
                             model_output_tokens=1200, model_reserve_tokens=1000)):
        tree = CapacityTree(tmp_path, 'offline', fake_worker(calls), '保留全部证据与格式',
                            '报告.md', [(source, '完整材料')])
        _, pieces, _ = tree.run()
        coverage = json.loads((tree.root / 'coverage.json').read_text(encoding='utf-8'))
        assert coverage[0]['start'] == 0 and coverage[-1]['end'] == len(content)
        assert all(a['end'] == b['start'] for a, b in zip(coverage, coverage[1:]))
        combined = merge_markdown(pieces)
        assert all(f"字符区间：{part['start']}-{part['end']}" in combined for part in coverage)
        count = len(calls)
        tree.run()
        assert len(calls) == count
    assert source.read_text(encoding='utf-8') == content


@pytest.mark.parametrize('interleaved', [False, True])
def test_chapter_thread_paths_inherit_capacity(tmp_path, monkeypatch, interleaved):
    """Exercise real pipeline thread creation, not just ContextVar primitives."""
    import run_pipeline as pipeline
    from test_analysis_concurrency import _write_project
    _write_project(tmp_path, 3)
    seen = []

    def worker(command, project, kind, pack, output, *args, **kwargs):
        seen.append(ContextBudget.from_env().usable_input)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{}' if output.suffix == '.json' else '# 分析', encoding='utf-8')
        return 0

    monkeypatch.setattr(pipeline, 'run_generation_worker', worker)
    monkeypatch.setattr(pipeline, 'validate_chapter_analysis_file', lambda path: (True, []))
    monkeypatch.setattr(pipeline, 'commit_chapter', lambda project, seq: 0)
    with capacity_scope({'model_input_tokens': 90000}):
        if interleaved:
            rc = pipeline.cmd_run_interleaved(tmp_path, run_mode='worker', worker_provider='manual',
                                              chapter_command='offline', audit_command='offline',
                                              concurrency=2, audit_interval=0)
        else:
            rc = pipeline.cmd_run_analysis(tmp_path, chapter_command='offline', concurrency=2,
                                           worker_loop='continuous')
    assert rc == 0 and len(seen) == (6 if interleaved else 3)
    assert set(seen) == {60000}


def test_checkpoint_cannot_bypass_changed_capacity(tmp_path):
    from worker_checkpoint import run_checkpointed_partial
    pack, output = tmp_path / 'pack.md', tmp_path / 'report.md'
    pack.write_text('## 原任务包内容\n\n原始材料', encoding='utf-8')
    calls = []

    def worker(*args, **kwargs):
        calls.append(1)
        output.write_text('原分析内容', encoding='utf-8')
        return 0

    for limit in [90000, 90000, 120000]:
        with capacity_scope({'model_input_tokens': limit}):
            assert run_checkpointed_partial(worker, 'offline', tmp_path, 'report', pack, output) == 0
    assert len(calls) == 2
