"""标书自查：12 条确定性规则（离线可跑）+ 可选 AI 复核。"""
from __future__ import annotations

import asyncio
import re
from collections import Counter

from ..config import LLMConfig
from .llm import chat, parse_json

PLACEHOLDER_PATTERNS = [
    r"〔待填[^〕]*〕", r"【[^】]{0,12}】", r"\bTODO\b", r"\bXXX\b", r"××××",
    r"待补充", r"待填写", r"未填写", r"此处填写",
]

STAR_KEY_MIN = 2  # 星号条款关键词命中最少个数


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _ngrams(text: str, n: int = 2) -> set[str]:
    text = _norm(text)
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


def _similarity(a: str, b: str) -> float:
    sa, sb = _ngrams(a), _ngrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _keywords(text: str, limit: int = 6) -> list[str]:
    """极简关键词：去掉标点后的 3-6 字连续片段，按出现频次取前 N。"""
    chunks = re.split(r"[，。；：、（）()【】《》\s,.;:!?]+", text)
    words: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        for size in (6, 5, 4, 3):
            if len(chunk) == size:
                words.append(chunk)
                break
        else:
            if len(chunk) > 6:
                words.extend([chunk[:6], chunk[-6:]])
    stop = {"投标人", "应当", "本项目", "招标人", "采购人", "满足", "提供", "要求"}
    return [w for w in dict.fromkeys(words) if w not in stop][:limit]


