"""SQLite 存储：项目、资料、知识库、章节、检查记录、任务。

每次调用独立连接，后台线程与请求线程都可安全使用。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import ctxpool
from .config import DATA_DIR, DB_PATH

# 当前请求属于哪个企业的库：默认企业仍是 data/app.db，其余 data/orgs/<org_id>.db。
# 物理分库而不是给每张表加 org_id —— 少写一个 WHERE 也读不到别家数据。
_db_ctx: contextvars.ContextVar = contextvars.ContextVar("bidagent_db", default=None)


def org_db(org_id: str) -> Path:
    if not org_id or org_id == "default":
        return Path(DB_PATH)
    return Path(DATA_DIR) / "orgs" / f"{org_id}.db"


def db_path() -> Path:
    return _db_ctx.get() or Path(DB_PATH)


@contextmanager
def use_org(org_id: str):
    """切到某个企业的库。请求入口设置一次，本请求内所有 SQL 都落到这个文件。"""
    p = init_org(org_id)
    tok = _db_ctx.set(p)
    try:
        yield p
    finally:
        _db_ctx.reset(tok)


_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def init_org(org_id: str) -> Path:
    """企业库不存在就建好；已存在的老库在首次访问时补齐后加的表 / 列。

    结构校准每个库每个进程只做一次，之后每次请求只付一次 set 查找的开销，
    避免「老库没有 doc_chapters 表 → 范例功能静默失效」这类问题。
    """
    p = org_db(org_id)
    key = str(p.resolve())
    fresh = not (p.exists() and p.stat().st_size > 0)
    if fresh:
        init_db(p)
        _SCHEMA_READY.add(key)
        if org_id != "default":
            tok = _db_ctx.set(p)
            try:
                from . import seed
                seed.ensure_workspace()
            finally:
                _db_ctx.reset(tok)
        return p
    if key not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if key not in _SCHEMA_READY:
                _apply_schema(p)
                _SCHEMA_READY.add(key)
    return p


def submit_job(fn, *args):
    """后台任务：把当前企业上下文带进工作线程，否则任务里读知识库会落到默认库。"""
    return ctxpool.submit_business(JOB_POOL, fn, *args)

# 后台长任务单队列：同一时刻只跑一个 compose / optimize，不会并发写同一份标书
# C03：并发数不靠猜，见 concurrency_advice() 的实测排队时间与进程锁边界说明
JOB_WORKERS = 1
JOB_POOL = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="bidagent-job")

# 历史表保留策略（只作用于运行日志与快照，绝不动 projects / sections / checks 正文）
KEEP_DAYS = 90
KEEP_BATCHES = 50

_TASK_TOKENS: dict[str, int] = {}
_TASK_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def conn(path: Path | str | None = None):
    """每次调用独立连接：busy_timeout + 提交/回滚 + 真正 close()。

    旧写法 `with conn() as c` 只提交事务、不关闭连接，长跑会积累文件句柄。
    contextmanager 对现有全部 `with conn() as c` 调用点保持兼容。
    不传 path 时用当前企业库（见 use_org）。
    """
    target = str(path or db_path())
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(target, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=20000")
        with c:
            yield c
    finally:
        c.close()


def _enable_wal(path: Path | str | None = None) -> None:
    """WAL 必须在事务外设置；只在启动时做一次。"""
    c = sqlite3.connect(str(path or db_path()), timeout=20)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    finally:
        c.close()


# ---------------- 单任务 token 计数 ----------------

def reset_task_tokens(pid: str) -> None:
    with _TASK_LOCK:
        _TASK_TOKENS[str(db_path()) + ":" + pid] = 0


def add_task_tokens(pid: str, n: int) -> int:
    with _TASK_LOCK:
        key = str(db_path()) + ":" + pid
        _TASK_TOKENS[key] = _TASK_TOKENS.get(key, 0) + max(0, int(n or 0))
        return _TASK_TOKENS[key]


def task_tokens(pid: str) -> int:
    with _TASK_LOCK:
        return _TASK_TOKENS.get(str(db_path()) + ":" + pid, 0)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tender_text TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    outline TEXT DEFAULT '[]',
    archived_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'project',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    page_map TEXT DEFAULT '[]',
    kind TEXT DEFAULT 'material',
    n_chapters INTEGER DEFAULT 0,
    source_pid TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
-- 范例按章拆分后的正文：写某一章时按「同名章节」参考，而不是拿零散段落拼
CREATE TABLE IF NOT EXISTS doc_chapters (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    idx INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    level INTEGER DEFAULT 1,
    content TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_chapters ON doc_chapters(doc_id, idx);
CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT DEFAULT '',
    protected INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    ver INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT DEFAULT '',
    gate_fingerprint TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_pid ON artifacts(project_id, created_at);
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    section_id TEXT DEFAULT '',
    anchor TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    confidence REAL DEFAULT 0.90,
    source_ref TEXT DEFAULT '',
    page INTEGER DEFAULT 0,
    bbox TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS section_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    text TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sec_versions_pid ON section_versions(project_id, batch_id, id);
CREATE TABLE IF NOT EXISTS outline_backups (
    project_id TEXT NOT NULL,
    batch_id TEXT PRIMARY KEY,
    reason TEXT DEFAULT '',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    actor TEXT DEFAULT 'agent',
    task TEXT DEFAULT '',
    tool TEXT DEFAULT '',
    model TEXT DEFAULT '',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    elapsed_ms INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    input_digest TEXT DEFAULT '',
    output_digest TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_pid ON usage(project_id, id);
CREATE TABLE IF NOT EXISTS source_pages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT DEFAULT '',
    title TEXT DEFAULT '',
    page INTEGER DEFAULT 0,
    physical INTEGER DEFAULT 0,
    source TEXT DEFAULT 'text',
    text TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_pages_pid ON source_pages(project_id, page, id);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    step TEXT DEFAULT '',
    result TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    consumed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    summary TEXT DEFAULT '',
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auto_runs_pid ON auto_runs(project_id, stage, id);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    message TEXT DEFAULT '',
    planner TEXT DEFAULT '',
    goal TEXT DEFAULT '',
    plan TEXT DEFAULT '{}',
    trace TEXT DEFAULT '[]',
    reply TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_pid ON agent_runs(project_id, id);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_pid ON messages(project_id, id);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    section_id TEXT DEFAULT '',
    scope TEXT DEFAULT '',
    title TEXT DEFAULT '',
    ref TEXT DEFAULT '',
    doc_id TEXT DEFAULT '',
    page INTEGER DEFAULT 0,
    start_pos INTEGER DEFAULT 0,
    end_pos INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    status TEXT DEFAULT 'bound',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_pid ON evidence(project_id, section_id, id);
CREATE TABLE IF NOT EXISTS section_quality (
    section_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    text_hash TEXT DEFAULT '',
    ok INTEGER DEFAULT 0,
    deliverable INTEGER DEFAULT 0,
    flags TEXT DEFAULT '{}',
    lines TEXT DEFAULT '[]',
    rounds INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sec_quality_pid ON section_quality(project_id);
CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'hard',
    text TEXT NOT NULL DEFAULT '',
    score TEXT DEFAULT '',
    source TEXT DEFAULT 'auto',
    status TEXT DEFAULT 'active',
    note TEXT DEFAULT '',
    origin_text TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_req_pid ON requirements(project_id, kind, id);
CREATE TABLE IF NOT EXISTS requirement_sync (
    project_id TEXT PRIMARY KEY,
    tender_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requirement_suppressed (
    project_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, text)
);
CREATE TABLE IF NOT EXISTS transient_tokens (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    pid TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    expires REAL NOT NULL,
    payload TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_exp ON transient_tokens(expires);
"""


def recover_stale_jobs() -> int:
    """服务重启后，上一进程留下的 queued / running 任务不可能还在执行。

    C03：标成 interrupted 而不是 failed，保留章节检查点供恢复；
    该状态不属于活跃任务，不会挡住同项目的新任务。
    """
    with conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='interrupted', progress=100, recoverable=1, "
            "finished_at=?, "
            "step='服务重启，任务已中断', "
            "error=COALESCE(NULLIF(error,''), '服务重启前任务中断；章节检查点已保留，可恢复') "
            "WHERE status IN ('queued','running','pending')", (now(),))
        return cur.rowcount


def request_cancel(jid: str) -> bool:
    """请求取消；只对还在排队 / 运行的任务生效，重复点无害。"""
    with conn() as c:
        return c.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE id=? "
            "AND status IN ('queued','running')", (jid,)).rowcount > 0


