"""导入用户自带的大纲：把一份现成的目录文本解析成章节树。

为什么需要它：系统原先只能"AI 生成"或"标准模板"，很多单位本来就有固定的大纲
（公司范本、招标文件第六章的投标文件格式、往年标书），让他们照 AI 的目录重排是
本末倒置。这里不调模型，纯规则解析，用户给什么就用什么。

支持的常见写法（混着写也能认）：
    第一章 投标函 / 第1章 / 第一篇        → 1 级
    第一节 xxx / 一、xxx / 1. xxx        → 2 级
    （一）xxx / 1.1 xxx / ①xxx           → 3 级
    # Markdown 标题 / 缩进（2 空格或 Tab） → 按层级递进

三条保证：
1. 不跳级：1 级下面直接出现 3 级时压成 2 级，否则 Word 目录会错乱（检查器 C02/层级会报）。
2. 目录页不重复计入：开头的"目 录"以及带引导点和页码的行会被剔掉。
3. 有评分表时按标题自动标注 scores，评分覆盖面板对用户大纲同样有效。
"""
from __future__ import annotations

import io as _io
import re
from pathlib import Path

from .outline import _name_hit

MAX_NODES = 60  # 单层节点上限，防止粘贴整篇正文把库撑爆
MAX_TITLE = 60
MAX_LEVEL = 3

CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*[章篇部]")
SECTION_RE = re.compile(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*节")
CHAPTER_NO_RE = re.compile(r"^第\s*([一二三四五六七八九十百千零〇\d]+)\s*[章篇部]")
CN_NO_RE = re.compile(r"^([一二三四五六七八九十]+)\s*[、.．]")
PAREN_NO_RE = re.compile(r"^[（(]\s*([一二三四五六七八九十\d]+)\s*[）)]")
DOTTED_RE = re.compile(r"^(\d+(?:[.．]\d+)+)\s*[.．、]?\s*")
ARABIC_RE = re.compile(r"^(\d{1,2})\s*[、.．)）]\s*")
CIRCLED_RE = re.compile(r"^([①-⑳])")
MD_RE = re.compile(r"^(#{1,6})\s+")
BULLET_RE = re.compile(r"^[-*•·●▪]\s+")
# 目录页特征：引导点 + 页码，或纯页码
PAGE_DOT_RE = re.compile(r"[.．·…]{2,}\s*\d{1,4}\s*$")
TOC_HEAD_RE = re.compile(r"^目\s*录$|^contents$", re.I)

CN_DIGITS = "零一二三四五六七八九"


def _cn_to_int(text: str) -> int:
    """中文数字转整数，支持 一 / 十二 / 二十三；纯数字原样返回。"""
    text = (text or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    total, section = 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    for ch in text:
        if ch in CN_DIGITS:
            section = CN_DIGITS.index(ch)
        elif ch in units:
            total += (section or 1) * units[ch]
            section = 0
        else:
            return 0
    return total + section


def _indent_of(line: str) -> int:
    """把缩进折算成层数：Tab=1 级，2 空格=1 级，4 空格=2 级。"""
    lead = len(line) - len(line.lstrip(" \t\u3000"))
    if not lead:
        return 0
    raw = line[:lead]
    tabs = raw.count("\t")
    spaces = len(raw.replace("\t", ""))
    return min(MAX_LEVEL - 1, tabs + spaces // 2)


def _clean_title(text: str) -> str:
    text = MD_RE.sub("", text.strip())          # 去掉 Markdown 的 # 前缀
    text = PAGE_DOT_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" 　.·…-—_~")
    return text[:MAX_TITLE]


def _scheme(rows: list[str]) -> str:
    """判断这份大纲主用哪套编号，决定各级怎么对应。"""
    if any(CHAPTER_RE.match(r) or SECTION_RE.match(r) for r in rows):
        return "chapter"   # 第X章 / 第X节
    if any(DOTTED_RE.match(r) for r in rows):
        return "dotted"    # 1.1 / 1.1.1
    if any(CN_NO_RE.match(r) for r in rows):
        return "cn"        # 一、 / （一）
    return "flat"          # 只有短行或 Markdown


def _marker_level(line: str, scheme: str) -> tuple[int, bool]:
    """返回 (层级, 是否识别到编号标记)。"""
    stripped = line.lstrip(" \t\u3000")
    md = MD_RE.match(stripped)
    if md:
        return min(MAX_LEVEL, len(md.group(1))), True
    if CHAPTER_RE.match(stripped):
        return 1, True
    if SECTION_RE.match(stripped):
        return 2, True
    dotted = DOTTED_RE.match(stripped)
    if dotted:
        depth = len(re.split(r"[.．]", dotted.group(1)))
        if scheme == "chapter":
            return min(MAX_LEVEL, depth), True
        return min(MAX_LEVEL, depth), True
    if PAREN_NO_RE.match(stripped):
        return (3 if scheme in ("chapter", "dotted", "cn") else 2), True
    if CN_NO_RE.match(stripped):
        return (1 if scheme == "cn" else 2), True
    if CIRCLED_RE.match(stripped):
        return MAX_LEVEL, True
    if ARABIC_RE.match(stripped):
        return (1 if scheme == "dotted" else 2), True
    if BULLET_RE.match(stripped):
        return 0, False  # 项目符号当正文条目，不单独成章
    return 0, False


def _is_noise(line: str, prev_level: int) -> bool:
    text = line.strip()
    if not text:
        return True
    if TOC_HEAD_RE.match(text):
        return True
    # 目录行带引导点和页码（"第一章 投标函 …… 1"）是有效条目，页码由 _clean_title 剥掉，
    # 不能当噪声丢弃——否则从招标文件粘目录会一条都读不进来。
    if re.fullmatch(r"[.．·…\s]{3,}", text):
        return True
    if BULLET_RE.match(text):
        return True
    if re.fullmatch(r"[0-9０-９]+", text):
        return True
    # 太长的行多半是正文段落，不是标题；只在前一层不存在时放过（整篇只有一段的情况）
    if len(text) > MAX_TITLE and prev_level >= 0:
        return True
    return False


def parse(text: str) -> dict:
    """把大纲文本解析成 [{title, brief, scores, children}]，并附带解析报告。"""
    raw_lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    candidates = [ln for ln in raw_lines if ln.strip()]
    scheme = _scheme(candidates)

    nodes: list[dict] = []          # 扁平结果，带 level
    skipped = 0
    prev_level = 0
    prev_indent = 0
    for line in candidates:
        if _is_noise(line, prev_level):
            skipped += 1
            continue
        level, marked = _marker_level(line, scheme)
        indent = _indent_of(line)
        if not marked:
            # 没有编号：靠缩进判断，缩进没变化就跟上一条同级
            if indent > prev_indent:
                level = min(MAX_LEVEL, prev_level + 1)
            elif indent == prev_indent and prev_level:
                level = prev_level
            else:
                level = 1
        elif indent and level == 1 and scheme != "flat":
            # 明明有缩进却判成 1 级，说明是下级（如"1. 投标函"缩进两格）
            level = min(MAX_LEVEL, max(2, indent + 1))
        level = max(1, min(MAX_LEVEL, level))
        # 不跳级：上一级是 1 时直接来 3 级，压成 2 级
        if level > prev_level + 1:
            level = prev_level + 1
        title = _clean_title(line)
        if not title:
            skipped += 1
            continue
        nodes.append({"title": title, "level": level, "brief": "", "scores": []})
        prev_level, prev_indent = level, indent
        if len(nodes) >= MAX_NODES * MAX_LEVEL:
            break

    chapters = _nest(nodes)
    return {
        "chapters": chapters,
        "count": len(nodes),
        "levels": max((n["level"] for n in nodes), default=0),
        "skipped": skipped,
        "scheme": scheme,
    }


def _nest(flat: list[dict]) -> list[dict]:
    """把带 level 的扁平列表还原成 children 嵌套结构。"""
    roots: list[dict] = []
    stack: list[dict] = []

    def strip(node: dict) -> dict:
        return {"title": node["title"], "brief": node.get("brief", ""),
                "scores": list(node.get("scores") or []), "children": []}

    for item in flat:
        node = strip(item)
        level = item["level"]
        while stack and stack[-1]["_level"] >= level:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
            node["_level"] = level
            stack.append(node)
        else:
            if level != 1:
                # 首个节点就是 2/3 级：提升为 1 级，避免导出时没有父章
                node["_level"] = 1
                roots.append(node)
                stack = [node]
                continue
            node["_level"] = level
            roots.append(node)
            stack = [node]

    def clean(nodes: list[dict]) -> list[dict]:
        out = []
        for n in nodes[:MAX_NODES]:
            out.append({
                "title": n["title"],
                "brief": n.get("brief") or "",
                "scores": n.get("scores") or [],
                "children": clean(n.get("children") or []),
            })
        return out

    return clean(roots)


def annotate_scores(chapters: list[dict], scoring: list[dict]) -> dict:
    """按标题给节点标注响应的评分项，让评分覆盖面板对用户大纲也有效。

    只做"标题互相包含/2-gram 重合"的确定性匹配，不调模型——宁可漏标，
    也不要为了覆盖率好看硬塞一个牛头不对马嘴的对应关系。
    """
    names = [str(row.get("item") or "").strip() for row in scoring or [] if isinstance(row, dict)]
    names = [n for n in names if n]
    if not names:
        return {"marked": 0, "unmatched": []}

    marked = 0
    hit_names: set[str] = set()

    def walk(nodes: list[dict]) -> None:
        nonlocal marked
        for node in nodes:
            title = str(node.get("title") or "")
            matched = [n for n in names if _name_hit(n, title)][:4]
            if matched:
                node["scores"] = matched
                hit_names.update(matched)
                marked += 1
            walk(node.get("children") or [])

    walk(chapters)
    return {"marked": marked, "unmatched": [n for n in names if n not in hit_names]}


def docx_text(path) -> str:
    """读 docx 大纲，尽量保住层级信息。

    python-docx 的 paragraph.text 会丢掉缩进，但 Word 大纲的层级藏在两个地方：
    ① 段落样式（标题 1/2/3、Heading 1/2/3）；② 编号属性 numPr/ilvl。
    这里把它们翻译成前导空格，交给 parse() 按缩进识别，避免整份 WORD 目录被压成 1 级。
    """
    from docx import Document

    # 走内存字节流：Windows 下直接开路径会占住文件句柄，上传完删临时文件会报错
    doc = Document(_io.BytesIO(Path(str(path)).read_bytes()))
    lines: list[str] = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        level = 0
        style = (para.style.name or "") if para.style is not None else ""
        m = re.search(r"(?:Heading|标题)\s*([1-6])", style, re.I)
        if m:
            level = max(0, int(m.group(1)) - 1)
        ppr = getattr(para._p, "pPr", None)
        numpr = getattr(ppr, "numPr", None) if ppr is not None else None
        if numpr is not None:
            ilvl = numpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ilvl")
            if ilvl is not None:
                try:
                    level = max(level, int(ilvl.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") or 0))
                except (TypeError, ValueError):
                    pass
        lines.append("  " * level + text)
    return "\n".join(lines)


def from_flat_sections(sections: list[dict]) -> list[dict]:
    """把数据库里的扁平章节还原成大纲树（改完章节后回写 meta.outline 用）。"""
    flat = [{"title": s["title"], "level": int(s.get("level") or 1),
             "brief": s.get("brief") or "", "scores": []} for s in sections]
    for s, f in zip(sections, flat):
        raw = s.get("score") or "[]"
        if isinstance(raw, str):
            try:
                import json
                f["scores"] = [str(x) for x in json.loads(raw)] if raw.strip().startswith("[") else []
            except Exception:
                f["scores"] = []
    return _nest(flat)
