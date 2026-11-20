"""招标文件合规体检：14 条确定性规则，帮投标方在动手写标前发现招标文件里的坑。

只做本地规则运算，不调模型。每条给出严重级、原文证据与处理建议。
四类规则视角：
- 硬伤类（高）：违反法定红线的表述，可能导致投诉或废标风险传导给投标人；
- 缺陷类（中）：招标文件自身写得不严谨，容易在评标时产生争议；
- 提示类（低）：需要人工留意核对的地方。

结果存项目 meta.tender_audit，可在解读页反复重跑（覆盖旧结果）。
"""
from __future__ import annotations

import re
from collections import defaultdict

# 招标文件正文里常见的"待补"痕迹
PLACEHOLDER_RE = re.compile(
    r"(待定|待补充|待明确|待填写|此处填写|______|［×+］|〔×+〕|\bXXX\b|×××)"
)

# 明确废止/被替代、仍常见于旧范本的法规文件名
DEPRECATED_LAWS: list[tuple[str, str]] = [
    ("工程建设项目招标范围和规模标准规定", "已被《必须招标的工程项目规定》（国家发展改革委令第 16 号，2018）替代"),
    ("国家计委3号令", "同上，3 号令已废止"),
    ("招标投标公证程序细则", "公证体制改革后已不再适用"),
    ("关于加强国有企业招标采购若干规定的通知", "已被后续国资监管文件取代"),
]

# "第X章/节/条/（一）/1.1"之外的必备要素关键词
REQUIRED_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("招标公告", ("招标公告", "投标邀请书", "竞争性磋商公告")),
    ("投标人须知", ("投标人须知", "供应商须知", "响应文件须知")),
    ("评标办法", ("评标办法", "评审办法", "磋商办法", "评分细则")),
    ("合同条款", ("合同条款", "合同主要条款", "合同格式", "合同书")),
    ("技术需求", ("技术需求", "技术要求", "采购需求", "技术标准", "工程量清单")),
    ("投标文件格式", ("投标文件格式", "响应文件格式", "投标文件组成")),
    ("异议投诉渠道", ("异议", "质疑", "投诉")),
]

# 实体抽取：找同一实体出现的多个不同值（跨章节矛盾）
ENTITY_PATTERNS: list[tuple[str, re.Pattern, re.Pattern]] = [
    # (名称, 抽取正则, 归一函数前处理)
    ("投标截止时间", re.compile(r"投标(?:文件(?:递交)?|响应文件)?(?:截止|递交(?:文件)?(?:截止)?时间)[^0-9]{0,6}(\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2})"), re.compile(r"\D")),
    ("投标有效期", re.compile(r"投标有效期[^0-9]{0,10}(\d{1,4})\s*(?:个)?日历天"), re.compile(r"\D")),
    ("质保期", re.compile(r"(?:质保期|保修期|质量保证期)[^0-9]{0,10}(\d{1,4})\s*(?:个)?(?:月|年|日历天|日)"), re.compile(r"\D")),
]
BUDGET_RE = re.compile(
    r"(?:预算(?:金额|资金)?|最高限价|控制价|采购预算)[^0-9]{0,8}(\d[\d,，]*(?:\.\d+)?)\s*(万元|万元整|元)"
)
BOND_RE = re.compile(
    r"投标保证金[^0-9]{0,12}(\d[\d,，]*(?:\.\d+)?)\s*(万元|元)"
)
VALIDITY_RE = re.compile(r"投标有效期[^0-9]{0,10}(\d{1,4})\s*(?:个)?日历天")
PUBLISH_DATE_RE = re.compile(r"(?:公告(?:发布)?(?:日期|时间)|发布日期|刊登日期)[^0-9]{0,6}(\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2})")
DEADLINE_DATE_RE = re.compile(r"投标(?:文件(?:递交)?|响应文件)?(?:截止|递交(?:文件)?(?:截止)?时间)[^0-9]{0,6}(\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2})")
SCORE_TOTAL_RE = re.compile(r"(?:分值合计|总分|满分|合计满分|评分总分为?)[^0-9]{0,6}(\d{2,3})\s*分")
PENALTY_RE = re.compile(r"(?:违约金|逾期违约金)[^。\n]{0,40}?(?:每日|每天|按日)[^0-9]{0,6}(\d+(?:\.\d+)?)\s*%")
BRAND_RE = re.compile(
    r"(指定|仅限|限定|首选|推荐使用|优先选用|倾向|必须采用|应当采用|只能采用|必须选用|应选用)"
    r"[^。\n]{0,14}(品牌|厂家|商标|原产地|供应商)"
)
# 只保留"明确否定指定行为"的词。像"不予""拒绝"这类泛否定词会把
# "其他品牌不予接受"这种真指向误当成免责条款，必须排除在外。
NEGATIVE = ("不得", "禁止", "不应", "不允许", "原则上不", "无效指定", "属于无效", "以技术要求为准", "以技术规格为准", "非指定", "不作为指定")
# 分档证据：出现【2.5-3分】这类写法，或明说"级差/分档"，就算已量化
GRADED_RE = re.compile(
    r"(梯度|分档|档位|每档|递减|递增|级差|档差"
    r"|【\s*\d+(?:\.\d+)?(?:\s*[-~—～至]\s*\d+(?:\.\d+)?)?\s*分?\s*】"
    r"|\d+(?:\.\d+)?\s*[-~—～]\s*\d+(?:\.\d+)?\s*分)"
)
LOCAL_PERF_RE = re.compile(r"(?:本市|本地|本省|区内)[^。\n]{0,12}(?:业绩|项目|案例|经历)")
REGISTER_CAP_RE = re.compile(r"注册(?:资本|资金)[^0-9]{0,8}(\d[\d,，]*(?:\.\d+)?)\s*(万|元)")
# 主观分表述：只认完整词，单字"优"会命中"优质/优先/优势"，误报率极高
SUBJECTIVE_RE = re.compile(r"(优秀|优良|良好|中等|较差|酌情|酌定|视情况|酌情给分|主观评分)")
SCORING_CTX_RE = re.compile(r"(评分|得分|打分|评审|分值|扣分)")
PAY_RATIO_RE = re.compile(r"(?:验收|交付|到货)[^。\n]{0,20}后[^。\n]{0,10}(\d{1,3})\s*%")
NUM_SECT_RE = re.compile(r"^(\d+)\.(\d{1,2})\b")

