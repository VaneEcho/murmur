import httpx


async def rewrite(content: str, date: str, prompt_template: str, url: str, api_key: str, model: str) -> str:
    prompt = prompt_template.replace("{content}", content).replace("{date}", date)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    base = url.rstrip("/")
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(base, json=payload, headers=headers)
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
