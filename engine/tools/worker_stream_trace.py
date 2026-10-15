"""Observe delivery boundaries without recording novel text or model reasoning.

This is diagnostic metadata only. Incomplete tool arguments are never promoted
to artifacts, and missing fields are never synthesized to satisfy a schema.
"""
from __future__ import annotations

import hashlib
import json
import time


class DeliveryTrace:
    """Track event gaps and complete StructuredOutput argument fingerprints."""

    def __init__(self, schema: dict, *, started: float | None = None):
        self.schema = schema
        self.started = time.monotonic() if started is None else started
        self.first_event = None
        self.last_event = None
        self.max_gap = 0.0
        self.requests = set()
        self.blocks = {}
        self.submissions = {}
        self.result = None

    def _submission(self, identity, arguments, raw: str, now: float):
        """Only inspect complete JSON objects; retain hashes/keys, never values."""
        if not isinstance(arguments, dict):
            return
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.submissions[identity] = {
            "offset_seconds": round(now - self.started, 3),
            "keys": sorted(arguments),
            "missing_required": [key for key in self.schema.get("required", []) if key not in arguments],
            "argument_chars": len(raw),
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    def observe(self, value: dict, *, now: float | None = None):
        now = time.monotonic() if now is None else now
        if value.get("type") not in {"stream_event", "assistant", "user", "result"} and "structured_output" not in value:
            return  # Session initialization/heartbeats are not model progress.
        event = value.get("event", {})
        if (value.get("type") == "stream_event" and isinstance(event, dict)
                and event.get("type") in {"ping", "heartbeat", "keepalive"}):
            return  # Provider keepalives do not deliver model text or arguments.
        if self.last_event is not None:
            self.max_gap = max(self.max_gap, now - self.last_event)
        if self.first_event is None:
            self.first_event = now
        self.last_event = now
        request = value.get("_messageId")
        if isinstance(request, str):
            self.requests.add(request)
        if isinstance(event, dict):
            key = (request, event.get("index"))
            block = event.get("content_block", {})
            if event.get("type") == "content_block_start" and isinstance(block, dict) and block.get("name") == "StructuredOutput":
                self.blocks[key] = {"id": block.get("id", str(key)), "input": block.get("input"), "parts": []}
            elif event.get("type") == "content_block_delta" and key in self.blocks:
                delta = event.get("delta", {})
                if isinstance(delta, dict) and delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                    self.blocks[key]["parts"].append(delta["partial_json"])
            elif event.get("type") == "content_block_stop" and key in self.blocks:
                current = self.blocks.pop(key)
                raw = "".join(current["parts"])
                try:
                    args = json.loads(raw) if raw else current["input"]
                except ValueError:
                    args = None
                self._submission(current["id"], args, raw, now)
        message = value.get("message", {})
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                    args = block.get("input")
                    self._submission(block.get("id", "assistant"), args, json.dumps(args, ensure_ascii=False), now)
        if "structured_output" in value:
            args = value["structured_output"]
            if isinstance(args, dict):
                self.result = {"keys": sorted(args), "sha256": hashlib.sha256(json.dumps(
                    args, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}

    def report(self, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        return {"elapsed_seconds": round(now - self.started, 3),
                "first_event_seconds": round(self.first_event - self.started, 3) if self.first_event is not None else None,
                "silence_seconds": round(now - (self.last_event if self.last_event is not None else self.started), 3),
                "max_event_gap_seconds": round(self.max_gap, 3),
                "model_messages": len(self.requests), "unfinished_submissions": len(self.blocks),
                "submissions": list(self.submissions.values()), "result": self.result}


def valid_terminal_result(value: dict, schema: dict) -> bool:
    """Permit early CLI cleanup only after a complete native success envelope.

    The normal host business validators still run after publication. Tool-call
    arguments, partial JSON, and failed envelopes are never early successes.
    """
    if (value.get("type") != "result" or value.get("is_error")
            or value.get("subtype", "success") != "success"
            or not isinstance(value.get("structured_output"), dict)):
        return False
    # Native StructuredOutput already performs the supplied JSON Schema check.
    # This boundary only requires its success envelope and complete root shape;
    # do not add a second schema engine or replace the host business validators.
    args = value["structured_output"]
    return all(key in args for key in schema.get("required", []))
