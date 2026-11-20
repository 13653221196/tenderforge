"""表格类产物的统一导出。

偏离表、报价表都要「一张表 + 可选合计行」，早先各自抄了一份 xlsx 表头样式与
docx/pdf 调用；新加第四张表时容易各写各的。这里只提供能力，不关心业务字段：

- ``write_xlsx``：表头加粗灰底、列宽、自动换行、可选合计行（加粗）
- ``markdown``：把表格转成 markdown，供 docx / pdf 走同一条既有导出链
- ``to_docx`` / ``to_pdf``：单表文档，无封面无目录

业务模块负责把行数据摆好，样式与导出交给这里。
"""
from __future__ import annotations

from pathlib import Path

HEADER_FILL = "F0F0F0"


def cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def markdown(headers: list[str], rows: list[list[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        cells = [cell_text(c).replace("|", "／").replace("\n", " ") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_xlsx(
    out_path: Path,
    *,
    sheet_title: str,
    headers: list[str],
    rows: list[list[object]],
    widths: tuple[int, ...],
    total_row: list[object] | None = None,
) -> Path:
    """写单表 xlsx。``total_row`` 非空时追加一行并加粗（合计行）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "表")[:20]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append([c if isinstance(c, (int, float)) else cell_text(c) for c in row])
    if total_row:
        ws.append([c if isinstance(c, (int, float)) else cell_text(c) for c in total_row])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for idx, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + idx)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return out_path


def _content(headers: list[str], rows: list[list[object]], footer: list[str] | None) -> str:
    text = markdown(headers, rows)
    if footer:
        text += "\n\n" + "\n".join(footer)
    return text


def to_docx(
    out_path: Path,
    project_name: str,
    *,
    title: str,
    headers: list[str],
    rows: list[list[object]],
    doc_type: str,
    footer: list[str] | None = None,
) -> Path:
    from .docx_export import export_docx

    return export_docx(
        out_path=out_path,
        project_name=project_name,
        sections=[{"title": title, "level": 2, "content": _content(headers, rows, footer)}],
        doc_type=doc_type,
        with_cover=False,
        with_toc=False,
    )


def to_pdf(
    out_path: Path,
    project_name: str,
    *,
    title: str,
    headers: list[str],
    rows: list[list[object]],
    doc_type: str,
    footer: list[str] | None = None,
) -> Path:
    from .htmlexport import export_pdf

    return export_pdf(
        out_path=out_path,
        project_name=project_name,
        sections=[{"title": title, "level": 2, "content": _content(headers, rows, footer)}],
        doc_type=doc_type,
        with_cover=False,
        with_toc=False,
    )
