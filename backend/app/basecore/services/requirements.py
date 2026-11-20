"""需求单：把招标文件的硬性要求收敛成一份 R-xxx 编号的唯一事实源。

设计要点：
- 需求条目是贯穿"解读 → 写作 → 检查"的单一事实源：写作时注入响应要求，
  检查时逐条算覆盖率，避免各环节对"要求是什么"各说各话。
- 条目来源三层：AI 解读结果（★条款/资格/评分）→ 招标原文确定性抽取（兜底）→ 去重合并。
- 原文抽取走过滤链：长度合适、含硬性词（必须/应当/不得/★/实质性），排除编辑性提示句；
  抽不到就以解读结果为准，绝不编造。
- 缺字段自动生成 TODO 待补清单。
"""
from __future__ import annotations

import re

from .checker import _find

HARD_WORDS = ("必须", "应当", "须", "不得", "不接受", "否则", "否决", "无效", "实质性", "★", "▲", "强制")
# 编辑性提示句：这些是文件作者对读文件的人说的话，不是对投标人的要求
EDITOR_HINTS = ("参见", "见第", "详见", "如下", "格式如下", "填写说明", "注意事项", "参考格式", "本页",
                "附件", "表格", "签字盖章处", "由投标人填写", "招标代理机构填写")
# 行首主体词：要求条款通常以这些词引出对投标人的动作
HARD_LINE_RE = re.compile(r"^[（(【]?\d{0,3}[）)】、.．]?\s*[^。]{6,200}[。；]?$")
TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("资质", ("资质", "资格", "证书", "许可", "营业执照", "等级")),
    ("业绩", ("业绩", "案例", "合同", "近三年", "近年")),
    ("财务", ("财务", "审计", "资信证明", "纳税", "社保")),
    ("人员", ("项目经理", "负责人", "人员", "工程师", "持证")),
    ("技术", ("技术", "参数", "功能", "性能", "配置", "标准")),
    ("商务", ("工期", "服务期", "质保", "保修", "响应时间", "售后")),
    ("保证金", ("保证金", "担保", "保函")),
    ("格式", ("格式", "份数", "装订", "密封", "签章", "电子标", "目录")),
]

# 必备字段 → TODO 待补清单
REQUIRED_FIELDS: list[tuple[str, str]] = [
    ("项目名称", "确认项目名称与标段号，全部文件需保持一致"),
    ("预算金额", "拿到预算或最高限价，决定报价策略"),
    ("投标保证金", "确认保证金金额与形式，提前安排资金或保函"),
    ("投标截止时间", "截止时间倒排编制计划"),
    ("工期或服务期", "确认工期/服务期并写入实施计划"),
    ("资格要求", "至少识别一条资格门槛，否则按无门槛准备"),
]


# 否定型条款（"不接受联合体投标""不得参加同一标包投标"）要求的是"不要做什么"，
# 投标文件天然不需要声明"我不组联合体"，逐字匹配必然报一堆假"未响应"。
NEG_RE = re.compile(r"(不得|不接受|不允许|禁止|严禁|拒绝|不予|不应|否决|无效|没有对|未对)")
# 我方主体词：判断分句是不是在约束投标人自己（"投标人不得…""投标文件没有对…响应"）。
_BIDDER_RE = re.compile(r"(投标人|投标文件|我方|联合体|供应商)")
# 肯定要求词。不能用裸"应/须"整句判定——"应"几乎出现在每个中文长句里，
# 会让否定型判定永远失效（实测 61 条要求里只剩 12 条被判成否定型）。
REQ_RE = re.compile(r"(应当|应|须|必须|需|要提供)")
_NEED_TIP = "须知"  # "投标人须知前附表"里的"须"不是要求词，判定前先剔掉
_CLAUSE_SPLIT = re.compile(r"[，,。；;：:！!？?\n]")
# 主体是招标人/评标委员会：这是对方的义务或流程，投标文件里没有可响应的内容。
# 允许前面带少量限定词（"除非招标人认为…""招标人在收到评标报告之日起…"）。
OWNER_RE = re.compile(
    r"^[^，。；]{0,8}(?:招标人|采购人|招标代理机构|招标方|发包人|业主|评标委员会|评审委员会|监督部门|行政监督)"
)
# 泛主体条款（"参与招标投标活动的各方应…保密"）：约束的是各方，不是本投标人。
GENERAL_RE = re.compile(r"^[^，。；]{0,6}(?:参与|参加)[^，。；]{0,10}(?:各方|当事人|单位)")
# 程序性条款（开标、投标截止时间、澄清、公示、评标报告）：属招投标流程规则，
# 既不是技术参数也不是商务条款，写进偏离表只会变成一堆假"未见响应"。
PROC_RE = re.compile(
    r"(开标|投标截止时间|递交投标文件|提交投标文件|澄清|公示|评标报告|中标通知书|"
    r"偏差范围|最高偏差项数|电子招标投标交易平台|评标委员会)"
)
# 程序性词命中但这些词也在时，说明是有实质内容的要求（保证金、报价、资格、业绩），
# 不能因为句子里出现"投标截止时间"就把整条判成流程条款。
PROC_EXCEPT_RE = re.compile(r"(保证金|保函|报价|价格|资格|业绩|财务|资质|试验报告|检测能力|生产设备)")
# 对方义务句里夹着"投标人"和这些词时，实质是约束本方的商务要求
# （"投标人不按要求提交投标保证金的，评标委员会将否决其投标"这类，
#  分句看主体像对方义务，实际是本方必须交保证金）。
OWNER_EXCEPT_RE = re.compile(r"(投标保证金|履约保证金|投标报价|报价)")


