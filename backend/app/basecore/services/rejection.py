"""废标条款核对：从招标文件抽取"否决/无效"条款，逐条比对投标文件。

设计要点（与通用检查规则的区别）：
- 通用规则（checker.py）查的是"通病"，与项目无关；
- 本模块查的是"这个项目写明的废标条件"，条款来自招标文件本身，必须逐条有结论；
- 条款数量、措辞因项目而异，所以先抽取成清单，再由人工确认，避免机器误判直接下结论。

离线可用：抽取与比对都只用规则；配置了模型后，抽取和判定会额外走一次 AI 复核。
"""
from __future__ import annotations

import re
from typing import Iterable

from ..config import LLMConfig
from .checker import _keywords, _norm, _similarity
from .llm import LLMError, chat, parse_json

# 触发"废标"表述的词。命中即认为该句可能是一条否决条款。
REJECT_WORDS = (
    "否决其投标", "否决投标", "投标无效", "无效投标", "作无效标处理", "按无效标处理",
    "废标", "不予受理", "不予接受", "拒绝其投标", "取消其投标资格", "取消投标资格",
    "视为放弃", "视为无效", "不得参加", "资格审查不合格", "符合性审查不合格",
)

# 条款分类：按命中词判定，命中顺序即优先级（越具体的类别放前面）。
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("报价", ("最高限价", "控制价", "预算价", "投标报价", "报价", "总价", "单价", "大写",
              "小写", "两个报价", "多个报价", "选择性报价", "折扣", "优惠", "保证金")),
    ("资格性", ("营业执照", "资质", "许可证", "认证", "业绩", "财务", "社保", "纳税",
                "信用", "失信", "黑名单", "注册资本", "联合体", "关联", "资格审查")),
    ("技术", ("技术参数", "技术指标", "技术规格", "偏离", "负偏离", "检测报告", "样品",
              "演示", "节能", "环保", "强制认证", "实质性要求", "★", "▲")),
    ("形式性", ("密封", "封装", "标记", "份数", "正本", "副本", "装订", "签字", "签署",
                "盖章", "公章", "授权委托", "法定代表人", "格式", "装袋", "递交", "送达",
                "有效期")),
    ("商务", ("工期", "交货期", "服务期", "质保", "付款", "违约", "售后", "培训",
              "合同条款", "承诺")),
]

# 机器无法判定、只能提示人工核验的要点（实物/流程类）
MANUAL_HINTS = ("密封", "封装", "装订", "装袋", "递交", "送达", "截止时间", "开标现场",
                "原件", "样品", "演示", "现场考察", "份数", "正本", "副本")
# 出现这些词说明该条款本身就是流程/实物要求，文本比对无意义
FLOW_WORDS = ("密封", "封装", "装订", "装袋", "递交", "送达", "份数", "正本", "副本", "原件")

# 可用于"是否出现"判定的实体词表（证照、材料、要素）
ENTITY_WORDS = (
    "营业执照", "资质证书", "安全生产许可证", "开户许可证", "信用中国", "信用报告",
    "社保", "纳税", "财务报告", "审计报告", "业绩", "合同", "验收报告", "中标通知书",
    "法定代表人", "授权委托书", "身份证", "项目经理", "建造师", "职称", "检测报告",
    "检验报告", "3C认证", "ISO9001", "ISO14001", "ISO45001", "保证金", "保函",
    "投标函", "开标一览表", "分项报价表", "技术偏离表", "商务偏离表", "售后服务承诺",
)

NUM_CONSTRAINT = re.compile(
    r"(?:不少于|不低于|不高于|不得超过|不超过|至少|以上|≥|≤|大于|小于)\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(万元|元|%|个|项|份|年|天|日|日历天|次|人)?"
)

SENT_SPLIT = re.compile(r"(?<=[。；;！!？?\n])")

