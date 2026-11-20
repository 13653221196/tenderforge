"""技能公共工具：分句、取证、数值、结果封装。

所有判定都在这里收敛成统一结构，前端只需一套渲染逻辑：
    {verdict, score, summary, stats, items[], tables[], notice}
"""
from __future__ import annotations

import re

SENT_SPLIT = re.compile(r"(?<=[。；;！!？?\n])")
NUM_PREFIX = re.compile(r"^\s*(?:[（(]?\s*[一二三四五六七八九十0-9]{1,3}\s*[）).、．]\s*)")
CN_DIGITS = {
    "零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000, "万": 10000, "亿": 100000000}

LEVEL_ORDER = {"high": 0, "mid": 1, "low": 2, "info": 3}
VERDICT_ORDER = {"fail": 0, "warn": 1, "info": 2, "pass": 3}


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def split_sentences(text: str, min_len: int = 6) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= min_len]


def line_of(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : end if end != -1 else len(text)].strip()


def excerpt(text: str, keyword: str, *, window: int = 60, limit: int = 160) -> str:
    """取关键词所在的那一句，作为证据。找不到返回空串。"""
    if not keyword:
        return ""
    pos = text.find(keyword)
    if pos == -1:
        return ""
    line = line_of(text, pos)
    if len(line) <= limit:
        return line
    left = max(0, pos - window)
    return text[left : pos + window].strip()


def pick_sentence(text: str, keywords: tuple[str, ...] | list[str], *, limit: int = 200) -> str:
    for sentence in split_sentences(text):
        if any(k in sentence for k in keywords):
            return sentence[:limit]
    return ""


def cn_to_number(raw: str) -> float | None:
    """中文数字转阿拉伯数字，支持「叁拾万元」「一百二十」这类写法。"""
    text = re.sub(r"[^零〇壹贰叁肆伍陆柒捌玖一二两三四五六七八九十百千万亿]", "", raw or "")
    if not text:
        return None
    if text.isdigit():
        return float(text)
    total, section, number = 0.0, 0.0, 0.0
    for char in text:
        if char in CN_DIGITS:
            number = float(CN_DIGITS[char])
        elif char in CN_UNITS:
            unit = CN_UNITS[char]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section, number = 0.0, 0.0
            else:
                section += (number or 1) * unit
                number = 0.0
    return total + section + number


MONEY_RE = re.compile(r"(?:人民币)?\s*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(万元|元|万)")
YEN_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
CN_MONEY_RE = re.compile(r"人民币?\s*([零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,})\s*(元|万元)?")


def to_yuan(value: float, unit: str) -> float:
    return value * 10000 if unit.startswith("万") else value


def find_amounts(text: str) -> list[tuple[float, str]]:
    """抽出文本里的金额（统一折算成元），返回 [(元, 原文)]。"""
    out: list[tuple[float, str]] = []
    for match in MONEY_RE.finditer(text or ""):
        raw = match.group(1).replace(",", "").replace("，", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        unit = match.group(2) or "元"
        out.append((to_yuan(value, unit), match.group(0).strip()))
    for match in YEN_RE.finditer(text or ""):
        raw = match.group(1).replace(",", "").replace("，", "")
        try:
            out.append((float(raw), match.group(0).strip()))
        except ValueError:
            continue
    for match in CN_MONEY_RE.finditer(text or ""):
        value = cn_to_number(match.group(1))
        if value is None:
            continue
        unit = match.group(2) or "元"
        out.append((to_yuan(value, unit), match.group(0).strip()))
    return out


def days_of(text: str) -> int | None:
    match = re.search(r"(?:不少于|不低于|至少|≥)?\s*([0-9]{1,4})\s*(?:个)?(?:日历)?[天日]", text or "")
    if not match:
        return None
    return int(match.group(1))


def years_of(text: str) -> int | None:
    match = re.search(r"([0-9]{1,2})\s*年", text or "")
    return int(match.group(1)) if match else None


def item(level: str, title: str, detail: str = "", evidence: str = "", advice: str = "") -> dict:
    return {
        "level": level,
        "title": title.strip(),
        "detail": detail.strip(),
        "evidence": (evidence or "").strip()[:300],
        "advice": (advice or "").strip(),
    }


def table(title: str, columns: list[str], rows: list[list], note: str = "") -> dict:
    return {"title": title, "columns": columns, "rows": rows, "note": note}


def verdict_of(items: list[dict], *, soft: bool = False) -> str:
    levels = {i["level"] for i in items}
    if "high" in levels:
        return "warn" if soft else "fail"
    if "mid" in levels or "low" in levels:
        return "warn"
    return "pass" if items else "info"


def score_of(items: list[dict], base: int = 100) -> int:
    penalty = {"high": 15, "mid": 7, "low": 3, "info": 0}
    total = base - sum(penalty.get(i["level"], 0) for i in items)
    return max(0, min(100, total))


def result(
    summary: str,
    items: list[dict] | None = None,
    *,
    verdict: str | None = None,
    score: int | None = None,
    stats: dict | None = None,
    tables: list[dict] | None = None,
    notice: str = "",
) -> dict:
    items = items or []
    items.sort(key=lambda x: LEVEL_ORDER.get(x["level"], 9))
    return {
        "verdict": verdict or verdict_of(items),
        "score": score,
        "summary": summary,
        "stats": stats or {},
        "items": items,
        "tables": tables or [],
        "notice": notice,
    }


def split_blocks(text: str, headers: tuple[str, ...]) -> dict[str, str]:
    """按标题关键词把正文切成若干段，便于跨章节比对。"""
    blocks: dict[str, list[str]] = {}
    current = "其他"
    for line in (text or "").split("\n"):
        stripped = line.strip()
        matched = next((h for h in headers if stripped.startswith(h) or (len(stripped) <= 24 and h in stripped)), None)
        if matched:
            current = matched
        blocks.setdefault(current, []).append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


def similar(a: str, b: str, n: int = 2) -> float:
    sa = {norm(a)[i : i + n] for i in range(max(0, len(norm(a)) - n + 1))}
    sb = {norm(b)[i : i + n] for i in range(max(0, len(norm(b)) - n + 1))}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
