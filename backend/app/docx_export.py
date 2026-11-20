"""Word 导出：run 级中文字体排版（直接操作 OOXML，不依赖任何外部模板）。

与「只把正文堆进去」的初版相比，这里是按投标文件交付形态排版：
封面 → 目录域 → 按大纲顺序的正文（Markdown 表格还原成真表格）→ 签署盖章页，
带页眉、页码；暗标模式自动隐去投标人标识与签章。

不改数据、不调模型，只把库里已定稿的章节渲染成 .docx。
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from . import provenance

# 暗标（技术标）不得出现可识别投标人的信息；保守只隐去明确标识
_BLIND_PATTERNS = [
    (r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}"
     r"(?:有限公司|有限责任公司|股份有限公司|集团公司)", "（投标人名称已隐去）"),
    (r"统一社会信用代码[:：]?\s*[0-9A-Z]{15,20}", "（统一社会信用代码已隐去）"),
    (r"法定代表人[:：]\s*[\u4e00-\u9fa5]{2,8}", "（法定代表人已隐去）"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "（联系电话已隐去）"),
    (r"[\w.\-]+@[\w.\-]+\.[A-Za-z]{2,6}", "（邮箱已隐去）"),
]


def blind_scrub(text: str) -> str:
    """暗标去标识：只做确定性替换，不猜测、不改写方案内容。"""
    s = str(text or "")
    for pat, repl in _BLIND_PATTERNS:
        s = re.sub(pat, repl, s)
    return s


def _set_run_font(run, east: str = "宋体", size: float = 12, bold: bool = False) -> None:
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), east)
    fonts.set(qn("w:ascii"), "Times New Roman")
    fonts.set(qn("w:hAnsi"), "Times New Roman")


def _page_number(paragraph) -> None:
    run = paragraph.add_run("第 ")
    _set_run_font(run, size=9)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run._element.addnext(fld)
    run2 = paragraph.add_run(" 页")
    _set_run_font(run2, size=9)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _header(doc, text: str) -> None:
    p = doc.sections[0].header.paragraphs[0]
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if text:
        _set_run_font(p.add_run(text), size=9)


def _toc(doc) -> None:
    """插入真正的 Word 目录域；打开文档更新域即可生成页码。"""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run("目　录"), east="黑体", size=16, bold=True)
    holder = doc.add_paragraph()
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), r'TOC \o "1-3" \h \z \u')
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "（在 Word 中右键「更新域」或按 F9 生成目录与页码）"
    r.append(t)
    fld.append(r)
    holder._p.append(fld)
    doc.add_page_break()


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and bool(re.fullmatch(r"\|[\s:\-\|]+\|", s))


def _table_rows(block: list[str]) -> list[list[str]]:
    rows = []
    for line in block:
        if _is_table_sep(line):
            continue
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return [r for r in rows if any(r)]


def _add_table(doc, rows: list[list[str]], blind: bool = False) -> None:
    """还原 Markdown 表格。

    暗标下单元格必须和正文同样脱敏：报价表、业绩表、人员表的“单位名称”一列
    最容易带出公司名，以前只处理普通段落，表格是漏的。
    """
    cols = max(len(r) for r in rows)
    if cols < 1:
        return
    table = doc.add_table(rows=0, cols=cols)
    try:
        table.style = "Table Grid"
    except Exception:
        pass
    for row in rows:
        cells = table.add_row().cells
        for i in range(cols):
            p = cells[i].paragraphs[0]
            value = str(row[i] if i < len(row) else "")
            _set_run_font(p.add_run(blind_scrub(value) if blind else value), size=10.5)
    doc.add_paragraph()


def _scrub_props(doc) -> None:
    """暗标：清空文档属性里的身份线索（作者 / 最后修改人 / 标题 / 备注等）。

    文件正文洗干净了、属性里还留着“作者：某某公司”，暗标一样会被认出来。
    """
    cp = doc.core_properties
    for attr in ("author", "last_modified_by", "title", "subject", "comments",
                 "keywords", "category", "content_status", "identifier",
                 "language", "version"):
        try:
            setattr(cp, attr, "")
        except Exception:
            pass


def _add_body(doc, text: str, blind: bool = False) -> None:
    """按空行分段；连续竖线行还原为表格，其余逐行成段。"""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if any(ln.startswith("|") for ln in lines) and any(_is_table_sep(ln) for ln in lines):
            _add_table(doc, _table_rows(lines), blind=blind)
            continue
        for line in lines:
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(24)
            _set_run_font(p.add_run(blind_scrub(line) if blind else line), size=12)
        doc.add_paragraph()


def _signature_page(doc, blind: bool) -> None:
    doc.add_page_break()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run("签署与盖章"), east="黑体", size=16, bold=True)
    doc.add_paragraph()
    if blind:
        p = doc.add_paragraph()
        _set_run_font(p.add_run("（暗标模式：本页不生成投标人名称、签章位与联系信息）"), size=11)
        return
    rows = [
        "投标人（盖章）：________________________",
        "法定代表人或授权代表（签字）：________________________",
        "统一社会信用代码：________________________",
        "联系电话：________________________",
        f"签署日期：{datetime.now().strftime('%Y 年 %m 月 %d 日')}",
        "附：资格证明、授权书、承诺函等按招标文件要求另行装订。",
    ]
    for line in rows:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(14)
        _set_run_font(p.add_run(line), size=12)


def _ordered_sections(sections: list[dict], required: list[str]) -> list[dict]:
    """按招标文件解读出的大纲顺序排，大纲之外的章节按原顺序附后。"""
    if not required:
        return list(sections)
    by_title = {str(s.get("title") or "").strip(): s for s in sections}
    out, used = [], set()
    for title in required:
        s = by_title.get(title)
        if s is not None and title not in used:
            out.append(s)
            used.add(title)
    out.extend(s for s in sections
               if str(s.get("title") or "").strip() not in used)
    return out


def build_docx(project_name: str, sections: list[dict],
               options: dict[str, Any] | None = None) -> bytes:
    """渲染 .docx；options: blind / toc / header / required_sections / signature。"""
    opt = options or {}
    blind = bool(opt.get("blind"))
    required = [str(x).strip() for x in (opt.get("required_sections") or [])
                if str(x).strip()]
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    style.paragraph_format.line_spacing = 1.5
    style.paragraph_format.space_after = Pt(6)

    # 封面：暗标不写项目名（可能含可识别线索），也不落其他标识
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run("（暗标）" if blind else project_name),
                  east="黑体", size=22, bold=True)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run("投 标 文 件"), east="黑体", size=26, bold=True)
    for _ in range(6):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run(str(opt.get("date") or datetime.now().strftime("%Y 年 %m 月 %d 日"))), size=12)
    doc.add_page_break()

    if opt.get("toc", True):
        _toc(doc)

    for sec in _ordered_sections(list(sections or []), required):
        title = str(sec.get("title") or "")
        h = doc.add_heading(level=1)
        _set_run_font(h.add_run(blind_scrub(title) if blind else title),
                      east="黑体", size=15, bold=True)
        # 内部工作标注（依据清单 / AI 建议 / 需人工补充）不进正式投标文件
        text = provenance.clean_for_export(sec.get("text") or "") or "（未填写）"
        _add_body(doc, text, blind=blind)

    _signature_page(doc, blind)
    _header(doc, "" if blind else str(opt.get("header") or project_name))
    _page_number(doc.sections[0].footer.paragraphs[0])
    if blind:
        _scrub_props(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
