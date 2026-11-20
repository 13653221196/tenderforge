"""docx 自动修复：按规则 ID 改文档，返回每条规则改了多少处。

设计原则：
1. 修复与检查共用 docx_audit 的模型思路——先看大多数段落长什么样（众数），
   再把离群的改成众数，所以"修得像"而不是"修成硬编码的某种格式"；
2. 只做安全修复：改格式、清痕迹、规范文本，绝不删除或改写有实际内容的段落
   （隐藏文字的删除只针对 vanish 标记，这是暗标检查的标准动作）；
3. 修复后的文件另存，原文件不动。
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from PIL import Image

from .docx_audit import NEAR_BLACK, build_model

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
HALF_TO_FULL = {",": "，", ";": "；", ":": "：", "!": "！", "?": "？", "(": "（", ")": "）"}
FULL_DIGITS = {chr(0xFF10 + i): str(i) for i in range(10)}
FULL_LATIN = {chr(0xFF21 + i): chr(65 + i) for i in range(26)}
FULL_LATIN.update({chr(0xFF41 + i): chr(97 + i) for i in range(26)})


class Fixer:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.doc = Document(str(path))
        # 关键：模型必须与 self.doc 同一棵树，否则改了也不生效
        self.model = build_model(self.doc, path=self.path)
        self.counts: dict[str, int] = {}
        self.notes: list[str] = []

    def bump(self, rule_id: str, n: int = 1) -> None:
        if n > 0:
            self.counts[rule_id] = self.counts.get(rule_id, 0) + n

    # ---------- 段落遍历辅助 ----------
    def _body_paras(self, *, with_table: bool = True):
        return [p for p in self.model.body_paras(include_table=with_table) if p.text.strip()]

    def _all_runs(self):
        for p in self.model.paras:
            for run in p.runs:
                yield run

    @staticmethod
    def _rpr(run_node):
        return run_node.find(qn("w:rPr"))

    @staticmethod
    def _drop_child(parent, tag: str) -> int:
        n = 0
        for el in parent.findall(qn(tag)):
            parent.remove(el)
            n += 1
        return n

    # ---------- 隐私 ----------
    def fix_hidden_text(self) -> None:
        for run in list(self._all_runs()):
            if run.fmt.vanish and run.text.strip():
                for t in run.node.findall(qn("w:t")):
                    run.node.remove(t)
                self.bump("text.hidden")

    def fix_revisions(self) -> None:
        for holder in (self.doc.element.body,):
            for tag, keep in ((qn("w:ins"), True), (qn("w:del"), False)):
                for node in list(holder.iter(tag)):
                    parent = node.getparent()
                    if parent is None:
                        continue
                    if keep:
                        idx = list(parent).index(node)
                        for child in list(node):
                            node.remove(child)
                            parent.insert(idx, child)
                            idx += 1
                        parent.remove(node)
                    else:
                        parent.remove(node)
                    self.bump("struct.revision")
            for tag in ("w:rPrChange", "w:pPrChange", "w:sectPrChange", "w:tblPrChange"):
                for node in list(holder.iter(qn(tag))):
                    node.getparent().remove(node)

    def fix_comments(self) -> None:
        for tag in ("w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"):
            nodes = list(self.doc.element.body.iter(qn(tag)))
            for node in nodes:
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)
            if nodes:
                self.bump("struct.comment", len(nodes))

    def fix_hyperlinks(self) -> None:
        for node in list(self.doc.element.body.iter(qn("w:hyperlink"))):
            parent = node.getparent()
            if parent is None:
                continue
            idx = list(parent).index(node)
            for child in list(node):
                node.remove(child)
                parent.insert(idx, child)
                idx += 1
            parent.remove(node)
            self.bump("struct.hyperlink")

    def fix_doc_props(self) -> None:
        cp = self.doc.core_properties
        for attr in ("author", "last_modified_by", "title", "subject", "keywords", "comments", "category"):
            try:
                if getattr(cp, attr, ""):
                    setattr(cp, attr, "")
                    self.bump("struct.doc_props")
            except Exception:
                pass

    # ---------- 图片（zip 阶段，save 之后执行） ----------
    def _rewrite_media(self, rule_id: str, should_touch, transform) -> None:
        src = self.path
        tmp = self.path.with_suffix(".tmp.docx")
        changed = 0
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith("word/media/") and should_touch(item.filename):
                    try:
                        img = Image.open(io.BytesIO(data))
                        data = transform(img)
                        changed += 1
                    except Exception:
                        pass
                if item.filename == "docProps/custom.xml":
                    continue  # 自定义属性整份剔除
                zout.writestr(item, data)
        tmp.replace(self.path)
        if changed:
            self.bump(rule_id, changed)
        # custom.xml 的引用关系也要清掉，否则部分校验工具会报损坏
        if "docProps/custom.xml" in self.model.zip_names:
            self._strip_custom_rel()

    def _strip_custom_rel(self) -> None:
        tmp = self.path.with_suffix(".tmp2.docx")
        with zipfile.ZipFile(self.path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "_rels/.rels":
                    text = data.decode("utf-8", "ignore")
                    text = re.sub(r"<Relationship[^>]*custom\.xml[^>]*/>", "", text)
                    data = text.encode("utf-8")
                elif item.filename == "[Content_Types].xml":
                    text = data.decode("utf-8", "ignore")
                    text = re.sub(r"<Override[^>]*custom\.xml[^>]*/>", "", text)
                    data = text.encode("utf-8")
                zout.writestr(item, data)
        tmp.replace(self.path)

    def fix_image_meta(self) -> None:
        def clean(img) -> bytes:
            buf = io.BytesIO()
            fmt = (img.format or "PNG").lower()
            if fmt == "jpeg":
                img.convert("RGB").save(buf, format="JPEG", quality=92)
            else:
                img.save(buf, format="PNG")
            return buf.getvalue()

        flagged = {img.name for img in self.model.images if img.gps or len(img.exif_tags) > 3}
        self._rewrite_media("image.meta", lambda name: name in flagged, clean)

    def fix_image_color(self) -> None:
        def gray(img) -> bytes:
            buf = io.BytesIO()
            img.convert("L").convert("RGB").save(buf, format="PNG")
            return buf.getvalue()

        flagged = {img.name for img in self.model.images if img.colored}
        self._rewrite_media("image.color", lambda name: name in flagged, gray)

    def fix_background(self) -> None:
        bg = self.doc.element.body.getprevious()
        root = self.doc.element
        for node in root.iter(qn("w:background")):
            node.getparent().remove(node)
            self.bump("page.background")

    # ---------- 段落 ----------
    def _para_fix(self, rule_id: str, mutate, *, with_table: bool = False) -> None:
        for p in self.model.paras:
            if not with_table and p.in_table:
                continue
            if mutate(p):
                self.bump(rule_id)

    def fix_alignment(self) -> None:
        body = [p for p in self._body_paras() if not p.is_heading and not p.in_table and p.align]
        if not body:
            return
        dom = Counter(p.align for p in body).most_common(1)[0][0]
        for p in body:
            if p.align != dom:
                p.node.find(qn("w:pPr")).find(qn("w:jc")).set(qn("w:val"), dom)
                self.bump("para.alignment")

    def fix_line_spacing(self) -> None:
        body = [p for p in self._body_paras() if not p.is_heading and not p.in_table and p.line and p.line_rule]
        if not body:
            return
        dom_line = Counter(p.line for p in body).most_common(1)[0][0]
        dom_rule = Counter(p.line_rule for p in body).most_common(1)[0][0]
        for p in body:
            if p.line != dom_line or p.line_rule != dom_rule:
                sp = p.node.find(qn("w:pPr")).find(qn("w:spacing"))
                sp.set(qn("w:line"), dom_line)
                sp.set(qn("w:lineRule"), dom_rule)
                self.bump("para.line_spacing")

    def fix_first_line(self) -> None:
        body = [p for p in self._body_paras() if not p.is_heading and not p.in_table and len(p.text.strip()) > 30]
        with_ind = [p.first_line for p in body if p.first_line]
        if not with_ind:
            return
        dom = Counter(with_ind).most_common(1)[0][0]
        for p in body:
            if p.first_line and p.first_line != dom:
                ind = p.node.find(qn("w:pPr")).find(qn("w:ind"))
                if ind.get(qn("w:firstLineChars")):
                    ind.set(qn("w:firstLineChars"), dom)
                else:
                    ind.set(qn("w:firstLine"), dom)
                self.bump("para.first_line_indent")

    def fix_space(self) -> None:
        def mutate(p) -> bool:
            if p.space_before in ("", "0") and p.space_after in ("", "0"):
                return False
            sp = p.node.find(qn("w:pPr")).find(qn("w:spacing"))
            if sp is None:
                return False
            changed = False
            if sp.get(qn("w:before")) not in (None, "0"):
                sp.set(qn("w:before"), "0")
                changed = True
            if sp.get(qn("w:after")) not in (None, "0"):
                sp.set(qn("w:after"), "0")
                changed = True
            return changed

        self._para_fix("para.space_before_after", mutate)

    def fix_page_break(self) -> None:
        def mutate(p) -> bool:
            if not p.page_break:
                return False
            p.node.find(qn("w:pPr")).remove(p.node.find(qn("w:pPr")).find(qn("w:pageBreakBefore")))
            return True

        self._para_fix("para.page_break_before", mutate)

    def fix_snap(self) -> None:
        def mutate(p) -> bool:
            if p.snap != "0":
                return False
            ppr = p.node.find(qn("w:pPr"))
            el = ppr.find(qn("w:snapToGrid"))
            if el is not None:
                ppr.remove(el)
                return True
            return False

        self._para_fix("para.snap_to_grid", mutate)

    def fix_empty_paragraph(self) -> None:
        removed = 0
        prev_empty = False
        for p in self.model.paras:
            if p.text.strip() or any(r.text.strip() for r in p.runs):
                prev_empty = False
                continue
            has_content = p.node.find(".//" + qn("w:drawing")) is not None or p.node.find(".//" + qn("w:tbl")) is not None
            if has_content:
                prev_empty = False
                continue
            if prev_empty:
                p.node.getparent().remove(p.node)
                removed += 1
            else:
                prev_empty = True
        if removed:
            self.bump("struct.empty_paragraph", removed)

    # ---------- 字体 ----------
    def _font_fix(self, rule_id: str, pick, tag: str) -> None:
        runs = [r for r in self._all_runs() if r.text.strip()]
        pairs = [(pick(r), r) for r in runs]
        values = [v for v, _ in pairs if v]
        if len(values) < 2:
            return
        dom = Counter(values).most_common(1)[0][0]
        if not dom:
            return
        for v, r in pairs:
            if not v or v == dom:
                continue
            rpr = self._rpr(r.node)
            if rpr is None:
                continue
            rfonts = rpr.find(qn("w:rFonts"))
            if rfonts is not None:
                rfonts.set(qn(tag), dom)
                self.bump(rule_id)

    def fix_font_east(self) -> None:
        self._font_fix("font.chinese", lambda r: r.fmt.east, "w:eastAsia")

    def fix_font_ascii(self) -> None:
        self._font_fix("font.western", lambda r: r.fmt.ascii, "w:ascii")

    def fix_font_size(self) -> None:
        runs = [r for r in self._all_runs()
                if r.text.strip() and r.fmt.size and not r.para.is_heading]
        if len(runs) < 5:
            return
        buckets = Counter(round(r.fmt.size / 0.5) for r in runs)
        dom = buckets.most_common(1)[0][0] * 0.5
        for r in runs:
            if abs(r.fmt.size - dom) > 0.6:
                rpr = self._rpr(r.node)
                if rpr is not None:
                    sz = rpr.find(qn("w:sz"))
                    if sz is not None:
                        sz.set(qn("w:val"), str(int(dom * 2)))
                        szcs = rpr.find(qn("w:szCs"))
                        if szcs is not None:
                            szcs.set(qn("w:val"), str(int(dom * 2)))
                        self.bump("font.size")

    def _toggle_fix(self, rule_id: str, tag: str) -> None:
        for r in self._all_runs():
            if not r.text.strip() or r.para.is_heading:
                continue
            rpr = self._rpr(r.node)
            if rpr is None:
                continue
            el = rpr.find(qn(tag))
            if el is not None:
                rpr.remove(el)
                self.bump(rule_id)

    def fix_bold(self) -> None:
        self._toggle_fix("font.bold", "w:b")

    def fix_italic(self) -> None:
        self._toggle_fix("font.italic", "w:i")

    def fix_underline(self) -> None:
        self._toggle_fix("font.underline", "w:u")

    def fix_color(self) -> None:
        for r in self._all_runs():
            if not r.text.strip() or not r.fmt.color or r.fmt.color in NEAR_BLACK:
                continue
            rpr = self._rpr(r.node)
            if rpr is not None:
                self._drop_child(rpr, "w:color")
                self.bump("font.color")

    def fix_highlight(self) -> None:
        for r in self._all_runs():
            if not r.text.strip():
                continue
            rpr = self._rpr(r.node)
            if rpr is None:
                continue
            n = self._drop_child(rpr, "w:highlight") + self._drop_child(rpr, "w:shd")
            if n:
                self.bump("font.highlight", n)

    def fix_effects(self) -> None:
        for r in self._all_runs():
            if not r.text.strip() or not r.fmt.effects:
                continue
            rpr = self._rpr(r.node)
            if rpr is None:
                continue
            n = sum(self._drop_child(rpr, f"w:{name}") for name in r.fmt.effects)
            if n:
                self.bump("font.effects", n)

    def fix_char_spacing(self) -> None:
        for r in self._all_runs():
            if not r.text.strip() or abs(r.fmt.char_spacing) <= 0.01:
                continue
            rpr = self._rpr(r.node)
            if rpr is not None and self._drop_child(rpr, "w:spacing"):
                self.bump("font.char_spacing")

    def fix_scaling(self) -> None:
        for r in self._all_runs():
            if not r.text.strip() or not r.fmt.scaling or r.fmt.scaling == 100:
                continue
            rpr = self._rpr(r.node)
            if rpr is not None and self._drop_child(rpr, "w:w"):
                self.bump("font.scaling")

    def fix_vert(self) -> None:
        for r in self._all_runs():
            if not r.text.strip() or not r.fmt.vert:
                continue
            rpr = self._rpr(r.node)
            if rpr is not None and self._drop_child(rpr, "w:vertAlign"):
                self.bump("font.script")

    # ---------- 文本 ----------
    def _rewrite_text(self, rule_id: str, transform) -> None:
        for r in self._all_runs():
            if not r.text.strip():
                continue
            new = transform(r.text)
            if new != r.text:
                for t in r.node.findall(qn("w:t")):
                    r.node.remove(t)
                el = r.node.makeelement(qn("w:t"), {})
                el.text = new
                el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                # 插到 rPr 之后
                rpr = self._rpr(r.node)
                idx = list(r.node).index(rpr) + 1 if rpr is not None else 0
                r.node.insert(idx, el)
                self.bump(rule_id)

    def fix_english_punct(self) -> None:
        pat = re.compile(r"([\u4e00-\u9fff])([,;:!?()])([\u4e00-\u9fff])")

        def transform(text: str) -> str:
            prev = None
            while prev != text:
                prev = text
                text = pat.sub(lambda m: m.group(1) + HALF_TO_FULL[m.group(2)] + m.group(3), text)
            return text

        self._rewrite_text("text.english_punct", transform)

    def fix_spaces(self) -> None:
        self._rewrite_text("text.spaces", lambda t: re.sub(r"  +", " ", t))

    def fix_linebreak(self) -> None:
        for r in self._all_runs():
            brs = [b for b in r.node.findall(qn("w:br")) if b.get(qn("w:type")) in (None, "textWrapping")]
            if len(brs) != 1:
                continue
            for b in brs:
                r.node.remove(b)
            self.bump("text.linebreak")

    def fix_halfwidth(self) -> None:
        def transform(text: str) -> str:
            out = []
            for i, ch in enumerate(text):
                if ch in "()" and (
                    (i > 0 and re.match(r"[\u4e00-\u9fff]", text[i - 1]))
                    or (i + 1 < len(text) and re.match(r"[\u4e00-\u9fff]", text[i + 1]))
                ):
                    out.append(HALF_TO_FULL[ch])
                else:
                    out.append(ch)
            return "".join(out)

        self._rewrite_text("text.halfwidth_punct", transform)

    def fix_fullwidth_digit(self) -> None:
        self._rewrite_text("text.fullwidth_digit", lambda t: "".join(FULL_DIGITS.get(c, c) for c in t))

    def fix_fullwidth_latin(self) -> None:
        self._rewrite_text("text.fullwidth_latin", lambda t: "".join(FULL_LATIN.get(c, c) for c in t))

    # ---------- 表格 ----------
    def fix_table_indent(self) -> None:
        for p in self.model.paras:
            if not p.where.startswith("表格") or not p.first_line:
                continue
            ppr = p.node.find(qn("w:pPr"))
            ind = ppr.find(qn("w:ind"))
            if ind is not None:
                ppr.remove(ind)
                self.bump("table.indent")

    # ---------- 执行入口 ----------
    FIXES: dict[str, str] = {
        "text.hidden": "fix_hidden_text",
        "struct.revision": "fix_revisions",
        "struct.comment": "fix_comments",
        "struct.hyperlink": "fix_hyperlinks",
        "struct.doc_props": "fix_doc_props",
        "image.meta": "fix_image_meta",
        "image.color": "fix_image_color",
        "page.background": "fix_background",
        "para.alignment": "fix_alignment",
        "para.line_spacing": "fix_line_spacing",
        "para.first_line_indent": "fix_first_line",
        "para.space_before_after": "fix_space",
        "para.page_break_before": "fix_page_break",
        "para.snap_to_grid": "fix_snap",
        "struct.empty_paragraph": "fix_empty_paragraph",
        "font.chinese": "fix_font_east",
        "font.western": "fix_font_ascii",
        "font.size": "fix_font_size",
        "font.bold": "fix_bold",
        "font.italic": "fix_italic",
        "font.underline": "fix_underline",
        "font.color": "fix_color",
        "font.highlight": "fix_highlight",
        "font.effects": "fix_effects",
        "font.char_spacing": "fix_char_spacing",
        "font.scaling": "fix_scaling",
        "font.script": "fix_vert",
        "text.english_punct": "fix_english_punct",
        "text.spaces": "fix_spaces",
        "text.linebreak": "fix_linebreak",
        "text.halfwidth_punct": "fix_halfwidth",
        "text.fullwidth_digit": "fix_fullwidth_digit",
        "text.fullwidth_latin": "fix_fullwidth_latin",
        "table.indent": "fix_table_indent",
    }


def fix(path: str | Path, rule_ids: list[str], out_path: str | Path) -> dict:
    """执行选中规则的修复，输出到 out_path。返回 {fixed: {rule: count}, notes}。

    分两阶段：内存阶段（改 python-docx 树 → save）之后是 zip 阶段
    （重写媒体文件与 docProps，必须发生在 save 之后，否则会被覆盖）。
    """
    fixer = Fixer(path)
    zip_rules = {"image.meta", "image.color"}
    pending_zip = []

    for rule_id in rule_ids:
        if rule_id in zip_rules:
            pending_zip.append(rule_id)
            continue
        method = Fixer.FIXES.get(rule_id)
        if not method:
            fixer.notes.append(f"规则 {rule_id} 不支持自动修复，已跳过")
            continue
        try:
            getattr(fixer, method)()
        except Exception as exc:  # 单条失败不影响其它
            fixer.notes.append(f"规则 {rule_id} 修复失败：{exc}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fixer.doc.save(str(out))

    if pending_zip:
        fixer.path = out  # zip 阶段直接在输出文件上做
        for rule_id in pending_zip:
            try:
                getattr(fixer, Fixer.FIXES[rule_id])()
            except Exception as exc:
                fixer.notes.append(f"规则 {rule_id} 修复失败：{exc}")

    return {"fixed": fixer.counts, "notes": fixer.notes, "path": str(out)}
