"""对话入口：默认交给智能体大脑自主规划；显式 action（旧快捷按钮）走确定性路由。

大脑路径：观察项目状态 → 规划（模型 / 本地策略）→ 选基座或 AI 工具 → 执行 → 汇总。
旧路由只作为兼容层保留，手动页签与固定快捷指令仍可用。
"""
from __future__ import annotations

from typing import Any

from . import (agent, auto_core, brain, chains, config, core_bridge, parsers, rag, store, tender)

ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("optimize", ("优化", "按问题改", "修复问题", "复检", "闭环", "改并复查")),
    ("check", ("体检", "检查", "验标", "审查", "查问题", "合规", "废标")),
    ("compose", ("整本", "一键", "全部生成", "生成全部", "写整本", "全部写")),
    ("outline", ("大纲", "目录", "章节结构")),
    ("interpret", ("解读", "分析招标", "招标要求", "要点")),
    ("materials", ("我的资料", "材料", "素材", "知识库", "资料清单", "依据")),
    ("export", ("导出", "下载", "word", "Word", "docx")),
    ("help", ("能做什么", "帮助", "怎么用", "功能", "你好")),
]

HELP = [
    "解读招标文件 → 抽取硬性要求",
    "生成大纲 → 按招标文件和我提供的资料排章节",
    "一键写整本 / 写第 N 章 → 逐章生成，可覆盖已有正文",
    "体检 → 自动跑 8 项基座规则并并入问题清单，模型只补充措辞问题",
    "按问题优化并复检 → 按基座 + 规则的问题只改命中段落，最多 2 轮",
    "看我的资料 / 上传文件 → 本项目资料与企业素材库",
    "导出 Word / Markdown → 导出前自动跑门禁核对",
    "废标核对 / 偏离表 / 暗标 / 报价校验 → 主链路已自动调用，也可单独点名运行",
]


CORE_WORDS: list[tuple[str, str]] = [
    ("废标条款核对", "rejection"), ("废标核对", "rejection"),
    ("招标文件体检", "tender_audit"), ("偏离表", "deviation"), ("响应表", "deviation"),
    ("暗标", "blind_text"), ("雷同", "dupcheck"), ("模拟评标", "evaluate"),
    ("评分矩阵", "scoring"), ("事实一致性", "facts"), ("引用溯源", "verify_citations"),
    ("检查报告", "report"), ("技能库", "skills"), ("报价表校验", "quote"),
    ("金额大写", "money"), ("质疑函", "query_letter"), ("澄清函", "query_letter"),
    ("版面预算", "layout"), ("大纲导入", "outline_import"), ("标书结构", "bid_structure"),
    ("正文清洗", "cleaner"), ("BM25", "kb_search"), ("版本对比", "textdiff"),
    ("排版审计", "docx_audit"), ("自动修复", "docx_fix"), ("智能排版", "restyle"),
    ("版式预设", "formats"), ("HTML 版", "htmlexport"), ("表格导出", "table_export"),
    ("分册", "bundle"), ("模板填空", "fill_template"), ("模板套用", "fill_template"),
]


def detect(message: str) -> str:
    for word, key in CORE_WORDS:
        if word in message:
            return f"core:{key}"
    if any(w in message for w in ("基座能力", "能力清单", "有哪些能力")):
        return "core_list"
    for action, words in ROUTES:
        if any(w in message for w in words):
            return action
    return "free"


def _sources(pid: str) -> list[str]:
    """「本次依据」只列本项目资料 + 与本项目语境相关的企业素材。

    以前直接取库里前 3 篇 kb，电缆附件项目也会显示「视频监控技术方案标准模板」。
    """
    mats = [f"我提供的资料·{m['title']}"
            for m in store.usable_docs(pid, include_kb=False)
            if m.get("scope") == "project"]
    kb = [f"企业素材库·{m['title']}" for m in rag.project_kb_rows(pid)][:3]
    return mats + kb


