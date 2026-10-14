"""Durable task candidates with idempotent edits and authoritative validation.

This module never writes the live story. A completion receipt identifies exact
candidate bytes; the host still runs its normal merge/structure/publication gates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from project_lock import project_write_lock
from worker_checkpoint import atomic_write_bytes

MAX_CANDIDATE_BYTES = 512 * 1024
MAX_OPERATIONS = 64


def encoded(value) -> bytes:
    """Canonical bytes bind operation identities independent of JSON formatting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def signature(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


class CandidateSession:
    """One frozen evidence version and one recoverable candidate per task."""

    def __init__(self, directory: Path, story: Path, task: str, chapter: str = ""):
        if task not in {"delta", "audit_correction"}:
            raise ValueError("候选任务类型未授权")
        self.directory, self.story, self.task, self.chapter = directory, story, task, chapter
        self.state_path = directory / "candidate.json"
        self.base_revision = hashlib.sha256(story.read_bytes()).hexdigest()

    def _load(self) -> dict:
        if hashlib.sha256(self.story.read_bytes()).hexdigest() != self.base_revision:
            raise ValueError("冻结故事版本已变化")
        if not self.state_path.exists():
            return {"version": 1, "task": self.task, "chapter": self.chapter,
                    "base_revision": self.base_revision, "revision": 0,
                    "delta": None, "sha256": signature(None), "operations": {}}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if (value.get("version") != 1 or value.get("task") != self.task
                or value.get("chapter") != self.chapter or value.get("base_revision") != self.base_revision):
            raise ValueError("候选任务或故事版本已变化，请重建任务")
        if value.get("sha256") != signature(value.get("delta")):
            raise ValueError("候选状态损坏，保留现场并拒绝交付")
        return value

    def _save(self, state):
        atomic_write_bytes(self.state_path, encoded(state))

    @staticmethod
    def _receipt(state):
        return {"revision": state["revision"], "candidate_sha256": state["sha256"],
                "base_revision": state["base_revision"]}

    def status(self) -> dict:
        with project_write_lock(self.directory):
            state = self._load()
            return {**self._receipt(state), "has_candidate": state["delta"] is not None,
                    "completed": state.get("completed") == self._receipt(state),
                    "accepted": state.get("accepted") == self._receipt(state),
                    "operations_used": len(state["operations"])}

    def _scope(self, delta):
        from validate_delta import ALLOWED_TOP_KEYS
        if not isinstance(delta, dict) or set(delta) - ALLOWED_TOP_KEYS:
            raise ValueError("候选只允许标准 Delta/治理字段")
        if len(encoded(delta)) > MAX_CANDIDATE_BYTES:
            raise ValueError("候选超过512KiB；应拆分宿主任务，不能截断内容")
        if self.task == "delta":
            if delta.get("治理操作"):
                raise ValueError("章节候选不允许治理操作")
            # The host pins the chapter. A model cannot silently switch scope.
            expected = re.match(r"第?(\d+)章", self.chapter)
            # The shared Delta template emits "0001"; legacy display labels use
            # "第001章" with an optional title. Accept both without accepting a
            # numeric prefix of another range or coercing a non-string value.
            label = delta.get("章节", "")
            actual = re.match(r"第?(\d+)(?:章|$)", label) if isinstance(label, str) else None
            if not expected or not actual or int(expected[1]) != int(actual[1]):
                raise ValueError("候选章节与宿主任务不一致")
        elif self._range(delta.get("章节范围", "")) != self._range(self.chapter):
            raise ValueError("候选治理范围与宿主任务不一致")

    @staticmethod
    def _range(value):
        """Accept the two established numeric chapter-range spellings only."""
        match = re.fullmatch(r"第?(\d+)(?:章)?\s*[-至]\s*第?(\d+)章", str(value))
        if not match:
            raise ValueError("章节范围格式无效")
        return tuple(map(int, match.groups()))

    def _mutate(self, operation_id, expected_revision, payload, transform, *, complete=False):
        if (not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", operation_id)
                or type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("需要操作ID和非负候选版本")
        request_hash = signature([expected_revision, payload])
        with project_write_lock(self.directory):
            state = self._load()
            previous = state["operations"].get(operation_id)
            if previous:
                if previous["request"] != request_hash:
                    raise ValueError("操作ID已用于不同参数，禁止重复应用")
                return previous["receipt"]
            if state["revision"] != expected_revision:
                raise ValueError("候选版本冲突；先查询candidate_status再决定修改")
            if len(state["operations"]) >= MAX_OPERATIONS:
                raise ValueError("候选操作预算耗尽，保留断点")
            delta = transform(copy.deepcopy(state["delta"]))
            self._scope(delta)
            state.update(delta=delta, revision=state["revision"] + 1, sha256=signature(delta))
            state.pop("completed", None)
            state.pop("accepted", None)
            receipt = self._receipt(state)
            if complete:
                # One durable transition carries the proposal, its validation
                # feedback and (only when valid) its exact completion receipt.
                validation = self._validate(state, state["revision"])
                if validation["passed"]:
                    state["completed"] = receipt
                receipt = {**validation, "completion": receipt if validation["passed"] else None}
            state["operations"][operation_id] = {"request": request_hash, "receipt": receipt}
            self._save(state)  # Candidate and receipt become durable together.
            return receipt

    def stage(self, operation_id: str, expected_revision: int, delta: dict) -> dict:
        """Replace the private candidate; invalid facts remain private for correction."""
        return self._mutate(operation_id, expected_revision, ["stage", delta], lambda _: delta)

    def submit(self, operation_id: str, expected_revision: int, delta: dict) -> dict:
        """Stage, validate and conditionally sign without three model round trips."""
        return self._mutate(operation_id, expected_revision, ["submit", delta],
                            lambda _: delta, complete=True)

    def patch(self, operation_id: str, expected_revision: int, changes: list) -> dict:
        """Set bounded JSON Pointer fields inside the candidate, never arbitrary files."""
        if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
            raise ValueError("changes必须包含1..50项")

        def apply(delta):
            if not isinstance(delta, dict):
                raise ValueError("请先stage完整候选")
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or set(change) != {"path", "value"}:
                    raise ValueError("每项只能包含path和value")
                path = change["path"]
                if not isinstance(path, str) or not path.startswith("/") or path in seen:
                    raise ValueError("路径无效或重复")
                seen.add(path)
                parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
                from validate_delta import ALLOWED_TOP_KEYS
                if parts[0] not in ALLOWED_TOP_KEYS:
                    raise ValueError("候选路径未授权")
                node = delta
                for part in parts[:-1]:
                    try:
                        node = node[int(part)] if isinstance(node, list) and part.isdecimal() else node[part]
                    except (KeyError, IndexError, TypeError):
                        raise ValueError("父字段不存在；请先读取候选") from None
                last = parts[-1]
                if isinstance(node, dict):
                    node[last] = change["value"]
                elif isinstance(node, list) and last.isdecimal() and int(last) < len(node):
                    node[int(last)] = change["value"]
                else:
                    raise ValueError("字段路径不存在")
            return delta

        return self._mutate(operation_id, expected_revision, ["patch", changes], apply)

    def read(self, offset: int = 0, limit: int = 8000):
        """Read back exact staged content when recovering a lost session."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
            raise ValueError("分页参数无效")
        with project_write_lock(self.directory):
            state = self._load()
            text = encoded(state["delta"]).decode("utf-8")
            if offset > len(text):
                raise ValueError("offset超出候选")
            end = min(len(text), offset + limit)
            return {**self._receipt(state), "text": text[offset:end],
                    "next_offset": end if end < len(text) else None}

    def _validate(self, state, expected_revision):
        if type(expected_revision) is not int or state["revision"] != expected_revision:
            raise ValueError("候选版本冲突")
        from validate_delta import delta_errors
        story = json.loads(self.story.read_text(encoding="utf-8"))
        try:
            errors, warnings = delta_errors(story, state["delta"],
                                            "process" if self.task == "delta" else "governance")
        except (TypeError, KeyError, AttributeError) as exc:
            # Legacy stage/patch can persist malformed shapes for later repair.
            # These are failed candidates, not a reason to block reconnection.
            # Snapshot I/O, identity and revision errors still fail closed above.
            errors, warnings = [f"候选字段类型错误，读取候选并按工具字段契约修正：{exc}"], []
        if self.task == "audit_correction" and isinstance(state["delta"], dict):
            start, end = self._range(self.chapter)
            declared = state["delta"].get("证据范围", [])
            covered = set()
            for label in declared if isinstance(declared, list) else []:
                match = re.fullmatch(r"第?(\d+)章", str(label))
                if match:
                    covered.add(int(match[1]))
            if covered != set(range(start, end + 1)):
                errors.append("证据范围必须逐章覆盖宿主审计窗口；不能跳过章节或声明窗口外证据")
        return {**self._receipt(state), "passed": not errors, "errors": errors, "warnings": warnings}

    def validate(self, expected_revision: int):
        with project_write_lock(self.directory):
            return self._validate(self._load(), expected_revision)

    def finish(self, expected_revision: int):
        """Sign validated candidate identity; this is not a story commit."""
        with project_write_lock(self.directory):
            state = self._load()
            if not self._validate(state, expected_revision)["passed"]:
                raise ValueError("候选校验失败；调用candidate_validate并修正具体错误")
            receipt = self._receipt(state)
            state["completed"] = receipt
            self._save(state)
            return receipt

    def completed_payload(self, receipt: dict, *, confirm_delivery: bool = False):
        """Verify the candidate; only the host can confirm final model delivery.

        A successful tool call can be an intermediate proposal. Persist the
        accepted checkpoint only after the host observes StructuredOutput, so a
        stopped probe cannot silently become the final artifact on retry.
        """
        with project_write_lock(self.directory):
            state = self._load()
            if receipt != self._receipt(state) or state.get("completed") != receipt:
                raise ValueError("候选完成凭证或版本不匹配")
            if not self._validate(state, receipt["revision"])["passed"]:
                raise ValueError("候选复验失败")
            if confirm_delivery:
                state["accepted"] = receipt
                self._save(state)
            return state["delta"]
