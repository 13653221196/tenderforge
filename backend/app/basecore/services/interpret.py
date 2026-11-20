"""招标文件解读：优先 AI 结构化抽取，失败或无配置时退回本地规则。"""
from __future__ import annotations

import asyncio
import re

from ..config import TENDER_TEXT_LIMIT, LLMConfig
from . import tender_sections
from .llm import LLMError, chat, parse_json
from .parser import rule_extract

SYSTEM = (
    "你是投标方的招标文件分析专家。你的任务是把招标文件里对投标决策最关键的硬信息抽出来，"
    "只输出 JSON，不要解释、不要客套、不要编造。"
    "原文没有的内容一律留空字符串或空数组，严禁猜测。"
)

TEMPLATE = """请从下面的招标文件中抽取信息，严格按此 JSON 结构输出：

{{
  "summary": "用一句话说明这是什么项目、招什么、预算与工期",
  "basic": [{{"label": "项目名称", "value": ""}}, {{"label": "项目编号", "value": ""}},
            {{"label": "招标人", "value": ""}}, {{"label": "预算金额", "value": ""}},
            {{"label": "投标保证金", "value": ""}}, {{"label": "投标截止时间", "value": ""}},
            {{"label": "开标时间", "value": ""}}, {{"label": "工期或服务期", "value": ""}},
            {{"label": "投标有效期", "value": ""}}, {{"label": "付款方式", "value": ""}},
            {{"label": "质保期", "value": ""}}, {{"label": "投标文件份数", "value": ""}}],
  "qualification": [{{"item": "资格要求原文要点", "evidence": "对应原文片段（20 字内）"}}],
  "scoring": [{{"item": "评分项名称", "score": "分值数字（无法确定写空）", "rule": "评分标准要点"}}],
  "star_clauses": [{{"item": "带★或▲的强制条款要点"}}],
  "risks": [{{"level": "高|中|低", "text": "可能导致废标或重大偏差的风险，及原因"}}]
}}

要求：
- qualification 最多 12 条，scoring 最多 20 条，star_clauses 最多 15 条，risks 最多 8 条；
- 金额、时间、分值保留原文写法，不要换算；
- 没有★/▲条款时 star_clauses 返回空数组。

招标文件正文：
---
{text}
---"""


def _placeholders() -> dict:
    return {
        "summary": "",
        "basic": [
            {"label": "项目名称", "value": ""},
            {"label": "项目编号", "value": ""},
            {"label": "招标人", "value": ""},
            {"label": "预算金额", "value": ""},
            {"label": "投标保证金", "value": ""},
            {"label": "投标截止时间", "value": ""},
            {"label": "开标时间", "value": ""},
            {"label": "工期或服务期", "value": ""},
            # P4 新增维度：没识别到就留空，与 tender_dims 的「未识别」口径一致
            {"label": "投标有效期", "value": ""},
            {"label": "付款方式", "value": ""},
            {"label": "质保期", "value": ""},
            {"label": "投标文件份数", "value": ""},
        ],
        "qualification": [],
        "scoring": [],
        "star_clauses": [],
        "risks": [],
    }


_CELL_JUNK = re.compile(r"^[\s|｜]*|[\s|｜]*$")


def _clean_value(v: object, limit: int = 300) -> str:
    """清掉表格拼接残留（"| 无"→"无"）与多余空白。"""
    s = str(v or "").strip()
    s = _CELL_JUNK.sub("", s)
    s = re.sub(r"\s*\|\s*", " / ", s)  # 中间竖线读起来像分隔符，换成斜杠
    s = re.sub(r"\s{2,}", " ", s)
    return s[:limit]


def _normalize(data: dict) -> dict:
    result = _placeholders()
    if isinstance(data.get("summary"), str):
        result["summary"] = data["summary"][:300]

    # 评分项单独放宽：真实评标办法常有三四十个评审要素（商务+技术+报价），
    # 限 24 条会把技术分细则整段丢掉，评标模拟直接少一半维度。
    caps = {"basic": 24, "qualification": 24, "scoring": 40, "star_clauses": 24, "risks": 24}
    for key, cap in caps.items():
        value = data.get(key)
        if isinstance(value, list):
            cleaned = []
            for item in value:
                if not isinstance(item, dict) or not any(item.values()):
                    continue
                row = dict(item)
                for field in ("value", "item", "rule", "evidence", "text", "label", "score"):
                    if field in row:
                        row[field] = _clean_value(row[field], 400 if field == "rule" else 300)
                cleaned.append(row)
            result[key] = cleaned[:cap]
    if result["basic"]:
        merged = {row["label"]: row for row in result["basic"] if row.get("label")}
        ordered = []
        for row in _placeholders()["basic"]:
            ordered.append(merged.get(row["label"], row))
        result["basic"] = ordered
    return result