def _find(corpus: str, text: str, need: int = STAR_KEY_MIN) -> bool:
    keys = _keywords(text)
    if not keys:
        return _norm(text)[:6] in corpus
    hit = sum(1 for k in keys if k in corpus)
    return hit >= min(need, max(1, len(keys) // 2))


CN_DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
             "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
CN_BIG = {"万": 10_000, "亿": 100_000_000}


def cn_amount(text: str) -> float | None:
    """把“壹拾贰万叁仟元整”这类大写金额转成数字，失败返回 None。"""
    text = _norm(text)
    if not text or not any(ch in text for ch in CN_DIGITS):
        return None
    total = 0.0
    section = 0.0
    number = 0.0
    for ch in text:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            section += (number or 1) * CN_UNITS[ch]
            number = 0
        elif ch in CN_BIG:
            section = (section + number) * CN_BIG[ch]
            total += section
            section = 0
            number = 0
        elif ch == "元":
            section += number
            total += section
            section = 0
            number = 0
            break
    if total == 0 and section == 0 and number == 0:
        return None
    return total + section + number


def _digits_amount(text: str, *, last: bool = False) -> float | None:
    items = list(re.finditer(r"(\d[\d,，]*(?:\.\d+)?)\s*(万元|元)", text))
    if not items:
        return None
    match = items[-1] if last else items[0]
    value = float(match.group(1).replace(",", "").replace("，", ""))
    return value * 10000 if match.group(2) == "万元" else value


def run_rules(project: dict, sections: list[dict], meta: dict) -> list[dict]:
    issues: list[dict] = []
    tender = project.get("tender_text") or ""
    # 两套语料必须分开：
    # - draft 只含目录标题与正文，用于「投标文件是否响应了这条要求」；
    # - full_text 只含正文，用于占位符、重复段落等文本质量判断。
    # 早期版本把招标原文混进覆盖判断，导致 ★条款、资格条件、评分项都能在原文里自证，
    # 三项高风险规则永远不报警（等于形同虚设），这里已拆开。
    draft_raw = "\n".join(f"{s.get('title') or ''}\n{s.get('content') or ''}" for s in sections)
    draft = _norm(draft_raw)
    full_text = "\n".join(s.get("content") or "" for s in sections)
    done = [s for s in sections if (s.get("content") or "").strip()]

    def add(code: str, level: str, title: str, detail: str, evidence: str = "") -> None:
        issues.append({"code": code, "level": level, "title": title, "detail": detail, "evidence": evidence[:180]})

    # C01 占位符残留
    hits: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        hits.extend(re.findall(pattern, full_text))
    if hits:
        add("C01", "高", "存在待填占位符",
            f"正文中仍有 {len(hits)} 处未填写内容，直接递交会被认定为未响应或格式不符。",
            "、".join(dict.fromkeys(hits))[:160])

    # C02 空章节
    empty = [s["title"] for s in sections if not (s.get("content") or "").strip()]
    if empty:
        add("C02", "高", "存在空章节",
            f"共 {len(empty)} 个章节没有正文，评标时按缺项处理。",
            "、".join(empty[:8]))

    # C03 金额大小写一致
    for section in done:
        content = section["content"]
        pairs = re.findall(r"大写[）)]?[：:\s]*([零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整角分]{2,})", content)
        for upper in pairs:
            upper_value = cn_amount(upper)
            pos = content.find(upper)
            tail = content[pos : pos + 120]
            # 先看大写之后（“大写：…元，小写 4,860,000 元”）
            lower_value = _digits_amount(tail)
            if lower_value is None and pos > 0:
                # 再看大写之前同一行（“人民币 4,860,000 元（大写：…）”是最常见写法）
                head = content[max(0, pos - 200) : pos].split("\n")[-1]
                lower_value = _digits_amount(head, last=True)
            if upper_value and lower_value and abs(upper_value - lower_value) > 1:
                add("C03", "高", "报价大小写不一致",
                    f"大写“{upper}”折算 {upper_value:,.2f} 元，小写为 {lower_value:,.2f} 元，差额 {abs(upper_value - lower_value):,.2f} 元。",
                    (tail or head)[:120])
                break

    # C04 星号条款覆盖（只看投标正文：招标原文里当然有这条，混进去就永远算“已响应”）
    for item in (meta.get("star_clauses") or [])[:15]:
        text = str(item.get("item") or "")
        if text and len(text) >= 6 and not _find(draft, text):
            add("C04", "高", "★条款可能未响应", f"未在正文中找到对该条款的明确响应：{text}", text[:120])

    # C05 资格要求覆盖
    for item in (meta.get("qualification") or [])[:12]:
        text = str(item.get("item") or "")
        if text and len(text) >= 6 and not _find(draft, text):
            add("C05", "中", "资格要求可能缺项", f"正文未见对应证明材料或承诺：{text}", text[:120])

    # C06 评分项覆盖
    missing: list[str] = []
    for item in (meta.get("scoring") or [])[:20]:
        name = str(item.get("item") or "").strip()
        if len(name) >= 3 and _norm(name) not in draft:
            missing.append(name)
    if missing:
        add("C06", "中", "评分项未在正文体现",
            f"{len(missing)} 个评分项名称未出现在目录或正文中，评委可能找不到对应内容。",
            "、".join(missing[:8]))

    # C07 投标函要素
    letter_text = "\n".join(s["content"] for s in done if "投标函" in s["title"])
    if done and letter_text:
        required = {"报价": ("报价", "总价", "金额"), "工期": ("工期", "服务期", "交货期"),
                    "有效期": ("有效期", "日历天"), "签署": ("法定代表人", "授权代表", "签字", "盖章")}
        gaps = [label for label, words in required.items() if not any(w in letter_text for w in words)]
        if gaps:
            add("C07", "高", "投标函要素缺失", f"投标函缺少：{'、'.join(gaps)}", "")

    # C08 签署位
    if done:
        sign_words = ("法定代表人", "授权代表", "盖章", "签字")
        if not any(w in full_text for w in sign_words):
            add("C08", "中", "未见签署要素", "全文未出现法定代表人、授权代表、签字或盖章字样，需补签章页。")

    # C09 目录层级跳级
    levels = [s["level"] for s in sections]
    for prev, curr in zip(levels, levels[1:]):
        if curr - prev > 1:
            idx = levels.index(curr)
            add("C09", "低", "目录层级跳级",
                f"“{sections[idx]['title']}”从 {prev} 级直接跳到 {curr} 级，Word 目录会错乱。")
            break

    # C10 重复段落
    paragraphs: list[tuple[str, str]] = []
    for section in done:
        for para in section["content"].split("\n"):
            para = para.strip()
            if len(para) >= 60 and not para.startswith("|"):
                paragraphs.append((section["title"], para))
    duplicated: list[str] = []
    for i in range(len(paragraphs)):
        for j in range(i + 1, min(i + 40, len(paragraphs))):
            if paragraphs[i][0] == paragraphs[j][0]:
                continue
            if _similarity(paragraphs[i][1], paragraphs[j][1]) >= 0.86:
                duplicated.append(f"{paragraphs[i][0]} ↔ {paragraphs[j][0]}")
    if duplicated:
        add("C10", "中", "章节间存在高度重复段落",
            f"发现 {len(duplicated)} 组重复内容，容易被判为套模板。",
            "；".join(dict.fromkeys(duplicated))[:160])

    # C11 标题重复
    titles = Counter(s["title"] for s in sections)
    repeats = [t for t, c in titles.items() if c > 1]
    if repeats:
        add("C11", "低", "章节标题重复", "同一标题出现多次，目录会重复。", "、".join(repeats[:6]))

    # C12 截止与开标时间顺序
    basic = {row.get("label"): row.get("value") for row in (meta.get("basic") or [])}
    deadline = basic.get("投标截止时间") or ""
    opening = basic.get("开标时间") or ""
    if deadline and opening:
        d = re.search(r"(\d{1,2})\s*[月\-/]\s*(\d{1,2})", deadline)
        o = re.search(r"(\d{1,2})\s*[月\-/]\s*(\d{1,2})", opening)
        if d and o and (int(o.group(1)), int(o.group(2))) < (int(d.group(1)), int(d.group(2))):
            add("C12", "高", "开标时间早于投标截止",
                f"截止：{deadline}；开标：{opening}。时间逻辑相反，需核对招标文件原文。")

    # C13 表格数值核对（数量×单价=合价，逐行）
    table_issues = _check_tables(done)
    for row in table_issues:
        add("C13", row["level"], row["title"], row["detail"], row["evidence"])

    order = {"高": 0, "中": 1, "低": 2}
    issues.sort(key=lambda i: (order.get(i["level"], 3), i["code"]))
    return issues


# markdown 表格行解析：| a | b | c |
TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
NUM_CELL_RE = re.compile(r"(-?\d[\d,，]*(?:\.\d+)?)")


def _cells_raw(line: str) -> list[str] | None:
    m = TABLE_ROW_RE.match(line)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|")]


def _is_sep(cells: list[str] | None) -> bool:
    return bool(cells) and all(c and set(c) <= set("-: ") for c in cells)


def _markdown_tables(content: str) -> list[tuple[list[str], list[list[str]]]]:
    """按「表头行 + 分隔行 + 数据行」整块识别表格，避免分隔行打断状态。"""
    lines = content.split("\n")
    tables: list[tuple[list[str], list[list[str]]]] = []
    i = 0
    while i < len(lines) - 1:
        header = _cells_raw(lines[i])
        sep = _cells_raw(lines[i + 1])
        if header and not _is_sep(header) and _is_sep(sep):
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines):
                row = _cells_raw(lines[j])
                if not row or _is_sep(row):
                    break
                rows.append(row)
                j += 1
            tables.append((header, rows))
            i = j
        else:
            i += 1
    return tables


def _header_index(headers: list[str], words: tuple[str, ...]) -> int:
    for i, h in enumerate(headers):
        if any(w in h.lower() for w in words):
            return i
    return -1


def _cell_num(cells: list[str], idx: int) -> float | None:
    if idx < 0 or idx >= len(cells):
        return None
    m = NUM_CELL_RE.search(cells[idx])
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace("，", ""))
    except ValueError:
        return None


