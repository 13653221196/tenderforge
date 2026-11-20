"""LangChain LCEL 链：解读 / 大纲 / 章节写作 / AI 复核 / 按问题改写 / 单章改写。

链结构统一为  ChatPromptTemplate | 模型 | 输出解析器。
演示模式与真实模式只替换「模型」节点，链本身、提示词、解析器完全相同——
这正是用 LangChain 抽象层的价值：换引擎不动业务。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_core.output_parsers import StrOutputParser

from . import config, guard, store

SYSTEM_BASE = """你是资深投标文件编制与合规审查专家。要求：
1. 只依据给定的招标文件、本项目资料、企业素材库作答，事实不得编造；
2. 涉及报价、资质、承诺函的内容不得自行改动；
3. 输出简洁、可核查，避免空泛承诺；
4. 不要自己写「依据 / 出处 / 页码 / 法规条号」，依据由系统单独绑定并存库；
5. 金额、比例、期限、数量等数值只能逐字引用给定资料或招标文件原文，
   不得自行计算、换算或编造；
6. 资料里没有的事实、数字、业绩、资质一律不要写；也不要在正文里写
   「未提供」「需人工补充」之类的占位文字，系统会把缺口单独列成待办。
"""


def safe_json(text: str) -> dict:
    """宽松 JSON 解析：去围栏、截大括号、去尾逗号。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    first, last = s.find("{"), s.rfind("}")
    if first >= 0 and last > first:
        s = s[first:last + 1]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


# ---------------- 模型节点 ----------------

class DemoChatModel(BaseChatModel):
    """离线演示模型：返回结构合规的样例输出，保证界面打开即可走通全链路。"""

    @property
    def _llm_type(self) -> str:
        return "bidagent-demo"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system = str(messages[0].content) if messages else ""
        human = str(messages[-1].content) if len(messages) > 1 else ""
        m = re.search(r"<<TASK:(\w+)>>", system)
        task = m.group(1) if m else "section"
        content = _demo_response(task, human)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _field(text: str, label: str, limit: int = 2000) -> str:
    m = re.search(rf"{re.escape(label)}[：:]\s*(.*?)(?=\n[^\n]*[：:]|\Z)", text, re.S)
    return (m.group(1).strip() if m else "")[:limit]


# 「本项目我提供的资料」这一段不止资料原文，还叠了事实边界、本章认领的
# 招标要求、检索覆盖说明、已核验企业事实与已写章节摘要。这些都是写给模型的
# 指令，不是可以写进标书的事实。以前整段按关键词捞，结果「不得编造证书编号」
# 「本章认领的招标要求（全表共 85 条…）」被当成本项目资料依据拼进 demo 正文，
# 并原样导出到 Word。这里按块边界把它们隔开。
_INTERNAL_BLOCK = re.compile(
    r"^【(?:事实边界|检索覆盖|已核验的企业事实|已写章节摘要|本章认领的招标要求|"
    r"本章篇幅预算|本章相关要求|作用域|写作边界|保护章节)[^】]*】")
_INSTRUCTION_LINE = re.compile(
    r"不得|不要|不臆造|不作推测|须逐条|本章认领|已核验|已写章节|检索覆盖|"
    r"篇幅预算|以原件为准|不重复展开|宁可|只能逐字|照抄|无法核查|缺依据时|"
    r"只学写法|人工核对|作用域约束|不参与判定|不作推测")
_FACT_LINE = re.compile(r"\d|有限公司|小时|分钟|万元|资质|验收|培训|认证|网点")


def _material_facts(human: str) -> list[str]:
    """只取「本项目我提供的资料」里**资料原文块**的事实句，跳过所有指令块。"""
    m = re.search(r"本项目我提供的资料[：:]\s*(.*?)(?=企业素材库命中[：:]|$)", human, re.S)
    if not m:
        return []
    facts: list[str] = []
    in_internal = False
    for raw in m.group(1).splitlines():
        line = raw.strip().lstrip("·-* \t").strip()
        if not line:
            continue
        if line.startswith("【"):
            # 资料块标题是上传文件名；内部指令块标题带固定前缀，整块跳过。
            in_internal = bool(_INTERNAL_BLOCK.match(line))
            continue
        if in_internal or _INSTRUCTION_LINE.search(line):
            continue
        if _FACT_LINE.search(line):
            facts.append(line)
            if len(facts) >= 6:
                break
    return facts


