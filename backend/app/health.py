"""整本生成后的自动体检闭环：跑完就出一份可核查的健康报告。

以前 compose 末尾只跑 auto_core 的 compose 阶段，结果塞进任务卡就结束了：
雷同检查、模拟评标、事实一致性、引用溯源这些确定性能力要么挂在别的手工
入口，要么根本不在 compose 阶段里；导出门禁又另算一套，两边可能不一致。

这里把「生成完必然该查的」收成一份报告：
  quality / 章节质量 / 逐条响应 / 雷同 / 模拟评标 / 事实 / 引用 / 合规边界
每项都记「查过 / 没查成 / 主动停用」，没查成绝不写成通过；报告按内容指纹
缓存并写入 auto_runs 留痕，导出门禁直接复用同一份结论。

不调模型、不改正文、不重写基座算法，只做编排与如实汇总。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import compliance, core_bridge, quality, reqtable, scope, store

STAGE = "health"
# 进程内缓存必须带企业：pid 是随机 id，但同一 pid 在不同企业库里可能存在，
# 只按 pid 命中会把别家的问题清单/交付结论返回给本企业。
_LAST: dict[tuple[str, str], tuple[str, dict]] = {}


def _cache_key(pid: str) -> tuple[str, str]:
    return (scope.org(), str(pid))
MAX_GATE_ISSUES = 10
DUP_HIGH = 0.45
SCORE_LOW = 60.0


def content_key(pid: str) -> str:
    """报告绑定内容：章节正文 / 版本 / 要求表 / 招标正文任一变化都要重算。"""
    p = store.get_project(pid) or {}
    secs = [[s.get("id"), int(s.get("ver") or 0),
             hashlib.md5(str(s.get("text") or "").encode("utf-8", "ignore")).hexdigest()[:12]]
            for s in store.list_sections(pid)]
    reqs = [[r.get("id"), r.get("status"), str(r.get("text") or "")[:80]]
            for r in store.list_requirements(pid)]
    from . import requirement_support
    body = [secs, store.list_requirements(pid), requirement_support.snapshot(pid),
            hashlib.md5(str(p.get("tender_text") or "")
                        .encode("utf-8", "ignore")).hexdigest()[:16]]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False,
                                     default=str, sort_keys=True)
                          .encode("utf-8", "ignore")).hexdigest()[:24]


def _rows(result: dict, title: str) -> list[dict]:
    for sec in result.get("sections") or []:
        if str(sec.get("title") or "") == title:
            return [x for x in (sec.get("rows") or []) if isinstance(x, dict)]
    return []


def _stat(result: dict, label: str) -> Any:
    for x in result.get("stats") or []:
        if str(x.get("label") or "") == label:
            return x.get("value")
    return None


def _finding(key: str, level: str, title: str, detail: str, *,
             blocking: bool = False, gate: bool = True,
             action: str = "", evidence: str = "") -> dict:
    return {"key": key, "level": level, "title": title, "detail": detail,
            "blocking": blocking, "gate": gate, "action": action,
            "evidence": evidence or "backend/app/health.py"}


def _cap(key: str, label: str, pid: str, checks: dict,
         findings: list[dict]) -> dict:
    """单项能力：区分「查过」「运行失败」「主动停用」三态。"""
    try:
        r = core_bridge.run(key, pid, {})
    except Exception as exc:
        r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if str(r.get("error") or "") == "cap_disabled":
        checks[key] = {"label": label, "status": "disabled",
                       "detail": "已在设置中停用，本次没有检查"}
        return {}
    if not r.get("ok") or r.get("error"):
        checks[key] = {"label": label, "status": "unavailable",
                       "detail": str(r.get("error") or "运行失败")[:160]}
        findings.append(_finding(
            f"health_{key}_unavailable", "高", f"自动体检未完成 · {label}",
            f"{label}没有跑完（{str(r.get('error') or '未知原因')[:100]}）；"
            "本轮结果不能当作已通过，请重试体检或人工复核。",
            blocking=True, action="重试体检"))
        return {}
    checks[key] = {"label": label, "status": "checked", "detail": "已检查"}
    return r


def assess(pid: str) -> dict:
    """只读汇总（合规模块会做一次匿名台账登记），不调模型、不改正文。"""
    checks: dict[str, dict] = {}
    findings: list[dict] = []
    metrics: dict[str, Any] = {}

    # 1) 生成质量与章节交付状态
    try:
        q = quality.assess_project(pid)
        if q.get("status") == "unavailable":
            checks["quality"] = {"label": "生成质量", "status": "unavailable",
                                 "detail": str(q.get("error") or "检查未完成")[:160]}
            findings.append(_finding(
                "health_quality_unavailable", "高", "生成质量检查未完成",
                "质量检查没有跑完，不能把本轮结果当作已通过；请重试体检。",
                blocking=True, action="重试体检"))
        else:
            n = len(q.get("defects") or [])
            checks["quality"] = {"label": "生成质量", "status": "checked",
                                 "detail": f"{n} 项待改进"}
            metrics["quality_defects"] = n
        secs = quality.section_status(pid)
        bad = [s for s in secs if not s.get("deliverable")]
        metrics["undeliverable_sections"] = len(bad)
        checks["sections"] = {"label": "章节交付状态", "status": "checked",
                              "detail": f"{len(secs)} 章，{len(bad)} 章未达交付标准"}
        if bad:
            findings.append(_finding(
                "health_sections", "中", f"{len(bad)} 章未达交付标准",
                "；".join(str(x.get("title") or "")[:24] for x in bad[:4])
                + "。章节级硬伤已在导出清单逐条列出，此处只作汇总。",
                gate=False))
    except Exception as exc:
        checks["quality"] = {"label": "生成质量", "status": "unavailable",
                             "detail": str(exc)[:160]}
        findings.append(_finding(
            "health_quality_error", "高", "生成质量检查未完成",
            f"体检过程出错（{type(exc).__name__}: {str(exc)[:100]}），不能当作已通过。",
            blocking=True, action="重试体检"))

    # 2) 逐条响应矩阵：只读已落库的矩阵，没重算过就如实说明
    try:
        rows = store.list_req_responses(pid)
        required = [r for r in store.list_requirements(pid)
                    if int(r.get("required") or 0)
                    and (r.get("status") or "active") in ("active", "na")
                    # 与要求表 effective() 同一口径：招标方义务不算投标人必需要求
                    and not reqtable.is_owner_obligation(
                        str(r.get("text") or ""), str(r.get("note") or ""))]
        missing = sum(1 for r in rows if r.get("status") == "missing")
        # 矩阵一条都没有时，旧的「待响应 0 条」是 0/0 口径错误：
        # required>0 而 rows=0 说明必需要求根本没逐条落实，必须判失败。
        uncovered = max(0, len(required) - len(rows))
        metrics.update({"requirements": len(required),
                        "matrix_rows": len(rows), "matrix_missing": missing,
                        "matrix_uncovered": uncovered})
        if uncovered:
            checks["coverage"] = {"label": "逐条响应", "status": "fail",
                                  "detail": (f"必需要求 {len(required)} 条，"
                                             f"矩阵 {len(rows)} 条，"
                                             f"未逐条落实 {uncovered} 条")}
            findings.append(_finding(
                "health_matrix_stale", "高", "必需要求未逐条落实",
                f"要求表有 {len(required)} 条必需要求，逐条响应矩阵只有 {len(rows)} 条；"
                "矩阵为空不能算「待响应 0 条」。导出门禁会逐条列出，"
                "请先补齐响应记录再复核。", blocking=True))
        else:
            checks["coverage"] = {"label": "逐条响应", "status": "checked",
                                  "detail": (f"必需要求 {len(required)} 条，"
                                             f"矩阵 {len(rows)} 条，待响应 {missing} 条")}
    except Exception as exc:
        checks["coverage"] = {"label": "逐条响应", "status": "unavailable",
                              "detail": str(exc)[:160]}

    # 3) 雷同 / 4) 模拟评标 / 5) 事实一致性 / 6) 引用溯源
    dup = _cap("dupcheck", "章节雷同", pid, checks, findings)
    if dup:
        pairs = _rows(dup, "rows")
        high = [x for x in pairs if float(x.get("相似度") or 0) >= DUP_HIGH]
        metrics["similar_pairs"] = len(pairs)
        metrics["high_similar_pairs"] = len(high)
        if high:
            findings.append(_finding(
                "health_dup", "中", f"{len(high)} 组章节疑似重复",
                "；".join(f"{str(x.get('章节 A') or '')[:16]} ↔ "
                          f"{str(x.get('章节 B') or '')[:16]} "
                          f"({x.get('相似度')})" for x in high[:3])
                + "。同一本标书内大段重复会被判为凑字数，提交前请人工合并。",
                action="检查重复章节"))
    ev = _cap("evaluate", "模拟评标", pid, checks, findings)
    if ev:
        rate = _stat(ev, "rate")
        metrics["score_rate"] = rate
        try:
            if float(rate or 0) < SCORE_LOW and float(_stat(ev, "full") or 0) > 0:
                findings.append(_finding(
                    "health_score", "中", "模拟评标得分偏低",
                    f"按评分办法粗估约 {rate} / {_stat(ev, 'full')} 分"
                    "（关键词覆盖率估算，不是真实得分）。重点补齐失分项的"
                    "具体做法、指标与证明材料。", action="查看模拟评标"))
        except (TypeError, ValueError):
            pass
    facts = _cap("facts", "事实一致性", pid, checks, findings)
    if facts:
        n = _stat(facts, "不一致") or 0
        metrics["fact_conflicts"] = n
        if n:
            findings.append(_finding(
                "health_facts", "中", f"发现 {n} 处全书事实不一致",
                "金额、比例、期限、名称在章节之间口径不同；以招标文件与"
                "已核验原件为唯一来源逐处订正。", action="核对事实表"))
    cites = _cap("verify_citations", "引用溯源", pid, checks, findings)
    if cites:
        n = _stat(cites, "待核实表述") or 0
        metrics["unverified_citations"] = n
        if n:
            findings.append(_finding(
                "health_citations", "中", f"{n} 处表述没有找到依据",
                "正文里的引用无法在本项目资料或知识库中定位；请补来源或删除。",
                action="补充依据"))

    # 7) 多企业合规边界：同标段多投标人是红线，单独列为重点提醒
    try:
        comp = compliance.audit(pid)
        checks["compliance"] = {
            "label": "多企业合规", "status": "checked" if comp.get("checked") else "unavailable",
            "detail": f"risk={comp.get('risk')}，其他企业项目 {comp.get('other_orgs', 0)} 个"}
        metrics.update({"compliance_risk": comp.get("risk"),
                        "other_orgs": comp.get("other_orgs", 0)})
        for it in comp.get("issues") or []:
            findings.append(_finding(
                str(it.get("key") or "compliance"), str(it.get("level") or "中"),
                str(it.get("title") or "合规提醒"), str(it.get("detail") or ""),
                blocking=bool(it.get("blocking")),
                gate=True, action=str(it.get("action") or ""),
                evidence=str(it.get("evidence") or "")))
    except Exception as exc:
        checks["compliance"] = {"label": "多企业合规", "status": "unavailable",
                                "detail": str(exc)[:160]}
        findings.append(_finding(
            "health_compliance_error", "高", "合规检查未完成",
            f"防雷同 / 同标段比对没有跑完（{type(exc).__name__}: {str(exc)[:100]}）；"
            "多企业使用场景下不能把没查过写成没问题。",
            blocking=True, action="重试体检"))

    blocking = [x for x in findings if x.get("blocking")]
    unavailable = [k for k, v in checks.items() if v.get("status") == "unavailable"]
    disabled = [k for k, v in checks.items() if v.get("status") == "disabled"]
    gate = [x for x in findings if x.get("gate")][:MAX_GATE_ISSUES]
    key = content_key(pid)
    undeliverable = int(metrics.get("undeliverable_sections") or 0)
    ok = not blocking and not unavailable
    # 「体检本身跑完且没有红线」≠「这本标书可以交出去」：空章 / 未达交付
    # 标准由章节质量与导出门禁单列，这里再给一个不含糊的可交付判定。
    return {
        "pid": pid, "stage": STAGE, "content_key": key,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks, "metrics": metrics, "findings": findings,
        "blocking": blocking, "unavailable": unavailable, "disabled": disabled,
        "ok": ok, "complete": not unavailable,
        "deliverable_ready": bool(ok and undeliverable == 0),
        "gate_issues": gate,
        "summary": (f"自动体检 {len(checks)} 项："
                    f"{sum(1 for v in checks.values() if v.get('status') == 'checked')} 项已查、"
                    f"{len(unavailable)} 项没查成、{len(disabled)} 项停用；"
                    f"{len(findings)} 条线索，其中 {len(blocking)} 条重点提醒；"
                    f"{undeliverable} 章未达交付标准，"
                    f"{'可进入人工复核' if ok and undeliverable == 0 else '尚不可交付'}"),
    }


def save(pid: str, report: dict) -> None:
    """进程内缓存 + auto_runs 留痕；留痕失败不影响本次报告返回。"""
    _LAST[_cache_key(pid)] = (str(report.get("content_key") or ""), report)
    try:
        slim = {k: report.get(k) for k in
                ("content_key", "checks", "metrics", "findings", "blocking",
                 "unavailable", "disabled", "ok", "complete",
                 "deliverable_ready", "gate_issues", "at")}
        store.save_auto_run(pid, STAGE, str(report.get("summary") or ""), slim)
    except Exception:
        pass


def latest(pid: str, *, refresh: bool = False) -> dict:
    """导出门禁复用同一份结论：内容没变就不重跑，变了必须重算。"""
    key = content_key(pid)
    if not refresh:
        hit = _LAST.get(_cache_key(pid))
        if hit and hit[0] == key:
            return hit[1]
        try:
            last = store.last_auto_run(pid, STAGE) or {}
            data = last.get("data") or {}
            if data.get("content_key") == key and data.get("checks"):
                report = {**data, "pid": pid, "stage": STAGE,
                          "content_key": key,
                          "summary": str(last.get("summary") or "")}
                report["blocking"] = data.get("blocking") or []
                report["ok"] = bool(data.get("ok"))
                report["complete"] = bool(data.get("complete"))
                report["deliverable_ready"] = bool(data.get("deliverable_ready"))
                _LAST[_cache_key(pid)] = (key, report)
                return report
        except Exception:
            pass
    report = assess(pid)
    save(pid, report)
    return report


def export_issues(pid: str) -> list[dict]:
    """接入导出门禁：只把需要人看的问题列出来，并标明来源。"""
    r = latest(pid)
    out = []
    for i, x in enumerate(r.get("gate_issues") or []):
        out.append({
            "key": f"health-{i}-{x.get('key')}",
            "title": "自动体检 · " + str(x.get("title") or ""),
            "level": str(x.get("level") or "中"),
            "detail": str(x.get("detail") or ""),
            "blocking": bool(x.get("blocking")),
            "action": str(x.get("action") or ""),
            "evidence": str(x.get("evidence") or "backend/app/health.py"),
        })
    if not r.get("complete"):
        out.append({
            "key": "health-incomplete", "level": "高", "blocking": True,
            "action": "重试体检",
            "title": "自动体检有项目没跑完，不能当作已通过",
            "detail": "没查成：" + "、".join(r.get("unavailable") or [])
                      + "。知情导出仍可下载，本项风险会随产物登记。",
            "evidence": "backend/app/health.py::latest",
        })
    return out[:MAX_GATE_ISSUES + 1]
