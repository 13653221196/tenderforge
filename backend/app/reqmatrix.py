"""B01 逐条响应矩阵：要求 → 响应章节/段落 → 事实/附件 → 检查 → 人工复核。

设计底线：
- 60% 之类的比例只用于「起草进度」展示，不能当作最终达标；
- 关键词命中、甚至逐字照抄要求，都只算「候选响应」，不等于证据充分；
- 证据充分与人工复核是两条独立记录；必需项必须逐条落实或明确标「不适用」并写理由；
- 撤销 / 失效 / 停用的要求不参与当前门禁。
"""
from __future__ import annotations

import re

from . import provenance, reqtable, requirement_support, store, substantive

ACTIVE = ("active", "na")


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", str(text or "")).lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sentences(sections: list[dict]) -> list[dict]:
    out = []
    for sec in sections or []:
        text = provenance.clean_for_export(str(sec.get("text") or ""))
        for s in re.split(r"[。；;！!\n]", text):
            s = s.strip()
            if len(s) >= 6:
                out.append({"section_id": sec.get("id") or "",
                            "title": str(sec.get("title") or ""),
                            "text": s, "anchor": s[:40]})
    return out[:3000]     # 只限制单章超长正文的暴力规模，不限制要求条数


def _match(req: dict, sents: list[dict]) -> dict:
    """找最像的一条响应句；顺便识别「把要求抄一遍」这种假响应。"""
    rg = _grams(req["text"])
    if not rg:
        return {"section_id": "", "anchor": "", "evidence": "",
                "candidate": False, "copy_only": False, "verb": False,
                "missing_numbers": []}
    best, best_ov = None, 0.0
    for s in sents:
        ov = len(rg & _grams(s["text"])) / len(rg)
        if ov > best_ov:
            best, best_ov = s, ov
    if not best or best_ov < substantive.HIT:
        return {"section_id": "", "anchor": "", "evidence": "",
                "candidate": False, "copy_only": False, "verb": False,
                "missing_numbers": []}
    flat = best["text"].replace(" ", "")
    verb = any(w in best["text"] for w in substantive.RESPONSE_WORDS)
    nums = [m.group(0).replace(" ", "")
            for m in substantive.NUM_RE.finditer(req["text"])]
    missing = [n for n in nums if n not in flat]
    # 句子几乎就是要求原文（去掉要求字面后没剩几个字）＝照抄，不给证据充分
    remainder = re.sub(r"\W", "", req["text"])
    sent_flat = re.sub(r"\W", "", best["text"])
    for token in (remainder[:24], remainder[-24:]):
        if token:
            sent_flat = sent_flat.replace(token, "")
    copy_only = len(sent_flat) < max(8, len(remainder) * 0.15)
    return {"section_id": best["section_id"], "anchor": best["anchor"],
            "evidence": best["text"][:600], "candidate": True,
            "copy_only": copy_only, "verb": verb, "missing_numbers": missing}


def set_na(pid: str, req_id: str, reason: str, reviewer: str = "人工复核") -> dict:
    """标不适用：必须有理由 + 责任人 + 时间 + 内容版本，否则拒绝。"""
    if not str(reason or "").strip():
        return {"ok": False, "error": "标「不适用」必须写明理由"}
    if not str(reviewer or "").strip():
        return {"ok": False, "error": "必须指定复核责任人"}
    row = store.update_requirement(pid, req_id, {"status": "na", "note": reason[:300],
                                                "confirmed_by": reviewer,
                                                "confirmed_at": store.now()})
    if not row:
        return {"ok": False, "error": "要求不存在"}
    store.save_req_response(pid, req_id, status="na", evidence=reason[:600],
                            reviewer=reviewer, reviewed_at=store.now())
    req = requirement_support.requirement(pid,req_id)
    stamp = requirement_support.evaluate(pid,req,store.get_req_response(pid,req_id))["fingerprint"]
    store.save_req_response(pid,req_id,fingerprint=stamp)
    store.add_confirmation(pid, "requirement:na", note=f"{req_id}: {reason[:200]}",
                           reviewer=reviewer)
    return {"ok": True, "status": "na"}


def confirm(pid: str, req_id: str, reviewer: str = "人工复核",
            note: str = "") -> dict:
    r = store.confirm_response(pid, req_id, reviewer=reviewer, note=note)
    if isinstance(r, dict) and r.get("ok") is False:
        return r
    if not r:
        return {"ok": False, "error": "没有这条要求的响应记录，请先跑一次体检"}
    return {"ok": True, "status": "confirmed"}