def _demo_response(task: str, human: str) -> str:
    if task == "interpret":
        return json.dumps({
            "project": _field(human, "项目名称") or "采购项目",
            "scope": "招标文件范围内全部货物、实施与服务",
            "must": ["按要求提供完整资格证明", "技术方案须逐条响应", "报价表须加盖公章",
                     "提供售后服务与运维承诺", "关键参数不得低于招标文件要求"],
            "qualification": ["营业执照", "相关资质证书", "类似项目业绩"],
            "summary": "已解析招标文件要点，建议按技术、商务、服务、报价、资格五条线组织响应。",
        }, ensure_ascii=False)

    if task == "outline":
        chapters = [{"title": t, "points": p} for t, p in [
            ("投标函及投标报价总说明", ["投标意愿", "报价依据", "有效期"]),
            ("投标人资格与商务响应", ["营业执照与资质", "类似业绩", "商务条款响应"]),
            ("技术方案与逐条响应", ["总体架构", "关键参数响应", "实施与验收"]),
            ("项目组织与实施计划", ["项目团队", "进度计划", "风险控制"]),
            ("售后服务与运维保障", ["响应时限", "服务内容", "培训计划"]),
            ("资格证明与承诺文件", ["资质清单", "法定代表人授权", "承诺书"]),
        ]]
        return json.dumps({"chapters": chapters}, ensure_ascii=False)

    if task == "review":
        # 演示模式不伪造「AI 复核」问题。确定性规则层已经给出可核查结论，
        # 再塞两条与本项目无关的固定问题会污染问题清单、导出门禁和影子比对。
        return json.dumps({"issues": [],
                           "note": "演示模式不产生 AI 建议；问题以规则与本地基座结论为准"},
                          ensure_ascii=False)

    if task == "fix":
        issue = _field(human, "问题")
        if "占位" in issue:
            return ("本系统采用前端采集、网络传输、集中存储与平台应用四层架构，"
                    "设备型号、数量及点位见附件 1《设备清单》，全部条目已填写完整并经内部复核。"
                    "主要设备附产品彩页与第三方检测报告，编号及页码在《技术规格响应表》中逐项列明。")
        if "负偏离" in issue:
            return ("关于系统拓扑图，我方随投标文件提交 CAD 源文件及 PDF 版本，"
                    "图纸编号 TY-2026-017-01，逐项标明前端点位、链路与存储节点，"
                    "并附设备清单与容量计算书供核查；本项与招标文件要求一致，偏离表中列为无偏离。")
        if "不可验证" in issue:
            return ("该项承诺量化为：7×24 小时热线受理，故障响应 30 分钟内，现场到达 4 小时内，"
                    "重大故障 24 小时内提交书面分析报告，提供 3 场现场培训与 3 年维保。"
                    "对应服务网点清单、6 名认证工程师资质证明及考核记录见附件 5，"
                    "未达标时的违约处理按合同条款执行。")
        if "材料" in issue or "核查" in issue:
            return ("关键参数逐项列于《技术规格响应表》，每条填写招标要求、投标响应、偏离情况"
                    "及证明材料编号与页码，附产品彩页、检测报告与容量计算书。"
                    "实施与验收阶段由项目经理复核，验收时提交加盖公章的响应对照表。")
        return ("本段依据招标文件要求及所附证明材料补充说明：所投产品关键参数、系统可用性 99.9%、"
                "录像保存 90 天等指标均在《技术规格响应表》中逐项对应，"
                "偏离情况如实列明，可供评标委员会逐条核查；未改动本章其他内容及报价、承诺文件。")

    if task == "free":
        return ("当前是内置演示模式，模型不产生自由文本。你可以点下方的快捷指令让我干活，"
                "或把右上角切到「真实模型」，我就能结合本项目资料自由回答。")

    # section / rewrite：按章节标题生成结构化正文，并把本项目资料里的事实写进去
    title = _field(human, "章节标题") or "本章"
    points = _field(human, "章节要点") or "按招标文件要求逐项响应"
    facts = _material_facts(human)
    facts_block = ""
    if facts:
        facts_block = "\n\n四、本项目资料依据（以下事实取自「我提供的资料」，未作推测）\n" + "".join(
            f"· {f[:120]}\n" for f in facts[:5])
    quantified = [f for f in facts if re.search(r"\d+\s*(?:分钟|小时|场|年|个|名|处)|响应|网点|培训", f)]
    promise = ("\n".join(f"· {f[:120]}" for f in quantified[:4]) if quantified else
               "服务响应时限、到场时限、培训场次与服务网点，以随投标文件提交的"
               "证明材料为准；本段不列未经本项目资料确认的具体数值。")
    return (
        f"一、编制依据\n"
        f"本章依据招标文件、本项目提供的资料以及企业素材库中的同类项目材料编制，"
        f"所引用的资质、业绩、参数与承诺均以可核查的原件为准。\n\n"
        f"二、{title}\n"
        f"我方已逐条核对招标文件对本部分的要求，并按「{points}」组织响应。"
        f"技术方案覆盖招标文件列明的全部功能点与关键参数，具体指标在响应表中逐项对应；"
        f"实施过程设置需求确认、到货验收、联调测试、试运行与最终验收五个控制节点，"
        f"每个节点形成书面记录并由双方签字确认。\n\n"
        f"三、保障与可核查性\n"
        f"以下承诺以可核查的原件为准，未从资料中取得的事实不作推测：\n{promise}\n"
        f"所有事实性表述均取自本项目资料与企业素材库，来源可在本项目记录中逐条回查。\n"
    ) + facts_block


