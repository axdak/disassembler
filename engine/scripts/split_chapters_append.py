#!/usr/bin/env python3
"""增量拆章脚本：在已有项目上追加新书续写章节，保留旧 seq 与已完成的章节产物。

适用场景：一本书已经拆完，后续作者又更新了新章节（纯末尾追加）。
直接重跑 split_chapters.py 会从 seq=1 重新编号，破坏旧快照/Delta/进度。
本脚本读取旧 _索引.json，切分新原文，按 source_title 前缀比对识别新增章节，
seq 接续旧索引最大值，只追加新章节 md 和索引条目，不动旧产物。

用法：
    python split_chapters_append.py <新原文文件> <项目目录>/原文拆解 [--pattern PATTERN] [--apply] [--increment-only]
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import List, Optional, Tuple

# 复用 split_chapters.py 的切分原子函数，避免重复实现与行为漂移。
# 本脚本与 split_chapters.py 同目录，运行时 sys.path[0] 即 scripts/，可直接 import。
from split_chapters import read_file, detect_pattern, extract_order, normalize_title, safe_name


def parse_chapters(content: str, pattern: str = '', sample_lines: int = 0, min_matches: int = 2) -> Tuple[Optional[str], List[dict], List[str]]:
    """切分原文为章节列表（不写文件）。返回 (pattern, chapters, preface_lines)。

    切分逻辑与 split_chapters.split_novel 保持一致，区别仅在不落盘。
    """
    pat = pattern or detect_pattern(content, sample_lines, min_matches)[0]
    if not pat:
        # 无法检测章节模式：split_chapters 会把整本当一章；增量追加场景下无法比对，直接返回空。
        return None, [], []
    lines = content.split('\n')
    chapters: List[dict] = []
    cur_title: Optional[str] = None
    cur: List[str] = []
    pre: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and re.match(pat, stripped):
            if cur_title is None:
                if ''.join(cur).strip():
                    pre = cur[:]
            else:
                chapters.append({'title': cur_title, 'content': '\n'.join(cur)})
            cur_title = stripped
            cur = []
        else:
            cur.append(line)
    if cur_title is not None:
        chapters.append({'title': cur_title, 'content': '\n'.join(cur)})
    return pat, chapters, pre


def load_old_index(out_dir: Path) -> dict:
    idx_path = out_dir / '_索引.json'
    if not idx_path.is_file():
        raise SystemExit(f'错误: 旧索引不存在: {idx_path}\n请先用 split_chapters.py 初始化项目并完成首次拆章。')
    with open(idx_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def match_prefix(old_titles: List[str], new_chapters: List[dict]) -> int:
    """返回新旧章节的最长公共前缀长度。前缀必须逐字相等。

    若旧索引第 i 章与新原文第 i 章标题不一致，说明前面章节有改动，
    不符合“纯末尾追加”前提，抛错退出。
    """
    n = min(len(old_titles), len(new_chapters))
    for i in range(n):
        if old_titles[i].strip() != new_chapters[i]['title'].strip():
            raise SystemExit(
                f'错误: 第 {i + 1} 章标题不一致，前面章节疑似有改动，不符合“纯末尾追加”前提：\n'
                f'  旧索引: {old_titles[i]!r}\n'
                f'  新原文: {new_chapters[i]["title"]!r}\n'
                f'若新原文只含新增片段，请加 --increment-only；若前面章节确有改动，'
                f'建议新建项目重拆并复用旧 故事结构_增量.json 作为初始过程库。'
            )
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description='增量追加新书续写章节，保留旧 seq 与已完成产物。')
    ap.add_argument('input_file', help='新原文文件（完整新书，含旧章节+新增章节）')
    ap.add_argument('output_dir', help='项目的 原文拆解/ 目录（须已有 _索引.json）')
    ap.add_argument('--pattern', default='', help='指定章节标题正则（默认自动检测）')
    ap.add_argument('--apply', action='store_true', help='真正写盘（默认 dry-run 只预览）')
    ap.add_argument('--increment-only', action='store_true', help='新原文只含新增片段，跳过前缀比对，全部当新增')
    ap.add_argument('--sample-lines', type=int, default=0, help='0=扫全文（默认）；>0=只看前N行检测模式')
    ap.add_argument('--min-matches', type=int, default=2)
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    if not out_dir.is_dir():
        raise SystemExit(f'错误: 输出目录不存在: {out_dir}')
    old_index = load_old_index(out_dir)
    old_chapters = old_index.get('chapters', [])
    if not old_chapters:
        raise SystemExit('错误: 旧索引 chapters 为空，请先用 split_chapters.py 完成首次拆章。')

    old_titles = [c.get('source_title', c.get('title', '')) for c in old_chapters]
    max_seq = max((c.get('seq', 0) for c in old_chapters), default=0)

    content = read_file(args.input_file)
    pat, new_chapters, pre = parse_chapters(content, args.pattern, args.sample_lines, args.min_matches)
    if not pat:
        raise SystemExit('错误: 无法检测新原文的章节标题模式，无法比对。请用 --pattern 显式指定。')
    if not new_chapters:
        raise SystemExit('错误: 新原文未切出任何章节。')
    print(f'新原文: {args.input_file}')
    print(f'检测/使用章节模式: {pat}（共 {len(new_chapters)} 章）')
    print(f'旧索引: {len(old_chapters)} 章，max seq = {max_seq:03d}')

    if pre and ''.join(pre).strip():
        print('警告: 新原文检测到前言。新增场景下前言应已在旧项目处理，本次不重复写入 _前言.md。')

    # 识别新增章节
    if args.increment_only:
        prefix_len = 0
        print('--increment-only: 跳过前缀比对，新原文全部视为新增。')
    else:
        prefix_len = match_prefix(old_titles, new_chapters)

    new_items = new_chapters[prefix_len:]
    if not new_items:
        print('未检测到新增章节（新原文前缀与旧索引完全一致，无追加内容）。')
        return 0

    start_seq = max_seq + 1
    print()
    print(f'将新增 {len(new_items)} 章，seq {start_seq:03d}..{start_seq + len(new_items) - 1:03d}：')
    for i, ch in enumerate(new_items):
        seq = start_seq + i
        print(f'  第{seq:03d}章  {ch["title"]}')

    if not args.apply:
        print()
        print('（dry-run 预览，未写盘。确认无误后加 --apply 执行。）')
        return 0

    # 写盘
    appended: List[dict] = []
    for i, ch in enumerate(new_items):
        seq = start_seq + i
        filename = f"第{seq:03d}章_{safe_name(ch['title'])}.md"
        (out_dir / filename).write_text(f"# {ch['title']}\n\n{ch['content']}", encoding='utf-8')
        appended.append({
            "seq": seq,
            "source_order": extract_order(ch['title']) or seq,
            "source_title": ch['title'],
            "normalized_title": normalize_title(ch['title']),
            "filename": filename,
            "title": ch['title'],
            "is_preface": False,
        })

    # 合并索引：旧条目不动，新条目追加；保留旧 pattern/preface_mode/source_file。
    # 仅当用户显式指定新 --pattern 时才更新 pattern 字段。
    merged_chapters = old_chapters + appended
    old_index['chapters'] = merged_chapters
    old_index['chapter_count'] = sum(1 for x in merged_chapters if not x.get('is_preface'))
    if args.pattern:
        old_index['pattern'] = args.pattern
    idx_path = out_dir / '_索引.json'
    with open(idx_path, 'w', encoding='utf-8') as f:
        json.dump(old_index, f, ensure_ascii=False, indent=2)

    print()
    print(f'已写入 {len(appended)} 个新章节 md，索引已更新：{idx_path}')
    print('旧章节文件、进度.json、故事结构_增量.json 未改动。')
    print()
    print('后续步骤：')
    print('  1. python progress_manager.py reconcile <项目目录>   # 识别新增章节为待处理')
    print('  2. python progress_manager.py resume <项目目录>      # 从第一个新增章节开始')
    print('  3. 逐章: 分析MD -> Delta -> validate_delta -> merge_delta -> validate_structure --chapter-check')
    print('  4. 新章节全部完成后，重跑步骤3(全书分析)和步骤4(最终整理)，刷新 故事结构.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
