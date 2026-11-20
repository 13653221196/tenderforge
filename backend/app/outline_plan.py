"""按招标文件的投标文件格式与评分办法展开目录，并给每章分配篇幅预算。

改之前 make_outline 只让模型出 6—8 章、每章 3 个要点，既不看招标文件要求的
投标文件组成，也不看评分项分值。真实标书常按「商务 / 技术 / 资格 / 报价」
分册、几十个章节；分值 40 分的技术项和 5 分的格式项拿同样的篇幅显然不合理。

这里不重造能力，把基座里已有的三块接进主链路：
  tender_sections.split_chapters 找招标文件「投标文件格式」那一章的目录；
  outline_import.annotate_scores 把评分项标到章节上；
  layout.pages_for_words          把目标字数换算成预计页数供界面核对。

模型仍负责给骨架补要点，但不再由它凭空决定「这本书有几章」。
本模块只算目录，不写库、不调模型。
"""
from __future__ import annotations

import re

from . import section_types
from .basecore.services import layout, outline_import, tender_sections

# 「一、投标函」「2. 技术规格响应表」这类目录行；表格行（含 |）一律排除
TITLE_LINE = re.compile(
    r"^\s*(?:[一二三四五六七八九十]{1,3}[、.．]|\d{1,2}[、.．]"
    r"|[（(][一二三四五六七八九十\d]{1,3}[）)])\s*"
    r"([\u4e00-\u9fa5][^\n|]{1,38})\s*$")
# 只有像「文件 / 表 / 函 / 书 / 清单 / 方案」这类才是投标文件组成项
SECTION_WORDS = ("函", "表", "书", "文件", "证明", "承诺", "授权", "清单",
                 "方案", "响应", "资格", "报价", "说明", "资料", "偏离", "偏差",
                 "业绩", "证书", "格式", "投标", "技术", "商务", "服务", "报告",
                 "措施", "规划", "能力", "凭证", "应答")
NOISE = ("第", "页", "共", "注", "见", "附", "盖章", "签字", "份", "元", "我方", "我公司")
SENTENCE_MARKS = "。；;，,！!：:"
FIRST_PERSON = ("我方", "我公司", "本公司", "本投标人", "投标人")
FORMAT_CHAPTER_WORDS = ("投标文件格式", "投标文件组成", "投标文件的组成", "投标文件")
# 表格目录的序号列：一 / （一） / 1
ROW_NO = re.compile(r"^(?:[一二三四五六七八九十]{1,3}"
                    r"|[（(][一二三四五六七八九十\d]{1,3}[）)]|\d{1,2})$")
VOLUME_NO = re.compile(r"^[一二三四五六七八九十]{1,3}$")
MAX_CHAPTERS = 32
MAX_FORMAT_TITLES = 14
MAX_SCORING_CHAPTERS = 12

# 招标文件模板里常把年份 / 单位名留成「XX年财务信息审核结果凭证单」这类占位，
# 原样搬进目录会逼用户逐章改名。生成阶段就剥掉占位前缀，只留可读主体。
_TITLE_PLACEHOLDER = re.compile(
    r"(?:20)?[Xx×＊*]{2,6}年?|【[Xx×＊*]{1,6}】|\[[Xx×＊*]{1,6}\]")


def _strip_placeholder(s: str) -> str:
    return _TITLE_PLACEHOLDER.sub("", s).strip(" 　:：、.．·-")


def _clean_title(raw: str) -> str:
    s = re.sub(r"\s+", "", str(raw or "")).strip(" 　:：、.．")
    s = re.sub(r"[（(].{0,12}[）)]$", "", s)
    s = _strip_placeholder(s)
    return s[:44] if 2 <= len(s) <= 44 else ""