# 条款编号前缀：1. / 1、 / （一） / (1)
NUM_PREFIX = re.compile(r"^\s*(?:[（(]?\s*[一二三四五六七八九十0-9]{1,3}\s*[）).、．]\s*)")
# “下列情形之一的，其投标无效”这类引导句
LEAD_IN = re.compile(r"(下列|以下|如下)[^。；]{0,10}(情形|情况|行为|之一)")
# 遇到这些词说明整段可能包含多条并列款，需要按编号再拆一次
MULTI_SPLIT = re.compile(r"(?=(?:^|\s)[（(]?[一二三四五六七八九十0-9]{1,3}[）).、．]\s*\S)")


class RejectError(RuntimeError):
    pass


# ---------------- 抽出条款 ----------------
def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= 8]


def classify(clause: str) -> str:
    for name, words in CATEGORIES:
        if any(w in clause for w in words):
            return name
    return "其他"


def extract_by_rules(tender_text: str, limit: int = 80) -> list[dict]:
    """规则抽取：按段落识别否决条款，能识别"下列情形之一"式的列表条款。

    招标文件的废标条款大多写成：
        14. 投标文件存在下列情形之一的，视为无效投标：
        （一）……（二）……
    所以不能按句子切，必须先按编号切段，再判断"引导句 + 枚举项"的结构。
    """
    items: list[dict] = []
    seen: set[str] = set()
    pending_list = False  # 上一条是引导句，后续枚举项逐条成条款

    def add(text: str) -> None:
        text = text.strip("；;。 ")
        key = _norm(text)[:36]
        # 枚举式条款常常很短（"质保期少于2年的"只有 8 个字），阈值不能太高
        if len(_norm(text)) < 6 or key in seen:
            return
        seen.add(key)
        items.append(_make_item(text, len(items)))

    for line in _lines(tender_text):
        flat = _norm(line)
        if len(flat) < 6:
            continue
        is_enum = bool(re.match(r"^\s*[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]", line))
        body = NUM_PREFIX.sub("", line).strip()
        has_reject = any(word in line for word in REJECT_WORDS)
        is_lead = bool(LEAD_IN.search(line)) and has_reject

        if is_lead:
            pending_list = True
            # 纯引导句（以冒号结尾、本身没有独立条件）不成条，只开启列表模式
            if body.rstrip().endswith(("：", ":")):
                continue
            add(body)
            continue

        if is_enum and pending_list:
            add(body)
            continue

        if is_enum and not pending_list:
            # 单独的枚举项，只有自身含废标表述才算
            if has_reject:
                add(body)
            continue

        if has_reject:
            add(body)
            pending_list = False
            continue

        # 普通段落：结束列表模式
        pending_list = False
        if len(items) >= limit:
            break
    return items[:limit]


def _lines(text: str) -> list[str]:
    """按段落切分，并处理"一行里塞了多个编号"的抽取结果。"""
    out: list[str] = []
    for raw in (text or "").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in MULTI_SPLIT.split(raw) if p.strip()]
        out.extend(parts)
    return out


def _make_item(clause: str, ord_hint: int) -> dict:
    clause = clause.strip("；;。 ")
    entities = [w for w in ENTITY_WORDS if w in clause]
    nums = NUM_CONSTRAINT.findall(clause)
    flow = [w for w in FLOW_WORDS if w in clause]

    hints: list[str] = []
    if flow:
        hints.append("人工核验：" + "、".join(dict.fromkeys(flow))[:24])
    for value, unit in nums[:2]:
        hints.append(f"数值：{value}{unit or ''}")
    if entities:
        hints.append("应体现：" + "、".join(entities[:4]))

    # 密封、递交、份数这类只能人看实物/流程；
    # 但如果条款实质在管报价（"不得报两个报价"），文本比对有效，不按流程处理。
    price_focus = any(w in clause for w in ("报价", "总价", "单价", "大写", "小写", "保证金"))
    manual_only = (bool(flow) and not price_focus) or (not entities and not nums and not price_focus)
    return {
        "category": classify(clause),
        "clause": clause[:400],
        "basis": "",
        "check_hint": "；".join(hints)[:200],
        "manual_only": manual_only,
        "source": "rule",
        "ord": ord_hint,
    }


