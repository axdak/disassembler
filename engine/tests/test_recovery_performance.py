"""Exercise measured I/O reuse without weakening evidence or candidate checks."""
import hashlib
import json
import os
import tempfile
import pytest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from capacity_tree import CapacityTree
from merge_delta import save_json
from repair_budget import REPAIR_EXHAUSTED


@pytest.mark.parametrize("filename", [
    "validate_delta.py", "validate_structure.py", "validate_chapter_analysis.py",
    "merge_delta.py", "apply_governance_ops.py", "bounded_final.py",
    "engine_contract.py", "repair_llm_json.py",
])
def test_changed_acceptance_code_reopens_exhausted_recovery(tmp_path, monkeypatch, filename):
    """验收或发布代码变化须重新评估耗尽账本，不能只跟踪调度层变化。"""
    import fault_recovery
    from test_fault_recovery import scenario, invoke, state
    output, pack, _, calls, published, options = scenario(tmp_path)
    options["worker"] = lambda *args, **kwargs: calls.append(args[2]) or 1
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert len(calls) == 3
    # 相同代码不得重获预算；仅修改指定验收依赖的摘要来模拟一次源码升级。
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert len(calls) == 3
    original_digest = fault_recovery.code_digest
    monkeypatch.setattr(fault_recovery, "code_digest", lambda path:
        "changed-acceptance-code" if path.name == filename else original_digest(path))
    assert invoke(tmp_path, output, pack, options) == REPAIR_EXHAUSTED
    assert state(tmp_path)["generation"] == 2
    assert [item["strategy"] for item in state(tmp_path)["attempts"]] == ["targeted", "evidence", "diagnosed"]
    assert len(calls) == 6
    assert not published


def test_capacity_code_fingerprints_do_not_reread_unchanged_code(tmp_path, monkeypatch):
    """The second real tree must spend zero byte reads on unchanged engine code."""
    engine = Path(__file__).resolve().parents[1]
    source = tmp_path / "evidence.md"
    source.write_text("完整原文", encoding="utf-8")
    original = Path.read_bytes
    reads = []
    import code_fingerprint
    monkeypatch.setattr(code_fingerprint, "_CACHE", OrderedDict())

    def measured(path):
        content = original(path)
        if path.is_relative_to(engine) and path.suffix in {".py", ".j2", ".json"}:
            reads.append(len(content))
        return content

    monkeypatch.setattr(Path, "read_bytes", measured)
    CapacityTree(tmp_path, "offline", None, "契约", "报告.md", [(source, "原文")])
    cold_bytes = sum(reads)
    assert cold_bytes > 0
    reads.clear()
    CapacityTree(tmp_path, "offline", None, "契约", "报告.md", [(source, "原文")])
    assert sum(reads) == 0
    print(f"CapacityTree code byte reads: cold={cold_bytes}, warm={sum(reads)}")


def test_code_cache_invalidates_edits_replacements_and_missing_files(tmp_path, monkeypatch):
    """An equal-sized edit or atomic replacement must not reuse stale code."""
    from code_fingerprint import code_digest
    path = tmp_path / "module.py"
    path.write_bytes(b"old")
    first = code_digest(path)
    stamp = path.stat()
    path.write_bytes(b"new")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10_000_000))
    assert code_digest(path) == hashlib.sha256(b"new").hexdigest() != first
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(b"alt")
    stamp = path.stat()
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(path)
    assert code_digest(path) == hashlib.sha256(b"alt").hexdigest()
    path.unlink()
    assert code_digest(path) == hashlib.sha256(b"").hexdigest()
    path.write_bytes(b"old")
    assert code_digest(path) == first


