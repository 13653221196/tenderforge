"""偏离表 / 响应表生成：把需求单条目与本方正文逐条对照，预判偏离情况。

数据来源：meta.requirements.items（R-xxx 编号需求单）＋ 已写正文（db.list_sections）。
按条目类型拆成技术偏离表与商务偏离表；对每条需求在正文里查响应：
- 命中 → 无偏离（摘录正文**整句**，按句号对齐、剔掉表格残片）；
- 未命中但属否定型条款 → 无偏离（承诺不违反）；
- 未命中 → 交一轮 AI 复核（只对"未见响应"行做，成本可控）：
    复核确认正文确有响应 → 无偏离并注明所在章节，否则维持未见响应；
- 招标人 / 评标委员会义务、程序性条款（开标、澄清、公示）→ 不进正文表，
  单列在 excluded，避免把"不该写"误报成"写漏了"；
- 要求里带数量 / 年限（近三年、不少于 3 个）→ 正文缺对应数字时在说明里标"待核验"。

只做本地确定性判定；用户可在页面上改状态与响应文字后保存，再导出 Word / Excel / PDF。
结果存项目 meta.deviation（按表别分键）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .checker import _find, _keywords
from .requirements import clause_kind

# 表别 → 需求条目类型
KIND_TYPES: dict[str, set[str]] = {
    "技术": {"技术", "人员", "其他"},
    "商务": {"资质", "业绩", "财务", "商务", "保证金", "格式"},
}
KIND_TITLE = {"技术": "技术偏离表", "商务": "商务偏离表"}
HEADERS = ["序号", "招标要求", "投标响应", "偏离情况", "说明"]
STATUSES = ["无偏离", "正偏离", "负偏离", "未见响应", "招标人义务", "程序性条款", "不适用"]
# 非投标人条款：能判定但不应进表，单列供页面折叠展示
NON_BIDDER = ("招标人义务", "程序性条款", "不适用")
# 数量 / 年限类要求：至少 N 个、近 N 年——要在正文里核到"满足下限"的数字，
# 只比字面会误报（要求不少于 3 个、正文写 5 个，字面不同但实质满足）。
NUM_MIN_RE = re.compile(r"(?:不少于|不低于|至少)\s*([一二三四五六七八九十\d]{1,3})\s*([个项套份人台次])")
NUM_YEAR_RE = re.compile(r"近\s*([一二三四五六七八九十\d]{1,2})\s*年")
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(token: str) -> int | None:
    """把「3」「三」「十五」「二十」折成整数，认不出返回 None。"""
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    if token in _CN_DIGIT:
        return _CN_DIGIT[token]
    if "十" in token:
        head, _, tail = token.partition("十")
        return (_CN_DIGIT.get(head, 1) * 10) + (_CN_DIGIT.get(tail, 0) if tail else 0)
    return None


# 量化约束抽取：把「不少于 3 个」「不超过 200 万元」「5~10 米」「介于 4 至 8 之间」这类
# 要求抽成可比的上下限，用来对正文数值判正 / 负 / 无偏离；抽不到约束、或正文没有同单位
# 数值可比时，回退到旧的字符提示逻辑（_number_note），不擅自下结论。
_NUM_CHARS = "0-9零一二三四五六七八九十百千万亿两"
_NUM_PAT = "[" + _NUM_CHARS + r"]+(?:\.[0-9]+)?"
_UNIT_PAT = (
    r"(?:%|％|个|项|套|份|人|台|次|条|张|件|只|辆|栋|层|间|处|位|"
    r"个月|月|日|天|小时|分钟|平方米|立方米|千米|公里|厘米|毫米|米|"
    r"吨|公斤|kg|升|度|万元|万|亿|元|"
    r"MHz|GHz|MPa|kV|kW|kWh|Mbps|GB|TB|MB|A|V|Hz|W|dpi)"
)
_MIN_RE = re.compile(
    r"(?:不少于|不低于|不小于|至少|不得低于|不得少于|不得小于|[≥≧])\s*"
    r"(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")?"
)
_MAX_RE = re.compile(
    r"(?:不超过|不大于|不高于|不多于|最多|至多|不得高于|不得大于|不得多于|[≤≦])\s*"
    r"(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")?"
)
# 区间：单位可能挂在每个数字后面（20%和80%），也可能只在末尾（20~80%）
_RANGE_RE = re.compile(
    r"(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")?\s*(?:~|～|—|-|至|到)\s*"
    r"(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")"
)
_BETWEEN_RE = re.compile(
    r"介于\s*(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")?\s*(?:和|与|至|~|～|—|-|到)\s*"
    r"(" + _NUM_PAT + r")\s*(" + _UNIT_PAT + r")?\s*之间"
)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def _parse_num(token: str) -> float | None:
    """阿拉伯数字或中文数字转数值（支持到「亿」），认不出返回 None。"""
    token = (token or "").strip()
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        pass
    total = section = number = 0.0
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = 0.0
            else:
                section += (number or 1) * unit
            number = 0.0
        else:
            return None
    return total + section + number


def _constraints(text: str) -> list[tuple[str, list[float], str]]:
    """抽出 (kind, values, unit)。kind ∈ min / max / range。"""
    out: list[tuple[str, list[float], str]] = []
    text = text or ""
    for match in _BETWEEN_RE.finditer(text):
        lo, hi = _parse_num(match.group(1)), _parse_num(match.group(3))
        unit = (match.group(2) or match.group(4) or "").strip()
        if lo is not None and hi is not None:
            out.append(("range", [min(lo, hi), max(lo, hi)], unit))
    for match in _RANGE_RE.finditer(text):
        lo, hi = _parse_num(match.group(1)), _parse_num(match.group(3))
        unit = (match.group(2) or match.group(4) or "").strip()
        if lo is not None and hi is not None:
            out.append(("range", [min(lo, hi), max(lo, hi)], unit))
    for match in _MIN_RE.finditer(text):
        value = _parse_num(match.group(1))
        if value is not None:
            out.append(("min", [value], (match.group(2) or "").strip()))
    for match in _MAX_RE.finditer(text):
        value = _parse_num(match.group(1))
        if value is not None:
            out.append(("max", [value], (match.group(2) or "").strip()))
    return out


def _corpus_numbers(corpus: str, unit: str) -> list[float]:
    """正文里与给定单位同时出现的数值；无单位时取正文全部数值。"""
    corpus = corpus or ""
    if unit:
        pattern = re.compile(r"(" + _NUM_PAT + r")\s*" + re.escape(unit))
        found = [_parse_num(m.group(1)) for m in pattern.finditer(corpus)]
    else:
        found = [_parse_num(t) for t in re.findall(_NUM_PAT, corpus)]
    return [v for v in found if v is not None]


def _relevant_sentences(text: str, sentences: list[str]) -> list[str]:
    """和条款沾边的句子：按关键词命中筛，全不中时用二元组重合度兜底。

    数值判定只看这些句子——全文捞同单位数字会把"人工预算 200 万元、
    设备噪声 ≤60 分贝"这种不相干的数值也捞进来，造成误判。
    """
    keys = _keywords(text)
    if not keys:
        return []
    scored: list[tuple[int, str]] = []
    for sent in sentences:
        hit = sum(1 for k in keys if k in sent)
        if hit:
            scored.append((hit, sent))
    if not scored:
        grams = {text[i : i + 2] for i in range(max(0, len(text) - 1))}
        for sent in sentences:
            hit = sum(1 for g in grams if g in sent)
            if hit >= 3:
                scored.append((hit, sent))
    if not scored:
        return []
    best = max(h for h, _ in scored)
    keep = max(1, best // 2)
    return [s for h, s in scored if h >= keep]


def _constraint_verdict(text: str, corpus: str, sentences: list[str] | None = None) -> tuple[str, str] | None:
    """按量化约束比对正文数值判正/负/无偏离；判不了返回 None（交回旧逻辑）。

    数值只从与条款相关的句子里取（_relevant_sentences）；相关句里没有
    同单位数值就不下结论，宁缺毋滥。sentences=None 时（旧接口/单测）
    退回全文取数。
    """
    if sentences:
        relevant = _relevant_sentences(text, sentences)
    else:
        relevant = []
    fallback_all = not sentences
    for kind, values, unit in _constraints(text):
        if relevant:
            nums = _corpus_numbers("\n".join(relevant), unit)
        elif fallback_all:
            nums = _corpus_numbers(corpus, unit)
        else:
            nums = []
        if not nums:
            continue
        u = unit or ""
        if kind == "min":
            need = values[0]
            if any(v >= need for v in nums):
                return ("无偏离", "")
            return ("负偏离", f"要求不低于 {need:g}{u}，正文同单位数值均低于该下限")
        if kind == "max":
            need = values[0]
            if any(v <= need for v in nums):
                return ("无偏离", "")
            return ("负偏离", f"要求不超过 {need:g}{u}，正文同单位数值均超出上限")
        if kind == "range":
            lo, hi = values
            if any(lo <= v <= hi for v in nums):
                return ("无偏离", "")
            return ("负偏离", f"要求 {lo:g}~{hi:g}{u}，正文同单位数值均不在区间内")
    return None


# markdown 残渣：表格竖线、标题井号、加粗星号、行内代码
_MD_NOISE = re.compile(r"[|#*`>]+")
_SENT_SPLIT = re.compile(r"(?<=[。；！？!?])|\n+")


def _clean(text: str) -> str:
    """去掉 markdown 残渣与多余空白，得到可放进 Word 表格的一句话。"""
    text = _MD_NOISE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    # 行首的项目符号与序号（"- 不良行为处理"、"5. 我方承诺"）不是响应内容
    text = re.sub(r"^[-–—•·]?\s*(?:\d{1,2}[.、)）]\s*)?", "", text)
    return text.strip(" \t\r\n，,、；;：:")


def _sentences(corpus: str) -> list[str]:
    """按句号/换行切句：偏离表要写"整句"响应，不能从半句开始截。"""
    out: list[str] = []
    # markdown 表格的每个单元格先当独立句处理，否则整行会粘成一句残渣
    for piece in _SENT_SPLIT.split((corpus or "").replace("|", "；")):
        core = _clean(piece)
        # 需求编号开头的是表格行残片（"R-001 仅接受…… | 已取得……"），不是正文句子
        core = re.sub(r"^R-\d{2,4}\s*", "", core)
        # 太短的多半是标题碎片；〔待填〕是生成时留的空位，摘进响应栏等于把占位符抄给评委
        if len(core) < 10 or "待填" in core:
            continue
        if not re.search(r"[\u4e00-\u9fa5]", core):
            continue
        out.append(core)
    return out


# 承诺/响应性措辞：句子里带了这些，说明不是纯粹把条款抄一遍，而是真在表态
_COMMIT_RE = re.compile(
    r"满足|响应|承诺|符合|已|将|按|采用|执行|提供|达到|优于|不低于|不少于|不超过|不高于|同意|接受"
)


def _is_echo(sent: str, echo: str) -> bool:
    """判断这句是不是「把条款原样抄一遍」。

    只有"开头与条款一致、且多出来的部分没有任何表态"才算回显；
    "设备处理器主频不低于2.8GHz，满足要求"这种带承诺的句子是真响应，必须留。
    """
    if not echo:
        return False
    body = sent.rstrip("。；")
    if not body.startswith(echo[:12]):
        return False
    extra = body[len(echo) :] if body.startswith(echo) else body[len(echo[:12]) :]
    return not _COMMIT_RE.search(extra)


def _excerpt(sentences: list[str], text: str, span: int = 90) -> str:
    """挑一句最贴条款的正文原句，按字数截断——不跨句拼凑，不留表格残片。

    先按关键词打分；关键词全不中时用二元组重合度兜底（"仅接受资格预审合格申请人投标"
    和"我方已取得资格预审合格通知书"这种，整词永远对不上，但二元组能对上）。
    把条款原文抄了一遍、且没有任何表态的句子不算响应，跳过——否则响应栏会出现
    "招标要求"的同义重复；但抄完条款接着说"满足要求"的，属于有效响应，要保留。
    """
    keys = _keywords(text)
    echo = _clean(text).rstrip("。；")
    best, best_hit = "", 0
    for sent in sentences:
        if _is_echo(sent, echo):
            continue
        hit = sum(1 for k in keys if k in sent)
        if hit > best_hit:
            best, best_hit = sent, hit
    if not best and len(text) >= 4:
        grams = {text[i : i + 2] for i in range(len(text) - 1)}
        for sent in sentences:
            if _is_echo(sent, echo):
                continue
            hit = sum(1 for g in grams if g in sent)
            if hit >= 3 and hit > best_hit:
                best, best_hit = sent, hit
    if not best:
        return ""
    return best if len(best) <= span else best[:span].rstrip("，,、") + "…"


def _number_note(text: str, corpus: str, sentences: list[str] | None = None) -> str:
    """要求带数量/年限时核正文数字：核不到只提示待核验，不擅自判负偏离。

    有相关句时优先只看相关句的数字，全文无关数字不算数。
    """
    gaps: list[str] = []
    if sentences:
        relevant = _relevant_sentences(text, sentences)
        corpus = "\n".join(relevant) if relevant else ""
    numbers = [int(x) for x in re.findall(r"\d{1,4}", corpus)]
    for token, unit in NUM_MIN_RE.findall(text or ""):
        need = _to_int(token)
        if need is None:
            continue
        if not any(v >= need for v in numbers):
            gaps.append(f"不少于{need}{unit}")
    years = [_to_int(t) for t in NUM_YEAR_RE.findall(corpus)]
    for token in NUM_YEAR_RE.findall(text or ""):
        need = _to_int(token)
        if need is None:
            continue
        # 「近三年」和「近3年」都要认，比字面会漏掉中文数字写法
        if need not in years:
            gaps.append(f"近{need}年")
    if not gaps:
        return ""
    return f"要求含数量/年限（{'、'.join(dict.fromkeys(gaps))[:40]}），正文未见对应数字，需人工核验"


def _judge(item: dict, corpus: str, sentences: list[str]) -> dict:
    text = str(item.get("text") or "")
    # 分类每次重算、不吃需求单里存的旧 kind：需求单可能是老版本规则生成的，
    # 沿用旧值会让"投标文件提交时间"这类流程条款一直挂在未见响应里。
    kind = clause_kind(text)
    if kind == "owner":
        return {"status": "招标人义务", "response": "", "note": "招标人 / 评标委员会义务，投标文件无需响应"}
    if kind == "procedural":
        return {"status": "程序性条款", "response": "", "note": "开标 / 澄清 / 公示等流程规则，不属于偏离判定范围"}
    if _find(corpus, text):
        response = _excerpt(sentences, text) or "满足招标文件要求"
        verdict = _constraint_verdict(text, corpus, sentences)
        if verdict is not None:
            status, note = verdict
            if status == "负偏离":
                return {"status": "负偏离", "response": response, "note": note}
            return {"status": "无偏离", "response": response, "note": ""}
        return {"status": "无偏离", "response": response, "note": _number_note(text, corpus, sentences)}
    if kind == "prohibit":
        return {"status": "无偏离", "response": "承诺不违反该项要求", "note": "否定型条款，靠不违反满足"}
    return {"status": "未见响应", "response": "", "note": "正文未检出对应响应，请补充或人工确认"}


def _excluded_notice(excluded: list[dict]) -> str:
    """剔除清单的一句话说明，按状态分组计数，避免"剔除了 4 条"却看不出剔的是什么。"""
    parts = []
    for status in ("招标人义务", "程序性条款", "不适用"):
        n = sum(1 for e in excluded if e.get("status") == status)
        if n:
            parts.append(f"{status} {n}")
    return (f"已剔除 {len(excluded)} 条非投标人条款（{' · '.join(parts)}），"
            "这类条款投标文件无需响应，不计入偏离统计。")


def build(meta: dict, sections: list[dict], kind: str = "技术") -> dict:
    kind = kind if kind in KIND_TYPES else "技术"
    items = ((meta.get("requirements") or {}).get("items")) or []
    types = KIND_TYPES[kind]
    corpus = "".join((s.get("content") or "") for s in sections)
    sentences = _sentences(corpus)
    # 本项目声明不接受联合体时，联合体相关条款对本投标人不适用
    no_consortium = any(
        "联合体" in str(i.get("text") or "") and re.search(r"不接受|不允许|禁止", str(i.get("text") or ""))
        for i in items
    )

    rows: list[dict] = []
    excluded: list[dict] = []
    seq = 0
    for item in items:
        if item.get("hard") is False:
            continue  # 评分项属软需求，不进偏离表
        if (item.get("type") or "其他") not in types:
            continue
        text = str(item.get("text") or "")
        if len(text) < 6:
            continue
        verdict = _judge(item, corpus, sentences)
        if no_consortium and "联合体" in text and not re.search(r"不接受|不允许|禁止", text):
            verdict = {"status": "不适用", "response": "", "note": "本项目不接受联合体投标，本条对本投标人不适用"}
        if verdict["status"] in NON_BIDDER:
            # 招标人义务 / 流程条款：不是本投标人要响应的内容，不进正文表
            excluded.append({
                "code": item.get("code") or "",
                "requirement": text[:240],
                "status": verdict["status"],
                "note": verdict["note"],
            })
            continue
        seq += 1
        rows.append({
            "seq": seq,
            "code": item.get("code") or "",
            "requirement": text[:240],
            "type": item.get("type") or "其他",
            "response": verdict["response"],
            "status": verdict["status"],
            "note": verdict["note"],
        })

    stats = _stats(rows, excluded)
    notice = ""
    if not items:
        notice = "尚无需求单，请先在「招标解读」页生成需求单。"
    elif not rows:
        notice = f"需求单里没有属于「{kind}」的硬性条目，可切换表别或先补充需求单。"
    elif excluded:
        notice = _excluded_notice(excluded)
    return {
        "kind": kind,
        "title": KIND_TITLE[kind],
        "headers": HEADERS,
        "rows": rows,
        "excluded": excluded,
        "stats": stats,
        "notice": notice,
        "ai": "",
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


async def build_async(meta: dict, sections: list[dict], kind: str = "技术",
                      cfg=None, use_ai: bool = True) -> dict:
    """本地判定 + 一轮 AI 复核。

    关键词匹配只能判"字面有没有"，判不了"意思有没有"——比如要求"具有生产设备和
    检测能力"，正文用硫化机/泵料机/局放试验设备清单来响应，字面永远对不上。
    所以只把判成"未见响应"的行交模型复核，通常只有几条，成本可控；
    模型不可用（未配置 / 超时 / 解析失败）就退回本地结果，不影响出表。
    """
    result = build(meta, sections, kind)
    if not use_ai or cfg is None or not getattr(cfg, "ready", False):
        return result
    pending = [r for r in result["rows"] if r["status"] == "未见响应"]
    if not pending:
        return result
    try:
        verdicts = await _ai_check(cfg, pending, sections)
    except Exception as exc:  # noqa: BLE001 — AI 是增强项，失败不能拖垮出表
        result["ai"] = f"AI 复核未完成（{type(exc).__name__}），以上为本地判定结果"
        return result
    if not verdicts:
        result["ai"] = "AI 复核没有返回可用结果，以上为本地判定（可人工确认未见响应行）"
        return result

    changed = 0
    to_drop: list[str] = []
    for row in result["rows"]:
        got = verdicts.get(row.get("code") or "") or verdicts.get(f"#{row['seq']}")
        if not got:
            continue
        reason = str(got.get("reason") or "")[:80]
        if not got.get("responded"):
            # 模型看出是流程/格式类条款：不该留在偏离表里，转进剔除清单
            if "程序" in reason or "流程" in reason or "格式" in reason:
                to_drop.append(row["code"] or f"#{row['seq']}")
                result.setdefault("excluded", []).append({
                    "code": row.get("code") or "",
                    "requirement": row["requirement"],
                    "status": "程序性条款",
                    "note": f"AI 复核：{reason}",
                })
            elif reason:
                row["note"] = f"AI 复核：{reason}"
            continue
        row["status"] = "无偏离"
        quote = _clean(str(got.get("quote") or ""))[:90]
        where = str(got.get("section") or "").strip()
        row["response"] = quote or "满足招标文件要求"
        row["note"] = f"AI 复核命中{'「' + where + '」' if where else ''}，可人工复核后保留"
        changed += 1

    if to_drop:
        result["rows"] = [
            {**r, "seq": i} for i, r in enumerate(
                (r for r in result["rows"] if (r.get("code") or f"#{r['seq']}") not in to_drop), 1
            )
        ]
    result["stats"] = _stats(result["rows"], result.get("excluded") or [])
    if result.get("excluded"):
        result["notice"] = _excluded_notice(result["excluded"])
    detail = f"AI 复核 {len(pending)} 条未见响应，补正 {changed} 条"
    if to_drop:
        detail += f"，另识别 {len(to_drop)} 条流程条款并剔除"
    result["ai"] = detail
    return result


_AI_SYSTEM = (
    "你是投标文件响应核对专家。只判断正文是否实质响应了招标要求，"
    "不评价写得是否漂亮。只输出 JSON，不要解释、不要围栏。"
)
_AI_PROMPT = """判断下列招标要求是否已被投标文件正文**实质**响应。

