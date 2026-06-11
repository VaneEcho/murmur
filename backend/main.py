from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import date as date_cls, timedelta

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.db import (
    get_settings, save_settings,
    save_draft, list_drafts, delete_draft,
    create_job, update_job, list_jobs, recover_stale_jobs,
)
from backend.llm import split_and_polish, merge_rewrite, suggest_glossary
from backend.memos import create_memo, update_memo_content, list_diary

app = FastAPI()
_root = os.path.join(os.path.dirname(__file__), "..")
templates = Jinja2Templates(directory=os.path.join(_root, "frontend", "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(_root, "frontend", "static")), name="static")

MISSING_WINDOW_DAYS = 14
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

PASSWORD = os.environ.get("MURMUR_PASSWORD", "")
COOKIE_NAME = "murmur_auth"


@app.on_event("startup")
async def recover_jobs():
    for job in recover_stale_jobs():
        save_draft(job["content"], job["date"])


def _auth_token() -> str:
    return hashlib.sha256(f"murmur:{PASSWORD}".encode()).hexdigest()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if PASSWORD and path != "/login" and not path.startswith("/static"):
        if request.cookies.get(COOKIE_NAME) != _auth_token():
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
            return RedirectResponse("/login")
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    if password != PASSWORD:
        return templates.TemplateResponse("login.html", {"request": request, "error": "密码不对"})
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, _auth_token(), max_age=90 * 24 * 3600, httponly=True)
    return resp


def _config_error(cfg: dict) -> str | None:
    missing = [k for k in ("memos_url", "memos_token", "llm_url", "llm_api_key") if not cfg.get(k)]
    if missing:
        return f"请先在设置页填写：{', '.join(missing)}"
    return None


def _with_header(polished: str, d: str, tag: str) -> str:
    weekday = WEEKDAYS[date_cls.fromisoformat(d).weekday()]
    return f"{d} {weekday}\n\n{polished}\n\n#{tag}"


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


async def run_job(job_id: int, content: str, ui_date: str):
    """后台执行：AI 拆分整理 → 逐条检查冲突（自动合并）→ 写入 Memos。"""
    update_job(job_id, "processing")
    cfg = get_settings()
    tag = cfg["diary_tag"]
    model = cfg.get("llm_model") or "gpt-4o-mini"
    llm_args = dict(
        prompt_template=cfg["prompt"], url=cfg["llm_url"],
        api_key=cfg["llm_api_key"], model=model, glossary=cfg.get("glossary", ""),
    )
    try:
        entries = await split_and_polish(content=content, default_date=ui_date, **llm_args)
    except Exception as e:
        save_draft(content, ui_date)
        update_job(job_id, "error", error=f"AI 整理失败：{e}（原文已转入草稿）")
        return

    results = []
    try:
        diary = await list_diary(cfg["memos_url"], cfg["memos_token"], tag)
        by_date = {d["date"]: d for d in diary}
        for entry in entries:
            d, text = entry["date"], entry["text"]
            existing = by_date.get(d)
            if existing:
                old = existing["content"].replace(f"#{tag}", "").strip()
                merged = await merge_rewrite(old=old, new=text, date=d, **llm_args)
                await update_memo_content(existing["name"], _with_header(merged, d, tag),
                                          cfg["memos_url"], cfg["memos_token"])
                results.append({"date": d, "action": "merged", "text": merged})
            else:
                await create_memo(_with_header(text, d, tag),
                                  cfg["memos_url"], cfg["memos_token"], display_date=d)
                results.append({"date": d, "action": "created", "text": text})
    except Exception as e:
        save_draft(content, ui_date)
        done = "；".join(f"{r['date']} 已发布" for r in results)
        note = f"（已完成：{done}）" if done else "（原文已转入草稿）"
        update_job(job_id, "error", result=json.dumps(results, ensure_ascii=False),
                   error=f"发布失败：{e}{note}")
        return

    update_job(job_id, "done", result=json.dumps(results, ensure_ascii=False))


@app.post("/api/submit")
async def submit(request: Request):
    body = await request.json()
    content = (body.get("content") or "").strip()
    ui_date = body.get("date") or str(date_cls.today())
    if not content:
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    job_id = create_job(content, ui_date)
    asyncio.create_task(run_job(job_id, content, ui_date))
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/api/jobs")
async def jobs():
    items = list_jobs()
    for it in items:
        it["result"] = json.loads(it["result"]) if it["result"] else []
    return JSONResponse({"ok": True, "jobs": items})


@app.get("/api/drafts")
async def drafts():
    return JSONResponse({"ok": True, "drafts": list_drafts()})


@app.post("/api/drafts/delete")
async def drafts_delete(request: Request):
    body = await request.json()
    delete_draft(int(body.get("id", 0)))
    return JSONResponse({"ok": True})


@app.post("/api/glossary/suggest")
async def glossary_suggest():
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        diary = await list_diary(cfg["memos_url"], cfg["memos_token"], cfg["diary_tag"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)
    if not diary:
        return JSONResponse({"ok": False, "error": "还没有日记记录，无法提取"}, status_code=400)

    texts = "\n---\n".join(d["content"].replace(f"#{cfg['diary_tag']}", "").strip() for d in diary[:30])
    try:
        terms = await suggest_glossary(
            texts=texts,
            current_glossary=cfg.get("glossary", ""),
            url=cfg["llm_url"],
            api_key=cfg["llm_api_key"],
            model=cfg.get("llm_model") or "gpt-4o-mini",
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"AI 提取失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "terms": terms})