# ---------------- AI 抽取 ----------------
AI_EXTRACT_SYSTEM = (
    "你是招标文件审查专家。任务：把招标文件中所有会导致投标被否决、无效、拒绝、不予受理的条件，"
    "拆成一条条可核对的独立条款。要求：\n"
    "1. 只从给定原文抽取，不得补充原文没有的条件；\n"
    "2. 每条只讲一个条件，长句按情形拆分；\n"
    "3. clause 保留原文表述（可精简，但不得改变含义与数字）；\n"
    "4. basis 写条款出处（如“第三章 3.7.2”），原文没有就留空；\n"
    "5. check_hint 用一句话说明“怎么算满足”，便于逐条核对；\n"
    "6. 纯流程性无法从文件判断的（如现场密封递交），category 填“形式性”并照常列出。\n"
    "只输出 JSON。"
)

AI_EXTRACT_TPL = """从下面的招标文件节选中抽取全部否决（废标）条款。

输出 JSON：
{{"items": [{{"category": "资格性|形式性|报价|技术|商务|其他", "clause": "条款原文", "basis": "出处", "check_hint": "判定要点"}}]}}

最多 {limit} 条，按原文顺序。原文没有废标条款时返回空数组。

招标文件节选：
{text}
"""


async def extract_by_ai(cfg: LLMConfig, tender_text: str, limit: int = 80) -> list[dict]:
    if not cfg.ready:
        return []
    # 废标条款多出现在"投标人须知"和"评标办法"，优先取这两段附近的文本
    text = _slice_relevant(tender_text)
    prompt = AI_EXTRACT_TPL.format(limit=limit, text=text[:16000])
    raw = await chat(
        cfg,
        [{"role": "system", "content": AI_EXTRACT_SYSTEM}, {"role": "user", "content": prompt}],
        json_mode=True,
        max_tokens=min(cfg.max_tokens, 6000), task="extract",
    )
    data = parse_json(raw)
    items: list[dict] = []
    for index, row in enumerate(data.get("items") or []):
        if not isinstance(row, dict):
            continue
        clause = str(row.get("clause") or "").strip()
        if len(_norm(clause)) < 6:
            continue
        items.append({
            "category": str(row.get("category") or classify(clause))[:8],
            "clause": clause[:400],
            "basis": str(row.get("basis") or "")[:80],
            "check_hint": str(row.get("check_hint") or "")[:200],
            "manual_only": any(w in clause for w in MANUAL_HINTS),
            "source": "ai",
            "ord": index,
        })
        if len(items) >= limit:
            break
    return items


def _slice_relevant(tender_text: str) -> str:
    """优先取含“否决/无效/评标办法”的段落，避免整篇送模型。"""
    marks = ("投标人须知", "评标办法", "废标", "否决", "无效", "资格审查", "符合性")
    chunks: list[str] = []
    for match in re.finditer(r"[一二三四五六七八九十]+、[^\n]{2,40}", tender_text):
        title = match.group(0)
        if any(m in title for m in marks):
            chunks.append(tender_text[match.start() : match.start() + 8000])
    if chunks:
        return "\n\n".join(chunks)[:20000]
    return tender_text[:20000]


# ---------------- 逐条比对 ----------------
def build_corpus(sections: Iterable[dict]) -> str:
    return "\n".join(
        f"{s.get('title', '')}\n{s.get('content') or ''}" for s in sections
    )


# 投标文件里可量化指标的抽取。只用"指标词 + 邻近数字"，避免把无关数字当指标。
METRIC_PATTERNS: dict[str, tuple[str, ...]] = {
    "报价": (
        r"(?:投标报价|投标总价|报价|总价)[^\d¥￥]{0,12}[¥￥]?\s*([\d][\d,，]*(?:\.\d+)?)\s*(万元|元)",
        r"人民币\s*([零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整]{2,20})",
    ),
    "工期": (
        r"(?:工期|交货期|服务期|履约期)[^\d]{0,16}(\d{1,4})\s*(?:个)?(?:日历天|天|日)",
    ),
    "有效期": (
        r"有效期[^\d]{0,16}(\d{1,4})\s*(?:个)?(?:日历天|天|日)",
    ),
    "质保": (
        r"质保期?[^\d]{0,16}(\d{1,3}(?:\.\d)?)\s*(年|个月)",
    ),
    "保证金": (
        r"(?:投标)?保证金[^\d¥￥]{0,12}[¥￥]?\s*([\d][\d,，]*(?:\.\d+)?)\s*(万元|元)",
    ),
}

