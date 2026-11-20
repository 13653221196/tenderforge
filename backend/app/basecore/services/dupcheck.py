"""多份投标文件的雷同检查。

思路（全部自研实现，不依赖任何第三方查重服务）：
1. 从 docx / pdf / txt 中抽出正文，按中文标点与换行切成"句"；
2. 用字符 n-gram 倒排索引做候选召回，再用 Dice 系数做精算，得到句级重复；
3. 剔除与招标文件原文高度重合的句子（这是最常见的误报来源——大家都抄招标原文）；
4. 叠加文档属性信号：作者、最后修改人、公司、修订号、创建时间接近度、RSID 编辑痕迹、内嵌图片指纹；
5. 输出两两矩阵 + 每对的证据句子，供人工复核。

注意：结论只作为评标线索，不构成认定。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ---------------- 阈值 ----------------
SENT_MIN_LEN = 20          # 参与比对的句子最短长度（归一化后字符数）
NEAR_DUP_DICE = 0.86       # 句级近重复阈值
GRAM = 3                   # 倒排索引用 3-gram
MAX_SAMPLES = 12           # 每对最多列出的重复句
MAX_TEXT = 400_000         # 单文档参与统计的最大字符数

PUNCT = re.compile(r"[\s\u3000！-～·、。，；：？！“”‘’（）《》【】—…‰%‰\-_/\\|@#$^&*+=~`\[\]{}<>\"']+")
KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


class DupError(RuntimeError):
    pass


# ---------------- 归一化 ----------------
def normalize(text: str) -> str:
    """全角转半角、去标点空白、统一小写。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = KEEP.sub("", text)
    return text.lower()


def split_sentences(text: str) -> list[str]:
    """按中文句末标点与换行切句，并保留较长的段落块。"""
    text = unicodedata.normalize("NFKC", text or "")
    parts = re.split(r"[。！？；\n]+", text)
    result: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 超长段落再按逗号切开，避免一句几千字导致召回失效
        if len(part) > 160:
            result.extend(p.strip() for p in re.split(r"[，、]", part) if p.strip())
        else:
            result.append(part)
    return result


def grams(text: str, n: int = GRAM) -> list[str]:
    if len(text) <= n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def dice(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = set(grams(a)), set(grams(b))
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return 2 * inter / (len(ga) + len(gb))


# ---------------- 文档抽取 ----------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _read_docx(path: Path) -> dict:
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    DC = "{http://purl.org/dc/elements/1.1/}"
    DCTERMS = "{http://purl.org/dc/terms/}"
    CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
    EP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"

    paragraphs: list[str] = []
    rsids: set[str] = set()
    images: list[str] = []
    meta: dict = {}

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "word/document.xml" in names:
            root = ET.fromstring(zf.read("word/document.xml"))
            for para in root.iter(f"{W}p"):
                texts = [node.text or "" for node in para.iter(f"{W}t")]
                line = "".join(texts).strip()
                if line:
                    paragraphs.append(line)
                for key, value in para.attrib.items():
                    if "rsid" in key.lower() and value:
                        rsids.add(value.upper())
        if "word/settings.xml" in names:
            try:
                root = ET.fromstring(zf.read("word/settings.xml"))
                for node in root.iter():
                    if node.tag.endswith("}rsid"):
                        value = node.attrib.get(f"{W}val") or ""
                        if value:
                            rsids.add(value.upper())
            except ET.ParseError:
                pass
        if "docProps/core.xml" in names:
            try:
                root = ET.fromstring(zf.read("docProps/core.xml"))
                for child in root:
                    tag = child.tag
                    value = (child.text or "").strip()
                    if not value:
                        continue
                    if tag == f"{DC}creator":
                        meta["author"] = value
                    elif tag == f"{CP}lastModifiedBy":
                        meta["last_modified_by"] = value
                    elif tag == f"{CP}revision":
                        meta["revision"] = value
                    elif tag == f"{DCTERMS}created":
                        meta["created"] = value
                    elif tag == f"{DCTERMS}modified":
                        meta["modified"] = value
            except ET.ParseError:
                pass
        if "docProps/app.xml" in names:
            try:
                root = ET.fromstring(zf.read("docProps/app.xml"))
                for child in root:
                    if child.tag == f"{EP}Company" and (child.text or "").strip():
                        meta["company"] = child.text.strip()
                    elif child.tag == f"{EP}Application" and (child.text or "").strip():
                        meta["application"] = child.text.strip()
            except ET.ParseError:
                pass
        for name in names:
            if name.startswith("word/media/"):
                try:
                    images.append(hashlib.sha256(zf.read(name)).hexdigest())
                except (KeyError, OSError):
                    continue

    return {
        "paragraphs": paragraphs,
        "text": "\n".join(paragraphs),
        "meta": meta,
        "rsids": rsids,
        "images": images,
        "format": "docx",
    }


def _read_pdf(path: Path) -> dict:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 兼容旧包名
        import fitz as pymupdf  # type: ignore

    paragraphs: list[str] = []
    images: list[str] = []
    with pymupdf.open(path) as doc:
        meta_raw = doc.metadata or {}
        for page in doc:
            for block in page.get_text("blocks"):
                text = (block[4] or "").strip()
                if text:
                    paragraphs.append(text)
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    data = doc.extract_image(xref)
                    images.append(hashlib.sha256(data["image"]).hexdigest())
                except Exception:  # noqa: BLE001 - 图片损坏不影响文本比对
                    continue
    meta = {}
    if meta_raw.get("author"):
        meta["author"] = meta_raw["author"]
    if meta_raw.get("creator"):
        meta["application"] = meta_raw["creator"]
    if meta_raw.get("producer"):
        meta["producer"] = meta_raw["producer"]
    if meta_raw.get("creationDate"):
        meta["created"] = meta_raw["creationDate"]
    if meta_raw.get("modDate"):
        meta["modified"] = meta_raw["modDate"]
    return {
        "paragraphs": paragraphs,
        "text": "\n".join(paragraphs),
        "meta": meta,
        "rsids": set(),
        "images": images,
        "format": "pdf",
    }


def _read_txt(path: Path) -> dict:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "ignore")
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "paragraphs": paragraphs,
        "text": "\n".join(paragraphs),
        "meta": {},
        "rsids": set(),
        "images": [],
        "format": "txt",
    }


