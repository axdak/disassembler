# 故事家运行时与受控 MCP 借鉴评估

> 评估对象：`C:\Users\53519\Downloads\故事家本地可控版`
> 评估范围：`resources/agent-runtime/src`（Node 协调层 + Story MCP）、`resources/pydantic-runtime`（Python 备用运行时）
> 对照方：`novel-disassembler-desktop` 现有 Worker 执行链与提交链
> 结论日期：2026-10-12

---

## 一、结论

**可以借鉴，而且部分机制拆书其实已经抄了一半；但只应借鉴"观测与恢复"层，不应借鉴"模型直写故事"的写入协议。**

判断依据有三点：

1. 两边共用同一份数据格式（七类集合 + 名称/别名/介绍/详情），故事家已实现针对该格式的导入转换，拆书的最终 JSON 就是它的输入。
2. 故事家真正的护城河不在"接了 MCP"，而在 **模型之外的控制层**：能力白名单、调用事件流、幂等账本、版本门禁、无进展超时。
3. 拆书在**写入安全上比故事家更保守**（模型只出草稿，程序校验后落盘），不应倒退去抄它的直写协议。

---

## 二、故事家运行时值得借鉴的四个机制

| 机制 | 实现位置 | 干什么 | 拆书现状 |
|---|---|---|---|
| **无进展超时 + 有限重试** | `agent-runtime/src/resilience/operation-recovery.js` | 每次尝试独立 `AbortSignal`；总时限与空闲时限分离，`progress()` 刷新空闲期限；指数退避上限 2s；`shouldRetry(error)` 决定是否重试 | 只有整次 `--print-timeout`（默认 30m），无空闲超时 |
| **错误分级** | `agent-runtime/src/resilience/error-classifier.js` | 把基础设施错误归一为 `provider_429` / `network_timeout` / `provider_unavailable` / `recovery_failure`；**领域失败与用户取消返回 `null`**，不进重试 | 无。业务失败与基础设施失败混在一起 |
| **幂等账本 + unknown 对账** | `agent-runtime/src/ledger/operation-ledger.js` | 严格状态机 `planned→started→终态`；只有白名单错误码可判定 `failed`，其余（含超时）一律 `unknown` 等对账；`requestDigest` 保证同一幂等键不被不同载荷复用；同进程并发合并 | 无 operation 级幂等，只有章节完成标记 |
| **版本门禁** | `repository/write-request-digest.js`、`mcp/story-tools.js:209` | 提交必须携带 `expected_story_revision`；版本不符即拒，防长时间任务覆盖新内容 | `story_snapshot()` 已返回 sha256 revision，但提交侧**不校验** |

补充两个次优先机制：

- **读取范围门禁**（`context/story-context-policy.js`）：`off` / `selected` / `adaptive` 三档。`selected` 模式下只放行精确 ID 读取，`story_search`、`story_list_units`、`story_read_sections`、`story_query_chapters` 全部拒绝——模型无法自行扩大范围。拆书目前只到"文件清单"级。
- **双预算**（`context/token-budget.js`）：`hardLimit` 防溢出，`softLimit` 防大窗口拖慢体感。拆书有上下文预算但未分双层。
- **统一事件流**（`pydantic-runtime/story_runtime/runtime.py`）：`tool_started` / `tool_completed` / `usage_updated` / `turn_completed` 逐行 JSONL；取消后 1 秒内结束。这是"界面能解释为什么卡住"的前提。

---

## 三、现状核对：拆书已经有什么

**重要发现：拆书已经有一个只读 MCP，且已实现故事家的读取接口子集。**

`engine/tools/worker_context.py`（`--story-tools` 开关）：

| 拆书已有工具 | 对应故事家工具 |
|---|---|
| `story_snapshot`（含 sha256 revision、简介、各类计数） | `story_get_snapshot` |
| `story_search`（分页，名称/别名/介绍/标签/分组） | `story_search` |
| `story_get_units`（`{collection, name}` + L0/L1/L2） | `story_get_units` |
| `story_query_chapters`（index/search/read） | `story_query_chapters` |
| `list_context` / `read_context`（冻结材料清单） | 无对应（拆书特有） |

接入点：`engine/tools/agent_worker.py:123`（仅 `BROAD_READ_TASKS` 启用）、`:381-383`。

材料冻结逻辑（`snapshot_context`）已经做到：拒绝源码目录、拒绝隐藏文件、拒绝目录穿越、只读私有快照副本。**这一块不需要再借鉴故事家。**

因此差异只剩四项：**空闲超时、错误分级、幂等回执、版本门禁**。

---

## 四、可直接借鉴（低改造成本，不动 Worker 架构）