def cancel_requested(jid: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
    return bool(r and r["cancel_requested"])


def job_queue_metrics(limit: int = 50) -> dict:
    """实测排队时间：从建任务到工作线程真正开始之间的等待，不是估算。"""
    with conn() as c:
        rows = c.execute(
            "SELECT created_at, started_at, finished_at FROM jobs "
            "WHERE COALESCE(started_at,'')<>'' ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    waits: list[float] = []
    for r in rows:
        try:
            a = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
            waits.append(max(0.0, (b - a).total_seconds()))
        except (TypeError, ValueError):
            continue
    if not waits:
        return {"samples": 0, "avg_wait_s": 0.0, "max_wait_s": 0.0,
                "p95_wait_s": 0.0}
    ordered = sorted(waits)
    return {"samples": len(waits),
            "avg_wait_s": round(sum(waits) / len(waits), 2),
            "max_wait_s": round(max(waits), 2),
            "p95_wait_s": round(ordered[min(len(ordered) - 1,
                                            int(len(ordered) * 0.95))], 2)}


def concurrency_advice() -> dict:
    """按实测排队时间给并发建议；如实写出当前进程锁的边界。"""
    m = job_queue_metrics()
    if m["samples"] < 5 or m["p95_wait_s"] < 10:
        suggested, why = 1, "排队样本不足或等待很短，维持单线程"
    elif m["p95_wait_s"] < 30:
        suggested, why = 2, "偶有排队，可考虑提高到 2 个工作线程并观察"
    else:
        suggested, why = 4, "排队明显，建议先拆分长任务或引入跨进程队列再提高并发"
    return {**m, "current_workers": JOB_WORKERS,
            "suggested_workers": suggested, "why": why,
            "boundary": ("跨进程同项目写互斥已用 SQLite 原子事务实现："
                         "多个 worker 也不会并发改写同一份正文。但每个进程仍是"
                         "单线程池，启动时的遗留任务恢复会扫全库，健康报告缓存等"
                         "仍在进程内；正式多进程 / 多机部署前还要处理这两处，"
                         "不能只靠加进程直接扩容。"),
            "claim_atomic": True, "implemented": False}


def prune_history(days: int = KEEP_DAYS, batches: int = KEEP_BATCHES) -> dict:
    """日志按天保留、章节快照按批保留；标书正文 / 检查结论 / 资料一律不动。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict = {}
    with conn() as c:
        for t in ("jobs", "agent_runs", "auto_runs", "usage", "messages"):
            try:
                out[t] = c.execute(f"DELETE FROM {t} WHERE created_at < ?", (cutoff,)).rowcount
            except sqlite3.OperationalError:
                out[t] = 0
        rows = [dict(r) for r in c.execute(
            "SELECT project_id, batch_id, MAX(created_at) AS at FROM section_versions "
            "GROUP BY project_id, batch_id ORDER BY at DESC").fetchall()]
        seen: dict[str, int] = {}
        drop = []
        for r in rows:
            pid = r["project_id"]
            seen[pid] = seen.get(pid, 0) + 1
            if seen[pid] > batches:
                drop.append((pid, r["batch_id"]))
        for pid, batch in drop:
            c.execute("DELETE FROM section_versions WHERE project_id=? AND batch_id=?",
                      (pid, batch))
        out["section_versions"] = len(drop)
    return out


def retention() -> dict:
    return {"log_days": KEEP_DAYS, "snapshot_batches_per_project": KEEP_BATCHES,
            "tables": ["jobs", "agent_runs", "auto_runs", "usage", "messages",
                       "section_versions"],
            "untouched": ["projects", "sections", "checks", "kb_docs", "evidence",
                          "section_quality", "requirements", "requirement_sync",
                          "requirement_suppressed"]}


def _apply_schema(target: Path) -> None:
    """建表 + 补列。全部是 IF NOT EXISTS / PRAGMA 判断，可反复执行。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tok = _db_ctx.set(target)
    try:
        _enable_wal()
        with conn() as c:
            c.executescript(SCHEMA)
            # 老库补列：不重建表，已有项目与检查记录原样保留
            cols = {r["name"] for r in c.execute("PRAGMA table_info(checks)")}
            for name, ddl in (
                ("status", "TEXT DEFAULT 'pending'"),
                ("confidence", "REAL DEFAULT 0.90"),
                ("source_ref", "TEXT DEFAULT ''"),
                ("page", "INTEGER DEFAULT 0"),
                ("bbox", "TEXT DEFAULT ''"),
            ):
                if name not in cols:
                    c.execute(f"ALTER TABLE checks ADD COLUMN {name} {ddl}")
            doc_cols = {r["name"] for r in c.execute("PRAGMA table_info(kb_docs)")}
            if "page_map" not in doc_cols:
                c.execute("ALTER TABLE kb_docs ADD COLUMN page_map TEXT DEFAULT '[]'")
            if "kind" not in doc_cols:
                c.execute("ALTER TABLE kb_docs ADD COLUMN kind TEXT DEFAULT 'material'")
            if "n_chapters" not in doc_cols:
                c.execute("ALTER TABLE kb_docs ADD COLUMN n_chapters INTEGER DEFAULT 0")
            proj_cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
            if "sample_doc_id" not in proj_cols:
                c.execute("ALTER TABLE projects ADD COLUMN sample_doc_id TEXT DEFAULT ''")
            # P1 目录确认闸门：只在第一次补列时把「已写过正文的老项目」视为
            # 已确认，避免升级后把线上正在用的项目全部锁死。之后重启不会再回填，
            # 用户手动清掉的确认状态也不会被偷偷恢复。
            if "outline_confirmed_at" not in proj_cols:
                c.execute("ALTER TABLE projects ADD COLUMN "
                          "outline_confirmed_at TEXT DEFAULT ''")
                c.execute("""UPDATE projects SET outline_confirmed_at=created_at
                             WHERE outline_confirmed_at=''
                               AND id IN (SELECT DISTINCT project_id FROM sections
                                          WHERE length(trim(text)) > 0)""")
            # R05：招标文件解析状态（未识别页 / 待核对）随项目保存，刷新后仍可见
            if "tender_parse" not in proj_cols:
                c.execute("ALTER TABLE projects ADD COLUMN tender_parse TEXT DEFAULT ''")
            # 归档：只是从侧栏收起，正文、检查记录、导出记录都保留，随时能恢复
            if "archived_at" not in proj_cols:
                c.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT DEFAULT ''")
            # 最近一次改动时间（改正文、改招标文件都算），项目列表按它排「最近在弄的」
            if "updated_at" not in proj_cols:
                c.execute("ALTER TABLE projects ADD COLUMN updated_at TEXT DEFAULT ''")
            # 知识库里标明「这本书是从哪个项目收进来的」，重复收录时更新同一篇而不是堆副本
            if "source_pid" not in doc_cols:
                c.execute("ALTER TABLE kb_docs ADD COLUMN source_pid TEXT DEFAULT ''")
            # 收录成稿要留来源版本与核验状态：未完成 / 未审核的稿不能默认当已审核范例
            for col, default in (("source_hash", "''"), ("verification", "'unreviewed'")):
                if col not in doc_cols:
                    c.execute(f"ALTER TABLE kb_docs ADD COLUMN {col} TEXT DEFAULT {default}")
            # R05：解析状态随资料一起落库，刷新后仍能看到「哪几页没认出来」
            # R05：未识别原件单独留存，状态 pending，不进依据、不算已核查
            c.execute("""CREATE TABLE IF NOT EXISTS originals (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL,
                path TEXT NOT NULL, size INTEGER DEFAULT 0, reason TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', created_at TEXT NOT NULL)""")
            doc_parse_cols = {
                ("parse_status", "'ok'"), ("effective_chars", "0"),
                ("pages", "0"), ("scan_pages", "0"), ("ocr_pages", "0"),
                ("unread_pages", "'[]'"), ("review_note", "''"),
                ("needs_review", "0"), ("confirmed_at", "''"), ("confirmed_by", "''"),
                # 证照类素材的有效期：只填了才检查，没填不打扰用户
                ("valid_until", "''"),
            }
            for col, default in doc_parse_cols:
                if col not in doc_cols:
                    c.execute(f"ALTER TABLE kb_docs ADD COLUMN {col} "
                              f"{'INTEGER' if default.isdigit() else 'TEXT'} DEFAULT {default}")
            # R06：产物归属、版本、快照与最终文件检查；记录不再因展示名相同被删
            art_cols = {r["name"] for r in c.execute("PRAGMA table_info(artifacts)")}
            for col, default in (("org_id", "'default'"), ("version", "0"),
                                 ("is_current", "1"), ("snapshot_fingerprint", "''"),
                                 ("purpose", "''"), ("options", "''"),
                                 ("file_check", "''"), ("status", "'ok'")):
                if col not in art_cols:
                    c.execute(f"ALTER TABLE artifacts ADD COLUMN {col} "
                              f"{'INTEGER' if default.isdigit() else 'TEXT'} DEFAULT {default}")
            # B01：要求稳定来源位置、来源版本、必需性与人工确认状态
            req_cols = {r["name"] for r in c.execute("PRAGMA table_info(requirements)")}
            for col, default in (("loc", "''"), ("source_version", "''"),
                                 ("required", "1"), ("confirmed_by", "''"),
                                 ("confirmed_at", "''")):
                if col not in req_cols:
                    c.execute(f"ALTER TABLE requirements ADD COLUMN {col} "
                              f"{'INTEGER' if default.isdigit() else 'TEXT'} DEFAULT {default}")
            # B01/B02/B03/B04/C03 的业务表
            c.execute("""CREATE TABLE IF NOT EXISTS tender_versions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER DEFAULT 1,
                kind TEXT DEFAULT 'original', filename TEXT DEFAULT '', text TEXT DEFAULT '',
                content_hash TEXT DEFAULT '', supersedes TEXT DEFAULT '',
                effective INTEGER DEFAULT 1, note TEXT DEFAULT '', created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS requirement_responses (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, req_id TEXT NOT NULL,
                section_id TEXT DEFAULT '', anchor TEXT DEFAULT '', evidence TEXT DEFAULT '',
                candidate INTEGER DEFAULT 0, evidence_ok INTEGER DEFAULT 0,
                status TEXT DEFAULT 'missing', reviewer TEXT DEFAULT '',
                reviewed_at TEXT DEFAULT '', updated_at TEXT NOT NULL)""")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_req_resp "
                      "ON requirement_responses(project_id, req_id)")
            c.execute("""CREATE TABLE IF NOT EXISTS project_facts (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, entity TEXT DEFAULT '',
                field TEXT DEFAULT '', value TEXT DEFAULT '', unit TEXT DEFAULT '',
                scope TEXT DEFAULT '', valid_until TEXT DEFAULT '',
                source_doc_id TEXT DEFAULT '', source_loc TEXT DEFAULT '',
                kind TEXT DEFAULT 'enterprise_fact',
                verification TEXT DEFAULT 'unverified', created_at TEXT NOT NULL)""")
            # P2 企业资产台账：属于企业、跨项目复用，随企业分库天然隔离。
            # 不设 project_id，避免资产被某个项目绑住；现有 facts / kb_docs 不迁移。
            c.execute("""CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'qualification',
                title TEXT NOT NULL, holder TEXT DEFAULT '', code TEXT DEFAULT '',
                valid_until TEXT DEFAULT '', source_ref TEXT DEFAULT '',
                note TEXT DEFAULT '', verification TEXT DEFAULT 'unverified',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fact_id TEXT DEFAULT '',
                filename TEXT DEFAULT '', path TEXT DEFAULT '', doc_id TEXT DEFAULT '',
                section_id TEXT DEFAULT '', page INTEGER DEFAULT 0, note TEXT DEFAULT '',
                created_at TEXT NOT NULL)""")
            att_cols = {r["name"] for r in c.execute("PRAGMA table_info(attachments)")}
            if "section_id" not in att_cols:
                c.execute("ALTER TABLE attachments ADD COLUMN section_id TEXT DEFAULT ''")
            c.execute("""CREATE TABLE IF NOT EXISTS manual_confirmations (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, target TEXT NOT NULL,
                section_id TEXT DEFAULT '', note TEXT DEFAULT '',
                content_hash TEXT DEFAULT '', reviewer TEXT DEFAULT '',
                reviewed_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS task_checkpoints (
                project_id TEXT NOT NULL, job_id TEXT NOT NULL, section_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending', ver INTEGER DEFAULT 0,
                note TEXT DEFAULT '', updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, job_id, section_id))""")
            # C02：项目协作元数据与名册；没有成员记录的老项目按「同企业默认可编辑」兼容
            for col, ddl in (("owner_id", "TEXT DEFAULT ''"),
                             ("due_at", "TEXT DEFAULT ''"),
                             ("final_version", "TEXT DEFAULT ''")):
                if col not in proj_cols:
                    c.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            c.execute("""CREATE TABLE IF NOT EXISTS project_members (
                project_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT DEFAULT 'reader', updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, user_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS section_assignees (
                project_id TEXT NOT NULL, section_id TEXT NOT NULL,
                user_id TEXT DEFAULT '', review_status TEXT DEFAULT 'pending',
                reviewer TEXT DEFAULT '', reviewed_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, section_id))""")
            # C01：项目级暗标 / 格式规则与分册归属，按项目独立保存
            c.execute("""CREATE TABLE IF NOT EXISTS project_rules (
                project_id TEXT PRIMARY KEY, data TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL)""")
            job_cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            if "consumed" not in job_cols:
                c.execute("ALTER TABLE jobs ADD COLUMN consumed INTEGER DEFAULT 0")
            # C03：取消请求、起止时间、可恢复标记（重启后不把旧 running 当仍在执行）
            for col, ddl in (("cancel_requested", "INTEGER DEFAULT 0"),
                             ("started_at", "TEXT DEFAULT ''"),
                             ("finished_at", "TEXT DEFAULT ''"),
                             ("recoverable", "INTEGER DEFAULT 0")):
                if col not in job_cols:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
            req_cols = {r["name"] for r in c.execute("PRAGMA table_info(requirements)")}
            if "origin_text" not in req_cols:
                c.execute("ALTER TABLE requirements ADD COLUMN origin_text TEXT DEFAULT ''")
            # 正文版本号：老库补 0，已有正文原样保留；写入时递增，用于检测「编辑期间被别处改过」
            sec_cols = {r["name"] for r in c.execute("PRAGMA table_info(sections)")}
            if "ver" not in sec_cols:
                c.execute("ALTER TABLE sections ADD COLUMN ver INTEGER DEFAULT 0")
            from . import generation_context
            generation_context.migrate(c)
            from . import requirement_support
            requirement_support.migrate(c)
    finally:
        _db_ctx.reset(tok)


def init_db(path: Path | str | None = None) -> None:
    """建库（含补表补列）并做一次启动收尾：中断任务标失败、历史日志按保留策略清理。"""
    target = Path(path or db_path())
    _apply_schema(target)
    tok = _db_ctx.set(target)
    try:
        recover_stale_jobs()
        prune_history()
    finally:
        _db_ctx.reset(tok)


def tidy_all_orgs(org_ids: list[str]) -> dict:
    """服务启动时为每个企业库收尾（中断任务标失败、老旧日志清理）。"""
    out = {}
    for oid in org_ids or []:
        try:
            p = init_org(oid)
            tok = _db_ctx.set(p)
            try:
                out[oid] = {"failed_jobs": recover_stale_jobs()}
            finally:
                _db_ctx.reset(tok)
        except Exception as exc:            # 单个企业库坏了不影响别的
            out[oid] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# ---------------- 证据绑定（章节 → 资料原文片段） ----------------

def save_evidence(pid: str, section_id: str, sources: list[dict]) -> int:
    """覆盖式写入本章证据；无依据时记一条 missing 待办，不写进标书正文。"""
    with conn() as c:
        c.execute("UPDATE evidence SET status='historical' WHERE project_id=? AND section_id=?",
                  (pid, section_id))
        ts = now()
        if not sources:
            c.execute(
                "INSERT INTO evidence(id,project_id,section_id,scope,title,ref,snippet,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id(), pid, section_id, "待办", "本章缺依据", "需人工补充",
                 "本章未检索到可引用的招标文件条款 / 本项目资料 / 企业素材库条目，"
                 "事实性表述请人工核对并补充出处。", "missing", ts))
            return 0
        c.executemany(
            "INSERT INTO evidence(id,project_id,section_id,scope,title,ref,doc_id,page,"
            "start_pos,end_pos,snippet,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(new_id(), pid, section_id, str(s.get("scope") or ""),
              str(s.get("title") or ""), str(s.get("ref") or ""),
              str(s.get("doc_id") or ""), int(s.get("page") or 0),
              int(s.get("start") or 0), int(s.get("end") or 0),
              str(s.get("snippet") or "")[:400], "bound", ts)
             for s in sources])
    return len(sources)


def list_evidence(pid: str, section_id: str = "") -> list[dict]:
    sql = "SELECT * FROM evidence WHERE project_id=?"
    vals: list = [pid]
    if section_id:
        sql += " AND section_id=?"
        vals.append(section_id)
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id", tuple(vals))]


# ---------------- 章节版本（可回退） ----------------

def snapshot_sections(pid: str, batch_id: str, reason: str,
                      section_ids: list[str] | None = None) -> int:
    """改写前存整章快照。只存即将被改动的章节，避免无意义的大批量记录。"""
    rows = list_sections(pid)
    if section_ids is not None:
        wanted = set(section_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not rows:
        return 0
    ts = now()
    with conn() as c:
        c.executemany(
            "INSERT INTO section_versions(id,project_id,batch_id,section_id,title,text,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(new_id(), pid, batch_id, r["id"], r["title"], r.get("text") or "", reason, ts)
             for r in rows],
        )
    return len(rows)


def snapshot_outline(pid: str, batch_id: str, reason: str) -> int:
    """整份目录快照（含标题、正文、保护标记）。

    换目录会把章节行整批重建，普通章节快照（按 section_id 写回）恢复不了，
    所以这里单独存一份完整目录，撤销时整份还原。
    """
    rows = list_sections(pid)
    if not rows:
        return 0
    data = [{"id": r["id"], "title": r["title"], "text": r.get("text") or "",
             "protected": int(r.get("protected") or 0),
             "ver": int(r.get("ver") or 0)} for r in rows]
    with conn() as c:
        c.execute(
            "INSERT INTO outline_backups(project_id,batch_id,reason,data,created_at) "
            "VALUES(?,?,?,?,?)",
            (pid, batch_id, reason, json.dumps(data, ensure_ascii=False), now()),
        )
    return len(rows)


