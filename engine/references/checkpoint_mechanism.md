# 拆书流程断点机制说明

## 概述

拆书流程包含4个主步骤：章节拆分、逐章分析与提取、全书维度分析、最终结构整理与强校验。

步骤2采用**逐切片串行 + 双产物 + 质量治理**机制：每个 `_索引.json` 中的 seq 先输出 MD 分析，再输出 Delta JSON；Delta 先经过 `validate_delta.py` 入库前校验，通过后才由 `merge_delta.py` 合并；合并后立刻运行 `validate_structure.py --chapter-check` 对合并后的整个过程库做 `process` 级全量校验，并报告本切片触碰元素数量。每个切片保存结构快照，确保中断后可以从最近可信切片继续。

这里的“第001章”是系统内部顺序号，不要求原文标题必须是“第X章”。原文可以按数字编号、回、节、幕、番外等形式切分。禁止把多个 seq 合并成一个产物，例如 `章节处理/第003-005章_*.md` 或 `章节处理/第003-005章_*.json`。

## 进度文件

进度文件存储在项目根目录下的 `进度.json`：

```json
{
  "项目目录": "/path/to/拆书_书名",
  "源文件": "/path/to/novel.txt",
  "创建时间": "2026-01-01T00:00:00",
  "更新时间": "2026-01-01T01:00:00",
  "当前步骤": "2",
  "步骤状态": {
    "1": {"状态": "completed", "详情": {}, "完成时间": "..."},
    "2": {
      "状态": "in_progress",
      "详情": {
        "已完成章节": ["第001章_标题.md", "第002章_标题.md"],
        "总章节数": 120,
        "最近审计章节": "第005章"
      },
      "完成时间": null
    },
    "3": {"状态": "pending", "详情": {"已完成子步骤": []}, "完成时间": null},
    "4": {"状态": "pending", "详情": {}, "完成时间": null}
  }
}
```

`已完成章节` 的含义：该章 MD、Delta、Delta校验、合并、章节级校验均已通过，且已保存 `story_after_chNNN.json` 快照。

可选两阶段运行时，使用 `run <项目目录> --phase analysis --run-mode worker --worker-provider auto` 生成并校验全部章节分析 MD，但这些 MD 只表示“分析阶段可继续”，不计入 `已完成章节`，也不会生成 `story_after_chNNN.json`。随后使用 `run <项目目录> --phase delta --run-mode worker --worker-provider auto`，它会先检查全部 MD 均通过现有章节分析校验，再进入原有 Delta、合并、快照与治理链。

## 每章产物

每章至少产生：

| 文件 | 内容 |
|------|------|
| `章节处理/第NNN章_标题.md` | 章节10维度分析 |
| `章节处理/第NNN章_标题.json` | 本章Delta |
| `质量治理/delta校验/第NNN章_标题.json` | 入库前校验报告 |
| `质量治理/章节校验/第NNN章_标题.json` | 合并后全量过程库校验报告，附本切片触碰元素统计 |
| `故事结构版本/story_before_chNNN.json` | 合并前快照 |
| `故事结构版本/story_after_chNNN.json` | 合并通过后快照 |

## 单章执行流程

```text
1. 主控读取当前章节原文。
2. 章节分析器输出 第NNN章_标题.md。
3. Delta提取器读取当前故事结构 + 本章原文 + 本章分析MD，输出 第NNN章_标题.json。
4. 运行 validate_delta.py。
5. 如果 validate_delta.py 失败：保留报告，只重跑Delta提取器。
6. 如果通过：保存 story_before_chNNN.json。
7. 运行 merge_delta.py。
8. 运行 validate_structure.py --chapter-check，对合并后的全量过程库执行 process 检查。
9. 如果章节校验失败：回滚到 story_before_chNNN.json，只重跑Delta提取器。
10. 如果通过：保存 story_after_chNNN.json，更新进度。
```

## 恢复流程

恢复时先运行：

```bash
python <skill_path>/scripts/progress_manager.py resume <项目目录>
python <skill_path>/scripts/progress_manager.py reconcile <项目目录>
```

然后按以下规则判断：

### 1. MD不存在

说明章节分析没有完成。重新从章节分析器开始。

### 2. MD存在，Delta不存在

说明文学分析完成，但结构提取未完成。只运行Delta提取器。

### 3. MD和Delta存在，但章节未记录完成

不能直接认为已合并成功。需要检查：

- 是否有 `质量治理/delta校验/第NNN章_标题.txt`
- 是否有 `质量治理/章节校验/第NNN章_标题.txt`
- 是否有 `故事结构版本/story_after_chNNN.json`

如果这些文件不完整，优先从最近可信快照恢复，并重放/重跑后续章节。

使用 `recover-story --snapshot N --to M` 从章节快照恢复时，如果恢复区间内存在已提交的周期治理状态（例如从 `story_after_ch005.json` 恢复，而 `audit_001-005.status.json` 已是 `committed`），脚本会在对应章节边界重放 `correction_001-005.json`，再继续后续章节 Delta。若 correction 文件缺失或为空，恢复会暂停并把该周期审计状态改回 `awaiting_agent`，避免静默丢失治理补丁。

### 4. 进度记录完成，但快照缺失

说明进度与文件系统不一致。该章应视为“不完全可信”，建议从上一章可信快照回滚后重放本章Delta。

### 5. 发现某章污染结构

1. 找到污染章前一章的 `story_after_chNNN.json`。
2. 覆盖 `故事结构_增量.json`。
3. 从污染章开始重跑Delta提取器或重放可信Delta。
4. 每章重新执行 validate_delta、merge_delta、chapter-check；chapter-check 会检查合并后的全量过程库。

## 周期审计断点

周期审计文件位于：

```text
质量治理/周期审计/
```

每次审计至少包含：

| 文件 | 内容 |
|------|------|
| `audit_001-005.md` | 结构审计报告 |
| `correction_001-005.json` | 修正Delta |
| `correction_001-005.validation.txt` | 修正Delta校验报告 |

审计Delta也必须走：

```bash
python <skill_path>/scripts/validate_delta.py <故事结构_增量.json> correction_001-005.json
python <skill_path>/scripts/merge_delta.py <故事结构_增量.json> correction_001-005.json
python <skill_path>/scripts/validate_structure.py --chapter-check <故事结构_增量.json> correction_001-005.json
```

## 最坏损失评估

| 中断位置 | 损失 | 恢复方式 |
|------|------|------|
| 章节分析器运行中 | 当前章MD未完成 | 重跑章节分析器 |
| Delta提取器运行中 | 当前章Delta未完成 | 重跑Delta提取器 |
| validate_delta失败 | 不污染结构 | 重写Delta |
| merge_delta之后、chapter-check之前 | 可能已合并但未确认 | 用 story_before_chNNN 回滚再重跑 |
| chapter-check失败 | 合并后的全量过程库未通过 process 检查，结构已回滚 | 重写Delta |
| 进度更新前中断 | 产物可能齐全但进度未记 | reconcile后检查快照和校验报告 |

## 关键设计原则

1. MD分析和JSON提取分离。
2. 每章只输出Delta，不全量重写故事结构。
3. Delta合并前必须校验。
4. 合并后必须执行全量过程库校验。
5. 每章保存前后快照。
6. 周期审计输出correction_delta，不直接覆盖完整JSON。
7. 最终完整JSON只在最终整理阶段生成，并必须通过强校验。
