"""数据出域闸门：所有发往外部模型的请求与内容都必须经过这里。

唯一出口是 outbound() / require_outbound()，做四件事：查开关、剔除受保护章节
（报价表 / 投标函 / 承诺函 / 资质）、脱敏（身份证 / 银行账号 / 手机号 / 邮箱）、
限长（只发必要片段），并记一条审计。开关关闭时抛 OutboundBlocked，调用方必须
降级为本地，不得自行发起外部请求（LCEL 链由 chains.build_model 统一降级）。

LCEL 链、basecore 的 6 个 AI 能力、技能执行、模型连通性测试都走这个出口。
"""
from __future__ import annotations

import re
from typing import Any

from . import config, rules, store

# 顺序敏感：先长号后短号，避免手机号规则先把身份证中段吃掉
PATTERNS = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证已脱敏]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[账号已脱敏]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已脱敏]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱已脱敏]"),
]

BLOCK_MARK = "[按隐私设置，本段为受保护章节，未外发]"
TRUNC_MARK = "\n…[按隐私设置已截断，仅发送必要片段]"


class OutboundBlocked(RuntimeError):
    """数据出域未确认或已关闭；调用方必须降级为本地，不得自行发外部请求。"""


def scrub(text: Any, limit: int | None = None) -> str:
    """脱敏 + 限长。返回可直接进提示词的文本。"""
    s = str(text if text is not None else "")
    if not s:
        return ""
    p = config.privacy()
    if p.get("scrub_sensitive", True):
        for pattern, repl in PATTERNS:
            s = pattern.sub(repl, s)
    cap = int(limit if limit is not None else (p.get("max_chars") or 12000))
    if len(s) > cap:
        s = s[:cap] + TRUNC_MARK
    return s


def guard_variables(variables: dict) -> dict:
    """对一条链的全部输入变量统一过闸。"""
    return {k: scrub(v) if isinstance(v, (str, int, float)) else v
            for k, v in variables.items()}


def draft_without_protected(sections: list[dict], per_section: int = 1500) -> str:
    """验标时只把可改写章节送给模型，保护章节的正文不出域。"""
    parts = []
    for s in sections:
        title = s.get("title") or ""
        body = (s.get("text") or "")[:per_section]
        if s.get("protected") or rules.is_protected(title):
            parts.append(f"【{title}】\n{BLOCK_MARK}")
        else:
            parts.append(f"【{title}】\n{body}")
    return "\n\n".join(parts)


def safe_sections(sections: list[dict], per_section: int = 1500) -> list[dict]:
    """返回可交给外部模型的章节副本：保护章节正文整体替换，不留在上下文里。

    basecore 的 AI 能力常直接收 sections 列表，必须在传入前替换，不能只靠提示词。
    """
    out: list[dict] = []
    for s in sections or []:
        item = dict(s)
        if item.get("protected") or rules.is_protected(str(item.get("title") or "")):
            item["content"] = BLOCK_MARK
            item["text"] = BLOCK_MARK
        else:
            body = str(item.get("content") or item.get("text") or "")[:per_section]
            item["content"] = body
            item["text"] = body
        out.append(item)
    return out


def _audit(pid: str, purpose: str, chars: int, allowed: bool) -> None:
    """每次出域或拦截都留一条可查记录；正文不入库，只记长度与用途。"""
    store.add_usage(pid, actor="guard", task=purpose, tool="outbound",
                    model="外部模型" if allowed else "已拦截（本地降级）",
                    tokens_in=int(chars / 2), ok=allowed,
                    input_text=f"purpose={purpose} chars={chars}",
                    output_text="允许出域" if allowed else "未确认/已关闭，未外发")


def require_outbound(pid: str = "", purpose: str = "") -> None:
    """不含标书正文、但会发起外部请求的操作（模型连通性测试）也过这道闸。"""
    if not config.outbound_allowed():
        _audit(pid, purpose, 0, False)
        raise OutboundBlocked("数据出域未确认或已关闭，请先到「数据出域」里确认")
    _audit(pid, purpose, 0, True)


def outbound(text: Any = "", *, pid: str = "", purpose: str = "",
             sections: list[dict] | None = None,
             limit: int | None = None) -> str:
    """唯一出域出口：开关 → 保护章节剔除 → 脱敏 → 限长 → 审计。

    传 sections 时按章节拼装并剔除保护章节（用于标书全文）；否则处理单段文本
    （招标文件、质疑函、参数等）。返回可直接进提示词的安全文本。
    """
    if not config.outbound_allowed():
        _audit(pid, purpose, 0, False)
        raise OutboundBlocked("数据出域未确认或已关闭，请先到「数据出域」里确认")
    raw = (draft_without_protected(list(sections)) if sections is not None
           else str(text if text is not None else ""))
    cap = int(limit if limit is not None else (config.privacy().get("max_chars") or 12000))
    safe = scrub(raw, limit=cap)
    _audit(pid, purpose, len(safe), True)
    return safe


def budget_exceeded(pid: str = "") -> bool:
    """当日 token 预算用尽：智能体本次只用本地基座，需求标注为人工补充。"""
    b = config.budget()
    if not b.get("enabled", True):
        return False
    return store.today_tokens(pid) >= int(b.get("daily_token_limit") or 200000)


def status() -> dict:
    p = config.privacy()
    return {
        **p,
        "outbound_allowed": config.outbound_allowed(),
        "mode": config.MODE,
        "note": ("已关闭：所有模型调用自动降级为本地演示模型，任何标书内容都不会外发。"
                 if not config.outbound_allowed() else
                 "已开启：仅发送必要片段，敏感信息脱敏，保护章节正文不外发。"),
    }
