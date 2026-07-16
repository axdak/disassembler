# 故事结构维度（叙事节拍 / 七要素 / 力学 / 工程学 / 骨架）设计

日期：2026-06-24
作者：fanzhen + Claude

## 一、目标

在现有拆书流程上新增 **故事结构** 这一独立维度，覆盖：

1. **三幕式** 节拍打点（章节级）
2. **三种节拍图并行**：三幕式 / Brooks 四部分 / Freytag 五段（全书级）
3. **故事七要素** 档案（全书级）
4. **Brooks 故事力学** 六项评估（全书级，章节级仅给"本章信号"）
5. **Brooks 故事工程学** 六大核心能力评估（全书级，章节级仅给"本章信号"）
6. **小说骨架**：故事核 Logline + 网文骨架 + Freytag 段位（全书级 + 章节级信号）

核心约束（沿用 `references/analysis_four_dimensions.md`）：从原文反推，不允许编造；不确定写"待确认"；引用 `角色:名称` / `事件:名称` 等已有元素，**不引入新编号**；**不写入 `故事结构_增量.json`**，纯分析产物。

## 二、与现有维度的边界

| 已有维度 | 关注层 | 与本维度的边界 |
|---|---|---|
| plot | 事件层（伏笔/冲突） | 本维度不重复列事件，引用 `事件:名称` |
| plotlines | 线索层（主/支/暗/成长/情感线） | 本维度的"节拍"是叙事节拍模板，与"剧情线"正交；plotlines 答"几条线怎么走"，本维度答"整本书在三幕/四部分/五段中处于哪一拍" |
| outline | 卷级宏观 | outline 答"按冲突归并出几卷"；本维度答"映射到节拍模型后每一拍落在哪卷哪章" |
| detailed_outline | 章/场景级微观 | detailed_outline 写"本章发生什么"；本维度写"本章是哪个节拍点" |

定位口诀：**plotlines 横切、outline 归卷、本维度按节拍模板对照**。

## 三、章节级——独立 task `chapter_structure`

不修改现有 `chapter_analysis.j2`。每章独立产出一个 MD，模式对齐视觉资产的"分章独立产物"。

### 3.1 产物路径

```
全书分析/故事结构/分章/ch001/章节结构.md
全书分析/故事结构/分章/ch002/章节结构.md
...
```

### 3.2 章节结构.md 模板

```markdown
# 第NNN章 故事结构定位

## 1. 三幕式定位
- 所处幕：第一幕（建置 Setup）/ 第二幕（对抗 Confrontation）/ 第三幕（解决 Resolution）/ 过渡章（无节拍）
- 节拍点（仅当本章为节拍章时填写）：钩子 Hook / 激励事件 Inciting Incident / 第一情节点 FPP / 第一夹点 / 中点 Midpoint / 第二夹点 / 第二情节点 SPP / 高潮 Climax / 收束 Resolution
- 节拍证据：引用原文段落或章节分析MD相应位置；非节拍章写"过渡推进，无显著节拍点"

## 2. 故事七要素 · 本章信号
逐条列出；本章对该要素无推进时写"本章无推进"，禁止编造。

1. 主角：本章主角行为/状态/选择
2. 缺陷：本章是否暴露/加剧/缓解主角缺陷
3. 有利的故事环境：本章故事环境（时代/地理/制度/势力）对主角的助力或反作用
4. 反面角色：本章反派/对立面的行动或揭示
5. 主角的盟友：本章盟友登场/退场/立场变化
6. 改变人生的事件 · 危险：本章是否出现转折性事件或新危险
7. 整合故事要素：本章如何把上述要素整合推进主题

## 3. 故事力学 · 本章贡献（Brooks 六力学）
仅列出本章有贡献的项；其余省略。

- 强迫性前提（Compelling Premise）
- 戏剧张力（Dramatic Tension）
- 节奏（Pacing）
- 英雄共情（Hero Empathy）
- 代入体验（Vicarious Experience）
- 叙事策略（Narrative Strategy）

每项后写"本章贡献 + 原文证据"。

## 4. 故事工程学 · 本章执行（六大核心能力）
仅列出本章有可观察执行的项。

- 概念（Concept）
- 人物（Character）
- 主题（Theme）
- 结构（Structure）
- 场景执行（Scene Execution）
- 写作声音（Writing Voice）

## 5. 小说骨架 · 本章信号
- Logline 推进：本章动了"谁 / 想要什么 / 阻碍 / 代价"中的哪一项，引用原文证据
- 网文骨架命中：金手指 / 升级 / 主角光环 / 装逼打脸 / 套路 / 核心爽点 / 反派配置 中本章命中的点；未命中省略
- Freytag 段位：序幕 / 上升 / 高潮 / 下降 / 结局
```

