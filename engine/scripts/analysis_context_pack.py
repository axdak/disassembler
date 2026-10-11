#!/usr/bin/env python3
"""
全书/局部分析上下文打包器。

用途：
  把“全书维度分析”从固定第3步，改造成随时可唤起、可按章节范围/目标元素执行、可分批归约的分析任务。

典型用法：
  python analysis_context_pack.py <项目目录> --task summary --chapters 1-50
  python analysis_context_pack.py <项目目录> --task characters --targets 角色:林婉 角色:沈墨 --chapters 1-120
  python analysis_context_pack.py <项目目录> --task plot --chapters 1-80 --max-pack-chars 70000
  python analysis_context_pack.py <项目目录> --task report --chapters all

输出：
  全书分析/_任务包/<timestamp>_<task>/manifest.json
  全书分析/_任务包/<timestamp>_<task>/pack_001.md ...
  若需要reduce，还会生成 reduce_prompt.md

核心原则：
  1. 大模型不直接吃全书；先分包，后归约。
  2. 优先使用章节分析MD + 故事结构_增量.json 摘要。
  3. 原文只按需要抽样/摘录，避免上下文爆炸。
  4. 每个分析结论必须能回指：章节、原文片段、故事结构元素。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from story_schema_rules import COLLECTION_KEYS, SUPPLEMENTARY_TAGS_DETAIL_KEY, parse_supplementary_tags
from prompt_loader import render_prompt
from chapter_naming import title_slug_from_filename

TYPE_TO_COLLECTION = {
    "角色": "角色集",
    "事件": "事件集",
    "地点": "地点集",
    "线索": "线索集",
    "阵营": "阵营集",
    "物品": "物品集",
    "其他事项": "其他事项集",
}

TASKS: Dict[str, Dict[str, Any]] = {
    "summary": {
        "name": "章节梗概汇总",
        "outputs": ["全书分析/剧情结构/章节梗概汇总.md"],
        "goal": "按章节输出500字左右梗概、章节功能、主要人物、核心冲突，并为后续剧情线/节奏分析提供稳定底座。",
    },
    "characters": {
        "name": "人物分析",
        "outputs": ["全书分析/人物分析/人物档案.md", "全书分析/人物分析/人物关系.md", "全书分析/人物分析/人物提及.json"],
        "goal": "基于原文、章节分析和角色集，梳理人物档案、人物关系变化、人物提及位置和证据。",
    },
    "plot": {
        "name": "剧情线分析",
        "outputs": ["全书分析/剧情结构/剧情线索.md", "全书分析/剧情结构/伏笔追踪.md", "全书分析/剧情结构/冲突图谱.md"],
        "goal": "基于章节梗概、事件集和线索集，整理主线/支线/伏笔/冲突推进。",
    },
    "style": {
        "name": "节奏与文风分析",
        "outputs": ["全书分析/风格分析/文风分析.md", "全书分析/风格分析/节奏分析.md", "全书分析/风格分析/高光场景.md"],
        "goal": "基于章节分析中的情绪点、章节功能和原文抽样，分析文风、节奏、爽点/高光场景。",
    },
    "report": {
        "name": "拆书总报告",
        "outputs": ["全书分析/拆书总报告.md"],
        "goal": "综合前置分析、故事结构JSON和原书梗概，输出完整拆书总报告。",
    },
    "custom": {
        "name": "自定义分析",
        "outputs": [],
        "goal": "根据用户指定问题，对原书、章节分析和故事结构JSON做局部或全书分析。",
    },
    "worldview": {
        "name": "世界观提取",
        "outputs": ["全书分析/世界观/世界观档案.md"],
        "goal": "从原文提取时代背景、地理环境、社会制度、势力格局、力量体系/魔法系统、种族设定、历史纪年、世界规则等世界观要素，并检查各要素之间的自洽性。",
    },
    "settings": {
        "name": "设定体系提取",
        "outputs": ["全书分析/世界观/设定档案.md"],
        "goal": "从原文提取具体设定，如力量体系、功法武技、特殊种族、职业体系、物品等级、特殊规则等，并梳理设定间的关联和层级结构。",
    },
    "plotlines": {
        "name": "剧情线梳理",
        "outputs": ["全书分析/剧情结构/剧情线总表.md", "全书分析/剧情结构/剧情线交汇矩阵.md"],
        "goal": "识别主线/支线/暗线伏笔/角色成长线/情感线，梳理每条线的起承转合节点和交汇点，标注优先级和推进节奏。",
    },
    "outline": {
        "name": "大纲反推",
        "outputs": ["全书分析/剧情结构/全书大纲.md"],
        "goal": "反推原文的卷/篇章级叙事框架，包含各卷核心冲突、关键转折、高潮事件、结局走向、角色出入场、设定/道具引入时机、伏笔操作。",
    },
    "detailed_outline": {
        "name": "细纲反推",
        "outputs": ["全书分析/剧情结构/章节细纲.md"],
        "goal": "反推原文的章节级甚至场景级结构，涵盖场景设置、出场人物、章节目标、核心冲突、关键事件、伏笔操作、章节钩子、氛围基调、字数、推进剧情线。",
    },
    "visual_assets": {
        "name": "视觉资产分析",
        "outputs": ["全书分析/视觉资产/视觉资产清单.md", "全书分析/视觉资产/关键场景分镜表.md", "全书分析/视觉资产/AI绘图提示词素材.md", "全书分析/视觉资产/角色外观一致性表.md", "全书分析/视觉资产/场景氛围表.md"],
        "goal": "基于事件集、章节分析中的画面/分镜候选和原文抽样，提取可用于封面、漫画分镜、短剧分镜、角色卡、场景概念图和AI绘图提示词的视觉生产资料。",
    },
    "chapter_structure": {
        "name": "章节结构定位（分章独立产物）",
        "outputs": ["全书分析/故事结构/分章/ch{NNN}_<标题>_章节结构.md"],
        "goal": "对每章独立产出 三幕式节拍定位 + 七要素本章信号 + 力学/工程学/骨架本章信号；不跨章归纳，不写入故事结构 JSON。",
    },
    "narrative_structure": {
        "name": "故事结构全书分析",
        "outputs": [
            "全书分析/故事结构/三幕式结构图.md",
            "全书分析/故事结构/Brooks四部分结构图.md",
            "全书分析/故事结构/Freytag五段结构图.md",
            "全书分析/故事结构/故事七要素档案.md",
            "全书分析/故事结构/故事力学评估.md",
            "全书分析/故事结构/故事工程学评估.md",
            "全书分析/故事结构/小说骨架.md",
        ],
        "goal": "基于章节分析MD、summary/outline/plotlines 以及分章 章节结构.md，全书级输出三种节拍图 + 七要素档案 + 力学/工程学评估 + 小说骨架。",
    },
}

# 视觉资产分章产物五件套（与顶层一一对应）
VISUAL_ASSETS_PER_CHAPTER_FILES = [
    "视觉资产清单.md",
    "关键场景分镜表.md",
    "AI绘图提示词素材.md",
    "角色外观一致性表.md",
    "场景氛围表.md",
]


def _title_slug(ch: Dict[str, Any]) -> str:
    """从章节字典提取标题段（薄包装，逻辑见 chapter_naming.title_slug_from_filename）。"""
    return title_slug_from_filename(ch.get("filename") or "")


def visual_assets_per_chapter_outputs(seq: int, name: str = "") -> List[str]:
    """单章视觉资产五个产物的相对路径（扁平：ch{seq:03d}_{标题}_{维度}.md）。"""
    prefix = f"全书分析/视觉资产/分章/ch{seq:03d}_{name}" if name else f"全书分析/视觉资产/分章/ch{seq:03d}"
    return [f"{prefix}_{fname}" for fname in VISUAL_ASSETS_PER_CHAPTER_FILES]


def chapter_structure_per_chapter_outputs(seq: int, name: str = "") -> List[str]:
    """单章故事结构产物的相对路径（扁平：ch{seq:03d}_{标题}_章节结构.md）。"""
    if name:
        return [f"全书分析/故事结构/分章/ch{seq:03d}_{name}_章节结构.md"]
    return [f"全书分析/故事结构/分章/ch{seq:03d}_章节结构.md"]


def _existing_chapter_structure_path(project_dir: Path, seq: int) -> Optional[Path]:
    """按 seq 前缀 glob 查找已存在的章节结构产物（标题无关，抗章节重命名）。"""
    base = project_dir / "全书分析" / "故事结构" / "分章"
    if not base.is_dir():
        return None
    for p in base.glob(f"ch{seq:03d}_*_章节结构.md"):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _existing_visual_paths(project_dir: Path, seq: int) -> List[Path]:
    """按 seq 前缀 glob 查找已存在的视觉资产产物（标题无关）。"""
    base = project_dir / "全书分析" / "视觉资产" / "分章"
    if not base.is_dir():
        return []
    return [p for p in base.glob(f"ch{seq:03d}_*.md") if p.is_file() and p.stat().st_size > 0]


def _visual_complete(project_dir: Path, seq: int) -> bool:
    """单章视觉资产五件套是否齐全且非空。"""
    found = {p.name.rsplit("_", 1)[-1][:-3] for p in _existing_visual_paths(project_dir, seq)}
    return all(stem in found for stem in (f[:-3] for f in VISUAL_ASSETS_PER_CHAPTER_FILES))


NARRATIVE_STRUCTURE_BACKGROUND_SPECS = [
    ("全书分析/剧情结构/章节梗概汇总.md", "章节梗概汇总"),
    ("全书分析/剧情结构/全书大纲.md", "全书大纲"),
    ("全书分析/剧情结构/剧情线总表.md", "剧情线总表"),
    ("全书分析/剧情结构/剧情线交汇矩阵.md", "剧情线交汇矩阵"),
]

# 结构类任务补充全书中间产物，但仍保留原文、章节分析与历史档案。
STRUCTURE_TASKS = frozenset({
    "characters", "plot", "worldview", "settings", "plotlines",
    "outline", "narrative_structure", "report",
})


def build_narrative_structure_background(project_dir: Path, chapters: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """收集 narrative_structure 全书任务所需的上游材料。

    优先级：
    分章 章节结构.md > 章节梗概汇总 > 全书大纲 > 剧情线总表 > 剧情线交汇矩阵

    上游产物缺失时显式标注「未生成」，不静默吞掉。
    """
    lines: List[str] = ["## 全书背景资料", ""]

    # 分章 章节结构.md
    lines.append("### 分章 章节结构.md（chapter_structure 产物）")
    found_any = False
    for ch in chapters:
        seq = int(ch["seq"])
        path = _existing_chapter_structure_path(project_dir, seq)
        if path is not None:
            found_any = True
            lines.append(f"#### 第{seq:03d}章 章节结构.md")
            lines.append("```markdown")
            lines.append(read_text(path, max_chars))
            lines.append("```")
            lines.append("")
    if not found_any:
        lines.append("【未生成任何分章 章节结构.md；建议先运行 --task chapter_structure --per-chapter】")
        lines.append("")

    # 4 个全书级参考产物
    for rel, title in NARRATIVE_STRUCTURE_BACKGROUND_SPECS:
        path = project_dir / rel
        lines.append(f"### {title}")
        if path.is_file() and path.stat().st_size > 0:
            lines.append("```markdown")
            lines.append(read_text(path, max_chars))
            lines.append("```")
        else:
            lines.append(f"【未生成 `{rel}`；narrative_structure 的对齐质量会受影响。】")
        lines.append("")

    return "\n".join(lines)


def _build_summary_index(summary_text: str) -> str:
    """把 章节梗概汇总.md 压缩为分章索引：每章一行「第NNN章 标题｜功能」。

    旧版索引工具，当前 build_structure_context 不再调用；不得用它替代完整证据。
    解析按 markdown 章节标题行（## 第NNN章 / ### 第NNN章 / # 第NNN章 等）切分；
    取标题行 + 紧随其后的第一个非空短行（通常是「章节功能」）作为索引项。
    无法识别章节标题时返回空串。
    """
    if not summary_text:
        return ""
    lines = summary_text.splitlines()
    index_lines: List[str] = ["# 章节梗概分章索引", ""]
    i = 0
    found = False
    while i < len(lines):
        line = lines[i].strip()
        # 匹配「第NNN章」开头的标题行（##/###/# 前缀或无前缀均可）
        if re.match(r"^#{0,6}\s*第\s*\d+\s*章", line):
            found = True
            title = re.sub(r"^#{0,6}\s*", "", line)
            # 紧随其后的第一个非空行作为功能摘要（截断到 60 字防长段）
            func = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                func = lines[j].strip()[:60]
            index_lines.append(f"- {title}｜{func}" if func else f"- {title}")
            i = j + 1
        else:
            i += 1
    return "\n".join(index_lines) if found else ""


def build_structure_context(project_dir: Path, task: str, chapters: List[Dict[str, Any]], max_pack_chars: int = 70000) -> str:
    """Keep complete structural reports; capacity is handled after material collection."""
    lines: List[str] = ["## 全书级分析材料", ""]

    if task == "narrative_structure":
        # narrative_structure 用专属 background（分章章节结构 + 章节梗概汇总 + 全书大纲 + 剧情线）
        lines.append(build_narrative_structure_background(project_dir, chapters))
        lines.append("")
    else:
        summary_path = project_dir / "全书分析" / "剧情结构" / "章节梗概汇总.md"
        lines.extend(["### 章节梗概汇总", read_text(summary_path) or "【未生成章节梗概汇总】", ""])

        if task == "report":
            # report 综合所有前置全书分析产物（已生成的才收入）
            for task_key, info in TASKS.items():
                if task_key in {"report", "custom", "visual_assets", "chapter_structure"}:
                    continue
                for rel in info.get("outputs", []):
                    path = project_dir / rel
                    if path.is_file() and path.stat().st_size > 0:
                        lines.append(f"### {info['name']}：{rel}")
                        lines.append("```markdown")
                        lines.append(read_text(path, 20000))
                        lines.append("```")
                        lines.append("")

    lines.append("### 上下文指引")
    lines.append("- 当前故事结构摘要已在上方列出（角色集/事件集/地点集/线索集/阵营集/物品集/其他事项集的元素与分组）。")
    lines.append("- 如需元素详情（关系/介绍/详情/时间/涉及章节等），可读取项目根目录的 `故事结构_增量.json`。")
    lines.append("- 所有结论必须回指章节号、原文片段或 `类型:名称` 元素；不确定写“待确认”。")
    lines.append("")
    return "\n".join(lines)


def read_text(path: Path, max_chars: Optional[int] = None) -> str:
    if not path.is_file():
        return ""
    # max_chars remains accepted for old callers, but can no longer discard evidence.
    return path.read_text(encoding="utf-8")



def load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def parse_chapter_spec(spec: str, all_chapters: Sequence[int]) -> List[int]:
    spec = (spec or "all").strip().lower()
    if spec in ("", "all", "全部"):
        return list(all_chapters)
    result: Set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            for i in range(min(start, end), max(start, end) + 1):
                result.add(i)
        else:
            result.add(int(part))
    return [i for i in all_chapters if i in result]


def load_index(project_dir: Path) -> List[Dict[str, Any]]:
    index_path = project_dir / "原文拆解" / "_索引.json"
    data = load_json(index_path)
    if isinstance(data, dict) and isinstance(data.get("chapters"), list):
        chapters = data["chapters"]
    elif isinstance(data, list):
        chapters = data
    else:
        chapters = []
    if chapters:
        normalized = []
        for i, ch in enumerate(chapters, 1):
            if not isinstance(ch, dict):
                continue
            filename = ch.get("filename") or ch.get("文件名") or ""
            if filename and filename.startswith("_"):
                continue
            seq = ch.get("seq") or ch.get("chapter") or ch.get("序号") or i
            try:
                seq = int(seq)
            except Exception:
                seq = i
            normalized.append({
                "seq": seq,
                "filename": filename,
                "title": ch.get("title") or ch.get("标题") or Path(filename).stem,
                "source_title": ch.get("source_title") or ch.get("原始标题") or ch.get("title") or Path(filename).stem,
                "is_preface": bool(ch.get("is_preface", False)),
            })
        return sorted(normalized, key=lambda x: x["seq"])

    # fallback scan
    split_dir = project_dir / "原文拆解"
    result = []
    for p in sorted(split_dir.glob("第*章_*.md")):
        m = re.match(r"第(\d+)章", p.name)
        seq = int(m.group(1)) if m else len(result) + 1
        result.append({"seq": seq, "filename": p.name, "title": p.stem, "source_title": p.stem, "is_preface": False})
    return result


def sample_original(text: str, mode: str, max_chars: int) -> str:
    """Legacy policy arguments are compatible aliases for complete evidence."""
    return text



def summarize_story_structure(story: Dict[str, Any], max_items: int = 80) -> str:
    lines = []
    intro = story.get("介绍", {}) if isinstance(story, dict) else {}
    if isinstance(intro, dict):
        lines.append(f"- 标题: {intro.get('标题', '')}")
        desc = intro.get("描述", "")
        if desc:
            lines.append(f"- 描述: {str(desc)[:500]}")
    for key in COLLECTION_KEYS:
        items = story.get(key, []) if isinstance(story, dict) else []
        lines.append(f"- {key}: {len(items)}")
        names = []
        # max_items <= 0 表示不限制（给全部名称），与 build_lightweight_index 的全索引语义对齐；
        # 供大书的全书分析任务包使用，避免 80 截断导致第 81 个起元素名称被遗漏。
        shown = items if max_items <= 0 else items[:max_items]
        for item in shown:
            if isinstance(item, dict) and item.get("名称"):
                brief = item.get("名称")
                group = item.get("分组")
                if group:
                    brief += f"({group})"
                # 视觉资产工作流的便利锚点：把 画面类型:/视觉用途: 这两个前缀
                # 在结构索引里平铺出来，避免 v12.1 标签压缩后 LLM 在 标签集
                # 中看不到、需要去 详情.补充标签 反查的体验回退。
                if key == "事件集":
                    visual_tags = collect_visual_tags(item)
                    if visual_tags:
                        brief += f"[{','.join(visual_tags)}]"
                names.append(str(brief))
        if names:
            lines.append("  - " + "、".join(names))
        if max_items > 0 and len(items) > max_items:
            lines.append(f"  - ……另有 {len(items) - max_items} 个未列出")
    return "\n".join(lines)


VISUAL_TAG_PREFIXES = ("画面类型:", "视觉用途:")


def collect_visual_tags(item: Dict[str, Any]) -> List[str]:
    """从 标签集 和 详情.补充标签 两处合并读取视觉资产入口标签。

    v12.1 起 `compress_tags.py` 把所有带受控前缀的标签下层到 `详情.补充标签`，
    包括 `画面类型:` / `视觉用途:`。下游需要展示「哪些事件已被标为视觉候选」时，
    必须两处都查，否则会以为压缩前后标注消失了。

    返回顺序：标签集中的视觉前缀优先（保留 LLM 直读），再追加补充标签里的非重复项。
    """
    found: List[str] = []
    seen: Set[str] = set()

    raw_tags = item.get("标签集") if isinstance(item, dict) else None
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, str) and tag.startswith(VISUAL_TAG_PREFIXES) and tag not in seen:
                found.append(tag)
                seen.add(tag)

    detail = item.get("详情") if isinstance(item, dict) else None
    if isinstance(detail, dict):
        raw_supp = detail.get(SUPPLEMENTARY_TAGS_DETAIL_KEY)
        if isinstance(raw_supp, str) and raw_supp.strip():
            try:
                supp_tags = parse_supplementary_tags(raw_supp)
            except ValueError:
                supp_tags = []
            for tag in supp_tags:
                if tag.startswith(VISUAL_TAG_PREFIXES) and tag not in seen:
                    found.append(tag)
                    seen.add(tag)
    return found


def item_names(item: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    name = item.get("名称")
    if isinstance(name, str) and name.strip():
        names.add(name.strip())
    aliases = item.get("别名", []) or []
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                names.add(alias.strip())
    return names


def select_target_items(story: Dict[str, Any], targets: List[str]) -> List[Tuple[str, str, Dict[str, Any]]]:
    found = []
    for target in targets:
        if ":" not in target:
            continue
        type_name, name = target.split(":", 1)
        type_name, name = type_name.strip(), name.strip()
        collection = TYPE_TO_COLLECTION.get(type_name)
        if not collection:
            continue
        for item in story.get(collection, []) or []:
            if isinstance(item, dict) and name in item_names(item):
                found.append((type_name, name, item))
                break
    return found


def build_chapter_block(project_dir: Path, ch: Dict[str, Any], task: str, include_original: str, max_original_chars: int, max_analysis_chars: int) -> str:
    filename = ch.get("filename") or ""
    split_path = project_dir / "原文拆解" / filename
    analysis_path = project_dir / "章节处理" / filename
    base = filename[:-3] if filename.endswith(".md") else Path(filename).stem
    delta_path = project_dir / "章节处理" / "Delta" / f"{base}.json"

    lines = []
    lines.append(f"### 第{ch['seq']:03d}章｜{ch.get('title') or ch.get('source_title') or base}")
    lines.append("")
    lines.append("#### 章节分析MD")
    lines.append("```markdown")
    # Analysis is evidence, not a disposable excerpt. Full-book generation splits
    # oversized rendered material below instead of discarding its trailing text.
    # Keep max_analysis_chars in the API for existing task/config compatibility.
    lines.append(read_text(analysis_path) or "【未找到章节分析MD】")
    lines.append("```")
    lines.append("")

    # Even persisted auto/sample/none settings must not skip source evidence.
    lines.extend(["#### 完整原文", "```markdown", read_text(split_path) or "【未找到原文章节】", "```", ""])

    # Every analysis dimension retains the complete chapter Delta as evidence.
    lines.append("#### 本章Delta")
    lines.append("```json")
    lines.append(read_text(delta_path) or "【未找到本章Delta】")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


_CONTRACT_CN = {
    "summary": "章节梗概汇总",
    "characters": "人物分析",
    "plot": "剧情线分析",
    "style": "节奏与文风",
    "report": "拆书总报告",
    "worldview": "世界观",
    "settings": "设定体系",
    "plotlines": "剧情线梳理",
    "outline": "大纲反推",
    "detailed_outline": "细纲反推",
    "visual_assets": "视觉资产",
    "chapter_structure": "章节结构",
    "narrative_structure": "故事结构全书",
    "custom": "自定义",
}


def output_contract(task: str) -> str:
    """全书分析任务的输出契约模板。

    契约正文已迁出至 prompts/全书分析/<中文名>.j2，这里按 task 选取并渲染。
    未知 task 回退到 自定义.j2（与原 if 链的 default 一致）。
    """
    name = _CONTRACT_CN.get(task, "自定义")
    return render_prompt(f"全书分析/{name}.j2")


def reduce_contract(task: str) -> str:
    return render_prompt("汇总/通用汇总.j2", task=task)


def reduce_contract_visual_per_chapter(seq: int, name: str = "") -> str:
    outs = visual_assets_per_chapter_outputs(seq, name)
    bullet = "\n".join(f"- {p}" for p in outs)
    return render_prompt("汇总/视觉资产_单章.j2", seq=seq, bullet=bullet)


def reduce_contract_chapter_structure_per_chapter(seq: int, name: str = "") -> str:
    target = chapter_structure_per_chapter_outputs(seq, name)[0]
    return render_prompt("汇总/章节结构_单章.j2", seq=seq, target=target)


def reduce_contract_visual_aggregate() -> str:
    return render_prompt("汇总/视觉资产_汇总.j2")



def split_batches(chapters: List[Dict[str, Any]], project_dir: Path, task: str, include_original: str, max_pack_chars: int, max_original_chars: int, max_analysis_chars: int) -> List[List[Dict[str, Any]]]:
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_size = 0
    for ch in chapters:
        block = build_chapter_block(project_dir, ch, task, include_original, max_original_chars, max_analysis_chars)
        size = len(block)
        if current and current_size + size > max_pack_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(ch)
        current_size += size
    if current:
        batches.append(current)
    return batches


def split_narrative_batches(project_dir: Path, chapters: List[Dict[str, Any]], max_pack_chars: int) -> List[List[Dict[str, Any]]]:
    """narrative_structure 专用分批：按分章 章节结构.md 的字符大小累加切批。

    先按完整分章结构报告粗分章节组；全书资料、历史、原文、章节分析和 Delta
    仍由 build_pack 完整加入，再由 split_rendered_pack 执行最终连续分片。
    此处预留值只是分组估算，不是截断上限或最终模型容量判定。
    """
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_size = 0
    OVERHEAD = 50000
    budget = max(20000, max_pack_chars - OVERHEAD)
    has_any = False
    for ch in chapters:
        seq = int(ch["seq"])
        path = _existing_chapter_structure_path(project_dir, seq)
        size = len(read_text(path)) if path and path.is_file() else 0
        if size > 0:
            has_any = True
        if current and current_size + size > budget:
            batches.append(current)
            current = []
            current_size = 0
        current.append(ch)
        current_size += size
    if current:
        batches.append(current)
    # 无任何分章章节结构.md → 整本一包（退化到原单包行为）
    return batches if has_any else [chapters]


def build_pack(project_dir: Path, task: str, chapters: List[Dict[str, Any]], targets: List[str], question: str, include_original: str, max_original_chars: int, max_analysis_chars: int, pack_no: int, total_packs: int, per_chapter_seq: Optional[int] = None, story_summary_max_items: int = 0, context_budget_chars: int = 160000, max_pack_chars: int = 70000) -> str:
    story_path = project_dir / "故事结构_增量.json"
    story = load_json(story_path) or {}
    task_info = TASKS[task]
    lines: List[str] = []
    if per_chapter_seq is not None:
        per_chapter_label = {
            "visual_assets": "视觉资产分章任务包",
            "chapter_structure": "章节结构分章任务包",
        }.get(task, f"{task_info['name']} 分章任务包")
        lines.append(f"# {per_chapter_label}：第{per_chapter_seq:03d}章 ({pack_no}/{total_packs})")
    else:
        lines.append(f"# 全书/局部分析任务包：{task_info['name']} ({pack_no}/{total_packs})")
    lines.append("")
    lines.append("## 任务定位")
    lines.append(task_info["goal"])
    if per_chapter_seq is not None:
        scope = "视觉资产" if task == "visual_assets" else "章节结构.md"
        lines.append("")
        lines.append(f"⚠️ 本任务包只针对**单章**生成{scope}。所有产物只能引用本章原文与本章分析MD的内容，禁止跨章总结。")
    if question:
        lines.append("")
        lines.append("## 用户指定问题")
        lines.append(question)
    lines.append("")
    lines.append("## 质量规则")
    lines.append("- 这是可重复唤起的分析任务，不要求等到全书结束。")
    lines.append("- 只能基于本任务包内的原文、章节分析和故事结构JSON判断。")
    lines.append("- 不确定的信息必须标记为“待确认”，不能编造成事实。")
    lines.append("- 大范围任务先产出分包结果，最后再根据 reduce_prompt.md 汇总。")
    lines.append("- 所有关键结论必须带证据章节或原文短句。")
    if per_chapter_seq is not None:
        lines.append("- 单章任务包**不要**写跨章汇总性结论；本章没有则相应段落写“本章无对应内容”。")
    lines.append("")

    lines.append("## 目标输出文件")
    if per_chapter_seq is not None:
        per_chapter_outputs = {
            "visual_assets": visual_assets_per_chapter_outputs,
            "chapter_structure": chapter_structure_per_chapter_outputs,
        }.get(task)
        # 扁平化产物文件名含章节标题，从单章 chapters[0] 取标题段。
        name = _title_slug(chapters[0]) if chapters else ""
        out_list = per_chapter_outputs(per_chapter_seq, name) if per_chapter_outputs else []
        for out in out_list:
            lines.append(f"- {out}")
    else:
        outputs = task_info.get("outputs") or []
        if outputs:
            for out in outputs:
                lines.append(f"- {out}")
        else:
            lines.append("- 用户自定义输出路径")
    lines.append("")

    target_material = []
    if targets:
        target_material.append("## 指定目标元素JSON片段")
        found = select_target_items(story, targets)
        if not found:
            target_material.append("未找到指定目标元素。")
        for type_name, name, item in found:
            target_material.extend([f"### {type_name}:{name}", "```json",
                                    json.dumps(item, ensure_ascii=False, indent=2), "```", ""])

    lines.append(output_contract(task))
    lines.append("")
    # All facts are in the splittable material section, not an unbounded header.
    lines.extend(["## 章节材料", "### 完整历史故事结构", "```json",
                  json.dumps(story, ensure_ascii=False, indent=2), "```", ""])
    lines.extend(target_material)
    if task in STRUCTURE_TASKS and per_chapter_seq is None:
        lines.append(build_structure_context(project_dir, task, chapters, max_pack_chars=max_pack_chars))
    for ch in chapters:
        lines.append(build_chapter_block(project_dir, ch, task, "full", max_original_chars, max_analysis_chars))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def split_rendered_pack(content: str, budget: int) -> List[str]:
    """Preserve every material character while repeating the complete contract."""
    if budget <= 0 or len(content) <= budget:
        return [content]
    markers = [marker for marker in ("## 章节材料\n", "## 全书级分析材料\n") if marker in content]
    if not markers:
        raise ValueError("任务包超过预算且没有可分包的材料段，请提高 max_pack_chars")
    marker = min(markers, key=content.index)
    header, _, material = content.partition(marker)
    # Reserve enough room for continuation metadata without changing the contract.
    capacity = budget - len(header) - len(marker) - 256
    if capacity < 512:
        raise ValueError("任务契约与故事索引已占满预算，请提高 max_pack_chars；未删除分析材料")
    pieces = [material[start:start + capacity] for start in range(0, len(material), capacity)]
    return [header + f"\n材料连续片段 {i}/{len(pieces)}：本包只分析以下片段，汇总时须覆盖全部片段；片段可能在段落或表格中间衔接。\n"
            + marker + piece for i, piece in enumerate(pieces, 1)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成全书/局部分析任务包")
    parser.add_argument("project_dir", help="拆书项目目录")
    parser.add_argument("--task", choices=sorted(TASKS.keys()), required=True, help="分析任务类型")
    parser.add_argument("--chapters", default="all", help="章节范围，如 all、1-50、1,3,8-12")
    parser.add_argument("--targets", nargs="*", default=[], help="指定结构元素，如 角色:林婉 事件:入门试炼")
    parser.add_argument("--question", default="", help="自定义分析问题，task=custom时尤其有用")
    parser.add_argument("--include-original", choices=["auto", "none", "sample", "full"], default="auto", help="兼容旧选项；所有模式均保留完整原文，超限分层处理")
    parser.add_argument("--max-pack-chars", type=int, default=70000, help="单个任务包最大字符数，超出自动分包")
    parser.add_argument("--max-original-chars", type=int, default=6000, help="兼容旧配置；原文保留全文，超限分层处理")
    parser.add_argument("--max-analysis-chars", type=int, default=12000, help="兼容旧配置；章节分析保留全文，超限通过分包处理")
    parser.add_argument("--story-summary-max-items", type=int, default=0, help="兼容旧配置；历史档案完整分层处理，不再按条目截断")
    parser.add_argument("--context-budget-chars", type=int, default=160000, help="兼容旧字符预算；模型容量由统一 Token 设置控制")
    parser.add_argument("--out-dir", default="", help="输出目录；默认 全书分析/_任务包/<timestamp>_<task>")
    parser.add_argument("--per-chapter", action="store_true", help="每个章节生成一份独立任务包（visual_assets / chapter_structure）")
    parser.add_argument("--aggregate", action="store_true", help="把分章产物合并为顶层五件套（仅 visual_assets）")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir)
    if not project_dir.is_dir():
        print(f"错误: 项目目录不存在: {project_dir}")
        return 1

    if args.per_chapter and args.task not in {"visual_assets", "chapter_structure"}:
        print("错误: --per-chapter 仅支持 --task visual_assets 或 chapter_structure")
        return 1
    if args.aggregate and args.task != "visual_assets":
        print("错误: --aggregate 当前仅支持 --task visual_assets")
        return 1
    if args.per_chapter and args.aggregate:
        print("错误: --per-chapter 和 --aggregate 不能同时使用")
        return 1
    if args.task == "chapter_structure" and not args.per_chapter:
        print("错误: --task chapter_structure 必须配合 --per-chapter 使用(每章独立产物)")
        return 1

    index = load_index(project_dir)
    if not index:
        print("错误: 未找到章节索引或章节文件，请先完成章节拆分。")
        return 1
    all_seq = [int(ch["seq"]) for ch in index]

    if args.aggregate:
        # 汇总使用显式章节范围；默认 all 保持原有全书行为。
        selected_seq = set(parse_chapter_spec(args.chapters, all_seq))
        return _build_visual_aggregate_pack(
            project_dir, args,
            [ch for ch in index if int(ch["seq"]) in selected_seq], all_seq,
        )

    selected_seq = set(parse_chapter_spec(args.chapters, all_seq))
    chapters = [ch for ch in index if int(ch["seq"]) in selected_seq and not ch.get("is_preface")]
    if not chapters:
        print("错误: 没有匹配的章节。")
        return 1

    if args.per_chapter:
        if args.task == "chapter_structure":
            return _build_chapter_structure_per_chapter_packs(project_dir, args, chapters)
        return _build_visual_per_chapter_packs(project_dir, args, chapters)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_{args.task}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.task == "narrative_structure":
        # narrative_structure：按分章 章节结构.md 分批 + partial→reduce（复用多包 reduce 机制）
        batches = split_narrative_batches(project_dir, chapters, args.max_pack_chars)
    elif args.task in STRUCTURE_TASKS:
        # 完整结构资料与章节证据统一交给下方连续分片器，不做摘要降载。
        batches = [chapters]
    else:
        batches = split_batches(
            chapters,
            project_dir,
            args.task,
            args.include_original,
            args.max_pack_chars,
            args.max_original_chars,
            args.max_analysis_chars,
        )

    pack_files = []
    for i, batch in enumerate(batches, 1):
        content = build_pack(
            project_dir,
            args.task,
            batch,
            args.targets,
            args.question,
            args.include_original,
            args.max_original_chars,
            args.max_analysis_chars,
            i,
            len(batches),
            story_summary_max_items=args.story_summary_max_items,
            context_budget_chars=args.context_budget_chars,
            max_pack_chars=args.max_pack_chars,
        )
        try:
            pieces = split_rendered_pack(content, args.max_pack_chars)
        except ValueError as exc:
            # The execution layer can page even a large story index. Preserve the
            # complete package and delegate capacity adaptation instead of asking
            # the user to increase a character limit and redispatch manually.
            print(f"分析材料转入自动分层处理：{exc}")
            pieces = [content]
        for piece in pieces:
            path = out_dir / f"pack_{len(pack_files) + 1:03d}.md"
            path.write_text(piece, encoding="utf-8")
            hierarchy = {"sources": [str(path.resolve())],
                         "contract": output_contract(args.task) + "\n" + TASKS[args.task]["goal"]
                                     + "\n用户指定问题：" + (args.question or "无")
                                     + "\n章节范围：" + args.chapters
                                     + "\n指定目标：" + json.dumps(args.targets or [], ensure_ascii=False)}
            path.with_suffix(".hierarchy.json").write_text(json.dumps(hierarchy, ensure_ascii=False, indent=2), encoding="utf-8")
            pack_files.append(str(path.relative_to(project_dir)))

    reduce_path = out_dir / "reduce_prompt.md"
    reduce_path.write_text(reduce_contract(args.task), encoding="utf-8")

    manifest = {
        "created_at": timestamp,
        "task": args.task,
        "task_name": TASKS[args.task]["name"],
        "chapters": [int(ch["seq"]) for ch in chapters],
        "chapter_range": args.chapters,
        "targets": args.targets,
        "question": args.question,
        "include_original": "full",
        "max_pack_chars": args.max_pack_chars,
        "pack_count": len(pack_files),
        "packs": pack_files,
        "reduce_prompt": str(reduce_path.relative_to(project_dir)),
        "target_outputs": TASKS[args.task].get("outputs", []),
        "recommended_workflow": [
            "逐个读取 pack_*.md，生成对应 partial_result_*.md 或 partial_result_*.json",
            "如果 pack_count > 1，读取 reduce_prompt.md + 所有 partial_result，汇总到 target_outputs",
            "把最终报告中的关键结论回指章节/原文证据/故事结构元素",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"分析任务包已生成: {out_dir}")
    print(f"任务: {TASKS[args.task]['name']} | 章节数: {len(chapters)} | 分包数: {len(pack_files)}")
    print(f"清单: {manifest_path}")
    for p in pack_files:
        print(f"- {p}")
    print(f"- {manifest['reduce_prompt']}")
    return 0


def _build_visual_per_chapter_packs(project_dir: Path, args: argparse.Namespace, chapters: List[Dict[str, Any]]) -> int:
    """为每个选定章节生成一份独立 visual_assets 任务包。"""
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_visual_assets_per_chapter"
    out_root.mkdir(parents=True, exist_ok=True)

    pack_records: List[Dict[str, Any]] = []
    for ch in chapters:
        seq = int(ch["seq"])
        # 跳过已有完整分章产物的章节（断点续传；按 seq 前缀 glob，标题无关）
        if _visual_complete(project_dir, seq):
            print(f"跳过第{seq:03d}章：分章视觉资产已存在。")
            continue
        name = _title_slug(ch)
        pack_content = build_pack(
            project_dir,
            "visual_assets",
            [ch],
            args.targets,
            args.question,
            args.include_original,
            args.max_original_chars,
            args.max_analysis_chars,
            1,
            1,
            per_chapter_seq=seq,
            story_summary_max_items=args.story_summary_max_items,
            context_budget_chars=args.context_budget_chars,
        )
        pack_path = out_root / f"pack_ch{seq:03d}.md"
        pack_path.write_text(pack_content, encoding="utf-8")
        reduce_path = out_root / f"reduce_prompt_ch{seq:03d}.md"
        reduce_path.write_text(reduce_contract_visual_per_chapter(seq, name), encoding="utf-8")
        pack_records.append({
            "seq": seq,
            "pack": str(pack_path.relative_to(project_dir)),
            "reduce_prompt": str(reduce_path.relative_to(project_dir)),
            "outputs": visual_assets_per_chapter_outputs(seq, name),
        })

    manifest = {
        "created_at": timestamp,
        "task": "visual_assets",
        "mode": "per_chapter",
        "chapter_range": args.chapters,
        "include_original": "full",
        "pack_count": len(pack_records),
        "packs": pack_records,
        "recommended_workflow": [
            "对每个 pack_chNNN.md：读取并按 reduce_prompt_chNNN.md 的契约直接写出 全书分析/视觉资产/分章/ 下 chNNN_<标题>_*.md 五个 MD",
            "完成所有章节后，可运行 `analysis-pack --task visual_assets --aggregate` 生成顶层汇总",
        ],
    }
    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"视觉资产分章任务包已生成: {out_root}")
    print(f"待处理章节数: {len(pack_records)}（已跳过已完成章节）")
    print(f"清单: {manifest_path}")
    for r in pack_records:
        print(f"- {r['pack']}")
    return 0


def _build_chapter_structure_per_chapter_packs(project_dir: Path, args: argparse.Namespace, chapters: List[Dict[str, Any]]) -> int:
    """为每个选定章节生成一份独立 chapter_structure 任务包。"""
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_chapter_structure_per_chapter"
    out_root.mkdir(parents=True, exist_ok=True)

    pack_records: List[Dict[str, Any]] = []
    for ch in chapters:
        seq = int(ch["seq"])
        # 跳过已有产物的章节（断点续传；按 seq 前缀 glob，标题无关）
        if _existing_chapter_structure_path(project_dir, seq) is not None:
            print(f"跳过第{seq:03d}章：章节结构.md 已存在。")
            continue
        name = _title_slug(ch)
        pack_content = build_pack(
            project_dir,
            "chapter_structure",
            [ch],
            args.targets,
            args.question,
            args.include_original,
            args.max_original_chars,
            args.max_analysis_chars,
            1,
            1,
            per_chapter_seq=seq,
            story_summary_max_items=args.story_summary_max_items,
            context_budget_chars=args.context_budget_chars,
        )
        pack_path = out_root / f"pack_ch{seq:03d}.md"
        pack_path.write_text(pack_content, encoding="utf-8")
        reduce_path = out_root / f"reduce_prompt_ch{seq:03d}.md"
        reduce_path.write_text(reduce_contract_chapter_structure_per_chapter(seq, name), encoding="utf-8")
        pack_records.append({
            "seq": seq,
            "pack": str(pack_path.relative_to(project_dir)),
            "reduce_prompt": str(reduce_path.relative_to(project_dir)),
            "outputs": chapter_structure_per_chapter_outputs(seq, name),
        })

    manifest = {
        "created_at": timestamp,
        "task": "chapter_structure",
        "mode": "per_chapter",
        "chapter_range": args.chapters,
        "include_original": "full",
        "pack_count": len(pack_records),
        "packs": pack_records,
        "recommended_workflow": [
            "对每个 pack_chNNN.md：读取并按 reduce_prompt_chNNN.md 的契约直接写出 全书分析/故事结构/分章/chNNN_<标题>_章节结构.md",
            "完成所有章节后，可执行 `--task narrative_structure` 做全书级汇总",
        ],
    }
    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"故事结构分章任务包已生成: {out_root}")
    print(f"待处理章节数: {len(pack_records)}（已跳过已完成章节）")
    print(f"清单: {manifest_path}")
    for r in pack_records:
        print(f"- {r['pack']}")
    return 0


def _build_visual_aggregate_pack(project_dir: Path, args: argparse.Namespace, index: List[Dict[str, Any]], all_seq: List[int]) -> int:
    """生成顶层视觉资产汇总任务包。"""
    per_chapter_root = project_dir / "全书分析" / "视觉资产" / "分章"
    if not per_chapter_root.is_dir():
        print(f"错误: 未找到分章视觉资产目录 {per_chapter_root}；请先用 --per-chapter 生成分章产物。")
        return 1

    available: List[int] = []
    missing: List[int] = []
    non_preface_seq = [int(ch["seq"]) for ch in index if not ch.get("is_preface")]
    for seq in non_preface_seq:
        # 按 seq 前缀 glob 判断五件套是否齐全（标题无关）
        if _visual_complete(project_dir, seq):
            available.append(seq)
        else:
            missing.append(seq)

    if not available:
        print("错误: 没有任何章节具备完整的分章视觉资产；请先用 --per-chapter 生成至少一章。")
        return 1

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_visual_assets_aggregate"
    out_root.mkdir(parents=True, exist_ok=True)

    inventory_lines = ["# 视觉资产分章产物清单", ""]
    for seq in available:
        inventory_lines.append(f"## 第{seq:03d}章")
        for p in _existing_visual_paths(project_dir, seq):
            inventory_lines.append(f"- {p.relative_to(project_dir)}")
            # The inventory doubles as complete bounded evidence; a path alone
            # cannot prove that an external worker read every chapter artifact.
            inventory_lines.append(p.read_text(encoding="utf-8"))
        inventory_lines.append("")
    if missing:
        inventory_lines.append("## 尚未生成分章产物的章节（aggregate 时将被跳过）")
        inventory_lines.append("、".join(f"第{s:03d}章" for s in missing))
        inventory_lines.append("")
    inventory_path = out_root / "inventory.md"
    inventory_path.write_text("\n".join(inventory_lines), encoding="utf-8")

    reduce_path = out_root / "reduce_prompt.md"
    reduce_path.write_text(reduce_contract_visual_aggregate(), encoding="utf-8")

    manifest = {
        "created_at": timestamp,
        "task": "visual_assets",
        "mode": "aggregate",
        "chapter_range": args.chapters,
        "chapters_available": available,
        "chapters_missing": missing,
        "inventory": str(inventory_path.relative_to(project_dir)),
        "reduce_prompt": str(reduce_path.relative_to(project_dir)),
        "target_outputs": TASKS["visual_assets"]["outputs"],
        "recommended_workflow": [
            "读取 inventory.md 列出的所有分章产物",
            "按 reduce_prompt.md 的契约合并为顶层五件套（顶层文件存在则覆盖）",
        ],
    }
    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"视觉资产顶层汇总任务包已生成: {out_root}")
    print(f"可合并章节数: {len(available)} | 缺失: {len(missing)}")
    print(f"清单: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