| # | 借鉴项 | 具体做法 | 解决的现有痛点 |
|---|---|---|---|
| A1 | 空闲超时 | 在现有整次超时之外增加"无进展超时"：worker 事件流一段时间无新增即判定卡死并回收到本章断点 | 周期审计两次各 20 分钟空等 |
| A2 | 错误分级 | 定义 `DETERMINISTIC`（程序可修，不消耗模型预算）/ `TRANSIENT`（可退避重试）/ `SEMANTIC`（才允许模型修复）/ `FATAL`（精确停机） | 第 11 章分类错误消耗 3 次修复 + 1 次诊断后停机 |
| A3 | 提交回执 + 幂等 | 每次提交带 `operation_id` + 内容摘要；超时后先查回执（未生成 / 已生成未合并 / 已合并），再决定是否重跑 | 重启后重复执行、重复合并 |
| A4 | 版本门禁 | `story_snapshot()` 的 sha256 revision 在提交时比对；不符则拒收并要求重出任务材料 | 长跑 worker 的结果覆盖新结构 |
| A5 | 读取范围精确化 | 在现有文件清单之上，为治理类任务附加"元素级白名单"，未列入的元素不可搜索、不可枚举 | 治理任务读取范围过宽 |

A2 与 A3 是投入产出比最高的两项：它们直接对应"不流畅"的两个根因（错误走了错误的处理通道、失败后无法安全续跑）。

---

## 五、需要引入运行时才能拿到的（中成本，建议后置）

| # | 项 | 说明 |
|---|---|---|
| B1 | 统一事件流 | 用 Pydantic AI + MCPToolset 起一个 Python 运行时（`pydantic-ai-harness==0.28.0`、`pydantic-ai-slim[mcp]==2.37.0`、`openai==3.0.0`，要求 Python ≥3.12,<3.13），把工具调用、用量、轮次归一为 JSONL，桌面端可直接显示"正在读取 / 正在生成 / 等待服务恢复" |
| B2 | 与 CodeBuddy 并存 | 作为 worker 的**新增 provider** 接入，而非替换。CodeBuddy 保留路径上下文能力，Pydantic 运行时用于需要精确轮次控制的批处理任务 |

B1 的收益是"可见性"而非"安全性"——A1–A5 已经把安全性补上。因此 B 类可以等 A 类稳定后再做。

注意：故事家 Pydantic 运行时的 `runtime.py` 本身**没有设置请求次数上限**，只做轮末滑动窗口压缩（`keep_messages=40`）。如果拆书要引入，必须自己补调用预算，不能假定框架会兜住。

---

## 六、不建议借鉴

| # | 不借鉴项 | 理由 |
|---|---|---|
| C1 | `story_apply_changes` 直写协议 | 故事家让模型按稳定 ID 直接增删改内存文档；拆书的 `merge_delta.py` 有章节序、增量字段保留、治理操作、引用清理等语义，套用会形成两套修改语义 |
| C2 | 完整 `agent-coordinator.js`（2191 行） | 为交互式故事编辑设计，含多智能体协作、提案账本、会话续跑；拆书是批处理流水线，绝大多数分支用不上 |
| C3 | 会话级 `SessionStore` / SQLite 账本 | 拆书已有章节断点与候选副本，引入第二套持久化会新增不一致面 |

---

## 七、接口对齐：拆书 JSON 交给故事家的真实缺口

拆书最终产物结构（实测 `故事结构_增量.json`）：

```
顶层：介绍（标题/描述）+ 角色集/事件集/地点集/线索集/阵营集/物品集/其他事项集
元素字段：名称、别名、标签集、介绍、详情、分组 …
```

故事家 MCP 的寻址方式是 **`{type, id}` 稳定 ID**（`mcp/story-tools.js:39`），而拆书是 **名称 + 别名**（`engine/scripts/element_identity.py`，且注释明确"The external story/Delta format remains name-based and unchanged"）。

导出侧 `backend/storyer_export.py` 目前输出 `序号 / 名称 / 正文 / 分析态`，**不输出元素 ID**。因此 ID 由故事家在导入时自行分配（`local-story-sync-core.mjs` 的映射层）。

**这是两边目前最大的结构性差异**，影响两件事：

1. 拆书若要用故事家式 MCP 做元素级精确读取（A5），需要一层"名称 → 稳定 ID"映射；拆书已有确定性名称/别名解析（歧义即报错），可作为该映射的基础。
2. 若希望同一元素在拆书与故事家之间保持同一身份（跨系统引用、增量同步），需要拆书在元素创建时分配稳定 ID，并让 `storyer_export.py` 一并导出。这会改动外部 Delta 格式，属较大变更，**建议单独立项，不并入本轮**。

---

## 八、建议顺序

| 阶段 | 动作 | 前置 |
|---|---|---|
| 1 | A2 错误分级 + A1 空闲超时 | 无 |
| 2 | A3 提交回执幂等 + A4 版本门禁 | 阶段 1 确立错误分类后 |
| 3 | A5 元素级读取白名单 | 需要名称→ID 映射（可先按名称实现） |
| 4 | B1/B2 引入 Pydantic 运行时作为新 provider | 阶段 1–3 稳定后 |

**验收标准**：任何一次任务失败，程序能准确说出它属于哪一类错误、已执行到哪一步、以及是重跑还是接续；不再出现"合法产物被程序规则拦死、耗尽修复次数后停机"。