### 3.3 质量规则

- 五个小节都强制原文/章节分析证据；没有信号写"本章无推进"或"过渡推进，无显著节拍点"。
- 不读全书原文，每章只用本章原文 + 本章章节分析MD。
- 禁止跨章归纳（跨章归纳交给全书级 task）。

## 四、全书级——独立 task `narrative_structure`

### 4.1 产物路径

```
全书分析/故事结构/三幕式结构图.md
全书分析/故事结构/Brooks四部分结构图.md
全书分析/故事结构/Freytag五段结构图.md
全书分析/故事结构/故事七要素档案.md
全书分析/故事结构/故事力学评估.md
全书分析/故事结构/故事工程学评估.md
全书分析/故事结构/小说骨架.md
```

### 4.2 七份产物字段约定

#### 三幕式结构图.md

| 字段 | 说明 |
|---|---|
| 幕 | 第一幕 / 第二幕上半 / 第二幕下半 / 第三幕 |
| 节拍 | 钩子 / 激励事件 / FPP / 第一夹点 / 中点 / 第二夹点 / SPP / 高潮 / 收束 |
| 章节区间 | 节拍覆盖章节号或单章 |
| 关键事件 | 引用 `事件:名称` |
| 证据 | 章节号 + 原文片段位置 |

#### Brooks四部分结构图.md

四部分（Setup / Response / Attack / Resolution）+ 五大里程碑（Hook / FPP / Midpoint / SPP / Climax）双表对齐：

| 部分 | 章节区间 | 角色任务 | 关键事件 | 与三幕式对齐处 | 证据 |
| 里程碑 | 章节号 | 触发事件 | 张力变化 | 证据 |

#### Freytag五段结构图.md

| 段位 | 章节区间 | 关键事件 | 与三幕式对齐处 | 证据 |
| --- | --- | --- | --- | --- |
| 序幕 Exposition |  |  |  |  |
| 上升 Rising Action |  |  |  |  |
| 高潮 Climax |  |  |  |  |
| 下降 Falling Action |  |  |  |  |
| 结局 Dénouement |  |  |  |  |

#### 故事七要素档案.md

每个要素一节：

```
## 1. 主角
- 主角全名：引用 `角色:名称`
- 初始状态 / 终态：原文证据
- 核心欲望（想要）：原文证据
- 核心需求（真正需要）：原文证据 / 待确认
- 演变轨迹：分阶段章节区间 + 关键事件 + 证据

## 2. 缺陷
- 缺陷类型（心理/道德/认知）
- 首次暴露章节 + 证据
- 高潮处是否克服 + 证据

（…3-7 同构…）
```

#### 故事力学评估.md

Brooks 六力学逐项：

| 力学项 | 强度（强/中/弱/待确认）| 评估说明 | 证据章节 | 风险/短板 |
|---|---|---|---|---|

#### 故事工程学评估.md

六大核心能力逐项：

| 能力 | 强度 | 评估说明 | 证据 | 短板诊断 |
|---|---|---|---|---|

#### 小说骨架.md

```
## 1. 故事核 / Logline
一句话：在 <时代/地点>，<主角>因为 <激励事件>，为了 <欲望/目标>，必须对抗 <反派/阻碍>，否则将付出 <代价>。

补充：
- 主题句：
- 类型标签：
- 目标读者：
- 证据章节：

## 2. 网文骨架零件
- 金手指：是否存在 / 类型 / 解锁规则 / 代价 / 证据
- 升级体系：等级阶梯 / 升级节奏 / 证据（与 settings 力量体系对齐）
- 主角光环点：场景列表 + 证据
- 装逼打脸节奏：典型循环章节 + 证据
- 套路类型：退婚流 / 赘婿流 / 系统流 / 重生流 / ……
- 核心爽点：从章节分析"爽点情绪点"汇总
- 反派配置：单线/梯队/合谋；引用 `角色:名称`

## 3. 节拍对照表
| 章节区间 | 三幕式节拍 | Brooks里程碑 | Freytag段位 |
冲突处标"待确认"，禁止强行统一。
```

### 4.3 三种节拍图的一致性要求

- 同一章在三张图里的节拍位置不能逻辑打架。
- 当三种节拍模板对同一区间给出冲突映射时，标"待确认"并保留三种各自的判断，**不强行折中**。
- "节拍对照表"是这一致性约束的可视化校验位。

## 五、执行顺序与依赖

```
summary → characters → plot → worldview → settings → plotlines → outline
       → detailed_outline → chapter_structure → narrative_structure → report
```

