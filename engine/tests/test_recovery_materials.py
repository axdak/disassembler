"""Prove material deduplication is lossless, scoped and does not change acceptance."""
import json

import pytest

from merge_delta import save_json
from recovery_materials import RecoveryMaterials, read_material_document, rebase_material_paths, write_change_index


def test_repeated_full_material_has_one_source_and_round_trips(tmp_path):
    source = tmp_path / "故事结构.json"
    original = '{\n  "介绍": "完整长篇分析、标点、空白都必须保留。"\n}\n'
    source.write_text(original, encoding="utf-8")
    bundle = RecoveryMaterials(tmp_path)
    bundle.add_source(source, "正式结构")
    bundle.add_source(source, "相同结构引用")
    full = "原输出要求不变。\n```json\n" + original + "```\n再核对一次：\n" + original
    bundle.write_document(tmp_path / "contract.md", full, "完整任务")
    bundle.write_document(tmp_path / "failure.md", full, "完整反馈")
    catalog = bundle.save()
    assert read_material_document(tmp_path / "contract.md") == full
    assert read_material_document(tmp_path / "failure.md") == full
    assert original.strip() not in (tmp_path / "contract.md").read_text(encoding="utf-8")
    assert "contract.md" in (tmp_path / "failure.md").read_text(encoding="utf-8")
    data = json.loads(catalog.read_text(encoding="utf-8"))
    assert len([m for m in data["materials"] if m["path"] == source.name]) == 1
    assert data["materials"][0]["roles"] == ["正式结构", "相同结构引用"]
    assert source.read_text(encoding="utf-8") == original


def test_different_versions_and_partial_mentions_are_not_collapsed(tmp_path):
    source = tmp_path / "原文.md"
    source.write_text("第一段\n第二段\n", encoding="utf-8")
    bundle = RecoveryMaterials(tmp_path)
    bundle.add_source(source, "原文")
    full = "开头第一段\n第二段结尾\n\n第一段\n不同内容\n"
    bundle.write_document(tmp_path / "contract.md", full, "任务")
    bundle.save()
    assert (tmp_path / "contract.md").read_text(encoding="utf-8") == full
    assert read_material_document(tmp_path / "contract.md") == full


def test_empty_documents_and_source_are_safe(tmp_path):
    source = tmp_path / "empty.md"
    source.write_text("", encoding="utf-8")
    bundle = RecoveryMaterials(tmp_path)
    bundle.add_source(source, "空源")
    bundle.write_document(tmp_path / "contract.md", "", "空任务")
    bundle.save()
    assert read_material_document(tmp_path / "contract.md") == ""


def test_nested_workspace_rebasing_is_idempotent_for_all_path_forms(tmp_path):
    destination = tmp_path / "质量治理/自动恢复/attempt"
    for value in (str(tmp_path), tmp_path.as_posix(), json.dumps(str(tmp_path), ensure_ascii=False)[1:-1]):
        text = value + "/材料.json"
        rebased = rebase_material_paths(text, tmp_path, destination)
        assert rebase_material_paths(rebased, tmp_path, destination) == rebased
        assert rebased.count("attempt") == 1


def test_history_preserves_old_feedback_before_rebasing(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    bundle = RecoveryMaterials(old)
    original = "旧任务要求：缺少理由。\n" * 20
    bundle.write_document(old / "contract.md", original, "旧任务")
    bundle.write_document(old / "failure_after.md", original, "旧反馈")
    bundle.save()
    current = RecoveryMaterials(new)
    current.write_document(new / "contract.md", "本轮任务已变化，不得冒充旧反馈。", "新任务")
    current.write_document(new / "previous_failure.md",
                           rebase_material_paths(read_material_document(old / "failure_after.md"), old, new), "历史")
    current.save()
    assert read_material_document(new / "previous_failure.md") == original


def test_change_index_locates_edits_without_repeating_analysis(tmp_path):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    narrative = "这是完整的分析正文，不能重复放进变更清单。" * 30
    save_json(before, {"详情": {"分析": narrative}})
    save_json(after, {"详情": {"分析": narrative, "提取理由": "原文中明确出现。"}})
    index = tmp_path / "changes.json"
    write_change_index(before, after, index)
    value = json.loads(index.read_text(encoding="utf-8"))
    assert value["changes"] == [{"path": "/详情/提取理由", "change": "added"}]
    assert narrative not in index.read_text(encoding="utf-8")


def test_material_catalog_cannot_escape_workspace(tmp_path):
    document = tmp_path / "contract.md"
    outside = tmp_path.parent / "outside.md"
    document.write_text(f"[完整材料 M001，须读全文且本次无需重复读取]({tmp_path / '../outside.md'})", encoding="utf-8")
    save_json(tmp_path / "materials.json", {"materials": [{"id": "M001", "path": "../outside.md"}],
                                           "documents": [{"path": "contract.md"}]})
    with pytest.raises(ValueError):
        read_material_document(document)
