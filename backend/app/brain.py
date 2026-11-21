"""智能体大脑：观察 → 规划 → 执行 → 汇总。

这是 bidagent 的唯一决策入口。用户只给一句话（可带附件），由大脑自己判断
当前项目状态，自己决定调哪些基座能力、哪些交给 AI、哪些走主链路，然后逐步执行。

分工不变：
- base.*  基座规则能力：本地、确定、毫秒级，管判定与抽取；
- stage.* 一组基座能力的批量编排，复用 auto_core；
- ai.*    LCEL 链：只在需要措辞、解读、自由回答时调用模型；
- flow.*  主链路复合动作（整本生成 / 闭环 / 体检 / 导出 / 入库）；
- sys.*   只读查询。

规划器在 live 模式由模型担任（LangChain LCEL），demo 模式由本地启发式担任，
两者输出同一种计划结构，界面与执行器不区分。模型不直接改数据，只能通过工具。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import (ThreadPoolExecutor,
                               TimeoutError as FutureTimeout)
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from . import (agent, auto_core, chains, config, core_bridge, ctxpool, guard,
               parsers, quality, rag, rules, samples, settings, store, tender)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-plan")
_RUN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-run")
ARGS_ALLOWED = {"target", "query", "text", "sid", "title", "instruction",
                "overwrite", "rounds", "key", "stage", "payload", "scope"}


# ---------------------------------------------------------------- 工具目录
# 高频能力单独写「什么时候用」，其余按分组给通用说明，避免目录过长。
BASE_WHEN: dict[str, str] = {
    "check_full": "想全面查标书问题、占位符、字数、响应缺失时",
    "rejection": "担心废标、要核对否决条款是否逐条响应时",
    "tender_audit": "刚拿到招标文件，要查合规 / 倾向性条款时",
    "requirements": "要看硬性要求是否都被标书覆盖时",
    "deviation": "要生成技术 / 商务偏离表、响应表时",
    "blind_text": "暗标项目，要查正文里的身份信息、公司名、证件号时",
    "blind_file": "暗标项目，要连 Word 页眉页脚一起查时",
    "dupcheck": "担心章节雷同、多份标书撞车时",
    "evaluate": "要按评分办法模拟评标、找失分点时",
    "scoring": "要看评分项与分值分布时",
    "facts": "要查全书数字、名称、日期是否前后一致时",
    "verify_citations": "要查有没有无依据、不可核查的表述时",
    "report": "要出一份可提交的检查报告时",
    "skills": "要查看 / 启停 23 项检查技能时",
    "ai_check": "愿意等模型做一次全量 AI 复核时（慢，需要真实模型）",
    "ai_rejection": "规则抽取不够，要模型辅助抽废标条款时（慢）",
    "ai_deviation": "要模型辅助逐条对照做偏离表时（慢）",
    "ai_evaluate": "要模型逐评分项给失分原因时（慢）",
    "ai_polish": "要模型润色质疑 / 澄清函时（慢）",
    "ai_feasibility": "要生成可研报告式的大纲与正文时（慢）",
    "quote": "要校验报价表算术、合计、大小写、漏项时",
    "money": "要把金额转成中文大写时",
    "query_letter": "要针对体检问题生成质疑 / 澄清函时",
    "interpret": "要规则版抽取招标要点（不用模型、快）时",
    "layout": "要按目标页数分配每章字数时",
    "outline_import": "已有现成目录，要导入成章节树时",
    "bid_structure": "要解析标书结构并挂接评分项时",
    "tender_sections": "要切分招标章节并给重要性打分时",
    "cleaner": "要统计字数、做正文格式 lint 时",
    "kb_search": "要在资料 / 知识库里检索依据时",
    "textdiff": "要对比两个版本逐段差异时",
    "parser": "要规则版结构化抽取招标文件时",
    "docx_audit": "要审计 Word 版式（60+ 条规则）时",
    "docx_fix": "要按规则 ID 自动修复 Word 排版并出文件时",
    "restyle": "Word 无样式，要识别标题层级重排时",
    "formats": "要套用版式预设时",
    "htmlexport": "要出带封面目录的 HTML 版标书时",
    "table_export": "要把表格导成 Markdown / Word / Excel / PDF 时",
    "bundle": "要按技术 / 商务 / 资格 / 报价分册时",
    "import_bid": "要从已有标书反推章节结构时",
    "fill_template": "要扫描占位符并用事实字典填空时",
    "jobs": "要查看任务状态机 / checkpoint 时",
    "skill_run": "要单独运行一项检查技能时",
}
GROUP_WHEN = {
    "检查与合规": "要查合规、废标、暗标、雷同、评分或出检查报告时",
    "AI 增强": "明确要求 AI 深度复核且能接受等待时（需要真实模型）",
    "报价与函件": "涉及报价算术、金额大写、质疑澄清函时",
    "写作与规划": "要抽取招标要点、排大纲、检索依据、清洗正文时",
    "排版与交付": "要 Word 排版、分册、模板填空或导出其他格式时",
    "引擎": "要查看底层任务状态时",
}
AI_NEED_MODEL = core_bridge.AI_NEED_MODEL_CAPS
# 与 core_bridge 共用一份定义，界面目录和规划器看到的口径必须一致
PLACEHOLDER_CAPS = core_bridge.PLACEHOLDER_CAPS

FLOW_TOOLS: list[tuple] = [
    ("flow.compose", "一键写整本标书", "flow", "async",
     "用户要写整本 / 一键生成 / 按招标书出竞标书，且已有大纲时；必须放最后一步",
     "需要大纲；后台逐章生成，已有正文默认跳过"),
    ("flow.optimize", "按问题改写并复检", "flow", "async",
     "用户要优化 / 按问题改 / 修复 / 闭环 / 复检时；必须放最后一步",
     "需要已有标书正文与体检问题"),
    ("flow.check", "标书全面体检", "flow", "model",
     "用户明确要求重新体检 / 查问题时", "内含规则判定 + 模型复核 + 基座自动核查，较慢"),
    ("flow.export", "导出前门禁与下载", "flow", "local",
     "用户要导出 / 下载 Word / 交付标书时", "自动跑导出门禁后给下载链接"),
    ("flow.import_draft", "导入已有竞标书", "flow", "local",
     "本次上传的文件是已有竞标书 / 投标文件时", "按标题切章，保护章节原样保留"),
    ("flow.set_tender", "设为招标文件", "flow", "local",
     "本次上传的文件是招标文件 / 采购需求时", "写入项目并自动跑招标侧基座能力"),
    ("flow.add_material", "加入我提供的资料", "flow", "local",
     "本次上传的是资质、业绩、参数、承诺等依据材料时", "本项目资料优先级最高"),
    ("flow.add_kb", "加入企业素材库", "flow", "local",
     "用户明说要跨项目复用 / 进知识库时", "后续所有项目可检索"),
    ("flow.materials", "查看资料清单", "flow", "local",
     "用户问有哪些资料 / 依据 / 素材时", "只读，不改数据"),
]

AI_TOOLS: list[tuple] = [
    ("ai.interpret", "AI 解读招标文件", "ai", "model",
     "刚拿到招标文件、要抽出硬性要求驱动大纲与写作时", "需要招标文件"),
    ("ai.outline", "AI 生成大纲", "ai", "model",
     "要按招标要求和我的资料排章节时", "会保留已有章节标题，正文不错位"),
    ("ai.section", "AI 写 / 重写单章", "ai", "model",
     "用户只要求写某一章、或补某一章正文时", "args 可给 sid 或 title"),
    ("ai.rewrite", "AI 按指令改写某章", "ai", "model",
     "用户点名要改某章并给了修改要求时", "args 可给 instruction"),
    ("ai.answer", "AI 自由回答", "ai", "model",
     "用户在问问题、要解释、要建议时", "自动带上本次基座结论与资料检索结果"),
]


def _availability(name: str, cost: str) -> str:
    if name.startswith("base.") and name[5:] in PLACEHOLDER_CAPS:
        return "占位"
    return "需模型" if cost in ("model", "async") else "可执行"


def _build_tools() -> tuple[list[dict], dict[str, dict]]:
    tools: list[dict] = []
    for cap in core_bridge.CAPABILITIES:
        key = cap["key"]
        cost = "model" if key in AI_NEED_MODEL else "local"
        name = f"base.{key}"
        tools.append({
            "name": name, "label": cap["name"], "kind": "base",
            "group": cap["group"], "cost": cost,
            "availability": _availability(name, cost),
            "desc": cap["desc"],
            "when": BASE_WHEN.get(key, GROUP_WHEN.get(cap["group"], "相关检查时")),
        })
    for st in auto_core.STAGES:
        name = f"stage.{st['key']}"
        tools.append({
            "name": name, "label": "批量基座 · " + st["label"],
            "kind": "stage", "group": "自动编排", "cost": "local",
            "availability": "可执行",
            "desc": st["desc"], "when": "在" + st["trigger"] + "时，一次跑 " +
                     str(len(settings.stage_caps(st["key"]))) + " 项已启用的本地基座能力",
        })
    for name, label, kind, cost, when, desc in FLOW_TOOLS + AI_TOOLS:
        tools.append({"name": name, "label": label, "kind": kind,
                      "group": "主链路" if name.startswith("flow") else "模型链",
                      "cost": cost, "availability": _availability(name, cost),
                      "desc": desc, "when": when})
    return tools, {t["name"]: t for t in tools}


TOOLS, TOOL_MAP = _build_tools()


def catalog_text() -> str:
    """只把「已启用且允许自动挑选」的工具交给规划器。

    配置一改，模型下一句话看到的目录就变，不需要重启服务。
    """
    lines = []
    for t in TOOLS:
        if t["availability"] == "占位":
            continue
        if not settings.tool_selectable(t["name"]):
            continue
        lines.append(f"{t['name']} | {t['label']} | {t['kind']} | "
                     f"{t['availability']} | {t['when']}")
    return "\n".join(lines)


def catalog() -> dict:
    by_avail = {k: len([t for t in TOOLS if t["availability"] == k])
                for k in ("可执行", "需模型", "占位")}
    selectable = [t for t in TOOLS
                  if t["availability"] != "占位"
                  and settings.tool_selectable(t["name"])]
    return {"tools": TOOLS, "count": len(TOOLS),
            "base": len([t for t in TOOLS if t["kind"] == "base"]),
            "stage": len([t for t in TOOLS if t["kind"] == "stage"]),
            "ai": len([t for t in TOOLS if t["kind"] == "ai"]),
            "flow": len([t for t in TOOLS if t["kind"] == "flow"]),
            "executable": by_avail["可执行"], "needs_model": by_avail["需模型"],
            "placeholder": by_avail["占位"],
            "placeholder_names": [t["name"] for t in TOOLS
                                  if t["availability"] == "占位"],
            "selectable": len(selectable),
            "configured_out": len(TOOLS) - by_avail["占位"] - len(selectable),
            "plannable": len(selectable)}


# ---------------------------------------------------------------- 观察
TENDER_WORDS = ("招标文件", "采购文件", "采购需求", "需求书", "磋商文件", "询价文件",
                "招标公告", "投标邀请", "技术规格书", "投标人须知", "评标办法")
DRAFT_WORDS = ("投标文件", "竞标书", "投标书", "响应文件", "商务标", "技术标",
               "投标函", "投标报价", "法定代表人授权")
KB_WORDS = ("素材库", "知识库", "跨项目", "复用")


GUESS_CONFIDENCE_MIN = 0.60

# 附件入库是服务端确定性动作，不再由模型规划器决定「这次要不要入库」。
# 这四个名字同时用于从计划里剔除重复步骤、以及给前端/审计留一条可核对的执行轨迹。
STORE_TOOLS = {"tender": "flow.set_tender", "draft": "flow.import_draft",
               "material": "flow.add_material", "kb": "flow.add_kb"}


def guess_target_detail(message: str, attachment: dict | None) -> dict:
    """判断附件用途，并给出置信度与依据。

    用途判断错了会污染整条链（把投标书当招标书、把资料当正文），所以
    「我只是猜的」必须显式表达出来：低于阈值时主入口暂停执行、先反问用户。
    """
    if not attachment:
        return {"target": "", "confidence": 1.0, "source": "none",
                "reason": "本次没有附件"}
    chosen = str(attachment.get("target") or "").strip()
    if chosen in ("tender", "draft", "material", "kb"):
        return {"target": chosen, "confidence": 1.0, "source": "user",
                "reason": "已明确指定用途"}
    name = str(attachment.get("filename") or "")
    text = str(attachment.get("text") or "")[:1200]
    msg = str(message or "")
    if any(w in msg for w in KB_WORDS):
        return {"target": "kb", "confidence": 0.90, "source": "message",
                "reason": "指令里点明了素材库"}
    if any(w in msg for w in ("招标文件", "招标书", "采购文件", "采购需求")):
        return {"target": "tender", "confidence": 0.90, "source": "message",
                "reason": "指令里点明了招标文件"}
    if any(w in msg for w in ("投标书", "竞标书", "响应文件", "已有标书")):
        return {"target": "draft", "confidence": 0.90, "source": "message",
                "reason": "指令里点明了已有竞标书"}
    # 「投标邀请书 / 资格预审文件」是招标方发的文件，不是投标人写的标书；
    # 必须先于「含投标二字」的竞标书规则判断，否则会被切成章节当正文导入。
    if any(w in name for w in ("招标", "采购", "磋商", "询价", "需求", "公告",
                              "投标邀请", "资格预审", "投标人须知", "补遗")):
        return {"target": "tender", "confidence": 0.85, "source": "filename",
                "reason": "文件名含招标类特征"}
    if any(w in name for w in ("投标", "竞标", "响应文件", "标书")):
        return {"target": "draft", "confidence": 0.82, "source": "filename",
                "reason": "文件名含投标类特征"}
    head = name + "\n" + text[:400]
    n_t = sum(1 for w in TENDER_WORDS if w in head)
    n_d = sum(1 for w in DRAFT_WORDS if w in head)
    if n_t >= 3 or any(w in head for w in ("投标人须知", "评标办法", "招标编号")):
        return {"target": "tender", "confidence": 0.78, "source": "content",
                "reason": "正文含多处招标文件特征词"}
    if n_d >= 3 or any(w in head for w in ("投标函", "我方承诺", "法定代表人授权")):
        return {"target": "draft", "confidence": 0.78, "source": "content",
                "reason": "正文含多处投标文件特征词"}
    if n_t >= 2 or n_d >= 2:
        return {"target": "tender" if n_t >= n_d else "draft",
                "confidence": 0.62, "source": "content",
                "reason": "正文有少量特征词，判定不够稳"}
    return {"target": "material", "confidence": 0.35, "source": "fallback",
            "reason": "文件名与正文都没有可判定的用途特征"}


def guess_target(message: str, attachment: dict | None) -> str:
    """兼容旧调用：只取用途字符串。"""
    return guess_target_detail(message, attachment)["target"]


def observe(pid: str, message: str = "", attachment: dict | None = None,
            history: list[dict] | None = None) -> dict:
    """把项目当下压缩成规划器能读懂的状态。"""
    p = store.get_project(pid) or {}
    secs = p.get("sections") or []
    empty = [s for s in secs if not (s.get("text") or "").strip()
             and not s.get("protected")]
    checks = p.get("checks") or []
    last_round = max((c["round"] for c in checks), default=0)
    recent = [c for c in checks if c["round"] == last_round]
    last_auto = store.last_auto_run(pid, "check")
    meta: dict = {}
    try:
        meta = json.loads(p.get("summary") or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except (ValueError, TypeError):
        meta = {}
    guess_detail = guess_target_detail(message, attachment)
    flags = {
        "pid": pid,
        "budget_exhausted": guard.budget_exceeded(pid),
        "has_tender": bool((p.get("tender_text") or "").strip()),
        "tender_chars": len(p.get("tender_text") or ""),
        "has_interpret": bool(meta.get("must")),
        "has_outline": bool(p.get("outline")),
        "sections": len(secs),
        "empty_sections": len(empty),
        "protected_sections": sum(1 for s in secs if s.get("protected")),
        "has_draft_text": any((s.get("text") or "").strip() for s in secs),
        "materials": len(store.list_docs(scope="project", project_id=pid)),
        "kb_docs": len(store.list_docs(scope="kb")),
        "last_issues": len(recent),
        "high_issues": sum(1 for c in recent if c.get("level") == "高"),
        "last_auto": (last_auto.get("summary") or "")[:90],
        "mode": config.MODE,
        "attachment": bool(attachment),
        "attachment_name": str((attachment or {}).get("filename") or ""),
        "attachment_chars": len(str((attachment or {}).get("text") or "")),
        "guess_target": guess_detail["target"],
        "guess_detail": guess_detail,
    }
    # 本次就要入库的招标文件 / 已有标书，规划时按"已具备"看待
    if attachment and flags["guess_target"] == "tender":
        flags["has_tender"] = True
        flags["tender_chars"] = flags["attachment_chars"] or flags["tender_chars"]
    if attachment and flags["guess_target"] == "draft":
        flags["has_draft_text"] = True
    lines = [
        f"运行模式：{config.MODE}（{'可调用外部模型' if config.MODE == 'live' else '内置演示模型，模型不产生自由文本'}）",
        f"当前模型：{current_model_label()}",
        f"招标文件：{'已导入 ' + str(flags['tender_chars']) + ' 字' if flags['has_tender'] else '尚未导入'}"
        f"；已解读硬性要求：{'是' if flags['has_interpret'] else '否'}",
        f"标书章节：{flags['sections']} 章，其中待写正文 {flags['empty_sections']} 章，"
        f"保护章节 {flags['protected_sections']} 章；已有正文：{'是' if flags['has_draft_text'] else '否'}",
        f"大纲：{'已生成' if flags['has_outline'] else '尚未生成'}",
        f"我提供的资料：{flags['materials']} 份；企业素材库：{flags['kb_docs']} 篇",
        f"最近一次体检：{flags['last_issues']} 个问题（高风险 {flags['high_issues']} 个）",
    ]
    if flags["last_auto"]:
        lines.append("最近自动基座结论：" + flags["last_auto"])
    if attachment:
        lines.append(f"本次上传「{flags['attachment_name']}」{flags['attachment_chars']} 字，"
                     f"初判用途：{flags['guess_target']}")
    if history:
        flags["history_turns"] = len(history)
        prev = "；".join(
            f"{h.get('role') or 'user'}:{str(h.get('text') or h.get('reply') or '')[:90]}"
            for h in history[-6:])
        lines.append(f"服务端对话记忆 {len(history)} 条，最近的是（最新在后）：" + prev[:600])
    return {"flags": flags, "text": "\n".join(lines), "project": p,
            "sections": secs, "meta": meta}


# ---------------------------------------------------------------- 规划
PLAN_SYSTEM = """你是「TenderForge」的规划器。用户只说一句话（可能带文件），由你决定调用哪些工具完成目标。

