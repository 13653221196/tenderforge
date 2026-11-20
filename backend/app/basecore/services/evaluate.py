"""模拟评标：按评分办法给自家标书打分，指出失分点。

两条路径：
1. 规则估算（无 AI 也能用）：把评分项拆成关键词，看正文命中率与是否给出具体数字，
   命中越多得分越高，只做粗估并明确标注；
2. AI 复核（配置模型后）：把评分项表 + 各章摘要交给模型，要求逐项给出
   "应得分 / 依据原文 / 失分原因 / 补分建议"，只允许引用给定正文，不许想象。

输出统一结构，前端按"得分条 + 失分清单"渲染。
"""
from __future__ import annotations

import asyncio
import json
import re

from ..config import LLMConfig
from .llm import LLMError, chat, parse_json

SYSTEM = (
    "你是评标委员会的商务/技术评委。只依据给定投标文件内容打分，不得假设文件里有但实际没写的内容。"
    "评分必须可追溯到原文：给出依据片段，找不到就判低分并说明缺什么。只输出 JSON。"
)

TEMPLATE = """请按下面的评分办法给这份投标文件打分。每个评分项要用两把尺子各打一次分：

评分办法（每项含满分）：
{items}

投标文件各章摘要：
{digest}

输出 JSON：
{{
  "items": [
    {{"index": 1, "score": 0, "score_strict": 0, "reason": "为什么给这个分", "evidence": "引用原文片段（30 字内，没有就留空）", "improve": "怎么补能多拿分"}}
  ],
  "summary": "一句话总体判断，指出最该补的两处"
}}

两把尺子的定义：
- score（宽松尺）：只要摘要里有相关内容就按质量正常给分；
- score_strict（苛刻尺）：只有内容包含具体数字、投标人名称、明确的响应/偏离表述时才给分，
  泛泛表态、模板话术一律按 0.5 折扣以下处理。

要求：
- score 与 score_strict 都不得超过该项满分，可给小数（0.5 分档）；
- 依据必须来自上面的摘要，不得编造项目业绩、证书编号、人员姓名；
- 全文没有对应内容就按 0 分处理，并在 improve 里写清楚需要补什么材料。
"""

# 两把尺子的分歧达到阈值时，取较小值并标记人工复核。
REVIEW_GAP_RATIO = 0.15  # 分歧 ≥ max(2, 满分×15%) 视为两路结论不可靠
REVIEW_GAP_MIN = 2.0

STOP = re.compile(r"[的了和与及或在为对按其这那有应能可需将已并等一二三四五六七八九十]")


def _keywords(text: str) -> list[str]:
    """从评分项描述里抽关键词：按中文分词取 2~4 字片段，去停用字。"""
    raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", text or "")
    words: list[str] = []
    for chunk in raw.split():
        if re.fullmatch(r"[A-Za-z0-9]+", chunk):
            if len(chunk) >= 2:
                words.append(chunk)
            continue
        clean = STOP.sub("", chunk)
        for size in (4, 3, 2):
            for i in range(0, max(0, len(clean) - size + 1), size):
                piece = clean[i : i + size]
                if len(piece) == size:
                    words.append(piece)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:12]


def _full_score(item: dict) -> float:
    raw = str(item.get("score") or "")
    match = re.search(r"(\d+(?:\.\d+)?)", raw)
    return float(match.group(1)) if match else 0.0


def _digest(sections: list[dict], limit: int = 14000) -> str:
    parts: list[str] = []
    used = 0
    for sec in sections:
        content = (sec.get("content") or "").strip()
        if not content:
            continue
        piece = f"【{sec.get('title')}】\n{content[:600]}"
        if used + len(piece) > limit:
            break
        parts.append(piece)
        used += len(piece)
    return "\n\n".join(parts)