def list_batches(pid: str, limit: int = 12) -> list[dict]:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT batch_id, reason, MIN(created_at) AS created_at, COUNT(*) AS sections "
            "FROM section_versions WHERE project_id=? "
            "GROUP BY batch_id ORDER BY created_at DESC LIMIT ?", (pid, limit))]
        full = [dict(r) for r in c.execute(
            "SELECT batch_id, reason, created_at, data FROM outline_backups "
            "WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (pid, limit))]
    for r in full:
        try:
            r["sections"] = len(json.loads(r.pop("data") or "[]"))
        except (ValueError, TypeError):
            r["sections"] = 0
    rows += full
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return rows[:limit]


def restore_batch(pid: str, batch_id: str,
                  expect_vers: dict[str, int] | None = None) -> dict:
    """撤销本轮：普通快照按章节写回；整份目录快照则按原样重建目录。

    两种恢复都会推进章节版本 / 换掉章节 id，让别处窗口手里拿的旧版本立刻失效，
    否则用户刚撤销完，另一个窗口的旧稿又会被当成「基于最新版」写回来。
    普通快照逐章执行，任一章失败只跳过该章并返回 partial，不谎报「全部成功」。
    """
    with conn() as c:
        ob = c.execute(
            "SELECT * FROM outline_backups WHERE project_id=? AND batch_id=?",
            (pid, batch_id)).fetchone()
    if ob:
        try:
            data = json.loads(ob["data"] or "[]")
        except (ValueError, TypeError):
            data = []
        if not data:
            return {"ok": False, "error": "快照为空"}
        with conn() as c:
            old_ids = [r["section_id"] for r in c.execute(
                "SELECT id AS section_id FROM sections WHERE project_id=?", (pid,)).fetchall()]
            for old_sid in old_ids:
                c.execute("UPDATE evidence SET status='historical' WHERE project_id=? AND section_id=?",
                          (pid,old_sid))
            c.execute("DELETE FROM sections WHERE project_id=?", (pid,))
            ts = now()
            for i, item in enumerate(data):
                restored_sid = new_id()
                c.execute(
                    "INSERT INTO sections(id,project_id,seq,title,text,protected,ver,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (restored_sid, pid, i, str(item.get("title") or f"第{i+1}章"),
                     str(item.get("text") or ""), int(item.get("protected") or 0),
                     int(item.get("ver") or 0) + 1, ts),
                )
                _restore_generation(c,pid,restored_sid,str(item.get("text") or ""),
                                    int(item.get("ver") or 0)+1,
                                    original_sid=str(item.get("id") or ""))
            c.execute("UPDATE requirement_responses SET status='review' WHERE project_id=? "
                      "AND status IN ('confirmed','na')", (pid,))
        # 目录结构跟着快照一起还原，否则章节列表变了、大纲还停在上一版
        update_project(pid, outline=[{"title": str(it.get("title") or ""), "points": []}
                                     for it in data])
        return {"ok": True, "restored": len(data), "batch_id": batch_id,
                "outline": True, "partial": False, "skipped": [],
                "invalidated": old_ids}

    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM section_versions WHERE project_id=? AND batch_id=?",
            (pid, batch_id))]
        if not rows:
            return {"ok": False, "error": "快照不存在"}
        ts = now()
        restored, skipped = 0, []
        for r in rows:
            sid = r["section_id"]
            want = (expect_vers or {}).get(sid)
            if want is not None:
                cur = c.execute(
                    "UPDATE sections SET text=?, updated_at=?, ver=ver+1 "
                    "WHERE id=? AND project_id=? AND ver=?",
                    (r["text"], ts, sid, pid, int(want)))
            else:
                cur = c.execute(
                    "UPDATE sections SET text=?, updated_at=?, ver=ver+1 "
                    "WHERE id=? AND project_id=?",
                    (r["text"], ts, sid, pid))
            if cur.rowcount:
                restored += 1
                version = c.execute("SELECT ver FROM sections WHERE id=?", (sid,)).fetchone()["ver"]
                _invalidate_section(c,pid,sid)
                _restore_generation(c,pid,sid,r["text"],version,original_sid=sid)
            else:
                skipped.append({"section_id": sid,
                                "reason": "版本已变化，未覆盖" if want is not None else "章节已不存在"})
    return {"ok": True, "restored": restored, "batch_id": batch_id, "outline": False,
            "partial": bool(skipped), "skipped": skipped, "invalidated": []}


# ---------------- 用量与审计 ----------------

def add_usage(pid: str, *, actor: str = "agent", task: str = "", tool: str = "",
              model: str = "", tokens_in: int = 0, tokens_out: int = 0,
              elapsed_ms: int = 0, ok: bool = True,
              input_text: str = "", output_text: str = "") -> None:
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO usage(id,project_id,actor,task,tool,model,tokens_in,tokens_out,"
                "elapsed_ms,ok,input_digest,output_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), pid, actor, task, tool, model, tokens_in, tokens_out,
                 elapsed_ms, 1 if ok else 0, str(input_text)[:160].replace("\n", " "),
                 str(output_text)[:160].replace("\n", " "), now()),
            )
    except Exception:
        pass  # 记账失败不能影响主链路


def usage_summary(pid: str = "", limit: int = 50) -> dict:
    sql = "SELECT * FROM usage WHERE 1=1"
    vals: list = []
    if pid:
        sql += " AND project_id=?"
        vals.append(pid)
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", (*vals, limit))]
        agg = c.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tin, "
            "COALESCE(SUM(tokens_out),0) AS tout, COALESCE(SUM(elapsed_ms),0) AS ms "
            "FROM usage WHERE 1=1" + (" AND project_id=?" if pid else ""), tuple(vals)).fetchone()
        return {"rows": rows, "summary": dict(agg), "today_tokens": today_tokens(pid)}


def today_tokens(pid: str = "") -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    with conn() as c:
        if pid:
            row = c.execute(
                "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS t FROM usage "
                "WHERE project_id=? AND created_at LIKE ?", (pid, day + "%")).fetchone()
        else:
            row = c.execute(
                "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS t FROM usage "
                "WHERE created_at LIKE ?", (day + "%",)).fetchone()
    return int(row["t"] or 0)


# ---------------- 项目 ----------------

def create_project(name: str, tender_text: str = "") -> dict:
    pid = new_id()
    ts = now()
    with conn() as c:
        c.execute(
            "INSERT INTO projects(id,name,tender_text,summary,outline,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, name, tender_text, "", "[]", ts, ts),
        )
    return get_project(pid)


def list_projects(include_archived: bool = False) -> list[dict]:
    """默认不含已归档：侧栏、状态接口、目录复用都只看见在用的项目。"""
    sql = "SELECT * FROM projects"
    if not include_archived:
        sql += " WHERE COALESCE(archived_at,'')=''"
    with conn() as c:
        rows = c.execute(sql + " ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


_STATE_FIELDS = "id, name, created_at, updated_at, archived_at"


def list_project_summaries(include_archived: bool = False) -> list[dict]:
    """侧栏 / state 专用：只取摘要列，不把每本几万字的招标全文读进内存。

    list_projects() 是 SELECT *，业务链里确实需要全文；但每次刷新都调的
    /api/state 不需要，改用本函数避免大字段反复进出 sqlite 与 JSON。
    """
    sql = f"SELECT {_STATE_FIELDS} FROM projects"
    if not include_archived:
        sql += " WHERE COALESCE(archived_at,'')=''"
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at DESC").fetchall()]


def archive_project(pid: str, on: bool = True) -> bool:
    """归档只改一个时间戳，不搬数据、不删文件。"""
    with conn() as c:
        cur = c.execute("UPDATE projects SET archived_at=? WHERE id=?",
                        (now() if on else "", pid))
        return cur.rowcount > 0


def delete_project(pid: str) -> dict:
    """删除项目连同它名下的一切：正文、检查、上传资料、任务、用量、导出记录。

    只删本项目数据：知识库里已经收进去的范例（scope='kb'）保留，
    那是用户主动沉淀的资产，不应该因为项目删了而消失。
    """
    p = get_project(pid)
    if not p:
        return {}
    arts = list_artifacts(pid)
    with conn() as c:
        for t in ("sections", "section_versions", "outline_backups", "checks", "evidence",
                  "section_quality", "requirements", "requirement_sync",
                  "requirement_suppressed", "jobs", "auto_runs", "agent_runs", "messages",
                  "usage", "source_pages", "artifacts", "originals", "tender_versions",
                  "requirement_responses", "project_facts", "attachments",
                  "manual_confirmations", "task_checkpoints", "project_rules",
                  "project_members", "section_assignees", "generation_runs", "generation_sources",
                  "requirement_support_links", "requirement_support_policies"):
            try:
                c.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
            except sqlite3.OperationalError:
                # 老库可能缺某张表，缺了就没有可删的数据，继续清其余表
                continue
        c.execute("DELETE FROM kb_docs WHERE project_id=? AND scope='project'", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"id": pid, "name": p.get("name") or "",
            "sections": len(p.get("sections") or []), "artifacts": len(arts)}


def touch_project(pid: str) -> None:
    with conn() as c:
        c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(), pid))


def project_stats() -> dict[str, dict]:
    """项目列表用：一次聚合出每本的章节数、已写章数、字数、最后改动时间。"""
    with conn() as c:
        rows = c.execute(
            "SELECT project_id, COUNT(*) AS n, "
            "SUM(CASE WHEN TRIM(COALESCE(text,''))<>'' THEN 1 ELSE 0 END) AS written, "
            "SUM(CASE WHEN COALESCE(protected,0)=0 AND TRIM(COALESCE(text,''))='' "
            "         THEN 1 ELSE 0 END) AS open_chapters, "
            "SUM(LENGTH(COALESCE(text,''))) AS chars, "
            "MAX(updated_at) AS last_write FROM sections GROUP BY project_id").fetchall()
    return {r["project_id"]: {"sections": int(r["n"] or 0),
                              "written": int(r["written"] or 0),
                              "open_chapters": int(r["open_chapters"] or 0),
                              "chars": int(r["chars"] or 0),
                              "last_write": r["last_write"] or ""} for r in rows}


