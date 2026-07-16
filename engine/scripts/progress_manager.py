#!/usr/bin/env python3
"""
进度管理脚本 - 管理拆书流程的断点续传（逐章模式 + 全书维度分析）

用法:
    python progress_manager.py init <项目目录> <源文件路径>   # 初始化项目
    python progress_manager.py init-incremental <项目目录>    # 初始化空的 故事结构_增量.json
    python progress_manager.py status <项目目录>             # 查看进度
    python progress_manager.py update <项目目录> <步骤> <详情JSON>  # 更新步骤进度
    python progress_manager.py complete <项目目录> <步骤>    # 标记步骤完成
    python progress_manager.py resume <项目目录>             # 获取断点信息(下一步该做什么)
    python progress_manager.py reconcile <项目目录>          # 交叉验证文件与进度，修复不一致
    python progress_manager.py summary <项目目录>            # 生成增量JSON元素名称摘要

步骤定义:
    1 - 章节拆分
    2 - 逐章分析与提取（MD分析 + Delta提取 + 质量治理）
    3 - 基准全书报告固化（由全书/局部分析工作台产物固化而来）
    4 - 最终结构整理与强校验
"""

import sys
import os
import json
from datetime import datetime

PROGRESS_FILE = "进度.json"

STEPS = {
    "1": "章节拆分",
    "2": "逐章分析与提取及质量治理",
    "3": "基准全书报告固化",
    "4": "最终结构整理与强校验"
}

STEP_ORDER = ["1", "2", "3", "4"]

# Step 3 不再限制分析只能在此阶段执行；这里只记录被固化的基准报告。
STEP3_REPORTS = {
    "summary": "章节梗概汇总",
    "characters": "人物分析",
    "plot": "剧情线分析",
    "style": "风格与节奏分析",
    "report": "拆书总报告"
}


STEP3_OUTPUTS = {
    "summary": "全书分析/剧情结构/章节梗概汇总.md",
    "characters": ["全书分析/人物分析/人物档案.md", "全书分析/人物分析/人物关系.md", "全书分析/人物分析/人物提及.json"],
    "plot": ["全书分析/剧情结构/剧情线索.md", "全书分析/剧情结构/伏笔追踪.md", "全书分析/剧情结构/冲突图谱.md"],
    "style": ["全书分析/风格分析/文风分析.md", "全书分析/风格分析/节奏分析.md", "全书分析/风格分析/高光场景.md"],
    "report": "全书分析/拆书总报告.md"
}


def get_progress_path(project_dir):
    return os.path.join(project_dir, PROGRESS_FILE)


def load_progress(project_dir):
    path = get_progress_path(project_dir)
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_progress(project_dir, data):
    path = get_progress_path(project_dir)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_all_chapters(project_dir):
    """获取原文拆解目录中所有章节md文件（按文件名排序）"""
    split_dir = os.path.join(project_dir, "原文拆解")
    if not os.path.isdir(split_dir):
        return []
    chapters = sorted([f for f in os.listdir(split_dir) if f.endswith('.md')])
    return chapters


def chapter_seq_from_filename(filename):
    """从 第NNN章_标题.md 提取 NNN；失败返回None。"""
    import re
    m = re.match(r"第(\d+)章", filename)
    if not m:
        return None
    return int(m.group(1))


def expected_quality_paths(project_dir, chapter_md):
    """返回章节完成所需的质量治理文件路径。"""
    base = chapter_md[:-3]
    seq = chapter_seq_from_filename(chapter_md)
    snapshot_before = snapshot_after = diff_path = None
    if seq is not None:
        snapshot_before = os.path.join(project_dir, "故事结构版本", f"story_before_ch{seq:03d}.json")
        snapshot_after = os.path.join(project_dir, "故事结构版本", f"story_after_ch{seq:03d}.json")
        diff_path = os.path.join(project_dir, "结构变更日志", f"diff_ch{seq:03d}.json")
    return {
        "analysis_md": os.path.join(project_dir, "章节处理", chapter_md),
        "delta_json": os.path.join(project_dir, "章节处理", f"{base}.json"),
        "delta_validation": os.path.join(project_dir, "质量治理", "delta校验", f"{base}.json"),
        "chapter_validation": os.path.join(project_dir, "质量治理", "章节校验", f"{base}.json"),
        "snapshot_before": snapshot_before,
        "snapshot_after": snapshot_after,
        "diff": diff_path,
    }