QUAL_ITEMS = (
    ("营业执照", ("营业执照",)),
    ("资质证书", ("资质", "资格证")),
    ("安全生产许可证", ("安全生产许可",)),
    ("业绩证明", ("业绩", "合同", "验收")),
    ("财务报告", ("财务", "审计")),
    ("社保缴纳证明", ("社保", "社会保险")),
)

NEG_ONLY = re.compile(r"(不得|禁止|严禁|否则|无效|不予|不接受|不合格|不足|少于|低于|超过|大于)")
MUST = re.compile(r"(须|应|必须|需|应当|要求)(提供|提交|具备|具有|附|加盖|签字|满足|达到)?")
MISSING_REQ = re.compile(r"(未提供|未提交|未附|未按|缺少|缺失|未加盖|未签字)")


def _yuan(value: float, unit: str) -> float:
    return value * 10000 if unit == "万元" else value


def _parse_constraint(clause: str) -> dict | None:
    """从条款里解析出可量化的硬指标，返回 {metric, op, value, unit, label, raw}。"""
    flat = _norm(clause)
    if not flat:
        return None

    # 最高限价 / 控制价：投标报价须 ≤ 该值
    match = re.search(r"(?:最高限价|控制价|预算金额|采购预算|最高投标限价)[^\d]{0,8}([\d,，.]*)\s*(万元|元)", flat)
    if match:
        raw = match.group(1).replace(",", "").replace("，", "")
        if raw:
            return {
                "metric": "报价", "op": "<=", "value": _yuan(float(raw), match.group(2)),
                "unit": "元", "raw": match.group(0)[:24],
                "label": "投标报价不得高于最高限价",
            }
    # "报价超过 380 万元" 这类写法
    match = re.search(r"(?:报价|总价)[^\d]{0,8}(?:超过|高于|大于)[^\d]{0,8}([\d,，.]*)\s*(万元|元)", flat)
    if match:
        raw = match.group(1).replace(",", "").replace("，", "")
        if raw:
            return {
                "metric": "报价", "op": "<=", "value": _yuan(float(raw), match.group(2)),
                "unit": "元", "raw": match.group(0)[:24],
                "label": "投标报价不得超过该金额",
            }
    # 工期上限
    if any(word in flat for word in ("工期", "交货期", "服务期")):
        match = re.search(r"(\d{1,4})\s*(?:个)?(?:日历天|天|日)", flat)
        if match and any(word in flat for word in ("超过", "大于", "不得")):
            return {
                "metric": "工期", "op": "<=", "value": float(match.group(1)),
                "unit": "日历天", "raw": match.group(0),
                "label": "承诺工期不得超过要求",
            }
    # 投标有效期下限
    if "有效期" in flat:
        match = re.search(r"(\d{1,4})\s*(?:个)?(?:日历天|天|日)", flat)
        if match and any(word in flat for word in ("不足", "少于", "低于", "不得")):
            return {
                "metric": "有效期", "op": ">=", "value": float(match.group(1)),
                "unit": "日历天", "raw": match.group(0),
                "label": "投标有效期不得短于要求",
            }
    # 质保期下限
    if "质保" in flat:
        match = re.search(r"(\d{1,3}(?:\.\d)?)\s*(年|个月)", flat)
        if match and any(word in flat for word in ("少于", "不足", "低于", "不得")):
            months = float(match.group(1)) * (12 if match.group(2) == "年" else 1)
            return {
                "metric": "质保", "op": ">=", "value": months,
                "unit": "个月", "raw": match.group(0),
                "label": "质保期不得短于要求",
            }
    # 投标保证金下限（不足额即废标）
    if "保证金" in flat:
        match = re.search(r"([\d,，.]*)\s*(万元|元)", flat)
        raw = match.group(1).replace(",", "").replace("，", "") if match else ""
        if raw:
            return {
                "metric": "保证金", "op": ">=", "value": _yuan(float(raw), match.group(2)),
                "unit": "元", "raw": match.group(0)[:20],
                "label": "投标保证金不得低于要求金额",
            }
    return None