def _check_tables(sections: list[dict]) -> list[dict]:
    """对正文里的 markdown 表格做确定性核对：数量×单价 是否等于合价。

    表格是标书数字最密集的地方（报价、清单、参数表都在表格里），
    只扫段落会漏掉一半问题——这里逐表逐行核算，缺值行跳过不猜。
    """
    out: list[dict] = []
    for section in sections:
        checked_rows = 0
        bad_rows = 0
        for headers, rows in _markdown_tables(section.get("content") or ""):
            qi = _header_index(headers, ("数量", "工程量"))
            pi = _header_index(headers, ("单价",))
            ai = _header_index(headers, ("合价", "总价", "金额"))
            if qi < 0 or pi < 0 or ai < 0 or ai == pi:
                continue
            for cells in rows:
                q, p, a = _cell_num(cells, qi), _cell_num(cells, pi), _cell_num(cells, ai)
                if q is None or p is None or a is None:
                    continue  # 缺值行跳过，不猜
                checked_rows += 1
                if abs(q * p - a) > max(0.05, abs(a) * 0.001):
                    bad_rows += 1
        if checked_rows and bad_rows:
            out.append(
                {
                    "level": "高" if bad_rows >= 3 else "中",
                    "title": "表格数值核算不符",
                    "detail": f"「{section['title']}」表格中 {bad_rows}/{checked_rows} 行「数量×单价」与合价对不上（容差 0.05 元或 0.1%），报价表数字错误会直接扣商务分。",
                    "evidence": section["title"],
                }
            )
    return out


