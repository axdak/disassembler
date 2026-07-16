# v12 Patch Notes

## 目标

围绕事件目录可读性和视觉资产生产能力做增强：

1. 事件分组从单纯 `编号-剧情段` 升级为 8 位段号和复合剧情段短名：

```text
8位段号-40字以内复合剧情段短名
```

示例：

```text
00000010-退婚尊严线冲突爆发羞辱反击身份尊严
```

短名需压缩包含剧情、剧情线主题、叙事功能、爽点/情绪点、冲突悬念类型，不使用 `+` 分隔长格式。

2. 新增视觉资产维度，用于提取图片、漫画分镜、短剧分镜、角色卡、场景概念图和 AI 绘图提示词素材。
3. 扩展事件标签词库，支持剧情线主题、爽点情绪点、冲突悬念类型、画面类型、视觉用途。

## 修改内容

- `SKILL.md`
  - 章节分析模板升级为 10 维度。
  - 新增“事件分组与标签建议”。
  - 新增“画面 / 分镜 / 视觉资产候选”。
  - 新增 `visual_assets` 分析任务和相关命令。

- `references/story_structure_spec.md`
  - 更新事件 `分组` 推荐格式。
  - 说明 `标签集` 与 `分组` 的职责边界。
  - 新增视觉详情字段建议。

- `references/narrative_taxonomy.json`
  - 新增 `剧情线主题`、`爽点情绪点`、`冲突悬念类型`、`画面类型`、`视觉用途`。

- `references/visual_asset_spec.md`
  - 新增视觉资产维度完整规范。

- `scripts/story_schema_rules.py`
  - 允许事件集使用新增受控标签前缀。

- `scripts/run_pipeline.py`
  - 单章任务包加入 10 维章节分析模板。
  - `analysis-pack --task` 支持 `visual_assets`。
  - 初始化目录加入 `全书分析/视觉资产`。

- `scripts/analysis_context_pack.py`
  - 新增 `visual_assets` 任务定义和输出合同。

## 设计原则

- `分组` 用于连续剧情段排序和页面浏览，不替代标签系统。
- `标签集` 用于跨事件筛选、聚合和导航。
- 每个事件应额外保留一个无冒号四维复合标签，格式为 `剧情线主题+叙事功能+爽点/情绪点+冲突悬念类型`；该标签不包含段号和剧情段名，不下沉到 `详情.补充标签`，用于界面直读。
- 视觉细节放在 `详情` 和 `全书分析/视觉资产`，不新增顶层 `图片集`。
- 所有视觉资产必须有原文或章节分析证据，不得为好看编造。

## v12.1 标签自动压缩

- 新增 `scripts/compress_tags.py`：在 Delta 入库前和 merge 后两个时点，机械地把所有带受控前缀（`X:Y` 形态，X 出现在 `narrative_taxonomy.json` 顶层键中）的标签从 `标签集` 下层到 `详情.补充标签`，同时按 `references/tag_limits.json` 对剩余自由标签做 FIFO 截断，超出部分也下层。
- 新增 `references/tag_limits.json`：每集合标签上限配置（事件集 12，其它 8）。
- `scripts/run_pipeline.py` 的 `run`/`replay` 流程：在 `validate_delta` 前加 `compress_tags --delta`，在 `validate_structure` 前加 `compress_tags --structure`；报告分别写入 `质量治理/delta校验/compress_ch{seq:03d}.json` 和 `质量治理/规范化/compress_after_ch{seq:03d}.json`。
- 设计目的：让「未知受控标签 / 越权前缀」一类报错由确定性脚本而非 LLM 解决，避免 repair 循环里 LLM 用「整删前缀」的捷径过校验导致语义丢失。
- 已知影响：`画面类型:`、`视觉用途:` 这两个视觉资产入口前缀也会被下层；后续若启用视觉资产工作流，相关查询要同时读 `标签集` 和 `详情.补充标签`。

## 故事结构维度（chapter_structure / narrative_structure）

新增"故事结构"分析维度，与视觉资产维度并列，提供 章节级节拍打点 + 全书级三种节拍图 + 故事七要素 + Brooks 故事力学/工程学 + 小说骨架。

- `scripts/analysis_context_pack.py`
  - 注册两个新 task：`chapter_structure`（分章独立产物，每章 `章节结构.md`）和 `narrative_structure`（全书 7 份产物）。
  - `--per-chapter` 兼容 `chapter_structure`；强制要求 `--per-chapter` 否则拒绝。
  - `narrative_structure` 全书包自动拼接「全书背景资料」段，含分章 `章节结构.md` 与 `章节梗概汇总.md` / `全书大纲.md` / `剧情线总表.md`。

- `scripts/run_pipeline.py`
  - `analysis-pack --task` 支持新 task。
  - `ANALYSIS_OUTPUTS` 加入 `narrative_structure` 7 份产物。
  - `final_pack` 把 7 份故事结构产物纳入最终整理参考材料。
  - `REQUIRED_DIRS` 加入 `全书分析/故事结构`。

- `references/`
  - 新增 `analysis_narrative_structure.md` 规范。
  - `fullbook_analysis_workbench.md`、`analysis_four_dimensions.md`、`file_structure.md`、`analysis_workbench_commands.md`、`commands_and_resources.md` 同步更新。

- 文件产物
  - `全书分析/故事结构/分章/chNNN/章节结构.md`（每章 1 份）
  - `全书分析/故事结构/三幕式结构图.md` 等 7 份全书级产物

- 不变事项
  - 不引入新 schema。
  - 不写入 `故事结构_增量.json`，纯分析产物。
  - 现有 `chapter_analysis.j2`、`visual_assets` 工作流保持不变。
