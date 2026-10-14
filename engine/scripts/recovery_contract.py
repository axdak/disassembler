"""Internal recovery messages: shared provider schema and local acceptance rules.

These contracts never change chapter Delta or governance output requirements.
Local checks also cover providers that ignore their native JSON schema option.
"""
from __future__ import annotations

import re
from local_json_patch import PATCH_SCHEMA, patch_shape_errors

CONTRACT_INVALID = 66
# These recovery protocols can query full frozen stories without a global index.
# Keep the scheduler and provider capability allowlist on the same task names.
MCP_RECOVERY_TASKS = frozenset({
    "auto_repair_chapter", "auto_audit_repair", "recovery_patch",
    "recovery_diagnosis", "recovery_review",
})
REVIEW_CHECKS = ("content_preserved", "contract_unchanged", "evidence_supported")
TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
RECOVERY_SCHEMAS = {
    "recovery_patch": PATCH_SCHEMA,
    "recovery_diagnosis": {
        "type": "object", "additionalProperties": False,
        "required": ["diagnosis", "steps"],
        "properties": {"diagnosis": TEXT,
                       "steps": {"type": "array", "minItems": 1, "items": TEXT}},
    },
    "recovery_review": {
        "type": "object", "additionalProperties": False,
        "required": ["passed", "candidate_sha256", "checks", "issues", "reason"],
        "properties": {
            "passed": {"type": "boolean"},
            "candidate_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "checks": {"type": "object", "additionalProperties": False,
                       "required": list(REVIEW_CHECKS),
                       "properties": {key: {"type": "boolean"} for key in REVIEW_CHECKS}},
            "issues": {"type": "array", "items": TEXT}, "reason": TEXT,
        },
    },
}


def recovery_output_errors(task: str, value) -> list[str]:
    """Validate internal message shape; success/evidence decisions stay upstream."""
    if task == "recovery_patch":
        return patch_shape_errors(value)
    schema = RECOVERY_SCHEMAS.get(task)
    if schema is None:
        return []
    if not isinstance(value, dict) or set(value) != set(schema["required"]):
        return [f"{task} 必须且只能包含字段：{', '.join(schema['required'])}"]

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    if task == "recovery_diagnosis":
        valid = (text(value["diagnosis"]) and isinstance(value["steps"], list)
                 and bool(value["steps"]) and all(text(step) for step in value["steps"]))
        return [] if valid else ["diagnosis 必须是非空字符串；steps 必须是非空字符串数组"]
    checks = value["checks"]
    valid = (type(value["passed"]) is bool and text(value["candidate_sha256"])
             and re.fullmatch(r"[0-9a-f]{64}", value["candidate_sha256"]) is not None
             and isinstance(checks, dict) and set(checks) == set(REVIEW_CHECKS)
             and all(type(item) is bool for item in checks.values())
             and isinstance(value["issues"], list) and all(text(item) for item in value["issues"])
             and text(value["reason"]))
    return [] if valid else ["复核必须包含布尔结论、64位候选摘要、三项布尔检查、问题数组及非空依据"]
