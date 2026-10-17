"""本地交付门禁及责任诊断；不生成内容、不重放章节、不调用模型。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from chronology import parse_chapter_numbers
from fault_recovery import digest
from merge_delta import save_json
from story_schema_rules import COLLECTION_KEYS, validate_exact_story_schema

# 与容量/修复预算返回码分离：前置内容缺失可回到内容阶段，字段问题需要原阶段修正。
CONTENT_PENDING = 68
QUALITY_PENDING = 69


def delivery_report_path(project: Path) -> Path:
    """统一桌面、编排器和维护入口读取的本地诊断位置。"""
    return project / "质量治理/最终审计/delivery_gate.json"


def _write_report(project: Path, *, kind: str, issues: list[dict], resume_stage="") -> None:
    source, draft = project / "故事结构_增量.json", project / "故事结构_草稿.json"
    save_json(delivery_report_path(project), {
        "kind": kind, "issues": issues, "resume_stage": resume_stage,
        "source_sha256": digest(source) if source.is_file() else "",
        "draft_sha256": digest(draft) if draft.is_file() else "",
    })


def check_delivery_prerequisites(project: Path, *, end_chapter_seq=0) -> int:
    """只检查已有完成凭据；包括不足五章的尾周期，执行归属章节内容阶段。"""
    from run_pipeline import (chapter_files, seq_from_file, is_chapter_complete,
                              audit_range_is_resolved)
    chapters = [ch for ch in chapter_files(project)
                if not end_chapter_seq or seq_from_file(ch) <= end_chapter_seq]
    missing = [seq_from_file(ch) for ch in chapters if not is_chapter_complete(project, ch)]
    if not chapters or missing:
        _write_report(project, kind="content_prerequisites", resume_stage="chapter_analysis", issues=[{
            "owner": "chapter_analysis", "chapters": missing,
            "message": "所选章节分析与 Delta 尚未完成，请回到章节内容阶段续跑。",
        }])
        return CONTENT_PENDING
    last = seq_from_file(chapters[-1])
    cycles = [(start, min(start + 4, last)) for start in range(1, last + 1, 5)]
    issues = [{"owner": "periodic_governance", "range": [start, end],
               "message": "周期治理尚未完成，请由内容阶段继续治理。"}
              for start, end in cycles if not audit_range_is_resolved(project, start, end)]
    if issues:
        _write_report(project, kind="content_prerequisites", issues=issues, resume_stage="chapter_delta")
        return CONTENT_PENDING
    return 0


def report_delivery_quality(project: Path, *, failed_step_code=0) -> int:
    """严格校验并定位责任；章节来源仅作待核线索，不能据此重放旧 Delta。"""
    draft = project / "故事结构_草稿.json"
    try:
        value = json.loads(draft.read_bytes())
        errors = validate_exact_story_schema(value, mode="final")[0]
        errors.extend(introduction_source_errors(project, value))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _write_report(project, kind="quality", issues=[{
            "owner": "maintenance", "path": "", "message": f"交付草稿无法验收：{exc}",
        }])
        return QUALITY_PENDING
    issues = []
    for error in errors:
        match = re.search(r"\[质量字段:([^\]]+)\]", error)
        path = match[1] if match else ""
        # 终稿没有可信的“哪次写入引入缺陷”凭据，不能按涉及章节猜责任周期。
        # 新缺陷已由内容提交门禁处理；终稿残留字段缺项归显式历史维护。
        issue = {"owner": "historical_maintenance", "path": path, "message": error}
        parts = path.strip("/").split("/")
        if parts[:1] == ["介绍"]:
            issue["owner"] = "project_metadata" if parts[-1] == "标题" else "historical_maintenance"
        elif len(parts) >= 3 and parts[0] in COLLECTION_KEYS and parts[1].isdigit():
            records = value.get(parts[0], [])
            index = int(parts[1])
            if isinstance(records, list) and index < len(records) and isinstance(records[index], dict):
                item = records[index]
                detail = item.get("详情", {})
                issue.update(collection=parts[0], name=item.get("名称", ""))
                if isinstance(detail, dict):
                    declared = set()
                    for field in ("涉及章节", "首次章节", "最近章节"):
                        declared.update(parse_chapter_numbers(detail.get(field)))
                    issue["declared_chapters"] = sorted(declared)
                issue["action"] = "缺陷引入阶段未证；选择显式历史缺项维护，或核对原文后修正当前过程库。禁止按章节线索重放旧 Delta。"
        issues.append(issue)
    if not issues and failed_step_code:
        # 外部校验进程故障与内容缺项不同，不能生成“已就绪”报告或诱导模型补写。
        _write_report(project, kind="local_failure", issues=[{
            "owner": "final_delivery", "path": "", "message": f"本地交付步骤失败 rc={failed_step_code}；请查看校验日志后从收尾续跑。",
        }])
        return QUALITY_PENDING
    _write_report(project, kind="quality" if issues else "ready", issues=issues)
    return QUALITY_PENDING if issues else 0


def introduction_source_errors(project: Path, value: dict) -> list[str]:
    """规范化字符串不是来源证据；版本与正文完全匹配的完成凭据仍可复用。"""
    root = project / "质量治理/最终审计/交付"
    source = project / "故事结构_增量.json"
    prepared_path, introduction_path = root / "source.json", root / "introduction.json"
    if not source.is_file() or not prepared_path.is_file():
        return []
    prepared = json.loads(prepared_path.read_bytes())
    signature = digest(source)
    if not isinstance(prepared, dict) or prepared.get("source") != signature:
        return []
    introduction = json.loads(introduction_path.read_bytes()) if introduction_path.is_file() else {}
    if (isinstance(introduction, dict) and introduction.get("source") == signature
            and completed_introduction_matches(introduction.get("introduction"), value.get("介绍"))):
        return []
    return [f"[质量字段:/介绍/{field}] 原始简介字段未提供有效文本，不能把规范化字符串视为内容已完成"
            for field, flag in (("描述", "source_description_valid"), ("标题", "source_title_type_valid"))
            if prepared.get(flag) is False]


def completed_introduction_matches(accepted, current) -> bool:
    """完成凭据接受原文或同一本地排版的派生值，其他字段必须完全相同。"""
    if not isinstance(accepted, dict) or not isinstance(current, dict):
        return False
    if accepted == current:
        return True
    if not isinstance(accepted.get("描述"), str):
        return False
    from synopsis_formatting import format_synopsis_text
    return {**accepted, "描述": format_synopsis_text(accepted["描述"])} == current


class DeliveryBlocked(RuntimeError):
    """让工作台保留失败报告；失败不是成功交付，也不能丢弃可操作的诊断。"""
    def __init__(self, project: Path, code: int):
        report = delivery_report_path(project)
        super().__init__(f"收尾未发布 rc={code}；请按交付诊断回到所属内容阶段处理，或显式补齐历史缺项。")
        try:
            kind = json.loads(report.read_bytes()).get("kind")
        except (OSError, ValueError, AttributeError):
            kind = ""
        self.result = {"task": "finalize", "artifacts": [], "report": str(report),
                       "repair_history_available": code == QUALITY_PENDING and kind == "quality"}
