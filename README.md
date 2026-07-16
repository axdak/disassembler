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

## 后端 (M3a)

```
runtime\venv\Scripts\python.exe -m pytest backend\tests\
runtime\venv\Scripts\python.exe backend\main.py --port 0
```

FastAPI 后端封装 orchestrator，提供项目 CRUD / 启动运行 / 状态 / 事件 / 设置 REST + WebSocket
事件流，随机端口 + session token 鉴权。启动打印 `BACKEND_READY host=... port=... token=...`。

> 注：本机代理会拦截 127.0.0.1（返回 502）。客户端需 `NO_PROXY=127.0.0.1,localhost`
> （M3b 由 Electron 主进程设置）。

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
- [ ] 3. Electron 工作台、托盘、日志、预览、设置
  - [x] 3a. Python 后端（封装 orchestrator + SQLite + REST/WebSocket）
  - [x] 3b. Electron 外壳 + 最小 UI（项目列表 + 一键运行 + 实时事件流）
  - [ ] 3c. 完整工作台（托盘/时间线/章节状态/预览/日志/导出/设置/队列）
- [ ] 4. Windows 安装包、假 worker e2e、真实 Agy 冒烟
