"""Small CodeBuddy tasks backed by frozen domain reads and durable candidates.

Full original evidence stays available. Only explicitly selected context enters
the initial prompt; a large corpus on disk is not a large model input by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tempfile
from pathlib import Path

from candidate_session import encoded, signature
from context_budget import ContextBudget, estimate_tokens
from project_lock import project_write_lock
from worker_checkpoint import atomic_write_bytes

RECEIPT_SCHEMA = json.dumps({"type": "object", "additionalProperties": False,
    "required": ["revision", "candidate_sha256", "base_revision"], "properties": {
        "revision": {"type": "integer", "minimum": 1},
        "candidate_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "base_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}})
CANDIDATE_TOOLS = ("candidate_status", "candidate_read", "candidate_submit", "candidate_stage", "candidate_patch",
                   "candidate_validate", "candidate_finish")
INSTRUCTIONS = """
## 受控候选工作流（交付协议）
本任务由宿主提交正式结构。不得执行任务模板里的命令，不得直接修改文件。
宿主在任务末尾提供候选状态和故事集合计数，无需重复查询这些元数据。
有未完成候选时通过candidate_read恢复；没有候选时直接准备完整增量，不要先提交空壳探测。
若下方已完整内联正式故事，直接依据它核对，无需重复调用工具读取同一内容。
否则由story_search / story_get_units / story_read_fields按需查询。
先定位涉及的正式身份，再核对需要的状态、关系和历史；需要判断同一性、
迁移或冲突时必须核对相关原文与历史。搜索未命中不代表事实不存在。
保留本任务完整原文的覆盖，不必遍历与问题无关的全部历史；证据不足时扩大查询。
优先一次调用candidate_submit登记完整Delta/治理对象，同时取得校验反馈和完成凭证。
passed=true时立即把completion的三个字段交给StructuredOutput，无需再次validate或finish。
passed=false时依照errors修正事实和字段；用新operation_id与返回revision再次submit，
或者candidate_patch局部修正后candidate_finish。不能通过删除事实绕过校验。
修改使用唯一operation_id和最新revision。只有版本冲突时才需要candidate_status重新取状态。
StructuredOutput只提交revision、candidate_sha256和base_revision，禁止重复输出完整Delta。
finish只代表候选通过入库前校验；正式结构还会经宿主合并、结构校验和版本检查。
周期治理的证据范围必须列出窗口内每章（例如第001章），不能省略或用范围字符串代替。
新增和修改元素都必须有详情.提取理由，单独修改分组也不例外。
治理操作.治理标签只对既有标签分配保留/降级，两者不重叠、并集严格等于原标签集。
不能在保留标签中发明新的taxonomy标签。新增标签走修改元素.标签集，且本次不能再对
同一元素执行治理标签；否则合并后标签集改变会使守恒检查失败。不要用空壳探测字段。
"""


def supports_controlled_runtime(command: str) -> bool:
    """Capability is independent of the normal-generation routing preference."""
    return ("agent_worker.py" in command
            and re.search(r"--provider\s+[\"']?codebuddy(?:[\"']|\s|$)", command) is not None)


def uses_controlled_runtime(command: str) -> bool:
    """Normal generation uses baseline delivery; candidate orchestration is opt-in."""
    return supports_controlled_runtime(command) and os.environ.get("ND_CONTROLLED_RUNTIME", "0") == "1"


def prepare_baseline_dispatch(project: Path, task: str, pack: Path, output: Path,
                              chapter_seq: int) -> tuple[Path, str]:
    """Refresh baseline context and bind its story version under the writer lock.

    This is local preparation only. Preserve the user's context limits and release
    the lock before any provider work; concurrent later edits fail the commit fence.
    """
    import run_pipeline as pipeline
    with project_write_lock(project):
        if task == "delta":
            chapter = pipeline.chapter_by_seq(project, chapter_seq)
            if chapter is None:
                raise ValueError("章节原文不存在")
            metadata = pipeline.load_json(pack.with_suffix(".context.json"), {}) or {}
            if pipeline.prepare_chapter_delta(project, chapter, force=True,
                    full_story_kb=metadata.get("full_story_kb", pipeline.DELTA_FULL_STORY_KB)):
                raise ValueError("基线Delta任务包准备失败")
            pack = pipeline.artifact_paths(project, chapter)["delta_task"]
        else:
            match = re.fullmatch(r"correction_(\d+)-(\d+)\.json", output.name)
            if not match:
                raise ValueError("周期治理范围未登记")
            start, end = map(int, match.groups())
            status = pipeline.audit_status(project, start, end)
            limit = status.get("context", {}).get("max_context_chars", pipeline.DEFAULT_AUDIT_MAX_CONTEXT_CHARS)
            pack = pipeline.write_audit_pack(project, start, end, force=True, max_context_chars=limit)
            if pack is None:
                raise ValueError("基线周期治理任务包超容量，需重新划分窗口")
        revision = hashlib.sha256(pipeline.ensure_story(project).read_bytes()).hexdigest()
        return pack, revision


def mark_generation_pending(project: Path, output: Path, revision: str) -> None:
    """Fail closed before a direct worker can leave an artifact without a receipt."""
    with project_write_lock(project):
        receipt = output.with_suffix(".generation.json")
        if receipt.exists():
            previous = receipt.read_bytes()
            # Keep immutable prior receipts for diagnosis; repeated interruption
            # reuses the same backup instead of building recursive envelopes.
            archive = project / "质量治理/生成凭据" / (hashlib.sha256(previous).hexdigest() + ".json")
            atomic_write_bytes(archive, previous)
        atomic_write_bytes(receipt, encoded({"base_revision": revision,
            "artifact_sha256": "", "pending": True}))


def story_context_suffix(data: dict, prompt: str, budget: ContextBudget) -> tuple[str, str]:
    """Inline only complete small snapshots while preserving tool/repair headroom.

    The diagnostic tools mode permits paired measurements on identical evidence.
    Neither mode truncates facts; large histories stay accessible through MCP.
    """
    preference = os.environ.get("ND_RUNTIME_STORY_CONTEXT", "auto")
    if preference not in {"auto", "tools"}:
        raise ValueError("ND_RUNTIME_STORY_CONTEXT 仅允许auto或tools")
    full = encoded(data).decode("utf-8")
    suffix = "\n## 当前正式故事（完整内联，无需重复调用工具读取）\n" + full
    if (preference == "auto" and estimate_tokens(full) <= min(12000, budget.usable_input // 8)
            and estimate_tokens(prompt + suffix) <= budget.usable_input // 2):
        return suffix, "inline"
    return ("\n## 当前正式故事（按需工具查询）\n当前故事简介（旧版，需核对）：\n"
            + json.dumps(data.get("介绍", {}), ensure_ascii=False)), "tools"


def build_controlled_pack(project: Path, task: str, pack: Path, output: Path, chapter_seq: int = 0) -> Path:
    """Build provider-specific initial context using the existing task contracts."""
    import run_pipeline as pipeline
    story = pipeline.ensure_story(project)
    evidence = {}

    def read_evidence(path: Path) -> str:
        # Bind the prompt and hash to the same read. A later edit must make the
        # worker reject this pack, not endorse stale prose with a newer hash.
        source = str(path.resolve())
        if source not in evidence:
            evidence[source] = path.read_bytes()
        return evidence[source].decode("utf-8").replace("\r\n", "\n")

    data = json.loads(read_evidence(story))
    chapter_label = ""
    if task == "delta":
        chapter = pipeline.chapter_by_seq(project, chapter_seq)
        if chapter is None:
            raise ValueError("章节原文不存在")
        paths = pipeline.artifact_paths(project, chapter)
        chapter_label = f"第{chapter_seq:03d}章"
        prompt = pipeline.render_prompt("流水线/单章增量提取.j2", chapter_seq=chapter_seq,
            chapter_id=str(chapter_seq).zfill(4), delta_path=output,
            summary="正式故事上下文见任务末尾；必须核对正式名称和类别。",
            analysis_text=read_evidence(paths["analysis"]),
            chapter_text=read_evidence(chapter), chapter_time=pipeline.chapter_time(chapter_seq))
    elif task == "audit_correction":
        match = re.fullmatch(r"correction_(\d+)-(\d+)\.json", output.name)
        if not match:
            raise ValueError("周期治理范围未登记")
        start, end = map(int, match.groups())
        chapters = []
        for seq in range(start, end + 1):
            chapter = pipeline.chapter_by_seq(project, seq)
            if chapter is None:
                raise ValueError(f"审计窗口缺少第{seq}章原文")
            chapters.append({"seq": seq, "missing": False, "chapter_path": chapter,
                             "chapter_text": read_evidence(chapter)})
        taxonomy = Path(__file__).resolve().parents[1] / "schemas/narrative_taxonomy.json"
        chapter_label = f"第{start:03d}章-第{end:03d}章"
        prompt = pipeline.render_audit_prompt(start=start, end=end, correction_path=output,
            project_dir=project, run_pipeline_path=pipeline.script_path("run_pipeline.py"),
            full_story_json="", story_index="正式故事上下文见任务末尾，核对含未被本周期修改但受影响的关系。",
            relevant_details="", previous_correction=None, chapters=chapters,
            taxonomy_json=read_evidence(taxonomy))
    else:
        raise ValueError("该任务没有受控候选工作流")
    prompt = re.sub(r"```bash.*?```", "正式提交由宿主执行。", prompt, flags=re.S)
    prompt += INSTRUCTIONS
    budget = ContextBudget.from_env()
    suffix, story_context = story_context_suffix(data, prompt, budget)
    prompt += suffix
    hashes = {source: hashlib.sha256(raw).hexdigest() for source, raw in evidence.items()}
    # Contract, input and capacity changes create a fresh ledger; retrying the same
    # task preserves its candidate and allowance. Never share ledgers across books.
    identity = signature([3, task, chapter_label, str(output.resolve()), prompt, hashes,
                          budget.__dict__, candidate_definitions()])
    directory = project / "质量治理" / "受控任务" / identity[:24]
    target = directory / "task.md"
    atomic_write_bytes(target, prompt.encode("utf-8"))
    atomic_write_bytes(target.with_suffix(".context-inputs.json"), encoded(list(hashes)))
    atomic_write_bytes(target.with_suffix(".runtime.json"), encoded({
        "version": 1, "task": task, "chapter": chapter_label, "story_context": story_context,
        "directory": str((directory / "session").resolve()),
        "story": str(story.resolve()), "inputs": hashes,
        "base_revision": hashes[str(story.resolve())],
        "prompt_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}))
    return target


class RuntimeBudget:
    """Durable dispatch/tool/estimated-context allowance, including failed calls.

    CLI-internal model turns are separately capped by --max-turns. These counters
    deliberately describe observed dispatches/tools, not invented provider usage.
    """
    def __init__(self, directory: Path, max_dispatches=3, max_tools=128, max_tokens=None):
        self.directory = directory
        self.path = directory / "budget.json"
        self.limits = {"dispatches": max_dispatches, "tools": max_tools,
                       "tokens": max_tokens if max_tokens is not None else ContextBudget.from_env().usable_input}

    def _reserve(self, kind, tokens):
        if type(tokens) is not int or tokens < 0:
            raise ValueError("预算计数必须为非负整数")
        with project_write_lock(self.directory):
            state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "dispatches": 0, "tools": 0, "tokens": 0, "started_at": time.time()}
            counts = {key: state[key] + (1 if key == kind else 0) for key in self.limits}
            counts["tokens"] += tokens
            if any(counts[key] > self.limits[key] for key in counts):
                raise ValueError("受控任务资源预算耗尽，候选与断点已保留")
            state.update(counts, last_progress_at=time.time())
            atomic_write_bytes(self.path, encoded(state))

    def dispatch(self, tokens: int):
        self._reserve("dispatches", tokens)

    def tool(self, tokens: int):
        self._reserve("tools", tokens)

    def charge(self, tokens: int):
        self._reserve("", tokens)


def accept_completion(session, raw: str) -> dict:
    """Require a durable receipt; prose, raw Delta and stale envelopes cannot pass."""
    value = json.loads(raw)
    if isinstance(value, list):
        value = next((item for item in reversed(value) if isinstance(item, dict)
                      and "structured_output" in item), {})
    if isinstance(value, dict) and "structured_output" in value:
        value = value["structured_output"]
    return session.completed_payload(value, confirm_delivery=True)


def candidate_submit_schema() -> dict:
    """Advertise existing essentials; authoritative validation stays in delta_errors.

    Reuse canonical field/type names. Sparse changes and free evidence fields
    remain supported; schema hints must not invent an alternate story format.
    """
    from validate_delta import ALLOWED_TOP_KEYS, REQUIRED_TOP_KEYS, REQUIRED_EVIDENCE_DETAIL_FIELDS
    from story_schema_rules import COLLECTION_KEYS, TYPE_TO_COLLECTION, VALID_FIELDS, DEFAULTS
    nonempty = {"type": "string", "minLength": 1}
    detail = {"type": "object", "required": list(REQUIRED_EVIDENCE_DETAIL_FIELDS),
              "properties": {key: nonempty for key in REQUIRED_EVIDENCE_DETAIL_FIELDS},
              "description": "非标准档案与溯源字段；分组、标签集、参与成员、发生地点等标准字段放在元素顶层，不要塞进详情。",
              "additionalProperties": True}
    collections = {}
    for key in COLLECTION_KEYS:
        fields = {}
        for field in sorted(VALID_FIELDS[key]):
            # All standard fields must be visible, even though sparse patches
            # only require name/evidence. Derive types from the canonical schema
            # defaults; these are type hints, never invented narrative values.
            default = DEFAULTS[key][field]
            if field == "详情":
                fields[field] = detail
            elif field == "名称":
                fields[field] = nonempty
            elif isinstance(default, list):
                fields[field] = {"type": "array", "items": {"type": "string"}}
            else:
                fields[field] = {"type": {bool: "boolean", int: "integer", str: "string"}[type(default)]}
        collections[key] = {"type": "array", "items": {"type": "object",
            "required": ["名称", "详情"], "properties": fields, "additionalProperties": False}}
    collection = {"type": "object", "properties": collections, "additionalProperties": False}
    tags = {"type": "array", "items": nonempty, "uniqueItems": True}
    tag_op = {"type": "object", "required": ["类型", "名称", "保留标签", "降级标签", "理由"],
        "properties": {"类型": {"type": "string", "enum": list(TYPE_TO_COLLECTION)}, "名称": nonempty,
                       "保留标签": tags, "降级标签": tags, "理由": nonempty}, "additionalProperties": False}
    properties = {key: {} for key in sorted(ALLOWED_TOP_KEYS)}
    properties.update({"新增元素": collection, "修改元素": collection,
        "治理操作": {"type": "object", "properties": {"治理标签": {"type": "array", "items": tag_op,
            "description": "只分配原有标签：保留与降级不重叠，并集等于原标签集；不得加入新标签。"}},
            "additionalProperties": True}})
    return {"type": "object", "required": sorted(REQUIRED_TOP_KEYS),
            "properties": properties, "additionalProperties": False}


def candidate_definitions() -> list[dict]:
    """Small MCP schemas expose only candidate operations, never publication."""
    revision = {"type": "integer", "minimum": 0}
    operation = {"operation_id": {"type": "string"}, "expected_revision": revision}
    specs = [
        ("submit", "首选：一次提交完整标准Delta/治理候选、校验并签发凭据。passed=true时将completion原样交给StructuredOutput；失败返回具体errors和新revision。不要先提交空壳探测。",
         {**operation, "delta": candidate_submit_schema()}, [*operation, "delta"]),
        ("status", "读取候选版本、完成状态和操作次数；重连后先查询。", {}, []),
        ("read", "分页读取候选JSON，按next_offset续读。", {"offset": revision,
          "limit": {"type": "integer", "minimum": 1, "maximum": 24000}}, []),
        ("stage", "登记完整候选Delta/治理补丁；仅写候选，不写正式故事。",
         {**operation, "delta": {"type": "object"}}, [*operation, "delta"]),
        ("patch", "原子修改候选字段。JSON Pointer路径仅指向候选；新值完整替换该字段。",
         {**operation, "changes": {"type": "array", "minItems": 1, "maxItems": 50,
          "items": {"type": "object", "additionalProperties": False, "required": ["path", "value"],
                    "properties": {"path": {"type": "string"}, "value": {}}}}}, [*operation, "changes"]),
        ("validate", "复用宿主入库前校验，返回具体错误；修正后再检验。",
         {"expected_revision": revision}, ["expected_revision"]),
        ("finish", "校验当前候选并签发凭证；将此回执原样交给StructuredOutput。宿主随后执行正式提交。",
         {"expected_revision": revision}, ["expected_revision"]),
    ]
    return [{"name": "candidate_" + name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}
        for name, description, properties, required in specs]


def generation_is_pending(output: Path, story: Path) -> bool:
    """Recognize same-baseline interrupted work without authorizing its bytes.

    Pending artifacts still require the ordinary validation/recovery gates. Only
    the exact unfinished receipt created by the host can take this route; stale
    baselines, malformed receipts and mismatched completed receipts remain fenced.
    """
    try:
        receipt = json.loads(output.with_suffix(".generation.json").read_text(encoding="utf-8"))
        return (isinstance(receipt, dict) and receipt.get("pending") is True
                and receipt.get("artifact_sha256") == ""
                and receipt.get("base_revision") == hashlib.sha256(story.read_bytes()).hexdigest())
    except (OSError, ValueError):
        return False


def generation_is_current(output: Path, story: Path) -> bool:
    """Fence both evidence and exact artifact bytes, including torn publication."""
    path = output.with_suffix(".generation.json")
    if not path.is_file():
        return True  # Existing manual/provider artifacts retain their contract.
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("base_revision") != hashlib.sha256(story.read_bytes()).hexdigest():
            return False
        raw = output.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual == receipt.get("artifact_sha256"):
            return True
        # A host transformation authorizes exactly its input and output bytes.
        # A stop between receipt/output replacements can leave either version;
        # both still undergo the ordinary validators on resume.
        if actual == receipt.get("host_transform_source_sha256"):
            return True
        # Older releases formatted governance in place after issuing a receipt.
        # Only a byte-authenticated original can prove a formatting-only change;
        # never accept arbitrary semantic equality, a new story, or pending work.
        if output.parent == story.parent / "质量治理" / "周期审计":
            canonical = _strict_json_bytes(raw)
            for original in output.parent.glob("original_input_*.json"):
                source = original.read_bytes()
                if (hashlib.sha256(source).hexdigest() == receipt.get("artifact_sha256")
                        and _strict_json_bytes(source) == canonical):
                    return True
        return False
    except (OSError, ValueError):
        return False


def _strict_json_bytes(raw: bytes) -> bytes:
    """Compare JSON values without conflating true/1 or silently dropping keys."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique_pairs)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def transform_generated_artifact(project: Path, output: Path, transform, *, allow_stale: bool = False) -> int:
    """Run a trusted local normalizer privately, preserving the original fence.

    Failed transformations never overwrite the candidate. The bridge receipt is
    written before replacement, so interruption cannot misclassify either exact
    host-owned version as external tampering. It never rebases a stale story.
    Legacy unsigned artifacts keep their existing validation contract. Only an
    explicit chapter --force may allow a stale input; its old baseline is kept
    and the caller must still run all content and final structure validators.
    """
    with project_write_lock(project):
        story = project / "故事结构_增量.json"
        receipt_path = output.with_suffix(".generation.json")
        receipt_raw = receipt_path.read_bytes() if receipt_path.is_file() else None
        if receipt_raw is not None and not allow_stale and not generation_is_current(output, story):
            return 65
        original = output.read_bytes()
        base = hashlib.sha256(story.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix=".host_normalize_", dir=output.parent) as directory:
            candidate = Path(directory) / output.name
            candidate.write_bytes(original)
            rc = transform(candidate)
            if rc:
                return rc
            content = candidate.read_bytes()
        if (output.read_bytes() != original or hashlib.sha256(story.read_bytes()).hexdigest() != base
                or (receipt_path.read_bytes() if receipt_path.is_file() else None) != receipt_raw):
            return 65
        if content == original:
            return 0
        if receipt_raw is not None:
            receipt = json.loads(receipt_raw)
            receipt.update(artifact_sha256=hashlib.sha256(content).hexdigest(),
                           host_transform_source_sha256=hashlib.sha256(original).hexdigest())
            atomic_write_bytes(receipt_path, encoded(receipt))
        atomic_write_bytes(output, content)
        return 0


