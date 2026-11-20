"""章节分型：决定「这一章能不能交给模型自由写」。

真实试点暴露的教训：模型写「资格证明文件」时输出了「我方已取得资格预审结果
通知书」——事实上系统里没有任何证照原件，这类既成事实承诺在投标里就是虚标。
行业通行的边界是「AI 起草 / 人类决策」：方案类可以让模型写，资格、证照、
承诺、报价只能由人核验原件后填写。

因此把章节分成三型：
  protected  证据/表单类，生成与改写一律不碰，人工填写原件；
  structured 资格/商务响应类，只做「要求 → 材料 → 状态」的确定性清单草稿，
             不调模型，绝不写「已取得 / 已具备」这类既成事实；
  narrative  技术方案/实施/服务类，允许 RAG 生成，但注入事实边界约束。

本模块不写库、不调模型，只做分类、清单渲染与措辞风险识别。
"""
from __future__ import annotations

import re

from . import rules

PROTECTED = "protected"
STRUCTURED = "structured"
NARRATIVE = "narrative"

# 比 config.PROTECTED_KEYWORDS 更严的一层：这些章节的内容必须是原件/表单，
# 模型连「草稿」都不该写。只在生成链路生效，不改全局保护判定，避免影响
# 验标脱敏、表单核验等既有行为。
PROTECT_MORE = (
    "资格证明", "资格文件", "资格审查", "资质证明", "资质文件", "证照",
    "法定代表人授权", "授权委托书", "投标保证金", "开标一览", "资格预审",
    "营业执照", "承诺函", "投标函", "报价",
)
# 这类章节可以生成，但只能是清单/对照表，不能自由发挥成叙述性承诺
STRUCTURED_WORDS = (
    "资格", "商务响应", "符合性", "业绩", "偏离表", "偏差表", "响应表", "格式",
    "签署", "盖章", "证件", "承诺", "资质", "清单", "应答", "凭证",
    # 招标文件「投标文件格式」目录里的原件类条目：报告 / 说明 / 规划 / 分析表
    # 都要求随投标文件提交具体材料，不能让模型替人编内容。
    "报告", "说明", "规划", "保障措施", "分析表", "关系", "补充文件", "文件",
)

# 「我方已取得 X」式既成事实。X 只能是外部原件能证明的东西，系统里没有原件
# 就不该出现。命中不等于一定错（可能确实已核验），但必须进入人工复核队列。
CLAIM_RE = re.compile(
    r"(?:我方|本公司|我司|本投标人|投标人)[^。；;\n]{0,14}"
    r"(?:已|已经|业已|均已|现|全部)?\s*"
    r"(?:取得|具备|获得|通过|持有|拥有|完成|取得并持有)"
    r"[^。；;\n]{0,36}"
    r"(?:资格预审|资格|资质|认证|证书|许可|授权|执照|检测报告|业绩|通知书|证明)")
# 明确否定的说法不该被当成编造，例如「未取得原件前不得表述为已取得」
NEGATION_RE = re.compile(r"(?:未|没有|不得|不可|尚未|禁止|不构成|不能)")


def classify(sec: dict) -> str:
    """返回章节类型。显式 protected 字段优先级最高。"""
    if sec.get("protected") or rules.is_protected(str(sec.get("title") or "")):
        return PROTECTED
    title = str(sec.get("title") or "")
    if any(k in title for k in PROTECT_MORE):
        return PROTECTED
    if any(k in title for k in STRUCTURED_WORDS):
        return STRUCTURED
    return NARRATIVE


def is_protected(sec: dict) -> bool:
    return classify(sec) == PROTECTED


def guard_text(kind: str = NARRATIVE) -> str:
    """注入提示词的事实边界。structured 章节不调模型，这里只服务 narrative。"""
    return (
        "\n【事实边界（硬性，违反即判为不可交付）】\n"
        "1. 不得写「我方已取得 / 已具备 / 已通过 / 持有」资质证书、资格预审结果、"
        "授权书、检测报告、业绩、认证等既成事实，除非「已核验的企业事实」里"
        "逐字列出了该项；缺依据时只写「按招标文件要求提供……（以随投标文件"
        "提交的原件为准）」这类可核查表述。\n"
        "2. 不得编造证书编号、证照有效期、业绩数量与名称、金额、承诺函内容、"
        "授权范围、人名与公章信息。\n"
        "3. 报价、保证金、投标有效期、响应时限等数值只能逐字引用招标文件要求"
        "或本项目资料原文，不得推算或按行业惯例补齐。\n"
        "4. 本章不替资格证明类章节下结论，涉及原件的事项一律标注为「以原件为准」。\n"
    )


def claim_violations(text: str, *, limit: int = 6) -> list[str]:
    """找出「无原件支撑的既成事实承诺」，供自检与定向重写使用。"""
    out: list[str] = []
    for m in CLAIM_RE.finditer(str(text or "")):
        hit = m.group(0).strip()
        # 句子里若带否定词，多半是在声明边界，不是承诺
        s = max(0, m.start() - 24)
        ctx = str(text)[s:m.start()]
        if NEGATION_RE.search(ctx):
            continue
        if hit not in out:
            out.append(hit)
        if len(out) >= limit:
            break
    return out


