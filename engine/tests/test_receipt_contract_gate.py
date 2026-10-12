"""提交回执的版本门禁：跨版本回执不得继续免检放行。

方向性约束（关键）：门禁只收紧，不放开——契约变化只会让旧回执多验一遍，
绝不会重跑模型、重新生成已完成章节，或把已完成章节判为未完成。
"""
import json
from pathlib import Path

import pytest
import engine_contract
import run_pipeline as pipeline
from test_audit_pack import empty_delta, empty_story, valid_analysis, write_chapter_source, write_json


def chapter_project(root: Path):
    write_json(root / "故事结构_增量.json", empty_story())
    chapter = write_chapter_source(root, 1)
    pipeline.init_dirs(root)
    paths = pipeline.artifact_paths(root, chapter)
    paths["analysis"].write_text(valid_analysis(1), encoding="utf-8")
    write_json(paths["delta"], empty_delta(1))
    story = pipeline.ensure_story(root)
    return chapter, paths, story


def read_receipt(paths) -> dict:
    return json.loads(paths["after"].with_suffix(".commit.json").read_text(encoding="utf-8"))


def write_receipt(paths, receipt: dict) -> None:
    paths["after"].with_suffix(".commit.json").write_text(
        json.dumps(receipt, ensure_ascii=False), encoding="utf-8")


# ---- 契约指纹模块本身 ----

def test_contract_version_is_stable_and_sensitive(tmp_path):
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    (engine / "schemas").mkdir(parents=True)
    (engine / "schemas" / "delta.schema.json").write_text("{}", encoding="utf-8")
    validator = engine / "scripts" / "validate_delta.py"
    validator.write_text("x = 1\n", encoding="utf-8")

    before = engine_contract.contract_version(engine)
    assert before == engine_contract.contract_version(engine), "同一份代码必须稳定"
    assert len(before) == 16

    validator.write_text("x = 2\n", encoding="utf-8")
    assert engine_contract.contract_version(engine) != before, "校验器一变版本必须变"


def test_missing_contract_file_still_changes_version(tmp_path):
    """删除校验器同样要改变版本，不能被静默忽略。"""
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    (engine / "schemas").mkdir(parents=True)
    validator = engine / "scripts" / "validate_delta.py"
    validator.write_text("x = 1\n", encoding="utf-8")
    before = engine_contract.contract_version(engine)
    validator.unlink()
    assert engine_contract.contract_version(engine) != before


def test_prompts_are_deliberately_out_of_contract():
    """改提示词不应让历史回执全部失效（否则升级会触发大规模重算）。"""
    names = {path.relative_to(path.parents[1]).as_posix() for path in engine_contract.contract_files()}
    assert not any(name.startswith("prompts") for name in names)
    assert "scripts/validate_delta.py" in names


# ---- 门禁判定 ----

def test_published_receipt_records_contract_version(tmp_path):
    _, paths, _ = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    receipt = read_receipt(paths)
    assert receipt["published"] is True
    assert receipt["contract"] == engine_contract.contract_version()


def test_same_contract_receipt_is_allowed(tmp_path):
    _, paths, _ = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    assert pipeline.receipt_shortcut_allowed(paths, read_receipt(paths))


def test_stale_or_legacy_receipt_is_rejected(tmp_path):
    _, paths, _ = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0

    stale = read_receipt(paths)
    stale["contract"] = "0000000000000000"
    assert not pipeline.receipt_shortcut_allowed(paths, stale)

    legacy = read_receipt(paths)
    legacy.pop("contract", None)
    assert not pipeline.receipt_shortcut_allowed(paths, legacy), "历史回执一律按跨版本处理"


def test_contract_change_invalidates_receipt(tmp_path, monkeypatch):
    """模拟上游收紧校验：同一份回执在新契约下不再免检。"""
    _, paths, _ = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    receipt = read_receipt(paths)
    monkeypatch.setattr(pipeline, "contract_version", lambda: "changed-contract")
    assert not pipeline.receipt_shortcut_allowed(paths, receipt)


def test_artifact_change_still_invalidates_receipt(tmp_path):
    """产物变化本来就该失效（原有语义必须保留）。"""
    _, paths, _ = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    paths["delta_report"].write_text("{}", encoding="utf-8")
    assert not pipeline.receipt_shortcut_allowed(paths, read_receipt(paths))


# ---- 方向性：跨版本不重算 ----

def test_stale_receipt_never_regenerates_completed_chapter(tmp_path, monkeypatch):
    chapter, paths, story = chapter_project(tmp_path)
    assert pipeline.commit_chapter(tmp_path, 1) == 0

    stale = read_receipt(paths)
    stale["contract"] = "0000000000000000"
    write_receipt(paths, stale)
    story_bytes = story.read_bytes()

    # 章节仍是"已完成"：门禁不得把它降级为未完成。
    assert pipeline.is_chapter_complete(tmp_path, chapter)
    # 回落到复验分支：不得重跑任何外部命令、不得改动正式故事结构。
    monkeypatch.setattr(pipeline, "ensure_story", lambda root: root / "故事结构_增量.json")
    monkeypatch.setattr(pipeline, "run_cmd", lambda *args, **kwargs: pytest.fail("跨版本回执不得触发重算"))
    assert pipeline.commit_chapter(tmp_path, 1) == 0
    assert story.read_bytes() == story_bytes
