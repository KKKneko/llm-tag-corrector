"""
批量预总结 Danbooru wiki 描述 → 存入数据库 wiki_pages.summary 列。
支持断点续传、并发请求、自动重试。

用法:
    python batch_summarize_wiki.py                  # 默认参数运行
    python batch_summarize_wiki.py --workers 8      # 8 并发
    python batch_summarize_wiki.py --batch-size 30  # 每批 30 个标签
    python batch_summarize_wiki.py --dry-run        # 只统计，不调用 API
"""

import os
import sys
import json
import re
import time
import random
import sqlite3
import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ================= 配置（从 gemini_caption.py 复制，可按需修改） =================
SUMMARY_API_KEY = "sk-709be516b6e042bbaa9e819c33aafdb2"
SUMMARY_BASE_URL = "https://api.deepseek.com/v1"
SUMMARY_MODEL_NAME = "deepseek-chat"
DANBOORU_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "danbooru_tags.db")

# 批处理参数
DEFAULT_BATCH_SIZE = 20       # 每次 API 请求包含多少个标签的 wiki
DEFAULT_WORKERS = 100           # 并发线程数
MAX_RETRIES = 3               # 单批最大重试次数
RETRY_DELAY = 3               # 重试间隔秒数
MAX_WIKI_BODY_LEN = 2000      # wiki body 截断长度


def ensure_summary_column(conn):
    """确保 wiki_pages 表有 summary 列"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
    if "summary" not in cols:
        conn.execute("ALTER TABLE wiki_pages ADD COLUMN summary TEXT DEFAULT NULL")
        conn.commit()
        print("[DB] 已添加 wiki_pages.summary 列")


def get_pending_wikis(conn, limit=None):
    """获取所有有 body 但没有 summary 的 wiki 条目"""
    query = """
        SELECT title, body FROM wiki_pages
        WHERE body IS NOT NULL AND body != '' AND summary IS NULL
        ORDER BY title
    """
    if limit:
        query += f" LIMIT {limit}"
    return conn.execute(query).fetchall()


def _extract_json_from_response(content):
    """从模型响应中提取 JSON"""
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)```", content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        pass
    return None


def summarize_batch(client, batch):
    """
    调用摘要模型，对一批 wiki 做总结。
    batch: [(title, body), ...]
    返回: {title: summary, ...}
    """
    combined_prompt = (
        "你是一个 Danbooru 标签描述总结专家。我会给你多个 Danbooru 标签的 wiki 描述（DText 格式），"
        "请为每个标签提取并总结关键信息。\n\n"
        "要求：\n"
        "- 每个标签总结为1-3句话\n"
        "- 对于描述视觉概念的标签（服装、动作、物体等）：总结其**视觉表现**（外观、颜色、形状、位置等）\n"
        "- 对于角色标签：总结角色的**出处作品、外貌特征**（发色、瞳色、发型、标志性服饰/配件等）\n"
        "- 对于作品/系列标签：总结作品的**类型、题材、主要设定**\n"
        "- 忽略 wiki 中的使用注意事项、相关标签链接、历史说明等非核心内容\n"
        "- 如果 wiki 描述中没有可总结的有效内容，返回\"无具体描述\"\n\n"
    )

    for title, body in batch:
        truncated = body[:MAX_WIKI_BODY_LEN] if len(body) > MAX_WIKI_BODY_LEN else body
        combined_prompt += f"### {title}\n{truncated}\n\n"

    combined_prompt += (
        "\n请按以下 JSON 格式输出结果，并被包裹在 ```json 和 ``` 之间：\n"
        "```json\n{\n  \"tag_name\": \"标签总结\",\n  ...\n}\n```"
    )

    response = client.chat.completions.create(
        model=SUMMARY_MODEL_NAME,
        messages=[{"role": "user", "content": combined_prompt}],
        temperature=0.1,
        max_tokens=4000,
    )

    content = response.choices[0].message.content
    if not content:
        return {}

    parsed = _extract_json_from_response(content)
    if isinstance(parsed, dict):
        return {k: v for k, v in parsed.items() if isinstance(v, str) and v.strip()}
    return {}


def save_summaries(conn, summaries):
    """将总结结果写回数据库"""
    cursor = conn.cursor()
    for title, summary in summaries.items():
        cursor.execute(
            "UPDATE wiki_pages SET summary = ? WHERE title = ?",
            (summary.strip(), title)
        )
    conn.commit()
    return cursor.rowcount


def process_batch_with_retry(client, batch, batch_idx):
    """带重试的单批处理"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = summarize_batch(client, batch)
            return result
        except Exception as e:
            print(f"  [批次 {batch_idx}] 第 {attempt}/{MAX_RETRIES} 次失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                traceback.print_exc()
                return {}


def main():
    parser = argparse.ArgumentParser(description="批量预总结 Danbooru wiki 描述")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每批标签数")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发线程数")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不调用 API")
    args = parser.parse_args()

    conn = sqlite3.connect(DANBOORU_DB_PATH)
    ensure_summary_column(conn)

    # 统计
    total_wiki = conn.execute(
        "SELECT COUNT(*) FROM wiki_pages WHERE body IS NOT NULL AND body != ''"
    ).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM wiki_pages WHERE summary IS NOT NULL"
    ).fetchone()[0]
    pending_count = total_wiki - done

    print(f"Wiki 总数: {total_wiki}, 已完成: {done}, 待处理: {pending_count}")

    if args.dry_run:
        conn.close()
        return

    if pending_count == 0:
        print("全部已完成，无需处理。")
        conn.close()
        return

    # 获取待处理数据
    pending = get_pending_wikis(conn, limit=args.limit)
    print(f"本次加载: {len(pending)} 条")

    # 分批
    batches = []
    for i in range(0, len(pending), args.batch_size):
        batches.append(pending[i:i + args.batch_size])

    print(f"共 {len(batches)} 批，每批 {args.batch_size} 条，{args.workers} 并发\n")

    client = OpenAI(api_key=SUMMARY_API_KEY, base_url=SUMMARY_BASE_URL)
    total_saved = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, batch in enumerate(batches):
            future = executor.submit(process_batch_with_retry, client, batch, i + 1)
            futures[future] = (i + 1, batch)

        for future in as_completed(futures):
            batch_idx, batch = futures[future]
            try:
                summaries = future.result()
                if summaries:
                    save_summaries(conn, summaries)
                    total_saved += len(summaries)

                    # 随机抽一条打印，供人工抽查
                    sample_tag = random.choice(list(summaries.keys()))
                    print(f"  ↳ 抽查 [{sample_tag}]: {summaries[sample_tag]}")

                batch_titles = [t for t, _ in batch]
                missed = [t for t in batch_titles if t not in summaries]
                elapsed = time.time() - start_time
                rate = total_saved / elapsed * 3600 if elapsed > 0 else 0

                status = f"[{batch_idx}/{len(batches)}] 成功 {len(summaries)}/{len(batch)}"
                if missed:
                    status += f"  未命中: {missed[:3]}{'...' if len(missed) > 3 else ''}"
                status += f"  | 累计: {total_saved}  速率: {rate:.0f}/h"
                print(status)

            except Exception as e:
                print(f"[{batch_idx}/{len(batches)}] 异常: {e}")

    elapsed = time.time() - start_time
    print(f"\n完成！共保存 {total_saved} 条摘要，耗时 {elapsed:.1f}s")

    conn.close()


if __name__ == "__main__":
    main()
