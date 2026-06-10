import httpx


async def create_memo(content: str, url: str, token: str) -> dict:
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"content": content}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{base}/api/v1/memos", json=payload, headers=headers)
        resp.raise_for_status()
    return resp.json()
