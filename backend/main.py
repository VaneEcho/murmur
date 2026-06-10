from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import date as date_cls
import os

from backend.db import get_settings, save_settings
from backend.llm import rewrite
from backend.memos import create_memo

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))


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
):
    save_settings({
        "memos_url": memos_url,
        "memos_token": memos_token,
        "llm_url": llm_url,
        "llm_api_key": llm_api_key,
        "llm_model": llm_model,
        "prompt": prompt,
    })
    cfg = get_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg, "saved": True})


@app.post("/api/process")
async def process(request: Request):
    body = await request.json()
    content = (body.get("content") or "").strip()
    record_date = body.get("date") or str(date_cls.today())

    if not content:
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)

    cfg = get_settings()
    missing = [k for k in ("memos_url", "memos_token", "llm_url", "llm_api_key") if not cfg.get(k)]
    if missing:
        return JSONResponse({"ok": False, "error": f"请先在设置页填写：{', '.join(missing)}"}, status_code=400)

    try:
        polished = await rewrite(
            content=content,
            date=record_date,
            prompt_template=cfg["prompt"],
            url=cfg["llm_url"],
            api_key=cfg["llm_api_key"],
            model=cfg.get("llm_model") or "gpt-4o-mini",
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"AI 整理失败：{e}"}, status_code=500)

    try:
        memo = await create_memo(
            content=polished,
            url=cfg["memos_url"],
            token=cfg["memos_token"],
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存到 Memos 失败：{e}"}, status_code=500)

    return JSONResponse({"ok": True, "polished": polished, "memo_id": memo.get("name", "")})
