import sqlite3
import os

_default_db = os.path.join(os.path.dirname(__file__), "..", "data", "murmur.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

DEFAULT_PROMPT = """你是一个日记整理助手。用户用语音口述了以下内容，请将其整理成自然、简洁的书面语记录，保留所有关键信息，去除口语化表达和冗余词汇。日期：{date}。

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
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
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
