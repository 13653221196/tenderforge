"""版面预算：把「页数」翻译成「每节该写多少字」。

思路来自易标 layoutBudget（clean-room 重写，只保留数学关系，不拷代码）：
1. 一页能装多少字，完全由纸张、页边距、正文字号、行距决定，是可以算准的常量；
2. Word 的"单倍行距"行高约等于字号 × 1.3（中文常用字体的实测系数），据此推每页行数；
3. 正文要排标题、表格、图片，纯文字容量要打个折扣，默认按 82% 有效；
4. 页数分配到章节按"规模权重"走：技术方案类最重，投标函/报价表最轻；
5. 每节再拆成段落骨架（默认每段 230 字，90~420 之间），避免"一大坨"或"一句话一段"。
"""
from __future__ import annotations

import math
import re

CM_PER_INCH = 2.54
PT_PER_CM = 72 / CM_PER_INCH

PAPERS: dict[str, tuple[float, float]] = {
    "a3": (29.7, 42.0),
    "a4": (21.0, 29.7),
    "b5": (17.6, 25.0),
    "letter": (21.59, 27.94),
    "legal": (21.59, 35.56),
}

# 字号名 → 磅值（Word 中文字号）
SIZES: dict[str, float] = {
    "小五": 9,
    "五号": 10.5,
    "小四": 12,
    "四号": 14,
    "小三": 15,
    "三号": 16,
    "小二": 18,
    "二号": 22,
    "小一": 24,
    "一号": 26,
}

SINGLE_LINE_FACTOR = 1.3   # 单倍行高 ≈ 字号 × 1.3
FILL_RATIO = 0.82          # 标题、表格、图片占位后的有效文字比例

PARA_MIN = 90
PARA_MAX = 420
PARA_DEFAULT = 230

# 章节规模权重：标题里出现这些词就加权，技术方案类天然篇幅大
WEIGHT_RULES: list[tuple[str, float]] = [
    (r"技术方案|技术部分|实施方案|施工组织|设计方案|总体方案", 2.2),
    (r"项目理解|需求分析|重点难点|应对措施", 1.6),
    (r"进度|工期|质量|安全|环保|文明施工|保障措施", 1.2),
    (r"售后|服务|培训|运维|应急", 1.1),
    (r"业绩|案例|类似项目", 0.8),
    (r"人员|团队|组织|配置", 0.8),
    (r"资格|资质|证书|财务|信用", 0.6),
    (r"投标函|承诺书|声明|授权|格式", 0.35),
    (r"报价|价格|清单|分项", 0.5),
    (r"目录|封面|扉页", 0.15),
]

LEVEL_FACTOR = {1: 1.0, 2: 1.0, 3: 0.55, 4: 0.35}


def _size_pt(size: float | str) -> float:
    if isinstance(size, (int, float)):
        return float(size)
    return SIZES.get(str(size).strip(), 12.0)


