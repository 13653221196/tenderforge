"""质疑/澄清函生成：把招标体检发现的问题整理成可提交的函件。

数据来源：tender_audit.run 的 issues（T01–T14）。按问题性质分两类：
- 质疑类：可依据法条向招标人提出质疑的硬伤（品牌指向、资质门槛、保证金超限等）；
- 澄清类：文件表述不清、口径不一致，需要招标人书面澄清的事项。

只做本地规则归类 + 固定模板措辞；可选 AI 润色（未配置或调用失败时自动退回模板）。
结果存项目 meta.query_letter。
"""
from __future__ import annotations

import html as _html
import re
from pathlib import Path

from ..config import LLMConfig
from .llm import LLMError, chat

# 每条体检规则 → 归类与参考依据。依据只写广为人知、可核对的条款，避免误引。
RULES: dict[str, dict[str, str]] = {
    "T01": {"category": "质疑", "basis": "《招标投标法实施条例》第三十二条",
            "demand": "请删除或修改指向性表述，明确以招标文件技术要求为准。"},
    "T02": {"category": "质疑", "basis": "《招标投标法》第十八条",
            "demand": "请取消地域、注册资本等不合理门槛，依法平等对待潜在投标人。"},
    "T03": {"category": "澄清", "basis": "《政府采购货物和服务招标投标管理办法》第五十五条",
            "demand": "请书面明确各主观评审因素的量化分档与对应分值。"},
    "T04": {"category": "澄清", "basis": "《招标投标法》第二十四条、《政府采购法》第三十五条",
            "demand": "请依法顺延投标截止时间，保证公告期不少于二十日。"},
    "T05": {"category": "质疑", "basis": "《招标投标法实施条例》第二十六条",
            "demand": "请将投标保证金调至法定上限（不超过招标项目估算价 2%）以内。"},
    "T06": {"category": "澄清", "basis": "《政府采购法实施条例》第三十四条",
            "demand": "请书面明确价格分的计算公式与基准价确定方式。"},
    "T07": {"category": "质疑", "basis": "应以现行有效的法律法规为准",
            "demand": "请更正引用的法规文件，以现行有效文件为准。"},
    "T08": {"category": "澄清", "basis": "",
            "demand": "请确认招标文件是否漏页，并补充缺失的必备章节。"},
    "T09": {"category": "澄清", "basis": "",
            "demand": "请以答疑纪要或更正公告统一口径，明确前后矛盾条款以哪一处为准。"},
    "T10": {"category": "澄清", "basis": "",
            "demand": "请明确合同付款与违约条款，降低投标人的履约风险。"},
    "T11": {"category": "澄清", "basis": "",
            "demand": "请补充招标文件中「待定/待补充」的具体内容。"},
    "T12": {"category": "澄清", "basis": "",
            "demand": "请核对并补充疑似断档的条款编号。"},
    "T13": {"category": "澄清", "basis": "《招标投标法实施条例》",
            "demand": "请说明投标有效期偏短的安排，或适当延长投标有效期。"},
    "T14": {"category": "质疑", "basis": "评标采用百分制",
            "demand": "请更正评分分值合计，确保各评审因素分值之和为 100 分。"},
}

SEV_ORDER = {"高": 0, "中": 1, "低": 2}
_CN_NUM = ["一", "二", "三", "四", "五"]
_HEAD_RE = re.compile(r"^[一二三四五六七八九十]、")
_ITEM_RE = re.compile(r"^\d+[.、]")
_SIGN_RE = re.compile(r"^(投标人|日\s*期|日期)\s*[：:]")


def _basic_map(meta: dict) -> dict[str, str]:
    return {str(r.get("label") or ""): str(r.get("value") or "") for r in (meta.get("basic") or [])}


def collect_issues(meta: dict, issues: list[dict] | None = None) -> list[dict]:
    if issues is not None:
        return list(issues)
    return list(((meta.get("tender_audit") or {}).get("issues")) or [])


