"""B02 企业事实、数值与原件附件。

核心区分：
- example（历史标书 / 表达范例）不能证明当前企业的业绩或数值；
- enterprise_fact 是当前企业事实，需带实体、单位、有效期、来源位置与人工核验状态；
- project_commitment 是本项目拟承诺值，必须由责任人确认，不伪装成历史事实；
- attachment 是实际提交的原件，须可打开并回跳。

数值核对不看「数字有没有在资料里出现过」，而是核对实体、单位、含义与计算关系；
金额一律用 Decimal + 显式 ROUND_HALF_UP 舍入。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from . import store

NUM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(万元|亿元|万|元|%|％|天|日|年|月|小时|分钟|台|套|个|项|次|人|分|米|公里|GB|TB|路|倍)")
UNIT_SCALE = {"元": Decimal("1"), "万": Decimal("10000"),
              "万元": Decimal("10000"), "亿元": Decimal("100000000")}
CENT = Decimal("0.01")


def parse_token(token: str) -> tuple[Decimal | None, str, str]:
    m = NUM_RE.search(str(token or ""))
    if not m:
        return None, "", ""
    try:
        value = Decimal(m.group(1))
    except InvalidOperation:
        return None, "", ""
    unit = m.group(2)
    category = ("金额" if unit in UNIT_SCALE else
                "比例" if unit in ("%", "％") else
                "期限" if unit in ("天", "日", "年", "月", "小时", "分钟") else "数量")
    return value, unit, category


def to_base(value: Decimal, unit: str) -> Decimal:
    """金额统一换算到元；非金额原样返回。"""
    return (value * UNIT_SCALE[unit]) if unit in UNIT_SCALE else value


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def quote_check(lines: list[dict]) -> dict:
    """报价：逐行 数量×单价 → 小计，合计用 Decimal，舍入规则写明。"""
    out, total = [], Decimal("0")
    for row in lines or []:
        try:
            qty = Decimal(str(row.get("qty") or "0"))
            price = Decimal(str(row.get("unit_price") or "0"))
        except (InvalidOperation, ValueError):
            out.append({"name": row.get("name"), "ok": False, "error": "数量或单价不是数字"})
            continue
        amount = money(qty * price)
        total += amount
        stated = row.get("amount")
        stated_ok = True
        if stated not in (None, ""):
            try:
                stated_ok = money(Decimal(str(stated))) == amount
            except (InvalidOperation, ValueError):
                stated_ok = False
        out.append({"name": row.get("name"), "qty": str(qty), "unit_price": str(price),
                    "amount": str(amount), "ok": stated_ok,
                    "rounding": "ROUND_HALF_UP / 2 位小数"})
    return {"ok": all(x.get("ok") for x in out), "lines": out,
            "total": str(money(total)), "rounding": "ROUND_HALF_UP / 2 位小数",
            "note": "计算关系可作为依据，仍须与原件附件和责任人确认对应"}


def _expired(row: dict) -> bool:
    until = str(row.get("valid_until") or "").strip()
    return bool(until) and until < date.today().isoformat()


def _verified(row: dict) -> bool:
    return str(row.get("verification") or "").startswith("verified")


def _computed(value: Decimal, unit: str, evidence: str) -> bool:
    """合法计算值：资料里存在带同一金额单位的数值，与数量相乘/相加等于正文数值。

    不能用「裸数字凑巧等于」放行：别的项目的 2 元 × 3 元 不能证明本项目 6 万元。
    换算统一放在最后一步，避免把「万元」先放大成一万元再相乘。
    """
    amt: list[Decimal] = []
    other: list[Decimal] = []
    for m in NUM_RE.finditer(evidence or ""):
        try:
            v = Decimal(m.group(1))
        except InvalidOperation:
            continue
        (amt if m.group(2) in UNIT_SCALE and m.group(2) == unit else other).append(v)
    if not amt:
        return False
    pool = (other + amt)[:40]
    target = value
    for a in amt[:40]:
        for b in pool:
            if money(a * b) == money(target) or money(a + b) == money(target):
                return True
    return False


def verify_token(token: str, evidence_text: str, facts: list[dict],
                 *, entity: str = "") -> dict:
    """单个数值的实体 / 单位 / 含义核对；返回 {ok, by, reason}。"""
    value, unit, category = parse_token(token)
    if value is None:
        return {"ok": True, "by": "非数值", "reason": ""}
    usable = [f for f in facts if str(f.get("kind")) != "example"
              and _verified(f) and not _expired(f)]
    for f in usable:
        fv, fu, fc = parse_token(f"{f.get('value') or ''}{f.get('unit') or ''}")
        if fv is None or fc != category:
            continue
        if to_base(fv, fu) != to_base(value, unit):
            continue
        if entity and f.get("entity") and entity not in str(f.get("entity")):
            continue
        return {"ok": True, "by": f"已核验事实·{f.get('kind')}·{f.get('entity') or '未标实体'}",
                "reason": ""}
    # 只有整个「数值+单位」在原文出现才算有据；裸数字子串不算
    compact = re.sub(r"\s+", "", str(evidence_text or ""))
    if re.sub(r"\s+", "", str(token)) in compact:
        return {"ok": True, "by": "原文逐字（含单位）", "reason": ""}
    if category == "金额" and _computed(value, unit, evidence_text or ""):
        return {"ok": True, "by": "合法计算值（Decimal 核算）", "reason": ""}
    reason = ("资料里只有相同数字、单位或实体对不上" if category == "金额"
              else "未找到带同一单位的原文或已核验事实")
    return {"ok": False, "by": "", "reason": reason}


def verify_sections(pid: str, sections: list[dict], sources: list[dict]) -> dict:
    """逐章数值核对：范例不能作证，拟承诺值列待补任务。"""
    facts = store.list_facts(pid)
    evidence = "\n".join(str(s.get("snippet") or s.get("content") or "")
                         for s in (sources or []))
    bad: list[dict] = []
    for sec in sections or []:
        text = str(sec.get("text") or "")
        for m in NUM_RE.finditer(text):
            token = m.group(0)
            r = verify_token(token, evidence, facts)
            if not r["ok"]:
                bad.append({"section": sec.get("title") or "",
                            "section_id": sec.get("id") or "",
                            "token": token, "reason": r["reason"]})
    return {"ok": not bad, "bad": bad, "facts": len(facts),
            "examples_ignored": sum(1 for f in facts if f.get("kind") == "example"),
            "note": "缺事实的项生成待补任务；拟承诺值须责任人确认，不写成历史事实"}


def gaps(pid: str, sections: list[dict], sources: list[dict]) -> list[dict]:
    """缺事实 → 待补任务；附上应补的实体 / 单位 / 来源位置与附件入口。"""
    rep = verify_sections(pid, sections, sources)
    out = []
    for b in rep["bad"]:
        value, unit, category = parse_token(b["token"])
        out.append({
            "key": f"fact-{b['section_id']}-{b['token']}",
            "title": "缺可核验事实 · " + b["token"],
            "detail": f"{b['section']}：{b['reason']}。请补企业事实（实体 / 字段 / 单位 / 有效期 / 来源位置）"
                      "或项目拟承诺值（由责任人确认），并绑定原件附件。",
            "entity": "", "unit": unit, "category": category,
            "section_id": b["section_id"], "action": "补事实 / 上传原件",
        })
    return out


def attachment_index(pid: str) -> list[dict]:
    """附件 → 事实 → 资料/页码的完整回跳关系。"""
    facts = {f["id"]: f for f in store.list_facts(pid)}
    docs = {d["id"]: d for d in store.list_docs(project_id=pid)}
    out = []
    for a in store.list_attachments(pid):
        f = facts.get(a.get("fact_id") or "", {})
        d = docs.get(f.get("source_doc_id") or a.get("doc_id") or "", {})
        out.append({**a, "fact": f.get("field") or "", "entity": f.get("entity") or "",
                    "source_title": d.get("title") or "",
                    "source_loc": f.get("source_loc") or "",
                    "page": a.get("page") or 0,
                    "traceable": bool(d or f or a.get("path"))})
    return out