def read_document(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".txt", ".md"):
        return _read_txt(path)
    raise DupError(f"暂不支持 {suffix or '该'} 格式，请使用 docx / pdf / txt")


# ---------------- 时间与元数据信号 ----------------
def _parse_dt(value: str | None):
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    match = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", value)
    if match:
        parts = [int(p) if p else 0 for p in match.groups()]
        try:
            return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
        except ValueError:
            return None
    return None


def meta_signals(a: dict, b: dict) -> list[str]:
    signals: list[str] = []
    ma, mb = a["meta"], b["meta"]
    for key, label in (
        ("author", "作者"),
        ("last_modified_by", "最后修改人"),
        ("company", "公司字段"),
        ("revision", "修订号"),
        ("application", "生成程序"),
        ("producer", "PDF 生成器"),
    ):
        va, vb = (ma.get(key) or "").strip(), (mb.get(key) or "").strip()
        if va and va == vb:
            signals.append(f"{label}相同：{va}")
    for key, label in (("created", "创建时间"), ("modified", "修改时间")):
        ta, tb = _parse_dt(ma.get(key)), _parse_dt(mb.get(key))
        if ta and tb and abs((ta - tb).total_seconds()) <= 300:
            signals.append(f"{label}相差不超过 5 分钟")
    if a["rsids"] and b["rsids"]:
        inter = a["rsids"] & b["rsids"]
        if inter:
            ratio = len(inter) / min(len(a["rsids"]), len(b["rsids"]))
            if ratio >= 0.25:
                signals.append(f"编辑痕迹（RSID）重合 {ratio:.0%}，可能同源编辑")
    if a["images"] and b["images"]:
        shared = set(a["images"]) & set(b["images"])
        if shared:
            signals.append(f"存在 {len(shared)} 张完全相同的内嵌图片")
    return signals


