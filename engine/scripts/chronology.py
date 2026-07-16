#!/usr/bin/env python3
"""章节序号、章节时间和事件分组的统一规则。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import re
from typing import Any


BASE_TIME = "0001-01-01T00:00:00"
EVENT_GROUP_RANK_WIDTH = 8
MAX_EVENT_GROUP_NAME_LENGTH = 40
_BASE_DATETIME = datetime(1, 1, 1)
_CHAPTER_NUMBER_RE = re.compile(r"(?<!\d)(\d+)(?!\d)")
_GROUP_ORDER_RE = re.compile(r"^(\d{4,})-(.+)$")
_NUMERIC_GROUP_NAME_RE = re.compile(r"^\d+(?:[-_./ ]\d+)*$")


def parse_chapter_numbers(value: Any) -> list[int]:
    """Extract positive chapter sequence numbers from compact or legacy text."""
    if value is None:
        return []
    if isinstance(value, int):
        return [value] if value > 0 else []
    if isinstance(value, (list, tuple, set)):
        numbers: list[int] = []
        for item in value:
            numbers.extend(parse_chapter_numbers(item))
        return numbers
    return [int(match) for match in _CHAPTER_NUMBER_RE.findall(str(value)) if int(match) > 0]


def chapter_width(numbers: list[int] | None = None) -> int:
    maximum = max(numbers or [0])
    return max(4, len(str(maximum)))


def format_chapter(sequence: int, width: int = 4) -> str:
    if sequence < 1:
        raise ValueError("章节序号必须为正整数")
    return str(sequence).zfill(max(width, len(str(sequence))))


def normalize_chapter_value(value: Any, width: int = 4) -> str:
    numbers = parse_chapter_numbers(value)
    return format_chapter(numbers[0], width) if numbers else ""


def normalize_involved_chapters(value: Any, width: int | None = None) -> str:
    """Return the canonical Chinese-comma, ascending, deduplicated chapter string."""
    numbers = sorted(set(parse_chapter_numbers(value)))
    if not numbers:
        return ""
    actual_width = max(width or 4, chapter_width(numbers))
    return "，".join(format_chapter(number, actual_width) for number in numbers)


def parse_involved_chapters(value: Any) -> list[int]:
    """Parse the canonical field as a sorted unique sequence list."""
    return sorted(set(parse_chapter_numbers(value)))


def chapter_time(sequence: int) -> str:
    if sequence < 1:
        raise ValueError("章节序号必须为正整数")
    dt = _BASE_DATETIME + timedelta(days=sequence - 1)
    # ``strftime('%Y')`` does not zero-pad years below 1000 consistently across
    # C runtimes.  Chapter time is a serialized contract, so format each field
    # explicitly instead of inheriting platform-specific behavior.
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def is_iso_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return value == (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def first_involved_chapter(event: dict[str, Any]) -> int | None:
    detail = event.get("详情", {})
    involved = detail.get("涉及章节") if isinstance(detail, dict) else None
    chapters = parse_involved_chapters(involved)
    return chapters[0] if chapters else None


def last_involved_chapter(event: dict[str, Any]) -> int | None:
    detail = event.get("详情", {})
    involved = detail.get("涉及章节") if isinstance(detail, dict) else None
    chapters = parse_involved_chapters(involved)
    return chapters[-1] if chapters else None


def normalize_event_temporal_fields(event: dict[str, Any], fallback_chapter: Any = None) -> dict[str, Any]:
    """Normalize an event in-place and derive chapter time from its first chapter."""
    detail = event.setdefault("详情", {})
    if not isinstance(detail, dict):
        detail = {}
        event["详情"] = detail

    chapters = normalize_involved_chapters(detail.get("涉及章节"))
    if not chapters:
        chapters = normalize_involved_chapters(
            detail.get("来源章节")
            or detail.get("首次出现章节")
            or detail.get("首次章节")
            or event.get("首次章节")
            or event.get("涉及章节")
            or fallback_chapter
        )
    detail["涉及章节"] = chapters
    parsed = parse_involved_chapters(chapters)
    if parsed:
        event["时间"] = chapter_time(parsed[0])
    elif not is_iso_time(event.get("时间")):
        event["时间"] = BASE_TIME
    return event


def strip_group_order(group: Any) -> str:
    text = str(group or "").strip()
    matched = _GROUP_ORDER_RE.match(text)
    return matched.group(2).strip() if matched else text


def split_event_group(group: Any) -> tuple[int | None, str]:
    """Return the optional visible segment rank and its human-readable name."""
    text = str(group or "").strip()
    matched = _GROUP_ORDER_RE.match(text)
    if matched:
        return int(matched.group(1)), matched.group(2).strip()
    return None, text


def event_group_rank_text(group: Any) -> str | None:
    """Return the visible rank text, preserving width for validation."""
    text = str(group or "").strip()
    matched = _GROUP_ORDER_RE.match(text)
    return matched.group(1) if matched else None


def format_event_group_rank(rank: int) -> str:
    return str(rank).zfill(max(EVENT_GROUP_RANK_WIDTH, len(str(rank))))


def event_group_name_errors(name: Any) -> list[str]:
    text = str(name or "").strip()
    errors: list[str] = []
    if not text:
        return errors
    if _NUMERIC_GROUP_NAME_RE.fullmatch(text):
        errors.append(f"段号后必须是有语义的复合剧情段短名，不能是纯编号[{text}]")
    if "+" in text:
        errors.append(f"复合剧情段短名不得使用+分隔长格式，需压缩为40字以内短语[{text}]")
    if len(text) > MAX_EVENT_GROUP_NAME_LENGTH:
        errors.append(f"复合剧情段短名必须控制在{MAX_EVENT_GROUP_NAME_LENGTH}字以内，实际{len(text)}字[{text}]")
    return errors


def is_semantic_event_group_name(name: Any) -> bool:
    """Whether a plot-segment name contains narrative information.

    The visible number before the dash is only a stable sort key.  A value such
    as ``00000010-0001`` therefore contains no usable grouping information and
    must never survive a governance pass as a valid plot segment.
    """
    text = str(name or "").strip()
    return bool(text) and not event_group_name_errors(text)


def event_group_name(event: dict[str, Any]) -> str:
    """Choose the semantic plot-segment name for an event.

    Existing semantic ``分组`` values stay authoritative.  Legacy numeric-only
    values are replaced by ``详情.剧情段`` when available, rather than being
    re-emitted as the misleading ``段号-章节号`` form.
    """
    _, name = split_event_group(event.get("分组"))
    if is_semantic_event_group_name(name):
        return name
    detail = event.get("详情")
    plot_segment = detail.get("剧情段") if isinstance(detail, dict) else None
    if is_semantic_event_group_name(plot_segment):
        return str(plot_segment).strip()
    return name


def assign_event_group_orders(events: list[dict[str, Any]], *, split_repeated: bool = False) -> None:
    """Assign visible ranks to chapter-local plot-segment runs without moving events.

    A group is a page-level plot segment scoped to its first involved chapter.
    The same short name may be used again in another chapter, but a same-chapter
    non-contiguous reuse remains a separate segment during migration.
    """
    runs: list[tuple[tuple[str, int | None], list[dict[str, Any]]]] = []
    for event in events:
        base_name = event_group_name(event)
        # Do not turn a missing/number-only name into ``00000010-0001``.  Keeping
        # the invalid source value lets validation report the data problem
        # clearly instead of laundering it into a plausible-looking group.
        if not is_semantic_event_group_name(base_name):
            continue
        run_key = (base_name, first_involved_chapter(event))
        if runs and runs[-1][0] == run_key:
            runs[-1][1].append(event)
        else:
            runs.append((run_key, [event]))

    occurrences: dict[tuple[str, int | None], int] = {}
    for run_index, ((base_name, chapter_scope), members) in enumerate(runs, start=1):
        occurrence_key = (base_name, chapter_scope)
        occurrences[occurrence_key] = occurrences.get(occurrence_key, 0) + 1
        display_name = base_name
        if split_repeated and occurrences[occurrence_key] > 1:
            display_name = f"{base_name}续{occurrences[occurrence_key]}"
        rank = run_index * 10
        rank_text = format_event_group_rank(rank)
        for member in members:
            member["分组"] = f"{rank_text}-{display_name}"


def event_group_order_errors(events: list[dict[str, Any]]) -> list[str]:
    """Validate chapter-local, contiguous page groups without moving events."""
    errors: list[str] = []
    active_key: tuple[str, int | None] | None = None
    closed_keys: set[tuple[str, int | None]] = set()
    expected_rank = 10

    for index, event in enumerate(events):
        rank, name = split_event_group(event.get("分组"))
        rank_text = event_group_rank_text(event.get("分组"))
        if not name:
            continue
        if rank is None:
            errors.append(f"事件[{index}].分组 必须为8位段号-40字以内复合剧情段短名，例如00000010-退婚尊严线冲突爆发羞辱反击身份尊严")
            continue
        if rank_text is not None and len(rank_text) < EVENT_GROUP_RANK_WIDTH:
            errors.append(f"事件[{index}].分组 段号必须至少{EVENT_GROUP_RANK_WIDTH}位，实际为{rank_text}")
        name_errors = event_group_name_errors(name)
        if name_errors:
            errors.extend(f"事件[{index}].分组 {error}" for error in name_errors)
            continue
        group_key = (name, first_involved_chapter(event))
        if group_key != active_key:
            if active_key is not None:
                closed_keys.add(active_key)
            if group_key in closed_keys:
                errors.append(f"事件[{index}].分组 在同一章节内非连续复用剧情段名称[{name}]；请拆分为新的剧情段")
            expected_rank_text = format_event_group_rank(expected_rank)
            if rank != expected_rank or rank_text != expected_rank_text:
                actual_rank_text = rank_text or str(rank)
                errors.append(f"事件[{index}].分组 段号应为{expected_rank_text}，实际为{actual_rank_text}")
            active_key = group_key
            expected_rank += 10
        elif rank != expected_rank - 10:
            errors.append(f"事件[{index}].分组 与当前连续剧情段的段号不一致")
    return errors


def recompute_event_group_prefixes(events: list[dict[str, Any]]) -> None:
    """Compatibility alias for callers still using the old function name."""
    assign_event_group_orders(events)


def insert_event_in_chapter_order(events: list[dict[str, Any]], event: dict[str, Any]) -> int:
    """Insert a newly created historical event while preserving equal-sequence order."""
    target = first_involved_chapter(event)
    if target is None:
        events.append(event)
        return len(events) - 1
    insert_at = len(events)
    for index, existing in enumerate(events):
        existing_first = first_involved_chapter(existing)
        if existing_first is not None and existing_first > target:
            insert_at = index
            break
    events.insert(insert_at, event)
    return insert_at


def chronological_event_order_errors(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous: int | None = None
    for index, event in enumerate(events):
        current = first_involved_chapter(event)
        if current is None:
            errors.append(f"事件[{index}]缺少有效涉及章节")
            continue
        if previous is not None and current < previous:
            errors.append(f"事件[{index}]最小涉及章节{format_chapter(current)}早于前一事件")
        previous = current
    return errors


def clone_with_normalized_event_temporal_fields(event: dict[str, Any], fallback_chapter: Any = None) -> dict[str, Any]:
    result = deepcopy(event)
    return normalize_event_temporal_fields(result, fallback_chapter)
