# 全书/局部分析工作台设计

## 设计目标

把原先固定在“步骤3”的全书维度分析，改造成一个可随时唤起的分析能力：

```text
原文证据 + 章节分析MD + 故事结构_增量.json
↓
分析任务包
↓
分包结果
↓
Reduce 汇总
↓
阶段性报告 / 最终报告
```

这样做的目的：

1. 避免一次性塞入全书导致上下文爆炸。
2. 允许在任意章节范围内做人物、剧情线、文风、伏笔分析。
3. 当故事结构JSON经过质量治理后，可以局部刷新相关报告。
4. 把大模型产出变成“分包 + 汇总 + 证据约束”的流程，而不是一次性自由发挥。

## 任务类型

基础维度：

| task | 对应旧步骤 | 输出 |
|---|---|---|
| summary | 3a | 全书分析/剧情结构/章节梗概汇总.md |
| characters | 3b | 人物档案.md、人物关系.md、人物提及.json |
| plot | 3c | 剧情线索.md、伏笔追踪.md、冲突图谱.md |
| style | 3d | 文风分析.md、节奏分析.md、高光场景.md |
| visual_assets | 新增 | 视觉资产清单.md、关键场景分镜表.md、AI绘图提示词素材.md、角色外观一致性表.md、场景氛围表.md |
| report | 3e | 拆书总报告.md |
| custom | 新增 | 用户指定输出 |

四维度（详见 `analysis_four_dimensions.md`）：

| task | 定位 | 输出 |
|---|---|---|
| worldview | 世界观提取 | 全书分析/世界观/世界观档案.md |
| settings | 设定体系提取 | 全书分析/世界观/设定档案.md |
| plotlines | 剧情线梳理 | 剧情线总表.md、剧情线交汇矩阵.md |
| outline | 大纲反推 | 全书分析/剧情结构/全书大纲.md |
| detailed_outline | 细纲反推 | 全书分析/剧情结构/章节细纲.md |
| chapter_structure | 章节结构定位(分章) | 全书分析/故事结构/分章/chNNN/章节结构.md |
| narrative_structure | 故事结构全书分析 | 全书分析/故事结构/三幕式结构图.md 等 7 份产物 |

四维度与基础维度的层次区别：

```text
plot         → 事件层：发生了什么事件，埋了什么伏笔，谁和谁冲突
plotlines    → 线索层：这些事件构成几条线，每条线怎么起承转合，线之间怎么交汇

characters   → 人物档案和关系
worldview    → 世界观要素：时代/地理/制度/势力/历史
settings     → 具体设定：力量体系/魔法系统/种族/职业/规则/功法/物品

summary      → 每章梗概
outline      → 卷/篇章级宏观框架反推
detailed_outline → 章/场景级微观结构反推
```

## 上下文优先级

不同任务应选择不同上下文：

### summary

优先级：

```text
章节分析MD > 必要原文抽样 > 本章Delta
```

一般不需要放完整原文。

### characters

优先级：

```text
角色集完整摘要 > 指定角色JSON片段 > 章节分析MD > 原文抽样 > 本章Delta
```

如果角色很多，应按角色或章节范围拆包。

### plot

优先级：

```text
事件集/线索集摘要 > 章节梗概汇总 > 章节分析MD > 原文抽样 > 本章Delta
```

伏笔追踪必须核对原文证据，不能只看线索名称。

### style

优先级：

```text
原文抽样 > 章节分析MD中的情绪点/章节功能 > 高光场景候选
```

文风分析不能只看剧情概括。

### visual_assets

优先级：

```text
事件集中的视觉标签/视觉详情 > 章节分析MD第9节 > 原文抽样 > 人物/地点/物品集
```

视觉资产必须核对原文证据；没有证据的外观、服装、道具、环境细节标“待确认”。

### report

优先级：

```text
前置分析报告 > 故事结构_增量.json > 章节梗概汇总 > 原文抽样
```

### worldview

优先级：

```text
地点集 + 阵营集 > 章节分析MD中的世界观描写 > 原文抽样（含设定密集章节） > 本章Delta
```

### settings

优先级：

```text
其他事项集 + 物品集 > 章节分析MD中的信息增量 > 原文抽样（含设定密集章节） > 本章Delta
```

### plotlines

优先级：

```text
线索集 + 事件集 > 章节梗概汇总 > 章节分析MD > 原文抽样 > 本章Delta
```

伏笔追踪必须核对原文证据，不能只看线索名称。

### outline

优先级：

```text
章节梗概汇总 > 事件集（按分组/涉及章节排序）> 线索集 > 章节分析MD > 原文抽样
```

### detailed_outline

优先级：

```text
章节分析MD（10维度）> 章节梗概汇总 > 事件集 + 线索集 > 原文抽样 > 本章Delta
```

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

## 分包策略

长篇小说必须采用分包：

```text
pack_001.md -> partial_result_001.md
pack_002.md -> partial_result_002.md
...
reduce_prompt.md + partial_results -> final report
```

推荐参数：

```bash
python scripts/analysis_context_pack.py <项目目录> \
  --task characters \
  --chapters all \
  --include-original sample \
  --max-pack-chars 70000 \
  --max-original-chars 6000 \
  --max-analysis-chars 12000
```

## 质量规则

1. 每个关键结论必须带章节证据。
2. 不确定写“待确认”。
3. 不为了完整性编造关系、伏笔、阵营、冲突。
4. 分包结果之间冲突时，不要强行统一，先列入“待确认”。
5. 故事结构JSON变更后，相关报告需要可局部刷新。

## 与进度步骤的关系

“步骤3”只表示“固化一版基准全书报告”，不表示分析能力只能在步骤3执行。

正确理解：

```text
A 工作台：随时可执行，可反复执行，可局部执行
步骤3：在某个时间点，把工作台结果固化成基准报告
```