def get_project(pid: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            return {}
        p = dict(row)
        p["sections"] = [dict(r) for r in c.execute(
            "SELECT * FROM sections WHERE project_id=? ORDER BY seq", (pid,))]
        p["materials"] = _with_page_map([dict(r) for r in c.execute(
            "SELECT * FROM kb_docs WHERE project_id=? AND scope='project' ORDER BY created_at", (pid,))])
        p["checks"] = [dict(r) for r in c.execute(
            "SELECT * FROM checks WHERE project_id=? ORDER BY round, id", (pid,))]
        p["outline"] = json.loads(p.get("outline") or "[]")
    return p


def update_project(pid: str, **fields) -> None:
    allowed = {"name", "tender_text", "summary", "outline", "sample_doc_id",
               "tender_parse", "owner_id", "due_at", "final_version"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "outline" or (k == "tender_parse" and not isinstance(v, str)):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(now())
    with conn() as c:
        c.execute(f"UPDATE projects SET {','.join(sets)} WHERE id=?", (*vals, pid))


# ---------------- 资料 / 知识库 ----------------

# material=通用素材（片段）；sample=整本标书范例，写新标书时可参考结构与措辞
DOC_KINDS = ("material", "sample")

def add_doc(title: str, content: str, scope: str = "project", project_id: str = "",
            page_map: list[dict] | None = None, kind: str = "material",
            source_pid: str = "", source_hash: str = "",
            verification: str = "unreviewed", parse: dict | None = None,
            valid_until: str = "") -> dict:
    """parse 是 parsers.parse_bytes 的元数据；带未识别页的资料自动标 needs_review。

    needs_review=1 且未 confirmed 的资料不参与事实依据与已确认要求，
    但正文保留、可查看、可补录、可人工确认后解除。
    """
    did = new_id()
    pm = json.dumps(page_map or [], ensure_ascii=False)
    if kind not in DOC_KINDS:
        kind = "material"
    meta = parse or {}
    needs = int(bool(meta.get("needs_review")))
    unread = meta.get("unread_pages")
    if unread is None:
        unread = [p.get("page") for p in (page_map or [])
                  if p.get("source") == "scan-unread"]
    with conn() as c:
        c.execute(
            "INSERT INTO kb_docs(id,project_id,scope,title,content,page_map,kind,"
            "source_pid,source_hash,verification,parse_status,effective_chars,pages,"
            "scan_pages,ocr_pages,unread_pages,review_note,needs_review,valid_until,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, project_id, scope, title, content, pm, kind, source_pid,
             source_hash, verification,
             str(meta.get("parse_status") or "ok"),
             int(meta.get("effective_chars") or 0),
             int(meta.get("pages") or 0), int(meta.get("scan_pages") or 0),
             int(meta.get("ocr_pages") or 0),
             json.dumps(unread or [], ensure_ascii=False),
             str(meta.get("review_note") or ""), needs,
             str(valid_until or "").strip()[:10], now()),
        )
    return get_doc(did) or {"id": did, "title": title}


def usable_docs(pid: str = "", *, include_kb: bool = True) -> list[dict]:
    """可作为事实依据 / 已确认要求的资料：排除不可用件与未核对的待处理件。"""
    docs = list_docs(project_id=pid, usable_only=True) if pid else []
    if include_kb:
        docs += list_docs(scope="kb", usable_only=True)
    seen, out = set(), []
    for d in docs:
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    return out


def pending_docs(pid: str = "") -> list[dict]:
    """解析不完整、还等人工核对或补录的资料。"""
    docs = list_docs(project_id=pid) if pid else []
    docs += list_docs(scope="kb")
    seen, out = set(), []
    for d in docs:
        if (int(d.get("needs_review") or 0) and not d.get("confirmed_at")
                and d["id"] not in seen):
            seen.add(d["id"])
            out.append(d)
    return out


def add_original(pid: str, filename: str, path: str, size: int = 0,
                 reason: str = "") -> dict:
    row = {"id": new_id(), "project_id": pid, "filename": filename,
           "path": path, "size": size, "reason": reason,
           "status": "pending", "created_at": now()}
    with conn() as c:
        c.execute("INSERT INTO originals(id,project_id,filename,path,size,reason,status,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (row["id"], pid, filename, path, size, reason, "pending", row["created_at"]))
    return row


def list_originals(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM originals WHERE project_id=? ORDER BY created_at", (pid,))]


def confirm_doc(did: str, by: str = "") -> dict | None:
    """人工确认：明确接受未识别范围，之后该资料才进入依据池。"""
    with conn() as c:
        c.execute("UPDATE kb_docs SET confirmed_at=?, confirmed_by=?, parse_status='confirmed' "
                  "WHERE id=?", (now(), by or "人工", did))
    return get_doc(did)


def add_page_text(did: str, page: int, text: str, by: str = "") -> dict | None:
    """补录某页未识别的正文：更新映射与全文，全部补完自动解除待核对。"""
    d = get_doc(did)
    if not d:
        return None
    body = str(text or "").strip()
    if not body:
        return d
    pm = list(d.get("page_map") or [])
    hit = next((p for p in pm if int(p.get("page") or 0) == int(page)), None)
    if hit:
        hit["source"] = "manual"
        hit["chars"] = len(body)
        hit["text"] = body[:2000]
    else:
        pm.append({"page": int(page), "physical": int(page), "source": "manual",
                   "chars": len(body), "text": body[:2000]})
    pm.sort(key=lambda p: int(p.get("page") or 0))
    # 展示文本按页重建，保证补录内容与页码坐标一致
    full = "\n\n".join(f"【第 {p.get('page')} 页】\n{p.get('text') or ''}" for p in pm)
    unread = [int(p.get("page") or 0) for p in pm if p.get("source") == "scan-unread"]
    done = not unread
    with conn() as c:
        c.execute(
            "UPDATE kb_docs SET content=?, page_map=?, unread_pages=?, scan_pages=?, "
            "needs_review=?, parse_status=?, review_note=?, confirmed_at=?, confirmed_by=? WHERE id=?",
            (full, json.dumps(pm, ensure_ascii=False), json.dumps(unread, ensure_ascii=False),
             len(unread), 0 if done else 1, "ok" if done else "needs_review",
             "" if done else f"第 {','.join(str(x) for x in unread)} 页未识别，待人工补录或确认",
             now() if done else "", by if done else "", did))
    return get_doc(did)


def find_doc_by_source(pid: str) -> dict | None:
    """知识库里已经收过这本书没有；收过就更新同一篇，不堆重复副本。"""
    with conn() as c:
        r = c.execute(
            "SELECT * FROM kb_docs WHERE scope='kb' AND source_pid=? ORDER BY created_at LIMIT 1",
            (pid,)).fetchone()
    return _with_page_map([dict(r)])[0] if r else None


def kb_source_pids() -> dict[str, str]:
    """哪些项目已经收进过知识库：{pid: doc_id}，项目列表据此显示「更新知识库」。"""
    with conn() as c:
        rows = c.execute(
            "SELECT source_pid, id FROM kb_docs "
            "WHERE scope='kb' AND COALESCE(source_pid,'')<>''").fetchall()
    return {r["source_pid"]: r["id"] for r in rows}


def _with_page_map(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["page_map"] = json.loads(d.get("page_map") or "[]")
        except (ValueError, TypeError):
            d["page_map"] = []
        if not isinstance(d["page_map"], list):
            d["page_map"] = []
        try:
            d["unread_pages"] = json.loads(d.get("unread_pages") or "[]")
        except (ValueError, TypeError):
            d["unread_pages"] = []
        if not isinstance(d["unread_pages"], list):
            d["unread_pages"] = []
        out.append(d)
    return out


def _doc_usable(d: dict) -> bool:
    return (str(d.get("parse_status") or "ok") != "unusable"
            and (not int(d.get("needs_review") or 0) or bool(d.get("confirmed_at"))))


def list_docs(scope: str = "", project_id: str = "", kind: str = "",
              *, usable_only: bool = False) -> list[dict]:
    """usable_only=True 时只给可作为依据的资料：排除不可用件与未核对的待处理件。"""
    sql = "SELECT * FROM kb_docs WHERE 1=1"
    vals: list = []
    if scope:
        sql += " AND scope=?"
        vals.append(scope)
    if kind:
        sql += " AND kind=?"
        vals.append(kind)
    if project_id:
        sql += " AND project_id=?"
        vals.append(project_id)
    sql += " ORDER BY created_at"
    with conn() as c:
        rows = _with_page_map([dict(r) for r in c.execute(sql, vals)])
    return [d for d in rows if _doc_usable(d)] if usable_only else rows


def get_doc(did: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM kb_docs WHERE id=?", (did,)).fetchone()
    return _with_page_map([dict(r)])[0] if r else None


def update_doc(did: str, **fields) -> dict | None:
    allowed = {"title", "content", "kind", "source_hash", "verification",
               "review_note", "needs_review", "parse_status", "valid_until"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "kind" and v not in DOC_KINDS:
            v = "material"
        if k == "valid_until":
            v = str(v or "").strip()[:10]
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        with conn() as c:
            c.execute(f"UPDATE kb_docs SET {','.join(sets)} WHERE id=?", (*vals, did))
    return get_doc(did)


def delete_doc(did: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM kb_docs WHERE id=?", (did,))
        c.execute("DELETE FROM doc_chapters WHERE doc_id=?", (did,))


# ---------------- 范例章节 ----------------

def save_chapters(doc_id: str, chapters: list[dict]) -> int:
    """整本范例按章拆开后逐章入库；重传同一篇时先清旧章节。"""
    ts = now()
    rows = [(new_id(), doc_id, int(ch.get("idx") or 0), str(ch.get("title") or "")[:80],
             int(ch.get("level") or 1), str(ch.get("content") or ""), ts)
            for ch in chapters]
    with conn() as c:
        c.execute("DELETE FROM doc_chapters WHERE doc_id=?", (doc_id,))
        c.executemany(
            "INSERT INTO doc_chapters(id,doc_id,idx,title,level,content,created_at) "
            "VALUES(?,?,?,?,?,?,?)", rows)
        c.execute("UPDATE kb_docs SET n_chapters=? WHERE id=?", (len(rows), doc_id))
    return len(rows)


def list_chapters(doc_id: str, with_content: bool = True) -> list[dict]:
    cols = "*" if with_content else "id,doc_id,idx,title,level,length(content) AS chars,created_at"
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {cols} FROM doc_chapters WHERE doc_id=? ORDER BY idx", (doc_id,))]


def chapter_counts() -> dict[str, dict]:
    """每本范例的章节统计：总数与一级章数（建目录只取一级章）。"""
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT doc_id, COUNT(*) AS n, "
            "SUM(CASE WHEN level=1 THEN 1 ELSE 0 END) AS top "
            "FROM doc_chapters GROUP BY doc_id")]
    return {r["doc_id"]: {"n": r["n"], "top": r["top"] or 0} for r in rows}


def list_doc_meta(scope: str = "", kind: str = "") -> list[dict]:
    """只要标题与统计，不取正文：正文页、选择器这类地方用，避免每次拉几 MB。"""
    sql = ("SELECT id,title,kind,n_chapters,length(content) AS chars,created_at "
           "FROM kb_docs WHERE 1=1")
    vals: list = []
    if scope:
        sql += " AND scope=?"
        vals.append(scope)
    if kind:
        sql += " AND kind=?"
        vals.append(kind)
    sql += " ORDER BY created_at"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, vals)]


# ---------------- 页码坐标与回跳原文 ----------------

def save_pages(pid: str, kind: str, title: str, page_map: list[dict]) -> int:
    """保存「物理页 ↔ 原文页码」映射；同名文件重传覆盖旧映射。"""
    if not page_map:
        return 0
    ts = now()
    with conn() as c:
        c.execute("DELETE FROM source_pages WHERE project_id=? AND kind=? AND title=?",
                  (pid, kind, title))
        c.executemany(
            "INSERT INTO source_pages(id,project_id,kind,title,page,physical,source,text,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(new_id(), pid, kind, title, int(p.get("page") or 0),
              int(p.get("physical") or p.get("page") or 0),
              str(p.get("source") or "text"), str(p.get("text") or "")[:3000], ts)
             for p in page_map],
        )
    return len(page_map)


def locate_page(pid: str, page: int = 0, keyword: str = "") -> dict:
    """按页码 / 关键词回跳原文；返回命中的页文本与坐标。"""
    sql = "SELECT * FROM source_pages WHERE project_id=?"
    vals: list = [pid]
    if page:
        sql += " AND page=?"
        vals.append(page)
    sql += " ORDER BY page, id LIMIT 40"
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, vals)]
    if keyword:
        kw = keyword.strip()[:30]
        scored = [r for r in rows if kw and kw in (r.get("text") or "")]
        if scored:
            rows = scored
    return {"pages": rows, "count": len(rows),
            "hint": "" if rows else "未找到该页原文；请确认文件是上传解析入库的（粘贴文本没有页码坐标）"}


# ---------------- 章节 ----------------

# 保护章节按标题关键词判定，和 set_sections / apply_outline_titles 保持同一口径
PROTECTED_HINTS = ("报价", "投标函", "承诺函", "授权", "资质")


def _sync_outline(pid: str, before: list[dict]) -> None:
    """章节增删改后同步 projects.outline；能按 id 对齐时保留原有章节要点。

    目录结构被导出、检查、大纲页共用，只改 sections 会让界面上的目录和
    Word 里的必需章节不一致。整份重建会丢掉 AI 生成的 points，所以优先按
    章节 id 复用旧条目。
    """
    p = get_project(pid)
    cur = p.get("sections") or []
    outline = [dict(x) for x in (p.get("outline") or []) if isinstance(x, dict)]
    if len(outline) == len(before):
        ids = [r["id"] for r in before]
        data = []
        for s in cur:
            i = ids.index(s["id"]) if s["id"] in ids else -1
            item = dict(outline[i]) if i >= 0 else {"points": []}
            item["title"] = s["title"]
            data.append(item)
    else:
        data = [{"title": s["title"], "points": []} for s in cur]
    update_project(pid, outline=data)
    # 增删改章节都算「目录变了」，原来的确认不能继续有效
    clear_outline_confirmation(pid)


# ---------------- P1：目录确认闸门 ----------------

def outline_confirmed(pid: str) -> bool:
    """轻量判断，供 HTTP / 对话 / 大脑三条生成入口共用，不加载整本项目。"""
    with conn() as c:
        row = c.execute("SELECT outline_confirmed_at FROM projects WHERE id=?",
                        (pid,)).fetchone()
    return bool(row and str(row["outline_confirmed_at"] or "").strip())


def clear_outline_confirmation(pid: str) -> None:
    """目录结构一变，之前的确认立即作废，必须重新确认才能写正文。"""
    with conn() as c:
        c.execute("UPDATE projects SET outline_confirmed_at='' WHERE id=?", (pid,))


def confirm_outline(pid: str, by: str = "") -> dict:
    """人工确认当前目录；空目录不许确认。"""
    secs = list_sections(pid)
    if not secs:
        return {"ok": False, "error": "empty", "detail": "还没有目录，先生成目录"}
    stamp = now()
    with conn() as c:
        c.execute("UPDATE projects SET outline_confirmed_at=?, updated_at=? "
                  "WHERE id=?", (stamp, stamp, pid))
    return {"ok": True, "confirmed_at": stamp, "chapters": len(secs),
            "titles": [s["title"] for s in secs], "by": by}


def move_section(pid: str, sid: str, direction: int) -> dict:
    """上移 / 下移一章：只交换 seq，章节 id、正文、版本、依据全都不动。"""
    before = list_sections(pid)
    idx = next((i for i, r in enumerate(before) if r["id"] == sid), -1)
    if idx < 0:
        return {"ok": False, "error": "not_in_project", "detail": "章节不属于本项目"}
    target = idx + (1 if direction > 0 else -1)
    if target < 0 or target >= len(before):
        return {"ok": False, "error": "boundary", "detail": "已经到头了"}
    a, b = before[idx], before[target]
    with conn() as c:
        # 先用负数让开，避免将来给 seq 加唯一约束时撞车
        c.execute("UPDATE sections SET seq=-1 WHERE id=?", (a["id"],))
        c.execute("UPDATE sections SET seq=? WHERE id=?", (b["seq"], a["id"]))
        c.execute("UPDATE sections SET seq=? WHERE id=?", (a["seq"], b["id"]))
    _sync_outline(pid, before)
    return {"ok": True, "id": sid, "title": a["title"],
            "from": idx, "to": target, "total": len(before)}


def _clean_title(title: str) -> tuple[str, str]:
    t = str(title or "").strip()
    if not t:
        return "", "章节名称不能为空"
    if len(t) > 120:
        return "", "章节名称最多 120 字"
    return t, ""


