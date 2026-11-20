"""招标解读汇总：把散在各处的结果聚成一张可核对的清单。

只读、不调模型、不写库。所有数字都来自已有的确定性抽取；
没有的如实说「未识别」，不补示例值，也不宣称「已经核对无误」。
前端默认收起、展开时才调用，避免真实招标文件拖慢资料页首屏。
"""
from __future__ import annotations

import re

from . import certificates, goods_list, quote_gate, reqtable, store, tender_dims

MAX_ITEMS = 6
KIND_LABELS = {"rejection": "废标条款", "hard": "硬性要求", "scoring": "评分项"}


def _text(value, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def scan(pid: str) -> dict:
    p = store.get_project(pid) or {}
    tender = str(p.get("tender_text") or "")
    chars = len(re.sub(r"\s+", "", tender))
    errors: list[str] = []

    # 要求表：人工修订优先，表为空时回落到基座自动抽取。
    try:
        summary = reqtable.summary(pid)
        effective = reqtable.effective(pid)
    except Exception as exc:
        errors.append(f"要求表没读出来（{type(exc).__name__}）")
        summary, effective = {"total": 0, "by_kind": {}}, {}
    counts = {k: int((summary.get("by_kind") or {}).get(k, {}).get("effective") or 0)
              for k in KIND_LABELS}
    samples = {
        kind: [_text(r.get("text")) for r in (effective.get(kind) or [])
               if _text(r.get("text"))][:MAX_ITEMS]
        for kind in KIND_LABELS
    }
    try:
        all_scoring = reqtable.scoring_rows(pid)
        scoring = all_scoring[:MAX_ITEMS]
    except Exception:
        all_scoring, scoring = [], []

    try:
        goods = goods_list.scan(pid)
    except Exception as exc:
        errors.append(f"货物清单没读出来（{type(exc).__name__}）")
        goods = {"found": False, "total": 0, "missing_qty_count": 0,
                 "cap": {"status": "absent", "note": "未读到"}, "summary": ""}
    try:
        quote = quote_gate.scan(pid)
    except Exception as exc:
        errors.append(f"报价核对没读出来（{type(exc).__name__}）")
        quote = {"quote_found": False, "cap_verdict": "unclear",
                 "cross_verdict": "no_quote", "summary": "未校验"}
    try:
        cert = certificates.scan(pid)
    except Exception as exc:
        errors.append(f"证照状态没读出来（{type(exc).__name__}）")
        cert = {"counts": {}, "total": 0, "items": []}
    cc = cert.get("counts") or {}

    try:
        pending = [d for d in store.list_docs(project_id=pid)
                   if d.get("needs_review") and not d.get("confirmed_at")]
    except Exception:
        pending = []
    # P4：招标要素维度与 ★▲ 参数；与货物清单分开列，全部只切原文、不补值
    try:
        td = tender_dims.scan(pid)
    except Exception as exc:
        errors.append(f"招标要素维度没读出来（{type(exc).__name__}）")
        td = {"dimensions": [], "total": 0, "found": 0, "missing": 0,
              "star_params": {"total": 0, "items": [], "truncated": False,
                              "note": ""},
              "chapter_focus": [], "note": "", "summary": "未识别"}
    star = td.get("star_params") or {}

    cap = goods.get("cap") or {}
    lines = [
        f"招标原文 {chars:,} 字" if chars else "还没有导入招标文件正文",
        (f"抽出 {summary.get('total', 0)} 条要求：废标 {counts['rejection']}、"
         f"硬性 {counts['hard']}、评分 {counts['scoring']}"),
        (f"货物清单 {goods.get('total', 0)} 行"
         + (f"，其中 {goods.get('missing_qty_count')} 行没读到数量"
            if goods.get("missing_qty_count") else "")
         if goods.get("found") else "未识别到货物清单"),
        f"最高限价：{cap.get('note') or cap.get('status') or '未读到'}",
        f"报价与总价：{quote.get('summary') or '未校验'}",
        (f"已登记有效期的证照/事实 {cert.get('total', 0)} 项："
         f"过期 {cc.get('expired', 0)}、临期 {cc.get('expiring', 0)}、"
         f"日期写错 {cc.get('invalid', 0)}"),
        (f"招标要素 {td.get('found', 0)}/{td.get('total', 0)} 项已识别；"
         f"★▲ 强制参数 {star.get('total', 0)} 条（与货物清单分开列）"),
    ]
    if pending:
        lines.append(f"还有 {len(pending)} 份资料带内嵌图片待人工核对，确认前不进依据池")
    if errors:
        lines.append("部分汇总没跑完：" + "；".join(errors))

    return {
        "project": p.get("name") or "",
        "tender_chars": chars,
        "requirements": {"total": summary.get("total", 0), "counts": counts,
                         "samples": samples},
        "scoring": {"total": len(all_scoring), "rows": scoring},
        "goods": {"found": bool(goods.get("found")), "total": goods.get("total", 0),
                  "missing_qty_count": goods.get("missing_qty_count", 0),
                  "cap_status": cap.get("status"), "cap_note": cap.get("note", "")},
        "quote": {"quote_found": bool(quote.get("quote_found")),
                  "total": quote.get("total"), "cap_verdict": quote.get("cap_verdict"),
                  "cross_verdict": quote.get("cross_verdict"),
                  "summary": quote.get("summary", "")},
        "certificates": {"total": cert.get("total", 0),
                         "expired": cc.get("expired", 0),
                         "expiring": cc.get("expiring", 0),
                         "invalid": cc.get("invalid", 0),
                         "items": [{"title": _text(x.get("title"), 60),
                                    "label": x.get("label"),
                                    "valid_until": x.get("valid_until")}
                                   for x in (cert.get("items") or [])[:MAX_ITEMS]]},
        "pending_docs": len(pending),
        "dimensions": {"total": td.get("total", 0), "found": td.get("found", 0),
                       "missing": td.get("missing", 0),
                       "rows": td.get("dimensions", [])},
        "star_params": star,
        "chapter_focus": td.get("chapter_focus", []),
        "dimensions_note": td.get("note", ""),
        "lines": lines,
        "summary": "；".join(lines[:4]) + "。",
        "note": ("以上是按规则抽出的待人工核对结果，不是已确认事实；"
                 "评分细则、限价口径与强制参数仍须对照招标原件逐项确认。"),
        "errors": errors,
    }
