"""标书生成类技能：实质性条款提取、结构模板、扩写（提示词型）。"""
from __future__ import annotations

import re

from ._common import item, line_of, result, similar, split_sentences, table

STAR_RE = re.compile(r"[★▲]\s*([^\n]{4,140})")
STRONG_RE = re.compile(r"(?:^|[\s。；;])((?:投标人|供应商|乙方|中标人)?(?:必须|应当|不得|严禁|须)[^\n。；;]{8,120})")
SUBSTANTIVE_HEAD = re.compile(r"(实质性要求|实质性条款|符合性审查|带★|带▲)")


def _dedupe(rows: list[dict], threshold: float = 0.82) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if any(similar(row["text"], keep["text"]) >= threshold for keep in out):
            continue
        out.append(row)
    return out


def mandatory_req_extract(ctx: dict) -> dict:
    text = ctx.get("tender_text") or ""
    rows: list[dict] = []

    for match in STAR_RE.finditer(text):
        body = re.sub(r"^[）)、:：\s]+", "", match.group(1)).strip()
        rows.append({"level": "强制（★/▲）", "text": body, "source": line_of(text, match.start())[:160]})

    head = SUBSTANTIVE_HEAD.search(text)
    if head:
        block = text[head.start() : head.start() + 6000]
        for sentence in split_sentences(block):
            if 8 <= len(sentence) <= 200 and any(
                w in sentence for w in ("必须", "不得", "应当", "须", "否决", "无效", "不予")
            ):
                rows.append({"level": "实质性", "text": sentence.strip(), "source": sentence.strip()[:160]})

    for match in STRONG_RE.finditer(text):
        rows.append({"level": "一般约束", "text": match.group(1).strip(), "source": match.group(1).strip()[:160]})

    rows = _dedupe(rows)
    rows = sorted(rows, key=lambda r: {"强制（★/▲）": 0, "实质性": 1, "一般约束": 2}[r["level"]])

    if not rows:
        return result(
            "未识别到 ★/▲ 条款或强制性表述。",
            [item("mid", "没有找到强制性条款", "可能是扫描件导致符号丢失，或该项目确实未设星号条款。",
                  advice="人工翻到「评标办法 / 符合性审查」章节确认一次。")],
            verdict="info",
        )

    forced = sum(1 for r in rows if r["level"].startswith("强制"))
    items: list[dict] = []
    if len(rows) > 30:
        items.append(
            item("mid", f"共 {len(rows)} 条约束，数量偏多，逐条响应成本高",
                 "条款越密，漏项导致废标的概率越大。",
                 advice="优先处理「强制」与「实质性」两档，一般约束按章节归并响应。")
        )
    if forced == 0:
        items.append(
            item("low", "有实质性表述但没有 ★/▲ 标注",
                 "部分招标文件用文字表述代替星号，仍可能按无效投标处理。",
                 advice="确认「符合性审查表」里是否另有强制项。")
        )

    return result(
        f"提取 {len(rows)} 条约束，其中强制 {forced} 条、实质性 "
        f"{sum(1 for r in rows if r['level'] == '实质性')} 条。",
        items,
        verdict="info",
        stats={"约束总数": len(rows), "强制条款": forced},
        tables=[table("实质性要求清单", ["档位", "条款内容", "原文位置"],
                      [[r["level"], r["text"], r["source"]] for r in rows],
                      note="按「强制 → 实质性 → 一般约束」排序，建议逐条在响应表中给出对应章节。")],
        notice="条款来自原文正则抽取，未做语义改写；请以招标文件原文为准。",
    )


