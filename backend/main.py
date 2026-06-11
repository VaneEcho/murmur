from __future__ import annotations

import hashlib
import os
from datetime import date as date_cls, timedelta

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.db import get_settings, save_settings
from backend.llm import rewrite
from backend.memos import create_memo, update_memo_content, list_diary

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))

MISSING_WINDOW_DAYS = 14
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

PASSWORD = os.environ.get("MURMUR_PASSWORD", "")
COOKIE_NAME = "murmur_auth"


def _auth_token() -> str:
    return hashlib.sha256(f"murmur:{PASSWORD}".encode()).hexdigest()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if PASSWORD and request.url.path != "/login":
        if request.cookies.get(COOKIE_NAME) != _auth_token():
            if request.url.path.startswith("/api/"):
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


@app.post("/api/process")
async def process(request: Request):
    body = await request.json()
    content = (body.get("content") or "").strip()
    ui_date = body.get("date") or str(date_cls.today())
    mode = body.get("mode") or "auto"          # auto | merge | new
    prepolished = (body.get("polished") or "").strip()  # 冲突重提交时复用已整理文本

    if not content and not prepolished:
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)

    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    tag = cfg["diary_tag"]

    # mode=new 且已有整理结果：直接落库，不再调用 AI
    if mode == "new" and prepolished:
        try:
            memo = await create_memo(_with_header(prepolished, ui_date, tag),
                                     cfg["memos_url"], cfg["memos_token"], display_date=ui_date)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"保存到 Memos 失败：{e}"}, status_code=500)
        return JSONResponse({"ok": True, "polished": prepolished, "date": ui_date, "memo_id": memo.get("name", "")})

    existing = None
    if mode == "merge":
        try:
            diary = await list_diary(cfg["memos_url"], cfg["memos_token"], tag)
            existing = next((d for d in diary if d["date"] == ui_date), None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)

    raw = content
    if existing:
        old = existing["content"].replace(f"#{tag}", "").strip()
        raw = f"这一天已有的记录：\n{old}\n\n新补充的口述内容：\n{content}\n\n请将两者合并整理为一条完整记录。"

    try:
        extracted_date, polished = await rewrite(
            content=raw,
            date=ui_date,
            prompt_template=cfg["prompt"],
            url=cfg["llm_url"],
            api_key=cfg["llm_api_key"],
            model=cfg.get("llm_model") or "gpt-4o-mini",
            glossary=cfg.get("glossary", ""),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"AI 整理失败：{e}"}, status_code=500)

    # 口述中提到的日期优先于界面选的日期（仅 auto 模式；merge 时日期已确定）
    final_date = ui_date
    if mode == "auto" and extracted_date:
        final_date = extracted_date

    if mode == "auto":
        try:
            diary = await list_diary(cfg["memos_url"], cfg["memos_token"], tag)
            conflict = next((d for d in diary if d["date"] == final_date), None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)
        if conflict:
            return JSONResponse({
                "ok": False,
                "conflict": True,
                "date": final_date,
                "polished": polished,
                "existing": conflict["content"],
                "error": f"{final_date} 已有记录",
            }, status_code=409)

    final = _with_header(polished, final_date, tag)
    try:
        if existing:
            memo = await update_memo_content(existing["name"], final, cfg["memos_url"], cfg["memos_token"])
        else:
            memo = await create_memo(final, cfg["memos_url"], cfg["memos_token"], display_date=final_date)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存到 Memos 失败：{e}"}, status_code=500)

    return JSONResponse({"ok": True, "polished": polished, "date": final_date, "memo_id": memo.get("name", "")})