def build_items(issues: list[dict]) -> list[dict]:
    items: list[dict] = []
    for issue in issues:
        code = str(issue.get("code") or "")
        rule = RULES.get(code, {})
        items.append({
            "code": code,
            "category": rule.get("category", "澄清"),
            "severity": issue.get("severity") or "低",
            "title": issue.get("title") or "",
            "fact": issue.get("detail") or "",
            "evidence": issue.get("evidence") or "",
            "basis": rule.get("basis", ""),
            "demand": rule.get("demand", "请招标人书面澄清或更正。"),
        })
    items.sort(key=lambda i: (SEV_ORDER.get(i["severity"], 3), 0 if i["category"] == "质疑" else 1, i["code"]))
    return items


def _title(items: list[dict]) -> str:
    has_challenge = any(i["category"] == "质疑" for i in items)
    has_clarify = any(i["category"] == "澄清" for i in items)
    if has_challenge and has_clarify:
        return "招标文件质疑（澄清）函"
    if has_challenge:
        return "招标文件质疑函"
    return "招标文件澄清申请"


def _paragraphs(project: str, owner: str, number: str, items: list[dict]) -> list[str]:
    paras: list[str] = [_title(items)]
    paras.append(f"致：{owner or '招标人'}")
    intro = (
        f"我单位作为潜在投标人，在研读贵单位发布的「{project or '本项目'}」招标文件"
        + (f"（项目编号：{number}）" if number else "")
        + "后，发现下列条款存在与现行法律法规不符或表述不够明确之处。为保障招标活动公平、公正，"
        "现依据相关法律法规提出如下质疑或澄清申请："
    )
    paras.append(intro)

    seq = 0
    for cat, label in (("质疑", "质疑事项"), ("澄清", "澄清申请")):
        cat_items = [i for i in items if i["category"] == cat]
        if not cat_items:
            continue
        paras.append(f"{_CN_NUM[seq]}、{label}")
        seq += 1
        for idx, it in enumerate(cat_items, 1):
            paras.append(f"{idx}. 【{it['code']}】{it['title']}")
            if it["fact"]:
                paras.append(f"事实与理由：{it['fact']}")
            if it["evidence"]:
                paras.append(f"原文：{it['evidence']}")
            if it["basis"]:
                paras.append(f"参考依据：{it['basis']}")
            paras.append(f"诉求：{it['demand']}")

    paras.append("恳请贵单位依法在规定期限内予以书面答复。")
    paras.append("投标人：______________________（盖章）")
    paras.append("日　期：______年____月____日")
    return paras


def build(meta: dict, issues: list[dict] | None = None) -> dict:
    items = build_items(collect_issues(meta, issues))
    basic = _basic_map(meta)
    project = basic.get("项目名称") or ""
    owner = basic.get("招标人") or "招标人"
    number = basic.get("项目编号") or ""

    challenge = [i for i in items if i["category"] == "质疑"]
    clarify = [i for i in items if i["category"] == "澄清"]
    stats = {
        "total": len(items),
        "challenge": len(challenge),
        "clarify": len(clarify),
        "high": sum(1 for i in items if i["severity"] == "高"),
    }
    paragraphs = _paragraphs(project, owner, number, items)
    return {
        "items": items,
        "stats": stats,
        "project": project,
        "owner": owner,
        "number": number,
        "title": _title(items),
        "paragraphs": paragraphs,
        "text": "\n".join(paragraphs),
        "source": "rule",
        "notice": "" if items else "尚未体检出可质疑/澄清的问题，请先在「招标合规体检」运行一次。",
    }


POLISH_SYSTEM = (
    "你是资深投标顾问。请把用户给出的质疑/澄清函草稿改写成正式、克制、有据可依的公文语言。"
    "要求：不得增删事实、依据和诉求条目，不得编造原文以外的内容；保持段落结构，每个自然段单独一行。"
    "直接输出改写后的函件全文，不要任何解释或前后缀。"
)


