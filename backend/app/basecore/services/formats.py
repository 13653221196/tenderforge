"""导出格式预设与格式合规检查。

预设只描述"版式长什么样"，不涉及内容：
- business 通用商务（默认）：宋体小四、1.5 倍行距、黑体标题；
- gov  党政机关公文（GB/T 9704-2012 常用取值）：仿宋_GB2312 三号、行距 28 磅、
       页边距 上 3.7 / 下 3.5 / 左 2.8 / 右 2.6、一级标题黑体三号；
- tender 招标文件常见要求：宋体小四、段前段后 0、页码居右。

检查函数只做确定性比对（字体、字号、行距、页边距、页码），报告差异不猜测意图。
"""
from __future__ import annotations

import copy
import math
import uuid
from copy import deepcopy
from datetime import datetime

PRESETS: dict[str, dict] = {
    "business": {
        "name": "通用商务",
        "desc": "宋体小四 / 1.5 倍行距 / 黑体标题，适合大多数投标文件",
        "paper": "a4",
        "orientation": "portrait",
        "margins": {"top": 2.54, "bottom": 2.54, "left": 3.0, "right": 2.6},
        "body": {
            "east": "宋体",
            "ascii": "Times New Roman",
            "size": 12,
            "line_spacing": 1.5,
            "indent_chars": 2,
            "space_before": 0,
            "space_after": 0,
            "align": "justify",
        },
        "headings": {
            "1": {"east": "黑体", "size": 16, "bold": False, "align": "center", "space_before": 12, "space_after": 6, "page_break": True},
            "2": {"east": "黑体", "size": 14, "bold": True, "align": "left", "space_before": 10, "space_after": 6, "page_break": False},
            "3": {"east": "黑体", "size": 12, "bold": True, "align": "left", "space_before": 8, "space_after": 6, "page_break": False},
        },
        "page_number": {"enabled": True, "format": "第 X 页", "size": 10.5, "east": "宋体", "align": "center"},
        "cover": {"title_size": 26, "sub_size": 20, "meta_size": 14},
        "toc": {"title_size": 18},
    },
    "gov": {
        "name": "党政机关公文（GB/T 9704）",
        "desc": "仿宋_GB2312 三号 / 行距 28 磅 / 页边距 3.7-3.5-2.8-2.6，适合正式送审稿",
        "paper": "a4",
        "orientation": "portrait",
        "margins": {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6},
        "body": {
            "east": "仿宋_GB2312",
            "ascii": "Times New Roman",
            "size": 16,
            "line_spacing": 1.75,
            "indent_chars": 2,
            "space_before": 0,
            "space_after": 0,
            "align": "justify",
        },
        "headings": {
            "1": {"east": "黑体", "size": 16, "bold": False, "align": "left", "space_before": 12, "space_after": 6, "page_break": True},
            "2": {"east": "楷体_GB2312", "size": 16, "bold": True, "align": "left", "space_before": 10, "space_after": 6, "page_break": False},
            "3": {"east": "仿宋_GB2312", "size": 16, "bold": True, "align": "left", "space_before": 8, "space_after": 6, "page_break": False},
        },
        "page_number": {"enabled": True, "format": "— X —", "size": 14, "east": "宋体", "align": "center"},
        "cover": {"title_size": 22, "sub_size": 18, "meta_size": 16},
        "toc": {"title_size": 18},
    },
    "tender": {
        "name": "招标文件常见要求",
        "desc": "宋体小四 / 单倍行距 / 段前段后 0 / 页码居右",
        "paper": "a4",
        "orientation": "portrait",
        "margins": {"top": 2.5, "bottom": 2.5, "left": 3.0, "right": 2.5},
        "body": {
            "east": "宋体",
            "ascii": "Times New Roman",
            "size": 12,
            "line_spacing": 1.25,
            "indent_chars": 2,
            "space_before": 0,
            "space_after": 0,
            "align": "justify",
        },
        "headings": {
            "1": {"east": "黑体", "size": 16, "bold": True, "align": "center", "space_before": 12, "space_after": 6, "page_break": True},
            "2": {"east": "黑体", "size": 14, "bold": True, "align": "left", "space_before": 8, "space_after": 4, "page_break": False},
            "3": {"east": "宋体", "size": 12, "bold": True, "align": "left", "space_before": 6, "space_after": 4, "page_break": False},
        },
        "page_number": {"enabled": True, "format": "第 X 页 共 Y 页", "size": 10.5, "east": "宋体", "align": "right"},
        "cover": {"title_size": 24, "sub_size": 18, "meta_size": 14},
        "toc": {"title_size": 18},
    },
}