def _extract_metric(metric: str, corpus: str) -> tuple[float, str] | None:
    """从投标文件里抽出指标值，返回 (数值, 依据文本片段)。"""
    for pattern in METRIC_PATTERNS.get(metric, ()):
        for match in re.finditer(pattern, corpus):
            raw = match.group(1)
            snippet = corpus[max(0, match.start() - 12) : match.end() + 8].replace("\n", " ").strip()
            if metric == "报价":
                if not raw[0].isdigit():
                    # 中文大写金额
                    from .checker import cn_amount

                    value = cn_amount(raw)
                    if not value:
                        continue
                    return value, snippet
                unit = match.group(2) if match.lastindex and match.lastindex >= 2 else "元"
                return _yuan(float(raw.replace(",", "").replace("，", "")), unit or "元"), snippet
            if metric == "质保":
                unit = match.group(2) if match.lastindex and match.lastindex >= 2 else "个月"
                return float(raw) * (12 if unit == "年" else 1), snippet
            if metric == "保证金":
                unit = match.group(2) if match.lastindex and match.lastindex >= 2 else "元"
                return _yuan(float(raw.replace(",", "").replace("，", "")), unit or "元"), snippet
            return float(raw), snippet
    return None


def _fmt(value: float, metric: str) -> str:
    if metric in ("报价", "保证金"):
        return f"{value:,.2f} 元"
    if metric == "质保":
        return f"{value:g} 个月"
    return f"{value:g} 日历天"


def _is_flow_clause(clause: str) -> bool:
    """密封、递交、份数这类只能看实物和流程。"""
    price_focus = any(w in clause for w in ("报价", "总价", "单价", "大写", "小写", "保证金"))
    return any(w in clause for w in FLOW_WORDS) and not price_focus


