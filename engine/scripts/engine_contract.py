"""校验语义契约版本：提交回执的版本门禁依据。

回执（*.commit.json）的意义是"这份产物在**某个版本的程序**下被判为通过"。
如果程序之后收紧了校验、修好了纠错或改了结构规则，旧回执就不再能证明
"当前程序也会接受它"。本模块给出一个可审计的契约指纹，让回执可以被
判定为"同版本、可复用"或"跨版本、必须重新校验"。

纳入范围只包含决定"什么算通过"的文件：
- schemas/*.json（产物结构契约）
- 校验器、确定性纠错、结构规则与受控类型注册表

刻意**不**纳入提示词（prompts/*.j2）、包装器与调度脚本：改写提示词不应让
已通过的历史回执全部失效并触发大规模重算——那只会让流水线变慢，
不会让结果更正确。提示词相关性由各自的检查点（容量分层/分包检查点）负责，
它们已经按内容哈希做了失效判定。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 决定校验语义的脚本。新增或替换校验规则时必须在这里登记，
# 否则跨版本的门禁会漏判。
_CONTRACT_SCRIPTS = (
    "scripts/validate_delta.py",
    "scripts/validate_structure.py",
    "scripts/validate_chapter_analysis.py",
    "scripts/coerce_delta.py",
    "scripts/compress_tags.py",
    "scripts/normalize_story_schema.py",
    "scripts/story_schema_rules.py",
    "scripts/registered_types.py",
    "scripts/governance_contract.py",
    "scripts/generation_contract.py",
    "scripts/local_json_patch.py",
    "scripts/apply_governance_ops.py",
    "scripts/chronology.py",
    "scripts/element_identity.py",
)

_cache: str | None = None


def contract_files(engine: Path | None = None) -> tuple[Path, ...]:
    """按固定顺序返回全部契约文件，便于测试与审计。"""
    root = engine or Path(__file__).resolve().parents[1]
    schemas = tuple(sorted((root / "schemas").glob("*.json")))
    return schemas + tuple(root / name for name in _CONTRACT_SCRIPTS)


def contract_version(engine: Path | None = None) -> str:
    """返回契约指纹短哈希；同一份代码在同一进程内只计算一次。

    缺失文件以相对路径参与哈希（而不是被跳过），这样"删除一个校验器"
    同样会改变版本，不会被静默忽略。
    """
    global _cache
    if engine is None and _cache is not None:
        return _cache
    digest = hashlib.sha256()
    root = engine or Path(__file__).resolve().parents[1]
    for path in contract_files(engine):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes() if path.is_file() else b"")
    value = digest.hexdigest()[:16]
    if engine is None:
        _cache = value
    return value


def reset_cache() -> None:
    """仅供测试：清空进程内缓存，使改动后的契约文件重新参与计算。"""
    global _cache
    _cache = None


# 兼容命名：调用方按"提交回执契约"理解也无歧义。
receipt_contract_version = contract_version
