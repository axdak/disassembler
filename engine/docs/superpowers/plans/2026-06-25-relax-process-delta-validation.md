# Relax Process Delta Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-chapter Delta validation permissive enough for LLM-authored JSON patches while preserving strict validation after merge, governance, and final delivery.

**Architecture:** Keep `validate_delta.py` as the Delta gate, but split process-mode validation from governance/final validation. Process mode checks only whether a Delta is a safe, mergeable patch; governance/final keep the existing stricter schema/reference checks. The existing pipeline continues to run `repair_llm_json.py`, `coerce_delta.py`, `merge_delta.py`, `normalize_story_schema.py`, and `validate_structure.py` so mechanical fixes and final structure quality remain outside the LLM's burden.

**Tech Stack:** Python 3.12, pytest, existing scripts under `scripts/`, Jinja2 prompt templates under `prompts/`.

## Global Constraints

- Match surrounding code style; avoid broad refactors.
- Do not remove governance/final quality gates.
- Process-mode Delta validation should only hard-fail structural problems that prevent safe merge.
- Per-chapter `修改元素` is a patch: it must not require unchanged fields such as `详情.首次章节`.
- Keep deterministic mechanical cleanup in scripts, not in LLM instructions.
- Do not add new dependencies.
- Do not commit unless explicitly asked during execution.

---

## File Structure

- Modify `scripts/validate_delta.py`
  - Add a permissive process-mode element validator.
  - Keep existing strict element validation for `--mode governance` and `--mode final`.
  - Preserve top-level validation and governance-only checks.
- Modify `scripts/test_validate_delta.py`
  - Replace tests that expect process-mode Delta trace/standard-field omissions to fail.
  - Add tests for permissive process-mode behavior.
  - Add tests proving governance/final still catch strict issues.
- Modify `prompts/delta_extract.j2`
  - Clarify that Delta is a patch, not a complete final structure.
  - Tell the LLM not to write Python to generate Delta.
  - Move strictness language from per-Delta schema to merge/normalize/governance gates.
- Optional modify `scripts/coerce_delta.py`
  - Only if tests expose a deterministic high-frequency mechanical issue already covered by the design, such as converting `详情.补充标签` array to its JSON string representation. Do not add semantic fixes.

---

### Task 1: Add process-mode permissive Delta validation tests

**Files:**
- Modify: `scripts/test_validate_delta.py`

**Interfaces:**
- Consumes: existing `validate_elements(current: dict, delta: dict, mode: str) -> tuple[list[str], list[str]]` from `scripts/validate_delta.py`.
- Produces: failing tests that define the new process-mode contract.

- [ ] **Step 1: Replace trace omission failure expectations with process-mode pass expectations**

In `scripts/test_validate_delta.py`, replace the existing functions `test_delta_missing_trace_fields_fails_before_merge_for_new_element` and `test_delta_missing_trace_fields_fails_before_merge_for_modified_element` with these two tests:

```python
def test_process_delta_allows_new_element_missing_trace_fields():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_trace_for_new_element())
        result = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert result.returncode == 0, result.stdout
        assert "首次章节" in result.stdout or "警告" in result.stdout, result.stdout


def test_process_delta_allows_modified_element_as_patch_without_trace_fields():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_trace_for_modified_element())
        result = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert result.returncode == 0, result.stdout
```

- [ ] **Step 2: Replace modified event strict process tests with warning-oriented process tests**

Replace `test_delta_modified_event_requires_time_matching_involved_chapters` and `test_delta_modified_event_requires_involved_chapters` with:

