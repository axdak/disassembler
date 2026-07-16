# 命令说明与资源清单

## 主控器运行方式

`run_pipeline.py` 默认不直接调用大模型。它负责推进已经具备产物的章节；遇到需要模型判断的章节、修复或审计工作时，会生成任务包并以返回码 `2` 交接给主控Agent。若显式配置外部 worker 命令，脚本会调用 worker 写当前任务包指定产物，并继续执行验收、校验、合并、快照和回滚。

```bash
python <skill_path>/scripts/run_pipeline.py split <项目目录> <原文文件>
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode subagent
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode serial
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode worker --worker-provider auto
```

内置 CLI worker 自动选择模式：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> \
  --run-mode worker \
  --worker-provider auto \
  --worker-timeout-seconds 1800 \
  --worker-retries 2
```

`--run-mode subagent` 表示主Agent调度子Agent，`--run-mode serial` 表示主Agent在返回 `2` 后亲自完成任务包并立刻重跑，`--run-mode worker` 表示脚本调用外部 worker。worker 默认 `--worker-loop supervised`，每次 worker 产出一个任务包或章节提交检查点后返回前台；`--worker-provider auto` 会优先选择 Agy；如果 Agy 不可用，再尝试 CodeBuddy。也可以显式指定：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode worker --worker-provider agy
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode worker --worker-provider codebuddy
```

外部 worker 模式：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> \
  --run-mode worker \
  --chapter-command "<章节worker命令>" \
  --audit-command "<审计worker命令>" \
  --worker-timeout-seconds 1800 \
  --worker-retries 2
```

内置通用 wrapper 可接 Antigravity CLI 或 CodeBuddy：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> \
  --run-mode worker \
  --chapter-command "python <skill_path>/tools/agent_worker.py --provider codebuddy" \
  --audit-command "python <skill_path>/tools/agent_worker.py --provider codebuddy"
```

将 `--provider codebuddy` 替换为 `--provider agy` 或 `--provider auto` 即可切换 provider。若 `agy.exe` 未加入 PATH，wrapper 会优先尝试 `%LOCALAPPDATA%\agy\bin\agy.exe`；也可在运行前显式设置：
```powershell
$env:ND_AGY_BIN = "C:\Users\53519\AppData\Local\agy\bin\agy.exe"
```

也可通过 `ND_AGY_BIN`、`ND_CODEBUDDY_BIN` 指定 CLI 完整路径，通过 `ND_CODEBUDDY_ARGS` 追加 CodeBuddy 参数，通过 `ND_AGENT_MODEL` 指定模型，通过 `ND_AGENT_PROVIDER_ORDER` 调整 auto 顺序。

worker 命令通过环境变量接收任务：
在 Windows 上，`run`、`visual-assets-auto` 和 `chapter-structure-auto` 的外部 worker 默认使用 `--worker-window hidden`：
```powershell
--worker-window hidden
```
默认模式不会为每个 worker 任务打开新的 PowerShell 窗口；输出会进入当前终端，并写入项目的 `质量治理/worker日志/`。
需要可见窗口时才使用 `--worker-window powershell`：
```powershell
--worker-window powershell
```
脚本会为每个 worker 任务打开一个新的 PowerShell 窗口，worker 结束后窗口自动关闭；完整输出仍会写入项目的 `质量治理/worker日志/`。窗口不保证展示模型逐 token 交互过程，尤其是 Agy provider 会以目标文件和后续校验结果作为成功依据；窗口主要用于观察 worker 命令、错误、退出码和日志路径。
如果需要确认命令确实在弹出的窗口中执行，并查看退出码，可改用：
```powershell
--worker-window powershell-keep
```
该模式会在 worker 结束后停在 PowerShell 窗口中，按 Enter 后再关闭。

