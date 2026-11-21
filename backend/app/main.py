"""FastAPI 入口：单端口同时提供 API 与界面。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import (access, agent, auth, auto_core, brain, chains, chat, config, core_bridge,
               delivery, docx_export, emb_bridge, facts, forms, guard, parsers, quality,
               rag, reqmatrix, reqtable, rules, samples, seed, settings, store, tender,
               blindcheck, bidbundle, readiness, scope, taskctl)
from . import generation_context, requirement_support

store.init_db()
seed.ensure_seed()
AUTH_INFO = auth.init_auth()
# C02：旧公共配置显式登记归属默认企业，不复制任何密钥给其他企业
scope.migrate_legacy()
store.tidy_all_orgs([o["id"] for o in auth.list_orgs() if o["id"] != auth.DEFAULT_ORG])


def _sweep_samples() -> None:
    """升级后整理一次：知识库里上传过的整本标书自动变成「范例」并拆好章。"""
    for oid in [auth.DEFAULT_ORG] + [o["id"] for o in auth.list_orgs()]:
        try:
            with store.use_org(oid):
                samples.sweep_existing()
        except Exception as exc:      # 单个企业库出错不影响启动
            print(f"[bidagent] 范例整理跳过 {oid}: {type(exc).__name__}: {exc}")


_sweep_samples()

app = FastAPI(title="TenderForge · LangChain 版", version="1.0")

COOKIE = "ba_session"
# 免登录：探活、登录注册本身；其余 /api 一律要求登录
PUBLIC_API = {"/api/health", "/api/auth/login", "/api/auth/register", "/api/auth/me"}

# 首页入口：/ 统一进新版 Vue 界面；旧外壳保留在 /index.html，需要时可直接访问
ENTRY_PAGE = "/vue/home.html"
LEGACY_PAGE = "/index.html"


def _token_of(request: Request) -> str:
    tk = request.cookies.get(COOKIE) or ""
    if not tk:
        head = request.headers.get("authorization") or ""
        if head.lower().startswith("bearer "):
            tk = head[7:].strip()
    return tk


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """所有接口先过登录；登录后把请求切到该企业的库，实现数据隔离。"""
    path = request.url.path
    if not path.startswith("/api/"):
        # 页面：没登录就先去登录页；静态资源本身不拦（拿不到它页面也渲染不出来）
        is_page = (path in ("/", LEGACY_PAGE)
                   or (path.startswith("/vue/") and path.endswith(".html")))
        if is_page:
            user = auth.resolve(_token_of(request))
            if not user:
                return RedirectResponse("/login.html", 302)
            if path == "/":
                # 默认落新版界面；旧外壳退到 /index.html 备用
                return RedirectResponse(ENTRY_PAGE, 302)
        return await call_next(request)
    if path in PUBLIC_API:
        return await call_next(request)
    user = auth.resolve(_token_of(request))
    if not user:
        return JSONResponse({"detail": "请先登录", "code": "unauthorized"}, status_code=401)
    tok = auth.set_current(user)
    try:
        with store.use_org(user["org_id"]):
            # C02：项目角色在这里统一校验；前端隐藏按钮不算权限
            ok, why = access.guard(path, request.method)
            if not ok:
                return JSONResponse({"detail": why, "code": "forbidden"},
                                    status_code=403)
            return await call_next(request)
    finally:
        auth.reset_current(tok)


@app.middleware("http")
async def no_cache_web_assets(request, call_next):
    """界面文件每次都取最新的：避免改了 CSS/JS 浏览器还用旧缓存，样式看起来像乱码。"""
    response = await call_next(request)
    p = request.url.path
    if p in ("/", "/index.html", "/style.css", "/app.js",
             "/login.html", "/login.css", "/login.js", "/manual.html") \
            or (p.startswith("/vue/") and p.endswith(".html")):
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/health")
def health() -> dict:
    """探活用，不暴露企业数量等内部信息。"""
    return {"ok": True, "app": "bidagent", "auth": "on"}


def _need_owner(what: str = "这个设置") -> dict:
    me = auth.current()
    if me.get("role") != "owner":
        raise HTTPException(403, f"{what}只有企业管理员能改")
    return me


def _need_project(pid: str) -> dict:
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


def _need_section(pid: str, sid: str) -> dict:
    """章节必须属于路径上的这个项目。

    不同项目各有一套章节 id，客户端把 A 的 pid 和 B 的 sid 拼在一起时，
    以前会照写 B 的正文（还会推高 B 的版本号），这里一律当「不存在」。
    """
    sec = store.get_section(sid)
    if not sec or str(sec.get("project_id") or "") != str(pid):
        raise HTTPException(404, "章节不存在或不属于该项目")
    return sec


def _require_outline_confirmed(pid: str) -> dict:
    """P1 目录确认闸门：目录没人工确认，不允许整本 / 单章写正文。

    老项目在升级补列时会把「已经写过正文」的项目自动视为已确认，
    所以线上存量项目不会被这次改动锁死。目录任何变动都会清空确认位。
    """
    p = _need_project(pid)
    if str(p.get("outline_confirmed_at") or "").strip():
        return p
    if not store.list_sections(pid):
        raise HTTPException(409, "还没有目录，请先生成并确认目录")
    raise HTTPException(409, "目录还没确认：先在目录区点「确认目录」，再写正文")


def _body(body: dict, key: str, default: Any = "") -> Any:
    v = body.get(key, default)
    return v if v is not None else default


def _start(kind: str, pid: str, fn, *args) -> dict:
    _need_project(pid)
    jid, busy = store.create_job_exclusive(kind, pid)
    if busy:
        # 连点两下 / 换页签再点：不起第二个任务，把正在跑的那个交回前端继续看
        return {"job_id": busy["id"], "status": busy["status"], "step": busy.get("step") or "",
                "dedup": True, "kind": busy.get("kind") or kind}
    store.submit_job(fn, pid, jid, *args)
    return {"job_id": jid, "status": "queued", "dedup": False}


KIND_LABEL = {"compose": "整本生成", "optimize": "优化改写",
              "section": "单章生成", "rewrite": "单章改写",
              "restore": "恢复快照", "import": "导入标书",
              "outline": "调整目录"}


def _refuse_if_busy(pid: str, what: str) -> None:
    """项目还有写任务在跑时，其它写操作一律排队，避免两条链路互相覆盖。"""
    busy = store.active_job(pid)
    if not busy:
        return
    label = KIND_LABEL.get(busy.get("kind"), "写任务")
    when = ("正在排队，还没开始" if busy.get("status") == "queued"
            else f"进行到 {int(busy.get('progress') or 0)}%")
    raise HTTPException(
        409, f"「{label}」{when}，为避免同一份正文被两处同时改写，{what}先等一下；"
             f"任务结束后再操作，或到任务卡里看进度。")


def _exclusive_write(pid: str, kind: str, what: str, fn, *args) -> dict:
    """同步写操作也占一个写任务：占用期间别的写进不来。

    正常完成、抛异常、乐观锁冲突都要释放（把状态改成 done / error），
    否则一次失败会让这个项目再也点不动任何写操作。
    """
    _refuse_if_busy(pid, what)
    jid, busy = store.create_job_exclusive(kind, pid)
    if busy:
        raise HTTPException(409, f"已有写任务正在进行，{what}请稍后再试")
    store.update_job(jid, status="running", progress=0, step=what)
    try:
        result = fn(*args)
        store.update_job(
            jid, status="done", progress=100, step="完成",
            result=result if isinstance(result, dict) else {"value": str(result)})
        return result
    except Exception as exc:
        store.update_job(jid, status="error", step="失败", error=str(exc)[:200])
        raise


# ---------------- 企业与账号 ----------------

ALLOW_SIGNUP = os.environ.get("BIDAGENT_ALLOW_SIGNUP", "0").strip() in ("1", "true", "yes")


def _set_cookie(resp, token: str, request: Request) -> None:
    resp.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", path="/",
                    secure=request.url.scheme == "https")


@app.post("/api/auth/login")
def auth_login(body: dict, request: Request) -> Response:
    try:
        r = auth.login(str(_body(body, "username", "")),
                       str(_body(body, "password", "")),
                       request.headers.get("user-agent") or "")
    except ValueError as exc:
        raise HTTPException(401, str(exc))
    resp = JSONResponse({"ok": True, "user": r["user"]})
    _set_cookie(resp, r["token"], request)
    return resp


@app.post("/api/auth/register")
def auth_register(body: dict, request: Request) -> Response:
    """自助开通企业：默认关闭，服务器上设 BIDAGENT_ALLOW_SIGNUP=1 才开。"""
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "本系统不开放自助注册，请让已有企业的管理员给你开账号")
    try:
        r = auth.create_org(str(_body(body, "org", "")),
                            str(_body(body, "username", "")),
                            str(_body(body, "password", "")),
                            str(_body(body, "name", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    store.init_org(r["org"]["id"])
    lr = auth.login(str(_body(body, "username", "")), str(_body(body, "password", "")))
    resp = JSONResponse({"ok": True, "user": lr["user"]})
    _set_cookie(resp, lr["token"], request)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> Response:
    auth.logout(_token_of(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    user = auth.resolve(_token_of(request))
    if not user:
        return {"ok": False, "user": None}
    return {"ok": True, "user": user, "signup_open": ALLOW_SIGNUP}


@app.post("/api/auth/password")
def auth_password(body: dict) -> dict:
    me = auth.current()
    old = str(_body(body, "old", ""))
    new = str(_body(body, "new", ""))
    u = auth.find_user(me.get("username") or "")
    if not u or not auth.verify_password(old, u["pass_hash"]):
        raise HTTPException(400, "原密码不对")
    try:
        auth.set_password(me["id"], new)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/org")
def org_info() -> dict:
    me = auth.current()
    org = auth.get_org(me.get("org_id") or "")
    return {
        "org": {"id": org.get("id"), "name": org.get("name"),
                "created_at": org.get("created_at")},
        "me": me,
        "members": auth.list_users(me.get("org_id") or ""),
        "stats": {"members": auth.count_users(me.get("org_id") or ""),
                  "projects": len(store.list_projects(include_archived=True)),
                  "kb": len(store.list_docs(scope="kb"))},
    }


@app.put("/api/org")
def org_rename(body: dict) -> dict:
    _need_owner("企业名称")
    try:
        return auth.rename_org(auth.org_id(), str(_body(body, "name", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/org/users")
def org_add_user(body: dict) -> dict:
    _need_owner("成员账号")
    try:
        u = auth.create_user(auth.org_id(),
                             str(_body(body, "username", "")),
                             str(_body(body, "password", "")),
                             str(_body(body, "name", "")),
                             str(_body(body, "role", "member") or "member"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "user": u}


@app.put("/api/org/users/{uid}")
def org_update_user(uid: str, body: dict) -> dict:
    me = _need_owner("成员账号")
    u = auth.get_user_by_id(uid)
    if not u or u["org_id"] != me["org_id"]:
        raise HTTPException(404, "这个成员不在你的企业里")
    try:
        if _body(body, "role", None):
            u = auth.set_user_role(uid, str(body["role"]))
        if _body(body, "status", None):
            u = auth.set_user_status(uid, str(body["status"]))
        if _body(body, "password", None):
            auth.set_password(uid, str(body["password"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "user": u}


@app.delete("/api/org/users/{uid}")
def org_del_user(uid: str) -> dict:
    me = _need_owner("成员账号")
    u = auth.get_user_by_id(uid)
    if not u or u["org_id"] != me["org_id"]:
        raise HTTPException(404, "这个成员不在你的企业里")
    if uid == me["id"]:
        raise HTTPException(400, "不能删除自己")
    try:
        auth.delete_user(uid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ---------------- 状态与模式 ----------------

@app.get("/api/state")
def state() -> dict:
    cfg = config.llm_config()
    reg = config.registry_public()
    health = config.model_health()
    active = next((m for m in reg["models"] if m["id"] == reg["active"]), None)
    if config.MODE == "live":
        model_name = (f"{active['label']} · {active['model']}" if active
                      else "未选择模型")
    else:
        model_name = "内置演示模型（无需外网）"
    me = auth.current()
    # /api/state 是每次切项目、每次刷新都要拉的接口。list_projects 是 SELECT *，
    # 会把每本几万字的招标全文、解读和章节一起下发（实测单个项目 79KB）。
    # 侧栏与项目选择只需要这几个摘要字段，正文一律走 /api/projects/{pid} 按需取。
    state_fields = ("id", "name", "created_at", "updated_at", "archived_at")
    return {
        "mode": config.MODE,
        "me": me,
        "is_owner": me.get("role") == "owner",
        "is_platform_admin": auth.is_platform_admin(),
        "llm_ready": cfg.ready,
        "model": model_name,
        "model_health": health,
        "model_usable": health.get("status") != "error",
        "models": {
            "active": reg["active"], "count": len(reg["models"]),
            "roles": reg["roles"],
            "list": [{k: m[k] for k in ("id", "label", "model", "ready", "provider")}
                     for m in reg["models"]],
        },
        "projects": sorted(
            ({k: p.get(k) for k in state_fields}
             for p in store.list_project_summaries()),
            key=lambda p: (0 if "演示" in (p.get("name") or "") else 1,
                           p.get("created_at") or "")),
        "archived_count": sum(
            1 for p in store.list_project_summaries(include_archived=True)
            if (p.get("archived_at") or "").strip()),
        "kb_count": len(store.list_docs(scope="kb")),
        "auto_enabled": auto_core.enabled(),
        "privacy": guard.status(),
        "config_scope": scope.status(),
        "chains": list(chains.PROMPTS.keys()),
        "framework": "langchain-core LCEL · ChatPromptTemplate | Model | Parser",
        "brain": {**{k: v for k, v in brain.catalog().items() if k != "tools"},
                  "planner": brain.planner_label()},
    }


@app.post("/api/mode")
def set_mode(body: dict) -> dict:
    _need_owner("运行方式")
    mode = str(_body(body, "mode", "demo"))
    if mode not in ("demo", "live"):
        raise HTTPException(400, "mode 只能是 demo 或 live")
    if mode == "live" and not config.llm_config().ready:
        raise HTTPException(400, "未检测到可用的模型配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）")
    config.set_mode(mode)
    return {"mode": config.MODE, "degraded": mode == "live" and not config.outbound_allowed()}


# ---------------- 数据出域 ----------------

@app.get("/api/privacy")
def privacy_get() -> dict:
    return guard.status()


@app.post("/api/privacy")
def privacy_set(body: dict) -> dict:
    _need_owner("外发设置")
    config.set_privacy(body)
    # 关闭外发时同步回演示模式，界面状态与实际行为保持一致
    if not config.outbound_allowed():
        config.set_mode("demo")
    return guard.status()


# ---------------- 向量检索（按企业开关，默认关闭） ----------------

@app.get("/api/embedding")
def embedding_get() -> dict:
    """脱敏配置 + 是否可用 + 向量覆盖率；只读，不建表、不发请求、不产生费用。"""
    return emb_bridge.status()


@app.post("/api/embedding")
def embedding_save(body: dict) -> dict:
    _need_owner("向量检索设置")
    if not isinstance(body, dict):
        raise HTTPException(400, "配置格式不对")
    try:
        return emb_bridge.save(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/embedding/test")
def embedding_test() -> dict:
    """只发一条极短文本测通，不触碰任何业务资料。"""
    _need_owner("向量检索设置")
    return emb_bridge.test_connection()


# ---------------- 多模型接入 ----------------

@app.get("/api/models")
def models_list() -> dict:
    """返回脱敏后的模型注册表；密钥不出后端。"""
    return config.registry_public()


@app.post("/api/models")
def models_save(body: dict) -> dict:
    _need_owner("模型配置")
    try:
        r = config.upsert_model(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return r


@app.post("/api/models/active")
def models_active(body: dict) -> dict:
    _need_owner("模型配置")
    """切换当前模型，并自动进入真实模型模式。"""
    try:
        r = config.set_active(str(_body(body, "id", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if r["active"]:
        config.set_mode("live")
    r["mode"] = config.MODE
    return r


@app.post("/api/models/role")
def models_role(body: dict) -> dict:
    """为某个环节单独指定模型；id 留空 = 跟随当前模型。"""
    _need_owner("模型配置")
    try:
        return config.set_role(str(_body(body, "role", "")), str(_body(body, "id", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/models/{mid}/test")
def models_test(mid: str) -> dict:
    _need_owner("模型配置")
    try:
        guard.require_outbound("", "model.test")
        return config.test_model(mid)
    except guard.OutboundBlocked as exc:
        return {"ok": False, "latency_ms": 0, "error": str(exc)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/models/{mid}")
def models_delete(mid: str) -> dict:
    _need_owner("模型配置")
    try:
        return config.delete_model(mid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------------- 项目 ----------------

@app.post("/api/projects")
def new_project(body: dict) -> dict:
    name = str(_body(body, "name", "")).strip() or "未命名投标项目"
    return store.create_project(name, str(_body(body, "tender_text", "")))


@app.get("/api/projects")
def projects(scope: str = "all", q: str = "") -> dict:
    """项目总览：全部项目（含已归档）+ 每本的进度，供「全部项目」页和侧栏用。

    scope: all / active（未归档）/ archived；q 按项目名模糊搜。
    """
    rows = store.list_projects(include_archived=True)
    stats = store.project_stats()
    collected = store.kb_source_pids()
    kw = (q or "").strip().lower()
    items = []
    for p in rows:
        st = stats.get(p["id"], {})
        n = st.get("sections", 0)
        opened = st.get("open_chapters", 0)
        arch = bool((p.get("archived_at") or "").strip())
        item = {
            "id": p["id"], "name": p.get("name") or "",
            "created_at": p.get("created_at") or "",
            "updated_at": p.get("updated_at") or p.get("created_at") or "",
            "archived": arch, "archived_at": p.get("archived_at") or "",
            "sections": n, "written": st.get("written", 0),
            "chars": st.get("chars", 0),
            # 没有章节 = 刚开始；有没写的章 = 进行中；全写了 = 正文已填写
            "state": "empty" if not n else ("done" if opened == 0 else "doing"),
            # 文字齐了只说明正文已填写，质量 / 要求 / 附件 / 人工复核另有状态，
            # 不能让项目列表显示成「已可交付」
            "state_label": ("尚未开始" if not n else
                            "正文已填写" if opened == 0 else "进行中"),
            "deliverable": False,
            # 已经收进过知识库的，按钮变成「更新知识库」
            "kb_doc_id": collected.get(p["id"], ""),
        }
        if scope == "active" and arch:
            continue
        if scope == "archived" and not arch:
            continue
        if kw and kw not in item["name"].lower():
            continue
        items.append(item)
    return {"items": items, "counts": {
        "all": len(rows),
        "active": sum(1 for p in rows if not (p.get("archived_at") or "").strip()),
        "archived": sum(1 for p in rows if (p.get("archived_at") or "").strip()),
    }}


def _manuscript(p: dict) -> tuple[str, list[str]]:
    """把章节拼成一本连续的稿子：标题单独一行，拆章规则认得出；空章单独列出来。"""
    parts, empty = [], []
    for s in p.get("sections") or []:
        title = (s.get("title") or "").strip()
        text = (s.get("text") or "").strip()
        if not text:
            empty.append(title or "（未命名章节）")
            continue
        parts.append(f"{title}\n{text}" if title else text)
    return "\n\n".join(parts), empty


@app.post("/api/projects/{pid}/to-kb")
def project_to_kb(pid: str, body: dict) -> dict:
    """把写完的标书整本收进知识库当范例，以后写别的项目可以照着结构写。

    同一本书重复收录会更新原条目，不会在知识库里堆一串同名范例。
    """
    p = _need_project(pid)
    text, empty = _manuscript(p)
    if not text.strip():
        raise HTTPException(400, "这本还没有正文，先写完或导入正文再收进知识库")
    # 收录的是哪一版、有没有核验：内容指纹 + 状态，未完成的稿不能当已审核成稿
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    verification = "incomplete" if empty else "unreviewed"
    kind = str(_body(body, "kind", "sample") or "sample")
    if kind not in store.DOC_KINDS:
        kind = "sample"
    title = str(_body(body, "title", "") or "").strip() or f"{p['name']}（成稿）"
    old = store.find_doc_by_source(pid)
    if old:
        doc = store.update_doc(old["id"], title=title, content=text, kind=kind,
                               source_hash=source_hash, verification=verification) or old
        did = old["id"]
    else:
        doc = store.add_doc(title, text, scope="kb", kind=kind, source_pid=pid,
                            source_hash=source_hash, verification=verification)
        did = doc["id"]
    n = samples.ingest(did, text) if kind == "sample" else 0
    return {"ok": True, "doc_id": did, "title": title, "kind": kind,
            "chapters": n, "updated": bool(old), "empty": empty,
            "source_hash": source_hash, "verification": verification,
            "verification_label": ("未完成稿 · 有章节无正文，需补齐后再核验" if empty
                                   else "未核验 · 收录仅保存正文，不代表质量与响应已通过"),
            "chars": len(text)}


@app.post("/api/projects/{pid}/archive")
def project_archive(pid: str, body: dict) -> dict:
    """归档：从侧栏收起来，数据一条不动，随时能取消归档。"""
    _need_project(pid)
    on = bool(_body(body, "on", True))
    if on and store.active_job(pid):
        raise HTTPException(409, "这本还在生成中，等它跑完再归档")
    if not store.archive_project(pid, on):
        raise HTTPException(404, "项目不存在")
    return {"ok": True, "archived": on}


@app.delete("/api/projects/{pid}")
def project_delete(pid: str) -> dict:
    """删除项目：正文、资料、检查记录、导出文件一起删，删完不能恢复。"""
    _need_owner("删除项目")
    p = _need_project(pid)
    busy = store.active_job(pid)
    if busy:
        raise HTTPException(409, "这本还在生成中，等它跑完再删")
    arts = store.list_artifacts(pid)
    out = store.delete_project(pid)
    # 先删记录再删文件：文件删失败最多留个孤儿文件，不会出现「记录还在、文件没了」
    for a in arts:
        try:
            Path(a.get("path") or "").unlink(missing_ok=True)
        except OSError:
            pass
    # C02：只清本企业的产物目录。旧布局的平铺文件归属默认企业，
    # 只有默认企业自己才能扫根目录，别家企业删项目时不碰别人的文件。
    exp_root = config.DATA_DIR / "exports"
    shutil.rmtree(exp_root / scope.org() / pid, ignore_errors=True)
    if scope.is_platform():
        for f in exp_root.glob(f"{pid}_*"):
            try:
                f.unlink()
            except OSError:
                pass
    try:
        auto_core.invalidate_cache()
    except Exception:
        pass
    return {"ok": True, "deleted": out}


@app.get("/api/projects/{pid}")
def project(pid: str) -> dict:
    p = _need_project(pid)
    try:
        import json
        p["summary_parsed"] = json.loads(p.get("summary") or "{}")
    except (ValueError, TypeError):
        p["summary_parsed"] = {"summary": p.get("summary") or ""}
    p["evidence"] = store.list_evidence(pid)
    # 当前参照的范例书名，正文页要显示「参照哪一本」
    sid = (p.get("sample_doc_id") or "").strip()
    p["sample_title"] = (store.get_doc(sid) or {}).get("title", "") if sid else ""
    return p


@app.post("/api/projects/{pid}/tender")
def set_tender(pid: str, body: dict) -> dict:
    """粘贴整份招标文件按「整份替换」入版本链，保留历史版本与失效要求。"""
    _need_project(pid)
    text = str(_body(body, "text", "") or "")
    kind = str(_body(body, "kind", "") or "")
    if kind not in store.TENDER_KINDS:
        kind = "replace" if store.list_tender_versions(pid) else "original"
    # 新版前端上传时会带回解析元数据（扫描页、OCR、待核对范围），
    # 老调用不传该字段，行为保持不变。
    r = tender.ingest(pid, kind, text,
                      filename=str(_body(body, "filename", "") or "粘贴的招标文件"),
                      parse=_body(body, "parse", None) or None)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "保存失败")
    auto = auto_core.run_stage("tender", pid, force=True)
    return {"ok": True, "auto": auto_core.as_card(auto),
            "requirements": r.get("requirements") or {},
            "version": r.get("version"), "retired_requirements": r.get("retired_requirements"),
            "stale_confirmations": r.get("stale_confirmations")}


# ---------------- 招标要求表（人工修订优先） ----------------

@app.get("/api/projects/{pid}/requirements")
def requirements_list(pid: str) -> dict:
    _need_project(pid)
    return {"rows": reqtable.list_rows(pid), **reqtable.summary(pid)}


@app.post("/api/projects/{pid}/requirements")
def requirements_add(pid: str, body: dict) -> dict:
    _need_project(pid)
    try:
        r = reqtable.add_row(pid, str(_body(body, "kind", "hard")),
                             str(_body(body, "text", "")),
                             str(_body(body, "score", "")),
                             str(_body(body, "note", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    auto_core.invalidate_cache()
    return {**r, **reqtable.summary(pid)}


@app.put("/api/projects/{pid}/requirements/{rid}")
def requirements_update(pid: str, rid: str, body: dict) -> dict:
    _need_project(pid)
    r = reqtable.update_row(pid, rid, body or {})
    if not r.get("ok"):
        raise HTTPException(400, "要求不存在或没有可修改字段")
    auto_core.invalidate_cache()
    return {**r, **reqtable.summary(pid)}


@app.delete("/api/projects/{pid}/requirements/{rid}")
def requirements_delete(pid: str, rid: str) -> dict:
    """删一条具体要求（小项）；传分类标识时等同删整组（大项）。"""
    _need_project(pid)
    r = reqtable.delete_row(pid, rid)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "要求不存在或已被删除")
    auto_core.invalidate_cache()
    return {**r, **reqtable.summary(pid)}


@app.delete("/api/projects/{pid}/requirements/group/{kind}")
def requirements_delete_group(pid: str, kind: str) -> dict:
    """删一个大项：整组要求一起删，分组本身不再显示内容。"""
    _need_project(pid)
    r = reqtable.delete_group(pid, kind)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "没有这个分类")
    auto_core.invalidate_cache()
    return {**r, **reqtable.summary(pid)}


@app.post("/api/projects/{pid}/requirements/sync")
def requirements_sync(pid: str, body: dict | None = None) -> dict:
    """从当前招标文件重抽；人工新增 / 改过 / 停用 / 删过的行不会被覆盖。

    传 {"restore": true} 可恢复此前被整行删除的自动要求。
    """
    _need_project(pid)
    r = reqtable.sync(pid, restore=bool((body or {}).get("restore")))
    auto_core.invalidate_cache()
    return {"ok": True, **r, **reqtable.summary(pid)}


# ---------------- B01：逐条响应矩阵 ----------------

@app.get("/api/projects/{pid}/reqmatrix")
def reqmatrix_get(pid: str) -> dict:
    _need_project(pid)
    return reqmatrix.build(pid)


@app.post("/api/projects/{pid}/reqmatrix/{rid}/confirm")
def reqmatrix_confirm(pid: str, rid: str, body: dict | None = None) -> dict:
    _need_project(pid)
    r = reqmatrix.confirm(pid, rid,
                          reviewer=str(auth.current().get("id") or auth.current().get("username") or "人工复核"),
                          note=str(_body(body or {}, "note", "") or ""))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "确认失败")
    auto_core.invalidate_cache()
    return {**r, **reqmatrix.build(pid)}


@app.post("/api/projects/{pid}/reqmatrix/{rid}/na")
def reqmatrix_na(pid: str, rid: str, body: dict | None = None) -> dict:
    _need_project(pid)
    reason = str(_body(body or {}, "reason", "") or "")
    r = reqmatrix.set_na(pid, rid, reason,
                         reviewer=str(auth.current().get("id") or auth.current().get("username") or "人工复核"))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "标记失败")
    auto_core.invalidate_cache()
    return {**r, **reqmatrix.build(pid)}


@app.get("/api/projects/{pid}/reqmatrix/{rid}/support")
def requirement_support_get(pid: str, rid: str):
    _need_project(pid)
    try:
        return {"links": requirement_support.list_links(pid,rid),
                "policy": requirement_support.policy(pid,rid),
                "types": requirement_support.LABELS,
                "facts": store.list_facts(pid), "attachments": store.list_attachments(pid),
                "evidence": [r for r in store.list_evidence(pid)
                             if r["status"] not in ("historical","review","missing")]}
    except ValueError as exc:
        raise HTTPException(404,str(exc))

@app.post("/api/projects/{pid}/reqmatrix/support-candidates")
def requirement_support_candidates(pid: str):
    _need_project(pid)
    count = 0
    for req in store.list_requirements(pid):
        if req.get("status") == "active":
            count += len(requirement_support.suggest_links(pid,req["id"]))
    auto_core.invalidate_cache()
    return {"ok": True,"created":count}


@app.put("/api/projects/{pid}/reqmatrix/{rid}/support-policy")
def requirement_support_policy(pid: str, rid: str, body: dict):
    _need_project(pid)
    try:
        result = requirement_support.set_policy(pid,rid,body,
            str(auth.current().get("id") or "人工复核"))
        auto_core.invalidate_cache()
        return {"ok": True, "policy": result}
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.post("/api/projects/{pid}/reqmatrix/{rid}/support")
def requirement_support_add(pid: str, rid: str, body: dict):
    _need_project(pid)
    try:
        result = requirement_support.add_link(pid,rid,body,
            str(auth.current().get("id") or "人工复核"))
        auto_core.invalidate_cache()
        return {"ok": True, "link": result}
    except (ValueError,OSError) as exc:
        raise HTTPException(400,str(exc))


@app.delete("/api/projects/{pid}/reqmatrix/{rid}/support/{lid}")
def requirement_support_delete(pid: str, rid: str, lid: str):
    _need_project(pid)
    ok = requirement_support.delete_link(pid,rid,lid)
    if not ok:
        raise HTTPException(404,"关联不存在")
    auto_core.invalidate_cache()
    return {"ok": True}


@app.get("/api/projects/{pid}/sections/{sid}/generation-runs")
def section_generation_runs(pid: str, sid: str):
    _need_section(pid,sid)
    sec = store.get_section(sid)
    rows = generation_context.list_runs(pid,sid)
    for row in rows:
        row["current"] = sec.get("generation_run_id") == row["id"]
        for source in row["sources"]:
            did = source.get("doc_id")
            doc = store.get_doc(did) if did and did != "tender" else None
            now_text = (store.get_project(pid).get("tender_text","") if did == "tender"
                        else doc.get("content","") if doc else "")
            source["source_changed"] = bool(did) and generation_context.digest(now_text) != source.get("source_hash")
    return {"rows": rows, "current_run_id": sec.get("generation_run_id",""),
            "source_unknown": not sec.get("generation_run_id")}


# ---------------- B04：招标文件 / 补遗 / 澄清版本 ----------------

@app.get("/api/projects/{pid}/tender-versions")
def tender_versions_list(pid: str) -> dict:
    _need_project(pid)
    return tender.summary(pid)


@app.post("/api/projects/{pid}/tender-versions")
def tender_versions_add(pid: str, body: dict) -> dict:
    """原文件 / 补遗 / 澄清 / 整份替换：保留版本链与有效顺序。

    补遗与澄清只追加受影响条款；旧自动要求转 superseded，人工修订标待复核；
    条款变化会让导出确认失效（内容指纹已含 tender_text 与要求表）。
    """
    _need_project(pid)
    kind = str(_body(body, "kind", "original") or "original")
    if kind not in store.TENDER_KINDS:
        raise HTTPException(400, "版本类型只能是 original / amend / clarify / replace")
    r = tender.ingest(pid, kind, str(_body(body, "text", "") or ""),
                      filename=str(_body(body, "filename", "") or "")[:120],
                      note=str(_body(body, "note", "") or "")[:300])
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "入库失败")
    return r


@app.post("/api/projects/{pid}/interpret")
def do_interpret(pid: str) -> dict:
    _need_project(pid)
    return agent.interpret(pid)


@app.post("/api/projects/{pid}/outline")
def do_outline(pid: str) -> dict:
    _need_project(pid)
    chapters = agent.make_outline(pid)
    return {"chapters": chapters, "plan": agent.outline_plan_for(pid)}


@app.get("/api/projects/{pid}/outline/plan")
def get_outline_plan(pid: str) -> dict:
    """目录计划留痕：刷新页面后仍能提示未匹配评分项与篇幅预算，不必重新生成目录。

    只读，取自进程内计划或 auto_runs 留痕；没有留痕时返回空对象，前端自行隐藏体检卡。
    """
    _need_project(pid)
    try:
        return agent.outline_plan_for(pid) or {}
    except Exception:
        return {}


@app.post("/api/projects/{pid}/outline/confirm")
def outline_confirm(pid: str, body: dict | None = None) -> dict:
    """人工确认当前目录；确认之后才允许写正文。目录一改就自动失效。"""
    _need_project(pid)
    by = str((auth.current() or {}).get("username") or "")
    r = store.confirm_outline(pid, by=by)
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail") or "确认失败")
    auto_core.invalidate_cache()
    return r


@app.get("/api/outlines")
def outline_memory(pid: str = "") -> dict:
    """项目记忆：历史上用过的目录。新项目可以一键沿用，只搬标题、不搬正文。"""
    items = []
    for p in store.list_projects()[:24]:
        if p["id"] == pid:
            continue
        rows = store.list_sections(p["id"])
        if not rows:
            continue
        items.append({
            "pid": p["id"], "name": p["name"],
            "created_at": p.get("created_at", ""),
            "count": len(rows),
            "written": sum(1 for r in rows if (r.get("text") or "").strip()),
            "titles": [r["title"] for r in rows],
        })
        if len(items) >= 6:
            break
    return {"items": items}


@app.post("/api/projects/{pid}/outline/apply")
def outline_apply(pid: str, body: dict) -> dict:
    """按给定标题重建目录。只保留同名章节的正文，绝不按位置把旧正文贴到新标题下。"""
    _need_project(pid)
    raw = body.get("titles") or []
    titles = [str(t).strip()[:60] for t in raw if str(t).strip()]
    if not titles:
        raise HTTPException(400, "没有可用的章节标题")
    if len(titles) > 80:
        raise HTTPException(400, "章节太多（最多 80 章）")
    old_ids = [s["id"] for s in store.list_sections(pid)]
    if old_ids:
        # 换目录会整批重建章节，普通按 id 的快照救不回来，这里存整份目录
        store.snapshot_outline(pid, store.new_id(), "套用旧目录前")
    store.apply_outline_titles(pid, titles)
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles])
    auto_core.invalidate_cache()
    return {"ok": True, "count": len(titles), "titles": titles}


@app.post("/api/projects/{pid}/outline/from-sample")
def outline_from_sample(pid: str, body: dict) -> dict:
    """按范例标书的目录建目录：只搬章节标题，正文留空，等逐章生成。"""
    _need_project(pid)
    did = str(_body(body, "doc_id", "")).strip()
    doc = _need_sample_doc(did)
    titles = samples.outline_titles(did)
    if not titles:
        raise HTTPException(400, "这本范例里没识别出章节标题，先点「编辑」把标题补上")
    old_ids = [s["id"] for s in store.list_sections(pid)]
    if old_ids:
        store.snapshot_outline(pid, store.new_id(), "按范例目录重建前")
    store.apply_outline_titles(pid, titles)
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles],
                         sample_doc_id=did)
    auto_core.invalidate_cache()
    return {"ok": True, "count": len(titles), "titles": titles,
            "verification": str(doc.get("verification") or "unreviewed"),
            "note": _sample_note(doc)}


@app.post("/api/projects/{pid}/sections")
def section_add(pid: str, body: dict) -> dict:
    """手动加一章。只插一行，已有章节的 id / 正文 / 分工都不动。"""
    _need_project(pid)
    r = _exclusive_write(pid, "outline", "新增章节", store.add_section, pid,
                         str(_body(body, "title", "") or ""),
                         str(_body(body, "after", "") or ""))
    if not r.get("ok"):
        raise HTTPException(404 if r.get("error") == "not_in_project" else 400,
                            r.get("detail") or "新增章节失败")
    auto_core.invalidate_cache()
    return r


@app.put("/api/projects/{pid}/sections/{sid}")
def section_rename(pid: str, sid: str, body: dict) -> dict:
    """改章节名：正文、版本、依据、分工跟同一 id 走，不等于删掉重建。"""
    _need_project(pid)
    r = _exclusive_write(pid, "outline", "改章节名", store.rename_section, pid, sid,
                         str(_body(body, "title", "") or ""))
    if not r.get("ok"):
        raise HTTPException(404 if r.get("error") == "not_in_project" else 400,
                            r.get("detail") or "改名失败")
    auto_core.invalidate_cache()
    return r


@app.delete("/api/projects/{pid}/sections/{sid}")
def section_delete(pid: str, sid: str) -> dict:
    """删一章：先留整份目录快照，可在「版本与回退」撤销。"""
    _need_project(pid)
    r = _exclusive_write(pid, "outline", "删除章节", store.delete_section, pid, sid)
    if not r.get("ok"):
        raise HTTPException(404 if r.get("error") == "not_in_project" else 400,
                            r.get("detail") or "删除章节失败")
    auto_core.invalidate_cache()
    return r


@app.post("/api/projects/{pid}/sections/{sid}/move")
def section_move(pid: str, sid: str, body: dict) -> dict:
    """上移 / 下移一章。只换顺序，不动 id、正文与依据；同时让目录确认失效。"""
    _need_project(pid)
    _need_section(pid, sid)
    try:
        direction = int(_body(body, "direction", 0) or 0)
    except (TypeError, ValueError):
        direction = 0
    if direction not in (-1, 1):
        raise HTTPException(400, "direction 只能是 -1（上移）或 1（下移）")
    r = _exclusive_write(pid, "outline", "调整章节顺序", store.move_section,
                         pid, sid, direction)
    if not r.get("ok"):
        raise HTTPException(404 if r.get("error") == "not_in_project" else 400,
                            r.get("detail") or "移动失败")
    auto_core.invalidate_cache()
    return r


@app.post("/api/projects/{pid}/sample")
def set_sample(pid: str, body: dict) -> dict:
    """指定本项目参照哪一本范例（写每一章时优先取它的同名章节）。"""
    _need_project(pid)
    did = str(_body(body, "doc_id", "")).strip()
    note = ""
    if did:
        note = _sample_note(_need_sample_doc(did))
    store.update_project(pid, sample_doc_id=did)
    return {"ok": True, "sample_doc_id": did, "note": note}


@app.post("/api/projects/{pid}/import")
def import_draft(pid: str, body: dict) -> dict:
    _need_project(pid)
    text = str(_body(body, "text", ""))

    def _do() -> dict:
        result = agent.import_draft(pid, text)
        if "error" in result:
            raise HTTPException(400, result["error"])
        result["auto"] = auto_core.as_card(auto_core.run_stage("draft", pid, force=True))
        return result

    return _exclusive_write(pid, "import", "导入标书", _do)


# ---------------- 资料与知识库 ----------------

def _need_kb_doc(did: str, msg: str = "素材不存在") -> dict:
    """知识库接口只碰知识库条目，不碰项目资料。

    项目资料（scope='project'）走 /api/projects/{pid}/materials，受项目成员
    权限约束；不能拿它的 id 从 /api/kb/{did} 绕过项目边界读、改、删。
    """
    d = store.get_doc(did)
    if not d or d.get("scope") != "kb":
        raise HTTPException(404, msg)
    return d


def _need_sample_doc(did: str) -> dict:
    """按范例建目录 / 指定参照范例：必须是本企业知识库里的整本标书。"""
    d = _need_kb_doc(did, "标书范例不存在")
    if d.get("kind") != "sample":
        raise HTTPException(400, "这是通用素材，不是整本标书范例")
    return d


def _sample_note(doc: dict) -> str:
    """范例收录时的核验状态：只如实提示，不冒充已审核成稿。"""
    state = str(doc.get("verification") or "unreviewed")
    return ("这本范例收录时还有空章，仅作结构参考" if state == "incomplete" else
            "这本范例尚未人工核验，仅作结构参考" if state != "confirmed" else "")


@app.get("/api/kb")
def kb_list(q: str = "", kind: str = "") -> dict:
    all_docs = store.list_docs(scope="kb")
    # 范例排前面：写标书时先看范例，素材是配角
    all_docs.sort(key=lambda d: (0 if d.get("kind") == "sample" else 1, d.get("created_at") or ""))
    counts = store.chapter_counts()
    for d in all_docs:
        d["n_top"] = counts.get(d["id"], {}).get("top", 0)
    docs = [d for d in all_docs if not kind or d.get("kind") == kind]
    kw = (q or "").strip().lower()
    if kw:
        docs = [d for d in docs
                if kw in (d.get("title") or "").lower() or kw in (d.get("content") or "").lower()]
    return {"docs": docs, "total": len(docs),
            "chars": sum(len(d.get("content") or "") for d in docs),
            "counts": {"all": len(all_docs),
                       "material": sum(1 for d in all_docs if d.get("kind") != "sample"),
                       "sample": sum(1 for d in all_docs if d.get("kind") == "sample")}}


@app.get("/api/kb/samples")
def kb_samples() -> dict:
    """范例清单（不含正文）：正文页「参照哪一本」和空项目起点用它，省流量。"""
    items = store.list_doc_meta(scope="kb", kind="sample")
    counts = store.chapter_counts()
    for it in items:
        it["n_top"] = counts.get(it["id"], {}).get("top", 0)
        it.pop("created_at", None)
    return {"items": items}


@app.get("/api/kb/{did}/chapters")
def kb_chapters(did: str, full: int = 0) -> dict:
    """范例的章节目录；full=1 时连每章正文一起返回。"""
    d = _need_kb_doc(did, "范例不存在")
    chapters = store.list_chapters(did, with_content=bool(full))
    if not chapters and (d.get("content") or "").strip():
        # 老数据没拆过章：现场拆一次并入库，之后不再重复算
        n = samples.ingest(did, d.get("content") or "")
        chapters = store.list_chapters(did, with_content=bool(full))
    for ch in chapters:
        ch.pop("created_at", None)
        if full:
            ch["chars"] = len(ch.get("content") or "")
    return {"id": did, "title": d.get("title") or "", "kind": d.get("kind") or "material",
            "count": len(chapters), "chapters": chapters}


@app.get("/api/kb/{did}")
def kb_get(did: str) -> dict:
    return _need_kb_doc(did)


@app.put("/api/kb/{did}")
def kb_update(did: str, body: dict) -> dict:
    _need_kb_doc(did)
    title = _body(body, "title", None)
    content = _body(body, "content", None)
    if title is not None:
        title = str(title).strip() or "未命名素材"
    kind = _body(body, "kind", None)
    valid_until = _body(body, "valid_until", None)
    out = store.update_doc(did, title=title, content=None if content is None else str(content),
                           kind=kind,
                           valid_until=None if valid_until is None else str(valid_until))
    # 改成范例、或改了范例正文：重新按章拆一遍，写作参考才跟着变
    if out and out.get("kind") == "sample" and (content is not None or kind == "sample"):
        samples.ingest(did, out.get("content") or "")
        out = store.get_doc(did) or out
    return out or {}


@app.delete("/api/kb/{did}")
def kb_delete(did: str) -> dict:
    _need_kb_doc(did)
    store.delete_doc(did)
    return {"ok": True}


@app.post("/api/kb")
def kb_add(body: dict) -> dict:
    title = str(_body(body, "title", "")).strip() or "未命名素材"
    pm = _body(body, "page_map", None) or []
    text = str(_body(body, "content", ""))
    kind = str(_body(body, "kind", "") or "")
    if not kind:
        kind = "sample" if samples.looks_like_bid(text) else "material"
    doc = store.add_doc(title, text, scope="kb", page_map=pm, kind=kind,
                        valid_until=str(_body(body, "valid_until", "") or ""))
    if kind == "sample":
        samples.ingest(doc["id"], text)
        doc = store.get_doc(doc["id"]) or doc
    return doc


@app.post("/api/projects/{pid}/materials")
def material_add(pid: str, body: dict) -> dict:
    _need_project(pid)
    title = str(_body(body, "title", "")).strip() or "未命名资料"
    pm = _body(body, "page_map", None) or []
    store.save_pages(pid, "material", title, pm)
    return store.add_doc(title, str(_body(body, "content", "")),
                         scope="project", project_id=pid, page_map=pm,
                         valid_until=str(_body(body, "valid_until", "") or ""))


@app.get("/api/projects/{pid}/certificates")
def certificates_scan(pid: str) -> dict:
    """只读：本项目能看到的证照有效期（企业知识库 + 项目资料 + 企业事实）。"""
    from . import certificates
    _need_project(pid)
    return certificates.scan(pid)


# ---------------- P2：企业资产台账（企业级，随企业分库隔离） ----------------

@app.get("/api/projects/{pid}/assets")
def assets_list(pid: str) -> dict:
    """只读：五分类资产与有效期五态。借项目路径做权限校验，资产本身跨项目复用。"""
    from . import assets
    _need_project(pid)
    return assets.scan()


@app.post("/api/projects/{pid}/assets")
def assets_add(pid: str, body: dict) -> dict:
    from . import assets
    _need_project(pid)
    clean, err = assets.validate(body or {})
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "item": store.add_asset(**clean)}


@app.put("/api/projects/{pid}/assets/{aid}")
def assets_update(pid: str, aid: str, body: dict) -> dict:
    from . import assets
    _need_project(pid)
    old = store.get_asset(aid)
    if not old:
        raise HTTPException(404, "资产不存在")
    clean, err = assets.validate({**old, **(body or {})})
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "item": store.update_asset(aid, **clean)}


@app.delete("/api/projects/{pid}/assets/{aid}")
def assets_delete(pid: str, aid: str) -> dict:
    _need_project(pid)
    if not store.delete_asset(aid):
        raise HTTPException(404, "资产不存在")
    return {"ok": True}


@app.post("/api/projects/{pid}/assets/{aid}/verify")
def assets_verify(pid: str, aid: str, body: dict | None = None) -> dict:
    _need_project(pid)
    by = str((auth.current() or {}).get("username") or "人工核验")
    row = store.verify_asset(aid, by)
    if not row:
        raise HTTPException(404, "资产不存在")
    return {"ok": True, "item": row}


@app.get("/api/projects/{pid}/asset-gap")
def asset_gap_scan(pid: str) -> dict:
    """只读：评分 / 硬性条款 × 企业资产 → 缺件候选清单（不判定得分）。"""
    from . import asset_match
    _need_project(pid)
    return asset_match.scan(pid)


@app.get("/api/projects/{pid}/goods")
def goods_scan(pid: str) -> dict:
    """只读：从招标文件与项目资料里抽出的货物清单与最高限价状态。"""
    from . import goods_list
    _need_project(pid)
    return goods_list.scan(pid)


@app.get("/api/projects/{pid}/quote-gate")
def quote_gate_scan(pid: str) -> dict:
    """只读：限价算术比对与多来源总价交叉核对结果。"""
    from . import quote_gate
    _need_project(pid)
    return quote_gate.scan(pid)


@app.get("/api/projects/{pid}/brief")
def brief_scan(pid: str) -> dict:
    """只读：招标解读汇总（要求 / 评分 / 清单 / 限价 / 报价 / 证照）。"""
    from . import brief
    _need_project(pid)
    return brief.scan(pid)


@app.get("/api/projects/{pid}/locate")
def locate(pid: str, page: int = 0, q: str = "") -> dict:
    """点击问题回跳原文：按页码 / 关键词取回上传文件的原页文本。"""
    _need_project(pid)
    return store.locate_page(pid, page, q)


@app.get("/api/projects/{pid}/evidence")
def evidence_list(pid: str, sid: str = "") -> dict:
    """章节绑定的证据片段：doc_id / 页码 / 字符区间 / 原文，供角标与回跳使用。"""
    _need_project(pid)
    if sid:
        _need_section(pid, sid)          # 不许拿别的项目的章节 id 读本章证据
    rows = store.list_evidence(pid, sid)
    ids = {r["id"] for r in rows}
    supports = []
    for req in store.list_requirements(pid):
        for link in requirement_support.list_links(pid,req["id"]):
            if link["target_type"] == "evidence" and link["target_id"] in ids:
                supports.append({**link,"requirement_text":req["text"]})
    return {"rows": rows, "count": len(rows),
            "support_links": supports,
            "missing": sum(1 for r in rows if r.get("status") == "missing")}


@app.delete("/api/docs/{did}")
def doc_delete(did: str) -> dict:
    store.delete_doc(did)
    return {"ok": True}


@app.post("/api/projects/{pid}/retrieve")
def retrieve(pid: str, body: dict) -> dict:
    _need_project(pid)
    context, hits = rag.retrieve(pid, str(_body(body, "query", "")), int(_body(body, "k", 4) or 4))
    return {"context": context, "hits": hits}


# ---------------- 写标书 ----------------

@app.post("/api/projects/{pid}/jobs/compose")
def compose(pid: str, body: dict) -> dict:
    _require_outline_confirmed(pid)
    return _start("compose", pid, agent.compose, bool(_body(body, "overwrite", False)))


@app.post("/api/projects/{pid}/section/{sid}/generate")
def section_generate(pid: str, sid: str) -> dict:
    _need_project(pid)
    _need_section(pid, sid)          # 章节不存在 / 不属于本项目先 404，再判目录闸门
    _require_outline_confirmed(pid)
    return _exclusive_write(pid, "section", "生成本章", agent.generate_section, pid, sid)


@app.post("/api/projects/{pid}/section/{sid}/rewrite")
def section_rewrite(pid: str, sid: str, body: dict) -> dict:
    _need_project(pid)
    _need_section(pid, sid)
    _require_outline_confirmed(pid)
    return _exclusive_write(pid, "rewrite", "改写本章", agent.rewrite_section,
                            pid, sid, str(_body(body, "instruction", "")))


@app.put("/api/projects/{pid}/section/{sid}")
def section_save(pid: str, sid: str, body: dict) -> dict:
    """保存正文。带 base_ver 时做乐观锁，返回 409 让前端决定用谁的版本。

    不带 base_ver 不再静默降级成「无条件覆盖」：要么显式传
    allow_overwrite=true（冲突后用户确认用本地稿覆盖、AI 稿应用等场景），
    要么带正确版本号。生成 / 导入等内部写入直接走 store 层，不受此限制。
    """
    _need_project(pid)
    _need_section(pid, sid)
    raw = _body(body, "base_ver", None)
    base_ver = None
    if raw not in (None, ""):
        try:
            base_ver = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "base_ver 必须是整数版本号")
        if base_ver < 0:
            raise HTTPException(400, "base_ver 不能为负数")
    elif not _body(body, "allow_overwrite", False):
        raise HTTPException(400, "保存需要带 base_ver；确需无条件覆盖请显式传 allow_overwrite")
    r = store.update_section(sid, str(_body(body, "text", "")), base_ver, pid=pid)
    if not r.get("ok"):
        err = r.get("error")
        if err in ("missing", "not_in_project"):
            raise HTTPException(404, "章节不存在或不属于该项目")
        if err == "busy":
            # 并发写撞锁：明确说「稍后再存」，不要伪装成版本冲突
            return JSONResponse(status_code=503, content={
                "error": "同时有别的保存正在写入，稍等一下再试", "busy": True})
        return JSONResponse(status_code=409, content={
            "error": "这一章在你编辑期间被改动过，你的版本没有覆盖它",
            "conflict": True, "ver": r.get("ver", 0), "text": r.get("text", "")})
    return {"ok": True, "ver": r.get("ver", 0)}


def _gate_header(gate: dict) -> str:
    return ("caps=" + str(gate.get("count", 0)) +
            ";findings=" + str(gate.get("findings", 0)) +
            ";high=" + str(gate.get("high", 0)))


def _check_export_gate(pid: str, token: str, snap: dict | None = None
                       ) -> tuple[dict, JSONResponse | None]:
    """导出检查：带「知情导出」令牌时只提醒、不禁止；不带令牌返回提醒清单。

    这里的 409 不是拒绝下载，只是给前端一个「有待确认的提醒」信号：
    前端据此列出完整清单，用户点「仍然导出」换知情令牌后重试即可下载。
    """
    gate = auto_core.export_gate(pid)
    ack = auto_core.consume_ack_token(pid, str(gate.get("fingerprint") or ""), token)
    if ack:
        # 用户已看过完整清单并明确选择继续：不符合项只作为提醒，不再拦下载。
        # 风险摘要随产物登记，不给「已通过检查」的假象。
        if snap and gate.get("fingerprint") != snap.get("fingerprint"):
            ack["snapshot_mismatch"] = True
            ack["note"] = "检查后内容又变了，本次提醒对应的是上一版内容，导出内容以本次快照为准"
        return {**gate, "acknowledged": ack}, None
    if snap and gate.get("fingerprint") != snap.get("fingerprint"):
        # 检查期间正文 / 要求 / 资料 / 策略被改过：不出与这次检查不一致的文件
        return gate, JSONResponse(status_code=409, content={
            "error": "检查期间内容或导出策略发生变化，本次检查与待导出版本不是同一份；请重新体检后再导出",
            "snapshot_fingerprint": snap.get("fingerprint"),
            "live_fingerprint": gate.get("fingerprint"), **gate})
    # 下面两处 409 只是「提醒信号」：前端据此列出清单，用户点「仍然导出」
    # 换知情令牌后重试，绝不永久拒绝下载。严格逐条确认令牌仍按原契约放行。
    if gate.get("blocked"):
        return gate, JSONResponse(status_code=409, content={
            "error": ("导出前有重点提醒：有章节未达交付标准，请看清单；"
                      "确认后仍可下载，文件会记下这次提醒"), **gate})
    if gate["requires_confirm"] and not auto_core.verify_export_token(
            pid, gate["fingerprint"], token):
        return gate, JSONResponse(status_code=409, content={
            "error": ("导出前有高风险提醒，请看清单；确认后仍可下载，"
                      "未逐条确认的旧路径不直接放行"), **gate})
    return gate, None


def _deliver(pid: str, snap: dict, gate: dict, data: bytes, filename: str,
             fmt: str, media_type: str, options: dict | None = None) -> Response:
    """正式输出共同入口：最终文件检查 → 落盘登记 → 才允许返回文件。

    带知情导出令牌时，检查不符合只作为提醒记录在产物里，不阻断下载。
    """
    ack = (gate or {}).get("acknowledged")
    opts = dict(options or {})
    check = delivery.inspect_document(data, fmt, snap,
                                      blind=bool(opts.get("blind")))
    if not ack and (not check.get("checked") or not check.get("ok")):
        # 没确认时先给完整提醒清单；带知情令牌时绝不因检查没过 / 没跑完拒绝下载
        fc = [{"key": f"fc{i}", "level": x.get("level") or "高",
               "title": "最终文件检查 · " + str(x.get("code") or ""),
               "detail": str(x.get("detail") or "")}
              for i, x in enumerate(check.get("issues") or [])]
        all_issues = list(gate.get("issues") or []) + fc
        return JSONResponse(status_code=409, content={
            "error": check.get("summary") or "最终文件检查未完成，不能标成正式交付件",
            "file_check": check,
            "fingerprint": gate.get("fingerprint"),
            "issues": all_issues,
            "high": int(gate.get("high") or 0) + len(check.get("blocking") or []),
            "blocked": True, "requires_confirm": True,
            **auto_core.group_export_issues(all_issues)})
    if ack:
        problem = (not check.get("ok")) or (not check.get("checked"))
        if problem:
            note = ("最终文件检查未完成" if not check.get("checked") else
                    f"最终文件检查发现 {len(check.get('blocking') or [])} 项问题")
            # 如实记风险：不把「没跑完 / 没通过」改写成「已通过」，只是不拦下载
            check = {**check, "acknowledged": True,
                     "risk_note": check.get("summary") or note,
                     "summary": note + "；用户已知悉并仍导出，需人工复核"}
            opts["export_ack"] = {**ack,
                                  "file_check_issues": len(check.get("blocking") or []),
                                  "file_check_completed": bool(check.get("checked"))}
        else:
            opts["export_ack"] = ack
    # 产物等级只看客观事实，与「能不能下载」完全解耦：
    # 只有「没有知情令牌 + 门禁无硬性否决 + 最终文件检查通过」才算正式交付件。
    # 用户看过提醒仍导出的，单独登记为知情导出草稿，不混进正式交付件列表，
    # 也不冒充「未经门禁」的普通审阅件。下载行为在这里一点不改。
    clean = (not ack and not (gate or {}).get("blocked")
             and bool(check.get("ok")) and bool(check.get("checked"))
             and bool(check.get("deliverable_ready", not check.get("draft_markers"))))
    kind = ("deliverable" if clean else
            delivery.ACK_DRAFT_KIND if ack else "review")
    try:
        art = delivery.register(pid, data, filename, kind=kind, snap=snap,
                                gate_fingerprint=str(gate.get("fingerprint") or ""),
                                fmt=fmt, options=opts, check=check)
    except Exception as exc:
        # 正式文件登记失败就明确失败，不发一份事后无法追溯的文件
        return JSONResponse(status_code=500, content={
            "error": f"正式文件登记失败，已取消本次导出（{type(exc).__name__}: {str(exc)[:120]}）；"
                     "请检查磁盘空间与权限后重试，本次没有产生可交付文件"})
    changed = delivery.live_changed(snap)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "X-Auto-Core": _gate_header(gate),
        "X-Artifact-Id": art["id"],
        "X-Artifact-Version": str(art.get("version") or 1),
        "X-Artifact-Current": "1" if art.get("is_current") else "0",
        "X-Artifact-Kind": str(art.get("kind") or kind),
        "X-Artifact-Purpose": ("deliverable" if kind == "deliverable" else
                              "acknowledged-draft" if kind == delivery.ACK_DRAFT_KIND
                              else "review-only"),
        "X-Content-Hash": art.get("content_hash") or "",
        "X-Snapshot-Version": snap.get("version") or "",
        "X-Snapshot-Fingerprint": snap.get("fingerprint") or "",
        # 知情导出一律如实标 acknowledged：即便最终文件检查过了，这也是用户
        # 看过提醒后下载的草稿，不写成「已通过检查」。无令牌时按事实分三档：
        # blocked（结构/身份硬伤）、draft（文件能打开但仍是未填完的清单骨架）、
        # pass。门禁是否被硬性否决另外单独报出来。
        "X-File-Check": ("acknowledged" if ack else
                         "blocked" if not check.get("ok") else
                         "draft" if check.get("draft_markers") else "pass"),
        "X-Export-Ack": "1" if ack else "0",
        "X-Gate-Blocked": "1" if (gate or {}).get("blocked") else "0",
        "X-Gate-Issues": str(len(gate.get("issues") or [])),
        "X-Live-Changed": "1" if changed else "0",
    }
    return Response(content=data, media_type=media_type, headers=headers)


@app.get("/api/projects/{pid}/export")
def export(pid: str, token: str = "") -> Response:
    """Markdown 与 Word 走同一道门禁、同一份冻结快照。"""
    _need_project(pid)
    snap = delivery.freeze(pid)
    gate, denied = _check_export_gate(pid, token, snap)
    if denied is not None:
        return denied
    lines = [f"# {snap['name']}", ""]
    for s in snap["sections"]:
        lines += [f"## {s['title']}", "", s.get("text") or "（未填写）", ""]
    data = "\n".join(lines).encode("utf-8")
    return _deliver(pid, snap, gate, data, snap["name"] + ".md", "md",
                    "text/markdown; charset=utf-8")


@app.get("/api/projects/{pid}/readiness")
def project_readiness(pid: str) -> dict:
    """一句话结论：现在能不能交。只读汇总质量 / 体检 / 门禁，不重跑体检。"""
    _need_project(pid)
    out = readiness.assess(pid)
    # 证照过期以前只有点导出才看得到，太晚。这里只前置一行提示，
    # 不改 readiness 的判定口径，也不把「提醒」升级成「禁止下载」。
    try:
        from . import certificates
        cc = certificates.scan(pid).get("counts") or {}
        n = {k: int(cc.get(k) or 0) for k in ("expired", "expiring", "invalid")}
        parts = ([f"{n['expired']} 项已过期"] if n["expired"] else []) \
            + ([f"{n['expiring']} 项 60 天内到期"] if n["expiring"] else []) \
            + ([f"{n['invalid']} 项有效期日期写错"] if n["invalid"] else [])
        out["cert_counts"] = n
        out["cert_hint"] = ("证照：" + "、".join(parts) + "，请在投标前更新") if parts else ""
    except Exception:
        out["cert_counts"] = {}
        out["cert_hint"] = "证照有效期没查成，请在投标前人工核对"
    # P2：资产台账统计随「能不能交」一起返回，供缺件清单与前端状态标使用。
    # 到期风险已由上面的 cert_hint 统一提示，这里不重复报警、不改判定口径。
    try:
        from . import assets
        ar = assets.scan()
        out["assets"] = {"total": ar["total"], "categories": ar["categories"],
                         "counts": ar["counts"], "attention": len(ar["attention"])}
    except Exception:
        out["assets"] = {}
    # P3：只把计数带进「能不能交」，完整清单点开时才按 /asset-gap 拉取，
    # 不给首页首屏添负担；缺件是提醒，不升级成「禁止下载」。
    try:
        from . import asset_match
        g = asset_match.scan(pid)
        out["asset_gap"] = {"total": g["total"], "missing": g["missing"],
                            "expired": g["expired"], "candidate": g["candidate"],
                            "no_requirements": g["no_requirements"]}
        out["asset_gap_hint"] = g["hint"]
    except Exception:
        out["asset_gap"] = {}
        out["asset_gap_hint"] = ""
    return out


@app.get("/api/projects/{pid}/quality")
def project_quality(pid: str) -> dict:
    """当前标书的生成质量硬指标；只读、不调模型、不改数据。"""
    _need_project(pid)
    return quality.assess_project(pid)


@app.get("/api/projects/{pid}/health")
def project_health(pid: str) -> dict:
    """整本自动体检报告：与导出门禁复用同一份按内容指纹缓存的结论。

    只读、不调模型；合规台账会在后台匿名登记招标指纹，不记录任何正文。
    """
    _need_project(pid)
    from . import health
    return health.latest(pid)


@app.get("/api/projects/{pid}/compliance")
def project_compliance(pid: str) -> dict:
    """多企业防雷同边界的只读结论，不返回其他企业标识、项目名或正文。"""
    _need_project(pid)
    from . import compliance
    return compliance.status(pid)


@app.get("/api/projects/{pid}/export/gate")
def export_gate_api(pid: str) -> dict:
    """导出前强制重跑门禁；返回内容指纹与待逐条确认的高风险问题。"""
    _need_project(pid)
    return auto_core.export_gate(pid)


@app.post("/api/projects/{pid}/export/confirm")
def export_confirm(pid: str, body: dict) -> dict:
    """两种确认：默认逐条勾选换严格令牌；acknowledge=true 换「知情导出」令牌。"""
    _need_project(pid)
    fp = str(_body(body, "fingerprint", "") or "")
    if _body(body, "acknowledge", False):
        r = auto_core.issue_ack_token(pid, fp)
    else:
        r = auto_core.issue_export_token(pid, fp,
                                         list(_body(body, "confirmed", []) or []))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "确认失败")
    return r


# ---------------- C02：项目负责人 / 成员角色 / 章节分工与复核 ----------------

@app.get("/api/projects/{pid}/team")
def team_get(pid: str) -> dict:
    _need_project(pid)
    return access.team_report(pid)


@app.post("/api/projects/{pid}/team")
def team_add(pid: str, body: dict) -> dict:
    _need_project(pid)
    access.require(pid, "manage")
    uid = str(_body(body, "user_id", "") or "").strip()
    if not uid:
        raise HTTPException(400, "先选一个本企业成员")
    store.set_project_member(pid, uid, str(_body(body, "role", "reader") or "reader"))
    return access.team_report(pid)


@app.delete("/api/projects/{pid}/team/{uid}")
def team_remove(pid: str, uid: str) -> dict:
    _need_project(pid)
    access.require(pid, "manage")
    store.remove_project_member(pid, uid)
    return access.team_report(pid)


@app.put("/api/projects/{pid}/team/meta")
def team_meta(pid: str, body: dict) -> dict:
    _need_project(pid)
    access.require(pid, "manage")
    store.update_project(
        pid, owner_id=str(_body(body, "owner_id", "") or "")[:80],
        due_at=str(_body(body, "due_at", "") or "")[:40],
        final_version=str(_body(body, "final_version", "") or "")[:80])
    return access.team_report(pid)


@app.put("/api/projects/{pid}/team/sections")
def team_sections(pid: str, body: dict) -> dict:
    _need_project(pid)
    access.require(pid, "manage")
    valid = {s["id"] for s in store.list_sections(pid)}
    me = auth.current()
    reviewer = str(me.get("name") or me.get("username") or "")
    for row in list(_body(body, "items", []) or []):
        if not isinstance(row, dict) or str(row.get("id") or "") not in valid:
            continue
        store.assign_section(
            pid, str(row["id"]), user_id=str(row.get("assignee") or ""),
            review_status=str(row.get("review_status") or "pending"),
            reviewer=reviewer)
    return access.team_report(pid)


# ---------------- C01：暗标规则 / 真实分册与投标包 ----------------

@app.get("/api/projects/{pid}/bundle/plan")
def bundle_plan(pid: str) -> dict:
    """分册归属规划预览；未确认前不生成任何文件。"""
    _need_project(pid)
    return bidbundle.plan(pid)


@app.get("/api/projects/{pid}/bundle/rules")
def bundle_rules_get(pid: str) -> dict:
    _need_project(pid)
    return blindcheck.rules_for(pid)


@app.post("/api/projects/{pid}/bundle/rules")
def bundle_rules_save(pid: str, body: dict) -> dict:
    _need_project(pid)
    return {"ok": True, "rules": blindcheck.save_rules(pid, body or {})}


@app.post("/api/projects/{pid}/bundle/scan")
def bundle_scan(pid: str, body: dict | None = None) -> dict:
    """按当前正文临时渲染一份 Word，做全包暗标扫描；不落盘、不登记产物。"""
    _need_project(pid)
    p = _need_project(pid)
    opts = {"blind": True, "header": "", "toc": True,
            "required_sections": [s["title"] for s in p["sections"]]}
    data = docx_export.build_docx(p["name"], p["sections"], opts)
    r = blindcheck.scan_docx(data, pid)
    return {"ok": True, "scan": r,
            "note": "扫描的是按当前正文临时渲染的 Word；正式提交前仍须对最终文件再扫一遍"}


@app.post("/api/projects/{pid}/bundle/export")
def bundle_export(pid: str, body: dict) -> Response:
    """用户确认分册归属后，生成真实分册包（zip）。

    必须过同一道导出检查；带知情导出令牌时，问题只作为提醒随包记录。
    """
    _need_project(pid)
    volumes = list(_body(body, "volumes", []) or [])
    if not volumes:
        raise HTTPException(400, "先确认每章的分册归属")
    if not _body(body, "confirmed", False):
        raise HTTPException(400, "请逐册确认归属后再生成投标包")
    snap = delivery.freeze(pid)
    gate, denied = _check_export_gate(pid, str(_body(body, "token", "") or ""), snap)
    if denied is not None:
        return denied
    r = bidbundle.build(pid, snap, volumes, blind=bool(_body(body, "blind", False)),
                        confirmed=True)
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": r.get("error") or "生成失败"})
    ack = (gate or {}).get("acknowledged")
    if r["blocking"] or not r.get("data"):
        # 分册包是正式交付物：逐册 Word 检查没过就不发，知情令牌也不能把
        # bidbundle 返回的空字节登记成 0 字节的「正式产物」。单文件导出另论。
        why = ("有分册没通过最终 Word 检查，本次不生成投标包；请修复后重试"
               if not ack else
               "已记录你的知情确认，但分册最终检查未通过，不能发放空投标包；"
               "请先修复下列问题再导出，或改用单册 Word 导出")
        return JSONResponse(status_code=409, content={
            "error": why,
            "checks": [{k: c[k] for k in ("volume", "file", "summary", "issues")}
                       for c in r["blocking"]]})
    data = r["data"]
    manifest = r["manifest"]
    warnings = list(manifest.get("warnings") or [])
    if r["blocking"]:
        warnings.append(f"{len(r['blocking'])} 册最终文件检查未通过，用户已知悉并仍导出")
    filename = (snap.get("name") or "投标文件") + ".投标包.zip"
    bundle_markers = sorted({m for c in manifest.get("final_checks") or []
                             for m in (c.get("draft_markers") or [])})
    # 与单文件导出同一口径：知情确认过、或仍是清单骨架的分册包不是正式交付件。
    bundle_clean = (not ack and not r["blocking"] and not bundle_markers
                    and not (gate or {}).get("blocked"))
    bundle_kind = ("deliverable" if bundle_clean else
                   delivery.ACK_DRAFT_KIND if ack else "review")
    try:
        art = delivery.register(
            pid, data, filename, kind=bundle_kind, snap=snap,
            gate_fingerprint=str(gate.get("fingerprint") or ""), fmt="zip",
            options={"volumes": [v["name"] for v in manifest["volumes"]],
                     "blind": bool(_body(body, "blind", False)),
                     **({"export_ack": ack} if ack else {})},
            check={"ok": not bool(r["blocking"]), "checked": True, "fmt": "zip",
                   "draft_markers": bundle_markers,
                   "deliverable_ready": bool(bundle_clean),
                   "acknowledged": bool(ack and r["blocking"]),
                   "summary": (f"{len(r['checks'])} 册逐册检查，"
                               f"{len(r['blocking'])} 册未通过（用户已知悉并仍导出）"
                               if r["blocking"] else
                               f"{len(r['checks'])} 册逐册通过最终文件检查"),
                   "warnings": warnings,
                   "volumes": manifest["final_checks"],
                   "capability_note": manifest["capability_note"]})
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": f"投标包登记失败，已取消本次导出（{type(exc).__name__}: {str(exc)[:120]}）"})
    return Response(
        content=data, media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Artifact-Id": art["id"], "X-Content-Hash": art.get("content_hash") or "",
            "X-Artifact-Kind": str(art.get("kind") or bundle_kind),
            "X-Artifact-Purpose": ("deliverable" if bundle_kind == "deliverable" else
                                  "acknowledged-draft" if bundle_kind == delivery.ACK_DRAFT_KIND
                                  else "review-only"),
            "X-Snapshot-Fingerprint": snap.get("fingerprint") or "",
            "X-File-Check": ("acknowledged" if ack else
                             "blocked" if r["blocking"] else
                             "draft" if bundle_markers else "pass"),
            "X-Export-Ack": "1" if ack else "0",
            "X-Gate-Blocked": "1" if (gate or {}).get("blocked") else "0",
            "X-Gate-Issues": str(len(gate.get("issues") or [])),
            "X-Bundle-Volumes": str(len(manifest["volumes"])),
            "X-Bundle-Warnings": str(len(warnings)),
            "X-Capability-Note": quote("目录域需 Word 更新；PDF 与平台专用格式未实现"),
        })


@app.get("/api/projects/{pid}/export.docx")
def export_word(pid: str, token: str = "", blind: bool = False) -> Response:
    _need_project(pid)
    snap = delivery.freeze(pid)
    # 每次导出都强制重跑门禁，不吃缓存；检查与渲染共用上面这份冻结快照
    gate, denied = _check_export_gate(pid, token, snap)
    if denied is not None:
        return denied
    outline = [str(c.get("title") or "") for c in snap.get("outline") or []
               if isinstance(c, dict) and c.get("title")]
    try:
        date_label = datetime.strptime(snap["at"][:10], "%Y-%m-%d").strftime("%Y 年 %m 月 %d 日")
    except (ValueError, KeyError):
        date_label = snap["at"][:10]
    options = {"blind": blind, "required_sections": outline,
               "header": snap["name"], "date": date_label,
               "snapshot_version": snap["version"]}
    data = docx_export.build_docx(snap["name"], snap["sections"], options)
    filename = snap["name"] + (".暗标.docx" if blind else ".docx")
    return _deliver(pid, snap, gate, data, filename, "docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options)


@app.get("/api/projects/{pid}/artifacts/{aid}/download")
def artifact_download(pid: str, aid: str) -> Response:
    """按导出记录回下载：核对归属、存在性、内容摘要，并标明版本与用途。"""
    _need_project(pid)
    art = store.get_artifact_by_id(aid)
    ok, why = delivery.verify_download(pid, art)
    if not ok:
        raise HTTPException(404 if "不属于" in why or "不存在" in why else 409, why)
    headers = {
        "X-Content-Hash": art.get("content_hash") or "",
        "X-Gate-Fingerprint": art.get("gate_fingerprint") or "",
        "X-Artifact-Kind": art.get("kind") or "",
        "X-Artifact-Version": str(art.get("version") or 1),
        "X-Artifact-Current": "1" if art.get("is_current") else "0",
        "X-Artifact-Purpose": ("deliverable" if art.get("kind") == "deliverable" else
                              "acknowledged-draft" if art.get("kind") == delivery.ACK_DRAFT_KIND
                              else "review-only"),
        "X-Snapshot-Version": art.get("snapshot_fingerprint") or "",
    }
    if not art.get("is_current"):
        headers["X-Artifact-Warn"] = quote(f"历史版本 v{art.get('version') or 1}，不是当前定稿")
    if art.get("kind") == delivery.ACK_DRAFT_KIND:
        headers["X-Artifact-Warn"] = quote(delivery.ACK_DRAFT_PURPOSE)
    elif art.get("kind") != "deliverable":
        headers["X-Artifact-Warn"] = quote("审阅件：未经导出门禁、未脱敏，不能作为投标正式文件")
    return FileResponse(str(art["path"]), filename=art["name"], headers=headers)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    """统一文件解析：docx / pdf / xlsx / csv / txt / md，返回纯文本 + 解析状态。"""
    data = await file.read()
    try:
        result = parsers.parse_bytes(file.filename or "", data)
    except parsers.ParseError as exc:
        detail = {"msg": str(exc), "parse_status": "unusable", "usable": False,
                  "effective_chars": 0, "filename": file.filename or ""}
        detail.update(exc.detail or {})
        raise HTTPException(400, detail)
    return {"filename": file.filename, **result}


# ---------------- R05：解析待核对（补录 / 人工确认 / 原件留存） ----------------

def _tender_parse(p: dict) -> dict:
    raw = p.get("tender_parse") or ""
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


@app.get("/api/projects/{pid}/parsing")
def parsing_pending(pid: str) -> dict:
    """刷新后仍能看到：哪几页没识别、哪份资料还等人工核对、哪些原件留存。"""
    p = _need_project(pid)
    tp = _tender_parse(p)
    tender = ({"filename": tp.get("filename") or "招标文件",
               "needs_review": bool(tp.get("needs_review")),
               "unread_pages": tp.get("unread_pages") or [],
               "review_note": tp.get("review_note") or "",
               # 内嵌图片 OCR 只给人核对，不进正文、不进依据池
               "image_text": tp.get("image_text") or "",
               "image_ocr_count": tp.get("image_ocr_count") or 0,
               "image_ocr_engine": tp.get("image_ocr_engine") or "",
               "images": tp.get("images") or 0,
               "confirmed_at": tp.get("confirmed_at") or ""}
              if tp else None)
    docs = [{"id": d["id"], "title": d["title"], "review_note": d.get("review_note") or "",
             "unread_pages": d.get("unread_pages") or [], "scan_pages": d.get("scan_pages") or 0,
             "effective_chars": d.get("effective_chars") or 0,
             "page_map": d.get("page_map") or []}
            for d in store.pending_docs(pid)]
    originals = store.list_originals(pid)
    return {"docs": docs, "tender": tender, "originals": originals,
            "count": len(docs) + (1 if tender and tender["needs_review"]
                                  and not tender["confirmed_at"] else 0)}


@app.post("/api/docs/{did}/confirm")
def doc_confirm(did: str, pid: str = "", body: dict | None = None) -> dict:
    """人工确认接受未识别范围；确认后该资料才进入依据池。"""
    d = store.get_doc(did)
    if not d:
        raise HTTPException(404, "资料不存在")
    if pid and d.get("scope") == "project" and d.get("project_id") != pid:
        raise HTTPException(404, "资料不存在")
    note = str(_body(body or {}, "note", "") or "").strip()
    store.confirm_doc(did, by=note or "人工确认")
    if note:
        store.update_doc(did, review_note=note)
    return {"ok": True, "doc": store.get_doc(did)}


@app.post("/api/docs/{did}/pages")
def doc_page_add(did: str, pid: str = "", body: dict | None = None) -> dict:
    """补录某页未识别的正文；全部补完自动解除待核对。"""
    d = store.get_doc(did)
    if not d:
        raise HTTPException(404, "资料不存在")
    if pid and d.get("scope") == "project" and d.get("project_id") != pid:
        raise HTTPException(404, "资料不存在")
    try:
        page = int(_body(body or {}, "page", 0) or 0)
    except (ValueError, TypeError):
        page = 0
    if page <= 0:
        raise HTTPException(400, "请指定要补录的页码")
    text = str(_body(body or {}, "text", "") or "").strip()
    if not text:
        raise HTTPException(400, "补录内容不能为空")
    out = store.add_page_text(did, page, text, by="人工补录")
    return {"ok": True, "doc": out, "needs_review": bool((out or {}).get("needs_review"))}


@app.post("/api/projects/{pid}/tender/confirm")
def tender_confirm(pid: str) -> dict:
    p = _need_project(pid)
    tp = _tender_parse(p)
    if not tp:
        raise HTTPException(400, "没有记录到招标文件的解析状态")
    tp["confirmed_at"] = store.now()
    tp["confirmed_by"] = "人工确认"
    store.update_project(pid, tender_parse=json.dumps(tp, ensure_ascii=False))
    return {"ok": True, "tender_parse": tp}


# ---------------- B02：企业事实 / 拟承诺值 / 原件附件 ----------------

@app.get("/api/projects/{pid}/facts")
def facts_list(pid: str) -> dict:
    _need_project(pid)
    return {"items": store.list_facts(pid), "attachments": facts.attachment_index(pid),
            "kinds": list(store.FACT_KINDS),
            "examples_excluded": quality.example_excluded(pid),
            "gaps": facts.gaps(pid, store.list_sections(pid), quality.project_sources(pid))[:20],
            "note": "表达范例不能证明当前企业事实；拟承诺值须责任人确认；数值核对实体与单位"}


@app.post("/api/projects/{pid}/facts")
def facts_add(pid: str, body: dict) -> dict:
    _need_project(pid)
    row = store.add_fact(
        pid, str(_body(body, "entity", "")), str(_body(body, "field", "")),
        str(_body(body, "value", "")),
        unit=str(_body(body, "unit", "")), scope=str(_body(body, "scope", "")),
        valid_until=str(_body(body, "valid_until", "")),
        source_doc_id=str(_body(body, "source_doc_id", "")),
        source_loc=str(_body(body, "source_loc", "")),
        kind=str(_body(body, "kind", "enterprise_fact") or "enterprise_fact"),
        verification=str(_body(body, "verification", "unverified") or "unverified"))
    return {"ok": True, "item": row}


@app.post("/api/projects/{pid}/facts/{fid}/verify")
def facts_verify(pid: str, fid: str, body: dict | None = None) -> dict:
    _need_project(pid)
    row = store.verify_fact(fid, str(_body(body or {}, "by", "") or "人工核验"))
    if not row:
        raise HTTPException(404, "事实不存在")
    return {"ok": True, "item": row}


@app.post("/api/projects/{pid}/attachments")
async def attachment_add(pid: str, file: UploadFile = File(...),
                         fact_id: str = "", doc_id: str = "",
                         section_id: str = "", page: int = 0,
                         note: str = "") -> dict:
    """实际提交的原件：落盘登记，绑定事实 / 章节 / 资料，可回跳打开。"""
    _need_project(pid)
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB")
    folder = scope.dir_for("originals", write=True) / pid / "submitted"
    folder.mkdir(parents=True, exist_ok=True)
    aid = store.new_id()
    suffix = Path(file.filename or "附件.bin").suffix[:12]
    dest = folder / f"{aid}{suffix}"
    dest.write_bytes(data)
    # 同一份文件的正文摘要也留档，便于核对解析版本与原件是否一致
    (folder / f"{aid}.sha256").write_text(hashlib.sha256(data).hexdigest(), encoding="utf-8")
    row = store.add_attachment(
        pid, file.filename or "附件", str(dest),
        fact_id=fact_id, doc_id=doc_id, section_id=section_id,
        page=page, note=note)
    return {"ok": True, "attachment": row,
            "note": "原件已留存；摘要与校验值随文件保存，可在附件索引里回跳"}


@app.get("/api/projects/{pid}/attachments/{aid}/open")
def attachment_open(pid: str, aid: str) -> Response:
    """按附件索引回跳打开原件；只认本项目登记过的路径，避免任意文件读取。"""
    _need_project(pid)
    row = next((a for a in store.list_attachments(pid) if a["id"] == aid), None)
    if not row:
        raise HTTPException(404, "附件不存在")
    f = Path(row["path"])
    # 旧数据仍放在平铺的 data/originals/{pid}；新原件按企业目录存，两处都认
    roots = [(scope.dir_for("originals") / pid).resolve(),
             (config.DATA_DIR / "originals" / pid).resolve()]
    if not f.is_file() or not any(f.resolve().is_relative_to(r) for r in roots):
        raise HTTPException(404, "原件已不在磁盘上，请重新上传")
    return FileResponse(f, filename=row["filename"])


# ---------------- B03：保护表单完成核验 ----------------

@app.get("/api/projects/{pid}/forms")
def forms_get(pid: str) -> dict:
    _need_project(pid)
    return forms.check(pid)


@app.post("/api/projects/{pid}/forms/confirm")
def forms_confirm(pid: str, body: dict) -> dict:
    _need_project(pid)
    r = forms.confirm(pid, str(_body(body, "section_id", "")),
                      str(_body(body, "target", "")),
                      reviewer=str(_body(body, "reviewer", "") or "人工复核"),
                      note=str(_body(body, "note", "") or ""))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "确认失败")
    auto_core.invalidate_cache()
    return {**r, **forms.check(pid)}


@app.post("/api/projects/{pid}/quote-check")
def quote_check(pid: str, body: dict) -> dict:
    """把 Word / Excel / 粘贴的报价行列转成校验器输入，Decimal 核算小计与合计。"""
    _need_project(pid)
    rows = _body(body, "rows", None)
    if isinstance(rows, list) and rows:
        return facts.quote_check(rows)
    return forms.quote_report(str(_body(body, "text", "") or ""))


@app.post("/api/projects/{pid}/originals")
async def original_add(pid: str, file: UploadFile = File(...)) -> dict:
    """纯扫描 / 空文件保留原件待处理：不解析、不入库、不作为任何依据。"""
    _need_project(pid)
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB，请拆分后再传")
    folder = scope.dir_for("originals", write=True) / pid
    folder.mkdir(parents=True, exist_ok=True)
    oid = store.new_id()
    suffix = Path(file.filename or "原件.bin").suffix[:12]
    dest = folder / f"{oid}{suffix}"
    dest.write_bytes(data)
    row = store.add_original(pid, file.filename or "原件", str(dest), len(data),
                             reason="解析无有效文字，仅留存原件")
    return {"ok": True, "original": row,
            "note": "已按「待处理」留存原件；它不会进入资料依据、要求或导出核查，需 OCR 或人工补录后重新上传。"}


# ---------------- 验标书 ----------------

@app.post("/api/projects/{pid}/check")
def do_check(pid: str) -> dict:
    _need_project(pid)
    p = store.get_project(pid)
    empty = not any(str(s.get("text") or "").strip() for s in p.get("sections", []))
    issues = agent.check(pid, 1)
    return {"issues": issues, "high": sum(1 for i in issues if i["level"] == "高"),
            "total": len(issues), "auto": auto_core.as_card(agent.last_auto(pid)),
            "empty": bool(empty),
            "reason": ("当前项目还没有任何正文，先导入招标文件并写标书，再来检查。" if empty else "")}


@app.post("/api/projects/{pid}/jobs/optimize")
def optimize(pid: str, body: dict) -> dict:
    rounds = int(_body(body, "rounds", 2) or 2)
    return _start("optimize", pid, agent.optimize, max(1, min(rounds, 2)))


# ---------------- 可回退与逐条采纳 ----------------

@app.get("/api/projects/{pid}/versions")
def versions(pid: str) -> dict:
    _need_project(pid)
    return {"batches": store.list_batches(pid)}


@app.post("/api/projects/{pid}/versions/{batch}/restore")
def version_restore(pid: str, batch: str) -> dict:
    _need_project(pid)

    def _do() -> dict:
        r = store.restore_batch(pid, batch)
        if not r.get("ok"):
            raise HTTPException(404, r.get("error") or "快照不存在")
        return r

    return _exclusive_write(pid, "restore", "恢复快照", _do)


@app.post("/api/projects/{pid}/checks/{cid}/status")
def check_status(pid: str, cid: str, body: dict) -> dict:
    _need_project(pid)
    r = store.set_check_status(pid, cid, str(_body(body, "status", "pending")))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "问题不存在")
    return r


@app.get("/api/projects/{pid}/usage")
def usage(pid: str, limit: int = 50) -> dict:
    _need_project(pid)
    return store.usage_summary(pid, max(1, min(limit, 200)))


@app.get("/api/budget")
def budget_get() -> dict:
    return {"budget": config.budget(), "today_tokens": store.today_tokens(),
            "retention": store.retention()}


@app.post("/api/budget")
def budget_set(body: dict) -> dict:
    _need_owner("预算设置")
    return {"budget": config.set_budget(body), "today_tokens": store.today_tokens(),
            "retention": store.retention()}


# ---------------- 智能体策略配置（唯一事实源） ----------------

@app.get("/api/settings")
def get_settings() -> dict:
    return settings.public()


@app.post("/api/settings")
def save_settings(body: dict) -> dict:
    """保存后立即生效：清掉阶段缓存，下一句话的规划目录就按新配置生成。"""
    _need_owner("策略配置")
    patch = body.get("patch", body)
    if not isinstance(patch, dict):
        raise HTTPException(400, "配置格式不对")
    settings.save(patch)
    auto_core.invalidate_cache()
    return settings.public()


# ---------------- 基座能力（本地业务引擎） ----------------

@app.get("/api/core/catalog")
def core_catalog() -> dict:
    return core_bridge.catalog()


@app.post("/api/core/run/{key}")
def core_run(key: str, body: dict | None = None) -> dict:
    body = body or {}
    pid = str(_body(body, "pid", ""))
    _need_project(pid)
    # 中间件按路径推断权限，匹配不到 /api/core/*（pid 在 body 里）。
    # 这里显式校验项目成员身份，避免同企业内非成员借基座能力读别的项目。
    access.require(pid, "read")
    payload = _body(body, "payload", {}) or {}
    return core_bridge.run(key, pid, payload)


@app.get("/api/core/auto/status")
def auto_status(pid: str = "") -> dict:
    if pid:
        _need_project(pid)
        access.require(pid, "read")
    return auto_core.status(pid)


@app.post("/api/core/auto/config")
def auto_config(body: dict) -> dict:
    _need_owner("策略配置")
    # 旧入口保留兼容，实际写入同一份配置
    settings.save({"auto_enabled": bool(_body(body, "enabled", True))})
    auto_core.invalidate_cache()
    return {"enabled": settings.auto_enabled(), "config": settings.load()}


@app.post("/api/core/auto/stage/{stage}")
def auto_stage(stage: str, body: dict | None = None) -> dict:
    body = body or {}
    pid = str(_body(body, "pid", ""))
    _need_project(pid)
    access.require(pid, "write")
    report = auto_core.run_stage(stage, pid, force=bool(_body(body, "force", True)))
    return {"report": report, "card": auto_core.as_card(report)}


@app.get("/api/core/download/{name}")
def core_download(name: str):
    """基座产出的下载：只认本项目登记的记录，跨企业 / 未登记 / 已清理一律 404。

    以前只判断「文件在不在导出目录」，等于跳过归属校验；现在归属以 artifacts 表为准，
    每个企业一个库，别的企业的文件名在本库里查不到。
    """
    art = store.get_artifact(name)
    if not art or not store.get_project(art.get("project_id") or ""):
        raise HTTPException(404, "文件不存在或已过期，请重新生成")
    # 归属不仅有 artifacts 记录，还要当前用户在该项目里具备下载权限
    access.require(str(art["project_id"]), "export")
    ok, why = delivery.verify_download(art["project_id"], art)
    if not ok:
        raise HTTPException(404 if "文件已不在" in why else 409, why)
    headers = {"X-Artifact-Kind": art["kind"],
               "X-Artifact-Version": str(art.get("version") or 1),
               "X-Artifact-Current": "1" if art.get("is_current") else "0",
               "X-Artifact-Purpose": ("deliverable" if art["kind"] == "deliverable" else
                                     "acknowledged-draft" if art["kind"] == delivery.ACK_DRAFT_KIND
                                     else "review-only"),
               "X-Content-Hash": art.get("content_hash") or ""}
    if art["kind"] == delivery.ACK_DRAFT_KIND:
        headers["X-Artifact-Warn"] = quote(delivery.ACK_DRAFT_PURPOSE)
    elif art["kind"] != "deliverable":
        headers["X-Artifact-Warn"] = quote(core_bridge.REVIEW_NOTE)
    return FileResponse(art["path"], filename=art["name"], headers=headers)


@app.get("/api/projects/{pid}/artifacts")
def project_artifacts(pid: str) -> dict:
    """该项目已登记的产出文件；界面用它区分「可交付」「历史版本」与「审阅件」。"""
    _need_project(pid)
    items = store.list_artifacts(pid)
    # 文件被外部清理时只标记状态、保留记录，不能悄悄把登记删了
    for a in items:
        if not Path(str(a.get("path") or "")).is_file() and a.get("status") != "missing":
            store.mark_artifact_status(a["id"], "missing")
            a["status"] = "missing"
        a["is_history"] = not a.get("is_current")
    return {"items": items}


@app.get("/api/jobs/{jid}")
def job(jid: str) -> dict:
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


# ---------------- C03：长任务取消 / 检查点 / 重试 / 恢复 ----------------

@app.post("/api/projects/{pid}/jobs/{jid}/cancel")
def job_cancel(pid: str, jid: str) -> dict:
    _need_project(pid)
    access.require(pid, "write")
    try:
        return taskctl.cancel(pid, jid)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/projects/{pid}/jobs/{jid}/checkpoints")
def job_checkpoints(pid: str, jid: str) -> dict:
    _need_project(pid)
    return taskctl.report(pid, jid)


@app.post("/api/projects/{pid}/jobs/{jid}/resume")
def job_resume(pid: str, jid: str) -> dict:
    _need_project(pid)
    access.require(pid, "write")
    try:
        return taskctl.start(pid, jid, "resume")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/projects/{pid}/jobs/{jid}/retry")
def job_retry(pid: str, jid: str) -> dict:
    _need_project(pid)
    access.require(pid, "write")
    try:
        return taskctl.start(pid, jid, "retry")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/jobs/queue")
def job_queue() -> dict:
    """实测排队时间与并发建议；同时如实写出单进程锁的边界。"""
    return store.concurrency_advice()


# ---------------- 对话 ----------------

@app.post("/api/chat")
def chat_send(body: dict) -> dict:
    pid = str(_body(body, "pid", ""))
    _need_project(pid)
    # 对话会落库并可能触发写正文，路径里没有 pid，单独在这里按项目校验
    access.require(pid, "write")
    message = str(_body(body, "message", ""))
    attachment = _body(body, "attachment", None)
    att_name = (str((attachment or {}).get("filename") or "")
                if isinstance(attachment, dict) else "")
    user_text = message or ("[上传文件] " + att_name).strip()
    # 对话记忆在服务端落库：刷新页面、换浏览器都不丢，且不依赖前端传 history
    try:
        store.add_message(pid, "user", user_text,
                          {"attachment": bool(attachment)})
    except Exception:
        pass
    result = chat.handle(
        pid,
        message,
        str(_body(body, "action", "")),
        attachment,
        _body(body, "history", []) or [],
        str(_body(body, "planner", "")),
    )
    cards = list(result.get("cards") or [])
    if len(json.dumps(cards, ensure_ascii=False, default=str)) > 24000:
        cards = cards[:1]           # 卡片太大只留执行轨迹，正文仍在库里
    try:
        store.add_message(pid, "agent", str(result.get("reply") or ""),
                          {"planner": result.get("planner", ""),
                           "loop_rounds": result.get("loop_rounds", 0),
                           "cards": cards})
    except Exception:
        pass
    return result


@app.get("/api/chat/messages")
def chat_messages(pid: str = "", limit: int = 30) -> dict:
    """界面刷新后恢复历史对话；能力卡片与执行轨迹一并返回，不再只恢复纯文本。"""
    if pid:
        _need_project(pid)
    return {"messages": store.list_messages(pid, limit)}


@app.get("/api/chat/pending")
def chat_pending(pid: str = "") -> dict:
    """刷新后续上：还在跑的后台任务，或跑完但结果还没补进对话的任务。"""
    if pid:
        _need_project(pid)
    return {"jobs": store.list_pending_jobs(pid)}


# ---------------- 智能体大脑 ----------------

@app.get("/api/brain/tools")
def brain_tools() -> dict:
    return brain.catalog()


@app.get("/api/brain/history")
def brain_history(pid: str = "", limit: int = 12) -> dict:
    if pid:
        _need_project(pid)
    return {"runs": store.list_agent_runs(pid, max(1, min(limit, 50)))}


@app.get("/api/brain/guess")
def brain_guess(filename: str = "", text: str = "") -> dict:
    """供界面预览：智能体会把这份文件当成什么，以及有多少把握。"""
    return brain.guess_target_detail("", {"filename": filename, "text": text})


@app.get("/api/chat/job/{jid}")
def chat_job(jid: str) -> dict:
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    if j["status"] in ("queued", "running"):
        return {"pending": True, "status": j["status"], "progress": j["progress"],
                "step": j["step"]}
    r = chat.job_result(j["project_id"], j["kind"], j)
    # 后台任务的结果也是一条对话：刷新页面后不会丢，且只补一次
    if not j.get("consumed"):
        try:
            store.add_message(j["project_id"], "agent", str(r.get("reply") or ""),
                              {"job_id": jid, "job_kind": j["kind"],
                               "cards": r.get("cards") or []})
            store.mark_job_consumed(jid)
        except Exception:
            pass
    return r


# 界面静态文件（放在最后，避免覆盖 /api 路由）
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