DEFAULT_ID = "business"
PAPER_CM = {"a4": (21.0, 29.7), "a3": (29.7, 42.0), "b5": (17.6, 25.0), "letter": (21.59, 27.94)}


def preset_list() -> list[dict]:
    return [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in PRESETS.items()]


# ---------------- 我的模板（存 settings，跨项目复用） ----------------

def _load_templates() -> list[dict]:
    from .. import db
    return db.get_setting("fmt_templates", []) or []


def user_templates() -> list[dict]:
    return _load_templates()


def save_user_template(name: str, fmt: dict) -> dict:
    from .. import db
    name = (name or "").strip() or "我的模板"
    item = {
        "id": "u_" + uuid.uuid4().hex[:8],
        "name": name,
        "fmt": deepcopy(fmt),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    items = _load_templates()
    items.append(item)
    db.set_setting("fmt_templates", items)
    return item


def delete_user_template(tid: str) -> bool:
    from .. import db
    items = [t for t in _load_templates() if t.get("id") != tid]
    if len(items) == len(_load_templates()):
        return False
    db.set_setting("fmt_templates", items)
    return True


def find_template(tid: str) -> dict | None:
    for t in _load_templates():
        if t.get("id") == tid:
            return t
    return None


def resolve(preset: str | None = None, override: dict | None = None) -> dict:
    """预设/我的模板 + 局部覆盖 → 完整配置。override 支持任意层级浅合并。"""
    key = str(preset or DEFAULT_ID)
    found = find_template(key)
    if found:
        base = deepcopy(found["fmt"])
    else:
        base = deepcopy(PRESETS.get(key, PRESETS[DEFAULT_ID]))
    base["id"] = key
    if override:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged = dict(base[key])
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict) and isinstance(merged.get(sub_key), dict):
                        merged[sub_key] = {**merged[sub_key], **sub_value}
                    else:
                        merged[sub_key] = sub_value
                base[key] = merged
            else:
                base[key] = value
    return base


# ---------------- 格式检查 ----------------
SIZE_TOLERANCE = 0.6      # 磅
CM_TOLERANCE = 0.06       # 厘米
SPACING_TOLERANCE = 0.06


def _cm(value) -> float:
    try:
        return round(value.cm, 2) if value is not None else 0.0
    except Exception:
        return 0.0


def _east_font(run) -> str:
    try:
        rpr = run._element.rPr
        if rpr is None:
            return ""
        fonts = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
        if fonts is None:
            return ""
        return fonts.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia") or ""
    except Exception:
        return ""


