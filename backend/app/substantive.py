"""逐条实质响应 / 评分项得分点覆盖（自动启发式，不是专家判定）。

词面覆盖只回答「关键词有没有出现」，太容易靠抄一句要求蒙混过去。
这里多加两把尺子：
1. 命中句里必须有明确响应表述（提供 / 满足 / 不低于 / 承诺 / 提交 …）；
2. 要求里带数值的（90 天、400 万像素、30 分钟），该数值必须逐字出现在命中句。
再单独核对评分办法里的每个评分项有没有对应正文。

**仍然是自动启发式**：不做语义理解、不判断方案优劣、不预测得分。
它能把「完全没提」「缺响应表述」「要求数值没写」筛出来，但**逐字照抄要求
（连响应词一起抄）仍可能被判为已响应**，这是已记录的未解决局限，不能替代专家复核。
"""
from __future__ import annotations

import re
from typing import Any

from . import provenance, rules

RESPONSE_WORDS = (
    "提供", "满足", "不低于", "不少于", "不超过", "不得少于", "不得超过",
    "不高于", "承诺", "响应", "配置", "支持", "符合", "加盖", "提交",
    "达到", "具备", "采用", "保证", "安排", "执行", "覆盖", "实现",
    "随附", "附有", "建立", "设置", "配备", "出具", "落实", "遵照", "接受",
)
NUM_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:天|日|年|月|小时|分钟|万|元|%|％|像素|台|套|"
    r"个|项|次|人|分|米|公里|GB|TB|路|倍)")
HIT = 0.45
TARGET = 0.60


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", str(text or "")).lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sentences(sections: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for sec in sections or []:
        title = str(sec.get("title") or "")
        text = provenance.clean_for_export(str(sec.get("text") or ""))
        for s in re.split(r"[。；;！!\n]", text):
            s = s.strip()
            if len(s) >= 6:
                out.append((title, s))
    return out[:600]


def responded_requirements(tender_text: str, sections: list[dict],
                           reqs: list[str] | None = None) -> dict:
    # 传入 reqs 时以「招标要求表」的人工修订版为准；不传才回落到自动抽取
    req_list = [str(x).strip() for x in (reqs or []) if str(x).strip()]
    if not req_list:
        req_list = rules.extract_requirements(tender_text)
    sents = _sentences(sections)
    items: list[dict] = []
    for req in req_list:
        rg = _grams(req)
        if not rg:
            continue
        best_s, best_ov, best_sec = "", 0.0, ""
        for title, s in sents:
            ov = len(rg & _grams(s)) / len(rg)
            if ov > best_ov:
                best_s, best_ov, best_sec = s, ov, title
        flat = best_s.replace(" ", "")
        word_hit = best_ov >= HIT
        verb_hit = any(w in best_s for w in RESPONSE_WORDS)
        nums = [m.group(0).replace(" ", "") for m in NUM_RE.finditer(req)]
        miss_nums = [n for n in nums if n not in flat]
        reasons: list[str] = []
        if not word_hit:
            reasons.append("正文未命中该要求关键词")
        if not verb_hit:
            reasons.append("命中句缺少明确响应表述")
        if miss_nums:
            reasons.append("要求数值未逐字出现：" + "、".join(miss_nums[:2]))
        items.append({
            "req": req[:90], "section": best_sec,
            "overlap": round(best_ov, 2),
            "responded": bool(word_hit and verb_hit and not miss_nums),
            "reasons": reasons,
            "evidence": best_s[:80],
        })
    total = len(items)
    responded = sum(1 for x in items if x["responded"])
    ratio = 1.0 if total == 0 else round(responded / total, 4)
    return {"total": total, "responded": responded, "ratio": ratio,
            "target": TARGET, "hit": HIT,
            "items": items[:12],
            "unresponded": [x["req"] for x in items if not x["responded"]][:6]}


def scoring_coverage(tender_text: str, sections: list[dict],
                     rows: list[dict] | None = None) -> dict:
    """评分办法里的每个得分点，正文有没有对应内容（只查词面，不估分）。"""
    if rows:
        parsed = [{"item": str(r.get("item") or ""),
                   "score": str(r.get("score") or "")} for r in rows
                  if isinstance(r, dict) and str(r.get("item") or "").strip()]
    else:
        try:
            from .basecore.services import evaluate
            parsed = evaluate.parse_scoring(tender_text)
        except Exception:
            parsed = []
    # parse_scoring 会把「响应时间不超过 30 分钟」这类要求句误认成评分项
    rows = [r for r in parsed if not any(
        w in str(r.get("item") or "")
        for w in ("须", "应", "不得", "分钟", "小时", "天", "不低于", "不少于"))]
    if not rows:
        return {"available": False, "total": 0, "covered": 0,
                "ratio": None, "items": [], "missing": []}
    sents = [s for _, s in _sentences(sections)]
    items = []
    for row in rows:
        name = str(row.get("item") or "")
        ng = _grams(name)
        hit = bool(ng) and any(
            len(ng & _grams(s)) / len(ng) >= HIT for s in sents)
        items.append({"item": name[:60], "score": str(row.get("score") or ""),
                      "covered": bool(hit)})
    covered = sum(1 for x in items if x["covered"])
    return {"available": True, "total": len(items), "covered": covered,
            "ratio": round(covered / max(1, len(items)), 4),
            "items": items[:12],
            "missing": [x["item"] for x in items if not x["covered"]][:6]}


def summarize(tender_text: str = "",
              sections: list[dict] | None = None,
              reqs: list[str] | None = None,
              scoring_rows: list[dict] | None = None) -> dict:
    r = responded_requirements(tender_text, list(sections or []), reqs)
    s = scoring_coverage(tender_text, list(sections or []), scoring_rows)
    req_ok = r["total"] == 0 or r["ratio"] >= TARGET
    score_ok = (not s["available"]) or (s["ratio"] or 0) >= TARGET
    return {
        "requirements": r, "scoring": s,
        "all_passed": bool(req_ok and score_ok),
        "scope": "逐条实质响应（词面 + 响应表述 + 数值）与评分项覆盖",
        "claim": "自动启发式，不等于专家对实质响应的判定；不评文采、方案优劣与得分",
    }