SEV_ORDER = {"高": 0, "中": 1, "低": 2}


def _sentence_span(text: str, start: int, end: int, extra: int = 1) -> str:
    """取匹配所在句 + 后续 extra 句（按 。 换行 切）。

    免责条款常写在同一句的尾半段（"…指定品牌的，均属于无效指定，以技术要求为准。"），
    只按前后固定字数取窗口会被正则尾巴吃掉，导致否定词检不出来。
    """
    b = start
    while b > 0 and text[b - 1] not in "。\n":
        b -= 1
    e = end
    seen = 0
    while e < len(text) and seen <= extra:
        if text[e] in "。\n":
            seen += 1
        e += 1
    return text[b:e]


def _num(text: str) -> float:
    return float(text.replace(",", "").replace("，", ""))


def _norm_date(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def _days_between(start: str, end: str) -> int | None:
    """两个 YYYY年M月D日 字符串之间的天数；解析失败返回 None（不猜）。"""
    try:
        from datetime import date

        def parts(s: str) -> tuple[int, int, int] | None:
            m = re.match(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})", s)
            if not m:
                return None
            return int(m.group(1)), int(m.group(2)), int(m.group(3))

        a, b = parts(start), parts(end)
        if not a or not b:
            return None
        return (date(*b) - date(*a)).days
    except Exception:
        return None


def _grep(text: str, pattern: re.Pattern, limit: int = 3) -> list[str]:
    out: list[str] = []
    for m in pattern.finditer(text):
        frag = m.group(0).strip()
        if frag and frag not in out:
            out.append(frag[:120])
        if len(out) >= limit:
            break
    return out


