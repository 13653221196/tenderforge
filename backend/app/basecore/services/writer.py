"""章节正文生成（流式）。输出受限 Markdown，便于稳定转 Word。"""
from __future__ import annotations

from typing import AsyncIterator

from ..config import LLMConfig
from .kb import MATERIAL_RULES
from .llm import LLMError, chat, chat_stream
from .planner import plan_block

SYSTEM = (
    "你是投标文件主笔，写作风格克制、专业、可验证。硬性要求：\n"
    "1. 只写与本节标题相关的内容，不要复述整份招标文件；\n"
    "2. 不得编造资质证书编号、合同金额、人员姓名、检测数据、公司名称与品牌型号；"
    "投标人名称只能用全局事实里给出的名称，未给出就写〔待填：投标人名称〕；缺失信息一律写成〔待填：xxx〕；\n"
    "3. 允许的排版只有：以 ### 开头的子标题、普通段落、以 - 开头的列表、简单 Markdown 表格；\n"
    "4. 不要输出 # 一级标题（章节标题由系统生成），不要使用加粗、斜体、代码块、图片；\n"
    "5. 段落之间空一行，单段不超过 200 字；\n"
    "6. 全文用中文书面语，不用“我们将”“致力于”这类空话，多用“本方案采用…”“响应…要求”的表述。"
)

TEMPLATE = """请为投标文件撰写以下章节的正文。

项目：{project}
章节层级：{level_label}
章节标题：{title}
编制要点：{brief}
字数要求：{words} 字左右（不少于 {min_words} 字）

{plan}

{context}

{facts}

{materials}

{extra}

直接输出正文，不要任何前言与结语。"""


def _context_block(meta: dict, tender_text: str, siblings: list[dict]) -> str:
    parts: list[str] = []
    if meta:
        basic = meta.get("basic") or []
        lines = [f"- {row.get('label')}：{row.get('value')}" for row in basic if row.get("value")]
        if lines:
            parts.append("项目基本信息：\n" + "\n".join(lines))
        scoring = meta.get("scoring") or []
        if scoring:
            items = [f"- {s.get('item')}（{s.get('score') or '分值未标注'}）：{s.get('rule') or ''}" for s in scoring[:12]]
            parts.append("评分项（写作时优先呼应）：\n" + "\n".join(items))
        star = meta.get("star_clauses") or []
        if star:
            items = [f"- {s.get('item')}" for s in star[:10]]
            parts.append("星号强制条款（必须正面响应）：\n" + "\n".join(items))
    if tender_text:
        parts.append("招标文件节选（仅供理解，不要照抄）：\n" + tender_text[:6000])
    if siblings:
        titles = [f"- {s['title']}" for s in siblings[:14]]
        parts.append("本篇其他章节（避免内容重复）：\n" + "\n".join(titles))
    return "\n\n".join(parts)


def _extra_block(instruction: str | None) -> str:
    if not instruction:
        return ""
    return f"额外要求（优先满足）：{instruction}"


def _material_block(materials: str, sources: list[dict] | None) -> str:
    """素材块由 kb.build_material_block 生成，这里只负责补上规则说明。"""
    if not (materials or "").strip():
        return ""
    return MATERIAL_RULES.format(materials=materials.strip())


def build_messages(
    *,
    project: str,
    title: str,
    brief: str,
    level: int,
    words: int,
    meta: dict,
    tender_text: str,
    siblings: list[dict],
    instruction: str | None = None,
    materials: str = "",
    facts_block: str = "",
    plan: dict | None = None,
) -> list[dict]:
    level_label = {1: "一级章节", 2: "二级小节", 3: "三级条目"}.get(level, "章节")
    prompt = TEMPLATE.format(
        project=project,
        level_label=level_label,
        title=title,
        brief=brief or "（无，自行按标题展开）",
        words=words,
        min_words=max(200, int(words * 0.7)),
        plan=plan_block(plan),
        context=_context_block(meta, tender_text, siblings),
        facts=facts_block,
        materials=_material_block(materials, None),
        extra=_extra_block(instruction),
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]


async def stream_section(cfg: LLMConfig, **kwargs) -> AsyncIterator[str]:
    messages = build_messages(**kwargs)
    async for chunk in chat_stream(cfg, messages, max_tokens=min(cfg.max_tokens, 8000), task="write"):
        yield chunk


async def write_section(cfg: LLMConfig, **kwargs) -> str:
    messages = build_messages(**kwargs)
    return await chat(cfg, messages, max_tokens=min(cfg.max_tokens, 8000), task="write")


# ---------------- 改写：补写 / 精简 / 润色 ----------------
REWRITE_MODES = {
    "expand": "内容偏少，请扩写到目标字数：补充具体做法、实施步骤、责任分工、时间节点、可验证的指标；不得重复已有句子凑数。",
    "condense": "篇幅超出，请压缩到目标字数：保留全部结论、数字与承诺，删去重复表述、空话与铺垫。",
    "polish": "保持篇幅基本不变，改写为更严谨的书面语：去掉口语与套话，句子改短，逻辑连接明确。",
}

REWRITE_TEMPLATE = """下面是《{title}》的现有正文，请按要求改写。

改写要求：{mode_label}
目标篇幅：{target} 字左右（当前 {current} 字）

硬性约束：
1. 原文中出现的项目名称、金额、日期、单位名称、人名、证书编号必须原样保留，不得改写或替换；
2. 不得新增未经证实的数据、业绩或资质，缺失信息仍写〔待填：xxx〕；
3. 保持原有的 ### 子标题、列表、表格结构，不要输出一级标题；
4. 只输出改写后的完整正文，不要解释改了什么。

{materials}

原文：
{text}"""


async def rewrite_section(
    cfg: LLMConfig,
    *,
    title: str,
    text: str,
    mode: str,
    target: int,
    current: int,
    materials: str = "",
) -> str:
    prompt = REWRITE_TEMPLATE.format(
        title=title,
        mode_label=REWRITE_MODES.get(mode, REWRITE_MODES["polish"]),
        target=target,
        current=current,
        materials=_material_block(materials, None),
        text=text[:12000],
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    return await chat(cfg, messages, max_tokens=min(cfg.max_tokens, 8000), task="rewrite")


def fallback_section(title: str, brief: str) -> str:
    """无 AI 时的结构化骨架，保证流程可走通且不伪造事实。"""
    lead = brief or "请围绕本节标题展开，先写结论再写做法。"
    return (
        f"### 编写说明\n\n"
        f"本节尚未接入 AI，因此仅生成结构骨架，以下三段按“现状—做法—验证”展开，每段 150 字左右。\n\n"
        f"### 一、响应要点\n\n"
        f"本节编制要点为：{lead}\n\n"
        f"需逐条对应招标文件相应条款，明确响应与否；存在差异的，须在此说明差异原因与替代方案。\n\n"
        f"### 二、具体做法\n\n"
        f"〔待填：本节的技术路线、实施步骤、责任人、时间节点〕\n\n"
        f"### 三、可验证的承诺\n\n"
        f"〔待填：可量化的指标、验收方式、不达标时的补救措施〕\n\n"
        f"> 提示：在“设置”中配置 AI 接口后，本节可一键生成完整正文。"
    )
