#!/usr/bin/env python3
"""里程碑1：校验内置 engine 副本可导入运行。

校验项：
1. engine 目录结构完整（scripts/tools/schemas/prompts/references/docs/tests + SKILL.md + requirements.txt）。
2. 第三方依赖可导入（jinja2、json_repair）。
3. 关键引擎模块可 import。引擎脚本使用扁平 import（from chronology import ...），
   运行时 sys.path[0] 即 scripts/，因此把 engine/scripts 与 engine/tools 加入 sys.path。
4. 全部 .py 可 compile（语法正确，内存编译，不写 .pyc）。

退出码：0 = 通过；1 = 结构/依赖/核心模块/语法任一致命失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
SCRIPTS = ENGINE / "scripts"
TOOLS = ENGINE / "tools"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TOOLS))

EXPECTED_DIRS = ["scripts", "tools", "schemas", "prompts", "references", "docs", "tests"]
EXPECTED_FILES = ["SKILL.md", "requirements.txt"]

# 里程碑1 必须可导入的核心入口。
CORE_MODULES = ["run_pipeline", "agent_worker"]
# 其余关键模块，逐个报告，失败为警告（部分 CLI 入口需在里程碑2 重构为可 import）。
EXTRA_MODULES = [
    "chronology",
    "story_schema_rules",
    "validate_chapter_analysis",
    "validate_structure",
    "validate_delta",
    "merge_delta",
    "export_obsidian_vault",
    "progress_manager",
    "split_chapters",
]

fatal: list[str] = []
warnings: list[str] = []


def step(name: str, ok: bool, detail: str = "", fatal_on_fail: bool = True) -> None:
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)
    if not ok:
        (fatal if fatal_on_fail else warnings).append(f"{name}: {detail}")


# 1. 结构
missing_dirs = [d for d in EXPECTED_DIRS if not (ENGINE / d).is_dir()]
missing_files = [f for f in EXPECTED_FILES if not (ENGINE / f).is_file()]
step(
    "engine 目录结构",
    not missing_dirs and not missing_files,
    f"缺失 dirs={missing_dirs} files={missing_files}" if (missing_dirs or missing_files) else "",
)


# 2. 依赖
dep_ok = True
dep_detail = []
try:
    import jinja2  # noqa: F401
    dep_detail.append(f"jinja2=={jinja2.__version__}")
except Exception as e:  # noqa: BLE001
    dep_ok = False
    dep_detail.append(f"jinja2: {type(e).__name__}: {e}")
try:
    import json_repair  # noqa: F401
    dep_detail.append("json_repair")
except Exception as e:  # noqa: BLE001
    dep_ok = False
    dep_detail.append(f"json_repair: {type(e).__name__}: {e}")
step("第三方依赖", dep_ok, ", ".join(dep_detail))


# 3. 核心模块 import（致命）
core_fail = []
for mod in CORE_MODULES:
    try:
        __import__(mod)
    except Exception as e:  # noqa: BLE001
        core_fail.append(f"{mod}: {type(e).__name__}: {e}")
step(
    "核心模块 import",
    not core_fail,
    "; ".join(core_fail) if core_fail else f"{len(CORE_MODULES)} 个 ({', '.join(CORE_MODULES)})",
)

# 4. 其余模块 import（警告）
for mod in EXTRA_MODULES:
    try:
        __import__(mod)
        print(f"[OK  ] extra import: {mod}", flush=True)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"extra import {mod}: {type(e).__name__}: {e}")
        print(f"[WARN] extra import: {mod} -- {type(e).__name__}: {e}", flush=True)

# 5. compile 全部 .py（内存编译，不写 .pyc）
py_files = sorted(ENGINE.rglob("*.py"))
compile_errors = []
for py in py_files:
    try:
        src = py.read_text(encoding="utf-8")
        compile(src, str(py), "exec")
    except SyntaxError as e:
        compile_errors.append(f"{py.relative_to(ENGINE)}: {e}")
    except Exception as e:  # noqa: BLE001
        compile_errors.append(f"{py.relative_to(ENGINE)}: {type(e).__name__}: {e}")
step(
    "compile 全部 .py（语法）",
    not compile_errors,
    f"{len(py_files)} 个文件" if not compile_errors else f"{len(compile_errors)} 个失败",
)
for err in compile_errors[:10]:
    print("       " + err)

print()
if warnings:
    print(f"警告 {len(warnings)} 项（非致命，供里程碑2参考）：")
    for w in warnings:
        print("  - " + w)
if fatal:
    print(f"\n校验未通过：{len(fatal)} 项致命失败。")
    for item in fatal:
        print("  - " + item)
    sys.exit(1)
print("\n校验通过：内置 engine 副本可导入运行。")
