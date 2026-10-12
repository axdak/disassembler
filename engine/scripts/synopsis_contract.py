"""故事简介（`介绍.描述`）的篇幅与结构契约。

简介是"读者入口"而不是"索引"：细节由角色/事件/地点/线索/阵营/物品档案承载。
因此简介需要明确的篇幅上限与固定结构，而这两者在三个地方必须一致：

- 生成端：周期治理提示词（`prompts/流水线/周期审计.j2`）要求模型在该字数内完成一次完整替换；
- 归并端：`governance_introduction.combine_introductions` 在多份候选冲突时按同一量级重写；
- 消费端：`analysis_context_pack.summarize_story_structure` 只把前 N 字放进任务包。

三者不一致的后果是真金白银的浪费：真实简介会随周期一直变长，而下游只读到前 N 字，
看不见的部分仍然占着故事结构体积、每轮输入预算和每周期一次的生成成本。
（实测：《斗破苍穹》0 → 257 → 376 → 510 字，每周期约 +120~134 字，且当时无任何上限。）

**结构化**：上限放宽到 1500 字之后，简介必须分节排布而不是一整坨连续长文。
固定四个分节、固定顺序、`【小标题】` 独占一行、节间空一行——这样它在纯文本
查看器、Markdown 导出和 JSON 原文里都能被稳定地读成一份"结构化的读者入口"。
刻意不用 `**加粗**` 之类 Markdown 语法：`介绍.描述` 是纯文本字段，
Markdown 标记在非渲染场景下会变成字面噪声。

本模块刻意**不**参与 `engine_contract` 的校验指纹：篇幅与分节都是软契约
（超出不判失败，见本目录 README 的"过程阶段不升级为 error"原则），
不是"什么算通过"的判定规则。把它纳入指纹只会让"调个字数值"触发历史回执
集体失效与大规模重算。
"""
from __future__ import annotations

# 简介篇幅上限（含标点）。多周期累积的简介不得突破该值：
# "已处理章节变多"只能靠压缩既有表述来换篇幅，不得逐周期累加。
SYNOPSIS_MAX_CHARS = 1500

# 分节装饰符。用中文书名号式方括号而不是 Markdown 标记：纯文本可读、渲染后不变形。
SYNOPSIS_SECTION_PREFIX = "【"
SYNOPSIS_SECTION_SUFFIX = "】"

# 简介的固定分节，元组顺序即契约要求的展示顺序。
# 覆盖"读者入口"需要的四个维度：整体脉络、主角当前处境、核心张力、读到哪儿了。
SYNOPSIS_SECTIONS = ("主线", "主角处境", "核心冲突", "当前进展")

# 归并路径的降级阶梯：首档即契约值，材料超出可用输入预算时逐步收紧目标。
# 与上限同源，保证"调字数"只需要改本文件。
SYNOPSIS_SYNTHESIS_BUDGETS = (SYNOPSIS_MAX_CHARS, 1100, 700, 400)


def synopsis_section_headings() -> tuple[str, ...]:
    """返回带装饰的分节标题（如 `【主线】`），供提示词与测试共用同一来源。"""
    return tuple(f"{SYNOPSIS_SECTION_PREFIX}{name}{SYNOPSIS_SECTION_SUFFIX}"
                 for name in SYNOPSIS_SECTIONS)


def synopsis_section_spec(joiner: str = "、") -> str:
    """把分节标题拼成可直接嵌入提示词的一段文字。"""
    return joiner.join(f"`{heading}`" for heading in synopsis_section_headings())