def get_completed_files(project_dir):
    """扫描章节处理目录，返回已完成的章节md文件名集合。

    新版质量治理要求一章完成必须具备：
      1. 章节分析MD
      2. Delta JSON
      3. validate_delta.py 报告
      4. validate_structure.py --chapter-check 报告
      5. story_after_chNNN.json 快照

    以上齐全，并且 JSON 报告中的 passed 字段为 true 时，才算该章可信完成。
    返回已完成章节的md文件名集合。
    """
    completed = set()
    for md_file in get_all_chapters(project_dir):
        paths = expected_quality_paths(project_dir, md_file)
        required_paths = [v for v in paths.values() if v]
        if not all(os.path.isfile(path) for path in required_paths):
            continue
            
        try:
            passed = True
            for report_key in ("delta_validation", "chapter_validation"):
                with open(paths[report_key], 'r', encoding='utf-8') as f:
                    report = json.load(f)
                if not isinstance(report, dict) or report.get("passed") is not True or report.get("mode") != "process":
                    passed = False
                    break
                if not isinstance(report.get("errors"), list) or not isinstance(report.get("warnings"), list):
                    passed = False
                    break
            
            if passed:
                completed.add(md_file)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
            
    return completed


def chapter_completion_count(project_dir, chapter_md):
    """返回章节完成度：已有文件数 / 应有文件数。"""
    paths = expected_quality_paths(project_dir, chapter_md)
    required_paths = [v for v in paths.values() if v]
    count = sum(1 for path in required_paths if os.path.isfile(path))
    return count, len(required_paths), paths


def get_incremental_json_path(project_dir):
    return os.path.join(project_dir, "故事结构_增量.json")


