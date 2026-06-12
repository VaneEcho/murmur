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
    create_job, update_job, list_jobs, recover_stale_jobs, delete_job,
    upsert_proposal, list_proposals, proposal_memo_names,
    set_proposal_status, get_proposal,
)
from backend.llm import split_and_polish, merge_rewrite, classify_memo, list_models
from backend.memos import (
    create_memo, update_memo_content, list_diary, list_all_memos,
    set_display_time, written_dates,
)

app = FastAPI()
_root = os.path.join(os.path.dirname(__file__), "..")
templates = Jinja2Templates(directory=os.path.join(_root, "frontend", "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(_root, "frontend", "static")), name="static")

MISSING_WINDOW_DAYS = 14
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# asyncio.create_task 的任务必须保持引用，否则可能被垃圾回收中途杀掉
_background_tasks: set = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

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
    organize_model: str = Form(""),
    llm_think: str = Form(""),
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
        "organize_model": organize_model.strip(),
        "llm_think": "1" if llm_think == "1" else "0",
    })
    cfg = get_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg, "saved": True})


@app.get("/api/status")
async def status():
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        diary, written = await written_dates(cfg["memos_url"], cfg["memos_token"], cfg["diary_tag"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)

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
        think=cfg.get("llm_think") == "1",
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
    spawn(run_job(job_id, content, ui_date))
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/api/jobs")
async def jobs():
    items = list_jobs()
    for it in items:
        it["result"] = json.loads(it["result"]) if it["result"] else []
    return JSONResponse({"ok": True, "jobs": items})


@app.post("/api/jobs/delete")
async def jobs_delete(request: Request):
    body = await request.json()
    delete_job(int(body.get("id", 0)))
    return JSONResponse({"ok": True})


@app.get("/api/drafts")
async def drafts():
    return JSONResponse({"ok": True, "drafts": list_drafts()})


@app.post("/api/drafts/delete")
async def drafts_delete(request: Request):
    body = await request.json()
    delete_draft(int(body.get("id", 0)))
    return JSONResponse({"ok": True})


SCAN_BATCH = 10
_scan_state = {"running": False, "done": 0, "total": 0, "remaining": 0, "error": None, "stop": False}


@app.get("/organize", response_class=HTMLResponse)
async def organize_page(request: Request):
    return templates.TemplateResponse("organize.html", {"request": request})


async def run_scan(batch: list, cfg: dict, existing_tags: str):
    """后台逐条分类，结果写入提议表。可被 stop 标志中断。"""
    model = cfg.get("organize_model") or cfg.get("llm_model") or "gpt-4o-mini"
    try:
        for m in batch:
            if _scan_state["stop"]:
                break
            created = (m.get("displayTime") or m.get("createTime") or "")[:10]
            result = await classify_memo(
                content=m["content"], created=created, existing_tags=existing_tags,
                url=cfg["llm_url"], api_key=cfg["llm_api_key"], model=model,
                think=cfg.get("llm_think") == "1",
            )
            result = _date_prefix_fallback(result, m["content"], created)
            result["current_date"] = created
            upsert_proposal(m["name"], m["content"], json.dumps(result, ensure_ascii=False))
            _scan_state["done"] += 1
            _scan_state["remaining"] = max(0, _scan_state["remaining"] - 1)
    except Exception as e:
        _scan_state["error"] = str(e)
    finally:
        _scan_state["running"] = False
        _scan_state["stop"] = False


def _collect_tags(memos: list) -> str:
    """统计全部 memo 里出现过的标签，按频次取前 30 个。"""
    import re
    from collections import Counter
    counter = Counter()
    for m in memos:
        counter.update(re.findall(r"#([^\s#，。]+)", m.get("content", "")))
    return "、".join(t for t, _ in counter.most_common(30))


_DATE_PREFIX = None


def _date_prefix_fallback(result: dict, content: str, created: str) -> dict:
    """确定性兜底：以「X月X日」开头的记录必为日记，不依赖 AI 判断。"""
    import re
    global _DATE_PREFIX
    if _DATE_PREFIX is None:
        _DATE_PREFIX = re.compile(r"^\s*(\d{1,2})月(\d{1,2})[日号]")
    m = _DATE_PREFIX.match(content)
    if not m or result["type"] == "diary":
        return result
    result["type"] = "diary"
    result["tags"] = []
    if not result["entries"]:
        year = created[:4] if created[:4].isdigit() else str(date_cls.today().year)
        d = f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        text = re.sub(
            r"^\s*\d{1,2}月\d{1,2}[日号][，,。.\s]*(周[一二三四五六日天]|星期[一二三四五六日天])?[，,。.\s]*",
            "", content,
        ).strip()
        result["entries"] = [{"date": d, "text": text or content}]
    return result


def _known_tags(cfg: dict) -> list:
    return [f"#{cfg['diary_tag']}", "#笔记", "#密码"]


@app.post("/api/organize/scan")
async def organize_scan(request: Request):
    """扫描旧 memo 交给 AI 分类。continuous=true 时连续扫完全部，否则只扫一批。"""
    if _scan_state["running"]:
        return JSONResponse({"ok": False, "error": "正在扫描中"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    continuous = bool(body.get("continuous"))
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        memos = await list_all_memos(cfg["memos_url"], cfg["memos_token"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取 Memos 失败：{e}"}, status_code=500)

    seen = proposal_memo_names()
    tags = _known_tags(cfg)
    candidates = [
        m for m in memos
        if m["name"] not in seen and not any(t in m.get("content", "") for t in tags)
    ]
    if not candidates:
        return JSONResponse({"ok": True, "started": 0, "remaining": 0, "message": "没有需要整理的旧记录了"})

    batch = candidates if continuous else candidates[:SCAN_BATCH]
    _scan_state.update({"running": True, "done": 0, "total": len(batch),
                        "remaining": len(candidates) - len(batch), "error": None, "stop": False})
    spawn(run_scan(batch, cfg, _collect_tags(memos)))
    return JSONResponse({"ok": True, "started": len(batch), "remaining": len(candidates) - len(batch)})


@app.post("/api/organize/stop")
async def organize_stop():
    if _scan_state["running"]:
        _scan_state["stop"] = True
    return JSONResponse({"ok": True})


@app.get("/api/organize/list")
async def organize_list():
    items = []
    for p in list_proposals("proposed"):
        prop = json.loads(p["proposal"])
        items.append({
            "id": p["id"], "memo_name": p["memo_name"],
            "content": p["content"], **prop,
        })
    return JSONResponse({"ok": True, "scan": _scan_state, "proposals": items})


@app.post("/api/organize/apply")
async def organize_apply(request: Request):
    """应用用户勾选确认的提议。"""
    body = await request.json()
    items = body.get("items") or []
    cfg = get_settings()
    if err := _config_error(cfg):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    tag_diary = cfg["diary_tag"]

    applied, errors, all_terms = [], [], []
    for item in items:
        p = get_proposal(int(item["id"]))
        if not p or p["status"] != "proposed":
            continue
        typ = item.get("type") or "other"
        try:
            if typ == "diary":
                entries = [e for e in (item.get("entries") or []) if (e.get("text") or "").strip()]
                if not entries:
                    errors.append(f"#{p['id']}: 没有可用的整理内容")
                    continue
                # 第一天更新原条目，其余天各自新建
                first, rest = entries[0], entries[1:]
                d0, t0 = first.get("date"), first["text"].strip()
                new_content = _with_header(t0, d0, tag_diary) if d0 else f"{t0}\n\n#{tag_diary}"
                await update_memo_content(p["memo_name"], new_content, cfg["memos_url"], cfg["memos_token"])
                if d0:
                    await set_display_time(p["memo_name"], d0, cfg["memos_url"], cfg["memos_token"])
                for e in rest:
                    d, t = e.get("date"), e["text"].strip()
                    if not d:
                        errors.append(f"#{p['id']}: 有一段没有日期，已跳过该段")
                        continue
                    await create_memo(_with_header(t, d, tag_diary),
                                      cfg["memos_url"], cfg["memos_token"], display_date=d)
            elif typ in ("note", "password"):
                # 只追加标签，绝不改写原文
                tags = [t.strip().lstrip("#") for t in (item.get("tags") or []) if t.strip()]
                # 统一加伞标签，重扫时据此跳过，避免二次整理（不依赖本地提议库）
                umbrella = "密码" if typ == "password" else "笔记"
                if umbrella not in tags:
                    tags.insert(0, umbrella)
                if not tags:
                    errors.append(f"#{p['id']}: 没有标签可打")
                    continue
                tag_line = " ".join(f"#{t}" for t in tags)
                new_content = f"{p['content'].rstrip()}\n\n{tag_line}"
                await update_memo_content(p["memo_name"], new_content, cfg["memos_url"], cfg["memos_token"])
            else:
                set_proposal_status(p["id"], "skipped")
                continue
            set_proposal_status(p["id"], "applied")
            applied.append(p["id"])
            all_terms.extend(item.get("terms") or [])
        except Exception as e:
            errors.append(f"#{p['id']}: {e}")

    if all_terms:
        glossary = cfg.get("glossary", "")
        existing = {line.strip() for line in glossary.splitlines() if line.strip()}
        new_terms = [t for t in dict.fromkeys(all_terms) if t not in existing]
        if new_terms:
            save_settings({"glossary": (glossary.strip() + "\n" if glossary.strip() else "") + "\n".join(new_terms)})

    return JSONResponse({"ok": not errors, "applied": len(applied),
                         "errors": errors, "terms_added": len(all_terms)})


@app.post("/api/organize/skip")
async def organize_skip(request: Request):
    body = await request.json()
    set_proposal_status(int(body.get("id", 0)), "skipped")
    return JSONResponse({"ok": True})


@app.post("/api/models")
async def models(request: Request):
    """拉取 LLM 接口的可用模型列表。优先用请求里带的地址和 Key（无需先保存）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = get_settings()
    url = (body.get("llm_url") or "").strip() or cfg.get("llm_url", "")
    key = (body.get("llm_api_key") or "").strip() or cfg.get("llm_api_key", "")
    if not url:
        return JSONResponse({"ok": False, "error": "请先填写 API 地址"}, status_code=400)
    try:
        ids = await list_models(url, key)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"拉取失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "models": ids})