def summarize(issues: list[dict], notice: str = "") -> dict:
    """三态结论：没有「大概通过」。有高→fail；有中或 AI 复核缺席→review；否则 pass。

    REVIEW 是一个明确的状态：意思是"规则没抓到死问题，但有人工必须复核的理由"，
    不允许把"没抓到"直接包装成"没问题"。
    """
    summary = {"高": 0, "中": 0, "低": 0}
    for item in issues:
        summary[item["level"]] = summary.get(item["level"], 0) + 1
    has_high = summary["高"] > 0
    has_mid = summary["中"] > 0
    ai_missing = bool(notice and ("跳过" in notice or "失败" in notice))
    if has_high:
        verdict = "fail"
        reason = f"存在 {summary['高']} 个高风险问题，可能构成无效响应，必须先处理。"
    elif has_mid:
        verdict = "review"
        reason = f"没有致命问题，但存在 {summary['中']} 个中风险项，提交前需人工复核。"
    elif ai_missing:
        verdict = "review"
        reason = "规则未发现硬伤，但 AI 复核缺席（未配置或调用失败），建议人工通读后再定。"
    else:
        verdict = "pass"
        reason = "规则与 AI 复核均未发现问题。规则只做提示，最终以招标文件为准。"
    return {"summary": summary, "verdict": verdict, "reason": reason}


AI_SYSTEM = (
    "你是评标专家，只做一件最苛刻的事：找出投标文件会被判无效或明显丢分的地方。"
    "禁止泛泛而谈，每条问题必须指向具体章节与原文片段。只输出 JSON。"
)

# 逐章复核：一次只送一章 + 与它最相关的招标节选，避免长标书被整段截断后只看开头
AI_CHAPTER_TEMPLATE = """对照招标要求，审查投标文件【{title}】这一章的实质性问题。

输出 JSON：{{"issues": [{{"level": "高|中|低", "title": "问题标题", "detail": "为什么会被扣分或废标", "evidence": "本章原文片段（30 字内）"}}]}}

只看这四类：响应缺失、负偏离未说明、承诺不可验证、与招标文件硬性要求冲突。最多 5 条，没有就返回空数组。

招标要求（与本章最相关的节选）：
{tender}

本章正文（节选）：
{draft}
"""

REVIEW_MAX_CHAPTERS = 16      # 单次复核最多送的章数，超出部分如实标注「仅规则体检」
REVIEW_PER_CHAPTER = 4000     # 每章正文送审上限
REVIEW_TENDER_EACH = 4500     # 每章匹配的招标原文上限
REVIEW_CONCURRENCY = 4        # 并发请求数，避免把模型接口打满
REVIEW_MAX_ISSUES = 20


