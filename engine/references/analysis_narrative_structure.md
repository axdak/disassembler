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
