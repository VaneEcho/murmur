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


# base_root -> 是否支持 Ollama 原生接口（首次调用探测后缓存）
_native_ollama: dict = {}


async def _chat(messages: list, url: str, api_key: str, model: str) -> str:
    """优先走 Ollama 原生 /api/chat 并关闭思考模式（推理模型快几十倍）；
    非 Ollama 接口（如 DeepSeek）自动回落到 OpenAI 兼容端点。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base = url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    root = base[:-3] if base.endswith("/v1") else base

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        if _native_ollama.get(root, True):
            try:
                resp = await client.post(
                    f"{root}/api/chat",
                    json={"model": model, "think": False, "stream": False, "messages": messages},
                    headers=headers,
                )
                if resp.status_code == 200:
                    _native_ollama[root] = True
                    return resp.json()["message"]["content"].strip()
                _native_ollama[root] = False
            except httpx.HTTPError:
                _native_ollama[root] = False

        resp = await client.post(f"{base}/chat/completions",
                                 json={"model": model, "messages": messages}, headers=headers)
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def list_models(url: str, api_key: str) -> list[str]:
    """拉取 OpenAI 兼容接口的可用模型列表。"""
    base = url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base}/models", headers=headers)
        resp.raise_for_status()
    data = resp.json().get("data", [])
    return sorted(m["id"] for m in data if m.get("id"))


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
            return _merge_same_date(entries)
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


CLASSIFY_INSTRUCTION = """你是笔记整理助手。下面是笔记软件里的一条旧记录，发布日期是 {created}（可能是后来补写的，这个日期不可信，但年份可以参考）。

请判断类型并输出 JSON 对象：

- "type": 四选一。"diary"＝流水账/日记，以「X月X日」「X月X日，周X」这类日期开头的几乎都是日记；"note"＝知识、心得、备忘、摘抄、链接类笔记；"password"＝账号、密码、密钥、卡号类敏感信息；"other"＝无法明确判断

- "entries": 仅 diary 时输出，其他类型为 []。一条记录可能一次补写了好几天的内容，请按天拆分成数组：[{{"date": "YYYY-MM-DD", "text": "..."}}, ...]
  - date：用正文里写的日期；只写了月日（如「6月4日」）时用发布日期 {created} 的年份补全；完全没写日期则为 null，不要拿发布日期当记录日期
  - text：该天内容的轻度整理。只做三件事：改正语音输入的错字同音字、理顺明显不通的语句、每件事写成一行。保留原本的口语说法，不要改成书面语或公文腔（例如「中午应该是有炸鸡腿」最多理顺成「中午吃了炸鸡腿」，绝不能写成「午餐摄入了炸鸡腿」）。不增加、不删减、不脑补。去掉开头的日期和星期（程序会另行添加）

- "tags": 仅 note 和 password 时输出 1~3 个内容标签，方便日后点标签查找。标签用中文（产品名等专有名词除外）。粒度适中：「证书」「网络配置」这种可以；「知识」「work」太宽泛；「2024年6月办公室路由器改造」太细。优先从已有标签里选：{existing_tags}。没有合适的可以新造。diary 为 []

- "terms": 人名，以及容易被语音输入法写错的专有名词（特定地名、公司名、系统名）。不收普通词汇。没有则 []

只输出 JSON 对象，不要任何其他内容。

记录正文：
{content}"""


def _merge_same_date(entries: list[dict]) -> list[dict]:
    """小模型常把同一天的内容按话题拆成多条，按日期合并兜底（None 视为同一组）。"""
    merged: dict = {}
    order = []
    for e in entries:
        d = e.get("date")
        if d in merged:
            merged[d]["text"] += "\n" + e["text"]
        else:
            merged[d] = {"date": d, "text": e["text"]}
            order.append(d)
    return [merged[d] for d in order]


def _clean_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


async def classify_memo(
    content: str,
    created: str,
    existing_tags: str,
    url: str,
    api_key: str,
    model: str,
) -> dict:
    """对一条旧 memo 分类。返回 {type, entries: [{date, text}], tags, terms}。"""
    prompt = CLASSIFY_INSTRUCTION.format(
        created=created, content=content,
        existing_tags=existing_tags or "（暂无）",
    )
    reply = await _chat([{"role": "user", "content": prompt}], url, api_key, model)
    try:
        data = json.loads(_strip_fences(reply))
        t = data.get("type")
        if t not in ("diary", "note", "password", "other"):
            t = "other"
        entries = []
        for e in (data.get("entries") or []):
            if not isinstance(e, dict):
                continue
            d = e.get("date")
            if d:
                try:
                    date_cls.fromisoformat(d)
                except ValueError:
                    d = None
            text = (e.get("text") or "").strip()
            if text:
                entries.append({"date": d, "text": text})
        return {
            "type": t,
            "entries": _merge_same_date(entries),
            "tags": _clean_list(data.get("tags"))[:3],
            "terms": _clean_list(data.get("terms")),
        }
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"type": "other", "entries": [], "tags": [], "terms": []}


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