def test_code_cache_bounds_memory_and_coalesces_concurrent_reads(tmp_path, monkeypatch):
    """Parallel fingerprint consumers read a single unchanged file only once."""
    import code_fingerprint
    monkeypatch.setattr(code_fingerprint, "_CACHE", OrderedDict())
    monkeypatch.setattr(code_fingerprint, "_MAX_ENTRIES", 2)
    original = Path.read_bytes
    reads = []

    def measured(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", measured)
    paths = [tmp_path / f"module{index}.py" for index in range(3)]
    for path in paths:
        path.write_bytes(b"code")
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert len(set(executor.map(code_fingerprint.code_digest, [paths[0]] * 8))) == 1
    assert reads == [paths[0]]
    for path in paths[1:]:
        code_fingerprint.code_digest(path)
    code_fingerprint.code_digest(paths[0])
    assert reads == [paths[0], paths[1], paths[2], paths[0]]


def test_project_evidence_digest_always_reads_current_bytes(tmp_path, monkeypatch):
    """Publication fencing must detect edits even with restored file metadata."""
    from fault_recovery import digest
    path = tmp_path / "story.json"
    path.write_bytes(b"old")
    stamp = path.stat()
    first = digest(path)
    path.write_bytes(b"new")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert digest(path) == hashlib.sha256(b"new").hexdigest() != first


def test_recovery_strategies_reuse_only_immutable_evidence_index(tmp_path, monkeypatch):
    """New candidates still dispatch; identical copied evidence is indexed once."""
    from bounded_generation import run_paged_recovery
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from worker_context import ContextReader, snapshot_context
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "7000")
    monkeypatch.setenv("ND_CONTEXT_CONTEXT_TOKENS", "10000")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "1000")
    monkeypatch.setenv("ND_CONTEXT_OUTPUT_TOKENS", "1200")
    calls = []
    source_text = "共享完整原文证据" * 1200 + "末尾证据不可丢失"

    def worker(command, root, kind, pack, output, chapter_seq=0, **options):
        prompt = pack.read_text(encoding="utf-8")
        calls.append((kind, prompt))
        if kind == "bounded_fragment":
            save_json(output, {"overview": "原件全文及末尾证据见完整材料", "content": None})
        else:
            # Use the real read-only worker boundary, not unrestricted Path reads:
            # shared evidence and original logical paths must both be available.
            with tempfile.TemporaryDirectory() as directory:
                reader = ContextReader(snapshot_context(root, pack, [output], Path(directory)))
                source = root / "chapter.md"
                assert reader.read(str(source))["text"] == source.read_text(encoding="utf-8")
                shared_materials = [item["source"] for item in reader.files
                                    if "shared" in Path(item["source"]).parts
                                    and Path(item["source"]).parent.name == "materials"]
                assert shared_materials
                assert reader.read(shared_materials[0])["text"] == source.read_text(encoding="utf-8")
            save_json(output, {"strategy": kind})
        return 0

    def attempt(name, evidence=source_text, origin="原文拆解/chapter.md"):
        workspace = tmp_path / name
        workspace.mkdir()
        source = workspace / "chapter.md"
        source.write_text(evidence, encoding="utf-8")
        candidate = workspace / "candidate.json"
        save_json(candidate, {"candidate": name})
        pack = workspace / "task.md"
        pack.write_text(f"本轮策略 {name}，完整候选 {candidate}", encoding="utf-8")
        save_json(pack.with_suffix(".md.sources.json"), [str(source), str(candidate)])
        result = workspace / "result.json"
        assert run_paged_recovery(worker, "offline", workspace, name, pack, result,
                                  evidence_index_root=tmp_path / "shared",
                                  immutable_sources=[(source, origin)]) == 0
        assert json.loads(result.read_text(encoding="utf-8")) == {"strategy": name}
        return workspace

    attempt("targeted")
    first_reads = sum(source_text[:32] in text for _, text in calls)
    assert first_reads > 1
    first_end = len(calls)
    attempt("evidence")
    assert not any(source_text[:32] in text for _, text in calls[first_end:])
    assert calls[-1][0] == "evidence"
    second_end = len(calls)
    print(f"Recovery index/dispatch worker calls: cold={first_end}, warm={second_end - first_end}")
    attempt("diagnosed", source_text + "新证据")
    assert any(source_text[:32] in text for _, text in calls[second_end:])
    assert calls[-1][0] == "diagnosed"
    third_end = len(calls)
    attempt("other_origin", origin="章节处理/chapter.md")
    assert any(source_text[:32] in text for _, text in calls[third_end:])
    # Every shared tree must prove complete contiguous source coverage.
    receipts = list((tmp_path / "shared").rglob("coverage.json"))
    assert receipts
    for receipt in receipts:
        spans = json.loads(receipt.read_text(encoding="utf-8"))
        assert spans[0]["start"] == 0
        assert spans[-1]["end"] in {len(source_text), len(source_text) + 3}
        assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))


def test_recovery_rejects_evidence_changed_after_shared_indexing(tmp_path, monkeypatch):
    """A concurrent edit before attempt-tree creation cannot bless a stale index."""
    import bounded_generation
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "7000")
    monkeypatch.setenv("ND_CONTEXT_CONTEXT_TOKENS", "10000")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "1000")
    monkeypatch.setenv("ND_CONTEXT_OUTPUT_TOKENS", "1200")
    source = tmp_path / "source.md"
    source.write_text("原始完整证据" * 1200, encoding="utf-8")
    pack = tmp_path / "task.md"
    pack.write_text("按完整证据修复候选", encoding="utf-8")
    save_json(pack.with_suffix(".md.sources.json"), [str(source)])
    original_tree = bounded_generation.CapacityTree

    def concurrently_changed(project, command, worker, contract, *args, **options):
        if "索引错误路径" in contract:
            source.write_text("并发修改后的不同证据", encoding="utf-8")
        return original_tree(project, command, worker, contract, *args, **options)

    def worker(command, root, kind, task, output, **options):
        save_json(output, {"overview": "完整证据位置", "content": None})
        return 0

    monkeypatch.setattr(bounded_generation, "CapacityTree", concurrently_changed)
    assert bounded_generation.run_paged_recovery(
        worker, "offline", tmp_path, "recovery_patch", pack, tmp_path / "result.json",
        evidence_index_root=tmp_path / "shared", immutable_sources=[(source, "原文/source.md")]
    ) == REPAIR_EXHAUSTED