def _apply_attachment(pid: str, att: dict) -> str:
    """上传的文件按用途入库，返回给对话的说明文字。R05 带解析待核对状态。"""
    name = str(att.get("filename") or "未命名文件")
    text = str(att.get("text") or "")
    target = str(att.get("target") or "material")
    meta = parsers.meta_from_attachment(att)
    chars = int(meta.get("effective_chars") or 0)
    if not text.strip() or not chars or meta.get("parse_status") == "unusable":
        return f"「{name}」没有解析出有效文字，未入库。"
    pm = att.get("page_map") or []
    store.save_pages(pid, target, name, pm)
    needs = bool(meta.get("needs_review"))
    note = (f"⚠ {meta.get('review_note')}；人工核对前这份资料不进入依据与已确认要求。"
            if needs else
            f"{int(meta.get('ocr_pages') or 0)} 页走本地 OCR，页码可回跳。" if meta.get("ocr_pages") else "")
    if target == "tender":
        kind = str(att.get("tender_kind") or "")
        if kind not in store.TENDER_KINDS:
            kind = "amend" if store.list_tender_versions(pid) else "original"
        r = tender.ingest(pid, kind, text, filename=name,
                          parse=dict(meta, filename=name))
        v = r.get("version") or {}
        return (f"已把「{name}」（有效 {chars} 字）作为**招标文件第 "
                f"{v.get('version', 1)} 版·{tender.KIND_LABELS.get(kind, kind)}**保存，"
                f"原条款保留；旧自动要求转失效 {r.get('retired_requirements', 0)} 条，"
                f"导出确认已失效，相关正文与检查需复核。{note}\n\n")
    if target == "draft":
        r = agent.import_draft(pid, text)
        return f"已把「{name}」（有效 {chars} 字）导入为**已有竞标书**，切成 {r.get('chapters', 0)} 章；报价表等保护章节保留。{note}\n\n"
    if target == "kb":
        store.add_doc(name, text, scope="kb", page_map=pm, parse=meta)
        return f"已把「{name}」（有效 {chars} 字）加入**企业素材库**，后续生成会检索引用。{note}\n\n"
    store.add_doc(name, text, scope="project", project_id=pid, page_map=pm, parse=meta)
    return f"已把「{name}」（有效 {chars} 字）加入**我提供的资料**，生成标书时优先采用。{note}\n\n"


def _auto_attachment(pid: str, att: dict | None) -> dict | None:
    """上传招标文件 / 已有竞标书后，自动跑对应阶段的基座能力。"""
    stage = {"tender": "tender", "draft": "draft"}.get(
        str((att or {}).get("target") or ""))
    if not stage:
        return None
    try:
        return auto_core.as_card(auto_core.run_stage(stage, pid, force=True))
    except Exception:
        return None


def _issue_items(pid: str, issues: list[dict]) -> list[dict]:
    p = store.get_project(pid)
    titles = {s["id"]: s["title"] for s in p.get("sections", [])}
    return [{
        "code": i.get("code", ""), "level": i.get("level", "中"),
        "title": i.get("title", ""), "detail": i.get("detail", ""),
        "section": titles.get(i.get("section_id", ""), "整本"),
        "anchor": i.get("anchor", ""), "source": i.get("source", "规则"),
    } for i in issues]


def _card_job(kind: str, pid: str, fn, *args) -> tuple[str, dict]:
    jid, busy = store.create_job_exclusive(kind, pid)
    if busy:
        # 同项目已有任务在排队 / 运行：接上那一个，不新建
        return busy["id"], {"type": "job", "title": "后台任务", "job_id": busy["id"],
                            "kind": busy.get("kind") or kind, "reused": True}
    store.submit_job(fn, pid, jid, *args)
    return jid, {"type": "job", "title": "后台任务", "job_id": jid, "kind": kind}


