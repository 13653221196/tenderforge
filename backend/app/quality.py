"""生成质量自动核查：主链路与验收评测共用的唯一实现。

四个维度（只查能自动判定的硬伤）：
1. 逐条响应词面覆盖 —— 招标硬性要求里的关键词有没有在正文出现；
2. 正文数值有据 —— 金额 / 比例 / 期限 / 数量能否在招标书或资料里逐字找到；
3. 无占位符与内部标注 —— XXX / TODO / 待填 / 依据清单等不得留在交付正文；
4. 章节不为空 —— 非保护章节不能是空壳或一句话。

刻意不评：文采、方案优劣、评分办法得分、专家判断、OCR 准确率。
**词面覆盖不等于实质响应**；这一层只回答「有没有明显硬伤」，不回答「能不能中标」。

主链路用法：写章后 assess_section 自检 → 按缺陷类型定向重写、二次复检 →
仍不合格标为「本章未达交付标准」并列入导出重点提醒 → 闭环复盘把
assess_project 的缺陷当本地事实回模型 → 导出门禁把硬伤列为待确认问题。
只有能自动判定的章节级事实硬伤（数值 / 占位符 / 空章）才硬阻断；
词面覆盖、实质响应、评分项覆盖是启发式结论，仍需人工确认，不冒充证据。
评测用法：assess / CHECK_LABELS 由 backend/eval 直接引用，口径完全一致。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import (forms, provenance, reqtable, rules, section_types, settings,
               store, substantive)

PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z])(?:XXX|TODO)(?![A-Za-z])|待填|待补充|待完善|"
    r"【需人工补充】|【依据与来源】|（依据：|\[请填写\]|_{3,}|\{\{[^}]{1,40}\}\}",
    re.I)

MIN_CHARS = 60
COVERAGE_TARGET = 0.60
OVERLAP_HIT = 0.45
TARGET_SCORING = 0.60

CHECK_KEYS = ("coverage", "numbers", "placeholders", "empty")
CHECK_LABELS = ("逐条响应词面覆盖", "正文数值有据",
                "无占位符与内部标注", "章节不为空")
# 导出门禁里按硬伤程度分级：占位符 / 空章必须逐条确认，其余提示即可
HIGH_KEYS = ("placeholders", "empty")
# 章节级事实硬伤：一律列为重点提醒（blocking），知情导出仍可下载并留下风险记录
BLOCK_KEYS = ("numbers", "placeholders", "empty", "claims")
BLOCK_LABELS = {"numbers": "正文数值缺少可核查依据",
                "placeholders": "正文残留占位符 / 内部标注",
                "empty": "空章 / 内容过少",
                "claims": "正文含无原件支撑的资质 / 承诺既成事实",
                "form": "保护表单字段 / 附件 / 签章未完成",
                "unavailable": "本轮检查未完成"}


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", str(text or "")).lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _as_text(src: Any) -> str:
    if isinstance(src, dict):
        return str(src.get("snippet") or src.get("content")
                   or src.get("text") or "")
    return str(src or "")


def _coverage(tender_text: str, body: str,
              reqs: list[str] | None = None) -> tuple[dict, bool]:
    # 招标要求表有人工修订版时优先用它，不再从原文重抽
    req_list = [str(x).strip() for x in (reqs or []) if str(x).strip()]
    if not req_list:
        req_list = rules.extract_requirements(tender_text)
    fg = _grams(body)
    covered, missing = 0, []
    for req in req_list:
        qg = _grams(req)
        if not qg:
            continue
        overlap = len(qg & fg) / len(qg)
        if overlap >= OVERLAP_HIT:
            covered += 1
        else:
            missing.append({"req": req[:90], "overlap": round(overlap, 2)})
    total = len(req_list)
    ratio = 1.0 if total == 0 else round(covered / total, 4)
    passed = total == 0 or ratio >= COVERAGE_TARGET
    return ({"total": total, "covered": covered, "ratio": ratio,
             "target": COVERAGE_TARGET, "overlap_hit": OVERLAP_HIT,
             "missing": missing[:6]}, passed)


def _numbers(sections: list[dict], sources: list[Any],
             tender_text: str, pid: str = "") -> list[dict]:
    ev = [{"snippet": _as_text(s)[:4000]} for s in (sources or [])]
    bad, seen = [], set()
    for sec in sections:
        # 抄录的招标原文（条款号 4.1、投标有效期 90 天等）不是我方声称的
        # 数值，先剔除引用列，避免被判成「无依据数字」。
        text = rules.strip_quoted(str(sec.get("text") or ""))
        if not text.strip():
            continue
        for token in provenance.verify_numbers(text, ev, tender_text, pid):
            key = (str(sec.get("title") or ""), token)
            if key not in seen:
                seen.add(key)
                bad.append({"section": key[0], "token": token})
    return bad[:12]


def _placeholders(sections: list[dict]) -> list[dict]:
    out = []
    for sec in sections:
        title = str(sec.get("title") or "")
        if sec.get("protected") or rules.is_protected(title):
            continue
        for m in PLACEHOLDER_RE.finditer(
                rules.strip_quoted(str(sec.get("text") or ""))):
            out.append({"section": title, "token": m.group(0)[:40]})
            if len(out) >= 12:
                return out
    return out


def _empty(sections: list[dict]) -> list[dict]:
    out = []
    for sec in sections:
        title = str(sec.get("title") or "")
        if sec.get("protected") or rules.is_protected(title):
            # 保护章节不套字数下限，但完全没写要单独报出来
            if not str(sec.get("text") or "").strip():
                out.append({"section": title, "chars": 0, "min": 1})
            continue
        n = len(str(sec.get("text") or "").strip())
        if n < MIN_CHARS:
            out.append({"section": title, "chars": n, "min": MIN_CHARS})
    return out[:12]


def assess(tender_text: str = "", sections: list[dict] | None = None,
           sources: list[Any] | None = None,
           reqs: list[str] | None = None,
           scoring_rows: list[dict] | None = None,
           pid: str = "") -> dict:
    """给一份标书打「可自动核查的硬指标」，不调模型、不写库。"""
    secs = [s for s in (sections or []) if isinstance(s, dict)]
    body = "\n".join(str(s.get("text") or "") for s in secs)
    tender = str(tender_text or "")
    cov, cov_ok = _coverage(tender, body, reqs)
    nums = _numbers(secs, list(sources or []), tender, pid)
    phs = _placeholders(secs)
    empties = _empty(secs)
    values = (cov_ok, not nums, not phs, not empties)
    flags = dict(zip(CHECK_KEYS, values))
    checks = dict(zip(CHECK_LABELS, values))
    return {
        "checks": checks, "flags": flags,
        "all_passed": all(values),
        # 三态：pass 全部通过 / fail 有未通过项 / unavailable 本轮没跑完。
        # 只有 pass 才能当成「检查过且没问题」，unavailable 必须提示重试或人工复核。
        "status": "pass" if all(values) else "fail",
        "passed_count": sum(1 for v in values if v),
        "total_checks": len(values),
        "sections": len(secs),
        "coverage": cov,
        "unsupported_numbers": nums,
        "placeholders": phs,
        "empty_sections": empties,
        "substantive": substantive.summarize(tender, secs, reqs, scoring_rows),
        "requirement_source": ("招标要求表（人工修订优先）" if reqs or scoring_rows
                               else "招标文件自动抽取"),
        "scope": "生成正文的四项自动硬指标（词面覆盖 / 数值有据 / 占位符 / 空章）+ 逐条实质响应",
        "claim": "只验证可自动核查的硬指标；词面覆盖不等于实质响应，实质响应判定仍是自动启发式，未经专家复核，不构成评标结论",
    }


def defects(result: dict) -> list[dict]:
    """把核查结果整理成可回模型 / 可进门禁的缺陷清单。"""
    out: list[dict] = []
    flags = result.get("flags") or {}
    cov = result.get("coverage") or {}
    if not flags.get("coverage", True):
        miss = "、".join(str(x.get("req") or "")[:40]
                        for x in (cov.get("missing") or [])[:2])
        out.append({"key": "coverage", "level": "中",
                    "title": "招标硬性要求响应覆盖不足",
                    "detail": f"覆盖 {cov.get('covered', 0)}/{cov.get('total', 0)}"
                              f"（目标 {cov.get('target', COVERAGE_TARGET)}）"
                              + (f"；未命中：{miss}" if miss else "")})
    if not flags.get("numbers", True):
        toks = "、".join(f"{x['section']}·{x['token']}"
                        for x in (result.get("unsupported_numbers") or [])[:3])
        out.append({"key": "numbers", "level": "中",
                    "title": "正文数值缺少可核查依据",
                    "detail": f"{len(result.get('unsupported_numbers') or [])} 处：{toks}"})
    if not flags.get("placeholders", True):
        toks = "、".join(f"{x['section']}·{x['token']}"
                        for x in (result.get("placeholders") or [])[:3])
        out.append({"key": "placeholders", "level": "高",
                    "title": "正文残留占位符 / 内部标注",
                    "detail": f"{len(result.get('placeholders') or [])} 处：{toks}"})
    if not flags.get("empty", True):
        toks = "、".join(f"{x['section']}（{x['chars']} 字）"
                        for x in (result.get("empty_sections") or [])[:3])
        out.append({"key": "empty", "level": "高",
                    "title": "存在空章 / 内容过少",
                    "detail": f"{len(result.get('empty_sections') or [])} 章：{toks}"})
    sub = result.get("substantive") or {}
    req = sub.get("requirements") or {}
    if req and not (req.get("total") == 0 or req.get("ratio", 0) >= req.get("target", 0.6)):
        out.append({"key": "substantive", "level": "中",
                    "title": "部分硬性要求缺实质响应",
                    "detail": f"逐条响应 {req.get('responded', 0)}/{req.get('total', 0)}"
                              + ("；未落实：" + "、".join(x[:24] for x in (req.get("unresponded") or [])[:2])
                                 if req.get("unresponded") else "")})
    sc = sub.get("scoring") or {}
    if sc.get("available") and (sc.get("ratio") or 0) < TARGET_SCORING:
        out.append({"key": "scoring", "level": "中",
                    "title": "评分办法得分点覆盖不足",
                    "detail": f"覆盖 {sc.get('covered', 0)}/{sc.get('total', 0)}"
                              + ("；未覆盖：" + "、".join((sc.get("missing") or [])[:2])
                                 if sc.get("missing") else "")})
    return out


def defect_lines(result: dict, limit: int = 6) -> list[str]:
    return [f"[{d['level']}] {d['title']}：{d['detail']}"
            for d in defects(result)[:limit]]


# 最近一次「资料读取 / 要求表」没读成的记录：读不到不是「没有依据」，是「没查」
_SOURCE_ERRORS: dict[str, str] = {}
# R05：解析不完整、尚未人工核对的资料名
_UNCHECKED: dict[str, list[str]] = {}
# B02：被排除的范例资料数量，界面据此说明「范例不作事实依据」
_EXAMPLE_EXCLUDED: dict[str, int] = {}


def example_excluded(pid: str) -> int:
    return int(_EXAMPLE_EXCLUDED.get(pid) or 0)


def source_error(pid: str) -> str:
    return _SOURCE_ERRORS.get(pid, "")


def unchecked_sources(pid: str) -> list[str]:
    """解析不完整、尚未人工核对的资料名；这些范围不能被算作「已核查」。"""
    return list(_UNCHECKED.get(pid, []))


def project_sources(pid: str) -> list[dict]:
    """核查数值有据时可用的证据：本项目资料 + 企业素材库。

    读取失败要留痕，不能静默回落到空列表——空列表会被下游当成「资料里确实没有」。
    R05：待核对资料不进依据池，但记下名字，导出门禁据此硬阻断。
    """
    try:
        docs = store.usable_docs(pid)
        pending = store.pending_docs(pid)
        p = store.get_project(pid) or {}
        tp = p.get("tender_parse")
        if isinstance(tp, str) and tp:
            try:
                tp = json.loads(tp)
            except (ValueError, TypeError):
                tp = {}
        if isinstance(tp, dict) and tp.get("needs_review") and not tp.get("confirmed_at"):
            pending = pending + [{"title": f"招标文件·{tp.get('filename') or '原件'}"}]
        _UNCHECKED[pid] = [str(d.get("title") or "未命名资料") for d in pending]
        _SOURCE_ERRORS.pop(pid, None)
        # B02：整本标书范例只能学结构/写法，不能证明当前企业的金额、业绩或资质数值。
        # 写作参考走 samples.reference_block 单独注入；数值证据池里一律排除 sample。
        evidence = [d for d in docs if str(d.get("kind") or "") != "sample"]
        _EXAMPLE_EXCLUDED[pid] = len(docs) - len(evidence)
        return [{"snippet": str(d.get("content") or "")[:4000]} for d in evidence]
    except Exception as exc:
        _SOURCE_ERRORS[pid] = f"{type(exc).__name__}: {exc}"[:160]
        _UNCHECKED[pid] = ["资料读取失败（未核查）"]
        return []


def assess_project(pid: str) -> dict:
    """从库里取当前标书跑核查；失败也不抛，返回明确的不可用状态。

    注意：检查本身出错时 status=unavailable、all_passed=False。
    以前这里返回 all_passed=True，等于「没查」被当成「查过没问题」，
    导出、领奖台、对话结论都会被它带着走。
    """
    # 状态契约：status 只回答「结论」——pass / fail / unavailable；
    # complete 与 unchecked 回答「有没有查全」，两者不互相替代。
    unchecked: list[dict] = []
    try:
        p = store.get_project(pid)
        secs = p.get("sections") or store.list_sections(pid)
        # 招标要求表（人工修订优先）同时喂给覆盖度、实质响应与评分项覆盖
        reqs, scoring = None, None
        try:
            reqs = reqtable.hard_texts(pid) or None
            scoring = reqtable.scoring_rows(pid) or None
        except Exception as exc:
            unchecked.append({"key": "requirements", "label": "招标要求表",
                              "status": "unavailable",
                              "reason": f"读取失败（{type(exc).__name__}: {str(exc)[:100]}）"})
        r = assess(p.get("tender_text") or "", secs,
                   project_sources(pid), reqs, scoring, pid=pid)
        r["pid"] = pid
        r["defects"] = defects(r)
        r["section_status"] = section_status(pid)
        r["undeliverable"] = [x for x in r["section_status"] if not x["deliverable"]]
        if any(x.get("unavailable") for x in r["section_status"]):
            unchecked.append({"key": "section_status", "label": "章节交付状态",
                              "status": "unavailable", "reason": "章节状态计算失败，无法确认每章是否达标"})
        err = source_error(pid)
        if err:
            unchecked.append({"key": "project_sources", "label": "资料与依据读取",
                              "status": "unavailable", "reason": f"读取失败（{err}）"})
        r.setdefault("status", "pass" if r.get("all_passed") else "fail")
        r["complete"] = not unchecked
        r["unchecked"] = unchecked
        if unchecked:
            r["claim"] = ((r.get("claim") or "") + "；未完成范围："
                          + "、".join(x["label"] for x in unchecked)).strip("；")
        return r
    except Exception as exc:
        return {"pid": pid, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:160],
                "status": "unavailable", "all_passed": False, "complete": False,
                "unchecked": [{"key": "assess", "label": "整本质量核查",
                               "status": "unavailable",
                               "reason": f"{type(exc).__name__}: {exc}"[:160]}],
                "passed_count": 0, "total_checks": 4,
                "flags": {}, "checks": {}, "defects": [], "section_status": [],
                "undeliverable": [], "coverage": {}, "unsupported_numbers": [],
                "placeholders": [], "empty_sections": [],
                "claim": "本轮检查没有跑完，结果不可用；请重试或人工复核，不能当作已通过"}


def assess_section(tender_text: str, sec: dict,
                   sources: list[Any] | None = None) -> dict:
    """单章自检：只查本章能判定的硬伤，不算整本覆盖（覆盖是全书指标）。"""
    clean = dict(sec or {})
    clean["text"] = provenance.clean_for_export(str(clean.get("text") or ""))
    nums = _numbers([clean], list(sources or []), str(tender_text or ""))
    phs = _placeholders([clean])
    empties = _empty([clean])
    # 保护章节是人工填的原件表单，里面出现「已取得」是正常表述；其余章节
    # 只要写了「我方已取得资格预审结果通知书」这类既成事实，就必须复核原件。
    claims = ([] if clean.get("protected") or rules.is_protected(
        str(clean.get("title") or "")) else
        section_types.claim_violations(clean.get("text") or ""))
    flags = {"numbers": not nums, "placeholders": not phs,
             "empty": not empties, "claims": not claims}
    lines: list[str] = []
    if nums:
        lines.append("正文含无据数值：" + "、".join(x["token"] for x in nums[:4]))
    if phs:
        lines.append("残留占位符：" + "、".join(x["token"] for x in phs[:4]))
    if empties:
        lines.append(f"章节仅 {empties[0]['chars']} 字，少于 {MIN_CHARS} 字")
    if claims:
        lines.append("正文含无原件支撑的既成事实承诺：" + "、".join(claims[:3]))
    return {"ok": all(flags.values()), "flags": flags, "lines": lines,
            "unsupported_numbers": nums, "placeholders": phs,
            "empty_sections": empties, "unsupported_claims": claims}


def section_status(pid: str) -> list[dict]:
    """按「当前正文」逐章重算交付状态，写章留痕只补充轮数，不当作放行依据。

    保护章节（报价表 / 投标函 / 承诺函 / 资质）由人工填写，不参与自动阻断。
    """
    try:
        p = store.get_project(pid)
        secs = p.get("sections") or store.list_sections(pid)
        tender = p.get("tender_text") or ""
        sources = project_sources(pid)
        saved = {r["section_id"]: r for r in store.list_section_quality(pid)}
        # B03：保护章节不参与 AI 改写，但字段 / 占位符 / 报价 / 签章 / 附件照常核验；
        # 短而完整的表单（投标函、授权书）不套普通正文的 60 字下限。
        form_conf = store.list_confirmations(pid)
        form_att = store.list_attachments(pid)
        out: list[dict] = []
        for sec in secs:
            if not isinstance(sec, dict):
                continue
            title = str(sec.get("title") or "")
            text = str(sec.get("text") or "")
            if sec.get("protected") or rules.is_protected(title):
                f = forms.check_section(pid, sec, confirmations=form_conf,
                                        attachments=form_att)
                lines = (f.get("gaps") or []) + (f.get("pending") or [])
                out.append({
                    "section_id": str(sec.get("id") or ""), "title": title,
                    "ok": f["ok"], "deliverable": f["ok"],
                    "lines": lines or ["保护表单已核验完成"],
                    "flags": {"form": f["ok"]},
                    "blocking_keys": [] if f["ok"] else ["form"],
                    "rounds": 0, "written": False, "stale": False,
                    "text_hash": text_hash(text), "updated_at": "",
                    "form": {"rule": f.get("rule") or "", "short_form": f.get("short_form"),
                             "gaps": f.get("gaps") or [], "pending": f.get("pending") or [],
                             "pending_targets": f.get("pending_targets") or [],
                             "content_hash": f.get("content_hash") or ""},
                })
                continue
            chk = assess_section(tender, {"title": title, "text": text}, sources)
            h = text_hash(text)
            rec = saved.get(str(sec.get("id") or "")) or {}
            stale = bool(rec and rec.get("text_hash") != h)
            keys = [k for k in BLOCK_KEYS if not chk["flags"].get(k, True)]
            out.append({
                "section_id": str(sec.get("id") or ""), "title": title,
                "ok": bool(chk.get("ok")), "deliverable": bool(chk.get("ok")),
                "lines": chk.get("lines") or [], "flags": chk.get("flags") or {},
                "blocking_keys": keys,
                "rounds": 0 if stale else int(rec.get("rounds") or 0),
                "written": bool(rec), "stale": stale, "text_hash": h,
                "updated_at": rec.get("updated_at", ""),
            })
        # 连一章正文都没有，不能算「已达标」：给一条整本级的硬阻断
        if not out:
            out.append({
                "section_id": "", "title": "（整本标书）", "ok": False,
                "deliverable": False, "lines": ["当前项目还没有可交付的正文章节"],
                "flags": {"empty": False}, "blocking_keys": ["empty"],
                "rounds": 0, "written": False, "stale": False,
                "text_hash": "", "updated_at": "",
            })
        return out
    except Exception as exc:
        # 返回空列表会让上层以为「逐章检查过了、零阻断」，必须显式报未完成
        return [{
            "section_id": "", "title": "（整本标书）", "ok": False,
            "deliverable": False, "unavailable": True,
            "lines": [f"章节状态计算失败（{type(exc).__name__}: {str(exc)[:100]}），本轮不能当作已检查"],
            "flags": {}, "blocking_keys": ["unavailable"],
            "rounds": 0, "written": False, "stale": False,
            "text_hash": "", "updated_at": "",
        }]


def section_blockers(pid: str) -> list[dict]:
    """章节级事实硬伤：导出只提醒不拦，但这类问题必须人工改好正文并复核。"""
    out: list[dict] = []
    for s in section_status(pid):
        if s["deliverable"]:
            continue
        # B03：保护表单由 forms.blocking 单独给「缺哪个字段 / 待复核哪个签章」的补齐入口，
        # 这里不再重复生成一条笼统的章节硬伤
        if s.get("form") is not None:
            continue
        labels = "、".join(BLOCK_LABELS.get(k, "检查未完成") for k in s["blocking_keys"]) or "自动核查未通过"
        out.append({
            "key": "block-" + s["section_id"],
            "title": "本章未达交付标准 · " + s["title"],
            "level": "高", "blocking": True,
            "section_id": s["section_id"],
            "detail": f"{labels}：{'；'.join(s['lines'][:3]) or '正文需人工核对'}",
            "action": ("到「写标书」重写本章或手工修好正文；"
                       + (f"定向重写已跑 {s['rounds']} 轮仍未达标。" if s["rounds"] else "")
                       + "知情导出仍可下载，但这项必须人工改好正文并复核"),
            "evidence": "backend/app/quality.py · section_status",
        })
    return out


def structure_issues(pid: str) -> list[dict]:
    """导出前核对章节完整性：大纲要求有、正文却没有的章节必须显式确认。"""
    try:
        p = store.get_project(pid)
        outline = [str(c.get("title") or "").strip()
                   for c in (p.get("outline") or []) if isinstance(c, dict)]
        outline = [t for t in outline if t]
        if not outline:
            return []
        titles = {str(s.get("title") or "").strip()
                  for s in (p.get("sections") or [])}
        missing = [t for t in outline if t not in titles]
        if not missing:
            return []
        return [{"key": "structure", "level": "高",
                 "title": "导出章节与招标文件大纲不一致",
                 "detail": (f"大纲 {len(outline)} 章，正文缺少 {len(missing)} 章："
                            + "、".join(missing[:3])),
                 "evidence": "project.outline"}]
    except Exception as exc:
        return [{"key": "structure_error", "level": "高", "unavailable": True,
                 "title": "章节结构检查未完成，不能当作已通过",
                 "detail": f"结构核对过程出错（{type(exc).__name__}: {str(exc)[:100]}），"
                           "本轮结果不可用；请重试或人工核对大纲与正文。",
                 "evidence": "backend/app/quality.py::structure_issues"}]


def export_issues(pid: str) -> list[dict]:
    """导出检查：生成质量缺陷（中）+ 章节完整性（可确认）+ 章节硬伤（重点提醒）。

    `export_quality` 只控制中等缺陷是否列出；章节未达交付标准属于重点提醒项，
    无论开关如何都返回 blocking 项，知情导出仍可下载并留下风险记录。
    """
    out: list[dict] = []
    # B03：保护表单的字段 / 占位符 / 报价 / 附件缺口硬阻断；签章等只能人工判的项列待复核
    try:
        out.extend(forms.blocking(pid))
    except Exception as exc:
        out.append({
            "key": "form_check_error", "title": "保护表单核验未完成",
            "level": "高", "blocking": True, "action": "重试体检",
            "detail": f"投标函 / 报价 / 授权 / 资质等保护章节没有核验完"
                      f"（{type(exc).__name__}: {str(exc)[:120]}）；本轮不能当作已通过。",
            "evidence": "backend/app/forms.py::blocking"})
    # B02：缺可核验事实的数值列成待补任务，附实体 / 单位 / 来源位置与附件入口；
    # 数值本身无依据仍由上面的章节级硬伤拦住，这里只负责「补什么、去哪补」
    try:
        from . import facts as facts_mod
        gap_items = facts_mod.gaps(pid, store.list_sections(pid), project_sources(pid))
        for g in gap_items[:12]:
            g["level"] = "中"
            g["blocking"] = False
            out.append(g)
    except Exception:
        pass
    # B01：每一条未落实的必需要求都进门禁，逐条给补齐入口；矩阵本身跑不完也不能算通过
    try:
        from . import reqmatrix
        out.extend(reqmatrix.blocking(pid))
    except Exception as exc:
        out.append({
            "key": "reqmatrix_error", "title": "逐条响应矩阵未完成",
            "level": "高", "blocking": True, "action": "重试体检",
            "detail": f"必需要求没有逐条核对完（{type(exc).__name__}: {str(exc)[:120]}）；"
                      "本轮不能当作已通过，请重试或到要求表人工复核。",
            "evidence": "backend/app/reqmatrix.py::blocking"})
    # R05：解析不完整且未人工核对的资料，其未识别范围不能算已核查
    project_sources(pid)
    pending = unchecked_sources(pid)
    if pending:
        out.append({
            "key": "source_pending", "title": "资料解析待核对，不能当作已核查",
            "level": "高", "blocking": True, "action": "补录或确认资料",
            "detail": ("有 " + str(len(pending)) + " 份资料存在未识别页或未核对范围："
                       + "、".join(pending[:6]) + ("等" if len(pending) > 6 else "")
                       + "。这些页上的要求与数值没有进入本轮检查；请到「我的资料」补录正文或人工确认后重新体检。"),
            "evidence": "backend/app/parsers.py::parse_bytes"})
    # 先判「有没有查完」：检查本身失败必须硬阻断，不能落成「没有质量问题」
    r = assess_project(pid)
    if r.get("status") == "unavailable":
        out.append({
            "key": "quality_unavailable", "title": "生成质量检查未完成",
            "level": "高", "blocking": True, "action": "重试体检",
            "detail": "本轮质量检查没有跑完（" + str(r.get("error") or "未知原因")
                      + "），不能当作已通过；请重试，或人工复核后再导出。"
                      + (("未检查范围：" + "、".join(x["label"] for x in r.get("unchecked") or []))
                         if r.get("unchecked") else ""),
            "evidence": "backend/app/quality.py::assess_project"})
    else:
        # 检查本身跑完了，但必需输入没读到：单独列硬阻断，不能被「没发现质量问题」盖过去
        for u in r.get("unchecked") or []:
            out.append({
                "key": "unchecked-" + u["key"], "title": "检查范围未完成 · " + u["label"],
                "level": "高", "blocking": True, "action": "重试体检",
                "detail": u["reason"] + "；这一项没查，本轮不能当作已检查通过。",
                "evidence": "backend/app/quality.py::assess_project"})
    if settings.export_quality():
        for d in r.get("defects") or []:
            if d["key"] in BLOCK_KEYS:
                continue       # 这三类改为按章列出硬阻断项，避免一条笼统提示
            out.append({"key": d["key"], "title": "生成质量 · " + d["title"],
                        "level": d["level"], "detail": d["detail"],
                        "blocking": False,
                        "evidence": "backend/app/quality.py"})
    for b in section_blockers(pid):
        out.append({"key": b["key"], "title": b["title"], "level": b["level"],
                    "detail": b["detail"], "blocking": True,
                    "section_id": b["section_id"], "action": b["action"],
                    "evidence": b["evidence"]})
    for s in structure_issues(pid):
        s["blocking"] = bool(s.get("unavailable"))  # 结构检查没跑完不能靠勾选放行
        out.append(s)
    return out
