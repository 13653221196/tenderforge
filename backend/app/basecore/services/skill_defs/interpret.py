"""招标解读类技能：评分矩阵、倾向性条款预警。"""
from __future__ import annotations

import re

from ._common import (
    excerpt,
    find_amounts,
    item,
    line_of,
    result,
    split_sentences,
    table,
)

# ---------------------------------------------------------------- 评分矩阵
SCORE_HINT = re.compile(r"(评分办法|评标办法|评审办法|评分标准|评审因素|评分因素|打分|评分细则)")
SCORE_END = re.compile(r"(合同条款|技术需求|技术要求|采购需求|投标人须知前附表|附件|格式)")

SCORE_LINE_PATTERNS = [
    # （一）价格分（30分）：……
    re.compile(r"^[（(]?[一二三四五六七八九十0-9]{1,3}[）).、．]?\s*([^\n（(：:]{2,24})[（(]\s*(\d{1,3}(?:\.\d)?)\s*分\s*[)）]"),
    # 技术方案  30分
    re.compile(r"^([^\n：:（(]{2,24}?)\s*[：:\s]\s*(\d{1,3}(?:\.\d)?)\s*分"),
    # 价格分\t30\t……
    re.compile(r"^([^\t\n|]{2,24})[\t|]\s*(\d{1,3}(?:\.\d)?)\s*[\t|]"),
]

SUBJECTIVE_WORDS = ("综合比较", "横向比较", "酌情", "优", "良", "一般", "较差", "合理性", "可行性",
                    "先进性", "科学性", "针对性", "完整", "清晰", "详尽", "方案")
OBJECTIVE_WORDS = ("证书", "资质", "认证", "业绩", "合同", "注册", "职称", "专利", "奖项",
                   "体系", "CMMI", "ISO", "等保", "检测报告")
PRICE_WORDS = ("报价", "价格", "费率", "折扣", "下浮")

CATEGORY_OF = [
    ("价格", PRICE_WORDS),
    ("技术", ("技术", "方案", "实施", "服务", "培训", "售后", "运维", "质量", "进度", "安全")),
    ("商务", ("业绩", "资质", "证书", "财务", "信誉", "信用", "注册", "认证", "奖项", "人员")),
]


def _score_section(text: str) -> str:
    match = SCORE_HINT.search(text or "")
    if not match:
        return (text or "")[:6000]
    start = max(0, match.start() - 200)
    tail = text[start : start + 9000]
    end = SCORE_END.search(tail, 200)
    return tail[: end.start()] if end else tail


def _classify(name: str, rule: str) -> str:
    # 先只看评分项名称，名称看不出来再看细则，避免「质量管理体系」把商务项归到技术类
    for blob in (name, f"{name}{rule}"):
        for label, words in CATEGORY_OF:
            if any(w in blob for w in words):
                return label
    return "其他"


def _nature(name: str, rule: str) -> str:
    blob = f"{name}{rule}"
    if any(w in blob for w in PRICE_WORDS):
        return "价格分"
    if any(w in blob for w in SUBJECTIVE_WORDS):
        return "主观分"
    if any(w in blob for w in OBJECTIVE_WORDS):
        return "客观分"
    return "待确认"


