from __future__ import annotations

import sqlite3
import os

_default_db = os.path.join(os.path.dirname(__file__), "..", "data", "murmur.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

DEFAULT_PROMPT = """你是我的日记助手。下面是我用语音输入法口述的日记草稿，请帮我轻度整理。

只做这三件事：
1. 改正语音识别产生的错字、同音字、近音字（结合上下文判断）
2. 修正明显不通顺的语句，理顺断句，补上合适的标点
3. 把每一件事写成单独的一行，只有一件事特别长时才在内部换行

绝对不要做的事：
- 不要改成书面语、公文腔或"更正式"的说法。保留我原本的用词和口语习惯。
  例如「中午应该是有炸鸡腿」最多理顺成「中午吃了炸鸡腿」，绝不能写成「午餐摄入了炸鸡腿」
- 不要替换我的词汇、不要润色修辞
- 不要增加、删减或脑补任何信息，拿不准的保持原样

日期：{date}
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
    "organize_model": "",
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


def init_proposals():
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proposals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "memo_name TEXT UNIQUE NOT NULL, "
            "content TEXT NOT NULL, "
            "proposal TEXT NOT NULL, "
            "status TEXT DEFAULT 'proposed', "  # proposed | applied | skipped
            "created TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.commit()


def upsert_proposal(memo_name: str, content: str, proposal: str):
    init_proposals()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO proposals (memo_name, content, proposal) VALUES (?, ?, ?) "
            "ON CONFLICT(memo_name) DO UPDATE SET content=excluded.content, "
            "proposal=excluded.proposal, status='proposed'",
            (memo_name, content, proposal),
        )
        conn.commit()


def list_proposals(status: str = "proposed") -> list:
    init_proposals()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, memo_name, content, proposal, status FROM proposals WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def proposal_memo_names() -> set:
    """所有已有提议（不论状态）的 memo_name，用于扫描时跳过。"""
    init_proposals()
    with get_conn() as conn:
        rows = conn.execute("SELECT memo_name FROM proposals").fetchall()
    return {r["memo_name"] for r in rows}


def set_proposal_status(proposal_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id))
        conn.commit()


def get_proposal(proposal_id: int) -> dict | None:
    init_proposals()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, memo_name, content, proposal, status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    return dict(row) if row else None


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


def delete_job(job_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ? AND status IN ('done', 'error')", (job_id,))
        conn.commit()


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
