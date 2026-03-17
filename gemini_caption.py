import os
import base64
import mimetypes
import time
import json
import re
import difflib
import traceback
import threading
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox
from concurrent.futures import ThreadPoolExecutor, Future
from PIL import Image, ImageTk
from openai import OpenAI

# ================= 配置区域 =================
API_KEY = "sk-Af4zRFZVP4Yu6UGcDXKntT9ULsTIMuODr8wx6zTQ37KTTOaS"  
BASE_URL = "https://sd.rnglg2.top:30000/v1"
MODEL_NAME = "google/gemini-3-flash-preview"
IMAGE_DIR = r"E:\Lora_dataset\鬼针草-tag"

# Danbooru 本地数据库配置（用于验证标签是否真实存在）
# 请先运行 build_danbooru_db.py 构建数据库
DANBOORU_ENABLED = True  # 是否启用 Danbooru 标签验证
DANBOORU_AUTOCORRECT = True  # 是否启用自动修正无效标签（通过本地数据库模糊匹配）
DANBOORU_AUTOCORRECT_MIN_SIMILARITY = 0.5  # 自动修正最低相似度阈值（0~1），低于此值则直接丢弃
DANBOORU_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "danbooru_tags.db")  # 本地标签数据库路径

# Wiki 摘要模型配置（用于总结 Danbooru wiki 描述的视觉含义，让标注模型审查标签是否匹配画面）
SUMMARY_API_KEY = "sk-8MwYLh7th9ke0o01Daq1p27aAnEE8tDi22PnEL5MMhF54pjA"          # 在此填入摘要模型的 API Key
SUMMARY_BASE_URL = "https://api.bltcy.ai/v1"         # 在此填入摘要模型的 Base URL（OpenAI 兼容格式）
SUMMARY_MODEL_NAME = "deepseek-v3.2"       # 在此填入摘要模型名称
WIKI_SUMMARY_ENABLED = True   # 是否启用 Wiki 摘要功能（需同时启用 DANBOORU_ENABLED）

# 功能开关
REMOVE_TAGS_ENABLED = True   # 是否启用删标功能（设为 False 则跳过删标步骤）
CORRECT_TAGS_ENABLED = True  # 是否启用改标功能（设为 False 则跳过改标步骤）
PROMPT_REMOVE = """你是一个精准的图像标签审核员。你的任务是：审查标签列表，删除与画面明显不符的标签。

我会给你一张图片和一组用逗号分隔的 booru tag。

## 审核框架

### 第一步：判断画面裁切范围
确定图片展示的是全身、上半身、面部特写还是其他构图。这决定了哪些元素"不可见"是正常的。

### 第二步：对每个标签分类判断

**A. 可直接验证的标签**（元素所在区域在画面内清晰可见）
- 该区域清晰可见但元素不存在 → 删除
- 该区域清晰可见且元素存在 → 保留

**B. 因裁切/遮挡而无法验证的标签**
- 元素所在区域被裁切到画面外或被其他物体遮挡 → 保留（无法否定）

**C. 构图/氛围类标签**（solo, upper_body, looking_at_viewer, simple_background 等）
- 这类标签描述画面整体属性，根据画面实际情况判断
- solo：只有一个角色时正确；有多个角色时删除
- upper_body/cowboy_shot/full_body 等：与实际构图不符时删除

**D. 互相矛盾的标签**
- 两个标签互相矛盾（如 standing 和 sitting 同时出现）→ 删除与画面不符的那个

### 第三步：最终检查
- 因裁切不可见的元素标签不算错误，不要删
- 颜色"有点偏差"但元素确实存在的标签不要删（颜色问题应交给改标处理）
- 不确定的标签保留

## 典型应删除案例
- 角色背部完全可见，无翅膀无尾巴，但标签含 wings / tail
- 全身图中角色没有佩戴帽子，但标签含 hat
- 画面中只有一个角色，但标签含 multiple_girls 或 2girls
- 画面是面部特写，但标签含 full_body
- 标签含 standing 但角色明显是坐着的，且全身可见

## 禁止删除的标签类型
以下类型的标签无论如何都不得删除：
- **角色名**（如 hatsune_miku, rem_(re:zero) 等）
- **画师名**（如 artist:xxx 或任何画师署名标签）
- **风格描述**（如 anime_coloring, watercolor, sketch, flat_color, monochrome, realistic, cel_shading 等）

## 典型不应删除案例
- 上半身构图中的 skirt / boots / thighhighs（可能在画面外）
- 有刘海遮挡时的 forehead / headband 类标签
- 背景阴暗看不清细节时的背景元素标签
- 颜色标签可能不太准但元素本身存在
- 角色名、画师名、风格标签（这些不在审查范围内）

当前标签列表：
{tags}

请在思考中完成推理，然后以以下 JSON 格式输出结果，包裹在 ```json 和 ``` 之间：
```json
{{
    "removed_tags": ["要删除的tag"]
}}
```
所有 tag 使用标准 booru tag 格式（下划线连接）。不要输出额外解释。
"""

PROMPT_CORRECT = """你是一个精准的图像标签审核员。你的任务是：审查标签列表，修正描述元素存在但属性标注有误的标签。

我会给你一张图片和一组用逗号分隔的 booru tag。

## 核心原则
- 修正的前提：元素本身存在于画面中，但属性描述有误
- 不要删除标签（不是你的任务）
- 不要添加新标签（不是你的任务）

## 禁止修正的标签类型
以下类型的标签无论如何都不得修改：
- **角色名**（如 hatsune_miku, rem_(re:zero) 等）
- **画师名**（如 artist:xxx 或任何画师署名标签）
- **风格描述**（如 anime_coloring, watercolor, sketch, flat_color, monochrome, realistic, cel_shading 等）

## 重点检查领域

### 1. 颜色类属性（最常见的错误）
仔细比对以下颜色标签与画面实际颜色：
- **发色**：black_hair / brown_hair / blonde_hair / red_hair / blue_hair / white_hair / pink_hair / green_hair / purple_hair / grey_hair / orange_hair / silver_hair
- **瞳色**：blue_eyes / red_eyes / green_eyes / brown_eyes / purple_eyes / yellow_eyes / pink_eyes / black_eyes / grey_eyes / orange_eyes / aqua_eyes
- **服装颜色**：如 red_dress → blue_dress, black_shirt → white_shirt
- 注意：强烈的环境光/色调可能影响颜色感知，判断时需考虑光照条件

### 2. 长度/尺度类属性
- **发长**：short_hair / medium_hair / long_hair / very_long_hair（参考：肩膀以上=short，肩到胸=medium，胸以下=long，过腰=very_long）
- **裙长**：miniskirt / skirt / long_skirt
- **袖长**：short_sleeves / long_sleeves / sleeveless

### 3. 数量类属性
- 人数：1girl / 2girls / 3girls / 1boy / 2boys 等
- 注意背景中或画面边缘的半可见角色

### 4. 类型混淆
- 相似但不同的衣物：skirt ↔ dress, hat ↔ cap, jacket ↔ coat
- 相似但不同的发型：ponytail ↔ twintails, braid ↔ french_braid, bob_cut ↔ short_hair
- 相似但不同的动作：sitting ↔ kneeling, waving ↔ reaching

## 修正判断标准
- 颜色与标签明显不同（排除光照因素后）→ 修正
- 长度分类差了两档以上（如 very_long_hair 标为 short_hair）→ 修正
- 物品类型完全不对（明显是裙子但标为裤子）→ 修正
- 颜色差异较小或在两个分类的边界上 → 不修正
- 光照/滤镜导致颜色失真无法确定真实颜色 → 不修正

当前标签列表：
{tags}

请在思考中完成推理，然后以以下 JSON 格式输出结果，包裹在 ```json 和 ``` 之间：
```json
{{
    "corrected_tags": {{"原始tag": "修正后tag"}}
}}
```
所有 tag 使用标准 booru tag 格式（下划线连接）。不要输出额外解释。

**重要：标签验证步骤**
在给出最终 JSON 结果之前，你**必须**先调用 `check_danbooru_tags` 工具，将你打算修正后的所有标签发送给工具进行验证。
- 只有经过验证确认存在于 Danbooru 数据库中的标签，才能出现在最终结果中。
- 如果工具返回某个标签不存在(not_found)，你必须从结果中移除该修正项，或尝试用一个相近的有效标签替代。
- 如果你没有要修正的标签，则无需调用工具。
"""

PROMPT_ADD = """你是一个精准的图像标签标注员。你的任务是：检查标签列表，补充画面中明显存在但被遗漏的重要特征标签。

我会给你一张图片和一组用逗号分隔的 booru tag。

## 核心原则
- 只补充明显遗漏的重要特征，不要过度标注
- 不要删除或修改现有标签（不是你的任务）
- 所有添加的标签必须是标准 booru tag

## 应补充的标签类型（按优先级）

### 优先级1：基础人物特征（缺一必补）
- 人数：1girl, 1boy, 2girls, multiple_girls 等
- 发色：blonde_hair, black_hair, blue_hair 等
- 瞳色：blue_eyes, red_eyes, green_eyes 等
- 发长：short_hair, medium_hair, long_hair, very_long_hair
- 明显发型：ponytail, twintails, braid, bob_cut, side_ponytail 等

### 优先级2：显著服装与配饰
画面中清晰可见但完全未被标记的：
- 主要衣物：dress, shirt, skirt, jacket, uniform, bikini, swimsuit 等
- 显眼配饰：glasses, hat, earrings, necklace, scarf, hair_ribbon, bow 等
- 鞋类（仅在可见时）：boots, shoes, barefoot, sandals 等

### 优先级3：构图与姿势
- 视角：from_above, from_below, from_side, from_behind
- 构图：portrait, upper_body, cowboy_shot, full_body
- 姿势：standing, sitting, lying, kneeling, walking 等
- 朝向：looking_at_viewer, looking_away, looking_back, profile

### 优先级4：显著画面元素
- 明显的背景：outdoors, indoors, sky, water, forest, cityscape 等
- 占据显著位置的物品：book, sword, umbrella, phone, cup 等
- 明显的表情：smile, open_mouth, closed_eyes, blush, crying 等

## 禁止添加的标签
- 风格类（anime_coloring, watercolor, sketch, flat_color, monochrome 等）
- 画师名
- 角色名（hatsune_miku, rem_(re:zero) 等）
- 版权/作品名（vocaloid, re:zero 等）

## 添加标准
- 只补充画面中清楚可见但标签列表完全未覆盖的特征
- 某特征已有相近标签覆盖时，不要重复添加
- 每次添加控制在合理数量（通常 0-5 个）
- 不确定的特征不要添加

当前标签列表：
{tags}

请在思考中完成推理，然后以以下 JSON 格式输出结果，包裹在 ```json 和 ``` 之间：
```json
{{
    "added_tags": ["要添加的tag"]
}}
```
所有 tag 使用标准 booru tag 格式（下划线连接）。不要输出额外解释。

**重要：标签验证步骤**
在给出最终 JSON 结果之前，你**必须**先调用 `check_danbooru_tags` 工具，将你打算添加的所有标签发送给工具进行验证。
- 只有经过验证确认存在于 Danbooru 数据库中的标签，才能出现在最终结果中。
- 如果工具返回某个标签不存在(not_found)，你必须从结果中移除该标签，或尝试用一个相近的有效标签替代。
- 如果你没有要添加的标签，则无需调用工具。
"""