# ---------------- 句级比对 ----------------
def _build_index(sentences: list[str]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = defaultdict(list)
    for idx, sentence in enumerate(sentences):
        for gram in set(grams(sentence)):
            index[gram].append(idx)
    return index


def _match_sentences(
    left: list[str], right: list[str], right_index: dict[str, list[int]]
) -> list[tuple[int, int, float]]:
    """对 left 的每句在 right 中找最相似的候选，返回 (i, j, dice)。"""
    hits: list[tuple[int, int, float]] = []
    right_len = len(right)
    for i, sentence in enumerate(left):
        gram_list = grams(sentence)
        if not gram_list:
            continue
        counts: Counter[int] = Counter()
        for gram in set(gram_list):
            for j in right_index.get(gram, ()):  # type: ignore[arg-type]
                counts[j] += 1
        if not counts:
            continue
        # 共享 gram 数越多越可能相似，取前 8 个精算
        for j, _ in counts.most_common(8):
            if j >= right_len:
                continue
            score = dice(sentence, right[j])
            if score >= NEAR_DUP_DICE:
                hits.append((i, j, score))
                break
    return hits


def _whitelist_set(tender_text: str) -> tuple[set[str], dict[str, list[int]], list[str]]:
    if not (tender_text or "").strip():
        return set(), {}, []
    sentences = [normalize(s) for s in split_sentences(tender_text)]
    sentences = [s for s in sentences if len(s) >= SENT_MIN_LEN]
    return set(sentences), _build_index(sentences), sentences


def _is_from_tender(
    sentence: str, tender_set: set[str], tender_index: dict[str, list[int]], tender_sentences: list[str]
) -> bool:
    if sentence in tender_set:
        return True
    if len(sentence) < 30:
        return False
    counts: Counter[int] = Counter()
    for gram in set(grams(sentence)):
        for j in tender_index.get(gram, ()):  # type: ignore[arg-type]
            counts[j] += 1
    for j, shared in counts.most_common(3):
        if shared / max(1, len(set(grams(sentence)))) >= 0.7:
            if dice(sentence, tender_sentences[j]) >= 0.75:
                return True
    return False


def _describe_pair(
    a: dict, b: dict, tender: tuple[set[str], dict[str, list[int]], list[str]]
) -> dict:
    tender_set, tender_index, tender_sentences = tender
    left, right = a["sentences"], b["sentences"]

    whitelisted = 0
    if tender_set:
        filtered_left = []
        for sentence in left:
            if _is_from_tender(sentence, tender_set, tender_index, tender_sentences):
                whitelisted += 1
            else:
                filtered_left.append(sentence)
        left = filtered_left

    rignt_index = _build_index(right)
    hits = _match_sentences(left, right, rignt_index)
    reuse = len(hits) / max(1, len(left))

    # 整体相似度：取双向较低值，避免一份长文件被短文件稀释
    stream_a = normalize(a["text"])[:MAX_TEXT]
    stream_b = normalize(b["text"])[:MAX_TEXT]
    overall = dice(stream_a, stream_b)

    signals = meta_signals(a, b)
    score = round(100 * min(1.0, 0.55 * overall + 0.45 * reuse))

    if reuse >= 0.30 or (reuse >= 0.18 and len(signals) >= 2) or (reuse >= 0.10 and len(signals) >= 3):
        risk = "高"
    elif reuse >= 0.12 or len(signals) >= 2:
        risk = "中"
    else:
        risk = "低"

    samples = []
    for i, j, sim in sorted(hits, key=lambda item: -item[2])[:MAX_SAMPLES]:
        samples.append({"a": left[i][:180], "b": right[j][:180], "sim": round(sim, 3)})

    return {
        "a": a["name"],
        "b": b["name"],
        "score": score,
        "risk": risk,
        "overall": round(overall, 3),
        "reuse": round(reuse, 3),
        "dup_sentences": len(hits),
        "base_sentences": len(left),
        "whitelisted": whitelisted,
        "signals": signals,
        "samples": samples,
    }


def analyze(items: list[tuple[str, Path]], tender_text: str = "") -> dict:
    """items: [(显示名, 文件路径)]"""
    docs: list[dict] = []
    for name, path in items:
        try:
            data = read_document(path)
        except DupError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个文件损坏不应中断整批
            raise DupError(f"{name} 解析失败：{exc.__class__.__name__}") from exc

        text = data["text"]
        if len(normalize(text)) < 80:
            raise DupError(f"{name} 可提取文字太少，可能是扫描件，需先 OCR 再比对")

        sentences = [s for s in (normalize(x) for x in split_sentences(text)) if len(s) >= SENT_MIN_LEN]
        if not sentences:
            raise DupError(f"{name} 没有可参与比对的长句，请检查文件内容")
        if len(sentences) > 6000:  # 控制计算量，保留前 6000 句（已覆盖正文主体）
            sentences = sentences[:6000]

        docs.append(
            {
                "name": name,
                "format": data["format"],
                "chars": len(text),
                "sentences": sentences,
                "text": text,
                "meta": data["meta"],
                "rsids": data["rsids"],
                "images": data["images"],
            }
        )

    tender = _whitelist_set(tender_text)
    pairs: list[dict] = []
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            pairs.append(_describe_pair(docs[i], docs[j], tender))
    pairs.sort(key=lambda p: -p["score"])

    files = [
        {
            "name": d["name"],
            "format": d["format"],
            "chars": d["chars"],
            "sentences": len(d["sentences"]),
            "author": d["meta"].get("author") or "",
            "last_modified_by": d["meta"].get("last_modified_by") or "",
            "company": d["meta"].get("company") or "",
            "rsids": len(d["rsids"]),
            "images": len(d["images"]),
        }
        for d in docs
    ]

    notice = "结论仅作为评标线索，是否认定雷同须由评标委员会结合原件判断。"
    if not tender[0]:
        notice = "未提供招标文件原文，未做“照抄招标原文”白名单剔除，重复率可能偏高。" + notice

    return {"files": files, "pairs": pairs, "notice": notice}