def init_incremental(project_dir):
    """初始化空的 故事结构_增量.json（顶层骨架 + 七类空集）。
    步骤1完成后调用。主控Agent无需手写JSON，避免格式不一致导致 merge_delta 崩溃。
    若文件已存在且结构完整，保留不动；缺键则补全。
    """
    path = get_incremental_json_path(project_dir)
    skeleton = {
        "介绍": {"标题": "", "描述": ""},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
        # 补全缺失的顶层键，不破坏已有内容
        for k, default in skeleton.items():
            if k not in data:
                data[k] = default
            elif k == "介绍" and not isinstance(data[k], dict):
                data[k] = default
            elif k != "介绍" and not isinstance(data[k], list):
                data[k] = default
    else:
        data = skeleton

    os.makedirs(project_dir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"增量JSON已初始化: {path}")
    print(f"  顶层骨架: 介绍 + 7类空集")
    return data


def init_project(project_dir, source_file):
    """初始化项目进度文件"""
    os.makedirs(project_dir, exist_ok=True)
    for subdir in [
        "原文", "原文拆解", "章节处理",
        "质量治理/delta校验", "质量治理/章节校验", "质量治理/周期审计", "质量治理/按需治理", "质量治理/最终审计",
        "故事结构版本", "结构变更日志",
        "全书分析/_任务包", "全书分析/剧情结构", "全书分析/人物分析", "全书分析/风格分析", "全书分析/迭代版本", "全书分析/局部分析",
    ]:
        os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

    progress = {
        "项目目录": os.path.abspath(project_dir),
        "源文件": os.path.abspath(source_file),
        "创建时间": datetime.now().isoformat(),
        "更新时间": datetime.now().isoformat(),
        "当前步骤": "1",
        "步骤状态": {
            "1": {"状态": "pending", "详情": {}, "完成时间": None},
            "2": {
                "状态": "pending",
                "详情": {
                    "已完成章节": [],
                    "总章节数": 0,
                    "最近审计章节": ""
                },
                "完成时间": None
            },
            "3": {
                "状态": "pending",
                "详情": {
                    "已完成报告": [],
                    "说明": "分析任务可通过 run_pipeline.py analysis-pack 随时生成；步骤3只表示基准报告固化"
                },
                "完成时间": None
            },
            "4": {"状态": "pending", "详情": {}, "完成时间": None},
        }
    }
    save_progress(project_dir, progress)
    print(f"项目已初始化: {project_dir}")
    return progress


def show_status(project_dir):
    """显示当前进度"""
    progress = load_progress(project_dir)
    if not progress:
        print("错误: 项目进度文件不存在，请先初始化")
        return

    print(f"源文件: {progress['源文件']}")
    current = progress['当前步骤']
    if current == "完成":
        print(f"当前步骤: 全部完成")
    else:
        print(f"当前步骤: {current} - {STEPS[current]}")
    print(f"更新时间: {progress['更新时间']}")
    print()

    for step_id in STEP_ORDER:
        step_info = progress['步骤状态'][step_id]
        status_icon = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]"
        }.get(step_info['状态'], "[?]")
        step_name = STEPS[step_id]
        detail = ""
        if step_id == "2":
            done = step_info['详情'].get('已完成章节', [])
            all_ch = get_all_chapters(project_dir)
            if all_ch:
                pct = len(done) * 100 // len(all_ch) if all_ch else 0
                detail = f" ({len(done)}/{len(all_ch)} 章, {pct}%)"
            else:
                detail = f" (已完成 {len(done)} 章)"
            detail += " | 逐章模式"
        elif step_id == "3":
            done_subs = step_info['详情'].get('已完成子步骤', [])
            total_subs = len(STEP3_REPORTS)
            detail = f" ({len(done_subs)}/{total_subs} 子步骤)"
            for sub_id, sub_name in STEP3_REPORTS.items():
                sub_icon = "[x]" if sub_id in done_subs else "[ ]"
                print(f"      {sub_icon} {sub_id}: {sub_name}")

        print(f"  {status_icon} 步骤{step_id}: {step_name}{detail}")

    # 显示增量JSON的元素统计
    incremental_path = get_incremental_json_path(project_dir)
    if os.path.isfile(incremental_path):
        with open(incremental_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print()
        print("  元素统计:")
        for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]:
            count = len(data.get(key, []))
            print(f"    {key}: {count}")