PROMPT_REVIEW = """你是一个精准的图像标签审核员。你的任务是：一次性审查标签列表，完成所有必要的删除、修正和补充。

## 工作方法
1. **观察图片**：注意人物特征、服装、姿势、视角、背景、画面裁切范围
2. **逐个审查现有标签**：结合 Wiki 描述（如有）判断正确性
3. **检查遗漏**：是否有明显的重要特征未被标记
4. **做出判断**：对有信心的判断做修改，不确定的保持原样

{wiki_context}

{task_rules}

当前标签列表：
{tags}

请在思考中完成推理，然后以以下 JSON 格式输出结果，包裹在 ```json 和 ``` 之间：
```json
{{
    "removed_tags": ["要删的tag"],
    "corrected_tags": {{"原tag": "新tag"}},
    "added_tags": ["要加的tag"]
}}
```
所有 tag 使用标准 booru tag 格式（下划线连接）。不要输出额外解释。
{disabled_fields_note}
{tool_instructions}"""

# 超参数
TEMPERATURE = 0.2
TOP_P = 0.9
MAX_RETRIES = 3
RETRY_DELAY = 1
PREFETCH_WORKERS = 125  # 后台预取并发数    
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB，适用于所有模型
ENABLE_RATE_LIMIT = False    # 是否启用请求速率限制
RATE_LIMIT_SECONDS = 1    # 两次请求之间的绝对最小间隔（秒）
# ===========================================

_request_lock = threading.Lock()
_last_request_time = 0.0

from io import BytesIO


# ================= Danbooru 本地数据库 =================

_db_conn_local = threading.local()


def _get_db_conn():
    """获取线程本地的 SQLite 连接（只读模式）"""
    if not hasattr(_db_conn_local, "conn") or _db_conn_local.conn is None:
        if not os.path.exists(DANBOORU_DB_PATH):
            raise FileNotFoundError(
                f"本地标签数据库不存在: {DANBOORU_DB_PATH}\n"
                "请先运行 build_danbooru_db.py 构建数据库。"
            )
        _db_conn_local.conn = sqlite3.connect(DANBOORU_DB_PATH, check_same_thread=False)
        _db_conn_local.conn.execute("PRAGMA query_only=ON")
    return _db_conn_local.conn


DANBOORU_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_danbooru_tags",
            "description": "查询 Danbooru 数据库，验证一组标签是否为真实存在的有效 booru tag。返回每个标签的存在状态，以及已存在标签的视觉含义描述（来自 Danbooru wiki）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要验证的标签列表，使用下划线格式（如 blue_eyes, long_hair）"
                    }
                },
                "required": ["tags"]
            }
        }
    }
]


def check_danbooru_tags(tags):
    """
    使用本地 SQLite 数据库批量验证标签是否存在。
    同时查 tag_aliases 解析别名。
    如果启用了 Wiki 摘要功能，还会获取并总结 wiki 描述。
    参数: tags — 标签列表（下划线格式）
    返回: {"found": [...], "not_found": [...], "wiki_summaries": {...}}
    """
    if not tags:
        return {"found": [], "not_found": [], "wiki_summaries": {}}

    query_tags = [t.strip().lower().replace(" ", "_") for t in tags if t.strip()]
    if not query_tags:
        return {"found": [], "not_found": [], "wiki_summaries": {}}

    try:
        conn = _get_db_conn()

        # 批量查询标签是否存在
        placeholders = ",".join("?" for _ in query_tags)
        rows = conn.execute(
            f"SELECT name FROM tags WHERE name IN ({placeholders})", query_tags
        ).fetchall()
        found_names = {r[0] for r in rows}

        # 对未找到的标签，检查是否是别名
        not_found_direct = [t for t in query_tags if t not in found_names]
        alias_resolved = {}
        if not_found_direct:
            ph2 = ",".join("?" for _ in not_found_direct)
            alias_rows = conn.execute(
                f"SELECT antecedent_name, consequent_name FROM tag_aliases WHERE antecedent_name IN ({ph2}) AND status='active'",
                not_found_direct
            ).fetchall()
            for ant, con in alias_rows:
                alias_resolved[ant] = con
                found_names.add(ant)  # 别名也算 found

        found = [t for t in query_tags if t in found_names]
        not_found = [t for t in query_tags if t not in found_names]

        # 获取并总结 wiki 描述
        wiki_summaries = {}
        if found and WIKI_SUMMARY_ENABLED and DANBOORU_ENABLED:
            try:
                # 对别名解析后的标签，查 wiki 时用 consequent_name
                wiki_query_tags = []
                for t in found:
                    wiki_query_tags.append(alias_resolved.get(t, t))
                wiki_bodies = fetch_local_wiki(wiki_query_tags)
                print(f"[本地 Wiki] 获取到 {len(wiki_bodies)}/{len(found)} 个标签的 wiki 页面")
                wiki_summaries = summarize_tag_wikis(wiki_bodies)
                print(f"[Wiki 摘要] 成功获取 {len(wiki_summaries)}/{len(found)} 个标签的摘要")
            except Exception as e:
                print(f"[Wiki 摘要] 获取摘要失败: {e}")

        return {"found": found, "not_found": not_found, "wiki_summaries": wiki_summaries}

    except FileNotFoundError:
        raise
    except Exception as e:
        print(f"[本地DB错误] {e}")
        return {"found": query_tags, "not_found": [], "wiki_summaries": {}, "error": str(e)}


