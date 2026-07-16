# Governance Tag Compression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure governance correction Delta files get deterministic tag compression before governance validation, matching chapter Delta behavior.

**Architecture:** Reuse the existing `compress_tags.py --delta` CLI from `run_pipeline.commit_governance()` before `validate_delta.py --mode governance`. Add one regression test in the existing audit/governance test module so the behavior is covered through the public pipeline function rather than by testing implementation internals.

**Tech Stack:** Python 3.12, pytest, existing scripts in `scripts/`.

## Global Constraints

- Keep the fix surgical: do not change taxonomy, do not loosen `validate_delta.py` or `story_schema_rules.py` controlled-tag validation.
- Preserve existing chapter path behavior; only close the governance-path gap.
- Follow TDD: add the failing regression test before changing production code.
- Match surrounding style in `scripts/run_pipeline.py` and existing tests.

---

### Task 1: Add Governance Compression Regression Test

**Files:**
- Modify: `scripts/test_audit_pack.py`

**Interfaces:**
- Consumes: existing `commit_governance(project_dir: Path, patch_path: str) -> int` from `scripts/run_pipeline.py`.
- Produces: a failing regression test named `test_governance_patch_compresses_tags_before_validation` proving a governance patch with `人物类型:掌柜` succeeds after compression.

- [ ] **Step 1: Read existing test helpers**

Open `scripts/test_audit_pack.py` and identify the existing helper functions for writing JSON and creating minimal story structures. Reuse them if present; otherwise keep new helper code local to the new test.

- [ ] **Step 2: Add the failing test**

Append this test near the other `commit_governance` tests in `scripts/test_audit_pack.py`:

```python
def test_governance_patch_compresses_tags_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        story = {
            "介绍": {"标题": "测试", "描述": "测试"},
            "角色集": [],
            "事件集": [],
            "地点集": [],
            "线索集": [],
            "阵营集": [],
            "物品集": [],
            "其他事项集": [],
        }
        story_path = project / "故事结构_增量.json"
        story_path.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")

        patch = project / "质量治理" / "周期审计" / "correction_001-005.json"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(
            json.dumps(
                {
                    "章节范围": "001-005",
                    "新增元素": {
                        "角色集": [
                            {
                                "名称": "客栈掌柜",
                                "是否主角": False,
                                "性别": 2,
                                "年龄": 0,
                                "生日": "0001-01-01T00:00:00",
                                "所属阵营": [],
                                "关系": [],
                                "分组": "路人",
                                "别名": [],
                                "标签集": ["人物类型:掌柜", "客栈"],
                                "介绍": "客栈掌柜。",
                                "详情": {
                                    "首次章节": "0001",
                                    "最近章节": "0001",
                                    "提取理由": "测试治理补丁标签压缩。",
                                },
                            }
                        ],
                        "事件集": [],
                        "地点集": [],
                        "线索集": [],
                        "阵营集": [],
                        "物品集": [],
                        "其他事项集": [],
                    },
                    "修改元素": {
                        "角色集": [],
                        "事件集": [],
                        "地点集": [],
                        "线索集": [],
                        "阵营集": [],
                        "物品集": [],
                        "其他事项集": [],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        rc = commit_governance(project, str(patch))

        assert rc == 0
        patched = json.loads(patch.read_text(encoding="utf-8"))
        character = patched["新增元素"]["角色集"][0]
        assert character["标签集"] == ["客栈"]
        assert json.loads(character["详情"]["补充标签"]) == ["人物类型:掌柜"]
        assert list((project / "质量治理" / "周期审计").glob("compress_governance_*.json"))
```

If `tempfile`, `Path`, `json`, or `commit_governance` are already imported at the top of the file, do not duplicate imports. If any are missing, add them using the existing import style.

- [ ] **Step 3: Run test to verify it fails for the intended reason**

Run:

```bash
pytest scripts/test_audit_pack.py::test_governance_patch_compresses_tags_before_validation -q
```

Expected before production change: FAIL because `commit_governance()` returns `1`, and the validation report contains `未知受控标签[人物类型:掌柜]`.

---

### Task 2: Wire Tag Compression Into Governance Commit

**Files:**
- Modify: `scripts/run_pipeline.py:1014-1040`
- Test: `scripts/test_audit_pack.py`

**Interfaces:**
- Consumes: existing `script_path("compress_tags.py")`, `run_cmd(...)`, `governance_dir`, and `patch` variables in `commit_governance()`.
- Produces: governance patches are rewritten by `compress_tags.py --delta` before `validate_delta.py --mode governance` runs; compression reports are saved as `compress_governance_<timestamp>.json`.

- [ ] **Step 1: Insert compression before governance validation**

In `scripts/run_pipeline.py`, inside `commit_governance()`, after these existing lines:

```python
report_delta = governance_dir / f"validate_delta_governance_{ts}.txt"
report_schema = governance_dir / f"validate_schema_governance_{ts}.txt"
shutil.copy2(story, before)
```

insert:

```python
    report_compress = governance_dir / f"compress_governance_{ts}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(patch), "--report", str(report_compress)])
    if rc != 0:
        print(f"治理补丁标签压缩失败: {report_compress}")
        return 1
```

Keep the existing `validate_delta.py --mode governance` call immediately after this new block.

- [ ] **Step 2: Run the new regression test**

Run:

```bash
pytest scripts/test_audit_pack.py::test_governance_patch_compresses_tags_before_validation -q
```

Expected after production change: PASS.

- [ ] **Step 3: Run related tests**

Run:

```bash
pytest scripts/test_audit_pack.py scripts/test_compress_tags.py scripts/test_validate_delta.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Inspect diff for surgical scope**

Run:

```bash
git diff -- scripts/run_pipeline.py scripts/test_audit_pack.py
```

Expected: only the new regression test and the small `commit_governance()` compression block changed.

---

## Self-Review

- Spec coverage: The plan covers the requested fix by closing the governance compression gap without changing taxonomy or validator strictness.
- Placeholder scan: No placeholders, TBDs, or vague steps remain.
- Type consistency: `commit_governance(project_dir: Path, patch_path: str) -> int` is used consistently; `compress_governance_<timestamp>.json` report naming matches existing governance report style.
