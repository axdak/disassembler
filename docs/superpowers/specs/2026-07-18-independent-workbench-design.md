# Novel Disassembler 独立任务工作台设计

日期：2026-07-18

## 1. 背景与目标

桌面版目前把章节分析、Delta、章节结构、视觉资产、全书分析、最终整理和导出串成单一固定流水线。底层引擎仍保留原项目的全书/局部分析、按需质量治理、分章视觉资产、分章叙事结构和 Obsidian/Markdown 导出能力，但桌面端没有把这些能力暴露成可发现、可配置、可重跑的产品动作。

本设计保留 JSON 生产链的无人值守自动化，同时新增独立任务工作台，使用户可以随时主动发起分析、质量治理、分章任务和导出，而不把这些能力继续塞入单一 `current_stage` 状态机。

目标：

1. 自动生产线默认继续运行，不为分析交互强制暂停。
2. 用户可随时创建工作台任务；与当前项目写操作冲突时可靠排队。
3. 全书分析拆为多个独立维度，支持全书、章节范围、目标元素和自定义问题。
4. 视觉资产和叙事结构同时支持分章独立产物与全书汇总。
5. 用户可主动触发质量审计；审计通过后自动应用，并保留快照与差异。
6. 书籍追加章节后只处理新增章节，并使相关下游产物进入明确的过期状态。
7. 故事结构 JSON 可主动导出为大 Markdown、Obsidian Vault、Dataview 和 Canvas。
8. 修复 Windows 开发终端中文乱码，确保中文路径、日志和错误完整可读。

非目标：

- 不重写现有提示词、Schema、确定性合并或校验规则。
- 不把 Markdown/Obsidian 变成新的主数据源。
- 不允许同一项目存在并发 JSON 写入。
- 不在本次改造中重写整个引擎为通用 DAG。

## 2. 方案选择

采用“自动生产线 + 独立任务工作台”。

未采用的方案：

- 仅在现有概览页增加按钮：改动小，但任务状态、重跑、排队和过期语义仍与全流程耦合。
- 把完整流水线重写为 DAG：依赖表达更强，但改造范围和现有运行风险过大。

独立工作台通过统一任务模型调用现有引擎入口；自动流水线继续由现有 Orchestrator 驱动，两者共享项目级写入调度规则，但不共享阶段状态。

## 3. 产品信息架构

项目页包含以下标签：

```text
概览 | 章节 | 分析工作台 | 质量治理 | 导出 | 日志
```

### 3.1 概览

概览保留自动流水线的启动、暂停、取消、恢复、当前阶段、完成阶段和事件流，并增加：

- 当前章节索引版本与故事结构版本。
- 工作台排队数、运行中任务和最近失败任务。
- 新增章节检查入口。
- 已过期分析和导出数量。

自动流水线继续执行：

```text
新增章节识别（用户触发检查或选择新版书籍）
→ 章节分析 MD
→ Delta JSON
→ 校验与合并
→ 周期质量治理
→ 默认全书分析
→ 最终整理
→ 默认导出
```

工作台任务不要求自动流水线暂停；遇到项目写冲突时进入项目队列。

### 3.2 章节工作台

章节列表增加以下列：

- 章节分析状态
- Delta 状态
- 章节视觉资产状态
- 章节叙事结构状态
- 输入或产物是否过期
- 最近任务与错误

支持单章和多选批量动作：

- 重跑章节分析
- 重跑 Delta
- 生成分章视觉资产
- 生成分章叙事结构
- 预览分析、Delta、视觉资产和章节结构产物

分章视觉资产输出到：

```text
全书分析/视觉资产/分章/chNNN/
```

分章叙事结构输出到：

```text
全书分析/故事结构/分章/chNNN/章节结构.md
```

分章产物完成后，用户可以主动创建对应全书汇总任务。自动流水线也可按默认配置执行汇总。

### 3.3 分析工作台

每个分析维度显示为独立任务卡片：

1. 章节梗概 `summary`
2. 人物档案与人物关系 `characters`
3. 情节、冲突与伏笔 `plot`
4. 文风、节奏与高光 `style`
5. 世界观 `worldview`
6. 设定体系 `settings`
7. 剧情线与交汇矩阵 `plotlines`
8. 全书大纲 `outline`
9. 章节细纲 `detailed_outline`
10. 视觉资产全书汇总 `visual_assets`
11. 全书叙事结构 `narrative_structure`
12. 拆书总报告 `report`
13. 自定义分析 `custom`

