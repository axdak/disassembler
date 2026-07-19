# 工作台缺口补全设计

日期：2026-07-19

## 1. 背景与目标

原始 v12 引擎提供的若干能力在桌面软件中尚未暴露或只能随自动流水线运行（见 `docs/superpowers/specs/2026-07-18-independent-workbench-design.md` 与功能覆盖对比）。本设计补齐这些缺口，使桌面端在保持自动流水线无人值守的同时，把维护与收尾类操作也暴露成可发现、可配置、可重跑的工作台任务。

目标：

1. 暴露 `recover_story`（快照恢复），让过程库损坏时可在应用内回滚到 `story_after_chNNN` 快照并重放后续 Delta。
2. 暴露 `migrate_chronology`（时间线迁移）：章节时间回填、回溯字段压缩、事件重排。
3. 把收尾四步（final-pack → commit-final-draft → validate-final → finalize）合并为单个可按需触发的 `finalize` 任务。
4. 暴露按需 `normalize`（全书故事结构归一化）与 `repair`（修复损坏的章节 Delta JSON）。
5. 明确 `custom` 分析维度不进自动流水线（需用户问题，属设计约束），在文档中说明。

非目标：

- 不重写现有提示词、Schema、确定性合并或校验逻辑。
- 不改变 `current_stage` 语义或自动流水线状态机。
- 不把 Markdown/Obsidian 变成可写数据源；JSON 始终权威。
- 不引入新的状态管理或 UI 组件库。

## 2. 方案选择

采用「统一工作台任务 + 维护标签页」。所有缺口都做成新的 `TaskAdapter` 写任务，复用 `WorkbenchTaskManager` 的 writer admission、事件、取消/重试、重启恢复与任务历史；UI 上新增第 7 个标签页「维护」集中入口。

未采用的方案：

- 维护类做直接 REST 动作、生产类做任务：分裂了互斥/事件/恢复语义。
- 全做直接 REST 动作：失去写互斥、事件、重试与重启恢复。
- 收尾拆 4 个独立任务：步骤间有顺序依赖、入口多、易误用。
- `custom` 直接加入 `FULLBOOK_TASKS`：无人值守无问题可填，会跑出空产物。

## 3. 任务模型

新增 5 个工作台任务类型，均为写任务（进 `WRITER_TASK_TYPES`）：

| type | 引擎复用 | prepare | 说明 |
|---|---|---|---|
| `recover_story` | `recover_story_from_snapshot` / `latest_snapshot_seq` / `max_available_delta_seq` / `replay_one_delta`（`engine/scripts/run_pipeline.py`） | 否（预览走独立 GET 端点） | 预览/应用两步，仿 append |
| `migrate_chronology` | `migrate_chronology` 迁移函数（`engine/scripts/migrate_chronology.py` + `run_pipeline.py` 的 `cmd_migrate_chronology`） | 否 | 时间线回填/压缩/重排 |
| `finalize` | `final_pack` → worker 改稿 → `commit_final_draft` → `validate_final` → `finalize`（`run_pipeline.py` 的 `cmd_final_pack`/`cmd_commit_final_draft`/`cmd_validate_final`/`cmd_finalize`） | 否 | 复合收尾，worker-backed |
| `normalize` | `normalize_story_schema`（`engine/scripts/normalize_story_schema.py`） | 否 | 全书故事 JSON 保格式归一化 |
| `repair` | `repair_llm_json(path, report_path, backup=False)`（`run_pipeline.py`） | 否 | 修损坏的章节 Delta，可选章节范围 |

适配器调用现有 Python 函数，不复制提示词、Schema、合并或校验逻辑。

### 3.1 recover_story（预览/应用两步）

- 预览（独立只读端点 `GET /projects/{id}/recover/snapshots`，不经任务队列）：列出项目目录下所有 `story_after_chNNN` 快照、各自覆盖到的 seq、可重放到的最大 seq（`max_available_delta_seq`），并标注"应用将丢弃所选快照之后的所有过程库改动"。
- 应用（writer 任务 `run`）：用户选定 `snapshot_seq` 后创建 `recover_story` 任务，`run` 在 writer admission 下调用 `recover_story_from_snapshot(project_dir, snapshot_seq)`；记录 before 修订；完成后返回 before/after 修订哈希与重放的 seq 区间。
- 失败回滚：应用后若全局校验失败，用 before 快照回滚（复用 governance 的 before/restore 模式）。

### 3.2 finalize（复合，worker-backed）

`run` 内按序：`cmd_final_pack`（生成最终草稿任务包）→ `run_worker`（worker 改稿）→ `cmd_commit_final_draft`（normalize + strict validate + 写草稿）→ `cmd_validate_final`（独立终校）→ `cmd_finalize`（写 `故事结构.json`）。worker 调用复用现有 `run_worker`，与 governance 在 writer admission 下跑 worker 的模式一致。`validate_final` 失败时不写 `故事结构.json`，保留草稿与校验报告。