def run(tender_text: str, basic: list[dict] | None = None) -> dict:
    text = tender_text or ""
    issues: list[dict] = []

    def add(code: str, sev: str, title: str, detail: str, evidence: str = "", suggestion: str = "") -> None:
        issues.append(
            {"code": code, "severity": sev, "title": title,
             "detail": detail[:300], "evidence": evidence[:180], "suggestion": suggestion[:200]}
        )

    if len(text.strip()) < 120:
        return {"issues": [], "summary": "招标文件内容过短，无法体检。", "verdict": "pass", "checked": 0}

    # T01 品牌指向性条款
    # 注意：大量招标文件会写"招标文件中指定品牌均属于无效指定，以技术要求为准"，
    # 这是**招标人自己声明不搞品牌指向**的免责条款。只查前置否定词会把这类
    # 合规写法判成"限制供应商"，逼用户去质疑招标人，属于高危误报。
    for m in BRAND_RE.finditer(text):
        ctx = _sentence_span(text, m.start(), m.end(), extra=0)
        if any(neg in ctx for neg in NEGATIVE):
            continue  # 否定/免责语境，不是品牌指向
        add("T01", "高", "疑似品牌指向性条款",
            "招标文件出现指定/首选某品牌的表述，涉嫌以不合理条件限制供应商，投标人响应前应要求澄清。",
            ctx.replace("\n", " ").strip()[:150], "在答疑期向招标人提出质疑或要求书面澄清。")
        break

    # T02 资质门槛超限
    if LOCAL_PERF_RE.search(text):
        hit = _grep(text, LOCAL_PERF_RE, 1)[0]
        add("T02", "中", "疑似地域限制业绩",
            "要求本市/本地业绩可能构成地域歧视，与营商环境要求不符。",
            hit, "可提出异议；同时准备替代业绩证明以免被形式审查卡住。")
    for m in REGISTER_CAP_RE.finditer(text):
        value = _num(m.group(1)) * (10000 if m.group(2).startswith("万") else 1)
        if value >= 5_000_000:  # 注册资本门槛 ≥500 万值得警惕
            add("T02", "低", "注册资本门槛较高",
                f"要求注册资本不低于 {value:,.0f} 元，中小供应商注意自身资格。",
                m.group(0), "")
            break

    # T03 主观分未量化
    # 只看"有没有优秀/良好"必然误报：真实评分表往往正是
    # 「优秀【2.5-3分】；较好【1.5-2分】；一般【1分】」这种带分档的写法。
    # 判据：逐行看，行内既有评分语境又有主观词、却全行找不到分档证据，才算未量化。
    unquantified: list[str] = []
    for line in text.split("\n"):
        if not SUBJECTIVE_RE.search(line) or not SCORING_CTX_RE.search(line):
            continue
        if GRADED_RE.search(line):
            continue  # 本行有【x分】/级差/分档，已量化
        unquantified.append(line.strip()[:130])
        if len(unquantified) >= 2:
            break
    if unquantified:
        add("T03", "中", "评分标准可能未量化",
            f"{len(unquantified)} 处主观表述所在行未见分档或级差说明，评标弹性大。",
            unquantified[0], "看评分细则是否另附分档说明；无分档时按最高档准备材料。")

    # T04 公告期是否足 20 日
    pub = PUBLISH_DATE_RE.search(text)
    dl = DEADLINE_DATE_RE.search(text)
    if pub and dl:
        days = _days_between(pub.group(1), dl.group(1))
        if days is not None and 0 < days < 20:
            add("T04", "中", "公告期不足 20 日",
                f"公告发布 {pub.group(1)}，投标截止 {dl.group(1)}，仅 {days} 天。",
                "", "法定公告期不足可提出异议；同时按剩余天数倒排编制计划。")

    # T05 保证金超限（>预算 2% 或 >10 万即提示，具体红线按项目类型判断）
    bond = BOND_RE.search(text)
    budget = BUDGET_RE.search(text)
    if bond:
        bond_value = _num(bond.group(1)) * (10000 if bond.group(2).startswith("万") else 1)
        cap = None
        if budget:
            budget_value = _num(budget.group(1)) * (10000 if budget.group(2).startswith("万") else 1)
            cap = budget_value * 0.02
        if cap and bond_value > cap + 1:
            add("T05", "高", "投标保证金可能超限",
                f"保证金 {bond_value:,.0f} 元，超过预算 2%（{cap:,.0f} 元）上限。",
                bond.group(0), "法定上限为预算金额 2%，可提出异议要求调低。")
        elif bond_value > 1_000_000:
            add("T05", "低", "保证金金额较大",
                f"保证金 {bond_value:,.0f} 元，占用资金多，注意退回条款与时限。",
                bond.group(0), "")

    # T06 价格分公式
    if re.search(r"价格分|价格部分评分|价格权重", text) and not re.search(
        r"(基准价|低价优先|价格分?计算|计算公式|满足要求且最低)", text
    ):
        add("T06", "中", "价格分计算方式未写明",
            "评分办法提到价格分，但没写基准价或计算公式，报价策略无从判断。",
            "", "答疑时要求明确价格分公式后再定报价。")

    # T07 废止法规引用
    for name, note in DEPRECATED_LAWS:
        if name in text:
            add("T07", "高", "引用了已废止文件",
                f"招标文件引用「{name}」：{note}。",
                name, "以现行有效文件为准响应；发现引用错误可在答疑中提出。")
            break

    # T08 必备要素（合并成一条，缺几项列几项，避免同一规则刷屏）
    missing_sections = [label for label, words in REQUIRED_SECTIONS if not any(w in text for w in words)]
    if missing_sections:
        add("T08", "中", f"缺 {len(missing_sections)} 个必备章节",
            "全文未找到以下章节相关内容：" + "、".join(missing_sections) + "。招标文件可能不完整或结构特殊。",
            "、".join(missing_sections), "确认文件是否漏页；缺关键章节时向招标人索要完整版。")

    # T09 跨章节矛盾（同一实体出现多个不同值）
    for entity, pattern, _norm in ENTITY_PATTERNS:
        values: dict[str, str] = {}
        for m in pattern.finditer(text):
            raw = m.group(1)
            key = _norm.sub("", raw) if entity != "投标截止时间" else _norm_date(raw)
            values.setdefault(key, raw)
        if len(values) >= 2:
            sample = "；".join(list(values.values())[:3])
            add("T09", "高", f"「{entity}」前后矛盾",
                f"同一项出现 {len(values)} 个不同值，评标时可能引发争议：{sample}",
                sample, "以答疑纪要/更正公告的最终口径为准。")

    # T10 合同风险
    for m in PENALTY_RE.finditer(text):
        if float(m.group(1)) > 1:
            add("T10", "中", "逾期违约金比例偏高",
                f"合同条款按日 {m.group(1)}% 计违约金，超过常见 0.5‰–1% 区间。",
                m.group(0), "中标前争取在合同谈判中调整。")
            break
    if re.search(r"(质保期|保修期)", text) is None and re.search(r"(合同|验收)", text):
        add("T10", "低", "未见质保期条款",
            "合同条款部分未检索到质保/保修表述，验收后责任边界不清。", "", "")
    for m in PAY_RATIO_RE.finditer(text):
        if float(m.group(1)) < 50:
            add("T10", "低", "尾款比例较高",
                f"验收后仅支付 {m.group(1)}%，资金垫付压力大。",
                m.group(0), "")
            break

    # T11 占位符
    ph = PLACEHOLDER_RE.findall(text)
    if ph:
        add("T11", "低", "招标文件含未定内容",
            f"发现 {len(ph)} 处「待定/待补充」类占位，关键条款可能尚未最终确定。",
            "、".join(dict.fromkeys(ph))[:120], "开标前留意澄清与更正公告。")

    # T12 编号断档
    groups: dict[str, list[int]] = defaultdict(list)
    for line in text.splitlines():
        m = NUM_SECT_RE.match(line.strip())
        if m:
            groups[m.group(1)].append(int(m.group(2)))
    for parent, subs in groups.items():
        if len(subs) >= 3:
            expect = list(range(subs[0], subs[0] + len(subs)))
            if subs[: len(expect)] != expect and sorted(set(subs)) != list(range(1, len(set(subs)) + 1)):
                add("T12", "低", f"条款编号疑似断档",
                    f"第 {parent} 组编号为 {subs[:12]}，可能缺漏条款。",
                    "", "")
                break

    # T13 投标有效期
    vm = VALIDITY_RE.search(text)
    if vm:
        days = int(vm.group(1))
        if days < 90:
            add("T13", "低", "投标有效期偏短",
                f"投标有效期 {days} 天（低于常见 90 天），注意保证金退回节奏。",
                vm.group(0), "")

    # T14 分值合计
    sm = SCORE_TOTAL_RE.search(text)
    if sm and int(sm.group(1)) != 100:
        add("T14", "高", "评分总分不等于 100",
            f"评分办法写明总分 {sm.group(1)} 分，与法定 100 分制不符，评标基准可能被更正。",
            sm.group(0), "等待更正公告；按最终更正后的分值表准备响应。")

    # 基本信息（来自 AI 解读）交叉核对：解读抽到但规则没抽到的补充提示
    if basic:
        labels = {row.get("label"): row.get("value") or "" for row in basic}
        if not bond and labels.get("投标保证金"):
            add("T05", "低", "保证金金额只出现在解读结果",
                f"规则未能从原文定位保证金金额，AI 解读为「{labels['投标保证金']}」，请人工核对上限。",
                "", "")

    issues.sort(key=lambda i: (SEV_ORDER.get(i["severity"], 3), i["code"]))
    verdict = "fail" if any(i["severity"] == "高" for i in issues) else (
        "review" if any(i["severity"] == "中" for i in issues) else "pass"
    )
    summary = (
        f"体检完成：发现 {len(issues)} 处需要注意的条款"
        + ("，其中硬伤级问题建议先澄清再写标。" if verdict == "fail" else "。" if issues else "，未发现明显风险条款。")
    )
    return {"issues": issues, "summary": summary, "verdict": verdict, "checked": len(text)}