卡片显示：未生成、最新、已过期、排队中、运行中、失败、已取消，以及上次运行范围、时间和产物入口。

创建分析任务时可配置：

- 章节范围：全部、单章、连续范围或离散章节。
- 目标元素：角色、事件、线索等。
- 自定义问题。
- 原文包含策略。
- 是否强制重跑。

### 3.4 质量治理

质量治理页同时展示自动周期审计和主动审计。

主动审计范围：

- 最近尚未审计的章节。
- 用户指定章节范围。
- 当前全书。

主动审计执行链：

```text
生成 audit pack
→ worker 生成 correction
→ validate_delta --mode governance
→ 提交治理补丁
→ apply_governance_ops / merge
→ validate_structure --mode governance
→ 成功：保存 before、after、diff 并提交
→ 失败：回滚过程 JSON，保留任务包、补丁、报告和日志
```

校验通过后自动应用，不要求人工确认。页面保留 correction、校验报告、before/after/diff 和受影响的过期产物列表。

### 3.5 导出中心

导出中心复用 `export_obsidian_vault.py`，支持：

- 过程 JSON 或最终 JSON 作为数据源。
- 单一故事结构大 Markdown。
- Obsidian Vault。
- Dataview 查询笔记。
- Canvas 故事图谱。
- 打开导出目录和强制重新导出。

JSON 始终是主数据。Markdown、Vault、Dataview 和 Canvas 是可重复生成的只读镜像，不提供反向写回。

## 4. 统一任务模型

新增 SQLite 表 `workbench_tasks`，至少包含：

```text
id
project_id
type
status
params_json
input_revision_json
result_json
error
created_at
started_at
ended_at
cancel_requested
```

任务类型：

```text
analysis
chapter_visual
chapter_structure
governance
export
append_chapters
chapter_analysis_rerun
chapter_delta_rerun
```

状态：

```text
queued
running
done
failed
cancelled
interrupted
```

每个任务记录：

- 任务参数和章节范围。
- 输入章节索引版本。
- 输入过程/最终故事结构版本。
- 预期和实际产物路径。
- worker 日志与错误摘要。
- 是否强制重跑。

应用重启时，`queued` 保留；残留 `running` 转为 `interrupted`，用户可重新排队。完成依据仍以实际产物和引擎校验结果为准，不仅依赖数据库状态。

## 5. 执行器与并发规则

新增 `WorkbenchTaskManager`，与现有 `RunManager` 分离。它负责持久化任务、调度适配器、取消、事件发送和重启恢复。

任务适配器：

- `AnalysisTaskAdapter`
- `ChapterVisualTaskAdapter`
- `ChapterStructureTaskAdapter`
- `GovernanceTaskAdapter`
- `ExportTaskAdapter`
- `AppendChaptersTaskAdapter`
- `ChapterRerunTaskAdapter`

适配器调用现有 Python 函数或受控 worker 入口，不复制引擎提示词或校验逻辑。

并发规则：

1. 同一项目只有一个修改项目事实文件的写任务可以运行。
2. 自动流水线、章节 Delta、质量治理、最终整理和追加章节属于写任务，必须串行。
3. 分析任务在开始时先生成固定任务包；任务包生成完成后，worker 基于稳定材料执行。
4. 导出读取一个明确的过程或最终 JSON 版本。
5. 不同项目可以并行。
6. 用户在冲突期间创建任务时，任务进入队列而不是丢失或返回模糊冲突。
7. 取消不删除已经通过校验的产物。

## 6. 输入版本与失效规则

项目计算两个轻量输入版本：

- `chapter_revision`：由 `_索引.json` 内容和相关章节源文件元数据派生。
- `story_revision`：由过程或最终故事结构文件的内容摘要派生。

每个任务完成时记录其依赖的版本。当前版本变化后，状态 API 根据任务类型和依赖映射返回 `stale=true`。

新增章节后：

- 新章节的分析、Delta、视觉资产和章节结构为未生成。
- 未修改的旧章节分章产物保持最新。
- 所有覆盖全书的分析报告标记过期。
- 全书视觉汇总和全书叙事结构标记过期。
- 最终 JSON、Markdown 和 Obsidian 导出标记过期。

主动质量治理改变过程 JSON 后：

- 原文和章节分析不失效。
- 已提交的章节 Delta 不自动重跑。
- 依赖故事结构的全书报告标记过期。
- 最终 JSON 和所有导出标记过期。

