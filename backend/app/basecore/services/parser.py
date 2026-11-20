"""文档解析：docx / pdf / xlsx / csv / txt / md。纯本地，不上传任何内容。"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

SUPPORTED = {".docx", ".pdf", ".xlsx", ".csv", ".txt", ".md", ".markdown"}
# 明确不支持但用户最容易传错的格式，给出针对性提示
UNSUPPORTED_HINT = {
    ".doc": "老版 Word（.doc）无法直接解析，请用 Word 另存为 .docx 后重传",
    ".xls": "老版 Excel（.xls）无法直接解析，请另存为 .xlsx 后重传",
    ".wps": "WPS 自有格式无法直接解析，请另存为 .docx 后重传",
    ".zip": "压缩包请解压后再上传其中的文档",
    ".png": "图片需先转成有文字层的 PDF，或安装 OCR 组件（pip install rapidocr-onnxruntime）",
    ".jpg": "图片需先转成有文字层的 PDF，或安装 OCR 组件（pip install rapidocr-onnxruntime）",
    ".jpeg": "图片需先转成有文字层的 PDF，或安装 OCR 组件（pip install rapidocr-onnxruntime）",
}


class ParseError(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_docx(path: Path) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    lines: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif tag == "tbl":
            table = Table(child, doc)
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
    return _clean("\n".join(lines))


def parse_pdf(path: Path, *, allow_ocr: bool = True, max_ocr_pages: int = 40) -> tuple[str, int]:
    """抽 PDF 文本层；文本过薄（扫描件）时自动走本地 OCR。

    返回 (text, pages)。OCR 失败时退回文本层结果，不抛错。
    """
    try:
        import pymupdf as fitz
    except ImportError:  # 兼容旧版 PyMuPDF
        import fitz  # type: ignore

    doc = fitz.open(str(path))
    pages: list[str] = []
    try:
        for page in doc:
            pages.append(page.get_text("text"))
    finally:
        doc.close()
    text = _clean("\n".join(pages))
    total_pages = len(pages)

    # 每页平均文字过少 → 大概率是扫描件，本地 OCR
    if allow_ocr and total_pages and len(text) < 100 * total_pages:
        try:
            ocr_text = _ocr_pdf(path, total_pages, max_ocr_pages)
            if len(ocr_text) > len(text):
                return ocr_text, total_pages
        except ParseError:
            raise
        except Exception:  # noqa: BLE001 — OCR 组件缺失或失败不阻塞，报文字太少的原始错误
            pass
    return text, total_pages


def _ocr_pdf(path: Path, total_pages: int, max_pages: int) -> str:
    """RapidOCR 本地识别（模型随包分发，不出本机）。"""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ParseError(
            "这是扫描版 PDF（没有文字层），本机还没有 OCR 组件。"
            "请在后端环境执行：pip install rapidocr-onnxruntime 后重试。"
        ) from exc

    engine = RapidOCR()
    texts: list[str] = []
    doc = fitz.open(str(path))
    try:
        for idx, page in enumerate(doc):
            if idx >= max_pages:
                texts.append(f"〔OCR 页数上限 {max_pages} 页，后面 {total_pages - max_pages} 页未识别〕")
                break
            try:
                pix = page.get_pixmap(dpi=144)
                result, _elapsed = engine(pix.tobytes("png"))
            except Exception as exc:  # noqa: BLE001
                texts.append(f"〔第 {idx + 1} 页识别失败：{exc}〕")
                continue
            if result:
                texts.append("\n".join(str(line[1]) for line in result))
    finally:
        doc.close()
    return _clean("\n".join(texts))


def parse_xlsx(path: Path) -> str:
    """读全部工作表，每行用 | 分隔——业绩台账、人员表、资质清单多是这种结构。"""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        sheets = wb.worksheets
        for sheet in sheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [
                    str(c).strip().replace("\n", " ")
                    for c in row
                    if c is not None and str(c).strip()
                ]
                if cells:
                    rows.append(" | ".join(cells))
            if not rows:
                continue
            if len(sheets) > 1:
                lines.append(f"【工作表：{sheet.title}】")
            lines.extend(rows)
            lines.append("")
    finally:
        wb.close()
    return _clean("\n".join(lines))


def parse_csv(path: Path) -> str:
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", errors="ignore")
    lines: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row if c and c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return _clean("\n".join(lines))


def parse_file(path: Path, min_chars: int = 40) -> dict:
    """解析文件为纯文本。

    min_chars 是「有效文字」下限：招标文件要求 40 字以上防止把扫描件当文本；
    本项目资料可能只是一小段要点，调用方可传 1 放宽。
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        hint = UNSUPPORTED_HINT.get(suffix)
        if hint:
            raise ParseError(hint)
        raise ParseError(f"暂不支持 {suffix or '该'} 格式，请另存为 docx / pdf / xlsx / csv / txt 后重试")
    if suffix == ".docx":
        text = parse_docx(path)
        page_hint = 0
    elif suffix == ".pdf":
        text, page_hint = parse_pdf(path)
    elif suffix == ".xlsx":
        text = parse_xlsx(path)
        page_hint = 0
    elif suffix == ".csv":
        text = parse_csv(path)
        page_hint = 0
    else:
        text = _clean(path.read_text(encoding="utf-8", errors="ignore"))
        page_hint = 0

    if len(text) < min_chars:
        raise ParseError(
            "未能提取到有效文字——可能是扫描件且 OCR 组件不可用。"
            "安装 OCR：pip install rapidocr-onnxruntime，或改用有文字层的文件。"
        )
    return {"text": text, "chars": len(text), "pages": page_hint, "suffix": suffix}