def scoring_matrix(ctx: dict) -> dict:
    text = ctx.get("tender_text") or ""
    section = _score_section(text)

    found: list[dict] = []
    seen: set[str] = set()
    for raw_line in section.split("\n"):
        line = raw_line.strip()
        if not line or len(line) > 400:
            continue
        for pattern in SCORE_LINE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            name = re.sub(r"^[（(]?[一二三四五六七八九十0-9]{1,3}[）).、．]?\s*", "", match.group(1)).strip()
            try:
                score = float(match.group(2))
            except ValueError:
                continue
            if not name or score <= 0 or score > 100:
                continue
            key = re.sub(r"\s+", "", name)
            if key in seen:
                continue
            seen.add(key)
            rule = line[match.end() :].strip(" :：—－-")[:180]
            found.append(
                {
                    "name": name,
                    "score": score,
                    "category": _classify(name, rule),
                    "nature": _nature(name, rule),
                    "rule": rule or "（原文未展开细则）",
                }
            )
            break

    if not found:
        return result(
            "未能在招标文件中定位到评分办法段落，可能是扫描件或评分表以图片形式存在。",
            [item("mid", "未识别到评分项", "检查招标文件是否包含「评分办法 / 评审因素」章节，或该章节为图片/扫描页。",
                  advice="可用「招标解读」页的 AI 抽取补全，或手工在评标办法处截图核对。")],
            verdict="info",
            stats={"评分项": 0, "合计分值": 0},
            notice="评分表常见于「第三章 评标办法」，若为扫描件需先做 OCR。",
        )

    total = sum(row["score"] for row in found)
    by_cat: dict[str, float] = {}
    by_nature: dict[str, float] = {}
    for row in found:
        by_cat[row["category"]] = by_cat.get(row["category"], 0) + row["score"]
        by_nature[row["nature"]] = by_nature.get(row["nature"], 0) + row["score"]

    items: list[dict] = []
    if abs(total - 100) > 1:
        items.append(
            item("mid", f"评分项合计 {total:g} 分，不等于 100 分",
                 "可能是漏识别了部分评分项，或原文本身含加分项/扣分项。",
                 advice="对照招标文件原文核对是否有遗漏（尤其表格形式的评分项）。")
        )
    objective = by_nature.get("客观分", 0)
    subjective = by_nature.get("主观分", 0)
    price = by_nature.get("价格分", 0)
    if objective >= 15:
        items.append(
            item("info", f"客观分占 {objective:g} 分，属于「必须拿满」的部分",
                 "证书、业绩、资质类分值只要材料齐全即可稳拿，缺一项就是硬失分。",
                 advice="按客观分条目逐项准备证明材料，优先补齐缺口。")
        )
    if subjective >= 30:
        items.append(
            item("info", f"主观分占 {subjective:g} 分，是拉开差距的主战场",
                 "评委按方案质量打分，分值集中在技术方案、实施方案、服务承诺。",
                 advice="主观分章节要写足篇幅、给出可核查的细节（人员、进度、方法、验收指标）。")
        )
    if price:
        items.append(
            item("info", f"价格分 {price:g} 分", "价格分通常按公式计算，需确认评标基准价规则。",
                 advice="核算报价与基准价的关系，避免低价无效或高价失分。")
        )

    rows = [
        [row["name"], f"{row['score']:g}", row["category"], row["nature"], row["rule"]]
        for row in found
    ]
    return result(
        f"共识别 {len(found)} 个评分项，合计 {total:g} 分；价格 {price:g} / 技术 "
        f"{by_cat.get('技术', 0):g} / 商务 {by_cat.get('商务', 0):g}。",
        items,
        verdict="warn" if items and items[0]["level"] in ("high", "mid") else "info",
        score=None,
        stats={
            "评分项": len(found),
            "合计分值": f"{total:g}",
            "价格分": f"{price:g}",
            "客观分": f"{objective:g}",
            "主观分": f"{subjective:g}",
        },
        tables=[table("评分矩阵", ["评分项", "分值", "归类", "性质", "评分细则"], rows,
                      note="归类与性质由关键词推断，仅为速览，最终以招标文件原文为准。")],
        notice="分值与细则均来自原文，未做任何改写；如与原文不符请以原文为准。",
    )