```text
ND_TASK_TYPE=analysis | analysis_regenerate | delta | repair_chapter | audit_correction | audit_repair
ND_PROJECT_DIR=<项目目录>
ND_TASK_PACK=<任务包路径>
ND_EXPECTED_OUTPUT=<期望输出文件>
ND_CHAPTER_SEQ=<章节序号，审计任务为空>
```

worker 必须只写 `ND_EXPECTED_OUTPUT` 指向的目标产物。`run_pipeline.py` 不信任 worker 自述成功；只按目标文件、章节分析校验、Delta/governance 校验、结构校验和快照结果继续推进。

如果希望先批量完成所有章节分析 MD，再进入结构 JSON 阶段，可显式拆成两段运行：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> --phase analysis --run-mode worker --worker-provider auto
python <skill_path>/scripts/run_pipeline.py run <项目目录> --phase delta --run-mode worker --worker-provider auto
```

`--phase analysis` 只生成/校验章节分析 MD，不创建 Delta 任务、不提交故事结构、不触发周期治理；`--phase delta` 会先要求所有章节分析 MD 通过现有校验，然后沿用原有串行 Delta 提交、快照、回滚和周期治理链。未指定 `--phase` 时保持旧的逐章交错流程。

视觉资产分章和故事结构章节分析也使用同一套路由：
```bash
python <skill_path>/scripts/run_pipeline.py visual-assets-auto <项目目录> --run-mode subagent
python <skill_path>/scripts/run_pipeline.py visual-assets-auto <项目目录> --run-mode serial
python <skill_path>/scripts/run_pipeline.py visual-assets-auto <项目目录> --run-mode worker --worker-provider auto

python <skill_path>/scripts/run_pipeline.py chapter-structure-auto <项目目录> --run-mode subagent
python <skill_path>/scripts/run_pipeline.py chapter-structure-auto <项目目录> --run-mode serial
python <skill_path>/scripts/run_pipeline.py chapter-structure-auto <项目目录> --run-mode worker --worker-provider auto
```

这两类命令的 `worker` 路由也可以显式指定多产物 worker：
```bash
python <skill_path>/scripts/run_pipeline.py visual-assets-auto <项目目录> --run-mode worker --worker-command "python <skill_path>/tools/agent_worker.py --provider codebuddy"
python <skill_path>/scripts/run_pipeline.py chapter-structure-auto <项目目录> --run-mode worker --worker-command "python <skill_path>/tools/agent_worker.py --provider codebuddy"
```

未配置 worker 时，返回 `2` 后，主控Agent/人工必须根据当前阶段任务包立即产出：
```text
task_chNNN_analysis.md -> 章节处理/第NNN章_xxx.md
task_chNNN_delta.md -> 章节处理/第NNN章_xxx.json
```

配置 `--run-mode worker` 加 `--worker-provider`、`--chapter-command` 或 `--audit-command` 后，返回 `2` 可能是默认 supervised 检查点，也可能表示 worker 未能完成交接、缺少配置或期望产物未写出；此时主控Agent应查看 worker 日志、任务状态和当前产物，然后重试同一条 worker 命令，不应在主会话中亲自写语义产物。不得等待 task-notification；不得使用后台任务托管主流程；必须等待前台命令返回码。`subagent` 和 `serial` 路由则分别由子Agent或主Agent按任务包产出后继续重跑。

然后继续：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录> --run-mode worker --worker-provider auto
```

返回码、任务包优先级、周期审计的处理纪律见 `references/autonomous_loop.md`。

查看断点：
```bash
python <skill_path>/scripts/run_pipeline.py resume <项目目录>
```

提交单章：
```bash
python <skill_path>/scripts/run_pipeline.py commit-chapter <项目目录> --chapter N
```

按需/周期治理补丁提交：
```bash
python <skill_path>/scripts/run_pipeline.py commit-governance <项目目录> <治理补丁.json>
```

## 步骤3：基准全书报告固化

