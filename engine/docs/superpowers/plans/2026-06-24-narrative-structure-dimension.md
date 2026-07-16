# 故事结构维度（叙事节拍 / 七要素 / 力学 / 工程学 / 骨架）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在拆书流程中新增独立的"故事结构"维度,分章节级(`chapter_structure`)和全书级(`narrative_structure`)两个 task,产出三幕式定位、Brooks 四部分/Freytag 五段结构图、故事七要素档案、故事力学/工程学评估、小说骨架等分析报告。

**Architecture:** 复用 `scripts/analysis_context_pack.py` 现有的 TASKS 注册 + 分包/reduce 框架。章节级模仿视觉资产 `--per-chapter` 模式,每章独立产物;全书级模仿 `plotlines` / `outline` 的多文件输出。**不修改 `chapter_analysis.j2`,不写入 `故事结构_增量.json`**。

**Tech Stack:** Python 3.12, pytest, Markdown, Jinja2(已有)。仅修改 `scripts/analysis_context_pack.py` 与若干 references/prompts 文档。

## Global Constraints

- 不修改 `prompts/chapter_analysis.j2`,不引入新的 schema,不写入故事结构 JSON。
- 引用故事结构元素只用 `类型:名称` 格式(`角色:林婉`、`事件:入门试炼` 等),不引入新编号。
- 所有结论必须可回指章节号或原文证据;空缺写"待确认",不允许编造。
- `chapter_structure` 五小节顺序固定:三幕式定位 → 七要素 → 力学 → 工程学 → 骨架。
- 三幕式术语用"第一幕(建置 Setup)/ 第二幕(对抗 Confrontation)/ 第三幕(解决 Resolution)/ 过渡章(无节拍)",不与 Brooks 四部分混用。
- 节拍冲突标"待确认",禁止强行折中。
- 文件编码 UTF-8,Windows 路径用 `pathlib.Path`,不写死分隔符。

---

## 文件结构

| 文件 | 角色 |
|---|---|
| `scripts/analysis_context_pack.py` | 注册 2 个新 task,新增 `output_contract()` 分支,新增 `chapter_structure` 分章产物路径生成与 `--per-chapter` 派发,允许 `--per-chapter` 兼容 `chapter_structure` 与 `visual_assets` |
| `scripts/test_analysis_context_pack.py` | 追加 ~6 个测试覆盖新 task 注册、分章路径、契约文本关键字、CLI 参数兼容 |
| `references/analysis_narrative_structure.md` | 新增,精简版规范文档(章节级 5 小节 + 全书 7 份产物结构) |
| `references/fullbook_analysis_workbench.md` | 任务表新增 2 行 + 上下文优先级新增 2 段 |
| `references/analysis_four_dimensions.md` | 末尾新增一句指向 `analysis_narrative_structure.md` |
| `references/file_structure.md` | 新增 `全书分析/故事结构/` 目录说明 |

实现按 5 个任务推进,每个任务都能独立测试与提交:

1. **Task 1**:在 `analysis_context_pack.py` 注册 2 个新 task + 全书级 `output_contract` 文本 + 单元测试
2. **Task 2**:为 `chapter_structure` 实现分章产物路径生成 + 复用 `--per-chapter` 派发(扩展现有逻辑)+ 单元测试
3. **Task 3**:为 `chapter_structure` 实现"分章独立 reduce 契约"文本 + 单元测试
4. **Task 4**:新增/更新 references 文档(`analysis_narrative_structure.md`、`fullbook_analysis_workbench.md`、`analysis_four_dimensions.md`、`file_structure.md`)
5. **Task 5**:端到端冒烟:在临时项目上跑通 `chapter_structure --per-chapter` 与 `narrative_structure` 两条命令,确认产物路径与文件存在

---

## Task 1: 注册 `chapter_structure` 与 `narrative_structure` 两个 task

**Files:**
- Modify: `scripts/analysis_context_pack.py`(TASKS 字典 + `output_contract` 分支)
- Test: `scripts/test_analysis_context_pack.py`(追加测试)

**Interfaces:**
- Consumes: 现有 `TASKS: Dict[str, Dict[str, Any]]`、`output_contract(task: str) -> str`
- Produces:
  - `TASKS["chapter_structure"]` 字典含 `name`、`outputs`、`goal` 三键
  - `TASKS["narrative_structure"]` 字典含 `name`、`outputs`(列表 7 个路径)、`goal`
  - `output_contract("chapter_structure")` 返回含五小节标题的契约文本
  - `output_contract("narrative_structure")` 返回含 7 个产物对应章节标题的契约文本

- [ ] **Step 1: 写失败测试 — 验证两个新 task 已注册**

在 `scripts/test_analysis_context_pack.py` 末尾追加:

