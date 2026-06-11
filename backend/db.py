import sqlite3
import os

_default_db = os.path.join(os.path.dirname(__file__), "..", "data", "murmur.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

DEFAULT_PROMPT = """你是一个日记整理助手。用户用语音口述了以下内容，请将其整理成自然、简洁的书面语记录，保留所有关键信息，去除口语化表达和冗余词汇。日期：{date}。

格式要求：每一件事写成一行；只有当某一件事特别长时才在这件事内部换行。不同的事不要合并到同一行。

原始内容：
{content}

直接输出整理后的文字，不需要任何说明。"""

DEFAULT_SETTINGS = {
    "memos_url": "",
    "memos_token": "",
    "llm_url": "",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    "prompt": DEFAULT_PROMPT,
    "diary_tag": "日记",
    "glossary": "",
}


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drafts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content TEXT NOT NULL, date TEXT NOT NULL, "
            "created TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content TEXT NOT NULL, date TEXT NOT NULL, "
            "status TEXT DEFAULT 'pending', "  # pending | processing | done | error
            "result TEXT, error TEXT, "
            "created TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()


def create_job(content: str, date: str) -> int:
    init_db()
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO jobs (content, date) VALUES (?, ?)", (content, date))
        conn.commit()
        return cur.lastrowid


def update_job(job_id: int, status: str, result: str = None, error: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result = ?, error = ? WHERE id = ?",
            (status, result, error, job_id),
        )
        conn.commit()


def list_jobs(limit: int = 10) -> list:
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, date, status, result, error, created FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recover_stale_jobs() -> list:
    """服务重启后，把卡在 pending/processing 的任务标记失败，返回其内容用于转草稿。"""
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, date FROM jobs WHERE status IN ('pending', 'processing')"
        ).fetchall()
        stale = [dict(r) for r in rows]
        conn.execute(
            "UPDATE jobs SET status = 'error', error = '服务重启中断，原文已转入草稿' "
            "WHERE status IN ('pending', 'processing')"
        )
        conn.commit()
    return stale


def save_draft(content: str, date: str) -> int:
    init_db()
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO drafts (content, date) VALUES (?, ?)", (content, date))
        conn.commit()
        return cur.lastrowid


def list_drafts() -> list:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT id, content, date, created FROM drafts ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_draft(draft_id: int):
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        conn.commit()


def get_settings() -> dict:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_settings(data: dict):
    init_db()
    with get_conn() as conn:
        for k, v in data.items():
            if k in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v)
                )
        conn.commit()