步骤3不再代表"唯一一次全书分析"，而是代表：当主控Agent认为当前阶段已经足够稳定时，把 A 工作台中生成的分析结果固化为一版基准报告。

推荐顺序仍然是：
```text
summary → characters → plot → worldview → settings → plotlines → outline → detailed_outline → visual_assets → chapter_structure → narrative_structure → report
```
但这只是报告依赖顺序，不是限制分析能力只能在步骤3执行。

完成某类报告后，可以记录状态：
```bash
python <skill_path>/scripts/progress_manager.py update <项目目录> 3 '{"已完成报告": ["summary", "characters"]}'
```

全部基准报告完成后：
```bash
python <skill_path>/scripts/progress_manager.py complete <项目目录> 3
```

## 步骤4：最终整理与交付

```bash
# 1. 生成最终整理任务包（复制增量为草稿 + final校验 + 打包全书分析/审计参考材料）
python <skill_path>/scripts/run_pipeline.py final-pack <项目目录>

# 2. 模型修改 故事结构_草稿.json 后，提交最终草稿（规范化 + strict校验，失败生成修复任务包）
python <skill_path>/scripts/run_pipeline.py commit-final-draft <项目目录>

# 3. 独立最终校验（可选，可指定 --file 校验不同文件）
python <skill_path>/scripts/run_pipeline.py validate-final <项目目录>

# 4. 校验通过后正式交付（前置strict校验 → 复制为故事结构.json → 完成步骤4 → 审计日志）
python <skill_path>/scripts/run_pipeline.py finalize <项目目录>
```

典型最终交付流程：
```text
final-pack → 模型根据任务包修改草稿 → commit-final-draft
  ↓ 失败：根据 repair_final.md 修正 → 再次 commit-final-draft
  ↓ 通过：finalize → 输出 故事结构.json，步骤4完成
```

## Obsidian Vault 导出

Obsidian 导出是只读镜像：`故事结构.json` 或 `故事结构_增量.json` 仍然是主库，导出的 Markdown Vault 用于阅读、图谱、反向链接、Dataview 查询、人工批注和 AI 插件检索；不要把 Obsidian 修改反写回故事结构 JSON。

推荐入口：

```bash
python <skill_path>/scripts/export_obsidian_vault.py <项目目录> \
  --source final \
  --out <项目目录>/导出/obsidian-vault \
  --single-file <项目目录>/导出/故事结构总览.md \
  --include-dataview \
  --include-canvas \
  --clean
```

也可以直接指定 JSON 文件：

```bash
python <skill_path>/scripts/export_obsidian_vault.py <项目目录>/故事结构.json \
  --out <项目目录>/导出/obsidian-vault \
  --single-file <项目目录>/导出/故事结构总览.md \
  --include-dataview \
  --clean
```

常用参数：

- `--source final|process`：输入为项目目录时，选择 `故事结构.json` 或 `故事结构_增量.json`。
- `--single-file <md路径>`：同时生成一个完整大的 Markdown 文件，便于 AI 插件、Claude Obsidian 或全文阅读。
- `--include-dataview`：生成 `_dataview/查询-主角.md`、`查询-未回收线索.md`、`查询-按章节事件.md`、`查询-按阵营角色.md`。
- `--include-canvas`：生成 `00-总览/故事图谱.canvas`。
- `--filename-mode name|type-name`：对象页文件名使用纯名称，或加类型前缀避免跨目录迁移时冲突。
- `--clean`：导出前清空旧 Vault，避免已经删除的对象残留。

导出目录结构：

```text
obsidian-vault/
  00-总览/
    故事总览.md
    时间线.md
    角色索引.md
    阵营索引.md
    地点索引.md
    线索索引.md
    物品索引.md
    其他事项索引.md
    故事图谱.canvas
  角色/
  事件/
  地点/
  线索/
  阵营/
  物品/
  其他事项/
  _dataview/
  _meta/
```

## 资源清单

### scripts/

