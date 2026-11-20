"""B03 保护表单、报价、资质与签章的完成核验。

保护只限制 AI 改写，**不跳过**必填字段、占位符、有效期、总价关系、签章与附件核验。
短而完整的表单（投标函、授权书）不套普通正文的字数下限；签章无法自动确认时
必须留人工复核记录（责任人 / 时间 / 内容版本），否则不能算完成。
"""
from __future__ import annotations

import hashlib
import re

from . import facts, rules, store

PLACEHOLDERS = ("XXX", "xxx", "TODO", "待补充", "待填写", "【需补充", "TBD")

FORM_RULES = [
    {"key": "bid_letter", "match": ("投标函", "投标书", "投标承诺书"),
     "fields": ("投标人", "项目名称", "投标总价", "工期", "法定代表人"),
     "signature": True, "attachments": ()},
    {"key": "quote", "match": ("报价", "开标一览", "工程量清单", "分项报价"),
     "fields": ("总价", "大写"), "signature": True, "attachments": ("报价明细",),
     "quote": True},
    {"key": "authorization", "match": ("授权", "委托"),
     "fields": ("授权人", "被授权人", "有效期"), "signature": True,
     "attachments": ("授权书原件",)},
    {"key": "commitment", "match": ("承诺",),
     "fields": ("承诺", "投标人"), "signature": True, "attachments": ()},
    {"key": "qualification", "match": ("资质", "资格", "证书"),
     "fields": ("证书编号", "有效期"), "signature": False,
     "attachments": ("资质证书",)},
    {"key": "deposit", "match": ("保证金",),
     "fields": ("金额", "缴纳方式"), "signature": False,
     "attachments": ("缴纳凭证",)},
]


def rule_for(title: str) -> dict | None:
    for r in FORM_RULES:
        if any(k in str(title or "") for k in r["match"]):
            return r
    return None


def _quote_rows(text: str) -> list[dict]:
    """粘贴 / Word 表格的「|」分隔行 → 校验器输入；识别不了就如实报未核对。"""
    out = []
    for line in str(text or "").splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 3:
            continue
        # 第一列是名称，后面依次是数量 / 单价 / 金额；名称列不能当数字列
        nums = []
        for c in cells[1:]:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)", c.replace(",", ""))
            nums.append(m.group(1) if m else None)
        if len(nums) >= 2 and nums[0] and nums[1]:
            out.append({"name": cells[0][:40], "qty": nums[0],
                        "unit_price": nums[1],
                        "amount": nums[2] if len(nums) > 2 and nums[2] else None})
    return out


def quote_report(text: str) -> dict:
    """给接口 / 前端用的报价核算结果：行数、逐行校验、合计、舍入规则。"""
    rows = _quote_rows(text)
    if not rows:
        return {"recognized": False, "lines": [], "total": "0.00", "ok": False,
                "note": "未识别到「名称 | 数量 | 单价 | 金额」表格行，需人工核对"}
    q = facts.quote_check(rows)
    return {**q, "recognized": True, "rows": len(rows)}