def build_model(max_tokens: int | None = None,
                timeout: int | None = None,
                role: str = "") -> BaseChatModel:
    """按当前模式与环节返回模型节点。切换模型即刻生效，无需重建链。

    role: plan / write / review / free —— 可为不同环节接不同模型，留空用全局当前模型。
    max_tokens / timeout 可按链覆盖（例如自由回答只要短文、要快速失败）。
    """
    cfg = config.llm_config(role)
    # 数据出域总闸：未确认告知或用户关闭外发时，一律降级为本地模型，零字节出域
    if config.MODE != "live" or not cfg.ready or not config.outbound_allowed():
        return DemoChatModel()
    from langchain_openai import ChatOpenAI
    # Codex 系列只有 /responses 端点，且不接受自定义温度，这两处必须跟着协议走
    responses = (cfg.wire_api or "") == "responses"
    model = ChatOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        temperature=None if responses else 0.3,
        max_tokens=max_tokens or cfg.max_tokens,
        timeout=min(timeout or cfg.timeout, 60),
        max_retries=0,
        # 思考型模型：把注册表里的关闭思考参数透传到请求 body
        extra_body=cfg.extra_body or None,
        use_responses_api=responses,
    )
    return model


# ---------------- 链定义 ----------------

class _UsageCallback(BaseCallbackHandler):
    """从 LangChain 回调拿真实 token；模型不回报时由 run() 退化为估算。"""

    def __init__(self) -> None:
        self.tin = 0
        self.tout = 0

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            for group in response.generations:
                for gen in group:
                    meta = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
                    self.tin += int(meta.get("input_tokens") or 0)
                    self.tout += int(meta.get("output_tokens") or 0)
            if not self.tin and not self.tout:
                usage = (getattr(response, "llm_output", None) or {}).get("token_usage", {}) or {}
                self.tin += int(usage.get("prompt_tokens") or 0)
                self.tout += int(usage.get("completion_tokens") or 0)
        except Exception:
            pass


class TaskBudgetExceeded(RuntimeError):
    """单任务 token 用尽；调用方应降级为摘要 + 人工补充，不再调外部模型。"""