def add_section(pid: str, title: str, after_sid: str = "") -> dict:
    """手动加一章：只插一行，不重建目录，已有章节的 id / 正文 / 版本全部不动。"""
    t, err = _clean_title(title)
    if err:
        return {"ok": False, "error": "invalid", "detail": err}
    before = list_sections(pid)
    if any(r["title"] == t for r in before):
        return {"ok": False, "error": "duplicate", "detail": "已有同名章节，请换个名字"}
    if after_sid and not any(r["id"] == after_sid for r in before):
        return {"ok": False, "error": "not_in_project", "detail": "插入位置不属于本项目"}
    pos = (next(i for i, r in enumerate(before) if r["id"] == after_sid) + 1
           if after_sid else len(before))
    sid = new_id()
    protected = 1 if any(k in t for k in PROTECTED_HINTS) else 0
    with conn() as c:
        c.execute("UPDATE sections SET seq=seq+1 WHERE project_id=? AND seq>=?",
                  (pid, pos))
        c.execute(
            "INSERT INTO sections(id,project_id,seq,title,text,protected,ver,updated_at) "
            "VALUES(?,?,?,?,?,?,0,?)", (sid, pid, pos, t, "", protected, now()))
    _sync_outline(pid, before)
    return {"ok": True, "id": sid, "title": t, "seq": pos,
            "protected": bool(protected), "total": len(before) + 1}


def rename_section(pid: str, sid: str, title: str) -> dict:
    """改章节名：id 与正文原样保留，别名章 / 分工 / 依据仍指向同一章。"""
    t, err = _clean_title(title)
    if err:
        return {"ok": False, "error": "invalid", "detail": err}
    before = list_sections(pid)
    row = next((r for r in before if r["id"] == sid), None)
    if not row:
        return {"ok": False, "error": "not_in_project", "detail": "章节不属于本项目"}
    if any(r["title"] == t and r["id"] != sid for r in before):
        return {"ok": False, "error": "duplicate", "detail": "已有同名章节，请换个名字"}
    with conn() as c:
        cur = c.execute(
            "UPDATE sections SET title=?, updated_at=? WHERE id=? AND project_id=?",
            (t, now(), sid, pid))
        if not cur.rowcount:
            return {"ok": False, "error": "not_in_project", "detail": "章节不属于本项目"}
    _sync_outline(pid, before)
    return {"ok": True, "id": sid, "title": t, "old_title": row["title"],
            "text_kept": bool((row.get("text") or "").strip())}


def delete_section(pid: str, sid: str) -> dict:
    """删一章：正文一起删，但先留整份目录快照，可在「版本与回退」整份撤销。

    只删一章不能靠按 section_id 写回的普通快照恢复（章已不存在），所以用
    snapshot_outline；恢复会换掉全部章节 id，界面已有明确提示（R03）。
    """
    before = list_sections(pid)
    row = next((r for r in before if r["id"] == sid), None)
    if not row:
        return {"ok": False, "error": "not_in_project", "detail": "章节不属于本项目"}
    batch = new_id()
    snap_count = snapshot_outline(pid, batch, f"删除章节「{row['title']}」前")
    with conn() as c:
        cur = c.execute("DELETE FROM sections WHERE id=? AND project_id=?", (sid, pid))
        if not cur.rowcount:
            return {"ok": False, "error": "not_in_project", "detail": "章节不属于本项目"}
        rest = [r["id"] for r in c.execute(
            "SELECT id FROM sections WHERE project_id=? ORDER BY seq", (pid,))]
        for i, other in enumerate(rest):
            c.execute("UPDATE sections SET seq=? WHERE id=? AND project_id=?", (i, other, pid))
        # 只清这一章的挂靠数据；要求本身还在，响应退回「未落实」而不是连要求一起删
        c.execute("UPDATE evidence SET status='historical' WHERE project_id=? AND section_id=?",
                  (pid,sid))
        for t in ("section_quality", "section_assignees", "task_checkpoints",
                  "manual_confirmations", "checks"):
            try:
                c.execute(f"DELETE FROM {t} WHERE project_id=? AND section_id=?", (pid, sid))
            except sqlite3.OperationalError:
                pass
        try:
            c.execute(
                "UPDATE requirement_responses SET section_id='', anchor='', "
                "evidence='', candidate=0, evidence_ok=0, status='missing', "
                "updated_at=? WHERE project_id=? AND section_id=?", (now(), pid, sid))
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("UPDATE attachments SET section_id='' "
                      "WHERE project_id=? AND section_id=?", (pid, sid))
        except sqlite3.OperationalError:
            pass
    _sync_outline(pid, before)
    return {"ok": True, "deleted": sid, "title": row["title"],
            "chars": len(row.get("text") or ""), "remaining": len(before) - 1,
            "batch_id": batch, "restorable": bool(snap_count),
            "note": "已留整份目录快照，可在「版本与回退」里撤销这次删除"}


def set_sections(pid: str, chapters: list[dict], replace_text: bool = False) -> None:
    """按大纲重建章节；已有正文优先按标题保留，其次按原顺序保留。"""
    with conn() as c:
        old_rows = [dict(r) for r in c.execute(
            "SELECT * FROM sections WHERE project_id=? ORDER BY seq", (pid,))]
        old = {r["title"]: r for r in old_rows}
        for r in old_rows:
            _invalidate_section(c,pid,r["id"])
        c.execute("DELETE FROM sections WHERE project_id=?", (pid,))
        for i, ch in enumerate(chapters):
            title = ch.get("title", f"第{i+1}章")
            old_row = old.get(title) or (old_rows[i] if i < len(old_rows) else None)
            sid = new_id()
            c.execute(
                "INSERT INTO sections(id,project_id,seq,title,text,protected,updated_at) VALUES(?,?,?,?,?,?,?)",
                (sid, pid, i, title,
                 ("" if replace_text else (old_row["text"] if old_row else "")),
                 1 if any(k in title for k in ("报价", "投标函", "承诺函", "授权", "资质")) else 0,
                 now()),
            )
            if old_row and not replace_text:
                _restore_generation(c,pid,sid,old_row["text"],0,original_sid=old_row["id"])
    clear_outline_confirmation(pid)


def apply_outline_titles(pid: str, titles: list[str]) -> int:
    """按标题重建目录：只保留同名章节的正文，不按位置继承。

    套用以前项目的目录时如果按位置继承，旧项目的正文会错贴到新标题下，
    比丢正文更糟，所以这里只认标题完全一致。
    """
    with conn() as c:
        old = {r["title"]: dict(r) for r in c.execute(
            "SELECT * FROM sections WHERE project_id=?", (pid,))}
        for row in old.values():
            _invalidate_section(c,pid,row["id"])
        c.execute("DELETE FROM sections WHERE project_id=?", (pid,))
        for i, title in enumerate(titles):
            row = old.get(title)
            sid = new_id()
            c.execute(
                "INSERT INTO sections(id,project_id,seq,title,text,protected,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, pid, i, title,
                 row["text"] if row else "",
                 1 if any(k in title for k in ("报价", "投标函", "承诺函", "授权", "资质")) else 0,
                 now()),
            )
            if row:
                _restore_generation(c,pid,sid,row["text"],0,original_sid=row["id"])
    clear_outline_confirmation(pid)
    return len(titles)


def update_section(sid: str, text: str, base_ver: int | None = None,
                   pid: str | None = None) -> dict:
    """写章节正文（带并发重试）。

    SQLite 同一刻只允许一个写事务。两个人同时保存时，后到的要么排队、要么直接失败：
    Linux 报 `database is locked`，Windows 的 WAL 会报 `attempt to write a readonly database`。
    两种都按「别人正在写，稍后重试」处理，重试几次仍不行就返回 busy 让上层回 503，
    而不是把并发保存变成 500。
    """
    last = ""
    for attempt in range(6):
        try:
            return _update_section_once(sid, text, base_ver, pid)
        except sqlite3.OperationalError as exc:
            last = str(exc)
            msg = last.lower()
            if not any(k in msg for k in ("locked", "busy", "readonly")):
                raise
            if attempt == 5:
                return {"ok": False, "error": "busy", "ver": 0, "text": "", "detail": last}
            time.sleep(0.03 * (attempt + 1))
    return {"ok": False, "error": "busy", "ver": 0, "text": "", "detail": last}


def _update_section_once(sid: str, text: str, base_ver: int | None,
                         pid: str | None) -> dict:
    with conn() as c:
        row = c.execute("SELECT ver, project_id FROM sections WHERE id=?", (sid,)).fetchone()
        if not row:
            return {"ok": False, "error": "missing", "ver": 0, "text": ""}
        owner = str(row["project_id"] or "")
        if pid is not None and str(pid) != owner:
            return {"ok": False, "error": "not_in_project", "ver": 0, "text": ""}

        ts = now()
        if base_ver is None:
            c.execute("UPDATE sections SET text=?, updated_at=?, ver=ver+1 WHERE id=?",
                      (text, ts, sid))
            got = c.execute("SELECT ver FROM sections WHERE id=?", (sid,)).fetchone()
            new_ver = int(got["ver"] or 0) if got else 0
            ok = True
        else:
            # 原子条件更新：版本对不上时 rowcount=0，并发两个请求只会有一个成功
            cur = c.execute(
                "UPDATE sections SET text=?, updated_at=?, ver=ver+1 "
                "WHERE id=? AND ver=?",
                (text, ts, sid, int(base_ver)))
            ok = cur.rowcount == 1
            new_ver = int(base_ver) + 1

        if not ok:
            late = c.execute("SELECT text, ver FROM sections WHERE id=?", (sid,)).fetchone()
            return {"ok": False, "error": "conflict",
                    "ver": int(late["ver"] or 0) if late else 0,
                    "text": str(late["text"] or "") if late else ""}

        # 项目列表按「最近动过」排序，靠这一句把时间推上去
        _invalidate_section(c, owner, sid)
        c.execute("UPDATE projects SET updated_at=? WHERE id=?", (ts, owner))
        return {"ok": True, "ver": new_ver}


def _invalidate_section(c, pid, sid):
    c.execute("UPDATE sections SET generation_run_id='' WHERE id=? AND project_id=?", (sid,pid))
    c.execute("UPDATE evidence SET status='review' WHERE section_id=? AND project_id=? "
              "AND status NOT IN ('missing','historical')", (sid,pid))
    c.execute("UPDATE requirement_responses SET status='review',updated_at=? "
              "WHERE project_id=? AND section_id=? AND status IN ('confirmed','na')",
              (now(),pid,sid))


def _restore_generation(c,pid,sid,text,version,original_sid=""):
    """仅恢复相同正文的已提交历史快照；无记录时来源仍为未知。"""
    from .generation_context import digest
    if not original_sid:
        return
    run = c.execute("SELECT id FROM generation_runs WHERE project_id=? AND (section_id=? OR id IN "
                    "(SELECT generation_run_id FROM evidence WHERE project_id=? AND section_id=?)) "
                    "AND text_hash=? AND status='committed' ORDER BY rowid DESC LIMIT 1",
                    (pid,original_sid,pid,original_sid,digest(text))).fetchone()
    if not run:
        return
    c.execute("UPDATE sections SET generation_run_id=? WHERE id=? AND project_id=?",
              (run["id"],sid,pid))
    rows = c.execute("SELECT * FROM evidence WHERE project_id=? AND section_id=? "
                     "AND generation_run_id=?", (pid,original_sid,run["id"])).fetchall()
    for row in rows:
        if original_sid == sid:
            c.execute("UPDATE evidence SET status='candidate',section_ver=? WHERE id=?",
                      (version,row["id"]))
        else:
            copied = dict(row)
            copied.update(id=new_id(),section_id=sid,section_ver=version,status="candidate")
            c.execute(f"INSERT INTO evidence({','.join(copied)}) "
                      f"VALUES({','.join('?' for _ in copied)})",tuple(copied.values()))


def commit_generation(pid, sid, text, base_ver, run_id, *, quality=None,
                      abort_check=None, partial=False):
    """正文、来源和确认失效在同一短事务内提交。"""
    from .generation_context import digest
    if not str(text or "").strip():
        return {"ok": False, "error": "empty"}
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM generation_runs WHERE id=? AND project_id=? "
                        "AND section_id=?", (run_id,pid,sid)).fetchone()
        if not run or run["status"] != "succeeded" or run["base_ver"] != int(base_ver):
            return {"ok": False, "error": "invalid_generation"}
        if callable(abort_check) and abort_check():
            c.execute("UPDATE generation_runs SET status='cancelled' WHERE id=?", (run_id,))
            return {"ok": False, "error": "cancelled"}
        cur = c.execute("UPDATE sections SET text=?,updated_at=?,ver=ver+1 "
                        "WHERE id=? AND project_id=? AND ver=?",
                        (text,now(),sid,pid,int(base_ver)))
        if not cur.rowcount:
            c.execute("UPDATE generation_runs SET status='conflict' WHERE id=?", (run_id,))
            late = c.execute("SELECT text,ver FROM sections WHERE id=? AND project_id=?",
                             (sid,pid)).fetchone()
            return {"ok": False, "error": "conflict", **(dict(late) if late else {})}
        _invalidate_section(c,pid,sid)
        if not partial:
            c.execute("UPDATE evidence SET status='historical' WHERE project_id=? AND section_id=?",
                      (pid,sid))
        text_hash = digest(text)
        for row in c.execute("SELECT id,data FROM generation_sources WHERE run_id=? AND project_id=?",
                             (run_id,pid)).fetchall():
            s = json.loads(row["data"])
            if not s.get("doc_id"):
                continue
            c.execute("INSERT INTO evidence "
                      "(id,project_id,section_id,scope,title,doc_id,page,start_pos,end_pos,"
                      "snippet,status,created_at,generation_run_id,generation_source_id,"
                      "section_ver,text_hash,relation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (new_id(),pid,sid,s.get("scope",""),s.get("title",""),s["doc_id"],
                       s.get("page",0),s.get("start") or 0,s.get("end") or 0,
                       s.get("snippet",""),"candidate",now(),run_id,row["id"],
                       int(base_ver)+1,text_hash,"input"))
        c.execute("UPDATE sections SET generation_run_id=? WHERE id=?", (run_id,sid))
        c.execute("UPDATE generation_runs SET status='committed',text_hash=?,committed_ver=?,"
                  "quality=? WHERE id=?", (text_hash,int(base_ver)+1,
                  json.dumps(quality or {},ensure_ascii=False),run_id))
        c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(),pid))
    return {"ok": True, "ver": int(base_ver)+1, "generation_run_id": run_id}


