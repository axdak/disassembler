# 自主循环执行纪律与进度汇报

## 自主循环执行纪律

用户要求持续拆书时，主控Agent必须把 `run_pipeline.py run` 视为可重复调用的主控器，而不是把返回 `2` 当作暂停点。推荐直接使用 `prompts/autonomous_run.j2` 作为启动提示词。

三种受控路由入口：
```bash
run_pipeline.py run <项目目录> --run-mode subagent
run_pipeline.py run <项目目录> --run-mode serial
run_pipeline.py run <项目目录> --run-mode worker --worker-provider auto
```

`subagent` 表示主Agent派发子Agent完成任务包；`serial` 表示主Agent在返回 `2` 后亲自完成当前任务包并立刻重跑；`worker` 表示由外部 CLI worker 完成任务包。worker provider 的 `auto` 默认优先调用 Agy；也可将 `auto` 替换为 `agy` 或 `codebuddy`。如果用户显式配置外部 worker 命令，也可直接运行：
```text
run_pipeline.py run <项目目录> --run-mode worker --chapter-command "<章节worker命令>" --audit-command "<审计worker命令>"
```

此时 `run_pipeline.py` 会在生成任务包后调用外部 worker，并继续执行校验、合并、快照和周期治理。worker 只负责写当前任务包指定的目标文件；主控Agent只负责启动主控器、看总进度、查看 worker 日志、重试或报告硬阻塞。

worker 模式仍然是前台监督流程。默认 `--worker-loop supervised` 会在每次 worker 写出任务包产物或章节提交检查点后返回前台；主控Agent必须持续观察 run_pipeline 输出、worker 日志、状态文件和返回码，并重跑同一路由命令。不能启动后台 worker 后结束对话；不得把后台任务 ID 当作完成状态；不得等待 task-notification；不得使用后台任务托管主流程；必须等待前台命令返回码。窗口已打开或命令已启动只表示 worker 被调度，不表示章节、Delta 或审计已经可信完成。只有用户明确要求无人值守连续跑到底时，才使用 `--worker-loop continuous`。

```text
循环运行 run_pipeline.py run <项目目录> --run-mode subagent|serial|worker
  -> 返回 0：本轮可处理状态已经完成；如仍有后续流程，继续同一条命令
  -> 返回 2：subagent 路由派发子Agent；serial 路由由主Agent产出后重跑；worker 路由可能是 supervised 检查点，也可能是 worker 交接/配置/期望输出问题，主控Agent必须检查 worker 日志、状态文件和当前产物后重跑同一路由
  -> 返回 1：读取校验报告、修复任务包、worker 日志和相关产物，优先通过同一 worker 入口修复后重试
```

需要“先全书章节分析 MD、再结构 JSON”时，循环命令可拆为：
```text
先循环运行 run_pipeline.py run <项目目录> --phase analysis --run-mode worker --worker-provider auto
  -> worker 每次只完成章节分析或分析重生成任务
  -> 返回 0 后，说明所有章节分析 MD 均通过现有校验

再循环运行 run_pipeline.py run <项目目录> --phase delta --run-mode worker --worker-provider auto
  -> 先确认全部 MD 合格，再按串行规则生成 Delta、提交、快照和周期治理
```

`subagent` 和 `serial` 路由返回 `2` 时表示当前任务包需要 agent 产出；`worker` 路由返回 `2` 通常表示 worker 未能完成交接、缺少配置、期望产物未写出或进入修复交接；主控Agent不得绕过当前路由规则继续流程。

## 语义生成与机械修复边界

章节分析MD和Delta JSON的语义内容必须由模型阅读当前章节材料后生成。主控Agent不得为了推进章节数、避免手写JSON、减少Token或提高吞吐，编写脚本批量生成或补全角色、事件、地点、线索、阵营、物品、其他事项等语义内容。

允许的脚本化操作仅限 format-only 机械修复：对已经存在的当前章节 Delta JSON 进行 JSON 语法、字符串转义、字段类型、章节时间、标签压缩等确定性修复。优先使用项目已有脚本（如 `repair_llm_json.py`、`coerce_delta.py`、`compress_tags.py`）。必要的一次性临时脚本不得读取原文生成内容，不得新增剧情事实，不得复用到多章生成。

每 5 章触发一次周期审计时，未配置 `--worker-provider` 或 `--audit-command` 的 `run` 会返回 `2` 并把审计状态记为 `awaiting_agent`。这不是失败；主控Agent必须完成真实审计。若父周期任务包超过上下文预算，管线会自动拆成连续子审计区间；主控Agent按当前返回的子任务包产出真实 correction，全部子区间通过后父周期自动标记为 `covered_by_children`。配置 `--worker-provider` 或 `--audit-command` 后，脚本会把审计包交给外部 worker，并用 `commit-governance` 验收 correction。两种模式都禁止用空 Delta、伪造脚本、跳过校验或跳过审计来继续流程。只有源文件缺失、权限不足、单章证据仍超上下文或必要工具不可用等硬阻塞，才可以停止并报告。

## 进度汇报规范

每完成 1 章或一次周期审计后，主控Agent应向用户汇报：
```text
进度更新
- 当前章节：{chNNN}
- 当前状态：passed_schema | failed | worker_retry | blocked
- 最近产物：{analysis/delta/correction 路径}
- 质量状态：章节分析校验{通过/失败} | Delta校验{通过/失败} | 最近审计{通过/待修正/未触发}
- 阻塞：{无/具体硬阻塞}
```

## 外部 worker 模式（默认关闭）

外部 worker 模式由 `--run-mode worker` 搭配 `--worker-provider` 或 `--chapter-command` / `--audit-command` 显式开启。脚本负责生成任务包、调用 worker、验收目标文件、校验、回滚和重试；worker 只负责自然语言理解与结构提取。默认 `--worker-loop supervised`，不会在一条命令里连续消费完整全流程；未显式选择 worker 前，`run_pipeline.py run` 仍只生成任务包并等待 subagent/serial/人工产出。