def run(task: str, variables: dict, json_out: bool = False,
        max_tokens: int | None = None, timeout: int | None = None,
        role: str = "", pid: str = "", actor: str = "agent",
        generation=None) -> Any:
    """执行一条 LCEL 链；默认按任务类型自动选该环节绑定的模型。

    输入在进提示词前统一过数据出域闸门（脱敏 / 限长 / 保护章节过滤），
    每次调用记账（token / 耗时 / 模型 / 触发者 / 输入输出摘要）。
    """
    b = config.budget()
    cap = int(b.get("per_task_token_limit") or 24000)
    if pid and config.MODE == "live" and b.get("enabled") \
            and store.task_tokens(pid) >= cap:
        store.add_usage(pid, actor=actor, task=task + ".budget", tool="guard.budget",
                        model="本次生成预算用尽", ok=False,
                        output_text="已降级为本地处理，本次不再调用外部模型")
        raise TaskBudgetExceeded(
            f"本项目生成预算 {cap} tokens 已用尽，本次生成降级为本地处理")
    original = dict(variables)
    variables = guard.guard_variables(variables)
    chain = _build(task, json_out, max_tokens, timeout, role,
                   generation=generation, original=original, retained=variables)
    cb = _UsageCallback()
    t0 = time.time()
    out: Any = None
    ok = True
    try:
        out = chain.invoke(variables, config={"callbacks": [cb]})
        if generation:
            generation.finish(out, "succeeded" if str(out or "").strip() else "failed")
    except Exception as exc:
        ok = False
        if generation:
            generation.finish(status="failed")
        if config.MODE == "live":
            config.record_model_error(exc, source="lcel." + task)
        raise
    else:
        # 模型恢复可用时，把状态页上的「不可用」提示撤掉
        if config.MODE == "live" and config.model_health().get("status") == "error":
            config.clear_model_error()
    finally:
        raw_chars = sum(len(str(v)) for v in variables.values())
        in_digest = " | ".join(str(v) for v in variables.values())[:160]
        out_digest = (str(out)[:160] if ok else "调用失败")
        tin = cb.tin or int(raw_chars / 2)
        tout = cb.tout or int(len(str(out or "")) / 2)
        label = "内置演示模型（估算）"
        if config.MODE == "live" and config.outbound_allowed():
            label = config.llm_config(role or TASK_ROLES.get(task, "")).label or "外部模型"
        store.add_usage(pid, actor=actor, task=task, tool="lcel." + task,
                        model=label, tokens_in=tin, tokens_out=tout,
                        elapsed_ms=int((time.time() - t0) * 1000), ok=ok,
                        input_text=in_digest, output_text=out_digest)
        if pid:
            store.add_task_tokens(pid, tin + tout)
    return out


# 每个环节可以单独指定模型，未指定则跟随全局当前模型
TASK_ROLES = {"interpret": "write", "outline": "write", "section": "write",
              "section_continue": "write",
              "rewrite": "write", "fix": "write", "review": "review", "free": "free"}


