"""Compare recorded Delta/governance runs on a frozen, explicitly labelled corpus.

This tool never calls a provider or commits a story. A valid JSON schema is not
semantic truth: optional human-authored JSON Pointer checks report only labelled
coverage, and missing timing/usage/labels remain unknown rather than zero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from validate_delta import delta_errors


def evidence_fingerprint(root: Path, names: list[str]) -> str:
    """Bind both arms to the exact ordered input manifest and original bytes."""
    entries = [(name, hashlib.sha256((root / name).read_bytes()).hexdigest()) for name in names]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def labelled_checks(value: dict, checks: list[dict]) -> list[dict]:
    """Evaluate explicit output assertions; never infer facts from model prose."""
    results = []
    for check in checks:
        path = check["path"]
        if not isinstance(path, str) or not path.startswith("/") or "equals" not in check:
            raise ValueError("每条人工标注必须包含 JSON Pointer path 和 equals")
        actual, found = value, True
        try:
            for token in path[1:].split("/"):
                key = token.replace("~1", "/").replace("~0", "~")
                actual = actual[int(key)] if isinstance(actual, list) else actual[key]
        except (KeyError, IndexError, TypeError, ValueError):
            found, actual = False, None
        results.append({"path": path, "passed": found and actual == check["equals"],
                        "expected": check["equals"], "actual": actual})
    return results


def metric(variant: dict, key: str):
    """Reject malformed measurements while preserving absent provider telemetry."""
    value = variant.get(key)
    if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
        raise ValueError(f"{key} 必须是非负有限数或省略")
    return value


def evaluate_manifest(manifest: Path) -> dict:
    """Read two recorded outputs per case and return auditable paired results."""
    root = manifest.resolve().parent
    spec = json.loads(manifest.read_text(encoding="utf-8"))
    report = {"version": 1, "scope": "recorded results; labels are not exhaustive semantic proof", "cases": []}
    for case in spec["cases"]:
        names = case["evidence"]
        if case["story"] not in names or not names or len(names) != len(set(names)):
            raise ValueError("证据清单必须包含基线故事且不重复")
        fingerprint = evidence_fingerprint(root, names)
        story = json.loads((root / case["story"]).read_text(encoding="utf-8"))
        item = {"name": case["name"], "input_fingerprint": fingerprint, "variants": {}, "comparison": None}
        for name in ("baseline", "controlled"):
            variant = case["variants"][name]
            raw = (root / variant["output"]).read_bytes()
            checks, errors, warnings = [], [], []
            try:
                value = json.loads(raw)
                errors, warnings = delta_errors(story, value,
                    mode="governance" if isinstance(value, dict) and "章节范围" in value else "process")
                checks = labelled_checks(value, case.get("checks", []))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                errors = [f"产物无法验收：{exc}"]
            item["variants"][name] = {
                "same_evidence": variant.get("input_fingerprint") == fingerprint,
                "artifact_sha256": hashlib.sha256(raw).hexdigest(),
                "structural_passed": not errors, "errors": errors, "warnings": warnings,
                "label_checks_passed": (not errors and all(x["passed"] for x in checks)) if case.get("checks") else None,
                "label_checks": checks,
                **{key: metric(variant, key) for key in
                   ("elapsed_seconds", "input_tokens", "output_tokens", "tool_calls", "retry_count")}}
        arms = item["variants"]
        if all(arm["same_evidence"] for arm in arms.values()):
            def reduction(key):
                old, new = arms["baseline"][key], arms["controlled"][key]
                return 1 - new / old if old is not None and old > 0 and new is not None else None
            item["comparison"] = {"elapsed_reduction": reduction("elapsed_seconds"),
                                  "input_token_reduction": reduction("input_tokens")}
        report["cases"].append(item)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate_manifest(args.manifest)
    # Only the explicitly requested report is written; corpus paths stay intact.
    from worker_checkpoint import atomic_write_bytes
    atomic_write_bytes(args.output, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"已评测 {len(result['cases'])} 个固定输入；结果：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
