"""Word 导出：封面 + 目录域 + 页眉页脚页码 + 可配置版式。

版式由 formats.py 的预设决定（通用商务 / 公文 / 招标常见要求），
段落、标题、页边距、页码格式全部从配置读取，不再写死。
打开文档后按 F9 或"更新域"即可生成页码与目录。
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

from .formats import PAPER_CM, resolve

ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _set_run_font(run, east: str, size: float, bold: bool = False, ascii_font: str = "Times New Roman") -> None:
    run.font.name = ascii_font
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), east)
    fonts.set(qn("w:ascii"), ascii_font)
    fonts.set(qn("w:hAnsi"), ascii_font)


def _add_field(paragraph, instruction: str) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "打开文档时自动更新"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, separate, placeholder, end):
        run._r.append(node)


def _page_number(paragraph, pn: dict) -> None:
    fmt = str(pn.get("format") or "第 X 页")
    paragraph.alignment = ALIGN.get(str(pn.get("align") or "center"), WD_ALIGN_PARAGRAPH.CENTER)
    east = str(pn.get("east") or "宋体")
    size = float(pn.get("size") or 10.5)
    head, _, tail = fmt.partition("X")
    if head:
        _set_run_font(paragraph.add_run(head), east, size)
    _add_field(paragraph, "PAGE")
    if "Y" in tail:
        mid, _, after = tail.partition("Y")
        if mid:
            _set_run_font(paragraph.add_run(mid), east, size)
        _add_field(paragraph, "NUMPAGES")
        if after:
            _set_run_font(paragraph.add_run(after), east, size)
    elif tail:
        _set_run_font(paragraph.add_run(tail), east, size)


def _restart_numbering(section) -> None:
    sect_pr = section._sectPr
    pg_num = sect_pr.find(qn("w:pgNumType"))
    if pg_num is None:
        pg_num = OxmlElement("w:pgNumType")
        sect_pr.append(pg_num)
    pg_num.set(qn("w:start"), "1")


def _apply_page_setup(section, fmt: dict) -> None:
    width, height = PAPER_CM.get(str(fmt.get("paper") or "a4"), PAPER_CM["a4"])
    if str(fmt.get("orientation") or "portrait") == "landscape":
        width, height = height, width
    section.page_width, section.page_height = Cm(width), Cm(height)
    margins = fmt.get("margins") or {}
    for attr, key in (
        ("top_margin", "top"),
        ("bottom_margin", "bottom"),
        ("left_margin", "left"),
        ("right_margin", "right"),
    ):
        if margins.get(key) is not None:
            setattr(section, attr, Cm(float(margins[key])))


def _body_style(doc: Document, fmt: dict) -> None:
    body = fmt.get("body") or {}
    east = str(body.get("east") or "宋体")
    ascii_font = str(body.get("ascii") or "Times New Roman")
    size = float(body.get("size") or 12)

    style = doc.styles["Normal"]
    style.font.name = ascii_font
    style.font.size = Pt(size)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), east)
    pf = style.paragraph_format
    pf.line_spacing = float(body.get("line_spacing") or 1.5)
    pf.space_before = Pt(float(body.get("space_before") or 0))
    pf.space_after = Pt(float(body.get("space_after") or 0))
    pf.first_line_indent = Pt(size * float(body.get("indent_chars") or 2))
    pf.alignment = ALIGN.get(str(body.get("align") or "justify"), WD_ALIGN_PARAGRAPH.JUSTIFY)

    headings = fmt.get("headings") or {}
    for level in (1, 2, 3):
        spec = headings.get(str(level)) or {}
        heading = doc.styles[f"Heading {level}"]
        heading.font.name = ascii_font
        heading.font.size = Pt(float(spec.get("size") or 14))
        heading.font.bold = bool(spec.get("bold", level != 1))
        heading.font.color.rgb = RGBColor(0x17, 0x19, 0x1C)
        heading.element.rPr.rFonts.set(qn("w:eastAsia"), str(spec.get("east") or "黑体"))
        heading.paragraph_format.alignment = ALIGN.get(str(spec.get("align") or "left"), WD_ALIGN_PARAGRAPH.LEFT)
        heading.paragraph_format.space_before = Pt(float(spec.get("space_before") or 8))
        heading.paragraph_format.space_after = Pt(float(spec.get("space_after") or 6))
        heading.paragraph_format.first_line_indent = Pt(0)
        heading.paragraph_format.line_spacing = float(body.get("line_spacing") or 1.5)


def _cover(doc: Document, project_name: str, doc_type: str, bidder: str, date_text: str, fmt: dict) -> None:
    body = fmt.get("body") or {}
    cover = fmt.get("cover") or {}
    head_east = str((fmt.get("headings") or {}).get("1", {}).get("east") or "黑体")
    ascii_font = str(body.get("ascii") or "Times New Roman")

    for _ in range(4):
        doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(title.add_run(project_name or "投标文件"), head_east, float(cover.get("title_size") or 26), True, ascii_font)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.paragraph_format.space_before = Pt(18)
    _set_run_font(sub.add_run(doc_type or "投标文件"), head_east, float(cover.get("sub_size") or 20), False, ascii_font)

    for _ in range(6):
        doc.add_paragraph()

    for label, value in (("投 标 人", bidder), ("日    期", date_text)):
        line = doc.add_paragraph()
        line.alignment = WD_ALIGN_PARAGRAPH.CENTER
        line.paragraph_format.space_before = Pt(6)
        _set_run_font(
            line.add_run(f"{label}：{value or '〔待填〕'}"),
            str(body.get("east") or "宋体"),
            float(cover.get("meta_size") or 14),
            False,
            ascii_font,
        )


def _toc_page(doc: Document, fmt: dict) -> None:
    head_east = str((fmt.get("headings") or {}).get("1", {}).get("east") or "黑体")
    size = float((fmt.get("toc") or {}).get("title_size") or 18)
    heading = doc.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(heading.add_run("目　　录"), head_east, size, True)
    heading.paragraph_format.space_after = Pt(16)
    body = doc.add_paragraph()
    body.paragraph_format.first_line_indent = Pt(0)
    _add_field(body, r'TOC \o "1-3" \h \z \u')


_IMG_LINE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")


def _add_image(doc: Document, alt: str, rel: str) -> None:
    """把 markdown 图片行落成 Word 图片；路径相对 data 目录，找不到就降级为图注文字。"""
    from ..config import DATA_DIR

    rel = rel.replace("\\", "/").lstrip("/")
    target = DATA_DIR / rel
    if not target.exists():
        para = doc.add_paragraph()
        _add_text(para, f"［配图缺失：{alt or rel}］", "宋体", 10.5)
        return
    try:
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.first_line_indent = Pt(0)
        run = para.add_run()
        run.add_picture(str(target), width=Inches(5.6))
    except Exception:
        para = doc.add_paragraph()
        _add_text(para, f"［配图无法嵌入：{alt or rel}］", "宋体", 10.5)
        return
    if alt.strip():
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.first_line_indent = Pt(0)
        _add_text(cap, alt.strip(), "宋体", 10.5)


def _parse_blocks(content: str):
    """把受限 Markdown 拆成段落 / 子标题 / 列表 / 表格。"""
    blocks: list[tuple[str, object]] = []
    lines = (content or "").split("\n")
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if len(table_rows) >= 2:
            blocks.append(("table", table_rows))
        elif table_rows:
            for row in table_rows:
                blocks.append(("p", " | ".join(row)))
        table_rows = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            table_rows.append(cells)
            continue
        flush_table()
        if not stripped:
            continue
        img = _IMG_LINE.match(stripped)
        if img:
            blocks.append(("img", (img.group(1).strip(), img.group(2).strip())))
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            if text:
                blocks.append(("h2" if level <= 2 else "h3", text))
        elif re.match(r"^[-*+]\s+", stripped):
            blocks.append(("li", re.sub(r"^[-*+]\s+", "", stripped)))
        elif stripped.startswith(">"):
            blocks.append(("quote", stripped.lstrip("> ").strip()))
        else:
            blocks.append(("p", stripped))
    flush_table()
    return blocks


BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _add_text(para, text: str, font: str, size: float, bold: bool = False, ascii_font: str = "Times New Roman") -> None:
    """按 **加粗** / __加粗__ 切分，保留强调而不是把星号打进 Word。"""
    if not BOLD_RE.search(text or ""):
        _set_run_font(para.add_run(text), font, size, bold, ascii_font)
        return
    pos = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > pos:
            _set_run_font(para.add_run(text[pos : m.start()]), font, size, bold, ascii_font)
        inner = m.group(1) or m.group(2) or ""
        _set_run_font(para.add_run(inner), font, size, True, ascii_font)
        pos = m.end()
    if pos < len(text):
        _set_run_font(para.add_run(text[pos:]), font, size, bold, ascii_font)


def _render_content(doc: Document, content: str, fmt: dict) -> None:
    body = fmt.get("body") or {}
    east = str(body.get("east") or "宋体")
    ascii_font = str(body.get("ascii") or "Times New Roman")
    size = float(body.get("size") or 12)

    for kind, payload in _parse_blocks(content):
        if kind in ("h2", "h3"):
            level = 2 if kind == "h2" else 3
            doc.add_paragraph(str(payload), style=f"Heading {level}")
        elif kind == "img":
            alt, rel = payload  # type: ignore[misc]
            _add_image(doc, str(alt), str(rel))
        elif kind == "li":
            para = doc.add_paragraph(style="List Bullet")
            para.paragraph_format.first_line_indent = Pt(0)
            para.paragraph_format.line_spacing = float(body.get("line_spacing") or 1.5)
            _add_text(para, str(payload), east, size, False, ascii_font)
        elif kind == "quote":
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.8)
            para.paragraph_format.first_line_indent = Cm(0)
            _add_text(para, str(payload), east, size - 1.5, False, ascii_font)
            if para.runs:
                para.runs[0].font.color.rgb = None
        elif kind == "table":
            rows: list[list[str]] = payload  # type: ignore[assignment]
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for r_idx, row in enumerate(rows):
                for c_idx in range(cols):
                    cell = table.cell(r_idx, c_idx)
                    cell.text = ""
                    para = cell.paragraphs[0]
                    para.paragraph_format.first_line_indent = Pt(0)
                    para.paragraph_format.line_spacing = 1.2
                    text = row[c_idx] if c_idx < len(row) else ""
                    _add_text(para, text, east, max(9.0, size - 1.5), r_idx == 0, ascii_font)
            doc.add_paragraph()
        else:
            para = doc.add_paragraph()
            _add_text(para, str(payload), east, size, False, ascii_font)


def _seal_page(doc: Document, doc_type: str, fmt: dict) -> None:
    """密封条说明页：随分册导出，提示密封线与骑缝章要求。

    招标常见要求是"密封线内不得标注投标人身份信息、封口骑缝盖章"，
    这里给一页可直接撕贴的密封条样式，装订时照着做即可。
    """
    body = fmt.get("body") or {}
    east = str(body.get("east") or "宋体")
    ascii_font = str(body.get("ascii") or "Times New Roman")
    head_east = str((fmt.get("headings") or {}).get("1", {}).get("east") or "黑体")

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.page_break_before = True
    title.paragraph_format.space_before = Pt(60)
    _set_run_font(title.add_run("密　封　条"), head_east, 22, True, ascii_font)

    for _ in range(2):
        doc.add_paragraph()
    for label in ("正　　本", "副　　本"):
        line = doc.add_paragraph()
        line.alignment = WD_ALIGN_PARAGRAPH.CENTER
        line.paragraph_format.space_after = Pt(14)
        _set_run_font(
            line.add_run("———————————— ◆ " + label + " ◆ ————————————"),
            east, 14, False, ascii_font,
        )
        inner = doc.add_paragraph()
        inner.alignment = WD_ALIGN_PARAGRAPH.CENTER
        inner.paragraph_format.space_after = Pt(14)
        _set_run_font(inner.add_run("（密封线内不得书写与投标人身份相关的任何文字）"), east, 10.5, False, ascii_font)
        inner2 = doc.add_paragraph()
        inner2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        inner2.paragraph_format.space_after = Pt(24)
        _set_run_font(inner2.add_run(f"本册：{doc_type or '投标文件'}"), east, 12, True, ascii_font)

    for _ in range(2):
        doc.add_paragraph()
    notes = doc.add_paragraph()
    notes.paragraph_format.first_line_indent = Pt(0)
    _set_run_font(notes.add_run("密封与骑缝章说明："), east, 12, True, ascii_font)
    for text in (
        "1. 密封袋封口处加盖投标人单位公章，公章骑缝压住封口全部边缘。",
        "2. 密封线指装订侧约 3cm 范围，线内不得标注任何识别投标人身份的信息。",
        "3. 各册页码独立从第 1 页起计，页眉标注册号，评委按册翻阅。",
    ):
        item = doc.add_paragraph()
        item.paragraph_format.first_line_indent = Pt(0)
        item.paragraph_format.left_indent = Cm(0.6)
        _set_run_font(item.add_run(text), east, 10.5, False, ascii_font)


def _volume_header(section, note: str) -> None:
    """正文节页眉：标注册号（第 X 册 / 共 Y 册），分册页码独立时评委也能对册。"""
    note = (note or "").strip()
    if not note:
        return
    section.header.is_linked_to_previous = False
    para = section.header.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.first_line_indent = Pt(0)
    _set_run_font(para.add_run(note), "宋体", 9, False)


def _enable_update_fields(doc: Document) -> None:
    """设置 updateFields：Word 打开文档时自动更新目录页码，不用再按 F9。"""
    try:
        settings = doc.settings.element
        upd = OxmlElement("w:updateFields")
        upd.set(qn("w:val"), "true")
        settings.append(upd)
    except Exception:  # noqa: BLE001
        pass


def export_docx(
    *,
    out_path: Path,
    project_name: str,
    sections: list[dict],
    doc_type: str = "投标文件",
    bidder: str = "",
    date_text: str = "",
    with_cover: bool = True,
    with_toc: bool = True,
    fmt: dict | None = None,
    seal_note: bool = False,
    volume_note: str = "",
) -> Path:
    fmt = fmt or resolve()
    doc = Document()
    _body_style(doc, fmt)

    section = doc.sections[0]
    _apply_page_setup(section, fmt)

    if with_cover:
        _cover(doc, project_name, doc_type, bidder, date_text, fmt)

    if seal_note:
        _seal_page(doc, doc_type, fmt)

    body_section = doc.add_section(WD_SECTION.NEW_PAGE)
    _apply_page_setup(body_section, fmt)
    _restart_numbering(body_section)

    pn = fmt.get("page_number") or {}
    if pn.get("enabled", True):
        body_section.footer.is_linked_to_previous = False
        _page_number(body_section.footer.paragraphs[0], pn)
    _volume_header(body_section, volume_note)

    if with_toc:
        _toc_page(doc, fmt)

    body = fmt.get("body") or {}
    east = str(body.get("east") or "宋体")
    ascii_font = str(body.get("ascii") or "Times New Roman")
    size = float(body.get("size") or 12)

    first_after_toc = with_toc
    for idx, item in enumerate(sections):
        title = str(item.get("title", "")).strip()
        level = int(item.get("level", 1))
        if not title:
            continue
        # 章节点（下面还有小节）自己没正文时不该打「本节尚未编制」，
        # 否则第一章标题下紧跟一行占位符，评审看着像没写完。
        has_child = idx + 1 < len(sections) and int(sections[idx + 1].get("level", 1)) > level
        heading = doc.add_paragraph(title, style=f"Heading {min(max(level, 1), 3)}")
        spec = (fmt.get("headings") or {}).get(str(min(max(level, 1), 3))) or {}
        if level == 1 and (spec.get("page_break", True) or first_after_toc or not with_cover):
            heading.paragraph_format.page_break_before = True
            first_after_toc = False
        content = item.get("content") or ""
        if content.strip():
            _render_content(doc, content, fmt)
        elif not has_child:
            para = doc.add_paragraph()
            _set_run_font(para.add_run("〔本节尚未编制〕"), east, size, False, ascii_font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _enable_update_fields(doc)
    doc.save(str(out_path))
    return out_path
