"""docx 审计引擎：一次建模型，九类规则跑检查，供暗标/格式检查与自动修复共用。

与旧 blind_rules.py 的关系：这里负责"文档体检"（格式、结构、痕迹），
blind_rules.py 继续负责"身份信息识别"（证件号校验位、企业名后缀），
两边在 blind.check 里汇总。

模型要点：
1. 只遍历一次文档，把段落/文字块/表格/图片/节属性全部解析成数据结构；
2. 规则只读模型不碰文档，修复层复用同一模型按规则 ID 改 XML；
3. 一致性类规则用"众数为基准找离群"的思路，不要求用户先配目标格式。
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from PIL import Image

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

SEVERITY_ORDER = {"high": 0, "mid": 1, "low": 2}
SEVERITY_LABEL = {"high": "严重", "mid": "中等", "low": "轻微"}

CATEGORIES: list[dict] = [
    {"id": "privacy", "label": "隐私痕迹", "hint": "修订、批注、隐藏文字、文档属性等身份线索"},
    {"id": "structure", "label": "文档结构", "hint": "页码、目录、书签、域、文本框"},
    {"id": "image", "label": "图片", "hint": "彩色图片、矢量图文字、图片元数据"},
    {"id": "page", "label": "页面设置", "hint": "纸张、边距、分栏、背景"},
    {"id": "paragraph", "label": "段落格式", "hint": "对齐、行距、缩进、段距"},
    {"id": "font", "label": "字体", "hint": "字体、字号、加粗、颜色、突出显示"},
    {"id": "text", "label": "文本规范", "hint": "标点、空格、全角半角、特殊字符"},
    {"id": "table", "label": "表格", "hint": "表格字体、底纹、嵌套、合并"},
    {"id": "consistency", "label": "一致性", "hint": "同级标题格式、编号、层级"},
]


# ---------------- 数据结构 ----------------
@dataclass
class RunFmt:
    east: str = ""
    ascii: str = ""
    size: float = 0.0
    bold: bool | None = None
    italic: bool | None = None
    underline: bool = False
    color: str = ""
    highlight: str = ""
    vanish: bool = False
    vert: str = ""
    char_spacing: float = 0.0
    scaling: int = 0
    effects: list[str] = field(default_factory=list)


@dataclass
class RunInfo:
    para: "ParaInfo"
    node: object
    text: str
    fmt: RunFmt


@dataclass
class ParaInfo:
    idx: int
    where: str
    node: object
    text: str
    style: str
    runs: list[RunInfo]
    in_table: bool = False
    is_heading: bool = False
    heading_level: int = 0
    align: str = ""
    line: str = ""
    line_rule: str = ""
    first_line: str = ""
    space_before: str = ""
    space_after: str = ""
    page_break: bool = False
    snap: str = ""


@dataclass
class SectionInfo:
    idx: int
    page_w: float = 0.0
    page_h: float = 0.0
    orient: str = ""
    margins: dict = field(default_factory=dict)
    columns: int = 0


@dataclass
class ImageInfo:
    name: str
    where: str
    colored: bool = False
    exif_tags: list[str] = field(default_factory=list)
    gps: bool = False
    width: int = 0
    height: int = 0


@dataclass
class Finding:
    rule: str
    title: str
    category: str
    severity: str
    where: str
    detail: str


@dataclass
class Model:
    path: Path
    paras: list[ParaInfo] = field(default_factory=list)
    sections: list[SectionInfo] = field(default_factory=list)
    images: list[ImageInfo] = field(default_factory=list)
    headings: list[ParaInfo] = field(default_factory=list)
    zip_names: list[str] = field(default_factory=list)
    doc_xml: str = ""
    core_xml: str = ""
    app_xml: str = ""
    custom_xml: str = ""
    comments_xml: str = ""
    revisions: int = 0
    fields: list[str] = field(default_factory=list)
    hyperlinks: list[str] = field(default_factory=list)
    bookmarks: list[str] = field(default_factory=list)
    textboxes: int = 0
    graphic_colors: list[str] = field(default_factory=list)
    vector_text: int = 0
    background: str = ""
    empty_paras: list[ParaInfo] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def body_paras(self, *, include_table: bool = False) -> list[ParaInfo]:
        return [p for p in self.paras if include_table or not p.in_table]


TWIPS = 567.0  # 1 cm = 567 twips


def _f(val, scale=1.0) -> float:
    try:
        return float(val) * scale
    except (TypeError, ValueError):
        return 0.0


# ---------------- 模型构建 ----------------
def _run_fmt(node) -> RunFmt:
    fmt = RunFmt()
    rpr = node.find(qn("w:rPr"))
    if rpr is None:
        return fmt
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is not None:
        fmt.east = rfonts.get(qn("w:eastAsia")) or ""
        fmt.ascii = rfonts.get(qn("w:ascii")) or ""
    sz = rpr.find(qn("w:sz"))
    if sz is not None:
        fmt.size = _f(sz.get(qn("w:val"))) / 2.0
    b = rpr.find(qn("w:b"))
    if b is not None:
        fmt.bold = b.get(qn("w:val")) != "0"
    i = rpr.find(qn("w:i"))
    if i is not None:
        fmt.italic = i.get(qn("w:val")) != "0"
    u = rpr.find(qn("w:u"))
    if u is not None:
        fmt.underline = u.get(qn("w:val")) not in (None, "none")
    c = rpr.find(qn("w:color"))
    if c is not None:
        fmt.color = (c.get(qn("w:val")) or "").upper()
    h = rpr.find(qn("w:highlight"))
    if h is not None:
        fmt.highlight = h.get(qn("w:val")) or ""
    if rpr.find(qn("w:vanish")) is not None:
        fmt.vanish = True
    v = rpr.find(qn("w:vertAlign"))
    if v is not None:
        fmt.vert = v.get(qn("w:val")) or ""
    sp = rpr.find(qn("w:spacing"))
    if sp is not None:
        fmt.char_spacing = _f(sp.get(qn("w:val"))) / 20.0
    sc = rpr.find(qn("w:w"))
    if sc is not None:
        fmt.scaling = int(_f(sc.get(qn("w:val"))))
    for name in ("w:strike", "w:dstrike", "w:emboss", "w:imprint", "w:shadow", "w:outline"):
        el = rpr.find(qn(name))
        if el is not None and el.get(qn("w:val")) != "0":
            fmt.effects.append(name.split(":")[1])
    return fmt


HEADING_STYLE = re.compile(r"^(heading|标题)\s*(\d)", re.I)
HEADING_TEXT = re.compile(r"^((第[一二三四五六七八九十百零〇\d]+[章编部分篇])|([一二三四五六七八九十]+[、．.])|(\d+(?:\.\d+){0,3})[、.\s（(]|[（(][一二三四五六七八九十\d]+[)）])")


def _classify(para) -> tuple[bool, int]:
    try:
        name = para.style.name or ""
    except Exception:
        name = ""
    m = HEADING_STYLE.match(name)
    if m:
        return True, int(m.group(2))
    sid = (para.style.style_id or "") if para.style is not None else ""
    m2 = re.match(r"^[1-6]$", sid.replace("Heading", "").replace("heading", "").strip())
    if m2 and name:
        return True, int(m2.group(1))
    text = (para.text or "").strip()
    if text and len(text) <= 40 and HEADING_TEXT.match(text):
        head = HEADING_TEXT.match(text).group(0)
        dots = head.count(".")
        return True, min(1 + dots, 6) if dots else 1
    return False, 0


def _para_info(idx: int, where: str, para, *, in_table: bool = False) -> ParaInfo:
    node = para._p
    ppr = node.find(qn("w:pPr"))
    info = ParaInfo(idx=idx, where=where, node=node, text=para.text or "",
                    style="", runs=[], in_table=in_table)
    try:
        info.style = para.style.style_id or ""
    except Exception:
        info.style = ""
    info.is_heading, info.heading_level = _classify(para)
    if ppr is not None:
        jc = ppr.find(qn("w:jc"))
        info.align = (jc.get(qn("w:val")) if jc is not None else "") or ""
        sp = ppr.find(qn("w:spacing"))
        if sp is not None:
            info.line = sp.get(qn("w:line")) or ""
            info.line_rule = sp.get(qn("w:lineRule")) or ""
            info.space_before = sp.get(qn("w:before")) or ""
            info.space_after = sp.get(qn("w:after")) or ""
        ind = ppr.find(qn("w:ind"))
        if ind is not None:
            info.first_line = ind.get(qn("w:firstLineChars")) or ind.get(qn("w:firstLine")) or ""
        if ppr.find(qn("w:pageBreakBefore")) is not None:
            info.page_break = True
        snap = ppr.find(qn("w:snapToGrid"))
        if snap is not None:
            info.snap = snap.get(qn("w:val")) or "1"
    for rnode in node.iter(qn("w:r")):
        text = "".join(t.text or "" for t in rnode.findall(qn("w:t")))
        info.runs.append(RunInfo(para=info, node=rnode, text=text, fmt=_run_fmt(rnode)))
    return info


def _iter_para_sources(doc):
    for i, p in enumerate(doc.paragraphs):
        yield p, f"正文第 {i + 1} 段", False
    for ti, table in enumerate(doc.tables):
        for ri, row in enumerate(table.rows):
            seen = set()
            for ci, cell in enumerate(row.cells):
                if id(cell._tc) in seen:
                    continue
                seen.add(id(cell._tc))
                for p in cell.paragraphs:
                    yield p, f"表格 {ti + 1} 第 {ri + 1} 行第 {ci + 1} 列", True


def _section_info(idx: int, section) -> SectionInfo:
    info = SectionInfo(idx=idx)
    try:
        info.page_w = round(section.page_width.cm, 2)
        info.page_h = round(section.page_height.cm, 2)
        info.orient = section.orientation.name.lower() if section.orientation is not None else ""
        info.margins = {
            "top": round(section.top_margin.cm, 2),
            "bottom": round(section.bottom_margin.cm, 2),
            "left": round(section.left_margin.cm, 2),
            "right": round(section.right_margin.cm, 2),
        }
        cols = section._sectPr.find(qn("w:cols"))
        info.columns = int(_f(cols.get(qn("w:num")))) if cols is not None and cols.get(qn("w:num")) else (1 if cols is not None else 1)
    except Exception:
        pass
    return info


def _image_info(name: str, data: bytes, where: str) -> ImageInfo | None:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return None
    info = ImageInfo(name=name, where=where, width=img.width, height=img.height)
    exif = img.getexif()
    info.exif_tags = [str(t) for t in exif.keys()][:12]
    try:
        gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
        info.gps = bool(gps)
    except Exception:
        info.gps = False
    if img.mode in ("RGB", "RGBA") and img.width > 0:
        small = img.convert("RGB").resize((min(img.width, 64), min(img.height, 64)))
        colored = sum(1 for r, g, b in small.getdata() if max(r, g, b) - min(r, g, b) > 28)
        info.colored = colored > len(list(small.getdata())) * 0.01
    return info


def build_model(source, path: str | Path | None = None) -> Model:
    """source 可传路径或已打开的 Document；后者用于修复场景（模型与修改同树）。"""
    if isinstance(source, (str, Path)):
        path = Path(source)
        doc = Document(str(path))
    else:
        doc = source
        path = Path(path) if path else None
    m = Model(path=path or Path("current.docx"))
    m.paras = []
    idx = 0
    for para, where, in_table in _iter_para_sources(doc):
        info = _para_info(idx, where, para, in_table=in_table)
        m.paras.append(info)
        if info.is_heading:
            m.headings.append(info)
        idx += 1

    m.sections = [_section_info(i, s) for i, s in enumerate(doc.sections)]

    # 隐藏文字之外的模型成员从原始 zip 里取，避免 python-docx 不暴露的部分
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        m.zip_names = names
        m.doc_xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        if "docProps/core.xml" in names:
            m.core_xml = zf.read("docProps/core.xml").decode("utf-8", "ignore")
        if "docProps/app.xml" in names:
            m.app_xml = zf.read("docProps/app.xml").decode("utf-8", "ignore")
        if "docProps/custom.xml" in names:
            m.custom_xml = zf.read("docProps/custom.xml").decode("utf-8", "ignore")
        if "word/comments.xml" in names:
            m.comments_xml = zf.read("word/comments.xml").decode("utf-8", "ignore")
        for name in names:
            if name.startswith("word/media/"):
                where = "文档图片"
                info = _image_info(name, zf.read(name), where)
                if info is not None:
                    m.images.append(info)

    m.revisions = m.doc_xml.count("<w:ins ") + m.doc_xml.count("<w:del ")
    for part in re.findall(r"<w:instrText[^>]*>([^<]+)</w:instrText>", m.doc_xml):
        token = part.strip().split(" ")[0].upper()
        if token:
            m.fields.append(token)
    m.hyperlinks = re.findall(r'TargetMode="External"[^>]*/>', m.doc_xml) or re.findall(
        r'Target="(?:http|https|ftp)[^"]*"[^>]*TargetMode="External"', m.doc_xml)
    m.bookmarks = [b for b in re.findall(r'<w:bookmarkStart[^>]*w:name="([^"]+)"', m.doc_xml) if not b.startswith("_")]
    m.textboxes = m.doc_xml.count("<w:txbxContent>") + m.doc_xml.count(":txbxContent>")
    m.graphic_colors = re.findall(r"<a:srgbClr\s+val=\"([0-9A-Fa-f]{6})\"", m.doc_xml)
    m.vector_text = len(re.findall(r"<a:t>", m.doc_xml))
    bg = re.search(r"<w:background[^>]*>", m.doc_xml)
    m.background = bg.group(0) if bg else ""

    # 空段落
    prev_empty = None
    for p in m.paras:
        if not p.text.strip() and not p.runs:
            if prev_empty is None:
                prev_empty = []
            prev_empty.append(p)
        else:
            if prev_empty:
                m.empty_paras.extend(prev_empty[1:])  # 首个空段保留，其后连续的算多余
            prev_empty = None
    if prev_empty:
        m.empty_paras.extend(prev_empty[1:])
    return m


# ---------------- 工具 ----------------
def _mode(values: list[str]) -> str:
    vals = [v for v in values if v]
    if not vals:
        return ""
    return Counter(vals).most_common(1)[0][0]


def _mode_num(values: list[float], precision: float = 0.26) -> float:
    vals = [v for v in values if v > 0]
    if not vals:
        return 0.0
    buckets = Counter(round(v / precision) for v in vals)
    best = buckets.most_common(1)[0][0]
    return best * precision


CJK = re.compile(r"[\u4e00-\u9fff]")
HALF_PUNCT = re.compile(r"[,;:!?()]{1}")
FULLWIDTH_DIGIT = re.compile(r"[０-９]")
FULLWIDTH_LATIN = re.compile(r"[Ａ-Ｚａ-ｚ]")
IRREGULAR = re.compile(r"[★☆●◆■□☐☑▲△※§⊙◇〇¤❖]")


def _add(model: Model, rule: "Rule", where: str, detail: str) -> None:
    model.findings.append(Finding(rule=rule.id, title=rule.title, category=rule.category,
                                  severity=rule.severity, where=where, detail=detail))


# ---------------- 规则定义 ----------------
@dataclass
class Rule:
    id: str
    title: str
    category: str
    severity: str
    check: object
    fixable: bool = False


def _r_privacy_keyword(model: Model, words: list[str]) -> None:
    """敏感词由 blind_rules 的识别逻辑负责，这里不做重复检查。"""


def rule_hidden_text(model: Model, rule: Rule, cfg) -> None:
    for p in model.paras:
        for run in p.runs:
            if run.fmt.vanish and run.text.strip():
                _add(model, rule, p.where, f"隐藏文字：{run.text[:24]}")
                break


def rule_revisions(model: Model, rule: Rule, cfg) -> None:
    if model.revisions:
        _add(model, rule, "文档全局", f"存在 {model.revisions} 处未处理的修订标记（插入/删除）")


def rule_comments(model: Model, rule: Rule, cfg) -> None:
    n = model.comments_xml.count("<w:comment ")
    if n:
        _add(model, rule, "文档全局", f"文档带 {n} 条批注")


def rule_header_footer(model: Model, rule: Rule, cfg) -> None:
    for p in model.paras:
        if p.where.startswith(("页眉", "页脚")) and p.text.strip():
            _add(model, rule, p.where, f"页眉页脚含文字：{p.text.strip()[:24]}")


def rule_hyperlinks(model: Model, rule: Rule, cfg) -> None:
    if model.hyperlinks:
        _add(model, rule, "文档全局", f"{len(model.hyperlinks)} 处外部超链接")


def rule_fields(model: Model, rule: Rule, cfg) -> None:
    whitelist = {"PAGE", "NUMPAGES", "TOC", "DATE", "TIME", "STYLEREF", "SEQ", "REF"}
    bad = [f for f in model.fields if f not in whitelist]
    if bad:
        kinds = Counter(bad).most_common(4)
        _add(model, rule, "文档全局", "域代码：" + "、".join(f"{k}×{n}" for k, n in kinds))


def rule_doc_props(model: Model, rule: Rule, cfg) -> None:
    found = []
    for xml, tag in ((model.core_xml, "core"), (model.app_xml, "app")):
        if not xml:
            continue
        for key in ("dc:creator", "cp:lastModifiedBy", "dc:title", "dc:subject",
                    "dc:description", "dc:keywords", "cp:keywords", "Company", "Manager"):
            if re.search(rf"<{key}>([^<]+)</{key}>", xml):
                found.append(key.split(":")[-1])
    if model.custom_xml and "<Properties" in model.custom_xml:
        found.append("custom")
    if found:
        _add(model, rule, "文档属性", "属性残留：" + "、".join(sorted(set(found))))


def rule_image_meta(model: Model, rule: Rule, cfg) -> None:
    for img in model.images:
        if img.gps or len(img.exif_tags) > 3:
            _add(model, rule, img.name, f"图片带元数据（{'含GPS，' if img.gps else ''}{len(img.exif_tags)} 个 EXIF 标签）")


def rule_empty_paragraph(model: Model, rule: Rule, cfg) -> None:
    if model.empty_paras:
        where = model.empty_paras[0].where
        _add(model, rule, where, f"{len(model.empty_paras)} 个多余空段落（连续空行）")


def rule_toc(model: Model, rule: Rule, cfg) -> None:
    has_toc_field = "TOC" in model.fields
    manual = 0
    for p in model.paras:
        if re.match(r"^\s*\d+(\.\d+)?\s*[.．]?\s*\t?", p.text) and "\t" in p.text and p.text.count("\t") >= 1:
            manual += 1
    if manual >= 3 and not has_toc_field:
        _add(model, rule, "目录区", f"疑似手工目录（{manual} 行带页码制表位，无 TOC 域）")


def rule_heading_number(model: Model, rule: Rule, cfg) -> None:
    tops = [h for h in model.headings if h.heading_level == 1]
    styles = [h.style for h in tops]
    if len(set(styles)) > 1:
        _add(model, rule, "标题", f"一级标题使用了 {len(set(styles))} 种样式，编号方式可能不统一")


def rule_textbox(model: Model, rule: Rule, cfg) -> None:
    if model.textboxes:
        _add(model, rule, "文档全局", f"{model.textboxes} 处文本框（暗标评审查排版痕迹）")


def rule_bookmark(model: Model, rule: Rule, cfg) -> None:
    if model.bookmarks:
        _add(model, rule, "文档全局", f"{len(model.bookmarks)} 个命名书签：" + "、".join(model.bookmarks[:4]))


def rule_page_number(model: Model, rule: Rule, cfg) -> None:
    body_hits = [p for p in model.body_paras() if re.search(r"第\s*\d+\s*页|-\s*\d+\s*-|^\s*\d+\s*$", p.text)]
    if body_hits:
        _add(model, rule, body_hits[0].where, f"正文里出现页码样式文字 {len(body_hits)} 处（页码应由页脚域生成）")


def rule_paper(model: Model, rule: Rule, cfg) -> None:
    sizes = {(s.page_w, s.page_h) for s in model.sections}
    if len(sizes) > 1:
        _add(model, rule, "页面设置", f"纸张大小不一致：{sorted(sizes)}")
    for s in model.sections:
        if s.page_w and abs(s.page_w - 21.0) > 0.5 and abs(s.page_w - 29.7) > 0.5:
            _add(model, rule, f"第 {s.idx + 1} 节", f"非常见纸张 {s.page_w}×{s.page_h}cm")
            break


def rule_orientation(model: Model, rule: Rule, cfg) -> None:
    orients = {s.orient for s in model.sections if s.orient}
    if len(orients) > 1:
        _add(model, rule, "页面设置", f"纸张方向混用：{sorted(orients)}")


def rule_margins(model: Model, rule: Rule, cfg) -> None:
    groups: dict[tuple, int] = {}
    for s in model.sections:
        key = tuple(sorted(s.margins.items()))
        groups[key] = groups.get(key, 0) + 1
    if len(groups) > 1:
        _add(model, rule, "页面设置", f"各节页边距不一致（{len(groups)} 种）")


def rule_columns(model: Model, rule: Rule, cfg) -> None:
    for s in model.sections:
        if s.columns and s.columns > 1:
            _add(model, rule, f"第 {s.idx + 1} 节", f"分栏数 {s.columns}")


def rule_sections(model: Model, rule: Rule, cfg) -> None:
    if len(model.sections) > 1:
        first = model.sections[0]
        for s in model.sections[1:]:
            if (s.page_w, s.page_h) != (first.page_w, first.page_h) or s.margins != first.margins:
                _add(model, rule, f"第 {s.idx + 1} 节", "与第 1 节页面设置不一致")
                break


def rule_background(model: Model, rule: Rule, cfg) -> None:
    if model.background:
        _add(model, rule, "文档全局", f"页面背景色 {model.background}")


def rule_alignment(model: Model, rule: Rule, cfg) -> None:
    body = [p for p in model.body_paras() if p.text.strip() and not p.is_heading]
    aligns = [p.align for p in body if p.align]
    if not aligns:
        return
    dom = _mode(aligns)
    bad = [p for p in body if p.align and p.align != dom]
    if bad:
        _add(model, rule, bad[0].where, f"正文对齐方式以「{dom}」为主，另有 {len(bad)} 段不同（如「{bad[0].align}」）")


def rule_line_spacing(model: Model, rule: Rule, cfg) -> None:
    body = [p for p in model.body_paras() if p.text.strip() and not p.is_heading]
    lines = [p.line for p in body if p.line and p.line_rule]
    if not lines:
        return
    dom = _mode(lines)
    bad = [p for p in body if p.line and p.line_rule and p.line != dom]
    if bad:
        _add(model, rule, bad[0].where, f"正文行距以「{dom}」为主，另有 {len(bad)} 段不同")


def rule_first_line(model: Model, rule: Rule, cfg) -> None:
    body = [p for p in model.body_paras() if len(p.text.strip()) > 30 and not p.is_heading]
    with_ind = [p.first_line for p in body if p.first_line]
    if not with_ind:
        if len(body) >= 5:
            _add(model, rule, body[0].where, f"正文 {len(body)} 段均未设置首行缩进")
        return
    dom = _mode(with_ind)
    bad = [p for p in body if p.first_line and p.first_line != dom]
    if bad:
        _add(model, rule, bad[0].where, f"首行缩进以「{dom}」为主，另有 {len(bad)} 段不同")


def rule_space(model: Model, rule: Rule, cfg) -> None:
    body = [p for p in model.body_paras() if p.text.strip() and not p.is_heading]
    bad = [p for p in body if (p.space_before not in ("", "0")) or (p.space_after not in ("", "0"))]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 段设置了段前/段后间距")


def rule_page_break(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.body_paras() if p.page_break]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 段设置了段前分页")


def rule_snap(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.body_paras() if p.snap and p.snap == "0"]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 段取消了对齐到网格")


def _run_font_rules(model: Model, rule: Rule, cfg, pick, fmt_bad, describe) -> None:
    body = model.body_paras(include_table=True)
    values = []
    for p in body:
        for run in p.runs:
            if run.text.strip():
                v = pick(run)
                if v:
                    values.append((v, run, p))
    if not values:
        return
    dominant = _mode([v for v, _, _ in values])
    bad = [(v, run, p) for v, run, p in values if v != dominant]
    if bad:
        v, run, p = bad[0]
        _add(model, rule, p.where, f"以「{dominant or '默认'}」为主，另有 {len(bad)} 处「{describe(v)}」")


def rule_font_east(model: Model, rule: Rule, cfg) -> None:
    _run_font_rules(model, rule, cfg, lambda r: r.fmt.east, None, lambda v: v)


def rule_font_ascii(model: Model, rule: Rule, cfg) -> None:
    _run_font_rules(model, rule, cfg, lambda r: r.fmt.ascii, None, lambda v: v)


def rule_font_size(model: Model, rule: Rule, cfg) -> None:
    body = model.body_paras(include_table=True)
    values = []
    for p in body:
        if p.is_heading or p.where.startswith("表格"):
            continue
        for run in p.runs:
            if run.text.strip() and run.fmt.size:
                values.append((run.fmt.size, run, p))
    if len(values) < 5:
        return
    dom = _mode_num([v for v, _, _ in values])
    bad = [(v, run, p) for v, run, p in values if abs(v - dom) > 0.6]
    if bad:
        v, run, p = bad[0]
        _add(model, rule, p.where, f"正文字号以「{dom:g}pt」为主，另有 {len(bad)} 处「{v:g}pt」")


def _toggle_rule(model: Model, rule: Rule, cfg, get_flag) -> None:
    body = [p for p in model.body_paras(include_table=True) if not p.is_heading and not p.where.startswith("表格")]
    bad = []
    for p in body:
        for run in p.runs:
            if run.text.strip() and get_flag(run.fmt):
                bad.append((run, p))
                break
    if bad:
        run, p = bad[0]
        _add(model, rule, p.where, f"{len(bad)} 段含此类设置，如「{run.text[:16]}」")


def rule_bold(model: Model, rule: Rule, cfg) -> None:
    _toggle_rule(model, rule, cfg, lambda f: f.bold is True)


def rule_italic(model: Model, rule: Rule, cfg) -> None:
    _toggle_rule(model, rule, cfg, lambda f: f.italic is True)


def rule_underline(model: Model, rule: Rule, cfg) -> None:
    _toggle_rule(model, rule, cfg, lambda f: f.underline)


NEAR_BLACK = {"000000", "0D0D0D", "111111", "191919", "1A1A1A", "262626", "333333", "404040", "AUTO"}


def rule_color(model: Model, rule: Rule, cfg) -> None:
    bad = []
    for p in model.body_paras(include_table=True):
        for run in p.runs:
            c = run.fmt.color
            if run.text.strip() and c and c not in NEAR_BLACK:
                bad.append((c, run, p))
    if bad:
        c, run, p = bad[0]
        _add(model, rule, p.where, f"{len(bad)} 处非常规颜色（如 #{c}）：" + "、".join(sorted({x[0] for x in bad})[:5]))


def rule_highlight(model: Model, rule: Rule, cfg) -> None:
    bad = []
    for p in model.body_paras(include_table=True):
        for run in p.runs:
            if run.text.strip() and run.fmt.highlight:
                bad.append((run.fmt.highlight, p))
    if bad:
        _add(model, rule, bad[0][1].where, f"{len(bad)} 处突出显示/底纹（如 {bad[0][0]}）")


def rule_effects(model: Model, rule: Rule, cfg) -> None:
    bad = []
    for p in model.body_paras(include_table=True):
        for run in p.runs:
            if run.text.strip() and run.fmt.effects:
                bad.append((run.fmt.effects, p))
    if bad:
        _add(model, rule, bad[0][1].where, f"{len(bad)} 处异常字体效果（{bad[0][0]}）")


def rule_char_spacing(model: Model, rule: Rule, cfg) -> None:
    bad = []
    for p in model.body_paras(include_table=True):
        for run in p.runs:
            if run.text.strip() and abs(run.fmt.char_spacing) > 0.01:
                bad.append(p)
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 处调整过字符间距")


def rule_scaling(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.body_paras(include_table=True)
           for run in p.runs if run.text.strip() and run.fmt.scaling and run.fmt.scaling != 100]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 处字体缩放非 100%")


def rule_vert(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.body_paras(include_table=True)
           for run in p.runs if run.text.strip() and run.fmt.vert]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 处上/下标")


def _text_rule(model: Model, rule: Rule, cfg, matcher, describe) -> None:
    bad = []
    for p in model.body_paras(include_table=True):
        if CJK.search(p.text) or matcher(p.text):
            hits = matcher(p.text)
            if hits:
                bad.append((hits, p))
    if bad:
        hits, p = bad[0]
        _add(model, rule, p.where, f"{len(bad)} 段含{describe}，如「{str(hits)[:30]}」")


def rule_english_punct(model: Model, rule: Rule, cfg) -> None:
    pat = re.compile(r"[\u4e00-\u9fff][,;:!?()][\u4e00-\u9fff]")
    _text_rule(model, rule, cfg, lambda t: pat.findall(t), "中文间英文标点")


def rule_spaces(model: Model, rule: Rule, cfg) -> None:
    pat = re.compile(r"\S{0,6}  +\S{0,6}|\S +$")
    _text_rule(model, rule, cfg, lambda t: pat.findall(t) or ([] if not re.search(r"\S  +", t) else ["  "]), "连续空格")


def rule_linebreak(model: Model, rule: Rule, cfg) -> None:
    n = model.doc_xml.count('<w:br w:type="textWrapping"') + model.doc_xml.count("<w:br/>")
    if n:
        _add(model, rule, "文档全局", f"{n} 处手动换行（软回车）")


def rule_tabs(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.body_paras() if "\t" in p.text]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 段含制表符")


def rule_irregular(model: Model, rule: Rule, cfg) -> None:
    _text_rule(model, rule, cfg, lambda t: IRREGULAR.findall(t), "特殊符号")


def rule_halfwidth(model: Model, rule: Rule, cfg) -> None:
    pat = re.compile(r"[\u4e00-\u9fff][()（）]|[(（)][\u4e00-\u9fff]")
    _text_rule(model, rule, cfg, lambda t: [t[m.start():m.start() + 2] for m in re.finditer(r"[\u4e00-\u9fff]\(", t)], "半角括号")


def rule_fullwidth_digit(model: Model, rule: Rule, cfg) -> None:
    _text_rule(model, rule, cfg, lambda t: FULLWIDTH_DIGIT.findall(t), "全角数字")


def rule_fullwidth_latin(model: Model, rule: Rule, cfg) -> None:
    _text_rule(model, rule, cfg, lambda t: FULLWIDTH_LATIN.findall(t), "全角字母")


def rule_image_color(model: Model, rule: Rule, cfg) -> None:
    bad = [img for img in model.images if img.colored]
    if bad:
        _add(model, rule, bad[0].name, f"{len(bad)} 张彩色图片（暗标通常要求黑白）")


def rule_graphic_color(model: Model, rule: Rule, cfg) -> None:
    colors = [c.upper() for c in model.graphic_colors if c.upper() not in NEAR_BLACK]
    if colors:
        _add(model, rule, "文档全局", f"{len(colors)} 处彩色图形填充：" + "、".join(sorted(set(colors))[:5]))


def rule_vector_text(model: Model, rule: Rule, cfg) -> None:
    if model.vector_text:
        _add(model, rule, "文档全局", f"图形/矢量图内有 {model.vector_text} 段文字（可能含身份信息）")


def rule_table_font(model: Model, rule: Rule, cfg) -> None:
    values = []
    for p in model.paras:
        if not p.where.startswith("表格"):
            continue
        for run in p.runs:
            if run.text.strip() and run.fmt.east:
                values.append((run.fmt.east, p))
    if not values:
        return
    dom = _mode([v for v, _ in values])
    bad = [(v, p) for v, p in values if v != dom]
    if bad:
        _add(model, rule, bad[0][1].where, f"表格字体以「{dom}」为主，另有 {len(bad)} 处不同")


def rule_table_size(model: Model, rule: Rule, cfg) -> None:
    values = []
    for p in model.paras:
        if not p.where.startswith("表格"):
            continue
        for run in p.runs:
            if run.text.strip() and run.fmt.size:
                values.append((run.fmt.size, p))
    if len(values) < 5:
        return
    dom = _mode_num([v for v, _ in values])
    bad = [(v, p) for v, p in values if abs(v - dom) > 0.6]
    if bad:
        _add(model, rule, bad[0][1].where, f"表格字号以「{dom:g}pt」为主，另有 {len(bad)} 处不同")


def rule_table_indent(model: Model, rule: Rule, cfg) -> None:
    bad = [p for p in model.paras if p.where.startswith("表格") and p.first_line]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 个表格段落带首行缩进（表格内通常不需要）")


def rule_table_shading(model: Model, rule: Rule, cfg) -> None:
    n = model.doc_xml.count("<w:shd ") - sum(
        1 for p in model.paras for run in p.runs if run.fmt.highlight)
    if n > 4:
        _add(model, rule, "文档全局", f"{n} 处单元格底纹")


def rule_table_nested(model: Model, rule: Rule, cfg) -> None:
    depth = model.doc_xml.count("<w:tbl>") 
    cells = model.doc_xml.count("<w:tc>")
    if depth and cells and cells / max(depth, 1) > 40:
        pass
    if "<w:tbl><w:tbl>" in model.doc_xml.replace(" ", ""):
        _add(model, rule, "文档全局", "存在嵌套表格")


def rule_table_alignment(model: Model, rule: Rule, cfg) -> None:
    tps = re.findall(r"<w:tblPr>.*?<w:jc\s+w:val=\"(\w+)\"", model.doc_xml)
    if tps and len(set(tps)) > 1:
        _add(model, rule, "文档全局", f"表格对齐方式不一致：{sorted(set(tps))}")


def rule_table_merge(model: Model, rule: Rule, cfg) -> None:
    n = model.doc_xml.count("<w:vMerge") + model.doc_xml.count("<w:hMerge")
    if n:
        _add(model, rule, "文档全局", f"{n} 处合并单元格")


def rule_consistency_heading(model: Model, rule: Rule, cfg) -> None:
    groups: dict[int, list[ParaInfo]] = {}
    for h in model.headings:
        groups.setdefault(h.heading_level, []).append(h)
    for level, items in sorted(groups.items()):
        fonts = [r.fmt.east for p in items for r in p.runs if r.fmt.east]
        sizes = [r.fmt.size for p in items for r in p.runs if r.fmt.size]
        if fonts and len(set(fonts)) > 1:
            _add(model, rule, items[0].where, f"{level} 级标题字体不统一：{sorted(set(fonts))[:3]}")
            return
        if sizes and len({round(s) for s in sizes}) > 1:
            _add(model, rule, items[0].where, f"{level} 级标题字号不统一")
            return


def rule_consistency_numbering(model: Model, rule: Rule, cfg) -> None:
    tops = [h for h in model.headings if h.heading_level == 1]
    nums = []
    for h in tops:
        m = re.match(r"^(第[一二三四五六七八九十\d]+[章节部分篇]|[一二三四五六七八九十]+[、．.]|\d+[、．.])", h.text.strip())
        if m:
            nums.append(m.group(1)[:1])
    if nums and len(set(nums)) > 1:
        _add(model, rule, tops[0].where, f"一级标题编号风格混用（{sorted(set(nums))}）")


def rule_consistency_jump(model: Model, rule: Rule, cfg) -> None:
    prev = 0
    for h in model.headings:
        if prev and h.heading_level - prev > 1:
            _add(model, rule, h.where, f"标题层级从 {prev} 级跳到 {h.heading_level} 级")
            return
        prev = h.heading_level


def rule_consistency_content(model: Model, rule: Rule, cfg) -> None:
    bad = [h for h in model.headings if h.text.strip().endswith(("，", "。", "、", ";", "；", ","))]
    if bad:
        _add(model, rule, bad[0].where, f"{len(bad)} 个标题以标点结尾")


def rule_consistency_body(model: Model, rule: Rule, cfg) -> None:
    body = [p for p in model.body_paras() if p.text.strip() and not p.is_heading]
    if len(body) < 6:
        return
    fonts = [r.fmt.east for p in body for r in p.runs if r.fmt.east]
    if fonts and len(set(fonts)) > 2:
        _add(model, rule, body[0].where, f"正文字体多达 {len(set(fonts))} 种：{sorted(set(fonts))[:4]}")


def rule_mixed_fonts(model: Model, rule: Rule, cfg) -> None:
    for p in model.body_paras(include_table=True):
        fonts = [r.fmt.east for r in p.runs if r.text.strip() and r.fmt.east]
        if len(set(fonts)) > 1:
            _add(model, rule, p.where, f"同段字体混用：{sorted(set(fonts))[:3]}")
            return


RULES: list[Rule] = [
    Rule("text.hidden", "隐藏文字", "privacy", "high", rule_hidden_text, fixable=True),
    Rule("struct.revision", "未处理的修订", "privacy", "high", rule_revisions, fixable=True),
    Rule("struct.comment", "批注", "privacy", "high", rule_comments, fixable=True),
    Rule("struct.header_footer", "页眉页脚内容", "privacy", "high", rule_header_footer),
    Rule("struct.hyperlink", "外部超链接", "privacy", "high", rule_hyperlinks, fixable=True),
    Rule("struct.field", "域代码", "privacy", "high", rule_fields),
    Rule("struct.doc_props", "文档属性", "privacy", "high", rule_doc_props, fixable=True),
    Rule("image.meta", "图片元数据", "privacy", "high", rule_image_meta, fixable=True),
    Rule("struct.empty_paragraph", "多余空段落", "structure", "mid", rule_empty_paragraph, fixable=True),
    Rule("struct.toc", "手工目录", "structure", "mid", rule_toc),
    Rule("struct.heading_number", "标题样式不统一", "structure", "mid", rule_heading_number),
    Rule("struct.textbox", "文本框", "structure", "low", rule_textbox),
    Rule("struct.bookmark", "命名书签", "structure", "low", rule_bookmark),
    Rule("struct.page_number_text", "正文页码文字", "structure", "mid", rule_page_number),
    Rule("image.color", "彩色图片", "image", "mid", rule_image_color, fixable=True),
    Rule("image.graphic_color", "彩色图形填充", "image", "mid", rule_graphic_color),
    Rule("image.vector", "矢量图内文字", "image", "high", rule_vector_text),
    Rule("page.paper_size", "纸张大小", "page", "mid", rule_paper),
    Rule("page.orientation", "纸张方向", "page", "low", rule_orientation),
    Rule("page.margins", "页边距", "page", "mid", rule_margins),
    Rule("page.columns", "分栏", "page", "low", rule_columns),
    Rule("page.sections", "各节设置一致", "page", "low", rule_sections),
    Rule("page.background", "页面背景色", "page", "mid", rule_background, fixable=True),
    Rule("para.alignment", "对齐方式", "paragraph", "mid", rule_alignment, fixable=True),
    Rule("para.line_spacing", "行距", "paragraph", "mid", rule_line_spacing, fixable=True),
    Rule("para.first_line_indent", "首行缩进", "paragraph", "mid", rule_first_line, fixable=True),
    Rule("para.space_before_after", "段前段后间距", "paragraph", "low", rule_space, fixable=True),
    Rule("para.page_break_before", "段前分页", "paragraph", "low", rule_page_break, fixable=True),
    Rule("para.snap_to_grid", "对齐到网格", "paragraph", "low", rule_snap, fixable=True),
    Rule("font.chinese", "中文字体", "font", "mid", rule_font_east, fixable=True),
    Rule("font.western", "西文字体", "font", "mid", rule_font_ascii, fixable=True),
    Rule("font.size", "字号", "font", "mid", rule_font_size, fixable=True),
    Rule("font.bold", "加粗", "font", "mid", rule_bold, fixable=True),
    Rule("font.italic", "斜体", "font", "mid", rule_italic, fixable=True),
    Rule("font.underline", "下划线", "font", "mid", rule_underline, fixable=True),
    Rule("font.color", "文字颜色", "font", "mid", rule_color, fixable=True),
    Rule("font.highlight", "突出显示", "font", "mid", rule_highlight, fixable=True),
    Rule("font.effects", "异常字体效果", "font", "mid", rule_effects, fixable=True),
    Rule("font.char_spacing", "字符间距", "font", "low", rule_char_spacing, fixable=True),
    Rule("font.scaling", "字体缩放", "font", "low", rule_scaling, fixable=True),
    Rule("font.script", "上下标", "font", "low", rule_vert, fixable=True),
    Rule("text.english_punct", "中文间英文标点", "text", "low", rule_english_punct, fixable=True),
    Rule("text.spaces", "多余空格", "text", "low", rule_spaces, fixable=True),
    Rule("text.linebreak", "手动换行", "text", "low", rule_linebreak, fixable=True),
    Rule("text.tabs", "制表符", "text", "low", rule_tabs),
    Rule("text.irregular", "特殊符号", "text", "low", rule_irregular),
    Rule("text.halfwidth_punct", "半角括号", "text", "low", rule_halfwidth, fixable=True),
    Rule("text.fullwidth_digit", "全角数字", "text", "low", rule_fullwidth_digit, fixable=True),
    Rule("text.fullwidth_latin", "全角字母", "text", "low", rule_fullwidth_latin, fixable=True),
    Rule("table.font", "表格字体", "table", "mid", rule_table_font),
    Rule("table.size", "表格字号", "table", "mid", rule_table_size),
    Rule("table.indent", "表格首行缩进", "table", "low", rule_table_indent, fixable=True),
    Rule("table.shading", "单元格底纹", "table", "mid", rule_table_shading),
    Rule("table.nested", "嵌套表格", "table", "low", rule_table_nested),
    Rule("table.alignment", "表格对齐", "table", "low", rule_table_alignment),
    Rule("table.merge", "合并单元格", "table", "low", rule_table_merge),
    Rule("consistency.heading_format", "同级标题格式", "consistency", "low", rule_consistency_heading),
    Rule("consistency.heading_numbering", "标题编号连续性", "consistency", "low", rule_consistency_numbering),
    Rule("consistency.heading_level_jump", "标题层级跳跃", "consistency", "low", rule_consistency_jump),
    Rule("consistency.heading_content", "标题内容异常", "consistency", "low", rule_consistency_content),
    Rule("consistency.body_format", "正文格式一致性", "consistency", "low", rule_consistency_body),
    Rule("consistency.mixed_fonts", "段内字体混用", "consistency", "low", rule_mixed_fonts),
]

RULE_MAP = {r.id: r for r in RULES}
RULE_COUNT = len(RULES)


def audit(path: str | Path, *, disabled: set[str] | None = None, sensitive_words: list[str] | None = None) -> dict:
    """跑全部规则，返回分组后的 findings。"""
    model = build_model(path)
    disabled = disabled or set()
    for rule in RULES:
        if rule.id in disabled:
            continue
        try:
            rule.check(model, rule, cfg=None)
        except Exception as exc:  # 单条规则失败不影响整体
            model.findings.append(Finding(rule=rule.id, title=rule.title, category=rule.category,
                                          severity="low", where="—", detail=f"规则执行失败：{exc}"))
    findings = model.findings
    by_category: dict[str, dict] = {}
    for cat in CATEGORIES:
        by_category[cat["id"]] = {**cat, "items": [], "count": 0}
    for f in findings:
        bucket = by_category.setdefault(f.category, {"id": f.category, "label": f.category, "items": [], "count": 0})
        bucket["items"].append(f.__dict__)
        bucket["count"] += 1
    counts = {"high": 0, "mid": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {
        "total": len(findings),
        "by_severity": counts,
        "by_category": list(by_category.values()),
        "rule_total": RULE_COUNT,
        "rule_ids": [r.id for r in RULES],
        "fixable": [r.id for r in RULES if r.fixable],
    }
