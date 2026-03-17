"""
build_danbooru_db.py — 拉取 Danbooru 标签、别名、Wiki 页面，存入本地 SQLite 数据库。

用法：
    python build_danbooru_db.py

特性：
    - 游标分页 (page=b{id})，每页 1000 条
    - 断点续传：重新运行会从上次停下的地方继续
    - 礼貌间隔：每次请求间隔 0.5s
    - 失败自动重试 3 次

配置：
    修改下方 CONFIG 区域的用户名、API Key、代理等。
"""

import os
import sys
import time
import json
import sqlite3
import requests

# ================= CONFIG =================
DANBOORU_USERNAME = "cc987"
DANBOORU_API_KEY = "5VU3KKukEaZjoR4wztoLW4W1"
DANBOORU_PROXY = "http://127.0.0.1:7897"  # 不需要代理设为 None
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "danbooru_tags.db")
REQUEST_INTERVAL = 0.5   # 请求间隔(秒)
RETRY_COUNT = 3
RETRY_BACKOFF = 2        # 重试退避基数(秒)
TIMEOUT = 30
PAGE_LIMIT = 1000
TAG_MIN_POST_COUNT = 1   # 只拉 post_count >= 此值的标签
# ==========================================


def create_session():
    s = requests.Session()
    if DANBOORU_PROXY:
        s.proxies = {"http": DANBOORU_PROXY, "https": DANBOORU_PROXY}
    if DANBOORU_USERNAME and DANBOORU_API_KEY:
        s.auth = (DANBOORU_USERNAME, DANBOORU_API_KEY)
    return s


