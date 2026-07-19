#!/usr/bin/env python3
"""清理空骨架治理快照。

背景：在尚无任何章节 Delta 提交时跑全书周期治理，每个区间的 no_change 提交
都会刷出一整套空的 before/after/diff/.bak 快照（207 字节空骨架）。本脚本删除
这些空骨架治理产物，不动有内容的治理快照、章节快照（story_after_chNNN.json）、
回滚文件与审计任务包/correction。

判定"空骨架"按内容（所有集合为空且介绍标题/描述为空），不依赖固定字节数，
这样骨架格式变化也不会误删或漏删。

用法：
    python cleanup_empty_governance_snapshots.py <project_dir>            # 预览（dry-run）
    python cleanup_empty_governance_snapshots.py <project_dir> --apply    # 实际删除
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from story_schema_rules import COLLECTION_KEYS  # noqa: E402

GOV_AFTER_RE = re.compile(r"^story_after_governance_(\d{8}_\d{6})\.json$")
GOV_BEFORE_RE = re.compile(r"^story_before_governance_(\d{8}_\d{6})\.json$")
DIFF_GOV_RE = re.compile(r"^diff_governance_(\d{8}_\d{6})\.json$")
APPLY_OPS_RE = re.compile(r"^apply_ops_(\d{8}_\d{6})\.txt$")
STORY_BAK_RE = re.compile(r"^故事结构_增量\.json\.bak_(\d{8}_\d{6})$")


def _is_empty_skeleton(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    intro = data.get("介绍", {})
    intro_empty = (
        not isinstance(intro, dict)
        or (not str(intro.get("标题", "")).strip() and not str(intro.get("描述", "")).strip())
    )
    collections_empty = all(
        isinstance(data.get(key), list) and not data.get(key) for key in COLLECTION_KEYS
    )
    return intro_empty and collections_empty


def collect(project_dir: Path) -> list[Path]:
    """返回所有应删除的空骨架治理产物路径。"""
    versions = project_dir / "故事结构版本"
    change_logs = project_dir / "结构变更日志"
    audit_dir = project_dir / "质量治理" / "周期审计"

    # 1. 以空骨架 story_after_governance_TS.json 为锚，整组删除同时间戳产物。
    empty_ts: set[str] = set()
    to_delete: list[Path] = []
    if versions.is_dir():
        for p in versions.iterdir():
            m = GOV_AFTER_RE.match(p.name)
            if m and _is_empty_skeleton(p):
                empty_ts.add(m.group(1))
        for ts in empty_ts:
            for p in (versions / f"story_before_governance_{ts}.json",
                      versions / f"story_after_governance_{ts}.json"):
                if p.is_file():
                    to_delete.append(p)
            d = change_logs / f"diff_governance_{ts}.json"
            if d.is_file():
                to_delete.append(d)
            a = audit_dir / f"apply_ops_{ts}.txt"
            if a.is_file():
                to_delete.append(a)

    # 2. 空骨架 .bak 备份（apply_governance_ops --backup 留下），单独按内容判定。
    if project_dir.is_dir():
        for p in project_dir.iterdir():
            if STORY_BAK_RE.match(p.name) and _is_empty_skeleton(p):
                to_delete.append(p)

    # 去重并稳定排序。
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in to_delete:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return sorted(uniq)


def main() -> int:
    ap = argparse.ArgumentParser(description="清理空骨架治理快照（默认 dry-run）")
    ap.add_argument("project_dir")
    ap.add_argument("--apply", action="store_true", help="实际执行删除；缺省仅预览")
    args = ap.parse_args()
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"项目目录不存在: {project_dir}")
        return 1

    targets = collect(project_dir)
    if not targets:
        print("未发现空骨架治理快照，无需清理。")
        return 0

    print(f"发现 {len(targets)} 个空骨架治理产物：")
    for p in targets:
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        print(f"  {'[删除]' if args.apply else '[预览]'} {size:>7} 字节  {p}")
    if not args.apply:
        print("\n以上为预览，未删除任何文件。加 --apply 实际执行删除。")
        return 0

    removed = 0
    for p in targets:
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            print(f"  删除失败: {p} ({e})")
    print(f"\n已删除 {removed}/{len(targets)} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
