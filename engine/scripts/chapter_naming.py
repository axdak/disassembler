"""章节标题 -> 扁平产物文件名段的共享工具。

analysis_context_pack / artifact_status / migrate_artifact_layout 三处原先各抄一份
_title_slug，逻辑漂移会导致"worker 写到甲、UI 找乙、迁移搬丙"的文件错位。统一到此处。
无项目内依赖，避免循环 import。
"""
import html
import re
from pathlib import Path


def title_slug_from_filename(filename: str) -> str:
    """从章节文件名提取用于扁平产物文件名的标题段。

    流程：取 stem -> 去 第NNN章_ 前缀 -> HTML 实体反转义 -> 文件名非法字符清洗。
    空标题返回 "无标题"（保证 glob 模式 ch{seq:03d}_*_{维度}.md 能命中）。
    """
    stem = filename[:-3] if filename.endswith(".md") else Path(filename).stem
    m = re.match(r"^第\d+章[_\-\s]*(.*)$", stem)
    title = (m.group(1) if m else stem).strip()
    title = html.unescape(title)  # 章节 filename 可能含 HTML 实体（如 &#039;），转回可读字符
    title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    return title or "无标题"
