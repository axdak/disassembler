#!/usr/bin/env python3
"""修复 LLM 生成的 JSON 语法并重新输出为严格 JSON。

这层只处理“JSON 本身能不能被程序读取”的问题：Markdown 代码块、单引号、尾逗号、
轻微括号缺失等。它不理解 Delta schema，也不做字段类型/引用/治理语义校验；后续仍由
coerce_delta.py、compress_tags.py、validate_delta.py 负责。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


FENCED_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _extract_jsonish_text(raw: str) -> tuple[str, bool]:
    """提取 LLM 输出中的 JSON-ish 主体。"""
    text = raw.lstrip("﻿").strip()
    match = FENCED_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip(), True

    starts = [(idx, char) for char in ("{", "[") if (idx := text.find(char)) != -1]
    if not starts:
        return text, False

    start, opening = min(starts, key=lambda pair: pair[0])
    closing = "}" if opening == "{" else "]"
    end = text.rfind(closing)
    if end >= start:
        return text[start : end + 1].strip(), start != 0 or end != len(text) - 1
    return text[start:].strip(), start != 0


def _strict_json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _write_report(report_path: Optional[Path], report: Dict[str, Any]) -> None:
    if not report_path:
        return
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_strict_json_text(report), encoding="utf-8")


def repair_llm_json_file(
    path: Path | str,
    *,
    report_path: Path | str | None = None,
    backup: bool = False,
) -> Dict[str, Any]:
    """把 path 中的 LLM JSON 修复/格式化为严格 JSON，返回审计报告。"""
    target = Path(path)
    raw = target.read_text(encoding="utf-8")
    raw_for_json = raw.lstrip("﻿").strip()
    candidate, extracted_jsonish_body = _extract_jsonish_text(raw)
    report: Dict[str, Any] = {
        "path": str(target),
        "input_was_valid_json": False,
        "repaired": False,
        "extracted_jsonish_body": extracted_jsonish_body,
        "output_valid_json": False,
        "original_error": None,
        "backup_path": None,
    }

    try:
        data = json.loads(raw_for_json)
        report["input_was_valid_json"] = True
    except json.JSONDecodeError as exc:
        report["original_error"] = str(exc)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
            except ImportError as import_exc:
                _write_report(Path(report_path) if report_path else None, report)
                raise ValueError(
                    "缺少第三方库 json-repair；请先运行: python -m pip install -r requirements.txt"
                ) from import_exc
            try:
                data = repair_json(candidate, return_objects=True)
            except Exception as repair_exc:  # pragma: no cover - 第三方库异常类型不稳定
                _write_report(Path(report_path) if report_path else None, report)
                raise ValueError(f"JSON repair failed for {target}: {repair_exc}") from repair_exc
        report["repaired"] = True

    if not isinstance(data, (dict, list)):
        _write_report(Path(report_path) if report_path else None, report)
        raise ValueError(f"JSON repair did not produce an object or array: {target}")

    output = _strict_json_text(data)
    json.loads(output)
    report["output_valid_json"] = True

    if backup:
        backup_path = target.with_suffix(
            target.suffix + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        shutil.copy2(target, backup_path)
        report["backup_path"] = str(backup_path)

    _atomic_write_text(target, output)
    _write_report(Path(report_path) if report_path else None, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复 LLM 生成的 JSON 语法并重新输出为严格 JSON。"
    )
    parser.add_argument("json_path", help="需要修复/格式化的 JSON 文件路径")
    parser.add_argument("--report", help="修复报告 JSON 输出路径")
    parser.add_argument("--backup", action="store_true", help="写回前备份原始文件")
    args = parser.parse_args()

    try:
        report = repair_llm_json_file(
            args.json_path,
            report_path=args.report,
            backup=args.backup,
        )
    except (OSError, ValueError) as exc:
        print(f"LLM JSON 修复失败: {exc}", file=sys.stderr)
        return 1

    action = "修复" if report["repaired"] else "格式化"
    print(f"LLM JSON 已{action}: {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
