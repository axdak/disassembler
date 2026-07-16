---
name: novel-disassembler
description: 拆书技能——将小说原文按章节拆分，逐章生成MD章节分析，再基于当前故事结构JSON、本章原文和章节分析提取Delta，并通过validate_delta.py、merge_delta.py、validate_structure.py形成可校验、可回滚、可审计的故事结构JSON。适用于拆解小说、分析小说结构、提取角色/事件/地点/线索/阵营/物品、生成高质量故事JSON，并沉淀视觉资产/分镜/AI绘图素材。
agent_created: true
---

# Novel Disassembler — 拆书技能

你是拆书流程主控。不要一次读完整本书。根据用户任务**按需读取** references 目录中的详细规则。

## 固定原则

1. 每次只处理一个章节切片。
2. 章节分析 MD 和 Delta JSON 分开生成。
3. 模型只输出 Delta，不重写完整故事结构。
4. 合并、校验、断点、回滚由脚本负责。
5. 遇到具体任务时，按下表加载对应文档。
6. 用户要求持续拆书或持续分章分析时，主控Agent必须自主循环，按 `--run-mode` 明确选择路由：`subagent`=主Agent调度、子Agent产出任务包；`serial`=主Agent在 `run_pipeline.py run` / `visual-assets-auto` / `chapter-structure-auto` 返回 `2` 后立刻按当前任务包产出，再重新运行同一命令；`worker`=由外部 CLI worker 产出。返回 `2` 表示有待办任务包或 worker 交接未完成，不是失败；主会话必须继续监控、重试或处理硬阻塞，不能把返回 `2` 当作流程结束。
7. 周期审计必须由主控Agent读取任务包后产出真实 `correction_XXX.json`；不得通过空 Delta、伪造 worker 或跳过审计来解除待办。
8. 单章必须分两次独立模型分析：`task_chNNN_analysis.md` 只产出章节分析MD；重新运行后生成的 `task_chNNN_delta.md` 才读取该MD并只产出Delta JSON。不得在分析任务中写Delta，也不得在Delta任务中重写分析MD。
   - 如需先批量完成全部章节分析，再进入结构 JSON 阶段，按所选路由追加 `--run-mode subagent|serial|worker`；worker 路由使用 `run_pipeline.py run <项目目录> --phase analysis --run-mode worker --worker-provider auto` 循环到返回 `0`，再使用 `run_pipeline.py run <项目目录> --phase delta --run-mode worker --worker-provider auto` 循环推进 Delta 与提交。未指定 `--phase` 时保持逐章交错流程。
9. 章节分析MD和Delta JSON的语义内容必须来自模型对本章原文、章节分析和当前结构索引的阅读理解。禁止编写或运行 Python/Bash/PowerShell 等脚本来批量生成、补全、扩写或替代角色、事件、地点、线索、阵营、物品、其他事项等语义内容。
10. 允许使用项目已有确定性修复脚本，或必要时使用一次性 format-only 修复脚本，处理已经存在的当前章节 Delta JSON 的语法、字符串转义、字段类型、章节时间、标签压缩等机械格式问题；这类脚本不得新增剧情事实、不得新增语义元素、不得读取原文生成内容、不得以提高效率或推进多章为目的复用。
11. 外部 worker 自动生成模式只在 `--run-mode worker` 或 auto 路由检测到 worker 配置时开启；未显式选择 worker 前，`run_pipeline.py` 只生成任务包并由 subagent/serial/人工按任务包产出。配置后，脚本仍只信任校验结果，不信任 worker 自述成功。

   worker 模式下默认 `--worker-loop supervised`：每次 worker 写出一个任务包产物或提交一个章节检查点后，管线返回前台让主控Agent接收状态并重跑同一路由命令；不能启动后台 worker 后结束对话；不得把后台任务 ID 当作完成状态；不得等待 task-notification；不得使用后台任务托管主流程；必须等待前台命令返回码。worker 是当前任务包的执行单元，不是替代主控Agent的后台服务。只有用户明确要求无人值守连续跑到底时，才使用 `--worker-loop continuous`。

12. 若用户希望脚本自动选择或固定调用 Antigravity CLI 或 CodeBuddy，章节主流程使用 `run_pipeline.py run <项目目录> --run-mode worker --worker-provider auto|agy|codebuddy`；视觉资产和章节结构分章分析使用 `visual-assets-auto` / `chapter-structure-auto` 搭配 `--run-mode worker --worker-provider auto|agy|codebuddy` 或显式 `--worker-command`。这是受控外部 worker 入口，等价于显式配置内置 wrapper；`auto` 默认优先调用 Agy，Agy 不可用时尝试 CodeBuddy；不再支持 Claude Code provider。默认 supervised 会频繁返回 `2` 作为前台监督检查点；需要恢复旧的一条命令连续跑到底时显式追加 `--worker-loop continuous`。

