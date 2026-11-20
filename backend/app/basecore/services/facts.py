"""项目级全局事实表：一本书里的数字与名称只能有一个来源。

生成时把事实表注入提示词做**事前约定**，导出前再做**事后核对**——
两处夹住，避免“前面写 120 天、后面写 90 天”这类硬伤。

事实只存确定值；不确定的一律留空，由人工填写，不用模型猜。
"""
from __future__ import annotations

import re

# key 是稳定标识，label 只是展示名，改 label 不影响历史引用
DEFAULT_FACTS: list[tuple[str, str, str]] = [
    ("project_name", "项目名称", "项目"),
    ("tender_no", "项目编号", "项目"),
    ("tenderer", "招标人", "项目"),
    ("agent", "招标代理机构", "项目"),
    ("budget", "最高限价", "报价"),
    ("bidder", "投标人", "投标人"),
    ("legal_person", "法定代表人", "投标人"),
    ("pm", "项目经理", "人员"),
    ("price", "投标总价", "报价"),
    ("price_text", "投标总价（大写）", "报价"),
    ("deposit", "投标保证金", "报价"),
    ("duration", "工期", "工期"),
    ("warranty", "质保期", "工期"),
    ("validity", "投标有效期", "工期"),
    ("qualification", "资质等级", "资质"),
]

LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "project_name": ("项目名称", "工程名称", "采购项目名称", "招标项目名称", "标段名称"),
    "tender_no": ("项目编号", "招标编号", "采购编号", "标段编号", "项目代码"),
    "tenderer": ("招标人", "采购人", "招标单位", "建设单位", "采购单位"),
    "agent": ("招标代理机构", "代理机构", "采购代理机构"),
    "budget": ("最高限价", "预算金额", "招标控制价", "项目预算", "采购预算"),
    "bidder": ("投标人", "供应商", "投标单位"),
    "duration": ("工期", "服务期", "交货期", "计划工期", "履约期限"),
    "warranty": ("质保期", "保修期", "质量保证期", "免费质保期"),
    "validity": ("投标有效期",),
    "deposit": ("投标保证金", "保证金"),
    "qualification": ("资质等级", "资质要求", "企业资质"),
    "pm": ("项目经理", "项目负责人"),
}

# 从招标正文抽候选值。定位词是硬门槛，宁可漏抽也不要抽错。
TEXT_RULES: list[tuple[str, str]] = [
    ("tender_no", r"(?:项目编号|招标编号|采购编号|标段编号)[：:\s]*([A-Za-z0-9\-_/／（）()]{6,40})"),
    ("project_name", r"(?:项目名称|工程名称)[：:\s]*([^\n，。；]{4,50})"),
    ("tenderer", r"(?:招标人|采购人|招标单位)[：:\s]*([^\n，。；:：]{3,40})"),
    ("agent", r"(?:招标代理机构|采购代理机构|代理机构)[：:\s]*([^\n，。；:：]{3,40})"),
    ("budget", r"(?:最高限价|预算金额|招标控制价|项目预算)[^0-9\n]{0,12}([0-9][0-9,.\s]*)\s*(万元|元)"),
    ("duration", r"(?:工期|服务期|交货期|履约期限)[^0-9\n]{0,10}(\d{1,4})\s*(?:个?日历天|天|日)"),
    ("validity", r"投标有效期[^0-9\n]{0,10}(\d{1,3})\s*(?:个?日历天|天|日)"),
    ("deposit", r"投标保证金[^0-9\n]{0,12}([0-9][0-9,.]*)\s*(万元|元)"),
]

# 事后核对用的定位规则：只有紧跟指标词的数字才算“同一个指标”
CHECK_RULES: dict[str, str] = {
    "duration": r"(?:工期|服务期|交货期|履约期限)[^0-9\n]{0,10}(\d{1,4})\s*(?:个?日历天|天|日)",
    "validity": r"投标有效期[^0-9\n]{0,10}(\d{1,3})\s*(?:个?日历天|天|日)",
    "deposit": r"投标保证金[^0-9\n]{0,12}([0-9][0-9,.]*)\s*(万?元)",
    "price": r"(?:投标总价|投标报价|总报价|报价总额|合计金额)[^0-9\n]{0,10}([0-9][0-9,.]*)\s*(万?元)",
    "budget": r"(?:最高限价|招标控制价)[^0-9\n]{0,10}([0-9][0-9,.]*)\s*(万?元)",
}

NUM_KEYS = set(CHECK_RULES)


def defaults() -> list[dict]:
    return [{"key": k, "label": lb, "category": c, "value": ""} for k, lb, c in DEFAULT_FACTS]


