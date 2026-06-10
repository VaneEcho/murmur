import httpx


async def rewrite(
    content: str,
    date: str,
    prompt_template: str,
    url: str,
    api_key: str,
    model: str,
    glossary: str = "",
) -> str:
    prompt = prompt_template.replace("{content}", content).replace("{date}", date)
    if glossary.strip():
        prompt += (
            "\n\n以下是常用词表（人名、地名、专有名词等）。原文来自语音输入，"
            "如出现与词表中条目同音或近音的错字，请校正为词表中的写法：\n"
            + glossary.strip()
        )
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
