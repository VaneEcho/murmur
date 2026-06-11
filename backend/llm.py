from __future__ import annotations

import json
import re
import httpx
from datetime import date as date_cls

EXTRACT_INSTRUCTION = """你是日记整理助手。今天是 {today}，本条记录的默认日期是 {default_date}。

请完成两件事：
1. 如果原文中明确提到了所记录的日期（如「6月8号」「昨天」「上周五」），换算成 YYYY-MM-DD 格式放入 date 字段；如果没有提到日期，date 为 null。
2. 按用户的整理要求处理文字，放入 text 字段。text 中不要包含日期或星期的抬头（程序会自动添加）。

只输出一个 JSON 对象，不要任何其他内容：
{{"date": "YYYY-MM-DD" 或 null, "text": "整理后的文字"}}"""


def _parse_reply(reply: str) -> tuple[str | None, str]:
    """解析模型返回的 JSON；解析失败时整段当作正文。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        data = json.loads(cleaned)
        d = data.get("date")
        if d:
            date_cls.fromisoformat(d)  # 验证格式
        return (d or None), (data.get("text") or "").strip()
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None, reply.strip()


async def rewrite(
    content: str,
    date: str,
    prompt_template: str,
    url: str,
    api_key: str,
    model: str,
    glossary: str = "",
) -> tuple[str | None, str]:
    """整理文字并提取原文中提到的日期。返回 (提取到的日期或 None, 整理后正文)。"""
    user_prompt = prompt_template.replace("{content}", content).replace("{date}", date)
    if glossary.strip():
        user_prompt += (
            "\n\n以下是常用词表（人名、地名、专有名词等）。原文来自语音输入，"
            "如出现与词表中条目同音或近音的错字，请校正为词表中的写法：\n"
            + glossary.strip()
        )
    system_prompt = EXTRACT_INSTRUCTION.format(today=str(date_cls.today()), default_date=date)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    base = url.rstrip("/")
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(base, json=payload, headers=headers)
        resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return _parse_reply(reply)