### 3.3 normalize / repair

- `normalize`：对全书 `故事结构_增量.json` 做保格式归一化；返回 before/after 哈希。
- `repair`：扫描章节 Delta，修复语法损坏的（`repair_llm_json`），跳过正常的；可选章节范围参数；返回修复了哪些 Delta + 报告路径。`repair` 只修 Delta 语法、不改故事结构，不触发下游 stale。

## 4. 产品信息架构

标签序列变为：

```text
概览 | 章节 | 分析工作台 | 质量治理 | 导出 | 日志 | 维护
```

维护页内 5 个区块：

- **快照恢复**：先调预览列出快照（含警示），用户选快照后应用。唯一的预览/应用两步流程。
- **时间线迁移**：一个"执行迁移"按钮 → `migrate_chronology`；展示上次结果与受影响章节。
- **重新收尾/定稿**：一个"重新收尾"按钮 → `finalize`；展示 before/after 修订哈希 + 校验报告入口。
- **归一化**：一个"归一化故事结构"按钮 → `normalize`；展示 before/after 哈希。
- **修复 Delta**：章节范围选择（默认全部损坏的）→ `repair`；列出修复的 Delta + 报告入口。

复用现有 `TaskStatus` 徽章、`useProjectTasks` 事件刷新（仅当前项目）、`ArtifactDrawer` 看报告/日志。中文错误，附日志/产物链接。

## 5. 并发与数据安全

1. 5 项均进 `WRITER_TASK_TYPES`，与自动流水线/governance/append 共享项目级写互斥，不并发改过程或最终 JSON。
2. recover-story 应用前记录 before 修订并强警示；应用后校验失败用 before 回滚。
3. finalize 的 validate-final 失败不写 `故事结构.json`。
4. recover/migrate/normalize/finalize 改过程或最终 JSON 后刷新 `project_revisions`，按 §6 失效映射标记相关全书产物过期。`repair` 不改故事结构，不触发 stale。
5. 不动 `current_stage` 语义与自动流水线状态机。

## 6. 输入版本与失效规则（沿用既有映射）

- `recover_story` / `migrate_chronology` / `normalize` 改过程 JSON → 全书分析报告 + 全书视觉汇总 + 全书叙事结构 + 最终 JSON + 导出均 stale；章节分析与原文不失效。
- `finalize` 写 `故事结构.json` → 最终 JSON 与所有导出 stale。
- `repair` 修 Delta 语法 → 不失效下游（不改故事结构语义）。

## 7. API 设计

复用既有 `POST /projects/{id}/tasks`，新增 type 取值：`recover_story`、`migrate_chronology`、`finalize`、`normalize`、`repair`。`recover_story` 走预览/应用：

```text
GET  /projects/{id}/recover/snapshots        # 列出可用快照（只读预览）
POST /projects/{id}/tasks                    # type=recover_story, params={snapshot_seq}
```

创建示例：

```json
{ "type": "finalize", "params": {} }
{ "type": "normalize", "params": {} }
{ "type": "repair", "params": { "chapters": "all" } }
{ "type": "migrate_chronology", "params": {} }
{ "type": "recover_story", "params": { "snapshot_seq": 120 } }
```

WebSocket 事件复用既有 `task_*`。

## 8. custom 维度说明

`custom` 分析维度需用户自定义问题，无人值守的自动流水线无问题可填，故**故意不进 `FULLBOOK_TASKS`**，仅在工作台「分析工作台」标签页手动触发。本设计不为 `custom` 增加代码，仅在 README 与本设计文档中明确该约束。

## 9. 测试与验收

### 9.1 后端
- 每个新适配器：参数转发、产物、失败路径。
- recover_story：预览只读、应用写盘、快照缺失/无 Delta 可重放、应用后校验失败回滚。
- finalize：worker-backed 全链、validate-final 失败不写最终 JSON。
- normalize：before/after 哈希。
- repair：修坏 Delta、跳过正常 Delta、可选章节范围。
- stale 联动：recover/migrate/normalize 后全书产物 stale；finalize 后导出 stale；repair 不 stale。

### 9.2 桌面端
- 维护标签页渲染、5 个入口、recover 预览/应用两步、其余直接提交。
- 任务事件仅刷新当前项目。
- TypeScript 类型检查与生产构建。

### 9.3 回归
- 现有 backend/engine/desktop 全套不破。

验收完成条件：用户能在维护页按需触发快照恢复、时间线迁移、重新收尾、归一化与 Delta 修复；recover-story 提供应用内过程库恢复路径；custom 不进自动流水线且有文档说明。
