"""整套投标包打包：按册切分章节 → 逐册导出 Word → 附偏离表/质疑函/资格清单 → 打成一个 zip。

分册默认走启发式（按一级章节标题关键词归到 商务标/技术标/资格文件/其他），
调用方也可传入分册方案覆盖（manual）。副本模式会额外生成一套「（副本）」。
"""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from .. import db
from ..config import DATA_DIR
from . import deviation, htmlexport, query_letter, quote
from .docx_export import export_docx

# 分册规则：按顺序命中即归类，关键词命中一级章节标题即生效。
VOLUME_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("资格文件", ("资格", "资信", "资质", "营业执照", "财务", "业绩", "信誉")),
    ("商务标", ("商务", "投标函", "法定代表人", "授权", "承诺", "声明", "报价")),
    (
        "技术标",
        ("技术", "方案", "实施", "施工", "服务", "设计", "组织", "进度", "质量", "安全", "工艺", "措施"),
    ),
]
DEFAULT_VOLUME = "其他"


DEFAULT_PATTERN = "{volume}-{doc_type}"

# 招标文件常见命名要求：「项目编号-投标人名称-分册」，用模板表达
NAME_FIELDS = ("project", "code", "bidder", "volume", "doc_type", "date")


def project_code(project: dict) -> str:
    """从解读出的基本信息里取招标编号，取不到返回空串。"""
    meta = project.get("meta") or {}
    for key in ("project_code", "tender_no", "code"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    for row in meta.get("basic") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        if "编号" in label or "招标号" in label:
            value = str(row.get("value") or "").strip()
            if value:
                return value
    return ""


def render_name(pattern: str, values: dict[str, str]) -> str:
    """按模板生成文件名主体；空占位符连同它旁边的分隔符一起去掉。"""
    pattern = (pattern or DEFAULT_PATTERN).strip() or DEFAULT_PATTERN
    filled = re.sub(
        r"\{(\w+)\}",
        lambda m: values.get(m.group(1), ""),
        pattern,
    )
    # 空值会留下 "— -" 这种残渣，压成分隔符并去掉首尾
    filled = re.sub(r"\s*([-—–_])\s*(?:\1\s*)+", r"\1", filled)
    filled = re.sub(r"^[\s\-—–_]+|[\s\-—–_]+$", "", filled)
    return filled.strip()


def safe_name(name: str, limit: int = 40) -> str:
    """去掉 Windows 文件名非法字符。"""
    return "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|').strip()[:limit]


def classify(title: str) -> str:
    text = (title or "").strip()
    for name, words in VOLUME_RULES:
        if any(w in text for w in words):
            return name
    return DEFAULT_VOLUME


def _chunks(sections: list[dict]) -> list[dict]:
    """把有序章节按一级节点切成块，下级章节归到所属一级节点。"""
    chunks: list[dict] = []
    for item in sections:
        level = int(item.get("level", 1))
        if level <= 1 or not chunks:
            chunks.append({"title": item.get("title", ""), "items": [item]})
        else:
            chunks[-1]["items"].append(item)
    return chunks


def plan_volumes(sections: list[dict], manual: list[dict] | None = None) -> list[dict]:
    """返回 [{"name": 卷名, "section_ids": [...]}]。

    manual 提供时以它为准（前端手动调整后的分册方案），未被覆盖的章节按启发式
    补成「其他」册，避免漏章节。
    """
    by_id = {s["id"]: s for s in sections}
    if manual:
        used: set[str] = set()
        volumes: list[dict] = []
        for grp in manual:
            ids = [sid for sid in (grp.get("section_ids") or []) if sid in by_id]
            if not ids:
                continue
            used.update(ids)
            volumes.append({"name": str(grp.get("name") or DEFAULT_VOLUME), "section_ids": ids})
        rest = [s["id"] for s in sections if s["id"] not in used]
        if rest:
            volumes.append({"name": DEFAULT_VOLUME, "section_ids": rest})
        return volumes

    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for chunk in _chunks(sections):
        name = classify(chunk["title"])
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].extend(s["id"] for s in chunk["items"])
    volumes = [{"name": n, "section_ids": buckets[n]} for n in order]
    if len(volumes) == 1 and volumes[0]["name"] == DEFAULT_VOLUME:
        volumes[0]["name"] = "投标文件"
    return volumes