用户强制重跑时，即使任务输入版本未变化也创建新任务并覆盖规范目标产物；旧任务历史仍保留。

## 7. 新增章节处理

项目保留源书路径，并提供“检查更新”和“选择新版书籍”。

流程：

```text
读取新版书籍
→ 与旧索引做严格标题前缀比对
→ 返回新增章节预览
→ 用户执行追加
→ 仅写入新增章节文件和索引条目
→ 更新 chapter_revision
→ 自动排队新增章节生产链
```

旧章节标题不一致时拒绝追加，不自动覆盖旧章节和旧产物。只包含增量片段的输入作为显式高级选项，不根据内容猜测。

## 8. API 设计

主要接口：

```text
POST /projects/{id}/tasks
GET  /projects/{id}/tasks
GET  /projects/{id}/tasks/{task_id}
POST /projects/{id}/tasks/{task_id}/cancel
POST /projects/{id}/tasks/{task_id}/retry
GET  /projects/{id}/artifacts/status
POST /projects/{id}/chapters/append-preview
POST /projects/{id}/chapters/append
```

创建分析任务示例：

```json
{
  "type": "analysis",
  "params": {
    "task": "plotlines",
    "chapters": "1-120",
    "targets": ["角色:林婉"],
    "question": "",
    "force": false
  }
}
```

创建主动审计示例：

```json
{
  "type": "governance",
  "params": {
    "chapters": "101-150",
    "force": false,
    "auto_apply": true
  }
}
```

WebSocket 事件增加 `task_queued`、`task_started`、`task_progress`、`task_done`、`task_failed`、`task_cancelled`，均包含 `task_id` 和 `project_id`。

## 9. 错误处理与数据安全

- worker 失败时保留任务包、部分产物、日志和错误摘要。
- 治理 correction 校验失败时不得修改过程 JSON。
- 治理应用后全局校验失败时自动恢复 before 快照。
- 新增章节前始终先生成预览；标题前缀不匹配即阻止写入。
- 路径越界继续由后端安全路径检查拦截。
- 任务取消使用现有进程树终止能力，并将任务标记为 cancelled。
- 任务重试创建新的执行尝试，保留此前日志。
- UI 展示简短中文错误，并提供完整 worker 日志、校验报告和任务包入口。

## 10. Windows 中文编码

乱码根因是开发终端代码页 936 与应用 UTF-8 输出不一致。实现要求：

1. Windows `dev` 启动入口在启动 electron-vite 前将当前控制台切到 UTF-8 代码页。
2. Python Backend 设置 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。
3. Electron 对 Backend stdout/stderr 显式使用 UTF-8 解码。
4. worker 子进程和日志显式使用 UTF-8。
5. 测试覆盖中文消息、中文书名、中文目录和 worker 日志。

## 11. 测试与验收

### 11.1 后端

- 任务创建、列表、详情、取消、重试。
- 队列顺序、同项目写互斥和跨项目并行。
- 重启后 queued 保留、running 转 interrupted。
- 输入版本和 stale 判断。
- 追加预览、成功追加、无新增和标题冲突。
- 主动治理成功、correction 失败和应用后回滚。

### 11.2 引擎适配

- 多维分析参数正确传递。
- 分章视觉资产与全书汇总。
- 分章叙事结构与全书叙事结构。
- JSON 到大 Markdown、Vault、Dataview 和 Canvas。
- 强制重跑覆盖规范目标产物但保留任务历史。

### 11.3 桌面端

- 工作台卡片和任务状态渲染。
- 章节多选与批量动作。
- 主动治理范围选择和历史结果。
- 导出选项与产物入口。
- 任务事件实时刷新与重启后恢复。
- TypeScript 类型检查和生产构建。

### 11.4 编码

- UTF-8 Python 输出经 Backend、Electron 到 dev 终端保持中文。
- 中文路径和日志内容无 mojibake。

### 11.5 回归

- 现有 Backend 测试。
- 现有 Engine 测试。
- 现有章节自动生产链与取消/恢复行为。

验收完成条件：用户能够在自动流水线继续运行的同时创建独立工作台任务；任务可靠排队、可恢复、可重跑；新增章节只追加并正确使全书产物过期；主动审计校验通过后自动应用；分章和全书视觉/叙事结构均可生成；JSON 导出与中文日志正常工作。
