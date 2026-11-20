"""文件解析：纯本地策略（docx 表格不丢、PDF 文本层、xlsx 逐格）。
改成面向上传字节流，供招标文件 / 已有竞标书 / 本项目资料 / 企业素材库共用。

证据级页码映射：保留「物理页 ↔ 原文页码 ↔ 章节」三套坐标。
分层识别：能用文本层就不用 OCR；无文字层的页才走本地 OCR；
OCR 引擎缺失时该页显式标「未识别 / 需人工录入」，绝不猜测填充。

R05：区分「展示文本」与「有效正文」。页码标记、工作表名称、扫描占位符都是
合成内容，不计入有效字数，也不能撑起一次「导入成功」。解析不完整的资料带
needs_review 走完整链路，在人工核对前不作为已确认要求或事实依据。

Word 内嵌图片走「只给人看」的独立通道：图片文字用本地 OCR 摘出，放进
image_text 供人工核对，绝不并进 text（二维码、系统截图会污染检索与依据）。
有图片的文件仍标 needs_review，确认前不自动放行。
"""
from __future__ import annotations

import csv
import io
import re
import zipfile

SUPPORTED = {".docx", ".pdf", ".xlsx", ".csv", ".txt", ".md", ".markdown"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 按「有效载荷」算，不是按压缩包原始体积
HINT = {
    ".doc": "老版 Word（.doc）无法直接解析，请用 Word 另存为 .docx 后重传",
    ".xls": "老版 Excel（.xls）无法直接解析，请另存为 .xlsx 后重传",
    ".wps": "WPS 自有格式请另存为 .docx 后重传",
    ".png": "图片需先转成 PDF，或手工粘贴文字",
    ".jpg": "图片需先转成 PDF，或手工粘贴文字",
    ".jpeg": "图片需先转成 PDF，或手工粘贴文字",
    ".ofd": "OFD 请先转成 PDF 后重传",
}
SCAN_MARK = "（本页为扫描件：未识别到文字，需人工录入或安装本地 OCR 引擎）"

# 合成标记：由解析器自己写进去的，不是文件里的正文
RE_PAGE_MARK = re.compile(r"【第\s*\d+\s*页】")
RE_SHEET_MARK = re.compile(r"【工作表：[^\n]*】")

_OCR_CACHE: tuple[str, object] | None = None


class ParseError(RuntimeError):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _clean(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def effective_len(text: str) -> int:
    """真正从文件里读出来 / OCR 出来的字，去掉所有合成标记与空白。"""
    body = RE_PAGE_MARK.sub("", text.replace(SCAN_MARK, ""))
    body = RE_SHEET_MARK.sub("", body)
    return len(re.sub(r"\s+", "", body))


def _slim_docx(data: bytes) -> tuple[bytes, int]:
    """剥离 docx 内嵌 OLE 对象的内容，返回（精简后数据，释放的字节数）。

    国网等真实招标文件常把 Excel / 签章对象以 word/embeddings/*.bin 内嵌，
    单份可占 20MB 以上，而 document.xml 正文与表格只有几 MB。按压缩包体积
    一刀切会把这类文件全部拒之门外。这里只把嵌件内容置空、保留条目与关系
    引用，python-docx 仍能正常打开，正文和表格一个字都不丢。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as src:
            embedded = [i.filename for i in src.infolist()
                        if i.filename.startswith("word/embeddings/")]
            if not embedded:
                return data, 0
            freed = sum(src.getinfo(n).file_size for n in embedded)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    payload = b"" if item.filename in embedded else src.read(item.filename)
                    dst.writestr(item, payload)
            return buf.getvalue(), freed
    except Exception:
        return data, 0


def _docx(data: bytes) -> tuple[str, int]:
    """返回（正文，内嵌图片数）。内嵌图片目前不做 OCR，只如实报告数量。"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
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
    try:
        images = len(doc.inline_shapes)
    except Exception:
        images = 0
    return _clean("\n".join(lines)), images


# 内嵌图片的文字只进「供人工核对」字段，绝不并进 text：
# 真实招标文件里常有加群二维码、系统操作截图等装饰图，OCR 出来的字若混进正文，
# 会污染按章检索、要求表抽取与事实依据，比不识别更有害。
_RASTER_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
MAX_IMAGE_OCR = 8
MAX_IMAGE_TEXT = 4000


def _ocr_image_bytes(raw: bytes, engine: tuple[str, object]) -> str:
    name, obj = engine
    if not name:
        return ""
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if name == "rapidocr_onnxruntime":
            result, _ = obj(np.array(img))  # type: ignore[operator]
            return _clean("\n".join(str(x[1]) for x in (result or []) if len(x) > 1))
        if name == "tesseract":
            import pytesseract
            return _clean(pytesseract.image_to_string(img, lang="chi_sim+eng"))
    except Exception:
        return ""
    return ""


def docx_image_text(data: bytes) -> tuple[str, int, str]:
    """OCR Word 内嵌位图，返回（供人工核对的文字、已识别张数、引擎名）。

    只处理位图；.emf/.wmf 是内嵌 Excel/OLE 对象的预览图，不是可识别内容。
    结果按张编号、带明确免责标题，调用方只能把它展示给人看。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [i.filename for i in z.infolist()
                     if i.filename.lower().endswith(_RASTER_EXT)
                     # thumbnail.* 是 Word 文件自身缩略图，不是文档里的插图
                     and not i.filename.rsplit("/", 1)[-1].lower().startswith("thumbnail.")]
            if not names:
                return "", 0, ""
            engine = ocr_engine()
            if not engine[0]:
                return "", 0, ""
            blocks: list[str] = []
            done = 0
            for idx, n in enumerate(sorted(names)[:MAX_IMAGE_OCR], 1):
                txt = _ocr_image_bytes(z.read(n), engine)
                if txt:
                    done += 1
                    blocks.append(f"[内嵌图片{idx}·{n.rsplit('/', 1)[-1]}]\n{txt}")
                joined = "\n\n".join(blocks)
                if len(joined) >= MAX_IMAGE_TEXT:
                    break
            return "\n\n".join(blocks)[:MAX_IMAGE_TEXT], done, engine[0]
    except Exception:
        return "", 0, ""


def ocr_engine() -> tuple[str, object]:
    """按可用性选本地 OCR 引擎：rapidocr_onnxruntime > tesseract > 无。"""
    global _OCR_CACHE
    if _OCR_CACHE is not None:
        return _OCR_CACHE  # type: ignore[return-value]
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_CACHE = ("rapidocr_onnxruntime", RapidOCR())
        return _OCR_CACHE
    except Exception:
        pass
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        _OCR_CACHE = ("tesseract", None)
        return _OCR_CACHE
    except Exception:
        pass
    _OCR_CACHE = ("", None)
    return _OCR_CACHE


def _ocr_page(page, engine: tuple[str, object]) -> str:
    name, obj = engine
    if not name:
        return ""
    try:
        import numpy as np
        pix = page.get_pixmap(dpi=180, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if name == "rapidocr_onnxruntime":
            result, _ = obj(img)  # type: ignore[operator]
            return "\n".join(str(x[1]) for x in (result or []) if len(x) > 1)
        if name == "tesseract":
            from PIL import Image
            import pytesseract
            return pytesseract.image_to_string(Image.fromarray(img), lang="chi_sim+eng")
    except Exception:
        return ""
    return ""


def _pdf(data: bytes) -> tuple[str, int, list[dict], int]:
    """逐页解析：文本层优先，扫描页走 OCR，并保留页码映射。

    返回（展示文本、总页数、page_map、有效字数）。有效字数只累加 text/ocr 页，
    scan-unread 页的占位符一个字都不算。
    """
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    parts: list[str] = []
    page_map: list[dict] = []
    engine: tuple[str, object] | None = None
    effective = 0
    for i, page in enumerate(doc, 1):
        text = (page.get_text("text") or "").strip()
        source = "text"
        if len(text) < 12:  # 该页基本没有文字层，交给 OCR
            if engine is None:
                engine = ocr_engine()
            text = _ocr_page(page, engine).strip()
            source = "ocr" if text else "scan-unread"
        if source != "scan-unread":
            effective += effective_len(text)
        body = text or SCAN_MARK
        page_map.append({"page": i, "physical": i, "source": source,
                         "chars": len(re.sub(r"\s+", "", text)), "text": text[:2000]})
        parts.append(f"【第 {i} 页】\n{body}")
    pages = len(doc)
    doc.close()
    return _clean("\n\n".join(parts)), pages, page_map, effective


def _xlsx_read(data: bytes, read_only: bool) -> tuple[str, int, int, int]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=read_only, data_only=True)
    lines: list[str] = []
    cell_chars = 0
    rows = 0
    try:
        for ws in wb.worksheets:
            lines.append(f"【工作表：{ws.title}】")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() if c is not None else "" for c in row]
                if any(cells):
                    lines.append(" | ".join(cells))
                    cell_chars += len(re.sub(r"\s+", "", "".join(cells)))
                    rows += 1
        sheets = len(wb.worksheets)
    finally:
        wb.close()
    return _clean("\n".join(lines)), sheets, cell_chars, rows


def _xlsx(data: bytes) -> tuple[str, int, int]:
    """返回（正文、工作表数、单元格有效字数）。空工作簿只有工作表名，不算内容。

    部分平台导出的 xlsx（如国网货物清单）没有正确的 dimension 元数据，
    read_only 模式只会看到表头甚至只有第一格，整张清单会被误判成空表。
    先走省内存的 read_only，行数异常少时退回常规模式重读一次。
    """
    text, sheets, cell_chars, rows = _xlsx_read(data, True)
    if rows <= 1 and cell_chars < 200:
        text2, sheets2, chars2, _ = _xlsx_read(data, False)
        if chars2 > cell_chars:
            return text2, sheets2, chars2
    return text, sheets, cell_chars


def _csv(data: bytes) -> str:
    text = _decode(data)
    lines = []
    for row in csv.reader(io.StringIO(text)):
        if any(c.strip() for c in row):
            lines.append(" | ".join(c.strip() for c in row))
    return _clean("\n".join(lines))


def parse_bytes(filename: str, data: bytes, *, min_chars: int = 1) -> dict:
    """解析并返回可直接入库的元数据。

    parse_status: ok（可直接作为依据） / needs_review（有内容但范围不全） /
                  unusable（抛出 ParseError，不允许作为有效资料）
    usable: 在人工核对前能否进入「已确认要求 / 事实依据」
    """
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix not in SUPPORTED:
        raise ParseError(HINT.get(suffix, f"暂不支持 {suffix or '该'} 格式，请另存为 docx / pdf / xlsx / csv / txt"))
    slimmed = 0
    if suffix == ".docx":
        data, slimmed = _slim_docx(data)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ParseError("文件有效内容超过 20MB，请拆分后再上传")
    pages, page_map = 0, []
    extra_note = ""
    images = 0
    image_text, image_ocr_count, image_ocr_engine = "", 0, ""
    if suffix == ".docx":
        if slimmed:
            extra_note = (f"已跳过内嵌 OLE 附件 {slimmed / 1024:.0f} KB，"
                          "正文与表格完整保留")
        text, images = _docx(data)
        effective = len(re.sub(r"\s+", "", text))
        if images:
            image_text, image_ocr_count, image_ocr_engine = docx_image_text(data)
    elif suffix == ".pdf":
        text, pages, page_map, effective = _pdf(data)
    elif suffix == ".xlsx":
        text, sheets, effective = _xlsx(data)
        if not effective:
            raise ParseError(
                f"「{filename}」只有 {sheets} 个工作表名称，没有读出任何单元格内容，未入库。"
                "请确认文件不是空工作簿；若内容在图片或批注里，请另存为文本或粘贴正文。",
                {"parse_status": "unusable", "reason": "empty_workbook"})
    elif suffix == ".csv":
        text = _csv(data)
        effective = effective_len(text)
    else:
        text = _clean(_decode(data))
        effective = len(re.sub(r"\s+", "", text))
    if not page_map:
        page_map = [{"page": 1, "physical": 1, "source": "text" if effective else "scan-unread",
                     "chars": effective, "text": text[:2000]}]
    scan_pages = sum(1 for p in page_map if p["source"] == "scan-unread")
    ocr_pages = sum(1 for p in page_map if p["source"] == "ocr")
    unread_pages = [p["page"] for p in page_map if p["source"] == "scan-unread"]

    # 有效字数为 0：无论展示文本被合成标记撑得多长，都一律不入库
    if effective == 0:
        if pages and scan_pages == pages:
            engine = ocr_engine()[0]
            why = "本机没有可用的 OCR 引擎" if not engine else f"{engine} 未识别出文字"
            raise ParseError(
                f"「{filename}」{pages} 页都是图片扫描件，{why}，有效识别 0 字，未入库。"
                "可上传原生 PDF、先做 OCR，或直接把正文粘贴进来；原件可另行留存为「待处理」。",
                {"parse_status": "unusable", "reason": "scan_no_ocr",
                 "pages": pages, "unread_pages": unread_pages})
        if suffix == ".docx" and images:
            raise ParseError(
                f"「{filename}」里有 {images} 张内嵌图片，但没有可读文字，有效识别 0 字，未入库。"
                "图片文字只作人工核对参考、不并入正文；请把其中属于招标要求的内容"
                "粘贴到正文或转成可复制的表格后再上传。",
                {"parse_status": "unusable", "reason": "image_only_docx",
                 "images": images, "image_text": image_text})
        if suffix == ".docx":
            raise ParseError(f"「{filename}」没有读到文字或表格（文件可能是空的），未入库。请确认内容后重传。",
                             {"parse_status": "unusable", "reason": "empty"})
        raise ParseError(f"「{filename}」里没有可读文字（文件可能是空的），未入库。请确认文件内容后重传。",
                         {"parse_status": "unusable", "reason": "empty"})
    if effective < min_chars:
        raise ParseError(f"「{filename}」只解析出 {effective} 个有效字，不足以作为资料，未入库。",
                         {"parse_status": "unusable", "reason": "too_short", "effective_chars": effective})

    notes: list[str] = []
    if scan_pages:
        notes.append(f"第 {','.join(str(x) for x in unread_pages[:12])}"
                     f"{'等' if len(unread_pages) > 12 else ''} 页未识别，共 {scan_pages} 页，"
                     "这些页上的要求未纳入核查，需人工补录或核对后再确认")
    if ocr_pages:
        notes.append(f"{ocr_pages} 页由本地 OCR 识别，建议对照原件抽查")
    if suffix == ".docx" and images:
        if image_ocr_count:
            notes.append(
                f"另有内嵌图片（文档插图 {images} 张、识别到文字 "
                f"{image_ocr_count} 张），已用 {image_ocr_engine} 摘出图片文字供核对；"
                "这些字未并入正文、不作为依据，请对照原件确认图片里没有遗漏的招标要求")
        else:
            why = "本机没有可用的 OCR 引擎" if not ocr_engine()[0] else "图片中未识别出文字"
            notes.append(f"另有 {images} 张内嵌图片，{why}，图片中的文字/签章需人工核对")
        extra_note = "、".join(
            x for x in (extra_note, "Word 内嵌图片文字只供人工核对，不进正文") if x)
    if effective < 200 and pages > 1:
        notes.append(f"只解析到 {effective} 个有效字，可能只有封面或少数页，请对照原件核对")
    needs_review = bool(notes)
    status = "needs_review" if needs_review else "ok"
    return {
        "text": text, "chars": len(text), "effective_chars": effective,
        "pages": pages, "suffix": suffix,
        "page_map": page_map, "scan_pages": scan_pages, "ocr_pages": ocr_pages,
        "unread_pages": unread_pages, "images": images,
        "image_text": image_text, "image_ocr_count": image_ocr_count,
        "image_ocr_engine": image_ocr_engine,
        "ocr_engine": ocr_engine()[0],
        "parse_status": status, "needs_review": needs_review,
        # 有未核对范围时，已识别部分可读，但不能当成已确认依据
        "usable": not needs_review,
        "review_note": "；".join(notes),
        "limit_note": extra_note,
    }


_UPLOAD_META_KEYS = (
    "effective_chars", "pages", "scan_pages", "ocr_pages", "unread_pages",
    "ocr_engine", "parse_status", "needs_review", "usable", "review_note",
    "limit_note", "suffix", "images",
    "image_text", "image_ocr_count", "image_ocr_engine",
)


def meta_from_attachment(att: dict) -> dict:
    """前端把 /api/upload 的结果原样交给对话接口；这里取出需要落库的解析元数据。"""
    meta = {k: att[k] for k in _UPLOAD_META_KEYS if k in att}
    # 老客户端只传 needs_review / scan_pages 时，也要能正确标成待核对
    if att.get("needs_review") and "parse_status" not in meta:
        meta["parse_status"] = "needs_review"
    if not meta.get("effective_chars"):
        meta["effective_chars"] = effective_len(str(att.get("text") or ""))
    return meta