def reconcile_progress(project_dir):
    """
    交叉验证文件系统与进度记录，修复不一致（质量治理版）。

    一章只有在 MD、Delta、两个校验报告、合并后快照全部存在时，才算可信完成。
    """
    progress = load_progress(project_dir)
    if not progress:
        print("错误: 项目进度文件不存在")
        return

    step2_info = progress['步骤状态']['2']
    recorded_done = set(step2_info['详情'].get('已完成章节', []))

    completed_md = get_completed_files(project_dir)
    all_chapters = get_all_chapters(project_dir)
    if not all_chapters:
        print("错误: 原文拆解目录中没有章节文件")
        return

    incomplete_chapters = []
    for ch_md in all_chapters:
        if ch_md in completed_md:
            continue
        count, total, paths = chapter_completion_count(project_dir, ch_md)
        if count > 0:
            missing = [name for name, path in paths.items() if path and not os.path.isfile(path)]
            incomplete_chapters.append((ch_md, count, total, missing))

    file_exists_not_recorded = completed_md - recorded_done
    recorded_but_file_missing = recorded_done - completed_md

    print("=== 交叉验证结果 ===")
    print(f"总章节: {len(all_chapters)}")
    print(f"进度记录已完成: {len(recorded_done)}")
    print(f"质量治理产物齐全: {len(completed_md)}")

    if file_exists_not_recorded:
        print(f"\n  质量治理产物齐全但进度未记录 ({len(file_exists_not_recorded)} 章):")
        for f in sorted(file_exists_not_recorded)[:10]:
            print(f"    {f}")
        if len(file_exists_not_recorded) > 10:
            print(f"    ... 共 {len(file_exists_not_recorded)} 章")
        print("    -> 这些章节将补充到进度记录中")

    if recorded_but_file_missing:
        print(f"\n  警告: 进度已记录但质量治理产物不齐全 ({len(recorded_but_file_missing)} 章):")
        for f in sorted(recorded_but_file_missing):
            print(f"    {f}")
        print("    -> 这些章节从已完成列表移除，建议从最近可信快照恢复后重跑")

    if incomplete_chapters:
        print(f"\n  部分完成的章节 ({len(incomplete_chapters)} 章):")
        for ch_md, count, total, missing in sorted(incomplete_chapters)[:10]:
            print(f"    {ch_md} ({count}/{total} 个产物存在，缺: {', '.join(missing)})")
        if len(incomplete_chapters) > 10:
            print(f"    ... 共 {len(incomplete_chapters)} 章")
        print("    -> 根据缺失文件判断从章节分析器、Delta提取器、校验或合并步骤恢复")

    new_done = list((recorded_done | file_exists_not_recorded) - recorded_but_file_missing)
    new_done.sort()

    step2_info['详情']['已完成章节'] = new_done
    step2_info['详情']['总章节数'] = len(all_chapters)

    changed = bool(file_exists_not_recorded or recorded_but_file_missing)
    if changed:
        progress['更新时间'] = datetime.now().isoformat()
        save_progress(project_dir, progress)
        print("\n进度已修复并保存")
    else:
        print("\n进度记录与文件系统一致，无需修复")

    remaining = [ch for ch in all_chapters if ch not in set(new_done)]

    return {
        "已修复已完成": new_done,
        "需补记": sorted(list(file_exists_not_recorded)),
        "需重新处理": sorted(list(recorded_but_file_missing)),
        "部分完成需恢复": [ch for ch, _, _, _ in sorted(incomplete_chapters)],
        "未处理": remaining,
    }

def generate_summary(project_dir):
    """
    生成增量JSON的元素名称摘要。
    当增量JSON较大时，主控Agent可将摘要传给执行单元，而非完整JSON。
    """
    incremental_path = get_incremental_json_path(project_dir)
    if not os.path.isfile(incremental_path):
        print("错误: 增量JSON不存在")
        return

    with open(incremental_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    json_size = os.path.getsize(incremental_path)

    print(f"=== 增量JSON摘要 ===")
    print(f"文件大小: {json_size:,} 字节 ({json_size/1024:.1f} KB)")
    print()

    type_names = {
        "角色集": "角色",
        "事件集": "事件",
        "地点集": "地点",
        "线索集": "线索",
        "阵营集": "阵营",
        "物品集": "物品"
    }

    summary = {}
    for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]:
        items = data.get(key, [])
        names = [item.get("名称", "?") for item in items]
        summary[key] = names
        name = type_names.get(key, key)
        print(f"  {name} ({len(names)}): {', '.join(names[:20])}")
        if len(names) > 20:
            print(f"    ... 共 {len(names)} 个")

    print()
    if json_size < 50000:
        print("建议: JSON较小，可完整传给执行单元")
    elif json_size < 100000:
        print("建议: JSON中等，可只传元素名称列表（如上）")
    elif json_size < 200000:
        print("建议: JSON较大，只传最近10章新增的元素名称")
    else:
        print("建议: JSON过大，只传主角和主要阵营相关元素名称")

    return summary


def update_step(project_dir, step_id, detail_json):
    """更新步骤进度详情"""
    progress = load_progress(project_dir)
    if not progress:
        print("错误: 项目进度文件不存在")
        return

    if step_id not in STEPS:
        print(f"错误: 无效步骤 {step_id}，有效步骤: {list(STEPS.keys())}")
        return

    detail = json.loads(detail_json) if isinstance(detail_json, str) else detail_json
    progress['步骤状态'][step_id]['详情'].update(detail)
    progress['步骤状态'][step_id]['状态'] = 'in_progress'
    progress['当前步骤'] = step_id
    progress['更新时间'] = datetime.now().isoformat()
    save_progress(project_dir, progress)
    print(f"步骤{step_id}({STEPS[step_id]})进度已更新")