def get_section(sid: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM sections WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else {}


# ---------------- 输出文件登记（交付件 / 审阅件） ----------------

def _file_hash(p: Path) -> str:
    """内容指纹：导出后核对「下载到的文件」和「检查过的内容」是否同一份。"""
    try:
        import hashlib
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:32]
    except Exception:
        return ""


def next_artifact_version(project_id: str, kind: str) -> int:
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(version),0) AS v FROM artifacts "
                        "WHERE project_id=? AND kind=?", (project_id, kind)).fetchone()
    return int(row["v"] or 0) + 1


def record_artifact(project_id: str, kind: str, path: Path | str, name: str = "",
                    content_hash: str = "", gate_fingerprint: str = "",
                    org_id: str = "", snapshot_fingerprint: str = "",
                    purpose: str = "", options: str = "", file_check: str = "",
                    status: str = "ok") -> dict:
    """登记一个落盘文件。同名展示文件各留各的记录，绝不先删后插。

    每个产物有独立 ID 与目录；is_current 只标同项目同类型里最新的一份，
    旧定稿仍可按版本号下载，但不会被误称为当前定稿。
    """
    p = Path(path)
    # 与 delivery.ACK_DRAFT_KIND 保持一致（本模块不能反向 import delivery）：
    # 知情导出草稿单独成一类，不挤进 deliverable，也不冒充普通审阅件。
    kind = kind if kind in ("deliverable", "review", "acknowledged_draft") else "review"
    version = next_artifact_version(project_id, kind)
    item = {
        "id": new_id(), "project_id": project_id, "kind": kind,
        "name": name or p.name, "path": str(p.resolve()),
        "content_hash": content_hash or _file_hash(p),
        "gate_fingerprint": gate_fingerprint, "org_id": org_id or "default",
        "version": version, "is_current": 0,
        "snapshot_fingerprint": snapshot_fingerprint,
        "purpose": purpose or ("正式交付" if kind == "deliverable" else
                              "知情导出草稿 · 已知悉风险 · 不可直接提交"
                              if kind == "acknowledged_draft" else "审阅件，非交付件"),
        "options": options, "file_check": file_check, "status": status,
        "created_at": now(),
    }
    with conn() as c:
        c.execute("UPDATE artifacts SET is_current=0 WHERE project_id=? AND kind=?",
                  (project_id, kind))
        cols = ",".join(item.keys())
        marks = ",".join("?" for _ in item)
        c.execute(f"INSERT INTO artifacts({cols}) VALUES({marks})", tuple(item.values()))
        c.execute("UPDATE artifacts SET is_current=1 WHERE id=?", (item["id"],))
    item["is_current"] = 1
    return item