- `chapter_structure` 依赖：原文 + 章节分析MD（本章）。
- `narrative_structure` 依赖：`summary`（章节梗概汇总）+ `outline`（卷级框架）+ `plotlines`（线索层）+ 分章 `chapter_structure`（节拍打点原料）。
- 全书级会 reduce 分章 `章节结构.md` 中的"三幕式定位"作为节拍图原料。

## 六、命令与脚本设计

`scripts/analysis_context_pack.py` 的 `TASKS` 字典新增两条：

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

`chapter_structure` 沿用视觉资产分章模式：每章一个独立 pack；提供 `--all-chapters` 一键全跑。

`narrative_structure` 沿用 plotlines/outline 的分包→reduce 流程。

### 6.1 上下文优先级

`chapter_structure`：

```
本章原文 + 本章章节分析MD > 本章 Delta
```

`narrative_structure`：

```
分章 章节结构.md > 章节梗概汇总 > 全书大纲 > 剧情线总表 > 章节分析MD抽样 > 原文抽样
```

### 6.2 分包参数（推荐默认值）

```bash
# 章节级
python scripts/analysis_context_pack.py <项目目录> \
  --task chapter_structure \
  --chapters all \
  --include-original sample \
  --max-pack-chars 40000 \
  --max-original-chars 8000 \
  --max-analysis-chars 12000

# 全书级
python scripts/analysis_context_pack.py <项目目录> \
  --task narrative_structure \
  --chapters all \
  --max-pack-chars 70000
```

## 七、不做的事（YAGNI）

1. 不在 `chapter_analysis.j2` 增加任何小节；现有 10 节保持不动。
2. 不写入 `故事结构_增量.json`，不引入节拍标签前缀（`三幕节拍:` 等不做）。
3. 不引入 PL- 之外的新编号体系，全部复用 `事件:名称` / `角色:名称`。
4. 不做"按节拍模板自动生成大纲"的反向生成能力——本维度只反推、不创作。
5. 不强行让三种节拍模板互相折中——冲突处标"待确认"。

## 八、文件改动清单

| 文件 | 改动 |
|---|---|
| `scripts/analysis_context_pack.py` | `TASKS` 字典新增 `chapter_structure` 与 `narrative_structure`；为 `chapter_structure` 复用视觉资产的"分章独立产物"路径生成逻辑 |
| `prompts/`（新增 2 个 j2）| `chapter_structure.j2`、`narrative_structure.j2`（任务包模板） |
| `references/analysis_narrative_structure.md`（新增）| 本规范的精简版，作为运行时参考文档 |
| `references/fullbook_analysis_workbench.md` | 任务表新增 2 行；上下文优先级章节新增 2 段 |
| `references/analysis_four_dimensions.md` | 在"维度之间联动"末尾补一句指向 `analysis_narrative_structure.md`，不重复内容 |
| `references/file_structure.md` | 新增 `全书分析/故事结构/` 目录说明 |

## 九、验收标准

1. 在已有项目上运行 `chapter_structure --chapters all` 能为每章生成 `章节结构.md`，且每章 MD 至少包含 1.三幕式定位 这一节（其余可"本章无推进"）。
2. 运行 `narrative_structure` 能生成 7 份产物文件；三种节拍图在"节拍对照表"中可对齐或显式标"待确认"。
3. 所有结论都能回指章节号或 `事件:名称` / `角色:名称`；空缺写"待确认"。
4. 不修改 `故事结构_增量.json` 或 `故事结构.json`。
5. 现有 `chapter_analysis.j2` 输出格式不变，旧项目可继续使用。

## 后记：实施偏差（2026-06-24）

§ 八「文件改动清单」原列：

- `prompts/chapter_structure.j2`、`prompts/narrative_structure.j2`（任务包模板）

实际实施：两份任务包契约直接写在 `scripts/analysis_context_pack.py` 的 `output_contract()` 和 `reduce_contract_chapter_structure_per_chapter()` 中，与现有 `visual_assets` / `outline` 等任务保持一致（这些任务的契约同样是 inline 字符串，不是 .j2 文件）。简化原则：项目惯例统一。

实际实施补充未列入原 § 八 的改动：

- `scripts/run_pipeline.py`：`analysis-pack --task` 增加两个 choice；`ANALYSIS_OUTPUTS` 增加 `narrative_structure` 7 份产物；`final_pack` 的 `analysis_specs` 接入这 7 份产物；`REQUIRED_DIRS` 加入 `全书分析/故事结构`。
- `references/analysis_workbench_commands.md`：增加新 task 命令示例。
- `references/commands_and_resources.md`：更新推荐执行顺序链。
- `references/v12_patch_notes.md`：新增 changelog 段。