def _clean_cell(raw: str) -> str:
    """表格目录里的一格：去掉容量/条件括注与引号，留下文件名称。"""
    s = str(raw or "").strip().strip('"“” 　')
    s = re.sub(r"（（[^）]*））", "", s)          # 「（（每包容量为30M））」
    s = re.sub(r"[（(][^）)]{0,30}(?:每包容量|容量为)[^）)]{0,30}[）)]", "", s)
    s = re.sub(r"[（(](?:如有|仅[^）]{0,36})[）)]", "", s)
    s = re.sub(r"\s+", "", s).strip(" 　:：、.．\"“”'")
    s = _strip_placeholder(s)
    if not (3 <= len(s) <= 40) or any(ch in s for ch in SENTENCE_MARKS):
        return ""
    return s


def format_titles(tender_text: str, *, limit: int = MAX_FORMAT_TITLES) -> list[dict]:
    """从招标文件「投标文件格式」章抽投标文件组成，返回 [{title, volume, no}]。

    国网等真实招标文件的组成清单是表格（「序号 | 内容 | 上传维度 | …」），
    第一列的一/二/三/四是分册，（一）/1 是具体文件；以前按纯文本编号行抓，
    结果把投标函正文里的「我方承诺…」当成章节。这里优先解析表格，解析不到
    才回退到编号行，并且严格排除第一人称与整句。
    """
    text = str(tender_text or "")
    if not text.strip():
        return []
    try:
        chapters = tender_sections.split_chapters(text)
    except Exception:
        return []
    body = "\n".join(
        c.get("body", "")[:24000] for c in chapters
        if any(k in str(c.get("title") or "") for k in FORMAT_CHAPTER_WORDS))
    if not body.strip():
        return []
    lines = body.splitlines()[:1600]
    table: list[dict] = []
    volume = ""
    for raw in lines:
        if "|" not in raw:
            continue
        cells = [x.strip() for x in raw.split("|")]
        if len(cells) < 2:
            continue
        no, name = cells[0], _clean_cell(cells[1])
        if not ROW_NO.match(no) or not name:
            continue
        if VOLUME_NO.match(no):
            # 「一 | 价格文件」是分册名，只作为后续条目的归属，不单列成章
            volume = name
            continue
        if not any(k in name for k in SECTION_WORDS):
            continue
        if any(name.startswith(n) for n in NOISE):
            continue
        if name not in [x["title"] for x in table]:
            table.append({"title": name, "volume": volume, "no": no})
    if len(table) >= 3:
        # 分册轮询：真实招标文件的组成清单常把商务文件列十几项，顺序截断会让
        # 技术文件 / 资质业绩文件一条都进不来。每个分册轮流取，保证各册都有代表。
        groups: dict[str, list[dict]] = {}
        for x in table:
            groups.setdefault(x["volume"] or "其他", []).append(x)
        order = list(groups)
        picked: list[dict] = []
        for r in range(max(len(g) for g in groups.values())):
            for v in order:
                if r < len(groups[v]) and len(picked) < limit:
                    picked.append(groups[v][r])
            if len(picked) >= limit:
                break
        return picked

    out: list[dict] = []
    for raw in lines:
        if "|" in raw or len(raw.strip()) > 60:
            continue
        m = TITLE_LINE.match(raw)
        if not m:
            continue
        t = _clean_title(m.group(1))
        if not t or not any(k in t for k in SECTION_WORDS):
            continue
        if any(t.startswith(n) for n in NOISE) or any(t.startswith(n) for n in FIRST_PERSON):
            continue
        if any(ch in t for ch in SENTENCE_MARKS):
            continue
        if t not in [x["title"] for x in out]:
            out.append({"title": t, "volume": "", "no": ""})
        if len(out) >= limit:
            break
    return out


def normalize_scoring(meta: dict) -> list[dict]:
    """解读结果里的评分项兼容字符串 / 字典两种写法，分值只认能解析的数字。"""
    rows = (meta or {}).get("scoring") or []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, str):
            item, score, rule = row.strip(), 0.0, ""
        elif isinstance(row, dict):
            item = str(row.get("item") or row.get("name") or "").strip()
            score = _score(row.get("score") or row.get("value") or 0)
            rule = str(row.get("rule") or row.get("note") or "").strip()
        else:
            continue
        if len(item) < 2:
            continue
        out.append({"item": item[:44], "score": score, "rule": rule[:160]})
    seen: set[str] = set()
    uniq = []
    for r in out:
        if r["item"] not in seen:
            seen.add(r["item"])
            uniq.append(r)
    return uniq