def validate_step_completion(project_dir, step_id):
    """完成步骤前的硬产物校验。返回 (ok, messages)。"""
    messages = []
    if step_id == "1":
        index_path = os.path.join(project_dir, "原文拆解", "_索引.json")
        chapters = get_all_chapters(project_dir)
        if not os.path.isfile(index_path):
            messages.append("缺少 原文拆解/_索引.json")
        if not chapters:
            messages.append("原文拆解目录中没有章节MD")
    elif step_id == "2":
        chapters = get_all_chapters(project_dir)
        completed = get_completed_files(project_dir)
        missing = [ch for ch in chapters if ch not in completed]
        if not chapters:
            messages.append("没有章节文件，不能完成步骤2")
        if missing:
            messages.append(f"步骤2仍有 {len(missing)} 章质量治理产物不齐全，示例: {', '.join(missing[:5])}")
    elif step_id == "3":
        missing = []
        for report_id, rels in STEP3_OUTPUTS.items():
            if isinstance(rels, str):
                rels = [rels]
            for rel in rels:
                path = os.path.join(project_dir, rel)
                if not os.path.isfile(path) or os.path.getsize(path) == 0:
                    missing.append(rel)
        if missing:
            messages.append(f"步骤3基准报告未全部固化，缺 {len(missing)} 个产物，示例: {', '.join(missing[:5])}")
    elif step_id == "4":
        final_path = os.path.join(project_dir, "故事结构.json")
        final_report = os.path.join(project_dir, "质量治理", "最终审计", "validate_report.txt")
        if not os.path.isfile(final_path):
            messages.append("缺少最终 故事结构.json")
        if not os.path.isfile(final_report):
            messages.append("缺少最终校验报告 质量治理/最终审计/validate_report.txt")
    return (len(messages) == 0), messages


def complete_step(project_dir, step_id, force=False):
    """标记步骤完成。默认先做硬产物校验；确需手动覆盖时使用 --force。"""
    progress = load_progress(project_dir)
    if not progress:
        print("错误: 项目进度文件不存在")
        return

    if step_id not in STEPS:
        print(f"错误: 无效步骤 {step_id}")
        return

    ok, messages = validate_step_completion(project_dir, step_id)
    if not ok and not force:
        print(f"步骤{step_id}({STEPS[step_id]})产物不完整，拒绝标记完成：")
        for m in messages:
            print(f"  - {m}")
        print("确需人工覆盖可使用: python progress_manager.py complete <项目目录> <步骤> --force")
        return
    if not ok and force:
        print("警告: 使用 --force 跳过完成前产物校验：")
        for m in messages:
            print(f"  - {m}")

    progress['步骤状态'][step_id]['状态'] = 'completed'
    progress['步骤状态'][step_id]['完成时间'] = datetime.now().isoformat()

    current_idx = STEP_ORDER.index(step_id)
    if current_idx + 1 < len(STEP_ORDER):
        next_step = STEP_ORDER[current_idx + 1]
        progress['当前步骤'] = next_step
    else:
        progress['当前步骤'] = "完成"

    progress['更新时间'] = datetime.now().isoformat()
    save_progress(project_dir, progress)
    print(f"步骤{step_id}({STEPS[step_id]})已完成，下一步: {progress['当前步骤']}")

