"""Enumerate complete, existing evidence without imposing model-window cutoffs."""
from pathlib import Path
import re


def chapter_evidence(project: Path, start: int = 1, end: int | None = None):
    """Keep historical originals, analyses and Deltas; never include future chapters."""
    sources = []
    # Enumerate independently: renamed/missing originals must not silently hide
    # an existing historical analysis or Delta with a different chapter title.
    for directory, pattern, role in (("原文拆解", "第*章_*.md", "完整原文"),
                                      ("章节处理", "第*章_*.md", "完整章节分析"),
                                      ("章节处理/Delta", "第*章_*.json", "完整章节Delta")):
        for path in sorted((project / directory).glob(pattern)):
            match = re.match(r"第(\d+)章", path.name)
            if not match or int(match[1]) < start or end is not None and int(match[1]) > end:
                continue
            if path.is_file():
                sources.append((int(match[1]), path, f"第{int(match[1]):03d}章{role}"))
    return [(path, role) for _, path, role in sorted(sources, key=lambda item: item[0])]


def history_evidence(project: Path, before: int):
    """The story archive and every available prior chapter are separate evidence."""
    story = project / "故事结构_增量.json"
    return ([(story, "完整历史故事结构")] if story.is_file() else []) + chapter_evidence(project, end=before - 1)


def render_sources(sources):
    """Embed full bytes as text; callers own splitting and coverage validation."""
    return "\n\n".join(f"## {role}\n来源：{path}\n{path.read_text(encoding='utf-8')}"
                       for path, role in sources)