def judge(item: dict, corpus: str) -> dict:
    """对单条否决条款给出三态结论。

    status: covered（已满足/已响应）/ missing（未见响应，有废标风险）/ manual（需人工核验）

    判定顺序（重要）：
    1. 涉及密封递交等实物环节 → 只能人工；
    2. 能解析出量化指标（限价、工期、有效期、质保）→ 抽投标文件同指标做算术比较；
    3. "未提供 X"型 → 查 X 是否出现，没有就是真缺失；
    4. 资格审查类 → 展开成六项资格材料清单，报缺项；
    5. 其余否定式条款（"不得…否则无效"）→ 默认人工，
       因为投标文件"没有出现违规情形"恰恰是正确的，不能当成缺失。
    """
    clause = item.get("clause") or ""
    if not _norm(corpus):
        return {"status": "manual", "confidence": "low", "reason": "投标文件为空，无法比对", "evidence": ""}

    # 1) 实物/流程
    if _is_flow_clause(clause):
        return {
            "status": "manual",
            "confidence": "low",
            "reason": "涉及密封、递交、正副本份数等现场环节，须人工核验装订与递交实物。",
            "evidence": "",
        }

    # 2) 量化指标
    constraint = _parse_constraint(clause)
    if constraint:
        found = _extract_metric(constraint["metric"], corpus)
        if not found:
            return {
                "status": "manual",
                "confidence": "low",
                "reason": f"条款要求“{constraint['label']}”，但未在投标文件中识别到{constraint['metric']}数值，请人工核对。",
                "evidence": "",
            }
        value, snippet = found
        ok = value <= constraint["value"] if constraint["op"] == "<=" else value >= constraint["value"]
        symbol = "≤" if constraint["op"] == "<=" else "≥"
        detail = (
            f"投标文件{constraint['metric']}为 {_fmt(value, constraint['metric'])}，"
            f"要求 {symbol} {_fmt(constraint['value'], constraint['metric'])}（{constraint['raw']}）。"
        )
        return {
            "status": "covered" if ok else "missing",
            "confidence": "high",
            "reason": detail + ("满足要求。" if ok else "不满足，存在废标风险，必须调整。"),
            "evidence": snippet,
        }

    # 3) 签章类条款（"未加盖公章/未签字"最常见，先单独处理）
    if any(w in clause for w in ("公章", "盖章", "签字", "签署")):
        flat_corpus = _norm(corpus)
        hits = [w for w in ("公章", "盖章", "签字", "法定代表人") if _norm(w) in flat_corpus]
        if hits:
            return {
                "status": "covered",
                "confidence": "low",
                "reason": f"正文出现 {'、'.join(hits)}；需核对签署位置是否正确、是否为原件。",
                "evidence": _pick_sentence(corpus, hits[0]),
            }
        return {
            "status": "missing",
            "confidence": "medium",
            "reason": "全文未出现公章或签字要素，需补签章页。",
            "evidence": "",
        }

    # 4) "未提供 X"型：实体必须出现
    entities = [w for w in ENTITY_WORDS if w in clause]
    if entities and MISSING_REQ.search(clause):
        hit = [e for e in entities if _norm(e) in _norm(corpus)]
        if not hit:
            return {
                "status": "missing",
                "confidence": "high",
                "reason": f"条款要求提供或体现：{'、'.join(entities[:4])}，投标文件中均未出现。",
                "evidence": "",
            }
        return {
            "status": "covered",
            "confidence": "medium",
            "reason": f"已出现：{'、'.join(hit[:4])}；仍需确认附件是否齐全、是否在有效期内。",
            "evidence": _pick_sentence(corpus, hit[0]),
        }

    # 5) 资格审查：拆成六项材料
    if "资格" in clause and ("审查" in clause or "要求" in clause):
        flat_corpus = _norm(corpus)
        missing_items = [
            name for name, words in QUAL_ITEMS if not any(_norm(w) in flat_corpus for w in words)
        ]
        present = [name for name, _ in QUAL_ITEMS if name not in missing_items]
        if not missing_items:
            return {
                "status": "covered",
                "confidence": "medium",
                "reason": f"六项资格材料均已出现：{'、'.join(present)}；请核对证书有效期与等级是否达标。",
                "evidence": _pick_sentence(corpus, present[0] if present else ""),
            }
        return {
            "status": "missing",
            "confidence": "medium",
            "reason": f"资格材料缺少：{'、'.join(missing_items)}。",
            "evidence": "",
        }

    # 6) 其余否定式条款：不给"缺失"结论，交人工
    if NEG_ONLY.search(clause):
        if "报价" in clause:
            hint = "确认只提交一份报价，无备选方案或选择性报价"
        elif "格式" in clause:
            hint = "对照招标文件格式章节逐项核对完整性"
        elif "偏离" in clause or "技术" in clause:
            hint = "逐条核对技术偏离表，确认无负偏离"
        else:
            hint = "确认本方不存在该情形"
        return {
            "status": "manual",
            "confidence": "low",
            "reason": f"否定式条件（“{clause[:24]}…”），机器无法证明“不存在该情形”，请人工{hint}。",
            "evidence": "",
        }

    # 7) 兜底人工：给出该条款指向的核对方向
    if "格式" in clause:
        hint = "对照招标文件规定的投标文件格式逐项检查目录、表格与装订"
    elif "技术" in clause or "规格" in clause:
        hint = "核对技术参数逐条响应情况与偏离表"
    elif "售后" in clause or "服务" in clause:
        hint = "核对售后响应时间、质保年限、培训承诺"
    else:
        hint = "对照条款原文逐句确认是否已在投标文件中体现"
    return {
        "status": "manual",
        "confidence": "low",
        "reason": f"条款为原则性表述，机器难以判定，请人工{hint}。",
        "evidence": "",
    }