def _find_best_tag_match(bad_tag, conn):
    """
    对一个无效标签，通过本地数据库模糊匹配找到最佳候选。
    策略：
      1. 别名精确匹配 — 直接查 tag_aliases
      2. trigram 索引模糊搜索 — 找出共享 trigram 最多的候选
      3. 前缀匹配 — LIKE 'prefix%'
      4. 从所有候选中，用 difflib 字符串相似度选最佳
    返回: (best_tag_name, similarity) 或 (None, 0)
    """
    candidates = {}  # {tag_name: post_count}

    # ---- 策略1: 别名精确匹配（最高优先级）----
    alias_row = conn.execute(
        "SELECT consequent_name FROM tag_aliases WHERE antecedent_name = ? AND status='active'",
        (bad_tag,)
    ).fetchone()
    if alias_row:
        # 直接命中别名，返回 1.0 相似度
        return alias_row[0], 1.0

    # 也尝试去掉/添加下划线的变体
    variants = set()
    if "_" in bad_tag:
        variants.add(bad_tag.replace("_", ""))
    else:
        for i in range(1, len(bad_tag)):
            variants.add(bad_tag[:i] + "_" + bad_tag[i:])
    for v in variants:
        row = conn.execute(
            "SELECT consequent_name FROM tag_aliases WHERE antecedent_name = ? AND status='active'",
            (v,)
        ).fetchone()
        if row:
            return row[0], 1.0

    # ---- 策略1.5: 组件级别名替换 ----
    # 对复合标签（如 butt_focus），逐段查别名（butt→ass），
    # 重组后检查是否为有效标签（ass_focus）
    if "_" in bad_tag:
        parts = bad_tag.split("_")
        if len(parts) >= 2:
            # 对每个组件查别名，收集所有可能的替换
            part_variants = []
            for p in parts:
                alias_row = conn.execute(
                    "SELECT consequent_name FROM tag_aliases WHERE antecedent_name = ? AND status='active'",
                    (p,)
                ).fetchone()
                if alias_row:
                    part_variants.append([p, alias_row[0]])  # 原始 + 别名
                else:
                    part_variants.append([p])
            # 生成所有组合（排除全部为原始值的情况）
            from itertools import product
            for combo in product(*part_variants):
                candidate = "_".join(combo)
                if candidate == bad_tag:
                    continue
                # 检查这个组合是否是有效标签
                tag_row = conn.execute(
                    "SELECT post_count FROM tags WHERE name = ? AND is_deprecated = 0",
                    (candidate,)
                ).fetchone()
                if tag_row:
                    return candidate, 1.0

    # ---- 策略2: trigram 模糊搜索 ----
    padded = f"  {bad_tag}  "
    trigrams = set()
    for i in range(len(padded) - 2):
        trigrams.add(padded[i:i+3])

    if trigrams:
        tri_placeholders = ",".join("?" for _ in trigrams)
        # 找出共享 trigram 最多的标签，取 top 30
        tri_rows = conn.execute(
            f"""SELECT t.tag_name, tags.post_count
                FROM tag_trigrams t
                JOIN tags ON tags.name = t.tag_name
                WHERE t.trigram IN ({tri_placeholders})
                GROUP BY t.tag_name
                ORDER BY COUNT(*) DESC
                LIMIT 30""",
            list(trigrams)
        ).fetchall()
        for name, pc in tri_rows:
            candidates[name] = max(candidates.get(name, 0), pc)

    # ---- 策略3: 前缀匹配 ----
    prefixes_to_try = set()
    if len(bad_tag) >= 4:
        prefixes_to_try.add(bad_tag[:len(bad_tag)*2//3])
    parts = bad_tag.split("_")
    for p in parts:
        if len(p) >= 3:
            prefixes_to_try.add(p)

    for prefix in prefixes_to_try:
        rows = conn.execute(
            "SELECT name, post_count FROM tags WHERE name LIKE ? AND is_deprecated=0 ORDER BY post_count DESC LIMIT 10",
            (prefix + "%",)
        ).fetchall()
        for name, pc in rows:
            candidates[name] = max(candidates.get(name, 0), pc)

    if not candidates:
        return None, 0.0

    # ---- 选择最佳候选 ----
    best_tag = None
    best_score = 0.0
    best_sim = 0.0
    for cand_tag, post_count in candidates.items():
        sim = difflib.SequenceMatcher(None, bad_tag, cand_tag).ratio()
        popularity_bonus = min(0.1, post_count / 1000000)
        score = sim + popularity_bonus
        if score > best_score:
            best_score = score
            best_tag = cand_tag
            best_sim = sim

    if best_tag:
        return best_tag, best_sim
    return None, 0.0


def autocorrect_invalid_tags(not_found_tags):
    """
    对一组无效标签，尝试自动修正为最接近的有效 Danbooru 标签。
    使用本地数据库进行模糊匹配。
    参数: not_found_tags — 无效标签列表
    返回: dict — {原始无效标签: 修正后的有效标签}，无法修正的不包含在内
    """
    if not not_found_tags or not DANBOORU_AUTOCORRECT:
        return {}

    conn = _get_db_conn()
    corrections = {}

    for bad_tag in not_found_tags:
        try:
            best_tag, similarity = _find_best_tag_match(bad_tag, conn)
            if best_tag and similarity >= DANBOORU_AUTOCORRECT_MIN_SIMILARITY:
                if best_tag != bad_tag:
                    corrections[bad_tag] = best_tag
                    print(f"[自动修正] '{bad_tag}' → '{best_tag}' (相似度: {similarity:.2f})")
                else:
                    print(f"[自动修正] '{bad_tag}' 最佳匹配是自身，跳过")
            else:
                if best_tag:
                    print(f"[自动修正] '{bad_tag}' 最佳匹配 '{best_tag}' 相似度 {similarity:.2f} 低于阈值 {DANBOORU_AUTOCORRECT_MIN_SIMILARITY}，丢弃")
                else:
                    print(f"[自动修正] '{bad_tag}' 无法找到任何匹配，丢弃")
        except Exception as e:
            print(f"[自动修正] 修正 '{bad_tag}' 时出错: {e}")

    return corrections


def fetch_local_wiki(tags):
    """
    从本地数据库获取 wiki 页面。
    参数: tags — 标签列表（下划线格式）
    返回: dict[str, str] — {tag_name: wiki_body}
    """
    if not tags:
        return {}

    conn = _get_db_conn()
    wiki_bodies = {}
    placeholders = ",".join("?" for _ in tags)
    rows = conn.execute(
        f"SELECT title, body FROM wiki_pages WHERE title IN ({placeholders})",
        [t.strip().lower().replace(" ", "_") for t in tags]
    ).fetchall()
    for title, body in rows:
        if body and body.strip():
            wiki_bodies[title] = body.strip()
    return wiki_bodies


def summarize_tag_wikis(wiki_bodies):
    """
    将 Danbooru wiki 描述总结为简洁的视觉特征描述。
    优先从数据库读取预缓存的摘要，只对缺失的调用摘要模型。
    参数: wiki_bodies — {tag_name: wiki_body_dtext}
    返回: {tag_name: summary_string}，总结失败的标签不包含在结果中
    """
    if not wiki_bodies:
        return {}

    summaries = {}

    # ---- 优先从数据库读取已缓存的摘要 ----
    try:
        conn = _get_db_conn()
        # 检查 summary 列是否存在
        cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
        if "summary" in cols:
            tags_to_query = list(wiki_bodies.keys())
            placeholders = ",".join("?" for _ in tags_to_query)
            rows = conn.execute(
                f"SELECT title, summary FROM wiki_pages WHERE title IN ({placeholders}) AND summary IS NOT NULL",
                tags_to_query
            ).fetchall()
            for title, summary in rows:
                if summary and summary.strip():
                    summaries[title] = summary.strip()
            if summaries:
                print(f"[Wiki 摘要] 从数据库缓存命中 {len(summaries)}/{len(wiki_bodies)} 个摘要")
    except Exception as e:
        print(f"[Wiki 摘要] 读取缓存摘要时出错: {e}")

    # ---- 对缓存未命中的标签，调用摘要模型 ----
    uncached = {tag: body for tag, body in wiki_bodies.items() if tag not in summaries}
    if not uncached:
        return summaries

    # 检查摘要模型配置是否完整
    if not SUMMARY_API_KEY or not SUMMARY_BASE_URL or not SUMMARY_MODEL_NAME:
        print("[Wiki 摘要] 摘要模型未配置（API_KEY/BASE_URL/MODEL_NAME 为空），跳过 wiki 总结")
        return summaries

    summary_client = OpenAI(api_key=SUMMARY_API_KEY, base_url=SUMMARY_BASE_URL)

    # 批量构造：将所有标签的 wiki 描述合并到一个请求中，单次 API 调用完成所有摘要
    combined_prompt = (
        "你是一个 Danbooru 标签描述总结专家。我会给你多个 Danbooru 标签的 wiki 描述（DText 格式），"
        "请为每个标签提取并总结其视觉特征描述。\n\n"
        "要求：\n"
        "- 每个标签总结为1-2句话\n"
        "- 只描述该标签在图片中对应的**视觉表现**（外观、颜色、形状、位置等）\n"
        "- 忽略 wiki 中的使用注意事项、相关标签链接、历史说明等非视觉内容\n"
        "- 如果 wiki 描述中没有视觉相关内容，返回\"无具体视觉描述\"\n\n"
    )

    for tag, body in uncached.items():
        # 截断过长的 wiki body，避免超出上下文限制
        truncated_body = body[:2000] if len(body) > 2000 else body
        combined_prompt += f"### {tag}\n{truncated_body}\n\n"

    combined_prompt += (
        "\n请按以下 JSON 格式输出结果，并被包裹在 ```json 和 ``` 之间：\n"
        "```json\n{\n  \"tag_name\": \"视觉特征总结\",\n  ...\n}\n```"
    )

    try:
        response = summary_client.chat.completions.create(
            model=SUMMARY_MODEL_NAME,
            messages=[{"role": "user", "content": combined_prompt}],
            temperature=0.1,
            max_tokens=4000,
        )

        content = response.choices[0].message.content
        if content:
            parsed = _extract_json_from_response(content)
            if isinstance(parsed, dict):
                new_summaries = {k: v for k, v in parsed.items() if isinstance(v, str) and v.strip()}
                print(f"[Wiki 摘要] 摘要模型返回 {len(new_summaries)} 个标签的总结")
                summaries.update(new_summaries)

                # 将新摘要写回数据库缓存
                try:
                    conn = _get_db_conn()
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
                    if "summary" in cols:
                        for tag, summary in new_summaries.items():
                            conn.execute(
                                "UPDATE wiki_pages SET summary = ? WHERE title = ?",
                                (summary.strip(), tag)
                            )
                        conn.commit()
                        print(f"[Wiki 摘要] 已将 {len(new_summaries)} 条新摘要写入数据库缓存")
                except Exception as e:
                    print(f"[Wiki 摘要] 写入缓存时出错: {e}")

    except Exception as e:
        print(f"[Wiki 摘要] 摘要模型调用失败: {e}")
        traceback.print_exc()

    return summaries


def prefetch_wiki_summaries(tags_text):
    """
    预取所有现有标签的 wiki 摘要，用于嵌入 prompt。
    解析 tags → 查本地 DB 确认存在 → 获取 wiki → 调摘要模型 → 返回 wiki 摘要 dict
    """
    if not WIKI_SUMMARY_ENABLED or not DANBOORU_ENABLED:
        return {}

    tags = [t.strip().lower().replace(" ", "_") for t in tags_text.split(",") if t.strip()]
    if not tags:
        return {}

    try:
        conn = _get_db_conn()

        # 批量查询标签是否存在
        placeholders = ",".join("?" for _ in tags)
        rows = conn.execute(
            f"SELECT name FROM tags WHERE name IN ({placeholders})", tags
        ).fetchall()
        found_names = {r[0] for r in rows}

        # 对未找到的标签，检查是否是别名
        not_found_direct = [t for t in tags if t not in found_names]
        alias_resolved = {}
        if not_found_direct:
            ph2 = ",".join("?" for _ in not_found_direct)
            alias_rows = conn.execute(
                f"SELECT antecedent_name, consequent_name FROM tag_aliases WHERE antecedent_name IN ({ph2}) AND status='active'",
                not_found_direct
            ).fetchall()
            for ant, con in alias_rows:
                alias_resolved[ant] = con
                found_names.add(ant)

        found = [t for t in tags if t in found_names]
        if not found:
            return {}

        # 获取 wiki（对别名解析后的标签用 consequent_name 查 wiki）
        wiki_query_tags = [alias_resolved.get(t, t) for t in found]
        wiki_bodies = fetch_local_wiki(wiki_query_tags)

        if not wiki_bodies:
            return {}

        print(f"[Wiki 预取] 获取到 {len(wiki_bodies)}/{len(found)} 个标签的 wiki 页面")
        summaries = summarize_tag_wikis(wiki_bodies)
        print(f"[Wiki 预取] 成功获取 {len(summaries)}/{len(found)} 个标签的摘要")
        return summaries

    except Exception as e:
        print(f"[Wiki 预取] 获取摘要失败: {e}")
        return {}


def encode_image(image_path):
    """
    将图片编码为 base64。
    如果图片超过大小限制，会自动压缩（先降 JPEG 质量，再缩小尺寸），
    不影响本地文件。
    """
    mime_type, _ = mimetypes.guess_type(image_path)

    with open(image_path, "rb") as f:
        raw_data = f.read()

    # 文件已经够小，无需压缩
    if len(raw_data) <= MAX_IMAGE_BYTES:
        return base64.b64encode(raw_data).decode("utf-8"), mime_type or "image/jpeg"

    # 需要压缩：用 PIL 重新编码
    pil_img = Image.open(BytesIO(raw_data))
    if pil_img.mode in ("RGBA", "P"):
        pil_img = pil_img.convert("RGB")

    # 阶段1：逐步降低 JPEG 质量
    for quality in [95, 90, 85, 80, 70, 60]:
        buf = BytesIO()
        pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            compressed = buf.getvalue()
            return base64.b64encode(compressed).decode("utf-8"), "image/jpeg"

    # 阶段2：质量固定60，逐步缩小尺寸
    scale = 0.9
    while scale >= 0.3:
        new_w = max(1, int(pil_img.width * scale))
        new_h = max(1, int(pil_img.height * scale))
        resized = pil_img.resize((new_w, new_h), Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format="JPEG", quality=60, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            compressed = buf.getvalue()
            return base64.b64encode(compressed).decode("utf-8"), "image/jpeg"
        scale -= 0.1

    # 最终兜底：最小尺寸
    compressed = buf.getvalue()
    return base64.b64encode(compressed).decode("utf-8"), "image/jpeg"


def _rate_limit_wait():
    """全局速率限制等待"""
    if ENABLE_RATE_LIMIT:
        global _last_request_time
        with _request_lock:
            current_time = time.time()
            elapsed = current_time - _last_request_time
            if elapsed < RATE_LIMIT_SECONDS:
                time.sleep(RATE_LIMIT_SECONDS - elapsed)
            _last_request_time = time.time()


def _get_api_kwargs(include_tools=False):
    """构建 API 调用的通用参数"""
    kwargs = dict(
        model=MODEL_NAME,
        temperature=TEMPERATURE,
        top_p=TOP_P,
        max_tokens=10000,
        extra_body={
            "google": {
                "thinking_config": {
                    "thinking_level": "high",
                    "include_thoughts": True
                },
                "safety_settings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
            }
        },
    )
    if include_tools and DANBOORU_ENABLED:
        kwargs["tools"] = DANBOORU_TOOLS
        kwargs["tool_choice"] = "auto"
    return kwargs


def _extract_json_from_response(content):
    """
    从模型响应文本中提取 JSON 字符串并解析。
    返回: dict 或抛出异常
    """
    json_str = content.strip()
    # 尝试从 markdown 代码块中提取 JSON
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', json_str, re.DOTALL)
    if match:
        json_str = match.group(1).strip()
    else:
        # 兜底：寻找大括号
        start = json_str.find('{')
        end = json_str.rfind('}')
        if start != -1 and end != -1:
            json_str = json_str[start:end + 1]

    return json.loads(json_str)


def _parse_remove_result(content):
    """解析删标任务的响应，返回 removed_tags 列表"""
    result = _extract_json_from_response(content)
    removed_tags = result.get("removed_tags", [])
    if not isinstance(removed_tags, list):
        removed_tags = []
    removed_tags = [t.strip() for t in removed_tags if isinstance(t, str) and t.strip()]
    removed_tags = [t.strip().replace("_", " ") for t in removed_tags if t.strip()]
    return removed_tags


def _parse_correct_result(content):
    """解析改标任务的响应，返回 corrected_tags 字典"""
    result = _extract_json_from_response(content)
    corrected_tags = result.get("corrected_tags", {})
    if not isinstance(corrected_tags, dict):
        corrected_tags = {}
    corrected_tags = {
        k.strip(): v.strip()
        for k, v in corrected_tags.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }
    corrected_tags = {
        k.strip().replace("_", " "): v.strip().replace("_", " ")
        for k, v in corrected_tags.items() if k.strip() and v.strip()
    }
    return corrected_tags


def _parse_add_result(content):
    """解析添标任务的响应，返回 added_tags 列表"""
    result = _extract_json_from_response(content)
    added_tags = result.get("added_tags", [])
    if not isinstance(added_tags, list):
        added_tags = []
    added_tags = [t.strip() for t in added_tags if isinstance(t, str) and t.strip()]
    added_tags = [t.strip().replace("_", " ") for t in added_tags if t.strip()]
    return added_tags


def filter_tags(image_path, tags_text, client, task_type="remove"):
    """
    调用 API，将图片和标签一起发送给模型，执行单一任务。
    task_type: "remove" — 删标, "correct" — 改标, "add" — 添标
    支持工具调用：模型可调用 check_danbooru_tags 验证标签有效性（改标和添标时）。
    返回: (result, error_message, wiki_summaries)
        - remove: (removed_tags_list, error, {})
        - correct: (corrected_tags_dict, error, wiki_summaries)
        - add: (added_tags_list, error, wiki_summaries)
    """
    # 根据任务类型选择 prompt、是否启用工具、解析函数
    task_config = {
        "remove": {
            "prompt_template": PROMPT_REMOVE,
            "use_tools": False,  # 删标不产生新标签，无需 Danbooru 验证
            "parser": _parse_remove_result,
            "empty_result": [],
            "label": "删标",
        },
        "correct": {
            "prompt_template": PROMPT_CORRECT,
            "use_tools": True,
            "parser": _parse_correct_result,
            "empty_result": {},
            "label": "改标",
        },
        "add": {
            "prompt_template": PROMPT_ADD,
            "use_tools": True,
            "parser": _parse_add_result,
            "empty_result": [],
            "label": "添标",
        },
    }
    config = task_config[task_type]
    all_wiki_summaries = {}  # 收集所有轮次的 wiki 摘要

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            base64_image, mime_type = encode_image(image_path)
            prompt = config["prompt_template"].format(tags=tags_text)

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            },
                        },
                    ],
                }
            ]

            # ========== 第一轮：发送请求 ==========
            include_tools = config["use_tools"]
            api_kwargs = _get_api_kwargs(include_tools=include_tools)
            _rate_limit_wait()

            print(f"[{config['label']}] 发送 API 请求...")
            response = client.chat.completions.create(messages=messages, **api_kwargs)
            print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))

            choice = response.choices[0]

            # ========== 检查模型是否发起了工具调用 ==========
            if choice.message.tool_calls and DANBOORU_ENABLED and include_tools:
                tool_call = choice.message.tool_calls[0]

                if tool_call.function.name == "check_danbooru_tags":
                    args = json.loads(tool_call.function.arguments)
                    tags_to_check = args.get("tags", [])

                    print(f"[Danbooru 验证/{config['label']}] 模型请求验证 {len(tags_to_check)} 个标签: {tags_to_check}")

                    # 执行 Danbooru 查询
                    danbooru_result = check_danbooru_tags(tags_to_check)
                    print(f"[Danbooru 验证/{config['label']}] 结果: 存在={danbooru_result['found']}, 不存在={danbooru_result['not_found']}")

                    # ========== 第二轮：伪造历史对话传回工具结果 ==========
                    assistant_text = f"我需要验证以下标签是否为有效的 Danbooru 标签: {', '.join(tags_to_check)}"
                    if choice.message.content:
                        assistant_text = choice.message.content + "\n" + assistant_text
                    messages.append({"role": "assistant", "content": assistant_text})

                    result_text = json.dumps(danbooru_result, ensure_ascii=False)

                    # 如果有 wiki 摘要，构造额外的视觉描述信息
                    wiki_section = ""
                    wiki_summaries = danbooru_result.get("wiki_summaries", {})
                    all_wiki_summaries.update(wiki_summaries)
                    if wiki_summaries:
                        wiki_lines = []
                        for tag_name, summary in wiki_summaries.items():
                            wiki_lines.append(f"  - {tag_name}: {summary}")
                        wiki_section = (
                            "\n\n以下是已验证标签在 Danbooru 中的视觉含义描述：\n"
                            + "\n".join(wiki_lines)
                            + "\n\n请结合以上视觉描述，检查你建议的标签是否真的与图片内容匹配。"
                            "如果某个标签的 Danbooru 定义描述的视觉特征与图片不符，请移除或替换该标签。"
                        )

                    messages.append({
                        "role": "user",
                        "content": (
                            f"Danbooru 标签验证结果如下：\n{result_text}\n\n"
                            f"请根据验证结果调整你的标签审核结论：\n"
                            f"- not_found 中的标签是无效的，不能使用\n"
                            f"- 你可以尝试用相近的有效标签替代无效标签，或直接移除\n"
                            f"{wiki_section}\n"
                            f"- 请输出最终的 JSON 结果（格式不变）"
                        )
                    })

                    api_kwargs_round2 = _get_api_kwargs(include_tools=False)
                    _rate_limit_wait()

                    response = client.chat.completions.create(messages=messages, **api_kwargs_round2)
                    print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))
                    choice = response.choices[0]

            # ========== 解析最终响应 ==========
            content = choice.message.content
            if not content or not content.strip():
                finish_reason = choice.finish_reason
                if finish_reason == "content_filter":
                    return config["empty_result"], "触发安全审查，无法处理", all_wiki_summaries
                continue

            parsed_result = config["parser"](content)

            # ========== 后置验证 + 自动修正：对模型最终输出的新标签做 Danbooru 验证 ==========
            if DANBOORU_ENABLED and DANBOORU_AUTOCORRECT and config["use_tools"]:
                tags_to_validate = []
                if task_type == "add" and isinstance(parsed_result, list):
                    tags_to_validate = [t.replace(" ", "_") for t in parsed_result if t.strip()]
                elif task_type == "correct" and isinstance(parsed_result, dict):
                    tags_to_validate = [v.replace(" ", "_") for v in parsed_result.values() if v.strip()]

                if tags_to_validate:
                    print(f"[后置验证/{config['label']}] 验证模型最终输出的 {len(tags_to_validate)} 个标签...")
                    post_check = check_danbooru_tags(tags_to_validate)
                    all_wiki_summaries.update(post_check.get("wiki_summaries", {}))
                    invalid_tags = post_check.get("not_found", [])

                    if invalid_tags:
                        print(f"[后置验证/{config['label']}] 发现 {len(invalid_tags)} 个无效标签: {invalid_tags}，尝试自动修正...")
                        corrections = autocorrect_invalid_tags(invalid_tags)

                        if task_type == "add":
                            new_result = []
                            for tag in parsed_result:
                                tag_normalized = tag.replace(" ", "_").lower()
                                if tag_normalized in invalid_tags:
                                    if tag_normalized in corrections:
                                        replacement = corrections[tag_normalized].replace("_", " ")
                                        print(f"[后置修正/添标] '{tag}' → '{replacement}'")
                                        new_result.append(replacement)
                                    else:
                                        print(f"[后置修正/添标] '{tag}' 无法修正，已丢弃")
                                else:
                                    new_result.append(tag)
                            parsed_result = new_result

                        elif task_type == "correct":
                            new_result = {}
                            for orig_tag, new_tag in parsed_result.items():
                                new_tag_normalized = new_tag.replace(" ", "_").lower()
                                if new_tag_normalized in invalid_tags:
                                    if new_tag_normalized in corrections:
                                        replacement = corrections[new_tag_normalized].replace("_", " ")
                                        print(f"[后置修正/改标] '{orig_tag}' → '{new_tag}' 修正为 → '{replacement}'")
                                        new_result[orig_tag] = replacement
                                    else:
                                        print(f"[后置修正/改标] '{new_tag}' 无法修正，丢弃该改标项")
                                else:
                                    new_result[orig_tag] = new_tag
                            parsed_result = new_result
                    else:
                        print(f"[后置验证/{config['label']}] 所有标签均有效")

            return parsed_result, None, all_wiki_summaries

        except json.JSONDecodeError as e:
            print(f"[{config['label']}/尝试 {attempt}/{MAX_RETRIES}] API返回格式异常，正在重试...")
            if attempt == MAX_RETRIES:
                return config["empty_result"], f"JSON 解析失败: {str(e)}\n(模型返回异常或被截断，建议稍后重试)", all_wiki_summaries
        except Exception as e:
            print(f"[{config['label']}/尝试 {attempt}/{MAX_RETRIES}] 错误: {type(e).__name__}: {e}")
            traceback.print_exc()
            if attempt == MAX_RETRIES:
                return config["empty_result"], f"API 调用失败: {str(e)}", all_wiki_summaries

        time.sleep(RETRY_DELAY)

    return config["empty_result"], f"在 {MAX_RETRIES} 次尝试后仍未成功", all_wiki_summaries