def _parse_issues(data: dict, prefix: str = "") -> list[dict]:
    if not isinstance(data, dict):
        return []
    items = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        title = str(item["title"])[:60]
        items.append({
            "code": "",
            "level": item.get("level") if item.get("level") in ("高", "中", "低") else "中",
            "title": f"【{prefix}】{title}" if prefix else title,
            "detail": str(item.get("detail", ""))[:300],
            "evidence": str(item.get("evidence", ""))[:180],
        })
    return items


def _pick_tender(pool: list[str], query: str, limit: int, take: int = 2) -> str:
    """按章节主题挑最相关的招标片段，长标书也不用整本塞进一次请求。"""
    if not pool:
        return ""
    ranked = sorted(pool, key=lambda c: _similarity(query, c[:1200]), reverse=True)
    out: list[str] = []
    used = 0
    for chunk in ranked[:take]:
        piece = chunk[: max(0, limit - used)]
        if piece.strip():
            out.append(piece.strip())
            used += len(piece)
        if used >= limit:
            break
    return "\n\n".join(out)


async def _review_chapters(cfg: LLMConfig, tender_text: str,
                           sections: list[dict]) -> tuple[list[dict], str]:
    """逐章复核：每章一章正文 + 最相关招标节选，全书的章都能覆盖到。"""
    from ... import guard, rules
    from . import tender_sections

    usable = [s for s in sections
              if str(s.get("text") or "").strip()
              and not s.get("protected")
              and not rules.is_protected(str(s.get("title") or ""))]
    if not usable:
        return [], "没有可复核的正文（只有保护章节或空章）"
    try:
        chunks, _ = tender_sections.plan(tender_text or "", REVIEW_TENDER_EACH)
    except Exception:  # noqa: BLE001 — 切章失败就退化成不匹配招标节选
        chunks = []
    pool = [str(c.get("text") or "") for c in chunks][:60]
    targets = usable[:REVIEW_MAX_CHAPTERS]
    skipped = len(usable) - len(targets)
    sem = asyncio.Semaphore(REVIEW_CONCURRENCY)
    failed: list[str] = []

    async def one(sec: dict) -> list[dict]:
        title = str(sec.get("title") or "未命名章节")
        body = guard.scrub(sec.get("text"), limit=REVIEW_PER_CHAPTER)
        rel = _pick_tender(pool, f"{title}\n{body[:600]}", REVIEW_TENDER_EACH)
        prompt = AI_CHAPTER_TEMPLATE.format(
            title=title, tender=guard.scrub(rel, limit=REVIEW_TENDER_EACH), draft=body)
        async with sem:
            try:
                raw = await chat(
                    cfg,
                    [{"role": "system", "content": AI_SYSTEM},
                     {"role": "user", "content": prompt}],
                    json_mode=True, max_tokens=1200, task="extract",
                )
                data = parse_json(raw)
            except Exception:  # noqa: BLE001 — 单章失败不影响其它章
                failed.append(title)
                return []
        return _parse_issues(data, title)[:5]

    batches = await asyncio.gather(*[one(s) for s in targets])
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for batch in batches:
        for item in batch:
            key = (item["title"], item["evidence"][:40])
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    items = items[:REVIEW_MAX_ISSUES]
    for index, item in enumerate(items, 1):
        item["code"] = f"AI{index:02d}"
    notice = []
    if skipped > 0:
        notice.append(f"已复核 {len(targets)}/{len(usable)} 章，其余 {skipped} 章仅做规则体检")
    if failed:
        notice.append(f"{len(failed)} 章模型复核失败：{'、'.join(failed[:3])}")
    return items, "；".join(notice)


async def ai_review(cfg: LLMConfig, tender_text: str,
                    sections: list[dict]) -> tuple[list[dict], str]:
    if not cfg.ready:
        return [], "未配置 AI，已跳过模型复核"
    return await _review_chapters(cfg, tender_text, sections)
