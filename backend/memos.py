from __future__ import annotations

import httpx
from datetime import datetime, date


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def create_memo(content: str, url: str, token: str, display_date: str | None = None) -> dict:
    """创建 memo；若指定日期则补设 displayTime，使其在 Memos 中按目标日期排序。"""
    base = url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{base}/api/v1/memos", json={"content": content}, headers=_headers(token))
        resp.raise_for_status()
        memo = resp.json()
        if display_date and display_date != str(date.today()):
            ts = f"{display_date}T12:00:00Z"
            patch = await client.patch(
                f"{base}/api/v1/{memo['name']}",
                json={"displayTime": ts},
                headers=_headers(token),
            )
            patch.raise_for_status()
            memo = patch.json()
    return memo


async def update_memo_content(name: str, content: str, url: str, token: str) -> dict:
    base = url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(f"{base}/api/v1/{name}", json={"content": content}, headers=_headers(token))
        resp.raise_for_status()
    return resp.json()


async def list_diary(url: str, token: str, tag: str, page_size: int = 200) -> list[dict]:
    """拉取最近 memo 并按标签过滤出日记，返回 [{name, date, content}]，按日期倒序。"""
    base = url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base}/api/v1/memos",
            params={"pageSize": page_size},
            headers=_headers(token),
        )
        resp.raise_for_status()
    memos = resp.json().get("memos", [])
    marker = f"#{tag}"
    diary = []
    for m in memos:
        if marker not in m.get("content", ""):
            continue
        dt = m.get("displayTime") or m.get("createTime") or ""
        try:
            d = datetime.fromisoformat(dt.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        diary.append({"name": m["name"], "date": str(d), "content": m["content"]})
    diary.sort(key=lambda x: x["date"], reverse=True)
    return diary
