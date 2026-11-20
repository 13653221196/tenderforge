"""标书核查类技能：把「招标要求」逐条对照「投标响应」。

统一判定口径（与废标核对页保持一致）：
- ``covered``：投标文件里有明确对应内容；
- ``missing``：招标要求了、投标文件里找不到；
- ``manual``：属于实物/流程/签章类，文本无法判定，必须人工看。
"""
from __future__ import annotations

import re

from ._common import (
    cn_to_number,
    days_of,
    excerpt,
    find_amounts,
    item,
    line_of,
    norm,
    pick_sentence,
    result,
    similar,
    split_sentences,
    table,
    years_of,
)

# 材料/证照实体表：招标提出 → 投标是否出现
ENTITY_GROUPS: dict[str, tuple[str, ...]] = {
    "营业执照": ("营业执照", "统一社会信用代码", "工商登记"),
    "资质证书": ("资质证书", "资质等级", "行业资质", "专业资质", "资格证书"),
    "安全生产许可证": ("安全生产许可证", "安全许可证"),
    "开户许可": ("开户许可证", "基本账户", "开户银行"),
    "信用报告": ("信用报告", "信用中国", "信用查询", "无失信"),
    "社保证明": ("社保证明", "社会保险", "社保缴纳"),
    "纳税证明": ("纳税证明", "完税证明", "税收缴纳"),
    "财务报告": ("财务报告", "审计报告", "财务报表", "资产负债表"),
    "业绩合同": ("业绩", "合同复印件", "中标通知书", "验收报告"),
    "法定代表人证明": ("法定代表人身份证明", "法人身份证明"),
    "授权委托书": ("授权委托书", "法定代表人授权"),
    "项目经理证书": ("项目经理", "建造师", "项目负责人"),
    "职称证书": ("职称", "工程师证书"),
    "检测报告": ("检测报告", "检验报告", "测试报告"),
    "CMA资质": ("CMA", "计量认证"),
    "CNAS资质": ("CNAS", "实验室认可"),
    "质量体系": ("ISO9001", "质量管理体系", "GB/T19001"),
    "环境体系": ("ISO14001", "环境管理体系"),
    "职业健康体系": ("ISO45001", "职业健康", "OHSAS18001"),
    "信息安全体系": ("ISO27001", "信息安全管理体系"),
    "CMMI": ("CMMI", "软件能力成熟度"),
    "等保测评": ("等保", "等级保护"),
    "3C认证": ("3C认证", "CCC认证", "强制性产品认证"),
    "投标保证金": ("保证金", "保函", "保险保单"),
    "投标函": ("投标函",),
    "开标一览表": ("开标一览表", "报价一览表"),
    "分项报价表": ("分项报价", "报价明细", "工程量清单"),
    "技术偏离表": ("技术偏离", "技术规格偏离"),
    "商务偏离表": ("商务偏离",),
    "售后承诺": ("售后服务", "服务承诺", "质保承诺"),
    "中小企业声明": ("中小企业声明",),
    "保密承诺": ("保密承诺", "保密协议"),
    "廉政承诺": ("廉政", "廉洁"),
}

QUAL_SECTION = re.compile(r"(投标人|供应商|申请人|投标单位)?(资格要求|资格条件|资格标准|合格投标人|资格审查)")
QUAL_STOP = re.compile(r"(评分办法|评标办法|技术需求|采购需求|合同条款|投标人须知前附表)")
# 真正的封装/递交类要求（不含“营业执照副本”这种词）
FLOW_PAT = re.compile(
    r"(密封|封装|装订|装袋|递交|送达|原件备查|份数|正本.{0,6}副本|副本.{0,6}正本|开标现场|现场考察)"
)


def _section(text: str, head: re.Pattern, stop: re.Pattern, *, span: int = 5000) -> str:
    match = head.search(text or "")
    if not match:
        return ""
    block = text[match.start() : match.start() + span]
    end = stop.search(block, 20)
    return block[: end.start()] if end else block


def _requirements(block: str) -> list[str]:
    """把资格/要求段落拆成条目：优先按编号，其次按分句。"""
    rows: list[str] = []
    for line in (block or "").split("\n"):
        line = line.strip()
        if len(line) < 8:
            continue
        if re.match(r"^[（(]?[一二三四五六七八九十0-9]{1,2}[）).、．]", line):
            rows.append(line)
        elif len(line) <= 120 and any(w in line for w in ("应当", "必须", "须", "具备", "提供", "具有")):
            rows.append(line)
    if not rows:
        rows = [s for s in split_sentences(block) if any(w in s for w in ("应当", "必须", "具备", "提供", "具有"))]
    out, seen = [], set()
    for row in rows:
        key = norm(row)[:24]
        if key in seen:
            continue
        seen.add(key)
        out.append(row[:220])
    return out[:40]


def _entities_in(text: str) -> list[str]:
    return [name for name, words in ENTITY_GROUPS.items() if any(w in text for w in words)]


def _covered(bid: str, entity: str) -> str:
    return excerpt(bid, ENTITY_GROUPS[entity][0], window=50) or ""


CN_LEVEL = {"特级": 4, "一级": 3, "甲级": 3, "二级": 2, "乙级": 2, "三级": 1, "丙级": 1}


def _level_gap(req: str, bid: str) -> str:
    """资质等级比较：招标要二级及以上，投标只有三级 → 不满足。"""
    req_level = next((v for k, v in CN_LEVEL.items() if k in req), None)
    bid_level = next((v for k, v in CN_LEVEL.items() if k in bid), None)
    if req_level is None or bid_level is None:
        return ""
    if bid_level < req_level:
        return f"招标要求等级高于投标文件所载（要求：{'/'.join(k for k, v in CN_LEVEL.items() if v == req_level)}）"
    return ""


