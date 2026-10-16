"""Catalog display may shrink paths without changing frozen context identities."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_worker  # noqa: E402
from context_budget import CONTEXT_EXCEEDED, estimate_tokens, inspect_pack  # noqa: E402
from worker_context import ContextReader  # noqa: E402


CATALOG_HEADER = "## 宿主材料目录（已列出，无需list_context）\n"
BASE_LABEL = "材料路径基准（冻结项目根目录）："


def prepare_catalog(tmp_path, monkeypatch):
    """Supply 16 duplicate basenames plus in-project and multi-root aliases."""
    project = tmp_path / "book"
    project.mkdir()
    pack, output = project / "task.md", project / "result.json"
    pack.write_text("本次完整任务正文。", encoding="utf-8")
    declared, mutable = [], []
    for index in range(16):
        for folder in ("原文", "分析"):
            path = project / folder / f"同名{index:02d}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"{folder}独有的冻结证据{index}", encoding="utf-8")
            declared.append(str(path))
            mutable.append(path)
    # A logical source may resolve to an explicit frozen alias, never its live value.
    for index, logical in enumerate((project / "alias" / "证据.md",
                                     tmp_path / "book-sibling" / "同名00.md",
                                     tmp_path / "another-root" / "同名00.md")):
        logical.parent.mkdir(exist_ok=True)
        logical.write_text(f"live version {index}", encoding="utf-8")
        frozen = tmp_path / f"alias_snapshot_{index}.txt"
        frozen.write_text(f"authoritative frozen alias {index}", encoding="utf-8")
        declared.append({"source": str(logical), "snapshot": str(frozen)})
        mutable.extend((logical, frozen))
    pack.with_suffix(".context-inputs.json").write_text(
        json.dumps(declared, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ND_TASK_PACK", str(pack))
    monkeypatch.setenv("ND_EXPECTED_OUTPUT", str(output))
    monkeypatch.delenv("ND_EXPECTED_OUTPUTS", raising=False)
    return project, pack, output, mutable


def outgoing_context(command, prompt):
    """Inspect only the real outbound prompt and its private frozen manifest."""
    config = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    manifest = Path(config["mcpServers"]["nd_context"]["args"][1])
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    catalog, _ = json.JSONDecoder().raw_decode(prompt.split(CATALOG_HEADER, 1)[1])
    return ContextReader(manifest), metadata, catalog


def test_outgoing_catalog_paths_keep_all_frozen_identities(tmp_path, monkeypatch):
    """Full relative paths retain duplicate names, inline flags and frozen aliases."""
    project, pack, _, mutable = prepare_catalog(tmp_path, monkeypatch)
    observed = {}

    def offline_stream(command, prompt, directory):
        reader, metadata, catalog = outgoing_context(command, prompt)
        assert prompt.count(BASE_LABEL) == 1
        assert BASE_LABEL + str(project.resolve()) in prompt
        assert len(catalog) == len(metadata["files"]) == 36
        assert len({item["path"] for item in catalog}) == 36
        # Source and snapshot bytes are frozen before any live inputs change.
        expected = {item["source"]: Path(item["snapshot"]).read_text(encoding="utf-8")
                    for item in metadata["files"]}
        before = json.dumps(metadata, sort_keys=True)
        for path in mutable:
            path.write_text("new live contents must never be read", encoding="utf-8")
        for supplied, shown in zip(metadata["files"], catalog):
            source = Path(supplied["source"])
            assert shown["provided_in_prompt"] is bool(supplied.get("provided_in_prompt"))
            if source.is_relative_to(project):
                assert shown["path"] == str(source.relative_to(project))
                candidate = project / shown["path"]
            else:
                assert shown["path"] == supplied["source"]
                candidate = Path(shown["path"])
            assert reader.key(candidate) == reader.key(source)
            assert reader.allowed[reader.key(candidate)] == reader.allowed[reader.key(source)]
            absolute = reader.read(str(source), reread=True)
            displayed = reader.read(shown["path"], reread=True)
            assert absolute["text"] == displayed["text"] == expected[supplied["source"]]
            assert absolute["next_offset"] is displayed["next_offset"] is None
        assert json.dumps(json.loads(manifest_path(command).read_text(encoding="utf-8")), sort_keys=True) == before
        assert sum(Path(item["path"]).name.startswith("同名") for item in catalog
                   if not Path(item["path"]).is_absolute()) == 32
        with pytest.raises(ValueError):
            reader.read("同名00.md")
        for traversal in ("../book/原文/同名00.md", "原文/../原文/同名00.md"):
            with pytest.raises(ValueError, match="确切路径"):
                reader.read(traversal)
        observed["called"] = True
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(agent_worker, "run_structured_stream", offline_stream)
    result = agent_worker.run_provider("codebuddy", ["offline", "--json-schema", "{}"],
                                       pack.read_text(encoding="utf-8"), project, "recovery_patch")
    assert result.returncode == 0 and observed["called"]


def manifest_path(command):
    """Recover the manifest used by the existing stdio tool configuration."""
    config = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    return Path(config["mcpServers"]["nd_context"]["args"][1])


def test_real_admission_counts_catalog_base_and_every_outgoing_entry(tmp_path, monkeypatch):
    """Admission must count the exact displayed catalog and its one base note."""
    project, pack, _, _ = prepare_catalog(tmp_path, monkeypatch)
    captured = {}

    def offline_stream(command, prompt, directory):
        _, metadata, catalog = outgoing_context(command, prompt)
        assert BASE_LABEL in prompt
        assert len(catalog) == len(metadata["files"])
        extra = prompt.replace(pack.read_text(encoding="utf-8"), "") + "{}"
        receipt = json.loads(pack.with_suffix(".md.capacity.json").read_text(encoding="utf-8"))
        assert receipt == inspect_pack(pack, extra)
        assert receipt["estimated_input_tokens"] == estimate_tokens(
            pack.read_text(encoding="utf-8") + extra)
        captured["receipt"] = receipt
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(agent_worker, "run_structured_stream", offline_stream)
    result = agent_worker.run_provider("codebuddy", ["offline", "--json-schema", "{}"],
                                       pack.read_text(encoding="utf-8"), project, "recovery_patch")
    assert result.returncode == 0 and captured["receipt"]["passed"]


def test_catalog_exceeding_real_budget_never_dispatches(tmp_path, monkeypatch):
    """Display compression must retain the existing real admission failure gate."""
    project, pack, _, _ = prepare_catalog(tmp_path, monkeypatch)
    monkeypatch.setenv("ND_CONTEXT_INPUT_TOKENS", "1")
    monkeypatch.setenv("ND_CONTEXT_CONTEXT_TOKENS", "0")
    monkeypatch.setenv("ND_CONTEXT_RESERVE_TOKENS", "0")
    admitted = []
    original_admit = agent_worker.admit_pack

    def observed_admit(path, extra):
        assert BASE_LABEL in extra and CATALOG_HEADER in extra
        admitted.append(extra)
        return original_admit(path, extra)

    monkeypatch.setattr(agent_worker, "admit_pack", observed_admit)
    monkeypatch.setattr(agent_worker, "run_structured_stream",
                        lambda *args: pytest.fail("over-budget input must not reach a model"))
    result = agent_worker.run_provider("codebuddy", ["offline", "--json-schema", "{}"],
                                       pack.read_text(encoding="utf-8"), project, "recovery_patch")
    assert result.returncode == CONTEXT_EXCEEDED and len(admitted) == 1
    assert not json.loads(pack.with_suffix(".md.capacity.json").read_text(encoding="utf-8"))["passed"]


def test_display_base_comes_from_exact_manifest_project(tmp_path, monkeypatch):
    """A caller directory is not a substitute for the reader's manifest base."""
    project, pack, _, _ = prepare_catalog(tmp_path, monkeypatch)
    original_snapshot = agent_worker.snapshot_context
    manifest_project = tmp_path

    def snapshot_with_registered_parent(*args, **kwargs):
        manifest = original_snapshot(*args, **kwargs)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["project"] = str(manifest_project)
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return manifest

    def offline_stream(command, prompt, directory):
        reader, metadata, catalog = outgoing_context(command, prompt)
        assert BASE_LABEL + str(manifest_project) in prompt
        for supplied, shown in zip(metadata["files"], catalog):
            assert shown["path"] == str(Path(supplied["source"]).relative_to(manifest_project))
            assert reader.key(manifest_project / shown["path"]) == reader.key(Path(supplied["source"]))
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(agent_worker, "snapshot_context", snapshot_with_registered_parent)
    monkeypatch.setattr(agent_worker, "run_structured_stream", offline_stream)
    assert agent_worker.run_provider("codebuddy", ["offline", "--json-schema", "{}"],
                                     pack.read_text(encoding="utf-8"), project, "recovery_patch").returncode == 0