**主控 & 进度**
- `run_pipeline.py`：主控器，推进章节、生成任务包、提交章节/治理/最终交付；默认不调用大模型，配置外部 worker 后可自动交接当前任务包并验收产物。
- `progress_manager.py`：进度管理脚本，初始化、查询、更新、断点恢复、摘要生成。
- `tools/agent_worker.py`：外部 CLI worker wrapper，支持 `auto`、`agy`、`codebuddy` provider，把当前任务包转换为 headless prompt；单产物任务把 CLI stdout 写入期望产物，多产物任务通过 `ND_EXPECTED_OUTPUTS` 要求 provider 直接写项目文件并由主控验收。

**Obsidian / Markdown 导出**
- `export_obsidian_vault.py`：把 `故事结构.json` 或 `故事结构_增量.json` 导出为 Obsidian Vault，并可同时生成完整大 Markdown。

**步骤 1：章节拆分**
- `split_chapters.py`：章节拆分脚本，自动检测章节模式并拆分，支持 txt/docx。

**步骤 2：逐章提取 & 合并**
- `repair_llm_json.py`：修复 LLM 生成的 JSON 语法（Markdown 代码块、单引号、尾逗号等），输出严格 JSON。
- `coerce_delta.py`：Delta 入库前机械纠错层，确定性类型/格式修复（性别映射、章节序号补零、时间推导等），减少 repair 循环。
- `compress_tags.py`：标签自动压缩，把带受控前缀的标签和超限自由标签降级到 `详情.补充标签`。
- `validate_delta.py`：Delta 入库前质量校验脚本，检查 schema、字段、类型、重复、交叉引用和新增/修改语义；支持 `--mode process/governance/final`。
- `merge_delta.py`：Delta 合并脚本，将单章 Delta 字段级深度合并到增量 JSON，只增不删，支持别名匹配。
- `validate_structure.py`：结构验证脚本，支持 `--mode process/governance/final` 三档模式、`--strict`（等价 `--mode final`）、`--chapter-check`；其中 `--chapter-check` 是每切片合并后的全量过程库检查，并附带本切片触碰元素统计。
- `normalize_story_schema.py`：保全式故事结构规范化，迁移非标准字段到详情、补齐默认值、别名规范化、无效引用隔离；不删信息。

**治理 & 审计**
- `apply_governance_ops.py`：执行治理补丁中的确定性操作：合并元素、重命名、替换引用、标签治理、降级、删除、设置字段。
- `diff_structure.py`：生成故事结构 before/after 差异报告（新增/删除/修改元素对比）。

**步骤 3：全书分析**
- `analysis_context_pack.py`：全书/局部分析上下文打包器，支持按章节范围/目标元素分包、reduce 归约，驱动全部分析任务类型。

**时间线 & 规则**
- `chronology.py`：章节序号、章节时间和事件分组的统一规则（库模块，被多个脚本引用）。
- `story_schema_rules.py`：故事结构 JSON Schema 常量与校验规则（库模块，被校验器和规范化脚本引用）。
- `migrate_chronology.py`：一次性迁移工具，将既有故事结构迁移到章节时间、紧凑追溯字段和事件顺序规范。

### references/
- `process_overview.md`：流程总览（核心决策、Agent分工、流程表、单章流程）。
- `file_structure.md`：项目文件结构、初始化、步骤1拆章、每章硬产物清单。
- `autonomous_loop.md`：自主循环执行纪律与进度汇报规范。
- `commands_and_resources.md`：本文档，命令说明与资源清单。
- `checkpoint_mechanism.md`：断点续传机制详细说明。
- `story_structure_spec.md`：故事结构JSON完整规范。
- `story_structure_quality_governance.md`：故事结构质量治理说明。
- `visual_asset_spec.md`：视觉资产、分镜和AI绘图素材提取规范。
- `v12_patch_notes.md`：本版分组与视觉资产增强说明。