PROMPTS: dict[str, tuple[str, str, bool]] = {
    "interpret": (
        "输出 JSON：{{\"project\":\"项目名称\",\"scope\":\"范围\",\"must\":[\"硬性要求\"],"
        "\"qualification\":[\"资格要求\"],"
        "\"star_clauses\":[\"原文中的实质性要求 / 否决投标条款，逐条摘录，不要改写\"],"
        "\"scoring\":[{{\"item\":\"评分项名称\",\"score\":\"分值\",\"rule\":\"评审规则\"}}],"
        "\"summary\":\"一句话结论\"}}。"
        "评分项与分值必须来自招标文件评标办法，没有就给空数组，不要编造。",
        "项目名称：{name}\n\n招标文件全文：\n{tender}", True),
    "outline": (
        "输出 JSON：{{\"chapters\":[{{\"title\":\"章节名\",\"points\":[\"要点\"]}}]}}，6 到 8 章，含报价与资格章节。"
        "若给了「范例目录」，章节颗粒度与顺序参照它，标题按本项目改写。",
        "项目名称：{name}\n招标文件要点：{summary}\n硬性要求：{must}\n"
        "本项目资料：{materials}\n范例目录：{sample_titles}", True),
    "section": (
        "只输出本章正文，不要标题、不要解释。按「章节要点」与「本章相关要求」逐点展开，"
        "每点 250—500 字，本次约 {words} 字，分点分段、写明措施与验收口径；"
        "涉及参数、指标、清单、响应对照时用简单 Markdown 表格呈现"
        "（首行表头、第二行 --- 分隔，表格前后各留一个空行），不要把表格写成流水句；"
        "篇幅要求高的章节先写满结构与措施，不要用重复内容凑字数。"
        "「本章相关要求」里的每一条都要有明确响应，不得只写「完全满足」这类空泛承诺；"
        "「用户补充要求」是这一章必须写到的内容，优先满足，但不得违反招标文件与已提供资料；"
        "数值只能逐字引用「招标文件要求」或「本项目我提供的资料」里的原文。"
        "「本项目我提供的资料」末尾若含检索覆盖说明、企业事实与已写章节摘要，"
        "那是作用域约束：不重复已写章节的内容，不改动前面定过的参数与承诺，"
        "未展示的招标条款不得臆造。如果给了「范例章节」，按范例的层次与小标题拆法"
        "组织内容，但只学写法：公司名、人名、金额、案例、证书编号一律不得照抄。",
        "项目名称：{name}\n章节标题：{title}\n章节要点：{points}\n本次目标字数：{words}\n"
        "用户补充要求（优先满足）：\n{brief}\n"
        "招标文件要求：\n{tender}\n本项目我提供的资料：\n{materials}\n"
        "企业素材库命中：\n{kb}\n范例章节（学结构，不抄事实）：\n{sample}", False),
    "section_continue": (
        "这是同一章正文的续写。只输出接下去的内容，不要标题、不要解释、不要重复上文，"
        "不要写「（续）」或总结语。沿用上文的小标题层级与编号继续往下写，"
        "本次约 {words} 字，重点覆盖还没展开的章节要点；"
        "「用户补充要求」还没写到的部分在续写里补齐，但不得违反已写内容；"
        "「本章已写要点」里列过的小标题和数字，只能继续沿用、不得改写或反转；"
        "已经开始写的小节要接着写完，不要另起一套结构；"
        "续写里出现参数对照、指标清单时，同样用简单 Markdown 表格呈现。"
        "招标文件要求里尚未响应的条款优先响应，响应口径必须与上文一致，"
        "数值只能逐字引用给定资料，不得新增未展示的资质、业绩或承诺。",
        "项目名称：{name}\n章节标题：{title}\n尚未展开完的要点：{points}\n本次目标字数：{words}\n"
        "用户补充要求（优先满足）：\n{brief}\n"
        "已写部分的结尾（不要重复，从这里接着写）：\n{tail}\n"
        "本章已写要点（已写过的不再展开，数字与承诺保持一致）：\n{written}\n"
        "招标文件要求：\n{tender}\n本项目我提供的资料：\n{materials}\n"
        "企业素材库命中（可继续引用，不必重复展示）：\n{kb}", False),
    "rewrite": (
        "只输出改写后的本章正文，不要标题、不要解释。"
        "「依据资料」里的本章相关招标原文与本章相关要求必须逐条呼应，不得与之冲突，"
        "也不得新增资料里没有的数值、资质、业绩或承诺。"
        "有范例章节时按它的层次组织，但不得照抄其中的公司名、金额与案例。",
        "项目名称：{name}\n章节标题：{title}\n修改要求：{instruction}\n"
        "依据资料：\n{materials}\n范例章节（学结构，不抄事实）：\n{sample}\n原文：\n{text}", False),
    "review": (
        "输出 JSON：{{\"issues\":[{{\"level\":\"高|中|低\",\"title\":\"问题\",\"detail\":\"原因\","
        "\"section\":\"所在章节名\",\"evidence\":\"原文片段\"}}]}}，最多 4 条，无问题返回空数组。",
        "招标文件要求：\n{tender}\n\n投标文件各章节：\n{draft}", True),
    "fix": (
        "只输出改写后的该段落正文，不要标题、不要解释、不要改动其他内容。",
        "章节：{title}\n问题：{issue_title}（{level}）\n问题说明：{issue_detail}\n"
        "命中段落原文：\n{anchor}\n可用依据：\n{evidence}", False),
    "free": (
        "结合给定资料回答用户的问题，资料里没有的事实不要编造；先给结论再列要点，控制在 300 字以内。",
        "本项目资料与知识库：\n{context}\n\n用户问题：{question}", False),
}


def _build(task: str, json_out: bool,
           max_tokens: int | None = None, timeout: int | None = None,
           role: str = "", generation=None, original=None, retained=None):
    system_extra, human, _ = PROMPTS[task]
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"<<TASK:{task}>>\n{SYSTEM_BASE}\n{system_extra}"),
        ("human", human),
    ])
    model = build_model(max_tokens=max_tokens, timeout=timeout,
                        role=role or TASK_ROLES.get(task, ""))
    # 超时与重试已在模型节点统一控制；这里不叠加 with_retry，避免单次调用被放大到数分钟。
    tail = RunnableLambda(safe_json) if json_out else StrOutputParser()
    if generation:
        def capture(value):
            messages = [{"role": m.type, "content": m.content} for m in value.to_messages()]
            generation.capture(task, original, retained, messages,
                               {"type": model._llm_type,
                                "id": getattr(model, "model_name", "demo"),
                                "role": role or TASK_ROLES.get(task, "")})
            return value
        return prompt | RunnableLambda(capture) | model | StrOutputParser() | tail
    return prompt | model | StrOutputParser() | tail
