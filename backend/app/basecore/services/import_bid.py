"""原方案还原（对标易标 originalPlanRestoration，clean-room 重写）。

导入已有投标文件 → 按标题切分 → 与当前大纲按标题相似度匹配 →
匹配上的章节回填原文（status=imported），匹配不上的整章追加。
原文只做确定性清洗（cleaner），不重写——需要润色时用已有的 refine/polish。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from docx import Document

from . import cleaner
from .restyle import detect, to_sections

THRESHOLD = 0.55  # 标题相似度匹配门槛


def _norm(text: str) -> str:
    return re.sub(r"[\s第章节篇一二三四五六七八九十百千0-9.．、（）()：:\-—]", "", text or "")


def parse_document(path: str | Path) -> dict:
    """解析 docx 为候选章节列表。"""
    doc = Document(str(path))
    found = detect(doc)
    sections = to_sections(found["blocks"])
    # 清洗每节正文，统计字数
    out: list[dict] = []
    for sec in sections:
        cleaned = cleaner.clean_text(sec.get("content") or "", title=sec.get("title") or "")
        out.append({
            "title": (sec.get("title") or "").strip(),
            "content": cleaned["text"],
            "words": cleaned["stats"]["words"],
        })
    out = [s for s in out if s["title"] or s["words"] > 20]
    # 文档标题行会被 detect 记成"前言"章——只是封面文字，不算正文，丢掉
    title = (found.get("title") or "").strip()
    out = [
        s for s in out
        if not (s["title"] == "前言" and (s["words"] < 40 or (title and s["content"].strip() == title)))
    ]
    return {"title": title, "stats": found["stats"], "sections": out}


def match(
    imported: list[dict],
    existing: list[dict],
    threshold: float = THRESHOLD,
) -> dict:
    """把导入章节对到现有章节。

    返回 plan: [{action: fill|replace|append, sid?, title, import_title, words}]
    fill: 现有章节没正文 → 直接回填；replace: 现有章节有正文 → 导入稿字数更多才覆盖，否则 skip。
    """
    plan: list[dict] = []
    used: set[str] = set()
    for sec in imported:
        best: dict | None = None
        best_ratio = 0.0
        for es in existing:
            if es["id"] in used:
                continue
            ratio = SequenceMatcher(None, _norm(es["title"]), _norm(sec["title"])).ratio()
            if ratio > best_ratio:
                best, best_ratio = es, ratio
        if best is not None and best_ratio >= threshold:
            used.add(best["id"])
            current = best.get("content") or ""
            if not current.strip():
                plan.append({
                    "action": "fill", "sid": best["id"], "title": best["title"],
                    "import_title": sec["title"], "words": sec["words"],
                })
            elif sec["words"] > cleaner.count_words(current) * 1.1:
                plan.append({
                    "action": "replace", "sid": best["id"], "title": best["title"],
                    "import_title": sec["title"], "words": sec["words"],
                    "current_words": cleaner.count_words(current),
                })
            else:
                plan.append({
                    "action": "skip", "sid": best["id"], "title": best["title"],
                    "import_title": sec["title"], "words": sec["words"],
                    "current_words": cleaner.count_words(current),
                    "reason": "现有正文与导入稿相当，默认保留现有内容",
                })
        else:
            plan.append({
                "action": "append", "title": sec["title"] or "（未命名章节）",
                "import_title": sec["title"], "words": sec["words"],
            })
    return {"plan": plan}


def apply(plan: list[dict], imported: list[dict], pid: str, progress=None, checkpoint=None) -> dict:
    """按计划落库。返回统计。"""
    from .. import db

    by_title = {s["title"]: s for s in imported}
    stats = {"fill": 0, "replace": 0, "append": 0, "skip": 0}
    total = max(1, len(plan))
    max_ord = max((s["ord"] for s in db.list_sections(pid)), default=-1)
    for idx, item in enumerate(plan):
        if checkpoint:
            checkpoint()
        action = item["action"]
        stats[action] = stats.get(action, 0) + 1
        if action in ("fill", "replace"):
            sec = by_title.get(item.get("import_title") or item["title"]) or {}
            content = sec.get("content") or ""
            db.update_section(
                item["sid"], content=content, status="imported", words=sec.get("words") or 0,
            )
        elif action == "append":
            sec = by_title.get(item["title"]) or {}
            max_ord += 1
            db.insert_section(
                pid, ord=max_ord, level=1, title=item["title"], brief="（原方案导入）",
                content=sec.get("content") or "", status="imported", words=sec.get("words") or 0,
            )
        if progress:
            progress(int((idx + 1) / total * 100), f"回填 {item.get('title', '')[:20]}")
    return stats
