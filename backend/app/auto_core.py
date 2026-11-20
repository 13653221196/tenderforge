"""基座能力自动编排：主链路到点自己跑，不需要人手点「运行」。

设计原则（与受控智能体一致）：
- 阶段元数据写在 settings.STAGE_META，每个阶段挂哪些能力由 data/agent_config.json 配置；
- 模型不决定调哪个能力、跑几轮；所有路径共用 settings 的准入判定；
- 只自动跑确定性、本地、不需要模型的能力（全部 43 项实测 0.4 秒内）；
- 结果回流：体检结论并入 issues，招标约束注入写作，导出前做门禁；
- 每步留痕（auto_runs）、可按开关关掉、同一份内容有缓存。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from . import config as appcfg
from . import core_bridge, delivery, quality, reqtable, settings, store

DATA = appcfg.DATA_DIR
DATA.mkdir(parents=True, exist_ok=True)

# 阶段说明（label / trigger / desc）；能力清单不再写死在这里
STAGES: list[dict] = [dict(s) for s in settings.STAGE_META]
STAGE_MAP = {s["key"]: s for s in STAGES}

# 哪些结果行要转成可定位、可改写的问题；没配置的能力只贡献指标，不制造问题
ISSUE_SPEC: dict[str, dict] = {
    "check_full": {"sections": ("issues",), "title": "title", "level": "level",
                   "detail": "detail", "evidence": "evidence", "limit": 4},
    "rejection": {"sections": ("items",), "title": "clause", "detail": "reason",
                  "evidence": "evidence", "level": "高", "limit": 3,
                  "where": lambda r: r.get("status") in ("missing", "pending", "manual")},
    "blind_text": {"sections": ("issues",), "title": "kind", "detail": "note",
                   "evidence": "excerpt", "level": "高", "limit": 3},
    # 报价表校验：基座只给问题明细、自己不判通过；明细必须逐条进门禁，
    # 否则「合价算错」只会被压缩成一句笼统的「检查未通过」，人不知道改哪行。
    "quote": {"sections": ("issues",), "title": "title", "detail": "detail",
              "evidence": "evidence", "level": "level", "limit": 6},
    "facts": {"sections": ("conflicts",), "title": "title", "level": "level",
              "detail": "detail", "evidence": "evidence", "limit": 3},
    "verify_citations": {"sections": ("unverified",), "title": "kind", "detail": "excerpt",
                         "evidence": "value", "level": "低", "limit": 2},
}
FINDING_LABELS = ("missing", "不一致", "待核实表述", "相似对", "long_paras", "未覆盖", "漏项")

# C02：缓存键带企业，避免不同企业同名项目 / 相同指纹互相命中
_CACHE: dict[tuple[str, str, str, str], tuple[float, dict]] = {}
_CACHE_TTL = 180.0
_CONSTRAINTS: dict[tuple[str, str, str], tuple[float, str]] = {}


# ---------------------------------------------------------------- 开关
def enabled() -> bool:
    """配置里的总开关；所有路径（含大脑自选基座）都认它。"""
    return settings.auto_enabled()


def set_enabled(value: bool) -> bool:
    v = bool(settings.save({"auto_enabled": bool(value)}).get("auto_enabled"))
    invalidate_cache()
    return v


def invalidate_cache() -> None:
    """配置改了立刻生效：阶段结果缓存与写作约束缓存一起失效。"""
    _CACHE.clear()
    _CONSTRAINTS.clear()


# ---------------------------------------------------------------- 内容指纹
def _fingerprint(pid: str) -> str:
    """确认令牌绑定内容、有效要求、资料、策略与模板版本，任一变化即失效。"""
    return delivery.content_fingerprint(pid)


# ---------------------------------------------------------------- 结果提取
def _rows(result: dict, names: tuple) -> list[dict]:
    out = []
    for sec in result.get("sections") or []:
        if sec.get("title") in names:
            out.extend(sec.get("rows") or [])
    return out


def _clean(value: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return s[:limit]


def _locate(pid: str, evidence: str, detail: str, title: str = "") -> tuple[str, str]:
    """把基座找到的证据定位到具体章节段落，找不到就不给锚点（只提示、不自动改写）。"""
    secs = store.list_sections(pid)
    probe = _clean(evidence, 160)
    paras = [(s, p.strip()) for s in secs
             for p in re.split(r"\n\s*\n", s.get("text") or "") if p.strip()]
    # 1) 证据本身就是原文片段
    for s in secs:
        key = probe[:12]
        if key and key in (s.get("text") or ""):
            return s["id"], probe[:80]
    for seg in re.findall(r"[\u4e00-\u9fa5]{6,16}", probe + " " + _clean(detail, 160))[:6]:
        for s in secs:
            if seg in (s.get("text") or ""):
                return s["id"], seg[:80]
    # 2) 规则给出的关键词清单（如「【待填】、TODO、XXX、待补充」）
    tokens = [t.strip() for t in re.split(r"[、,，;；/|]+", probe) if len(t.strip()) >= 2][:8]
    for tok in tokens:
        for s, para in paras:
            if tok in para:
                return s["id"], para[:80]
    # 3) 标题 / 说明里的强关键词命中某一段
    words = []
    for chunk in re.findall(r"[\u4e00-\u9fa5]{4,8}", (title + " " + detail)[:200]):
        for w in (chunk[:4], chunk[-4:]):
            if w not in words and w not in ("投标文件", "招标文件", "评标委员会", "本项目资料"):
                words.append(w)
    best = (0, "", "")
    for s, para in paras:
        hits = [w for w in words[:24] if w in para]
        if len(hits) > best[0]:
            best = (len(hits), s["id"], para[:80])
    if best[0] >= 2:
        return best[1], best[2]
    return "", ""


def _cap_status(r: dict) -> str:
    """每项基座检查的执行状态，五态不混用：

    pass 查过且通过 / fail 查过且不通过 / unavailable 运行出错导致没查成 /
    not_applicable 本项不适用 / missing_input 缺必要输入（要列缺失项，不能显示「不适用」）。
    """
    err = str(r.get("error") or "")
    text = (err + " " + str(r.get("summary") or "")).lower()
    if r.get("skipped"):
        if any(k in text for k in ("未上传", "缺少", "缺输入", "暂无", "没有", "未配置", "未填", "未找到")):
            return "missing_input"
        return "not_applicable"
    if err:
        return "unavailable"
    if not r.get("ok"):
        return "fail"
    return "pass"


def _issues_from(key: str, name: str, result: dict, pid: str) -> tuple[list[dict], int]:
    spec = ISSUE_SPEC.get(key)
    if not spec or not result.get("ok"):
        return [], 0
    rows = _rows(result, spec["sections"])
    where = spec.get("where")
    if where:
        rows = [r for r in rows if isinstance(r, dict) and where(r)]
    issues: list[dict] = []
    for i, row in enumerate(rows[:int(spec.get("limit", 3))]):
        if not isinstance(row, dict):
            continue
        title = _clean(row.get(spec["title"], ""), 90)
        if not title:
            continue
        detail = _clean(row.get(spec["detail"], ""), 260)
        evidence = _clean(row.get(spec["evidence"], ""), 160)
        level = _clean(row.get(spec.get("level", ""), ""), 4)
        if level not in ("高", "中", "低"):
            level = str(spec.get("level", "中")) if spec.get("level") in ("高", "中", "低") else "中"
        sid, anchor = _locate(pid, evidence, detail, title)
        issues.append({
            "code": "CORE-" + key.upper()[:12] + "-" + str(i + 1),
            "level": level,
            "title": "[" + name + "] " + title[:55],
            "detail": detail or title,
            "evidence": evidence[:80],
            "section_id": sid,
            "anchor": anchor,
            "source": "基座·" + name,
            "auto": True,
        })
    return issues, len(rows)


def _findings(key: str, result: dict) -> int:
    spec = ISSUE_SPEC.get(key)
    if spec:
        rows = _rows(result, spec["sections"])
        where = spec.get("where")
        if where:
            rows = [r for r in rows if isinstance(r, dict) and where(r)]
        return len(rows)
    total = 0
    for s in result.get("stats") or []:
        label = str(s.get("label", ""))
        value = s.get("value")
        if any(w in label for w in FINDING_LABELS) and isinstance(value, (int, float)):
            total += int(value)
    return total


# ---------------------------------------------------------------- 执行阶段
def run_stage(stage: str, pid: str, payload: dict | None = None, force: bool = False,
              record: bool = True) -> dict:
    cfg = STAGE_MAP.get(stage)
    if not cfg:
        return {"stage": stage, "ok": False, "error": "未知阶段", "auto": True, "ran": [], "issues": []}
    # 导出门禁是安全底线：即使「自动调用」总开关关了，下载前也要强制核对
    if stage != "export" and not enabled():
        return {"stage": stage, "label": cfg["label"], "ok": True, "auto": True,
                "enabled": False, "ran": [], "issues": [], "findings": 0, "high": 0,
                "summary": "自动编排已关闭"}
    fp = _fingerprint(pid)
    from . import scope
    ck = (scope.org(), pid, stage, fp)
    hit = _CACHE.get(ck)
    if not force and hit and time.time() - hit[0] < _CACHE_TTL:
        cached = dict(hit[1])
        cached["cached"] = True
        return cached

    t0 = time.time()
    ran: list[dict] = []
    issues: list[dict] = []
    total_findings = 0
    for key in settings.stage_caps(stage):
        cap = next((x for x in core_bridge.CAPABILITIES if x["key"] == key), None)
        name = cap["name"] if cap else key
        try:
            r = core_bridge.run(key, pid, payload or {})
        except Exception as exc:
            r = {"ok": False, "error": str(exc)[:180], "stats": [], "sections": []}
        new_issues, _ = _issues_from(key, name, r, pid)
        issues.extend(new_issues)
        n = _findings(key, r)
        total_findings += n
        status = _cap_status(r)
        # quote 基座把问题放在明细里、外层 ok 恒为 True；明细有高风险就不能记成 pass，
        # 否则「报价表算错了」在检查清单里反而显示成通过。
        if key == "quote" and any(x.get("level") == "高" for x in new_issues):
            status = "fail"
            r = {**r, "ok": False}
        stats = [{"label": str(x.get("label")), "value": x.get("value")}
                 for x in (r.get("stats") or [])[:8]]
        download = next((str(x["value"]) for x in stats if x["label"] == "download"), "")
        ran.append({
            "key": key, "name": name, "ok": bool(r.get("ok")),
            "status": status, "scope": str(cap.get("desc") or cap.get("label") or "")[:120] if cap else "",
            "required": stage == "export" and key in settings.REQUIRED_EXPORT_CAPS,
            "findings": n, "summary": str(r.get("summary") or "")[:120],
            "stats": stats, "download": download,
            "skipped": bool(r.get("skipped")),
            "error": str(r.get("error") or "")[:160],
        })
    # 自动问题去重并限量，避免淹没原有 7 类规则结论
    seen: set[str] = set()
    uniq = []
    for it in issues:
        k = re.sub(r"\W", "", it["title"] + it["evidence"])[:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    issues = uniq[:12]
    high = sum(1 for i in issues if i["level"] == "高")
    required_bad = [x for x in ran if x.get("required")
                    and x["status"] in ("unavailable", "fail", "missing_input")]
    unchecked = [{"key": x["key"], "name": x["name"], "status": x["status"],
                  "scope": x["scope"], "error": x["error"]}
                 for x in ran if x["status"] in ("unavailable", "missing_input")]
    status = ("unavailable" if any(x["status"] in ("unavailable", "missing_input")
                                   for x in required_bad) else
              "fail" if required_bad else "pass")
    summary = ("自动调用 " + str(len(ran)) + " 项基座能力，发现 " + str(total_findings) +
               " 条线索，其中 " + str(high) + " 条高风险")
    if unchecked:
        summary += "；未检查范围 " + str(len(unchecked)) + " 项（" + "、".join(
            x["name"] for x in unchecked[:3]) + "）"
    if required_bad:
        summary += "；必需检查未完成，本阶段结果不能作为放行依据"
    report = {
        "stage": stage, "label": cfg["label"], "trigger": cfg["trigger"],
        "desc": cfg["desc"], "ok": not required_bad, "auto": True, "enabled": True,
        "status": status, "complete": not unchecked, "unchecked": unchecked,
        "required": [x["key"] for x in ran if x.get("required")],
        "count": len(ran), "ran": ran, "issues": issues,
        "findings": total_findings, "high": high,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "cached": False, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
    }
    _CACHE[ck] = (time.time(), report)
    if record:
        try:
            slim = {k: report[k] for k in ("stage", "label", "count", "ran", "issues",
                                           "findings", "high", "elapsed_ms", "at", "summary")}
            store.save_auto_run(pid, stage, report["summary"], slim)
        except Exception:
            pass
    return report


def issues_for_check(pid: str) -> list[dict]:
    """体检时自动并入的基座问题；失败不影响原有规则体检。"""
    try:
        return run_stage("check", pid)["issues"]
    except Exception:
        return []


def constraints(pid: str, auto_run: bool = True, chapter: str = "",
                outline: bool = False) -> str:
    """从招标要求表生成写作约束（人工修订优先），注入大纲 / 逐章生成 / 改写。

    B01：业务侧不再有「前 N 条」固定截断。
    - outline=True：分批概览 + 明确未处理范围，避免把几百条一次塞进提示词；
    - chapter：逐章写作按相关度分配本批；
    - 都不传：完整清单（页面展示分页另做）。
    """
    try:
        if outline and reqtable.constraint_items(pid):
            return reqtable.outline_constraints(pid)
    except Exception:
        pass
    if chapter:
        try:
            return reqtable.constraints_for_chapter(pid, chapter)
        except Exception:
            return ""
    from . import scope
    ck = (scope.org(), pid, _fingerprint(pid))
    hit = _CONSTRAINTS.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    # 表里有启用行就用它：人工修订 > 自动抽取，抽错一条能直接在页面上改回来
    try:
        manual = reqtable.constraints_text(pid)
    except Exception:
        manual = ""
    if manual:
        _CONSTRAINTS[ck] = (time.time(), manual)
        return manual
    last = store.last_auto_run(pid, "tender")
    if (not last.get("data") or not last["data"].get("ran")) and auto_run:
        try:
            run_stage("tender", pid)
            last = store.last_auto_run(pid, "tender")
        except Exception:
            return ""
    data = last.get("data") or {}
    by_key = {x["key"]: x for x in data.get("ran", [])}
    lines: list[str] = []
    # 用一次轻量调用拿到原始行（留痕里只存了摘要）
    for key, label, field in (
        ("requirements", "必须逐条响应的硬性要求", "text"),
        ("rejection", "会导致废标的条款", "clause"),
        ("scoring", "评分项", "item"),
    ):
        if key not in by_key:
            continue
        try:
            r = core_bridge.run(key, pid, {})
        except Exception:
            continue
        # 业务处理不截断：这一路只是基座兜底，完整清单以要求表为准
        rows = _rows(r, ("items", "rows", "basic", "scoring"))
        vals = []
        for row in rows:
            if isinstance(row, dict):
                v = _clean(row.get(field) or row.get("title") or row.get("值") or "", 160)
                if v and v not in vals:
                    vals.append(v)
        if vals:
            lines.append(f"【{label}·共 {len(vals)} 条】" + "；".join(vals))
    if not lines:
        _CONSTRAINTS[ck] = (time.time(), "")
        return ""
    text = ("\n\n【基座自动抽取的招标约束，写作必须逐条响应，不得与之冲突；"
            "完整清单见要求表与逐条响应矩阵】\n" + "\n".join(lines))
    _CONSTRAINTS[ck] = (time.time(), text)
    return text


def status(pid: str = "") -> dict:
    stages = []
    for s in STAGES:
        last = store.last_auto_run(pid, s["key"]) if pid else {}
        keys = settings.stage_caps(s["key"])
        caps = [{"key": k, "name": next((c["name"] for c in core_bridge.CAPABILITIES
                                         if c["key"] == k), k)} for k in keys]
        stages.append({**s, "caps": keys, "caps_detail": caps,
                       "enabled": settings.stage_enabled(s["key"]),
                       "last": {"at": last.get("created_at", ""),
                                "summary": last.get("summary", ""),
                                "findings": (last.get("data") or {}).get("findings", 0)}})
    recent = [{"stage": r["stage"], "at": r["created_at"], "summary": r["summary"]}
              for r in store.list_auto_runs(pid, 10)] if pid else []
    cfg = settings.load()
    return {"enabled": settings.auto_enabled(), "stages": stages, "recent": recent,
            "total_caps": len(core_bridge.CAPABILITIES),
            "disabled_caps": cfg.get("disabled_caps", []),
            "brain_auto_allow": cfg.get("brain_auto_allow", "*"),
            "allow_model_caps_auto": cfg.get("allow_model_caps_auto", False)}


# ---------------- 导出检查：不符合项只作提醒，知情确认后照常导出 ----------------
# 令牌一次性、绑定项目内容指纹：确认之后正文又被改动，令牌立即失效。
# 落在企业库（store.token_*）而不是进程内存：多 worker / 重启后仍能验同一张令牌。

class _TokenDict(MutableMapping):
    """保留原来的 dict 写法，读写改走数据库；pop 是原子的一次性消费。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __getitem__(self, key):
        value = store.token_get(self.kind, key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        store.token_put(self.kind, key, value)

    def __delitem__(self, key):
        if store.token_pop(self.kind, key) is None:
            raise KeyError(key)

    def __iter__(self):
        return iter(store.token_keys(self.kind))

    def __len__(self):
        return len(store.token_keys(self.kind))

    def get(self, key, default=None):
        value = store.token_get(self.kind, key)
        return default if value is None else value

    def pop(self, key, default=None):
        value = store.token_pop(self.kind, key)
        return default if value is None else value


_EXPORT_TOKENS = _TokenDict("export")
_EXPORT_TOKEN_TTL = 300


# ---------------- 导出提醒聚合：上百条清单按类别收拢，一条都不删 ----------------
# 顺序即重要程度：前端按这个顺序展示分组，「先看哪几条」也按它轮转取样。
GATE_GROUP_LABELS: tuple[tuple[str, str], ...] = (
    ("incomplete", "检查没跑完"),
    ("compliance", "合规与雷同"),
    ("sections", "章节正文待修改"),
    ("requirements", "必需要求待落实"),
    ("forms", "保护表单与附件"),
    ("sources", "资料待核对"),
    ("facts", "数值与证据待补"),
    ("health", "自动体检提示"),
    ("other", "其他提醒"),
)
_GATE_GROUP_WORDS: dict[str, tuple[str, ...]] = {
    "incomplete": ("检查未完成", "没跑完", "没有跑完", "核验未完成", "矩阵未完成",
                   "体检未完成", "未完成，需人工", "不能当作已通过"),
    "compliance": ("同一招标文件", "多企业", "串标", "雷同", "跨企业", "合规红线",
                   "同一标段"),
    "sections": ("未达交付标准", "空章", "占位符", "章节级事实", "本章未", "空白章节"),
    "requirements": ("必需要求未逐条落实", "逐条响应矩阵", "要求未落实", "必需要求"),
    "forms": ("保护表单", "投标函", "报价", "授权", "资质", "签章", "附件缺口"),
    "sources": ("资料解析待核对", "未识别页", "未核对范围", "补录或确认资料"),
    "facts": ("待补", "数值", "出处", "引用溯源", "证据"),
    "health": ("自动体检", "模拟评标", "事实一致性"),
}
_GATE_ERROR_KEYS = frozenset({
    "quality_error", "health_error", "form_check_error", "reqmatrix_error",
    "health-incomplete",
})


def _gate_group(issue: dict) -> str:
    """只做归类，不改内容。先看标题与动作，再看详情，避免长说明带偏分类。"""
    key = str(issue.get("key") or "")
    if key.startswith("required-") or key in _GATE_ERROR_KEYS:
        return "incomplete"
    head = " ".join(str(issue.get(f) or "") for f in ("title", "action"))
    detail = str(issue.get("detail") or "")
    for scope in (head, detail):
        if not scope:
            continue
        for gkey, _label in GATE_GROUP_LABELS:
            if any(w in scope for w in _GATE_GROUP_WORDS.get(gkey, ())):
                return gkey
    return "other"


BUSINESS_LEVELS = ("硬性否决", "硬性扣分", "加分项", "参考项")
# 这些分组的问题会直接影响评分表或资格响应，不只是「看看就好」
_SCORE_GROUPS = ("requirements", "forms")


def business_level(issue: dict, group: str = "") -> str:
    """把「高/中」翻译成投标业务的四种后果，只是提示性归类，不是评审结论。

    加分项刻意恒不产生：系统目前只查废标风险与扣分项，不自动判定加分项，
    硬凑一类会让用户以为「加分机会也查过了」。
    """
    if issue.get("blocking"):
        return "硬性否决"
    if str(issue.get("level") or "") == "高" or group in _SCORE_GROUPS:
        return "硬性扣分"
    return "参考项"


def group_export_issues(issues: list[dict]) -> dict:
    """一句结论 + 几条重点 + 可展开分组；`issues` 全量原样保留，供逐条确认与留痕。

    真实招标文件会一次给出上百条提醒，平铺会把「最该先看的那几条」淹掉。
    这里只增加摘要字段，不删、不改、不重新判定任何一条风险等级；
    business_level 只加在分组用的拷贝上，原始 issues 保持逐条确认口径。
    """
    buckets: dict[str, list[dict]] = {k: [] for k, _ in GATE_GROUP_LABELS}
    for it in issues or []:
        gkey = _gate_group(it)
        item = dict(it)
        item["business_level"] = business_level(it, gkey)
        buckets[gkey].append(item)
    groups = []
    for gkey, label in GATE_GROUP_LABELS:
        items = buckets[gkey]
        if not items:
            continue
        groups.append({
            "key": gkey, "label": label, "count": len(items),
            "high": sum(1 for x in items
                        if x.get("level") == "高" or x.get("blocking")),
            "items": items,  # 全量：界面默认折叠，用户随时能展开逐条看
        })
    # 重点项按组轮转、每组最多 2 条，保证用户看到的不是 6 条同类要求
    top: list[dict] = []
    for gr in groups:
        ordered = sorted(gr["items"], key=lambda x: 0 if (
            x.get("level") == "高" or x.get("blocking")) else 1)
        for x in ordered[:2]:
            top.append({**x, "group_label": gr["label"]})
        if len(top) >= 6:
            break
    top = top[:6]
    total = len(issues or [])
    high = sum(g["high"] for g in groups)
    summary = f"下载前的检查有 {total} 项提醒"
    if high:
        summary += f"，其中 {high} 项需重点核对"
    if total >= 5 and groups:
        biggest = max(groups, key=lambda g: g["count"])
        summary += f"；最多的是「{biggest['label']}」{biggest['count']} 项"
    impact = {k: 0 for k in BUSINESS_LEVELS}
    for gr in groups:
        for x in gr["items"]:
            k = x.get("business_level")
            if k in impact:
                impact[k] += 1
    return {"groups": groups, "top": top, "summary": summary + "。",
            "impact": impact,
            "impact_note": ("按评审后果归类，仅作提示、不等于评标结论；"
                            "系统只查废标与扣分风险，不自动判定加分项")}


def export_gate(pid: str) -> dict:
    """强制重跑导出门禁（不吃 180 秒缓存），返回本次内容与待确认的高风险问题。"""
    rep = run_stage("export", pid, force=True)
    issues = [{
        "key": f"e{i}",
        "title": str(it.get("title") or "")[:120],
        "level": str(it.get("level") or "中"),
        "detail": str(it.get("detail") or it.get("evidence") or "")[:220],
    } for i, it in enumerate(rep.get("issues") or [])]
    # 必需检查没跑完（出错 / 缺输入 / 判为失败）：不管配置怎么关都照实列出来，
    # 作为重点提醒；知情导出仍可下载，但风险会随产物登记，不写成「已通过」。
    for i, x in enumerate(rep.get("ran") or []):
        if not x.get("required") or x.get("status") not in ("unavailable", "fail", "missing_input"):
            continue
        st = x["status"]
        label = ("运行出错，没查完" if st == "unavailable" else
                 "缺少必需输入，不能算不适用或已通过" if st == "missing_input" else
                 "检查未通过")
        issues.append({
            "key": f"required-{x['key']}-{i}", "level": "高", "blocking": True,
            "title": "必需检查未完成 · " + str(x.get("name") or x["key"]),
            "detail": f"{label}；检查范围：{x.get('scope') or '整本标书'}"
                      + (f"；错误：{x.get('error')}" if x.get("error") else "")
                      + "。请补齐输入并重新体检；知情导出仍可下载，但这项必须人工复核。",
            "action": "重试体检", "status": st,
        })
    # 生成质量缺陷（中）、章节完整性与「本章未达交付标准」的重点提醒项同列。
    # 开关在设置页（settings.export_quality），关掉后只不列中等缺陷；
    # 未达标章节始终列为 blocking 提醒，知情导出仍可下载并留下风险记录。
    try:
        for i, q in enumerate(quality.export_issues(pid)):
            issues.append({"key": f"q{i}", "title": q["title"],
                           "level": q["level"], "detail": q["detail"],
                           "blocking": bool(q.get("blocking")),
                           "section_id": q.get("section_id", ""),
                           "action": q.get("action", "")})
    except Exception as exc:
        # 不能吞：检查没跑完就当作「没问题」，会把未核过的标书放出去
        issues.append({
            "key": "quality_error", "level": "高", "blocking": True, "action": "重试体检",
            "title": "质量检查未完成，需人工复核",
            "detail": f"检查过程出错（{type(exc).__name__}: {str(exc)[:120]}），"
                      "本轮结果不可用；请重试或人工复核。"})
    # 整本自动体检：雷同 / 模拟评标 / 事实一致性 / 引用溯源 / 多企业合规边界。
    # 与生成任务末尾共用同一份按内容指纹缓存的报告，不另算一套结论。
    health_metrics: dict = {}
    try:
        from . import health
        # 口径数字与本次门禁引用的体检报告同源：不再另外查一次库，
        # 否则导出阶段刚写入的响应记录会让同一时刻冒出两套条数。
        health_metrics = dict((health.latest(pid) or {}).get("metrics") or {})
        issues.extend(health.export_issues(pid))
    except Exception as exc:
        issues.append({
            "key": "health_error", "level": "高", "blocking": True,
            "action": "重试体检",
            "title": "整本自动体检未完成，需人工复核",
            "detail": f"体检没有跑完（{type(exc).__name__}: {str(exc)[:120]}）；"
                      "本轮结果不可用，知情导出也需人工逐项复核。"})
    # 证照有效期：只查填了有效期的条目，纯日期判定；过期列高风险重点提醒。
    # 沿用「提醒 + 知情令牌」契约，不在这里改成禁止下载。
    try:
        from . import certificates
        issues.extend(certificates.export_issues(pid))
    except Exception as exc:
        issues.append({
            "key": "certificate_error", "level": "高", "blocking": True,
            "action": "重试体检", "title": "证照有效期未完成核对",
            "detail": f"有效期检查没跑完（{type(exc).__name__}: {str(exc)[:120]}）；"
                      "本轮不能当作已核对，请人工确认资质证照在开标日仍有效。"})
    # 货物清单：只提醒漏数量、限价含糊等可核对的点，不编造预算列。
    try:
        from . import goods_list
        issues.extend(goods_list.export_issues(pid))
    except Exception as exc:
        issues.append({
            "key": "goods_error", "level": "高", "blocking": True,
            "action": "重试体检", "title": "货物清单未完成核对",
            "detail": f"清单检查没跑完（{type(exc).__name__}: {str(exc)[:120]}）；"
                      "本轮不能当作已核对，请人工确认清单数量与限价条款。"})
    # 报价一致性：限价算术比对 + 多张报价表 / 总价声明交叉核对。
    # 结论只说算术事实，不代替评审口径判断含税价、单价限价或角分剔除。
    try:
        from . import quote_gate
        issues.extend(quote_gate.export_issues(pid))
    except Exception as exc:
        issues.append({
            "key": "quote_gate_error", "level": "高", "blocking": True,
            "action": "重试体检", "title": "报价一致性未完成核对",
            "detail": f"限价与总价检查没跑完（{type(exc).__name__}: {str(exc)[:120]}）；"
                      "本轮不能当作已核对，请人工确认投标总价与最高限价。"})
    blockers = [x for x in issues if x.get("blocking")]
    high = [x for x in issues if x["level"] == "高"]
    # 同一时刻四处数字各说各话：这里给出带名字的整本口径，前端与文案一律
    # 引用它们，不再把「未达标章数 / 必需要求条数 / 提醒条目数」混成一个数。
    sections_bad = int(health_metrics.get("undeliverable_sections") or 0)
    matrix_required = int(health_metrics.get("requirements") or 0)
    matrix_rows = int(health_metrics.get("matrix_rows") or 0)
    return {
        "pid": pid, "fingerprint": _fingerprint(pid),
        "count": int(rep.get("count") or 0), "findings": int(rep.get("findings") or 0),
        "high": len(high), "issues": issues,
        "blocked": bool(blockers), "blockers": blockers,
        "requires_confirm": bool(high) or bool(blockers), "cached": False,
        "sections_undeliverable": sections_bad,
        "matrix_required": matrix_required, "matrix_rows": matrix_rows,
        "issues_total": len(issues), "blockers_total": len(blockers),
        "counts_note": "章数 / 条数 / 提醒条目数是三种口径，不能互相代替",
        **group_export_issues(issues),
    }


def issue_export_token(pid: str, fingerprint: str, confirmed: list[str]) -> dict:
    """严格逐条确认路径：发一次性令牌；章节硬伤不走这条路径，改用知情导出。"""
    gate = export_gate(pid)
    if gate["fingerprint"] != fingerprint:
        return {"ok": False, "error": "标书内容在确认期间发生变化，请重新体检后再确认"}
    if gate.get("blocked"):
        return {"ok": False,
                "error": "严格确认路径不处理章节级硬伤，请点「仍然导出」按知情方式下载",
                "blocked": True, "blockers": gate.get("blockers") or []}
    required = {x["key"] for x in gate["issues"] if x["level"] == "高"}
    missing = required - set(confirmed or [])
    if missing:
        return {"ok": False, "error": f"还有 {len(missing)} 条高风险问题未逐条确认"}
    token = secrets.token_urlsafe(18)
    _EXPORT_TOKENS[token] = (pid, gate["fingerprint"], time.time() + _EXPORT_TOKEN_TTL)
    return {"ok": True, "token": token, "expires_in": _EXPORT_TOKEN_TTL}


_ACK_TOKENS = _TokenDict("ack")


def issue_ack_token(pid: str, fingerprint: str) -> dict:
    """知情导出：用户看过完整提醒后换令牌，不因检查不符合而拒绝导出。

    严格模式的 issue_export_token 保持原样，供需要逐条确认的场景使用。
    """
    gate = export_gate(pid)
    if gate["fingerprint"] != fingerprint:
        return {"ok": False, "error": "标书内容在确认期间发生变化，请重新体检后再导出"}
    token = secrets.token_urlsafe(18)
    ack = {"mode": "acknowledged", "issues": len(gate["issues"]),
           "high": gate["high"], "blockers": len(gate["blockers"]),
           "fingerprint": gate["fingerprint"]}
    _ACK_TOKENS[token] = (pid, gate["fingerprint"],
                          time.time() + _EXPORT_TOKEN_TTL, ack)
    return {"ok": True, "token": token, "expires_in": _EXPORT_TOKEN_TTL,
            "acknowledged": ack,
            "warning": (f"已知悉 {len(gate['issues'])} 项检查提醒"
                        f"（高风险 {gate['high']} 项），本次照常导出；"
                        "文件仍需人工复核后再提交")}


def consume_ack_token(pid: str, fingerprint: str, token: str) -> dict | None:
    """一次性核对知情导出令牌；返回风险摘要，无效则 None。"""
    item = _ACK_TOKENS.get(token or "")
    if not item or item[0] != pid or item[1] != fingerprint or item[2] < time.time():
        return None
    _ACK_TOKENS.pop(token, None)
    return dict(item[3])


def verify_export_token(pid: str, fingerprint: str, token: str) -> bool:
    """一次性：核对项目 / 内容指纹 / 有效期，用完即焚。"""
    item = _EXPORT_TOKENS.pop(token or "", None)
    if not item:
        return False
    return item[0] == pid and item[1] == fingerprint and item[2] >= time.time()


def as_card(report: dict) -> dict:
    """对话里的「自动基座」卡片；没有实际运行就不出卡片。"""
    if not report or not report.get("ran"):
        return {}
    return {"type": "auto", "title": "自动基座 · " + str(report.get("label", "")),
            "stage": report.get("stage", ""),
            "summary": report.get("summary", ""),
            "count": report.get("count", 0),
            "findings": report.get("findings", 0),
            "high": report.get("high", 0),
            "elapsed_ms": report.get("elapsed_ms", 0),
            "cached": report.get("cached", False),
            "items": [{"key": x["key"], "name": x["name"], "ok": x["ok"],
                       "findings": x["findings"], "summary": x["summary"],
                       "download": x.get("download", ""), "skipped": x.get("skipped", False)}
                      for x in report.get("ran", [])]}
