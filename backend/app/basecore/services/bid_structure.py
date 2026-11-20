"""把一份已经写好的标书还原成项目结构：目录树 + 每节正文。

和相邻模块的分工要分清，否则很容易又走回"先生成大纲"的老路：
- outline_import：用户只给了**目录**，正文还要 AI 写；
- import_bid：**已经**有 AI 大纲，只想把旧稿内容按标题相似度回填进那些章节；
- bid_structure（本模块）：用户交上来的是**成品标书**，章节结构和正文都照他的来——
  不生成大纲、不匹配、不重写，正文一个字都不动。

解析要点（都是真实标书里踩过的坑）：
1. 目录页必须跳过：Word 的目录段落带 TOC/目录 样式，另有一批是"第一章 投标函……1"
   的引导点+页码文本，两者都要滤掉，否则整份标书的章节会翻倍。
2. 层级优先看编号语义（第X章 > 第X节 > 一、 >（一）> 1.1 > 1.1.1），编号认不出来
   才退回 Word 标题样式；两者都没有才看"短行+加粗/居中/大字号"。
3. 层级不跳级、封顶 3 级：1 级下面直接出现 3 级时压成 2 级，否则导出 Word 目录会错乱。
4. 表格不能丢：投标文件里报价表、偏离表、业绩表全是表格，按行拼成文本并进所属章节。
5. 封面不能当章节：正文前那段"投标文件/项目名称/投标人/日期"是封面，字数少就丢弃。
"""
from __future__ import annotations

import io as _io
import re
from collections import Counter
from pathlib import Path

from . import cleaner
from .outline_import import annotate_scores

MAX_LEVEL = 3
MAX_TITLE = 60
TITLE_MAX = 40

CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*[章篇部]")
SECTION_RE = re.compile(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*节")
CN_RE = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]\s*\S")
PAREN_RE = re.compile(r"^[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]\s*\S")
D2_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}")
D1_RE = re.compile(r"^\d{1,2}\.\d{1,2}(?!\.\d)")
ARABIC_RE = re.compile(r"^\d{1,2}\s*[.、）)]\s*\S")

HEAD_STYLE_RE = re.compile(r"(?:heading|标题)\s*([1-6])", re.I)
TOC_STYLE_RE = re.compile(r"(?:^|[\s_])toc(?:\s|\d|$)|目录", re.I)
TOC_LINE_RE = re.compile(r"[.．·…]{3,}\s*[0-9ivxIVX]{1,5}\s*$")
PAGE_ONLY_RE = re.compile(r"^[-—\s]*\d{1,4}[-—\s]*$")
END_PUNCT = tuple("。；，！？：、”』》）】")
TOC_TITLES = {"目录", "目 录", "contents", "CONTENTS"}


def _dominant(para) -> tuple[float, bool]:
    """段落主字号与是否加粗（以最长 run 为准）。"""
    size, bold, best = 0.0, False, 0
    for r in para.runs:
        text = r.text or ""
        if not text.strip():
            continue
        if len(text) > best:
            best = len(text)
            size = r.font.size.pt if r.font.size else 0.0
            bold = bool(r.font.bold)
    return size, bold


def _body_size(doc) -> float:
    """正文字号：按文字量加权取出现最多的字号。"""
    sizes: dict[float, int] = {}
    for para in doc.paragraphs:
        for r in para.runs:
            if (r.text or "").strip() and r.font.size:
                pt = round(r.font.size.pt, 1)
                sizes[pt] = sizes.get(pt, 0) + len(r.text)
    return max(sizes, key=sizes.get) if sizes else 0.0


