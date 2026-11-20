"""把四套结论收敛成一句话：现在能不能交出去。

以前 quality / health / export_gate / delivery 各说一套，用户看到好几张卡片
也不知道该信哪个。这里只做「取数 + 判定」：不新增检查、不调模型、不改正文、
不在这里强制重跑体检（否则打开一次页面就把 8 项检查全跑一遍）。

取数来源：
  - quality.assess_project   正文质量与每章是否达标（纯读）
  - auto_runs 最近一次整本体检留痕（内容变了就标 stale，不拿旧结论充数）
  - quality.export_issues    导出门禁的重点提醒清单（纯读）

判定逻辑抽成 decide()，可直接用构造数据回归三态，不依赖数据库。
诚实性约束：没查完、没体检、体检过期一律不能判成「可以导出」。
"""
from __future__ import annotations

import time

from . import health, quality, store

MAX_FINDINGS = 5


def _last_health(pid: str) -> tuple[dict, str]:
    """返回 (最近一次体检留痕, fresh / stale / missing)。绝不在这里重跑体检。"""
    try:
        last = store.last_auto_run(pid, health.STAGE) or {}
        data = last.get("data") or {}
        if not data.get("checks"):
            return {}, "missing"
        state = "fresh" if data.get("content_key") == health.content_key(pid) else "stale"
        return {**data, "summary": str(last.get("summary") or "")}, state
    except Exception:
        return {}, "missing"


def effective_mode() -> str:
    """只有「live + 已配好可用模型」才算真生成；其余正文只是演示模板。"""
    from . import config
    try:
        if config.MODE == "live" and config.llm_config("write").ready:
            return "live"
    except Exception:
        pass
    return "demo"


def decide(q: dict, h: dict, gate: list[dict], *,
           health_state: str = "fresh", chapters: int = 0,
           mode: str = "live") -> dict:
    """纯判定：红 = 还不能交，黄 = 可导出但需确认，绿 = 可以导出。

    mode='demo'（没接模型 / 内置演示）时一律不判绿：demo 稿是通用模板，
    不针对这份招标逐条响应，质量规则查不出「没响应」不代表可以作为投标依据。
    """
    q = q or {}
    h = h or {}
    gate = [x for x in (gate or []) if isinstance(x, dict)]
    bad = [x for x in (q.get("undeliverable") or []) if isinstance(x, dict)]
    q_status = str(q.get("status") or ("pass" if q.get("all_passed") else "fail"))
    q_complete = bool(q.get("complete", True))
    blocking = [x for x in gate if x.get("blocking")]
    high = [x for x in gate if str(x.get("level") or "") == "高"]
    health_issues = [x for x in (h.get("gate_issues") or []) if isinstance(x, dict)]
    risk = blocking or high or health_issues

    reasons: list[str] = []
    if mode != "live":
        reasons.append("当前是演示 / 未接模型模式，正文是模板稿，不能作为投标依据")
    if q_status == "unavailable" or not q_complete:
        reasons.append("正文检查没跑完，不能当作没问题")
    if q_status == "fail":
        reasons.append("正文检查还有未通过项")
    if bad:
        reasons.append(f"{len(bad)} 章未达交付标准")
    if health_state == "missing":
        reasons.append("还没做整本体检")
    elif health_state == "stale":
        reasons.append("正文改过，上次的体检结论已过期")
    elif not h.get("deliverable_ready"):
        reasons.append("自动体检没有判定为可交付")
    if health_state == "fresh" and not h.get("complete"):
        reasons.append("体检有项目没查成")

    # 硬问题必须由人处理；缺体检 / 结论过期 / 有风险提醒只降为「需确认」
    hard = (bool(bad) or q_status in ("fail", "unavailable")
            or not q_complete or mode != "live")
    if health_state == "fresh":
        hard = hard or not h.get("deliverable_ready") or not h.get("complete")
    soft = health_state != "fresh" or bool(risk)
    level = "red" if hard else ("yellow" if soft else "green")

    if level == "red":
        title = "还不能交"
        reason = "；".join(reasons) or "有章节未达交付标准"
        action = ({"label": "去改正文", "tab": "write"}
                  if bad or q_status == "fail"
                  else {"label": "重新检查", "act": "check"})
    elif level == "yellow":
        title = "可以导出，但要先确认"
        parts = list(reasons)
        if risk:
            parts.append(f"门禁还有 {len(risk)} 项重点提醒")
        reason = "；".join(parts) or "有风险项需要知情确认"
        action = ({"label": "先跑一次整本体检", "act": "check"}
                  if health_state != "fresh"
                  else {"label": "下载 Word", "act": "exportWord"})
    else:
        title = "可以导出"
        reason = f"{chapters} 章均达标，质量与体检都查过，门禁没有重点提醒"
        action = {"label": "下载 Word", "act": "exportWord"}

    findings = [{"level": str(x.get("level") or "中"),
                 "title": str(x.get("title") or "")[:80],
                 "detail": str(x.get("detail") or "")[:160]}
                for x in (blocking or high or health_issues or gate)[:MAX_FINDINGS]]
    return {
        "level": level, "title": title, "reason": reason, "action": action,
        "findings": findings,
        "counts": {"chapters": chapters, "undeliverable": len(bad),
                   "blocking": len(blocking), "high": len(high),
                   "health_issues": len(health_issues)},
        "checks": {"quality": q_status, "health": health_state,
                   "gate_items": len(gate)},
        "hint": "导出时会再核一次；点「仍然导出」属于知情确认，不等于检查通过",
    }


def assess(pid: str) -> dict:
    """只读汇总：不写库、不重跑体检、不调模型。"""
    q = quality.assess_project(pid)
    h, state = _last_health(pid)
    try:
        gate = quality.export_issues(pid)
    except Exception as exc:
        # 门禁没跑完本身就是要人看的结论，不能吞成「没问题」
        gate = [{"key": "gate_error", "level": "高", "blocking": True,
                 "title": "导出门禁没查完",
                 "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}]
    try:
        chapters = len(store.list_sections(pid))
    except Exception:
        chapters = 0
    mode = effective_mode()
    r = decide(q, h, gate, health_state=state, chapters=chapters, mode=mode)
    r.update({"pid": pid, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "health_state": state, "mode": mode})
    return r