# ================= 任务规则模板（用于统一审查 prompt 动态拼接） =================

_REVIEW_RULES_REMOVE = """\
### 删标规则
按以下框架判断每个标签：
1. **判断裁切范围** → 确定画面展示的是全身/上半身/面部特写，决定哪些不可见是正常的
2. **逐标签判断**：
   - 元素所在区域清晰可见但元素不存在 → 删除
   - 元素可能被裁切/遮挡而不可见 → 保留
   - 构图/氛围标签（solo, upper_body 等）→ 根据画面实际情况判断
   - 两个标签互相矛盾 → 删除错误的那个

典型应删除：翅膀/尾巴在完全可见的背部不存在、人数标签与画面不符、构图标签与实际构图矛盾、全身可见时不存在的服装配件
典型不应删除：上半身图中的下半身服装标签、被遮挡物体的标签、颜色存疑但元素存在的标签
禁止删除：角色名、画师名、风格描述标签（这些不在审查范围内）"""

_REVIEW_RULES_CORRECT = """\
### 改标规则
修正元素存在但属性描述有误的标签，重点检查：
- **颜色**：发色/瞳色/服装颜色与画面明显不同（排除光照因素）→ 修正为正确颜色
- **长度/尺度**：发长/裙长/袖长分类明显错误（如过腰长发标为 short_hair）→ 修正
- **类型混淆**：物品类型不对（如裙子标为裤子, ponytail 标为 twintails）→ 修正
- **数量**：人数标签与画面不符 → 修正
- 颜色差异较小或在两个分类边界上 → 不修正
- 光照/滤镜导致无法确定真实颜色 → 不修正
禁止修正：角色名、画师名、风格描述标签（这些不在审查范围内）"""

