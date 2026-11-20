"""报价一致性总闸：最高限价算术比对 + 多来源总价交叉核对。

只做确定性计算，不调模型、不猜金额：
- 招标文件明确写了最高限价金额，且能算出投标总价 → 直接比大小（Q10）
- 标书正文里出现多张报价表、投标函 / 开标一览表 / 大写金额等
  多个总价声明 → 交叉比对，彼此必须一致（Q09）
- 任何一边缺输入、限价含糊、没读到报价表 → 如实说「未校验」

口径边界：结论只是算术事实，不代替评审口径判断含税价、单价限价、
角分剔除、分包限价或万元报价单位；这些仍需对照前附表人工确认。
"""
from __future__ import annotations

import re

from . import goods_list, store
from .basecore.services import quote as quote_svc
from .basecore.services.checker import cn_amount

# 总价声明的标签。必须是「总价」级别，明细行里的「小计」不算。
# 必须用非捕获组包住整个 alternation：否则和后面的字符类拼接时
# 「|」会把表达式拆成多个分支，只有最后一个分支才带捕获组。
_DECL = (
    r"(?:投标函?总价|投标总报价|投标报价|开标一览表|报价合计|总价合计|"
    r"合计金额|总价|合\s*计|大写金额|人民币大写)"
)
_DECL_RE = re.compile(
    _DECL + r"[^\d¥￥零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{0,20}"
    r"([¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:万元|万|元)?)"
)
_DECL_LABEL = re.compile(_DECL)
_CN_CHARS = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]")
# 招标原文、评分、税额里的数字不是投标总价，一律排除。
_EXCLUDE_LINE = re.compile(
    r"最高限价|招标控制价|控制价|预算|暂列|暂估|费率|税率|评分|分值|"
    r"单价限价|第[一二三四五六七八九十\d]+章")
_TABLE_HEAD = re.compile(r"名称|品名|货物|项目|内容|description|name", re.I)
_TABLE_MONEY = re.compile(r"单价|合价|金额|总价|unit price|amount", re.I)
_UNIT = re.compile(r"万元|万|元|¥|￥")


def _amount(raw: str) -> float | None:
    value = quote_svc.parse_number(raw)
    return value if value and value > 0 else None


def _cap_yuan(cap: dict) -> float | None:
    """只有明确抽到金额的限价才参与比较；「无 / 含糊 / 未提及」一律不猜。"""
    if (cap or {}).get("status") != "amount":
        return None
    return _amount(str(cap.get("value") or ""))