def _score(value) -> float:
    try:
        m = re.search(r"\d+(?:\.\d+)?", str(value))
        return float(m.group(0)) if m else 0.0
    except (TypeError, ValueError):
        return 0.0


def _scoring_name(raw) -> str:
    """评分项名称清洗：去掉分值括注、序号与表格残留，只留要素名。"""
    s = re.sub(r"[（(][^）)]{0,44}[）)]", "", str(raw or ""))
    s = re.sub(r"^[一二三四五六七八九十\d]{1,3}\s*[、.．)）]\s*", "", s.strip())
    s = re.sub(r"\s+", "", s).strip(" :：、|·-\"'“”【】")
    return s[:30] if 2 <= len(s) <= 30 else ""


def merge_scoring(meta: dict, extra: list[dict] | None = None, *,
                  limit: int = MAX_SCORING_CHAPTERS) -> list[dict]:
    """把「要求表里的评分项」并进解读的 scoring，再按分值排优先级。

    真实试点里模型解读只给出 2 个评分项，而要求表按评分办法抽出了 20+ 项；
    只靠解读展开目录，绝大部分得分点没有独立章节认领。要求表是人工可修订的
    唯一事实源，同名的评分项以要求表的分值为准（人工修订优先于模型解读）。
    """
    rows: list[dict] = []
    index: dict[str, dict] = {}
    for r in normalize_scoring(meta):
        item = _scoring_name(r["item"])
        if not item:
            continue
        row = {**r, "item": item, "source": "interpret"}
        index[_key(item)] = row
        rows.append(row)
    for r in extra or []:
        if not isinstance(r, dict):
            continue
        item = _scoring_name(r.get("item") or r.get("text") or r.get("name"))
        score = _score(r.get("score") or r.get("value") or 0)
        if not item or not (0.5 <= score <= 100):
            continue
        k = _key(item)
        hit = index.get(k)
        if hit:
            # 同名评分项：人工修订过的分值优先，但章节仍只建一个
            if score > hit["score"]:
                hit["score"] = score
                hit["source"] = "requirements"
            continue
        row = {"item": item, "score": score,
               "rule": str(r.get("rule") or r.get("note") or "")[:160],
               "source": "requirements"}
        index[k] = row
        rows.append(row)
    # 分值高的先占章节配额；分值相同按名称稳定排序，避免每次生成目录都在抖
    rows.sort(key=lambda r: (-r["score"], r["item"]))
    return rows[:max(1, limit)]


def _key(title: str) -> str:
    return re.sub(r"[\s·、（）()第章节分]", "", str(title or "")).lower()


# 「投标函及投标报价总说明」与「投标函及附件」这种重复不是子串包含，
# 靠 _key 去不掉；改取标题开头主题词归并。只认完全相等的主题，配一张
# 显式同义词表，宁可少合并也不把「技术方案 / 技术规格」这类不同章并掉。
_TOPIC_ALIAS = {"资格证明": "资格", "资格文件": "资格", "资格材料": "资格"}
_GENERIC_LEAD = ("投标人", "投标单位", "单位", "我公司", "我方", "本次")


def _lead_topic(title: str) -> str:
    s = re.sub(r"^评分响应\s*[·•:：-]?\s*", "", str(title or ""))
    s = re.split(r"[及与和、，,·：:（）()\[\]／/]", s)[0]
    for g in _GENERIC_LEAD:
        if s.startswith(g) and len(s) > len(g):
            s = s[len(g):]
            break
    s = s.strip(" 　:：、.．·-")
    return _TOPIC_ALIAS.get(s, s)[:6]