_REVIEW_RULES_ADD = """\
### 添标规则
按优先级补充遗漏的明显特征：
1. **基础人物特征**（缺一必补）：人数(1girl/1boy等)、发色、瞳色、发长、明显发型
2. **显著服装与配饰**：画面中清晰可见但未标记的主要衣物和显眼配饰
3. **构图与姿势**：视角(from_above等)、构图(upper_body等)、姿势(standing/sitting等)、朝向(looking_at_viewer等)
4. **显著画面元素**：明显的背景类型、占据显著位置的物品、明显的表情

禁止添加：风格标签(anime_coloring等)、画师名、角色名、版权名
控制数量：通常 0-5 个，只补充完全未被现有标签覆盖的特征
不确定的特征不要添加"""


def review_tags(image_path, tags_text, client, wiki_summaries):
    """
    统一审查标签：一次 API 调用完成删/改/加三个任务。
    参数:
        image_path — 图片路径
        tags_text — 逗号分隔的标签文本
        client — OpenAI client
        wiki_summaries — 预取的 wiki 摘要 dict
    返回: (removed_tags, corrected_tags, added_tags, error, wiki_summaries)
    """
    all_wiki_summaries = dict(wiki_summaries)

    # ---- 构建 wiki 上下文 ----
    if wiki_summaries:
        wiki_lines = [f"  - {tag}: {summary}" for tag, summary in wiki_summaries.items()]
        wiki_context = (
            "## 标签 Wiki 视觉描述（来自 Danbooru）\n"
            "以下是当前标签的视觉含义描述，请结合这些描述来判断标签是否正确：\n"
            + "\n".join(wiki_lines)
        )
    else:
        wiki_context = ""

    # ---- 根据开关动态拼接任务规则 ----
    task_rules_parts = []
    if REMOVE_TAGS_ENABLED:
        task_rules_parts.append(_REVIEW_RULES_REMOVE)
    if CORRECT_TAGS_ENABLED:
        task_rules_parts.append(_REVIEW_RULES_CORRECT)
    task_rules_parts.append(_REVIEW_RULES_ADD)  # 添标始终启用
    task_rules = "\n\n".join(task_rules_parts)

    # ---- 禁用字段说明 ----
    disabled_parts = []
    if not REMOVE_TAGS_ENABLED:
        disabled_parts.append('- "removed_tags" 必须为空数组 []（删标功能已关闭）')
    if not CORRECT_TAGS_ENABLED:
        disabled_parts.append('- "corrected_tags" 必须为空对象 {}（改标功能已关闭）')
    disabled_fields_note = "\n".join(disabled_parts)

    # ---- 工具调用说明 ----
    tool_instructions = ""
    if DANBOORU_ENABLED:
        tool_instructions = (
            "\n**重要：标签验证步骤**\n"
            "在给出最终 JSON 结果之前，你**必须**先调用 `check_danbooru_tags` 工具，"
            "将你打算**修正后的标签(corrected_tags 的值)和新增的标签(added_tags)**合并发送给工具进行验证。\n"
            "- 只有经过验证确认存在于 Danbooru 数据库中的标签，才能出现在最终结果中。\n"
            "- 如果工具返回某个标签不存在(not_found)，你必须从结果中移除该项，或尝试用一个相近的有效标签替代。\n"
            "- 如果你没有新标签要验证（即 corrected_tags 和 added_tags 都为空），则无需调用工具，直接输出 JSON 结果即可。"
        )

    # ---- 格式化 prompt ----
    prompt = PROMPT_REVIEW.format(
        wiki_context=wiki_context,
        task_rules=task_rules,
        tags=tags_text,
        disabled_fields_note=disabled_fields_note,
        tool_instructions=tool_instructions,
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            base64_image, mime_type = encode_image(image_path)

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            },
                        },
                    ],
                }
            ]

            # ========== 发送请求 ==========
            include_tools = DANBOORU_ENABLED
            api_kwargs = _get_api_kwargs(include_tools=include_tools)
            _rate_limit_wait()

            print(f"[统一审查] 发送 API 请求...")
            response = client.chat.completions.create(messages=messages, **api_kwargs)
            print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))

            choice = response.choices[0]

            # ========== 处理工具调用 ==========
            if choice.message.tool_calls and DANBOORU_ENABLED and include_tools:
                tool_call = choice.message.tool_calls[0]

                if tool_call.function.name == "check_danbooru_tags":
                    args = json.loads(tool_call.function.arguments)
                    tags_to_check = args.get("tags", [])

                    print(f"[Danbooru 验证/统一审查] 模型请求验证 {len(tags_to_check)} 个标签: {tags_to_check}")

                    danbooru_result = check_danbooru_tags(tags_to_check)
                    print(f"[Danbooru 验证/统一审查] 结果: 存在={danbooru_result['found']}, 不存在={danbooru_result['not_found']}")

                    # 第二轮：传回工具结果
                    assistant_text = f"我需要验证以下标签是否为有效的 Danbooru 标签: {', '.join(tags_to_check)}"
                    if choice.message.content:
                        assistant_text = choice.message.content + "\n" + assistant_text
                    messages.append({"role": "assistant", "content": assistant_text})

                    result_text = json.dumps(danbooru_result, ensure_ascii=False)

                    wiki_section = ""
                    wiki_summaries_from_tool = danbooru_result.get("wiki_summaries", {})
                    all_wiki_summaries.update(wiki_summaries_from_tool)
                    if wiki_summaries_from_tool:
                        wiki_lines = [f"  - {tag_name}: {summary}" for tag_name, summary in wiki_summaries_from_tool.items()]
                        wiki_section = (
                            "\n\n以下是已验证标签在 Danbooru 中的视觉含义描述：\n"
                            + "\n".join(wiki_lines)
                            + "\n\n请结合以上视觉描述，检查你建议的标签是否真的与图片内容匹配。"
                            "如果某个标签的 Danbooru 定义描述的视觉特征与图片不符，请移除或替换该标签。"
                        )

                    messages.append({
                        "role": "user",
                        "content": (
                            f"Danbooru 标签验证结果如下：\n{result_text}\n\n"
                            f"请根据验证结果调整你的标签审核结论：\n"
                            f"- not_found 中的标签是无效的，不能使用\n"
                            f"- 你可以尝试用相近的有效标签替代无效标签，或直接移除\n"
                            f"{wiki_section}\n"
                            f"- 请输出最终的 JSON 结果（格式不变）"
                        )
                    })

                    api_kwargs_round2 = _get_api_kwargs(include_tools=False)
                    _rate_limit_wait()

                    response = client.chat.completions.create(messages=messages, **api_kwargs_round2)
                    print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))
                    choice = response.choices[0]

            # ========== 解析统一 JSON 响应 ==========
            content = choice.message.content
            if not content or not content.strip():
                finish_reason = choice.finish_reason
                if finish_reason == "content_filter":
                    return [], {}, [], "触发安全审查，无法处理", all_wiki_summaries
                continue

            result = _extract_json_from_response(content)

            # 解析 removed_tags
            removed_tags = result.get("removed_tags", [])
            if not isinstance(removed_tags, list):
                removed_tags = []
            removed_tags = [t.strip().replace("_", " ") for t in removed_tags if isinstance(t, str) and t.strip()]

            # 解析 corrected_tags
            corrected_tags = result.get("corrected_tags", {})
            if not isinstance(corrected_tags, dict):
                corrected_tags = {}
            corrected_tags = {
                k.strip().replace("_", " "): v.strip().replace("_", " ")
                for k, v in corrected_tags.items()
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
            }

            # 解析 added_tags
            added_tags = result.get("added_tags", [])
            if not isinstance(added_tags, list):
                added_tags = []
            added_tags = [t.strip().replace("_", " ") for t in added_tags if isinstance(t, str) and t.strip()]

            # 强制禁用的字段为空
            if not REMOVE_TAGS_ENABLED:
                removed_tags = []
            if not CORRECT_TAGS_ENABLED:
                corrected_tags = {}

            # ========== 后置验证 + 自动修正 ==========
            if DANBOORU_ENABLED and DANBOORU_AUTOCORRECT:
                tags_to_validate = []
                tags_to_validate += [t.replace(" ", "_") for t in added_tags if t.strip()]
                tags_to_validate += [v.replace(" ", "_") for v in corrected_tags.values() if v.strip()]

                if tags_to_validate:
                    print(f"[后置验证/统一审查] 验证模型最终输出的 {len(tags_to_validate)} 个标签...")
                    post_check = check_danbooru_tags(tags_to_validate)
                    all_wiki_summaries.update(post_check.get("wiki_summaries", {}))
                    invalid_tags = post_check.get("not_found", [])

                    if invalid_tags:
                        print(f"[后置验证/统一审查] 发现 {len(invalid_tags)} 个无效标签: {invalid_tags}，尝试自动修正...")
                        corrections = autocorrect_invalid_tags(invalid_tags)

                        # 修正 added_tags
                        new_added = []
                        for tag in added_tags:
                            tag_normalized = tag.replace(" ", "_").lower()
                            if tag_normalized in invalid_tags:
                                if tag_normalized in corrections:
                                    replacement = corrections[tag_normalized].replace("_", " ")
                                    print(f"[后置修正/添标] '{tag}' → '{replacement}'")
                                    new_added.append(replacement)
                                else:
                                    print(f"[后置修正/添标] '{tag}' 无法修正，已丢弃")
                            else:
                                new_added.append(tag)
                        added_tags = new_added

                        # 修正 corrected_tags
                        new_corrected = {}
                        for orig_tag, new_tag in corrected_tags.items():
                            new_tag_normalized = new_tag.replace(" ", "_").lower()
                            if new_tag_normalized in invalid_tags:
                                if new_tag_normalized in corrections:
                                    replacement = corrections[new_tag_normalized].replace("_", " ")
                                    print(f"[后置修正/改标] '{orig_tag}' → '{new_tag}' 修正为 → '{replacement}'")
                                    new_corrected[orig_tag] = replacement
                                else:
                                    print(f"[后置修正/改标] '{new_tag}' 无法修正，丢弃该改标项")
                            else:
                                new_corrected[orig_tag] = new_tag
                        corrected_tags = new_corrected
                    else:
                        print(f"[后置验证/统一审查] 所有标签均有效")

            return removed_tags, corrected_tags, added_tags, None, all_wiki_summaries

        except json.JSONDecodeError as e:
            print(f"[统一审查/尝试 {attempt}/{MAX_RETRIES}] API返回格式异常，正在重试...")
            if attempt == MAX_RETRIES:
                return [], {}, [], f"JSON 解析失败: {str(e)}\n(模型返回异常或被截断，建议稍后重试)", all_wiki_summaries
        except Exception as e:
            print(f"[统一审查/尝试 {attempt}/{MAX_RETRIES}] 错误: {type(e).__name__}: {e}")
            traceback.print_exc()
            if attempt == MAX_RETRIES:
                return [], {}, [], f"API 调用失败: {str(e)}", all_wiki_summaries

        time.sleep(RETRY_DELAY)

    return [], {}, [], f"在 {MAX_RETRIES} 次尝试后仍未成功", all_wiki_summaries