def handle(pid: str, message: str = "", action: str = "", attachment: dict | None = None,
           history: list[dict] | None = None, planner: str = "") -> dict:
    # 默认路径：一句话（可带附件）交给大脑自己决定调哪些基座 / 哪些走 AI
    if not action or action in ("brain", "auto"):
        return brain.handle(pid, message, attachment, history, force_planner=planner)
    # 兼容路径：页签 / 旧快捷指令显式指定了动作
    if attachment and not message:
        # 只上传文件、没下指令：入库 + 自动跑对应基座阶段，不跑自由问答
        auto_card = _auto_attachment(pid, attachment)
        items = ["解读招标文件", "生成大纲", "根据我提供的资料写整本标书", "体检", "导出 Word"]
        if auto_card and auto_card.get("count"):
            items.insert(0, "自动基座已跑完 " + str(auto_card["count"]) + " 项能力，发现 " +
                         str(auto_card["findings"]) + " 条线索；可以直接说「体检」")
        return {
            "reply": _apply_attachment(pid, attachment).rstrip(),
            "action": "attachment",
            "cards": ([auto_card] if auto_card else []) +
                     [{"type": "list", "title": "接下来可以说", "items": items}],
            "sources": _sources(pid),
            "job_id": None,
        }
    prefix = _apply_attachment(pid, attachment) if attachment else ""
    auto_card = _auto_attachment(pid, attachment)
    act = action or detect(message)
    reply, cards, job_id = "", [], None
    p = store.get_project(pid)

    # 材料闸门：这一步需要内容，但项目里什么都没有 → 不执行，先提示上传或填写
    gate = brain.content_gate_act(act, pid, message, attachment)
    if gate and gate.get("reply"):
        return {
            "reply": prefix + str(gate["reply"]),
            "action": "need_content",
            "need_content": {"missing": gate.get("missing") or [], "blocked": []},
            "cards": gate.get("cards") or [],
            "sources": _sources(pid),
            "job_id": None,
        }
    prefix += str((gate or {}).get("note") or "") + ("\n\n" if gate and gate.get("note") else "")

    if act == "interpret":
        data = agent.interpret(pid)
        must = data.get("must", [])
        reply = f"已解读招标文件，抽到 {len(must)} 条硬性要求。这些要求会驱动大纲、写作和体检。"
        cards = [
            {"type": "kv", "title": "解读结果", "rows": [
                ["项目", str(data.get("project", p["name"]))],
                ["范围", str(data.get("scope", "—"))[:80]],
                ["资格要求", "、".join(data.get("qualification", [])) or "—"],
            ]},
            {"type": "list", "title": "硬性要求", "items": must[:8]},
        ]

    elif act == "outline":
        chapters = agent.make_outline(pid)
        reply = f"已生成 {len(chapters)} 章大纲，已有章节标题保留，正文不会错位。"
        cards = [{"type": "list", "title": f"大纲（{len(chapters)} 章）",
                  "items": [c["title"] for c in chapters]}]

    elif act == "compose":
        if not store.outline_confirmed(pid):
            return {
                "reply": prefix + "目录还没确认：请先在「写标书」页的目录区核对章节，"
                         "点「确认目录」后再回来写整本，避免目录不对整本返工。",
                "action": "outline_unconfirmed",
                "cards": [{"type": "list", "title": "先确认目录",
                           "items": ["打开「写标书」页", "核对章节名称与顺序",
                                     "点目录区的「确认目录」"]}],
                "sources": _sources(pid),
                "job_id": None,
            }
        overwrite = any(w in message for w in ("覆盖", "重写", "重新生成", "全部重"))
        jid, card = _card_job("compose", pid, agent.compose, overwrite)
        job_id = jid
        reply = ("这个项目已经有一份整本生成在跑，已接上原有任务，不会重复写同一份正文。"
                 if card.get("reused") else
                 "已在后台开始整本生成，逐章写、单章失败不影响其余章节。"
                 + ("已勾选覆盖已有正文。" if overwrite
                    else "已有正文的章节会跳过，需要覆盖请说「重写」。"))
        cards = [card]

    elif act == "check":
        issues = agent.check(pid, 1)
        high = sum(1 for i in issues if i["level"] == "高")
        ck_card = auto_core.as_card(agent.last_auto(pid))
        auto_note = ""
        if ck_card.get("count"):
            auto_note = (f" 已自动调用 {ck_card['count']} 项基座能力"
                         f"（发现 {ck_card['findings']} 条线索），结论已并入问题清单。")
        reply = (f"体检完成：{len(issues)} 个问题，其中高风险 {high} 个。"
                 "规则判定可复现，模型只补充措辞问题；报价/承诺函等保护章节不会被自动改写。"
                 + auto_note)
        cards = ([ck_card] if ck_card.get("count") else []) + [
            {"type": "kv", "title": "体检汇总", "rows": [["问题总数", str(len(issues))], ["高风险", str(high)]]},
            {"type": "issues", "title": "问题清单（前 10 条）", "items": _issue_items(pid, issues)[:10]},
        ]

    elif act == "optimize":
        jid, card = _card_job("optimize", pid, agent.optimize, 2)
        job_id = jid
        reply = "已启动闭环：体检 → 按问题只改命中段落 → 复检，最多 2 轮。每条改动留前后对照与取材来源。"
        cards = [card]

    elif act == "materials":
        mats = store.list_docs(scope="project", project_id=pid)
        kb = store.list_docs(scope="kb")
        reply = f"本项目有 {len(mats)} 份我提供的资料，企业素材库有 {len(kb)} 篇。生成与改写时，本项目资料优先级最高。"
        cards = [
            {"type": "docs", "title": f"我提供的资料（{len(mats)}）", "items": [
                {"title": d["title"], "chars": str(len(d["content"])), "preview": d["content"][:90]} for d in mats]},
            {"type": "docs", "title": f"企业素材库（{len(kb)}）", "items": [
                {"title": d["title"], "chars": str(len(d["content"])), "preview": d["content"][:90]} for d in kb]},
        ]

    elif act == "export":
        gate = auto_core.run_stage("export", pid, force=True)
        gate_card = auto_core.as_card(gate)
        high = int(gate_card.get("high") or 0)
        reply = ("导出前已强制重跑 " + str(gate_card.get("count", 0)) + " 项基座门禁，发现 "
                 + str(gate_card.get("findings", 0)) + " 条线索、高风险 " + str(high) + " 条。"
                 + ("下载 Word 时会先逐条确认这些高风险问题，确认后才放行。"
                    if high else "无高风险，可直接下载。")
                 + " Word 沿用 run 级中文字体排版（封面 + 章节 + 页码），正文不含内部工作标注。")
        cards = [
            gate_card,
            {"type": "link", "title": "导出", "url": f"/api/projects/{pid}/export.docx", "label": "下载 Word（.docx）"},
            {"type": "link", "title": "导出", "url": f"/api/projects/{pid}/export", "label": "下载 Markdown（.md）"},
        ]

    elif act.startswith("core:"):
        key = act.split(":", 1)[1]
        r = core_bridge.run(key, pid, {"query": message, "text": message})
        if not r.get("ok"):
            reply = f"基座能力执行失败：{r.get('error')}"
        else:
            stats = r.get("stats") or []
            cards = [{"type": "kv", "title": r["name"],
                      "rows": [[str(x["label"]), str(x["value"])[:120]] for x in stats[:8]]}]
            for sec in (r.get("sections") or [])[:2]:
                items = [" · ".join(f"{k}:{str(v)[:60]}" for k, v in row.items())
                         for row in (sec.get("rows") or [])[:6]]
                if items:
                    cards.append({"type": "list", "title": sec["title"], "items": items})
            dl = next((x for x in stats if x["label"] == "download"), None)
            if dl:
                cards.append({"type": "link", "title": "导出",
                              "url": str(dl["value"]), "label": "下载生成的文件"})
            reply = r.get("summary") or f"已运行「{r['name']}」。"
            if r.get("skipped"):
                reply += "（这一步需要切到真实模型）"
            reply += " 单项完整结果可到「自动基座」页签查看。"

    elif act == "core_list":
        cat = core_bridge.catalog()
        stt = auto_core.status(pid)
        reply = (f"基座 {cat['modules']} 个业务模块，目录登记 {cat['count']} 项能力："
                 f"可执行 {cat['executable']} 项、需真实模型 {cat['needs_model']} 项、"
                 f"占位 {cat['placeholder']} 项（{'、'.join(sorted(core_bridge.PLACEHOLDER_CAPS))}，"
                 f"未接入主链路、不参与自动编排）。"
                 f"共 {len(stt['stages'])} 个节点会在主链路里自己跑，"
                 + ("当前已开启。" if stt["enabled"] else "当前已关闭，可在「自动基座」页签开启。")
                 + " 不用手动点运行；需要单看某一项时直接说名字即可。")
        cards = [{"type": "list", "title": "自动调用节点",
                  "items": [f"{s['label']}（{s['trigger']}）：自动跑 {len(s['caps'])} 项 · {s['desc']}"
                            for s in stt["stages"]]}]

    elif act == "help":
        reply = "我能做这些事，也可以直接把文件拖进来当资料："
        cards = [{"type": "list", "title": "能力清单", "items": HELP}]

    else:  # free
        context = rag.materials_block(pid)[:3000] + "\n\n" + rag.retrieve(pid, message)[0][:2500]
        auto_free = None
        if any(w in message for w in ("问题", "风险", "合规", "废标", "能不能", "缺",
                                      "建议", "如何", "怎么样", "检查", "评审", "扣分")):
            rep = auto_core.run_stage("check", pid)
            auto_free = auto_core.as_card(rep)
            lines = [f"- {x['name']}：{x['findings']} 条" for x in rep.get("ran", []) if x["findings"]]
            context += ("\n\n【基座自动核查结论，回答时可引用】\n"
                        + ("\n".join(lines[:8]) or "本次核查未发现线索"))[:1200]
        try:
            answer = str(chains.run("free", {"context": context, "question": message},
                                    pid=pid, actor="chat")).strip()
        except Exception as exc:
            answer = f"模型调用失败：{str(exc)[:120]}"
        reply = answer
        cards = ([auto_free] if auto_free else []) + cards
        if config.MODE != "live":
            cards += [{"type": "list", "title": "可以直接说", "items": HELP[:5]}]

    if auto_card:
        cards = [auto_card] + cards
    return {
        "reply": prefix + reply,
        "action": act,
        "cards": cards,
        "sources": _sources(pid),
        "job_id": job_id,
    }