def _iter_body(doc):
    """按文档顺序产出段落与表格（doc.paragraphs 会漏掉表格，报价表就丢了）。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "p", Paragraph(child, doc)
        elif tag == "tbl":
            yield "tbl", Table(child, doc)


def _classify(text: str, style: str, body_size: float, size: float, bold: bool, centered: bool) -> tuple[str, int]:
    """返回 (类型, 样式层级)。类型 body/toc 之外的都算标题候选。"""
    if TOC_STYLE_RE.search(style or ""):
        return "toc", 0
    if _is_toc_title(text) or TOC_LINE_RE.search(text):
        return "toc", 0
    if PAGE_ONLY_RE.match(text):
        return "toc", 0

    style_level = 0
    m = HEAD_STYLE_RE.search(style or "")
    if m:
        style_level = max(1, min(MAX_LEVEL, int(m.group(1))))

    short = len(text) <= MAX_TITLE and not text.rstrip().endswith(END_PUNCT)
    if CHAPTER_RE.match(text) and len(text) <= MAX_TITLE:
        return "chapter", style_level
    if SECTION_RE.match(text) and len(text) <= MAX_TITLE:
        return "section", style_level
    if CN_RE.match(text) and short:
        return "cn", style_level
    if PAREN_RE.match(text) and short:
        return "paren", style_level
    if D2_RE.match(text) and len(text) <= TITLE_MAX + 10:
        return "d2", style_level
    if D1_RE.match(text) and len(text) <= TITLE_MAX + 10:
        return "d1", style_level
    # "1. xxx" 在正文里绝大多数是编号条目，只有带标题样式时才敢当标题
    if ARABIC_RE.match(text) and short and style_level:
        return "arabic", style_level
    if style_level and short and len(text) <= TITLE_MAX:
        return "heading", style_level
    bigger = body_size > 0 and size >= body_size * 1.18
    if short and len(text) <= 30 and (bold or centered or bigger) and not ARABIC_RE.match(text):
        return "look", 0
    return "body", 0


def _level_table(kinds: Counter) -> dict[str, int]:
    """按文档自己的编号体系决定各类标记对应几级标题。"""
    has_ch = kinds["chapter"] >= 1
    has_sec = kinds["section"] >= 1
    has_cn = kinds["cn"] >= 2
    has_d1 = kinds["d1"] >= 2
    table: dict[str, int] = {}
    if has_ch:
        table.update(chapter=1, section=2)
        table["cn"] = 3 if has_sec else 2
        table["paren"] = 3
        table["d1"] = 3 if has_sec else 2
        table["d2"] = 3
    elif has_cn:
        # 没有"第X章"，那"一、"就是最高一级（很多公司范本这样写）
        table.update(cn=1, paren=2, d1=2, d2=3, chapter=1, section=1)
    elif has_d1:
        table.update(d1=1, d2=2, paren=2, cn=1, chapter=1, section=1)
    else:
        table.update(chapter=1, section=1, cn=1, paren=1, d1=1, d2=1)
    return table


def _clean_title(text: str) -> str:
    text = TOC_LINE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" 　.·…-—_~")
    return text[:MAX_TITLE]


def _is_toc_title(text: str) -> bool:
    return re.sub(r"\s+", "", text or "").lower() in ("目录", "contents")


def _assemble(blocks: list[dict]) -> dict:
    """把顺序块组装成章节树，块里的 body 归到当前章节正文。"""
    kinds = Counter(b["kind"] for b in blocks if b["kind"] != "body")
    table = _level_table(kinds)
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []
    cover: list[str] = []
    cover_heads: list[str] = []
    title = ""
    marker_seen = False
    dropped = 0

    def push(level: int, text: str) -> dict:
        while stack and stack[-1][0] >= level:
            stack.pop()
        # 不跳级：上一级是 1 时直接来 3 级，压成 2 级
        if stack:
            level = min(level, stack[-1][0] + 1)
        else:
            level = 1  # 没有父章的一律是 1 级，避免整棵树起点漂到 2 级
        node = {"title": text, "level": level, "brief": "", "scores": [], "content": "", "children": []}
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((level, node))
        return node

    numbered_kinds = ("chapter", "section", "cn", "paren", "d1", "d2")
    has_marker = any(b["kind"] in numbered_kinds for b in blocks)

    for b in blocks:
        kind, text = b["kind"], b["text"]
        if kind == "body":
            if stack:
                cur = stack[-1][1]
                cur["content"] = (cur["content"] + "\n" + text).strip() if cur["content"] else text
            else:
                cover.append(text)
            continue
        if kind in numbered_kinds:
            marker_seen = True
        elif has_marker and not marker_seen and kind in ("look", "look_title"):
            # 第一个带编号的章节之前那些"居中大字"是封面（项目名/投标文件），不是章节
            cover.append(text)
            cover_heads.append(text)
            continue
        level = max(1, min(MAX_LEVEL, table.get(kind) or b.get("style_level") or 2))
        clean = _clean_title(text)
        if not clean or _is_toc_title(clean):
            dropped += 1
            continue
        node = push(level, clean)
        node["_kind"] = kind

    if not title and cover_heads:
        # 封面第一行是项目名，第二行常是"投标文件"，合起来当文档名；
        # 但"投 标 人：xxx"这类字段行不能拼进标题
        parts = [cover_heads[0]]
        for head in cover_heads[1:3]:
            if re.match(r"^(投标文件|响应文件|投标书|报价文件|技术标|商务标|磋商文件|谈判文件)", head):
                parts.append(head)
                break
        title = " ".join(p for p in parts if p)[:80]
    if not title and roots:
        title = roots[0]["title"]

    pre_text = "\n".join(cover).strip()
    pre_words = cleaner.count_words(pre_text)
    if pre_words >= 120:
        # 正文前有整段没编号的内容（如"投标函"直接写正文），单列一节而不是丢掉
        roots.insert(0, {
            "title": "前言", "level": 1, "brief": "", "scores": [],
            "content": pre_text, "children": [],
        })
    elif pre_words:
        dropped += 1

    for node in roots:
        _finish(node)

    flat = _flatten(roots)
    return {
        "chapters": roots,
        "title": title,
        "count": len(flat),
        "levels": max((n["level"] for n in flat), default=0),
        "words": sum(n["words"] for n in flat),
        "skipped": dropped,
        "preamble_words": pre_words,
    }


def _finish(node: dict) -> None:
    node["content"] = re.sub(r"\n{3,}", "\n\n", (node.get("content") or "").strip())
    node["words"] = cleaner.count_words(node["content"])
    for child in node.get("children") or []:
        _finish(child)


def _flatten(nodes: list[dict], level: int = 1) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.append({
            "title": node["title"],
            "level": level,
            "brief": node.get("brief") or "",
            "scores": list(node.get("scores") or []),
            "content": node.get("content") or "",
            "words": node.get("words") or 0,
        })
        out.extend(_flatten(node.get("children") or [], level + 1))
    return out


def parse_docx(path) -> dict:
    """解析 docx 成品标书：返回章节树 + 每节正文。

    从内存字节流打开（不是直接用文件路径）：Windows 上 python-docx 会把文件句柄占到
    垃圾回收为止，紧接着删临时文件必然 WinError 32 —— 上传接口会 500。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document(_io.BytesIO(Path(str(path)).read_bytes()))
    body_size = _body_size(doc)
    blocks: list[dict] = []
    tables = 0
    for kind, obj in _iter_body(doc):
        if kind == "tbl":
            rows = []
            for row in obj.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append({"kind": "body", "text": "\n".join(rows)})
                tables += 1
            continue
        text = (obj.text or "").strip()
        if not text:
            continue
        style = (obj.style.name or "") if obj.style is not None else ""
        size, bold = _dominant(obj)
        role, style_level = _classify(
            text, style, body_size, size, bold, obj.alignment == WD_ALIGN_PARAGRAPH.CENTER
        )
        if role == "toc":
            continue
        blocks.append({"kind": role, "text": text, "style_level": style_level})

    result = _assemble(blocks)
    result["tables"] = tables
    result["source"] = "docx"
    return result


def parse_text(text: str) -> dict:
    """解析粘贴进来的正文/大纲文本（没有 Word 样式，只能靠编号与缩进）。"""
    blocks: list[dict] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t\u3000"))
        role, style_level = _classify(stripped, "", 0.0, 0.0, False, False)
        if role == "toc":
            continue
        item = {"kind": "body" if indent >= 4 and role in ("look", "heading") else role,
                "text": stripped, "style_level": style_level}
        blocks.append(item)
    result = _assemble(blocks)
    result["tables"] = 0
    result["source"] = "text"
    return result


def to_chapters(parsed: dict, scoring: list[dict] | None = None) -> dict:
    """把解析结果整理成可落库的章节树，并按评分表标注对应关系。"""
    chapters = parsed["chapters"]
    marked = annotate_scores(chapters, scoring or [])
    return {
        "chapters": chapters,
        "flat": _flatten(chapters),
        "count": parsed["count"],
        "levels": parsed["levels"],
        "words": parsed["words"],
        "title": parsed.get("title") or "",
        "tables": parsed.get("tables") or 0,
        "marked": marked["marked"],
        "unmatched": marked["unmatched"],
    }