def prefetch_worker(index, image_path, txt_path, client):
    """
    后台预取线程的工作函数。
    读取标签 → 预取 wiki 摘要 → 一次 API 统一审查（删标、改标、添标）→ 返回结果字典。
    """
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            tags_text = f.read().strip()

        if not tags_text:
            return {
                "index": index,
                "image_path": image_path,
                "txt_path": txt_path,
                "tags_text": "",
                "remove_result": {"removed_tags": [], "error": None},
                "correct_result": {"corrected_tags": {}, "error": None},
                "add_result": {"added_tags": [], "error": None},
                "wiki_summaries": {},
                "empty": True,
            }

        # 第1步：预取所有现有标签的 wiki 摘要
        wiki_summaries = prefetch_wiki_summaries(tags_text)

        # 第2步：一次 API 调用完成统一审查
        removed_tags, corrected_tags, added_tags, error, merged_wiki = review_tags(
            image_path, tags_text, client, wiki_summaries
        )

        return {
            "index": index,
            "image_path": image_path,
            "txt_path": txt_path,
            "tags_text": tags_text,
            "remove_result": {"removed_tags": removed_tags, "error": error},
            "correct_result": {"corrected_tags": corrected_tags, "error": error},
            "add_result": {"added_tags": added_tags, "error": error},
            "wiki_summaries": merged_wiki,
            "empty": False,
        }
    except Exception as e:
        return {
            "index": index,
            "image_path": image_path,
            "txt_path": txt_path,
            "tags_text": "",
            "remove_result": {"removed_tags": [], "error": str(e)},
            "correct_result": {"corrected_tags": {}, "error": str(e)},
            "add_result": {"added_tags": [], "error": str(e)},
            "wiki_summaries": {},
            "empty": False,
        }