def build(pid: str) -> dict:
    """重算并落库整个矩阵；返回完整清单 + 起草进度 + 未完成必需项。"""
    items = [x for x in reqtable.constraint_items(pid) if x["status"] in ACTIVE]
    sents = _sentences(store.list_sections(pid))
    out = []
    for it in items:
        old = store.get_req_response(pid, it["id"]) or {}
        m = _match(it, sents)
        if old.get("status") in ("na","review") and it["status"] == "na":
            m.update(section_id=old.get("section_id",""),anchor=old.get("anchor",""),
                     evidence=old.get("evidence",""))
        support = requirement_support.evaluate(pid,it,m)
        evidence_ok = bool(m["candidate"] and m["verb"] and not m["copy_only"]
                           and not m["missing_numbers"] and support["ok"])
        unchanged = bool(old.get("fingerprint") and old["fingerprint"] == support["fingerprint"])
        if old.get("status") == "confirmed" and unchanged and evidence_ok:
            status = "confirmed"
        elif old.get("status") == "na" and unchanged and old.get("reviewer") and (old.get("evidence") or "").strip():
            status = "na"
        elif old.get("status") in ("review","confirmed","na"):
            # B04：招标文件出新版，旧确认降级；即便词面仍命中也要重新人工确认
            status = "review"
        elif evidence_ok:
            status = "evidence_ready"
        elif m["candidate"]:
            status = "candidate"
        else:
            status = "missing"
        store.save_req_response(
            pid, it["id"], section_id=m["section_id"], anchor=m["anchor"],
            evidence=m["evidence"], candidate=m["candidate"],
            evidence_ok=evidence_ok, status=status,
            reviewer=old.get("reviewer") or "", reviewed_at=old.get("reviewed_at") or "")
        out.append({**it, "section_id": m["section_id"], "anchor": m["anchor"],
                    "evidence": m["evidence"], "candidate": m["candidate"],
                    "copy_only": m["copy_only"], "verb": m["verb"],
                    "missing_numbers": m["missing_numbers"],
                    "evidence_ok": evidence_ok, "status": status,
                    "reviewer": old.get("reviewer") or "",
                    "reviewed_at": old.get("reviewed_at") or "",
                    "fingerprint": support["fingerprint"],
                    "support_links": support["links"], "support_policy": support["policy"],
                    "support_gaps": support["gaps"], "attachments": support["attachments"]})
    required = [x for x in out if x["required"]]
    done = [x for x in required if x["status"] == "confirmed"]
    na = [x for x in required if x["status"] == "na"]
    draft_progress = round(len(done) / len(required), 4) if required else 1.0
    pending = [x for x in required if x["status"] not in ("confirmed", "na")]
    # 已导入招标正文却一条要求都没抽到，通常是抽取失败而不是「全部已响应」；
    # 不能因为 pending 为空就判可交付，否则零要求会一路绿灯走到导出。
    p = store.get_project(pid) or {}
    empty_but_tender = bool(str(p.get("tender_text") or "").strip()) and not out
    return {
        "items": out, "total": len(out), "required": len(required),
        "confirmed": len(done), "na": len(na), "pending": len(pending),
        # 仅起草进度：60% 之类的阈值只用于展示，最终达标看 pending 是否为 0
        "draft_progress": draft_progress,
        "empty_requirements": empty_but_tender,
        "deliverable": (not pending) and not empty_but_tender,
        "pending_items": pending,
        "scope_note": "关键词命中 / 照抄要求只算候选响应；证据充分与人工复核分别记录",
    }


def blocking(pid: str) -> list[dict]:
    """导出门禁：每一条未落实的必需项都给一个补齐入口。"""
    rep = build(pid)
    out = []
    for x in rep["pending_items"]:
        hint = ("正文里没有命中这条要求" if x["status"] == "missing" else
                "招标文件已出新版，原来的逐条确认已失效，需重新复核" if x["status"] == "review" else
                "命中句可能只是照抄要求" if x["copy_only"] else
                "缺明确响应表述或要求数值" if x["status"] == "candidate" else
                "自动证据已齐，还差人工复核")
        out.append({
            "key": "req-" + str(x["id"]), "level": "高", "blocking": True,
            "title": "必需要求未逐条落实 · " + x["text"][:48],
            "detail": f"{hint}；位置：{x['loc'] or '未标注原文位置'}；"
                      f"来源版本：{x['source_version'] or '未登记'}；"
                      f"当前状态：{x['status']}。"
                      + (f"命中章节：{x['section_id']}；" if x["section_id"] else "")
                      + "请补写正文、补证据/附件，或在要求表里人工确认 / 标不适用并写理由。",
            "action": "去逐条响应矩阵补齐", "req_id": x["id"],
            "section_id": x["section_id"], "status": x["status"],
        })
    return out