# ---------------------------------------------------------------- 结构模板
TEMPLATES: dict[str, dict] = {
    "信息化": {
        "keywords": ("系统", "平台", "软件", "开发", "信息化", "数据", "云", "接口", "集成", "APP", "小程序"),
        "chapters": [
            ("第一章 投标函及投标函附录", "投标函、投标函附录、法定代表人身份证明、授权委托书", "格式必须逐字照抄，金额与工期与开标一览表一致"),
            ("第二章 商务标", "开标一览表、分项报价表、商务偏离表、付款方式响应", "报价大小写一致，偏离表不留空"),
            ("第三章 资格证明文件", "营业执照、资质证书、财务报告、社保纳税、信用查询、业绩合同", "按招标顺序排列并编页码索引"),
            ("第四章 技术方案", "需求理解、总体架构、功能设计、数据设计、接口方案、安全方案、性能指标", "逐条对应技术需求编号，给出实现方式与验证方法"),
            ("第五章 实施方案", "项目组织、进度计划、里程碑、开发流程、测试方案、上线与培训", "进度甘特图与里程碑必须覆盖工期要求"),
            ("第六章 运维与售后", "运维体系、响应时限、故障处理、备件与升级、服务承诺", "响应时限不得低于招标要求"),
            ("第七章 其他材料", "中小企业声明、廉政承诺、保密承诺、招标文件要求的其他附件", "漏一份即可能被否决"),
        ],
    },
    "货物": {
        "keywords": ("设备", "采购", "货物", "供货", "安装", "调试", "器材", "材料", "仪器", "产品"),
        "chapters": [
            ("第一章 投标函及附录", "投标函、开标一览表、法定代表人证明、授权委托书", "金额、数量、交货期三处必须一致"),
            ("第二章 资格证明文件", "营业执照、生产/经销授权、检测报告、业绩、财务与信用", "授权链完整：厂家→代理商→投标人"),
            ("第三章 技术响应", "技术规格偏离表、参数逐条响应、产品彩页、配置清单", "逐条响应，正偏离写明优势，负偏离需说明"),
            ("第四章 供货与安装方案", "供货计划、包装运输、安装调试、验收方案、培训", "明确到货时间与验收标准"),
            ("第五章 售后服务", "质保期、响应时限、备件供应、巡检、技术支持", "质保期不得低于招标要求"),
            ("第六章 报价文件", "分项报价表、备品备件报价、运费与税费说明", "分项合计必须等于总价"),
        ],
    },
    "工程": {
        "keywords": ("施工", "工程", "土建", "装修", "改造", "安装工程", "总承包", "监理", "市政"),
        "chapters": [
            ("第一章 投标函及附录", "投标函、法定代表人证明、授权委托书、投标保证金", "保证金形式与金额严格按招标要求"),
            ("第二章 商务标", "开标一览表、工程量清单报价、措施费、规费税金", "清单不缺项、不改量、不增项"),
            ("第三章 资格审查资料", "营业执照、资质等级、安全生产许可证、建造师证书、业绩", "建造师专业与等级必须匹配"),
            ("第四章 施工组织设计", "工程概况、施工部署、进度计划、资源配置、主要施工方法、质量与安全措施", "关键工序要有专项方案"),
            ("第五章 质量与安全", "质量保证体系、创优计划、安全文明施工、应急预案", "安全措施需与现场条件匹配"),
            ("第六章 其他", "承诺书、联合体协议（如有）、招标文件要求的其他材料", "联合体需明确牵头人与分工"),
        ],
    },
    "服务": {
        "keywords": ("服务", "咨询", "运维", "物业", "保洁", "保安", "培训", "外包", "评估", "设计"),
        "chapters": [
            ("第一章 投标函及附录", "投标函、开标一览表、法定代表人证明、授权委托书", "服务期与报价口径与招标一致"),
            ("第二章 资格证明文件", "营业执照、资质、人员证书、业绩、财务与信用", "人员证书需与拟派人员一致"),
            ("第三章 服务方案", "需求理解、服务内容、服务标准、作业流程、质量控制", "把服务标准量化成可考核指标"),
            ("第四章 项目组织", "组织架构、人员配置、岗位职责、人员稳定性保障", "拟派人员表要附证书与社保"),
            ("第五章 应急预案与服务承诺", "应急响应、投诉处理、考核与奖惩、退出交接", "响应时限写成具体小时数"),
            ("第六章 报价文件", "报价明细、人员单价、耗材与管理费", "人天×单价要与总价吻合"),
        ],
    },
    "运维": {
        "keywords": ("运维", "维保", "运行维护", "值守", "巡检", "技术支持", "驻场"),
        "chapters": [
            ("第一章 投标函及附录", "投标函、开标一览表、法定代表人证明、授权委托书", "服务期限与值守方式写清楚"),
            ("第二章 资格证明文件", "营业执照、运维资质、人员证书、同类业绩", "驻场人员需提供社保与证书"),
            ("第三章 运维服务方案", "服务范围、服务级别（SLA）、巡检计划、故障分级、响应与恢复时限", "SLA 指标必须不低于招标要求"),
            ("第四章 团队与保障", "驻场人员配置、轮班机制、备件与工具、知识库", "明确到人到岗"),
            ("第五章 安全与保密", "安全管理制度、数据保密、权限管理、应急演练", "涉密项目需附保密承诺"),
            ("第六章 报价文件", "年度服务费、人员单价、备件费用", "分项与总价一致"),
        ],
    },
}


