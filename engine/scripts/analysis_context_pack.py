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
        "outputs": ["全书分析/故事结构/分章/ch{NNN}/章节结构.md"],
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


def visual_assets_per_chapter_outputs(seq: int) -> List[str]:
    """单章视觉资产五个产物的相对路径。"""
    base = f"全书分析/视觉资产/分章/ch{seq:03d}"
    return [f"{base}/{name}" for name in VISUAL_ASSETS_PER_CHAPTER_FILES]


def chapter_structure_per_chapter_outputs(seq: int) -> List[str]:
    """单章故事结构产物的相对路径，与视觉资产分章对齐。"""
    return [f"全书分析/故事结构/分章/ch{seq:03d}/章节结构.md"]


NARRATIVE_STRUCTURE_BACKGROUND_SPECS = [
    ("全书分析/剧情结构/章节梗概汇总.md", "章节梗概汇总"),
    ("全书分析/剧情结构/全书大纲.md", "全书大纲"),
    ("全书分析/剧情结构/剧情线总表.md", "剧情线总表"),
    ("全书分析/剧情结构/剧情线交汇矩阵.md", "剧情线交汇矩阵"),
]


def build_narrative_structure_background(project_dir: Path, chapters: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """收集 narrative_structure 全书任务所需的上游材料。

    优先级（与 `references/analysis_narrative_structure.md` 一致）：
    分章 章节结构.md > 章节梗概汇总 > 全书大纲 > 剧情线总表 > 剧情线交汇矩阵

    上游产物缺失时显式标注「未生成」，不静默吞掉。
    """
    lines: List[str] = ["## 全书背景资料", ""]

    # 分章 章节结构.md
    lines.append("### 分章 章节结构.md（chapter_structure 产物）")
    found_any = False
    for ch in chapters:
        seq = int(ch["seq"])
        rel = chapter_structure_per_chapter_outputs(seq)[0]
        path = project_dir / rel
        if path.is_file() and path.stat().st_size > 0:
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


def read_text(path: Path, max_chars: Optional[int] = None) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + f"\n\n……【已截断，原文/分析共 {len(text)} 字】"
    return text


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
    if mode == "none" or not text:
        return ""
    if mode == "full":
        if max_chars > 0 and len(text) > max_chars:
            return text[:max_chars] + f"\n\n……【原文已截断，完整长度 {len(text)} 字】"
        return text
    # sample: beginning + middle + ending
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    part = max_chars // 3
    mid = len(text) // 2
    return (
        text[:part]
        + "\n\n……【中段抽样】……\n\n"
        + text[max(0, mid - part // 2): mid + part // 2]
        + "\n\n……【末段抽样】……\n\n"
        + text[-part:]
    )


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
        for item in items[:max_items]:
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
        if len(items) > max_items:
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
    delta_path = project_dir / "章节处理" / f"{base}.json"

    lines = []
    lines.append(f"### 第{ch['seq']:03d}章｜{ch.get('title') or ch.get('source_title') or base}")
    lines.append("")
    lines.append("#### 章节分析MD")
    lines.append("```markdown")
    lines.append(read_text(analysis_path, max_analysis_chars) or "【未找到章节分析MD】")
    lines.append("```")
    lines.append("")

    if include_original != "none":
        raw = read_text(split_path)
        sampled = sample_original(raw, include_original, max_original_chars)
        lines.append(f"#### 原文{'抽样' if include_original == 'sample' else ''}")
        lines.append("```markdown")
        lines.append(sampled or "【未找到原文章节】")
        lines.append("```")
        lines.append("")

    if task in {"characters", "plot", "report", "custom", "worldview", "settings", "plotlines", "outline", "detailed_outline", "visual_assets", "chapter_structure", "narrative_structure"}:
        delta = load_json(delta_path)
        lines.append("#### 本章Delta")
        lines.append("```json")
        lines.append(json.dumps(delta, ensure_ascii=False, indent=2) if delta is not None else "【未找到本章Delta】")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def output_contract(task: str) -> str:
    if task == "summary":
        return """# 输出要求\n\n输出 Markdown 表格：\n\n| 章节 | 标题 | 500字梗概 | 章节功能 | 主要人物 | 核心冲突 | 证据章节 |\n|------|------|-----------|----------|----------|----------|----------|\n\n要求：\n- 梗概只写本章明确发生的剧情。\n- 章节功能可以是：铺垫/转折/冲突升级/人物塑造/伏笔埋设/伏笔回收/高潮/过渡。\n- 核心冲突必须具体，不要泛写“主角遇到困难”。\n"""
    if task == "characters":
        return """# 输出要求\n\n请按三个部分输出：\n\n## 人物档案增量\n- 每个人物包含：身份定位、性格特征、核心动机、能力/资源、变化轨迹、原文证据章节。\n\n## 人物关系增量\n- 格式：A -> B：关系类型；关系状态；变化章节；证据。\n\n## 人物提及JSON\n```json\n{\n  "人物提及": [\n    {"人物": "", "章节": "第001章", "作用": "出场/推动事件/关系变化/伏笔", "证据": "原文短句或章节分析依据"}\n  ]\n}\n```\n\n要求：\n- 不要为了完整而补不存在的关系。\n- 不确定身份写“待确认”，不要写成事实。\n"""
    if task == "plot":
        return """# 输出要求\n\n请按三个部分输出：\n\n## 剧情线索\n- 主线、支线分别列出：起点、推进节点、转折、当前状态、证据章节。\n\n## 伏笔追踪\n| 伏笔 | 埋设章节 | 表现 | 当前状态 | 回收章节/待回收 | 证据 |\n\n## 冲突图谱\n- 人物冲突、阵营冲突、目标冲突、信息差冲突分别列出。\n\n要求：\n- 伏笔必须有原文表现或章节分析支持。\n- 普通气氛描写不要强行当伏笔。\n"""
    if task == "style":
        return """# 输出要求\n\n请按三个部分输出：\n\n## 文风分析\n- 叙述视角、句式节奏、描写偏好、对白风格、情绪调性、类型化表达。\n\n## 节奏分析\n- 按章节列节奏：信息密度、冲突强度、情绪强度、爽点/压抑点、转场方式。\n\n## 高光场景\n| 场景 | 章节 | 高光类型 | 为什么有效 | 可复用写法 | 原文证据 |\n\n要求：\n- 必须结合原文抽样，不只根据剧情概括判断文风。\n"""
    if task == "report":
        return """# 输出要求\n\n输出《拆书总报告》，建议结构：\n\n1. 全书一句话概括\n2. 故事核心卖点\n3. 世界观/设定框架\n4. 主要人物与关系\n5. 主线剧情与阶段结构\n6. 伏笔与回收\n7. 冲突系统\n8. 文风与节奏\n9. 高光场景与可复用写法\n10. 结构问题与风险\n11. 后续改编/续写可用素材\n\n要求：\n- 必须引用章节或结构JSON元素作为依据。\n- 不能只写空泛评价。\n"""
    if task == "worldview":
        return """# 输出要求\n\n输出《世界观档案》，按以下要素表输出，每要素必须有原文证据章节：\n\n| 要素 | 内容 | 原文证据 | 自洽性检查 |\n|------|------|----------|------------|\n\n要素清单：时代背景、地理环境、社会制度、势力格局、历史纪年。\n\n要求：\n- 引用阵营集/地点集元素时用 `类型:名称` 格式。\n- 势力格局需引用 `阵营集` 元素并标注敌友关系证据。\n- 孤立设定（只提一次无后续影响）标"疑似孤立"。\n- 不确定写"待确认"，不编造原文未明确的世界观要素。\n"""
    if task == "settings":
        return """# 输出要求\n\n输出《设定档案》，按以下要素表输出，每要素必须有原文证据章节：\n\n| 要素 | 内容 | 原文证据 | 自洽性检查 |\n|------|------|----------|------------|\n\n要素清单：力量体系、功法武技、特殊种族、职业体系、物品法宝、世界规则。\n\n要求：\n- 引用物品集/其他事项集/角色集元素时用 `类型:名称` 格式。\n- 力量体系需说明代价/限制；无代价标"待确认"。\n- 功法/物品的品级体系需检查是否前后矛盾。\n- 孤立设定（只提一次无后续影响）标"疑似孤立"。\n- 不确定写"待确认"，不编造原文未明确的设定。\n"""
    if task == "plotlines":
        return """# 输出要求\n\n请按两个部分输出：\n\n## 剧情线总表\n\n| 线编号 | 类型 | 线名 | 涉及角色 | 涉及事件 | 涉及线索 | 起 | 承 | 转 | 合 | 优先级 | 推进节奏 | 证据章节 |\n|--------|------|------|----------|----------|----------|------|------|------|------|--------|----------|----------|\n\n- 类型：MAIN(主线)/SUB(支线)/DARK(暗线伏笔)/GROW(角色成长)/EMO(情感线)\n- 线编号：`PL-MAIN-001`、`PL-SUB-002`、`PL-DARK-001` 等\n- 涉及角色/事件/线索用 `类型:名称` 格式引用故事结构元素\n- 起承转合填章节号+触发事件\n- 优先级：P0(核心)/P1(重要)/P2(次要)\n- 推进节奏：集中推进/间歇推进/贯穿全程\n\n## 剧情线交汇矩阵\n\n行列为各剧情线编号，单元格标注交汇章节和交汇事件。\n\n要求：\n- 每条线必须有原文事件支撑，不能凭空归纳。\n- 交汇点必须有具体章节和事件证据，无证据标"待确认"。\n- 暗线/伏笔线必须有早期细节作为铺垫证据。\n"""
    if task == "outline":
        return """# 输出要求\n\n输出《全书大纲》，按卷/篇章级反推叙事框架：\n\n| 卷号 | 章节范围 | 卷名 | 核心冲突 | 关键转折 | 高潮事件 | 结局走向 | 主要角色出入场 | 重要设定/道具引入 | 伏笔操作 |\n|------|----------|------|----------|----------|----------|----------|----------------|-------------------|----------|\n\n要求：\n- 优先尊重原文分卷/分篇标记；无分卷则按核心冲突变化划分篇章边界。\n- 卷边界必须有明确的冲突转换或场景转换依据，不能机械按章节数切分。\n- 核心冲突/转折/高潮必须引用 `事件集` 元素（`事件:名称`）。\n- 角色出入场引用 `角色集` 元素，需有首次章节/最近章节证据。\n- 设定/道具引入引用 `物品集` 元素。\n- 伏笔操作引用 `线索集` 元素，标注埋设/回收。\n- 不确定写"待确认"，不编造原文未明确的卷级结构。\n"""
    if task == "visual_assets":
        return """# 输出要求

请按五个部分输出视觉资产资料，所有画面判断都必须有章节号、事件或原文证据。

## 视觉资产清单

| 资产编号 | 对应事件 | 章节 | 视觉等级 | 画面类型 | 视觉用途 | 核心画面 | 情绪氛围 | 证据 |
|----------|----------|------|----------|----------|----------|----------|----------|------|

## 关键场景分镜表

| 分镜编号 | 对应事件 | 镜头景别 | 人物/主体 | 动作/状态 | 场景环境 | 情绪 | 转场/节奏 |
|----------|----------|----------|-----------|-----------|----------|------|----------|

## AI绘图提示词素材

按资产编号输出：人物、地点、动作、构图、氛围、光线、关键物品、需要避免的无证据内容。不要补原文没有的服装、长相、道具。

## 角色外观一致性表

| 角色 | 已有外观证据 | 待确认外观 | 可用于角色卡的稳定元素 | 证据章节 |
|------|--------------|------------|--------------------------|----------|

## 场景氛围表

| 地点/场景 | 氛围关键词 | 常见人物关系 | 适合画面类型 | 证据章节 |
|-----------|------------|--------------|--------------|----------|

要求：
- 优先选择视觉等级 S/A 的事件。
- `画面类型` 与 `视觉用途` 尽量使用 taxonomy 中的受控词。
- 当事件的 `标签集` 或 `详情.补充标签`（v12.1 起标签压缩会把带前缀标签下层到这里，值是 JSON 字符串数组）已含 `画面类型:*` 或 `视觉用途:*`，必须沿用既有标注，不要重新编一份；结构索引中事件名后的 `[画面类型:…,视觉用途:…]` 即为已存在的标注。
- 没有原文证据的外观、服装、道具、环境细节必须写”待确认”。
- 输出的是生产资料，不是成品图片描述；不要把小说没有写的东西变成事实。
"""
    if task == "chapter_structure":
        return """# 输出要求

本任务包**只针对单章**输出一份 `章节结构.md`，结构固定为五个小节，按顺序写。

## 1. 三幕式定位
- 所处幕：第一幕（建置 Setup）/ 第二幕（对抗 Confrontation）/ 第三幕（解决 Resolution）/ 过渡章（无节拍）
- 节拍点（仅当本章为节拍章时填写）：钩子 Hook / 激励事件 Inciting Incident / 第一情节点 FPP / 第一夹点 / 中点 Midpoint / 第二夹点 / 第二情节点 SPP / 高潮 Climax / 收束 Resolution
- 节拍证据：引用原文段落或本章分析MD相应位置；非节拍章写"过渡推进，无显著节拍点"

## 2. 故事七要素 · 本章信号
逐条列出；本章对该要素无推进时写"本章无推进"，禁止编造。

1. 主角
2. 缺陷
3. 有利的故事环境
4. 反面角色
5. 主角的盟友
6. 改变人生的事件 · 危险
7. 整合故事要素

## 3. 故事力学 · 本章贡献（Brooks 六力学）
仅列出本章有贡献的项,其余省略;每项后写"本章贡献 + 原文证据"。

- 强迫性前提（Compelling Premise）
- 戏剧张力（Dramatic Tension）
- 节奏（Pacing）
- 英雄共情（Hero Empathy）
- 代入体验（Vicarious Experience）
- 叙事策略（Narrative Strategy）

## 4. 故事工程学 · 本章执行（六大核心能力）
仅列出本章有可观察执行的项。

- 概念（Concept）
- 人物（Character）
- 主题（Theme）
- 结构（Structure）
- 场景执行（Scene Execution）
- 写作声音（Writing Voice）

## 5. 小说骨架 · 本章信号
- Logline 推进：本章动了"谁 / 想要什么 / 阻碍 / 代价"中的哪一项,引用原文证据
- 网文骨架命中：金手指 / 升级 / 主角光环 / 装逼打脸 / 套路 / 核心爽点 / 反派配置 中本章命中的点;未命中省略
- Freytag 段位：序幕 / 上升 / 高潮 / 下降 / 结局

## 强制要求

1. 五小节顺序固定,缺数据写"本章无推进"或"过渡推进,无显著节拍点",不要省略小节。
2. 所有结论必须引用本章原文短句或本章分析MD作为证据,不确定写"待确认"。
3. 禁止跨章归纳,例如"主角自第3章后……"这类描述。
4. 不要修改 `故事结构_增量.json` 或其他章节文件。
"""
    if task == "narrative_structure":
        return """# 输出要求

请按 7 份产物分别输出 Markdown,每份产物独立成文件:

## 三幕式结构图.md

| 幕 | 节拍 | 章节区间 | 关键事件 | 证据 |
|----|------|----------|----------|------|

- 幕:第一幕(建置)/ 第二幕(对抗)/ 第三幕(解决)
- 节拍:钩子 / 激励事件 / FPP / 第一夹点 / 中点 / 第二夹点 / SPP / 高潮 / 收束
- 关键事件用 `事件:名称` 引用 `事件集` 元素

## Brooks四部分结构图.md

双表对齐:

第一表:

| 部分 | 章节区间 | 角色任务 | 关键事件 | 与三幕式对齐处 | 证据 |
|------|----------|----------|----------|----------------|------|

部分:Setup(铺垫)/ Response(反应)/ Attack(进攻)/ Resolution(决战)

第二表 — 五大里程碑:

| 里程碑 | 章节号 | 触发事件 | 张力变化 | 证据 |
|--------|--------|----------|----------|------|

里程碑:Hook / FPP / Midpoint / SPP / Climax

## Freytag五段结构图.md

| 段位 | 章节区间 | 关键事件 | 与三幕式对齐处 | 证据 |
|------|----------|----------|----------------|------|

段位:序幕 Exposition / 上升 Rising Action / 高潮 Climax / 下降 Falling Action / 结局 Dénouement

## 故事七要素档案.md

逐项独立一节,每节字段统一:初始状态、终态、演变轨迹、原文证据章节;不确定写"待确认"。

1. 主角(引用 `角色:名称`)
2. 缺陷
3. 有利的故事环境(引用 `阵营集`/`地点集`)
4. 反面角色(引用 `角色:名称`)
5. 主角的盟友(引用 `角色:名称` 列表)
6. 改变人生的事件 · 危险(引用 `事件:名称`)
7. 整合故事要素(主题统合)

## 故事力学评估.md

Brooks 六力学逐项评估:

| 力学项 | 强度(强/中/弱/待确认) | 评估说明 | 证据章节 | 风险/短板 |
|--------|-----------------------|----------|----------|-----------|

力学项:强迫性前提 / 戏剧张力 / 节奏 / 英雄共情 / 代入体验 / 叙事策略

## 故事工程学评估.md

六大核心能力逐项:

| 能力 | 强度 | 评估说明 | 证据 | 短板诊断 |
|------|------|----------|------|----------|

能力:概念 / 人物 / 主题 / 结构 / 场景执行 / 写作声音

## 小说骨架.md

```
## 1. 故事核 / Logline
一句话:在 <时代/地点>,<主角>因为 <激励事件>,为了 <欲望/目标>,必须对抗 <反派/阻碍>,否则将付出 <代价>。

补充:主题句、类型标签、目标读者、证据章节

## 2. 网文骨架零件
- 金手指 / 升级体系 / 主角光环点 / 装逼打脸节奏 / 套路类型 / 核心爽点 / 反派配置
- 引用 `角色:名称` 与章节证据

## 3. 节拍对照表
| 章节区间 | 三幕式节拍 | Brooks里程碑 | Freytag段位 |
冲突处标"待确认",禁止强行统一。
```

## 强制要求

1. 三种节拍图必须互相对齐,冲突处标"待确认"。
2. 所有结论必须引用章节号、原文证据或 `类型:名称` 元素;不确定写"待确认"。
3. 不写入 `故事结构_增量.json`,不引入新编号。
4. 7 份产物全部输出;某项无内容写"本书无对应内容"而非省略文件。
"""
    if task == "detailed_outline":
        return """# 输出要求\n\n输出《章节细纲》，每章一行：\n\n| 章节号 | 章节标题 | 场景列表 | 出场人物 | 章节目标 | 核心冲突 | 关键事件 | 伏笔操作 | 章节钩子 | 氛围基调 | 字数 | 推进剧情线 |\n|--------|----------|----------|----------|----------|----------|----------|----------|----------|----------|------|------------|\n\n要求：\n- 覆盖全部已分析章节，不能跳章。\n- 章节号用固定宽度（如 0043）。\n- 出场人物/关键事件引用 `角色集`/`事件集` 元素（`类型:名称`）。\n- 伏笔操作标注埋设/暗示/回收，引用 `线索集` 元素。\n- 章节钩子必须是原文实际存在的悬念，不能编造。\n- 字数填原文实际字数，不估算。\n- 推进剧情线填 `PL-xxxx-xxx` 编号（需先执行 plotlines 任务）。\n- 不确定写"待确认"。\n\n关键章节（高潮/转折/重要伏笔章节）可在表格后补充场景级细纲：\n\n| 场景编号 | 所属章节 | 时间 | 地点 | 出场人物 | 场景冲突 | 场景功能 | 原文证据 |\n|----------|----------|------|------|----------|----------|----------|----------|\n"""
    return """# 输出要求\n\n根据用户指定问题输出分析。所有结论都必须标注证据来源：章节号、原文片段、章节分析或故事结构元素。\n"""


def reduce_contract(task: str) -> str:
    return f"""# Reduce 汇总任务\n\n你将读取多个 pack 的分批分析结果，请合并为任务 `{task}` 的最终报告。\n\n要求：\n1. 合并重复观点。\n2. 保留证据章节。\n3. 对冲突结论标记“待确认”，不要强行统一。\n4. 输出到 manifest.json 指定的目标文件。\n5. 不要凭空新增前面分批结果里没有依据的内容。\n"""


def reduce_contract_visual_per_chapter(seq: int) -> str:
    outs = visual_assets_per_chapter_outputs(seq)
    bullet = "\n".join(f"- {p}" for p in outs)
    return f"""# 单章视觉资产产出任务

本任务包只针对**第{seq:03d}章**生成视觉资产。

## 目标输出（五个文件，全部位于 `全书分析/视觉资产/分章/ch{seq:03d}/`）

{bullet}

## 强制要求

1. 五个文件全部必须生成；某节本章无可写内容时，写一行 `本章无对应内容。` 而不是省略文件。
2. 所有结论必须引用本章原文短句或本章分析MD作为证据；禁止编造服装、外观、道具。
3. 禁止跨章归纳，例如"主角自第3章后……"这类描述。
4. 表格结构与全书版本一致，便于后续 aggregate 阶段直接拼接。
5. 不要写顶层 `全书分析/视觉资产/*.md`，那是 aggregate 阶段的产物。
"""


def reduce_contract_chapter_structure_per_chapter(seq: int) -> str:
    target = chapter_structure_per_chapter_outputs(seq)[0]
    return f"""# 单章故事结构产出任务

本任务包只针对**第{seq:03d}章**生成 `章节结构.md`。

## 目标输出(一个文件)

- {target}

## 章节结构.md 模板(五小节顺序固定)

```markdown
# 第{seq:03d}章 故事结构定位

## 1. 三幕式定位
- 所处幕:第一幕(建置 Setup)/ 第二幕(对抗 Confrontation)/ 第三幕(解决 Resolution)/ 过渡章(无节拍)
- 节拍点:钩子 / 激励事件 / FPP / 第一夹点 / 中点 / 第二夹点 / SPP / 高潮 / 收束;非节拍章写"过渡推进,无显著节拍点"
- 节拍证据:原文片段或本章分析MD

## 2. 故事七要素 · 本章信号
1. 主角
2. 缺陷
3. 有利的故事环境
4. 反面角色
5. 主角的盟友
6. 改变人生的事件 · 危险
7. 整合故事要素
每项写一行"本章推进 + 证据";本章无推进写"本章无推进"。

## 3. 故事力学 · 本章贡献(Brooks 六力学)
仅列出本章有贡献的项;每项后写"本章贡献 + 原文证据"。

## 4. 故事工程学 · 本章执行(六大核心能力)
仅列出本章有可观察执行的项。

## 5. 小说骨架 · 本章信号
- Logline 推进:动了"谁/想要/阻碍/代价"中的哪一项
- 网文骨架命中:金手指/升级/光环/装逼打脸/套路/爽点/反派 中本章命中的点
- Freytag 段位:序幕/上升/高潮/下降/结局
```

## 强制要求

1. 五小节必须按顺序齐全;本章无内容也要写"本章无推进"或"过渡推进,无显著节拍点"。
2. 所有结论必须引用本章原文短句或本章分析MD作为证据;禁止编造。
3. 禁止跨章归纳,例如"主角自第3章后……"这类描述。
4. 不要修改 `故事结构_增量.json` 或其他章节文件;只写目标文件本身。
"""


def reduce_contract_visual_aggregate() -> str:
    return """# 视觉资产顶层汇总任务

你将读取 `全书分析/视觉资产/分章/chNNN/` 下所有章节的五件套视觉资产，合并为全书顶层汇总：

- 全书分析/视觉资产/视觉资产清单.md
- 全书分析/视觉资产/关键场景分镜表.md
- 全书分析/视觉资产/AI绘图提示词素材.md
- 全书分析/视觉资产/角色外观一致性表.md
- 全书分析/视觉资产/场景氛围表.md

要求：

1. 顺序按章节号合并；表格行直接拼接，保留每行的章节列。
2. 角色外观一致性表需跨章去重并合并同一角色的不同章节证据；冲突描述标"待确认"。
3. AI绘图提示词素材按资产编号顺序合并；重复资产合并条目。
4. 合并过程中不得添加任何分章产物里没有依据的新内容。
5. 顶层汇总文件存在则覆盖；分章源文件不动。
"""


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


def build_pack(project_dir: Path, task: str, chapters: List[Dict[str, Any]], targets: List[str], question: str, include_original: str, max_original_chars: int, max_analysis_chars: int, pack_no: int, total_packs: int, per_chapter_seq: Optional[int] = None) -> str:
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
        out_list = per_chapter_outputs(per_chapter_seq) if per_chapter_outputs else []
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

    lines.append("## 当前故事结构摘要")
    lines.append(summarize_story_structure(story))
    lines.append("")

    if targets:
        lines.append("## 指定目标元素JSON片段")
        found = select_target_items(story, targets)
        if not found:
            lines.append("未找到指定目标元素。")
        for type_name, name, item in found:
            lines.append(f"### {type_name}:{name}")
            lines.append("```json")
            lines.append(json.dumps(item, ensure_ascii=False, indent=2))
            lines.append("```")
        lines.append("")

    lines.append(output_contract(task))
    lines.append("")
    if task == "narrative_structure" and per_chapter_seq is None:
        lines.append(build_narrative_structure_background(project_dir, chapters))
        lines.append("")
    lines.append("## 章节材料")
    for ch in chapters:
        lines.append(build_chapter_block(project_dir, ch, task, include_original, max_original_chars, max_analysis_chars))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成全书/局部分析任务包")
    parser.add_argument("project_dir", help="拆书项目目录")
    parser.add_argument("--task", choices=sorted(TASKS.keys()), required=True, help="分析任务类型")
    parser.add_argument("--chapters", default="all", help="章节范围，如 all、1-50、1,3,8-12")
    parser.add_argument("--targets", nargs="*", default=[], help="指定结构元素，如 角色:林婉 事件:入门试炼")
    parser.add_argument("--question", default="", help="自定义分析问题，task=custom时尤其有用")
    parser.add_argument("--include-original", choices=["none", "sample", "full"], default="sample", help="是否包含原文：none/sample/full")
    parser.add_argument("--max-pack-chars", type=int, default=70000, help="单个任务包最大字符数，超出自动分包")
    parser.add_argument("--max-original-chars", type=int, default=6000, help="每章原文最多放入字符数；sample模式会抽样")
    parser.add_argument("--max-analysis-chars", type=int, default=12000, help="每章分析MD最多放入字符数")
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
        return _build_visual_aggregate_pack(project_dir, args, index, all_seq)

    selected_seq = set(parse_chapter_spec(args.chapters, all_seq))
    chapters = [ch for ch in index if int(ch["seq"]) in selected_seq and not ch.get("is_preface")]
    if not chapters:
        print("错误: 没有匹配的章节。")
        return 1

    if args.per_chapter:
        if args.task == "chapter_structure":
            return _build_chapter_structure_per_chapter_packs(project_dir, args, chapters)
        return _build_visual_per_chapter_packs(project_dir, args, chapters)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_{args.task}"
    out_dir.mkdir(parents=True, exist_ok=True)

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
        )
        path = out_dir / f"pack_{i:03d}.md"
        path.write_text(content, encoding="utf-8")
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
        "include_original": args.include_original,
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
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_visual_assets_per_chapter"
    out_root.mkdir(parents=True, exist_ok=True)

    pack_records: List[Dict[str, Any]] = []
    for ch in chapters:
        seq = int(ch["seq"])
        # 跳过已有完整分章产物的章节（断点续传）
        existing = [project_dir / rel for rel in visual_assets_per_chapter_outputs(seq)]
        if all(p.is_file() and p.stat().st_size > 0 for p in existing):
            print(f"跳过第{seq:03d}章：分章视觉资产已存在。")
            continue
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
        )
        pack_path = out_root / f"pack_ch{seq:03d}.md"
        pack_path.write_text(pack_content, encoding="utf-8")
        reduce_path = out_root / f"reduce_prompt_ch{seq:03d}.md"
        reduce_path.write_text(reduce_contract_visual_per_chapter(seq), encoding="utf-8")
        pack_records.append({
            "seq": seq,
            "pack": str(pack_path.relative_to(project_dir)),
            "reduce_prompt": str(reduce_path.relative_to(project_dir)),
            "outputs": visual_assets_per_chapter_outputs(seq),
        })

    manifest = {
        "created_at": timestamp,
        "task": "visual_assets",
        "mode": "per_chapter",
        "chapter_range": args.chapters,
        "include_original": args.include_original,
        "pack_count": len(pack_records),
        "packs": pack_records,
        "recommended_workflow": [
            "对每个 pack_chNNN.md：读取并按 reduce_prompt_chNNN.md 的契约直接写出 全书分析/视觉资产/分章/chNNN/ 下的 5 个 MD",
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
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_chapter_structure_per_chapter"
    out_root.mkdir(parents=True, exist_ok=True)

    pack_records: List[Dict[str, Any]] = []
    for ch in chapters:
        seq = int(ch["seq"])
        existing = [project_dir / rel for rel in chapter_structure_per_chapter_outputs(seq)]
        if all(p.is_file() and p.stat().st_size > 0 for p in existing):
            print(f"跳过第{seq:03d}章：章节结构.md 已存在。")
            continue
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
        )
        pack_path = out_root / f"pack_ch{seq:03d}.md"
        pack_path.write_text(pack_content, encoding="utf-8")
        reduce_path = out_root / f"reduce_prompt_ch{seq:03d}.md"
        reduce_path.write_text(reduce_contract_chapter_structure_per_chapter(seq), encoding="utf-8")
        pack_records.append({
            "seq": seq,
            "pack": str(pack_path.relative_to(project_dir)),
            "reduce_prompt": str(reduce_path.relative_to(project_dir)),
            "outputs": chapter_structure_per_chapter_outputs(seq),
        })

    manifest = {
        "created_at": timestamp,
        "task": "chapter_structure",
        "mode": "per_chapter",
        "chapter_range": args.chapters,
        "include_original": args.include_original,
        "pack_count": len(pack_records),
        "packs": pack_records,
        "recommended_workflow": [
            "对每个 pack_chNNN.md：读取并按 reduce_prompt_chNNN.md 的契约直接写出 全书分析/故事结构/分章/chNNN/章节结构.md",
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
        outs = [project_dir / rel for rel in visual_assets_per_chapter_outputs(seq)]
        if all(p.is_file() and p.stat().st_size > 0 for p in outs):
            available.append(seq)
        else:
            missing.append(seq)

    if not available:
        print("错误: 没有任何章节具备完整的分章视觉资产；请先用 --per-chapter 生成至少一章。")
        return 1

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_visual_assets_aggregate"
    out_root.mkdir(parents=True, exist_ok=True)

    inventory_lines = ["# 视觉资产分章产物清单", ""]
    for seq in available:
        inventory_lines.append(f"## 第{seq:03d}章")
        for rel in visual_assets_per_chapter_outputs(seq):
            inventory_lines.append(f"- {rel}")
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