```python
from analysis_context_pack import TASKS, output_contract


def test_chapter_structure_task_registered():
    assert "chapter_structure" in TASKS
    info = TASKS["chapter_structure"]
    assert info["name"]  # 非空
    assert info["goal"]
    # 章节级 output 路径用 {NNN} 占位,实际章节号由 per-chapter 派发时替换
    assert any("分章" in p and "章节结构.md" in p for p in info["outputs"])


def test_narrative_structure_task_registered():
    assert "narrative_structure" in TASKS
    info = TASKS["narrative_structure"]
    expected = {
        "全书分析/故事结构/三幕式结构图.md",
        "全书分析/故事结构/Brooks四部分结构图.md",
        "全书分析/故事结构/Freytag五段结构图.md",
        "全书分析/故事结构/故事七要素档案.md",
        "全书分析/故事结构/故事力学评估.md",
        "全书分析/故事结构/故事工程学评估.md",
        "全书分析/故事结构/小说骨架.md",
    }
    assert expected.issubset(set(info["outputs"]))


def test_chapter_structure_output_contract_has_five_sections():
    contract = output_contract("chapter_structure")
    # 五小节标题必须全部出现
    for header in ["三幕式定位", "故事七要素", "故事力学", "故事工程学", "小说骨架"]:
        assert header in contract
    # 三幕式术语锁定
    assert "第一幕" in contract and "建置" in contract
    assert "第二幕" in contract and "对抗" in contract
    assert "第三幕" in contract and "解决" in contract
    # 禁止编造的硬规则
    assert "待确认" in contract


def test_narrative_structure_output_contract_has_seven_artifacts():
    contract = output_contract("narrative_structure")
    for artifact in [
        "三幕式结构图", "Brooks四部分结构图", "Freytag五段结构图",
        "故事七要素档案", "故事力学评估", "故事工程学评估", "小说骨架",
    ]:
        assert artifact in contract
    # 七要素清单出现
    for elem in [
        "主角", "缺陷", "有利的故事环境", "反面角色",
        "主角的盟友", "改变人生的事件", "整合故事要素",
    ]:
        assert elem in contract
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py -v -k "narrative_structure or chapter_structure"
```

Expected: 4 个新测试全部 FAIL(KeyError 或 AssertionError)。

- [ ] **Step 3: 在 `analysis_context_pack.py` TASKS 字典追加两个条目**

在 `scripts/analysis_context_pack.py` 的 `TASKS` 字典末尾(`visual_assets` 之后)追加:

```python
    "chapter_structure": {
        "name": "章节结构定位（分章独立产物）",
        "outputs": ["全书分析/故事结构/分章/ch{NNN}/章节结构.md"],
        "goal": "对每章独立产出 三幕式节拍定位 + 七要素本章信号 + 力学/工程学/骨架本章信号；不跨章归纳，不写入故事结构 JSON。",
    },
    "narrative_structure": {
        "name": "故事结构全书分析",
        "outputs": [
            "全书分析/故事结构/三幕式结构图.md",
            "全书分析/故事结构/Brooks四部分结构图.md",
            "全书分析/故事结构/Freytag五段结构图.md",
            "全书分析/故事结构/故事七要素档案.md",
            "全书分析/故事结构/故事力学评估.md",
            "全书分析/故事结构/故事工程学评估.md",
            "全书分析/故事结构/小说骨架.md",
        ],
        "goal": "基于章节分析MD、summary/outline/plotlines 以及分章 章节结构.md，全书级输出三种节拍图 + 七要素档案 + 力学/工程学评估 + 小说骨架。",
    },
```

- [ ] **Step 4: 在 `output_contract` 中追加两个 if 分支**

在 `scripts/analysis_context_pack.py` 的 `output_contract` 函数内、`if task == "detailed_outline":` 之前(或 `if task == "visual_assets":` 之后)插入:

```python
    if task == "chapter_structure":
        return """# 输出要求

本任务包**只针对单章**输出一份 `章节结构.md`，结构固定为五个小节，按顺序写。

## 1. 三幕式定位
- 所处幕：第一幕（建置 Setup）/ 第二幕（对抗 Confrontation）/ 第三幕（解决 Resolution）/ 过渡章（无节拍）
- 节拍点（仅当本章为节拍章时填写）：钩子 Hook / 激励事件 Inciting Incident / 第一情节点 FPP / 第一夹点 / 中点 Midpoint / 第二夹点 / 第二情节点 SPP / 高潮 Climax / 收束 Resolution
- 节拍证据：引用原文段落或本章分析MD相应位置；非节拍章写"过渡推进，无显著节拍点"

## 2. 故事七要素 · 本章信号
逐条列出；本章对该要素无推进时写"本章无推进"，禁止编造。

1. 主角
2. 缺陷
3. 有利的故事环境
4. 反面角色
5. 主角的盟友
6. 改变人生的事件 · 危险
7. 整合故事要素

## 3. 故事力学 · 本章贡献（Brooks 六力学）
仅列出本章有贡献的项,其余省略;每项后写"本章贡献 + 原文证据"。

- 强迫性前提（Compelling Premise）
- 戏剧张力（Dramatic Tension）
- 节奏（Pacing）
- 英雄共情（Hero Empathy）
- 代入体验（Vicarious Experience）
- 叙事策略（Narrative Strategy）

## 4. 故事工程学 · 本章执行（六大核心能力）
仅列出本章有可观察执行的项。

- 概念（Concept）
- 人物（Character）
- 主题（Theme）
- 结构（Structure）
- 场景执行（Scene Execution）
- 写作声音（Writing Voice）

## 5. 小说骨架 · 本章信号
- Logline 推进：本章动了"谁 / 想要什么 / 阻碍 / 代价"中的哪一项,引用原文证据
- 网文骨架命中：金手指 / 升级 / 主角光环 / 装逼打脸 / 套路 / 核心爽点 / 反派配置 中本章命中的点;未命中省略
- Freytag 段位：序幕 / 上升 / 高潮 / 下降 / 结局

## 强制要求

1. 五小节顺序固定,缺数据写"本章无推进"或"过渡推进,无显著节拍点",不要省略小节。
2. 所有结论必须引用本章原文短句或本章分析MD作为证据,不确定写"待确认"。
3. 禁止跨章归纳,例如"主角自第3章后……"这类描述。
4. 不要修改 `故事结构_增量.json` 或其他章节文件。
"""
    if task == "narrative_structure":
        return """# 输出要求

请按 7 份产物分别输出 Markdown,每份产物独立成文件:

## 三幕式结构图.md

| 幕 | 节拍 | 章节区间 | 关键事件 | 证据 |
|----|------|----------|----------|------|

- 幕:第一幕(建置)/ 第二幕(对抗)/ 第三幕(解决)
- 节拍:钩子 / 激励事件 / FPP / 第一夹点 / 中点 / 第二夹点 / SPP / 高潮 / 收束
- 关键事件用 `事件:名称` 引用 `事件集` 元素

## Brooks四部分结构图.md

双表对齐:

第一表:

| 部分 | 章节区间 | 角色任务 | 关键事件 | 与三幕式对齐处 | 证据 |
|------|----------|----------|----------|----------------|------|

部分:Setup(铺垫)/ Response(反应)/ Attack(进攻)/ Resolution(决战)

第二表 — 五大里程碑:

| 里程碑 | 章节号 | 触发事件 | 张力变化 | 证据 |
|--------|--------|----------|----------|------|

里程碑:Hook / FPP / Midpoint / SPP / Climax

## Freytag五段结构图.md

| 段位 | 章节区间 | 关键事件 | 与三幕式对齐处 | 证据 |
|------|----------|----------|----------------|------|

段位:序幕 Exposition / 上升 Rising Action / 高潮 Climax / 下降 Falling Action / 结局 Dénouement

## 故事七要素档案.md

逐项独立一节,每节字段统一:初始状态、终态、演变轨迹、原文证据章节;不确定写"待确认"。

1. 主角(引用 `角色:名称`)
2. 缺陷
3. 有利的故事环境(引用 `阵营集`/`地点集`)
4. 反面角色(引用 `角色:名称`)
5. 主角的盟友(引用 `角色:名称` 列表)
6. 改变人生的事件 · 危险(引用 `事件:名称`)
7. 整合故事要素(主题统合)

## 故事力学评估.md

Brooks 六力学逐项评估:

| 力学项 | 强度(强/中/弱/待确认) | 评估说明 | 证据章节 | 风险/短板 |
|--------|-----------------------|----------|----------|-----------|

力学项:强迫性前提 / 戏剧张力 / 节奏 / 英雄共情 / 代入体验 / 叙事策略

## 故事工程学评估.md

六大核心能力逐项:

| 能力 | 强度 | 评估说明 | 证据 | 短板诊断 |
|------|------|----------|------|----------|

能力:概念 / 人物 / 主题 / 结构 / 场景执行 / 写作声音

## 小说骨架.md

```
## 1. 故事核 / Logline
一句话:在 <时代/地点>,<主角>因为 <激励事件>,为了 <欲望/目标>,必须对抗 <反派/阻碍>,否则将付出 <代价>。

补充:主题句、类型标签、目标读者、证据章节

## 2. 网文骨架零件
- 金手指 / 升级体系 / 主角光环点 / 装逼打脸节奏 / 套路类型 / 核心爽点 / 反派配置
- 引用 `角色:名称` 与章节证据

## 3. 节拍对照表
| 章节区间 | 三幕式节拍 | Brooks里程碑 | Freytag段位 |
冲突处标"待确认",禁止强行统一。
```

## 强制要求

1. 三种节拍图必须互相对齐,冲突处标"待确认"。
2. 所有结论必须引用章节号、原文证据或 `类型:名称` 元素;不确定写"待确认"。
3. 不写入 `故事结构_增量.json`,不引入新编号。
4. 7 份产物全部输出;某项无内容写"本书无对应内容"而非省略文件。
"""
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py -v
```

Expected: 全部通过(包括 4 个新测试 + 已有测试)。

