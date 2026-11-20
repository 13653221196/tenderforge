"""无样式文档智能排版：识别旧文档的标题层级，再按目标版式重排。

思路（启发式，不引入模型）：
1. 逐段收集特征——编号形态、字数、末尾标点、加粗、字号、对齐；
2. 编号规则优先：第X章/一、/（一）/1.1/1.1.1 各自映射到 1~3 级标题，
   1.  (1) ① 这类短编号是条目，不算标题；
3. 没编号时看外观：短行 + 无句末标点 + 加粗/居中/字号偏大 → 标题候选；
4. 组装成 sections（1 级标题分章、2/3 级进正文 markdown），复用 docx_export
   套目标版式输出——字体、行距、页边距、页码全部按格式预设走。
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH

from .docx_export import export_docx
from .formats import resolve

CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百千0-9]+\s*[章节篇]")
L1_RE = re.compile(r"^[一二三四五六七八九十]{1,3}\s*、\s*\S")
L2_CN_RE = re.compile(r"^[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]\s*\S")
L2_NUM_RE = re.compile(r"^\d{1,2}\.\d{1,2}(?!\.\d)[^0-9]")
L3_NUM_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}")
LIST_RE = re.compile(
    r"^\d{1,3}[\.、]\s*\S|^[(（]\s*\d{1,3}\s*[)）]\s*\S|^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]"
)
END_PUNCT = tuple("。；，！？：、”』》）]")

BODY_HINT_MAX = 60  # 编号行超过这个长度多半是正文而非标题


def _dominant_run(p) -> tuple[float, bool]:
    """段落的主字号与是否加粗（以最长 run 为准）。"""
    best = None
    best_len = 0
    for r in p.runs:
        if not (r.text or "").strip():
            continue
        size = r.font.size.pt if r.font.size else 0
        bold = bool(r.font.bold)
        if len(r.text) > best_len:
            best_len = len(r.text)
            best = (size, bold)
    return best or (0.0, False)


def classify(p, body_size: float) -> tuple[str, int]:
    """返回 (role, level)。role ∈ heading/body。"""
    text = (p.text or "").strip()
    if not text:
        return ("empty", 0)
    size, bold = _dominant_run(p)
    short = len(text) <= BODY_HINT_MAX and not text.rstrip().endswith(END_PUNCT)
    centered = p.alignment == WD_ALIGN_PARAGRAPH.CENTER

    if CHAPTER_RE.match(text) and short:
        return ("heading", 1)
    if L1_RE.match(text) and short:
        return ("heading", 1)
    if L3_NUM_RE.match(text) and len(text) <= 40:
        return ("heading", 3)
    if L2_NUM_RE.match(text) and len(text) <= 40:
        return ("heading", 2)
    if L2_CN_RE.match(text) and short:
        return ("heading", 2)
    # 无编号：靠外观判定
    bigger = body_size > 0 and size >= body_size * 1.15
    if short and (bold or centered or bigger) and len(text) <= 40 and not LIST_RE.match(text):
        return ("heading", 2)
    return ("body", 0)


def detect(doc: Document) -> dict:
    """扫描全文，返回 {title, blocks, stats}。blocks: [("h1"|"h2"|"h3"|"p", text)]"""
    # 估正文字号：出现最多的字号
    sizes: dict[float, int] = {}
    for p in doc.paragraphs:
        for r in p.runs:
            if (r.text or "").strip() and r.font.size:
                pt = round(r.font.size.pt, 1)
                sizes[pt] = sizes.get(pt, 0) + len(r.text)
    body_size = max(sizes, key=sizes.get) if sizes else 0.0

    blocks: list[tuple[str, str]] = []
    counts = {"h1": 0, "h2": 0, "h3": 0, "p": 0}
    doc_title = ""
    for p in doc.paragraphs:
        role, level = classify(p, body_size)
        text = (p.text or "").strip()
        if role == "empty":
            continue
        if not doc_title and not blocks and role == "heading" and len(text) <= 50:
            doc_title = text  # 文档最前面的标题行作为文件名，不当章节
            continue
        if role == "heading":
            blocks.append((f"h{level}", text))
            counts[f"h{level}"] += 1
        elif role == "body":
            if not doc_title and blocks == [] and 4 <= len(text) <= 50:
                doc_title = text  # 文档最前面的短行当文件名候选
                counts["p"] += 1
                blocks.append(("p", text))
            else:
                counts["p"] += 1
                blocks.append(("p", text))
    return {"title": doc_title, "blocks": blocks, "stats": {**counts, "body_size": body_size}}


def to_sections(blocks: list[tuple[str, str]]) -> list[dict]:
    """把扁平块组装成 export_docx 需要的 sections 结构。"""
    sections: list[dict] = []
    cur: dict | None = None
    lines: list[str] = []
    for kind, text in blocks:
        if kind == "h1":
            if cur:
                cur["content"] = "\n".join(lines)
                sections.append(cur)
            cur = {"title": text, "level": 1, "content": ""}
            lines = []
        else:
            if cur is None:
                # 标题前的内容（封面文字等）挂一个"前言"
                cur = {"title": "前言", "level": 1, "content": ""}
                lines = []
            if kind == "h2":
                lines.append(f"## {text}")
            elif kind == "h3":
                lines.append(f"### {text}")
            else:
                lines.append(text)
    if cur:
        cur["content"] = "\n".join(lines)
        sections.append(cur)
    return sections


def restyle(
    path: str | Path,
    out_path: str | Path,
    *,
    preset: str | None = None,
    override: dict | None = None,
    with_cover: bool = False,
    doc_type: str = "投标文件",
    blocks_override: list[tuple[str, str]] | None = None,
) -> dict:
    """识别旧文档结构并按目标版式重排输出。返回识别统计。

    blocks_override：AI 层级识别后的块列表，传入时跳过启发式结果。
    """
    if blocks_override is not None:
        blocks = blocks_override
        title = ""
        for kind, text in blocks[:4]:
            if kind.startswith("h") and len(text) <= 50:
                title = text
                break
        counts = {"h1": 0, "h2": 0, "h3": 0, "p": 0}
        for kind, _ in blocks:
            counts[kind] = counts.get(kind, 0) + 1
        found = {"title": title, "blocks": blocks, "stats": {**counts, "body_size": 0.0}}
    else:
        doc = Document(str(path))
        found = detect(doc)
    sections = to_sections(found["blocks"])
    fmt = resolve(preset, override)
    export_docx(
        out_path=Path(out_path),
        project_name=found["title"] or "文档",
        sections=sections,
        doc_type=doc_type,
        bidder="",
        date_text="",
        with_cover=with_cover,
        with_toc=True,
        fmt=fmt,
    )
    return {"stats": found["stats"], "title": found["title"], "sections": len(sections)}


# ---------------- AI 层级识别（启发式兜底不了的段落） ----------------
AI_SYSTEM = (
    "你是文档结构分析师。只输出 JSON。判断每个段落是标题还是正文："
    "h1=章级标题（如 第一章/一、）、h2=节级标题（如 1.1/（一）/短小标题）、h3=小节标题、body=正文。"
    "没有编号或加粗信息时，靠语义判断：完整句子、有句末标点、超过 30 字的一律是 body。"
)

AI_TEMPLATE = """判断下列文档段落各是标题还是正文。