def check_section(pid: str, sec: dict, *, confirmations: list[dict] | None = None,
                  attachments: list[dict] | None = None) -> dict:
    """单个保护章节的完成核验；返回 ok / 缺口 / 待人工复核项与确认入口。"""
    title = str(sec.get("title") or "")
    sid = str(sec.get("id") or "")
    text = str(sec.get("text") or "")
    r = rule_for(title)
    if not r:
        return {"section_id": sid, "title": title, "ok": bool(text.strip()),
                "gaps": [] if text.strip() else ["保护章节还是空的，需要人工填写"],
                "pending": [], "pending_targets": [], "short_form": False}
    if not text.strip():
        return {"section_id": sid, "title": title, "ok": False,
                "gaps": ["保护章节还是空的，需要人工填写"], "pending": [],
                "pending_targets": [], "short_form": False, "rule": r["key"]}
    content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]
    confs = confirmations if confirmations is not None else store.list_confirmations(pid)
    atts = attachments if attachments is not None else store.list_attachments(pid)
    gaps: list[str] = []
    pending: list[str] = []
    targets: list[str] = []

    def _manual(key: str, label: str) -> None:
        """人工确认与正文版本绑定：改过正文，旧确认失效。"""
        rows = [c for c in confs if c.get("target") == key]
        if not rows:
            pending.append(label)
            targets.append(key)
        elif rows[0].get("content_hash") != content_hash:
            gaps.append("人工确认绑定的正文版本已变化，需重新确认")

    for f in r["fields"]:
        if f not in text:
            gaps.append(f"缺必填字段「{f}」")
    for ph in PLACEHOLDERS:
        if ph in text:
            gaps.append(f"仍有占位符「{ph}」")
    # 短而完整的表单：字段齐、无占位符时不套普通正文 60 字下限
    short_form = bool(not gaps and len(text.strip()) < 60)
    if r["key"] == "qualification":
        _manual("validity:" + sid, "资质有效期需对照证书原件核验")
    if r.get("quote"):
        q = quote_report(text)
        if q["recognized"]:
            if not q["ok"]:
                gaps.append("报价行列小计与单价×数量不一致（Decimal 核算）")
            elif "总价" in text and q["total"] not in text.replace(",", ""):
                gaps.append(f"正文总价与明细合计 {q['total']} 不一致")
        else:
            _manual("quote:" + sid, "报价表行列结构未识别，需人工核对单价×数量与合计")
    if r["signature"]:
        _manual("signature:" + sid, "签章无法自动确认，需人工复核")
    for need in r["attachments"]:
        hit = [a for a in atts if need in (str(a.get("note") or "") + str(a.get("filename") or ""))
               or str(a.get("section_id") or "") == sid]
        if not hit:
            gaps.append(f"缺证明附件「{need}」")
    return {"section_id": sid, "title": title, "rule": r["key"],
            "ok": not gaps and not pending, "gaps": gaps, "pending": pending,
            "pending_targets": targets, "short_form": short_form,
            "content_hash": content_hash,
            "signature_confirmed": not any(t.startswith("signature:") for t in targets)}


def check(pid: str, sections: list[dict] | None = None) -> dict:
    secs = sections if sections is not None else store.list_sections(pid)
    confs = store.list_confirmations(pid)
    atts = store.list_attachments(pid)
    items = [check_section(pid, s, confirmations=confs, attachments=atts)
             for s in secs if s.get("protected") or rules.is_protected(s.get("title") or "")]
    return {"items": items, "total": len(items),
            "incomplete": [x for x in items if not x["ok"]],
            "note": "保护章节不参与 AI 改写，但字段、占位符、报价、签章与附件照常核验"}


def blocking(pid: str) -> list[dict]:
    """导出检查：字段/占位符/报价/附件缺口列为重点提醒；只能人工判定的项列待复核。

    签章、资质有效期、无法自动识别的报价行列不能由机器判「已完成」，
    但也不该伪装成机器已确认——单列为待人工复核，界面给确认入口。
    """
    rep = check(pid)
    out = []
    for x in rep["incomplete"]:
        gaps = x.get("gaps") or []
        pending = x.get("pending") or []
        out.append({
            "key": "form-" + str(x["section_id"]), "level": "高",
            "blocking": bool(gaps),
            "title": ("保护表单有缺口 · " if gaps else "保护表单待人工复核 · ") + x["title"],
            "detail": "；".join(gaps + pending)
                      + "。请补齐字段与附件，或在「保护表单核验」里做人工确认"
                        "（记录责任人、时间与内容版本）。",
            "section_id": x["section_id"],
            "action": "去核验保护表单",
            "gaps": gaps, "pending": pending,
            "pending_targets": x.get("pending_targets") or [],
        })
    return out


def confirm(pid: str, section_id: str, target: str, reviewer: str = "人工复核",
            note: str = "") -> dict:
    """人工确认签章 / 有效期 / 报价结构：绑定正文内容版本，正文一改就失效。"""
    sec = store.get_section(section_id)
    if not sec or sec.get("project_id") != pid:
        return {"ok": False, "error": "章节不存在或不属于本项目"}
    text = str(sec.get("text") or "")
    content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]
    row = store.add_confirmation(
        pid, target, section_id=section_id, note=note[:300],
        content_hash=content_hash, reviewer=reviewer or "人工复核")
    return {"ok": True, "content_hash": content_hash, "reviewer": row["reviewer"],
            "reviewed_at": row["reviewed_at"],
            "note": "确认仅对当前正文版本有效；正文改动后需要重新确认"}
