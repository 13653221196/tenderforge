"""按章构建写作上下文：让模型看到「与本章相关的招标原文」，而不是每章同一份文件开头。

背景：改之前 generate_section 把 ``tender_text[:6000]`` 原样塞给每一章。真实招标文件
常有几十万字，技术参数、评分办法、合同条款散在中后部，模型从头到尾只看得到开头的
公告与投标人须知；要求表虽然不再截断，逐章仍只挑 12 条按字面相关的条款。结果是
「一键写整本」出来的正文像通用模板，而不是对着这份招标文件逐条响应的标书。

本模块只负责「给什么上下文」，不负责生成、不写业务库、不调用模型：
  - 把招标原文按页标记 + 条款起始行切成带坐标的片段；
  - 用「章节标题 + 要点 + 本章相关要求」检索最相关的片段，硬性条款加权；
  - 如实写出检索覆盖范围，未注入的内容明确禁止臆造；
  - 顺带组装已核验企业事实与已写章节摘要，供跨章一致性使用。

招标文件短到能整份放进预算时直接全文注入，不做检索，避免小文件反而丢内容。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from . import emb_bridge, store

PAGE_RE = re.compile(r"【第\s*(\d+)\s*页】")
# 条款起始：第X条 / 1.2 / （一） / 一、 等，用于把被压成一大段的长文件拆开
CLAUSE_START = re.compile(
    r"(?m)^\s*(?:第[一二三四五六七八九十百零\d]+[条章节款]"
    r"|[（(][一二三四五六七八九十\d]+[）)]"
    r"|[一二三四五六七八九十]+[、]"
    r"|\d+(?:\.\d+){0,3}\s*[、.．]?\s)")
# 命中这些词的片段通常是要逐条响应的硬条款，检索时加权
MUST_WORDS = re.compile(
    r"必须|须|应|不得|不低于|不少于|至少|废标|否决|实质性|评分|分值|"
    r"有效期|保证金|资质|证书|截止|响应|承诺|加盖|签章|参数|验收")

TARGET_CHARS = 480          # 单个片段目标长度
DEFAULT_TENDER_BUDGET = 9000  # guard.scrub 单变量上限 12000，留出余量
DEFAULT_CLAUSES = 16
# 同一份招标文件在整本生成里会被切 N 次；按长度 + 首尾片段缓存，避免重复切几十万字
_SPLIT_CACHE: dict[tuple, list[dict]] = {}
# 章标题：给片段标注「出自招标文件哪一章」，用于章节归属加权与跨章多样性
CHAPTER_RE = re.compile(r"第[一二三四五六七八九十百\d]{1,4}\s*[章节部分]\s*[^\n|]{0,36}")
# 检索索引按全文缓存：片段 2-gram、文档频率、所属章，整本生成只算一次
_INDEX_CACHE: dict[tuple, tuple] = {}


def _text_key(text: str) -> tuple[int, str]:
    """缓存键用全文指纹，不用「长度 + 首尾片段」这种弱指纹。

    多企业同时投同一标段时，两家上传的招标文件常常模板相同、首尾公告相同、
    只在中段技术参数/包号不同，字符数也可能恰好一样。旧键会把 A 企业的切分
    结果直接给 B 企业，喂给模型的就是别家正文、页码坐标也全错。缓存本身是
    纯函数（文本 -> 切分），不含企业数据；按全文 sha256 命中既安全又照样省
    掉整本生成里几十万字的重复切分。
    """
    return (len(text), hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest())


def grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "").lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _ref(page: int, start: int, end: int) -> str:
    return (f"第 {page} 页·字符 {start}-{end}" if page
            else f"字符 {start}-{end}")


def split_clauses(text: str) -> list[dict]:
    """按【第 N 页】→ 空行段落 → 条款起始行三级切分，保留全文字符坐标。

    坐标用于证据回跳与「本段出自招标文件哪一处」的说明；切分只影响喂给模型的
    片段边界，不改动 p.tender_text 原文。
    """
    text = text or ""
    if not text.strip():
        return []
    key = _text_key(text)
    hit = _SPLIT_CACHE.get(key)
    if hit is not None:
        return hit
    marks = list(PAGE_RE.finditer(text))
    ranges: list[tuple[int, int, int]] = []
    if not marks:
        ranges.append((0, len(text), 0))
    else:
        prev = 0
        for i, m in enumerate(marks):
            # 首个页标记之前的正文同样要保留（公告 / 投标人须知常在这里）
            if m.start() > prev:
                ranges.append((prev, m.start(), 0))
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            ranges.append((m.end(), end, int(m.group(1))))
            prev = end
    out: list[dict] = []
    for s0, e0, page in ranges:
        body = text[s0:e0]
        pieces: list[tuple[int, str]] = []
        pos = s0
        for block in re.split(r"(\n\s*\n)", body):
            if block.strip():
                pieces.append((pos, block))
            pos += len(block)
        clauses: list[tuple[int, str]] = []
        for p0, block in pieces:
            starts = [m.start() for m in CLAUSE_START.finditer(block)]
            if not starts:
                starts = [0]
            elif starts[0] > 0:
                starts.insert(0, 0)
            for i, st in enumerate(starts):
                en = starts[i + 1] if i + 1 < len(starts) else len(block)
                seg = block[st:en].strip()
                if seg:
                    clauses.append((p0 + st, seg))
        buf: list[str] = []
        buf_start = 0
        for c0, seg in clauses:
            if not buf:
                buf_start = c0
            buf.append(seg)
            if len("\n".join(buf)) >= TARGET_CHARS:
                joined = "\n".join(buf)
                out.append({"start": buf_start, "end": buf_start + len(joined),
                            "text": joined, "page": page,
                            "ref": _ref(page, buf_start, buf_start + len(joined))})
                buf = []
        if buf:
            joined = "\n".join(buf)
            out.append({"start": buf_start, "end": buf_start + len(joined),
                        "text": joined, "page": page,
                        "ref": _ref(page, buf_start, buf_start + len(joined))})
    result = [c for c in out if c["text"].strip()]
    if len(_SPLIT_CACHE) > 4:
        _SPLIT_CACHE.clear()
    _SPLIT_CACHE[key] = result
    return result


def _index(text: str) -> tuple[list[dict], list[set[str]], Counter, int]:
    """按全文缓存检索索引：片段 / 每片 2-gram / 文档频率 / 所属招标章节。"""
    key = _text_key(text)
    hit = _INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    clauses = split_clauses(text)
    chapter = ""
    for c in clauses:
        m = CHAPTER_RE.search(c["text"][:360])
        if m:
            chapter = re.sub(r"\s+", "", m.group(0))[:40]
        c["chapter"] = chapter
    dgs = [grams(c["text"][:1600]) for c in clauses]
    df: Counter = Counter()
    for gs in dgs:
        df.update(gs)
    result = (clauses, dgs, df, len(clauses))
    if len(_INDEX_CACHE) > 4:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = result
    return result


def semantic_for(pid: str, text: str, query: str, *,
                 budget: int = DEFAULT_TENDER_BUDGET,
                 top_k: int = 24) -> tuple[list[dict] | None, list[str] | None]:
    """构造招标原文的语义排名与片段键；只有开关打开且长到装不进预算时才调外部接口。

    短文件整份注入、开关关闭、接口失败一律返回 (None, None)，调用方走原词法路径。
    切片坐标与 split_clauses 的缓存顺序一一对应，正文不落向量表。
    """
    body = str(text or "")
    cap = max(800, int(budget or DEFAULT_TENDER_BUDGET))
    if not emb_bridge.ready("tender") or not body.strip() or len(body) <= cap:
        return None, None
    try:
        clauses = split_clauses(body)
        chunks = emb_bridge.chunks_from_clauses(clauses, pid)
        semantic = emb_bridge.rank_chunks("tender", chunks, query, pid=pid, top_k=top_k)
    except Exception:
        # 配置 / 向量库异常绝不打断写标书，静默退回纯词法
        return None, None
    if not semantic:
        return None, None
    return semantic, [str(c["chunk_key"]) for c in chunks]


def retrieve_for_chapter(tender_text: str, query: str = "", *,
                         budget: int = DEFAULT_TENDER_BUDGET,
                         k: int = DEFAULT_CLAUSES,
                         semantic: list[dict] | None = None,
                         chunk_keys: list[str] | None = None) -> tuple[str, dict]:
    """检索与本章最相关的招标原文片段；返回 (可注入提示词的文本, 覆盖信息)。

    真实招标文件里「评标办法 / 否决 / 前附表」会在几十个片段里反复出现，
    纯字面命中会让四个不同章节拿到同一批开头片段。这里改成 IDF 加权（高频
    词降权）、硬性条款按密度而非是否出现加权、片段所属招标章标题与本章主题
    相符时加权，再按「每章最多 3 段」做跨章多样性，保证上下文既有区分度
    又不集中在一处。零命中时跨章均匀抽样，不再退化为「取开头 16 段」。
    """
    text = str(tender_text or "")
    budget = max(800, int(budget or DEFAULT_TENDER_BUDGET))
    want = max(1, int(k or DEFAULT_CLAUSES))
    clauses, dgs, df, n_docs = _index(text)
    meta: dict[str, Any] = {
        "total_clauses": len(clauses), "selected": 0, "chars": len(text),
        "full": False, "budget": budget, "query_hit": False,
        "chapters": 0, "stop_grams": 0, "top_score": 0.0,
    }
    if not text.strip():
        meta["note"] = "【本章招标原文检索覆盖】未导入招标文件正文，不得臆造招标要求。"
        return "（未导入招标文件正文）", meta
    if len(text) <= budget:
        meta.update(full=True, selected=len(clauses),
                    note=f"【本章招标原文检索覆盖】招标文件已整份注入，共 {len(clauses)} 段；"
                         "请逐条响应其中与本章相关的条款。")
        return text, meta

    q_all = grams(query)
    # 出现在过半片段里的 2-gram 没有章节区分度，直接降权为零（不是删 query）
    stop = {g for g in q_all if df.get(g, 0) > 0.55 * max(1, n_docs)}
    q = q_all - stop
    meta["stop_grams"] = len(stop)
    idf = {g: math.log(1.0 + (n_docs - df.get(g, 0) + 0.5) /
                       (df.get(g, 0) + 0.5)) for g in q}
    scored: list[tuple[float, int, dict, int]] = []
    for idx, (c, gs) in enumerate(zip(clauses, dgs)):
        base = (sum(idf[g] for g in (q & gs)) / max(1.0, len(q) ** 0.5)
                if q else 0.0)
        density = len(MUST_WORDS.findall(c["text"][:800]))
        weight = 1.0 + min(0.5, density / 24.0)
        bonus = 1.35 if (grams(c.get("chapter") or "") & q) else 1.0
        scored.append((base * weight * bonus, idx, c, density))
    ranked = sorted(scored, key=lambda x: (-x[0], x[1]))
    hit = [x for x in ranked if x[0] > 0]
    sem_only_idx: set[int] = set()
    sem_rank: list[dict] = list(semantic or [])
    fused_score: dict[str, float] = {}
    if sem_rank and chunk_keys and len(chunk_keys) == len(clauses):
        key_of = {i: str(chunk_keys[i]) for i in range(len(clauses))}
        dense_keys = {str(r["chunk_key"]) for r in sem_rank}
        lex_keys = {key_of[x[1]] for x in ranked if x[0] > 0}
        sem_only_idx = {i for i, key in key_of.items()
                        if key in dense_keys and key not in lex_keys}
        if hit:
            # 词法名次（已含 IDF / 硬条款 / 章节归属加权）与语义名次按
            # chunk_key 做 RRF 融合；跨章多样性、每章 3 段、预算截断照旧。
            # 词法零命中时不重排，保留原有跨章均匀抽样兜底。
            lex_rows = [{"chunk_key": key_of[x[1]]} for x in ranked if x[0] > 0]
            dense_rows = [{"chunk_key": str(r["chunk_key"]), "score": r["score"]}
                          for r in sem_rank]
            fused = emb_bridge.fuse([lex_rows, dense_rows], top_k=len(clauses))
            fused_score = {str(r["chunk_key"]): float(r["score"]) for r in fused}
            ranked = sorted(ranked, key=lambda x: (-fused_score.get(key_of[x[1]], 0.0), x[1]))
            hit = [x for x in ranked
                   if x[0] > 0 or fused_score.get(key_of[x[1]], 0.0) > 0]
    meta["query_hit"] = bool(hit)
    meta["top_score"] = round(ranked[0][0], 3) if ranked else 0.0

    picked: list[tuple] = []
    if hit:
        per_ch: Counter = Counter()
        for x in hit:
            ch = x[2].get("chapter") or "_"
            if per_ch[ch] >= 3:
                continue
            picked.append(x)
            per_ch[ch] += 1
            if len(picked) >= want:
                break
        if len(picked) < min(3, want):
            for x in hit:
                if x not in picked:
                    picked.append(x)
                if len(picked) >= min(3, want):
                    break
    else:
        # 零命中：按硬词密度排序后跨章轮询，且明确标注这是抽样而非本章专属条款
        by_density = sorted(scored, key=lambda x: (-x[3], x[1]))
        groups: dict[str, list] = {}
        for x in by_density:
            groups.setdefault(x[2].get("chapter") or "_", []).append(x)
        order = list(groups)
        for r in range(want):
            for v in order:
                if r < len(groups[v]) and len(picked) < want:
                    picked.append(groups[v][r])
            if len(picked) >= want:
                break
    if sem_rank and chunk_keys and len(chunk_keys) == len(clauses):
        # 语义候选作为「补充」占位，最多 want/3 段：词法零命中时也不挤掉
        # 原有跨章抽样，避免开了向量反而只看得到一两段。
        idx_of_key = {str(chunk_keys[i]): i for i in range(len(clauses))}
        scored_by_idx = {x[1]: x for x in scored}
        have = {x[1] for x in picked}
        for r in sem_rank[:max(1, want // 3)]:
            i = idx_of_key.get(str(r["chunk_key"]))
            x = scored_by_idx.get(i)
            if x is not None and x[1] not in have:
                picked.insert(0, x)
                have.add(x[1])
        picked = picked[:max(1, want)]
    if sem_rank:
        meta["semantic_hits"] = len(sem_only_idx & {x[1] for x in picked})
    picked.sort(key=lambda x: x[1])  # 按原文顺序输出，保持条款上下文连贯
    meta["chapters"] = len({x[2].get("chapter") or "_" for x in picked})

    # 预算常常只装得下几段：先让高分片段占位，再按原文顺序输出，
    # 避免「按原文顺序截断」把本章最相关的后部条款挤掉。
    lines: list[tuple[int, str]] = []
    used = 0
    for _, idx, c, _ in sorted(picked, key=lambda x: (-x[0], x[1])):
        ch = c.get("chapter") or "未分章"
        mark = "【语义召回候选·须人工核对】\n" if idx in sem_only_idx else ""
        line = f"【招标文件·{ch}·{c['ref']}】\n{mark}{c['text']}"
        room = budget - used - 300  # 给末尾覆盖说明留位置
        if room <= 0:
            break
        if len(line) > room:
            line = line[:room] + "…[本片段按上下文预算截断]"
        lines.append((idx, line))
        used += len(line) + 2
    lines.sort(key=lambda x: x[0])  # 按原文顺序输出，保持条款上下文连贯
    meta["selected"] = len(lines)
    if not lines:
        c = clauses[0]
        lines = [(0, f"【招标文件·{c['ref']}·开头片段，本章相关条款未检索到】\n"
                     f"{c['text'][:budget - 300]}")]
        meta["selected"] = 1
    scope = ("按本章标题、要点与相关要求的相关度选取，高频通用词已降权，"
             "硬性/废标/评分条款按密度加权，并跨招标章节均衡取样"
             if hit else "未检索到本章专属条款，以下为跨章均匀抽样，"
                        "不代表招标文件里没有本章要求")
    meta["note"] = ("【本章招标原文检索覆盖】招标文件共 "
                    f"{len(clauses)} 段 / {len(text)} 字，本批注入 {meta['selected']} 段、"
                    f"覆盖 {meta['chapters']} 个招标章节；{scope}。"
                    "未注入的条款不得臆造其内容；本章要求涉及未展示条款时，以「本章相关要求」"
                    "条目和资料原文响应，缺依据的部分不要编数字、资质或承诺。")
    if meta.get("semantic_hits"):
        meta["note"] += (f" 本批另含 {meta['semantic_hits']} 段语义召回候选，"
                         "片段前已标注「须人工核对」；这类片段字面不命中本章检索词，"
                         "须回原文确认适用后方可引用，不得据此写数值、资质或承诺。")
    return "\n\n".join(x[1] for x in lines), meta


def facts_block(pid: str, *, limit: int = 1600) -> str:
    """已人工核验的企业事实；未核验的不进提示词，避免模型拿草稿当事实。"""
    try:
        rows = store.list_facts(pid)
    except Exception:
        return ""
    lines: list[str] = []
    used = 0
    for r in rows:
        if not str(r.get("verification") or "").startswith("verified"):
            continue
        entity = str(r.get("entity") or "").strip()
        field = str(r.get("field") or "").strip()
        value = str(r.get("value") or "").strip()
        unit = str(r.get("unit") or "").strip()
        if not value:
            continue
        label = "·".join(x for x in (entity, field) if x) or "企业事实"
        src = str(r.get("source_loc") or "").strip()
        line = f"- {label}：{value}{unit}" + (f"（来源：{src}）" if src else "")
        if used + len(line) > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return ("\n\n【已核验的企业事实，可直接引用，不得改写数值或扩大适用范围】\n"
            + "\n".join(lines))


def prev_block(brief: str = "", *, limit: int = 900) -> str:
    """已写章节摘要：让后续章节不重复展开、不改动前面定过的口径。"""
    s = str(brief or "").strip()
    if not s:
        return ""
    return ("\n\n【已写章节摘要（只用于保持全本一致，不要重复展开或改写这些章节的内容）】\n"
            + s[:limit])


def render_assignment(items: list[dict], total: int, chapter: str) -> str:
    """把「本章认领的要求」渲染成写作约束，并如实写出全表规模。"""
    if not items:
        return ""
    lines = []
    for it in items:
        kind = str(it.get("kind") or "hard")
        label = {"hard": "硬性", "rejection": "废标", "scoring": "评分"}.get(kind, kind)
        lines.append(f"- [{it.get('id') or ''}·{label}] {it.get('text') or ''}")
    return ("【本章认领的招标要求（全表共 " + str(total) + " 条，本章 "
            + str(len(items)) + " 条，须逐条明确响应；其余条目由其他章节响应，"
            "不要在本章重复展开，也不要臆造未展示的条款）】\n" + "\n".join(lines))


def assign_requirements(items: list[dict], titles: list[str],
                        *, per_chapter: int = 24) -> dict[str, list[dict]]:
    """把整份要求表分配到章节：每条要求至少被一章认领，归属明确的优先分。

    以前每章各自按字面相关度挑前 12 条，结果往往是所有章重复拿同一批靠前的
    要求，中后部条款谁都没看到。这里先给「与某一章明显更相关」的要求定位，
    再把归属模糊的补进尚有额度的章节；所有章都满时宁可超额也不丢条款。
    """
    buckets: dict[str, list[dict]] = {t: [] for t in titles}
    if not titles or not items:
        return buckets
    tg = {t: grams(t) for t in titles}
    ranked: list[tuple[float, int, dict, list[tuple[float, str]]]] = []
    for idx, it in enumerate(items):
        ig = grams(str(it.get("text") or ""))
        scores = sorted(
            ((len(tg[t] & ig) / max(1, len(tg[t])) if tg[t] else 0.0, t)
             for t in titles), key=lambda x: -x[0])
        gap = scores[0][0] - (scores[1][0] if len(scores) > 1 else 0.0)
        ranked.append((gap, idx, it, scores))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    for _, _, it, scores in ranked:
        for _, t in scores:
            if len(buckets[t]) < max(1, per_chapter):
                buckets[t].append(it)
                break
        else:
            buckets[scores[0][1]].append(it)
    return buckets


def assigned_requirements(pid: str, titles: list[str]) -> dict[str, str]:
    """取当前有效要求表并渲染成「章节标题 → 本章约束文本」。

    取不到要求表时返回空字典，调用方自动回退到原来的逐章 12 条逻辑，
    绝不因为分配失败就让整本生成停摆。
    """
    try:
        from . import reqtable
        items = reqtable.constraint_items(pid)
    except Exception:
        return {}
    if not items:
        return {}
    buckets = assign_requirements(items, titles)
    return {t: render_assignment(v, len(items), t) for t, v in buckets.items()}


def for_chapter(pid: str, project: dict, title: str, points: str = "",
                *, requirements: str = "", prev_brief: str = "",
                tender_budget: int = DEFAULT_TENDER_BUDGET) -> dict:
    """一站式组装本章写作上下文。agent 只调这里，不自己拼接截断字符串。"""
    query = " ".join(x for x in (str(title or ""), str(points or ""),
                                 str(requirements or "")[:600]) if x)
    raw_tender = project.get("tender_text") or ""
    semantic, chunk_keys = semantic_for(pid, raw_tender, query,
                                        budget=tender_budget)
    tender, cover = retrieve_for_chapter(raw_tender, query,
                                         budget=tender_budget,
                                         semantic=semantic,
                                         chunk_keys=chunk_keys)
    facts = facts_block(pid)
    prev = prev_block(prev_brief)
    # 单独成块：agent 把它拼在资料末尾，保证不会被前面的长资料截掉
    notes = "\n".join(x for x in (cover.get("note") or "", facts, prev) if x)
    return {
        "tender": tender,
        "coverage": cover,
        "facts": facts,
        "prev": prev,
        "notes": ("\n\n" + notes) if notes else "",
    }
