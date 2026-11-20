"""可行性研究报告生成：参数 → AI 大纲 → 逐章生成 → 汇总导出。

与投标文件完全解耦的第二种交付物。标准 12 章结构，用户可以增删改章节，
逐章生成时带全量项目参数，保证口径一致（总投资、规模在每章引用一致）。
"""

from __future__ import annotations

from .llm import chat, parse_json
from .cleaner import clean_text

# (标题, 写作提示)
DEFAULT_OUTLINE: list[dict] = [
    {"title": "总论", "hint": "项目名称、建设单位、建设地点、建设内容与规模、总投资与资金来源、主要结论，让人一页看懂全项目"},
    {"title": "项目建设背景与必要性", "hint": "政策背景、行业背景、现状问题，从问题推导出建设的必要性"},
    {"title": "市场分析与需求预测", "hint": "市场现状、需求规模、发展趋势，给出可核对的需求测算逻辑"},
    {"title": "建设规模与建设内容", "hint": "确定建设规模的理由、分期安排、主要建设内容清单"},
    {"title": "场址选择与建设条件", "hint": "场址概况、自然条件、交通水电等配套条件、用地情况"},
    {"title": "工程技术方案", "hint": "总体布局、主体工程方案、配套工程方案、主要设备选型"},
    {"title": "环境保护与节能", "hint": "主要污染物与治理措施、节能设计、环保投资"},
    {"title": "组织机构与项目实施进度", "hint": "建设期管理机构、运营期组织、实施进度计划表（分期与里程碑）"},
    {"title": "投资估算与资金筹措", "hint": "投资估算表逻辑（工程费、其他费、预备费）、资金来源构成、分年用款计划"},
    {"title": "财务与经济效益分析", "hint": "收入测算、成本测算、盈利能力指标（回收期、IRR/收益率口径）、不确定性分析"},
    {"title": "社会效益与风险分析", "hint": "社会效益、主要风险识别与应对措施"},
    {"title": "结论与建议", "hint": "可行性结论、下一步工作建议"},
]

SYSTEM = (
    "你是资深工程咨询顾问，编写可行性研究报告章节。规则：\n"
    "1. 只依据「项目参数」与「项目补充资料」写作，参数里没有的数字用〔待补：说明〕占位，绝不编造金额、面积、产能。\n"
    "2. 金额、面积、产能等关键数据必须与项目参数一致，引用时保持同一口径。\n"
    "3. 输出 Markdown：小节用 ### ，表格用 Markdown 表格。不要输出本章标题以外的章标题。\n"
    "4. 文风正式、克制，多用数据与逻辑衔接，少用空话。\n"
    "5. 目标字数指正文文字量（表格不计入），按目标 ±15% 控制。必须写成连贯段落而不是提纲，"
    "每个小节至少一段完整论述；字数不够就展开做法、依据与影响分析，不要用「略」或「详见」。"
)

GEN_TITLE_SYSTEM = (
    "你是工程咨询顾问。根据项目参数输出可行性研究报告章节大纲，JSON 数组，每项 {\"title\": 章名, \"hint\": 本章写什么}。"
    "12~14 章，覆盖总论、背景必要性、市场、规模内容、场址、技术方案、环保节能、组织进度、投资估算与资金筹措、"
    "财务经济、社会效益与风险、结论建议。只回 JSON 数组。"
)


def _params_block(params: dict) -> str:
    label = {
        "name": "项目名称", "owner": "建设单位", "location": "建设地点",
        "scale": "建设内容与规模", "investment": "总投资", "fund": "资金来源",
        "period": "建设周期", "industry": "行业领域", "extra": "补充资料",
    }
    lines = []
    for key, value in label.items():
        v = (params.get(key) or "").strip()
        if v:
            lines.append(f"- {value}：{v}")
    return "\n".join(lines) or "- （未提供参数）"


async def gen_outline(cfg, params: dict) -> list[dict]:
    """AI 生成章节大纲（参数敏感，如分期项目会多出分期章节）。"""
    user = f"项目参数：\n{_params_block(params)}\n\n请输出本章大纲 JSON 数组。"
    text = await chat(cfg, [{"role": "system", "content": GEN_TITLE_SYSTEM}, {"role": "user", "content": user}],
                      max_tokens=2000, task="extract")
    data = parse_json(text)
    if isinstance(data, dict):
        data = data.get("chapters") or data.get("outline") or []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("title"):
            out.append({"title": str(item["title"])[:40], "hint": str(item.get("hint") or "")[:120]})
        elif isinstance(item, str) and item.strip():
            out.append({"title": item.strip()[:40], "hint": ""})
    return out or [dict(x) for x in DEFAULT_OUTLINE]


async def gen_chapter(cfg, params: dict, title: str, hint: str, words: int, prev_summary: str = "") -> str:
    """生成一章正文。prev_summary 传已生成章节的标题，保持衔接不重复。"""
    user = (
        f"项目参数：\n{_params_block(params)}\n\n"
        + (f"已完成章节：{prev_summary}\n（不要重复已完成章节的内容）\n\n" if prev_summary else "")
        + f"本章：{title}\n本章要点：{hint or '按可研报告惯例展开'}\n"
        + f"本章目标字数：约 {words} 字（表格不计入，须写成连贯段落）。\n"
        + "本章至少分 3 个 ### 小节，每小节 200 字以上完整论述；数据不足处用〔待补〕，不要压缩成提纲。\n"
        + "请直接输出本章正文（Markdown）。"
    )
    text = await chat(
        cfg,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        max_tokens=max(1500, min(words * 3, 6000)), task="write",
    )
    return clean_text(text, title=title)["text"]


_LONG_CH = ("技术", "投资", "财务", "市场", "规模")


def chapter_words(title: str, n: int, total: int) -> int:
    """按章节类型粗分篇幅：技术/投资/财务/市场类章节篇幅加倍。"""
    base = total // max(n, 1)
    return int(base * 1.5) if any(k in title for k in _LONG_CH) else base