def get_with_retry(session, url, params):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == RETRY_COUNT:
                raise
            wait = RETRY_BACKOFF ** attempt
            print(f"  请求失败 (第{attempt}次)，{wait}s 后重试: {e}")
            time.sleep(wait)


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tags (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            post_count  INTEGER NOT NULL DEFAULT 0,
            category    INTEGER NOT NULL DEFAULT 0,
            is_deprecated INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tag_aliases (
            id              INTEGER PRIMARY KEY,
            antecedent_name TEXT NOT NULL,
            consequent_name TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS wiki_pages (
            id    INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            body  TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            task_name   TEXT PRIMARY KEY,
            last_id     INTEGER NOT NULL DEFAULT 0,
            done        INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        CREATE INDEX IF NOT EXISTS idx_tags_post_count ON tags(post_count);
        CREATE INDEX IF NOT EXISTS idx_aliases_antecedent ON tag_aliases(antecedent_name);
        CREATE INDEX IF NOT EXISTS idx_aliases_consequent ON tag_aliases(consequent_name);
        CREATE INDEX IF NOT EXISTS idx_wiki_title ON wiki_pages(title);
    """)
    conn.commit()
    return conn


def get_sync_state(conn, task_name):
    """获取同步进度。返回 (last_id, done)"""
    row = conn.execute(
        "SELECT last_id, done FROM sync_state WHERE task_name = ?", (task_name,)
    ).fetchone()
    if row:
        return row[0], bool(row[1])
    return 0, False


def set_sync_state(conn, task_name, last_id, done=False):
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (task_name, last_id, done) VALUES (?, ?, ?)",
        (task_name, last_id, int(done))
    )
    conn.commit()


# ─────────────────── 拉取 Tags ───────────────────

def pull_tags(session, conn):
    task = "tags"
    last_id, done = get_sync_state(conn, task)
    if done:
        count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        print(f"[Tags] 已完成，共 {count} 条，跳过。")
        return

    if last_id == 0:
        # 首次拉取：从最大 ID 开始
        # 先获取一个最大 ID
        data = get_with_retry(session,
            "https://danbooru.donmai.us/tags.json",
            {"limit": 1, "search[order]": "id_desc"})
        if not data:
            print("[Tags] 无法获取最大 ID，退出")
            return
        cursor_id = data[0]["id"] + 1
        print(f"[Tags] 最大 ID: {cursor_id - 1}，开始拉取...")
    else:
        cursor_id = last_id
        count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        print(f"[Tags] 从上次断点继续 (cursor={cursor_id}，已有 {count} 条)")

    total_inserted = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    batch_count = 0

    while True:
        params = {
            "limit": PAGE_LIMIT,
            "page": f"b{cursor_id}",
            "search[order]": "id_desc",
        }
        if TAG_MIN_POST_COUNT > 0:
            params["search[post_count]"] = f">={TAG_MIN_POST_COUNT}"

        data = get_with_retry(session, "https://danbooru.donmai.us/tags.json", params)

        if not data:
            print(f"[Tags] 拉取完毕，共 {total_inserted} 条")
            set_sync_state(conn, task, cursor_id, done=True)
            break

        rows = [
            (t["id"], t["name"], t.get("post_count", 0),
             t.get("category", 0), int(t.get("is_deprecated", False)))
            for t in data
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO tags (id, name, post_count, category, is_deprecated) VALUES (?, ?, ?, ?, ?)",
            rows
        )
        total_inserted += len(rows)
        cursor_id = min(t["id"] for t in data)
        batch_count += 1

        conn.commit()
        set_sync_state(conn, task, cursor_id)

        if batch_count % 10 == 0:
            print(f"[Tags] 已拉取 {total_inserted} 条 (cursor={cursor_id})")

        time.sleep(REQUEST_INTERVAL)

    print(f"[Tags] 完成，共 {total_inserted} 条")


# ─────────────────── 拉取 Tag Aliases ───────────────────

def pull_aliases(session, conn):
    task = "tag_aliases"
    last_id, done = get_sync_state(conn, task)
    if done:
        count = conn.execute("SELECT COUNT(*) FROM tag_aliases").fetchone()[0]
        print(f"[Aliases] 已完成，共 {count} 条，跳过。")
        return

    if last_id == 0:
        data = get_with_retry(session,
            "https://danbooru.donmai.us/tag_aliases.json",
            {"limit": 1, "search[order]": "id_desc"})
        if not data:
            print("[Aliases] 无法获取最大 ID，退出")
            return
        cursor_id = data[0]["id"] + 1
        print(f"[Aliases] 最大 ID: {cursor_id - 1}，开始拉取...")
    else:
        cursor_id = last_id
        count = conn.execute("SELECT COUNT(*) FROM tag_aliases").fetchone()[0]
        print(f"[Aliases] 从上次断点继续 (cursor={cursor_id}，已有 {count} 条)")

    total_inserted = conn.execute("SELECT COUNT(*) FROM tag_aliases").fetchone()[0]
    batch_count = 0

    while True:
        params = {
            "limit": PAGE_LIMIT,
            "page": f"b{cursor_id}",
            "search[order]": "id_desc",
            "search[status]": "active",
        }

        data = get_with_retry(session, "https://danbooru.donmai.us/tag_aliases.json", params)

        if not data:
            print(f"[Aliases] 拉取完毕，共 {total_inserted} 条")
            set_sync_state(conn, task, cursor_id, done=True)
            break

        rows = [
            (a["id"], a["antecedent_name"], a["consequent_name"], a.get("status", "active"))
            for a in data
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO tag_aliases (id, antecedent_name, consequent_name, status) VALUES (?, ?, ?, ?)",
            rows
        )
        total_inserted += len(rows)
        cursor_id = min(a["id"] for a in data)
        batch_count += 1

        conn.commit()
        set_sync_state(conn, task, cursor_id)

        if batch_count % 10 == 0:
            print(f"[Aliases] 已拉取 {total_inserted} 条 (cursor={cursor_id})")

        time.sleep(REQUEST_INTERVAL)

    print(f"[Aliases] 完成，共 {total_inserted} 条")


# ─────────────────── 拉取 Wiki Pages ───────────────────

def pull_wiki(session, conn):
    task = "wiki_pages"
    last_id, done = get_sync_state(conn, task)
    if done:
        count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        print(f"[Wiki] 已完成，共 {count} 条，跳过。")
        return

    if last_id == 0:
        data = get_with_retry(session,
            "https://danbooru.donmai.us/wiki_pages.json",
            {"limit": 1, "search[order]": "id_desc"})
        if not data:
            print("[Wiki] 无法获取最大 ID，退出")
            return
        cursor_id = data[0]["id"] + 1
        print(f"[Wiki] 最大 ID: {cursor_id - 1}，开始拉取...")
    else:
        cursor_id = last_id
        count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        print(f"[Wiki] 从上次断点继续 (cursor={cursor_id}，已有 {count} 条)")

    total_inserted = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
    batch_count = 0

    while True:
        params = {
            "limit": PAGE_LIMIT,
            "page": f"b{cursor_id}",
            "search[order]": "id_desc",
        }

        data = get_with_retry(session, "https://danbooru.donmai.us/wiki_pages.json", params)

        if not data:
            print(f"[Wiki] 拉取完毕，共 {total_inserted} 条")
            set_sync_state(conn, task, cursor_id, done=True)
            break

        rows = [
            (w["id"], w.get("title", ""), w.get("body", ""))
            for w in data
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO wiki_pages (id, title, body) VALUES (?, ?, ?)",
            rows
        )
        total_inserted += len(rows)
        cursor_id = min(w["id"] for w in data)
        batch_count += 1

        conn.commit()
        set_sync_state(conn, task, cursor_id)

        if batch_count % 10 == 0:
            print(f"[Wiki] 已拉取 {total_inserted} 条 (cursor={cursor_id})")

        time.sleep(REQUEST_INTERVAL)

    print(f"[Wiki] 完成，共 {total_inserted} 条")


# ─────────────────── 构建辅助数据 ───────────────────

def build_search_index(conn):
    """构建用于模糊搜索的辅助索引 — 把 tag name 按 trigram 拆分存储"""
    print("[索引] 构建 trigram 搜索索引...")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tag_trigrams (
            trigram TEXT NOT NULL,
            tag_name TEXT NOT NULL
        );
        DELETE FROM tag_trigrams;
    """)

    # 只索引 post_count >= 10 的标签（太冷门的标签不值得模糊匹配）
    rows = conn.execute(
        "SELECT name FROM tags WHERE post_count >= 10 AND is_deprecated = 0"
    ).fetchall()

    trigram_rows = []
    for (name,) in rows:
        padded = f"  {name}  "
        trigrams = set()
        for i in range(len(padded) - 2):
            trigrams.add(padded[i:i+3])
        for tri in trigrams:
            trigram_rows.append((tri, name))

    conn.executemany(
        "INSERT INTO tag_trigrams (trigram, tag_name) VALUES (?, ?)",
        trigram_rows
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trigram ON tag_trigrams(trigram)")
    conn.commit()

    print(f"[索引] 完成，{len(rows)} 个标签，{len(trigram_rows)} 条 trigram")


def print_stats(conn):
    tags_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    aliases_count = conn.execute("SELECT COUNT(*) FROM tag_aliases").fetchone()[0]
    wiki_count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]

    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)

    print(f"\n========== 数据库统计 ==========")
    print(f"标签数量:   {tags_count:>10,}")
    print(f"别名数量:   {aliases_count:>10,}")
    print(f"Wiki 页面:  {wiki_count:>10,}")
    print(f"数据库大小: {db_size:>10.1f} MB")
    print(f"路径:       {DB_PATH}")
    print(f"================================\n")


def main():
    print(f"数据库路径: {DB_PATH}")
    session = create_session()
    conn = init_db(DB_PATH)

    try:
        pull_tags(session, conn)
        pull_aliases(session, conn)
        pull_wiki(session, conn)
        build_search_index(conn)
        print_stats(conn)
    except KeyboardInterrupt:
        print("\n中断，进度已保存，下次运行可继续。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
