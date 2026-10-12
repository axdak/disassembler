# 独立桌面项目设计（复制引入·可修改版）

## 本次变更要点

相对初版「只读外部引擎」方案，本版改为将引擎源码**完整复制进新项目并作为自有代码自由修改**：

- 引擎源码（仅源码与资源）从 `novel-disassembler-v12\novel-disassembler`（基线 commit `dba0213`，2026-07-04）一次性复制进新项目的 `engine\` 目录，此后新项目自有并可直接修改、扩展。
- 原引擎仓库仅作为上游基线，复制后运行时不再访问；新项目不向原仓库写入任何内容。
- 删除「引擎路径」设置与只读 Engine Adapter：后端直接 import 引擎模块，无需 CLI 翻译层。
- 全书分析与最终整理的缺口**在 `engine\` 副本内补齐**为一等公民命令，不再靠外部编排逆向拼装协议。
- 结构化事件**在引擎内直接发出**（进程内回调 / 事件总线），不再从事实文件逆向推断；事实文件仍作为崩溃恢复的真实状态来源。
- 仍保留隔离的私有 Python Runtime，运行的是内置引擎副本。
- 验收标准中「测试前后原引擎 git status 一致」改为「原引擎仓库仅在复制时读取一次，之后不再被运行时访问或写入」。

## 总体边界

新建独立 Git 项目：

`C:\Users\53519\Downloads\novel-disassembler-desktop`

引擎源码来源：

`C:\Users\53519\Downloads\novel-disassembler-v12\novel-disassembler`（基线 commit `dba0213`，2026-07-04）

复制策略：

- 初始基线一次性复制**仅源码与资源**：`scripts\`、`tools\`、`schemas\`、`prompts\`、`references\`、`docs\`、`tests\`、`SKILL.md`、`requirements.txt`。后续重构已删除 `SKILL.md` 与 `references\`，仍需使用的数据统一迁移至 `schemas\`。
- 排除：`.git\`、`.pytest_cache\`、`__pycache__\`、`novel_disassembler_release.zip`、`故事结构_增量-*.json`、`archive\`、`.agents\`、`.codex\`、`.claude\`、`.superpowers\` 等非源码产物与缓存。
- 复制进新项目 `engine\` 目录，纳入新项目 Git，作为自有代码。
- 记录上游基线 commit `dba0213` 于 `engine\UPSTREAM_BASELINE`，便于日后人工对照；不再自动同步上游。

新项目可以：

- 修改、扩展、重构 `engine\` 内的代码，补齐全流程自动化与结构化事件。
- 在 `engine\` 内新增模块、命令、Schema、模板。

新项目不得：

- 向原引擎仓库 `novel-disassembler-v12\novel-disassembler` 写入或修改任何内容（复制后不再访问）。
- 在运行时依赖原引擎仓库路径；所有运行只使用内置 `engine\` 副本与用户选择的 `拆书_*` 目录。

启动时校验**内置引擎副本**完整性：`engine\scripts\run_pipeline.py`、`engine\tools\agent_worker.py`、必要 schemas/prompts 资源存在且可被私有 Runtime 导入；不兼容时提示用户，不自动修补。

## 独立软件架构

```text
novel-disassembler-desktop
├── Electron Renderer        # React + TypeScript 工作台
├── Electron Main            # 窗口/托盘/通知/单实例锁/后端生命周期
├── Python Backend           # localhost REST/WebSocket，随机端口 + 会话令牌
│   ├── Orchestrator         # 编排引擎调用、监督 worker 子进程、状态机、恢复
│   └── 直接 import engine\  # 进程内调用引擎模块（校验、读状态、归约、事件）
├── engine\                  # 自有引擎副本（基线 dba0213），可自由修改扩展
│   └── 全流程自动化与结构化事件在此补齐
├── SQLite / Logs
└── Private Python Runtime   # 隔离 Python + jinja2 + json-repair，运行内置 engine\
```

- Electron + React + TypeScript 实现 Windows 工作台。
- Electron Main 管理窗口、托盘、通知、单实例锁和 Python 后端生命周期。
- Python Backend 提供 localhost REST/WebSocket，随机端口和会话令牌；**直接 import `engine\` 模块**进行编排、校验、状态读取与归约，不再经过 CLI 翻译层。
- 现有 CLI 入口在 `engine\` 内重构为可 import 的函数（与 `__main__` argparse 分离），供后端直接调用。
- Orchestrator 在后端进程内编排全流程；长时运行的 worker（Agy continuous）作为私有 Runtime 的受监督子进程执行，捕获 stdout 与退出状态，保证崩溃隔离。
- 全流程自动化（全书分析、最终整理）与结构化事件**在 `engine\` 副本内实现**为一等公民；后端通过进程内回调/事件总线接收实时事件，经 WebSocket 推送 Renderer。
- 项目业务产物写入用户选择的 `拆书_*` 目录；`engine\` 源码目录本身在运行时只读（不被业务产物污染）。
- 软件附带隔离 Python Runtime 与所需依赖，不要求系统安装 Python。
- 调用 worker 时设置 `PYTHONDONTWRITEBYTECODE=1`，缓存统一重定向到软件自己的 AppData。

## 自动化方式

章节主流程直接调用引擎入口（后端进程序列化调用，worker 部分以子进程执行）：

```text
run --phase analysis --run-mode worker --worker-provider agy --worker-loop continuous
run --phase delta --run-mode worker --worker-provider agy --worker-loop continuous
chapter-structure-auto ... --run-mode worker --worker-provider agy --worker-loop continuous
visual_assets-auto ... --run-mode worker --worker-provider agy --worker-loop continuous
```

原引擎缺失的「统一自动执行的全书分析与最终整理」，**在 `engine\` 副本内新增模块实现**（不再外部逆向编排）：

- 新增引擎命令 `analysis-pack` 生成 manifest 和 pack。
- 读取 manifest，为每个 pack 创建规范要求的 `partial_result_NNN.md`。
- 通过 `agent_worker.py` 和 `ND_*` 环境变量调用 Agy（子进程）。
- 将 `reduce_prompt.md`、manifest 和 partial results 组成归约任务，验收 manifest 指定的目标文件。
- `custom` 分析由用户填写问题和输出名称后执行，不进入默认全流程。
- 新增引擎命令完成最终阶段：调用 `final-pack`，让 worker 修改 `故事结构_草稿.json`，调用 `commit-final-draft`；失败时读取 `repair_final.md` 重试，通过后调用 `finalize`。
- 导出阶段直接调用引擎 `export_obsidian_vault.py`。

结构化事件**在引擎内直接发出**：在上述模块的关键节点（pack 生成、partial 完成、归约验收、最终校验、导出）发射事件，后端进程内订阅并转发 WebSocket。同时保留以下事实文件作为崩溃恢复的真实状态来源：

- `进度.json`、`_索引.json`
- 章节分析、Delta、校验报告和快照
- 周期审计 status JSON
- 全书分析 manifest 和目标文件
- 最终校验报告、finalize 日志和导出报告
- worker 日志与进程退出状态

## 产品功能与恢复策略

工作台包含项目库、新建/接管项目、单任务队列、流水线时间线、章节状态、分析结果、只读预览、日志、导出、设置和系统托盘。

默认行为：

- 默认 Agy，用户可手动切换 CodeBuddy；不自动切换。
- 所有 worker 阶段使用 continuous，无需人工重复运行命令。
- 每次 worker 最多 3 次尝试，校验修复最多 3 轮。
- 同一缺失产物连续 3 次且状态、时间戳、文件大小均无变化时判定硬阻塞。
- Agy 未登录、磁盘不可写、源文件损坏或严格校验持续失败时暂停当前项目并通知。
- 当前项目阻塞后，单任务队列继续下一项目。
- 关闭窗口缩到托盘继续；安全暂停等待当前原子任务结束。
- 崩溃重启后通过引擎 `resume`、状态文件和实际产物恢复，SQLite 百分比不作为完成依据。
- 取消不删除已生成产物，再次运行时复用通过校验的结果。

## 数据、打包与验收

软件自己的数据写入：

- `%APPDATA%\NovelDisassemblerDesktop\app.db`
- `%LOCALAPPDATA%\NovelDisassemblerDesktop\logs`
- `%LOCALAPPDATA%\NovelDisassemblerDesktop\cache`
- `%LOCALAPPDATA%\NovelDisassemblerDesktop\runs`

SQLite 保存项目、队列、运行、阶段、事件和设置，不复制故事数据。Renderer 禁用 Node 集成，只能通过类型化 preload IPC 访问白名单接口；worker 以参数数组启动私有 Runtime 子进程，不接受界面传入的任意 shell 命令。

实施分为四个独立里程碑：

1. 创建独立项目骨架、私有 Python Runtime、复制引擎源码（仅源码与资源）进 `engine\`、校验内置引擎可导入运行。
2. 在 `engine\` 内补齐全功能编排（全书分析、manifest 归约、最终整理）、状态机、崩溃恢复与结构化事件。
3. 实现 Electron 工作台、托盘、日志、结果预览和设置。
4. 完成 Windows 安装包、假 worker 端到端测试和真实 Agy 冒烟测试。

验收必须证明：

- 新项目可以接管现有拆书项目或创建新项目。
- 一键完成章节分析、Delta、治理、全书分析、最终整理和导出。
- 无需人工处理 continuous 检查点。
- 中断后能够从真实产物恢复。
- 中文、空格路径及 TXT/DOCX 正常工作。
- 原引擎仓库 `novel-disassembler-v12\novel-disassembler` 仅在复制时读取一次（基线 `dba0213`），之后运行时不再访问或写入；新项目所有运行只读写自己的 `engine\` 副本与用户 `拆书_*` 目录。