def _pick_sentence(corpus: str, keyword: str) -> str:
    if not keyword:
        return ""
    index = corpus.find(keyword)
    if index < 0:
        return ""
    start = max(0, index - 40)
    end = min(len(corpus), index + len(keyword) + 60)
    return corpus[start:end].replace("\n", " ").strip()


def judge_all(items: list[dict], corpus: str) -> list[dict]:
    """批量比对；保留人工已确认的结论（status 以 manual_* 开头的不覆盖）。"""
    results = []
    for item in items:
        if str(item.get("status") or "").startswith("confirmed"):
            results.append(item)
            continue
        verdict = judge(item, corpus)
        merged = dict(item)
        merged.update(verdict)
        merged["status"] = "confirmed_" + verdict["status"] if item.get("confirmed") else verdict["status"]
        results.append(merged)
    return results


# ---------------- AI 复核 ----------------
AI_JUDGE_SYSTEM = (
    "你是评标专家。给定若干条招标文件中的否决（废标）条款，以及一份投标文件的相关章节，"
    "逐条判断该投标文件是否满足。判断必须基于给定文本，找不到依据就判 uncertain。只输出 JSON。"
)

AI_JUDGE_TPL = """逐条判断投标文件是否满足下列否决条款。

输出 JSON：
{{"verdicts": [{{"no": 1, "status": "covered|missing|uncertain", "reason": "20 字内说明", "evidence": "投标文件原文片段（40 字内，没有就空）"}}]}}

条款清单：
{items}

投标文件（节选）：
{bid}
"""


async def ai_judge(cfg: LLMConfig, items: list[dict], corpus: str) -> dict[int, dict]:
    """返回 {条款序号: 判定}，序号从 1 开始，对应传入顺序。"""
    if not cfg.ready or not items:
        return {}
    listing = "\n".join(
        f"{i + 1}. [{item.get('category')}] {item.get('clause')}" for i, item in enumerate(items[:40])
    )
    prompt = AI_JUDGE_TPL.format(items=listing, bid=corpus[:14000])
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": AI_JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=min(cfg.max_tokens, 4000), task="evaluate",
        )
        data = parse_json(raw)
    except LLMError:
        return {}

    out: dict[int, dict] = {}
    for row in data.get("verdicts") or []:
        if not isinstance(row, dict):
            continue
        try:
            no = int(row.get("no"))
        except (TypeError, ValueError):
            continue
        status = str(row.get("status") or "").lower()
        if status not in ("covered", "missing", "uncertain"):
            continue
        out[no] = {
            "status": "manual" if status == "uncertain" else status,
            "confidence": "high",
            "reason": "AI：" + str(row.get("reason") or "")[:80],
            "evidence": str(row.get("evidence") or "")[:120],
        }
    return out


# ---------------- 汇总 ----------------
STATUS_LABEL = {
    "covered": "已响应",
    "missing": "未见响应",
    "manual": "需人工确认",
    "confirmed_covered": "已确认响应",
    "confirmed_missing": "已确认缺失",
    "confirmed_manual": "已转人工",
    "na": "不适用",
    "pending": "待核对",
}


def summarize(items: list[dict]) -> dict:
    counts = {key: 0 for key in ("covered", "missing", "manual", "na", "pending")}
    for item in items:
        status = str(item.get("status") or "pending")
        status = status.replace("confirmed_", "")
        if status not in counts:
            status = "manual"
        counts[status] += 1
    risk = "high" if counts["missing"] else ("medium" if counts["manual"] else "low")
    return {"total": len(items), **counts, "risk": risk}


def dedupe(items: list[dict]) -> list[dict]:
    """去掉措辞不同但实质相同的条款（相似度 ≥ 0.8）。"""
    kept: list[dict] = []
    for item in items:
        clause = _norm(item.get("clause") or "")
        if not clause:
            continue
        if any(_similarity(clause, _norm(k.get("clause") or "")) >= 0.8 for k in kept):
            continue
        kept.append(item)
    return kept
