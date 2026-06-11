from __future__ import annotations

import json
import re
import httpx
from datetime import date as date_cls

LLM_TIMEOUT = 300  # 本地大模型推理可能很慢

SPLIT_INSTRUCTION = """你是日记整理助手。今天是 {today}，本条记录的默认日期是 {default_date}。

用户可能在一次口述中讲了多天的内容。请：
1. 识别原文中提到的日期（如「6月8号」「昨天」「前天」「上周五」），把内容按所属日期拆分；没有明确属于其他日期的内容归入默认日期。
2. 对每一天的内容，按用户的整理要求处理文字。text 中不要包含日期或星期的抬头（程序会自动添加）。

只输出一个 JSON 数组，不要任何其他内容：
[{{"date": "YYYY-MM-DD", "text": "整理后的文字"}}, ...]
按日期升序排列。"""


async def _chat(messages: list, url: str, api_key: str, model: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base = url.rstrip("/")
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(base, json={"model": model, "messages": messages}, headers=headers)
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _strip_fences(s: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip())


def _build_user_prompt(content: str, date: str, prompt_template: str, glossary: str) -> str:
    user_prompt = prompt_template.replace("{content}", content).replace("{date}", date)
    if glossary.strip():
        user_prompt += (
            "\n\n以下是常用词表（人名、地名、专有名词等）。原文来自语音输入，"
            "如出现与词表中条目同音或近音的错字，请校正为词表中的写法：\n"
            + glossary.strip()
        )
    return user_prompt


async def split_and_polish(
    content: str,
    default_date: str,
    prompt_template: str,
    url: str,
    api_key: str,
    model: str,
    glossary: str = "",
) -> list[dict]:
    """整理口述内容并按日期拆分。返回 [{"date": "YYYY-MM-DD", "text": "..."}]。"""
    system_prompt = SPLIT_INSTRUCTION.format(today=str(date_cls.today()), default_date=default_date)
    user_prompt = _build_user_prompt(content, default_date, prompt_template, glossary)
    reply = await _chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        url, api_key, model,
    )
    try:
        data = json.loads(_strip_fences(reply))
        entries = []
        for item in data:
            d = item.get("date") or default_date
            date_cls.fromisoformat(d)  # 验证格式
            text = (item.get("text") or "").strip()
            if text:
                entries.append({"date": d, "text": text})
        if entries:
            return entries
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
        pass
    # 解析失败时整段当作默认日期的一条
    return [{"date": default_date, "text": reply}]


async def merge_rewrite(
    old: str,
    new: str,
    date: str,
    prompt_template: str,
    url: str,
    api_key: str,
    model: str,
    glossary: str = "",
) -> str:
    """把某天已有记录与新口述合并整理为一条，返回正文。"""
    raw = f"这一天已有的记录：\n{old}\n\n新补充的口述内容：\n{new}\n\n请将两者合并整理为一条完整的记录。"
    entries = await split_and_polish(raw, date, prompt_template, url, api_key, model, glossary)
    return "\n\n".join(e["text"] for e in entries)


async def suggest_glossary(
    texts: str,
    current_glossary: str,
    url: str,
    api_key: str,
    model: str,
) -> list[str]:
    """从历史日记文本中提取专有名词，排除已在词表中的，返回候选列表。"""
    prompt = (
        "以下是一些日记片段。请从中提取反复出现或重要的专有名词"
        "（人名、地名、公司/产品名、固定称呼等），用于语音输入的错字校正词表。\n"
        "不要提取普通名词和日常词汇。只输出一个 JSON 数组，如 [\"张三\", \"星巴克\"]，不要其他内容。\n"
    )
    if current_glossary.strip():
        prompt += f"\n以下词已在词表中，不要重复提取：\n{current_glossary.strip()}\n"
    prompt += f"\n日记片段：\n{texts}"
    reply = await _chat([{"role": "user", "content": prompt}], url, api_key, model)
    try:
        terms = json.loads(_strip_fences(reply))
        return [str(t).strip() for t in terms if str(t).strip()] if isinstance(terms, list) else []
    except json.JSONDecodeError:
        return []