def estimate(scoring: list[dict], sections: list[dict], tender_text: str = "") -> dict:
    """规则估算：关键词覆盖率 × 是否有量化表述，做保守给分。"""
    corpus = "\n".join((s.get("content") or "") for s in sections)
    items: list[dict] = []
    for idx, row in enumerate(scoring, 1):
        full = _full_score(row)
        if full <= 0:
            continue
        words = _keywords(f"{row.get('item') or ''} {row.get('rule') or ''}")
        hit = [w for w in words if w in corpus]
        coverage = len(hit) / len(words) if words else 0.0
        # 有数字、有"承诺/响应/满足"这类落实性表述再加一点
        concrete = bool(re.search(r"(承诺|响应|满足|不低于|不少于|达到|保证)", corpus))
        ratio = min(1.0, coverage * 0.75 + (0.15 if concrete else 0) + (0.1 if len(corpus) > 3000 else 0))
        score = round(full * ratio * 2) / 2
        items.append(
            {
                "index": idx,
                "item": row.get("item") or f"评分项 {idx}",
                "rule": row.get("rule") or "",
                "full": full,
                "score": score,
                "level": "得分" if ratio >= 0.8 else ("部分" if ratio >= 0.45 else "失分"),
                "reason": f"关键词命中 {len(hit)}/{len(words)}（{'、'.join(hit[:6]) or '无'}），按覆盖率估算",
                "evidence": "",
                "improve": "补写与该项对应的具体做法、指标与证明材料" if ratio < 0.8 else "",
            }
        )
    total_full = round(sum(i["full"] for i in items), 1)
    total = round(sum(i["score"] for i in items), 1)
    return {
        "mode": "rule",
        "items": items,
        "total": total,
        "full": total_full,
        "rate": round(total / total_full * 100, 1) if total_full else 0,
        "summary": "未配置 AI，以下为关键词覆盖率粗估，仅用于定位缺口，不能当作真实得分。",
        "notice": "配置模型后可获得逐项打分与补分建议。",
    }


def _merge(scoring: list[dict], data: dict) -> list[dict]:
    rows = data.get("items") if isinstance(data.get("items"), list) else []
    by_index = {}
    for row in rows:
        if isinstance(row, dict) and row.get("index") is not None:
            by_index[int(row["index"])] = row
    out: list[dict] = []
    for idx, item in enumerate(scoring, 1):
        full = _full_score(item)
        if full <= 0:
            continue
        got = by_index.get(idx) or {}
        def _num(key: str) -> float:
            try:
                return float(got.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0
        # 双路评分：宽松尺与苛刻尺各自给分，分歧大时取小值并标人工复核
        score_a = _num("score")
        score_b = _num("score_strict") if got.get("score_strict") is not None else score_a
        score_a = max(0.0, min(full, round(score_a * 2) / 2))
        score_b = max(0.0, min(full, round(score_b * 2) / 2))
        gap = abs(score_a - score_b)
        review = gap >= max(REVIEW_GAP_MIN, full * REVIEW_GAP_RATIO)
        score = min(score_a, score_b) if review else round((score_a + score_b) / 2 * 2) / 2
        rate = score / full if full else 0
        base_reason = str(got.get("reason") or "")[:200]
        if review:
            base_reason = f"双路评分分歧 {gap:.1f} 分（宽松 {score_a:g} / 苛刻 {score_b:g}），已取较小值并标记复核。" + base_reason
        out.append(
            {
                "index": idx,
                "item": item.get("item") or f"评分项 {idx}",
                "rule": item.get("rule") or "",
                "full": full,
                "score": score,
                "score_min": min(score_a, score_b),
                "score_max": max(score_a, score_b),
                "review": review,
                "level": "得分" if rate >= 0.8 else ("部分" if rate >= 0.45 else "失分"),
                "reason": base_reason,
                "evidence": str(got.get("evidence") or "")[:120],
                "improve": str(got.get("improve") or "")[:200],
            }
        )
    return out


async def evaluate(
    cfg: LLMConfig,
    *,
    scoring: list[dict],
    sections: list[dict],
    tender_text: str = "",
    only: list[int] | None = None,
    prior_items: list[dict] | None = None,
) -> dict:
    """only: 只重跑这些评分项（按 scoring 顺序的 1-based 序号）；
    prior_items: 之前的结果项，重跑时其余项沿用旧结论。"""
    ok, why = scoring_valid(scoring)
    if not ok:
        raise ValueError(why)
    only_set = {int(i) for i in only or [] if str(i).strip()}
    run_scoring = [
        row for idx, row in enumerate(scoring, 1)
        if not only_set or idx in only_set
    ] if only_set else scoring
    if not scoring:
        return {
            "mode": "none",
            "items": [],
            "total": 0,
            "full": 0,
            "rate": 0,
            "summary": "还没有评分办法：先跑招标解读，或在「模拟评标」里粘贴评分表。",
            "notice": "",
        }
    if not any((s.get("content") or "").strip() for s in sections):
        return {
            "mode": "none",
            "items": [],
            "total": 0,
            "full": 0,
            "rate": 0,
            "summary": "还没有生成正文，无法评标。",
            "notice": "",
        }

    base = estimate(run_scoring, sections, tender_text)
    if not cfg.ready:
        result = _finalize(base, run_scoring, prior_items, only_set, scoring)
        return result

    items_text = "\n".join(
        f"{i}. {row.get('item')}（满分 {row.get('score') or '未标注'}）：{row.get('rule') or ''}"
        for i, row in enumerate([r for r in run_scoring if _full_score(r) > 0], 1)
    )
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": TEMPLATE.format(items=items_text, digest=_digest(sections)),
        },
    ]
    try:
        raw = await asyncio.wait_for(chat(cfg, messages, json_mode=True, max_tokens=3000, task="evaluate"), timeout=180)
        data = parse_json(raw)
        merged = _merge(run_scoring, data)
        if not merged:
            return _finalize(base, run_scoring, prior_items, only_set, scoring)
        # 重跑场景：把子集序号映射回原始序号
        if only_set:
            orig_index = [idx for idx, row in enumerate(scoring, 1) if idx in only_set]
            for pos, row in enumerate(merged):
                if pos < len(orig_index):
                    row["index"] = orig_index[pos]
        return _finalize({"mode": "ai", "items": merged}, run_scoring, prior_items, only_set, scoring,
                         extra={"summary": str(data.get("summary") or "")[:300],
                                "estimate": {"total": base["total"], "rate": base["rate"]}})
    except (LLMError, asyncio.TimeoutError, json.JSONDecodeError):
        base["notice"] = "AI 复核失败，已退回关键词粗估。"
        return _finalize(base, run_scoring, prior_items, only_set, scoring)


