"""SQLite 持久化。只依赖标准库 sqlite3，无 ORM。

连接参数（超时 / WAL / busy_timeout）来自 config.Settings，可用 .env 覆盖。
"""
from __future__ import annotations

import contextvars
import hmac
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DB_PATH, get_settings

_cfg = get_settings()

# 基座库也按企业物理分库：默认企业沿用旧的 basecore.db，其余企业
# 用 data/orgs/<org>.basecore.db。技能运行记录里带项目 id 与结果摘要，
# 不能让 A 企业在自己的库里看到 B 企业的记录。
_db_ctx: contextvars.ContextVar = contextvars.ContextVar("basecore_db", default=None)
_ORG_READY: set[str] = set()
_ORG_LOCK = threading.Lock()


def org_db(org_id: str) -> Path:
    if not org_id or org_id == "default":
        return Path(DB_PATH)
    return Path(DATA_DIR) / "orgs" / f"{org_id}.basecore.db"


@contextmanager
def use_org(org_id: str):
    """把本次基座能力调用切到该企业自己的基座库，首次访问自动建表。"""
    p = org_db(org_id)
    key = str(p.resolve())
    tok = _db_ctx.set(p)
    try:
        if key not in _ORG_READY:
            with _ORG_LOCK:
                if key not in _ORG_READY:
                    init_db()
                    _ORG_READY.add(key)
        yield p
    finally:
        _db_ctx.reset(tok)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    tender_name TEXT DEFAULT '',
    tender_text TEXT DEFAULT '',
    meta TEXT DEFAULT '',
    outline TEXT DEFAULT '',
    stage TEXT DEFAULT 'upload',
    doc_format TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    brief TEXT DEFAULT '',
    content TEXT DEFAULT '',
    refs TEXT DEFAULT '',
    words INTEGER NOT NULL DEFAULT 0,
    target_words INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'empty',
    plan TEXT DEFAULT '',
    score TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejection_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ord INTEGER NOT NULL DEFAULT 0,
    category TEXT DEFAULT '',
    clause TEXT NOT NULL,
    basis TEXT DEFAULT '',
    check_hint TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    confidence TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    note TEXT DEFAULT '',
    confirmed INTEGER NOT NULL DEFAULT 0,
    source TEXT DEFAULT 'rule'
);
CREATE TABLE IF NOT EXISTS skill_states (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    category TEXT DEFAULT '',
    version TEXT DEFAULT '1.0.0',
    source TEXT DEFAULT 'builtin',
    runs INTEGER NOT NULL DEFAULT 0,
    last_run TEXT DEFAULT '',
    installed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    verdict TEXT DEFAULT '',
    score INTEGER,
    summary TEXT DEFAULT '',
    payload TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprises (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    note TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_login TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    value TEXT DEFAULT '',
    category TEXT DEFAULT '其他',
    source TEXT DEFAULT 'manual',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    category TEXT DEFAULT 'other',
    filename TEXT DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    text TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    heading TEXT DEFAULT '',
    text TEXT NOT NULL,
    chars INTEGER NOT NULL DEFAULT 0,
    emb BLOB
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id, ord);
-- 本项目资料：用户为「这一次投标」单独提供的依据材料，与企业级素材库分开。
-- 生成/改写正文时优先采用，避免全企业检索把别的项目材料带进来。
CREATE TABLE IF NOT EXISTS project_materials (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    enterprise_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    category TEXT DEFAULT 'other',
    filename TEXT DEFAULT '',
    text TEXT DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_materials_pid ON project_materials(project_id);
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    category TEXT DEFAULT 'other',
    name TEXT NOT NULL,
    holder TEXT DEFAULT '',
    cert_no TEXT DEFAULT '',
    cert_level TEXT DEFAULT '',
    issued_at TEXT DEFAULT '',
    expires_at TEXT DEFAULT '',
    filename TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_ent ON credentials(enterprise_id, category);
CREATE TABLE IF NOT EXISTS feasibility (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    outline TEXT DEFAULT '[]',
    chapters TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feasibility_ent ON feasibility(enterprise_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    step TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    trace TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ent ON jobs(enterprise_id, created_at);
CREATE TABLE IF NOT EXISTS deviations (
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT DEFAULT '',
    updated TEXT DEFAULT '',
    PRIMARY KEY (project_id, kind)
);
CREATE TABLE IF NOT EXISTS query_letters (
    project_id TEXT PRIMARY KEY,
    payload TEXT DEFAULT '',
    updated TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS section_versions (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    words INTEGER NOT NULL DEFAULT 0,
    author TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_quotes (
    project_id TEXT PRIMARY KEY,
    rows TEXT DEFAULT '[]',
    tax_rate REAL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    target_type TEXT DEFAULT '',
    target_id TEXT DEFAULT '',
    project_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_section_versions ON section_versions(section_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_ent ON audit_log(enterprise_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_log(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sections_project ON sections(project_id, ord);
CREATE INDEX IF NOT EXISTS idx_checks_project ON checks(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON facts(project_id, key);
CREATE INDEX IF NOT EXISTS idx_rejection_project ON rejection_items(project_id, ord);
CREATE INDEX IF NOT EXISTS idx_skill_runs ON skill_runs(name, created_at);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect() -> sqlite3.Connection:
    """建立连接。参数来自 .env（DB_TIMEOUT / DB_BUSY_TIMEOUT / DB_WAL）。

    busy_timeout 让并发写入（写作任务 + 前端轮询）排队等待而不是立刻抛 database is locked。
    """
    target = Path(_db_ctx.get() or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=_cfg.db_timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(_cfg.db_busy_timeout)}")
    if _cfg.db_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 轻量迁移：给旧库补 enterprise_id 列
        for table in ("projects", "kb_docs"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "enterprise_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN enterprise_id TEXT DEFAULT ''")
        # 轻量迁移：老库补列，避免用户升级后丢功能
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "doc_format" not in pcols:
            conn.execute("ALTER TABLE projects ADD COLUMN doc_format TEXT DEFAULT ''")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sections)")}
        if "emb" not in {r["name"] for r in conn.execute("PRAGMA table_info(kb_chunks)")}:
            conn.execute("ALTER TABLE kb_chunks ADD COLUMN emb BLOB")
        if "refs" not in cols:
            conn.execute("ALTER TABLE sections ADD COLUMN refs TEXT DEFAULT ''")
        if "words" not in cols:
            conn.execute("ALTER TABLE sections ADD COLUMN words INTEGER NOT NULL DEFAULT 0")
        if "target_words" not in cols:
            conn.execute("ALTER TABLE sections ADD COLUMN target_words INTEGER NOT NULL DEFAULT 0")
        if "plan" not in cols:
            conn.execute("ALTER TABLE sections ADD COLUMN plan TEXT DEFAULT ''")
        if "score" not in cols:
            conn.execute("ALTER TABLE sections ADD COLUMN score TEXT DEFAULT ''")
        # 轻量迁移：会话过期时间（老库的会话视为永久，登录一次后自动带过期时间）
        scols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "expires_at" not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT DEFAULT ''")
        _migrate_meta_blobs(conn)


def _migrate_meta_blobs(conn: sqlite3.Connection) -> None:
    """把旧版塞在 projects.meta 里的偏离表 / 质疑函搬到独立表，并从 meta 删除。

    只在升级后的第一次启动有活干；新表已有同键记录时保留新表数据（DO NOTHING），
    避免用户重新生成过的内容被旧 meta 覆盖。
    """
    rows = conn.execute(
        "SELECT id, meta FROM projects "
        "WHERE meta LIKE '%\"deviation\"%' OR meta LIKE '%\"query_letter\"%'"
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(meta, dict):
            continue
        changed = False
        tables = meta.get("deviation")
        if isinstance(tables, dict):
            for kind, payload in tables.items():
                if not isinstance(payload, dict):
                    continue
                conn.execute(
                    "INSERT INTO deviations(project_id, kind, payload, updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(project_id, kind) DO NOTHING",
                    (
                        row["id"],
                        str(kind),
                        json.dumps(payload, ensure_ascii=False),
                        str(payload.get("updated") or now()),
                    ),
                )
            meta.pop("deviation", None)
            changed = True
        letter = meta.get("query_letter")
        if isinstance(letter, dict):
            conn.execute(
                "INSERT INTO query_letters(project_id, payload, updated) VALUES(?,?,?) "
                "ON CONFLICT(project_id) DO NOTHING",
                (row["id"], json.dumps(letter, ensure_ascii=False), now()),
            )
            meta.pop("query_letter", None)
            changed = True
        if changed:
            conn.execute(
                "UPDATE projects SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
    # 历史孤儿：早期删项目没清这两张表，留下无主记录，启动时一并收掉
    conn.execute("DELETE FROM deviations WHERE project_id NOT IN (SELECT id FROM projects)")
    conn.execute("DELETE FROM query_letters WHERE project_id NOT IN (SELECT id FROM projects)")


# ---------- settings ----------
def get_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def set_setting(key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, payload),
        )


# ---------- projects ----------
def create_project(name: str, enterprise_id: str = "") -> dict:
    pid = new_id()
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(id, enterprise_id, name, created_at, updated_at) VALUES(?,?,?,?,?)",
            (pid, enterprise_id, name, ts, ts),
        )
    return get_project(pid)  # type: ignore[return-value]


def list_projects(enterprise_id: str = "") -> list[dict]:
    where, args = "", []
    if enterprise_id:
        where, args = "WHERE enterprise_id=?", [enterprise_id]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, enterprise_id, name, tender_name, stage, created_at, updated_at, "
            "length(tender_text) AS tender_len, "
            "(SELECT COUNT(*) FROM sections s WHERE s.project_id = projects.id) AS section_count, "
            "(SELECT COUNT(*) FROM sections s WHERE s.project_id = projects.id AND s.status='done') AS done_count "
            f"FROM projects {where} ORDER BY datetime(updated_at) DESC",
            args,
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(pid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["meta"] = json.loads(data["meta"]) if data.get("meta") else {}
    data["outline"] = json.loads(data["outline"]) if data.get("outline") else []
    data["doc_format"] = json.loads(data["doc_format"]) if data.get("doc_format") else {}
    return data


def update_project(pid: str, **fields: Any) -> None:
    if not fields:
        return
    for key in ("meta", "outline", "doc_format"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))


def delete_project(pid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sections WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM checks WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM rejection_items WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM facts WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM deviations WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM query_letters WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project_quotes WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM section_versions WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project_materials WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))


# ---------- sections ----------
def _norm_title(text: str) -> str:
    import re as _re

    return _re.sub(r"[\s第章节篇一二三四五六七八九十百千0-9.．、（）()：:]", "", text or "")


def replace_sections(pid: str, nodes: list[dict], preserve: bool = True) -> dict:
    """nodes: [{title, level, brief, scores?, children:[...]}]，扁平化写入。

    preserve=True（默认）时按归一化标题保留旧章节的正文/引用/计划，重新生成
    大纲不再清空已写内容——这是对标易标"已确认节点锁定"的核心保护。
    返回 {kept, total}。
    """
    flat: list[dict] = []

    def walk(items: list[dict], level: int) -> None:
        for item in items:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            scores = item.get("scores") or []
            if not isinstance(scores, list):
                scores = []
            flat.append({
                "title": title, "level": level,
                "brief": str(item.get("brief", "")),
                "scores": [str(s) for s in scores if str(s).strip()][:4],
            })
            children = item.get("children") or []
            if children:
                walk(children, level + 1)

    walk(nodes, 1)
    kept = 0
    with connect() as conn:
        old: dict[str, dict] = {}
        if preserve:
            rows = conn.execute(
                "SELECT * FROM sections WHERE project_id=? AND (content<>'' OR refs<>'' OR plan<>'')",
                (pid,),
            ).fetchall()
            old = {_norm_title(r["title"]): dict(r) for r in rows if _norm_title(r["title"])}
        conn.execute("DELETE FROM sections WHERE project_id=?", (pid,))
        for idx, item in enumerate(flat):
            prev = old.get(_norm_title(item["title"]))
            if prev is not None:
                kept += 1
                conn.execute(
                    "INSERT INTO sections(id, project_id, ord, level, title, brief, content, refs, words, "
                    "target_words, status, plan, score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id(), pid, idx, item["level"], item["title"],
                        item["brief"] or prev["brief"],
                        prev["content"], prev["refs"], prev["words"], prev["target_words"],
                        prev["status"], prev.get("plan") or "", json.dumps(item["scores"], ensure_ascii=False),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO sections(id, project_id, ord, level, title, brief, score) VALUES(?,?,?,?,?,?,?)",
                    (new_id(), pid, idx, item["level"], item["title"], item["brief"],
                     json.dumps(item["scores"], ensure_ascii=False)),
                )
    update_project(pid, stage="outline")
    return {"kept": kept, "total": len(flat)}


def replace_sections_full(pid: str, rows: list[dict]) -> dict:
    """用一份成品标书整本替换章节（含正文）。

    和 replace_sections 的区别：那个只建目录、正文留给 AI 写；这个是"用户已经写好了"，
    正文原样落库（status=imported），不保留旧内容也不清洗——用户交什么就是什么。
    rows: [{title, level, content, words?, brief?, scores?}]
    """
    total, words, kept = 0, 0, 0
    with connect() as conn:
        conn.execute("DELETE FROM sections WHERE project_id=?", (pid,))
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            scores = [str(s) for s in (row.get("scores") or []) if str(s).strip()][:4]
            content = str(row.get("content") or "")
            word_count = int(row.get("words") or 0)
            conn.execute(
                "INSERT INTO sections(id, project_id, ord, level, title, brief, content, words, "
                "status, score, target_words) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id(), pid, total, max(1, min(3, int(row.get("level") or 1))), title,
                    str(row.get("brief") or ""), content, word_count, "imported",
                    json.dumps(scores, ensure_ascii=False), int(row.get("target_words") or 0),
                ),
            )
            total += 1
            words += word_count
            if content.strip():
                kept += 1
    update_project(pid, stage="writing")
    return {"total": total, "words": words, "with_content": kept}


def list_sections(pid: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, ord, level, title, brief, content, status, refs, words, target_words, plan, score "
            "FROM sections WHERE project_id=? ORDER BY ord",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_section(sid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sections WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    data = dict(row)
    raw = data.get("refs") or ""
    try:
        data["refs"] = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        data["refs"] = []
    return data


def update_section(sid: str, **fields: Any) -> None:
    if not fields:
        return
    if "refs" in fields and not isinstance(fields["refs"], str):
        fields["refs"] = json.dumps(fields["refs"], ensure_ascii=False)
    if "plan" in fields and not isinstance(fields["plan"], str):
        fields["plan"] = json.dumps(fields["plan"], ensure_ascii=False)
    if "score" in fields and not isinstance(fields["score"], str):
        fields["score"] = json.dumps(fields["score"], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sections SET {cols} WHERE id=?", (*fields.values(), sid))


def delete_section(sid: str, cascade: bool = True) -> dict:
    """删除章节并重排 ord。cascade=True 时连带删掉它下面所有下级章节。

    非级联删除时，下级章节会被提升一级（level-1），避免留下没有父章的孤儿节点
    ——那会让导出的目录出现 1 级标题挂在 2 级标题下面的情况。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM sections WHERE id=?", (sid,)).fetchone()
        if not row:
            return {"deleted": 0, "titles": [], "project_id": ""}
        target = dict(row)
        pid, level, base_ord = target["project_id"], int(target["level"]), int(target["ord"])
        rows = conn.execute(
            "SELECT id, ord, level, title FROM sections WHERE project_id=? ORDER BY ord", (pid,)
        ).fetchall()

        start = next((i for i, r in enumerate(rows) if r["id"] == sid), None)
        if start is None:
            return {"deleted": 0, "titles": [], "project_id": pid}

        victims: list[dict] = [dict(rows[start])]
        if cascade:
            for r in rows[start + 1:]:
                if int(r["level"]) <= level:
                    break
                victims.append(dict(r))

        for item in victims:
            conn.execute("DELETE FROM sections WHERE id=?", (item["id"],))
            conn.execute("DELETE FROM section_versions WHERE section_id=?", (item["id"],))

        if not cascade:
            # 子章节提升一级，避免出现没有父章的孤儿 2 级标题
            for r in rows[start + 1:]:
                if int(r["level"]) <= level:
                    break
                conn.execute("UPDATE sections SET level=? WHERE id=?", (int(r["level"]) - 1, r["id"]))

        # 重排 ord，保持连续（导出与"上一节/下一节"都依赖它）
        remain = conn.execute(
            "SELECT id FROM sections WHERE project_id=? ORDER BY ord", (pid,)
        ).fetchall()
        for idx, r in enumerate(remain):
            conn.execute("UPDATE sections SET ord=? WHERE id=?", (idx, r["id"]))

    return {
        "deleted": len(victims),
        "titles": [v["title"] for v in victims],
        "project_id": pid,
    }


def insert_section(
    pid: str,
    *,
    ord: int,
    level: int,
    title: str,
    brief: str = "",
    content: str = "",
    status: str = "empty",
    words: int = 0,
) -> str:
    sid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sections(id, project_id, ord, level, title, brief, content, status, words) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, pid, int(ord), int(level), title, brief, content, status, int(words)),
        )
    return sid


# ---------- facts（项目级全局事实表） ----------
def list_facts(pid: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM facts WHERE project_id=? ORDER BY category, rowid", (pid,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_fact(
    pid: str,
    *,
    key: str,
    label: str,
    value: str,
    category: str = "其他",
    source: str = "manual",
) -> dict:
    """按 (project_id, key) 覆盖写入；key 是稳定标识，改 label 不影响引用。"""
    ts = now()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM facts WHERE project_id=? AND key=?", (pid, key)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE facts SET label=?, value=?, category=?, source=?, updated_at=? WHERE id=?",
                (label, value, category, source, ts, row["id"]),
            )
            fid = row["id"]
        else:
            fid = new_id()
            conn.execute(
                "INSERT INTO facts(id, project_id, key, label, value, category, source, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, pid, key, label, value, category, source, ts),
            )
    return {"id": fid, "project_id": pid, "key": key, "label": label, "value": value,
            "category": category, "source": source, "updated_at": ts}


def get_fact(fid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    return dict(row) if row else None


def delete_fact(fid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE id=?", (fid,))


def delete_project_facts(pid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE project_id=?", (pid,))


# ---------- checks ----------
def replace_checks(pid: str, items: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM checks WHERE project_id=?", (pid,))
        for item in items:
            conn.execute(
                "INSERT INTO checks(id, project_id, code, level, title, detail, evidence, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    new_id(),
                    pid,
                    item.get("code", ""),
                    item.get("level", "中"),
                    item.get("title", ""),
                    item.get("detail", ""),
                    item.get("evidence", ""),
                    now(),
                ),
            )
    update_project(pid, stage="checked")


def list_checks(pid: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT code, level, title, detail, evidence FROM checks WHERE project_id=? ORDER BY code",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- rejection_items（废标条款清单） ----------
REJECTION_FIELDS = (
    "ord", "category", "clause", "basis", "check_hint",
    "status", "confidence", "reason", "evidence", "note", "confirmed", "source",
)


def replace_rejections(pid: str, items: list[dict], keep: dict[str, dict] | None = None) -> int:
    """整体替换清单。keep: {归一化条款前 30 字: 旧记录}，用于保留人工确认结果。"""
    keep = keep or {}
    with connect() as conn:
        conn.execute("DELETE FROM rejection_items WHERE project_id=?", (pid,))
        for idx, item in enumerate(items):
            row = {key: item.get(key) for key in REJECTION_FIELDS}
            old = keep.get(str(item.get("_key") or ""))
            if old:
                row["status"] = old.get("status") or row["status"]
                row["note"] = old.get("note") or ""
                row["confirmed"] = old.get("confirmed") or 0
            conn.execute(
                "INSERT INTO rejection_items(id, project_id, ord, category, clause, basis, check_hint, "
                "status, confidence, reason, evidence, note, confirmed, source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id(), pid, int(row.get("ord") if row.get("ord") is not None else idx),
                    row.get("category") or "", row.get("clause") or "", row.get("basis") or "",
                    row.get("check_hint") or "", row.get("status") or "pending",
                    row.get("confidence") or "", row.get("reason") or "", row.get("evidence") or "",
                    row.get("note") or "", int(row.get("confirmed") or 0), row.get("source") or "rule",
                ),
            )
    return len(items)


def list_rejections(pid: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rejection_items WHERE project_id=? ORDER BY ord, rowid", (pid,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_rejection(iid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM rejection_items WHERE id=?", (iid,)).fetchone()
    return dict(row) if row else None


def update_rejection(iid: str, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in REJECTION_FIELDS}
    if not allowed:
        return
    cols = ", ".join(f"{k}=?" for k in allowed)
    with connect() as conn:
        conn.execute(f"UPDATE rejection_items SET {cols} WHERE id=?", (*allowed.values(), iid))


def add_rejection(pid: str, **fields: Any) -> str:
    iid = new_id()
    with connect() as conn:
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) AS m FROM rejection_items WHERE project_id=?", (pid,)
        ).fetchone()["m"]
        conn.execute(
            "INSERT INTO rejection_items(id, project_id, ord, category, clause, basis, check_hint, "
            "status, confidence, reason, evidence, note, confirmed, source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                iid, pid, int(max_ord) + 1, fields.get("category") or "其他",
                fields.get("clause") or "", fields.get("basis") or "", fields.get("check_hint") or "",
                fields.get("status") or "pending", "", "", "", "", 0, "manual",
            ),
        )
    return iid


def delete_rejection(iid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM rejection_items WHERE id=?", (iid,))


# ---------- skill_states（技能启停与安装状态） ----------
def list_skill_states() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM skill_states").fetchall()
    return {r["name"]: dict(r) for r in rows}


def set_skill_state(
    name: str,
    *,
    enabled: bool | None = None,
    category: str | None = None,
    version: str | None = None,
    source: str | None = None,
) -> None:
    with connect() as conn:
        row = conn.execute("SELECT name FROM skill_states WHERE name=?", (name,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO skill_states(name, enabled, category, version, source, installed_at) "
                "VALUES(?,?,?,?,?,?)",
                (name, 1 if (enabled is None or enabled) else 0, category or "", version or "1.0.0",
                 source or "builtin", now()),
            )
            return
        fields = {
            "enabled": None if enabled is None else (1 if enabled else 0),
            "category": category,
            "version": version,
            "source": source,
        }
        payload = {k: v for k, v in fields.items() if v is not None}
        if not payload:
            return
        cols = ", ".join(f"{k}=?" for k in payload)
        conn.execute(f"UPDATE skill_states SET {cols} WHERE name=?", (*payload.values(), name))


def delete_skill_state(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM skill_states WHERE name=?", (name,))


def bump_skill_run(name: str) -> None:
    ts = now()
    with connect() as conn:
        row = conn.execute("SELECT name FROM skill_states WHERE name=?", (name,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO skill_states(name, enabled, runs, last_run, installed_at) VALUES(?,1,1,?,?)",
                (name, ts, ts),
            )
        else:
            conn.execute("UPDATE skill_states SET runs = runs + 1, last_run=? WHERE name=?", (ts, name))


# ---------- skill_runs（运行记录） ----------
def add_skill_run(
    name: str,
    *,
    project_id: str = "",
    verdict: str = "",
    score: int | None = None,
    summary: str = "",
    payload: dict | None = None,
) -> str:
    rid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO skill_runs(id, name, project_id, verdict, score, summary, payload, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                rid, name, project_id, verdict, score, summary,
                json.dumps(payload or {}, ensure_ascii=False), now(),
            ),
        )
    return rid


def list_skill_runs(name: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT id, name, project_id, verdict, score, summary, created_at FROM skill_runs"
    args: tuple = ()
    if name:
        sql += " WHERE name=?"
        args = (name,)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def get_skill_run(rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM skill_runs WHERE id=?", (rid,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["payload"] = json.loads(data["payload"]) if data.get("payload") else {}
    return data


# ---------- kb_docs / kb_chunks（企业素材知识库） ----------
def create_kb_doc(
    *,
    title: str,
    category: str,
    filename: str,
    text: str,
    chunks: list[dict],
    note: str = "",
    enterprise_id: str = "",
) -> str:
    did = new_id()
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO kb_docs(id, enterprise_id, title, category, filename, chars, chunk_count, text, note, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (did, enterprise_id, title, category, filename, len(text), len(chunks), text, note, ts, ts),
        )
        for idx, chunk in enumerate(chunks):
            conn.execute(
                "INSERT INTO kb_chunks(id, doc_id, ord, heading, text, chars) VALUES(?,?,?,?,?,?)",
                (new_id(), did, idx, chunk.get("heading") or "", chunk["text"], len(chunk["text"])),
            )
    return did


def list_kb_docs(category: str = "", keyword: str = "", enterprise_id: str = "") -> list[dict]:
    sql = (
        "SELECT id, enterprise_id, title, category, filename, chars, chunk_count, note, created_at, updated_at "
        "FROM kb_docs WHERE 1=1"
    )
    args: list = []
    if enterprise_id:
        sql += " AND enterprise_id=?"
        args.append(enterprise_id)
    if category:
        sql += " AND category=?"
        args.append(category)
    if keyword:
        sql += " AND (title LIKE ? OR note LIKE ? OR text LIKE ?)"
        like = f"%{keyword}%"
        args.extend([like, like, like])
    sql += " ORDER BY datetime(updated_at) DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_kb_doc(did: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM kb_docs WHERE id=?", (did,)).fetchone()
    return dict(row) if row else None


def update_kb_doc(
    did: str,
    *,
    title: str | None = None,
    category: str | None = None,
    note: str | None = None,
    text: str | None = None,
    chunks: list[dict] | None = None,
) -> None:
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if category is not None:
        fields["category"] = category
    if note is not None:
        fields["note"] = note
    if text is not None:
        fields["text"] = text
        fields["chars"] = len(text)
    if chunks is not None:
        fields["chunk_count"] = len(chunks)
    fields["updated_at"] = now()

    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE kb_docs SET {cols} WHERE id=?", (*fields.values(), did))
        if chunks is not None:
            conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
            for idx, chunk in enumerate(chunks):
                conn.execute(
                    "INSERT INTO kb_chunks(id, doc_id, ord, heading, text, chars) VALUES(?,?,?,?,?,?)",
                    (new_id(), did, idx, chunk.get("heading") or "", chunk["text"], len(chunk["text"])),
                )


def delete_kb_doc(did: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
        conn.execute("DELETE FROM kb_docs WHERE id=?", (did,))


def list_kb_chunks(did: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, ord, heading, text, chars FROM kb_chunks WHERE doc_id=? ORDER BY ord", (did,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- 本项目资料 ----------------
def add_project_material(
    pid: str,
    title: str,
    text: str,
    *,
    category: str = "other",
    filename: str = "",
    enterprise_id: str = "",
) -> dict:
    from datetime import datetime as _dt
    import uuid as _uuid

    mid = _uuid.uuid4().hex[:12]
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_materials "
            "(id, project_id, enterprise_id, title, category, filename, text, chars, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, pid, enterprise_id, title, category, filename, text, len(text or ""), now),
        )
    return {"id": mid, "title": title, "category": category, "filename": filename,
            "chars": len(text or ""), "created_at": now}


def list_project_materials(pid: str, with_text: bool = False) -> list[dict]:
    cols = "*" if with_text else "id, project_id, title, category, filename, chars, created_at"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM project_materials WHERE project_id=? ORDER BY datetime(created_at) ASC",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_project_material(pid: str, mid: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM project_materials WHERE id=? AND project_id=?", (mid, pid)
        )
    return cur.rowcount > 0


def all_kb_chunks(enterprise_id: str = "") -> list[dict]:
    where, args = "", []
    if enterprise_id:
        where, args = "WHERE d.enterprise_id=?", [enterprise_id]
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_chunks)")}
        emb_col = ", c.emb" if "emb" in cols else ", NULL AS emb"
        rows = conn.execute(
            "SELECT c.id, c.doc_id, c.ord, c.heading, c.text, c.chars" + emb_col + ", "
            "d.title AS doc_title, d.category AS category "
            "FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id "
            f"{where} ORDER BY c.doc_id, c.ord",
            args,
        ).fetchall()
    return [dict(r) for r in rows]


def set_chunk_emb(chunk_id: str, blob: bytes) -> None:
    with connect() as conn:
        conn.execute("UPDATE kb_chunks SET emb=? WHERE id=?", (blob, chunk_id))


def clear_chunk_emb(enterprise_id: str = "") -> int:
    """换向量模型时清空旧向量：维度不同算不出余弦，留着只会让语义召回全变 0 分。"""
    where, args = "WHERE 1=1", []
    if enterprise_id:
        where, args = "WHERE doc_id IN (SELECT id FROM kb_docs WHERE enterprise_id=?)", [enterprise_id]
    with connect() as conn:
        cur = conn.execute(f"UPDATE kb_chunks SET emb=NULL {where}", args)
    return cur.rowcount or 0


def emb_stats(enterprise_id: str = "") -> tuple[int, int]:
    """返回 (有向量分块数, 总分块数)。"""
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_chunks WHERE doc_id IN "
            "(SELECT id FROM kb_docs " + ("WHERE enterprise_id=?" if enterprise_id else "") + ")",
            [enterprise_id] if enterprise_id else [],
        ).fetchone()["n"]
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_chunks WHERE emb IS NOT NULL AND doc_id IN "
            "(SELECT id FROM kb_docs " + ("WHERE enterprise_id=?" if enterprise_id else "") + ")",
            [enterprise_id] if enterprise_id else [],
        ).fetchone()["n"]
    return done, total


def kb_version(enterprise_id: str = "") -> str:
    where, args = "", []
    if enterprise_id:
        where, args = "WHERE enterprise_id=?", [enterprise_id]
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS ts FROM kb_docs {where}", args
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_chunks WHERE doc_id IN "
            "(SELECT id FROM kb_docs " + ("WHERE enterprise_id=?" if enterprise_id else "") + ")",
            args,
        ).fetchone()["n"]
    return f"{row['n']}-{chunks}-{row['ts']}"


def kb_stats(enterprise_id: str = "") -> dict:
    where, args = "", []
    if enterprise_id:
        where, args = "WHERE enterprise_id=?", [enterprise_id]
    with connect() as conn:
        docs = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(chars), 0) AS c FROM kb_docs {where}", args
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_chunks WHERE doc_id IN "
            f"(SELECT id FROM kb_docs {where})", args
        ).fetchone()["n"]
        by_cat = conn.execute(
            "SELECT category, COUNT(*) AS n, COALESCE(SUM(chars), 0) AS c, "
            "COALESCE(SUM(chunk_count), 0) AS k "
            f"FROM kb_docs {where} GROUP BY category", args
        ).fetchall()
    return {
        "docs": docs["n"],
        "chars": docs["c"],
        "chunks": chunks,
        "by_category": {r["category"]: r["n"] for r in by_cat},
        # 分类明细供知识库总览页画覆盖矩阵（份数 / 字数 / 片段数）
        "category_detail": {
            r["category"]: {"docs": r["n"], "chars": r["c"], "chunks": r["k"]} for r in by_cat
        },
    }


# ---------- credentials（企业资信库） ----------
def list_credentials(enterprise_id: str, category: str = "") -> list[dict]:
    sql = "SELECT * FROM credentials WHERE enterprise_id=?"
    args: list = [enterprise_id]
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY expires_at IS NULL, expires_at, created_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_credential(cid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()
    return dict(row) if row else None


def add_credential(enterprise_id: str, fields: dict) -> dict:
    item = {
        "id": new_id(),
        "enterprise_id": enterprise_id,
        "category": (fields.get("category") or "other").strip(),
        "name": (fields.get("name") or "").strip(),
        "holder": (fields.get("holder") or "").strip(),
        "cert_no": (fields.get("cert_no") or "").strip(),
        "cert_level": (fields.get("cert_level") or "").strip(),
        "issued_at": (fields.get("issued_at") or "").strip(),
        "expires_at": (fields.get("expires_at") or "").strip(),
        "filename": (fields.get("filename") or "").strip(),
        "note": (fields.get("note") or "").strip(),
        "created_at": now(),
        "updated_at": now(),
    }
    if not item["name"]:
        raise ValueError("证件名称不能为空")
    with connect() as conn:
        keys = ", ".join(item.keys())
        marks = ", ".join("?" for _ in item)
        conn.execute(f"INSERT INTO credentials({keys}) VALUES({marks})", list(item.values()))
    return item


def update_credential(cid: str, fields: dict) -> dict | None:
    cur = get_credential(cid)
    if not cur:
        return None
    editable = ("category", "name", "holder", "cert_no", "cert_level", "issued_at", "expires_at", "note")
    updates = {k: (str(fields.get(k, cur.get(k)) or "").strip()) for k in editable}
    updates["updated_at"] = now()
    with connect() as conn:
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE credentials SET {sets} WHERE id=?", [*updates.values(), cid])
    return get_credential(cid)


def delete_credential(cid: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM credentials WHERE id=?", (cid,))
        return cur.rowcount > 0


def get_enterprise_profile(enterprise_id: str) -> dict:
    return get_setting(f"ent_profile:{enterprise_id}", {}) or {}


# ---------- feasibility（可行性研究报告） ----------
def list_feasibility(enterprise_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, params, outline, chapters, created_at, updated_at FROM feasibility "
            "WHERE enterprise_id=? ORDER BY created_at DESC",
            (enterprise_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_feasibility(fid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM feasibility WHERE id=?", (fid,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["params"] = json.loads(item["params"] or "{}")
    item["outline"] = json.loads(item["outline"] or "[]")
    item["chapters"] = json.loads(item["chapters"] or "[]")
    return item


def add_feasibility(enterprise_id: str, title: str, params: dict) -> dict:
    fid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO feasibility(id, enterprise_id, title, params, outline, chapters, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (fid, enterprise_id, title, json.dumps(params, ensure_ascii=False), "[]", "[]", now(), now()),
        )
    return get_feasibility(fid)


def update_feasibility(fid: str, *, title: str | None = None, params: dict | None = None,
                       outline: list | None = None, chapters: list | None = None) -> dict | None:
    cur = get_feasibility(fid)
    if not cur:
        return None
    sets = ["updated_at=?"]
    args: list = [now()]
    if title is not None:
        sets.append("title=?")
        args.append(title)
    if params is not None:
        sets.append("params=?")
        args.append(json.dumps(params, ensure_ascii=False))
    if outline is not None:
        sets.append("outline=?")
        args.append(json.dumps(outline, ensure_ascii=False))
    if chapters is not None:
        sets.append("chapters=?")
        args.append(json.dumps(chapters, ensure_ascii=False))
    args.append(fid)
    with connect() as conn:
        conn.execute(f"UPDATE feasibility SET {', '.join(sets)} WHERE id=?", args)
    return get_feasibility(fid)


def delete_feasibility(fid: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM feasibility WHERE id=?", (fid,)).rowcount > 0


# ---------- jobs（后台任务队列） ----------
def create_job(*, kind: str, title: str, payload: dict, enterprise_id: str = "") -> dict:
    jid = new_id()
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id, enterprise_id, kind, title, payload, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (jid, enterprise_id, kind, title, json.dumps(payload, ensure_ascii=False), "queued", ts, ts),
        )
    return get_job(jid)  # type: ignore[return-value]


def get_job(jid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item["payload"] or "{}")
    except json.JSONDecodeError:
        item["payload"] = {}
    try:
        item["result"] = json.loads(item["result"]) if item["result"] else None
    except json.JSONDecodeError:
        item["result"] = item["result"]
    return item


def list_jobs(enterprise_id: str = "", limit: int = 30) -> list[dict]:
    sql = (
        "SELECT id, kind, title, status, progress, step, error, created_at, updated_at "
        "FROM jobs"
    )
    args: list = []
    if enterprise_id:
        sql += " WHERE enterprise_id=?"
        args.append(enterprise_id)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def update_job(jid: str, **fields: Any) -> None:
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    if "payload" in fields and not isinstance(fields["payload"], str):
        fields["payload"] = json.dumps(fields["payload"], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))


def recover_jobs() -> None:
    """服务启动：把上次进程里没跑完的任务标记为中断。"""
    with connect() as conn:
        for status in ("running", "pausing", "queued"):
            conn.execute(
                "UPDATE jobs SET status='error', error='服务重启，任务中断', step='中断', updated_at=? "
                "WHERE status=?",
                (now(), status),
            )


def set_enterprise_profile(enterprise_id: str, profile: dict) -> dict:
    cur = get_enterprise_profile(enterprise_id)
    cur.update(profile or {})
    set_setting(f"ent_profile:{enterprise_id}", cur)
    return cur


# ---------- 企业 / 用户 / 会话 ----------
def first_enterprise() -> dict | None:
    """最早创建的企业，作为管理员未指定 X-Enterprise 时的默认工作区。

    不能用 list_enterprises()[0]：那个按创建时间倒序，新建一家企业就会把
    管理员的默认工作区切走，导致「昨天的项目突然 404」。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM enterprises ORDER BY datetime(created_at) ASC, rowid ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def list_enterprises() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.*, "
            "(SELECT COUNT(*) FROM users u WHERE u.enterprise_id=e.id) AS user_count, "
            "(SELECT COUNT(*) FROM projects p WHERE p.enterprise_id=e.id) AS project_count "
            "FROM enterprises e ORDER BY datetime(e.created_at) DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_enterprise(eid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM enterprises WHERE id=?", (eid,)).fetchone()
    return dict(row) if row else None


def create_enterprise(name: str, note: str = "") -> dict:
    eid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO enterprises(id, name, note, created_at) VALUES(?,?,?,?)",
            (eid, name, note, now()),
        )
    return get_enterprise(eid)  # type: ignore[return-value]


def update_enterprise(eid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE enterprises SET {cols} WHERE id=?", (*fields.values(), eid))


def delete_enterprise(eid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM enterprises WHERE id=?", (eid,))
        conn.execute("DELETE FROM users WHERE enterprise_id=?", (eid,))


PBKDF2_ROUNDS = 120_000
_LEGACY_SALT = "bs::"


def hash_password(raw: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，格式：pbkdf2_sha256$轮数$盐$摘要。

    旧版本用的是无盐 sha256（bs:: 前缀）。这里保留 verify_password 兼容，
    老用户仍能用原密码登录，登录成功后由 main 侧自动升级为新格式。
    """
    import hashlib
    import secrets

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def legacy_hash_password(raw: str) -> str:
    """0.1.x 的哈希方式，仅用于校验老数据。"""
    import hashlib

    return hashlib.sha256((_LEGACY_SALT + raw).encode("utf-8")).hexdigest()


def verify_password(raw: str, stored: str) -> bool:
    """校验密码，兼容旧格式。返回 True 时调用方可考虑重新哈希升级。"""
    import hashlib

    stored = (stored or "").strip()
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt, digest = stored.split("$", 3)
            calc = hashlib.pbkdf2_hmac(
                "sha256", (raw or "").encode("utf-8"), salt.encode("utf-8"), int(rounds)
            ).hex()
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(calc, digest)
    return hmac.compare_digest(legacy_hash_password(raw), stored)


def password_needs_rehash(stored: str) -> bool:
    return not (stored or "").startswith("pbkdf2_sha256$")


def create_user(
    *,
    username: str,
    password: str,
    enterprise_id: str = "",
    role: str = "member",
    display_name: str = "",
) -> dict | None:
    uid = new_id()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO users(id, enterprise_id, username, password, display_name, role, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, enterprise_id, username, hash_password(password), display_name, role, now()),
            )
    except sqlite3.IntegrityError:
        return None
    return get_user(uid)


def list_users(enterprise_id: str = "") -> list[dict]:
    where, args = "", []
    if enterprise_id:
        where, args = "WHERE enterprise_id=?", [enterprise_id]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, enterprise_id, username, display_name, role, status, created_at, last_login "
            f"FROM users {where} ORDER BY datetime(created_at) DESC",
            args,
        ).fetchall()
    return [dict(r) for r in rows]


def get_user(uid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def get_user_by_name(username: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def update_user(uid: str, **fields: Any) -> None:
    fields.pop("id", None)
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))


def delete_user(uid: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


def touch_login(uid: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now(), uid))


def create_session(user_id: str, ttl_hours: int | None = None) -> str:
    """新建登录会话。ttl_hours<=0 表示永不过期（不建议，仅兼容老行为）。"""
    import secrets

    ttl = _cfg.session_ttl_hours if ttl_hours is None else int(ttl_hours)
    expires = ""
    if ttl != 0:
        # 负数表示"过去的时间"，即已过期（测试与强制下线用）；0 表示永不过期
        expires = (datetime.now() + timedelta(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    token = secrets.token_hex(32)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, user_id, now(), expires),
        )
    return token


def get_session_user(token: str) -> dict | None:
    """按 token 取用户，过期会话顺手删掉。

    老库的会话 expires_at 为空，视为不过期（升级后第一次登录会带新过期时间）。
    """
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at AS _session_expires FROM sessions s "
            "JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
    if not row:
        return None
    expires = (row["_session_expires"] or "").strip() if "_session_expires" in row.keys() else ""
    if expires and expires < now():
        delete_session(token)
        return None
    data = dict(row)
    data.pop("_session_expires", None)
    return data


def delete_session(token: str) -> bool:
    """吊销会话（退出登录 / 改密码后强制重登都用它）。"""
    if not token:
        return False
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    return cur.rowcount > 0


def purge_expired_sessions() -> int:
    """清理过期会话，可定期调用；返回删除条数。"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at != '' AND expires_at < ?", (now(),))
    return cur.rowcount


def delete_user_sessions(user_id: str, keep_token: str = "") -> int:
    """踢掉某用户的全部会话（管理员停用账号 / 用户改密码时用）。"""
    with connect() as conn:
        if keep_token:
            cur = conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user_id, keep_token))
        else:
            cur = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return cur.rowcount


def admin_stats() -> dict:
    with connect() as conn:
        ent = conn.execute("SELECT COUNT(*) FROM enterprises").fetchone()[0]
        usr = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        proj = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*), COALESCE(SUM(chars),0) FROM kb_docs").fetchone()
        recent = conn.execute(
            "SELECT r.name, r.verdict, r.score, r.created_at, "
            "p.name AS project FROM skill_runs r LEFT JOIN projects p ON p.id=r.project_id "
            "ORDER BY datetime(r.created_at) DESC LIMIT 10"
        ).fetchall()
        logins = conn.execute(
            "SELECT username, display_name, last_login FROM users "
            "WHERE last_login<>'' ORDER BY datetime(last_login) DESC LIMIT 10"
        ).fetchall()
    return {
        "enterprises": ent,
        "users": usr,
        "projects": proj,
        "skill_runs": runs,
        "kb_docs": docs[0],
        "kb_chars": docs[1],
        "recent_runs": [dict(r) for r in recent],
        "recent_logins": [dict(r) for r in logins],
    }


# ---------- 偏离表 / 质疑函（独立表，不塞 meta） ----------
def get_deviation(project_id: str, kind: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM deviations WHERE project_id=? AND kind=?", (project_id, kind)
        ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_deviation(project_id: str, kind: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO deviations(project_id, kind, payload, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(project_id, kind) DO UPDATE SET payload=excluded.payload, updated=excluded.updated",
            (
                project_id,
                kind,
                json.dumps(payload, ensure_ascii=False),
                str(payload.get("updated") or now()),
            ),
        )


def list_deviation_kinds(project_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT kind FROM deviations WHERE project_id=?", (project_id,)).fetchall()
    return [r["kind"] for r in rows]


def get_query_letter(project_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM query_letters WHERE project_id=?", (project_id,)
        ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_query_letter(project_id: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO query_letters(project_id, payload, updated) VALUES(?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET payload=excluded.payload, updated=excluded.updated",
            (project_id, json.dumps(payload, ensure_ascii=False), now()),
        )


# ---------------- 项目报价明细 ----------------
def get_project_quote(project_id: str) -> dict | None:
    """返回 {rows, tax_rate, updated_at}；没保存过返回 None。

    报价明细按项目落库，是为了「填写一次、打包时直接引用」——
    否则导出页拿不到报价页内存里的那几十行数据。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT rows, tax_rate, updated_at FROM project_quotes WHERE project_id=?",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    try:
        rows = json.loads(row["rows"] or "[]")
    except json.JSONDecodeError:
        rows = []
    if not isinstance(rows, list):
        rows = []
    return {
        "rows": rows,
        "tax_rate": row["tax_rate"],
        "updated_at": row["updated_at"],
    }


def save_project_quote(project_id: str, rows: list[dict], tax_rate: float | None = None) -> dict:
    payload = json.dumps(rows, ensure_ascii=False)
    stamp = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_quotes(project_id, rows, tax_rate, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET rows=excluded.rows, "
            "tax_rate=excluded.tax_rate, updated_at=excluded.updated_at",
            (project_id, payload, tax_rate, stamp),
        )
    return {"rows": rows, "tax_rate": tax_rate, "updated_at": stamp}


# ---------------- 章节版本 ----------------
def add_section_version(
    section_id: str,
    project_id: str,
    *,
    title: str,
    content: str,
    words: int = 0,
    author: str = "",
    note: str = "",
) -> str:
    vid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO section_versions(id, section_id, project_id, title, content, words, author, note, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (vid, section_id, project_id, title or "", content or "", int(words or 0), author or "", note or "", now()),
        )
        # 每个章节只留最近 50 个快照：正文是长文本，不设上限会把库撑爆
        conn.execute(
            "DELETE FROM section_versions WHERE section_id=? AND id NOT IN ("
            "SELECT id FROM section_versions WHERE section_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 50"
            ")",
            (section_id, section_id),
        )
    return vid


def list_section_versions(section_id: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, section_id, title, words, author, note, created_at, content "
            "FROM section_versions WHERE section_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (section_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_section_version(vid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM section_versions WHERE id=?", (vid,)).fetchone()
    return dict(row) if row else None


# ---------------- 操作审计 ----------------
def add_audit(
    *,
    action: str,
    enterprise_id: str = "",
    user_id: str = "",
    username: str = "",
    target_type: str = "",
    target_id: str = "",
    project_id: str = "",
    detail: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(id, enterprise_id, user_id, username, action, target_type, target_id, project_id, detail, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_id(), enterprise_id or "", user_id or "", username or "", action, target_type or "", target_id or "", project_id or "", detail or "", now()),
        )


def list_audit(
    *,
    enterprise_id: str = "",
    project_id: str = "",
    limit: int = 200,
    offset: int = 0,
    since: str = "",
    until: str = "",
) -> list[dict]:
    where: list[str] = []
    args: list = []
    if enterprise_id:
        where.append("enterprise_id=?")
        args.append(enterprise_id)
    if project_id:
        where.append("project_id=?")
        args.append(project_id)
    # 时间筛选：since/until 为 ISO 日期或日期时间（2026-11-08 或 2026-11-08T09:30）
    if since:
        where.append("created_at >= ?")
        args.append(_norm_dt(since, start=True))
    if until:
        where.append("created_at <= ?")
        args.append(_norm_dt(until, start=False))
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
    args.extend([int(limit), int(offset)])
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count_audit(
    *,
    enterprise_id: str = "",
    project_id: str = "",
    since: str = "",
    until: str = "",
) -> int:
    where: list[str] = []
    args: list = []
    if enterprise_id:
        where.append("enterprise_id=?")
        args.append(enterprise_id)
    if project_id:
        where.append("project_id=?")
        args.append(project_id)
    if since:
        where.append("created_at >= ?")
        args.append(_norm_dt(since, start=True))
    if until:
        where.append("created_at <= ?")
        args.append(_norm_dt(until, start=False))
    sql = "SELECT COUNT(*) FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def _norm_dt(value: str, *, start: bool) -> str:
    """把「2026-11-08」或「2026-11-08T09:30」归一成 created_at 的「2026-11-08 09:30:00」可比格式。

    纯日期按整天算：start=True 归到 00:00:00，False 归到 23:59:59。
    """
    value = (value or "").strip().replace("T", " ").replace("t", " ")
    if len(value) == 10:
        value += " 23:59:59" if not start else " 00:00:00"
    elif len(value) == 13:      # 2026-11-08 09
        value += ":59:59" if not start else ":00:00"
    elif len(value) == 16:      # 2026-11-08 09:30
        value += ":59" if not start else ":00"
    return value