判定口径：
- 正文有对应内容即算响应，允许同义表述、设备/证书/人员清单、方案描述；
- 只在承诺书里写"承诺满足/无偏离"而正文无实质内容的，算未响应；
- 格式、时限、开标、澄清这类程序性要求，不判响应（responded=false，reason 写"程序性条款"）。

要求（code + requirement）：
{reqs}

投标文件正文（按章节，标题在【】内，内容已截断）：
{doc}

只输出 JSON：
{{"items":[{{"code":"R-001","responded":true,"section":"4.3.1 硫化机","quote":"正文原句，最多30字","reason":"不超过20字"}}]}}
每条要求都要给出一条结果，顺序与输入一致。"""


def _digest(sections: list[dict], texts: list[str], per: int = 600, limit: int = 10) -> str:
    """按关键词重合度挑相关章节，拼成给模型看的正文摘要。

    关键词全不命中时不能直接返回空（商务条款短、措辞差异大，实测经常一条都命不中），
    退化成"全书摘要"：按章序每节取前 220 字，保证模型总能看到正文。
    """
    keys = {k for t in texts for k in _keywords(t)}
    scored: list[tuple[int, int, str, str]] = []
    for s in sections:
        body = (s.get("content") or "").strip()
        if not body:
            continue
        title = s.get("title") or ""
        score = sum(1 for k in keys if k in title + body)
        scored.append((score, int(s.get("ord") or 0), title, body))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    if scored[0][0] == 0:
        scored.sort(key=lambda x: x[1])
        return "\n".join(f"【{t}】{b[:220]}" for _, _, t, b in scored)
    return "\n".join(f"【{t}】{b[:per]}" for _, _, t, b in scored[:limit])


async def _ai_check(cfg, rows: list[dict], sections: list[dict]) -> dict:
    """对未见响应的行做一轮复核，返回 {code: {responded, section, quote, reason}}。"""
    from . import llm

    reqs = "\n".join(f"- {r.get('code') or '#' + str(r['seq'])}：{r['requirement'][:160]}" for r in rows)
    doc = _digest(sections, [r["requirement"] for r in rows])
    if not doc:
        return {}
    messages = [
        {"role": "system", "content": _AI_SYSTEM},
        {"role": "user", "content": _AI_PROMPT.format(reqs=reqs, doc=doc[:9000])},
    ]
    raw = await llm.chat(cfg, messages, json_mode=True, max_tokens=1600, task="extract")
    data = llm.parse_json(raw)
    out: dict[str, dict] = {}
    for got in data.get("items") or []:
        if isinstance(got, dict) and got.get("code"):
            out[str(got["code"])] = got
    return out


def _stats(rows: list[dict], excluded: list[dict] | None = None) -> dict:
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r.get("status") or "未见响应"] = counts.get(r.get("status") or "未见响应", 0) + 1
    return {
        "total": len(rows),
        "ok": counts.get("无偏离", 0) + counts.get("正偏离", 0),
        "deviation": counts.get("负偏离", 0),
        "missing": counts.get("未见响应", 0),
        "owner": counts.get("招标人义务", 0),
        "procedural": counts.get("程序性条款", 0),
        "na": counts.get("不适用", 0),
        "excluded": len(excluded or []),
    }


def apply_rows(result: dict, rows: list[dict]) -> dict:
    """保存用户在页面上编辑过的行，重算统计。"""
    cleaned: list[dict] = []
    for i, row in enumerate(rows or [], 1):
        status = str(row.get("status") or "未见响应")
        if status not in STATUSES:
            status = "未见响应"
        cleaned.append({
            "seq": i,
            "code": str(row.get("code") or ""),
            "requirement": str(row.get("requirement") or "")[:240],
            "type": str(row.get("type") or "其他"),
            "response": str(row.get("response") or "")[:240],
            "status": status,
            "note": str(row.get("note") or "")[:160],
        })
    out = dict(result)
    out["rows"] = cleaned
    out["stats"] = _stats(cleaned, result.get("excluded") or [])
    out["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return out


# ---------------- 导出 ----------------

def _rows(result: dict) -> list[list[object]]:
    return [
        [r["seq"], r["requirement"], r["response"], r["status"], r.get("note") or ""]
        for r in result.get("rows") or []
    ]


def template_rows(result: dict) -> list[dict]:
    """给模板回填用的字段化行，键与 deviation_template.COLUMN_KEYS 对齐。"""
    return [
        {
            "seq": r.get("seq"),
            "requirement": r.get("requirement") or "",
            "response": r.get("response") or "",
            "status": r.get("status") or "",
            "note": r.get("note") or "",
        }
        for r in result.get("rows") or []
    ]


def to_docx(out_path: Path, project_name: str, result: dict) -> Path:
    from . import table_export

    title = result.get("title") or "偏离表"
    return table_export.to_docx(
        out_path,
        project_name,
        title=title,
        headers=HEADERS,
        rows=_rows(result),
        doc_type=title,
    )


def to_pdf(out_path: Path, project_name: str, result: dict) -> Path:
    from . import table_export

    title = result.get("title") or "偏离表"
    return table_export.to_pdf(
        out_path,
        project_name,
        title=title,
        headers=HEADERS,
        rows=_rows(result),
        doc_type=title,
    )


def to_xlsx(out_path: Path, result: dict) -> Path:
    from . import table_export

    return table_export.write_xlsx(
        out_path,
        sheet_title=result.get("title") or "偏离表",
        headers=HEADERS,
        rows=_rows(result),
        widths=(6, 54, 40, 12, 26),
    )