def from_rules(text: str, reason: str = "") -> dict:
    raw = rule_extract(text)
    info = raw["基本信息"]
    result = _placeholders()
    result["summary"] = "（未启用 AI，以下为规则抽取结果，需要人工补全）"
    label_map = {
        "项目编号": "项目编号",
        "招标人": "招标人",
        "预算金额": "预算金额",
        "投标保证金": "投标保证金",
        "投标截止": "投标截止时间",
        "开标时间": "开标时间",
        "工期/服务期": "工期或服务期",
    }
    for row in result["basic"]:
        source_key = next((k for k, v in label_map.items() if v == row["label"]), None)
        if source_key and source_key in info:
            row["value"] = info[source_key]
    result["qualification"] = [{"item": q, "evidence": ""} for q in raw["资格要求"]]
    result["scoring"] = [
        {"item": s["item"], "score": s["score"], "rule": ""} for s in raw["评分办法"]
    ]
    result["star_clauses"] = [{"item": s} for s in raw["星号条款"]]
    if reason:
        result["risks"] = [{"level": "低", "text": reason}]
    result["source"] = "rule"
    return result


def _merge_rules(ai: dict, text: str) -> dict:
    """AI 缺字段时用规则结果补齐（尤其基本信息里的编号、保证金）。"""
    rules = rule_extract(text)
    info = rules["基本信息"]
    filled = []
    for row in ai["basic"]:
        if not row.get("value"):
            for key, value in info.items():
                if key in row["label"] or row["label"] in key:
                    # 规则结果是原文切片，同样要清表格残留（"| 无"）
                    row["value"] = _clean_value(value)
                    break
        filled.append(row)
    ai["basic"] = filled
    if not ai["star_clauses"] and rules["星号条款"]:
        ai["star_clauses"] = [{"item": s} for s in rules["星号条款"][:10]]
    return ai


def _key(text: object) -> str:
    """合并去重用的归一化键：去掉空白与标点，截前 60 字。"""
    s = re.sub(r"[\s，。、；：,.;:（）()【】\[\]\"'“”‘’/\\|—-]+", "", str(text or ""))
    return s.lower()[:60]


def _merge_passes(parts: list[dict]) -> dict:
    """把多段解读结果合成一份：基本信息取首个非空，条目类做去重并集。"""
    out = _placeholders()
    out["summary"] = next((p.get("summary") for p in parts if p.get("summary")), "")
    vals: dict[str, str] = {}
    for part in parts:
        for row in part.get("basic") or []:
            label = row.get("label")
            value = (row.get("value") or "").strip()
            if label and value and not vals.get(label):
                vals[label] = value
    out["basic"] = [{"label": r["label"], "value": vals.get(r["label"], "")} for r in out["basic"]]

    caps = {"qualification": 40, "scoring": 60, "star_clauses": 30, "risks": 30}
    for key, field in (
        ("qualification", "item"),
        ("scoring", "item"),
        ("star_clauses", "item"),
        ("risks", "text"),
    ):
        seen: set[str] = set()
        rows: list[dict] = []
        for part in parts:
            for row in part.get(key) or []:
                k = _key(row.get(field, ""))
                if not k or k in seen:
                    continue
                seen.add(k)
                rows.append(row)
        out[key] = rows[: caps[key]]
    return out


async def _interpret_once(cfg: LLMConfig, text: str) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TEMPLATE.format(text=text)},
    ]
    raw = await chat(cfg, messages, json_mode=True, max_tokens=3000, task="interpret")
    return _normalize(parse_json(raw))


