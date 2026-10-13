"""提示词模板统一加载器。

把 prompts/ 下的 Jinja2 模板集中管理：

- 每个模板顶部可带 YAML frontmatter（name/description/inputs），用 pyyaml 解析
  （requirements.txt 含 jinja2 + json-repair + pyyaml）。
- render_prompt 渲染前校验 frontmatter 声明的 inputs 是否齐全，缺变量立刻报
  KeyError，比让 StrictUndefined 在模板深处报错更易定位；模板正文里用到未传
  变量时 StrictUndefined 仍兜底报错。
- frontmatter 不会进入渲染结果：自定义 loader 在读取时剥离，只把正文交给 Jinja2。
- list_prompts 列出全部模板及其元信息，便于排查“哪个任务用哪个模板”。

所有任务包/契约模板都从 prompts/ 单一来源生成，避免脚本和模板漂移。
"""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any, Dict, List

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class _FrontmatterFileSystemLoader(FileSystemLoader):
    """FileSystemLoader 子类：读取时剥离模板顶部的 frontmatter。

    这样 Jinja2 渲染结果只含模板正文，frontmatter 不会泄漏进任务包；
    get_prompt_meta 仍能从原文件读出 frontmatter 元信息。
    """

    def get_source(self, environment: Environment, template: str):
        source, filename, uptodate = super().get_source(environment, template)
        _, body = _split_frontmatter(source)
        return body, filename, uptodate


_JINJA_ENV = Environment(
    loader=_FrontmatterFileSystemLoader(str(PROMPTS_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)

# 匹配单独成行的 ``---``（frontmatter 开/闭界符，允许行尾空白）。
_FM_DELIM = re.compile(r"^---[ \t]*$", re.MULTILINE)


def _split_frontmatter(text: str) -> "tuple[Dict[str, Any], str]":
    """分离 ``---`` 包裹的 frontmatter 与模板正文。

    无 frontmatter、或“界符之间解析不出任何字段”时返回 ``({}, text)``，
    后者保护“正文首行恰好是 markdown 分隔线 ``---``”的模板不被误剥离。
    闭合界符后的首个换行被去掉，保证正文起点与无 frontmatter 时一致。
    """
    if not text.startswith("---"):
        return {}, text
    matches = list(_FM_DELIM.finditer(text))
    # 开界符必须在文件首，闭合界符是其后第一个单独成行的 ---
    if len(matches) < 2 or matches[0].start() != 0:
        return {}, text
    fm_text = text[matches[0].end():matches[1].start()]
    body = text[matches[1].end():]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    meta = _parse_simple_frontmatter(fm_text)
    if not meta:
        return {}, text
    return meta, body


def _parse_simple_frontmatter(fm_text: str) -> Dict[str, Any]:
    """用 pyyaml 解析 frontmatter（支持完整 YAML 语法，比手写更稳健）。"""
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def get_prompt_meta(name: str) -> Dict[str, Any]:
    """读取模板的 frontmatter 元信息（不渲染）。无 frontmatter 返回 {}。"""
    path = PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")
    meta, _ = _split_frontmatter(text)
    return meta


def render_prompt(name: str, **context: Any) -> str:
    """渲染 prompts/<name> 模板。

    name 相对 prompts/ 的路径（含子目录与 .j2 后缀），如 ``流水线/章节分析.j2``。
    渲染前校验 frontmatter 声明的 inputs 是否全部传入；缺失立刻报 KeyError。
    """
    required = get_prompt_meta(name).get("inputs") or []
    missing = [k for k in required if k not in context]
    if missing:
        raise KeyError(f"prompt {name!r} missing required inputs: {missing}")
    return _JINJA_ENV.get_template(name).render(**context)


def list_prompts() -> List[Dict[str, Any]]:
    """列出 prompts/ 下全部模板及其元信息，便于排查。"""
    result: List[Dict[str, Any]] = []
    for path in sorted(PROMPTS_DIR.rglob("*.j2")):
        rel = path.relative_to(PROMPTS_DIR).as_posix()
        meta = get_prompt_meta(rel)
        result.append({
            "name": rel,
            "path": str(path),
            "description": meta.get("description", ""),
            "inputs": meta.get("inputs", []),
        })
    return result


def prompt_fingerprint(name: str) -> str:
    """模板和其生成参数共同使任务包失效，不影响提交回执的校验契约。"""
    digest = hashlib.sha256((PROMPTS_DIR / name).read_bytes())
    if name == "流水线/周期审计.j2":
        # 简介字数/分节是生成约束；改变后必须重建任务包，但不能重生成历史章节。
        digest.update(Path(__file__).with_name("synopsis_contract.py").read_bytes())
    return digest.hexdigest()