## 严格执行边界

你（主控 Agent）执行 `novel-disassembler` 项目的流水线时，请严格遵守当前 `--run-mode` 的委派规则：

1. **按路由执行**：`subagent` 模式下，脚本返回 `2` 后由主Agent派发子Agent完成任务包，主会话只监控和重跑管线；`serial` 模式下，脚本返回 `2` 后主Agent可以亲自读取当前任务包并产出目标文件，然后立刻重跑管线；`worker` 模式下，任务包必须交给外部 worker，主Agent只看终端输出、worker 日志、状态文件和校验结果。

你绝对禁止

1. 自行跳过周期审计。
2. 自行判断审计不需要做。
3. 自行写 Python/JS/脚本修复故事结构 JSON。
4. 自行直接修改 故事结构\_增量.json。
5. 自行修改 merge_delta.py / validate_delta.py / run_pipeline.py。
6. 简化章节分析模板。
7. 简化 Delta JSON 字段。
8. 合并章节分析和 Delta 提取。
9. 一次处理多个章节。
10. 发现流程耗时长时自行缩短输出。

## 按需加载表 (Router)

当你要执行对应的任务时，请**务必先读取**右侧指定的参考文档和模板：

| 任务阶段                                                        | 需要读取的参考文件 / 模板 / Schema                                                                                                                                                                             |
| :-------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **了解全局流程**                                                | 读取 `references/process_overview.md`。                                                                                                                                                                        |
| **初始化项目 / 步骤1 拆章 / 每章硬产物**                        | 读取 `references/file_structure.md`。                                                                                                                                                                          |
| **持续自主执行 / 进度汇报**                                     | 读取 `references/autonomous_loop.md` 与 `prompts/autonomous_run.j2`。                                                                                                                                          |
| **`run_pipeline.py` 命令、步骤3/4 命令、资源清单**              | 读取 `references/commands_and_resources.md`。                                                                                                                                                                  |
| **断点续传 / 恢复**                                             | 读取 `references/checkpoint_mechanism.md`。                                                                                                                                                                    |
| **执行章节分析**                                                | 读取 `references/chapter_analysis.md` (规则) 和 `prompts/chapter_analysis.j2` (模板)。                                                                                                                         |
| **提取本章 Delta**                                              | 读取 `references/delta_extraction.md` (规则), `prompts/delta_extract.j2` (模板) 以及 `schemas/delta.schema.json`（仅作轻量格式提示；强校验见 `scripts/validate_delta.py` + `scripts/story_schema_rules.py`）。 |
| **周期性结构审计**                                              | 读取 `references/governance.md` (规则), `prompts/audit.j2` (模板) 以及 `schemas/audit_correction.schema.json`（同上，仅作格式提示；强校验见 `scripts/validate_delta.py`）。                                    |
| **整理最终交付物**                                              | 读取 `references/finalization.md` (规则) 和 `prompts/final_draft.j2` (模板)。                                                                                                                                  |
| **导出 Obsidian Vault / Markdown 知识图谱 / 故事结构大MD**      | 读取 `references/commands_and_resources.md` 的「Obsidian Vault 导出」部分；默认使用 `scripts/export_obsidian_vault.py`，不反写故事结构 JSON。                                                                 |
| **查阅全局结构规范**                                            | 读取 `references/story_structure_spec.md`。                                                                                                                                                                    |
| **查阅故事质量治理**                                            | 读取 `references/story_structure_quality_governance.md`。                                                                                                                                                      |
| **提取视觉资产/分镜**                                           | 读取 `references/visual_asset_spec.md`。                                                                                                                                                                       |
| **按章生成视觉资产 / 视觉资产自主循环**                         | 读取 `references/visual_asset_spec.md` 的「分章独立产物」段，以及 `references/analysis_workbench_commands.md` 中的 `--per-chapter` / `visual-assets-auto --run-mode subagent\|serial\|worker` 命令。                                               |
| **进行全书综合分析**                                            | 读取 `references/fullbook_analysis_workbench.md` 或 `references/analysis_four_dimensions.md`，执行命令参考 `references/analysis_workbench_commands.md`。                                                       |
| **执行故事结构维度（chapter_structure / narrative_structure）** | 读取 `references/analysis_narrative_structure.md`；执行命令参考 `references/analysis_workbench_commands.md` 的 `chapter-structure-auto --run-mode subagent\|serial\|worker` 和 narrative_structure 段。                                                    |
