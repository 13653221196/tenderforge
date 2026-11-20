"""C02 项目协作权限：服务端唯一判定点。

- 平台管理员（默认企业的管理员或显式标记）> 企业管理员 > 项目角色；
- 项目角色：负责人 lead / 编辑 editor / 复核 reviewer / 只读 reader；
- 老项目没有成员名册时，同企业登录用户默认按 editor 兼容（明确标 legacy_open），
  一旦负责人加了第一个成员，就切换成「按名单访问」，不在名单里连下载都不行；
- 所有判定都在服务端做，前端隐藏按钮不算权限。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from fastapi import HTTPException

from . import auth, store

ROLES = ("lead", "editor", "reviewer", "reader")
ROLE_LABEL = {"lead": "项目负责人", "editor": "编辑", "reviewer": "复核",
              "reader": "只读", "": "无访问权"}
ACTION_LABEL = {"read": "查看", "write": "写正文或改配置", "review": "复核",
                "export": "下载文件", "manage": "归档 / 删除 / 收录 / 恢复 / 管成员"}
PERMS: dict[str, set[str]] = {
    "read": set(ROLES),
    "write": {"lead", "editor"},
    "review": {"lead", "reviewer"},
    "export": {"lead", "editor", "reviewer"},
    "manage": {"lead"},
}

# 这些 POST 只是临时计算 / 只读预览，不写项目数据
READ_ONLY_POST = ("/quote-check", "/retrieve", "/bundle/scan")
MANAGE_HINTS = ("/archive", "/to-kb", "/restore", "/team")
EXPORT_HINTS = ("/export", "/artifacts/", "/attachments/")


def is_platform_admin() -> bool:
    return auth.is_platform_admin()


def project_role(pid: str) -> dict:
    """当前用户在这个项目里的角色与访问模式。"""
    me = auth.current()
    uid = str(me.get("id") or me.get("username") or "")
    org_admin = bool(me.get("role") == "owner")
    plat = is_platform_admin()
    members = store.list_project_members(pid)
    base = {"user_id": uid, "org_admin": org_admin,
            "platform_admin": plat, "member_count": len(members),
            "legacy_open": not members}
    if org_admin or plat:
        # 企业管理员 / 平台管理员在本企业项目里拥有全部权限
        return {**base, "role": "lead", "via": "enterprise_admin"}
    if not members:
        # 老数据兼容：没有名册就按同企业可编辑，界面和手册里如实写明
        return {**base, "role": "editor", "via": "legacy_open"}
    row = next((m for m in members if str(m.get("user_id") or "") == uid), None)
    if not row:
        return {**base, "role": "", "via": "no_access"}
    return {**base, "role": str(row.get("role") or "reader"),
            "via": "project_member"}


def can(pid: str, action: str) -> tuple[bool, str]:
    r = project_role(pid)
    role = r["role"]
    if not role:
        return False, "你不在这个项目的成员名单里，没有访问权"
    if role in PERMS.get(action, set()):
        return True, ""
    return False, (f"项目角色「{ROLE_LABEL.get(role, role)}」不能"
                   f"{ACTION_LABEL.get(action, action)}；请联系项目负责人")


def require(pid: str, action: str) -> dict:
    ok, why = can(pid, action)
    if not ok:
        raise HTTPException(403, why)
    return project_role(pid)


def action_for(path: str, method: str) -> tuple[str, str]:
    """从路径推断需要的权限；返回 (pid, action)，pid 为空表示不用校验。"""
    m = re.match(r"^/api/projects/([^/]+)(/.*)?$", path)
    if m:
        pid, tail = unquote(m.group(1)), m.group(2) or ""
        if method == "GET":
            action = "export" if any(k in tail for k in EXPORT_HINTS) else "read"
        elif "/reqmatrix/" in tail and (tail.endswith("/confirm") or tail.endswith("/na")
                                        or "/support" in tail):
            action = "review"
        elif any(k in tail for k in MANAGE_HINTS) or (method == "DELETE" and not tail):
            action = "manage"
        elif method == "POST" and any(tail.endswith(k) for k in READ_ONLY_POST):
            action = "read"
        else:
            action = "write"
        return pid, action
    # 资料补录 / 确认不在 /projects/{pid} 路径下，按资料所属项目校验
    m2 = re.match(r"^/api/docs/([^/]+)", path)
    if m2 and method != "GET":
        doc = store.get_doc(unquote(m2.group(1)))
        if doc and str(doc.get("project_id") or ""):
            return str(doc["project_id"]), "write"
    return "", ""


def guard(path: str, method: str) -> tuple[bool, str]:
    pid, action = action_for(path, method)
    if not pid:
        return True, ""
    # 项目不存在时走 legacy_open，交由路由返回 404；不在这里伪装成权限问题，
    # 也避免每个请求都多加载一次整本项目（章节 / 资料 / 检查记录）。
    return can(pid, action)


def team_report(pid: str) -> dict:
    """项目团队面板的数据：元数据、成员名册、章节分工与复核状态。"""
    p = store.get_project(pid) or {}
    members = store.list_project_members(pid)
    assigns = {a["section_id"]: a for a in store.list_section_assignees(pid)}
    secs = [{"id": s["id"], "title": s.get("title") or "",
             "assignee": (assigns.get(s["id"]) or {}).get("user_id") or "",
             "review_status": (assigns.get(s["id"]) or {}).get("review_status") or "pending"}
            for s in p.get("sections") or []]
    users = []
    try:
        for u in auth.list_users(auth.org_id()):
            users.append({"id": str(u.get("id") or ""),
                          "name": str(u.get("name") or u.get("username") or ""),
                          "username": str(u.get("username") or ""),
                          "role": str(u.get("role") or "")})
    except Exception:
        users = []
    r = project_role(pid)
    return {
        "meta": {"owner_id": str(p.get("owner_id") or ""),
                 "due_at": str(p.get("due_at") or ""),
                 "final_version": str(p.get("final_version") or "")},
        "members": members, "users": users, "sections": secs,
        "my_role": r["role"], "role_label": ROLE_LABEL.get(r["role"], r["role"]),
        "legacy_open": bool(r["legacy_open"]), "roles": [
            {"key": k, "label": ROLE_LABEL[k]} for k in ROLES],
        "note": ("还没有项目成员名册：同企业账号默认可编辑（旧数据兼容）。"
                 "添加第一个成员后，只有名册里的人能访问本项目。"
                 if r["legacy_open"] else
                 "本项目按成员名册授权；不在名册里的同企业账号不能查看或下载。"),
    }
