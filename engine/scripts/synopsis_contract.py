"""简介的共享篇幅预算与可识别标题。

周期治理及分片归并专注内容，准确的旧简介不因排版而重写。篇幅预算仍由
生成、归并及消费端共享；已存在的小标题由最终收尾或独立排版功能整理。
标题列表仅用于识别既有展示结构，不要求模型补齐四节或强制重排内容。
本模块不参与 engine_contract 的硬校验指纹，排版变化不使历史提交失效。
"""
from __future__ import annotations

# 简介篇幅上限（含标点）。多周期累积的简介不得突破该值：
# "已处理章节变多"只能靠压缩既有表述来换篇幅，不得逐周期累加。
SYNOPSIS_MAX_CHARS = 1500

# 分节装饰符。用中文书名号式方括号而不是 Markdown 标记：纯文本可读、渲染后不变形。
SYNOPSIS_SECTION_PREFIX = "【"
SYNOPSIS_SECTION_SUFFIX = "】"

# 已有简介可识别的分节；排版沿用原有顺序，不自动分配正文到这些类别。
SYNOPSIS_SECTIONS = ("主线", "主角处境", "核心冲突", "当前进展")

# 归并路径的降级阶梯：首档即契约值，材料超出可用输入预算时逐步收紧目标。
# 与上限同源，保证"调字数"只需要改本文件。
SYNOPSIS_SYNTHESIS_BUDGETS = (SYNOPSIS_MAX_CHARS, 1100, 700, 400)


def synopsis_section_headings() -> tuple[str, ...]:
    """返回可识别的已有标题（如 `【主线】`），供排版与兼容调用共用。"""
    return tuple(f"{SYNOPSIS_SECTION_PREFIX}{name}{SYNOPSIS_SECTION_SUFFIX}"
                 for name in SYNOPSIS_SECTIONS)


def synopsis_section_spec(joiner: str = "、") -> str:
    """把分节标题拼成可直接嵌入提示词的一段文字。"""
    return joiner.join(f"`{heading}`" for heading in synopsis_section_headings())
