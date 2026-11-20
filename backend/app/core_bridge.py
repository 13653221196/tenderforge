"""基座能力桥接层。

把 `basecore`（本地业务引擎，43 项基座能力）接到 bidagent 的
store / API / 界面上。职责只有三件：字段适配（text -> content）、能力注册、
结果归一化。不重写任何业务算法，也不接管 HTTP 路由。

分工不变：LCEL 链负责生成文字，basecore 负责判定、排版、导出与可审计性。
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from pathlib import Path
from typing import Any

from . import config as appcfg
from . import ctxpool, delivery, docx_export, guard, rag, rules, scope, settings, store
from .basecore import config as basecfg
from .basecore import db as basedb
from .basecore.services import (
    bid_structure,
    blind,
    blind_rules,
    bundle,
    checker,
    cleaner,
    deviation,
    docx_audit,
    docx_fix,
    dupcheck,
    evaluate,
    facts,
    feasibility,
    fill_template,
    formats,
    htmlexport,
    import_bid,
    interpret,
    jobs,
    kb,
    layout,
    money,
    outline_import,
    parser,
    query_letter,
    quote,
    rejection,
    report,
    requirements,
    restyle,
    skill_engine,
    table_export,
    tender_audit,
    tender_sections,
    textdiff,
)

DATA = appcfg.DATA_DIR
EXPORTS = DATA / "exports"
EXPORTS.mkdir(parents=True, exist_ok=True)

# 基座产出的性质说明：这些文件是审阅件，没经导出门禁、也没有暗标脱敏，
# 下载入口会带 X-Artifact-Kind: review，界面据此提示，避免被当成正式投标文件。
REVIEW_NOTE = "审阅件：未经导出门禁、不含暗标脱敏，不能直接作为投标文件提交。"


def _artifact(c: dict, path: Path, kind: str = "review") -> dict:
    """登记落盘文件，并返回带下载地址的结果片段。

    kind=deliverable 才会被当成可交付件；基座产出统一按 review 登记，
    这样「能不能下载」有据可查，跨企业 / 跨项目的文件名也拿不到。
    内容指纹由 store.record_artifact 计算。
    """
    try:
        item = store.record_artifact(c["pid"], kind, path, name=path.name,
                                    org_id=delivery.org_id(),
                                    purpose=("正式交付" if kind == "deliverable" else REVIEW_NOTE))
    except Exception:
        return {"output": str(path)}
    body = {"output": str(path), "download": f"/api/core/download/{path.name}",
            "artifact_id": item["id"], "artifact_kind": item["kind"]}
    if item["kind"] != "deliverable":
        body["note"] = REVIEW_NOTE
    return body

try:
    basedb.init_db()
except Exception:
    pass


# ---------------------------------------------------------------- 上下文适配
def _ctx(pid: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    p = store.get_project(pid)
    if not p:
        raise ValueError("项目不存在")
    raw = p.get("sections") or []
    secs: list[dict] = []
    for i, s in enumerate(raw):
        # 基座里的暗标规则 / 引用溯源 / 模拟评标读的是这里的正文。
        # 逐字抄录的招标原文不是我方表述，先剔除引用列：招标方名称、
        # 条款号、负偏离措辞都不再被算成我方身份信息或无依据数字。
        text = rules.strip_quoted(str(s.get("text") or ""))
        secs.append({
            "id": s.get("id") or f"s{i}",
            "ord": s.get("seq", i),
            "seq": s.get("seq", i),
            "title": s.get("title") or f"第{i + 1}章",
            "content": text,
            "text": text,
            "level": 1,
            "words": len(re.sub(r"\s", "", text)),
            "protected": bool(s.get("protected")),
        })
    tender = p.get("tender_text") or ""
    bid_text = "\n".join(f"{s['title']}\n{s['content']}" for s in secs)
    proj_docs = store.list_docs(scope="project", project_id=pid, usable_only=True)
    kb_docs = rag.project_kb_rows(pid)
    materials = "\n".join(f"【{d['title']}】\n{d['content']}" for d in proj_docs)
    corpus = "\n".join(s["content"] for s in secs)
    meta = {
        "project_name": p.get("name") or "",
        "tender_name": p.get("name") or "",
        "bidder": "",
    }
    return {
        "pid": pid, "p": p, "project": p, "secs": secs, "tender": tender,
        "bid_text": bid_text, "corpus": corpus, "meta": meta,
        "materials": materials, "proj_docs": proj_docs, "kb_docs": kb_docs,
        "payload": payload,
    }


def _base_project(c: dict) -> dict:
    return {
        "id": c["pid"], "name": c["p"].get("name") or "",
        "tender_text": c["tender"], "meta": json.dumps(c["meta"], ensure_ascii=False),
        "outline": "",
    }


def _docx(c: dict) -> Path:
    path = delivery.output_path(c["pid"], "basecore.docx", kind="review")
    path.write_bytes(docx_export.build_docx(c["p"].get("name") or "投标文件", c["secs"]))
    return path


def _async(coro):
    """在独立线程里跑协程，避开 FastAPI 线程池 / 已有事件循环的冲突。

    带当前企业上下文：否则基座 AI 能力在子线程里会读回平台默认配置。
    """
    return ctxpool.run_sync(asyncio.run, coro, timeout=240)


BASE_AI_ROLE = "review"


def _cfg(role: str = BASE_AI_ROLE):
    """基座 AI 判定能力用「验标书 / 复核」环节绑定的模型。

    AI 复核、否决条款、偏离表、模拟评标、润色、可研与 AI 技能都是判定类任务，
    对「查问题准」要求最高，不该只跟随界面里那个中等档的全局当前模型。
    角色未绑定或绑定的模型没配全时回落全局当前模型；关闭外发则走本地降级。
    """
    llm = appcfg.llm_config(role)
    if not llm.ready:
        llm = appcfg.llm_config()
    # 用户关闭外发 / 未完成首次告知时，基座 AI 能力自动走本地降级，不出域
    if appcfg.MODE == "live" and llm.ready and appcfg.outbound_allowed():
        return basecfg.LLMConfig(base_url=llm.base_url, api_key=llm.api_key,
                                 model=llm.model, max_tokens=llm.max_tokens,
                                 timeout=llm.timeout, wire_api=llm.wire_api,
                                 extra_body=dict(llm.extra_body or {}))
    # 空配置：ready=False，任何基座 AI 能力都拿不到可用外发地址
    return basecfg.LLMConfig(base_url="", api_key="", model="")


def _need_ai() -> bool:
    return (appcfg.MODE == "live" and appcfg.outbound_allowed()
            and _cfg().ready)


def _safe(c: dict, purpose: str, *, text: Any = None,
          sections: list[dict] | None = None, limit: int | None = None) -> tuple[str, str]:
    """所有发给外部模型的正文统一过 guard 唯一出口，返回 (安全文本, 错误)。"""
    try:
        return guard.outbound(text if text is not None else "",
                              pid=c["pid"], purpose=purpose,
                              sections=sections, limit=limit), ""
    except guard.OutboundBlocked as exc:
        return "", str(exc)


def _safe_secs(c: dict, purpose: str, per_section: int = 1500) -> tuple[list[dict], str]:
    """basecore 直接接收 sections 列表的能力，先把保护章节正文替换掉。"""
    try:
        guard.require_outbound(c["pid"], purpose)
        return guard.safe_sections(c["secs"], per_section=per_section), ""
    except guard.OutboundBlocked as exc:
        return [], str(exc)


# ---------------------------------------------------------------- 能力实现
# 每个 _cap_xxx(c) 返回 dict；异常由 run() 统一兜住，单能力失败不影响其它。

def _cap_check_full(c):
    issues = checker.run_rules(_base_project(c), c["secs"], c["meta"])
    return {"summary": "基座 12 条确定性规则全量体检",
            **checker.summarize(issues, "未配置 AI，已跳过模型复核"),
            "issues": issues}


def _cap_rejection(c):
    items = rejection.dedupe(rejection.extract_by_rules(c["tender"], limit=80))
    judged = rejection.judge_all(items, rejection.build_corpus(c["secs"]))
    return {"summary": f"从招标文件抽出 {len(items)} 条否决条款并逐条比对投标文件",
            **rejection.summarize(judged), "items": judged}


def _cap_tender_audit(c):
    return {"summary": "招标文件合规体检（14 条确定性规则）", **tender_audit.run(c["tender"])}


def _cap_requirements(c):
    data = requirements.build(c["meta"], c["tender"])
    items = data.get("items") or []
    return {"summary": "招标需求条目与投标响应覆盖度", **data,
            "coverage": requirements.coverage(items, c["secs"])}


def _cap_deviation(c):
    kind = c["payload"].get("kind") or "技术"
    meta = {**c["meta"], **requirements.build(c["meta"], c["tender"])}
    result = deviation.build(meta, c["secs"], kind)
    return {"summary": f"{kind}偏离表 / 响应表", **result,
            "rows": deviation.template_rows(result)}


def _cap_blind_text(c):
    issues = blind_rules.scan_text(c["bid_text"])
    orgs = sorted(blind_rules.frequent_orgs(c["bid_text"], min_count=2))
    return {"summary": "暗标文本级检查（身份信息 / 证件号 / 高频机构名）",
            "issues": issues, "frequent_orgs": orgs,
            "stats": {"疑似命中": len(issues), "高频机构": len(orgs)}}


def _cap_blind_file(c):
    return {"summary": "暗标 docx 合规检查（含格式规则）", **blind.check(_docx(c))}


def _cap_dupcheck(c):
    """章节两两雷同度：复用基座 dice / normalize / meta_signals。"""
    secs = [s for s in c["secs"] if len(s["content"].strip()) > 60]
    rows = []
    for i in range(len(secs)):
        for j in range(i + 1, len(secs)):
            score = dupcheck.dice(dupcheck.normalize(secs[i]["content"]),
                                 dupcheck.normalize(secs[j]["content"]))
            if score >= 0.18:
                rows.append({"章节 A": secs[i]["title"], "章节 B": secs[j]["title"],
                             "相似度": round(score, 3),
                             "判定": "疑似重复" if score >= 0.45 else "轻微相似"})
    rows.sort(key=lambda r: -r["相似度"])
    return {"summary": "本项目章节间雷同度扫描（多文件比对可用上传入口）",
            "stats": {"参与章节": len(secs), "相似对": len(rows)}, "rows": rows}


def _cap_evaluate(c):
    scoring = evaluate.parse_scoring(c["tender"])
    data = evaluate.estimate(scoring, c["secs"], c["tender"])
    return {"summary": "按评分办法模拟评标、定位失分点",
            "scoring": scoring, **data}


def _cap_scoring(c):
    scoring = evaluate.parse_scoring(c["tender"])
    return {"summary": "评分矩阵（评分项 × 分值）", "rows": scoring,
            "stats": {"识别评分项": len(scoring)}}


def _cap_facts(c):
    items = facts.from_tender(c["tender"], limit=12)
    texts = {s["title"]: s["content"] for s in c["secs"]}
    conflicts = facts.consistency(texts, items)
    return {"summary": "项目级全局事实表与全书数字 / 名称一致性",
            "facts": items, "conflicts": conflicts,
            "stats": {"抽取事实": len(items), "不一致": len(conflicts)},
            "facts_block": facts.build_block(items)}


def _cap_quote(c):
    text = c["payload"].get("text") or c["bid_text"]
    try:
        result = quote.analyze(text=text)
    except quote.QuoteError:
        # 没有真实报价表就明确说「无法校验」，绝不用内置示例数字冒充校验结果
        return {"summary": "无法校验：缺少本项目的报价表（正文中未识别到标准报价表）",
                "note": "请粘贴或上传本项目的报价表后再运行；系统不会用示例金额代替你的报价。",
                "_skipped": True,
                "stats": {"结论": "无法校验", "原因": "缺少本项目报价表"}}
    return {"summary": "报价表算术 / 合计 / 大小写 / 漏项校验",
            "note": "按本项目正文中的报价表校验", **result}


def _cap_money(c):
    value = c["payload"].get("amount")
    if value in (None, ""):
        return {"summary": "金额数字转中文大写",
                "note": "未填写金额，未做换算；请输入本项目实际金额后再运行，系统不预置示例金额。",
                "_skipped": True, "stats": {"结论": "未换算", "原因": "缺少金额"}}
    return {"summary": "金额数字转中文大写",
            "stats": {"数字": value, "中文大写": money.to_cn_amount(value)},
            "parse": str(money.parse_amount(value))}


def _cap_query_letter(c):
    issues = tender_audit.run(c["tender"]).get("issues") or []
    result = query_letter.build(c["meta"], issues)
    return {"summary": "质疑 / 澄清函（按招标文件体检问题生成）", **result}


def _cap_interpret(c):
    return {"summary": "招标文件规则解读（不调用模型的确定性抽取）",
            **interpret.from_rules(c["tender"])}


def _cap_layout(c):
    return {"summary": "版面预算：把目标页数翻译成每章该写多少字",
            **layout.allocate(c["secs"], total_pages=int(c["payload"].get("pages") or 60))}


def _cap_outline_import(c):
    return {"summary": "导入现成目录文本 → 章节树",
            **outline_import.parse(c["payload"].get("text") or c["corpus"][:3000])}


def _cap_bid_structure(c):
    parsed = bid_structure.parse_text(c["bid_text"])
    return {"summary": "标书结构解析与评分项挂接", **parsed,
            **bid_structure.to_chapters(parsed, evaluate.parse_scoring(c["tender"]))}


def _cap_tender_sections(c):
    chapters = tender_sections.split_chapters(c["tender"])
    return {"summary": "招标文件章节切分与采样",
            "rows": [{"标题": x.get("title"), "字数": x.get("words"),
                      "得分": x.get("score")} for x in chapters[:40]],
            "stats": {"章节数": len(chapters)}}


def _cap_cleaner(c):
    stats = cleaner.analyze(c["corpus"])
    return {"summary": "正文清洗建议与字数统计", **stats,
            "lint": cleaner.lint(c["corpus"], stats)}


def _cap_kb_search(c):
    """基座 BM25 混合检索：对项目资料 + 企业素材库建索引。"""
    query = c["payload"].get("query") or "响应 服务 资质 报价"
    chunks: list[dict] = []
    for d in c["proj_docs"] + c["kb_docs"]:
        for ch in kb.split_chunks(d["content"]):
            chunks.append({**ch, "title": d["title"]})
    index = kb.build_index(c["pid"], chunks, "v1")
    hits = index.search(query, top_k=8) if chunks else []
    return {"summary": f"基座 BM25 检索「{query}」",
            "rows": [{"标题": h.get("title", ""), "片段": str(h.get("text", ""))[:160],
                      "得分": round(float(h.get("score", 0)), 3)} for h in hits],
            "coverage": kb.coverage_stats(hits),
            "material_block": kb.build_material_block(hits) if hits else ""}


def _cap_verify_citations(c):
    allowed = c["materials"] + "\n" + "\n".join(d["content"] for d in c["kb_docs"])
    unverified = kb.verify_citations(c["corpus"], allowed)
    return {"summary": "正文引用溯源：找出找不到依据的表述",
            "unverified": unverified,
            "stats": {"待核实表述": len(unverified),
                      "资料字数": len(allowed)}}


def _cap_textdiff(c):
    before = c["payload"].get("before") or ""
    after = c["payload"].get("after") or c["corpus"]
    return {"summary": "前后版本逐段对比", **textdiff.compare(before, after)}


def _cap_docx_audit(c):
    return {"summary": "Word 排版审计（字体 / 字号 / 页边距 / 表格 / 域代码等 60+ 规则）",
            **docx_audit.audit(_docx(c))}


def _cap_docx_fix(c):
    src = _docx(c)
    out = delivery.output_path(c["pid"], "fixed.docx", kind="review")
    audit = docx_audit.audit(src)
    ids = [r.get("id") or r.get("rule") for r in (audit.get("rules") or [])
           if (r.get("count") or 0) > 0]
    ids = [str(x) for x in ids if x][:20]
    if not ids:
        return {"summary": "未发现可自动修复项", "rules": []}
    result = docx_fix.fix(src, ids, out)
    return {"summary": "Word 自动修复", "rules": ids, **result, **_artifact(c, out)}


def _cap_restyle(c):
    from docx import Document
    src = _docx(c)
    out = delivery.output_path(c["pid"], "restyled.docx", kind="review")
    detected = restyle.detect(Document(src))
    result = restyle.restyle(src, out, with_cover=True)
    return {"summary": "无样式文档智能重排（识别标题层级 + 目标版式）",
            "detect": detected, **result, **_artifact(c, out)}


def _cap_formats(c):
    return {"summary": "版式预设与用户模板", "rows": formats.preset_list(),
            "resolve": formats.resolve(c["payload"].get("preset"))}


def _cap_htmlexport(c):
    html = htmlexport.build_html(project_name=c["p"].get("name") or "投标文件",
                                 sections=c["secs"])
    out = delivery.output_path(c["pid"], "basecore.html", kind="review")
    banner = (f'<div style="padding:10px;border:1px solid #d33;color:#a00;'
              f'font-size:13px;margin:8px 0">{REVIEW_NOTE}</div>')
    marked = (html.replace("<body>", "<body>" + banner, 1)
              if "<body>" in html else banner + html)
    out.write_text(marked, encoding="utf-8")
    return {"summary": "带封面与目录的 HTML 版投标文件（审阅件）", **_artifact(c, out),
            "stats": {"字符数": len(html)}, "preview": html[:600]}


def _cap_table_export(c):
    md = table_export.markdown(
        ["序", "章节", "字数", "状态"],
        [[i + 1, s["title"], s["words"], "已填写" if s["content"].strip() else "空"]
         for i, s in enumerate(c["secs"])])
    return {"summary": "章节清单表格（Markdown / Word / Excel / PDF 四态导出零件）",
            "markdown": md}


def _cap_bundle(c):
    from . import bidbundle
    volumes = bundle.plan_volumes(c["secs"])
    proj = _base_project(c)
    proj["meta"] = c["meta"]  # project_code 期望 dict，不是 JSON 字符串
    preview = bidbundle.plan(c["pid"])
    return {"summary": "整套投标包分册规划（技术 / 商务 / 资格 / 报价）；规划不是文件，"
                       "确认归属后到导出页生成真实分册包",
            "rows": volumes, "code": bundle.project_code(proj),
            "preview_volumes": [{"name": v["name"],
                                 "section_ids": [s["id"] for s in v["sections"]]}
                                for v in preview["volumes"]],
            "needs_confirmation": True,
            "note": "本能力只做规划；实际分册 Word、附件、索引与清单由导出页生成"}


def _cap_import_bid(c):
    src = _docx(c)
    parsed = import_bid.parse_document(src)
    imported = parsed.get("sections") or parsed.get("chapters") or []
    matched = import_bid.match(imported, c["secs"]) if imported else {}
    return {"summary": "原方案还原：从已有标书反推章节结构并与当前目录匹配",
            "parsed_count": len(imported), **matched}


def _cap_fill_template(c):
    src = _docx(c)
    scanned = fill_template.scan(src)
    dictionary = fill_template.build_dictionary(
        facts.from_tender(c["tender"]), c["meta"])
    planned = fill_template.plan(scanned.get("spots") or [], dictionary)
    return {"summary": "模板占位符扫描与事实字典填充", **scanned,
            "dictionary_size": len(dictionary), "plan": planned}


def _cap_parser(c):
    return {"summary": "招标文件结构化抽取（基座 parser.rule_extract）",
            **parser.rule_extract(c["tender"])}


def _cap_report(c):
    issues = checker.run_rules(_base_project(c), c["secs"], c["meta"])
    rej = rejection.judge_all(
        rejection.dedupe(rejection.extract_by_rules(c["tender"], limit=40)),
        rejection.build_corpus(c["secs"]))
    out = delivery.output_path(c["pid"], "检查报告.docx", kind="review")
    path = report.build_check_report(
        project_name=c["p"].get("name") or "", checks=issues,
        rejections=rej, evaluate=evaluate.estimate(
            evaluate.parse_scoring(c["tender"]), c["secs"], c["tender"]),
        out_path=out)
    return {"summary": "Word 检查报告（规则 + 废标 + 模拟评标汇总）",
            **_artifact(c, path),
            "stats": {"规则问题": len(issues), "否决条款": len(rej),
                      "文件大小": path.stat().st_size}}


def _cap_skills(c):
    catalog = skill_engine.catalog()
    return {"summary": "基座技能库（可启停 / 可安装 / 可执行）",
            "rows": [{"名称": s.get("title") or s.get("name"),
                      "标识": s.get("name"), "分类": s.get("category_label") or s.get("category"),
                      "模式": s.get("mode_label") or s.get("mode"),
                      "启用": "是" if s.get("enabled", 1) else "否"}
                     for s in catalog],
            "stats": skill_engine.stats()}


def _cap_skill_run(c):
    if not scope.is_platform():
        return {"summary": "技能运行记录目前使用平台共享库，本企业暂不开放此入口",
                "_skipped": True, "ok": False}
    name = c["payload"].get("skill") or "scoring_matrix"
    ctx = {
        "project_id": c["pid"], "tender_text": c["tender"], "bid_text": c["bid_text"],
        "sections": c["secs"], "meta": c["meta"], "materials": c["materials"],
    }
    cfg = _cfg() if _need_ai() else None
    if cfg is not None:
        tender, e1 = _safe(c, "skill.tender", text=c["tender"], limit=6000)
        draft, e2 = _safe(c, "skill.draft", sections=c["secs"], limit=8000)
        secs, e3 = _safe_secs(c, "skill.sections")
        materials, e4 = _safe(c, "skill.materials", text=c["materials"], limit=3000)
        if e1 or e2 or e3 or e4:
            cfg = None  # 出域被拦：技能退回本地模板，绝不外发
        else:
            ctx["tender_text"], ctx["bid_text"] = tender, draft
            ctx["sections"], ctx["materials"] = secs, materials
    try:
        data = _async(skill_engine.execute(name, ctx, cfg))
    except Exception as exc:
        return {"summary": f"技能「{name}」未能执行：{exc}",
                "note": "请先在项目里上传招标文件与投标文件，或换一项技能（如 scoring_matrix）",
                "stats": {"技能": name}}
    return {"summary": f"执行技能「{name}」", **data}


def _cap_ai_check(c):
    if not _need_ai():
        return {"summary": "AI 复核需要切到真实模型（右上角）", "_skipped": True}
    # 逐章送审：长标书不再被整段截断成「只看开头几章」
    secs, err = _safe_secs(c, "ai_check.sections", per_section=checker.REVIEW_PER_CHAPTER)
    if err:
        return {"summary": err, "_skipped": True}
    issues, notice = _async(checker.ai_review(_cfg(), c["tender"], secs))
    usable = [s for s in secs
              if str(s.get("text") or "").strip()
              and str(s.get("text")) != guard.BLOCK_MARK]
    covered = min(len(usable), checker.REVIEW_MAX_CHAPTERS)
    return {"summary": f"基座 AI 复核：逐章送审，覆盖 {covered} 章（重试 / JSON 五层容错 / 报错脱敏）",
            "issues": issues, "notice": notice,
            "stats": {"覆盖章节": covered}}


def _cap_ai_rejection(c):
    if not _need_ai():
        return {"summary": "AI 抽取否决条款需要真实模型", "_skipped": True}
    tender, err = _safe(c, "ai_rejection.tender", text=c["tender"], limit=8000)
    if err:
        return {"summary": err, "_skipped": True}
    items = _async(rejection.extract_by_ai(_cfg(), tender, limit=60))
    judged = rejection.judge_all(items, rejection.build_corpus(c["secs"]))
    return {"summary": "AI 抽取 + 规则判定否决条款", **rejection.summarize(judged),
            "items": judged}


def _cap_ai_deviation(c):
    if not _need_ai():
        return {"summary": "AI 偏离表需要真实模型", "_skipped": True}
    secs, err = _safe_secs(c, "ai_deviation.sections")
    if err:
        return {"summary": err, "_skipped": True}
    return {"summary": "AI 辅助偏离表",
            **_async(deviation.build_async(c["meta"], secs, "技术",
                                           cfg=_cfg(), use_ai=True))}


def _cap_ai_evaluate(c):
    if not _need_ai():
        return {"summary": "AI 模拟评标需要真实模型", "_skipped": True}
    secs, e1 = _safe_secs(c, "ai_evaluate.sections")
    tender, e2 = _safe(c, "ai_evaluate.tender", text=c["tender"], limit=6000)
    if e1 or e2:
        return {"summary": e1 or e2, "_skipped": True}
    scoring = evaluate.parse_scoring(c["tender"])
    return {"summary": "AI 逐项模拟评标",
            **_async(evaluate.evaluate(_cfg(), scoring=scoring, sections=secs,
                                       tender_text=tender))}


def _cap_ai_polish(c):
    if not _need_ai():
        return {"summary": "AI 润色质疑函需要真实模型", "_skipped": True}
    base = query_letter.build(c["meta"], tender_audit.run(c["tender"]).get("issues"))
    text, err = _safe(c, "ai_polish.text", text=base.get("text") or "", limit=6000)
    if err:
        return {"summary": err, "_skipped": True}
    base = {**base, "text": text}
    return {"summary": "AI 润色后的质疑 / 澄清函", **_async(query_letter.polish(_cfg(), base))}


def _cap_ai_feasibility(c):
    if not _need_ai():
        return {"summary": "可研报告生成需要真实模型", "_skipped": True}
    background, err = _safe(c, "ai_feasibility.background",
                            text=c["tender"][:3000], limit=3000)
    if err:
        return {"summary": err, "_skipped": True}
    params = {"project": c["p"].get("name") or "", "background": background}
    outline = _async(feasibility.gen_outline(_cfg(), params))
    return {"summary": "可行性研究报告大纲", "outline": outline}


def _cap_jobs(c):
    return {"summary": "基座任务状态机已挂载（线程池 + SQLite + 暂停 / 恢复 / 取消）",
            "stats": {"已注册任务": len(getattr(jobs, "RUNNERS", {}) or {}),
                      "模块": "basecore.services.jobs"},
            "note": "bidagent 当前长任务仍走自身轻量队列；此处提供基座状态机供后续切换"}


# ---------------------------------------------------------------- 注册表
CAPABILITIES: list[dict] = [
    ("check_full", "标书全面体检", "检查与合规", "12 条确定性规则 + 三态结论"),
    ("rejection", "废标条款核对", "检查与合规", "抽取否决条款并逐条比对"),
    ("tender_audit", "招标文件体检", "检查与合规", "14 条合规 / 倾向性规则"),
    ("requirements", "需求覆盖度", "检查与合规", "硬性要求条目与响应覆盖"),
    ("deviation", "偏离表 / 响应表", "检查与合规", "逐条对照、Word/Excel/PDF 导出零件"),
    ("blind_text", "暗标文本检查", "检查与合规", "身份信息、证件号、机构名"),
    ("blind_file", "暗标 Word 检查", "检查与合规", "含页眉页脚、格式规则"),
    ("dupcheck", "雷同检查", "检查与合规", "章节两两相似度"),
    ("evaluate", "模拟评标", "检查与合规", "按评分办法定位失分点"),
    ("scoring", "评分矩阵", "检查与合规", "评分项 × 分值"),
    ("facts", "事实一致性", "检查与合规", "全书数字与名称只能有一个来源"),
    ("verify_citations", "引用溯源", "检查与合规", "找出无依据的表述"),
    ("report", "检查报告", "检查与合规", "规则 + 废标 + 评标汇总"),
    ("skills", "技能库", "检查与合规", "23 项可启停技能"),
    ("skill_run", "执行技能", "检查与合规", "按名称运行一项技能"),
    ("ai_check", "AI 复核", "AI 增强", "带重试与容错的全量复核"),
    ("ai_rejection", "AI 抽取废标条款", "AI 增强", "模型抽取 + 规则判定"),
    ("ai_deviation", "AI 偏离表", "AI 增强", "模型辅助逐条对照"),
    ("ai_evaluate", "AI 模拟评标", "AI 增强", "逐评分项给出失分原因"),
    ("ai_polish", "AI 质疑函", "AI 增强", "生成后模型润色"),
    ("ai_feasibility", "可研报告", "AI 增强", "大纲与逐章生成"),
    ("quote", "报价表校验", "报价与函件", "算术 / 合计 / 大小写 / 漏项"),
    ("money", "金额大写", "报价与函件", "数字转中文大写"),
    ("query_letter", "质疑 / 澄清函", "报价与函件", "按体检问题成函"),
    ("interpret", "招标解读", "写作与规划", "规则版确定性抽取"),
    ("layout", "版面预算", "写作与规划", "目标页数 → 每章字数"),
    ("outline_import", "大纲导入", "写作与规划", "现成目录 → 章节树"),
    ("bid_structure", "标书结构解析", "写作与规划", "结构 + 评分项挂接"),
    ("tender_sections", "招标章节切分", "写作与规划", "章节识别与重要性打分"),
    ("cleaner", "正文清洗", "写作与规划", "字数统计与格式 lint"),
    ("kb_search", "BM25 混合检索", "写作与规划", "基座索引 + 命中片段 + 覆盖统计"),
    ("textdiff", "版本对比", "写作与规划", "逐段前后差异"),
    ("parser", "招标结构化抽取", "写作与规划", "基座 parser 规则抽取"),
    ("docx_audit", "Word 排版审计", "排版与交付", "60+ 条版式规则"),
    ("docx_fix", "Word 自动修复", "排版与交付", "按规则 ID 修复并输出文件"),
    ("restyle", "无样式智能排版", "排版与交付", "识别标题层级后重排"),
    ("formats", "版式预设", "排版与交付", "内置预设与用户模板"),
    ("htmlexport", "HTML 版标书", "排版与交付", "封面 + 目录"),
    ("table_export", "表格导出", "排版与交付", "Markdown / Word / Excel / PDF"),
    ("bundle", "整套投标包分册", "排版与交付", "技术 / 商务 / 资格 / 报价"),
    ("import_bid", "原方案还原", "排版与交付", "从已有标书反推章节"),
    ("fill_template", "模板填空", "排版与交付", "占位符扫描 + 事实字典"),
    ("jobs", "任务状态机", "引擎", "暂停 / 恢复 / 取消 / checkpoint"),
]
CAPABILITIES = [dict(zip(("key", "name", "group", "desc"), row)) for row in CAPABILITIES]

# 目录里已登记、但还没接进主链路的空壳。展示可以，不能让规划器当成真能力来选：
# jobs 只挂载了基座状态机（长任务仍走 bidagent 自己的队列）；
# table_export 只产出 Markdown 章节清单，没有描述里的四态导出。
PLACEHOLDER_CAPS = {"jobs", "table_export"}
AI_NEED_MODEL_CAPS = {"ai_check", "ai_rejection", "ai_deviation",
                      "ai_evaluate", "ai_polish", "ai_feasibility"}

GROUPS = ["检查与合规", "AI 增强", "报价与函件", "写作与规划", "排版与交付", "引擎"]


# ---------------------------------------------------------------- 结果归一化
def _to_rows(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        return []
    if isinstance(value[0], dict):
        return [{str(k): (v if isinstance(v, (str, int, float, bool, type(None)))
                          else json.dumps(v, ensure_ascii=False, default=str)[:200])
                 for k, v in row.items()} for row in value[:60]]
    return []


def _present(res: Any, name: str, key: str) -> dict:
    raw_summary = res.get("summary") if isinstance(res, dict) else ""
    out: dict[str, Any] = {
        "key": key, "name": name, "ok": True,
        "summary": raw_summary if isinstance(raw_summary, str) else name,
        "stats": [], "sections": [], "text": "",
    }
    if not isinstance(res, dict):
        out["text"] = str(res)
        return out
    leftover: dict[str, Any] = {}
    for k, v in res.items():
        if k in ("summary", "_skipped"):
            if k == "summary" and isinstance(v, dict):
                for sk, sv in v.items():
                    out["stats"].append({"label": str(sk), "value": sv})
            continue
        if k == "note":
            # 「审阅件」「未脱敏」这类提醒单独传，别混进统计数字里被截断
            out["note"] = str(v or "")
            continue
        if k == "stats" and isinstance(v, dict):
            for sk, sv in v.items():
                out["stats"].append({"label": str(sk), "value": sv})
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out["stats"].append({"label": k, "value": v})
        elif isinstance(v, str) and 0 < len(v) <= 90:
            out["stats"].append({"label": k, "value": v})
        elif k == "tables" and isinstance(v, list):
            for t in v:
                if not isinstance(t, dict):
                    continue
                headers = t.get("headers") or []
                rows = t.get("rows") or []
                dict_rows = [dict(zip(headers, r)) if isinstance(r, (list, tuple)) else r
                             for r in rows]
                out["sections"].append({"title": t.get("title") or "表格",
                                        "rows": _to_rows(dict_rows)})
        elif isinstance(v, list):
            rows = _to_rows(v)
            if rows:
                out["sections"].append({"title": k, "rows": rows})
            elif v and all(isinstance(x, str) for x in v):
                out["sections"].append({"title": k,
                                        "rows": [{"值": x} for x in v[:40]]})
        elif isinstance(v, str) and len(v) > 90:
            out["text"] += (f"\n\n### {k}\n" if out["text"] else f"### {k}\n") + v[:12000]
        elif isinstance(v, dict):
            rows = [{"字段": str(kk), "值": str(vv)[:300]} for kk, vv in v.items()]
            out["sections"].append({"title": k, "rows": rows})
        else:
            leftover[k] = v
    if res.get("_skipped"):
        out["skipped"] = True
    if leftover:
        out["raw"] = json.dumps(leftover, ensure_ascii=False, default=str)[:2000]
    return out


def run(key: str, pid: str, payload: dict | None = None) -> dict:
    cap = next((x for x in CAPABILITIES if x["key"] == key), None)
    if not cap:
        return {"key": key, "name": key, "ok": False, "error": "未知能力"}
    # 唯一执行入口：停用的能力在任何路径（大脑 / 阶段 / 单项调试 / 门禁）都不跑
    if not settings.cap_enabled(key):
        return {"key": key, "name": cap["name"], "ok": False,
                "summary": "该基座能力已在「设置」中停用，本次未执行。",
                "error": "cap_disabled", "stats": [], "sections": [], "text": ""}
    handler = globals().get(f"_cap_{key}")
    if handler is None:
        return {"key": key, "name": cap["name"], "ok": False, "error": "能力未实现"}
    try:
        # 基座库也按企业分：技能运行记录 / 启停状态不跨企业共享。
        # _async 带主应用上下文；skill_engine 的运行记录仍属平台共享库。
        with basedb.use_org(scope.org()):
            c = _ctx(pid, payload)
            result = handler(c)
            return _present(result, cap["name"], key)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        low = msg.lower()
        model_issue = any(k in low for k in (
            "402", "401", "403", "insufficient", "llmerror", "timed out", "timeout"))
        if model_issue:
            appcfg.record_model_error(msg, source="core." + key)
            hint = ("真实模型不可用（余额 / 密钥 / 网络），本项已跳过；"
                    "请换一个可用模型，或在「管理多个模型」里测试连通性")
        else:
            hint = "本项前置条件未满足：" + str(exc)[:120]
        # 失败也要给界面一句人能看懂的话，不能只返回空白 summary
        return {"key": key, "name": cap["name"], "ok": False, "degraded": True,
                "summary": hint, "error": msg,
                "stats": [], "sections": [], "text": ""}


def _service_module_count() -> int:
    """真正接进来的基座服务模块数。

    不写死数字：新增 / 移除 import 时目录自动跟着变。按对象身份比对，
    避免把 bidagent 自己也有的同名模块（docx_export）算成基座模块。
    """
    from types import ModuleType
    from .basecore import services as pkg
    g = globals()
    return sum(1 for name, mod in vars(pkg).items()
               if not name.startswith("_") and isinstance(mod, ModuleType)
               and g.get(name) is mod)


def catalog() -> dict:
    """目录按真实可用性标注，避免把「登记数量」当成「已有能力数量」。"""
    caps = []
    for c in CAPABILITIES:
        av = ("占位" if c["key"] in PLACEHOLDER_CAPS else
              "需模型" if c["key"] in AI_NEED_MODEL_CAPS else "可执行")
        caps.append({**c, "availability": av})
    by_avail = {k: sum(1 for c in caps if c["availability"] == k)
                for k in ("可执行", "需模型", "占位")}
    return {"capabilities": caps, "groups": GROUPS, "count": len(caps),
            "executable": by_avail["可执行"], "needs_model": by_avail["需模型"],
            "placeholder": by_avail["占位"],
            "modules": _service_module_count(), "skills": len(skill_engine.scan())}