- [ ] **Step 6: 提交**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler
git add scripts/analysis_context_pack.py scripts/test_analysis_context_pack.py
git commit -m "feat(analysis): register chapter_structure and narrative_structure tasks"
```

---

## Task 2: 让 `--per-chapter` 兼容 `chapter_structure`,并实现分章产物路径

**Files:**
- Modify: `scripts/analysis_context_pack.py`(新增 `chapter_structure_per_chapter_outputs`、扩展 `--per-chapter` / `--aggregate` 校验、扩展 `_build_visual_per_chapter_packs` 为通用分章派发或新增 `_build_chapter_structure_per_chapter_packs`)
- Test: `scripts/test_analysis_context_pack.py`

**Interfaces:**
- Consumes: 现有 `_build_visual_per_chapter_packs`、`visual_assets_per_chapter_outputs`、`build_pack`
- Produces:
  - 函数 `chapter_structure_per_chapter_outputs(seq: int) -> List[str]`,返回 `["全书分析/故事结构/分章/ch{NNN}/章节结构.md"]`
  - CLI:`python analysis_context_pack.py <proj> --task chapter_structure --per-chapter --chapters all` 为每章生成一个 `pack_chNNN.md`,产物目标路径为 `全书分析/故事结构/分章/ch{NNN}/章节结构.md`
  - 跳过逻辑:目标文件已存在且非空时跳过该章(同视觉资产分章模式)

- [ ] **Step 1: 写失败测试 — 验证分章路径生成**

在 `test_analysis_context_pack.py` 末尾追加:

```python
from analysis_context_pack import chapter_structure_per_chapter_outputs


def test_chapter_structure_per_chapter_outputs_path():
    outs = chapter_structure_per_chapter_outputs(7)
    assert outs == ["全书分析/故事结构/分章/ch007/章节结构.md"]


def test_chapter_structure_per_chapter_outputs_zero_padding():
    outs = chapter_structure_per_chapter_outputs(123)
    assert outs == ["全书分析/故事结构/分章/ch123/章节结构.md"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py::test_chapter_structure_per_chapter_outputs_path -v
```

Expected: FAIL(ImportError: cannot import name `chapter_structure_per_chapter_outputs`)。

- [ ] **Step 3: 实现分章路径函数**

在 `scripts/analysis_context_pack.py` 中,紧邻 `visual_assets_per_chapter_outputs` 函数后追加:

```python
def chapter_structure_per_chapter_outputs(seq: int) -> List[str]:
    """单章故事结构产物的相对路径,与视觉资产分章对齐。"""
    return [f"全书分析/故事结构/分章/ch{seq:03d}/章节结构.md"]
```

- [ ] **Step 4: 放宽 `--per-chapter` / `--aggregate` 的 task 校验**

在 `main()` 中找到:

```python
    if (args.per_chapter or args.aggregate) and args.task != "visual_assets":
        print("错误: --per-chapter / --aggregate 仅支持 --task visual_assets")
        return 1
```

替换为:

```python
    if args.per_chapter and args.task not in {"visual_assets", "chapter_structure"}:
        print("错误: --per-chapter 仅支持 --task visual_assets 或 chapter_structure")
        return 1
    if args.aggregate and args.task != "visual_assets":
        print("错误: --aggregate 当前仅支持 --task visual_assets")
        return 1
```

- [ ] **Step 5: 在 `main()` 的 per_chapter 派发处分流**

找到:

```python
    if args.per_chapter:
        return _build_visual_per_chapter_packs(project_dir, args, chapters)
```

替换为:

```python
    if args.per_chapter:
        if args.task == "chapter_structure":
            return _build_chapter_structure_per_chapter_packs(project_dir, args, chapters)
        return _build_visual_per_chapter_packs(project_dir, args, chapters)
```

- [ ] **Step 6: 新增 `_build_chapter_structure_per_chapter_packs` 函数**

在 `_build_visual_per_chapter_packs` 函数之后追加(整体仿照视觉资产分章实现,只换 task 名、输出路径函数、reduce_prompt 函数):

```python
def _build_chapter_structure_per_chapter_packs(project_dir: Path, args: argparse.Namespace, chapters: List[Dict[str, Any]]) -> int:
    """为每个选定章节生成一份独立 chapter_structure 任务包。"""
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out_dir) if args.out_dir else project_dir / "全书分析" / "_任务包" / f"{timestamp}_chapter_structure_per_chapter"
    out_root.mkdir(parents=True, exist_ok=True)

    pack_records: List[Dict[str, Any]] = []
    for ch in chapters:
        seq = int(ch["seq"])
        existing = [project_dir / rel for rel in chapter_structure_per_chapter_outputs(seq)]
        if all(p.is_file() and p.stat().st_size > 0 for p in existing):
            print(f"跳过第{seq:03d}章:章节结构.md 已存在。")
            continue
        pack_content = build_pack(
            project_dir,
            "chapter_structure",
            [ch],
            args.targets,
            args.question,
            args.include_original,
            args.max_original_chars,
            args.max_analysis_chars,
            1,
            1,
            per_chapter_seq=seq,
        )
        pack_path = out_root / f"pack_ch{seq:03d}.md"
        pack_path.write_text(pack_content, encoding="utf-8")
        reduce_path = out_root / f"reduce_prompt_ch{seq:03d}.md"
        reduce_path.write_text(reduce_contract_chapter_structure_per_chapter(seq), encoding="utf-8")
        pack_records.append({
            "seq": seq,
            "pack": str(pack_path.relative_to(project_dir)),
            "reduce_prompt": str(reduce_path.relative_to(project_dir)),
            "outputs": chapter_structure_per_chapter_outputs(seq),
        })

    manifest = {
        "created_at": timestamp,
        "task": "chapter_structure",
        "mode": "per_chapter",
        "chapter_range": args.chapters,
        "include_original": args.include_original,
        "pack_count": len(pack_records),
        "packs": pack_records,
        "recommended_workflow": [
            "对每个 pack_chNNN.md:读取并按 reduce_prompt_chNNN.md 的契约直接写出 全书分析/故事结构/分章/chNNN/章节结构.md",
            "完成所有章节后,可执行 `--task narrative_structure` 做全书级汇总",
        ],
    }
    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"故事结构分章任务包已生成: {out_root}")
    print(f"待处理章节数: {len(pack_records)}(已跳过已完成章节)")
    print(f"清单: {manifest_path}")
    for r in pack_records:
        print(f"- {r['pack']}")
    return 0
