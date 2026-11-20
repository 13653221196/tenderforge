"""暗标 AI 复核（对标易标 blindCheckAgent，clean-room 重写）。

两层设计：
1. 预设提取：从招标文件里 AI 抽取"暗标"相关要求（版式、份数、密封、页数上限、
   不得出现的内容等），存项目 meta.blind_preset，供检查与复核共同使用；
2. AI 复核：把招标要求 + 规则初筛结果 + 文档格式摘要一起交给模型，
   让它对照招标文件逐条判定——补规则层的漏（规则只认识固定模式，模型能理解
   "封面不得出现投标人标识"这类自然语言要求）。

模型输出仅作补充发现，标注 source=ai，不覆盖规则结论。
"""
from __future__ import annotations

import json

from ..config import LLMConfig
from .llm import LLMError, chat, parse_json

PRESET_SYSTEM = (
    "你是招标文件审查专员。只输出 JSON。只提取与「暗标/密封/身份信息」有关的硬性要求，"
    "与暗标无关的技术、商务要求不要提取。"
)

PRESET_TEMPLATE = """请从下面的招标文件中提取与"暗标评审"相关的全部要求。

暗标评审常见要求：排版格式（纸张/边距/字体/字号/行距）、正文中不得出现的内容
（投标人名称/人员姓名/Logo/地址/联系方式等能识别身份的信息）、份数与密封要求、
页数上限、是否允许图表彩色等。

输出 JSON：
{{
  "requirements": [{{"item": "要求名称（10字内）", "detail": "具体要求原文或转述"}}],
  "forbidden": ["不得出现的内容1", "内容2"],
  "notes": "其他值得注意的要求（100字内，没有给空字符串）"
}}

招标文件（节选）：
{excerpt}
"""

REVIEW_SYSTEM = (
    "你是暗标评审的合规审查员。只依据给定的招标要求与检查结果判断，不得想象文件里没有的内容。"
    "只输出 JSON。规则引擎已经查过固定模式（证件号/企业名/彩字/字体过杂等），"
    "你重点补它查不了的语义类问题。"
)

REVIEW_TEMPLATE = """请对照招标文件的暗标要求，复核这份投标文件（由程序规则引擎初筛过）的合规情况。

招标文件暗标要求：
{requirements}

规则引擎初筛结果（已确认的命中，不要重复报）：
{findings}

文档格式摘要（导出稿的实际版式）：
{format_summary}

输出 JSON：
{{
  "findings": [
    {{"title": "问题（15字内）", "detail": "对照哪条要求、文件里实际情况", "level": "高|中|低", "basis": "依据的要求内容"}}
  ],
  "confirmed": ["规则引擎查到且确实违反要求的问题标题"],
  "summary": "一句话结论"
}}

要求：
- findings 只报规则引擎没查到、但对照招标要求确实有风险的问题（语义类：如封面出现单位简称、
  页眉含项目地点可推测投标人、表格里出现人名等）；
- 招标要求里没提的，不要想当然报问题；
- level 用 高/中/低；没有新发现就给空数组，这很正常。
"""


async def extract_preset(cfg: LLMConfig, tender_text: str) -> dict:
    if not cfg.ready:
        return {"requirements": [], "forbidden": [], "notes": "", "source": "none",
                "notice": "未配置 AI，无法自动提取暗标要求"}
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": PRESET_SYSTEM},
             {"role": "user", "content": PRESET_TEMPLATE.format(excerpt=(tender_text or "")[:14000])}],
            json_mode=True,
            max_tokens=1500, task="extract",
        )
        data = parse_json(raw)
        return _clean_preset(data)
    except (LLMError, ValueError) as exc:
        return {"requirements": [], "forbidden": [], "notes": "", "source": "none",
                "notice": f"提取失败：{exc}"}


def _clean_preset(data: dict) -> dict:
    reqs = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    reqs = [
        {"item": str(r.get("item") or "").strip()[:30], "detail": str(r.get("detail") or "").strip()[:200]}
        for r in reqs if isinstance(r, dict) and str(r.get("item") or "").strip()
    ][:15]
    forbidden = data.get("forbidden") if isinstance(data.get("forbidden"), list) else []
    forbidden = [str(f).strip()[:40] for f in forbidden if str(f).strip()][:10]
    return {
        "requirements": reqs,
        "forbidden": forbidden,
        "notes": str(data.get("notes") or "")[:200],
        "source": "ai",
    }


async def review(
    cfg: LLMConfig,
    *,
    preset: dict,
    findings: list[dict],
    format_summary: dict,
    tender_text: str = "",
) -> dict:
    if not cfg.ready:
        return {"findings": [], "confirmed": [], "summary": "", "source": "none",
                "notice": "未配置 AI，跳过复核"}
    req_lines = []
    for r in preset.get("requirements") or []:
        req_lines.append(f"- {r.get('item')}：{r.get('detail')}")
    for f in preset.get("forbidden") or []:
        req_lines.append(f"- 正文不得出现：{f}")
    if preset.get("notes"):
        req_lines.append(f"- 其他：{preset['notes']}")
    if not req_lines:
        # 没有预设时把招标原文里"不得出现/密封/份数"相关句子捞出来兜底
        import re

        for line in (tender_text or "").splitlines():
            if re.search(r"不得出现|不能出现|密封|暗标|正文中不得|投标人名称", line or ""):
                req_lines.append("- " + line.strip()[:120])
            if len(req_lines) >= 12:
                break
    if not req_lines:
        return {"findings": [], "confirmed": [], "summary": "", "source": "ai",
                "notice": "招标文件里没有提取到暗标要求，无需复核"}

    hit_lines = []
    for hit in (findings.get("hits") or [])[:25]:
        hit_lines.append(f"- [{hit.get('kind')}] {hit.get('word')} @ {hit.get('zone')}")
    for item in (findings.get("format_issues") or [])[:15]:
        hit_lines.append(f"- [{item.get('level')}] {item.get('title')}：{item.get('detail', '')[:80]}")

    prompt = REVIEW_TEMPLATE.format(
        requirements="\n".join(req_lines) or "（无）",
        findings="\n".join(hit_lines) or "（规则初筛无命中）",
        format_summary=json.dumps(format_summary, ensure_ascii=False)[:2500],
    )
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=1800, task="extract",
        )
        data = parse_json(raw)
        out_findings = []
        rows = data.get("findings") if isinstance(data.get("findings"), list) else []
        for row in rows[:12]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            level = str(row.get("level") or "中")
            out_findings.append({
                "title": title[:40],
                "detail": str(row.get("detail") or "")[:250],
                "level": level if level in ("高", "中", "低") else "中",
                "basis": str(row.get("basis") or "")[:120],
                "source": "ai",
            })
        return {
            "findings": out_findings,
            "confirmed": [str(c)[:40] for c in (data.get("confirmed") or []) if str(c).strip()][:10],
            "summary": str(data.get("summary") or "")[:200],
            "source": "ai",
        }
    except (LLMError, ValueError) as exc:
        return {"findings": [], "confirmed": [], "summary": "", "source": "ai",
                "notice": f"AI 复核失败：{exc}"}