def _finalize(result: dict, run_scoring, prior_items, only_set, scoring, extra: dict | None = None) -> dict:
    """合并重跑项与沿用项，算总分。"""
    items = list(result.get("items") or [])
    if only_set and prior_items:
        prior_by_index = {p.get("index"): p for p in prior_items}
        for row in items:
            prior_by_index[row.get("index")] = row
        items = [
            prior_by_index[idx]
            for idx, row in enumerate(scoring, 1)
            if _full_score(row) > 0 and idx in prior_by_index
        ]
    total_full = round(sum(i["full"] for i in items), 1)
    total = round(sum(i["score"] for i in items), 1)
    out = {
        "mode": result.get("mode"),
        "items": items,
        "total": total,
        "full": total_full,
        "rate": round(total / total_full * 100, 1) if total_full else 0,
        "rerun": sorted(only_set) if only_set else [],
        "review_items": len([i for i in items if i.get("review")]),
    }
    if extra:
        out.update(extra)
    else:
        out.setdefault("summary", result.get("summary") or "")
        out.setdefault("notice", result.get("notice") or "")
    return out


# ---------------- 手输评分表 ----------------
SCORE_NUM = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*分")


def parse_scoring(text: str) -> list[dict]:
    """从粘贴的评分表文本里解析评分项：支持"名称 30分"、竖线表格、"名称：30"等常见形态。"""
    items: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or re.match(r"^(评分|序号|评审|评分项)", line) or set(line) <= set("|-— "):
            continue
        line = line.strip("|").strip()
        if not line:
            continue
        if "|" in line:
            cells = [c.strip() for c in line.split("|") if c.strip()]
            full = 0.0
            name = ""
            for cell in cells:
                m = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*分?", cell)
                if m:
                    if not full:
                        full = float(m.group(1))
                elif not name and len(cell) >= 3:
                    name = cell
            if full and name:
                items.append({"item": name[:60], "score": f"{full:g}分", "rule": ""})
            continue
        m = SCORE_NUM.search(line)
        if not m:
            # "名称：30" 形态：冒号后是纯数字
            m2 = re.search(r"[：:]\s*(\d{1,3}(?:\.\d+)?)\s*$", line)
            if not m2:
                continue
            full = float(m2.group(1))
            name = line[: m2.start()]
        else:
            full = float(m.group(1))
            name = line[: m.start()]
        if full <= 0 or full > 200:
            continue
        name = name.strip(" :：、.。|\t-　")
        name = re.sub(r"^\d+[、..]\s*", "", name)
        name = re.sub(r"^[（(][一二三四五六七八九十\d]+[)）]\s*", "", name)
        if not name:
            continue
        items.append({"item": name[:60], "score": f"{full:g}分", "rule": ""})
    return items