# ---------- 规则抽取（无 AI 时兜底） ----------
PATTERNS: list[tuple[str, str]] = [
    ("项目编号", r"(?:项目编号|招标编号|采购编号)[：:\s]*([A-Za-z0-9\-—（）()/\.]{4,40})"),
    ("招标人", r"(?:招标人|采购人|招标单位)[：:\s]*([^\n，,。；;]{2,30})"),
    ("代理机构", r"(?:招标代理机构|代理机构)[：:\s]*([^\n，,。；;]{2,30})"),
    # 竖线和冒号都要排除：表格行"3.2.4 | 最高限价 | 无"会被抽成"| 无"
    # 允许千分位逗号（5,000,000 元），只排除句读与竖线
    ("预算金额", r"(?:预算金额|采购预算|最高限价|控制价)[：:\s]*\|?\s*([^\n。；;|｜]{2,40})"),
    ("投标保证金", r"(?:投标保证金|保证金)[：:\s]*(?:人民币)?\s*([0-9,，\.]+\s*(?:万元|元))"),
    ("投标截止", r"(?:投标截止时间|递交投标文件截止时间|响应文件提交截止时间)[：:\s]*([^\n。；;]{6,40})"),
    ("开标时间", r"开标时间[：:\s]*([^\n。；;]{6,40})"),
    ("工期/服务期", r"(?:工期|服务期|交货期|履约期限)[：:\s]*([^\n，,。；;]{2,30})"),
]


def rule_extract(text: str) -> dict:
    """用最保守的正则抽出确定字段，作为 AI 不可用时的兜底展示。"""
    head = text[:20000]
    info: dict[str, str] = {}
    for label, pattern in PATTERNS:
        for match in re.finditer(pattern, head):
            value = re.sub(r"^[为是：:\s]+", "", match.group(1).strip())
            # 金额类字段必须带数字，否则会把"本项目无最高限价"这类表述当金额
            if label == "预算金额" and not re.search(r"\d", value):
                continue
            info[label] = value
            break

    requirements = []
    for match in re.finditer(r"[★▲]\s*([^\n]{4,60})", head):
        requirements.append(match.group(1).strip())
    qualifications = []
    for keyword in ("资质", "业绩", "认证", "证书", "注册资金", "人员"):
        for match in re.finditer(rf"[^\n。；;]{{0,30}}{keyword}[^\n。；;]{{0,30}}", head):
            snippet = match.group(0).strip()
            if 6 <= len(snippet) <= 70 and snippet not in qualifications:
                qualifications.append(snippet)
            if len(qualifications) >= 8:
                break

    scores = []
    for match in re.finditer(r"([^\n，,。；;：:]{2,20})\s*[（(]?\s*(\d{1,2}(?:\.\d)?)\s*分", head):
        name, value = match.group(1).strip(), match.group(2)
        if name and not name.isdigit():
            scores.append({"item": name, "score": value})
        if len(scores) >= 20:
            break

    return {
        "基本信息": info,
        "资格要求": qualifications[:8],
        "评分办法": scores[:20],
        "星号条款": requirements[:20],
        "风险提示": [],
        "source": "rule",
    }
