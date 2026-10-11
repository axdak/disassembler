"""Offline crash, cancellation and resume tests; never invoke a real provider."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import run_pipeline as pipeline
import orchestrator
from repair_budget import REPAIR_EXHAUSTED, begin_retry_session
from worker_admission import WorkerAdmission
from worker_checkpoint import run_checkpointed_partial
from test_audit_pack import empty_story, empty_delta, valid_analysis, write_chapter_source, write_json


def chapter_project(root):
    write_json(root / "故事结构_增量.json", empty_story())
    chapter = write_chapter_source(root, 1)
    pipeline.init_dirs(root)
    paths = pipeline.artifact_paths(root, chapter)
    paths["analysis"].write_text(valid_analysis(1), encoding="utf-8")
    write_json(paths["delta"], empty_delta(1))
    story = pipeline.ensure_story(root)
    return chapter, paths, story


@pytest.mark.parametrize("failed_step", ["merge_delta.py", "normalize_story_schema.py",
                                         "compress_tags.py", "validate_structure.py", "diff_structure.py"])
def test_chapter_failure_never_publishes_partial_structure(tmp_path, monkeypatch, failed_step):
    chapter, paths, story = chapter_project(tmp_path)
    original = story.read_bytes()
    real = pipeline.run_cmd

    def fail_candidate(command, *args, **kwargs):
        if Path(command[1]).name == failed_step and any(".chapter_candidate_" in str(arg) for arg in command):
            return 7
        return real(command, *args, **kwargs)

    monkeypatch.setattr(pipeline, "run_cmd", fail_candidate)
    assert pipeline.commit_chapter(tmp_path, 1) == 7
    assert story.read_bytes() == original
    assert not pipeline.is_chapter_complete(tmp_path, chapter)
    assert not paths["after"].exists()


@pytest.mark.parametrize("after_publish", [False, True])
def test_chapter_recovers_crash_at_publication_boundary(tmp_path, monkeypatch, after_publish):
    chapter, paths, story = chapter_project(tmp_path)
    original = story.read_bytes()
    real_command, real_replace = pipeline.run_cmd, pipeline.os.replace
    merges = []

    def changed_merge(command, *args, **kwargs):
        rc = real_command(command, *args, **kwargs)
        if Path(command[1]).name == "merge_delta.py":
            merges.append(command)
            target = Path(command[2])
            data = json.loads(target.read_text(encoding="utf-8"))
            data["介绍"]["描述"] = "已合并的新章节证据"
            write_json(target, data)
        return rc

    def crash(source, destination):
        if str(source).endswith(".chapter_candidate_001.json") and Path(destination) == story:
            if after_publish:
                real_replace(source, destination)
            raise RuntimeError("simulated abrupt stop")
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline, "run_cmd", changed_merge)
    monkeypatch.setattr(pipeline.os, "replace", crash)
    with pytest.raises(RuntimeError, match="simulated"):
        pipeline.commit_chapter(tmp_path, 1)
    assert not pipeline.is_chapter_complete(tmp_path, chapter)
    assert pipeline.latest_snapshot_seq(tmp_path) is None
    assert (story.read_bytes() != original) is after_publish
    monkeypatch.setattr(pipeline.os, "replace", real_replace)
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    assert len(merges) == (1 if after_publish else 2)
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert pipeline.latest_snapshot_seq(tmp_path) == 1


def test_completed_snapshot_cache_invalidates_changed_files(tmp_path, monkeypatch):
    from test_chapter_completion import build_project
    chapter = build_project(tmp_path)
    calls = []
    monkeypatch.setattr(pipeline, "validate_structure_file", lambda *a, **k: (calls.append(a) or True, [], []))
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert len(calls) == 1
    pipeline.artifact_paths(tmp_path, chapter)["after"].write_text('{"changed": 1}', encoding="utf-8")
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    assert len(calls) == 2


def test_final_repair_transport_and_outer_retries_share_one_budget(tmp_path, monkeypatch):
    orch = orchestrator.Orchestrator(tmp_path, config=orchestrator.Config(
        worker_provider="agy", worker_retries=3, max_repair_rounds=2))
    repair = tmp_path / "质量治理/最终审计/repair_final.md"
    repair.parent.mkdir(parents=True)
    repair.write_text("repair", encoding="utf-8")
    calls = []
    monkeypatch.setattr(orchestrator, "cmd_commit_final_draft", lambda *a: 1)
    monkeypatch.setattr(pipeline, "wait_before_worker_retry", lambda attempt: False)
    monkeypatch.setattr(pipeline, "stream_shell_command", lambda *a: (calls.append(a) or 9, "transport failure"))
    assert orch._stage_commit_final_draft() == REPAIR_EXHAUSTED
    assert orch._stage_commit_final_draft() == REPAIR_EXHAUSTED
    assert len(calls) == 2
    begin_retry_session(tmp_path)  # Legacy session reset cannot reset automatic recovery.
    assert orch._stage_commit_final_draft() == REPAIR_EXHAUSTED
    assert len(calls) == 2


def test_worker_admission_cancels_waiter_and_releases_after_exception():
    gate = WorkerAdmission(1)
    cancelled, entered = threading.Event(), threading.Event()
    result = []

    def waiter():
        entered.set()
        with gate.slot(cancelled) as acquired:
            result.append(acquired)

    with pytest.raises(RuntimeError):
        with gate.slot() as acquired:
            assert acquired
            thread = threading.Thread(target=waiter)
            thread.start()
            assert entered.wait(2)
            cancelled.set()
            thread.join(2)
            assert not thread.is_alive() and result == [False]
            raise RuntimeError("worker failed")
    with gate.slot() as acquired:
        assert acquired


def test_partial_checkpoint_reuses_only_matching_uncorrupted_success(tmp_path):
    calls = []

    def worker(command, project, task, pack, output, **kwargs):
        calls.append(command)
        output.write_text("完整分析结果" + str(len(calls)), encoding="utf-8")
        return 0

    def run(folder, material="同一完整证据", command="local-fake", reuse=True):
        directory = tmp_path / folder
        directory.mkdir(exist_ok=True)
        output, pack = directory / "partial.md", directory / "pack.md"
        pack.write_text(f"只写入：{output}\n\n## 原任务包内容\n\n{material}", encoding="utf-8")
        assert run_checkpointed_partial(worker, command, tmp_path, "summary", pack, output, reuse=reuse) == 0
        return output

    first = run("attempt1").read_bytes()
    assert run("attempt2").read_bytes() == first
    assert len(calls) == 1
    cached = next((tmp_path / "质量治理/分包检查点").glob("*.md"))
    cached.write_text("damaged", encoding="utf-8")
    run("attempt3")
    run("attempt4", material="证据发生变化")
    run("attempt5", command="changed-model")
    assert len(calls) == 4
    write_json(tmp_path / "故事结构_增量.json", {"介绍": "新增元素详情"})
    run("attempt6")
    assert len(calls) == 5
    run("explicit-force", reuse=False)
    assert len(calls) == 6


def test_long_analysis_is_fully_covered_by_bounded_packs(tmp_path):
    from analysis_context_pack import build_chapter_block, split_rendered_pack
    chapter, paths, _ = chapter_project(tmp_path)
    analysis = "完整分析证据\n" * 4000 + "尾部唯一证据"
    paths["analysis"].write_text(analysis, encoding="utf-8")
    block = build_chapter_block(tmp_path, {"seq": 1, "filename": chapter.name}, "summary", "none", 6000, 12000)
    assert analysis in block
    header = "# 分析任务\n## 输出契约\n保留所有证据\n"
    packs = split_rendered_pack(header + "## 章节材料\n" + block, 5000)
    assert len(packs) > 1 and all(len(pack) <= 5000 and header in pack for pack in packs)
    assert "".join(pack.split("## 章节材料\n", 1)[1] for pack in packs) == block


def test_failed_partial_is_never_reused(tmp_path):
    output, pack = tmp_path / "partial.md", tmp_path / "pack.md"
    pack.write_text(f"{output}\n## 原任务包内容\n\n证据", encoding="utf-8")
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        output.write_text("写了一半", encoding="utf-8")
        return 9

    for _ in range(2):
        assert run_checkpointed_partial(failed, "local", tmp_path, "summary", pack, output) == 9
    assert len(calls) == 2
    assert not (tmp_path / "质量治理/分包检查点").exists()


def test_both_worker_apis_share_model_admission(tmp_path, monkeypatch):
    gate = WorkerAdmission(1)
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    results = []
    monkeypatch.setattr(pipeline, "worker_admission", gate)

    def worker(command, project, env, timeout):
        if command == "first":
            started.set()
            assert release.wait(3)
        else:
            second_started.set()
        Path(env["ND_EXPECTED_OUTPUT"]).write_text(command, encoding="utf-8")
        return 0, command

    monkeypatch.setattr(pipeline, "stream_shell_command", worker)
    first = threading.Thread(target=lambda: results.append(pipeline.run_worker(
        "first", tmp_path, "analysis", tmp_path / "pack", tmp_path / "one.md")))
    second = threading.Thread(target=lambda: results.append(pipeline.run_multi_output_worker(
        "second", tmp_path, "summary", tmp_path / "pack", [tmp_path / "two.md"])))
    try:
        first.start()
        assert started.wait(2)
        second.start()
        assert not second_started.wait(0.1)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert second_started.is_set() and results == [0, 0]


def test_worker_log_is_durable_before_process_completion(tmp_path):
    """A stopped backend can retain output already emitted by its local child."""
    worker = tmp_path / "local_worker.py"
    release, log = tmp_path / "release", tmp_path / "worker.log"
    worker.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "print('evidence before completion', flush=True)\n"
        "while not Path(os.environ['TEST_RELEASE']).exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    result = []
    env = dict(os.environ, ND_WORKER_LOG_PATH=str(log), TEST_RELEASE=str(release))
    command = subprocess.list2cmdline([sys.executable, str(worker)])
    thread = threading.Thread(target=lambda: result.append(pipeline.stream_shell_command(command, tmp_path, env, 5)))
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while (not log.is_file() or not log.read_text(encoding="utf-8")) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert thread.is_alive()
        assert "evidence before completion" in log.read_text(encoding="utf-8")
    finally:
        release.touch()
        thread.join(6)
    assert not thread.is_alive() and result[0][0] == 0


def test_progress_failure_leaves_resumable_receipt(tmp_path, monkeypatch):
    chapter, paths, _ = chapter_project(tmp_path)
    real_progress = pipeline._record_chapter_progress

    def interrupted(*args):
        raise OSError("simulated progress write interruption")

    monkeypatch.setattr(pipeline, "_record_chapter_progress", interrupted)
    with pytest.raises(OSError, match="progress write"):
        pipeline.commit_chapter(tmp_path, 1)
    assert not pipeline.is_chapter_complete(tmp_path, chapter)
    monkeypatch.setattr(pipeline, "_record_chapter_progress", real_progress)
    # Recovery must not need another merge, repair, or model call.
    monkeypatch.setattr(pipeline, "run_cmd", lambda *a, **k: pytest.fail("unexpected replay"))
    # Publishing changed only the file identity; its bytes were already normalized.
    monkeypatch.setattr(pipeline, "ensure_story", lambda root: root / "故事结构_增量.json")
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    assert pipeline.is_chapter_complete(tmp_path, chapter)