def _declarations(text: str) -> list[dict]:
    """从标书正文里抽「投标函 / 开标一览表 / 大写」等总价声明。"""
    out: list[dict] = []
    for line in str(text or "").splitlines():
        if _EXCLUDE_LINE.search(line) or not _DECL_LABEL.search(line):
            continue
        # 中文大写金额单独走大写解析，避免把「壹拾」当数字抠错。
        if "大写" in line and _CN_CHARS.search(line):
            value = cn_amount(line)
            if value and value > 0:
                out.append({"label": "大写金额", "value": round(value, 2),
                            "source": line.strip()[:80]})
            continue
        m = _DECL_RE.search(line)
        if not m or m.group(1) is None:
            continue
        raw = m.group(1).strip()
        value = _amount(raw)
        # 「第三章 投标报价 3.1」这类章节号不是金额；无单位的小数字也不当金额。
        if not value or (not _UNIT.search(raw) and value < 1000):
            continue
        out.append({"label": _DECL_LABEL.search(line).group(0),
                    "value": round(value, 2), "source": line.strip()[:80]})
    seen: set[tuple[str, float]] = set()
    uniq: list[dict] = []
    for d in out:
        key = (d["label"], d["value"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    return uniq[:8]


def _tables(text: str) -> list[dict]:
    """按表头行把正文切成若干报价表，各自算合计。

    原来 quote.analyze 只看整段文本的前 12 行表头，正文里第二张报价表
    会被当成第一张的明细行，多表总价根本没有比较对象。
    """
    lines = str(text or "").splitlines()
    heads = [i for i, ln in enumerate(lines)
             if _TABLE_HEAD.search(ln) and _TABLE_MONEY.search(ln)
             and any(sep in ln for sep in ("|", "\t", ","))]
    if not heads:
        try:
            r = quote_svc.analyze(text=text)
        except Exception:
            return []
        s = r.get("summary") or {}
        return [{"index": 1, "total": s.get("table_total") or s.get("sum_amount"),
                 "rows": s.get("row_count", 0), "issues": len(r.get("issues") or [])}]
    out: list[dict] = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        seg = "\n".join(lines[start:end]).strip()
        if not seg:
            continue
        try:
            r = quote_svc.analyze(text=seg)
        except Exception:
            continue
        s = r.get("summary") or {}
        out.append({"index": len(out) + 1,
                    "total": s.get("table_total") or s.get("sum_amount"),
                    "rows": s.get("row_count", 0),
                    "issues": len(r.get("issues") or [])})
    return out


def scan(pid: str) -> dict:
    """只读汇总，不写库、不调模型。"""
    cap = goods_list.extract_cap("\n".join(
        [str((store.get_project(pid) or {}).get("tender_text") or "")] +
        [str(d.get("content") or "") for d in store.list_docs(project_id=pid)]
    ))
    bid_text = "\n".join(
        f"{s.get('title') or ''}\n{s.get('text') or ''}"
        for s in store.list_sections(pid)
    )
    tables = _tables(bid_text)
    decls = _declarations(bid_text)
    cap_yuan = _cap_yuan(cap)

    # 投标总价优先取投标函声明，其次表内合计行，最后才是明细算术和。
    primary = next((d["value"] for d in decls
                    if any(w in d["label"] for w in ("投标", "开标", "大写"))), None)
    table_total = next((t["total"] for t in tables if t["total"] is not None), None)
    total = primary if primary is not None else table_total
    total_source = ("总价声明" if primary is not None
                    else "报价表合计行" if table_total is not None else "")

    candidates: list[dict] = ([{"label": f"第{t['index']}张报价表合计", "value": t["total"]}
                              for t in tables if t["total"] is not None] +
                             [{"label": d["label"], "value": d["value"]} for d in decls])
    uniq: list[dict] = []
    for c in candidates:
        if not any(abs(c["value"] - u["value"]) <= 0.02 for u in uniq):
            uniq.append(c)
    mismatch = len(uniq) > 1

    if cap_yuan is None:
        cap_verdict = {"amount": "unclear", "none": "none",
                       "absent": "absent", "unclear": "unclear"}.get(cap.get("status"), "unclear")
    elif total is None:
        cap_verdict = "no_quote"
    else:
        cap_verdict = "over" if total > cap_yuan + 0.02 else "within"

    issues: list[dict] = []
    if cap_verdict == "over":
        issues.append({
            "key": "quote-over-cap", "level": "高", "blocking": False,
            "action": "核对报价与限价", "source": "最高限价",
            "title": f"投标总价 {total:,.2f} 元超过最高限价 {cap_yuan:,.2f} 元",
            "detail": (f"按{total_source}与招标文件载明的限价直接比较，超出 "
                       f"{total - cap_yuan:+,.2f} 元。这是算术结论；若前附表约定按含税价、"
                       "单价限价、分包限价或剔除角分计算，仍须按评审口径人工复核。"),
            "evidence": f"quote_gate:{cap.get('value')}"})
    elif cap_verdict == "no_quote":
        issues.append({
            "key": "quote-cap-no-table", "level": "中", "blocking": False,
            "action": "补报价表", "source": "最高限价",
            "title": f"本项目设最高限价 {cap.get('value')}，但没读到报价表",
            "detail": (f"限价折算 {cap_yuan:,.2f} 元；标书正文里没有可解析的报价表，"
                       "因此无法判定是否超限。系统不会用示例金额代替你的报价，"
                       "请录入本项目报价表后再核对。"),
            "evidence": "quote_gate::no_quote"})
    if mismatch and tables:
        listed = "；".join(f"{c['label']} {c['value']:,.2f} 元" for c in uniq[:5])
        issues.append({
            "key": "quote-total-mismatch", "level": "高", "blocking": False,
            "action": "对齐各处总价", "source": "报价一致性",
            "title": f"报价表与总价声明出现 {len(uniq)} 个不同金额",
            "detail": (f"{listed}。投标函、开标一览表、报价明细与大写金额必须完全一致，"
                       "不一致通常会被要求澄清或直接按不利口径评审。"),
            "evidence": "quote_gate::cross_check"})

    if not tables:
        summary = "没有读到本项目的报价表，限价与总价均未校验"
    elif cap_verdict == "over":
        summary = f"报价合计 {total:,.2f} 元，超过限价 {cap_yuan:,.2f} 元"
    elif cap_verdict == "within":
        summary = f"报价合计 {total:,.2f} 元，未超过限价 {cap_yuan:,.2f} 元"
    else:
        summary = (f"读到 {len(tables)} 张报价表，合计 {total:,.2f} 元；"
                   f"限价：{cap.get('note') or cap.get('status')}")
    return {
        "quote_found": bool(tables), "tables": tables, "declarations": decls,
        "total": total, "total_source": total_source,
        "cap": cap, "cap_yuan": cap_yuan, "cap_verdict": cap_verdict,
        "cross_verdict": "mismatch" if mismatch else ("ok" if tables else "no_quote"),
        "issues": issues, "summary": summary,
    }


def export_issues(pid: str) -> list[dict]:
    """接入导出门禁。异常时明确说「没核对完」，不静默当作通过。"""
    try:
        return scan(pid)["issues"]
    except Exception as exc:
        return [{"key": "quote_gate_error", "level": "高", "blocking": True,
                 "action": "重试体检", "title": "限价与总价核对没跑完",
                 "detail": (f"报价一致性检查未完成（{type(exc).__name__}: "
                            f"{str(exc)[:120]}），本轮不能当作已核对。"),
                 "evidence": "backend/app/quote_gate.py::scan"}]