def _clean(value: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", (value or "").strip())


BAD_VALUE = re.compile(r"不予受理|否决|无效|废标|应当|不得|须提供|拒绝")


def _normalize(key: str, value: str) -> str:
    """把解读结果里的长句收敛成事实值；收不敛的直接丢弃，宁缺勿错。"""
    value = _clean(value).strip("：:，,。；; ")
    if not value:
        return ""
    if key in ("duration", "warranty", "validity"):
        m = re.search(r"(\d{1,4})\s*(?:个?日历天|天|日)", value) or re.search(r"(\d{1,4})", value)
        return f"{m.group(1)} 日历天" if m else ""
    if key in ("budget", "deposit", "price", "price_text"):
        m = re.search(r"([0-9][0-9,.]*)\s*(万元|万|元)", value)
        if not m:
            return ""
        return f"{m.group(1)}{'万元' if m.group(2).startswith('万') else '元'}"
    if key in ("tenderer", "agent", "bidder", "project_name", "legal_person", "pm"):
        if len(value) > 40 or BAD_VALUE.search(value):
            return ""
        return value
    return value


def parse_amount(raw: str) -> float | None:
    """把「1,286.5万元」「￥1286500.00」统一折算成元。"""
    if not raw:
        return None
    text = str(raw).replace(",", "").replace("，", "").replace("￥", "").replace("¥", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万元|万|元)?", text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if m.group(2) in ("万元", "万"):
        value *= 10000
    return value


def parse_days(raw: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", str(raw or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _fact_value(fact: dict) -> float | None:
    key = fact.get("key")
    value = fact.get("value") or ""
    if not value:
        return None
    if key in ("price", "price_text", "deposit", "budget"):
        return parse_amount(value)
    if key in ("duration", "warranty", "validity"):
        return parse_days(value)
    return None


def from_meta(meta: dict, enterprise_name: str = "") -> dict[str, dict]:
    """把招标解读出的 basic 列表映射成事实候选。"""
    found: dict[str, dict] = {}
    for row in (meta or {}).get("basic") or []:
        label = _clean(str(row.get("label") or ""))
        value = _clean(str(row.get("value") or ""))
        if not label or not value:
            continue
        for key, aliases in LABEL_ALIASES.items():
            if key in found:
                continue
            if any(alias in label for alias in aliases):
                normalized = _normalize(key, value)
                if not normalized:
                    continue
                category = next((c for k, _, c in DEFAULT_FACTS if k == key), "其他")
                found[key] = {"key": key, "label": next((lb for k, lb, _ in DEFAULT_FACTS if k == key), label),
                              "value": normalized, "category": category, "source": "interpret"}
                break
    if enterprise_name and "bidder" not in found:
        found["bidder"] = {"key": "bidder", "label": "投标人", "value": enterprise_name,
                           "category": "投标人", "source": "enterprise"}
    return found


def from_tender(text: str, limit: int = 12) -> list[dict]:
    """从招标正文抽候选；只返回规范化后的值，抽不准的宁可不给。"""
    text = text or ""
    out: list[dict] = []
    for key, pattern in TEXT_RULES:
        if len(out) >= limit:
            break
        value = ""
        # 同一指标可能先出现干扰句（“投标人对招标人……不予受理”），往后找 5 个候选
        for m in list(re.finditer(pattern, text))[:5]:
            if key in ("budget", "deposit"):
                raw = f"{_clean(m.group(1))}{'万元' if m.group(2).startswith('万') else '元'}"
            elif key in ("duration", "validity"):
                raw = f"{m.group(1)} 日历天"
            else:
                raw = _clean(m.group(1))
            value = _normalize(key, raw)
            if value:
                break
        if not value:
            continue
        category = next((c for k, _, c in DEFAULT_FACTS if k == key), "其他")
        label = next((lb for k, lb, _ in DEFAULT_FACTS if k == key), key)
        out.append({"key": key, "label": label, "value": value, "category": category, "source": "extract"})
    return out


def build_block(facts: list[dict]) -> str:
    """注入提示词的事实块。空值不出现，避免模型把空字段当已知。"""
    rows = [f"- {f['label']}：{f['value']}" for f in facts if (f.get("value") or "").strip()]
    if not rows:
        return ""
    tips = ["本节如确需用到表中没有的事实，写〔待填：xxx〕，不得自行编造。"]
    if not any((f.get("value") or "").strip() for f in facts if f.get("key") == "bidder"):
        tips.append("投标人名称尚未提供，凡需出现投标人/公司名称处一律写〔待填：投标人名称〕，不得虚构公司名。")
    return (
        "全局事实（全书必须一致，下列值原样使用，不得改写数字、单位与名称）：\n"
        + "\n".join(rows)
        + "\n"
        + "".join(tips)
    )


def consistency(texts: dict[str, str], facts: list[dict]) -> list[dict]:
    """事后核对：正文里同一指标出现了不同数值就报冲突。

    texts: {来源名: 正文}，来源名用章节标题，便于定位。
    """
    issues: list[dict] = []
    checked = [f for f in facts if f.get("key") in NUM_KEYS and (f.get("value") or "").strip()]
    if not checked:
        return issues

    for fact in checked:
        key = fact["key"]
        expect = _fact_value(fact)
        if expect is None:
            continue
        pattern = re.compile(CHECK_RULES[key])
        conflicts: list[str] = []
        for source, body in texts.items():
            for m in pattern.finditer(body or ""):
                groups = m.groups()
                raw_num = groups[0]
                unit = groups[1] if len(groups) > 1 and groups[1] else ""
                if key in ("duration", "validity"):
                    got = parse_days(raw_num)
                else:
                    got = parse_amount(f"{raw_num}{'万元' if unit.startswith('万') else '元'}")
                if got is None:
                    continue
                if abs(got - expect) > max(0.5, expect * 0.001):
                    snippet = (body[max(0, m.start() - 12) : m.end() + 8]).replace("\n", " ")
                    conflicts.append(f"{source}：{snippet}")
        if conflicts:
            issues.append(
                {
                    "code": f"F-{key}",
                    "level": "高",
                    "title": f"{fact['label']}与全局事实不一致",
                    "detail": f"事实表为「{fact['value']}」，正文出现 {len(conflicts)} 处不同数值。"
                              "同一指标全书必须一致，否则评标时会被质疑。",
                    "evidence": "；".join(dict.fromkeys(conflicts))[:180],
                }
            )
    return issues


def summary(facts: list[dict]) -> dict:
    filled = [f for f in facts if (f.get("value") or "").strip()]
    by_cat: dict[str, int] = {}
    for f in filled:
        by_cat[f.get("category") or "其他"] = by_cat.get(f.get("category") or "其他", 0) + 1
    missing = [f["label"] for f in facts if not (f.get("value") or "").strip()]
    return {"total": len(facts), "filled": len(filled), "by_category": by_cat, "missing": missing[:8]}
