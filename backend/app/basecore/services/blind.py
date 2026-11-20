"""暗标合规检查与一键脱敏。

暗标评审要求投标文件不得出现任何能识别投标人身份的信息。本模块：
1. 检查：文档属性（作者/公司/最后修改者）、正文（含表格与文本框）、页眉页脚、
   批注与修订残留、图片数量（图片无法自动脱敏，只提醒人工核对 logo）；
2. 脱敏：生成一份副本，属性清空、命中敏感词替换为"█"，页眉页脚同步处理。
   只改副本，用户原文件不动。

全部基于 python-docx + lxml，不调用任何外部服务。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import blind_rules
from . import docx_audit

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


class BlindError(RuntimeError):
    pass


# ---------------- 收集所有文本区域 ----------------
def _iter_paragraphs_in_table(table: Table, path: str):
    for ri, row in enumerate(table.rows):
        for ci, cell in enumerate(row.cells):
            for child in cell._element.iterchildren():
                tag = child.tag.split("}")[-1]
                if tag == "p":
                    yield "表格", f"{path} R{ri + 1}C{ci + 1}", Paragraph(child, cell)
                elif tag == "tbl":
                    yield from _iter_paragraphs_in_table(Table(child, cell), f"{path} R{ri + 1}C{ci + 1}")


def _zones(doc: Document) -> list[tuple[str, str, Paragraph]]:
    """返回 [(区域名, 定位, Paragraph)]。区域：正文 / 表格 / 页眉 / 页脚。"""
    out: list[tuple[str, str, Paragraph]] = []

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            out.append(("正文", "", Paragraph(child, doc)))
        elif tag == "tbl":
            out.extend(_iter_paragraphs_in_table(Table(child, doc), "表格"))

    for si, section in enumerate(doc.sections, 1):
        for label, part in (
            ("页眉", section.header),
            ("页脚", section.footer),
            ("首页页眉", section.first_page_header),
            ("首页页脚", section.first_page_footer),
            ("奇数页页眉", section.even_page_header),
            ("奇数页页脚", section.even_page_footer),
        ):
            if part is None:
                continue
            for para in part.paragraphs:
                out.append((f"第{si}节{label}", "", para))
            for table in part.tables:
                out.extend(_iter_paragraphs_in_table(table, f"第{si}节{label}表格"))
    return out


# ---------------- 检查 ----------------
# 同一个词被多路识别时的类别取舍：越具体越靠前，合并后保留信息量更大的标签。
_KIND_RANK = {"企业名称": 3, "第三方机构": 2, "自定义词": 1}


def check(
    path: Path,
    words: list[str] | None = None,
    *,
    preset_words: bool = True,
    exclude_orgs: set[str] | None = None,
) -> dict:
    """检查暗标合规：自动识别证件号/企业名/联系方式，可叠加用户自定义敏感词。

    preset_words=False 时只跑自定义词（兼容旧调用）。
    """
    if path.suffix.lower() != ".docx":
        raise BlindError("暗标检查仅支持 .docx，请先在 Word / WPS 中另存为 .docx")
    custom = [w.strip() for w in (words or []) if w and w.strip()]
    custom = sorted(set(custom), key=len, reverse=True)

    doc = Document(str(path))
    zones = _zones(doc)
    total_chars = sum(len(p.text) for _, _, p in zones)

    # 1) 自动识别 + 2) 自定义词命中
    entities = blind_rules.scan_doc(doc, zones, exclude_orgs) if preset_words else []
    hits: list[dict] = []
    # 同一处泄漏在多个段落/表格里重复出现时合并成一条，带出现次数——
    # 否则一个公司名会在问题清单里刷出十几条，用户根本看不完。
    # 按「词」合并而不是按「类别+词」：用户把公司名也填进自定义敏感词时，
    # 同一个词会同时以「企业名称」和「自定义词」各报一次，看起来像两个问题。
    # 类别取更具体的那个（企业名称 > 第三方机构 > 自定义词）。
    merged: dict[str, dict] = {}

    def _add(zone: str, kind: str, word: str, excerpt: str, note: str, para: str = "") -> None:
        row = merged.get(word)
        if row is not None:
            row["count"] += 1
            if _KIND_RANK.get(kind, 0) > _KIND_RANK.get(row["kind"], 0):
                row["kind"] = kind
                row["note"] = note or row["note"]
            return
        merged[word] = {
            "zone": zone,
            "kind": kind,
            "word": word,
            "excerpt": excerpt,
            "para": (para or excerpt)[:80],
            "note": note or "",
            "count": 1,
        }
        hits.append(merged[word])

    for item in entities:
        _add(item["zone"], item["kind"], item["value"], item["excerpt"], item.get("note") or "")

    for zone, locator, para in zones:
        text = para.text
        if not text.strip() or not custom:
            continue
        occupied: list[tuple[int, int]] = []
        for word in custom:  # 已按长度降序
            start = 0
            while True:
                index = text.find(word, start)
                if index < 0:
                    break
                start = index + 1
                end = index + len(word)
                if any(s <= index and end <= e for s, e in occupied):
                    continue
                occupied.append((index, end))
                left = max(0, index - 16)
                right = min(len(text), end + 16)
                _add(
                    zone + (f"（{locator}）" if locator else ""),
                    "自定义词",
                    word,
                    text[left:right].replace("\n", " "),
                    "",
                    text,
                )
                if len(hits) >= 400:
                    break
            if len(hits) >= 400:
                break
        if len(hits) >= 400:
            break

    # 3) 文档属性
    props: dict[str, str] = {}
    cp = doc.core_properties
    for key, label in (
        ("author", "作者"),
        ("last_modified_by", "最后修改者"),
        ("title", "标题"),
        ("subject", "主题"),
        ("comments", "备注"),
        ("category", "类别"),
        ("keywords", "关键词"),
    ):
        value = str(getattr(cp, key) or "").strip()
        if value:
            props[label] = value
    prop_hits = [
        {"field": label, "value": value, "words": [w for w in custom if w in value] or ["—"]}
        for label, value in props.items()
        if (not custom or any(w in value for w in custom))
    ]
    company = _read_company(path)

    # 4) 结构痕迹 + 5) 格式规则
    xml = _read_document_xml(path)
    revisions = len(xml.findall(".//w:ins", NS)) + len(xml.findall(".//w:del", NS))
    comments = len(xml.findall(".//w:commentReference", NS))
    images = len(xml.findall(".//w:drawing", NS)) + len(xml.findall(".//w:pict", NS))
    fmt_issues = blind_rules.format_issues(doc, xml)

    # 6) 全量规则审计（62 条，隐私/结构/图片/页面/段落/字体/文本/表格/一致性）
    try:
        rules = docx_audit.audit(path)
    except Exception:
        rules = {"total": 0, "by_severity": {}, "by_category": [], "rule_total": 0, "fixable": []}

    by_kind: dict[str, int] = {}
    for h in hits:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1

    serious = {"身份证号", "统一社会信用代码", "企业名称", "手机号", "固定电话", "电子邮箱", "银行账号", "自定义词"}
    high = any(k in serious for k in by_kind) or any(i["level"] == "高" for i in fmt_issues)
    medium = bool(hits) or bool(prop_hits) or bool(company) or any(i["level"] == "中" for i in fmt_issues)
    risk = "high" if high else ("medium" if medium else "low")

    return {
        "words": custom,
        "summary": {
            "zones": len({h["zone"] for h in hits}),
            "hits": len(hits),
            "hit_paras": len({h["para"] for h in hits}),
            "auto_hits": len(entities),
            "custom_hits": len([h for h in hits if h["kind"] == "自定义词"]),
            "by_kind": by_kind,
            "prop_hits": len(prop_hits),
            "company": company,
            "revisions": revisions,
            "comments": comments,
            "images": images,
            "format_issues": len(fmt_issues),
            "rules_total": rules.get("total", 0),
            "rules_high": (rules.get("by_severity") or {}).get("high", 0),
            "rule_count": rules.get("rule_total", 0),
            "chars": total_chars,
            "risk": risk,
        },
        "props": props,
        "prop_hits": prop_hits,
        "hits": hits,
        "entities": entities,
        "format_issues": fmt_issues,
        "rules": rules,
        "advice": _advice(hits, prop_hits, company, revisions, comments, images, fmt_issues),
    }


def _advice(
    hits: list,
    prop_hits: list,
    company: str | None,
    revisions: int,
    comments: int,
    images: int,
    fmt_issues: list | None = None,
) -> list[str]:
    tips: list[str] = []
    if fmt_issues:
        tips.extend(f"{i['type']}：{i['title']}" for i in fmt_issues[:4])
    if hits:
        tips.append(f"正文/页眉页脚发现 {len(hits)} 处敏感信息，逐处点击脱敏或手工删除。")
    if prop_hits:
        tips.append("文档属性中含单位信息，Word「文件-信息-检查问题」可一键清除；导出副本已自动清除。")
    if company:
        tips.append(f"Office 属性里的公司字段为“{company}”，导出副本已清除。")
    if revisions:
        tips.append(f"文档还有 {revisions} 处修订痕迹，请在 Word 中「接受所有修订并停止修订」后重新导出再检查。")
    if comments:
        tips.append(f"文档有 {comments} 处批注，请删除全部批注。")
    if images:
        tips.append(f"文档含 {images} 张图片，无法自动脱敏，请逐一确认不含 logo、营业执照照片或带单位名称的截图。")
    if not tips:
        tips.append("未发现身份残留。仍建议打印前抽查页眉、封面与落款页。")
    return tips


# ---------------- 脱敏 ----------------
def _replace_in_paragraph(para: Paragraph, words: list[str], mask: str) -> int:
    """先逐 run 替换，跨 run 的命中把整段合并到第一个 run 再替换。返回替换次数。"""
    count = 0
    for run in para.runs:
        for word in words:
            if word in run.text:
                count += run.text.count(word)
                run.text = run.text.replace(word, mask)
    text = para.text
    if any(word in text for word in words):
        # 跨 run：把全部文字合并到第一个 run 再替换（段落级格式保留）
        merged = "".join(run.text for run in para.runs)
        for word in words:
            n = merged.count(word)
            if n:
                count += n
                merged = merged.replace(word, mask)
        runs = list(para.runs)
        if runs:
            runs[0].text = merged
            for r in runs[1:]:
                r.text = ""
    return count


def sanitize(path: Path, words: list[str], out_path: Path, mask: str = "█") -> dict:
    """生成脱敏副本。返回替换统计。"""
    if path.suffix.lower() != ".docx":
        raise BlindError("脱敏仅支持 .docx 文件")
    clean_words = [w.strip() for w in (words or []) if w and w.strip()]
    doc = Document(str(path))
    if not clean_words:
        # 未填敏感词时，用自动识别到的实体来脱敏
        auto = blind_rules.scan_doc(doc, _zones(doc))
        clean_words = sorted({item["value"] for item in auto if item["kind"] != "占位符"}, key=len, reverse=True)
        if not clean_words:
            raise BlindError("未识别到需要脱敏的内容，可手工补充敏感词后再试")
    clean_words = sorted(set(clean_words), key=len, reverse=True)
    replaced = 0

    for zone, _, para in _zones(doc):
        replaced += _replace_in_paragraph(para, clean_words, mask)

    cp = doc.core_properties
    for key in ("author", "last_modified_by", "title", "subject", "comments", "category", "keywords"):
        try:
            setattr(cp, key, "")
        except Exception:  # 个别属性只读时不阻断
            pass

    doc.save(str(out_path))
    # 清 app.xml 的 Company / Manager
    cleared = _strip_app_fields(out_path)
    return {"ok": True, "replaced": replaced, "company_cleared": cleared, "file": out_path.name}


# ---------------- XML 层工具 ----------------
def _read_company(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read("docProps/app.xml").decode("utf-8", errors="ignore")
    except (KeyError, OSError):
        return None
    match = re.search(r"<Company>([^<]+)</Company>", data)
    return match.group(1).strip() if match else None


def _strip_app_fields(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            data = {n: zf.read(n) for n in names}
        app = data.get("docProps/app.xml")
        if not app:
            return False
        text = app.decode("utf-8", errors="ignore")
        text = re.sub(r"<Company>[^<]*</Company>", "<Company></Company>", text)
        text = re.sub(r"<Manager>[^<]*</Manager>", "<Manager></Manager>", text)
        data["docProps/app.xml"] = text.encode("utf-8")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in data.items():
                zf.writestr(name, content)
        return True
    except (OSError, zipfile.BadZipFile):
        return False


def _read_document_xml(path: Path):
    from lxml import etree

    with zipfile.ZipFile(path) as zf:
        return etree.fromstring(zf.read("word/document.xml"))
