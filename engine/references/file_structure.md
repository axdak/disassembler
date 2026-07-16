# 项目文件结构与初始化

## 项目目录结构

在**小说文件所在目录**下创建 `拆书_<书名>/` 项目目录：

```text
<小说文件所在目录>/
├── <小说文件>.txt
└── 拆书_<书名>/
    ├── 进度.json
    ├── 原文/
    │   └── <小说文件>
    ├── 原文拆解/
    │   ├── _索引.json
    │   ├── 第001章_<标题>.md
    │   └── ...
    ├── 章节处理/
    │   ├── 第001章_<标题>.md          # 章节10维度分析
    │   ├── 第001章_<标题>.json        # 本章Delta
    │   ├── 第001章_<标题>.validation.json  # Delta/章节校验报告，可选
    │   └── ...
    ├── 质量治理/
    │   ├── delta校验/
    │   │   ├── 第001章_<标题>.json
    │   │   └── ...
    │   ├── 章节校验/
    │   │   ├── 第001章_<标题>.json
    │   │   └── ...
    │   ├── 规范化/
    │   │   └── ...
    │   ├── 周期审计/
    │   │   ├── audit_001-005.md
    │   │   ├── correction_001-005.json
    │   │   └── ...
    │   ├── 按需治理/
    │   │   └── ...
    │   └── 最终审计/
    │       ├── final_audit.md
    │       ├── final_correction_delta.json
    │       └── validate_report.txt
    ├── 故事结构版本/
    │   ├── story_after_ch001.json
    │   ├── story_after_ch002.json
    │   └── ...
    ├── 结构变更日志/
    │   ├── diff_ch001.json
    │   ├── diff_ch002.json
    │   └── ...
    ├── 全书分析/
    │   ├── 剧情结构/
    │   │   ├── 章节梗概汇总.md
    │   │   ├── 剧情线索.md
    │   │   ├── 伏笔追踪.md
    │   │   └── 冲突图谱.md
    │   ├── 人物分析/
    │   │   ├── 人物档案.md
    │   │   ├── 人物关系.md
    │   │   └── 人物提及.json
    │   ├── 风格分析/
    │   │   ├── 文风分析.md
    │   │   ├── 节奏分析.md
    │   │   └── 高光场景.md
    │   ├── 视觉资产/
    │   │   ├── 视觉资产清单.md
    │   │   ├── 关键场景分镜表.md
    │   │   ├── AI绘图提示词素材.md
    │   │   ├── 角色外观一致性表.md
    │   │   └── 场景氛围表.md
    │   ├── 故事结构/
    │   │   ├── 分章/
    │   │   │   ├── ch001/章节结构.md
    │   │   │   ├── ch002/章节结构.md
    │   │   │   └── ...
    │   │   ├── 三幕式结构图.md
    │   │   ├── Brooks四部分结构图.md
    │   │   ├── Freytag五段结构图.md
    │   │   ├── 故事七要素档案.md
    │   │   ├── 故事力学评估.md
    │   │   ├── 故事工程学评估.md
    │   │   └── 小说骨架.md
    │   └── 拆书总报告.md
    ├── 故事结构_增量.json            # 过程数据库
    ├── 故事结构_草稿.json            # 最终整理草稿
    └── 故事结构.json                 # 最终交付
```

## 前置：初始化项目

1. 确认用户的小说文件路径。
2. 在小说文件所在目录下创建 `拆书_<书名>/` 项目目录。
3. 创建项目子目录结构：`原文/`、`原文拆解/`、`章节处理/`、`质量治理/`、`故事结构版本/`、`结构变更日志/`、`全书分析/` 等。
4. 复制小说文件到 `原文/` 目录。
5. 运行：

```bash
python <skill_path>/scripts/progress_manager.py init <项目目录> <源文件路径>
```

## 步骤1：章节拆分

**目标**：将小说原文按章节标题拆分为多个 md 文件。

```bash
python <skill_path>/scripts/split_chapters.py <原文文件> <项目目录>/原文拆解
```

脚本会：
- 自动检测文件格式，支持 `.txt` 和 `.docx`。
- txt 自动检测编码。
- docx 通过解析 Word XML 提取纯文本。
- 读取前500行检测章节标题模式。
- 支持"第X章""Chapter X""数字序号""序章/尾声/番外"等常见格式。
- 输出章节 md 文件和 `_索引.json`。

完成后：
```bash
python <skill_path>/scripts/progress_manager.py init-incremental <项目目录>
python <skill_path>/scripts/progress_manager.py complete <项目目录> 1
```
注意：`故事结构_增量.json` 必须用 `init-incremental` 初始化，不要由主控Agent手写骨架。

## 每章成功提交的硬产物

一章可信完成必须同时存在：

```text
章节处理/第NNN章_xxx.md
章节处理/第NNN章_xxx.json
质量治理/delta校验/第NNN章_xxx.json
质量治理/章节校验/第NNN章_xxx.json
故事结构版本/story_before_chNNN.json
故事结构版本/story_after_chNNN.json
结构变更日志/diff_chNNN.json
```

缺任何一个，都不算可信完成。