def get_resume_info(project_dir):
    """获取断点续传信息"""
    progress = load_progress(project_dir)
    if not progress:
        print("错误: 项目进度文件不存在")
        return

    current_step = progress['当前步骤']
    if current_step == "完成":
        print("所有步骤已完成！")
        return

    step_info = progress['步骤状态'][current_step]
    print(f"=== 断点续传信息 ===")
    print(f"当前步骤: {current_step} - {STEPS[current_step]}")
    print(f"步骤状态: {step_info['状态']}")

    if current_step == "1":
        print("操作: 执行章节拆分脚本")
    elif current_step == "2":
        # 先执行reconcile获取最新状态
        result = reconcile_progress(project_dir)

        done = step_info['详情'].get('已完成章节', [])
        all_ch = get_all_chapters(project_dir)
        remaining = [f for f in all_ch if f not in set(done)]

        print(f"\n已完成章节: {len(done)}/{len(all_ch)}")
        print(f"待处理章节: {len(remaining)}")

        if remaining:
            print(f"\n下一章: {remaining[0]}")
            print(f"\n操作: 先生成章节分析MD，再生成Delta；依次运行validate_delta.py、merge_delta.py、validate_structure.py --chapter-check")

        # 显示增量JSON状态
        incremental_path = get_incremental_json_path(project_dir)
        if os.path.isfile(incremental_path):
            with open(incremental_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            print(f"\n增量JSON已有元素:")
            for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]:
                print(f"  {key}: {len(data.get(key, []))}")

        print(f"\n建议先执行: python progress_manager.py reconcile <项目目录>")
        print(f"可执行: python progress_manager.py summary <项目目录> 查看元素摘要")
    elif current_step == "3":
        done_reports = step_info['详情'].get('已完成报告', step_info['详情'].get('已完成子步骤', []))
        print(f"\n已固化报告: {len(done_reports)}/{len(STEP3_REPORTS)}")
        for report_id, report_name in STEP3_REPORTS.items():
            report_icon = "[x]" if report_id in done_reports else "[ ]"
            print(f"  {report_icon} {report_id}: {report_name}")

        next_report = None
        for report_id in STEP3_REPORTS:
            if report_id not in done_reports:
                next_report = report_id
                break

        if next_report:
            print(f"\n下一建议报告: {next_report} - {STEP3_REPORTS[next_report]}")
            print(f"操作: python run_pipeline.py analysis-pack <项目目录> --task {next_report} --chapters all")
        else:
            print(f"\n所有基准报告已固化")
    elif current_step == "4":
        print("操作: 最终结构整理，运行validate_structure.py --strict，生成故事结构.json")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法:")
        print("  python progress_manager.py init <项目目录> <源文件路径>")
        print("  python progress_manager.py init-incremental <项目目录>")
        print("  python progress_manager.py status <项目目录>")
        print("  python progress_manager.py update <项目目录> <步骤> <详情JSON>")
        print("  python progress_manager.py complete <项目目录> <步骤>")
        print("  python progress_manager.py resume <项目目录>")
        print("  python progress_manager.py reconcile <项目目录>")
        print("  python progress_manager.py summary <项目目录>")
        print()
        print("步骤: 1=章节拆分 2=逐章分析与质量治理 3=全书维度分析 4=最终结构整理与强校验")
        print("Step 3子步骤: 3a=梗概汇总 3b=人物分析 3c=剧情线 3d=风格节奏 3e=总报告")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        init_project(sys.argv[2], sys.argv[3])
    elif cmd == "init-incremental":
        init_incremental(sys.argv[2])
    elif cmd == "status":
        show_status(sys.argv[2])
    elif cmd == "update":
        update_step(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "{}")
    elif cmd == "complete":
        complete_step(sys.argv[2], sys.argv[3], force=("--force" in sys.argv))
    elif cmd == "resume":
        get_resume_info(sys.argv[2])
    elif cmd == "reconcile":
        result = reconcile_progress(sys.argv[2])
        if result:
            print(f"\n=== 恢复操作指引 ===")
            step_num = 1
            if result.get('需补记'):
                print(f"{step_num}. 有 {len(result['需补记'])} 个质量治理产物齐全但未记录进度的章节，已补记")
                step_num += 1
            if result.get('部分完成需恢复'):
                print(f"{step_num}. 有 {len(result['部分完成需恢复'])} 个部分完成章节，按缺失产物从相应环节恢复")
                step_num += 1
            if result['需重新处理']:
                print(f"{step_num}. 重新处理 {len(result['需重新处理'])} 个文件丢失章节")
                step_num += 1
            print(f"{step_num}. 继续逐章处理 {len(result['未处理'])} 个未处理章节")
            if result['未处理']:
                print(f"   从 {result['未处理'][0]} 开始")
    elif cmd == "summary":
        generate_summary(sys.argv[2])
    else:
        print(f"未知命令: {cmd}")
