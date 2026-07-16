# Controlled Delta Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Agent-created scripts from generating semantic chapter analysis/Delta content while preserving deterministic JSON format repair and documenting an API mode that stays disabled by default.

**Architecture:** This is a documentation/prompt contract change only. The existing pipeline remains task-package based; `run_pipeline.py` continues to use existing deterministic repair scripts. We clarify boundaries in `SKILL.md`, `prompts/autonomous_run.j2`, and `references/autonomous_loop.md` so future agents distinguish semantic generation from format-only repair.

**Tech Stack:** Markdown/Jinja prompt files, existing Python pipeline scripts, no new dependencies, no active API integration.

## Global Constraints

- Do not implement active LLM API calls in this change.
- Do not remove or weaken existing deterministic repair steps: `repair_llm_json.py`, `coerce_delta.py`, `compress_tags.py`.
- Semantic chapter analysis and semantic Delta content must come from model reading of the chapter materials, not from generated helper scripts.
- Format-only repair scripts are allowed only for mechanical correction of an already-existing current-chapter Delta and must not create or change story facts.
- API mode is documentation-only / future-facing and defaults to disabled.

---

### Task 1: Clarify skill-level contract

**Files:**
- Modify: `SKILL.md`

**Interfaces:**
- Consumes: Existing fixed principles in `SKILL.md`.
- Produces: A clarified rule set that downstream prompt files can reference conceptually.

- [ ] **Step 1: Update fixed principles**

Add a principle after the existing rule that separates analysis and Delta:

```markdown
9. 章节分析MD和Delta JSON的语义内容必须来自模型对本章原文、章节分析和当前结构索引的阅读理解。禁止编写或运行 Python/Bash/PowerShell 等脚本来批量生成、补全、扩写或替代角色、事件、地点、线索、阵营、物品、其他事项等语义内容。
10. 允许使用项目已有确定性修复脚本，或必要时使用一次性 format-only 修复脚本，处理已经存在的当前章节 Delta JSON 的语法、字符串转义、字段类型、章节时间、标签压缩等机械格式问题；这类脚本不得新增剧情事实、不得新增语义元素、不得读取原文生成内容、不得以提高效率或推进多章为目的复用。
11. LLM API 自动生成模式是未来可选能力，默认关闭；未显式配置并授权前，`run_pipeline.py` 只生成任务包并由主控Agent/人工按任务包产出。
```

- [ ] **Step 2: Review wording**

Verify the added rules do not contradict existing rules 1-8. Expected result: rules 9-11 refine automation boundaries without changing file paths or commands.

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "docs(skill): clarify controlled delta generation"
```

---

### Task 2: Clarify autonomous run prompt boundaries

**Files:**
- Modify: `prompts/autonomous_run.j2`

**Interfaces:**
- Consumes: Existing `越权创建禁令` and `真实质量门` sections.
- Produces: A precise distinction between banned semantic helper scripts and allowed format-only repair.

- [ ] **Step 1: Replace broad script ban wording with semantic/format split**

In `prompts/autonomous_run.j2`, update the `【越权创建禁令】` block so it includes this exact section:

```markdown
【语义产物脚本化禁令】

