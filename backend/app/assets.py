"""P2 企业资产台账：五分类 + 有效期五态 + 到期预警。

只读汇总，不做三件事，避免看起来聪明但实际误导投标：

- 不判定「有这个证能得几分」（得分反查在 asset_match.py，且只报候选材料）；
- 不自动从证书图片 / OCR 认到期日（认错一个数字比不认更危险）；
- 不给没填有效期的资产报警（否则每条普通素材都要被盘问一遍）。

资产属于企业、跨项目复用，没有 project_id；随企业物理分库天然隔离。
既有的 project_facts / kb_docs 一条都不迁移，只在 certificates._collect()
里作为第四个来源并列读取。
"""
from __future__ import annotations

from datetime import date

from . import certificates, store

CATEGORIES = store.ASSET_CATEGORIES
LABELS = store.ASSET_LABELS
WARN_DAYS = certificates.WARN_DAYS


def validate(payload: dict) -> tuple[dict, str]:
    """新增 / 编辑共用：返回 (清洗后的字段, 错误文案)。"""
    title = str((payload or {}).get("title") or "").strip()
    if not title:
        return {}, "资产名称不能为空"
    if len(title) > 120:
        return {}, "资产名称最多 120 字"
    cat = str((payload or {}).get("category") or "qualification").strip()
    if cat not in CATEGORIES:
        return {}, "资产分类只能是资质 / 人员 / 业绩 / 检测 / 获奖"
    until = str((payload or {}).get("valid_until") or "").strip()[:10]
    if until and certificates.expiry_status(until) == "invalid":
        return {}, "有效期要写 YYYY-MM-DD，也可以留空"
    clean = {
        "category": cat, "title": title,
        "holder": str((payload or {}).get("holder") or "").strip()[:120],
        "code": str((payload or {}).get("code") or "").strip()[:120],
        "valid_until": until,
        "source_ref": str((payload or {}).get("source_ref") or "").strip()[:300],
        "note": str((payload or {}).get("note") or "").strip()[:500],
    }
    return clean, ""


def scan(*, today: date | None = None, warn_days: int = WARN_DAYS) -> dict:
    """只读体检：按五类列出资产，复用证照有效期的同一套五态判定。"""
    t = today or date.today()
    rows = store.list_assets()
    items: list[dict] = []
    for r in rows:
        until = str(r.get("valid_until") or "").strip()[:10]
        st = certificates.expiry_status(until, today=t, warn_days=warn_days)
        try:
            days = (date.fromisoformat(until) - t).days if until else None
        except ValueError:
            days = None
        items.append({
            "id": r.get("id"), "category": r.get("category"),
            "category_label": LABELS.get(r.get("category"), "未分类"),
            "title": r.get("title") or "", "holder": r.get("holder") or "",
            "code": r.get("code") or "", "valid_until": until,
            "source_ref": r.get("source_ref") or "", "note": r.get("note") or "",
            "verification": r.get("verification") or "unverified",
            "status": st, "label": certificates.STATUS_LABELS[st], "days": days,
        })
    order = {"expired": 0, "invalid": 1, "expiring": 2, "ok": 3, "unknown": 4}
    items.sort(key=lambda x: (order.get(x["status"], 9), x["category"], x["title"]))
    categories = {
        cat: {"label": LABELS[cat],
              "count": sum(1 for x in items if x["category"] == cat),
              "expired": sum(1 for x in items
                             if x["category"] == cat and x["status"] == "expired"),
              "expiring": sum(1 for x in items
                              if x["category"] == cat and x["status"] == "expiring")}
        for cat in CATEGORIES
    }
    counts = {k: sum(1 for x in items if x["status"] == k)
              for k in certificates.STATUS_LABELS}
    attention = [x for x in items if x["status"] in ("expired", "expiring", "invalid")]
    return {
        "items": items, "total": len(items), "categories": categories,
        "labels": dict(LABELS), "counts": counts, "warn_days": warn_days,
        "attention": attention,
        "summary": (f"已登记 {len(items)} 项企业资产（"
                    + "、".join(f"{LABELS[c]} {categories[c]['count']}" for c in CATEGORIES)
                    + f"）；过期 {counts['expired']} 项、{warn_days} 天内到期 "
                    + f"{counts['expiring']} 项、日期写错 {counts['invalid']} 项"
                    if items else "还没有登记企业资产"),
        "note": ("这里只登记「有什么证、有效期到哪天」，不自动判定能得多少分；"
                 "有效期由人工填写并对照原件核验，缺原件位置的请补齐。"),
    }


def hint(report: dict | None = None) -> str:
    """给「能不能交」用的一行提示：只报到期风险，不升级成禁止下载。"""
    r = report or scan()
    c = r.get("counts") or {}
    parts = ([f"{c['expired']} 项已过期"] if c.get("expired") else []) \
        + ([f"{c['expiring']} 项 {r.get('warn_days', WARN_DAYS)} 天内到期"]
           if c.get("expiring") else []) \
        + ([f"{c['invalid']} 项日期写错"] if c.get("invalid") else [])
    return ("企业资产：" + "、".join(parts) + "，请在投标前更新") if parts else ""