def check_docx(path, fmt: dict, *, sample_limit: int = 6) -> dict:
    """把导出的 docx 与格式要求逐项比对，输出问题清单。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document(str(path))
    issues: list[dict] = []
    stats = {"sections": 0, "paragraphs": 0, "headings": 0, "checked": 0}

    paper_w, paper_h = PAPER_CM.get(str(fmt.get("paper", "a4")).lower(), PAPER_CM["a4"])
    if str(fmt.get("orientation", "portrait")) == "landscape":
        paper_w, paper_h = paper_h, paper_w
    margins = fmt.get("margins") or {}

    for idx, section in enumerate(doc.sections, 1):
        stats["sections"] += 1
        actual_w, actual_h = _cm(section.page_width), _cm(section.page_height)
        if abs(actual_w - min(paper_w, paper_h)) > 0.2 and abs(actual_w - paper_w) > 0.2:
            issues.append(
                {
                    "level": "中",
                    "type": "纸张",
                    "title": f"第 {idx} 节纸张不符合要求",
                    "detail": f"实际 {actual_w}×{actual_h} cm，要求 {paper_w}×{paper_h} cm",
                    "fix": "导出时选择对应纸张，或在 Word 的页面设置中改回 A4。",
                }
            )
        for key, label, attr in (
            ("top", "上", "top_margin"),
            ("bottom", "下", "bottom_margin"),
            ("left", "左", "left_margin"),
            ("right", "右", "right_margin"),
        ):
            want = margins.get(key)
            if want is None:
                continue
            got = _cm(getattr(section, attr))
            if abs(got - float(want)) > CM_TOLERANCE:
                issues.append(
                    {
                        "level": "低",
                        "type": "页边距",
                        "title": f"第 {idx} 节{label}边距为 {got} cm",
                        "detail": f"要求 {want} cm，差 {round(abs(got - float(want)), 2)} cm",
                        "fix": "在页面设置里调整页边距，评审常按页边距卡版式。",
                    }
                )

    body_fmt = fmt.get("body") or {}
    want_size = float(body_fmt.get("size") or 12)
    want_font = str(body_fmt.get("east") or "")
    want_spacing = float(body_fmt.get("line_spacing") or 1.5)
    bad_size, bad_font, bad_spacing = 0, 0, 0
    samples: list[str] = []

    # 正文从第一个标题开始：封面、目录页的标题字号本来就该更大，不参与正文比对
    body_started = False
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style_name = para.style.name if para.style is not None else ""
        if style_name.startswith("Heading"):
            stats["headings"] += 1
            body_started = True
            continue
        if not body_started or style_name in ("Title", "Subtitle", "TOC Heading"):
            continue
        stats["paragraphs"] += 1
        if not para.runs:
            continue
        run = para.runs[0]
        stats["checked"] += 1
        if run.font.size and abs(run.font.size.pt - want_size) > SIZE_TOLERANCE:
            bad_size += 1
            if len(samples) < sample_limit:
                samples.append(f"「{text[:18]}」{run.font.size.pt}pt")
        east = _east_font(run) or (run.font.name or "")
        if want_font and east and east != want_font and want_font not in east:
            bad_font += 1
        spacing = para.paragraph_format.line_spacing
        if isinstance(spacing, float) and abs(spacing - want_spacing) > SPACING_TOLERANCE:
            bad_spacing += 1

    total = max(1, stats["checked"])
    if bad_size:
        issues.append(
            {
                "level": "中" if bad_size / total > 0.1 else "低",
                "type": "字号",
                "title": f"{bad_size} 段正文不是 {want_size} 磅",
                "detail": f"例如：{'；'.join(samples)}",
                "fix": f"统一改回 {want_size} 磅（约「三号」16pt / 「小四」12pt）。",
            }
        )
    if bad_font:
        issues.append(
            {
                "level": "低",
                "type": "字体",
                "title": f"{bad_font} 段正文中文字体不是 {want_font}",
                "detail": "Word 里看是宋体，可能实际存的是等线或主题字体，导出到别的机器会变。",
                "fix": f"全选正文，把中文字体显式设置为 {want_font}。",
            }
        )
    if bad_spacing:
        issues.append(
            {
                "level": "低",
                "type": "行距",
                "title": f"{bad_spacing} 段行距不是 {want_spacing} 倍",
                "detail": "行距不统一会直接体现在页数估算上。",
                "fix": "全选正文 → 段落 → 行距设为固定值或多倍行距。",
            }
        )

    wants = (fmt.get("headings") or {})
    for level in ("1", "2", "3"):
        want = wants.get(level) or {}
        want_size_h = float(want.get("size") or 0)
        if not want_size_h:
            continue
        try:
            style = doc.styles[f"Heading {level}"]
        except KeyError:
            continue
        actual = style.font.size.pt if style.font.size else 0
        if actual and abs(actual - want_size_h) > SIZE_TOLERANCE:
            issues.append(
                {
                    "level": "低",
                    "type": "标题字号",
                    "title": f"{level} 级标题是 {actual} 磅",
                    "detail": f"要求 {want_size_h} 磅",
                    "fix": f"修改「标题 {level}」样式字号。",
                }
            )

    pn = fmt.get("page_number") or {}
    if pn.get("enabled"):
        has_page = False
        for section in doc.sections:
            for para in section.footer.paragraphs:
                xml = para._p.xml
                if "PAGE" in xml:
                    has_page = True
                    break
            if has_page:
                break
        if not has_page:
            issues.append(
                {
                    "level": "中",
                    "type": "页码",
                    "title": "页脚没有检测到页码域",
                    "detail": "页码缺失的标书常被要求补正。",
                    "fix": "插入 → 页码（域代码 PAGE），或重新用本系统导出。",
                }
            )

    grade = "通过" if not issues else ("待修" if any(i["level"] == "中" for i in issues) else "轻微")
    return {"ok": not issues, "grade": grade, "issues": issues, "stats": stats}