def metrics(
    *,
    paper: str = "a4",
    landscape: bool = False,
    margin_top: float = 2.54,
    margin_bottom: float = 2.54,
    margin_left: float = 3.0,
    margin_right: float = 2.6,
    body_size: float | str = "小四",
    line_spacing: float = 1.5,
    indent_chars: int = 2,
) -> dict:
    """算出这页版面能装多少字。所有长度单位都是厘米。"""
    width, height = PAPERS.get(str(paper).lower(), PAPERS["a4"])
    if landscape:
        width, height = height, width

    content_w = max(1.0, width - margin_left - margin_right)
    content_h = max(1.0, height - margin_top - margin_bottom)

    size_pt = _size_pt(body_size)
    char_w = size_pt / PT_PER_CM
    line_h = char_w * SINGLE_LINE_FACTOR * max(1.0, float(line_spacing or 1.0))

    chars_per_line = max(1, int(content_w // char_w))
    lines_per_page = max(1, int(content_h // line_h))
    chars_per_page = chars_per_line * lines_per_page

    return {
        "paper": str(paper).lower(),
        "landscape": bool(landscape),
        "page_width_cm": round(width, 2),
        "page_height_cm": round(height, 2),
        "content_width_cm": round(content_w, 2),
        "content_height_cm": round(content_h, 2),
        "body_size_pt": size_pt,
        "line_height_cm": round(line_h, 3),
        "chars_per_line": chars_per_line,
        "lines_per_page": lines_per_page,
        "chars_per_page": chars_per_page,
        # 扣掉标题、表格、图片占位后的"可写正文字数"
        "effective_chars": int(chars_per_page * FILL_RATIO),
        "indent_chars": int(indent_chars or 0),
    }


def _title_weight(title: str) -> float:
    text = title or ""
    hits = [w for pat, w in WEIGHT_RULES if re.search(pat, text)]
    if not hits:
        return 1.0
    # 多条命中取加权平均，避免"技术方案保障措施"叠成 4 倍
    return round(sum(hits) / len(hits) + 0.15 * (len(hits) - 1), 3)


def _bounded_alloc(weights: list[float], total: int, floor: int, cap: int) -> list[int]:
    """按权重分配总字数，同时守住每节的下限与上限（注水法，多出的再分给未满的）。"""
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [max(floor, min(cap, total))] if total > 0 else [0]

    alloc = [0] * n
    remaining = max(0, total)
    active = [i for i in range(n) if weights[i] > 0] or list(range(n))
    for _ in range(6):
        if remaining <= 0 or not active:
            break
        wsum = sum(weights[i] for i in active) or float(len(active))
        progressed = False
        for i in list(active):
            add = int(remaining * (weights[i] if weights[i] > 0 else 1.0) / wsum)
            add = min(add, cap - alloc[i])
            if add > 0:
                alloc[i] += add
                remaining -= add
                progressed = True
            if alloc[i] >= cap:
                active.remove(i)
        if not progressed:
            break

    # 下限：从最富的章节里匀，匀不出来就保持原样（总字数本来就紧张）
    for i in range(n):
        if alloc[i] >= floor or weights[i] <= 0:
            continue
        need = floor - alloc[i]
        donors = sorted(range(n), key=lambda k: -alloc[k])
        for d in donors:
            if need <= 0:
                break
            spare = max(0, alloc[d] - floor)
            take = min(spare, need)
            if take > 0:
                alloc[d] -= take
                alloc[i] += take
                need -= take
    return alloc


def _group_chapters(sections: list[dict]) -> list[dict]:
    """按一级标题切章；没有一级标题时整本算一章。"""
    chapters: list[dict] = []
    current: dict | None = None
    for sec in sections:
        if int(sec.get("level") or 1) <= 1 or current is None:
            current = {"root": sec, "sections": []}
            chapters.append(current)
        current["sections"].append(sec)
    return chapters


def allocate(
    sections: list[dict],
    *,
    total_pages: int = 60,
    m: dict | None = None,
    min_words: int = 200,
    max_words: int = 0,
    fixed: dict[str, int] | None = None,
) -> dict:
    """把总页数分配到每节，返回每节目标字数与段落骨架。

    fixed: {section_id: words} 用户锁定的章节，不参与重新分配，但会占用页数。
    """
    m = m or metrics()
    fixed = fixed or {}
    total_pages = max(1, int(total_pages))
    effective = max(1, int(m["effective_chars"]))

    if not sections:
        return {"metrics": m, "chapters": [], "sections": [], "summary": {"pages": 0, "words": 0}}

    # 锁定章节先扣页数
    locked_words = sum(fixed.values())
    locked_pages = sum(max(1, round(v / effective)) for v in fixed.values()) if locked_words else 0
    remain_pages = max(1, total_pages - locked_pages)

    chapters = _group_chapters(sections)
    for ch in chapters:
        leaves = [s for s in ch["sections"] if int(s.get("level") or 1) >= 2] or [ch["root"]]
        ch["leaves"] = leaves
        root_w = _title_weight(ch["root"].get("title") or "")
        leaf_w = sum(_title_weight(x.get("title") or "") for x in leaves) / len(leaves)
        # 章标题与下属小节标题各占一半，再按小节数量微调（小节多说明内容确实多）
        ch["weight"] = round((root_w * 0.5 + leaf_w * 0.5) * (0.75 + 0.25 * math.log(1 + len(leaves))), 3)
    weight_sum = sum(ch["weight"] for ch in chapters) or 1.0

    plan_chapters: list[dict] = []
    plan_sections: list[dict] = []

    for ch in chapters:
        root = ch["root"]
        leaves = ch["leaves"]
        if root["id"] in fixed:
            pages = max(1, round(fixed[root["id"]] / effective))
        else:
            pages = max(1, round(remain_pages * ch["weight"] / weight_sum))
        plan_chapters.append(
            {
                "id": root["id"],
                "title": root.get("title") or "",
                "level": int(root.get("level") or 1),
                "weight": ch["weight"],
                "pages": pages,
            }
        )

        # 章内按标题权重与层级二次分配
        inner = []
        for sec in leaves:
            if sec["id"] in fixed:
                inner.append((sec, 0.0))
                continue
            inner.append(
                (
                    sec,
                    _title_weight(sec.get("title") or "")
                    * LEVEL_FACTOR.get(int(sec.get("level") or 1), 1.0),
                )
            )
        chapter_words = int(pages * effective)
        floor = max(min_words, int(effective * 0.15))
        # 单节上限随本章篇幅浮动：既防止一节吃掉整章，又不会把总字数卡住
        cap = int(max_words) if max_words else max(1500, int(chapter_words / max(1, len(inner)) * 2.2))
        alloc = _bounded_alloc([w for _, w in inner], chapter_words, floor, cap)

        for (sec, w), words in zip(inner, alloc):
            level = int(sec.get("level") or 1)
            if sec["id"] in fixed:
                words = int(fixed[sec["id"]])
            elif level == 1 and not (len(leaves) == 1 and sec["id"] == root["id"]):
                words = 0
            else:
                words = int(round(words / 50.0) * 50)
            plan_sections.append(
                {
                    "id": sec["id"],
                    "title": sec.get("title") or "",
                    "level": level,
                    "words": words,
                    "pages": round(words / effective, 2) if words else 0,
                    "skeleton": skeleton(words, m) if words else None,
                }
            )

    total_words = sum(s["words"] for s in plan_sections)
    return {
        "metrics": m,
        "summary": {
            "pages": total_pages,
            "allocated_pages": round(sum(c["pages"] for c in plan_chapters), 1),
            "words": total_words,
            "sections": len([s for s in plan_sections if s["words"]]),
            "chars_per_page": m["chars_per_page"],
            "effective_chars": effective,
        },
        "chapters": plan_chapters,
        "sections": plan_sections,
    }


def skeleton(words: int, m: dict | None = None) -> dict:
    """段落骨架：多少段、每段多少字、排几行、尾行是否太空。"""
    m = m or metrics()
    if words <= 0:
        return {"paragraphs": 0, "chars": 0, "lines": 0, "tail_lines": 0, "advice": "本节不写正文"}
    chars_per_line = max(1, int(m["chars_per_line"]))
    paras = max(1, round(words / PARA_DEFAULT))
    per = int(round(words / paras))
    if per > PARA_MAX:
        paras = max(1, round(words / PARA_MAX))
        per = int(round(words / paras))
    if per < PARA_MIN and words >= PARA_MIN:
        paras = max(1, round(words / PARA_MIN))
        per = int(round(words / paras))
    per = max(20, min(PARA_MAX, per))
    lines = max(1, -(-per // chars_per_line))
    # 最后一行只剩几个字会显得空，提示补一句
    tail = per - (lines - 1) * chars_per_line
    advice = ""
    if tail < chars_per_line * 0.25 and lines > 1:
        advice = f"末行仅 {tail} 字，建议补到 {chars_per_line} 字左右收尾"
    return {
        "paragraphs": paras,
        "chars": per,
        "lines": lines,
        "tail_lines": tail,
        "advice": advice,
    }


def pages_for_words(words: int, m: dict | None = None) -> float:
    m = m or metrics()
    return round(words / max(1, int(m["chars_per_page"])), 2)
