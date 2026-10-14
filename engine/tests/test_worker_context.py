"""The worker context tool must have no access outside immutable task inputs."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worker_context import ContextReader, READ_LIMIT, snapshot_context  # noqa: E402


def story_reader(tmp_path, items):
    """Freeze a small authoritative story for identity and paging regressions."""
    story = tmp_path / "故事结构_增量.json"
    story.write_text(json.dumps({"角色集": items}, ensure_ascii=False), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"project": str(tmp_path), "files": [
        {"source": str(story), "snapshot": str(story)}]}), encoding="utf-8")
    return ContextReader(manifest)


def test_story_lookup_prefers_canonical_name_over_earlier_alias(tmp_path):
    reader = story_reader(tmp_path, [{"名称": "甲", "别名": ["乙"]}, {"名称": "乙"}])
    result = reader.story_get_units([{"collection": "角色集", "name": "乙"}], "L2")
    assert result["items"][0]["record"]["名称"] == "乙"


@pytest.mark.parametrize("items,query", [
    ([{"名称": "甲", "别名": ["共同"]}, {"名称": "乙", "别名": ["共同"]}], "共同"),
    ([{"名称": "甲"}, {"名称": "甲"}], "甲"),
])
def test_story_lookup_rejects_ambiguous_identity(tmp_path, items, query):
    reader = story_reader(tmp_path, items)
    with pytest.raises(ValueError, match="匹配多个"):
        reader.story_get_units([{"collection": "角色集", "name": query}])


def test_large_story_fields_are_lossless_and_explicitly_paged(tmp_path):
    detail = "很久以前的重要承诺" * 6000
    reader = story_reader(tmp_path, [{"名称": "甲", "详情": {"0001-承诺": detail, "0002-状态": "离开"}}])
    pages, offset = [], 0
    while offset is not None:
        page = reader.story_read_fields("角色集", "甲", ["/详情/0001-承诺"], offset, 1000)
        assert len(page["text"]) <= 1000
        assert page["revision"] == reader.story_snapshot()["revision"]
        pages.append(page["text"])
        offset = page["next_offset"]
    assert json.loads("".join(pages)) == {"/详情/0001-承诺": detail}
    index = reader.story_read_fields("角色集", "甲")
    assert "/详情/0001-承诺" in json.loads(index["text"])["fields"]
    with pytest.raises(ValueError, match="story_read_fields"):
        reader.story_get_units([{"collection": "角色集", "name": "甲"}], "L2")


def test_story_field_query_reports_missing_and_escaped_keys(tmp_path):
    reader = story_reader(tmp_path, [{"名称": "甲", "详情": {"a/b~c": "证据"}}])
    page = reader.story_read_fields("角色集", "甲", ["/详情/a~1b~0c"])
    assert json.loads(page["text"]) == {"/详情/a~1b~0c": "证据"}
    with pytest.raises(ValueError, match="字段不存在"):
        reader.story_read_fields("角色集", "甲", ["/详情/不存在"])
    with pytest.raises(ValueError):
        reader.story_read_fields("角色集", "甲", [], -1)


def test_story_search_finds_detail_without_returning_giant_record(tmp_path):
    reader = story_reader(tmp_path, [{"名称": "甲", "详情": {"0001-承诺": "关键旧约定" * 10000}}])
    result = reader.story_search("关键旧约定")
    assert result["items"][0]["名称"] == "甲"
    assert len(json.dumps(result, ensure_ascii=False)) < 1000


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


def test_story_domain_queries_use_only_frozen_authoritative_material(tmp_path):
    project = tmp_path / "book"
    chapter = project / "原文拆解" / "第001章_相遇.md"
    story = project / "故事结构_增量.json"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("# 第一章\n萧炎在坊市遇见薰儿。", encoding="utf-8")
    story.write_text(json.dumps({
        "介绍": {"标题": "测试故事", "描述": "当前简介"},
        "角色集": [{"名称": "萧炎", "别名": ["岩枭"], "介绍": "主角"}],
        "事件集": [], "地点集": [{"名称": "坊市", "介绍": "交易地点"}],
        "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }, ensure_ascii=False), encoding="utf-8")
    pack = project / "audit.md"
    pack.write_text(f"故事：`{story}`\n原文：`{chapter}`", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    reader = ContextReader(snapshot_context(project, pack, [], sandbox))

    original_revision = reader.story_snapshot()["revision"]
    story.write_text("{}", encoding="utf-8")
    assert reader.story_snapshot()["revision"] == original_revision
    assert reader.story_search("岩枭")["items"][0]["名称"] == "萧炎"
    full = reader.story_get_units([{"collection": "地点集", "name": "坊市"}], "L2")
    assert full["items"][0]["record"]["介绍"] == "交易地点"
    assert reader.story_query_chapters("search", query="薰儿")["matches"][0]["chapter"] == chapter.name
    assert "萧炎" in reader.story_query_chapters("read", chapter=chapter.name, limit=100)["text"]