def structure_template(ctx: dict) -> dict:
    text = ctx.get("tender_text") or ""
    scores = {name: sum(text.count(k) for k in cfg["keywords"]) for name, cfg in TEMPLATES.items()}
    best = max(scores, key=lambda k: scores[k]) if text.strip() else "服务"
    if text.strip() and scores[best] == 0:
        best = "服务"

    cfg = TEMPLATES[best]
    rows = [[str(i), ch, body, tip] for i, (ch, body, tip) in enumerate(cfg["chapters"], 1)]
    total_chars = 30000 if best in ("信息化", "工程") else 20000

    items = [
        item("info", f"按「{best}类」项目生成结构",
             f"依据招标正文关键词判定（{('、'.join(f'{k}:{v}' for k, v in scores.items() if v)) or '无命中'}）。",
             advice="如与实际项目类型不符，可切换到其他模板或手工增删章节。"),
        item("info", f"建议正文规模 {total_chars // 10000} 万字以上",
             "评委通常按篇幅与细节密度形成第一印象，过短会被判为方案不完整。",
             advice="技术方案章节占 50% 以上篇幅，且每节给出可核查的细节。"),
    ]

    return result(
        f"已生成 {len(cfg['chapters'])} 章结构模板（{best}类），共 "
        f"{sum(len(c[1].split('、')) for c in cfg['chapters'])} 个二级要点。",
        items,
        verdict="info",
        stats={"模板类型": best, "章节数": len(cfg["chapters"]), "建议字数": f"{total_chars}"},
        tables=[table(f"{best}类标书结构", ["章", "章节名", "应含内容", "写作要点"], rows,
                      note="章节顺序遵循评标顺序：先资格与商务（评委先翻），后技术方案（评委细看）。")],
        notice="模板是通用骨架，必须与招标文件的「投标文件组成」逐项对齐后再使用。",
    )


# ---------------------------------------------------------------- 提示词型技能正文
# 说明：prompt 型技能由 skill_engine 统一走 LLM，这里只声明附加参数。
PROMPT_PARAMS: dict[str, list[dict]] = {
    "content_expand": [
        {"key": "mode", "label": "处理方式", "type": "select",
         "options": ["扩写", "精简", "改写", "润色"], "default": "扩写"},
        {"key": "target", "label": "目标字数", "type": "text", "default": "800"},
        {"key": "source", "label": "待处理正文", "type": "textarea", "default": "", "required": True},
    ],
    "tender_dimensions": [
        {"key": "focus", "label": "重点关注", "type": "text", "default": "资格、评分、废标、工期"},
    ],
}
