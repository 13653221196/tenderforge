"""企业与账号：注册企业、登录、会话、成员管理。

设计要点
- 认证数据单独放 `data/auth.db`（组织 / 用户 / 会话），与业务库分离。
- 业务数据按企业分库：默认企业用 `data/app.db`，其余用 `data/orgs/<org_id>.db`。
  物理隔离，任何一处漏写过滤条件也读不到别家数据。
- 密码 PBKDF2-SHA256 加盐，会话 token 只存 SHA-256 摘要，库被拷走也不能直接登录。
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

AUTH_DB = DATA_DIR / "auth.db"
DEFAULT_ORG = "default"          # 老数据的归属企业，库仍是 data/app.db
SESSION_DAYS = 30
ROLES = ("owner", "member")
ROLE_LABEL = {"owner": "管理员", "member": "成员"}

# 单次请求的登录态：中间件写入，接口只读取
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "bidagent_current_user", default=None)

_HASH_ROUNDS = 150_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',
    pass_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_login TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    ua TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    return uuid_hex()


def uuid_hex() -> str:
    return secrets.token_hex(6)


@contextmanager
def conn():
    c = sqlite3.connect(str(AUTH_DB), timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=20000")
        c.execute("PRAGMA journal_mode=WAL")
        with c:
            yield c
    finally:
        c.close()


# ---------------- 密码与会话 ----------------

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), _HASH_ROUNDS)
    return f"{salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, digest = str(stored).split("$", 1)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), _HASH_ROUNDS)
    return hmac.compare_digest(dk.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(u: dict) -> dict:
    return {
        "id": u["id"], "org_id": u["org_id"], "org_name": u.get("org_name", ""),
        "username": u["username"], "name": u.get("name") or u["username"],
        "role": u["role"], "role_label": ROLE_LABEL.get(u["role"], u["role"]),
        "status": u.get("status", "active"),
        "created_at": u.get("created_at", ""), "last_login": u.get("last_login", ""),
    }


# ---------------- 企业 ----------------

def list_orgs() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM orgs ORDER BY created_at").fetchall()]


def get_org(org_id: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    return dict(row) if row else {}


def create_org(name: str, username: str, password: str, display: str = "") -> dict:
    """新建企业，并建该企业的第一个管理员。任一步失败都回滚，不留半个企业。"""
    name = (name or "").strip()
    username = normalize_username(username)
    if not name:
        raise ValueError("请填写企业名称")
    if not username:
        raise ValueError("请填写登录账号")
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    if find_user(username):
        raise ValueError("这个账号已被占用，换一个（例如加企业简称前缀）")
    org_id = "org_" + uuid_hex()
    with conn() as c:
        c.execute("INSERT INTO orgs(id,name,status,created_at) VALUES(?,?,?,?)",
                  (org_id, name, "active", now()))
        uid = uuid_hex()
        c.execute(
            "INSERT INTO users(id,org_id,username,name,role,pass_hash,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, org_id, username, (display or username).strip(), "owner",
             hash_password(password), "active", now()))
    return {"org": get_org(org_id), "user_id": uid}


def rename_org(org_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("企业名称不能为空")
    with conn() as c:
        c.execute("UPDATE orgs SET name=? WHERE id=?", (name, org_id))
    return get_org(org_id)


# ---------------- 用户 ----------------

def normalize_username(s: str) -> str:
    return re.sub(r"[^a-z0-9._@-]", "", (s or "").strip().lower())[:40]


def find_user(username: str) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT u.*, o.name AS org_name, o.status AS org_status FROM users u "
            "JOIN orgs o ON o.id=u.org_id WHERE u.username=?",
            (normalize_username(username),)).fetchone()
    return dict(row) if row else {}


def list_users(org_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT u.*, o.name AS org_name FROM users u JOIN orgs o ON o.id=u.org_id "
            "WHERE u.org_id=? ORDER BY CASE u.role WHEN 'owner' THEN 0 ELSE 1 END, u.created_at",
            (org_id,)).fetchall()
    return [_public_user(dict(r)) for r in rows]


def create_user(org_id: str, username: str, password: str,
                display: str = "", role: str = "member") -> dict:
    username = normalize_username(username)
    if not username:
        raise ValueError("账号只能包含字母、数字、. _ @ -")
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    if role not in ROLES:
        raise ValueError("角色只能是 owner 或 member")
    if find_user(username):
        raise ValueError("这个账号已被占用，换一个")
    uid = uuid_hex()
    with conn() as c:
        c.execute(
            "INSERT INTO users(id,org_id,username,name,role,pass_hash,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, org_id, username, (display or username).strip(), role,
             hash_password(password), "active", now()))
    return get_user_by_id(uid)


def get_user_by_id(uid: str) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT u.*, o.name AS org_name FROM users u JOIN orgs o ON o.id=u.org_id "
            "WHERE u.id=?", (uid,)).fetchone()
    return _public_user(dict(row)) if row else {}


def set_password(uid: str, password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    with conn() as c:
        c.execute("UPDATE users SET pass_hash=? WHERE id=?", (hash_password(password), uid))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


def set_user_status(uid: str, status: str) -> dict:
    if status not in ("active", "disabled"):
        raise ValueError("状态只能是 active 或 disabled")
    with conn() as c:
        row = c.execute("SELECT org_id, role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ValueError("成员不存在")
        if status == "disabled":
            owners = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE org_id=? AND role='owner' "
                "AND status='active'", (row["org_id"],)).fetchone()["n"]
            if row["role"] == "owner" and owners <= 1:
                raise ValueError("这是本企业最后一个管理员，不能停用")
        c.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
        if status == "disabled":
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return get_user_by_id(uid)


def set_user_role(uid: str, role: str) -> dict:
    if role not in ROLES:
        raise ValueError("角色只能是 owner 或 member")
    with conn() as c:
        row = c.execute("SELECT org_id, role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ValueError("成员不存在")
        if row["role"] == "owner" and role != "owner":
            owners = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE org_id=? AND role='owner' "
                "AND status='active'", (row["org_id"],)).fetchone()["n"]
            if owners <= 1:
                raise ValueError("这是本企业最后一个管理员，不能降级")
        c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    return get_user_by_id(uid)


def delete_user(uid: str) -> None:
    with conn() as c:
        row = c.execute("SELECT org_id, role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ValueError("成员不存在")
        if row["role"] == "owner":
            owners = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE org_id=? AND role='owner' "
                "AND status='active'", (row["org_id"],)).fetchone()["n"]
            if owners <= 1:
                raise ValueError("这是本企业最后一个管理员，不能删除")
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


def count_users(org_id: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users WHERE org_id=?",
                         (org_id,)).fetchone()["n"]


# ---------------- 登录 / 会话 ----------------

def login(username: str, password: str, ua: str = "") -> dict:
    u = find_user(username)
    if not u or not verify_password(password, u["pass_hash"]):
        raise ValueError("账号或密码不对")
    if u["status"] != "active":
        raise ValueError("这个账号已被停用，请联系企业管理员")
    if u.get("org_status") != "active":
        raise ValueError("企业已被停用")
    token = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        c.execute(
            "INSERT INTO sessions(token_hash,user_id,org_id,ua,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (_token_hash(token), u["id"], u["org_id"], (ua or "")[:200], now(), exp))
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
        # 顺手清掉过期会话，免得表无限涨
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    return {"token": token, "expires_at": exp, "user": _public_user(u)}


def resolve(token: str) -> dict:
    if not token:
        return {}
    with conn() as c:
        row = c.execute(
            "SELECT u.*, o.name AS org_name, o.status AS org_status, s.expires_at "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "JOIN orgs o ON o.id=u.org_id WHERE s.token_hash=?",
            (_token_hash(token),)).fetchone()
    if not row:
        return {}
    u = dict(row)
    if u["expires_at"] < now() or u["status"] != "active" or u["org_status"] != "active":
        return {}
    return _public_user(u)


def logout(token: str) -> None:
    if not token:
        return
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def logout_all(uid: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


# ---------------- 当前请求上下文 ----------------

def set_current(user: dict | None):
    return _current.set(user or None)


def reset_current(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, RuntimeError):
        _current.set(None)


def current() -> dict:
    return _current.get() or {}


def org_id() -> str:
    return current().get("org_id") or DEFAULT_ORG


def is_owner() -> bool:
    return current().get("role") == "owner"


def is_platform_admin() -> bool:
    """平台管理员：默认企业的管理员（老部署天然如此）或显式标记的账号。

    企业管理员只管本企业；平台管理员在本企业上下文里拥有全部项目权限。
    跨企业切换仍按登录账号所属企业，不实现跨库漫游。
    """
    u = current()
    return (bool(u.get("platform_admin"))
            or (str(u.get("org_id") or DEFAULT_ORG) == DEFAULT_ORG
                and u.get("role") == "owner"))


# ---------------- 初始化 ----------------

def init_auth() -> dict:
    """建表；库里有组织但没有用户时，生成一个初始管理员。

    初始密码随机生成，写到 data/初始管理员.txt，只在文件不存在时创建一次。
    """
    AUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        orgs = c.execute("SELECT id FROM orgs").fetchall()
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if not orgs:
            c.execute("INSERT INTO orgs(id,name,status,created_at) VALUES(?,?,?,?)",
                      (DEFAULT_ORG, "默认企业", "active", now()))
            orgs = [{"id": DEFAULT_ORG}]
    out = {"created_admin": False, "orgs": [o["id"] for o in orgs]}
    if users:
        return out
    # 初始管理员
    pwd = _initial_password()
    org = get_org(DEFAULT_ORG) or {}
    name = org.get("name") or "默认企业"
    with conn() as c:
        c.execute(
            "INSERT INTO users(id,org_id,username,name,role,pass_hash,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid_hex(), DEFAULT_ORG, "admin", "系统管理员", "owner",
             hash_password(pwd), "active", now()))
    note = DATA_DIR / "初始管理员.txt"
    try:
        note.write_text(
            "标书智能体 · 初始管理员\n"
            "====================\n"
            f"企业：{name}\n账号：admin\n密码：{pwd}\n\n"
            "登录后请到「设置 · 企业与成员」改掉密码，并给同事开账号。\n"
            "这个文件可以删掉，删除后不再生成。\n",
            encoding="utf-8")
    except OSError:
        pass
    print(f"[bidagent] 初始管理员已创建：账号 admin / 密码 {pwd}（也写入 data/初始管理员.txt）")
    out["created_admin"] = True
    out["password"] = pwd
    return out


def _initial_password() -> str:
    """首次初始化管理员用的密码。

    本地/自有服务器仍随机生成并写进 data/初始管理员.txt。发布到平台或容器时
    这个文件在远端读不到，允许用 BIDAGENT_INIT_ADMIN_PASSWORD 指定一次；
    只在「账号库为空、首次建管理员」时生效，已有账号的库完全不受影响。
    短于 8 位一律忽略并回退随机，避免把弱口令写进生产库。
    """
    configured = (os.environ.get("BIDAGENT_INIT_ADMIN_PASSWORD") or "").strip()
    return configured if len(configured) >= 8 else _random_password()


def _random_password(n: int = 12) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