def _topic_dup(topic: str, topics: list[str]) -> str:
    """返回与之重复的在册主题。
    - 完全相等：直接算重复（「资格」=「资格」这类 2 字词也要合并）；
    - 前缀包含（售后服务 vs 售后服务承诺）：较短一方须≥3 字，
      避免「技术」把「技术方案 / 技术规格」都误并掉。
    """
    if not topic:
        return ""
    for old in topics:
        if topic == old:
            return old
        short, long = (topic, old) if len(topic) <= len(old) else (old, topic)
        if len(short) >= 3 and (long.startswith(short) or short.startswith(long)):
            return old
    return ""


def _merge(base: list[dict], additions: list[dict], cap: int,
           merged_out: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    keys: set[str] = set()
    topics: list[str] = []
    for ch in list(base) + list(additions):
        t = str(ch.get("title") or "").strip()
        if not t:
            continue
        k = _key(t)
        topic = _lead_topic(t)
        dup_key = (not k or k in keys or
                   any(k in x or x in k for x in keys if len(x) > 5 and len(k) > 5))
        dup_topic = _topic_dup(topic, topics)
        item = dict(ch)
        item["title"] = t[:60]
        if dup_key or dup_topic:
            # 保护章（投标函 / 资格证明等）优先留住：若后来这章是证据型、
            # 而先前同主题章不是，用它替换，不能让保护要求被普通章吞掉。
            new_protected = section_types.classify({"title": t}) == section_types.PROTECTED
            replaced = ""
            if dup_topic and not dup_key and new_protected:
                for idx, old in enumerate(out):
                    if _lead_topic(str(old.get("title") or "")) != dup_topic:
                        continue
                    if section_types.classify({"title": str(old.get("title") or "")}) != section_types.PROTECTED:
                        replaced = str(old.get("title") or "")
                        out[idx] = item
                        if k:
                            keys.add(k)
                        break
            # 只记「主题词相同导致合并」的，纯子串去重是同义标题，不必提示
            if dup_topic and not dup_key and merged_out is not None:
                merged_out.append(replaced or t)
            continue
        if k:
            keys.add(k)
        if topic:
            topics.append(topic)
        out.append(item)
        if len(out) >= cap:
            break
    return out


def _allocate(chapters: list[dict], scoring: list[dict]) -> dict:
    """按分值 + 章节性质分配目标字数；保护章节不参与篇幅预算。"""
    score_by_name = {r["item"]: r["score"] for r in scoring}
    weights: list[float] = []
    for ch in chapters:
        t = str(ch.get("title") or "")
        kind = section_types.classify({"title": t})
        if kind == section_types.PROTECTED:
            weights.append(0.0)
            continue
        w = 1.0
        if any(k in t for k in ("技术方案", "技术规格", "逐条响应", "实施方案", "评分")):
            w = 1.6
        elif any(k in t for k in ("资格", "商务", "符合性", "承诺")):
            w = 0.85
        if kind == section_types.STRUCTURED:
            w *= 0.7
        linked = ch.get("scores") or []
        w *= 1.0 + min(1.2, sum(score_by_name.get(x, 0.0) for x in linked) / 40.0)
        weights.append(w)
    n = max(1, len([x for x in weights if x > 0]))
    # 真实投标文件按章计通常 2—3 万字（技术标更高），单章预算上限不再压到 3000；
    # 超出单次模型输出长度的章节由 agent.generate_section 分段续写完成。
    total = max(1800, 2400 * n)
    wsum = sum(weights)
    words: list[int] = []
    for w in weights:
        if w <= 0:
            words.append(0)
            continue
        v = total * w / (wsum or 1.0)
        words.append(int(round(max(1000, min(6000, v)) / 100.0) * 100))
    for ch, n_words in zip(chapters, words):
        ch["words"] = n_words
        ch["kind"] = section_types.classify({"title": ch.get("title") or ""})
    return {"total_words": sum(words),
            "estimated_pages": layout.pages_for_words(sum(words)) if sum(words) else 0.0}


def expand(base: list[dict], tender_text: str, meta: dict,
           *, max_chapters: int = MAX_CHAPTERS,
           extra_scoring: list[dict] | None = None) -> tuple[list[dict], dict]:
    """把模型骨架展开成「格式目录 + 评分项 + 骨架」的章节表。

    任何一步失败都退回 base，绝不让目录生成因解析异常而停摆。
    返回 (章节列表, 计划说明)，章节额外带 scores / words / kind 字段。
    """
    base_ch = [{"title": str(c.get("title") or ""),
                "points": list(c.get("points") or []) if isinstance(
                    c.get("points"), list) else [str(c.get("points") or "")]}
               for c in (base or []) if c.get("title")]
    if not base_ch:
        return [], {"used_scoring": 0, "used_format": 0, "reason": "骨架为空"}
    try:
        all_scoring = merge_scoring(meta, extra_scoring, limit=1000)
        scoring = all_scoring[:MAX_SCORING_CHAPTERS]
        fmt = format_titles(tender_text)
        # 投标文件的章节顺序要贴合评分办法：技术方案的细分项紧跟在骨架的
        # 技术章之后，格式/资格类组成项放末尾，不把商务章挤到技术册中间。
        tech_pos = next((i for i, c in enumerate(base_ch)
                         if "技术" in str(c.get("title") or "")), len(base_ch) - 1)
        for i, c in enumerate(base_ch):
            c["_order"] = i * 10
        additions: list[dict] = []
        # 评分项优先：分值高的技术项各自成章，篇幅与要求在生成时逐条对应
        for j, row in enumerate(scoring):
            name = row["item"]
            # 统一用「评分响应·」前缀：既让页面一眼看出这是得分点章节，又不会
            # 因为要素名里带「报价 / 资质」被分型误判成证据型保护章节。
            title = name if name.startswith("评分响应") else f"评分响应·{name}"
            pts = [p for p in (row["rule"], f"响应评分项「{name}」（{row['score']:g} 分）") if p][:3]
            additions.append({"title": title[:60], "points": pts,
                              "source": "scoring", "score": row["score"],
                              "_order": (tech_pos + 1) * 10 - 5 + j / 100})
        # 招标文件要求的投标文件组成，补齐资格 / 商务 / 格式类章节；
        # 同一个要素已在评分项里单独成章时不再重复列一章（真实目录里常见）。
        score_keys = {_key(r["item"]) for r in scoring}
        for j, row in enumerate(fmt):
            if _key(row.get("title") or "") in score_keys:
                continue
            additions.append({"title": row.get("title") or "", "points": [],
                              "volume": row.get("volume") or "",
                              "source": "tender_format", "_order": 1000 + j})
        merged_topics: list[str] = []
        merged = _merge(base_ch, additions, max_chapters, merged_topics)
        merged.sort(key=lambda c: float(c.get("_order") or 0))
        for c in merged:
            c.pop("_order", None)
        # 用基座确定性匹配给章节标注评分项，宁可漏标也不硬塞
        nodes = [{"title": c["title"], "children": []} for c in merged]
        # 标注 / 篇幅预算用全量评分项，保证没单独建章的得分点也能挂到相关章节
        mark = outline_import.annotate_scores(nodes, all_scoring)
        for c, node in zip(merged, nodes):
            c["scores"] = node.get("scores") or []
        plan = _allocate(merged, all_scoring)
        plan.update({
            "used_scoring": len(scoring),
            "used_format": sum(1 for a in additions
                               if a.get("source") == "tender_format"),
            "scoring_items": len(all_scoring),
            "scoring_from_requirements": sum(
                1 for r in all_scoring if r.get("source") == "requirements"),
            "unmatched_scoring": mark.get("unmatched") or [],
            "chapters": len(merged),
            "merged_topics": merged_topics[:20],
            "reason": ("按评分办法与投标文件格式展开"
                       if all_scoring or fmt else "招标文件未提供可用评分项 / 格式目录，沿用骨架"),
        })
        return merged, plan
    except Exception as exc:
        for c in base_ch:
            c.setdefault("scores", [])
            c.setdefault("words", 0)
            c.setdefault("kind", section_types.classify({"title": c["title"]}))
        return base_ch, {"used_scoring": 0, "used_format": 0,
                         "reason": f"展开失败，沿用骨架：{type(exc).__name__}"}