# ---------------------------------------------------------------- 倾向性条款
RISK_RULES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("指定品牌或型号", "high",
     "限定特定品牌、型号或「相当于」某品牌，属于以不合理条件限制竞争。",
     "可书面要求澄清；仍不修正的可向监管部门质疑。",
     ("指定品牌", "仅限", "须为.*品牌", "相当于", "参考品牌", "同档次", "原厂授权", "唯一授权")),
    ("地域或注册地限制", "high",
     "要求投标人在本地注册、设立分支机构或在本地有业绩，违反公平竞争原则。",
     "保存原文，作为质疑依据。",
     ("本地注册", "注册地", "在本市", "在本省", "属地", "本地业绩", "当地", "区内注册")),
    ("业绩门槛偏高", "mid",
     "要求近年类似业绩数量或金额明显高于项目规模，可能构成排他。",
     "核算自身业绩是否够格，不够则考虑联合体或放弃。",
     ("类似业绩", "同类业绩", "业绩.*个", "合同金额.*以上", "近.*年.*业绩")),
    ("特定认证或资质", "mid",
     "要求特定认证（如 CMMI、涉密、双软）且与项目无必然关联，可能排他。",
     "确认是否能在投标截止前取得；不能则评估质疑可行性。",
     ("CMMI", "涉密", "双软", "等保", "信息系统集成", "特定认证", "行业协会")),
    ("注册资本或规模门槛", "mid",
     "以注册资本、营业收入、人员规模设门槛，可能超出项目实际需要。",
     "核对营业执照与财务数据是否达标。",
     ("注册资本", "实缴", "营业收入", "净资产", "人员规模", "社保.*人")),
    ("唯一来源或专利壁垒", "high",
     "以专利、专有技术、唯一供应商为由限制，需核实是否具备单一来源条件。",
     "若项目非单一来源采购，此条款可质疑。",
     ("唯一", "独家", "专有技术", "专利技术", "单一来源")),
    ("不合理工期或付款", "mid",
     "工期显著短于合理实施周期，或付款比例严重后置，履约风险高。",
     "测算现金流与实施排期，必要时在偏离表中说明。",
     ("工期.*日内", "日历天内完成", "付款.*%", "验收后.*付款", "无预付款", "垫资")),
    ("实质性条款绑定", "low",
     "以★/▲标注大量条款并规定负偏离即废标，容错空间小。",
     "逐条建立响应表，确保无遗漏。",
     ("★", "▲", "实质性要求", "负偏离", "不满足即")),
]

NEGATION = ("不得", "禁止", "严禁", "否则", "无效", "不予", "不接受", "拒绝")


def risk_alert(ctx: dict) -> dict:
    text = ctx.get("tender_text") or ""
    if not text.strip():
        return result("未提供招标文件正文。", [], verdict="info")

    items: list[dict] = []
    hits: list[list] = []
    for idx, (label, level, why, advice, keywords) in enumerate(RISK_RULES, 1):
        evidence = ""
        for keyword in keywords:
            pattern = keyword if any(c in keyword for c in ".*|") else re.escape(keyword)
            match = re.search(pattern, text)
            if match:
                evidence = line_of(text, match.start())[:200]
                break
        if not evidence:
            continue
        # “不得指定品牌”这类否定表述不算倾向性
        if any(neg in evidence for neg in NEGATION) and label in ("指定品牌或型号", "地域或注册地限制"):
            continue
        items.append(item(level, label, why, evidence, advice))
        hits.append([f"R{idx:02d}", label, "高" if level == "high" else ("中" if level == "mid" else "低"), evidence[:80]])

    if not items:
        return result(
            "未发现明显的排他性或倾向性表述，条款看起来是通用写法。",
            [item("info", "未命中倾向性规则", "已检查品牌、地域、业绩、认证、注册资本、唯一来源、付款条件等 8 类常见排他写法。")],
            verdict="pass",
            score=95,
            stats={"命中": 0},
            notice="规则只能识别显性表述，隐性倾向（如评分细则偏向某方案）需人工判断。",
        )

    high = sum(1 for i in items if i["level"] == "high")
    score = max(0, 100 - high * 18 - (len(items) - high) * 8)
    return result(
        f"命中 {len(items)} 类风险表述，其中高风险 {high} 项。",
        items,
        verdict="fail" if high else "warn",
        score=score,
        stats={"命中": len(items), "高风险": high},
        tables=[table("风险清单", ["编号", "类型", "等级", "原文摘录"], hits)],
        notice="风险分越低表示越可能排他；命中不等于违规，需结合项目性质判断。",
    )