def _unique(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    suffix = f".{ext}" if dot else ""
    i = 2
    while f"{base}({i}){suffix}" in used:
        i += 1
    final = f"{base}({i}){suffix}"
    used.add(final)
    return final


def _credentials_docx(out_path: Path, title: str, rows: list[dict]) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=1)
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for idx, head in enumerate(("序号", "证书名称", "持有人", "证书编号", "有效期至")):
        table.cell(0, idx).text = head
    for i, row in enumerate(rows, start=1):
        cells = table.add_row().cells
        cells[0].text = str(i)
        cells[1].text = str(row.get("name") or "")
        cells[2].text = str(row.get("holder") or "")
        cells[3].text = str(row.get("cert_no") or "")
        cells[4].text = str(row.get("expires_at") or "长期")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


def build_bundle(
    *,
    work_dir: Path,
    out_zip: Path,
    project: dict,
    sections: list[dict],
    fmt: dict,
    doc_type: str = "投标文件",
    bidder: str = "",
    date_text: str = "",
    with_cover: bool = True,
    with_toc: bool = True,
    manual_groups: list[dict] | None = None,
    duplicate_copy: bool = False,
    include_deviations: bool = True,
    include_query_letter: bool = True,
    include_credentials: bool = True,
    enterprise_id: str = "",
    quote_rows: list[dict] | None = None,
    quote_tax: float | None = None,
    pdf: bool = False,
    name_pattern: str = DEFAULT_PATTERN,
    seal_note: bool = True,
) -> dict:
    """生成各分册 docx（可选 PDF）与附表，打包成 zip，返回清单信息。

    pdf=True 时每册再出一份 PDF；单册 PDF 失败只记 warning，不拖垮整个包
    （服务器上没装 Chrome 也能拿到 Word 分册）。
    seal_note=True 时每册封面后附一页密封条/骑缝章说明；多册时页眉标注
    「第 X 册 / 共 Y 册」，页码各册独立从 1 起。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    by_id = {s["id"]: s for s in sections}
    volumes = plan_volumes(sections, manual_groups)
    used: set[str] = set()
    files: list[str] = []
    volumes_out: list[dict] = []
    warnings: list[str] = []

    base_values = {
        "project": safe_name(project.get("name") or ""),
        "code": safe_name(project_code(project)),
        "bidder": safe_name(bidder or ""),
        "doc_type": safe_name(doc_type or "投标文件"),
        "date": safe_name(date_text or ""),
    }

    def _stem(vol_name: str, copy_tag: str) -> str:
        values = {**base_values, "volume": safe_name(f"{vol_name}{copy_tag}") or "分册"}
        return safe_name(render_name(name_pattern, values)) or safe_name(vol_name) or "分册"

    def _render(vol_name: str, ids: list[str], copy_tag: str, vol_no: int = 0, vol_total: int = 0) -> None:
        subset = [by_id[sid] for sid in ids if sid in by_id]
        if not subset:
            return
        label = f"{vol_name}{copy_tag}"
        stem = _stem(vol_name, copy_tag)
        fname = _unique(f"{stem}.docx", used)
        vol_note = f"第 {vol_no} 册 / 共 {vol_total} 册" if vol_total > 1 else ""
        export_docx(
            out_path=work_dir / fname,
            project_name=project["name"],
            sections=subset,
            doc_type=label,
            bidder=bidder,
            date_text=date_text,
            with_cover=with_cover,
            with_toc=with_toc,
            fmt=fmt,
            seal_note=seal_note,
            volume_note=vol_note,
        )
        files.append(fname)
        record = {"name": label, "file": fname, "count": len(subset), "copy": bool(copy_tag), "pdf": ""}
        volumes_out.append(record)
        if pdf:
            pdf_name = _unique(f"{stem}.pdf", used)
            try:
                htmlexport.export_pdf(
                    out_path=work_dir / pdf_name,
                    project_name=project["name"],
                    sections=subset,
                    doc_type=label,
                    bidder=bidder,
                    date_text=date_text,
                    with_cover=with_cover,
                    with_toc=with_toc,
                    fmt=fmt,
                )
                files.append(pdf_name)
                record["pdf"] = pdf_name
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{label} 的 PDF 未生成（{exc}），仅提供 Word 版")

    vol_total = len(volumes)
    for no, vol in enumerate(volumes, start=1):
        _render(vol["name"], vol["section_ids"], "", vol_no=no, vol_total=vol_total)
    if duplicate_copy:
        for no, vol in enumerate(volumes, start=1):
            _render(vol["name"], vol["section_ids"], "（副本）", vol_no=no, vol_total=vol_total)

    extras: list[str] = []
    if include_deviations:
        for kind in db.list_deviation_kinds(project["id"]):
            result = db.get_deviation(project["id"], kind)
            if not result or not result.get("rows"):
                continue
            fname = _unique(f"偏离表-{safe_name(kind) or '响应'}.docx", used)
            try:
                deviation.to_docx(work_dir / fname, project["name"], result)
                files.append(fname)
                extras.append(fname)
            except Exception:  # noqa: BLE001
                pass

    if include_query_letter:
        letter = db.get_query_letter(project["id"])
        if letter and letter.get("items"):
            fname = _unique("质疑澄清函.docx", used)
            try:
                query_letter.to_docx(work_dir / fname, letter)
                files.append(fname)
                extras.append(fname)
            except Exception:  # noqa: BLE001
                pass

    if include_credentials and enterprise_id:
        creds = db.list_credentials(enterprise_id)
        if creds:
            fname = _unique("资格文件清单.docx", used)
            try:
                _credentials_docx(work_dir / fname, "资格文件清单", creds)
                files.append(fname)
                extras.append(fname)
            except Exception:  # noqa: BLE001
                pass
            # 证件原件：资信库里传过扫描件的，一并放进包里，评委要看原件
            src_dir = DATA_DIR / "credentials" / enterprise_id
            for row in creds:
                fn = str(row.get("filename") or "").strip()
                src = src_dir / fn if fn else None
                if not src or not src.is_file():
                    continue
                dst_dir = work_dir / "资格文件附件"
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst_dir / _unique(src.name, used))

    if quote_rows:
        try:
            result = quote.compute(quote_rows, project_name=project["name"], tax_rate=quote_tax)
            if result.get("rows"):
                fname = _unique("投标报价表.xlsx", used)
                quote.to_xlsx(work_dir / fname, project["name"], result)
                files.append(fname)
                extras.append(fname)
                if pdf:
                    pdf_name = _unique("投标报价表.pdf", used)
                    try:
                        quote.to_pdf(work_dir / pdf_name, project["name"], result)
                        files.append(pdf_name)
                        extras.append(pdf_name)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"投标报价表的 PDF 未生成（{exc}），仅提供 Excel 版")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"投标报价表未生成（{exc}）")

    _write_manifest(work_dir / "投标文件目录.txt", project, volumes_out, extras, doc_type)

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root in sorted(work_dir.iterdir()):
            if root.is_file():
                zf.write(root, root.name)
            elif root.is_dir():
                for child in sorted(root.rglob("*")):
                    if child.is_file():
                        zf.write(child, f"{root.name}/{child.relative_to(root).as_posix()}")

    return {
        "ok": True,
        "file": out_zip.name,
        "size": out_zip.stat().st_size,
        "volumes": volumes_out,
        "files": files + ["投标文件目录.txt"],
        "warnings": warnings,
    }


def _write_manifest(path: Path, project: dict, volumes: list[dict], extras: list[str], doc_type: str) -> None:
    lines = [f"{doc_type}分册清单", "", f"项目名称：{project.get('name') or ''}", ""]
    main = [v for v in volumes if not v.get("copy")]
    copies = [v for v in volumes if v.get("copy")]
    if main:
        lines.append("一、正本分册")
        for vol in main:
            extra = f" / {vol['pdf']}" if vol.get("pdf") else ""
            lines.append(f"  - {vol['name']}（{vol['count']} 节）→ {vol['file']}{extra}")
    if copies:
        lines.append("")
        lines.append("二、副本分册")
        for vol in copies:
            extra = f" / {vol['pdf']}" if vol.get("pdf") else ""
            lines.append(f"  - {vol['name']}（{vol['count']} 节）→ {vol['file']}{extra}")
    if extras:
        lines.append("")
        lines.append(("三、附表" if copies else "二、附表"))
        for name in extras:
            lines.append(f"  - {name}")
    lines.append("")
    lines.append("说明：目录页码已在文档里设了自动更新域，Word 打开时如提示「是否更新域」请点「是」。")
    lines.append("      各册页码独立从第 1 页起计，页眉标注册号；每册附密封条说明页（封口骑缝章要求见该页）。")
    path.write_text("\n".join(lines), encoding="utf-8-sig")