async def _interpret_chunks(cfg: LLMConfig, chunks: list[dict], concurrency: int = 3) -> tuple[list[dict], int]:
    """逐段解读（并发）；失败段自动补跑一次。返回 (成功结果, 仍失败段数)。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    got: dict[int, dict] = {}
    failed_idx: list[int] = []

    async def worker(idx: int) -> None:
        async with sem:
            try:
                got[idx] = await _interpret_once(cfg, chunks[idx]["text"])
            except (LLMError, ValueError):
                failed_idx.append(idx)

    await asyncio.gather(*(worker(i) for i in range(len(chunks))))
    if failed_idx:
        retry = sorted(set(failed_idx))
        failed_idx.clear()
        await asyncio.gather(*(worker(i) for i in retry))
    return [got[i] for i in sorted(got)], len(chunks) - len(got)


async def interpret(cfg: LLMConfig, text: str) -> dict:
    if not text.strip():
        raise ValueError("招标文件内容为空，请先上传文件")
    if not cfg.ready:
        return from_rules(text, "当前未配置 AI 接口，本页结果为本地规则抽取；在“设置”里填入接口后可获得完整解读。")

    # 超长文件分段送模型、结果合并——不截断、不丢章节。
    # 招标文件的长度不由投标方决定，少看一章就可能漏掉否决条款或格式要求。
    chunks, report = tender_sections.plan(text, TENDER_TEXT_LIMIT)
    try:
        if len(chunks) <= 1:
            data = await _interpret_once(cfg, chunks[0]["text"] if chunks else text)
            failed = 0
        else:
            parts, failed = await _interpret_chunks(cfg, chunks)
            if not parts:
                raise LLMError("各分段均解读失败")
            data = _merge_passes(parts)
        data = _merge_rules(data, text)
        data["source"] = "ai"
        data["sample"] = report
        if len(chunks) > 1:
            note = report["note"]
            if failed:
                note += f"其中 {failed} 段调用失败，该段内容尚未纳入，建议稍后重新解读补齐。"
            data["notice"] = note
        return data
    except LLMError as exc:
        fallback = from_rules(text, f"AI 调用失败（{exc}），已退回规则抽取。")
        fallback["sample"] = report
        if len(chunks) > 1:
            fallback["notice"] = report["note"]
        return fallback


# ---------------- 招标要素分解（18 项） ----------------
# 一次让模型抽 18 件事会丢项，所以拆成 18 个独立小任务，并发跑、只回 JSON。
ELEMENTS: list[tuple[str, str, str]] = [
    ("overview", "项目概览", "这个项目招什么、规模多大、分几个标段、建设或服务地点在哪里"),
    ("project_info", "项目基本信息", "项目名称、编号、招标人、资金来源、预算或最高限价"),
    ("tech", "技术需求", "技术指标、性能参数、系统功能、验收标准，逐条列出"),
    ("delivery", "交付与服务要求", "交付期、服务期、质保期、响应时限、驻场或培训要求"),
    ("purchase", "采购清单", "货物或工程量清单：名称、规格、数量、单位"),
    ("response_req", "响应文件要求", "投标文件由哪些部分组成、装订与份数要求、格式模板要求"),
    ("agent", "代理机构信息", "招标代理机构名称、联系人、电话、地址、文件售价"),
    ("key_info", "关键时间节点", "报名、答疑、投标截止、开标、评标、公示时间"),
    ("margin", "保证金与费用", "投标保证金金额与形式、履约保证金、代理服务费"),
    ("qualification", "资格条件", "投标人必须具备的资质、业绩、财务、人员条件"),
    ("compliance", "符合性审查", "形式审查与响应性审查的具体条目"),
    ("opening", "开标程序", "开标时间地点、唱标内容、投标人代表要求"),
    ("eval", "评标办法", "评标方法（综合评分/最低价）、评审流程、定标原则"),
    ("business_score", "商务评分细则", "商务部分的评分项、分值、评分标准"),
    ("reject", "废标条款", "哪些情形会被否决投标或作废标处理"),
    ("sign", "签署与装订", "签字盖章要求、密封要求、电子投标要求"),
    ("contract", "合同条款要点", "付款方式、违约责任、验收与结算、知识产权"),
    ("other", "其他特殊要求", "样品、演示、现场踏勘、联合体、分包等特别规定"),
]

ELEMENT_SYSTEM = (
    "你是招标文件解析器。只抽取原文中明确写出的内容，原文没有就返回空数组，严禁推测或补全。"
    "只输出 JSON，不要解释。"
)

ELEMENT_TEMPLATE = """从下面的招标文件中抽取「{title}」相关信息：{hint}

输出 JSON：
{{"summary": "一句话概括这方面的情况（原文没有就留空）",
  "items": [{{"label": "条目名", "value": "具体内容（保留原文数字与单位）", "evidence": "对应原文片段，20 字内"}}]}}

