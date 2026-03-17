# LLM Tag Corrector

利用多模态大语言模型（VLM）自动审核和修正 Danbooru 风格图像标签的工具。提供 GUI 界面逐张审核，支持后台批量并发预取，大幅提升标注效率。

## 这个工具解决什么问题？

在训练 LoRA 等图像模型时，通常需要为训练图片标注 Danbooru 风格的 tag。自动标注工具（如 WD Tagger）经常产生以下问题：

- **错误标签**：画面中没有翅膀却标了 `wings`，只有一个角色却标了 `multiple_girls`
- **属性偏差**：明明是金发却标成 `black_hair`，长发标成 `short_hair`
- **遗漏标签**：明显的特征（如眼镜、帽子、坐姿）没有被标注
- **无效标签**：标注了 Danbooru 数据库中根本不存在的 tag

本工具通过 VLM 看图理解画面内容，结合本地 Danbooru 标签数据库进行验证，自动完成标签的删除、修正和补充。

## 工作流程

```
图片 + 原始标签
       │
       ▼
 ┌─────────────────────┐
 │  预取 Wiki 摘要      │  从本地 DB 获取标签的 Danbooru Wiki 描述
 │  （缓存命中则跳过）   │  总结为视觉特征描述，帮助 VLM 理解标签含义
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │  VLM 统一审查        │  一次 API 调用同时完成：
 │                      │  1. 删标：移除与画面不符的标签
 │                      │  2. 改标：修正属性描述有误的标签
 │                      │  3. 添标：补充明显遗漏的特征标签
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │  Danbooru 标签验证    │  VLM 通过 tool call 调用本地数据库
 │                      │  验证新标签是否为有效 Danbooru 标签
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │  后置自动修正         │  对验证失败的标签进行模糊匹配：
 │                      │  - 别名解析（如 `blush_stickers` → `blush`)
 │                      │  - trigram 模糊搜索 + 前缀匹配
 │                      │  - 相似度低于阈值则丢弃
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │  GUI 人工审核         │  显示图片、原始标签、建议修改
 │                      │  可单独勾选接受 删标/改标/添标
 │                      │  确认后覆盖原 txt 文件
 └─────────────────────┘
```

## 核心特性

- **三合一审查**：删标、改标、添标在一次 API 调用中完成，节省 token 消耗
- **本地 Danbooru 数据库**：离线验证标签有效性，无需在线查询 Danbooru API
- **Wiki 摘要缓存**：标签的视觉含义描述预先总结并缓存到数据库，避免重复调用摘要模型
- **自动模糊修正**：无效标签通过别名解析、trigram 索引、前缀匹配自动修正为最接近的有效标签
- **高并发预取**：后台线程池并发预取 API 结果，审核时无需等待
- **断点续传**：已处理的图片记录在 `process.log` 中，重启后自动跳过
- **GUI 审核界面**：逐张展示图片和标签修改建议，支持手动编辑最终结果
- **图片自动压缩**：超过大小限制的图片自动降质/缩放，不影响原文件

## 快速开始

### 1. 安装依赖

```bash
pip install openai Pillow
```

### 2. 配置

复制配置模板并填入你的 API 密钥：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
    "caption_api": {
        "api_key": "你的标注模型 API Key",
        "base_url": "OpenAI 兼容 API 地址",
        "model_name": "支持视觉的模型名称（如 gemini-2.5-flash）"
    },
    "summary_api": {
        "api_key": "你的摘要模型 API Key",
        "base_url": "OpenAI 兼容 API 地址",
        "model_name": "文本模型名称（如 deepseek-v3）"
    }
}
```

- **caption_api**：用于看图审查标签的多模态模型（需支持视觉 + tool calling）
- **summary_api**：用于总结 Wiki 描述的文本模型（普通文本模型即可，推荐便宜的）

### 3. 准备数据

将图片和对应的 `.txt` 标签文件放在同一目录下：

```
你的图片目录/
├── image001.jpg
├── image001.txt    ← 包含逗号分隔的 booru tag
├── image002.png
├── image002.txt
└── ...
```

修改 `gemini_caption.py` 中的 `IMAGE_DIR` 为你的图片目录路径。

### 4. 运行

```bash
python gemini_caption.py
```

GUI 窗口会逐张展示审核建议，你可以：
- 勾选/取消勾选 删标、改标、添标 的建议
- 手动编辑最终标签
- 点击「确认」覆盖原 txt 文件，或「跳过」保留原标签

## Danbooru 标签数据库

仓库通过 Git LFS 提供了预构建的 `danbooru_tags.db`（约 475MB），包含：
- 所有 Danbooru 标签及其 post count
- 标签别名映射
- Wiki 页面描述
- 预生成的 Wiki 视觉摘要（已缓存大部分常用标签）

直接使用即可，无需自己构建或调用摘要 API。

## 配置选项

在 `gemini_caption.py` 顶部可调整：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DANBOORU_ENABLED` | 启用 Danbooru 标签验证 | `True` |
| `DANBOORU_AUTOCORRECT` | 启用无效标签自动修正 | `True` |
| `DANBOORU_AUTOCORRECT_MIN_SIMILARITY` | 自动修正最低相似度阈值 | `0.5` |
| `WIKI_SUMMARY_ENABLED` | 启用 Wiki 摘要（需同时启用 Danbooru） | `True` |
| `REMOVE_TAGS_ENABLED` | 启用删标功能 | `True` |
| `CORRECT_TAGS_ENABLED` | 启用改标功能 | `True` |
| `PREFETCH_WORKERS` | 后台预取并发数 | `125` |
| `TEMPERATURE` | 模型温度 | `0.2` |

## 许可

MIT
