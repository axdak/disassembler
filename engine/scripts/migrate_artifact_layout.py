#!/usr/bin/env python3
"""一次性迁移脚本：把旧布局产物搬到新布局（Part1 delta 子目录 + Part2 分章扁平化）。

迁移内容：
  1. 章节处理/第NNN章_*..json          -> 章节处理/Delta/第NNN章_*..json   （Delta JSON 单独子目录）
  2. 全书分析/故事结构/分章/chNNN/章节结构.md
     -> 全书分析/故事结构/分章/chNNN_<标题>_章节结构.md                      （扁平化+标题）
  3. 全书分析/视觉资产/分章/chNNN/<五件套>.md
     -> 全书分析/视觉资产/分章/chNNN_<标题>_<五件套>.md                     （扁平化+标题）
  迁移后删除空的 chNNN/ 目录。

标题取自 原文拆解/_索引.json 的 filename（与 engine analysis_context_pack._title_slug 一致：
filename 去掉 第NNN章_ 前缀、清洗非法字符）。

默认 dry-run（只打印不改动）；加 --apply 才实际移动文件。
幂等：旧路径不存在或新路径已存在时跳过。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Optional

from chapter_naming import title_slug_from_filename


def _title_slug(filename: str) -> str:
    """标题段提取（薄包装，逻辑见 chapter_naming.title_slug_from_filename）。"""
    return title_slug_from_filename(filename)


def load_seq_title_map(project_dir: Path) -> Dict[int, str]:
    """从 原文拆解/_索引.json 读 seq -> 标题段（filename 派生）。"""
    index_path = project_dir / "原文拆解" / "_索引.json"
    if not index_path.is_file():
        return {}
    import json
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    chapters = index.get("chapters", []) if isinstance(index, dict) else []
    mapping: Dict[int, str] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        seq = ch.get("seq")
        filename = ch.get("filename")
        if isinstance(seq, int) and isinstance(filename, str) and filename:
            mapping[seq] = _title_slug(filename)
    return mapping


def _seq_from_dirname(name: str) -> Optional[int]:
    m = re.match(r"^ch(\d+)$", name)
    return int(m.group(1)) if m else None


def migrate_delta(project_dir: Path, apply: bool) -> int:
    """章节处理/第*章_*.json -> 章节处理/Delta/。"""
    src_dir = project_dir / "章节处理"
    dst_dir = src_dir / "Delta"
    if not src_dir.is_dir():
        return 0
    moved = 0
    for p in sorted(src_dir.glob("第*章_*.json")):
        if not p.is_file():
            continue
        target = dst_dir / p.name
        if target.is_file():
            # 已迁移过（幂等）：旧文件若残留则移除，避免重复。
            if apply:
                p.unlink(missing_ok=True)
            print(f"  [skip] {p.name} 已在 Delta/")
            continue
        print(f"  [delta] {p.name} -> Delta/{p.name}")
        if apply:
            dst_dir.mkdir(parents=True, exist_ok=True)
            p.rename(target)
        moved += 1
    return moved


def _flatten_per_chapter(project_dir: Path, dimension_dir: Path, aspect_files: list[str], title_map: Dict[int, str], apply: bool) -> int:
    """通用扁平化：dimension_dir/chNNN/<aspect_files> -> dimension_dir/chNNN_<标题>_<aspect>。"""
    if not dimension_dir.is_dir():
        return 0
    moved = 0
    for ch_dir in sorted(dimension_dir.iterdir()):
        if not ch_dir.is_dir():
            continue
        seq = _seq_from_dirname(ch_dir.name)
        if seq is None:
            continue
        title = title_map.get(seq)
        if not title:
            print(f"  [warn] {dimension_dir.name}/{ch_dir.name}: _索引.json 无此章标题，跳过")
            continue
        for aspect in aspect_files:
            src = ch_dir / aspect
            if not src.is_file():
                continue
            target = dimension_dir / f"ch{seq:03d}_{title}_{aspect}"
            if target.is_file():
                if apply:
                    src.unlink(missing_ok=True)
                print(f"  [skip] {dimension_dir.name}/ch{seq:03d}_{title}_{aspect} 已存在")
                continue
            print(f"  [flat]  {dimension_dir.name}/{ch_dir.name}/{aspect} -> ch{seq:03d}_{title}_{aspect}")
            if apply:
                src.rename(target)
            moved += 1
        # 删除空目录
        if apply and ch_dir.is_dir() and not any(ch_dir.iterdir()):
            ch_dir.rmdir()
    return moved


def migrate_structure(project_dir: Path, title_map: Dict[int, str], apply: bool) -> int:
    return _flatten_per_chapter(
        project_dir,
        project_dir / "全书分析" / "故事结构" / "分章",
        ["章节结构.md"],
        title_map,
        apply,
    )


def migrate_visual(project_dir: Path, title_map: Dict[int, str], apply: bool) -> int:
    return _flatten_per_chapter(
        project_dir,
        project_dir / "全书分析" / "视觉资产" / "分章",
        ["视觉资产清单.md", "关键场景分镜表.md", "AI绘图提示词素材.md", "角色外观一致性表.md", "场景氛围表.md"],
        title_map,
        apply,
    )


def migrate_root_baks(project_dir: Path, apply: bool) -> int:
    """根级故事 JSON 的时间戳备份 (.bak_*) 移到 故事结构版本/备份/。"""
    if not project_dir.is_dir():
        return 0
    bak_dir = project_dir / "故事结构版本" / "备份"
    moved = 0
    for p in sorted(project_dir.iterdir()):
        if not p.is_file():
            continue
        # 时间戳备份（故事结构_增量.json.bak_YYYYMMDD_HHMMSS 等）或 chronology 迁移备份
        if re.match(r"^故事结构.*\.bak_\d{8}_\d{6}$", p.name) or p.name.endswith(".before_chronology_migration.bak"):
            target = bak_dir / p.name
            if target.is_file():
                if apply:
                    p.unlink(missing_ok=True)
                print(f"  [skip] {p.name} 已在 备份/")
                continue
            print(f"  [bak]   {p.name} -> 故事结构版本/备份/{p.name}")
            if apply:
                bak_dir.mkdir(parents=True, exist_ok=True)
                p.rename(target)
            moved += 1
    return moved


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="迁移产物到新布局（delta 子目录 + 分章扁平化 + 根级.bak归档）。默认 dry-run。")
    ap.add_argument("project_dir")
    ap.add_argument("--apply", action="store_true", help="实际移动文件；缺省仅预览")
    args = ap.parse_args(argv)
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"找不到项目目录: {project_dir}")
        return 1

    title_map = load_seq_title_map(project_dir)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== 迁移 {mode} | 项目: {project_dir} ===")
    print(f"标题映射章节数: {len(title_map)}")

    print("-- Delta JSON -> 章节处理/Delta/ --")
    n1 = migrate_delta(project_dir, args.apply)
    print("-- 故事结构 分章扁平化 --")
    n2 = migrate_structure(project_dir, title_map, args.apply)
    print("-- 视觉资产 分章扁平化 --")
    n3 = migrate_visual(project_dir, title_map, args.apply)
    print("-- 根级 .bak 备份 -> 故事结构版本/备份/ --")
    n4 = migrate_root_baks(project_dir, args.apply)

    print(f"\n合计: delta {n1}、结构 {n2}、视觉 {n3}、bak {n4} 个文件待/已迁移。")
    if not args.apply and (n1 or n2 or n3 or n4):
        print("（dry-run 预览；加 --apply 实际执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
