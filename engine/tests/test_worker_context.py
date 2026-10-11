"""The worker context tool must have no access outside immutable task inputs."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worker_context import ContextReader, READ_LIMIT, snapshot_context  # noqa: E402


def context(tmp_path):
    """Include a permitted nested material and tempting unapproved/code paths."""
    project = tmp_path / "book"
    project.mkdir()
    material = project / "chapter.md"
    material.write_text("原文证据" * 10000, encoding="utf-8")
    unrelated = project / "unrelated.md"
    unrelated.write_text("任务外材料", encoding="utf-8")
    code = project / "source.py"
    code.write_text("raise RuntimeError('must never execute')", encoding="utf-8")
    outside = tmp_path / "private.md"
    outside.write_text("不应读取", encoding="utf-8")
    pack = project / "task.md"
    pack.write_text(f"读取 `{material}`。不要读取 `{code}` 或 `{outside}`。", encoding="utf-8")
    frozen = tmp_path / "isolated"
    frozen.mkdir()
    manifest = snapshot_context(project, pack, [], frozen)
    return manifest, pack, material, unrelated, code, outside


def test_snapshot_only_supplied_text_and_immutable_pagination(tmp_path):
    manifest, pack, material, unrelated, code, outside = context(tmp_path)
    reader = ContextReader(manifest)
    assert {item["source"] for item in reader.files} == {str(pack), str(material)}
    original = material.read_text(encoding="utf-8")
    material.write_text("真实输入随后改变", encoding="utf-8")
    first = reader.read("chapter.md")
    assert first["text"] == original[:READ_LIMIT]
    remainder = reader.read(str(material), first["next_offset"])
    assert first["text"] + remainder["text"] == original
    assert remainder["next_offset"] is None
    for path in (unrelated, code, outside, Path("../book/chapter.md")):
        with pytest.raises(ValueError):
            reader.read(str(path))
    assert code.read_text(encoding="utf-8").startswith("raise")


def test_mcp_protocol_denies_unknown_tools_and_invalid_paths(tmp_path):
    manifest, pack, material, unrelated, *_ = context(tmp_path)
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {"name": "read_context", "arguments": {"path": str(material), "limit": 5}}},
        {"id": 4, "method": "tools/call", "params": {"name": "read_context", "arguments": {"path": str(unrelated)}}},
        {"id": 5, "method": "tools/call", "params": {"name": "Bash", "arguments": {"command": "anything"}}},
    ]
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "tools/worker_context.py"), str(manifest)],
                            input="\n".join(json.dumps(request) for request in requests) + "\n",
                            text=True, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    responses = {row["id"]: row["result"] for row in map(json.loads, result.stdout.splitlines())}
    assert {tool["name"] for tool in responses[2]["tools"]} == {"list_context", "read_context"}
    assert json.loads(responses[3]["content"][0]["text"])["text"] == "原文证据原"
    assert responses[4]["isError"] and responses[5]["isError"]


def test_material_link_cannot_escape_project(tmp_path):
    """Resolve junction/symlink targets before including any inferred material."""
    manifest, pack, material, unrelated, code, outside = context(tmp_path)
    link = material.parent / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Platform does not permit symlink creation")
    pack.write_text(f"读取 `{link}`", encoding="utf-8")
    updated = snapshot_context(material.parent, pack, [], manifest.parent)
    with pytest.raises(ValueError):
        ContextReader(updated).read(str(link))


def test_capacity_request_reads_declared_siblings_and_frozen_source_aliases(tmp_path):
    """Nested request directories must retain access to the complete evidence tree."""
    from bounded_final import guarded_worker
    from merge_delta import save_json

    live = tmp_path / "live.md"
    live.write_text("后续已经改变的原文", encoding="utf-8")
    frozen = tmp_path / "tree/materials/00000.md"
    frozen.parent.mkdir(parents=True)
    frozen.write_text("执行任务时的完整冻结原文" * 4000, encoding="utf-8")
    request = tmp_path / "tree/request"
    request.mkdir()
    pack, output = request / "task.md", request / "result.json"
    pack.write_text(f"核对原件：{live}", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    def worker(command, project, kind, task, target, **options):
        reader = ContextReader(snapshot_context(project, task, [target], sandbox))
        chunks, offset = [], 0
        while offset is not None:
            page = reader.read(str(live), offset)
            chunks.append(page["text"])
            offset = page["next_offset"]
        assert "".join(chunks) == frozen.read_text(encoding="utf-8")
        save_json(target, {"ok": True})
        return 0

    assert guarded_worker(worker, "fake", request, "bounded_fragment", pack, output,
                          [pack, frozen], {}, context_aliases=[(live, frozen)]) == (0, False)
    assert live.read_text(encoding="utf-8") == "后续已经改变的原文"


def test_recovery_catalog_is_followed_but_story_json_paths_are_not(tmp_path):
    project = tmp_path / "book"
    project.mkdir()
    evidence, story, unrelated = [project / name for name in ("evidence.md", "story.json", "unrelated.md")]
    evidence.write_text("完整修复材料", encoding="utf-8")
    unrelated.write_text("无关文件", encoding="utf-8")
    story.write_text(json.dumps({"详情": str(unrelated)}), encoding="utf-8")
    catalog = project / "materials.json"
    catalog.write_text(json.dumps({"materials": [{"path": "evidence.md"}, {"path": "story.json"}]}), encoding="utf-8")
    pack = project / "task.md"
    pack.write_text(f"统一材料目录：{catalog}", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    reader = ContextReader(snapshot_context(project, pack, [], sandbox))
    assert reader.read(str(evidence))["text"] == "完整修复材料"
    with pytest.raises(ValueError):
        reader.read(str(unrelated))
