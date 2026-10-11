"""Shared, explicit capacity admission for file-backed model tasks.

Character counts are never presented as measured tokens. The portable estimator
is deliberately conservative; CLI-owned tools/history remain unobservable and
are reported separately. No evidence is truncated to make admission succeed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

CONTEXT_EXCEEDED = 64


def estimate_tokens(text: str) -> int:
    """Estimate mixed prose/JSON, retaining a margin for unknown tokenizers."""
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 3 + (len(text) - ascii_count) * 2)


@dataclass(frozen=True)
class ContextBudget:
    input_tokens: int = 300000
    output_tokens: int = 20000
    context_tokens: int = 300000  # Safe shared-window default; explicit zero means documented separate limits.
    reserve_tokens: int = 30000  # Provider instructions, tools and estimation error.

    @classmethod
    def from_env(cls):
        """Read per-process limits without mutating shared backend environment."""
        values = {}
        for key, default in asdict(cls()).items():
            raw = os.environ.get("ND_CONTEXT_" + key.upper(), str(default))
            value = int(raw)
            if value < 0 or (key in {"input_tokens", "output_tokens"} and value == 0):
                raise ValueError(f"Invalid capacity: {key}={raw}")
            values[key] = value
        result = cls(**values)
        if result.usable_input <= 0:
            raise ValueError("上下文容量不足以容纳输出和预留空间")
        return result

    @property
    def usable_input(self) -> int:
        shared = self.context_tokens - self.output_tokens if self.context_tokens else self.input_tokens
        return min(self.input_tokens, shared) - self.reserve_tokens

    def fits(self, text: str) -> bool:
        return estimate_tokens(text) <= self.usable_input


def inspect_pack(pack: Path, extra: str = "", sources=()) -> dict:
    """Count full declared sources once, including recovery material catalogs.

    Only manifests generated beside the pack are interpreted. Arbitrary links in
    novel text are not followed. Reports contain hashes/sizes, never credentials
    or book contents. Sources already embedded verbatim are not counted twice.
    """
    budget = ContextBudget.from_env()
    paths = [pack, *sources]
    catalog = pack.parent / "materials.json"
    if catalog.is_file():
        value = json.loads(catalog.read_text(encoding="utf-8"))
        for item in value.get("materials", []):
            source = (pack.parent / item["path"]).resolve()
            source.relative_to(pack.parent.resolve())
            paths.append(source)
    manifest = pack.with_suffix(pack.suffix + ".sources.json")
    if manifest.is_file():
        for item in json.loads(manifest.read_text(encoding="utf-8")):
            paths.append(Path(item))
    documents, records = [], []
    for path in dict.fromkeys(Path(p).resolve() for p in paths):
        content = path.read_text(encoding="utf-8")
        documents.append(content)
        records.append({"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "chars": len(content)})
    unique = []
    for content in sorted(documents, key=len, reverse=True):
        if not any(content in previous for previous in unique):
            unique.append(content)
    tokens = estimate_tokens("\n".join(unique) + extra)
    return {"version": 1, "limits": asdict(budget), "usable_input_tokens": budget.usable_input,
            "estimated_input_tokens": tokens, "measurement": "conservative_estimate",
            "passed": tokens <= budget.usable_input, "sources": records,
            "runtime_tool_usage": "not_observable_by_cli_adapter"}


def admit_pack(pack: Path, extra: str = "", sources=()) -> bool:
    """Persist an admission receipt; overflow never truncates the input pack."""
    report = inspect_pack(pack, extra, sources)
    from worker_checkpoint import atomic_write_bytes
    atomic_write_bytes(pack.with_suffix(pack.suffix + ".capacity.json"),
                       json.dumps(report, ensure_ascii=False, indent=2).encode())
    if not report["passed"]:
        print(f"任务材料超过容量预算：估算 {report['estimated_input_tokens']} token，"
              f"可用 {report['usable_input_tokens']}；完整材料已保留，需要重新分包。", flush=True)
    return report["passed"]