{items}

输出 JSON：
{{"roles": [{{"i": 0, "role": "body"}}]}}
"""


def _heading_candidates(blocks: list[tuple[str, str]]) -> list[int]:
    """启发式判成正文、但外观像标题的段落序号（短、无句末标点、非列表）。"""
    out: list[int] = []
    for idx, (kind, text) in enumerate(blocks):
        if kind != "p":
            continue
        t = text.strip()
        if not t or len(t) > 40:
            continue
        if t.endswith(END_PUNCT):
            continue
        if LIST_RE.match(t):
            continue
        out.append(idx)
    return out


async def ai_classify(cfg, blocks: list[tuple[str, str]]) -> dict[int, str]:
    """AI 复核候选段落，返回 {块序号: "h1"|"h2"|"h3"|"body"}。"""
    from .llm import chat, parse_json

    if not getattr(cfg, "ready", False):
        return {}
    candidates = _heading_candidates(blocks)
    if not candidates:
        return {}
    result: dict[int, str] = {}
    for start in range(0, len(candidates), 60):
        batch = candidates[start:start + 60]
        items = "\n".join(f"- i={i}: {blocks[i][1][:44]}" for i in batch)
        try:
            raw = await chat(
                cfg,
                [{"role": "system", "content": AI_SYSTEM},
                 {"role": "user", "content": AI_TEMPLATE.format(items=items)}],
                json_mode=True,
                max_tokens=1500, task="polish",
            )
            data = parse_json(raw)
            for row in data.get("roles") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    i = int(row.get("i"))
                except (TypeError, ValueError):
                    continue
                role = str(row.get("role") or "").strip()
                if role in ("h1", "h2", "h3", "body") and i in batch:
                    result[i] = role
        except Exception:  # noqa: BLE001 — AI 失败就用启发式结果
            continue
    return result


def apply_ai_blocks(blocks: list[tuple[str, str]], roles: dict[int, str]) -> list[tuple[str, str]]:
    """把 AI 判定合并进块列表：body→h* 升级、h*→body 降级都允许（AI 看过全文语义）。"""
    if not roles:
        return blocks
    out: list[tuple[str, str]] = []
    for idx, (kind, text) in enumerate(blocks):
        role = roles.get(idx)
        if role and role != kind and role != "p":
            out.append((role, text))
        elif role == "body" and kind.startswith("h"):
            out.append(("p", text))
        else:
            out.append((kind, text))
    return out