def _judge_req(req: str, bid: str) -> dict:
    entities = _entities_in(req)
    # “不接受联合体”这类对投标形式的禁止，独立投标即视为满足
    if re.search(r"(不接受|不允许|禁止)[^。；]{0,12}(联合体|分包|转包)", req):
        return {"status": "covered", "reason": "该条为禁止性要求，独立投标即满足", "evidence": ""}
    # 只把真正的封装/递交要求判为人工，注意「营业执照副本」里的“副本”不算流程词
    if FLOW_PAT.search(req):
        return {"status": "manual", "reason": "属于封装/递交/实物类要求，文本无法判定", "evidence": ""}

    if entities:
        missing = [e for e in entities if not any(w in bid for w in ENTITY_GROUPS[e])]
        if not missing:
            hit = next((excerpt(bid, ENTITY_GROUPS[e][0], window=40) for e in entities
                        if excerpt(bid, ENTITY_GROUPS[e][0], window=40)), "")
            gap = _level_gap(req, bid)
            if gap:
                return {"status": "missing", "reason": gap, "evidence": hit}
            return {"status": "covered", "reason": f"投标文件中出现：{'、'.join(entities)}", "evidence": hit}
        return {"status": "missing", "reason": f"投标文件未找到：{'、'.join(missing)}", "evidence": ""}

    # 数值型要求
    req_days, req_years = days_of(req), years_of(req)
    if req_days and "日" in req:
        found = days_of(bid)
        if found:
            return ({"status": "covered", "reason": f"要求 {req_days} 日，投标承诺 {found} 日", "evidence": ""}
                    if found >= req_days else
                    {"status": "missing", "reason": f"要求不少于 {req_days} 日，投标仅 {found} 日", "evidence": ""})
    if req_years:
        found = years_of(bid)
        if found is not None:
            return ({"status": "covered", "reason": f"要求 {req_years} 年，投标承诺 {found} 年", "evidence": ""}
                    if found >= req_years else
                    {"status": "missing", "reason": f"要求 {req_years} 年，投标仅 {found} 年", "evidence": ""})

    # 关键词兜底
    keywords = [w for w in re.split(r"[，,。；;、：:\s]+", req) if 4 <= len(w) <= 12]
    if keywords:
        hit = sum(1 for k in keywords if k in bid)
        if hit >= max(1, len(keywords) // 3):
            return {"status": "covered", "reason": f"命中 {hit}/{len(keywords)} 个关键表述", "evidence": pick_sentence(bid, keywords[:2])}
    return {"status": "manual", "reason": "未匹配到明确证据，需要人工核对", "evidence": ""}


STATUS_LABEL = {"covered": "已响应", "missing": "未见响应", "manual": "人工核对"}


def _audit(req_block: str, reqs: list[str], bid: str) -> tuple[list[list], list[dict], list[dict]]:
    rows, items, payload = [], [], []
    for idx, req in enumerate(reqs, 1):
        judged = _judge_req(req, bid)
        rows.append([f"{idx:02d}", req[:80], STATUS_LABEL[judged["status"]], judged["reason"][:70]])
        payload.append({"req": req, **judged})
        if judged["status"] == "missing":
            items.append(item("high", f"未见响应：{req[:40]}", judged["reason"], judged["evidence"],
                              advice="补齐对应证明材料或在该章节补充响应段落。"))
        elif judged["status"] == "manual":
            items.append(item("low", f"需人工核对：{req[:40]}", judged["reason"],
                              advice="打印后逐项对照原件核验。"))
    return rows, items, payload


def _empty_bid(bid: str) -> dict | None:
    if (bid or "").strip():
        return None
    return result(
        "未提供投标文件，只能抽取招标侧要求，无法判定响应情况。",
        [item("mid", "缺少投标文件", "本技能需要投标文件正文才能逐条比对。",
              advice="上传待检的投标文件（docx / pdf / txt）后重新运行。")],
        verdict="info",
    )


# ---------------------------------------------------------------- 资格核查
def qualification_check(ctx: dict) -> dict:
    block = _section(ctx.get("tender_text") or "", QUAL_SECTION, QUAL_STOP)
    reqs = _requirements(block)
    if not reqs:
        return result("未在招标文件中定位到资格要求章节。",
                      [item("mid", "未识别到资格要求", "可能章节标题不同（如「申请人条件」「投标人资质」），或为扫描件。")],
                      verdict="info")

    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        rows = [[f"{i:02d}", r[:90], "待比对", ""] for i, r in enumerate(reqs, 1)]
        return result(f"提取到 {len(reqs)} 条资格要求，等待投标文件比对。",
                      [item("info", "已抽取资格要求", "上传投标文件后可自动判定响应情况。")],
                      verdict="info", stats={"资格要求": len(reqs)},
                      tables=[table("资格要求清单", ["序", "要求", "状态", "说明"], rows)])

    rows, items, payload = _audit(block, reqs, bid)
    missing = sum(1 for r in rows if r[2] == "未见响应")
    manual = sum(1 for r in rows if r[2] == "人工核对")
    score = max(0, 100 - missing * 12 - manual * 2)
    return result(
        f"{len(reqs)} 条资格要求：已响应 {len(reqs) - missing - manual} 条、未见响应 {missing} 条、待人工 {manual} 条。",
        items,
        verdict="fail" if missing else ("warn" if manual else "pass"),
        score=score,
        stats={"资格要求": len(reqs), "未见响应": missing, "人工核对": manual},
        tables=[table("资格响应对照表", ["序", "资格要求", "状态", "判定说明"], rows)],
        notice="资格类条款缺一项即可能被否决，建议对「未见响应」逐条核实原始附件。",
    )


# ---------------------------------------------------------------- 保证金
DEPOSIT_HEAD = re.compile(r"(投标保证金|保证金)")
DEPOSIT_FORMS = ("电汇", "转账", "银行保函", "保函", "保险", "支票", "汇票", "担保")


def deposit_check(ctx: dict) -> dict:
    tender = ctx.get("tender_text") or ""
    sentences = [s for s in split_sentences(tender) if "保证金" in s]
    if not sentences:
        return result("招标文件未提及投标保证金。",
                      [item("info", "无需缴纳保证金", "未在正文中找到保证金条款，请确认是否为免保证金项目。")],
                      verdict="pass")

    amount, amount_raw = None, ""
    for sentence in sentences:
        found = find_amounts(sentence)
        if found:
            amount, amount_raw = found[0][0], found[0][1]
            break
    forms = [f for f in DEPOSIT_FORMS if any(f in s for s in sentences)]
    deadline = ""
    for sentence in sentences:
        match = re.search(r"([0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日[^，。；]{0,12})", sentence)
        if match:
            deadline = match.group(1).strip()
            break

    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        rows = [["金额", amount_raw or "未识别", "—"],
                ["形式", "、".join(forms) or "未识别", "—"],
                ["递交截止", deadline or "未识别", "—"]]
        return result("已抽取保证金要求，等待投标文件比对。",
                      [item("info", "保证金要求", f"金额 {amount_raw or '未识别'}；形式 {'、'.join(forms) or '未识别'}。")],
                      verdict="info", stats={"金额": amount_raw or "—"},
                      tables=[table("保证金要求", ["项", "招标要求", "投标响应"], rows)])

    items: list[dict] = []
    bid_amounts = find_amounts(bid)
    bid_has = any("保证金" in s for s in split_sentences(bid))
    matched = ""
    # 只有在投标文件确实提到保证金时，才谈“金额不一致”，否则直接报“未见表述”
    if amount and bid_amounts and bid_has:
        for value, raw in bid_amounts:
            if abs(value - amount) < 1:
                matched = raw
                break
        if not matched:
            nearest = min(bid_amounts, key=lambda x: abs(x[0] - amount))
            if abs(nearest[0] - amount) <= amount * 0.02:
                matched = nearest[1]
                items.append(item("low", "保证金金额四舍五入不一致",
                                  f"招标 {amount_raw}，投标 {nearest[1]}，差额在 2% 以内。",
                                  advice="以招标金额为准，避免因金额不符被判无效。"))
            else:
                items.append(item("high", "保证金金额与招标要求不一致",
                                  f"招标要求 {amount_raw}，投标文件中出现 {nearest[1]}。",
                                  excerpt(bid, nearest[1]), advice="按招标金额重新出具保函或转账凭证。"))

    if not bid_has:
        items.append(item("high", "投标文件中未见保证金相关表述",
                          "保证金（或保函）是废标高发项，投标文件应明确承诺缴纳方式与金额。",
                          advice="补充投标保证金缴纳承诺，并附转账凭证或保函扫描件。"))
    missing_forms = [f for f in forms if f not in bid]
    if forms and missing_forms == forms:
        items.append(item("mid", "保证金形式未响应",
                          f"招标接受 {'、'.join(forms)}，投标文件未提及任何形式。",
                          advice="在投标函或承诺书中写明采用的具体形式。"))
    if deadline and deadline not in bid:
        items.append(item("low", "未见递交时间承诺",
                          f"招标要求于 {deadline} 前递交，投标文件未对应说明。",
                          advice="确认缴纳时间早于截止时间，留出到账时间。"))

    rows = [
        ["金额", amount_raw or "未识别", matched or ("已提及" if bid_has else "未见")],
        ["形式", "、".join(forms) or "未识别", "、".join(f for f in forms if f in bid) or "未见"],
        ["递交截止", deadline or "未识别", "已说明" if deadline and deadline in bid else "未见"],
    ]
    return result(
        f"保证金 {amount_raw or '未识别'}；发现 {len([i for i in items if i['level'] in ('high', 'mid')])} 项需修正。",
        items,
        verdict="fail" if any(i["level"] == "high" for i in items) else ("warn" if items else "pass"),
        score=max(0, 100 - len([i for i in items if i["level"] == "high"]) * 30 - len(items) * 5),
        stats={"金额": amount_raw or "—", "形式": "、".join(forms) or "—"},
        tables=[table("保证金对照", ["项", "招标要求", "投标响应"], rows)],
        notice="到账时间、保函真伪需人工核实，系统只能核对文本表述。",
    )


# ---------------------------------------------------------------- 签字盖章
SIGN_POINTS = (
    ("投标函", ("投标函",), "需法定代表人签字并加盖单位公章"),
    ("法定代表人身份证明", ("法定代表人身份证明",), "需加盖单位公章"),
    ("授权委托书", ("授权委托书",), "需法定代表人签字、被授权人签字、双方盖章"),
    ("开标一览表", ("开标一览表", "报价一览表"), "需法定代表人或授权代表签字并盖章"),
    ("分项报价表", ("分项报价", "报价明细"), "需加盖单位公章"),
    ("技术/商务偏离表", ("偏离表",), "需签字盖章"),
    ("售后服务承诺", ("售后", "服务承诺"), "需加盖单位公章"),
    ("投标文件封面", ("投标文件",), "需项目名称、投标人名称、日期并盖章"),
)


def signature_check(ctx: dict) -> dict:
    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        return result("未提供投标文件，无法核查签章。",
                      [item("mid", "缺少投标文件", "签章核查需要投标文件正文定位签章位置。")], verdict="info")

    tender = ctx.get("tender_text") or ""
    items: list[dict] = []
    rows: list[list] = []
    for name, keywords, rule in SIGN_POINTS:
        present = any(k in bid for k in keywords)
        mentioned = any(k in tender for k in keywords) if tender.strip() else True
        if not mentioned and not present:
            continue
        if not present:
            rows.append([name, "未找到", "缺失", rule])
            items.append(item("high", f"缺少「{name}」", "投标文件中未出现该文件标题。", advice=rule))
            continue
        window = ""
        for keyword in keywords:
            pos = bid.find(keyword)
            if pos != -1:
                window = bid[pos : pos + 200]
                break
        signed = any(w in window for w in ("签字", "盖章", "公章", "签署", "签章"))
        rows.append([name, "已找到", "文本已提及签章" if signed else "需人工确认", rule])
        if not signed:
            items.append(item("mid", f"「{name}」未在文中标注签章",
                              "找到文件标题，但附近 200 字内没有签字/盖章说明。",
                              window[:120], advice="打印后核对是否已签字盖章，避免漏章。"))

    items.append(item("low", "骑缝章与每页小签需人工检查",
                      "文本层面无法判断骑缝章、小签、页码连续性。",
                      advice="装订前逐页检查：封面章、骑缝章、需要小签的页面。"))
    missing = sum(1 for r in rows if r[2] == "缺失")
    return result(
        f"核查 {len(rows)} 个签章点：缺失 {missing} 个，其余需人工确认实体章。",
        items,
        verdict="fail" if missing else ("warn" if any(i["level"] == "mid" for i in items) else "pass"),
        score=max(0, 100 - missing * 25 - sum(1 for i in items if i["level"] == "mid") * 8),
        stats={"签章点": len(rows), "缺失": missing},
        tables=[table("签章核查表", ["文件", "是否找到", "判定", "签章要求"], rows)],
        notice="签章属于实物要素，系统只能提示位置，必须以纸质件为准。",
    )


# ---------------------------------------------------------------- 投标有效期
def _days_near(text: str, keyword: str = "有效期", *, window: int = 40) -> int | None:
    """取「有效期」这个词之后紧邻的天数，避免被同一句里的工期数字干扰。"""
    pos = (text or "").find(keyword)
    if pos == -1:
        return days_of(text)
    return days_of(text[pos : pos + window])


def validity_check(ctx: dict) -> dict:
    tender = ctx.get("tender_text") or ""
    bid = ctx.get("bid_text") or ""
    req_sent = pick_sentence(tender, ("投标有效期", "有效期"))
    req_days = _days_near(req_sent) if req_sent else None
    if req_days is None:
        match = re.search(r"投标有效期[^。；;\n]{0,40}?([0-9]{1,4})", tender)
        req_days = int(match.group(1)) if match else None

    if req_days is None:
        return result("招标文件未明确投标有效期天数。",
                      [item("info", "未识别有效期要求", "常见写法为「投标有效期为 90 日历天」，请在投标函中对照原文。")],
                      verdict="info")

    items: list[dict] = []
    if not bid.strip():
        return result(f"招标要求投标有效期 {req_days} 天，等待投标文件比对。",
                      [item("info", "已抽取有效期要求", f"要求不少于 {req_days} 天。")],
                      verdict="info", stats={"要求有效期": req_days})

    bid_sent = pick_sentence(bid, ("投标有效期", "有效期"))
    bid_days = _days_near(bid_sent) if bid_sent else _days_near(bid)
    if bid_days is None:
        items.append(item("high", "投标文件中未见投标有效期承诺",
                          f"招标要求 {req_days} 天，投标文件未找到对应表述。",
                          advice="在投标函中明确写入「投标有效期为 X 日历天」，且不小于招标要求。"))
    elif bid_days < req_days:
        items.append(item("high", "投标有效期短于招标要求",
                          f"要求 {req_days} 天，投标承诺 {bid_days} 天。",
                          bid_sent, advice="按招标要求的天数修改投标函，否则可能被判无效。"))
    else:
        items.append(item("info", "投标有效期满足要求", f"要求 {req_days} 天，承诺 {bid_days} 天。"))

    return result(
        f"投标有效期：要求 {req_days} 天，投标 {'未承诺' if bid_days is None else f'{bid_days} 天'}。",
        items,
        verdict="fail" if any(i["level"] == "high" for i in items) else "pass",
        score=100 if not any(i["level"] == "high" for i in items) else 60,
        stats={"要求有效期": req_days, "投标有效期": bid_days or "—"},
        notice="有效期还需覆盖合同签订期，部分项目要求「投标有效期至合同签订后 30 天」。",
    )


# ---------------------------------------------------------------- 文件完整性
MUST_FILES: dict[str, tuple[str, ...]] = {
    "投标函": ("投标函",),
    "开标一览表": ("开标一览表", "报价一览表"),
    "分项报价表": ("分项报价", "报价明细", "工程量清单"),
    "法定代表人身份证明": ("法定代表人身份证明", "法人身份证明"),
    "授权委托书": ("授权委托书",),
    "资格证明文件": ("营业执照", "资质证书"),
    "技术偏离表": ("技术偏离", "技术规格偏离"),
    "商务偏离表": ("商务偏离",),
    "售后服务承诺": ("售后服务", "服务承诺"),
}


def doc_integrity(ctx: dict) -> dict:
    bid = ctx.get("bid_text") or ""
    tender = ctx.get("tender_text") or ""
    if not bid.strip():
        return result("未提供投标文件，无法核查完整性。",
                      [item("mid", "缺少投标文件", "完整性核查需要投标文件目录与正文。")], verdict="info")

    required = {name: kws for name, kws in MUST_FILES.items() if not tender.strip() or any(k in tender for k in kws)}
    rows, items = [], []
    for name, kws in required.items():
        present = any(k in bid for k in kws)
        rows.append([name, "有" if present else "缺", "—"])
        if not present:
            items.append(item("high", f"缺少必备文件：{name}", "招标文件要求提交，但投标文件中未找到该标题。",
                              advice="按招标文件「投标文件组成」补齐并重新编页。"))

    headings = [l.strip() for l in bid.split("\n") if 2 <= len(l.strip()) <= 40
                and re.match(r"^(第[一二三四五六七八九十]+[章节]|[一二三四五六七八九十]+、|\d+(\.\d+)*\s)", l.strip())]
    dup = len(headings) - len({norm(h) for h in headings})
    if dup > 0:
        items.append(item("low", f"存在 {dup} 个重复标题", "同一标题在正文中多次出现，可能是复制粘贴残留。",
                          advice="合并重复章节，保持目录与正文一一对应。"))

    if not headings:
        items.append(item("mid", "未识别到章节标题", "正文可能缺少编号体系，或标题未按「第X章」编写。",
                          advice="补齐章节编号，便于评委按目录定位。"))

    missing = sum(1 for r in rows if r[2] == "缺")
    return result(
        f"必备文件 {len(rows)} 项，缺失 {missing} 项；识别章节标题 {len(headings)} 个。",
        items,
        verdict="fail" if missing else ("warn" if items else "pass"),
        score=max(0, 100 - missing * 20 - len(items) * 3),
        stats={"必备文件": len(rows), "缺失": missing, "章节数": len(headings)},
        tables=[table("文件完整性", ["文件", "有无", "备注"], rows)],
        notice="只按标题匹配，若文件以附件形式另册装订，请人工确认。",
    )


# ---------------------------------------------------------------- 实质性条款对照
def mandatory_req_check(ctx: dict) -> dict:
    from .generate import mandatory_req_extract

    extracted = mandatory_req_extract(ctx)
    rows = []
    for tbl in extracted.get("tables") or []:
        rows = tbl.get("rows") or []
    if not rows:
        return result("未提取到实质性条款，无需对照。", [], verdict="info")

    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        return result(f"提取到 {len(rows)} 条实质性条款，等待投标文件比对。",
                      [item("info", "已抽取条款", "上传投标文件后可逐条判定响应。")],
                      verdict="info", tables=extracted.get("tables"))

    out_rows, items = [], []
    for idx, row in enumerate(rows, 1):
        clause = row[1]
        judged = _judge_req(clause, bid)
        out_rows.append([f"{idx:02d}", clause[:70], STATUS_LABEL[judged["status"]], judged["reason"][:60]])
        if judged["status"] == "missing":
            items.append(item("high", f"实质性条款未响应：{clause[:36]}", judged["reason"], judged["evidence"],
                              advice="★/▲ 条款负偏离通常直接废标，必须补齐。"))
    missing = sum(1 for r in out_rows if r[2] == "未见响应")
    return result(
        f"{len(out_rows)} 条实质性条款：未见响应 {missing} 条。",
        items,
        verdict="fail" if missing else "pass",
        score=max(0, 100 - missing * 18),
        stats={"条款": len(out_rows), "未见响应": missing},
        tables=[table("实质性要求响应表", ["序", "条款", "状态", "说明"], out_rows)],
        notice="建议把本表直接作为投标文件中的「实质性要求响应表」底稿。",
    )


# ---------------------------------------------------------------- 跨章节一致性
CONSIST_FIELDS = {
    "项目名称": re.compile(r"项目名称[：:\s]*([^\n，。；]{4,40})"),
    "项目编号": re.compile(r"(?:项目|招标|采购)编号[：:\s]*([A-Za-z0-9\-—－_/（）()]{4,40})"),
    "招标人": re.compile(r"(?:招标人|采购人|招标单位)[：:\s]*([^\n，。；]{2,30})"),
    "投标人": re.compile(r"(?:投标人|供应商)名称[：:\s]*([^\n，。；]{2,30})"),
    "投标总价": re.compile(r"(?:投标总价|投标报价|总报价)[（(]?小写[)）]?[：:\s]*([^\n，。；]{2,30})"),
    "工期/服务期": re.compile(r"(?:工期|交货期|服务期|履约期限)[：:\s]*([^\n，。；]{2,30})"),
}


def consistency_check(ctx: dict) -> dict:
    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        return result("未提供投标文件，无法比对一致性。",
                      [item("mid", "缺少投标文件", "一致性核查需要投标文件正文。")], verdict="info")

    rows, items = [], []
    for field, pattern in CONSIST_FIELDS.items():
        values = [m.group(1).strip() for m in pattern.finditer(bid)]
        values = [v for v in values if v and not re.fullmatch(r"[_\-—\s]+", v)]
        unique: list[str] = []
        for value in values:
            if not any(similar(value, u, 3) > 0.85 for u in unique):
                unique.append(value)
        if not unique:
            continue
        if len(unique) > 1:
            rows.append([field, "冲突", " ｜ ".join(unique[:4])])
            items.append(item("high", f"「{field}」在不同位置不一致",
                              f"出现 {len(unique)} 种写法：{'、'.join(unique[:4])}",
                              advice="统一为招标文件中的标准写法，尤其是金额与项目编号。"))
        else:
            rows.append([field, "一致", unique[0][:40]])

    if not rows:
        return result("未识别到可比较的字段，可能正文缺少「项目名称：」这类标注。",
                      [item("low", "字段识别为空", "建议在封面与投标函中使用规范的「字段：值」写法。")],
                      verdict="info")

    conflicts = sum(1 for r in rows if r[1] == "冲突")
    return result(
        f"比对 {len(rows)} 个关键字段，发现 {conflicts} 处不一致。",
        items,
        verdict="fail" if conflicts else "pass",
        score=max(0, 100 - conflicts * 15),
        stats={"字段": len(rows), "冲突": conflicts},
        tables=[table("一致性对照", ["字段", "结论", "取值"], rows)],
        notice="金额、项目编号不一致是常见废标原因，务必逐个核对。",
    )


# ---------------------------------------------------------------- 文本规范
TEXT_RULES: list[tuple[str, str, str, re.Pattern]] = [
    ("全角数字", "high", "正文混用全角数字，排版不专业", re.compile(r"[０-９]{1,}")),
    ("连续标点", "low", "出现连续标点（。。／，，）", re.compile(r"[，。；：！？]{2,}")),
    ("英文标点混用", "low", "中文语境使用英文逗号/句号", re.compile(r"[\u4e00-\u9fa5][,;:][\u4e00-\u9fa5]")),
    ("中英文括号混用", "low", "同一段落混用（）与 ()", re.compile(r"[（(][^）)]{0,20}[)）]")),
    ("多余空格", "low", "中文之间出现空格", re.compile(r"[\u4e00-\u9fa5]\s{1,3}[\u4e00-\u9fa5]")),
    ("占位符残留", "high", "存在未替换的占位符",
     re.compile(r"((?<![A-Za-z0-9])XXX(?![A-Za-z0-9])|×{3,}|待填|此处填写|【[^】]{0,10}】|〔[^〕]{0,10}〕)")),
    ("重复词", "low", "出现重复词（的的／公司公司）", re.compile(r"([\u4e00-\u9fa5]{1,2})\1")),
]

UNIT_RULES = (("万元", "元"), ("日历天", "天"), ("个工作日", "工作日"))


def ai_text_check(ctx: dict) -> dict:
    text = ctx.get("bid_text") or ""
    if not text.strip():
        return result("未提供投标文件，无法检查文本规范。",
                      [item("mid", "缺少投标文件", "文本规范检查需要投标文件正文。")], verdict="info")

    items, rows = [], []
    for name, level, why, pattern in TEXT_RULES:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        sample = "、".join(dict.fromkeys(m.group(0) for m in matches[:5]))
        rows.append([name, str(len(matches)), sample[:40]])
        items.append(item(level, f"{name}：{len(matches)} 处", why, sample,
                          advice="用查找替换批量修正。" if level != "high" else "逐处确认后修正，占位符残留会直接暴露不专业。"))

    for a, b in UNIT_RULES:
        if a in text and re.search(rf"[0-9]\s*{b}", text):
            items.append(item("low", f"「{a}」与「{b}」混用", f"同一文件中两种单位并存，易被理解为数值错误。",
                              advice=f"统一为「{a}」或「{b}」，并在首次出现处注明换算关系。"))
            rows.append([f"单位混用（{a}/{b}）", "—", "两种写法并存"])

    for pattern, label in ((r"(我公司|本公司|我方|投标人)", "自称"), (r"(招标人|采购人|甲方)", "对方称谓")):
        values = set(re.findall(pattern, text))
        if len(values) > 1:
            items.append(item("low", f"{label}不统一：{'、'.join(values)}", "同一文件多种称谓，影响严谨度。",
                              advice="全文统一为一种称谓。"))

    high = sum(1 for i in items if i["level"] == "high")
    return result(
        f"发现 {len(items)} 类文本问题，其中高风险 {high} 类。",
        items,
        verdict="warn" if items else "pass",
        score=max(0, 100 - high * 10 - (len(items) - high) * 3),
        stats={"问题类型": len(items), "高风险": high, "字数": len(text)},
        tables=[table("问题分布", ["类型", "次数", "示例"], rows)] if rows else [],
        notice="只检查确定性写法问题；错别字、语句不通需人工或 AI 复核。",
    )


# ---------------------------------------------------------------- 报价逻辑
def pricing_logic(ctx: dict) -> dict:
    bid = ctx.get("bid_text") or ""
    if not bid.strip():
        return result("未提供投标文件，无法核对报价逻辑。",
                      [item("mid", "缺少投标文件", "报价逻辑核对需要投标文件中的报价表与投标函。")], verdict="info")

    items: list[dict] = []
    lower = re.search(r"小写[)）]?[：:\s]*([^\n，。；]{1,30})", bid)
    upper = re.search(r"大写[)）]?[：:\s]*([^\n，。；]{1,40})", bid)
    lower_amount = find_amounts(lower.group(1))[0][0] if lower and find_amounts(lower.group(1)) else None
    upper_amount = find_amounts(upper.group(1))[0][0] if upper and find_amounts(upper.group(1)) else None

    if lower_amount and upper_amount:
        if abs(lower_amount - upper_amount) > 1:
            items.append(item("high", "报价大小写金额不一致",
                              f"小写 {lower.group(1).strip()}，大写 {upper.group(1).strip()}。",
                              advice="以大写为准修正小写，两处必须完全一致。"))
        else:
            items.append(item("info", "大小写金额一致", f"{lower.group(1).strip()} / {upper.group(1).strip()}"))

    # 定位报价表：从表头「单价 + 金额」的下一行开始逐行解析，兼容制表符/竖线/多空格分隔
    lines = bid.split("\n")
    start = next((i for i, line in enumerate(lines)
                  if "单价" in line and ("金额" in line or "合价" in line)), None)
    rows: list[list[str]] = []
    total_line: float | None = None
    if start is not None:
        for line in lines[start + 1 : start + 60]:
            stripped = line.strip()
            if not stripped:
                continue
            # 遇到下一个章节标题就停，避免把资格证明里的数字当报价行
            if re.match(r"^([一二三四五六七八九十]+、|第[一二三四五六七八九十]+[章节]|\d+\.\s*[^\d])", stripped):
                break
            nums = re.findall(r"[0-9][0-9,]*(?:\.[0-9]+)?", stripped)
            name = re.split(r"\s{2,}|\t|\|", stripped)[0][:30]
            if any(w in stripped for w in ("合计", "总计", "小计")):
                if nums:
                    try:
                        total_line = float(nums[-1].replace(",", ""))
                    except ValueError:
                        pass
                continue
            if len(nums) < 3:
                continue
            try:
                qty, price, amount = (float(x.replace(",", "")) for x in nums[-3:])
            except ValueError:
                continue
            rows.append([name, f"{qty:g}", f"{price:g}", f"{amount:g}"])
            if abs(qty * price - amount) > max(1.0, amount * 0.001):
                items.append(item("mid", f"分项金额不符：{name}",
                                  f"{qty:g} × {price:g} = {qty * price:.2f}，表内填写 {amount:g}。",
                                  advice="按单价×数量重算，或修正单价。"))

    if total_line and lower_amount and abs(total_line - lower_amount) > 1:
        items.append(item("high", "分项合计与投标总价不符",
                          f"表内合计 {total_line:g}，投标函小写 {lower_amount:g}。",
                          advice="统一总价，避免评委认定报价不唯一。"))
    elif total_line and not lower_amount:
        items.append(item("mid", "只找到分项合计，未找到投标函小写总价",
                          f"表内合计 {total_line:g}，无法与总价交叉验证。",
                          advice="确认投标函中写明小写金额，两处必须一致。"))

    if not items:
        items.append(item("info", "未发现报价逻辑矛盾", "大小写一致，未识别到分项计算错误。"))

    high = sum(1 for i in items if i["level"] == "high")
    return result(
        f"报价逻辑检查：{'未发现矛盾' if high == 0 else f'发现 {high} 处硬伤'}。",
        items,
        verdict="fail" if high else ("warn" if any(i["level"] == "mid" for i in items) else "pass"),
        score=max(0, 100 - high * 30 - sum(1 for i in items if i["level"] == "mid") * 10),
        stats={"分项行": len(rows), "硬伤": high},
        notice="报价唯一性是废标高发点：同一项目出现两个报价、选择性报价均可能被否决。",
    )


# ---------------------------------------------------------------- 贴合度
def fit_score(ctx: dict) -> dict:
    from .interpret import scoring_matrix

    matrix = scoring_matrix(ctx)
    rows: list[list] = []
    for tbl in matrix.get("tables") or []:
        for row in tbl.get("rows") or []:
            rows.append(row)
    if not rows:
        return result("未识别到评分项，无法评估贴合度。", [], verdict="info")

    bid = ctx.get("bid_text") or ""
    total = 0.0
    expected = 0.0
    out_rows: list[list] = []
    for row in rows:
        name, score_text, _cat, nature, rule = row[0], row[1], row[2], row[3], row[4]
        try:
            score = float(score_text)
        except ValueError:
            continue
        total += score
        if nature == "价格分":
            rate, note = 0.85, "按基准价规则测算"
        elif nature == "客观分":
            keywords = [w for w in re.split(r"[、，,；;（）()\s]+", f"{name}{rule}") if 2 <= len(w) <= 8]
            hit = sum(1 for k in keywords if k in bid) if bid else 0
            rate = min(1.0, 0.45 + hit / max(1, len(keywords)) * 0.55) if bid else 0.6
            note = f"命中 {hit}/{len(keywords)} 个要点" if bid else "未提供投标文件，按 60% 估算"
        else:
            rate, note = (0.7 if bid else 0.5), "主观分取决于方案质量"
        expected += score * rate
        out_rows.append([name, f"{score:g}", f"{score * rate:.1f}", note])

    coverage = expected / total * 100 if total else 0
    items = [
        item("info", f"预估可争取得分 {expected:.1f} / {total:g}（{coverage:.0f}%）",
             "按客观分命中率与主观分经验系数估算，仅供排序参考。",
             advice="把资源投到分值高且差距大的主观分章节。"),
    ]
    if bid and coverage < 70:
        items.append(item("high", f"贴合度偏低（{coverage:.0f}%）",
                          "多个评分项在投标文件中找不到对应内容。",
                          advice="按评分矩阵逐条补齐响应段落，先保证不丢客观分。"))

    return result(
        f"{len(out_rows)} 个评分项，预估得分 {expected:.1f} / {total:g}。",
        items,
        verdict="warn" if coverage < 70 else "pass",
        score=int(coverage),
        stats={"评分项": len(out_rows), "预估得分": f"{expected:.1f}", "总分": f"{total:g}"},
        tables=[table("贴合度测算", ["评分项", "分值", "预估得分", "测算依据"], out_rows,
                      note="系数为经验值，实际得分以评委打分为准。")],
        notice="本表用于排优先级，不构成得分承诺。",
    )


# ---------------------------------------------------------------- 综合风险分
def _full_audit(ctx: dict) -> dict:
    checks = {
        "资格": qualification_check,
        "保证金": deposit_check,
        "有效期": validity_check,
        "完整性": doc_integrity,
        "一致性": consistency_check,
        "文本规范": ai_text_check,
        "报价逻辑": pricing_logic,
    }
    out: dict[str, dict] = {}
    for name, fn in checks.items():
        try:
            out[name] = fn({**ctx, "bid_text": ctx.get("bid_text") or "占位"})
        except Exception:  # 单个技能失败不影响整体
            out[name] = {"verdict": "info", "score": None, "items": [], "summary": "执行失败"}
    return out


def risk_score(ctx: dict) -> dict:
    audit = _full_audit(ctx)
    weights = {"资格": 25, "保证金": 15, "有效期": 10, "完整性": 20, "一致性": 10, "文本规范": 5, "报价逻辑": 15}
    rows, items, total_risk = [], [], 0.0
    for name, weight in weights.items():
        data = audit.get(name) or {}
        score = data.get("score")
        if score is None:
            score = 100 if data.get("verdict") == "pass" else 60
        risk = (100 - score) / 100 * weight
        total_risk += risk
        rows.append([name, str(weight), f"{score}", f"{risk:.1f}"])
        if risk >= weight * 0.4:
            items.append(item("high" if risk >= weight * 0.7 else "mid",
                              f"{name}维度风险偏高", f"该维度得分 {score}/100，权重 {weight}。",
                              advice=f"优先处理「{name}」相关条目。"))
    risk_value = min(100, total_risk * 1.15)
    # 有维度直接判“不通过”时，不能让加权平均把风险稀释掉
    fails = sum(1 for data in audit.values() if (data or {}).get("verdict") == "fail")
    if fails >= 2:
        risk_value = max(risk_value, 62)
    elif fails == 1:
        risk_value = max(risk_value, 45)
    grade = "高" if risk_value >= 60 else ("中" if risk_value >= 30 else "低")
    return result(
        f"综合废标风险 {risk_value:.0f} 分（{grade}风险）。",
        items,
        verdict="fail" if risk_value >= 60 else ("warn" if risk_value >= 30 else "pass"),
        score=int(100 - risk_value),
        stats={"风险值": f"{risk_value:.0f}", "等级": f"{grade}风险"},
        tables=[table("风险分解", ["维度", "权重", "维度得分", "风险贡献"], rows)],
        notice="风险值 = Σ(100-维度得分)×权重 × 1.15，用于横向比较不同标书的成熟度。",
    )


def selfcheck_list(ctx: dict) -> dict:
    audit = _full_audit(ctx)
    rows, items = [], []
    passed = 0
    for name, data in audit.items():
        verdict = data.get("verdict") or "info"
        label = {"pass": "通过", "warn": "有问题", "fail": "不通过", "info": "待确认"}.get(verdict, "待确认")
        highs = [i for i in data.get("items") or [] if i.get("level") == "high"]
        detail = highs[0]["title"] if highs else (data.get("summary") or "")[:50]
        rows.append([name, label, detail])
        if verdict == "pass":
            passed += 1
        elif verdict == "fail":
            items.append(item("high", f"{name}未通过", detail, advice="按对应技能详情逐条修正。"))
        elif verdict == "warn":
            items.append(item("mid", f"{name}存在待修项", detail))

    rows.append(["签章与密封", "需人工", "骑缝章、每页小签、密封袋标记"])
    rows.append(["原件与份数", "需人工", "正副本份数、原件备查"])
    total = len(audit)
    return result(
        f"{total} 个自动项中 {passed} 项通过；另 2 项必须人工核对。",
        items,
        verdict="fail" if any(i["level"] == "high" for i in items) else ("warn" if items else "pass"),
        score=int(passed / total * 100) if total else 0,
        stats={"自动项": total, "通过": passed, "人工项": 2},
        tables=[table("废标自查清单", ["检查项", "结论", "说明"], rows)],
        notice="这是投标前最后一道机器检查，仍需人工复核签章、密封、原件三项。",
    )


# ---------------------------------------------------------------- 样品/检测报告
def sample_report_check(ctx: dict) -> dict:
    tender = ctx.get("tender_text") or ""
    bid = ctx.get("bid_text") or ""
    sentences = [s for s in split_sentences(tender) if any(w in s for w in ("检测报告", "检验报告", "样品", "CMA", "CNAS"))]
    if not sentences:
        return result("招标文件未要求样品或检测报告。",
                      [item("info", "无此项要求", "未找到检测报告/样品相关条款。")], verdict="pass")

    items: list[dict] = []
    rows: list[list] = []
    need_cma, need_cnas, either = False, False, False
    for sentence in sentences:
        has_cma, has_cnas = "CMA" in sentence, "CNAS" in sentence
        if has_cma and has_cnas and re.search(r"(或|/|、|任一)", sentence):
            either = True
        elif has_cma:
            need_cma = True
        elif has_cnas:
            need_cnas = True
    validity = next((years_of(s) for s in sentences if years_of(s)), None)

    if either:
        present = ("CMA" in bid) or ("CNAS" in bid)
        rows.append(["CMA / CNAS 资质", "具备其一", "已提供" if present else "未见"])
        if not present:
            items.append(item("high", "检测报告缺少 CMA/CNAS 资质",
                              "招标要求二者具备其一，投标文件未找到相应字样。",
                              advice="换用具备资质的检测机构报告，或补充资质页。"))
    for label, need, words in (("CMA 资质", need_cma, ("CMA",)), ("CNAS 资质", need_cnas, ("CNAS",))):
        if not need:
            continue
        present = any(w in bid for w in words)
        rows.append([label, "要求", "已提供" if present else "未见"])
        if not present:
            items.append(item("high", f"检测报告缺少 {label}", "招标明确要求具备该资质，投标文件未找到。",
                              advice="换用具备资质的检测机构报告，或补充资质页。"))
    if validity:
        rows.append(["报告有效期", f"近 {validity} 年", "需人工核对"])
        items.append(item("mid", f"检测报告应在近 {validity} 年内", "超期的报告通常不被认可。",
                          advice="核对报告出具日期与投标截止日的间隔。"))
    rows.append(["检测项对应", "逐项对应技术要求", "需人工核对"])
    items.append(item("low", "检测项需逐项对应", "报告中的检测项目必须覆盖招标的技术参数。",
                      advice="做一张「技术参数—检测项」对照表附在报告前。"))

    return result(
        f"样品/检测报告要求 {len(sentences)} 处；生成 {len(rows)} 项核查点。",
        items,
        verdict="fail" if any(i["level"] == "high" for i in items) else "warn",
        score=max(0, 100 - sum(1 for i in items if i["level"] == "high") * 30 - len(items) * 5),
        stats={"要求条款": len(sentences)},
        tables=[table("报告核查", ["项", "要求", "投标情况"], rows)],
        notice="报告真伪与出具机构资质需在发证机构官网核验。",
    )


# ---------------------------------------------------------------- 联合体
def joint_bid_check(ctx: dict) -> dict:
    tender = ctx.get("tender_text") or ""
    bid = ctx.get("bid_text") or ""
    allows = re.search(r"(不接受|不允许)[^。；]{0,10}联合体", tender)
    if allows:
        return result("招标文件明确不接受联合体投标。",
                      [item("info", "不允许联合体", "如以联合体形式投标将被否决。", allows.group(0))],
                      verdict="pass")
    need = re.search(r"联合体[^。；]{0,60}", tender)
    if not need:
        return result("未提及联合体要求。", [item("info", "无联合体条款", "按独立投标准备即可。")], verdict="info")

    elements = [
        ("联合体协议书", ("联合体协议",), "需各方盖章、法定代表人签字"),
        ("牵头人", ("牵头", "主办"), "明确牵头人及各方分工"),
        ("资质分担", ("分工", "承担", "分别"), "按专业分工对应各自资质"),
        ("连带责任", ("连带",), "各方对招标人承担连带责任"),
        ("各方证照", ("营业执照",), "各方均需提供资格证明"),
    ]
    items, rows = [], []
    for name, words, rule in elements:
        present = any(w in bid for w in words) if bid.strip() else False
        rows.append([name, "有" if present else ("未提供投标文件" if not bid.strip() else "未见"), rule])
        if bid.strip() and not present:
            items.append(item("high" if name in ("联合体协议书", "连带责任") else "mid",
                              f"联合体材料缺少：{name}", "联合体投标的必备要素。", advice=rule))

    return result(
        f"联合体要求 {len(elements)} 项，未满足 {sum(1 for r in rows if r[1] == '未见')} 项。",
        items,
        verdict="fail" if any(i["level"] == "high" for i in items) else ("warn" if items else "pass"),
        score=max(0, 100 - len([i for i in items if i["level"] == "high"]) * 25 - len(items) * 5),
        stats={"要素": len(elements)},
        tables=[table("联合体核查", ["要素", "情况", "要求"], rows)],
        notice="联合体各方不得再单独或与其他单位组成联合体参加同一项目。",
    )


# ---------------------------------------------------------------- 电子投标
EBID_ITEMS = [
    ("CA 数字证书", "投标人 CA 锁在有效期内，且与报名主体一致"),
    ("电子签章", "投标文件生成后完成电子签章，签章位置正确"),
    ("文件格式", "按平台要求的格式（通常为专用加密格式）生成"),
    ("文件大小", "不超过平台单文件/单包上限"),
    ("上传时间", "在投标截止时间前完成上传，预留解密时间"),
    ("加密与解密", "加密方式与开标解密一致，密码由投标人保管"),
    ("保证金到账", "按平台要求通过基本户或指定渠道缴纳"),
    ("开标一览表", "平台在线填报内容与投标文件一致"),
]


def ebid_submit_check(ctx: dict) -> dict:
    tender = ctx.get("tender_text") or ""
    platform = ""
    match = re.search(r"([\u4e00-\u9fa5]{2,20}(?:电子交易平台|公共资源交易平台|招投标平台|交易中心))", tender)
    if match:
        platform = match.group(1)
    items = [item("info", f"{name}：{rule}", "需在开标前逐项确认。", advice="勾选确认后打勾，未完成项不得上传。")
             for name, rule in EBID_ITEMS]
    items.insert(0, item("low", "电子投标没有文本可比对，本技能输出的是人工核对清单",
                         "CA、加密、上传等环节无法从文档内容判断。",
                         advice="按清单逐项确认，建议开标前一天完成一次完整演练。"))
    return result(
        f"生成 {len(EBID_ITEMS)} 项电子投标核对清单" + (f"（平台：{platform}）" if platform else "") + "。",
        items,
        verdict="info",
        stats={"核对项": len(EBID_ITEMS), "平台": platform or "未识别"},
        tables=[table("电子投标核对清单", ["环节", "要求"], [[n, r] for n, r in EBID_ITEMS])],
        notice="清单为通用项，具体以交易平台的操作手册为准。",
    )