def clause_kind(text: str) -> str:
    """判定条款类型。

    require = 肯定型（要提供/要满足什么）→ 正文必须有对应内容
    prohibit= 否定型（不做什么）→ 靠"没做"满足，正文无对应句属正常
    owner   = 对方义务/流程（招标人、评标委员会要做的事）→ 投标文件无需响应
    procedural = 招投标流程规则（开标、截止时间、澄清、公示）→ 同样无需响应，
                 但和"招标人义务"分档，便于页面区分"不是写漏了，是不该写"

    按分句判而不是整句判：一句里常同时有"招标人设有最高限价的"（对方）
    和"投标人报价不得超过最高限价"（我方否定型），整句取一种必然错一半。
    """
    probe = re.sub(r"\s+", "", str(text or ""))
    if not probe:
        return "require"
    if GENERAL_RE.match(probe):
        return "owner"
    clauses = [c for c in _CLAUSE_SPLIT.split(probe.replace(_NEED_TIP, "")) if c]
    owner_hit = any(OWNER_RE.match(c) for c in clauses)
    bidder_req = any(_BIDDER_RE.search(c) and REQ_RE.search(c) for c in clauses)
    # 首句就是"招标人不得/不应…"：这是对方自己的禁令，不是本方的承诺。
    # （"招标人设有最高限价"没否定词，不会走到这里，仍按本方条款判。）
    if clauses and OWNER_RE.match(clauses[0]) and re.search(r"(不得|不应|严禁|禁止)", clauses[0]):
        return "owner"
    # 我方否定型优先级最高：这类条款靠"没做"满足，判成 owner 会掩盖风险提示，
    # 判成 require 又会进"未响应"清单刷假阳性，必须单独一档。
    for clause in clauses:
        if _BIDDER_RE.search(clause) and NEG_RE.search(clause):
            return "prohibit"
    # 对方义务：整句只说招标人/评标委员会要做什么。若同时有约束本方的肯定型分句，
    # 说明是双方都提的混合句，不能让 owner 把本方要求一起吞掉。
    if owner_hit and not bidder_req:
        # "投标人不按要求提交投标保证金的，评标委员会将否决其投标"：分句看像对方义务，
        # 实质是本方必须交保证金。若对方分句自己就提到保证金/报价
        # （"招标人向投标人退还投标保证金"），说明确实在说对方，仍判 owner。
        owner_duty = any(OWNER_RE.match(c) and OWNER_EXCEPT_RE.search(c) for c in clauses)
        bidder_duty = OWNER_EXCEPT_RE.search(probe) and _BIDDER_RE.search(probe)
        if not (bidder_duty and not owner_duty):
            return "owner"
    # 流程条款（开标、截止时间、澄清、公示）既不是技术参数也不是商务条款，
    # 进偏离表只会变成假"未见响应"；带保证金/报价/资格等实质内容的句子不在此列。
    if PROC_RE.search(probe) and not PROC_EXCEPT_RE.search(probe):
        return "procedural"
    if bidder_req:
        return "require"
    for clause in clauses:
        if REQ_RE.search(clause) and not NEG_RE.search(clause):
            return "require"
    for clause in clauses:
        if NEG_RE.search(clause):
            return "prohibit"
    return "require"


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", "", line or "").strip("。；；")


def _classify(text: str) -> str:
    for label, words in TYPE_RULES:
        if any(w in text for w in words):
            return label
    return "其他"


