"""逐节内容规划（对标易标 CONTENT_PLAN，clean-room 重写）。

写正文前先给每节出一份"计划"：写作要点、要不要表格、要不要配图、目标字数。
计划存 sections.plan，写正文时注入提示词——让模型先想清楚再落笔，
避免"标题即提纲"式的泛泛而谈。
"""
from __future__ import annotations

import json

from ..config import LLMConfig
from .llm import LLMError, chat, parse_json

SYSTEM = (
    "你是投标文件编制组长，负责给主笔分配写作任务。只输出 JSON，计划必须具体可执行："
    "写作要点要落到本项目实际内容，不许写“根据招标文件要求提供优质服务”这类空话。"
)

TEMPLATE = """请为投标文件的以下章节制定写作计划。

项目：{project}
章节标题：{title}
编制要点：{brief}
{scoring}
招标文件要点（节选）：
{excerpt}

输出 JSON：
{{
  "points": ["写作要点1（具体做法/响应内容）", "要点2", "要点3"],
  "table": {{"need": true/false, "purpose": "表格用途，如 进度计划表/人员配置表，不需要给空字符串"}},
  "figure": true/false,
  "target_words": 900,
  "note": "给主笔的一句话提醒（30字内）"
}}

要求：
- points 3~6 条，每条是一个具体的写作方向，按重要性排序；
- 涉及流程/进度/人员/分工的内容建议用表格呈现（table.need=true）；
- target_words 在 600~1500 之间，按该节内容体量估；
- figure 只在该节确实适合放流程图/架构图时为 true。
"""


async def plan_section(
    cfg: LLMConfig,
    *,
    project: str,
    title: str,
    brief: str,
    scoring: list[dict],
    tender_text: str,
) -> dict:
    if not cfg.ready:
        return _fallback(title, brief)
    scoring_text = ""
    if scoring:
        lines = [f"- {s.get('item')}（{s.get('score') or ''}）：{s.get('rule') or ''}" for s in scoring[:12]]
        scoring_text = "相关评分项（计划要覆盖）：\n" + "\n".join(lines) + "\n"
    prompt = TEMPLATE.format(
        project=project, title=title, brief=brief or "（无，按标题自行规划）",
        scoring=scoring_text, excerpt=(tender_text or "")[:8000],
    )
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=1500, task="outline",
        )
        data = parse_json(raw)
        return _normalize(data, title, brief)
    except (LLMError, ValueError):
        return _fallback(title, brief)


def _normalize(data: dict, title: str, brief: str) -> dict:
    points = data.get("points") if isinstance(data.get("points"), list) else []
    points = [str(p).strip()[:80] for p in points if str(p).strip()][:6]
    table = data.get("table") if isinstance(data.get("table"), dict) else {}
    try:
        words = int(data.get("target_words") or 0)
    except (TypeError, ValueError):
        words = 0
    words = max(400, min(words or 900, 2000))
    return {
        "points": points or [brief or f"围绕「{title}」展开具体做法与响应"],
        "table": {"need": bool(table.get("need")), "purpose": str(table.get("purpose") or "")[:60]},
        "figure": bool(data.get("figure")),
        "target_words": words,
        "note": str(data.get("note") or "")[:60],
        "source": "ai",
    }


def _fallback(title: str, brief: str) -> dict:
    return {
        "points": [brief or f"围绕「{title}」展开具体做法与响应"],
        "table": {"need": False, "purpose": ""},
        "figure": False,
        "target_words": 900,
        "note": "未配置 AI，这是默认计划",
        "source": "default",
    }


def plan_block(plan: dict | None) -> str:
    """把计划转成提示词块，writer.build_messages 注入。"""
    if not plan:
        return ""
    lines = ["本节写作计划（已规划，请严格按计划展开）："]
    points = plan.get("points") or []
    for idx, point in enumerate(points, 1):
        lines.append(f"{idx}. {point}")
    table = plan.get("table") or {}
    if table.get("need") and table.get("purpose"):
        lines.append(f"- 请用 Markdown 表格呈现：{table['purpose']}")
    if plan.get("figure"):
        lines.append("- 本节适合配一张流程/架构图（如系统支持可在生成后添加配图）")
    if plan.get("note"):
        lines.append(f"- 提醒：{plan['note']}")
    return "\n".join(lines)