- 章节分析MD与Delta JSON的语义内容必须来自模型对本章原文、章节分析和当前结构索引的阅读理解。
- 禁止编写或运行 Python/Bash/PowerShell 等脚本来批量生成、补全、扩写或替代角色、事件、地点、线索、阵营、物品、其他事项等语义内容。
- 禁止创建 `generate_delta*.py`、`batch_delta*.py`、`worker*.py` 等以生成或推进多章语义 Delta 为目的的辅助脚本。
- 允许使用项目内已有确定性修复脚本，或在必要时编写一次性 format-only 修复脚本，处理已经存在的当前章节 Delta JSON 的语法、字符串转义、字段类型、章节时间、标签压缩等机械格式问题。
- format-only 修复脚本不得新增剧情事实、不得新增语义元素、不得读取原文生成内容、不得改写角色/事件/地点/线索等文本含义、不得以“提高效率/加速后续章节”为目的复用到多章。
- 如果需要 format-only 临时脚本，优先写入质量治理或临时位置，运行后说明其只做机械修复；不得把临时脚本作为章节语义产物的一部分提交。
```

- [ ] **Step 2: Preserve existing report discipline**

Keep the existing bans on emoji, percentage progress, `快速生成`, `高效`, and evidence/self-doubt quality gate.

- [ ] **Step 3: Commit**

```bash
git add prompts/autonomous_run.j2
git commit -m "docs(prompt): distinguish semantic delta generation from format repair"
```

---

### Task 3: Update autonomous loop reference and API note

**Files:**
- Modify: `references/autonomous_loop.md`

**Interfaces:**
- Consumes: Existing autonomous-loop behavior docs.
- Produces: User-facing explanation that mechanical repair is allowed, semantic script generation is banned, and API mode is future/default-off.

- [ ] **Step 1: Add a controlled generation section after the return-code priority list**

Insert this section after the list that ends with `task_chNNN_delta.md`:

```markdown
## 语义生成与机械修复边界

章节分析MD与Delta JSON的语义内容必须由模型阅读当前章节材料后生成。主控Agent不得为了推进章节数、避免手写JSON、减少Token或提高吞吐，编写脚本批量生成或补全角色、事件、地点、线索、阵营、物品、其他事项等语义内容。

允许的脚本化操作仅限 format-only 机械修复：对已经存在的当前章节 Delta JSON 进行 JSON 语法、字符串转义、字段类型、章节时间、标签压缩等确定性修复。优先使用项目已有脚本（如 `repair_llm_json.py`、`coerce_delta.py`、`compress_tags.py`）。必要的一次性临时脚本不得读取原文生成内容，不得新增剧情事实，不得复用到多章生成。
```

- [ ] **Step 2: Add API default-off note**

Append this section near the end:

```markdown
## LLM API 模式（预留，默认关闭）

未来可以增加由 `run_pipeline.py` 编排的 LLM API 模式：脚本负责读取章节材料、调用模型、保存章节分析与Delta、校验、回滚和重试；模型只负责自然语言理解与结构提取。当前版本不启用该模式，也不要求配置 API key。未显式实现、配置并授权前，`run_pipeline.py run` 仍只生成任务包并等待主控Agent或人工产出。
```

- [ ] **Step 3: Commit**

```bash
git add references/autonomous_loop.md
git commit -m "docs(reference): document controlled generation boundaries"
```

---

### Task 4: Verify documentation consistency

**Files:**
- Check: `SKILL.md`
- Check: `prompts/autonomous_run.j2`
- Check: `references/autonomous_loop.md`

**Interfaces:**
- Consumes: Changes from Tasks 1-3.
- Produces: Verified docs with no stale contradiction on script repair or API mode.

- [ ] **Step 1: Search for conflicting phrasing**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in [Path('SKILL.md'), Path('prompts/autonomous_run.j2'), Path('references/autonomous_loop.md')]:
    text = path.read_text(encoding='utf-8')
    print(f'--- {path} ---')
    for needle in ['脚本', 'format-only', 'API', '快速生成', '高效']:
        print(needle, text.count(needle))
PY
```

Expected: command succeeds and shows references to script constraints and API default-off note. Existing banned terms may remain only inside ban lists.

- [ ] **Step 2: Run lightweight tests for prompt/schema docs unaffected**

Run:

```bash
python -m pytest scripts/test_repair_llm_json.py scripts/test_coerce_delta.py -q
```

Expected: tests pass. If tests are unavailable in the environment, record the exact error.

- [ ] **Step 3: Commit verification-only fixes if needed**

If Step 1 or Step 2 reveals wording contradictions, edit the relevant docs and commit:

```bash
git add SKILL.md prompts/autonomous_run.j2 references/autonomous_loop.md
git commit -m "docs: resolve controlled generation wording"
```

---

## Self-Review

- Spec coverage: The plan covers prompt-level semantic script bans, preserves format-only repair, and documents API default-off behavior.
- Placeholder scan: No TBD/TODO placeholders remain.
- Type consistency: No code interfaces are introduced; file paths and script names match existing project names.