class TagReviewApp:
    """逐张审核标签的 GUI 应用，后台并发预取 API 结果"""

    def __init__(self, pairs, client):
        self.pairs = pairs
        self.client = client
        self.current_index = 0
        self.stats = {"confirmed": 0, "skipped": 0, "failed": 0}
        self.current_error = False

        # ---- 后台预取 ----
        self._futures: dict[int, Future] = {}  # index -> Future
        self._executor = ThreadPoolExecutor(max_workers=PREFETCH_WORKERS)
        self._stop_event = threading.Event()

        self.root = tk.Tk()
        self.root.title("标签过滤审核工具")
        self.root.geometry("1200x800")
        self.root.configure(bg="#1e1e2e")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 样式
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#1e1e2e")
        style.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4",
                         font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background="#1e1e2e", foreground="#89b4fa",
                         font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Status.TLabel", background="#313244", foreground="#a6adc8",
                         font=("Microsoft YaHei UI", 10))
        style.configure("Queue.TLabel", background="#1e1e2e", foreground="#f9e2af",
                         font=("Microsoft YaHei UI", 10))

        self._build_ui()
        # 启动所有预取任务
        self._submit_all_prefetch()
        # 显示第一张
        self._process_current()

    def _record_processed(self, filepath):
        """记录已处理的文件"""
        log_path = os.path.join(os.path.dirname(filepath), "process.log")
        filename = os.path.basename(filepath)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{filename}\n")
        except Exception as e:
            print(f"写入 process.log 失败: {e}")

    # ==================== 预取逻辑 ====================

    def _submit_all_prefetch(self):
        """一次性提交所有图片的预取任务"""
        for i, (img_path, txt_path) in enumerate(self.pairs):
            future = self._executor.submit(
                prefetch_worker, i, img_path, txt_path, self.client
            )
            self._futures[i] = future

    def _get_prefetch_stats(self):
        """返回 (已完成数, 总数)"""
        done = sum(1 for f in self._futures.values() if f.done())
        return done, len(self._futures)

    # ==================== UI 构建 ====================

    def _build_ui(self):
        # ---- 顶部状态栏 ----
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        self.progress_label = ttk.Label(top_frame, text="", style="Title.TLabel")
        self.progress_label.pack(side=tk.LEFT)

        self.queue_label = ttk.Label(top_frame, text="", style="Queue.TLabel")
        self.queue_label.pack(side=tk.LEFT, padx=(20, 0))

        self.status_label = ttk.Label(top_frame, text="", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # ---- 主内容区域 ----
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧：图片
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        img_title = ttk.Label(left_frame, text="📷 图片预览", style="Title.TLabel")
        img_title.pack(anchor=tk.W, pady=(0, 5))

        self.image_canvas = tk.Canvas(
            left_frame, bg="#181825", highlightthickness=1, highlightbackground="#45475a"
        )
        self.image_canvas.pack(fill=tk.BOTH, expand=True)
        self.image_canvas.bind("<Configure>", self._on_canvas_resize)
        self._current_pil_image = None
        self._photo_image = None

        # 右侧：标签区域（使用滚动框架）
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # -- 原始标签（只读） --
        orig_title = ttk.Label(
            right_frame, text="📋 原始标签", style="Title.TLabel"
        )
        orig_title.pack(anchor=tk.W, pady=(0, 3))

        self.orig_text = tk.Text(
            right_frame, wrap=tk.WORD, height=5, font=("Consolas", 10),
            bg="#181825", fg="#cdd6f4", insertbackground="#cdd6f4",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
            state=tk.DISABLED,
        )
        self.orig_text.pack(fill=tk.X, pady=(0, 6))

        # -- 🗑️ 删标建议 --
        remove_header = ttk.Frame(right_frame)
        remove_header.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(remove_header, text="🗑️ 删标建议", style="Title.TLabel").pack(side=tk.LEFT)
        self.remove_var = tk.BooleanVar(value=True)
        self.remove_check = tk.Checkbutton(
            remove_header, text="接受", variable=self.remove_var,
            command=self._recalculate_filtered,
            font=("Microsoft YaHei UI", 9), bg="#1e1e2e", fg="#a6e3a1",
            selectcolor="#313244", activebackground="#1e1e2e", activeforeground="#a6e3a1",
        )
        self.remove_check.pack(side=tk.RIGHT)

        self.remove_text = tk.Text(
            right_frame, wrap=tk.WORD, height=3, font=("Consolas", 10),
            bg="#181825", fg="#f38ba8", insertbackground="#cdd6f4",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
            state=tk.DISABLED,
        )
        self.remove_text.pack(fill=tk.X, pady=(0, 6))

        # -- 🔄 改标建议 --
        correct_header = ttk.Frame(right_frame)
        correct_header.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(correct_header, text="🔄 改标建议", style="Title.TLabel").pack(side=tk.LEFT)
        self.correct_var = tk.BooleanVar(value=True)
        self.correct_check = tk.Checkbutton(
            correct_header, text="接受", variable=self.correct_var,
            command=self._recalculate_filtered,
            font=("Microsoft YaHei UI", 9), bg="#1e1e2e", fg="#a6e3a1",
            selectcolor="#313244", activebackground="#1e1e2e", activeforeground="#a6e3a1",
        )
        self.correct_check.pack(side=tk.RIGHT)

        self.correct_text = tk.Text(
            right_frame, wrap=tk.WORD, height=3, font=("Consolas", 10),
            bg="#181825", fg="#fab387", insertbackground="#cdd6f4",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
            state=tk.DISABLED,
        )
        self.correct_text.pack(fill=tk.X, pady=(0, 6))
        self.correct_text.tag_configure(
            "old_tag", foreground="#fab387", overstrike=True, font=("Consolas", 10, "bold")
        )
        self.correct_text.tag_configure(
            "arrow", foreground="#6c7086", font=("Consolas", 10)
        )
        self.correct_text.tag_configure(
            "new_tag", foreground="#89dceb", font=("Consolas", 10, "bold")
        )

        # -- ➕ 添标建议 --
        add_header = ttk.Frame(right_frame)
        add_header.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(add_header, text="➕ 添标建议", style="Title.TLabel").pack(side=tk.LEFT)
        self.add_var = tk.BooleanVar(value=True)
        self.add_check = tk.Checkbutton(
            add_header, text="接受", variable=self.add_var,
            command=self._recalculate_filtered,
            font=("Microsoft YaHei UI", 9), bg="#1e1e2e", fg="#a6e3a1",
            selectcolor="#313244", activebackground="#1e1e2e", activeforeground="#a6e3a1",
        )
        self.add_check.pack(side=tk.RIGHT)

        self.add_text = tk.Text(
            right_frame, wrap=tk.WORD, height=3, font=("Consolas", 10),
            bg="#181825", fg="#f9e2af", insertbackground="#cdd6f4",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
            state=tk.DISABLED,
        )
        self.add_text.pack(fill=tk.X, pady=(0, 6))

        # -- 📖 Wiki 摘要（只读，可折叠） --
        wiki_header = ttk.Frame(right_frame)
        wiki_header.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(wiki_header, text="📖 Wiki 摘要", style="Title.TLabel").pack(side=tk.LEFT)
        self._wiki_visible = tk.BooleanVar(value=False)
        self.wiki_toggle_btn = tk.Button(
            wiki_header, text="展开", command=self._toggle_wiki,
            font=("Microsoft YaHei UI", 9), bg="#313244", fg="#cdd6f4",
            activebackground="#45475a", activeforeground="#cdd6f4",
            relief=tk.FLAT, padx=8, cursor="hand2",
        )
        self.wiki_toggle_btn.pack(side=tk.RIGHT)

        self.wiki_text = tk.Text(
            right_frame, wrap=tk.WORD, height=6, font=("Consolas", 9),
            bg="#181825", fg="#b4befe", insertbackground="#cdd6f4",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
            state=tk.DISABLED,
        )
        # 默认隐藏
        self.wiki_text.tag_configure(
            "wiki_tag", foreground="#f9e2af", font=("Consolas", 9, "bold")
        )
        self.wiki_text.tag_configure(
            "wiki_sep", foreground="#6c7086", font=("Consolas", 9)
        )

        # -- ✅ 最终结果（可编辑） --
        self._filtered_title_widget = ttk.Label(
            right_frame, text="✅ 最终结果（可手动编辑）", style="Title.TLabel"
        )
        self._filtered_title_widget.pack(anchor=tk.W, pady=(0, 3))

        self.filtered_text = tk.Text(
            right_frame, wrap=tk.WORD, height=5, font=("Consolas", 10),
            bg="#181825", fg="#a6e3a1", insertbackground="#a6e3a1",
            selectbackground="#45475a", relief=tk.FLAT, padx=8, pady=6,
        )
        self.filtered_text.pack(fill=tk.BOTH, expand=True)

        # ---- 底部按钮区域 ----
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        self.confirm_btn = tk.Button(
            btn_frame, text="✔ 确认覆盖", command=self._on_confirm,
            font=("Microsoft YaHei UI", 12, "bold"), bg="#a6e3a1", fg="#1e1e2e",
            activebackground="#94e2d5", activeforeground="#1e1e2e",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2",
        )
        self.confirm_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.skip_btn = tk.Button(
            btn_frame, text="⏭ 跳过", command=self._on_skip,
            font=("Microsoft YaHei UI", 12), bg="#45475a", fg="#cdd6f4",
            activebackground="#585b70", activeforeground="#cdd6f4",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2",
        )
        self.skip_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.abort_btn = tk.Button(
            btn_frame, text="⛔ 中止", command=self._on_abort,
            font=("Microsoft YaHei UI", 12), bg="#f38ba8", fg="#1e1e2e",
            activebackground="#eba0ac", activeforeground="#1e1e2e",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2",
        )
        self.abort_btn.pack(side=tk.RIGHT)

    # ==================== Wiki 折叠 ====================

    def _toggle_wiki(self):
        if self._wiki_visible.get():
            self.wiki_text.pack_forget()
            self._wiki_visible.set(False)
            self.wiki_toggle_btn.config(text="展开")
        else:
            # 在添标区域后、最终结果区域前插入
            self.wiki_text.pack(fill=tk.X, pady=(0, 6), before=self._filtered_title_widget)
            self._wiki_visible.set(True)
            self.wiki_toggle_btn.config(text="收起")

    # ==================== 图片显示 ====================

    def _on_canvas_resize(self, event=None):
        if self._current_pil_image:
            self._display_image(self._current_pil_image)

    def _display_image(self, pil_image):
        self._current_pil_image = pil_image
        canvas_w = self.image_canvas.winfo_width()
        canvas_h = self.image_canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return

        img_w, img_h = pil_image.size
        ratio = min(canvas_w / img_w, canvas_h / img_h)
        new_w = max(1, int(img_w * ratio))
        new_h = max(1, int(img_h * ratio))

        resized = pil_image.resize((new_w, new_h), Image.LANCZOS)
        self._photo_image = ImageTk.PhotoImage(resized)

        self.image_canvas.delete("all")
        x = canvas_w // 2
        y = canvas_h // 2
        self.image_canvas.create_image(x, y, anchor=tk.CENTER, image=self._photo_image)

    # ==================== 更新预取队列状态 ====================

    def _update_queue_label(self):
        done, total = self._get_prefetch_stats()
        self.queue_label.config(text=f"🚀 预取进度: {done}/{total}")

    # ==================== 核心处理流程 ====================

    def _process_current(self):
        """处理当前索引的图片"""
        if self.current_index >= len(self.pairs):
            self._finish()
            return

        image_path, txt_path = self.pairs[self.current_index]
        total = len(self.pairs)
        idx = self.current_index + 1
        filename = os.path.basename(image_path)

        self.progress_label.config(text=f"[{idx}/{total}] {filename}")
        self._update_queue_label()
        self._set_buttons_state(tk.DISABLED)

        # 重置当前结果缓存
        self._current_original_tags = []
        self._current_removed_tags = []
        self._current_corrected_tags = {}
        self._current_added_tags = []

        # 清空所有文本框
        for text_widget in [self.orig_text, self.remove_text, self.correct_text, self.add_text]:
            text_widget.config(state=tk.NORMAL)
            text_widget.delete("1.0", tk.END)
            text_widget.config(state=tk.DISABLED)
        self.filtered_text.delete("1.0", tk.END)

        # 重置复选框状态
        self.remove_var.set(True)
        self.correct_var.set(True)
        self.add_var.set(True)

        # 显示图片
        try:
            pil_img = Image.open(image_path)
            self.root.update_idletasks()
            self._display_image(pil_img)
        except Exception as e:
            self.image_canvas.delete("all")
            self.image_canvas.create_text(
                self.image_canvas.winfo_width() // 2,
                self.image_canvas.winfo_height() // 2,
                text=f"无法加载图片:\n{e}", fill="#f38ba8", font=("Consolas", 12),
            )

        # 检查预取结果是否已就绪
        future = self._futures.get(self.current_index)
        if future and future.done():
            # 结果已有，直接使用
            self._apply_result(future.result())
        else:
            # 等待结果，轮询检查
            self.status_label.config(text="⏳ 等待 AI 分析结果...")
            self._poll_result()

    def _poll_result(self):
        """定时轮询当前图片的预取结果"""
        if self._stop_event.is_set():
            return
        idx = self.current_index
        future = self._futures.get(idx)
        self._update_queue_label()
        if future and future.done():
            self._apply_result(future.result())
        else:
            self.root.after(200, self._poll_result)

    def _apply_result(self, result):
        """将预取结果应用到 GUI 上（三个独立操作区域）"""
        self._update_queue_label()
        self.current_error = False

        if result.get("empty"):
            self.status_label.config(text="⚠️ 标签文件为空，自动跳过")
            self._record_processed(result["image_path"])
            self.stats["skipped"] += 1
            self.current_index += 1
            self.root.after(500, self._process_current)
            return

        tags_text = result["tags_text"]
        remove_result = result["remove_result"]
        correct_result = result["correct_result"]
        add_result = result["add_result"]

        # 解析原始标签
        original_tags = [t.strip() for t in tags_text.split(",") if t.strip()]
        self._current_original_tags = original_tags

        # 提取三次调用的结果
        removed_tags = remove_result.get("removed_tags", [])
        corrected_tags = correct_result.get("corrected_tags", {})
        added_tags = add_result.get("added_tags", [])
        remove_error = remove_result.get("error")
        correct_error = correct_result.get("error")
        add_error = add_result.get("error")

        # 缓存当前结果（用于复选框重算）
        self._current_removed_tags = removed_tags
        self._current_corrected_tags = corrected_tags
        self._current_added_tags = added_tags

        # 检查是否全部失败
        all_failed = remove_error and correct_error and add_error
        if all_failed:
            self.current_error = True
            filename = os.path.basename(result["image_path"])
            print(f"[{filename}] 全部请求失败")
            self.status_label.config(text=f"❌ 三次请求均失败")
            self.stats["failed"] += 1

        # -- 显示原始标签 --
        self.orig_text.config(state=tk.NORMAL)
        self.orig_text.delete("1.0", tk.END)
        self.orig_text.insert(tk.END, ", ".join(original_tags))
        self.orig_text.config(state=tk.DISABLED)

        # -- 显示删标建议 --
        self.remove_text.config(state=tk.NORMAL)
        self.remove_text.delete("1.0", tk.END)
        if remove_error:
            self.remove_text.insert(tk.END, f"❌ {remove_error}")
            self.remove_var.set(False)  # 失败时默认不勾选
        elif removed_tags:
            self.remove_text.insert(tk.END, ", ".join(removed_tags))
        else:
            self.remove_text.insert(tk.END, "（无需删除）")
        self.remove_text.config(state=tk.DISABLED)

        # -- 显示改标建议 --
        self.correct_text.config(state=tk.NORMAL)
        self.correct_text.delete("1.0", tk.END)
        if correct_error:
            self.correct_text.insert(tk.END, f"❌ {correct_error}")
            self.correct_var.set(False)  # 失败时默认不勾选
        elif corrected_tags:
            for i, (old_tag, new_tag) in enumerate(corrected_tags.items()):
                if i > 0:
                    self.correct_text.insert(tk.END, ", ")
                self.correct_text.insert(tk.END, old_tag, "old_tag")
                self.correct_text.insert(tk.END, " → ", "arrow")
                self.correct_text.insert(tk.END, new_tag, "new_tag")
        else:
            self.correct_text.insert(tk.END, "（无需修正）")
        self.correct_text.config(state=tk.DISABLED)

        # -- 显示添标建议 --
        self.add_text.config(state=tk.NORMAL)
        self.add_text.delete("1.0", tk.END)
        if add_error:
            self.add_text.insert(tk.END, f"❌ {add_error}")
            self.add_var.set(False)  # 失败时默认不勾选
        elif added_tags:
            self.add_text.insert(tk.END, ", ".join(added_tags))
        else:
            self.add_text.insert(tk.END, "（无需添加）")
        self.add_text.config(state=tk.DISABLED)

        # -- 填充 Wiki 摘要 --
        wiki_summaries = result.get("wiki_summaries", {})
        self.wiki_text.config(state=tk.NORMAL)
        self.wiki_text.delete("1.0", tk.END)
        if wiki_summaries:
            for i, (tag_name, summary) in enumerate(wiki_summaries.items()):
                if i > 0:
                    self.wiki_text.insert(tk.END, "\n")
                self.wiki_text.insert(tk.END, f"{tag_name}", "wiki_tag")
                self.wiki_text.insert(tk.END, ": ", "wiki_sep")
                self.wiki_text.insert(tk.END, summary)
            self.wiki_toggle_btn.config(text="展开", state=tk.NORMAL)
        else:
            self.wiki_text.insert(tk.END, "（无 Wiki 摘要）")
            self.wiki_toggle_btn.config(text="展开", state=tk.DISABLED)
        self.wiki_text.config(state=tk.DISABLED)
        # 切换图片时收起 wiki
        if self._wiki_visible.get():
            self.wiki_text.pack_forget()
            self._wiki_visible.set(False)
            self.wiki_toggle_btn.config(text="展开")

        # -- 计算并显示最终结果 --
        self._recalculate_filtered()

        # -- 状态栏 --
        status_parts = []
        if not remove_error and removed_tags:
            status_parts.append(f"剔除 {len(removed_tags)} 个")
        if not correct_error and corrected_tags:
            status_parts.append(f"修正 {len(corrected_tags)} 个")
        if not add_error and added_tags:
            status_parts.append(f"新增 {len(added_tags)} 个")

        error_parts = []
        if remove_error:
            error_parts.append("删标")
        if correct_error:
            error_parts.append("改标")
        if add_error:
            error_parts.append("添标")

        if status_parts and not error_parts:
            self.status_label.config(text=f"✅ 分析完成 — 建议{'，'.join(status_parts)}标签")
        elif status_parts and error_parts:
            self.status_label.config(
                text=f"⚠️ {'，'.join(error_parts)}失败 | {'，'.join(status_parts)}"
            )
        elif error_parts and not status_parts:
            self.status_label.config(text=f"❌ {'，'.join(error_parts)}失败")
        else:
            self.status_label.config(text="✅ 分析完成 — 所有标签均正确")

        self._set_buttons_state(tk.NORMAL)

    def _recalculate_filtered(self):
        """根据三个复选框状态，从原始标签重新计算最终标签"""
        original_tags = self._current_original_tags
        removed_tags = self._current_removed_tags
        corrected_tags = self._current_corrected_tags
        added_tags = self._current_added_tags

        accept_remove = self.remove_var.get()
        accept_correct = self.correct_var.get()
        accept_add = self.add_var.get()

        removed_set = set(t.lower() for t in removed_tags) if accept_remove else set()
        corrected_map = {k.lower(): v for k, v in corrected_tags.items()} if accept_correct else {}

        filtered_tags = []
        for t in original_tags:
            t_lower = t.lower().strip()
            if t_lower in removed_set:
                continue
            elif t_lower in corrected_map:
                filtered_tags.append(corrected_map[t_lower])
            else:
                filtered_tags.append(t)

        if accept_add:
            for t in added_tags:
                filtered_tags.append(t)

        self.filtered_text.delete("1.0", tk.END)
        self.filtered_text.insert(tk.END, ", ".join(filtered_tags))

    # ==================== 按钮操作 ====================

    def _set_buttons_state(self, state):
        self.confirm_btn.config(state=state)
        self.skip_btn.config(state=state)

    def _on_confirm(self):
        """确认：用过滤后的标签覆盖原文件"""
        img_path, txt_path = self.pairs[self.current_index]
        new_content = self.filtered_text.get("1.0", tk.END).strip()
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        if not self.current_error:
            self._record_processed(img_path)
        self.stats["confirmed"] += 1
        self.current_index += 1
        self._process_current()

    def _on_skip(self):
        """跳过：保留原文件不变"""
        img_path, _ = self.pairs[self.current_index]
        if not self.current_error:
            self._record_processed(img_path)
        self.stats["skipped"] += 1
        self.current_index += 1
        self._process_current()

    def _on_abort(self):
        """中止处理"""
        if messagebox.askyesno("确认中止", "确定要中止处理吗？\n剩余图片将不会被处理。"):
            self._finish()

    def _on_close(self):
        """窗口关闭"""
        self._stop_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def _finish(self):
        """处理完成，显示统计"""
        self._stop_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

        total = len(self.pairs)
        msg = (
            f"处理完成！\n\n"
            f"总计: {total} 张图片\n"
            f"确认覆盖: {self.stats['confirmed']} 张\n"
            f"跳过: {self.stats['skipped']} 张\n"
            f"失败: {self.stats['failed']} 张"
        )
        messagebox.showinfo("处理完成", msg)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    valid_extensions = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

    if not os.path.exists(IMAGE_DIR):
        print(f"错误: 目录 {IMAGE_DIR} 不存在")
        return

    process_log_path = os.path.join(IMAGE_DIR, "process.log")
    processed_files = set()
    if os.path.exists(process_log_path):
        try:
            with open(process_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        processed_files.add(line)
        except Exception as e:
            print(f"读取 process.log 失败: {e}")

    pairs = []
    for f in sorted(os.listdir(IMAGE_DIR)):
        if f.lower().endswith(valid_extensions):
            if f in processed_files:
                continue
            image_path = os.path.join(IMAGE_DIR, f)
            txt_path = os.path.splitext(image_path)[0] + ".txt"
            if os.path.exists(txt_path):
                pairs.append((image_path, txt_path))

    if not pairs:
        print("没有找到未处理的图片+txt配对文件。")
        return

    print(f"找到 {len(pairs)} 组图片+标签配对，启动 GUI 审核...")
    print(f"后台预取并发数: {PREFETCH_WORKERS}")

    app = TagReviewApp(pairs, client)
    app.run()


if __name__ == "__main__":
    main()