"""Recorded A/B results need identical evidence and explicit semantic labels."""
import hashlib
import json

import pytest
from test_audit_pack import empty_delta, empty_story, write_json


def fixture(root):
    """One frozen corpus, two recorded providers; no live model or book writes."""
    write_json(root / "story.json", empty_story())
    (root / "source.md").write_bytes("一封来信。\n".encode("utf-8"))
    write_json(root / "baseline.json", empty_delta(1))
    write_json(root / "controlled.json", empty_delta(1))
    evidence = ["story.json", "source.md"]
    fingerprint = hashlib.sha256(json.dumps(
        [(name, hashlib.sha256((root / name).read_bytes()).hexdigest()) for name in evidence],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    case = {"name": "chapter-1", "story": "story.json", "evidence": evidence,
            "checks": [{"path": "/章节", "equals": "第001章"}], "variants": {
                "baseline": {"output": "baseline.json", "input_fingerprint": fingerprint,
                             "elapsed_seconds": 10, "input_tokens": 100},
                "controlled": {"output": "controlled.json", "input_fingerprint": fingerprint,
                               "elapsed_seconds": 6, "input_tokens": 30}}}
    manifest = root / "benchmark.json"
    write_json(manifest, {"cases": [case]})
    return manifest, case


def test_benchmark_compares_matching_inputs_and_preserves_artifacts(tmp_path):
    from runtime_benchmark import evaluate_manifest
    manifest, _ = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    report = evaluate_manifest(manifest)
    case = report["cases"][0]
    assert case["comparison"]["elapsed_reduction"] == pytest.approx(0.4)
    assert case["comparison"]["input_token_reduction"] == pytest.approx(0.7)
    assert case["variants"]["controlled"]["label_checks_passed"] is True
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_benchmark_rejects_changed_evidence_and_exposes_semantic_miss(tmp_path):
    from runtime_benchmark import evaluate_manifest
    manifest, case = fixture(tmp_path)
    case["checks"] = [{"path": "/新增元素/角色集/0/名称", "equals": "主角"}]
    case["variants"]["controlled"]["input_fingerprint"] = "outdated"
    write_json(manifest, {"cases": [case]})
    report = evaluate_manifest(manifest)["cases"][0]
    assert report["comparison"] is None
    assert report["variants"]["baseline"]["label_checks_passed"] is False
    assert report["variants"]["controlled"]["same_evidence"] is False


def test_unlabelled_results_never_claim_semantic_accuracy(tmp_path):
    from runtime_benchmark import evaluate_manifest
    manifest, case = fixture(tmp_path)
    case.pop("checks")
    case["variants"]["controlled"].pop("elapsed_seconds")
    write_json(manifest, {"cases": [case]})
    report = evaluate_manifest(manifest)["cases"][0]
    assert report["variants"]["controlled"]["label_checks_passed"] is None
    assert report["comparison"]["elapsed_reduction"] is None
