"""PDF 导出与旧版 .doc 转换（对标 BidMaster format/pdf_export + doc_export）。

- PDF：把正文渲染成带 @page 版式的 HTML，用本机 Chrome 无头打印（复用 diagrams
  的 Playwright 通道），页边距/字体/字号全部来自格式预设；
- .doc：生成带 Word 命名空间的 HTML 存为 .doc——Word/WPS 原生可开，用于兼容
  要求"doc 格式"的旧招标场景。

不装 LibreOffice / 不依赖 Word 进程，零新依赖。
"""
from __future__ import annotations

import html as _html
import re
from pathlib import Path

from .docx_export import _parse_blocks
from .formats import PAPER_CM, resolve

BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _inline(text: str) -> str:
    """段落文本：转义 + 加粗。"""
    escaped = _html.escape(text or "")
    if not BOLD.search(escaped):
        return escaped
    escaped = BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
    return escaped


def _render_blocks(content: str) -> str:
    parts: list[str] = []
    for kind, payload in _parse_blocks(content):
        if kind == "h2":
            parts.append(f"<h2>{_inline(str(payload))}</h2>")
        elif kind == "h3":
            parts.append(f"<h3>{_inline(str(payload))}</h3>")
        elif kind == "li":
            parts.append(f"<p class=li>• {_inline(str(payload))}</p>")
        elif kind == "quote":
            parts.append(f"<p class=quote>{_inline(str(payload))}</p>")
        elif kind == "table":
            rows: list[list[str]] = payload  # type: ignore[assignment]
            cells = ""
            for r, row in enumerate(rows):
                tag = "th" if r == 0 else "td"
                cells += "<tr>" + "".join(
                    f"<{tag}>{_inline(c)}</{tag}>" for c in row
                ) + "</tr>"
            parts.append(f"<table>{cells}</table>")
        else:
            parts.append(f"<p>{_inline(str(payload))}</p>")
    return "\n".join(parts)


def build_html(
    *,
    project_name: str,
    sections: list[dict],
    doc_type: str = "投标文件",
    bidder: str = "",
    date_text: str = "",
    with_cover: bool = True,
    with_toc: bool = True,
    fmt: dict | None = None,
    word_ns: bool = False,
) -> str:
    fmt = fmt or resolve()
    body = fmt.get("body") or {}
    east = str(body.get("east") or "宋体")
    ascii_font = str(body.get("ascii") or "Times New Roman")
    size = float(body.get("size") or 12)
    line = float(body.get("line_spacing") or 1.5)
    margins = fmt.get("margins") or {}
    width, height = PAPER_CM.get(str(fmt.get("paper") or "a4"), PAPER_CM["a4"])
    h1_spec = (fmt.get("headings") or {}).get("1") or {}

    ns_attr = ""
    ns_decl = ""
    if word_ns:
        ns_attr = ' xmlns:w="urn:schemas-microsoft-com:office:word" xmlns:o="urn:schemas-microsoft-com:office:office"'
        ns_decl = (
            "<!--[if gte mso 9]><xml><w:WordDocument>"
            "<w:View>Print</w:View><w:Zoom>100</w:Zoom></w:WordDocument></xml><![endif]-->"
        )

    cover = ""
    if with_cover:
        cover = f"""
<div class="cover">
  <div class="cover-title">{_html.escape(project_name or "投标文件")}</div>
  <div class="cover-sub">{_html.escape(doc_type or "投标文件")}</div>
  <div class="cover-meta">投 标 人：{_html.escape(bidder or "〔待填〕")}<br>日　　期：{_html.escape(date_text or "〔待填〕")}</div>
</div>"""

    toc = ""
    if with_toc:
        items = []
        for sec in sections:
            level = min(max(int(sec.get("level") or 1), 1), 3)
            indent = (level - 1) * 24
            items.append(
                f'<div class="toc-item" style="margin-left:{indent}pt">{_html.escape(str(sec.get("title") or ""))}</div>'
            )
        toc = '<div class="toc"><div class="toc-title">目　　录</div>' + "".join(items) + "</div>"

    body_html = []
    for sec in sections:
        title = str(sec.get("title") or "").strip()
        level = min(max(int(sec.get("level") or 1), 1), 3)
        body_html.append(f"<h{level}>{_html.escape(title)}</h{level}>")
        content = sec.get("content") or ""
        if content.strip():
            body_html.append(_render_blocks(content))
        else:
            body_html.append('<p class="placeholder">〔本节尚未编制〕</p>')

    css = f"""
@page {{ size: {width}cm {height}cm; margin: {margins.get('top', 2.54)}cm {margins.get('right', 2.6)}cm
         {margins.get('bottom', 2.54)}cm {margins.get('left', 3.0)}cm; }}
body {{ font-family: "{east}", "{ascii_font}", serif; font-size: {size}pt; line-height: {line};
        margin: 0; color: #17191c; }}
p {{ margin: 0 0 0.35em 0; text-indent: {size * 2}pt; text-align: justify; }}
h1 {{ font-size: {float(h1_spec.get('size') or 16)}pt; font-weight: bold;
      break-before: page; margin: 0.6em 0 0.8em; }}
h2 {{ font-size: {float(((fmt.get('headings') or {}).get('2') or {}).get('size') or 14)}pt;
      margin: 0.8em 0 0.4em; }}
h3 {{ font-size: {float(((fmt.get('headings') or {}).get('3') or {}).get('size') or 12)}pt;
      margin: 0.7em 0 0.3em; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.6em 0; font-size: {max(9, size - 1.5)}pt; }}
th, td {{ border: 1px solid #444; padding: 3pt 5pt; text-align: left; }}
th {{ background: #f0f0f0; font-weight: bold; }}
p.li, p.quote {{ text-indent: 0; }}
p.quote {{ margin-left: 24pt; color: #444; font-size: {size - 1.5}pt; }}
.cover {{ text-align: center; padding-top: 220pt; break-after: page; }}
.cover-title {{ font-size: {float((fmt.get('cover') or {}).get('title_size') or 26)}pt; font-weight: bold; }}
.cover-sub {{ font-size: {float((fmt.get('cover') or {}).get('sub_size') or 20)}pt; margin-top: 28pt; }}
.cover-meta {{ font-size: {float((fmt.get('cover') or {}).get('meta_size') or 14)}pt; margin-top: 90pt; line-height: 2; }}
.toc {{ break-after: page; }}
.toc-title {{ text-align: center; font-size: {float((fmt.get('toc') or {}).get('title_size') or 18)}pt;
              font-weight: bold; margin-bottom: 18pt; }}
.toc-item {{ margin: 4pt 0; }}
.placeholder {{ color: #999; }}
"""

    return (
        f'<!doctype html><html{ns_attr}><head><meta charset="utf-8">{ns_decl}<style>{css}</style></head>'
        f'<body>{cover}{toc}' + "\n".join(body_html) + "</body></html>"
    )


