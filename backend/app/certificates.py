"""证照有效期：只做确定性日期判定，不猜证书内容。

项目里原本就有两处基础：`project_facts.valid_until` 与 `facts._expired()`
（过期事实不会被当成可核验依据）。这里补的是把它们连成用户看得见的链路：

1. 知识库 / 项目资料里的证照也能带有效期（kb_docs.valid_until）；
2. 「已过期」「快到期」进导出门禁，并在资料卡片上标出来。

刻意不做两件事，避免既不准又打扰人：
- 不自动从证书图片 OCR 到期日（认错一个数字比不认更危险），日期由人填一次；
- 不给没填有效期的素材报警（否则每条普通素材都要被盘问一遍）。

导出口径沿用既定契约：过期是「高风险重点提醒」，知情令牌仍可下载，
但风险随产物登记，不写成「已通过」。
"""
from __future__ import annotations

from datetime import date

from . import store

WARN_DAYS = 60
STATUS_LABELS = {
    "expired": "已过期",
    "expiring": "即将到期",
    "ok": "有效",
    "invalid": "日期格式不对",
    "unknown": "未填有效期",
}


def expiry_status(valid_until: str, *, today: date | None = None,
                  warn_days: int = WARN_DAYS) -> str:
    """expired / expiring / ok / invalid / unknown，纯函数便于回归。"""
    raw = str(valid_until or "").strip()[:10]
    if not raw:
        return "unknown"
    try:
        until = date.fromisoformat(raw)
    except ValueError:
        return "invalid"
    t = today or date.today()
    if until < t:
        return "expired"
    if (until - t).days <= warn_days:
        return "expiring"
    return "ok"


def _collect(pid: str) -> list[dict]:
    """汇总四个来源里「填了有效期」的条目；没填的一律不进清单。

    第四个来源是 P2 企业资产台账（企业级、跨项目复用）。旧的三路一条都不迁移，
    资产只是并列加进来，所以老项目原有的证照提醒不会丢、也不会重复。
    """
    sources: list[tuple[str, list[dict]]] = []
    for label, fn in (("企业知识库", lambda: store.list_docs(scope="kb")),
                      ("本项目资料", lambda: store.list_docs(project_id=pid)),
                      ("企业事实", lambda: store.list_facts(pid)),
                      ("企业资产", lambda: store.list_assets())):
        try:
            sources.append((label, list(fn() or [])))
        except Exception:
            # 某一路读不出来不能当成「没有证照」，交给 export_issues 兜底报错
            sources.append((label, []))
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for origin, rows in sources:
        for row in rows:
            until = str(row.get("valid_until") or "").strip()
            if not until:
                continue
            rid = str(row.get("id") or "")
            key = (origin, rid)
            if key in seen:
                continue
            seen.add(key)
            title = (str(row.get("title") or "")
                     or " · ".join(x for x in (str(row.get("entity") or ""),
                                               str(row.get("field") or "")) if x)
                     or "未命名证照")
            out.append({"id": rid, "origin": origin, "title": title[:80],
                        "category": str(row.get("category") or ""),
                        "valid_until": until[:10]})
    return out


def scan(pid: str, *, today: date | None = None,
         warn_days: int = WARN_DAYS) -> dict:
    """只读体检：按项目汇总证照有效期状态，不写库、不调模型。"""
    t = today or date.today()
    items = []
    for it in _collect(pid):
        st = expiry_status(it["valid_until"], today=t, warn_days=warn_days)
        try:
            days = (date.fromisoformat(it["valid_until"][:10]) - t).days
        except ValueError:
            days = None
        items.append({**it, "status": st, "label": STATUS_LABELS[st],
                      "days": days})
    order = {"expired": 0, "invalid": 1, "expiring": 2, "ok": 3}
    items.sort(key=lambda x: (order.get(x["status"], 9), x["valid_until"]))
    counts = {k: sum(1 for x in items if x["status"] == k) for k in STATUS_LABELS}
    counts.pop("unknown", None)
    return {"items": items, "counts": counts, "warn_days": warn_days,
            "total": len(items),
            "summary": (f"已登记有效期 {len(items)} 项：{counts['expired']} 项已过期、"
                        f"{counts['expiring']} 项 {warn_days} 天内到期"
                        if items else "还没有登记过证照有效期")}


def export_issues(pid: str) -> list[dict]:
    """接入导出门禁：过期列高风险重点提醒，临期与坏日期列中等提醒。"""
    try:
        r = scan(pid)
    except Exception as exc:
        return [{"key": "certificate_error", "title": "证照有效期没查完",
                 "level": "高", "blocking": True, "action": "重试体检",
                 "detail": (f"有效期检查未能完成（{type(exc).__name__}: "
                            f"{str(exc)[:120]}），本轮不能当作已核对。"),
                 "evidence": "backend/app/certificates.py::scan"}]
    out: list[dict] = []
    for i, it in enumerate(r["items"]):
        st = it["status"]
        if st == "ok":
            continue
        if st == "expired":
            out.append({
                "key": f"certificate-expired-{i}", "level": "高", "blocking": True,
                "action": "更新证照", "source": "证照有效期",
                "title": f"证照已过期 · {it['title']}",
                "detail": (f"{it['origin']}的「{it['title']}」有效期至 "
                           f"{it['valid_until']}，已过期 {abs(it['days'] or 0)} 天。"
                           "过期证照不会作为可核验依据；请更新原件并重新核对，"
                           "否则可能构成资格项不响应。"),
                "evidence": f"certificates:{it['origin']}:{it['id']}"})
        elif st == "expiring":
            out.append({
                "key": f"certificate-expiring-{i}", "level": "中", "blocking": False,
                "action": "核对证照", "source": "证照有效期",
                "title": f"证照即将到期 · {it['title']}",
                "detail": (f"{it['origin']}的「{it['title']}」有效期至 "
                           f"{it['valid_until']}，还有 {it['days']} 天到期。"
                           "请确认开标日与递标日仍在有效期内。"),
                "evidence": f"certificates:{it['origin']}:{it['id']}"})
        elif st == "invalid":
            out.append({
                "key": f"certificate-invalid-{i}", "level": "中", "blocking": False,
                "action": "更正日期", "source": "证照有效期",
                "title": f"有效期日期格式不对 · {it['title']}",
                "detail": (f"填的是「{it['valid_until']}」，应为 YYYY-MM-DD；"
                           "格式不对时系统不会做任何到期判定。"),
                "evidence": f"certificates:{it['origin']}:{it['id']}"})
    return out
