from __future__ import annotations

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import date as date_cls, timedelta
import os

from backend.db import get_settings, save_settings
from backend.llm import rewrite
from backend.memos import create_memo, update_memo_content, list_diary

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))

MISSING_WINDOW_DAYS = 14


def _config_error(cfg: dict) -> str | None:
    missing = [k for k in ("memos_url", "memos_token", "llm_url", "llm_api_key") if not cfg.get(k)]
    if missing:
        return f"请先在设置页填写：{', '.join(missing)}"
    return None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "today": str(date_cls.today())})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = get_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg})


@app.post("/settings")
async def save_settings_post(
    request: Request,
    memos_url: str = Form(""),
    memos_token: str = Form(""),
    llm_url: str = Form(""),
    llm_api_key: str = Form(""),
    llm_model: str = Form(""),
    prompt: str = Form(""),
    diary_tag: str = Form("日记"),
    glossary: str = Form(""),
):
    save_settings({
        "memos_url": memos_url,
        "memos_token": memos_token,
        "llm_url": llm_url,
        "llm_api_key": llm_api_key,
        "llm_model": llm_model,
        "prompt": prompt,
        "diary_tag": diary_tag.strip().lstrip("#") or "日记",
        "glossary": glossary,
    })
    cfg = get_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg, "saved": True})


@app.get("/api/status")
async def status():
    """返回上次记录日期和最近 14 天内的空缺日期。"""
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        diary = await list_diary(cfg["memos_url"], cfg["memos_token"], cfg["diary_tag"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)

    written = {d["date"] for d in diary}
    today = date_cls.today()
    missing = []
    for i in range(MISSING_WINDOW_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        if str(d) not in written:
            missing.append(str(d))
    return JSONResponse({
        "ok": True,
        "last_date": diary[0]["date"] if diary else None,
        "missing_days": missing,
        "today": str(today),
    })


@app.post("/api/process")
async def process(request: Request):
    body = await request.json()
    content = (body.get("content") or "").strip()
    record_date = body.get("date") or str(date_cls.today())
    mode = body.get("mode") or "auto"  # auto | merge | new

    if not content:
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)

    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    tag = cfg["diary_tag"]
    existing = None
    if mode != "new":
        try:
            diary = await list_diary(cfg["memos_url"], cfg["memos_token"], tag)
            existing = next((d for d in diary if d["date"] == record_date), None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)

    if existing and mode == "auto":
        return JSONResponse({
            "ok": False,
            "conflict": True,
            "existing": existing["content"],
            "error": f"{record_date} 已有记录",
        }, status_code=409)

    raw = content
    if existing and mode == "merge":
        old = existing["content"].replace(f"#{tag}", "").strip()
        raw = f"这一天已有的记录：\n{old}\n\n新补充的口述内容：\n{content}\n\n请将两者合并整理为一条完整记录。"

    try:
        polished = await rewrite(
            content=raw,
            date=record_date,
            prompt_template=cfg["prompt"],
            url=cfg["llm_url"],
            api_key=cfg["llm_api_key"],
            model=cfg.get("llm_model") or "gpt-4o-mini",
            glossary=cfg.get("glossary", ""),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"AI 整理失败：{e}"}, status_code=500)

    final = f"{polished}\n\n#{tag}"
    try:
        if existing and mode == "merge":
            memo = await update_memo_content(existing["name"], final, cfg["memos_url"], cfg["memos_token"])
        else:
            memo = await create_memo(final, cfg["memos_url"], cfg["memos_token"], display_date=record_date)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存到 Memos 失败：{e}"}, status_code=500)

    return JSONResponse({"ok": True, "polished": polished, "memo_id": memo.get("name", "")})