LEAD_IN_RE = re.compile(r"(下列|如下|以下|前述|上述)(情形|条款|要求|内容|条件)|情形(之一|如下)|如下[：:]")
# 字段引用："招标人：见投标人须知前附表。" —— 只是指路，不是可响应的条款
REF_ONLY_RE = re.compile(r"^[^：:]{0,28}[：:]\s*(见|详见|参考)|^见(?:第|本|招标|投标|前附表)")
REF_TAIL_RE = re.compile(r"(见|详见|参见)[^。；;]{0,24}(前附表|本章|本目|招标文件)[^。；;]{0,8}[。；;]?\s*$")
FILLER_RE = re.compile(r"其他[^。；;]{0,24}(资料|情形|要求|条件|事项|规定)")
SUBHEAD_RE = re.compile(r"前附表之附表|^附\s*表|附表[一二三四五六七八九十\d]+$")
# 行首编号：（12） / 1.4.3 / 一、 —— 只用于清理展示，不用于判断
NUM_PREFIX_RE = re.compile(r"^\s*(?:[（(]\s*\d{1,3}\s*[）)]|\d{1,2}(?:\.\d{1,2}){0,3}|[一二三四五六七八九十]{1,3}[、.])\s*")
TOC_LINE_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百零\d]{1,4}[章部分篇]|目\s*录|前附表|附\s*件[\s\d]*$"
    r"|.{0,12}须知前附表$|.{0,12}评审标准$|.{0,12}评分标准$)"
)