```python
def test_process_delta_modified_event_does_not_require_time_matching_involved_chapters():
    current = base_story()
    current["事件集"] = [{
        "名称": "入门试炼", "发生地点": "青云山", "参与成员": ["张三"], "重量级": 20,
        "目标事件": [], "分组": "0001-卷一", "时间": "0001-01-01T00:00:00",
        "别名": [], "标签集": [], "介绍": "试炼", "详情": {"涉及章节": "0001"},
    }]
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    delta = {
        "章节": "第001章 测试",
        "新增元素": empty,
        "修改元素": {
            **empty,
            "事件集": [{"名称": "入门试炼", "详情": {"提取理由": "补充描述", "涉及章节": "0001"}}],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert any("时间" in warning or "涉及章节" in warning for warning in warnings), warnings


def test_process_delta_modified_event_does_not_require_involved_chapters():
    current = base_story()
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    delta = {
        "章节": "第001章 测试",
        "新增元素": empty,
        "修改元素": {
            **empty,
            "事件集": [{
                "名称": "既有事件",
                "时间": "0001-01-01T00:00:00",
                "详情": {"提取理由": "补充描述"},
            }],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert any("涉及章节" in warning for warning in warnings), warnings
```

- [ ] **Step 3: Add process-mode minimal patch test**

Append this test after the process-mode tests:

```python
def test_process_delta_accepts_minimal_named_patch():
    current = base_story()
    delta = {
        "章节": "第002章 测试",
        "新增元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]},
        "修改元素": {
            "角色集": [{"名称": "张三", "详情": {"提取理由": "本章继续出现", "最近章节": "0002"}}],
            "事件集": [],
            "地点集": [],
            "线索集": [],
            "阵营集": [],
            "物品集": [],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert isinstance(warnings, list)
```

- [ ] **Step 4: Add process-mode structural hard-fail tests**

Append these tests to prove process mode still blocks non-mergeable patches:

```python
def test_process_delta_still_rejects_element_without_name():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0].pop("名称")
    errors, _ = validate_elements(current, delta, "process")
    assert any("名称 必须是非空字符串" in error for error in errors), errors


def test_process_delta_still_rejects_non_object_detail():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0]["详情"] = "不是对象"
    errors, _ = validate_elements(current, delta, "process")
    assert any("详情 必须是对象" in error for error in errors), errors


def test_process_delta_still_rejects_invalid_detail_value_type():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0]["详情"] = {"提取理由": "首次登场", "数值": 123}
    errors, _ = validate_elements(current, delta, "process")
    assert any("值类型必须是 string 或 string[]" in error for error in errors), errors
```

- [ ] **Step 5: Add governance strictness regression tests**

Append this test to prove strict modes remain strict:

```python
def test_governance_delta_still_requires_trace_fields_for_traceable_elements():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction.json"
        patch = bad_delta_missing_trace_for_new_element()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        write_json(story, base_story())
        write_json(delta, patch)
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "首次章节" in result.stdout, result.stdout
```

- [ ] **Step 6: Run the targeted tests and confirm they fail before implementation**

Run:

```bash
python -m pytest scripts/test_validate_delta.py -q
```

Expected before implementation: failures in the new process-mode permissive tests because `validate_delta.py` still uses strict validation for process mode.

---

### Task 2: Implement process-mode permissive validator in `validate_delta.py`

**Files:**
- Modify: `scripts/validate_delta.py`

**Interfaces:**
- Consumes: `VALID_FIELDS`, `LIST_FIELDS`, `STRING_FIELDS`, `STRUCTURAL_REFS`, `validate_detail_schema(...)`, and existing helper functions.
- Produces: `validate_process_patch_item(...) -> None`, used only when `mode == "process"`.

- [ ] **Step 1: Add process-mode scalar/list warning helper**

In `scripts/validate_delta.py`, after `validate_evidence_detail`, add:

```python
def process_issue(warnings: List[str], label: str, message: str) -> None:
    warnings.append(f"{label} {message}；过程阶段允许先合并，后续由 coerce/normalize/治理收敛")
```

- [ ] **Step 2: Add permissive process item validator**

Immediately after `process_issue`, add this function:

