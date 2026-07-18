# Novel Disassembler Desktop

独立 Windows 工作台，封装并扩展 novel-disassembler 引擎，实现一键拆书全流程
（章节分析、Delta、治理、全书分析、最终整理、导出）。

设计文档见 [DESIGN.md](DESIGN.md)。

## 引擎

`engine\` 是从上游 `novel-disassembler-v12\novel-disassembler`（基线 `dba0213`）
一次性复制的源码副本，为本项目自有代码，可自由修改扩展。基线记录见
`engine\UPSTREAM_BASELINE`。上游不再自动同步。

## 运行时

`runtime\venv\` 是隔离的 Python 虚拟环境（jinja2、json-repair）。开发期用于校验
与运行引擎；正式打包（里程碑4）将替换为随包分发的 embeddable Python。

## 校验

```
runtime\venv\Scripts\python.exe validate_engine.py
```

校验 `engine\` 目录结构、第三方依赖、关键模块 import 与全部 `.py` 语法。

## 开发命令

```powershell
# 后端测试（含工作台集成测试）
runtime\venv\Scripts\python.exe -m pytest backend\tests -q

# 引擎测试（在 engine/ 下运行）
cd engine; ..\runtime\venv\Scripts\python.exe -m pytest tests -q; cd ..

# 桌面端测试 / 类型检查 / 构建（在 desktop/ 下运行）
cd desktop
npm test -- --run
npm run typecheck:node
npm run typecheck:web
npm run build
cd ..

# 引擎校验
runtime\venv\Scripts\python.exe validate_engine.py
```

## Windows UTF-8 启动

桌面端 `npm run dev` 在 Windows 上先执行 `chcp 65001` 切换控制台代码页为 UTF-8，再启动
`electron-vite dev`；后端子进程的 spawn env 设置 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`，
主进程用 `StringDecoder('utf8')` 拼接跨 Buffer 边界的多字节字符，闭环避免中文 stdout/stderr
变成乱码（U+FFFD）。非 Windows 或 CI 直接用 `npm run dev:direct`（即 `electron-vite dev`），
`build` / `typecheck:*` 脚本不受影响。

## 后端 (M3a)

```
runtime\venv\Scripts\python.exe -m pytest backend\tests\
runtime\venv\Scripts\python.exe backend\main.py --port 0
```

FastAPI 后端封装 orchestrator，提供项目 CRUD / 启动运行 / 状态 / 事件 / 设置 REST + WebSocket
事件流，随机端口 + session token 鉴权。启动打印 `BACKEND_READY host=... port=... token=...`。

> 注：本机代理会拦截 127.0.0.1（返回 502）。客户端需 `NO_PROXY=127.0.0.1,localhost`
> （M3b 由 Electron 主进程设置）。

## 独立工作台（Workbench）

在自动流水线（`/run`、`/queue`）之外，后端提供一套持久化的独立任务工作台。任务由
SQLite 存储，调度线程按项目 FIFO 排队，写者任务（治理 / 章节重跑 / 增量追加）通过
`WriterCoordinator` 与自动流水线互斥共享同一项目的写权限；读者任务（全书分析 / 视觉 /
结构 / 导出）可在写者空闲时并发。任务事件经 `BroadcastHub` 推送到 WebSocket。

### 六个标签页（desktop 桌面端）

- **总览**：项目状态、当前阶段、自动流水线一键运行/取消、任务与陈旧计数摘要。
- **章节**：章节卡片（分析 / Delta / 视觉 / 结构四维状态 + 陈旧标记 + 最近任务 + 错误），
  支持多选与批量重跑。
- **分析工作台**：全书分析维度卡片，配置范围（全部 / 区间 / 离散）、目标、问题、原文策略、
  强制重跑，创建 `analysis` 读者任务。
- **治理**：章节区间与历史，创建 `governance` 写者任务（审计包 → 修正 → 提交，结构校验失败
  自动回滚 before 快照）。
- **导出**：选择 process / final 源、单文件 / Dataview / Canvas / 清空 / 文件名模式，
  创建 `export` 读者任务，产出 Obsidian vault。
- **日志**：worker 日志文件列表与内容预览；任务/产物抽屉按需打开。

### 任务生命周期

`queued` → `running` → `done` / `failed` / `cancelled` / `interrupted`。重跑（retry）创建新行
并经 `retry_of` 链接旧行，旧行保留。进程重启时残留的 `running` 任务被标记为 `interrupted`，
`queued` 任务在重启后继续调度。事件类型：`task_queued`、`task_started`、`task_progress`、
`task_done`、`task_failed`、`task_cancelled`，每条携带 `task_id` 与 `project_id`。

### 增量追加安全

`POST /projects/{id}/chapters/append-preview` 只读预览（切分新原文、比对旧索引、返回 AppendPlan，
  不写盘）；`POST /projects/{id}/chapters/apply` 将 plan 排入 `append_chapters` 写者任务。apply
  拒绝带前缀冲突的 plan，且只写新增章节 md + 原子更新索引（永不覆盖旧章节）。apply 完成后为
  新 seq 排入 `chapter_analysis_rerun` + `chapter_delta_rerun`，全书分析产物因章节 revision 变化
  而变陈旧。

### JSON 权威性

`故事结构_增量.json` / `故事结构.json` 是权威数据源。Markdown 与 Obsidian vault 产物均为只读
镜像，工作台任务从不把它们当输入写回；任务历史与 revision hash 仅描述执行与陈旧状态，不替代
产物校验。

## 目录

```
novel-disassembler-desktop
├── DESIGN.md          # 设计文档
├── engine\            # 自有引擎副本（基线 dba0213）
├── runtime\venv\      # 私有 Python Runtime（开发期 venv）
├── backend\           # Python 后端（里程碑2+）
├── desktop\           # Electron + React（里程碑3+）
├── data\              # 开发期数据（生产用 %APPDATA%/%LOCALAPPDATA%）
└── validate_engine.py
```

## 里程碑

- [x] 1. 项目骨架、私有 Runtime、复制引擎、校验可导入
- [x] 2. 全功能编排、状态机、崩溃恢复、结构化事件
- [x] 3. Electron 工作台、托盘、日志、预览、设置
  - [x] 3a. Python 后端（封装 orchestrator + SQLite + REST/WebSocket）
  - [x] 3b. Electron 外壳 + 最小 UI（项目列表 + 一键运行 + 实时事件流）
  - [x] 3c. 完整工作台（托盘/时间线/章节状态/预览/日志/导出/设置/队列）
- [ ] 4. Windows 安装包、假 worker e2e、真实 Agy 冒烟