```

注意:`reduce_contract_chapter_structure_per_chapter` 函数会在 Task 3 创建;本步先确保引用名一致。

- [ ] **Step 7: 给 `build_pack` 的 per_chapter 分支增加 chapter_structure 兼容**

在 `build_pack` 中,找到:

```python
    if per_chapter_seq is not None:
        lines.append(f"# 视觉资产分章任务包:第{per_chapter_seq:03d}章 ({pack_no}/{total_packs})")
    else:
        lines.append(f"# 全书/局部分析任务包:{task_info['name']} ({pack_no}/{total_packs})")
```

替换为:

```python
    if per_chapter_seq is not None:
        per_chapter_label = {
            "visual_assets": "视觉资产分章任务包",
            "chapter_structure": "章节结构分章任务包",
        }.get(task, f"{task_info['name']} 分章任务包")
        lines.append(f"# {per_chapter_label}:第{per_chapter_seq:03d}章 ({pack_no}/{total_packs})")
    else:
        lines.append(f"# 全书/局部分析任务包:{task_info['name']} ({pack_no}/{total_packs})")
```

并在下方找到:

```python
    if per_chapter_seq is not None:
        lines.append("")
        lines.append("⚠️ 本任务包只针对**单章**生成视觉资产。所有产物只能引用本章原文与本章分析MD的内容,禁止跨章总结。")
```

替换为(让告警措辞与 task 对齐):

```python
    if per_chapter_seq is not None:
        scope = "视觉资产" if task == "visual_assets" else "章节结构.md"
        lines.append("")
        lines.append(f"⚠️ 本任务包只针对**单章**生成{scope}。所有产物只能引用本章原文与本章分析MD的内容,禁止跨章总结。")
```

在下方找到 `## 目标输出文件` 块:

```python
    lines.append("## 目标输出文件")
    if per_chapter_seq is not None:
        for out in visual_assets_per_chapter_outputs(per_chapter_seq):
            lines.append(f"- {out}")
    else:
        ...
```

替换为:

```python
    lines.append("## 目标输出文件")
    if per_chapter_seq is not None:
        per_chapter_outputs = {
            "visual_assets": visual_assets_per_chapter_outputs,
            "chapter_structure": chapter_structure_per_chapter_outputs,
        }.get(task)
        out_list = per_chapter_outputs(per_chapter_seq) if per_chapter_outputs else []
        for out in out_list:
            lines.append(f"- {out}")
    else:
        outputs = task_info.get("outputs") or []
        if outputs:
            for out in outputs:
                lines.append(f"- {out}")
        else:
            lines.append("- 用户自定义输出路径")
```

- [ ] **Step 8: 跑测试确认路径函数通过**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py::test_chapter_structure_per_chapter_outputs_path test_analysis_context_pack.py::test_chapter_structure_per_chapter_outputs_zero_padding -v
```

Expected: 2 个测试通过。注意 `_build_chapter_structure_per_chapter_packs` 引用了 Task 3 才创建的函数 — 不要在本任务步骤运行 `_build_chapter_structure_per_chapter_packs`(只跑导入级测试),module 顶层 import 不会失败,因为函数体内引用在调用时才解析。

- [ ] **Step 9: 提交**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler
git add scripts/analysis_context_pack.py scripts/test_analysis_context_pack.py
git commit -m "feat(analysis): wire chapter_structure into per-chapter dispatch"
```

---

## Task 3: 实现 `chapter_structure` 分章 reduce 契约

**Files:**
- Modify: `scripts/analysis_context_pack.py`(新增 `reduce_contract_chapter_structure_per_chapter`)
- Test: `scripts/test_analysis_context_pack.py`

**Interfaces:**
- Consumes: 现有 `reduce_contract_visual_per_chapter` 模式
- Produces: `reduce_contract_chapter_structure_per_chapter(seq: int) -> str`,返回单章产物契约字符串,要求覆盖五小节顺序与"本章无推进"占位规则

- [ ] **Step 1: 写失败测试**

在 `test_analysis_context_pack.py` 末尾追加:

```python
from analysis_context_pack import reduce_contract_chapter_structure_per_chapter


def test_reduce_contract_chapter_structure_mentions_target_file():
    text = reduce_contract_chapter_structure_per_chapter(42)
    assert "全书分析/故事结构/分章/ch042/章节结构.md" in text
    # 五小节顺序必须出现
    for header in ["三幕式定位", "故事七要素", "故事力学", "故事工程学", "小说骨架"]:
        assert header in text
    # 禁止跨章归纳的硬规则
    assert "禁止跨章" in text
    # 强制五小节齐全的硬规则
    assert "本章无推进" in text or "本章无对应内容" in text
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py::test_reduce_contract_chapter_structure_mentions_target_file -v
```