```python
def validate_process_patch_item(
    collection_key: str,
    item: Dict[str, Any],
    label: str,
    name_sets: Dict[str, Set[str]],
    alias_maps: Dict[str, Dict[str, str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """Process-mode Delta is a mergeable patch, not a complete final element."""
    valid = VALID_FIELDS[collection_key]
    for field in item.keys():
        if field not in valid:
            errors.append(f"{label} 包含规范外字段[{field}]；非标准信息必须放入详情")

    for field in LIST_FIELDS:
        if field in item:
            value = item[field]
            if not isinstance(value, list):
                process_issue(warnings, label, f".{field} 建议为数组")
            else:
                for i, elem in enumerate(value):
                    if not isinstance(elem, str):
                        errors.append(f"{label}.{field}[{i}] 必须是字符串")

    for field in STRING_FIELDS:
        if field in item and not isinstance(item[field], str):
            process_issue(warnings, label, f".{field} 建议为字符串")

    if "是否主角" in item and not isinstance(item["是否主角"], bool):
        process_issue(warnings, label, ".是否主角 建议为布尔值")
    if "性别" in item and (not isinstance(item["性别"], int) or item["性别"] not in (0, 1, 2)):
        process_issue(warnings, label, ".性别 建议为整数0/1/2")
    if "年龄" in item and not isinstance(item["年龄"], int):
        process_issue(warnings, label, ".年龄 建议为整数")
    if "重量级" in item and (not isinstance(item["重量级"], int) or item["重量级"] < 0 or item["重量级"] > 100):
        process_issue(warnings, label, ".重量级 建议为0-100之间整数")
    if collection_key == "角色集" and "生日" in item and not is_iso_time(item["生日"]):
        process_issue(warnings, label, f".生日 建议为合法ISO时间，未知可用{BASE_TIME}")

    detail = item.get("详情")
    if detail is not None:
        validate_detail_schema(detail, label, name_sets, errors, warnings, mode="delta", alias_maps=alias_maps)
    else:
        process_issue(warnings, label, ".详情 缺失，normalize_story_schema.py 将补为空对象")

    if collection_key in TRACEABLE_COLLECTION_KEYS:
        if not isinstance(detail, dict):
            return
        for field in TRACE_DETAIL_FIELDS:
            value = detail.get(field)
            if value is None:
                process_issue(warnings, label, f".详情.{field} 缺失")
            elif not isinstance(value, str) or not value or normalize_chapter_value(value) != value:
                process_issue(warnings, label, f".详情.{field} 建议为固定宽度章节序号，例如0001")
        first = detail.get("首次章节")
        recent = detail.get("最近章节")
        if (
            isinstance(first, str)
            and isinstance(recent, str)
            and normalize_chapter_value(first) == first
            and normalize_chapter_value(recent) == recent
            and int(first) > int(recent)
        ):
            errors.append(f"{label}.详情.首次章节 不得晚于 最近章节")

    if collection_key == "事件集":
        detail_obj = detail if isinstance(detail, dict) else {}
        involved = detail_obj.get("涉及章节")
        if involved is None:
            process_issue(warnings, label, ".详情.涉及章节 缺失")
        elif not isinstance(involved, str) or not involved or normalize_involved_chapters(involved) != involved:
            process_issue(warnings, label, ".详情.涉及章节 建议为固定宽度、升序、去重的字符串")
        elif "时间" in item:
            expected_time = chapter_time(first_involved_chapter(item))
            if item.get("时间") != expected_time:
                process_issue(warnings, label, f".时间 建议等于最小涉及章节对应章节时间[{expected_time}]")

    validate_refs(collection_key, item, label, name_sets, alias_maps, errors, warnings, "process")
```

- [ ] **Step 3: Change `validate_elements` to dispatch by mode**

In `validate_elements`, replace this block:

```python
                validate_types(collection_key, item, label, errors, warnings)
                validate_evidence_detail(item, label, errors)
                validate_refs(collection_key, item, label, name_sets, alias_maps, errors, warnings, mode)
```

with:

```python
                if mode == "process":
                    validate_process_patch_item(collection_key, item, label, name_sets, alias_maps, errors, warnings)
                else:
                    validate_types(collection_key, item, label, errors, warnings)
                    validate_evidence_detail(item, label, errors)
                    validate_refs(collection_key, item, label, name_sets, alias_maps, errors, warnings, mode)
```