async def polish(cfg: LLMConfig, result: dict) -> dict:
    """可选 AI 润色：只改措辞，不改事实与诉求条目；失败自动退回模板。"""
    if not result.get("items"):
        return result
    if not cfg.ready:
        result["notice"] = "未配置 AI 接口，已使用固定模板措辞。"
        return result
    messages = [
        {"role": "system", "content": POLISH_SYSTEM},
        {"role": "user", "content": result["text"]},
    ]
    try:
        polished = (await chat(cfg, messages, max_tokens=2000, task="polish")).strip()
    except LLMError as exc:
        result["notice"] = f"AI 润色失败（{exc}），已使用固定模板措辞。"
        return result
    if not polished:
        return result
    out = dict(result)
    out["text"] = polished
    out["paragraphs"] = [ln for ln in polished.split("\n")]
    out["source"] = "ai"
    out["notice"] = "已用 AI 润色措辞，事实与诉求未改动，请人工复核后再提交。"
    return out


# ---------------- 导出：Word / PDF ----------------

def _classify(para: str, index: int) -> str:
    if index == 0:
        return "title"
    if para.startswith("致："):
        return "to"
    if _HEAD_RE.match(para):
        return "head"
    if _ITEM_RE.match(para):
        return "item"
    if _SIGN_RE.match(para):
        return "sign"
    return "body"


def to_docx(out_path: Path, result: dict) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    from .docx_export import _set_run_font

    doc = Document()
    for i, para in enumerate(result.get("paragraphs") or []):
        kind = _classify(para, i)
        p = doc.add_paragraph()
        if kind == "title":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(p.add_run(para), "黑体", 18, True)
            p.paragraph_format.space_after = Pt(14)
        elif kind == "to":
            _set_run_font(p.add_run(para), "宋体", 12)
        elif kind == "head":
            _set_run_font(p.add_run(para), "黑体", 12.5, True)
            p.paragraph_format.space_before = Pt(8)
        elif kind == "item":
            _set_run_font(p.add_run(para), "宋体", 12, True)
        elif kind == "sign":
            _set_run_font(p.add_run(para), "宋体", 12)
            p.paragraph_format.left_indent = Pt(200)
        else:
            p.paragraph_format.first_line_indent = Pt(24)
            _set_run_font(p.add_run(para), "宋体", 12)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


_HTML_WRAP = """<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; }}
body {{ font-family: "宋体", serif; font-size: 12pt; line-height: 1.7; color: #17191c; margin: 0; }}
h1 {{ font-family: "黑体", serif; font-size: 18pt; text-align: center; margin: 0 0 16pt; }}
h2 {{ font-family: "黑体", serif; font-size: 13pt; margin: 14pt 0 6pt; }}
p {{ margin: 0 0 6pt; text-align: justify; }}
p.item {{ font-weight: bold; }}
p.sign {{ margin-left: 220pt; }}
</style></head><body>
{body}
</body></html>"""


def _to_html(result: dict) -> str:
    parts = []
    for i, para in enumerate(result.get("paragraphs") or []):
        kind = _classify(para, i)
        text = _html.escape(para).replace("\n", "<br>")
        if kind == "title":
            parts.append(f"<h1>{text}</h1>")
        elif kind == "head":
            parts.append(f"<h2>{text}</h2>")
        elif kind in ("item", "sign"):
            parts.append(f"<p class='{kind}'>{text}</p>")
        else:
            parts.append(f"<p>{text}</p>")
    return _HTML_WRAP.format(body="\n".join(parts))


def to_pdf(out_path: Path, result: dict) -> Path:
    from .diagrams import _chrome_path

    chrome = _chrome_path()
    if not chrome:
        raise RuntimeError("未找到本机 Chrome/Edge，无法打印 PDF")
    tmp_html = out_path.with_suffix(".tmp.html")
    tmp_html.write_text(_to_html(result), encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome, args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.goto(tmp_html.as_uri(), wait_until="load")
                page.pdf(
                    path=str(out_path),
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template=(
                        '<div style="width:100%;text-align:center;font-size:9pt;color:#444;">'
                        '第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</div>'
                    ),
                    margin={"top": "2.5cm", "bottom": "2.5cm", "left": "2.8cm", "right": "2.6cm"},
                )
            finally:
                browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)
    return out_path