def _req_lines(requirements: str, *, limit: int = 18) -> list[str]:
    """把「本章认领的招标要求」拆成清单条目，去掉内部标注符号。"""
    lines: list[str] = []
    for raw in str(requirements or "").splitlines():
        s = raw.strip().lstrip("-·* ")
        if not s or s.startswith("【") and s.endswith("】"):
            continue
        s = re.sub(r"^\[[^\]]{0,24}\]\s*", "", s).strip()
        if len(s) < 6:
            continue
        lines.append(s[:180])
        if len(lines) >= limit:
            break
    return lines


def _table_cell(value: str, limit: int = 180) -> str:
    """Markdown 表格单元格：竖线会切断列，换行会切断行，一律先压平。"""
    flat = re.sub(r"\s+", " ", str(value or "").replace("|", "｜"))
    return flat.strip()[:limit]


def _table_columns(title: str) -> tuple[list[str], str]:
    """按章节用途选对照表列头；只给格式，金额与响应内容仍由人工填写。

    招标文件的投标文件格式里，偏差表 / 逐条应答表 / 报价表 / 单价分析表
    都是真表格，过去被渲染成纯文字清单，导出的 Word 一个表格都没有。
    """
    t = str(title or "")
    if any(k in t for k in ("报价", "单价", "开标一览", "清单")):
        return (["序号", "招标要求（引自招标文件）", "报价项 / 原件",
                 "金额（人工填写）", "核验状态"],
                "待人工核验原件后填写报价项名称与依据；金额不得由系统推算")
    if any(k in t for k in ("偏差", "应答", "响应表", "偏离")):
        return (["序号", "招标要求（引自招标文件）", "投标响应（人工填写）",
                 "偏离情况", "核验状态"],
                "待人工核验后逐条填写实际响应；偏离情况如实填入，不得留空")
    return (["序号", "招标要求（引自招标文件）", "响应材料",
             "偏离情况", "核验状态"],
            "待人工核验原件后填写材料名称、出具单位与所在页码")


def structured_draft(title: str, points: str = "", requirements: str = "",
                     facts: str = "") -> str:
    """资格/商务响应类章节的确定性清单草稿。

    刻意不调模型：这一章的价值是「哪条要求对应哪份原件」，模型写得再流畅
    也无法替人确认证照真伪。输出里所有未完成项都标成待人工核验，不写任何
    「已取得」式承诺，也不留会触发占位符门禁的内部标记。
    """
    t = str(title or "本章").strip()
    reqs = _req_lines(requirements)
    fact_lines = [x.strip().lstrip("-· ").strip()
                  for x in str(facts or "").splitlines()
                  if x.strip().startswith("-")]
    pts = [x.strip() for x in re.split(r"[、,，;；\n]", str(points or "")) if x.strip()]

    cols, pending = _table_columns(t)
    rows = reqs or ["以本项目要求表与招标文件对应条款为准"]
    # 招标文件「投标文件格式」里的偏差表 / 报价表 / 逐条应答表 / 单价分析表
    # 都要求真表格。这里输出 Markdown 表格，docx_export 会还原成 Word 表格；
    # 每张表自成一段（前后留空行），否则会被当成普通文字行。
    out = [
        f"本章为{t}的资料对照清单，内容以随投标文件提交并加盖公章的原件为准。",
        "下列条目在人工核验原件、补齐名称与页码之前，不构成任何投标承诺，"
        "不得据此认定我方已具备相应资格。",
        "",
        "一、本章须响应的招标要求与对应材料（对照表）",
        "",
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for i, r in enumerate(rows, 1):
        # 抄录列带引用标记：规则检查 / 数字溯源 / 暗标扫描据此把它当引文，
        # 不把招标原文里的负偏离措辞、招标方名称与条款号算成我方表述。
        out.append(f"| {i} | {rules.QUOTE_MARK}{_table_cell(r)} | "
                   f"{pending} | 待核验 | 待人工核验 |")
    out += ["", "二、已核验的企业事实（可直接引用，不得扩大适用范围）"]
    if fact_lines:
        out += [f"- {x}" for x in fact_lines[:12]]
    else:
        out.append("- 目前没有已核验的企业事实，本章不引用任何企业名称、证照、"
                   "业绩或数值；请先在项目「事实」中核验原件。")
    out += ["", "三、人工核验清单"]
    if pts:
        for p in pts[:8]:
            out.append(f"- {p}：核对原件名称、编号、有效期、签章与页码，"
                       "确认与本章要求一一对应。")
    out += [
        "- 营业执照、资质证书、授权书、承诺书、业绩证明等原件的名称、编号、"
        "有效期与页码须逐项核对，未核验项不得表述为已取得或已具备。",
        "- 每条材料与本章招标要求一一对应；无对应原件的条款如实列入偏离表，"
        "不得以文字承诺替代证明材料。",
        "- 核对完成后由项目负责人签字确认，并在此处补入实际材料名称与页码。",
        "",
        "四、本章内容的确定方式",
        f"{t}提交时以经核验的原件清单与招标文件的格式要求为准；本章由系统生成"
        "清单骨架，事实性内容由人工填写并负责，自动改写不会替人确认资格。",
    ]
    return "\n".join(out)


# 供审计/日志使用：界面上可以直接显示「本章为何不让模型写」
REASON = {
    PROTECTED: "证据/表单类章节，须人工填写原件并由项目负责人核验",
    STRUCTURED: "资格/商务响应类章节，只生成要求与材料的对照清单，事实由人工核验",
    NARRATIVE: "方案/实施/服务类章节，可依据招标原文与资料由模型起草",
}