Expected: FAIL(ImportError 或 AssertionError)。

- [ ] **Step 3: 实现函数**

在 `scripts/analysis_context_pack.py` 中,紧邻 `reduce_contract_visual_per_chapter` 函数后追加:

```python
def reduce_contract_chapter_structure_per_chapter(seq: int) -> str:
    target = chapter_structure_per_chapter_outputs(seq)[0]
    return f"""# 单章故事结构产出任务

本任务包只针对**第{seq:03d}章**生成 `章节结构.md`。

## 目标输出(一个文件)

- {target}

## 章节结构.md 模板(五小节顺序固定)

```markdown
# 第{seq:03d}章 故事结构定位

## 1. 三幕式定位
- 所处幕:第一幕(建置 Setup)/ 第二幕(对抗 Confrontation)/ 第三幕(解决 Resolution)/ 过渡章(无节拍)
- 节拍点:钩子 / 激励事件 / FPP / 第一夹点 / 中点 / 第二夹点 / SPP / 高潮 / 收束;非节拍章写"过渡推进,无显著节拍点"
- 节拍证据:原文片段或本章分析MD

## 2. 故事七要素 · 本章信号
1. 主角
2. 缺陷
3. 有利的故事环境
4. 反面角色
5. 主角的盟友
6. 改变人生的事件 · 危险
7. 整合故事要素
每项写一行"本章推进 + 证据";本章无推进写"本章无推进"。

## 3. 故事力学 · 本章贡献(Brooks 六力学)
仅列出本章有贡献的项;每项后写"本章贡献 + 原文证据"。

## 4. 故事工程学 · 本章执行(六大核心能力)
仅列出本章有可观察执行的项。

## 5. 小说骨架 · 本章信号
- Logline 推进:动了"谁/想要/阻碍/代价"中的哪一项
- 网文骨架命中:金手指/升级/光环/装逼打脸/套路/爽点/反派 中本章命中的点
- Freytag 段位:序幕/上升/高潮/下降/结局
```

## 强制要求

1. 五小节必须按顺序齐全;本章无内容也要写"本章无推进"或"过渡推进,无显著节拍点"。
2. 所有结论必须引用本章原文短句或本章分析MD作为证据;禁止编造。
3. 禁止跨章归纳,例如"主角自第3章后……"这类描述。
4. 不要修改 `故事结构_增量.json` 或其他章节文件;只写目标文件本身。
"""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py -v
```

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler
git add scripts/analysis_context_pack.py scripts/test_analysis_context_pack.py
git commit -m "feat(analysis): add per-chapter reduce contract for chapter_structure"
```

---

## Task 4: 新增/更新 references 文档

**Files:**
- Create: `references/analysis_narrative_structure.md`
- Modify: `references/fullbook_analysis_workbench.md`、`references/analysis_four_dimensions.md`、`references/file_structure.md`

**Interfaces:**
- 仅文档,无函数接口

- [ ] **Step 1: 创建 `references/analysis_narrative_structure.md`**

写入以下内容(精简版规范文档,与 spec 一致但去掉了"实现计划"语境):

```markdown
# 故事结构维度规范

本规范定义拆书场景的 **故事结构** 维度,覆盖叙事节拍 / 七要素 / 力学 / 工程学 / 骨架五个层面。

> 核心原则:从原文反推,不允许编造;不确定写"待确认";引用 `角色:名称` / `事件:名称` 等已有元素,**不引入新编号**;**不写入 `故事结构_增量.json`**。

## 与四维度的关系

| 维度 | 关注层 | 与本维度的边界 |
|---|---|---|
| plot | 事件层 | 本维度不重复列事件,引用 `事件:名称` |
| plotlines | 线索层(主/支/暗/成长/情感) | 节拍模板与剧情线正交 |
| outline | 卷级宏观 | 本维度做节拍映射,而非按冲突归卷 |
| detailed_outline | 章/场景级 | 本维度写"本章是哪个节拍点" |

## 章节级 task:`chapter_structure`

每章独立产出 `全书分析/故事结构/分章/chNNN/章节结构.md`,五小节固定顺序:

1. **三幕式定位**:所处幕(第一幕建置 / 第二幕对抗 / 第三幕解决 / 过渡章)+ 节拍点 + 证据
2. **故事七要素 · 本章信号**:主角 / 缺陷 / 有利的故事环境 / 反面角色 / 主角的盟友 / 改变人生的事件 · 危险 / 整合故事要素
3. **故事力学 · 本章贡献**(Brooks 六力学):强迫性前提 / 戏剧张力 / 节奏 / 英雄共情 / 代入体验 / 叙事策略
4. **故事工程学 · 本章执行**(六大核心能力):概念 / 人物 / 主题 / 结构 / 场景执行 / 写作声音
5. **小说骨架 · 本章信号**:Logline 推进 + 网文骨架命中 + Freytag 段位

CLI:

```bash
python scripts/analysis_context_pack.py <项目目录> \
  --task chapter_structure \
  --per-chapter \
  --chapters all \
  --include-original sample
```

## 全书级 task:`narrative_structure`

7 份产物全部位于 `全书分析/故事结构/`:

| 文件 | 内容 |
|---|---|
| 三幕式结构图.md | 幕/拍 → 章节区间 → 关键事件 |
| Brooks四部分结构图.md | 四部分(Setup/Response/Attack/Resolution)+ 五大里程碑(Hook/FPP/Midpoint/SPP/Climax)双表 |
| Freytag五段结构图.md | 序幕/上升/高潮/下降/结局 |
| 故事七要素档案.md | 七要素全书档案 + 演变轨迹 |
| 故事力学评估.md | Brooks 六力学逐项评估 |
| 故事工程学评估.md | 六大核心能力评估 + 短板诊断 |
| 小说骨架.md | Logline + 网文骨架零件 + 节拍对照表 |

三种节拍图必须互相对齐;冲突处标"待确认",禁止强行折中。

## 上下文优先级

`chapter_structure`:

```
本章原文 + 本章章节分析MD > 本章 Delta
```

`narrative_structure`:

```
分章 章节结构.md > 章节梗概汇总 > 全书大纲 > 剧情线总表 > 章节分析MD抽样 > 原文抽样
```

## 执行顺序

```
summary → characters → plot → worldview → settings → plotlines
→ outline → detailed_outline → chapter_structure → narrative_structure → report
```

## 质量规则

1. 结论必须能回指章节号、原文片段、章节分析或故事结构元素。
2. 不确定写"待确认",不允许编造。
3. 节拍冲突标"待确认",不强行折中。
4. 不写入 `故事结构_增量.json`,不引入新编号。
5. `chapter_structure` 禁止跨章归纳;跨章归纳交给 `narrative_structure`。
```

- [ ] **Step 2: 在 `fullbook_analysis_workbench.md` 任务表新增 2 行**

读取 `references/fullbook_analysis_workbench.md`,在四维度任务表(`| detailed_outline | 细纲反推 | ...`)之后追加 2 行;并在文末"上下文优先级"章节追加 2 段。

具体定位:在表格(`| detailed_outline | 细纲反推 | 全书分析/剧情结构/章节细纲.md |`)行下方追加:

```markdown
| chapter_structure | 章节结构定位(分章) | 全书分析/故事结构/分章/chNNN/章节结构.md |
| narrative_structure | 故事结构全书分析 | 全书分析/故事结构/三幕式结构图.md 等 7 份产物 |
```

在 `## 上下文优先级` 章节末尾(`### detailed_outline ... > 本章Delta` 之后)追加:

```markdown
### chapter_structure

优先级:

```text
本章原文 + 本章章节分析MD > 本章 Delta
```

### narrative_structure

优先级:

```text
分章 章节结构.md > 章节梗概汇总 > 全书大纲 > 剧情线总表 > 章节分析MD抽样 > 原文抽样
```

三种节拍图(三幕式/Brooks四部分/Freytag五段)必须互相对齐;冲突处标"待确认",不强行折中。
```

- [ ] **Step 3: 在 `analysis_four_dimensions.md` 末尾追加一句指向**

在 `references/analysis_four_dimensions.md` 文档末尾(最后一节"质量要求")追加:

```markdown

## 扩展:故事结构维度

四维度之外,另有 **故事结构维度** 覆盖叙事节拍 / 七要素 / 故事力学 / 故事工程学 / 小说骨架,详见 [`analysis_narrative_structure.md`](analysis_narrative_structure.md)。
```

- [ ] **Step 4: 在 `file_structure.md` 新增目录说明**

读取 `references/file_structure.md`,找到 `全书分析/` 目录树说明区(应已包含 `视觉资产/` 等子目录),在合适位置追加 `故事结构/` 子目录:

```
全书分析/故事结构/
├── 分章/
│   ├── ch001/章节结构.md
│   ├── ch002/章节结构.md
│   └── ...
├── 三幕式结构图.md
├── Brooks四部分结构图.md
├── Freytag五段结构图.md
├── 故事七要素档案.md
├── 故事力学评估.md
├── 故事工程学评估.md
└── 小说骨架.md
```

如果原文件没有目录树而是叙述性段落,则改为追加一段:"`全书分析/故事结构/`:故事结构维度的产物目录,含分章 `章节结构.md` 与全书 7 份产物(三幕式 / Brooks四部分 / Freytag / 七要素 / 力学 / 工程学 / 骨架);由 `chapter_structure` / `narrative_structure` 任务生成,不写入故事结构 JSON。"

- [ ] **Step 5: 自检文档**

逐文件用 Read 工具检查改动是否落地。