可用工具目录（名称 | 中文名 | 类型 | 成本 | 什么时候用）：
{catalog}

项目当前状态：
{state}

硬性规则：
1. 只从目录里选工具名，原样输出，禁止编造；步骤 1 到 7 步，按依赖顺序排列。
2. 判定、抽取、核对、排版一律优先用 base.* / stage.*（本地确定性、毫秒级）；只有需要写措辞、解读、自由回答时才用 ai.*。
3. 用户要"写整本 / 一键生成 / 按招标书写竞标书"：先确保有招标文件与大纲（缺就先加 ai.interpret / ai.outline），最后一步必须是 flow.compose。
4. 用户要"优化 / 按问题改 / 复检"：最后一步必须是 flow.optimize；它之前可加 flow.check 让问题清单更新。
5. 用户要"验标 / 体检 / 查风险"：用 flow.check，必要时补 1~3 个相关 base.* 深挖（如 base.rejection / base.blind_text / base.quote）。
6. 用户只是提问、要解释或建议：用 ai.answer；若问题与风险 / 合规 / 废标有关，先在前面放一个相关 base.* 或 stage.check 提供事实，AI 会自动引用。
7. 点名了某项具体能力（偏离表、暗标、雷同、报价、质疑函…）就选对应的 base.*，不要绕去跑整条链路。
8. 只有状态里出现「本次上传」时才选 flow.set_tender / flow.import_draft / flow.add_material / flow.add_kb，并按"初判用途"选；没有新附件时绝不选这四个工具，招标文件 / 已有竞标书已在库中就直接解读或写作。只上传不写标书时不要选 flow.compose。
9. flow.compose 与 flow.optimize 是后台长任务，必须放在计划最后一步，之后不能再有步骤。
10. 报价表、投标函、承诺函、资质章节属于保护章节，任何工具都不会自动改写；不要安排改写它们的步骤。
11. {mode_note}
12. 不要安排 base.ai_* 这类全量 AI 复核能力，除非用户明确点名要 AI 深度复核；自由回答最多只安排 1 个 ai.answer。
13. 只是问风险 / 能不能提交 / 有什么问题时，用 stage.check 拿本地事实即可，不要安排 flow.check（它含模型复核、较慢）；只有用户明确说"重新体检""闭环改写"时才用 flow.check。
14. 状态里「招标文件尚未导入」且「已有正文：否」「我提供的资料：0 份」时，任何核对 / 写作 / 导出都做不了：不要排 flow.check / stage.check / base.* / flow.compose / flow.export，只排 flow.materials + ai.answer，说明需要用户先上传招标文件或填写正文。