# ---------------- 类别默认评分模板 ----------------
# 不同采购类别的评分权重结构差异很大：货物看参数，服务看方案与团队，
# 工程看施工组织与业绩。解读没有抽到评分办法时，按类别套用默认模板兜底，
# 让评标在"无评分表"时也能跑起来——结论始终标注为模板推定。
DEFAULT_RUBRICS: dict[str, list[tuple[str, float, str]]] = {
    "货物": [
        ("投标报价", 40, "满足要求且报价最低的为基准价，基准价得满分，其余按比例折算"),
        ("技术参数响应", 25, "对照采购需求逐项响应，负偏离酌减，★参数不响应不得分"),
        ("实施方案", 10, "供货、安装、调试与验收方案完整可操作"),
        ("售后服务", 10, "质保期、响应时限、备品备件与培训方案"),
        ("同类业绩", 8, "近三年同类合同，每提供 1 个得 2 分，最多 8 分"),
        ("项目团队", 4, "项目组成员资质与经验"),
        ("企业信用", 3, "信用记录、体系认证等"),
    ],
    "服务": [
        ("投标报价", 20, "采用低价优先法计算价格分"),
        ("服务方案", 25, "服务思路、流程与保障措施的针对性"),
        ("技术能力", 15, "技术路线、工具平台与质量管控"),
        ("实施计划", 10, "进度安排、里程碑与交付物"),
        ("服务团队", 10, "团队构成、驻场安排与人员资质"),
        ("应急与售后", 8, "应急预案、服务承诺与考核配合"),
        ("同类业绩", 8, "近三年同类服务合同"),
        ("企业信用", 4, "信用记录与体系认证"),
    ],
    "工程": [
        ("投标报价", 30, "以有效投标报价的平均值下浮为基准价计算价格分"),
        ("施工组织设计", 20, "总体部署、平面布置与施工工艺"),
        ("进度计划", 10, "总工期、网络计划与赶工措施"),
        ("质量与安全", 12, "质量保证体系与安全生产措施"),
        ("项目管理机构", 10, "项目经理与主要管理人员资质业绩"),
        ("企业业绩与信用", 13, "近三年同类工程业绩与信用评价"),
        ("资源投入", 5, "劳动力、机械设备与材料计划"),
    ],
}

CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "货物": ("货物", "设备", "采购", "仪器", "硬件", "产品", "器材", "物资"),
    "服务": ("服务", "运维", "咨询", "设计", "监理", "外包", "托管", "维修保养"),
    "工程": ("工程", "施工", "建设", "改建", "扩建", "修缮", "装修", "安装工程"),
}


def detect_category(text: str, name: str = "") -> str:
    """从招标文件开头与项目名猜采购类别；猜不出默认货物类。"""
    probe = (name or "") + "\n" + (text or "")[:3000]
    best, hits = "货物", -1
    for cat, words in CATEGORY_WORDS.items():
        count = sum(probe.count(w) for w in words)
        if count > hits:
            best, hits = cat, count
    return best


def rubric_for(category: str) -> list[dict]:
    rows = DEFAULT_RUBRICS.get(category) or DEFAULT_RUBRICS["货物"]
    return [
        {"item": name, "score": f"{score:g}分", "rule": rule}
        for name, score, rule in rows
    ]


# 这些是评分表的"表头/汇总"行，不是可打分的评审要素。
# 真实招标文件里它们往往和具体要素同表出现（"分值构成""合计"），
# 一旦被当成评分项，评标会给出 0 分这种看起来合理、实则完全错误的结论。
HEADER_ITEMS = (
    "分值构成", "分值权重", "分值构成（权重）", "评分标准", "评审标准", "评分办法", "评标办法",
    "评分因素", "评审因素", "评分项", "合计", "小计", "总计", "总分", "满分", "权重",
    "得分合计", "分数", "分值",
)


def scoring_valid(scoring: list[dict]) -> tuple[bool, str]:
    """判断评分表能不能支撑一次有意义的模拟评标。"""
    items = [str(s.get("item") or "").strip() for s in (scoring or []) if isinstance(s, dict)]
    items = [i for i in items if i]
    if not items:
        return False, "还没有评分办法：先跑「招标解读」，或在下方粘贴/手输评分表。"

    def is_header(name: str) -> bool:
        return any(name == h or name.endswith(h) for h in HEADER_ITEMS)

    real = [i for i in items if not is_header(i)]
    if len(real) < 2:
        shown = "、".join(items[:4])
        return False, (
            f"评分表只解析出 {len(real)} 个可打分的评审项（识别到：{shown}），"
            "这些更像是表格标题而不是评分要素。请在下方粘贴完整评分表（每行「评审要素 + 分值」），"
            "或先确认招标文件的评标办法章节是否被完整解析。"
        )
    # 分值全空说明只拿到了名称，没有权重，同样无法算总分
    with_score = [s for s in scoring if str((s or {}).get("score") or "").strip()]
    if len(with_score) < 2:
        return False, "评分项都没有分值，无法计算总分。请粘贴带分值的评分表（如：技术方案 30分）。"
    return True, ""