def _from_tender_text(tender_text: str, limit: int = 80) -> list[str]:
    """从招标原文确定性抽取硬性要求条款（兜底通道）。

    这一层是"AI 挂了也能出结果"的保障，所以宁可少抽也不能抽脏：
    章节标题（"第二章 投标人须知"）、目录项、以冒号结尾的引子句
    （"投标人不得存在下列情形之一："）都不是可响应的条款，抽进来只会
    让后面的覆盖检查报一堆假"未响应"。
    """
    lines: list[str] = []
    for raw in (tender_text or "").splitlines():
        if "|" in raw:
            # docx 表格被拍平成 "条款号 | 条款名称 | 编列内容"。
            # 投标人须知前附表 / 评审标准里最关键的约束就在最后一格，
            # 整行跳过会让这些实质性条款永远进不了要求表。
            cells = [c.strip() for c in raw.split("|") if c.strip()]
            if cells:
                lines.append(cells[-1])
        else:
            lines.append(raw)
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        src = (raw or "").strip()
        if not src or TOC_LINE_RE.match(src):
            continue
        # 引子句：以冒号结尾，或"下列情形之一"这类承接句
        if src.endswith(("：", ":")):
            continue
        if LEAD_IN_RE.search(src):
            continue
        text = NUM_PREFIX_RE.sub("", src).strip()
        if not (12 <= len(text) <= 200):
            continue
        if "|" in text:
            continue
        if REF_ONLY_RE.search(text):
            continue  # "招标人：见投标人须知前附表。" 只是指路
        if REF_TAIL_RE.search(text):
            continue  # "…具体要求见投标人须知前附表。" 具体内容在别处，无法独立核对
        if len(text) < 44 and FILLER_RE.search(text):
            continue  # "…规定的其他资料。"这类承接碎片
        if SUBHEAD_RE.search(text) and len(text) < 32:
            continue  # 表头/子表名
        if len(text) < 26 and re.search(r"(其他情形|其他要求|其他条件|其他事项)", text):
            continue  # 承接句的尾巴
        if any(h in text for h in EDITOR_HINTS):
            continue
        # "须知"里含"须"，不剔掉会把"投标人须知"整段当硬性条款
        probe = text.replace("须知", "").replace("须提交", "提交")
        if not any(h in probe for h in HARD_WORDS):
            continue
        if not HARD_LINE_RE.match(src):
            continue
        norm = _norm_line(text)
        key = norm[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _item_text(item: object, *keys: str) -> str:
    """解读结果里的条目既可能是字符串，也可能是 {"item": ...} 字典。"""
    if isinstance(item, dict):
        for k in keys or ("item", "text", "name", "clause"):
            v = str(item.get(k) or "").strip()
            if v:
                return v
        return ""
    return str(item or "").strip()


def build(meta: dict, tender_text: str) -> dict:
    """合并 AI 解读结果与原文抽取，产出编号需求条目与 TODO 清单。"""
    meta = meta or {}
    entries: list[dict] = []

    # 解读提示词输出 must / star_clauses 两种字段名，都要能进要求表
    star = meta.get("star_clauses") or meta.get("must") or []
    for item in star[:24]:
        text = _item_text(item)
        if len(text) >= 6:
            entries.append({"text": text, "type": _classify(text), "hard": True,
                            "source": "解读·实质性条款"})

    for item in (meta.get("qualification") or [])[:16]:
        text = _item_text(item)
        if len(text) >= 4:
            entries.append({"text": text, "type": _classify(text), "hard": True,
                            "source": "解读·资格"})

    # 原文确定性抽取：作为补充（解读漏掉的硬性条款）
    existing_keys = {_norm_line(e["text"])[:24] for e in entries}
    for line in _from_tender_text(tender_text):
        if line[:24] in existing_keys or any(e["text"][:24] in line or line[:24] in e["text"][:24] for e in entries):
            continue
        entries.append({"text": line, "type": _classify(line), "hard": True, "source": "原文抽取"})
        existing_keys.add(line[:24])

    # 评分项作为软需求（响应评分项能拿分，不满足不废标）
    for item in (meta.get("scoring") or [])[:24]:
        name = _item_text(item)
        if len(name) < 3:
            continue
        score = str(item.get("score") or "") if isinstance(item, dict) else ""
        rule = str(item.get("rule") or "") if isinstance(item, dict) else ""
        entries.append(
            {"text": f"评分项「{name}」（{score or '分值未标注'}）：{rule or '按评分标准响应'}",
             "type": "评分", "hard": False, "source": "解读·评分"}
        )

    # 编号
    items: list[dict] = []
    n_hard = 0
    for idx, e in enumerate(entries, 1):
        n_hard += 1 if e["hard"] else 0
        items.append(
            {"code": f"R-{idx:03d}", "text": e["text"][:240], "type": e["type"],
             "hard": e["hard"], "source": e["source"], "status": "open",
             "kind": clause_kind(e["text"])}
        )

    basic = {row.get("label"): (row.get("value") or "").strip() for row in (meta.get("basic") or [])}
    # "资格要求"不是 basic 里的字段，它来自 qualification / 已抽到的资格类条目。
    # 老逻辑一律按 basic 判空，导致明明抽到 12 条资格还报"未识别资格门槛"。
    has_qual = bool(meta.get("qualification")) or any(e.get("type") == "资格" for e in entries)
    todos: list[dict] = []
    for label, why in REQUIRED_FIELDS:
        if label == "资格要求":
            if has_qual:
                continue
        elif basic.get(label):
            continue
        todos.append({"label": label, "why": why})

    notice = ""
    if not items:
        notice = "未识别到需求条目：请先在「招标解读」跑一次（★条款/资格要求是主要来源），或确认招标文件包含硬性条款。"
    return {
        "items": items,
        "hard_count": n_hard,
        "total": len(items),
        "todos": todos,
        "notice": notice,
        "built": "meta+tender",
    }


def coverage(items: list[dict], sections: list[dict]) -> dict:
    """需求条目在已写正文中的覆盖情况：hard 条目逐条核对。

    三档而不是两档：
    - 否定型条款（不得/不接受/禁止）单列为 manual：这类要求靠"没做"来满足，
      正文里找不到对应句子是正常的，硬报"未响应"会把用户淹没在假阳性里。
    - 对方义务条款（招标人公示中标候选人、评标委员会否决投标）单列为 owner：
      标书里根本没有可写的内容，计入分母只会把覆盖率压得毫无意义。
    """
    corpus = "".join((s.get("content") or "") for s in sections)
    covered, missing, manual, owner = 0, [], [], []
    for item in items:
        text = str(item.get("text") or "")
        if len(text) < 6:
            continue
        if item.get("hard") is False:
            continue  # 软需求（评分项）由评分覆盖面板负责
        kind = clause_kind(text)
        if kind in ("owner", "procedural"):
            owner.append({"code": item.get("code"), "text": text[:120]})
            continue
        hit = _find(corpus, text)
        if hit:
            covered += 1
        elif kind == "prohibit":
            manual.append({"code": item.get("code"), "text": text[:120]})
        else:
            missing.append({"code": item.get("code"), "text": text[:120]})
    total = covered + len(missing) + len(manual)
    return {
        "covered": covered,
        "missing": missing[:15],
        "missing_total": len(missing),
        "manual": manual[:15],
        "manual_total": len(manual),
        "owner": owner[:10],
        "owner_total": len(owner),
        "rate": round(covered / total * 100, 1) if total else 0,
    }


def require_block(items: list[dict], hard_only: bool = True, limit: int = 14) -> str:
    """写作注入块：编号需求条目清单，提示词要求逐条响应。"""
    rows = []
    for item in items:
        if hard_only and not item.get("hard"):
            continue
        mark = "★必须" if item.get("hard") else "评分"
        rows.append(f"{item['code']}（{mark}·{item['type']}）{item['text'][:120]}")
    if not rows:
        return ""
    shown = rows[:limit]
    tail = f"\n（另有 {len(rows) - limit} 条，见需求单）" if len(rows) > limit else ""
    return (
        "【招标需求单（必须逐条响应）】\n"
        + "\n".join(shown)
        + tail
        + "\n写作时凡涉及上述条目的内容必须明确响应，数字与承诺要与条目一致，不得漏项。"
    )
