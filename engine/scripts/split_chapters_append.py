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
import argparse, hashlib, json, os, re, sys, tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 复用 split_chapters.py 的切分原子函数，避免重复实现与行为漂移。
# 本脚本与 split_chapters.py 同目录，运行时 sys.path[0] 即 scripts/，可直接 import。
from split_chapters import read_file, detect_pattern, extract_order, normalize_title, safe_name


def _file_signature(path: Path) -> str:
    """Fingerprint an exact preview input so apply can reject stale plans."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _first_mismatch(old_titles: List[str], new_chapters: List[dict]) -> Optional[Dict[str, Any]]:
    """返回新旧章节首个标题不一致位置；全部匹配返回 None。"""
    n = min(len(old_titles), len(new_chapters))
    for i in range(n):
        if old_titles[i].strip() != new_chapters[i]['title'].strip():
            return {
                'index': i,
                'old_title': old_titles[i],
                'new_title': new_chapters[i]['title'],
                'message': (
                    f'第 {i + 1} 章标题不一致，前面章节疑似有改动，不符合“纯末尾追加”前提：\n'
                    f'  旧索引: {old_titles[i]!r}\n'
                    f'  新原文: {new_chapters[i]["title"]!r}\n'
                    f'若新原文只含新增片段，请加 --increment-only；若前面章节确有改动，'
                    f'建议新建项目重拆并复用旧 故事结构_增量.json 作为初始过程库。'
                ),
            }
    return None


def match_prefix(old_titles: List[str], new_chapters: List[dict]) -> int:
    """返回新旧章节的最长公共前缀长度。前缀必须逐字相等。

    若旧索引第 i 章与新原文第 i 章标题不一致，说明前面章节有改动，
    不符合“纯末尾追加”前提，抛错退出。

    保留 SystemExit 行为供 CLI 使用；plan_append 使用 _first_mismatch 报告冲突而不抛错。
    """
    conflict = _first_mismatch(old_titles, new_chapters)
    if conflict is not None:
        raise SystemExit(conflict['message'])
    return min(len(old_titles), len(new_chapters))


def plan_append(
    source_file: str,
    split_dir: str,
    pattern: str = '',
    increment_only: bool = False,
) -> Dict[str, Any]:
    """预览增量追加：切分新原文，比对旧索引，返回 AppendPlan（不写盘）。

    - 完整新书（含旧章节+新增章节）必须与旧索引逐字匹配前缀；首个不一致位置
      记录在 plan['conflict']，appended 为空。
    - increment_only=True 时跳过前缀比对，新原文全部视为新增。
    - 无新增章节时 plan['appended'] 为空列表。
    """
    out_dir = Path(split_dir)
    if not out_dir.is_dir():
        raise SystemExit(f'错误: 输出目录不存在: {out_dir}')
    old_index = load_old_index(out_dir)
    old_index_signature = _file_signature(out_dir / '_索引.json')
    old_chapters = old_index.get('chapters', []) or []
    if not old_chapters:
        raise SystemExit('错误: 旧索引 chapters 为空，请先用 split_chapters.py 完成首次拆章。')

    old_titles = [c.get('source_title', c.get('title', '')) for c in old_chapters]
    max_seq = max((c.get('seq', 0) for c in old_chapters), default=0)

    content = read_file(source_file)
    source_signature = _file_signature(Path(source_file))
    pat, new_chapters, preface_lines = parse_chapters(content, pattern)
    if not pat:
        raise SystemExit('错误: 无法检测新原文的章节标题模式，无法比对。请用 --pattern 显式指定。')
    if not new_chapters:
        raise SystemExit('错误: 新原文未切出任何章节。')

    if increment_only:
        prefix_len = 0
    else:
        conflict = _first_mismatch(old_titles, new_chapters)
        if conflict is not None:
            return {
                'source_file': str(Path(source_file).resolve()),
                'split_dir': str(out_dir.resolve()),
                'pattern': pat,
                'increment_only': bool(increment_only),
                'old_max_seq': max_seq,
                'old_chapter_count': sum(1 for c in old_chapters if not c.get('is_preface')),
                'old_index_signature': old_index_signature,
                'source_signature': source_signature,
                'preface_lines': preface_lines,
                'new_chapter_count': len(new_chapters),
                'prefix_len': 0,
                'appended': [],
                'conflict': conflict,
            }
        # No mismatch in the shared prefix: prefix covers the overlapping range.
        prefix_len = min(len(old_titles), len(new_chapters))

    new_items = new_chapters[prefix_len:]
    start_seq = max_seq + 1
    appended: List[Dict[str, Any]] = []
    for i, ch in enumerate(new_items):
        seq = start_seq + i
        filename = f"第{seq:03d}章_{safe_name(ch['title'])}.md"
        appended.append({
            'seq': seq,
            'source_order': extract_order(ch['title']) or seq,
            'source_title': ch['title'],
            'normalized_title': normalize_title(ch['title']),
            'filename': filename,
            'title': ch['title'],
            'is_preface': False,
            'content': ch['content'],
        })

    return {
        'source_file': str(Path(source_file).resolve()),
        'split_dir': str(out_dir.resolve()),
        'pattern': pat,
        'increment_only': bool(increment_only),
        'old_max_seq': max_seq,
        'old_chapter_count': sum(1 for c in old_chapters if not c.get('is_preface')),
        'old_index_signature': old_index_signature,
        'source_signature': source_signature,
        'preface_lines': preface_lines,
        'new_chapter_count': len(new_chapters),
        'prefix_len': prefix_len,
        'appended': appended,
        'conflict': None,
    }


def apply_append(plan: Dict[str, Any]) -> Dict[str, Any]:
    """对 preview 出的 plan 落盘：只写新增 md + 原子更新索引。

    - 永不覆盖旧章节文件；仅当目标 md 不存在时写入。
    - 索引写盘走 临时文件 + os.replace，source_file 更新为新书路径。
    - 旧条目不动，新条目追加；保留旧 pattern/preface_mode，仅在 plan 携带新 pattern 时覆盖。
    """
    out_dir = Path(plan['split_dir'])
    if not out_dir.is_dir():
        raise SystemExit(f'错误: 输出目录不存在: {out_dir}')
    idx_path = out_dir / '_索引.json'
    if not idx_path.is_file():
        raise SystemExit(f'错误: 旧索引不存在: {idx_path}')

    expected_signature = str(plan.get('old_index_signature', ''))
    if not expected_signature:
        raise ValueError('追加计划缺少旧索引签名，请重新预览后再执行。')
    if _file_signature(idx_path) != expected_signature:
        raise ValueError('章节索引在预览后已变化，请重新预览；本次未写入任何文件。')
    source_path = Path(str(plan.get('source_file', '')))
    expected_source_signature = str(plan.get('source_signature', ''))
    if (
        not expected_source_signature
        or not source_path.is_file()
        or _file_signature(source_path) != expected_source_signature
    ):
        raise ValueError('新原文在预览后已变化或不可读取，请重新预览；本次未写入任何文件。')

    with open(idx_path, 'r', encoding='utf-8') as f:
        index = json.load(f)
    old_chapters = list(index.get('chapters', []) or [])
    existing_filenames = {c.get('filename') for c in old_chapters if isinstance(c, dict)}
    old_seqs = [int(c.get('seq', 0)) for c in old_chapters if isinstance(c, dict)]
    planned = plan.get('appended', []) or []
    planned_seqs = [int(entry.get('seq', 0)) for entry in planned if isinstance(entry, dict)]
    current_max = max(old_seqs, default=0)
    expected_seqs = list(range(current_max + 1, current_max + 1 + len(planned)))
    if planned_seqs != expected_seqs:
        raise ValueError('追加计划章节序号与当前索引不连续，请重新预览。')
    planned_targets: List[Path] = []
    for entry in planned:
        filename = str(entry.get('filename', ''))
        target = out_dir / filename
        resolved_target = target.resolve()
        if not filename or out_dir.resolve() not in resolved_target.parents:
            raise ValueError(f'追加章节文件名越界或为空: {filename}')
        planned_targets.append(target)

    written: List[str] = []
    appended_entries: List[Dict[str, Any]] = []
    for entry, target in zip(planned, planned_targets):
        filename = entry['filename']
        if filename in existing_filenames:
            # 永不覆盖旧章节文件：跳过已存在条目。
            continue
        if target.is_file():
            # 同名文件已存在但不在索引中：避免覆盖，跳过写盘但仍追加索引条目。
            pass
        else:
            target.write_text(
                f"# {entry['source_title']}\n\n{entry.get('content', '')}",
                encoding='utf-8',
            )
            written.append(str(target))
        clean = {k: v for k, v in entry.items() if k != 'content'}
        appended_entries.append(clean)

    merged_chapters = old_chapters + appended_entries
    index['chapters'] = merged_chapters
    index['chapter_count'] = sum(1 for x in merged_chapters if not x.get('is_preface'))
    index['source_file'] = plan['source_file']
    if plan.get('pattern'):
        index['pattern'] = plan['pattern']

    # 原子写盘：临时文件 + os.replace。
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix='._索引.json.', suffix='.tmp', dir=str(idx_path.parent)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(idx_path))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    return {
        'appended': appended_entries,
        'written': written,
        'index_path': str(idx_path),
        'source_file': plan['source_file'],
    }


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

    plan = plan_append(
        args.input_file,
        args.output_dir,
        pattern=args.pattern,
        increment_only=args.increment_only,
    )

    print(f'新原文: {args.input_file}')
    print(f'检测/使用章节模式: {plan["pattern"]}（共 {plan["new_chapter_count"]} 章）')
    print(f'旧索引: {plan["old_chapter_count"]} 章，max seq = {plan["old_max_seq"]:03d}')

    if plan.get('preface_lines') and ''.join(plan['preface_lines']).strip():
        print('警告: 新原文检测到前言。新增场景下前言应已在旧项目处理，本次不重复写入 _前言.md。')

    if plan['conflict'] is not None:
        print(plan['conflict']['message'])
        return 1

    if not plan['appended']:
        print('未检测到新增章节（新原文前缀与旧索引完全一致，无追加内容）。')
        return 0

    start_seq = plan['appended'][0]['seq']
    end_seq = plan['appended'][-1]['seq']
    print()
    print(f'将新增 {len(plan["appended"])} 章，seq {start_seq:03d}..{end_seq:03d}：')
    for entry in plan['appended']:
        print(f'  第{entry["seq"]:03d}章  {entry["source_title"]}')

    if not args.apply:
        print()
        print('（dry-run 预览，未写盘。确认无误后加 --apply 执行。）')
        return 0

    report = apply_append(plan)
    idx_path = report['index_path']
    print()
    print(f'已写入 {len(report["appended"])} 个新章节 md，索引已更新：{idx_path}')
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
