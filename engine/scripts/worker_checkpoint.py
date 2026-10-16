"""Content-addressed checkpoints for successful full-book partial outputs.

Only fully embedded task packs are eligible. Changed evidence, worker command,
provider configuration, or worker/prompt code invalidates the checkpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from merge_delta import save_json
from context_budget import ContextBudget
from code_fingerprint import code_digest
from story_source import declared_story_source


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace one generated artifact only after its bytes are durably written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_checkpointed_partial(worker, command: str, project: Path, task: str,
                             pack: Path, output: Path, *, reuse: bool = True, **kwargs) -> int:
    """Reuse an integrity-checked partial, excluding only its transient output path."""
    text = pack.read_text(encoding="utf-8")
    # The wrapper embeds all evidence; only its output location changes per run.
    # Do not strip dates or paths elsewhere: they may be real story evidence.
    header, separator, material = text.partition("## 原任务包内容\n\n")
    if not separator:
        return worker(command, project, task, pack, output, **kwargs)
    header = header.replace(str(output), "<PARTIAL_OUTPUT>")
    engine = Path(__file__).resolve().parents[1]
    code_hash = hashlib.sha256()
    for path in sorted([*engine.joinpath("prompts").rglob("*.j2"),
                        *engine.joinpath("schemas").glob("*.json"),
                        *(engine / "scripts" / name for name in (
                            "capacity_tree.py", "bounded_generation.py", "context_budget.py",
                            "run_pipeline.py", "analysis_context_pack.py", "worker_checkpoint.py",
                            "code_fingerprint.py", "prompt_loader.py", "synopsis_contract.py", "story_source.py")),
                        engine / "tools" / "agent_worker.py",
                        engine / "tools" / "worker_context.py"]):
        code_hash.update(str(path.relative_to(engine)).encode())
        code_hash.update(code_digest(path).encode())
    settings = {key: value for key, value in os.environ.items()
                if key.startswith(("ND_", "OPENAI_", "ANTHROPIC_", "GEMINI_"))}
    settings.update(ContextBudget.from_env().to_env())
    # Track exactly the archive declared in this task, including unembedded
    # detail fields. Legacy packs keep their original process-library behavior.
    story = declared_story_source(project, text) or project / "故事结构_增量.json"
    story_digest = hashlib.sha256(story.read_bytes() if story.is_file() else b"").hexdigest()
    hierarchy = pack.with_suffix(".hierarchy.json")
    hierarchy_identity = None
    if hierarchy.is_file():
        metadata = json.loads(hierarchy.read_text(encoding="utf-8"))
        source_digests = []
        for value in metadata.get("sources", []):
            source = Path(value).resolve()
            source.relative_to(project.resolve())
            source_digests.append(hashlib.sha256(source.read_bytes()).hexdigest())
        # Use content identities, not timestamped task-directory paths, so an
        # equivalent regenerated manifest can still reuse its checked partial.
        hierarchy_identity = [metadata.get("contract"), source_digests]
    identity = json.dumps([1, command, task, header + separator + material, [str(story.resolve()), story_digest],
                           settings, code_hash.hexdigest(), hierarchy_identity], sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(identity.encode()).hexdigest()
    directory = project / "质量治理" / "分包检查点"
    artifact, receipt = directory / f"{digest}.md", directory / f"{digest}.json"
    try:
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        content = artifact.read_bytes()
        if reuse and content.strip() and saved.get("sha256") == hashlib.sha256(content).hexdigest():
            atomic_write_bytes(output, content)
            print(f"已恢复通过检查的分析分包：{output.name}，无需重复请求模型。", flush=True)
            return 0
    except (OSError, ValueError, AttributeError):
        pass  # Missing/corrupt checkpoints never masquerade as completed work.
    rc = worker(command, project, task, pack, output, **kwargs)
    if rc == 0 and output.is_file() and output.read_bytes().strip():
        content = output.read_bytes()
        atomic_write_bytes(artifact, content)
        save_json(receipt, {"sha256": hashlib.sha256(content).hexdigest()})
    return rc
