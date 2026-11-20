"""提示词型技能的执行器。

约定：SKILL.md 正文中的第一个 ```prompt 代码块即提示词模板，
可用 ``{{tender}}``、``{{bid}}``、``{{params}}``、``{{focus}}`` 占位。
模型只收到用户自己配置的接口，不做任何外部调用。
"""
from __future__ import annotations

import asyncio
import re

from ...config import LLMConfig, TENDER_TEXT_LIMIT
from .. import tender_sections
from ..llm import LLMError, chat

PROMPT_BLOCK = re.compile(r"```prompt\s*\n([\s\S]*?)```", re.I)


def extract_prompt(body: str) -> str:
    match = PROMPT_BLOCK.search(body or "")
    if match:
        return match.group(1).strip()
    return (body or "").strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n……（后文已截断）"


def build_messages(meta, ctx: dict, *, tender_text: str | None = None, part_note: str = "") -> list[dict]:
    template = extract_prompt(meta.body)
    params = ctx.get("params") or {}
    focus = str(params.get("focus") or "").strip()

    tender = ctx.get("tender_text") if tender_text is None else tender_text
    tender = (tender or "").strip()
    if part_note:
        tender = f"{part_note}\n\n{tender}"

    filled = (
        template.replace("{{tender}}", _clip(tender, TENDER_TEXT_LIMIT))
        .replace("{{bid}}", _clip(ctx.get("bid_text") or "", TENDER_TEXT_LIMIT))
        .replace("{{params}}", "\n".join(f"- {k}：{v}" for k, v in params.items() if k != "focus"))
        .replace("{{focus}}", focus or "无特别指定")
    )
    # 支持任意自定义参数占位，如 {{mode}}、{{target}}
    for key, value in params.items():
        filled = filled.replace("{{" + str(key) + "}}", str(value))
    system = (
        f"你是投标业务专家，正在执行技能「{meta.title or meta.name}」。"
        "严格依据给定材料作答，材料里没有的信息标注「原文未提及」，不得编造。"
        "输出使用 Markdown，结构清晰，不要客套话。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": filled}]


async def run_prompt(meta, ctx: dict, cfg: LLMConfig) -> dict:
    if not cfg.ready:
        from ._common import item, result

        return result(
            "该技能需要调用大模型，但当前尚未配置 AI 接口。",
            [item("mid", "AI 未配置",
                  f"「{meta.title or meta.name}」属于提示词型技能，需要模型完成。",
                  advice="到「设置」页填写接口地址与模型名后重试；规则型技能无需配置即可运行。")],
            verdict="info",
        )

    messages = build_messages(meta, ctx)
    tender = (ctx.get("tender_text") or "").strip()
    chunks, report = tender_sections.plan(tender, TENDER_TEXT_LIMIT) if tender else ([], {})
    try:
        if len(chunks) > 1:
            # 招标文件超长：分段落逐段执行同一技能，再把结果拼成一份。
            # 不截断——技能（如废标条款、资格审查）漏看一章就会漏条。
            sem = asyncio.Semaphore(3)

            async def run_chunk(idx: int, chunk: dict) -> str:
                note = (
                    f"（这是招标文件的第 {idx}/{len(chunks)} 段，只针对本段内容作答，"
                    "不要复述其他段落，也不要因为本段没提到就判为不存在。）"
                )
                async with sem:
                    msgs = build_messages(meta, ctx, tender_text=chunk["text"], part_note=note)
                    return await chat(cfg, msgs, max_tokens=cfg.max_tokens)

            texts = await asyncio.gather(*(run_chunk(i, c) for i, c in enumerate(chunks, 1)))
            body_parts: list[str] = []
            for i, (chunk, out) in enumerate(zip(chunks, texts), 1):
                chapter = chunk["chapters"][0] if chunk.get("chapters") else ""
                head = f"### 第 {i} 段（{chapter}）" if chapter else f"### 第 {i} 段"
                body_parts.append(f"{head}\n\n{out}")
            body = "\n\n".join(body_parts)
            text = f"（招标文件共 {report['total_chars']:,} 字，已分 {len(chunks)} 段全文分析）\n\n{body}"
        else:
            text = await chat(cfg, messages, max_tokens=cfg.max_tokens)
    except LLMError as exc:
        from ._common import item, result

        return result(
            "模型调用失败。",
            [item("high", "AI 调用失败", str(exc), advice="检查接口地址、密钥与模型名是否正确。")],
            verdict="warn",
        )

    from ._common import result

    data = result(
        f"AI 已完成「{meta.title or meta.name}」，输出 {len(text)} 字。",
        [],
        verdict="info",
        stats={"输出字数": len(text), "模式": "AI 提示词"},
        notice="内容由模型生成，请人工复核关键数字与结论。",
    )
    data["markdown"] = text
    return data