- [ ] **Step 4: Preserve governance temporal guard after mode dispatch**

Do not move or remove the existing block:

```python
                if mode == "governance" and collection_key == "事件集":
                    existing_event = existing_event_by_name_or_alias(current, name.strip())
                    ...
```

It must remain after the strict validation dispatch so ordinary governance still prevents temporal rewrites.

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python -m pytest scripts/test_validate_delta.py -q
```

Expected after implementation: all tests in `scripts/test_validate_delta.py` pass.

---

### Task 3: Update Delta extraction prompt to match permissive patch semantics

**Files:**
- Modify: `prompts/delta_extract.j2`

**Interfaces:**
- Consumes: pipeline variables already used by the template, including `chapter_id`, `chapter_seq`, `chapter_time`, and `chapter_text`.
- Produces: LLM task instructions aligned with relaxed process validation and existing merge/normalize stages.

- [ ] **Step 1: Update the `修改元素` section**

Find the section that currently says:

```markdown
### 修改元素

只填写本章**新增或变化**的字段，不要回填旧字段、不要重写 `介绍`。仍然必须在 `详情` 里写 `提取理由`，并按上表更新 `最近章节`（角色/地点/线索/阵营/物品）或追加 `涉及章节`（事件，用中文全角逗号拼接历史与本章）。
```

Replace it with:

```markdown
### 修改元素

`修改元素` 是 patch，不是完整元素重写：只填写本章**新增或变化**的字段，不要为了凑 schema 回填旧字段、不要重写旧 `介绍`。建议在 `详情` 里写 `提取理由`；角色/地点/线索/阵营/物品若能确认本章再次出现，可写 `最近章节: "{{ chapter_id }}"`，但不要强行回填 `首次章节`。事件若能确认涉及本章，可在 `详情.涉及章节` 中补充本章；不确定时把证据写入普通详情键，后续 normalize/治理会收敛。
```

- [ ] **Step 2: Replace overly strict common mistakes wording**

In the `### 常错点速查` section, replace these lines:

```markdown
- `首次章节`/`最近章节`/`涉及章节`：必须放在 `详情` 里，不能放在元素顶层。
- `提取理由`：必须放在 `详情` 里，每个新增/修改元素都不能缺。
```

with:

```markdown
- `首次章节`/`最近章节`/`涉及章节`：如果填写，必须放在 `详情` 里，不能放在元素顶层；修改元素不要强行回填旧的 `首次章节`。
- `提取理由`：建议放在 `详情` 里，便于追溯；过程校验不会因为缺少它反复卡住，但质量治理会检查证据是否充分。
```

- [ ] **Step 3: Replace strict reference hard-stop wording**

Replace this line:

```markdown
- 引用一致性：`所属阵营`/`参与成员`/`发生地点`/`父级地点`/`座落地点`/`涉及事件`/`目标事件`/`关系` 里的名称必须是**当前结构索引已有元素**或**本 Delta 新增元素**，否则按"不存在引用"卡住。陌生名称放进详情说明，不要塞结构引用字段。
```

with:

```markdown
- 引用一致性：`所属阵营`/`参与成员`/`发生地点`/`父级地点`/`座落地点`/`涉及事件`/`目标事件`/`关系` 尽量使用**当前结构索引已有元素**或**本 Delta 新增元素**的正式名称；过程阶段前向引用会先作为 warning，周期/最终治理再收敛。陌生且不确定的名称优先放进详情说明，不要为了通过校验编造元素。
```

- [ ] **Step 4: Replace the final strict instruction block**

At the end of the Delta requirements, replace:

```markdown
- 新增元素必须补齐所有标准字段；非标准信息必须放入 `详情`。
- 上述未覆盖的边角情况以 `scripts/validate_delta.py` 校验报告为准。**遇到校验失败时，按报告精确修复出错的字段，不要重写整个 Delta**——重写会引入新的不一致和漂移。
```