def publish_generated_artifact(project: Path, output: Path, content: bytes,
                               base_revision: str, **metadata) -> None:
    """Publish receipt first, then its exact artifact, under the host writer lock.

    A stop between the two replacements leaves a failing hash fence; the durable
    candidate/recovery ledger supplies the same bytes on retry. Never infer a
    fresh baseline here: the caller must have frozen and validated that version.
    """
    receipt = {**metadata, "base_revision": base_revision,
               "artifact_sha256": hashlib.sha256(content).hexdigest()}
    with project_write_lock(project):
        atomic_write_bytes(output.with_suffix(".generation.json"), encoded(receipt))
        atomic_write_bytes(output, content)


def regenerate_conflicted_artifact(project: Path, task: str, pack: Path, output: Path,
                                   chapter_seq: int, command: str, worker, validate,
                                   **options) -> int:
    """Rebuild a stale proposal from current evidence, never mechanically rebase it.

    A structurally valid old assignment can overwrite a newer fact. Preserve its
    bytes and receipt, generate into a separate durable slot, then fence all
    evidence and publish under the same writer lock as the formal commit.
    """
    if not supports_controlled_runtime(command):
        print("生成版本或产物摘要冲突：已保留原候选，需要基于当前证据重新生成。")
        return 65
    story = project / "故事结构_增量.json"
    base = hashlib.sha256(story.read_bytes()).hexdigest()
    original = output.read_bytes()
    key = signature([str(output.resolve()), base, hashlib.sha256(original).hexdigest()])[:24]
    directory = project / "质量治理" / "版本冲突" / key
    atomic_write_bytes(directory / "original.json", original)
    atomic_write_bytes(directory / "original.generation.json", output.with_suffix(".generation.json").read_bytes())
    staged = directory / output.name
    generation_task = {"repair_chapter": "delta", "audit_repair": "audit_correction"}[task]
    controlled_pack = build_controlled_pack(project, generation_task, pack, staged, chapter_seq)
    config = json.loads(controlled_pack.with_suffix(".runtime.json").read_text(encoding="utf-8"))
    def staged_matches() -> bool:
        # A story hash alone does not identify a task: chapters, analyses and
        # model contracts can change while that story remains byte-identical.
        try:
            receipt = json.loads(staged.with_suffix(".generation.json").read_text(encoding="utf-8"))
            return (receipt.get("candidate_directory") == config["directory"]
                    and generation_is_current(staged, story))
        except (OSError, ValueError, AttributeError):
            return False

    # Resuming after a successful generation must not spend another model call.
    if not staged_matches():
        rc = worker(command, project, generation_task, controlled_pack, staged, chapter_seq, **options)
        if rc:
            return rc
    with project_write_lock(project):
        if (output.read_bytes() != original or not staged_matches()
                or any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
                       for path, digest in config["inputs"].items())):
            print("冲突重生成期间输入再次变化，保留新候选并拒绝覆盖。")
            return 65
        publish_generated_artifact(project, output, staged.read_bytes(), config["base_revision"],
                                   candidate_directory=config["directory"])
        return validate(project)[0]
