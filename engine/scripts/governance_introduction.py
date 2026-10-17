"""Reconcile periodic synopsis proposals without appending independent versions."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from capacity_tree import CapacityTree, TreeFailure, merge_json
from context_budget import CONTEXT_EXCEEDED
from merge_delta import save_json
from synopsis_contract import SYNOPSIS_SYNTHESIS_BUDGETS

# 分片归并只解决内容冲突，不能重新引入周期治理已移除的排版任务。
_CONTENT_RULE = (
    "只核对与归并简介内容；保留已有的事实限定和有依据的内容。"
    "不强制四个分节，不补写标题或占位段落，不得为排版重写内容；"
    "排版由最终收尾或独立简介排版功能处理。"
)


def combine_introductions(tree, proposals, **options):
    """Keep all proposals as evidence; synthesize only conflicting descriptions.

    The ordinary one-patch path adds no model call. Oversized proposals use the
    existing capacity tree and receipts, independently of unchanged story fields.
    The caller still reviews the combined patch against complete cycle evidence.
    """
    result, descriptions = {}, []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise ValueError("治理分片的介绍必须为对象")
        for key, value in proposal.items():
            if key in {"标题", "描述"} and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"治理分片的介绍.{key}必须为非空字符串")
            if key == "描述":
                if value not in descriptions:
                    descriptions.append(value)
            elif key == "标题" and result.get(key) and result[key] != value:
                result[key] = tree.resolve("/介绍/标题", result[key], value)
            else:
                result[key] = merge_json(result.get(key), value, "/介绍/" + key, tree.resolve)
    if len(descriptions) <= 1:
        if descriptions:
            result["描述"] = descriptions[0]
        return result

    signature = hashlib.sha256(json.dumps(proposals, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    evidence = tree.root / f"synopsis_proposals_{signature}.json"
    save_json(evidence, proposals)
    synthesis = CapacityTree(
        tree.root, tree.command, tree.worker,
        "核对本周期多份简介候选及其原文依据，归并成一份覆盖已处理故事的连贯简介。"
        "候选不是原文事实；矛盾须查证，不拼接多份版本，不编造后续章节。"
        "本任务仅生成介绍.描述，不得改动或删减原文、分析、Delta及元素档案。"
        + _CONTENT_RULE,
        "周期简介.json", [(evidence, "完整简介候选；旧版与分片原件均保留")],
        **{**options, "reference_sources": tree.reference_sources +
           [(Path(path), role) for path, _, role in tree.sources], "adaptive_overviews": True})
    overview, _, refs = synthesis.run(overview_only=True)

    def validate(content):
        if (not isinstance(content, dict) or set(content) != {"描述"}
                or not isinstance(content["描述"], str) or not content["描述"].strip()):
            raise ValueError('简介归并必须只输出 {"描述":"完整新版简介"}')

    for budget in SYNOPSIS_SYNTHESIS_BUDGETS:
        try:
            value, _ = synthesis.request(
                f"完整候选根节点：{refs[-1]}\n候选关联索引：{overview}\n"
                f"依据原件写一份约 {budget} 字以内的完整简介，覆盖核心主线。"
                "这是替换旧简介的完整新版，不是在上版基础上追加段落；"
                "篇幅上限是契约，不得因为候选变多而突破。"
                + _CONTENT_RULE +
                'content 仅为 {"描述":"完整新版简介"}；不是新增段落，不含过程笔记。',
                f"周期简介归并（描述预算 {budget} 字）", validate_content=validate)
            if not synthesis.unchanged():
                raise ValueError("简介候选或证据在归并期间改变，保留断点等待重新核验")
            result["描述"] = value["content"]["描述"]
            return result
        except TreeFailure as exc:
            if exc.code != CONTEXT_EXCEEDED:
                raise
    raise TreeFailure(CONTEXT_EXCEEDED)