只输出 JSON，不要解释、不要 Markdown 围栏：
{{"goal":"一句话目标","reasoning":"为什么这么排，40 字以内","steps":[{{"tool":"工具名","why":"选它的理由，20 字以内","args":{{}}}}]}}
args 只用这些键：target / query / text / sid / title / instruction / overwrite / rounds / key / stage / scope，值必须是字符串或布尔值或数字。
"""


def _normalize_plan(raw: dict, state: dict, message: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    steps: list[dict] = []
    for s in (raw.get("steps") or [])[:8]:
        if not isinstance(s, dict):
            continue
        name = str(s.get("tool") or "").strip()
        tool = TOOL_MAP.get(name)
        if not tool or not settings.tool_selectable(name):
            continue
        args = s.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        args = {k: v for k, v in args.items()
                if k in ARGS_ALLOWED and isinstance(v, (str, int, float, bool))}
        steps.append({"tool": name, "label": tool["label"], "kind": tool["kind"],
                      "cost": tool["cost"], "why": str(s.get("why") or "")[:60],
                      "args": args})
    if not steps:
        return None
    # 后台长任务必须是最后一步
    for i, s in enumerate(steps):
        if s["cost"] == "async":
            steps = steps[:i + 1]
            break
    # demo 模式不安排需要外部模型的工具，避免白等
    if state["flags"]["mode"] != "live":
        steps = [s for s in steps
                 if not (s["tool"].startswith("base.ai_") or
                         (s["tool"].startswith("ai.") and s["tool"] != "ai.answer"))]
        if not steps:
            return None
    return {"goal": str(raw.get("goal") or "")[:80],
            "reasoning": str(raw.get("reasoning") or "")[:120],
            "steps": steps, "planner": "model"}


def _model_plan(message: str, state: dict) -> dict | None:
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", PLAN_SYSTEM),
            ("human", "用户这句话：{message}\n\n请给出执行计划 JSON。"),
        ])
        chain = (prompt | chains.build_model(max_tokens=700, timeout=35, role="plan")
                 | StrOutputParser() | RunnableLambda(chains.safe_json))
        mode_note = ("当前可调用外部模型。" if state["flags"]["mode"] == "live"
                     else "当前是演示模式，不要选需要外部模型的工具。")
        t0 = time.time()
        fut = ctxpool.submit(_POOL, chain.invoke, {
            "catalog": catalog_text(), "state": guard.scrub(state["text"]),
            "message": guard.scrub(message or "（无文字指令）"), "mode_note": mode_note,
        })
        raw = fut.result(timeout=40)
        store.add_usage(state["flags"].get("pid", ""), actor="brain", task="plan",
                        tool="brain.planner", model=current_model_label(),
                        tokens_in=len(catalog_text() + state["text"]) // 2,
                        tokens_out=len(str(raw)) // 2,
                        elapsed_ms=int((time.time() - t0) * 1000), ok=True,
                        input_text=message[:120], output_text=str(raw)[:120])
        return _normalize_plan(raw, state, message)
    except Exception:
        return None


def _heuristic_plan(message: str, state: dict, attachment: dict | None) -> dict:
    """无模型时的本地规划器：同样输出结构化计划，界面与执行器不区分。"""
    f = state["flags"]
    m = (message or "").strip()
    steps: list[dict] = []

    def add(tool: str, why: str, **args) -> None:
        t = TOOL_MAP.get(tool)
        if t and settings.tool_selectable(tool) and not any(
                s["tool"] == tool for s in steps):
            steps.append({"tool": tool, "label": t["label"], "kind": t["kind"],
                          "cost": t["cost"], "why": why,
                          "args": {k: v for k, v in args.items() if v not in (None, "")}})

    # 1) 附件：自己判断用途并入库，再跑对应基座组
    if attachment:
        target = f["guess_target"] or "material"
        if target == "tender":
            add("flow.set_tender", "文件判定为招标文件，先入库", target="tender")
            add("stage.tender", "自动抽取硬性要求、废标条款与评分项")
        elif target == "draft":
            add("flow.import_draft", "文件判定为已有竞标书，按标题切章", target="draft")
            add("stage.draft", "导入后自动跑全面体检、废标、暗标与报价校验")
        elif target == "kb":
            add("flow.add_kb", "用户要跨项目复用，进企业素材库", target="kb")
            add("base.kb_search", "确认新素材已进入检索索引，后续所有项目可引用")
        else:
            add("flow.add_material", "文件判定为本项目依据资料", target="material")
            add("base.kb_search", "确认新资料可被检索，后续写作会优先引用")
        if not m:
            return {"goal": "导入文件并自动核查",
                    "reasoning": "只收到文件：按内容判用途，入库后跑对应基座能力",
                    "steps": steps, "planner": "local"}

    # 2) 单项基座能力点名，优先于主链路
    named = ""
    for word, key in CORE_WORDS:
        name = f"base.{key}"
        if word in m and name in TOOL_MAP and settings.tool_selectable(name):
            named = key
            break
    if (not named and "报价" in m
            and any(w in m for w in ("校验", "核对", "检查", "大写", "合计"))
            and settings.tool_selectable("base.quote")):
        named = "quote"
    if named and steps:
        add(f"base.{named}", "用户点名了这项能力")
        if named in ("rejection", "blind_text", "quote", "facts", "deviation"):
            add("stage.check", "顺带跑一组本地核对，避免只看单点")
        return {"goal": "运行指定基座能力", "reasoning": "点名了具体能力，直接调用并补充同组核对",
                "steps": steps, "planner": "local"}

    write = any(w in m for w in ("写整本", "整本", "一键", "全部生成", "生成全部", "写全部",
                                 "写标书", "生成标书", "出标书", "做一份标书", "写竞标",
                                 "生成竞标", "按招标书写", "帮我写", "成稿"))
    outline_only = any(w in m for w in ("大纲", "目录", "章节结构")) and not write
    check_only = any(w in m for w in ("体检", "验标", "检查", "审查", "查问题", "合规",
                                      "废标", "风险", "评分", "扣分项"))
    optimize = any(w in m for w in ("优化", "按问题改", "修复问题", "复检", "闭环",
                                    "改并复查", "改一下", "改写"))
    export = any(w in m for w in ("导出", "下载", "word", "Word", "docx", "交付"))
    materials = any(w in m for w in ("我的资料", "资料清单", "素材", "知识库", "依据有哪些",
                                     "有哪些资料", "哪些资料", "多少资料", "资料有哪些",
                                     "资料概况", "看看资料"))
    question = any(w in m for w in ("?", "？", "能不能", "有没有", "怎么", "如何", "为什么",
                                    "建议", "怎么样", "是否", "多少", "哪些"))

    if optimize:
        add("flow.check", "先更新问题清单，闭环才有可定位的锚点")
        add("flow.optimize", "按问题只改命中段落并自动复检")
        return {"goal": "按问题改写并复检", "reasoning": "用户要闭环：先体检定位，再改写复检",
                "steps": steps, "planner": "local"}

    if export:
        add("stage.export", "导出前自动跑占位符 / 废标 / 报价 / 暗标门禁")
        add("flow.export", "生成 Word 与 Markdown 下载")
        return {"goal": "导出标书", "reasoning": "交付前先跑门禁，再给下载",
                "steps": steps, "planner": "local"}

    if materials:
        add("flow.materials", "列出本项目资料与企业素材库")
        return {"goal": "查看依据资料", "reasoning": "只读查询，不改数据",
                "steps": steps, "planner": "local"}

    if write:
        if not f["has_tender"]:
            add("flow.materials", "还没有招标文件，先确认已有依据资料")
            add("ai.answer", "说明缺什么、以及现在能做什么")
            return {"goal": "写标书前置检查", "reasoning": "缺招标文件，不编造事实，先说明需要什么",
                    "steps": steps, "planner": "local"}
        if not f["has_interpret"]:
            add("ai.interpret", "先抽出硬性要求，约束后续写作")
        add("stage.tender", "本地抽取废标条款、需求覆盖与评分项")
        if not f["has_outline"]:
            add("ai.outline", "按招标要求和我的资料排章节")
        overwrite = any(w in m for w in ("覆盖", "重写", "重新生成", "全部重"))
        add("flow.compose", "按大纲逐章写完，已有正文默认跳过",
            **({"overwrite": True} if overwrite else {}))
        return {"goal": "根据招标书与资料写整本竞标书",
                "reasoning": "已有招标文件：先规则抽取约束，再排大纲，最后整本生成",
                "steps": steps, "planner": "local"}

    if outline_only:
        if f["has_tender"] and not f["has_interpret"]:
            add("ai.interpret", "先从招标文件抽硬性要求")
        add("ai.outline", "按招标要求和我的资料排章节")
        return {"goal": "生成大纲", "reasoning": "用户只要目录结构，不动正文",
                "steps": steps, "planner": "local"}

    if check_only:
        add("stage.check", "先跑一组本地基座规则拿到可核查结论")
        add("flow.check", "规则 + 模型复核 + 基座结论合并成问题清单")
        if any(w in m for w in ("废标", "报价", "暗标", "雷同")):
            key = "rejection" if "废标" in m else ("quote" if "报价" in m else
                  ("blind_text" if "暗标" in m else "dupcheck"))
            add(f"base.{key}", "用户特别点到的风险，单独深挖")
        return {"goal": "给标书做全面体检",
                "reasoning": "判定交给本地规则，AI 只补充措辞问题，结论并入问题清单",
                "steps": steps, "planner": "local"}

    if any(w in m for w in ("解读", "分析招标", "招标要求", "要点")):
        add("ai.interpret", "抽出硬性要求与资格条件")
        add("stage.tender", "本地补抽废标条款与评分项")
        return {"goal": "解读招标文件", "reasoning": "模型解读 + 规则抽取，结果驱动后续写作与体检",
                "steps": steps, "planner": "local"}

    if any(w in m for w in ("能做什么", "帮助", "怎么用", "功能", "你好")):
        add("flow.materials", "先给出现有依据资料概况")
        add("ai.answer", "说明当前能做什么")
        return {"goal": "说明能力", "reasoning": "只读 + 一次简短回答",
                "steps": steps, "planner": "local"}

    # 3) 自由提问：先看是不是风险类问题，是就先跑基座拿事实，再让 AI 引用回答
    if question and any(w in m for w in ("问题", "风险", "合规", "废标", "缺", "建议",
                                          "检查", "评审", "扣分", "能不能", "是否")):
        add("stage.check", "问题涉及风险，先让本地基座给出可核查结论")
    add("ai.answer", "结合资料与本次基座结论回答")
    return {"goal": "回答用户问题", "reasoning": "自由提问：资料检索 + 必要的基座事实，再交给模型措辞",
            "steps": steps, "planner": "local"}


# 单项能力关键词（用户点名时直接命中，不让模型绕路）
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
    ("分册", "bundle"), ("模板填空", "fill_template"),
]


PLANNER_LABELS = {"model": "模型规划", "local": "本地策略规划",
                  "degraded": "模型不可用 · 本地策略接管"}


def planner_label() -> str:
    """状态页用：如实说明当前这一轮到底由谁在挑工具。"""
    if config.MODE != "live":
        return "本地策略规划（演示模式）"
    if config.model_health().get("status") == "error":
        return "模型不可用 · 本地策略接管"
    return "模型规划"


def _mark_degraded(p: dict, reason: str) -> dict:
    p["planner"] = "degraded"
    p["fallback_reason"] = reason
    return p


# ---------------------------------------------------------------- 内容闸门
# 没有可核对的内容就不执行任何工具：空项目跑体检必然得到「0 个问题」，
# 那会被读成「没有废标风险」，比不查更危险。所以先要材料，再干活。
INLINE_MIN_CHARS = 150          # 直接贴在对话里的文字，够长才当「写了内容」
BID_ONLY_TOOLS = ("flow.export", "stage.export", "flow.optimize", "ai.rewrite")
# 这些能力的事实来源只有招标文件：没有招标文件，它们的结论必然是「0 条」
TENDER_ONLY_TOOLS = ("ai.interpret", "stage.tender", "base.rejection",
                     "base.tender_audit", "base.requirements", "base.scoring",
                     "base.deviation", "base.evaluate")
ANY_CONTENT_TOOLS = ("flow.compose", "ai.outline", "ai.section")
FREE_TOOLS = ("flow.materials", "ai.answer", "flow.set_tender", "flow.import_draft",
              "flow.add_material", "flow.add_kb", "base.kb_search", "base.skills",
              "base.formats", "base.template")
ACT_TOOLS = {"check": "stage.check", "optimize": "flow.optimize",
             "compose": "flow.compose", "export": "flow.export",
             "interpret": "ai.interpret", "outline": "ai.outline"}


def _content_kind(tool: str) -> str:
    """这一步需要哪类内容：bid=投标正文 / tender=招标文件 / any=任一 / check=招标文件或正文 / ''=不需要。"""
    if tool in FREE_TOOLS:
        return ""
    if tool in BID_ONLY_TOOLS:
        return "bid"
    if tool in TENDER_ONLY_TOOLS:
        return "tender"
    if tool in ANY_CONTENT_TOOLS:
        return "any"
    if tool.startswith(("base.", "stage.", "flow.")):
        return "check"
    return ""


def _content_state(state: dict, message: str, attachment: dict | None) -> dict:
    f = state.get("flags") or {}
    inline_chars = len(re.sub(r"\s", "", message or ""))
    return {
        "tender": bool(f.get("has_tender")),
        "bid": bool(f.get("has_draft_text")),
        "materials": bool(f.get("materials")),
        "attachment": bool(str((attachment or {}).get("text") or "").strip()),
        "inline_chars": inline_chars,
        "inline": inline_chars >= INLINE_MIN_CHARS,
    }


def _kind_ok(kind: str, cs: dict) -> bool:
    if kind == "bid":
        return cs["bid"]
    if kind == "tender":
        return cs["tender"]
    if kind == "any":
        return cs["tender"] or cs["bid"] or cs["materials"]
    return cs["tender"] or cs["bid"]


def _missing_note(blocked: list[dict], cs: dict) -> str:
    """部分步骤被砍掉时，先说清缺什么、结论少看了哪一半。"""
    labels = "、".join(list(dict.fromkeys(
        str(s.get("label") or s.get("tool")) for s in blocked))[:3])
    lacks = []
    if not cs["tender"]:
        lacks.append("招标文件")
    if not cs["bid"]:
        lacks.append("标书正文")
    lack = "、".join(lacks) or "对应内容"
    return (f"（提醒：项目里还缺{lack}，所以「{labels}」这一步没有跑——"
            "下面的结论只覆盖已有的部分，不能当成完整的废标结论。）")


# 计划里没排具体步骤、但目标本身要材料的（例如「写整本」缺招标文件时只排了查询）
GOAL_NEEDS_CONTENT = ("写标", "整本", "生成标书", "出标书", "体检", "验标", "核对",
                      "检查", "审查", "导出", "改写", "优化", "复检", "解读")
KEYS_TAIL = ("\n\n给我内容，马上继续（任选一种）：\n"
             "1. 点下面的「上传文件」，传招标文件或已有的投标文件（docx / pdf / xlsx / txt 都行），用途我自己判断；\n"
             "2. 到「标书正文」按章节把内容贴进去（自动保存），回来说一句「体检」；\n"
             "3. 只想先看看流程，说一句「你能做什么」。")


def _inline_hint(cs: dict) -> str:
    if not cs.get("inline"):
        return ""
    return (f"\n\n另外：你贴在对话框里的 {cs['inline_chars']} 字我没有当成标书正文——"
            "对话里的文字不入库、也没法按章核对。想核这段内容，可以把它存成 docx / txt 上传，"
            "或直接贴进「标书正文」。")


def _gate_reply(blocked: list[dict], cs: dict, mode: str = "check",
                what: str = "") -> str:
    labels = what or "、".join(list(dict.fromkeys(
        str(s.get("label") or s.get("tool")) for s in blocked))[:3])
    if mode == "bid":
        if cs["tender"]:
            head = "先停一下：项目里有招标文件，但还没有标书正文"
        else:
            head = "先停一下：项目里还没有标书正文"
        return (f"{head}，所以「{labels}」我一步都没跑。\n\n"
                "没有正文，导出会是一个空文件、改写也没有可改的段落——"
                "这种「已经完成」比直接报错更耽误事，我不给你这种结果。"
                + KEYS_TAIL + _inline_hint(cs))
    if mode == "write":
        if cs["tender"] or cs["materials"]:
            head = "先停一下：项目里有素材，但还没有招标文件"
        else:
            head = "先停一下：这个项目现在还是空的——没有招标文件，也没有我提供的资料"
        return (f"{head}，所以「{labels}」我一步都没跑。\n\n"
                "按这个状态写出来的正文只能是我编的：没有招标要求可对齐、没有素材可引，"
                "写成什么样都不算投这个项目的东西。我不编这个。"
                + KEYS_TAIL + _inline_hint(cs))
    # 核对类（体检 / 废标 / 风险）
    if not (cs["tender"] or cs["bid"] or cs["materials"]):
        head = "先停一下：这个项目现在还是空的——没有招标文件，也没有标书正文"
    elif not cs["tender"] and not cs["bid"]:
        head = "先停一下：项目里只有我提供的资料，既没有招标文件，也没有标书正文"
    elif not cs["tender"]:
        head = "先停一下：项目里有标书正文，但还没有招标文件"
    else:
        head = "先停一下：项目里有招标文件，但还没有标书正文"
    return (f"{head}，所以「{labels}」我一步都没跑。\n\n"
            "空着跑，结果一定是「0 个问题」——但那是没内容可查，不是没有废标风险，"
            "这种结论比不查更容易误导人，我不给你这个假答案。"
            + KEYS_TAIL + _inline_hint(cs)
            + "\n\n废标风险要两边对着看才判得准：招标文件（抽否决条款、资格与格式要求）"
              "+ 投标文件正文（逐条比对是否真的响应）。")


def _gate_cards(blocked: list[dict], cs: dict, what: str = "") -> list[dict]:
    labels = what or "、".join(list(dict.fromkeys(
        str(s.get("label") or s.get("tool")) for s in blocked))[:3])
    rows = [["这一步要做", labels],
            ["招标文件", "已导入" if cs["tender"] else "缺少"],
            ["标书正文", "已有正文" if cs["bid"] else "缺少"],
            ["我提供的资料", "已上传" if cs["materials"] else "暂无"]]
    return [{"type": "kv", "title": "本次没有执行的原因", "rows": rows},
            {"type": "cta", "title": "下一步", "tab": "write",
             "text": "把招标文件或标书正文放进来，再让我核对一遍；上传的文件只在本机解析。",
             "label": "去「标书正文」填写"}]


def content_gate(plan_data: dict, state: dict, message: str = "",
                 attachment: dict | None = None) -> dict | None:
    """执行前的材料闸门。

    None                → 放行（有内容，或这一步本来就不需要内容）
    {"reply","cards"}   → 全部该做的步骤都缺料：不执行，直接要材料
    {"note"}            → 部分步骤缺料：砍掉那几步，把缺什么写进回复
    """
    cs = _content_state(state, message, attachment)
    if cs["attachment"]:
        return None                       # 本次带文件，入库后就有内容
    goal = str(plan_data.get("goal") or "")
    graded = [(s, _content_kind(str(s.get("tool") or "")))
              for s in (plan_data.get("steps") or []) if s.get("tool")]
    graded = [(s, k) for s, k in graded if k]
    if not graded:
        # 计划里没有需要内容的步骤，但目标本身要材料（写整本 / 体检 / 导出缺料时
        # 规划器会退化成「先列资料 + 回答」）。完全空的时候照样拦住，提示要材料。
        if goal and any(w in goal for w in GOAL_NEEDS_CONTENT) and not (
                cs["tender"] or cs["bid"] or cs["materials"]):
            mode = "write" if any(w in goal + message for w in
                                  ("写", "整本", "生成", "大纲")) else "check"
            return {"reply": _gate_reply([], cs, mode, "帮你" + goal),
                    "cards": _gate_cards([], cs, "帮你" + goal),
                    "missing": ["tender", "bid"]}
        return None
    ok = [s for s, k in graded if _kind_ok(k, cs)]
    blocked = [s for s, k in graded if not _kind_ok(k, cs)]
    if not blocked:
        return None
    if not ok:
        kinds = {_content_kind(s["tool"]) for s in blocked}
        mode = ("bid" if kinds == {"bid"} else
                "write" if any(s["tool"] in ANY_CONTENT_TOOLS for s in blocked) else "check")
        return {"reply": _gate_reply(blocked, cs, mode), "cards": _gate_cards(blocked, cs),
                "missing": sorted(kinds)}
    drop = {id(s) for s in blocked}
    plan_data["steps"] = [s for s in plan_data["steps"] if id(s) not in drop]
    return {"note": _missing_note(blocked, cs)}


def content_gate_act(act: str, pid: str, message: str = "",
                     attachment: dict | None = None) -> dict | None:
    """旧快捷指令（页签 / 按钮）走确定性路由时，用同一套闸门。"""
    tool = ACT_TOOLS.get(act or "")
    if not tool:
        return None
    state = observe(pid, message, attachment)
    plan_data = {"steps": [{"tool": tool,
                            "label": (TOOL_MAP.get(tool) or {}).get("label", tool)}]}
    return content_gate(plan_data, state, message, attachment)


def plan(message: str, state: dict, attachment: dict | None) -> dict:
    live = state["flags"]["mode"] == "live"
    model_ready = (live and config.llm_config().ready
                   and config.outbound_allowed()
                   and config.model_health().get("status") != "error")
    if model_ready:
        p = _model_plan(message, state)
        p = p or _mark_degraded(_heuristic_plan(message, state, attachment),
                                "模型规划未返回或结果不可解析，已回退本地策略")
    elif live:
        p = _mark_degraded(_heuristic_plan(message, state, attachment),
                           "真实模型当前不可用（余额 / 密钥 / 网络），规划由本地策略接管")
    else:
        p = _heuristic_plan(message, state, attachment)
    if state["flags"].get("budget_exhausted"):
        # 预算护栏：本次只跑本地确定性的基座规则，需生成文字的部分显式留给人工
        p["steps"] = [s for s in p["steps"] if s.get("cost") == "local"]
        p = _mark_degraded(p, (p.get("fallback_reason") or "")
                           + "；当日 token 预算已用尽，本次只跑本地基座")
        p["goal"] = (p.get("goal") or "本地核查") + "（预算降级）"
        p["reasoning"] = ((p.get("reasoning") or "") +
                          "；需要生成的文字请人工补充。")
    return p


# ---------------------------------------------------------------- 执行
def _sources(pid: str) -> list[str]:
    mats = [f"我提供的资料·{m['title']}"
            for m in store.list_docs(scope="project", project_id=pid, usable_only=True)]
    kb = [f"企业素材库·{m['title']}" for m in rag.project_kb_rows(pid)][:3]
    return mats + kb


def _apply_attachment(pid: str, att: dict, target: str) -> dict:
    """把附件按智能体判定的用途入库。R05：解析不完整的资料带待核对状态。"""
    name = str(att.get("filename") or "未命名文件")
    text = str(att.get("text") or "")
    meta = parsers.meta_from_attachment(att)
    chars = int(meta.get("effective_chars") or 0)
    if not text.strip() or not chars or meta.get("parse_status") == "unusable":
        return {"ok": False, "summary": f"「{name}」没有解析出有效文字，未入库。", "cards": []}
    pm = att.get("page_map") or []
    scan = int(att.get("scan_pages") or 0)
    needs = bool(meta.get("needs_review"))
    store.save_pages(pid, target, name, pm)
    note = (f"⚠ {meta.get('review_note')}；这份资料在人工核对前不会进入依据与已确认要求。"
            if needs else
            f"{int(meta.get('ocr_pages') or 0)} 页走本地 OCR，页码可回跳。" if meta.get("ocr_pages") else "")
    page_row = ["页码坐标", f"{len(pm)} 页" + ("（含未识别页）" if scan else "")]
    review_row = ["解析状态", "待人工核对" if needs else "完整"]
    if target == "tender":
        # B04：不再用一个 tender_text 覆盖全部版本。首次为原文件，之后一律按补遗追加，
        # 旧自动要求转失效、人工修订标待复核；澄清 / 整份替换到版本面板显式选。
        kind = str(att.get("tender_kind") or "")
        if kind not in store.TENDER_KINDS:
            kind = "amend" if store.list_tender_versions(pid) else "original"
        r = tender.ingest(pid, kind, text, filename=name,
                          parse=dict(meta, filename=name))
        v = r.get("version") or {}
        label = tender.KIND_LABELS.get(kind, kind)
        return {"ok": True,
                "summary": (f"已把「{name}」（有效 {chars} 字）作为**招标文件第 "
                            f"{v.get('version', 1)} 版·{label}**保存，原条款保留。{note}"
                            f"旧自动要求转失效 {r.get('retired_requirements', 0)} 条，"
                            "相关正文、证据与检查需重新复核；导出确认已失效。"
                            "若是澄清或整份替换，请到「我的资料 · 招标文件版本」里显式选择。"),
                "cards": [{"type": "kv", "title": "入库结果", "rows": [
                    ["文件", name], ["用途", f"招标文件第 {v.get('version', 1)} 版·{label}"],
                    ["有效字数", str(chars)], page_row, review_row,
                    ["失效要求", str(r.get("retired_requirements", 0)) + " 条"]]}]}
    if target == "draft":
        r = agent.import_draft(pid, text)
        return {"ok": True,
                "summary": f"已把「{name}」判定为**已有竞标书**，切成 {r.get('chapters', 0)} 章；报价表等保护章节保留。{note}",
                "cards": [{"type": "kv", "title": "入库结果", "rows": [
                    ["文件", name], ["用途", "已有竞标书"],
                    ["章节", str(r.get("chapters", 0))], page_row, review_row]}]}
    if target == "kb":
        # 整本标书当范例（按章拆开，写作时参考同名章节），零散片段当通用素材
        is_bid = samples.looks_like_bid(text)
        doc = store.add_doc(name, text, scope="kb", page_map=pm,
                            kind="sample" if is_bid else "material", parse=meta)
        if is_bid:
            n = samples.ingest(doc["id"], text)
            label = f"标书范例（已按章拆成 {n} 章）"
        else:
            n = 0
            label = "通用素材"
        return {"ok": True,
                "summary": (f"已把「{name}」（有效 {chars} 字）放进知识库·{label}；"
                            "写标书时会参考它的章节结构与写法（不照抄事实）。" if is_bid
                            else f"已把「{name}」（有效 {chars} 字）放进知识库·通用素材，写标书时会检索引用。")
                           + note,
                "cards": [{"type": "kv", "title": "入库结果", "rows": [
                    ["文件", name], ["类型", "标书范例" if is_bid else "通用素材"],
                    ["有效字数", str(chars)]] + ([["章节", f"{n} 章"]] if is_bid else []) + [page_row, review_row]}]}
    store.add_doc(name, text, scope="project", project_id=pid, page_map=pm, parse=meta)
    return {"ok": True, "summary": f"已把「{name}」（有效 {chars} 字）判定为**我提供的资料**，生成标书时优先采用。{note}",
            "cards": [{"type": "kv", "title": "入库结果", "rows": [
                ["文件", name], ["用途", "我提供的资料"], ["有效字数", str(chars)],
                page_row, review_row]}]}


def store_attachment(pid: str, attachment: dict | None, guess: dict) -> dict | None:
    """一句话 + 一个文件：先把文件确定性入库，再让规划器决定「入库之后做什么」。

    以前是否入库由模型规划器决定，用户写「帮我分析一下」时模型往往只排分析工具，
    文件被反复退回、点几次发送都进不了资料库。入库是本地、零费用、可预期的动作，
    不该交给模型赌。用途判不准时按最保守的「我提供的资料」入库，不再反问卡住用户。
    """
    if not attachment or not str(attachment.get("text") or "").strip():
        return None
    explicit = str(attachment.get("target") or "").strip() in STORE_TOOLS
    target = str(guess.get("target") or "material")
    if target not in STORE_TOOLS:
        target = "material"
    confidence = float(guess.get("confidence") or 1.0)
    low = not explicit and confidence < GUESS_CONFIDENCE_MIN
    if low:
        target = "material"          # 猜错也不能把招标书当正文、或把资料当招标文件
    name = str(attachment.get("filename") or "这份文件")
    try:
        r = _apply_attachment(pid, attachment, target)
    except Exception as exc:         # 入库失败要明确告诉前端，附件留在输入框
        r = {"ok": False, "summary": f"「{name}」入库时出错：{str(exc)[:120]}", "cards": []}
    tool = STORE_TOOLS[target]
    ok = bool(r.get("ok"))
    summary = str(r.get("summary") or "")
    label = str((TOOL_MAP.get(tool) or {}).get("label") or "附件入库")
    return {
        "ok": ok, "target": target, "tool": tool, "summary": summary,
        "cards": list(r.get("cards") or []), "low_confidence": low,
        "confidence": confidence, "reason": str(guess.get("reason") or ""),
        "trace": {"index": 0, "tool": tool, "label": label, "kind": "flow",
                  "cost": "local", "why": "本次上传的文件按用途入库（确定性步骤）",
                  "ok": ok, "summary": summary[:220], "findings": 0, "high": 0,
                  "elapsed_ms": 0, "async": False,
                  "validation": "ok" if ok else "failed"},
    }


def _base_result(pid: str, key: str, message: str, args: dict) -> dict:
    payload = {"query": message, "text": message}
    payload.update({k: v for k, v in args.items() if k in ("query", "text", "key", "scope")})
    r = core_bridge.run(key, pid, payload)
    if not r.get("ok"):
        return {"ok": False, "summary": "执行失败：" + str(r.get("error") or "")[:100], "cards": []}
    cards: list[dict] = []
    stats = r.get("stats") or []
    if stats:
        cards.append({"type": "kv", "title": r["name"],
                      "rows": [[str(x.get("label")), str(x.get("value"))[:110]]
                               for x in stats[:8]]})
    for sec in (r.get("sections") or [])[:2]:
        items = [" · ".join(f"{k}:{str(v)[:55]}" for k, v in row.items())
                 for row in (sec.get("rows") or [])[:6]]
        if items:
            cards.append({"type": "list", "title": sec["title"], "items": items})
    dl = next((x for x in stats if x.get("label") == "download"), None)
    if dl:
        cards.append({"type": "link", "title": "导出", "url": str(dl["value"]),
                      "label": "下载生成的文件"})
    summary = str(r.get("summary") or f"已运行「{r['name']}」")[:160]
    if r.get("skipped"):
        summary += "（这一步需要真实模型）"
    rows = sum(len(s.get("rows") or []) for s in (r.get("sections") or []))
    return {"ok": True, "summary": summary, "findings": rows,
            "download": str(dl["value"]) if dl else "", "cards": cards}


def _stage_result(pid: str, key: str) -> dict:
    rep = auto_core.run_stage(key, pid, force=True)
    card = auto_core.as_card(rep)
    return {"ok": bool(rep.get("ok", True)),
            "summary": rep.get("summary") or "自动编排已关闭",
            "findings": int(rep.get("findings") or 0),
            "high": int(rep.get("high") or 0),
            "cards": [card] if card else []}


def _issue_items(pid: str, issues: list[dict]) -> list[dict]:
    p = store.get_project(pid)
    titles = {s["id"]: s["title"] for s in p.get("sections", [])}
    return [{"code": i.get("code", ""), "level": i.get("level", "中"),
             "title": i.get("title", ""), "detail": i.get("detail", ""),
             "section": titles.get(i.get("section_id", ""), "整本"),
             "anchor": i.get("anchor", ""), "source": i.get("source", "规则")}
            for i in issues]


def _flow_result(pid: str, name: str, args: dict, ctx: dict) -> dict:
    message = ctx["message"]
    att = ctx.get("attachment")
    # 规划器偶尔把「解读这份招标书」误判成「上传了招标文件」。没有新附件时这两个
    # 入库工具应幂等：数据已在库中就跳过，不报失败、不触发无谓的重规划。
    if name in ("flow.set_tender", "flow.import_draft") and not str(
            (att or {}).get("text") or "").strip():
        p = store.get_project(pid) or {}
        if name == "flow.set_tender" and str(p.get("tender_text") or "").strip():
            return {"ok": True, "summary": "招标文件已在项目中，本次没有新上传的文件，跳过重复入库。", "cards": []}
        if name == "flow.import_draft" and (p.get("sections") or []):
            return {"ok": True, "summary": "已有竞标书已在项目中，本次没有新上传的文件，跳过重复导入。", "cards": []}
    if name == "flow.set_tender":
        return _apply_attachment(pid, att or {}, "tender")
    if name == "flow.import_draft":
        return _apply_attachment(pid, att or {}, "draft")
    if name == "flow.add_material":
        return _apply_attachment(pid, att or {}, "material")
    if name == "flow.add_kb":
        return _apply_attachment(pid, att or {}, "kb")
    if name == "flow.materials":
        mats = store.list_docs(scope="project", project_id=pid)
        kb = store.list_docs(scope="kb")
        return {"ok": True,
                "summary": f"本项目 {len(mats)} 份我提供的资料，企业素材库 {len(kb)} 篇；生成与改写时本项目资料优先级最高。",
                "cards": [
                    {"type": "docs", "title": f"我提供的资料（{len(mats)}）", "items": [
                        {"title": d["title"], "chars": str(len(d["content"])),
                         "preview": d["content"][:90]} for d in mats]},
                    {"type": "docs", "title": f"企业素材库（{len(kb)}）", "items": [
                        {"title": d["title"], "chars": str(len(d["content"])),
                         "preview": d["content"][:90]} for d in kb]},
                ]}
    if name == "flow.check":
        issues = agent.check(pid, 1)
        high = sum(1 for i in issues if i["level"] == "高")
        auto_card = auto_core.as_card(agent.last_auto(pid))
        cards = ([auto_card] if auto_card else []) + [
            {"type": "kv", "title": "体检汇总", "rows": [
                ["问题总数", str(len(issues))], ["高风险", str(high)]]},
            {"type": "issues", "title": "问题清单（前 10 条）",
             "items": _issue_items(pid, issues)[:10]},
        ]
        return {"ok": True,
                "summary": f"体检完成：{len(issues)} 个问题，其中高风险 {high} 个；规则判定可复现，模型只补充措辞问题。",
                "findings": len(issues), "high": high, "cards": cards}
    if name == "flow.export":
        # 强制重跑，不吃 180 秒缓存；真正下载时后端还会再强制一次门禁
        rep = auto_core.run_stage("export", pid, force=True)
        gate = auto_core.as_card(rep)
        high = int(rep.get("high") or 0)
        note = (f"其中 {high} 条高风险，下载 Word 时会要求逐条确认后才放行。"
                if high else "无高风险，可直接下载。")
        return {"ok": True,
                "summary": f"导出前强制重跑 {rep.get('count', 0)} 项基座门禁，发现 "
                           f"{rep.get('findings', 0)} 条线索；" + note,
                "findings": int(rep.get("findings") or 0), "high": high,
                "cards": [gate,
                          {"type": "link", "title": "导出",
                           "url": f"/api/projects/{pid}/export.docx", "label": "下载 Word（.docx）"},
                          {"type": "link", "title": "导出",
                           "url": f"/api/projects/{pid}/export", "label": "下载 Markdown（.md）"}]}
    if name == "flow.compose":
        if not store.outline_confirmed(pid):
            return {"ok": False,
                    "summary": "目录还没确认：请先在「写标书」页核对章节并点「确认目录」，"
                               "再写整本，避免目录不对整本返工。",
                    "cards": []}
        jid, busy = store.create_job_exclusive("compose", pid)
        if busy:
            return {"ok": True, "async": True, "job_id": busy["id"], "kind": busy.get("kind"),
                    "summary": "这个项目的整本生成已经在排队 / 进行中，已接上原任务，不会重复写。",
                    "cards": [{"type": "job", "title": "后台任务", "job_id": busy["id"],
                               "kind": busy.get("kind") or "compose", "reused": True}]}
        store.submit_job(agent.compose, pid, jid, bool(args.get("overwrite", False)))
        return {"ok": True, "async": True, "job_id": jid, "kind": "compose",
                "summary": "已在后台开始整本生成（逐章写，单章失败不影响其余章节）。",
                "cards": [{"type": "job", "title": "后台任务", "job_id": jid, "kind": "compose"}]}
    if name == "flow.optimize":
        jid, busy = store.create_job_exclusive("optimize", pid)
        if busy:
            return {"ok": True, "async": True, "job_id": busy["id"], "kind": busy.get("kind"),
                    "summary": "这个项目已有一个写正文的任务在跑，已接上原任务；等它结束后再优化。",
                    "cards": [{"type": "job", "title": "后台任务", "job_id": busy["id"],
                               "kind": busy.get("kind") or "optimize", "reused": True}]}
        store.submit_job(agent.optimize, pid, jid, 2)
        return {"ok": True, "async": True, "job_id": jid, "kind": "optimize",
                "summary": "已启动闭环：体检 → 按问题只改命中段落 → 复检，最多 2 轮。",
                "cards": [{"type": "job", "title": "后台任务", "job_id": jid, "kind": "optimize"}]}
    return {"ok": False, "summary": "未知主链路动作", "cards": []}


def _local_answer(pid: str, ctx: dict) -> str:
    """演示模式下的自由回答：只陈述本次真实跑出来的事实，不编造、不假装模型生成。"""
    p = store.get_project(pid) or {}
    secs = p.get("sections") or []
    empty = sum(1 for s in secs if not (s.get("text") or "").strip() and not s.get("protected"))
    mats = store.list_docs(scope="project", project_id=pid)
    kb = store.list_docs(scope="kb")
    checks = p.get("checks") or []
    rnd = max((c["round"] for c in checks), default=0)
    recent = [c for c in checks if c["round"] == rnd]
    high = [c for c in recent if c.get("level") == "高"]
    lines = [f"先说结论：「{p.get('name', '')}」目前有招标文件 {len(p.get('tender_text') or '')} 字、"
             f"标书 {len(secs)} 章（待写正文 {empty} 章），本项目资料 {len(mats)} 份、"
             f"企业素材库 {len(kb)} 篇。"]
    if ctx.get("evidence"):
        lines += ["", "本次我实际跑过的基座核查：", str(ctx["evidence"])[:1200]]
    if recent:
        lines += ["", f"最近一次体检 {len(recent)} 个问题，其中高风险 {len(high)} 个。"]
        lines += [f"· [高] {c['title']}" for c in high[:3]]
    q = str(ctx.get("message") or "")
    if any(w in q for w in ("提交", "能交", "可以交", "风险", "能投")):
        lines += ["", "据此判断：高风险项未清零前不建议直接提交，可以说「按问题把标书改好并复检」。"]
    if config.MODE == "live":
        lines += ["", "这次模型回答没能在时限内返回，上面先给出已核查到的本地事实；稍后再问一次即可重新组织措辞。"]
    else:
        lines += ["", "以上都是本地基座规则跑出来的可核查结论。切到右上角「真实模型」后，"
                  "我会结合资料把每条问题解释清楚并给出改写建议。"]
    return "\n".join(lines)


def _ai_result(pid: str, name: str, args: dict, ctx: dict) -> dict:
    p = store.get_project(pid)
    if name == "ai.interpret":
        data = agent.interpret(pid)
        must = data.get("must", [])
        return {"ok": True, "summary": f"已解读招标文件，抽到 {len(must)} 条硬性要求。",
                "findings": len(must),
                "cards": [{"type": "kv", "title": "解读结果", "rows": [
                    ["项目", str(data.get("project", p["name"]))],
                    ["范围", str(data.get("scope", "—"))[:80]],
                    ["资格要求", "、".join(data.get("qualification", [])) or "—"]]},
                    {"type": "list", "title": "硬性要求", "items": must[:8]}]}
    if name == "ai.outline":
        chapters = agent.make_outline(pid)
        return {"ok": True, "summary": f"已生成 {len(chapters)} 章大纲，已有章节标题保留。",
                "findings": len(chapters),
                "cards": [{"type": "list", "title": f"大纲（{len(chapters)} 章）",
                           "items": [c["title"] for c in chapters]}]}
    if name in ("ai.section", "ai.rewrite"):
        secs = store.list_sections(pid)
        sid = str(args.get("sid") or "")
        title = str(args.get("title") or "")
        sec = next((s for s in secs if s["id"] == sid), None) or \
              next((s for s in secs if title and title in s["title"]), None)
        if not sec:
            sec = next((s for s in secs if not (s.get("text") or "").strip()
                        and not s.get("protected")), None)
        if not sec:
            return {"ok": False, "summary": "没有找到可写的章节，先生成大纲。", "cards": []}
        if sec.get("protected") or rules.is_protected(str(sec.get("title") or "")):
            return {"ok": False, "summary": agent.PROTECTED_MSG, "cards": []}
        busy = store.active_job(pid)
        if busy:
            # 后台正在整本写 / 优化：这时再单章写会和外层任务抢同一份正文
            label = "整本生成" if busy.get("kind") == "compose" else "优化改写"
            when = ("正在排队" if busy.get("status") == "queued"
                    else f"进行到 {int(busy.get('progress') or 0)}%")
            return {"ok": False,
                    "summary": f"「{label}」{when}，等它结束再单章{'生成' if name == 'ai.section' else '改写'}，"
                               "避免同一个项目两处同时写正文。",
                    "cards": []}
        if not store.outline_confirmed(pid):
            return {"ok": False,
                    "summary": "目录还没确认：请先在「写标书」页核对章节并点「确认目录」，"
                               "再生成或改写单章。",
                    "cards": []}
        if name == "ai.section":
            r = agent.generate_section(pid, sec["id"])
            text = str(r.get("text") or "")
        else:
            r = agent.rewrite_section(pid, sec["id"], str(args.get("instruction") or ""))
            text = str(r.get("text") or "")
        if r.get("error"):
            return {"ok": False, "summary": str(r["error"]), "cards": []}
        return {"ok": bool(text),
                "summary": f"已{'生成' if name == 'ai.section' else '改写'}「{sec['title']}」，{len(text)} 字。",
                "findings": 1 if text else 0,
                # 步间校验要能确认「真的产出了正文」，别把成功的生成误判成空手而归
                "text": text[:200],
                "cards": [{"type": "kv", "title": sec["title"], "rows": [
                    ["动作", "生成正文" if name == "ai.section" else "按指令改写"],
                    ["字数", str(len(text))],
                    ["依据", "、".join(r.get("used_materials", []))[:100] or "招标文件 + 资料库"]]}]}
    # ai.answer：把本次已跑出的基座结论作为事实喂给模型
    context = rag.materials_block(pid)[:1600]
    try:
        context += "\n\n" + rag.retrieve(pid, ctx["message"])[0][:1000]
    except Exception:
        pass
    if ctx.get("evidence"):
        context += "\n\n【本次智能体已核查的基座结论，回答时可引用】\n" + str(ctx["evidence"])[:800]
    if not (config.MODE == "live" and config.outbound_allowed()):
        answer = _local_answer(pid, ctx)
        return {"ok": True, "summary": answer[:400], "answer": answer, "cards": []}
    try:
        answer = str(chains.run("free", {"context": context,
                                         "question": ctx["message"] or "请介绍当前项目状态与可用能力"},
                                max_tokens=700, timeout=35,
                                pid=pid, actor="brain")).strip()
        if not answer:
            raise ValueError("模型返回空内容")
    except Exception as exc:
        return {"ok": False,
                "summary": f"模型回答未完成（{str(exc)[:90]}），已改用本地核查结论作答。",
                "cards": []}
    return {"ok": True, "summary": answer[:400], "answer": answer, "cards": []}


def _call(pid: str, step: dict, ctx: dict) -> dict:
    ctxpool.require_business_context(pid)
    name, args = step["tool"], step["args"]
    if name.startswith("base."):
        return _base_result(pid, name[5:], ctx["message"], args)
    if name.startswith("stage."):
        return _stage_result(pid, name[6:])
    if name.startswith("flow."):
        return _flow_result(pid, name, args, ctx)
    if name.startswith("ai."):
        return _ai_result(pid, name, args, ctx)
    return {"ok": False, "summary": "未知工具", "cards": []}


def _call_guarded(pid: str, step: dict, ctx: dict) -> dict:
    """单步独立超时：某一步被模型拖住时跳过它，不让整句话卡死。

    这里是所有工具执行的唯一出口：配置里停用 / 不允许自动挑选的能力，
    即使规划器、复盘器或兜底重规划把它排进来了，也不会真的执行。
    """
    tool, cost = step["tool"], step["cost"]
    if not settings.tool_selectable(tool):
        return {"ok": False, "cards": [],
                "summary": f"「{step.get('label') or tool}」已在设置中停用或不允许自动调用，本次未执行。"}
    slow = cost == "model" or tool.startswith("base.ai_")
    if tool == "ai.answer":
        limit = 50
    elif slow:
        limit = 75
    elif cost == "async":
        limit = 15
    else:
        limit = 25
    fut = ctxpool.submit(_RUN_POOL, _call, pid, step, ctx)
    try:
        return fut.result(timeout=limit)
    except FutureTimeout:
        return {"ok": False,
                "summary": f"这一步超过 {limit} 秒未完成，已跳过（模型忙或网络慢），可稍后单独再说一次。",
                "cards": []}
    except Exception as exc:
        return {"ok": False, "summary": f"{type(exc).__name__}: {str(exc)[:140]}", "cards": []}


FALLBACK_TOOLS = ("base.check_full", "ai.answer")


# ---------------------------------------------------------------- 闭环复盘
# 一次计划跑完后，把「实际执行结果 + 本地证据」回给模型，由它决定收工还是补跑。
# 受控边界：只能补白名单里的工具、不能重复、不能触发长任务，轮数与总步数都封顶。
LOOP_MAX_ROUNDS = 2
LOOP_MAX_STEPS = 12
LOOP_MAX_NEW = 3
LOOP_ALLOWED = ("base.", "stage.", "ai.answer", "ai.section")

LOOP_SYSTEM = """你是「TenderForge」的执行复盘器。用户只说了一句话，系统已按计划跑完一批工具；现在由你看**实际执行结果**决定：目标是否已达成，还差什么。