要求：items 最多 12 条；只写原文有的，不要编造。

招标文件正文：
---
{text}
---"""


async def extract_element(cfg: LLMConfig, key: str, title: str, hint: str, text: str) -> dict:
    """text 应已是 plan() 切好的一段，不再二次截断。"""
    messages = [
        {"role": "system", "content": ELEMENT_SYSTEM},
        {"role": "user", "content": ELEMENT_TEMPLATE.format(title=title, hint=hint, text=text)},
    ]
    raw = await chat(cfg, messages, json_mode=True, max_tokens=1200, task="extract")
    data = parse_json(raw)
    items = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and (item.get("value") or item.get("label")):
            items.append(
                {
                    "label": str(item.get("label") or "")[:40],
                    "value": str(item.get("value") or "")[:300],
                    "evidence": str(item.get("evidence") or "")[:60],
                }
            )
    return {
        "key": key,
        "title": title,
        "summary": str(data.get("summary") or "")[:200],
        "items": items[:12],
        "source": "ai",
    }


def _merge_element(key: str, title: str, parts: list[dict]) -> dict:
    """合并同一要素在各分段上的抽取结果：条目去重，摘要拼接。"""
    items: list[dict] = []
    seen: set[str] = set()
    for part in parts:
        for it in part.get("items") or []:
            k = _key(str(it.get("label") or "") + "|" + str(it.get("value") or ""))
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(it)
    summaries = [p.get("summary", "").strip() for p in parts if (p.get("summary") or "").strip()]
    summary = "；".join(dict.fromkeys(summaries))[:300]
    return {"key": key, "title": title, "summary": summary, "items": items[:24], "source": "ai"}


async def extract_elements(
    cfg: LLMConfig,
    text: str,
    existing: dict | None = None,
    *,
    only_missing: bool = True,
    concurrency: int = 4,
) -> dict:
    """并发抽取 18 项要素；超长文件按段抽取后合并，覆盖全文。"""
    existing = existing or {}
    todo = [
        (key, title, hint)
        for key, title, hint in ELEMENTS
        if not (only_missing and (existing.get(key) or {}).get("items"))
    ]
    if not todo:
        return {"elements": existing, "done": [], "failed": [], "skipped": len(ELEMENTS)}

    # 不截断：按章节把全文切段，每个要素在每段上各抽一次，最后合并。
    chunks, report = tender_sections.plan(text, TENDER_TEXT_LIMIT)

    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, dict] = dict(existing)
    done: list[str] = []
    failed: list[dict] = []
    got: dict[str, dict[int, dict]] = {}
    errors: dict[str, str] = {}

    async def worker(key: str, title: str, hint: str, idx: int) -> None:
        async with sem:
            try:
                part = await extract_element(cfg, key, title, hint, chunks[idx]["text"])
                got.setdefault(key, {})[idx] = part
            except (LLMError, ValueError) as exc:
                errors[key] = str(exc)[:120]

    await asyncio.gather(*(worker(k, t, h, i) for k, t, h in todo for i in range(len(chunks))))

    # 失败的分段补跑一次：长文件并发时偶发超时/坏 JSON，不应让要素缺一段
    missing = [
        (k, t, h, i)
        for k, t, h in todo
        for i in range(len(chunks))
        if i not in (got.get(k) or {})
    ]
    if missing:
        async def retry_worker(key: str, title: str, hint: str, idx: int) -> None:
            async with sem:
                try:
                    got.setdefault(key, {})[idx] = await extract_element(
                        cfg, key, title, hint, chunks[idx]["text"]
                    )
                except (LLMError, ValueError):
                    pass

        await asyncio.gather(*(retry_worker(*m) for m in missing))

    for key, title, _hint in todo:
        parts = got.get(key) or {}
        if not parts:
            failed.append({"key": key, "title": title, "error": errors.get(key, "各分段抽取均失败")})
            continue
        merged = _merge_element(key, title, [parts[i] for i in sorted(parts)])
        if len(parts) < len(chunks):
            merged["partial"] = f"仅 {len(parts)}/{len(chunks)} 段成功"
        results[key] = merged
        done.append(key)

    return {
        "elements": results,
        "done": done,
        "failed": failed,
        "skipped": len(ELEMENTS) - len(todo),
        "sample": report,
        "chunks": len(chunks),
    }