def export_pdf(
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
) -> Path:
    """Chrome 无头打印 PDF。Chrome/Playwright 不可用时抛 RuntimeError。"""
    from .diagrams import _chrome_path

    chrome = _chrome_path()
    if not chrome:
        raise RuntimeError("未找到本机 Chrome/Edge，无法打印 PDF")
    html = build_html(
        project_name=project_name, sections=sections, doc_type=doc_type, bidder=bidder,
        date_text=date_text, with_cover=with_cover, with_toc=with_toc, fmt=fmt,
    )
    tmp_html = out_path.with_suffix(".tmp.html")
    tmp_html.write_text(html, encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chrome, args=["--no-sandbox", "--disable-gpu"],
            )
            try:
                page = browser.new_page()
                page.goto(tmp_html.as_uri(), wait_until="load")
                fmt_ = fmt or resolve()
                margins = fmt_.get("margins") or {}
                page.pdf(
                    path=str(out_path),
                    prefer_css_page_size=True,
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template=(
                        '<div style="width:100%;text-align:center;font-size:9pt;color:#444;">'
                        '第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</div>'
                    ),
                    margin={
                        "top": f"{margins.get('top', 2.54)}cm",
                        "bottom": f"{margins.get('bottom', 2.54)}cm",
                        "left": f"{margins.get('left', 3.0)}cm",
                        "right": f"{margins.get('right', 2.6)}cm",
                    },
                )
            finally:
                browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)
    return out_path


def export_doc(
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
) -> Path:
    """Word 命名空间 HTML 存成 .doc——Word/WPS 直接打开，兼容旧格式要求。"""
    html = build_html(
        project_name=project_name, sections=sections, doc_type=doc_type, bidder=bidder,
        date_text=date_text, with_cover=with_cover, with_toc=with_toc, fmt=fmt, word_ns=True,
    )
    header = (
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/related; boundary=\"=_bs\"\r\n\r\n"
        "--=_bs\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + html + "\r\n--=_bs--\r\n", encoding="utf-8")
    return out_path