可用工具（只能从这里选，原样输出工具名）：
{catalog}

硬性规则：
1. 大多数情况目标已经达成：直接输出 decision=stop，不要为了凑步骤补跑工具。
2. 只有确实发现「该查的没查」「该答的没答」「某步失败且本地兜底没覆盖到」时才输出 continue，最多补 3 步。
3. 只能选 base.* / stage.* / ai.answer / ai.section；禁止 flow.*、ai.rewrite、占位工具，禁止整本生成、优化改写、导出。
4. 不要重复已执行过的工具；判定与抽取优先本地 base.* / stage.*，ai.answer 最多 1 个。
5. 报价表、投标函、承诺函、资质章节属于保护章节，不安排任何改写。
6. 若「生成质量自动核查」列出占位符、空章、无据数值等硬伤，且本轮确实有章节生成/改写动作，可用 ai.section 带 title 补写一次该章；只补一个章，不要重复整本生成。核查硬伤本身不算「该查的没查」，不要仅为此补跑判定工具。
7. {mode_note}

只输出 JSON，不要解释、不要 Markdown 围栏：
{{"decision":"stop|continue","reason":"30 字以内","steps":[{{"tool":"工具名","why":"15 字以内","args":{{}}}}]}}
args 只能用这些键：target / query / text / sid / title / instruction / key / stage / scope，值必须是字符串、数字或布尔值。
"""


def _args_key(args: dict | None) -> str:
    """工具签名 = 工具名 + 参数；同名工具换参数重跑是纠错，原地重跑才要拦。"""
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)


def _review_steps(raw: dict, done: set[tuple[str, str]], has_answer: bool,
                  mode: str) -> list[dict]:
    """复盘器给的新步骤也要过同一套白名单；从模型输出里筛出能安全执行的。"""
    out: list[dict] = []
    if not isinstance(raw, dict):
        return out
    for s in (raw.get("steps") or [])[:LOOP_MAX_NEW]:
        if not isinstance(s, dict):
            continue
        name = str(s.get("tool") or "").strip()
        tool = TOOL_MAP.get(name)
        if (not tool or tool["availability"] == "占位"
                or not settings.tool_selectable(name)):
            continue
        if not name.startswith(LOOP_ALLOWED):
            continue
        if name == "ai.answer" and has_answer:
            continue
        if mode != "live" and (name.startswith("base.ai_") or
                               (name.startswith("ai.") and name != "ai.answer")):
            continue
        args = s.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        args = {k: v for k, v in args.items()
                if k in ARGS_ALLOWED and isinstance(v, (str, int, float, bool))}
        signature = (name, _args_key(args))
        if signature in done:   # 同一工具、同一参数不原地打转；换参数重跑允许
            continue
        out.append({"tool": name, "label": tool["label"], "kind": tool["kind"],
                    "cost": tool["cost"], "why": str(s.get("why") or "")[:60],
                    "args": args})
        done.add(signature)
    return out


def _model_review(pid: str, message: str, goal: str, trace: list[dict],
                  evidence: str, answers: list[str], state: dict | None) -> dict | None:
    """把执行轨迹回给模型；任何异常都返回 None，由本地按原结果收工。"""
    try:
        guard.require_outbound(pid, "brain.loop")
        lines = [f"{t['index']}. {t['tool']} | {'成功' if t['ok'] else '失败'} | "
                 f"{t['summary'][:90]} | 校验={t.get('validation', '')}"
                 for t in trace if t.get("kind") not in ("loop", "replan")]
        prompt = ChatPromptTemplate.from_messages([
            ("system", LOOP_SYSTEM),
            ("human", "用户这句话：{message}\n原计划目标：{goal}\n\n"
                      "已执行步骤：\n{trace}\n\n本次核查到的本地事实：\n{evidence}\n\n"
                      "已产生的回答：{answers}\n\n当前项目状态：\n{state}\n\n"
                      "请判断是否可以结束，输出 JSON。"),
        ])
        chain = (prompt | chains.build_model(max_tokens=320, timeout=25, role="plan")
                 | StrOutputParser() | RunnableLambda(chains.safe_json))
        payload = {
            "catalog": catalog_text(),
            "mode_note": ("当前可调用外部模型。" if config.MODE == "live"
                          else "当前是演示模式，只能补本地基座工具。"),
            "message": guard.scrub(message or "（无文字指令）", limit=600),
            "goal": guard.scrub(goal, limit=200),
            "trace": guard.scrub("\n".join(lines), limit=2200),
            "evidence": guard.scrub(evidence, limit=1200),
            "answers": guard.scrub(" / ".join(a[:120] for a in answers[-2:]), limit=400),
            "state": guard.scrub((state or {}).get("text") or "", limit=900),
        }
        t0 = time.time()
        fut = ctxpool.submit(_POOL, chain.invoke, payload)
        raw = fut.result(timeout=30)
        store.add_usage(pid, actor="brain", task="loop", tool="brain.reviewer",
                        model=current_model_label(),
                        tokens_in=len(str(payload)) // 2,
                        tokens_out=len(str(raw)) // 2,
                        elapsed_ms=int((time.time() - t0) * 1000), ok=True,
                        input_text=message[:120], output_text=str(raw)[:120])
        decision = "continue" if str(raw.get("decision")) == "continue" else "stop"
        reason = str(raw.get("reason") or "")[:80]
        return {"decision": decision, "reason": reason, "raw": raw}
    except Exception as exc:
        store.add_usage(pid, actor="brain", task="loop", tool="brain.reviewer",
                        model=current_model_label(), ok=False,
                        output_text=f"复盘未生效：{type(exc).__name__}: {str(exc)[:100]}")
        return None


def _step_problem(step: dict, r: dict) -> str:
    """步间校验：明确失败，或该产出文字却空手而归，都算这一步没过。"""
    if not r.get("ok"):
        return str(r.get("summary") or "这一步未成功")[:120]
    tool = step["tool"]
    if tool in ("ai.section", "ai.rewrite") and not (r.get("text") or r.get("answer")):
        return "本步没有产出章节正文"
    if tool == "ai.answer" and not (r.get("answer") or r.get("summary")):
        return "本步没有产出回答内容"
    return ""


def execute(pid: str, plan_data: dict, ctx: dict,
            state: dict | None = None) -> dict:
    """逐步执行 → 步间校验 → 有上限本地兜底 → 把实际结果回模型复盘决定继续或收工。"""
    trace: list[dict] = []
    cards: list[dict] = []
    evidence: list[str] = []
    answers: list[str] = []
    seen_auto: set[str] = set()
    seen_signatures: set[tuple[str, str]] = set()
    job_id = ""
    steps = list(plan_data["steps"])
    replanned, replan_reason = False, ""
    loop_rounds = 0
    loop_events: list[str] = []
    # 只有模型规划且模型确实可用时才回看；降级轮次绝不假装在闭环
    loop_enabled = (plan_data.get("planner") == "model"
                    and config.MODE == "live"
                    and config.llm_config().ready
                    and config.outbound_allowed()
                    and config.model_health().get("status") != "error"
                    and not (state or {}).get("flags", {}).get("budget_exhausted"))
    i = 0
    while i < len(steps):
        step = steps[i]
        seen_signatures.add((step["tool"], _args_key(step.get("args"))))
        t0 = time.time()
        if (TOOL_MAP.get(step["tool"]) or {}).get("availability") == "占位":
            # 双保险：即使规划器幻觉出占位工具名，也不让它真的跑出「已执行」的假结论
            r = {"ok": False, "cards": [],
                 "summary": f"「{step['label']}」当前是占位能力，未接入实际执行，已跳过"}
        else:
            r = _call_guarded(pid, step, {**ctx, "evidence": "\n".join(evidence)})
        elapsed = int((time.time() - t0) * 1000)
        problem = _step_problem(step, r)
        # 同一阶段的自动基座卡片只留一张，避免链路内部重复跑时刷屏
        for c in (r.get("cards") or []):
            if c.get("type") == "auto":
                k = str(c.get("stage") or c.get("title") or "")
                if k in seen_auto:
                    continue
                seen_auto.add(k)
            cards.append(c)
        if r.get("answer"):
            answers.append(str(r["answer"]))
        if r.get("summary") and step["tool"].startswith(("base.", "stage.")):
            evidence.append(f"- {step['label']}：{r['summary'][:110]}"
                            + (f"（{r.get('findings', 0)} 条）" if r.get("findings") else ""))
        trace.append({
            "index": i + 1, "tool": step["tool"], "label": step["label"],
            "kind": step["kind"], "cost": step["cost"], "why": step["why"],
            "ok": bool(r.get("ok")), "summary": str(r.get("summary") or "")[:220],
            "findings": int(r.get("findings") or 0), "high": int(r.get("high") or 0),
            "elapsed_ms": elapsed, "async": bool(r.get("async")),
            "validation": problem or "ok",
        })
        if problem and not replanned:
            # 只重规划一次：放弃剩余步骤，改跑一组本地可核查的兜底工具，原因写进轨迹
            replanned, replan_reason = True, f"{step['tool']}：{problem}"
            done = {t["tool"] for t in trace}
            extra = []
            for name in FALLBACK_TOOLS:
                t = TOOL_MAP.get(name)
                if (t and name not in done and t["availability"] != "占位"
                        and settings.tool_selectable(name)):
                    extra.append({"tool": name, "label": t["label"],
                                  "kind": t["kind"], "cost": t["cost"],
                                  "why": "上一步未通过校验，改走本地兜底", "args": {}})
            dropped = max(0, len(steps) - i - 1)
            trace.append({
                "index": len(trace) + 1, "tool": "sys.replan", "label": "有上限重规划",
                "kind": "replan", "cost": "local", "why": replan_reason, "ok": True,
                "summary": f"放弃剩余 {dropped} 步，改跑 {len(extra)} 项本地兜底（本次唯一一次重规划）",
                "findings": 0, "high": 0, "elapsed_ms": 0, "async": False,
                "validation": "replanned",
            })
            steps = steps[:i + 1] + extra
        if r.get("async"):
            job_id = str(r.get("job_id") or "")
            break
        i += 1
        # 当前计划跑完：把「实际执行结果 + 本地证据」回给模型，由它决定收工还是补跑
        if (i >= len(steps) and loop_enabled and loop_rounds < LOOP_MAX_ROUNDS
                and not job_id and not guard.budget_exceeded(pid)):
            t0 = time.time()
            # 把「写出来的正文到底有没有硬伤」也作为本地事实回模型，
            # 让它能针对占位符 / 空章补一次章节，而不是只看工具成功与否。
            loop_evidence = "\n".join(evidence)
            q_mid_lines = quality.defect_lines(quality.assess_project(pid), limit=6)
            if q_mid_lines:
                loop_evidence += ("\n- 生成质量自动核查："
                                  + "；".join(q_mid_lines)[:420])
            review = _model_review(pid, str(ctx.get("message") or ""),
                                   str(plan_data.get("goal") or ""), trace,
                                   loop_evidence, answers, state)
            loop_rounds += 1
            new_steps: list[dict] = []
            if review and review["decision"] == "continue":
                new_steps = _review_steps(review["raw"], seen_signatures,
                                          bool(answers), config.MODE)
                new_steps = new_steps[:max(0, LOOP_MAX_STEPS - len(steps))]
            if review is None:
                ok, validation = False, "loop_unavailable"
                summ = "模型复盘未生效（未返回 / 超时 / 出域被拦），按原执行结果结束"
                loop_events.append("复盘未生效，已按原结果结束")
            elif review["decision"] == "continue" and not new_steps:
                ok, validation = True, "loop_blocked"
                summ = ("模型想继续，但补跑步骤被白名单 / 去重 / 总步数上限拦下，"
                        "按原结果结束")
                loop_events.append("模型提出的补跑步骤未通过受控校验")
            elif new_steps:
                ok, validation = True, "loop_continue"
                summ = (f"模型判定继续：{review['reason']}；补跑 {len(new_steps)} 步"
                        f"（第 {loop_rounds}/{LOOP_MAX_ROUNDS} 轮）")
                loop_events.append(f"第 {loop_rounds} 轮补跑 {len(new_steps)} 步")
                steps.extend(new_steps)
            else:
                ok, validation = True, "loop_stop"
                summ = (f"模型判定可以结束：{review['reason'] or '目标已达成'}"
                        f"（第 {loop_rounds}/{LOOP_MAX_ROUNDS} 轮）")
                loop_events.append(f"第 {loop_rounds} 轮判定可以结束")
            trace.append({
                "index": len(trace) + 1, "tool": "sys.loop",
                "label": "执行结果回模型复盘", "kind": "loop", "cost": "model",
                "why": "看实际跑出来的结果，决定收工还是补跑", "ok": ok,
                "summary": summ[:220], "findings": len(new_steps), "high": 0,
                "elapsed_ms": int((time.time() - t0) * 1000), "async": False,
                "validation": validation,
            })
    # 模型这一步超时 / 失败，或发生过重规划，都用已核查到的本地事实给出可用回答
    if not answers and (replanned or
                        any(t["tool"] == "ai.answer" and not t["ok"] for t in trace)):
        answers.append(_local_answer(pid, {"message": ctx.get("message", ""),
                                           "evidence": "\n".join(evidence)}))
    # 收工前对当前标书做一次生成质量硬指标复检，结果如实进卡片与回复
    q_final = quality.assess_project(pid)
    q_lines = quality.defect_lines(q_final, limit=5)
    if q_lines:
        cards.append({"type": "list", "title": "生成质量自检（自动硬指标）",
                      "items": q_lines})
    quality_summary = {
        "available": bool(q_final.get("ok", True)),
        # pass / fail / unavailable：没跑完不能当作「没问题」，回复里要单独说明
        "status": q_final.get("status") or ("pass" if q_final.get("all_passed") else "fail"),
        "error": str(q_final.get("error") or "")[:160],
        "all_passed": bool(q_final.get("all_passed")),
        "passed_count": int(q_final.get("passed_count") or 0),
        "total_checks": int(q_final.get("total_checks") or 4),
        "lines": q_lines,
        "defects": q_final.get("defects") or [],
    }
    return {"trace": trace, "cards": cards, "evidence": "\n".join(evidence)[:1800],
            "answers": answers, "job_id": job_id,
            "replanned": replanned, "replan_reason": replan_reason,
            "loop_rounds": loop_rounds,
            "loop_note": "；".join(loop_events),
            "quality": quality_summary}


# ---------------------------------------------------------------- 汇总回答
def _reply(plan_data: dict, run: dict, state: dict) -> str:
    trace = run["trace"]
    # 系统控制节点（重规划 / 复盘）不算用户可感知的执行步数
    real = [t for t in trace if t.get("kind") not in ("loop", "replan")]
    ok = [t for t in real if t["ok"]]
    n_base = sum(1 for t in ok if t["kind"] in ("base", "stage"))
    n_other = len(ok) - n_base
    lines = [f"我看了当前状态，判断「{plan_data['goal']}」——{plan_data['reasoning']}。",
             f"于是自己排了 {len(real)} 步：本地基座判定 {n_base} 项、模型与主链路 {n_other} 项，已跑完 {len(ok)} 步。"]
    failed = [t for t in real if not t["ok"]]
    if failed:
        lines += ["", f"有 {len(failed)} 步未完成（{failed[0]['label']}），已用本次核查到的本地结论继续作答，其余步骤不受影响。"]
    ai_answer = (run.get("answers") or [""])[-1]
    if ai_answer:
        lines.append("")
        lines.append(ai_answer)
    else:
        highlights = [f"· {t['label']}：{t['summary']}" for t in ok if t["summary"]][:5]
        if highlights:
            lines.append("")
            lines.extend(highlights)
    if any(t["async"] for t in trace):
        lines.append("")
        lines.append("长任务在后台跑，完成后我会把结果发回来；报价表、投标函、承诺函等保护章节不会自动改写。")
    if plan_data.get("planner") == "degraded":
        lines.append("")
        lines.append("（" + str(plan_data.get("fallback_reason") or "本轮由本地策略接管")
                     + "；本轮工具不是模型自主挑选的。）")
    if run.get("replanned"):
        lines.append("")
        lines.append("（有一步未通过校验，已重规划一次："
                     + str(run.get("replan_reason") or "")[:110] + "）")
    if run.get("loop_note"):
        lines.append("")
        lines.append("（" + str(run["loop_note"]) + "）")
    q = run.get("quality") or {}
    if q.get("status") == "unavailable":
        lines.append("")
        lines.append("（这一轮质量自检没有跑完："
                     + str(q.get("error") or "检查模块报错")[:110]
                     + "。所以「没发现问题」这句话现在不成立——导出会被拦住，"
                       "可以再让我体检一次，或人工核对一遍。）")
    elif q.get("available"):
        lines.append("")
        if q.get("all_passed"):
            lines.append(f"生成质量自检：{q.get('total_checks')} 项自动硬指标全部通过（只查明显硬伤，不代表评标结论）。")
        else:
            lines.append(f"生成质量自检：{q.get('passed_count')}/{q.get('total_checks')} 项通过，仍有硬伤——"
                         + "；".join(str(x) for x in (q.get("lines") or [])[:2])[:180]
                         + "。已如实列出，可在写标书页逐章修，或让我补写对应章节。")
    if state["flags"]["mode"] != "live":
        lines.append("")
        lines.append("（当前是演示模式，规划由本地策略完成；右上角切到「真实模型」后，由模型自己挑工具。）")
    return "\n".join(lines)


def current_model_label() -> str:
    if config.MODE != "live":
        return "内置演示模型（无外部调用）"
    cfg = config.llm_config()
    if not cfg.ready:
        return "未配置"
    reg = config.registry_public()
    names = {m["id"]: m["label"] for m in reg["models"]}
    bound = [f"{reg['role_labels'][r]}→{names[mid]}"
             for r, mid in reg["roles"].items() if mid in names]
    return (cfg.label or cfg.model) + ("（分工：" + "、".join(bound) + "）" if bound else "")


def _plan_card(plan_data: dict, run: dict) -> dict:
    planner = plan_data.get("planner", "local")
    return {"type": "plan", "title": "智能体执行轨迹",
            "goal": plan_data.get("goal", ""),
            "reasoning": plan_data.get("reasoning", ""),
            "planner": planner,
            "planner_label": PLANNER_LABELS.get(planner, "本地策略规划"),
            "fallback_reason": plan_data.get("fallback_reason", ""),
            "replanned": bool(run.get("replanned")),
            "replan_reason": run.get("replan_reason", ""),
            "loop_rounds": int(run.get("loop_rounds") or 0),
            "loop_note": run.get("loop_note", ""),
            "model": current_model_label(),
            "elapsed_ms": sum(t["elapsed_ms"] for t in run["trace"]),
            "items": run["trace"]}


def handle(pid: str, message: str = "", attachment: dict | None = None,
           history: list[dict] | None = None, force_planner: str = "") -> dict:
    """对话主入口：观察 → 规划 → 执行 → 模型复盘 → 汇总 → 留痕。"""
    # 记忆以服务端库为准；前端传的 history 只在库为空时作兼容兜底
    try:
        recalled = store.list_messages(pid, limit=12)
    except Exception:
        recalled = []
    if recalled:
        history = [{"role": m.get("role") or "user",
                    "text": str(m.get("text") or "")} for m in recalled]
    state = observe(pid, message, attachment, history)
    # 附件入库是确定性动作：不反问、不等模型决定，先落地再规划后续动作。
    # 用途判不准时按最保守的「我提供的资料」入库并在回复里说明，不让用户反复重发。
    stored = store_attachment(pid, attachment, state["flags"].get("guess_detail") or {})
    if force_planner == "local":
        plan_data = _heuristic_plan(message, state, attachment)
    elif force_planner == "model":
        plan_data = _model_plan(message, state) or _mark_degraded(
            _heuristic_plan(message, state, attachment),
            "模型规划未返回或结果不可解析，已回退本地策略")
    else:
        plan_data = plan(message, state, attachment)
    if stored:
        # 文件已经确定性入库；计划里再出现入库工具只会写第二份、再触发一次版本失效
        plan_data["steps"] = [s for s in plan_data["steps"]
                              if s.get("tool") not in STORE_TOOLS.values()]
        if not plan_data["steps"]:
            t = TOOL_MAP.get("ai.answer")
            if t:
                plan_data["steps"] = [{"tool": "ai.answer", "label": t["label"],
                                       "kind": t["kind"], "cost": t["cost"],
                                       "why": "文件已入库，回答这句话", "args": {}}]
    ctx = {"message": message, "attachment": attachment, "pid": pid}
    # 材料闸门：没有可核对的内容就不执行，先要材料（否则空跑出的「0 个问题」会被当成没风险）
    gate = content_gate(plan_data, state, message, attachment)
    if gate and gate.get("reply"):
        reply = str(gate["reply"])
        plan_obj = {"goal": "缺少可核对的内容，先要材料",
                    "reasoning": "项目里没有招标文件 / 标书正文，不空跑核对",
                    "planner": "local", "steps": []}
        try:
            store.save_agent_run(pid, message, plan_obj, [], reply)
        except Exception:
            pass
        return {"reply": reply, "action": "need_content",
                "need_content": {"missing": gate.get("missing") or [],
                                 "blocked": [s["label"] for s in plan_data["steps"]]},
                "planner": "local", "planner_label": "等待材料",
                "cards": gate.get("cards") or [], "sources": _sources(pid),
                "trace": [], "plan": plan_obj, "loop_rounds": 0, "loop_note": "",
                "quality": {}, "job_id": None, "fallback_reason": "",
                "attachment_stored": bool(stored and stored["ok"]),
                "attachment_target": (stored or {}).get("target", ""),
                "attachment_summary": (stored or {}).get("summary", "")}
    run = execute(pid, plan_data, ctx, state)
    if stored:
        # 入库轨迹排在最前：界面上能看见文件确实被写进了哪个库，而不是只凭回复文字
        run["trace"] = [stored["trace"]] + run["trace"]
        for i, t in enumerate(run["trace"]):
            t["index"] = i + 1
        run["cards"] = stored["cards"] + run["cards"]
    reply = _reply(plan_data, run, state)
    if stored:
        head = stored["summary"]
        if stored["ok"] and stored["low_confidence"]:
            head += (f"\n\n（用途没判准，把握只有 {stored['confidence']:.2f}：{stored['reason']}。"
                     "为避免猜错带偏整本标书，已按最保守的「我提供的资料」入库；"
                     "它不会当招标文件要求，也不会切成标书正文。若判错了，"
                     "在输入框「用途拿不准？手动指定」里选好用途，重新上传一次即可。）")
        reply = head + "\n\n" + reply
    if gate and gate.get("note"):
        reply = str(gate["note"]) + "\n\n" + reply
    # 用户点名了被停用的能力时，明确说出来，别让模型自由回答编一个「没法做」的理由
    for word, key in CORE_WORDS:
        if word in (message or "") and not settings.cap_enabled(key):
            cap = next((c for c in core_bridge.CAPABILITIES if c["key"] == key), None)
            name = cap["name"] if cap else word
            reply = (f"提示：「{name}」已在「设置」里停用，本次没有执行；"
                     f"需要用到的话到设置页启用它。\n\n" + reply)
            break
    cards = [_plan_card(plan_data, run)] + run["cards"]
    try:
        store.save_agent_run(pid, message, plan_data, run["trace"], reply)
    except Exception:
        pass
    return {"reply": reply, "action": "brain", "planner": plan_data["planner"],
            "planner_label": PLANNER_LABELS.get(plan_data["planner"], "本地策略规划"),
            "fallback_reason": plan_data.get("fallback_reason", ""),
            "replanned": bool(run.get("replanned")),
            "replan_reason": run.get("replan_reason", ""),
            "loop_rounds": int(run.get("loop_rounds") or 0),
            "loop_note": run.get("loop_note", ""),
            "quality": run.get("quality") or {},
            "history_turns": int(state["flags"].get("history_turns") or 0),
            "goal": plan_data["goal"], "cards": cards, "sources": _sources(pid),
            "job_id": run["job_id"] or None,
            "plan": {"goal": plan_data["goal"], "reasoning": plan_data["reasoning"],
                     "planner": plan_data["planner"],
                     "steps": [{"tool": s["tool"], "label": s["label"],
                                "kind": s["kind"], "why": s["why"]}
                               for s in plan_data["steps"]]},
            "trace": run["trace"],
            "attachment_stored": bool(stored and stored["ok"]),
            "attachment_target": (stored or {}).get("target", ""),
            "attachment_summary": (stored or {}).get("summary", "")}