def mark_artifact_status(aid: str, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE artifacts SET status=? WHERE id=?", (status, aid))


def get_artifact(name: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM artifacts WHERE name=? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (name,)).fetchone()
        return dict(row) if row else {}


def get_artifact_by_id(aid: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else {}


def list_artifacts(project_id: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at DESC", (project_id,))]


def list_sections(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM sections WHERE project_id=? ORDER BY seq", (pid,))]


# ---------------- 检查记录 ----------------

def save_checks(pid: str, round_no: int, issues: list[dict]) -> None:
    """重建本轮问题；已有的人工采纳/忽略决定跨轮保留（按 章节+锚点+规则码 匹配）。"""
    with conn() as c:
        old = [dict(r) for r in c.execute(
            "SELECT title,code,section_id,anchor,status,confidence,source_ref,page,bbox,created_at "
            "FROM checks WHERE project_id=? ORDER BY created_at, id", (pid,))]
        # 按时间顺序取「最后一次人工决定」。旧写法直接建 dict，未排序时另一轮的
        # pending 记录会覆盖已采纳状态，导致复检后采纳莫名丢失、闸门失效。
        decided = {}
        for r in old:
            if r.get("status") in ("accepted", "ignored"):
                decided[((r["title"] or "")[:24], r["section_id"],
                         (r["anchor"] or "")[:40])] = r
        c.execute("DELETE FROM checks WHERE project_id=? AND round=?", (pid, round_no))
        for it in issues:
            key = ((it.get("title") or "")[:24], it.get("section_id", ""),
                   (it.get("anchor") or "")[:40])
            prev = decided.get(key, {})
            status = prev.get("status") or it.get("status") or "pending"
            confidence = it.get("confidence")
            if confidence is None:
                confidence = prev.get("confidence", 0.90)
            c.execute(
                "INSERT INTO checks(id,project_id,round,code,level,title,detail,evidence,"
                "section_id,anchor,status,confidence,source_ref,page,bbox,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), pid, round_no, it.get("code", ""), it.get("level", "中"),
                 it.get("title", ""), it.get("detail", ""), it.get("evidence", ""),
                 it.get("section_id", ""), it.get("anchor", ""), status,
                 float(confidence or 0), it.get("source_ref") or prev.get("source_ref") or "",
                 int(it.get("page") or prev.get("page") or 0),
                 it.get("bbox") or prev.get("bbox") or "", now()),
            )


def set_check_status(pid: str, cid: str, status: str) -> dict:
    if status not in ("pending", "accepted", "ignored"):
        return {"ok": False, "error": "状态只能是 pending / accepted / ignored"}
    with conn() as c:
        # 「采纳」等于人工确认：把置信度抬到可自动改写线以上，AI 建议才能进入闭环
        if status == "accepted":
            cur = c.execute(
                "UPDATE checks SET status=?, "
                "confidence=CASE WHEN confidence < 0.90 THEN 0.90 ELSE confidence END "
                "WHERE id=? AND project_id=?", (status, cid, pid))
        else:
            cur = c.execute("UPDATE checks SET status=? WHERE id=? AND project_id=?",
                            (status, cid, pid))
    return {"ok": bool(cur.rowcount), "status": status}


def list_checks(pid: str, round_no: int | None = None) -> list[dict]:
    """从库里读回问题：带 id、人工采纳状态、置信度、来源与页码。"""
    with conn() as c:
        if round_no is None:
            rows = c.execute("SELECT * FROM checks WHERE project_id=? ORDER BY round,id", (pid,))
        else:
            rows = c.execute("SELECT * FROM checks WHERE project_id=? AND round=? ORDER BY id",
                             (pid, round_no))
        return [dict(r) for r in rows]


# ---------------- 任务 ----------------

# 会改写正文的后台任务：同一项目同时只允许一个，避免两个任务互相覆盖同一章
# 单章生成 / 改写 / 恢复也算写任务：占住项目，避免两条链路同时改同一份正文
WRITE_JOB_KINDS = ("compose", "optimize", "section", "rewrite", "restore", "import",
                   "outline")
_ACTIVE_JOB_LOCK = threading.Lock()


def create_job(kind: str, pid: str) -> str:
    """建任务。刚建出来是 queued（还没被工作线程领取），领取后由任务自己改成 running。"""
    jid = new_id()
    with conn() as c:
        c.execute(
            "INSERT INTO jobs(id,kind,project_id,status,progress,step,result,error,created_at) "
            "VALUES(?,?,?,'queued',0,'等待执行','{}','',?)",
            (jid, kind, pid, now()),
        )
    return jid


def active_job(pid: str, kinds: tuple[str, ...] | None = None) -> dict:
    """该项目正在排队 / 运行中的写任务；排队也算占用，避免连点两下起两个任务。"""
    ks = tuple(kinds or WRITE_JOB_KINDS)
    ph = ",".join("?" * len(ks))
    with conn() as c:
        row = c.execute(
            "SELECT id,kind,status,progress,step,created_at FROM jobs "
            f"WHERE project_id=? AND status IN ('queued','running') AND kind IN ({ph}) "
            "ORDER BY rowid LIMIT 1", (pid, *ks)).fetchone()
    return dict(row) if row else {}


def create_job_exclusive(kind: str, pid: str) -> tuple[str, dict]:
    """同一个项目不重复起写任务：已有排队 / 运行中的就返回它，不新建。

    返回 (jid, 占用的任务)。jid 为空字符串表示这次没有新建任务。

    跨进程边界：``_ACTIVE_JOB_LOCK`` 只挡得住同一个进程。多 worker 时每个
    进程各持一把锁，若「查有没有任务」和「插一条新任务」分在两步，两边会
    同时看到空闲并各插一条，于是两个任务并发改写同一份正文。这里改用
    SQLite 的 BEGIN IMMEDIATE 在同一个写事务里完成检查与插入，由数据库
    串行化所有进程的认领；同进程仍保留快路径锁，避免无谓抢写锁。
    """
    with _ACTIVE_JOB_LOCK:
        jid = new_id()
        ks = WRITE_JOB_KINDS
        ph = ",".join("?" * len(ks))
        last = ""
        # WAL 模式下别的进程正在写时，Linux 报 locked、Windows 报 readonly；
        # 与 update_section 同口径重试，绝不把「没抢到」误当成「没有任务」而放行。
        for attempt in range(8):
            try:
                with conn() as c:
                    if not c.in_transaction:
                        c.execute("BEGIN IMMEDIATE")
                    row = c.execute(
                        "SELECT id,kind,status,progress,step,created_at FROM jobs "
                        "WHERE project_id=? AND status IN ('queued','running') "
                        f"AND kind IN ({ph}) ORDER BY rowid LIMIT 1",
                        (pid, *ks)).fetchone()
                    if row:
                        return "", dict(row)
                    c.execute(
                        "INSERT INTO jobs(id,kind,project_id,status,progress,step,"
                        "result,error,created_at) "
                        "VALUES(?,?,?,'queued',0,'等待执行','{}','',?)",
                        (jid, kind, pid, now()))
                return jid, {}
            except sqlite3.OperationalError as exc:
                last = str(exc)
                if not any(k in last.lower()
                           for k in ("locked", "busy", "readonly")):
                    raise
                time.sleep(0.02 * (attempt + 1))
        # 重试仍拿不到写锁：宁可显式失败，也不能让第二个任务并发改写正文
        raise RuntimeError(f"写任务认领失败（数据库忙）：{last[:160]}")


def update_job(jid: str, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k == "result":
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", (*vals, jid))


def get_job(jid: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        return {}
    d = dict(row)
    try:
        d["result"] = json.loads(d.get("result") or "{}")
    except (ValueError, TypeError):
        d["result"] = {}
    return d


def list_pending_jobs(pid: str, hours: int = 24) -> list[dict]:
    """刷新页面后续上：还在跑的任务，以及跑完但结果还没补进对话的任务。"""
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        rows = c.execute(
            "SELECT id,kind,status,progress,step,created_at FROM jobs "
            "WHERE project_id=? AND created_at>=? AND "
            "(status IN ('queued','running') OR (status IN ('done','partial','failed') AND "
            " COALESCE(consumed,0)=0)) ORDER BY rowid", (pid, since)).fetchall()
    return [dict(r) for r in rows]


def mark_job_consumed(jid: str) -> None:
    with conn() as c:
        c.execute("UPDATE jobs SET consumed=1 WHERE id=?", (jid,))


# ---------------- 基座自动编排留痕 ----------------

def save_auto_run(pid: str, stage: str, summary: str, data: dict) -> str:
    rid = new_id()
    with conn() as c:
        c.execute(
            "INSERT INTO auto_runs(id,project_id,stage,summary,data,created_at) VALUES(?,?,?,?,?,?)",
            (rid, pid, stage, summary, json.dumps(data, ensure_ascii=False, default=str), now()),
        )
    return rid


# ---------------- 一次性确认令牌：落库而不是进程内存 ----------------
# 以前导出令牌放在模块级 dict，多 worker 部署时「确认」落在 A 进程、
# 「下载」被分到 B 进程就验不过；服务一重启令牌全丢。这里统一放企业库，
# 原子消费保证「用完即焚」在多进程下同样成立。

def _token_row(row) -> tuple | None:
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    return (row["pid"], row["fingerprint"], float(row["expires"]), payload)


def token_put(kind: str, token: str, value) -> None:
    """写入令牌；顺手清掉过期行。value = (pid, fingerprint, expires[, payload])。"""
    pid, fp, exp = str(value[0]), str(value[1]), float(value[2])
    payload = value[3] if len(value) > 3 else {}
    with conn() as c:
        c.execute("DELETE FROM transient_tokens WHERE expires < ?", (time.time(),))
        c.execute("INSERT OR REPLACE INTO transient_tokens"
                  "(token,kind,pid,fingerprint,expires,payload,created_at) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (str(token), kind, pid, fp, exp,
                   json.dumps(payload or {}, ensure_ascii=False, default=str), now()))


def token_get(kind: str, token: str) -> tuple | None:
    with conn() as c:
        row = c.execute("SELECT pid,fingerprint,expires,payload FROM transient_tokens "
                        "WHERE token=? AND kind=?", (str(token), kind)).fetchone()
    return _token_row(row)


def token_pop(kind: str, token: str) -> tuple | None:
    """原子消费：DELETE ... RETURNING，两个 worker 同时验也只能用掉一次。"""
    with conn() as c:
        try:
            row = c.execute(
                "DELETE FROM transient_tokens WHERE token=? AND kind=? "
                "RETURNING pid,fingerprint,expires,payload",
                (str(token), kind)).fetchone()
        except sqlite3.OperationalError:  # 极老的 SQLite 没有 RETURNING
            row = c.execute("SELECT pid,fingerprint,expires,payload FROM transient_tokens "
                            "WHERE token=? AND kind=?", (str(token), kind)).fetchone()
            if row:
                c.execute("DELETE FROM transient_tokens WHERE token=?", (str(token),))
    return _token_row(row)


def token_keys(kind: str) -> list[str]:
    with conn() as c:
        return [r[0] for r in c.execute(
            "SELECT token FROM transient_tokens WHERE kind=?", (kind,)).fetchall()]


def list_auto_runs(pid: str = "", limit: int = 12) -> list[dict]:
    with conn() as c:
        if pid:
            rows = c.execute(
                "SELECT * FROM auto_runs WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM auto_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except (ValueError, TypeError):
            d["data"] = {}
        out.append(d)
    return out


def last_auto_run(pid: str, stage: str) -> dict:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM auto_runs WHERE project_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (pid, stage)).fetchone()
    if not r:
        return {}
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except (ValueError, TypeError):
        d["data"] = {}
    return d


# ---------------- 对话记忆（跨轮 / 跨刷新） ----------------

def add_message(pid: str, role: str, text: str,
                meta: dict | None = None) -> str:
    """落一条对话。只存文本与轻量元信息，能力卡片 / 执行轨迹仍在 agent_runs。"""
    mid = new_id()
    with conn() as c:
        c.execute(
            "INSERT INTO messages(id,project_id,role,text,meta,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (mid, pid, role, str(text or "")[:3000],
             json.dumps(meta or {}, ensure_ascii=False, default=str), now()),
        )
    return mid


def list_messages(pid: str, limit: int = 20) -> list[dict]:
    """按时间正序返回（最新在后），供观察阶段当上下文、供界面刷新恢复。"""
    with conn() as c:
        rows = c.execute(
            "SELECT role,text,meta,created_at FROM messages WHERE project_id=? "
            "ORDER BY rowid DESC LIMIT ?", (pid, max(1, min(limit, 100)))
        ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (ValueError, TypeError):
            d["meta"] = {}
        out.append(d)
    return out


# ---------------- 智能体决策留痕 ----------------

def save_agent_run(pid: str, message: str, plan: dict, trace: list[dict], reply: str) -> str:
    rid = new_id()
    with conn() as c:
        c.execute(
            "INSERT INTO agent_runs(id,project_id,message,planner,goal,plan,trace,reply,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, pid, message[:500], str(plan.get("planner") or ""),
             str(plan.get("goal") or "")[:200],
             json.dumps(plan, ensure_ascii=False, default=str),
             json.dumps(trace, ensure_ascii=False, default=str),
             reply[:3000], now()),
        )
    return rid


def list_agent_runs(pid: str = "", limit: int = 12) -> list[dict]:
    with conn() as c:
        if pid:
            rows = c.execute(
                "SELECT * FROM agent_runs WHERE project_id=? ORDER BY rowid DESC LIMIT ?",
                (pid, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM agent_runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("plan", "trace"):
            try:
                d[key] = json.loads(d.get(key) or ("{}" if key == "plan" else "[]"))
            except (ValueError, TypeError):
                d[key] = {} if key == "plan" else []
        out.append(d)
    return out


# ---------------- 章节交付状态（写章自检留痕） ----------------

def save_section_quality(pid: str, sid: str, text_hash: str, ok: bool,
                         flags: dict, lines: list[str], rounds: int,
                         deliverable: bool) -> None:
    """记录本章最近一次写章/改写的自检结论；hash 用于判断人工后续是否改过正文。"""
    with conn() as c:
        c.execute(
            "INSERT INTO section_quality(section_id,project_id,text_hash,ok,deliverable,"
            "flags,lines,rounds,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(section_id) DO UPDATE SET "
            "text_hash=excluded.text_hash, ok=excluded.ok, "
            "deliverable=excluded.deliverable, flags=excluded.flags, "
            "lines=excluded.lines, rounds=excluded.rounds, updated_at=excluded.updated_at",
            (sid, pid, text_hash, int(bool(ok)), int(bool(deliverable)),
             json.dumps(flags or {}, ensure_ascii=False),
             json.dumps(lines or [], ensure_ascii=False), int(rounds or 0), now()),
        )


def list_section_quality(pid: str) -> list[dict]:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM section_quality WHERE project_id=?", (pid,))]
    for r in rows:
        for key, fallback in (("flags", {}), ("lines", [])):
            try:
                r[key] = json.loads(r.get(key) or ("{}" if key == "flags" else "[]"))
            except (ValueError, TypeError):
                r[key] = fallback
    return rows


# ---------------- 招标要求表（人工修订优先于自动抽取） ----------------

def list_requirements(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM requirements WHERE project_id=? ORDER BY id", (pid,))]


def add_requirements(pid: str, rows: list[dict]) -> int:
    ts = now()
    with conn() as c:
        before = {r["text"] for r in c.execute(
            "SELECT text FROM requirements WHERE project_id=?", (pid,))}
        # 人工从某条自动行改过来的：原文本不再补回，否则「改了又被重抽覆盖」
        before.update(r["origin_text"] for r in c.execute(
            "SELECT origin_text FROM requirements WHERE project_id=? AND origin_text<>''",
            (pid,)))
        before.update(r["text"] for r in c.execute(
            "SELECT text FROM requirement_suppressed WHERE project_id=?", (pid,)))
        n = 0
        for r in rows:
            text = str(r.get("text") or "").strip()
            if not text or text in before:
                continue
            before.add(text)
            c.execute(
                "INSERT INTO requirements(id,project_id,kind,text,score,source,status,"
                "note,loc,source_version,required,confirmed_by,confirmed_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), pid, str(r.get("kind") or "hard"), text[:300],
                 str(r.get("score") or "")[:30], str(r.get("source") or "auto"),
                 str(r.get("status") or "active"), str(r.get("note") or "")[:300],
                 str(r.get("loc") or "")[:120], str(r.get("source_version") or "")[:40],
                 int(r.get("required") if r.get("required") is not None else 1),
                 str(r.get("confirmed_by") or ""), str(r.get("confirmed_at") or ""),
                 ts, ts),
            )
            n += 1
    return n


def update_requirement(pid: str, rid: str, patch: dict) -> bool:
    allowed = ("kind", "text", "score", "status", "note", "loc",
               "source_version", "required", "confirmed_by", "confirmed_at")
    cols = {k: (int(bool(v)) if k == "required" else str(v)[:300])
            for k, v in (patch or {}).items() if k in allowed}
    if not cols:
        return False
    # 人工改过的行一律标为 manual，后续重抽不会覆盖它；
    # 同时记下最初的自动原文，重抽时不再把这条要求以旧面貌补回来。
    cols["source"] = "manual"
    cols["updated_at"] = now()
    with conn() as c:
        if "text" in cols:
            old = c.execute(
                "SELECT text, origin_text FROM requirements WHERE id=? AND project_id=?",
                (rid, pid)).fetchone()
            if old:
                origin = str(old["origin_text"] or "") or str(old["text"] or "")
                if origin and origin != cols["text"]:
                    cols["origin_text"] = origin[:300]
        sets = ", ".join(f"{k}=?" for k in cols)
        cur = c.execute(
            f"UPDATE requirements SET {sets} WHERE id=? AND project_id=?",
            (*cols.values(), rid, pid))
        return cur.rowcount > 0


def delete_requirement(pid: str, rid: str) -> bool:
    """删一条具体要求；三个固定分类（大项）不受影响，同时清掉它的响应记录。"""
    if str(rid) in ("hard", "rejection", "scoring"):
        return False
    with conn() as c:
        old = c.execute(
            "SELECT text, origin_text FROM requirements WHERE id=? AND project_id=?",
            (rid, pid)).fetchone()
        cur = c.execute("DELETE FROM requirements WHERE id=? AND project_id=?",
                        (rid, pid))
        if cur.rowcount and old:
            # 逐条响应矩阵里的旧记录也一起清掉，避免删掉的要求还留在检查口径里
            try:
                c.execute("DELETE FROM requirement_responses "
                          "WHERE project_id=? AND req_id=?", (pid, rid))
            except sqlite3.OperationalError:
                pass
            for t in (str(old["text"] or ""), str(old["origin_text"] or "")):
                if t:
                    c.execute(
                        "INSERT OR IGNORE INTO requirement_suppressed"
                        "(project_id,text,created_at) VALUES(?,?,?)",
                        (pid, t[:300], now()))
        return cur.rowcount > 0


def clear_suppressed(pid: str) -> int:
    """「恢复被删的自动要求」时清空抑制名单，再重抽即可找回。"""
    with conn() as c:
        return c.execute(
            "DELETE FROM requirement_suppressed WHERE project_id=?", (pid,)).rowcount


# 大项（整组要求）删除标记：借用抑制名单存一条特殊记录，
# 「恢复被删要求」会随 clear_suppressed 一起把它清掉，分组重新出现。
_GROUP_TAG = "__group__:"


def hide_requirement_group(pid: str, kind: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO requirement_suppressed"
                  "(project_id,text,created_at) VALUES(?,?,?)",
                  (pid, _GROUP_TAG + kind, now()))


def show_requirement_group(pid: str, kind: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM requirement_suppressed WHERE project_id=? AND text=?",
                  (pid, _GROUP_TAG + kind))


def hidden_requirement_groups(pid: str) -> list[str]:
    with conn() as c:
        rows = c.execute("SELECT text FROM requirement_suppressed WHERE project_id=?",
                         (pid,)).fetchall()
    tag = len(_GROUP_TAG)
    return [str(r["text"])[tag:] for r in rows
            if str(r["text"] or "").startswith(_GROUP_TAG)]


def clear_auto_requirements(pid: str) -> int:
    """重抽前删掉仍为自动的行；人工新增 / 人工改过的行保留。"""
    with conn() as c:
        cur = c.execute(
            "DELETE FROM requirements WHERE project_id=? AND source='auto'", (pid,))
        return cur.rowcount


# ---------------- 要求表同步标记 ----------------

def get_req_sync_hash(pid: str) -> str:
    with conn() as c:
        row = c.execute("SELECT tender_hash FROM requirement_sync WHERE project_id=?",
                        (pid,)).fetchone()
        return str(row[0]) if row else ""


def set_req_sync_hash(pid: str, h: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO requirement_sync(project_id,tender_hash,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET tender_hash=excluded.tender_hash, "
            "updated_at=excluded.updated_at", (pid, h, now()))


# ---------------- B04：招标文件 / 补遗 / 澄清版本 ----------------

TENDER_KINDS = ("original", "amend", "clarify", "replace")


def add_tender_version(pid: str, kind: str, text: str, filename: str = "",
                       note: str = "", *, effective: bool = True) -> dict:
    kind = kind if kind in TENDER_KINDS else "original"
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(version),0) AS v FROM tender_versions "
                        "WHERE project_id=?", (pid,)).fetchone()
        version = int(row["v"] or 0) + 1
        prev = c.execute("SELECT id FROM tender_versions WHERE project_id=? "
                         "AND effective=1 ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
        if kind == "replace":
            c.execute("UPDATE tender_versions SET effective=0 WHERE project_id=?", (pid,))
        item = {"id": new_id(), "project_id": pid, "version": version, "kind": kind,
                "filename": filename, "text": text,
                "content_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32],
                "supersedes": (prev["id"] if prev else ""),
                "effective": 1 if effective else 0, "note": note, "created_at": now()}
        cols = ",".join(item)
        c.execute(f"INSERT INTO tender_versions({cols}) "
                  f"VALUES({','.join('?' for _ in item)})", tuple(item.values()))
    return item


def list_tender_versions(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tender_versions WHERE project_id=? ORDER BY version", (pid,))]


def current_tender_version(pid: str) -> str:
    rows = [v for v in list_tender_versions(pid) if v.get("effective")]
    return str(rows[-1]["id"]) if rows else ""


def reactivate_requirements(pid: str, texts: list[str], version_id: str) -> int:
    """补遗 / 澄清后仍存在于新全文里的旧自动要求：原地恢复有效。

    add_requirements 按全部历史文本去重，旧行被 retire 后同文本不会重新插入，
    于是「只补新条款」会让没被修改的旧条款凭空消失。这里不新增重复行，
    直接把 superseded 的自动行恢复成 active 并挂到新版本上。
    """
    n = 0
    with conn() as c:
        for text in texts:
            cur = c.execute(
                "UPDATE requirements SET status='active', source_version=?, "
                "note=replace(COALESCE(note,''), ' [新版待复核]', ''), updated_at=? "
                "WHERE project_id=? AND text=? AND source='auto' "
                "AND status='superseded'",
                (version_id, now(), pid, text[:300]))
            n += cur.rowcount
    return n


def retire_requirements_for_version(pid: str, version_id: str) -> int:
    """旧自动要求转历史/失效；人工修订行保留但标待复核，不静默沿用。"""
    with conn() as c:
        cur = c.execute(
            "UPDATE requirements SET status='superseded' WHERE project_id=? "
            "AND source='auto' AND source_version=? AND source_version<>''",
            (pid, version_id))
        c.execute("UPDATE requirements SET note=(note || ' [新版待复核]') "
                  "WHERE project_id=? AND source='manual' AND status='active'",
                  (pid,))
        return cur.rowcount


# ---------------- B01：逐条响应矩阵 ----------------

REQ_STATUS = ("missing", "candidate", "evidence_ready", "confirmed", "na", "review")


def mark_responses_review(pid: str) -> int:
    """B04：招标文件出新版后，之前逐条确认过的要求回到待复核，不能静默沿用。"""
    with conn() as c:
        cur = c.execute(
            "UPDATE requirement_responses SET status='review', updated_at=? "
            "WHERE project_id=? AND status='confirmed'", (now(), pid))
        return cur.rowcount


def save_req_response(pid: str, req_id: str, **fields) -> dict:
    allowed = ("section_id", "anchor", "evidence", "candidate", "evidence_ok",
               "status", "reviewer", "reviewed_at", "fingerprint")
    cols = {k: (int(bool(v)) if k in ("candidate", "evidence_ok") else str(v)[:600])
            for k, v in fields.items() if k in allowed}
    with conn() as c:
        old = c.execute("SELECT id FROM requirement_responses "
                        "WHERE project_id=? AND req_id=?", (pid, req_id)).fetchone()
        if old:
            cols["updated_at"] = now()
            sets = ",".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE requirement_responses SET {sets} WHERE id=?",
                      (*cols.values(), old["id"]))
            rid = old["id"]
        else:
            rid = new_id()
            cols.update({"id": rid, "project_id": pid, "req_id": req_id,
                         "updated_at": now()})
            c.execute(f"INSERT INTO requirement_responses({','.join(cols)}) "
                      f"VALUES({','.join('?' for _ in cols)})", tuple(cols.values()))
    return get_req_response(pid, req_id) or {}


def get_req_response(pid: str, req_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM requirement_responses "
                      "WHERE project_id=? AND req_id=?", (pid, req_id)).fetchone()
        return dict(r) if r else None


def list_req_responses(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM requirement_responses WHERE project_id=?", (pid,))]


def confirm_response(pid: str, req_id: str, reviewer: str = "人工复核",
                     note: str = "") -> dict | None:
    """人工确认：确认后该条才算落实；不适用必须有理由，否则拒绝。"""
    from . import reqmatrix, requirement_support
    if not str(reviewer or "").strip():
        return {"ok": False, "error": "必须指定复核责任人"}
    report = reqmatrix.build(pid)
    item = next((r for r in report["items"] if r["id"] == req_id), None)
    if not item:
        return {"ok": False, "error": "要求不存在或已停用"}
    if not item.get("evidence_ok"):
        return {"ok": False, "error": "响应或支持材料未核验完成：" +
                "；".join(item.get("support_gaps") or ["缺明确响应、数值或检查未完成"])}
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM requirement_responses WHERE project_id=? "
                      "AND req_id=?", (pid, req_id)).fetchone()
        row = dict(r) if r else None
        if not row:
            return None
        latest = requirement_support.evaluate(pid,
            requirement_support.requirement(pid,req_id),row)
        if latest["fingerprint"] != item["fingerprint"] or not latest["ok"]:
            return {"ok": False, "error": "复核期间材料已变化，请刷新"}
        c.execute("UPDATE requirement_responses SET status='confirmed',reviewer=?,reviewed_at=?,"
                  "fingerprint=?,updated_at=? WHERE project_id=? AND req_id=?",
                  (reviewer,now(),latest["fingerprint"],now(),pid,req_id))
        c.execute("INSERT INTO manual_confirmations "
                  "(id,project_id,target,note,content_hash,reviewer,reviewed_at) VALUES(?,?,?,?,?,?,?)",
                  (new_id(),pid,"requirement:"+req_id,note,latest["fingerprint"],reviewer,now()))
    return get_req_response(pid,req_id)


# ---------------- B02：企业事实、承诺与原件附件 ----------------

FACT_KINDS = ("example", "enterprise_fact", "project_commitment", "attachment")


def add_fact(pid: str, entity: str, field: str, value: str, *, unit: str = "",
             scope: str = "", valid_until: str = "", source_doc_id: str = "",
             source_loc: str = "", kind: str = "enterprise_fact",
             verification: str = "unverified") -> dict:
    item = {"id": new_id(), "project_id": pid, "entity": entity, "field": field,
            "value": value, "unit": unit, "scope": scope, "valid_until": valid_until,
            "source_doc_id": source_doc_id, "source_loc": source_loc,
            "kind": kind if kind in FACT_KINDS else "enterprise_fact",
            "verification": verification, "created_at": now()}
    with conn() as c:
        c.execute(f"INSERT INTO project_facts({','.join(item)}) "
                  f"VALUES({','.join('?' for _ in item)})", tuple(item.values()))
    return item


def list_facts(pid: str, kind: str = "") -> list[dict]:
    sql = "SELECT * FROM project_facts WHERE project_id=?"
    vals: list = [pid]
    if kind:
        sql += " AND kind=?"
        vals.append(kind)
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at", vals)]


def verify_fact(fid: str, by: str = "人工核验") -> dict | None:
    with conn() as c:
        c.execute("UPDATE project_facts SET verification=?, source_loc="
                  "(source_loc || ' [已核验]') WHERE id=?", ("verified:" + by, fid))
        r = c.execute("SELECT * FROM project_facts WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


# ---------------- P2：企业资产台账（资质 / 人员 / 业绩 / 检测 / 获奖） ----------------

ASSET_CATEGORIES = ("qualification", "personnel", "performance", "testing", "award")
ASSET_LABELS = {
    "qualification": "资质证照", "personnel": "人员证书", "performance": "业绩证明",
    "testing": "检测报告", "award": "获奖荣誉",
}
ASSET_FIELDS = ("category", "title", "holder", "code", "valid_until",
                "source_ref", "note")


def add_asset(*, category: str = "qualification", title: str = "",
              holder: str = "", code: str = "", valid_until: str = "",
              source_ref: str = "", note: str = "") -> dict:
    stamp = now()
    item = {"id": new_id(),
            "category": category if category in ASSET_CATEGORIES else "qualification",
            "title": str(title or "").strip()[:120],
            "holder": str(holder or "").strip()[:120],
            "code": str(code or "").strip()[:120],
            "valid_until": str(valid_until or "").strip()[:10],
            "source_ref": str(source_ref or "").strip()[:300],
            "note": str(note or "").strip()[:500],
            "verification": "unverified",
            "created_at": stamp, "updated_at": stamp}
    with conn() as c:
        c.execute(f"INSERT INTO assets({','.join(item)}) "
                  f"VALUES({','.join('?' for _ in item)})", tuple(item.values()))
    return item


def list_assets(category: str = "") -> list[dict]:
    sql = "SELECT * FROM assets WHERE 1=1"
    vals: list = []
    if category:
        sql += " AND category=?"
        vals.append(category)
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY category, created_at", vals)]


def get_asset(aid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def update_asset(aid: str, **fields) -> dict | None:
    patch = {k: str(v or "").strip()[:500] for k, v in fields.items()
             if k in ASSET_FIELDS}
    if "category" in patch and patch["category"] not in ASSET_CATEGORIES:
        patch.pop("category")
    if "title" in patch:
        patch["title"] = patch["title"][:120]
    if "valid_until" in patch:
        patch["valid_until"] = patch["valid_until"][:10]
    if not patch:
        return get_asset(aid)
    patch["updated_at"] = now()
    with conn() as c:
        c.execute(f"UPDATE assets SET {','.join(k + '=?' for k in patch)} WHERE id=?",
                  (*patch.values(), aid))
        r = c.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def delete_asset(aid: str) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM assets WHERE id=?", (aid,))
    return bool(cur.rowcount)


def verify_asset(aid: str, by: str = "人工核验") -> dict | None:
    with conn() as c:
        c.execute("UPDATE assets SET verification=?, updated_at=? WHERE id=?",
                  ("verified:" + by, now(), aid))
        r = c.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def add_attachment(pid: str, filename: str, path: str, *, fact_id: str = "",
                   doc_id: str = "", section_id: str = "", page: int = 0,
                   note: str = "") -> dict:
    item = {"id": new_id(), "project_id": pid, "fact_id": fact_id,
            "filename": filename, "path": path, "doc_id": doc_id,
            "section_id": section_id, "page": page, "note": note,
            "created_at": now()}
    with conn() as c:
        c.execute(f"INSERT INTO attachments({','.join(item)}) "
                  f"VALUES({','.join('?' for _ in item)})", tuple(item.values()))
    return item


def list_attachments(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM attachments WHERE project_id=? ORDER BY created_at", (pid,))]


# ---------------- B03：人工确认（签章 / 表单 / 有效期） ----------------

def add_confirmation(pid: str, target: str, *, section_id: str = "",
                     note: str = "", content_hash: str = "",
                     reviewer: str = "人工复核") -> dict:
    item = {"id": new_id(), "project_id": pid, "target": target,
            "section_id": section_id, "note": note, "content_hash": content_hash,
            "reviewer": reviewer, "reviewed_at": now()}
    with conn() as c:
        c.execute(f"INSERT INTO manual_confirmations({','.join(item)}) "
                  f"VALUES({','.join('?' for _ in item)})", tuple(item.values()))
    return item


def list_confirmations(pid: str, target: str = "") -> list[dict]:
    sql = "SELECT * FROM manual_confirmations WHERE project_id=?"
    vals: list = [pid]
    if target:
        sql += " AND target=?"
        vals.append(target)
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY reviewed_at DESC", vals)]


# ---------------- C03：长任务章节检查点 ----------------

def save_checkpoint(pid: str, job_id: str, section_id: str, status: str,
                    ver: int = 0, note: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO task_checkpoints(project_id,job_id,section_id,status,"
                  "ver,note,updated_at) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(project_id,job_id,section_id) DO UPDATE SET "
                  "status=excluded.status,ver=excluded.ver,note=excluded.note,"
                  "updated_at=excluded.updated_at",
                  (pid, job_id, section_id, status, ver, note, now()))


def list_checkpoints(pid: str, job_id: str = "") -> list[dict]:
    sql = "SELECT * FROM task_checkpoints WHERE project_id=?"
    vals: list = [pid]
    if job_id:
        sql += " AND job_id=?"
        vals.append(job_id)
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY updated_at", vals)]



# ---------------- C01：项目暗标 / 格式 / 分册规则 ----------------

def get_project_rules(pid: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT data FROM project_rules WHERE project_id=?", (pid,)).fetchone()
    if not r:
        return {}
    try:
        v = json.loads(r["data"] or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def save_project_rules(pid: str, data: dict) -> dict:
    raw = json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
    with conn() as c:
        c.execute("INSERT INTO project_rules(project_id,data,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(project_id) DO UPDATE SET data=excluded.data, "
                  "updated_at=excluded.updated_at", (pid, raw, now()))
    return data


# ---------------- C02：项目负责人 / 成员角色 / 章节分工与复核 ----------------

PROJECT_ROLES = ("lead", "editor", "reviewer", "reader")
REVIEW_STATES = ("pending", "approved", "rejected")


def list_project_members(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM project_members WHERE project_id=? ORDER BY role, user_id",
            (pid,))]


def set_project_member(pid: str, uid: str, role: str) -> dict:
    role = role if role in PROJECT_ROLES else "reader"
    with conn() as c:
        c.execute(
            "INSERT INTO project_members(project_id,user_id,role,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET "
            "role=excluded.role, updated_at=excluded.updated_at",
            (pid, uid, role, now()))
    return {"project_id": pid, "user_id": uid, "role": role}


def remove_project_member(pid: str, uid: str) -> bool:
    with conn() as c:
        return c.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (pid, uid)).rowcount > 0


def list_section_assignees(pid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM section_assignees WHERE project_id=?", (pid,))]


def assign_section(pid: str, sid: str, *, user_id: str | None = None,
                   review_status: str | None = None, reviewer: str = "") -> dict:
    """分工与复核状态分开写；复核结论带责任人与时间。"""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM section_assignees WHERE project_id=? AND section_id=?",
            (pid, sid)).fetchone()
        cur = dict(row) if row else {
            "project_id": pid, "section_id": sid, "user_id": "",
            "review_status": "pending", "reviewer": "", "reviewed_at": ""}
        if user_id is not None:
            cur["user_id"] = str(user_id)
        if review_status in REVIEW_STATES:
            cur["review_status"] = review_status
            cur["reviewer"] = reviewer
            cur["reviewed_at"] = now() if review_status != "pending" else ""
        c.execute(
            "INSERT INTO section_assignees(project_id,section_id,user_id,review_status,"
            "reviewer,reviewed_at,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,section_id) DO UPDATE SET "
            "user_id=excluded.user_id, review_status=excluded.review_status, "
            "reviewer=excluded.reviewer, reviewed_at=excluded.reviewed_at, "
            "updated_at=excluded.updated_at",
            (pid, sid, cur["user_id"], cur["review_status"], cur["reviewer"],
             cur["reviewed_at"], now()))
    return cur