with:

```markdown
- Delta 是可合并 patch：新增元素尽量补齐标准字段，但不要为凑字段编造原文没有的信息；缺失的默认字段会由 normalize 阶段补齐。
- 不要编写 Python 或脚本来生成 Delta。直接写 JSON 内容即可；JSON 语法、常见类型和顶层追溯字段位置会由 `repair_llm_json.py` / `coerce_delta.py` 机械处理。只有名称、分类、引用语义、原文证据这类内容问题需要你判断修复。
- 上述未覆盖的边角情况以 `scripts/validate_delta.py` 校验报告为准；过程阶段 warning 不需要反复重写 Delta，只有 error 才需要修。
```

- [ ] **Step 5: Search prompt for remaining contradictory strict language**

Run:

```bash
python - <<'PY'
from pathlib import Path
p = Path('prompts/delta_extract.j2')
text = p.read_text(encoding='utf-8')
for needle in ['必须补齐所有标准字段', '每个新增/修改元素都不能缺', '否则按"不存在引用"卡住', '不要写 Python']:
    print(needle, '=>', needle in text)
PY
```

Expected:

```text
必须补齐所有标准字段 => False
每个新增/修改元素都不能缺 => False
否则按"不存在引用"卡住 => False
不要写 Python => False
```

The exact phrase `不要编写 Python` should remain present; the old phrase `不要写 Python` should not be required.

---

### Task 4: Verify pipeline-level behavior with merge and structure validation

**Files:**
- Modify: `scripts/test_validate_delta.py`

**Interfaces:**
- Consumes: `merge_delta.py`, `normalize_story_schema.py`, and `validate_structure.py` CLIs.
- Produces: regression coverage proving loose Delta can still become valid structure after normalize.

- [ ] **Step 1: Add a CLI run helper for normalize if not already sufficient**

No new helper is required because `run(cmd)` already exists in `scripts/test_validate_delta.py`.

- [ ] **Step 2: Add minimal new-element merge-normalize test**

Append this test before the `if __name__ == "__main__"` block:

```python
def test_process_delta_minimal_new_element_merges_then_normalizes_to_valid_structure():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        current = base_story()
        patch = {
            "章节": "第002章 测试",
            "新增元素": {
                "角色集": [{"名称": "王五", "详情": {"提取理由": "本章首次出现"}}],
                "事件集": [],
                "地点集": [],
                "线索集": [],
                "阵营集": [],
                "物品集": [],
            },
            "修改元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]},
        }
        write_json(story, current)
        write_json(delta, patch)

        validate = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert validate.returncode == 0, validate.stdout

        merge = run([sys.executable, str(MERGER), str(story), str(delta)])
        assert merge.returncode == 0, merge.stdout

        normalize = run([sys.executable, str(ROOT / "normalize_story_schema.py"), str(story), "--in-place"])
        assert normalize.returncode == 0, normalize.stdout

        check = run([sys.executable, str(STRUCT_VALIDATOR), "--chapter-check", "--mode", "process", str(story), str(delta)])
        assert check.returncode == 0, check.stdout
```

- [ ] **Step 3: Run the full relevant test set**

Run:

```bash
python -m pytest scripts/test_validate_delta.py scripts/test_coerce_delta.py -q
```

Expected: all tests pass.

---

### Task 5: Optional coerce improvement for `详情.补充标签`

**Files:**
- Modify: `scripts/coerce_delta.py`
- Modify: `scripts/test_coerce_delta.py`

**Interfaces:**
- Consumes: `coerce_delta_in_place(delta: dict) -> list[str]`.
- Produces: deterministic conversion of `详情.补充标签` list values to compact JSON strings, matching `story_schema_rules.parse_supplementary_tags`.

Only do this task if current tests or manual reproduction show `补充标签` array is still a frequent process-mode blocker after Task 2. If not needed, skip the task and state it was skipped.

- [ ] **Step 1: Add failing coerce test**