- [ ] **Step 6: 提交**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler
git add references/analysis_narrative_structure.md references/fullbook_analysis_workbench.md references/analysis_four_dimensions.md references/file_structure.md
git commit -m "docs(references): document chapter_structure and narrative_structure dimensions"
```

---

## Task 5: 端到端冒烟测试

**Files:**
- Test only,验证 CLI 能跑通,**不**生成任何模型产物(只生成 pack 与 manifest)

**Interfaces:**
- 验证:`--task chapter_structure --per-chapter`、`--task narrative_structure` 两条 CLI 都能产出 pack 文件且不报错

- [ ] **Step 1: 找一个最小可用项目目录**

```bash
ls C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/ -la
# 找一个含 `原文拆解/_索引.json` 与 `章节处理/第NNN章_*.md` 的子目录;
# 若没有现成测试项目,跳过本任务的实际执行,只做静态校验:
python -c "from scripts.analysis_context_pack import TASKS; assert 'chapter_structure' in TASKS and 'narrative_structure' in TASKS"
```

如果当前仓库不带 sample 项目,改为跑下面的静态烟测脚本(创建临时迷你项目):

```bash
mkdir -p /tmp/np_smoke/原文拆解 /tmp/np_smoke/章节处理
printf '%s' '{"chapters":[{"seq":1,"filename":"第001章_序章.md","title":"序章"}]}' > /tmp/np_smoke/原文拆解/_索引.json
printf '%s' '# 第001章 序章\n\n原文内容' > /tmp/np_smoke/原文拆解/第001章_序章.md
printf '%s' '# 第001章 序章\n\n## 1. 剧情梗概\n本章序章。' > /tmp/np_smoke/章节处理/第001章_序章.md
```

- [ ] **Step 2: 跑 `chapter_structure --per-chapter`**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python analysis_context_pack.py /tmp/np_smoke --task chapter_structure --per-chapter --chapters 1
```

Expected 输出包含:

```
故事结构分章任务包已生成: /tmp/np_smoke/全书分析/_任务包/<timestamp>_chapter_structure_per_chapter
待处理章节数: 1
- 全书分析/_任务包/<timestamp>_chapter_structure_per_chapter/pack_ch001.md
```

- [ ] **Step 3: 验证 pack 与 reduce_prompt 内容**

```bash
ls /tmp/np_smoke/全书分析/_任务包/*_chapter_structure_per_chapter/
# 应有:pack_ch001.md、reduce_prompt_ch001.md、manifest.json
grep -E "三幕式定位|故事七要素|故事力学|故事工程学|小说骨架" /tmp/np_smoke/全书分析/_任务包/*_chapter_structure_per_chapter/pack_ch001.md
grep "章节结构.md" /tmp/np_smoke/全书分析/_任务包/*_chapter_structure_per_chapter/reduce_prompt_ch001.md
```

Expected: 五小节标题全部命中;reduce_prompt 提及目标产物路径。

- [ ] **Step 4: 跑 `narrative_structure`**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python analysis_context_pack.py /tmp/np_smoke --task narrative_structure --chapters all
```

Expected 输出包含:

```
分析任务包已生成: /tmp/np_smoke/全书分析/_任务包/<timestamp>_narrative_structure
任务: 故事结构全书分析 | 章节数: 1 | 分包数: 1
```

- [ ] **Step 5: 验证 narrative_structure 产物清单**

```bash
cat /tmp/np_smoke/全书分析/_任务包/*_narrative_structure/manifest.json | python -m json.tool | grep -E "三幕式结构图|Brooks四部分|Freytag|故事七要素档案|故事力学评估|故事工程学评估|小说骨架"
```

Expected: 7 份产物路径全部在 `target_outputs` 列表中。

- [ ] **Step 6: 清理冒烟项目**

```bash
rm -rf /tmp/np_smoke
```

- [ ] **Step 7: 跑全部测试**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler/scripts
python -m pytest test_analysis_context_pack.py -v
```

Expected: 全部通过。

- [ ] **Step 8: 提交(若有未提交的修复)**

```bash
cd C:/Users/53519/Downloads/novel-disassembler-v12/novel-disassembler
git status
# 若无改动则跳过 commit;若有,统一提交
git add -A
git commit -m "test(analysis): smoke verify chapter_structure and narrative_structure CLI"
```

---

## 自检 Self-Review

**1. Spec 覆盖检查**

| Spec 章节 | 对应 task |
|---|---|
| 三 章节级 `chapter_structure` 五小节模板 | Task 1 Step 4(契约文本)+ Task 3(reduce 契约) |
| 四 全书级 `narrative_structure` 7 份产物字段 | Task 1 Step 3(outputs)+ Step 4(契约文本) |
| 五 执行顺序 / 上下文优先级 | Task 4 Step 1, 2(文档) |
| 六 命令与脚本设计(`--per-chapter`) | Task 2 全部 |
| 七 YAGNI(不改 chapter_analysis.j2 / 不写 Delta / 不引入新编号) | Task 1-3 实现中只增不改 `chapter_analysis.j2`;不引入标签 |
| 八 文件改动清单 | Task 1-4 全覆盖 |
| 九 验收标准 | Task 5 端到端冒烟 |

**2. 占位符扫描**:所有 step 含具体代码或具体命令;无 TODO/TBD/"类似 Task N 的写法"。

**3. 类型一致性**:
- `chapter_structure_per_chapter_outputs(seq: int) -> List[str]` 在 Task 2 定义,Task 2 Step 6、Task 3 Step 3 引用,命名一致。
- `reduce_contract_chapter_structure_per_chapter(seq: int) -> str` 在 Task 3 Step 3 定义,Task 2 Step 6 引用,命名一致。
- `TASKS["chapter_structure"]["outputs"]` 用 `{NNN}` 占位字符串,实际分章路径由 `chapter_structure_per_chapter_outputs` 生成;两处一致。

无类型冲突。计划可执行。