def job_result(pid: str, kind: str, job: dict) -> dict:
    """后台任务结束后，转成一条对话消息。partial（部分完成）也要如实说清楚哪几章没成。"""
    r = job.get("result") or {}
    status = job.get("status")
    if status == "failed" and not r:
        return {"reply": f"任务失败：{job.get('error') or '未知错误'}",
                "cards": [], "sources": _sources(pid)}
    if kind == "compose":
        p = store.get_project(pid)
        written = sum(1 for s in p["sections"] if (s.get("text") or "").strip())
        c = r.get("counts") or {}
        bad = (c.get("failed", 0) + c.get("empty", 0) + c.get("conflict", 0)
               + c.get("undeliverable", 0))
        label = "整本生成完成" if status == "done" else "整本生成部分完成"
        if status == "failed":
            label = "整本生成没写成"
        detail = "，".join(f"{k} {c[v]} 章" for k, v in
                           (("模型没返回", "empty"), ("生成期间被你改过", "conflict"),
                            ("出错", "failed"), ("未达交付标准", "undeliverable")) if c.get(v))
        reply = (f"{label}：新写 {r.get('generated', 0)} 章，跳过已有正文 {r.get('skipped', 0)} 章。"
                 + (f" 另有 {detail}，这些章节仍是空的或需要人工处理。" if bad else "")
                 + (f" 已自动跑 {r['auto'].get('count', 0)} 项基座复检。" if r.get("auto", {}).get("count") else "")
                 + ((" 整本现有 " + str(r.get("undeliverable_total")
                                        or r.get("undeliverable") or 0)
                     + " 章未达交付标准；导出会列出提醒，确认后仍可下载，"
                       "但文件不能直接提交投标。")
                    if (r.get("undeliverable_total") or r.get("undeliverable")) else "")
                 + ("" if bad else " 正文已经放进「标书正文」，可以逐章改。"))
        todo = [f"{x['title']} · {x.get('note') or x['outcome']}"
                for x in (r.get("chapters") or [])
                if x.get("outcome") in ("empty", "conflict", "failed", "undeliverable")]
        return {
            "reply": reply,
            "cards": ([r["auto"]] if r.get("auto", {}).get("count") else []) + [
                {"type": "cta", "title": "接着改正文",
                 "text": f"现在有 {written} 章正文。左边选章节，右边改内容，改完自动保存。",
                 "tab": "write", "label": "去正文逐章修改"},
                {"type": "kv", "title": "生成结果", "rows": [
                    ["新写章节（已落库）", str(r.get("generated", 0))],
                    ["跳过（已有正文）", str(r.get("skipped", 0))],
                    ["模型未返回", str(c.get("empty", 0))],
                    ["生成期间被人工改过", str(c.get("conflict", 0))],
                    ["出错", str(c.get("failed", 0))],
                    ["未达交付标准", str(c.get("undeliverable", 0))],
                ]},
                {"type": "list", "title": "要人工处理的章节", "items": todo[:12]},
                {"type": "list", "title": "章节", "items": [
                    f"{s['title']} · {len(s['text'])} 字" for s in p["sections"]]},
            ],
            "sources": _sources(pid),
        }
    return {
        "reply": (f"闭环完成：问题 {len(r.get('before', []))} → {len(r.get('after', []))}，"
                  f"改写 {r.get('fixed_count', 0)} 段，执行 {r.get('rounds', 0)} 轮。"
                  + (f" 有 {r.get('error_count', 0)} 条改写没落地（生成期间被改过或出错），见清单。"
                     if r.get("error_count") else "")
                  + (f" 已自动出复检报告与偏离表（{r['auto'].get('count', 0)} 项）。"
                     if r.get("auto", {}).get("count") else "")),
        "cards": ([r["auto"]] if r.get("auto", {}).get("count") else []) + [
            {"type": "kv", "title": "复检对比", "rows": [
                ["初检问题", str(len(r.get("before", [])))],
                ["复检问题", str(len(r.get("after", [])))],
                ["已改写段落", str(r.get("fixed_count", 0))],
                ["保护章节跳过", str(r.get("protected_count", 0))],
            ]},
            {"type": "diff", "title": "逐条改动", "items": [
                {"title": f.get("title", ""), "section": f.get("section", ""),
                 "before": f.get("before", ""), "after": f.get("after", ""),
                 "sources": [h.get("title", "") for h in f.get("sources", [])]}
                for f in r.get("fixes", []) if f.get("status") == "fixed"]},
            {"type": "issues", "title": "复检剩余问题", "items": _issue_items(pid, r.get("after", []))[:8]},
        ],
        "sources": _sources(pid),
    }