In `scripts/test_coerce_delta.py`, append:

```python
def test_补充标签_数组_转json字符串():
    delta = _empty_delta()
    delta["新增元素"]["角色集"].append(
        {"名称": "甲", "详情": {"补充标签": ["莽撞", "客栈"]}}
    )
    report = coerce_delta_in_place(delta)
    value = delta["新增元素"]["角色集"][0]["详情"]["补充标签"]
    assert value == '["莽撞","客栈"]'
    assert any("补充标签" in line for line in report)
```

- [ ] **Step 2: Implement deterministic conversion**

In `scripts/coerce_delta.py`, update imports from `story_schema_rules` to include `SUPPLEMENTARY_TAGS_DETAIL_KEY` and `dump_supplementary_tags`:

```python
from story_schema_rules import (
    COLLECTION_KEYS,
    TRACEABLE_COLLECTION_KEYS,
    SUPPLEMENTARY_TAGS_DETAIL_KEY,
    dump_supplementary_tags,
)
```

Then add this function after `_coerce_involved_chapters`:

```python
def _coerce_supplementary_tags(detail: Dict[str, Any], label: str, report: List[str]) -> None:
    if SUPPLEMENTARY_TAGS_DETAIL_KEY not in detail:
        return
    value = detail[SUPPLEMENTARY_TAGS_DETAIL_KEY]
    if not isinstance(value, list):
        return
    tags = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    if tags:
        detail[SUPPLEMENTARY_TAGS_DETAIL_KEY] = dump_supplementary_tags(tags)
        report.append(f"{label}.详情.{SUPPLEMENTARY_TAGS_DETAIL_KEY} 数组 → JSON字符串")
```

In `_coerce_item`, after `detail = _ensure_detail(item)`, add:

```python
    _coerce_supplementary_tags(detail, label, report)
```

- [ ] **Step 3: Run coerce tests**

Run:

```bash
python -m pytest scripts/test_coerce_delta.py -q
```

Expected: all tests pass.

---

### Task 6: Final verification

**Files:**
- No direct file edits.

**Interfaces:**
- Consumes: modified validator, tests, and prompt.
- Produces: evidence that relaxed process-mode Delta validation works and stricter downstream gates remain intact.

- [ ] **Step 1: Run targeted validation/coerce tests**

Run:

```bash
python -m pytest scripts/test_validate_delta.py scripts/test_coerce_delta.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run pipeline-related tests if present**

Run:

```bash
python -m pytest scripts/test_audit_pack.py scripts/test_atomic_json_writes.py -q
```

Expected: all tests pass. If tests are unavailable or fail for unrelated pre-existing reasons, capture the exact output and do not claim full verification.

- [ ] **Step 3: Run a prompt consistency search**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path('prompts/delta_extract.j2').read_text(encoding='utf-8')
for phrase in [
    '修改元素` 是 patch',
    '不要编写 Python 或脚本来生成 Delta',
    '过程阶段 warning 不需要反复重写 Delta',
]:
    print(f'{phrase}:', phrase in text)
PY
```

Expected:

```text
修改元素` 是 patch: True
不要编写 Python 或脚本来生成 Delta: True
过程阶段 warning 不需要反复重写 Delta: True
```

- [ ] **Step 4: Review git diff**

Run:

```bash
git diff -- scripts/validate_delta.py scripts/test_validate_delta.py scripts/coerce_delta.py scripts/test_coerce_delta.py prompts/delta_extract.j2
```

Expected: diff only contains the planned validation, tests, optional coerce, and prompt updates.

---

## Self-Review

- Spec coverage: The plan covers process-mode permissiveness, patch semantics for `修改元素`, downstream validation preservation, prompt alignment, and optional deterministic coerce for `补充标签`.
- Placeholder scan: No TBD/TODO placeholders remain. The optional task has an explicit skip condition and implementation details.
- Type consistency: New helper signatures use existing imports and type aliases already present in `validate_delta.py`; tests use existing `run`, `write_json`, `base_story`, and script constants.
