"""P4 招标要素维度：从招标原文做确定性抽取，只切原文、不生成内容。

和「货物清单」严格分开：货物清单是 `名称 | 数量 | 单价` 的表格行，
这里是保证金、开标时间、付款方式、★▲ 强制参数等决策要素，两者互不混数。

三条硬边界：
- 命中关键词时只把原文片段切出来，绝不改写、换算或补全；
- 没命中一律写「未识别」+ status=missing，不填示例值、不猜；
- 不调模型、不写库，展开汇总卡时才计算。
"""
from __future__ import annotations

import re
from typing import Any

from . import store

# (key, 显示名, 关键词正则, 命中后向后截取的字符数)
DIMS: list[tuple[str, str, str, int]] = [
    ("project_name", "项目名称", r"项目名称|工程名称|采购项目名称", 90),
    ("project_no", "项目编号", r"项目编号|招标编号|采购编号|分标编号|标段编号", 90),
    ("tenderee", "招标人 / 采购人", r"招标人|采购人|招标单位|建设单位", 100),
    ("agency", "招标代理机构", r"招标代理机构|采购代理机构|代理机构名称", 110),
    ("budget", "预算 / 最高限价", r"最高限价|预算金额|采购预算|招标控制价", 130),
    ("bid_bond", "投标保证金", r"投标保证金|保证金金额|保证金形式", 150),
    ("bid_deadline", "投标截止时间", r"投标截止|递交.*截止|截止时间", 130),
    ("opening_time", "开标时间 / 地点", r"开标时间|开标地点|开标日期", 130),
    ("bid_validity", "投标有效期", r"投标有效期", 110),
    ("duration", "工期 / 服务期", r"工期|服务期|交付期|交货期|供货期", 130),
    ("warranty", "质保期", r"质保期|保修期|质量保证期|免费维护期", 130),
    ("payment", "付款方式 / 结算", r"付款方式|支付方式|结算方式|付款条件|付款比例", 150),
    ("contract", "合同条款要点", r"违约责任|验收标准|结算条款|知识产权|合同主要条款", 150),
    ("qualification", "资格条件", r"资格要求|投标人资格|资格条件|资质要求", 150),
    ("eval_method", "评标办法", r"评标办法|评审办法|综合评分法|最低评标价法", 130),
    ("reject", "废标 / 否决条款", r"否决投标|废标|无效投标|作废标处理", 150),
    ("response_format", "投标文件组成 / 份数", r"投标文件的组成|投标文件组成|正本.{0,6}副本|装订.{0,6}份数", 150),
    ("sign_seal", "签署 / 密封 / 电子投标", r"签字盖章|签字或盖章|密封|电子投标|加盖公章", 150),
    ("samples", "样品 / 演示 / 踏勘", r"样品|演示|现场踏勘|答疑会", 130),
    ("joint", "联合体 / 分包", r"联合体|分包|不得转包", 130),
    ("contact", "联系人 / 电话", r"联系人|联系电话|联系方式", 110),
    ("delivery_place", "交付 / 服务地点", r"交货地点|交付地点|服务地点|项目地点", 110),
]

STAR_RE = re.compile(r"[★▲][^\n★▲]{0,180}")
STAR_LIMIT = 40

# 按章节标题关联的重点维度；只做确定性映射，不用模型判断
FOCUS_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("报价", "投标函", "商务"), ("budget", "bid_bond", "payment", "bid_validity")),
    (("技术", "方案", "参数"), ("star_params", "duration", "warranty", "contract")),
    (("资格", "资质", "证明"), ("qualification", "reject", "response_format")),
    (("服务", "售后", "运维"), ("warranty", "duration", "payment")),
    (("合同", "条款"), ("contract", "payment", "reject")),
]

NOTE = ("维度值是从招标原文按关键词切出的片段；没识别到的写「未识别」，"
        "不补示例值、不换算单位。★▲ 强制技术参数与货物清单分开列，"
        "逐项仍需对照招标原件人工核对。")


def clip(text: str, start: int, width: int, limit: int = 190) -> str:
    """从命中点往前带一点上下文、往后截一段，压掉表格换行，绝不加内容。"""
    frag = text[max(0, start - 12): start + width]
    frag = re.sub(r"\s+", " ", frag).strip()
    return frag[:limit]


def extract_dimensions(text: str) -> list[dict]:
    src = str(text or "")
    out: list[dict] = []
    for key, label, pattern, width in DIMS:
        m = re.search(pattern, src)
        if m:
            frag = clip(src, m.start(), width)
            out.append({"key": key, "label": label, "value": frag,
                        "status": "found", "evidence": frag[:60],
                        "pos": m.start(), "loc": f"字符 {m.start()}"})
        else:
            out.append({"key": key, "label": label, "value": "未识别",
                        "status": "missing", "evidence": "", "pos": -1,
                        "loc": ""})
    return out


def extract_star_params(text: str, limit: int = STAR_LIMIT) -> dict:
    """★▲ 强制参数独立成表，不与 goods_list 的货物清单合并。"""
    src = str(text or "")
    rows: list[dict] = []
    for m in STAR_RE.finditer(src):
        raw = m.group(0).strip()
        body = re.sub(r"\s+", " ", raw[1:]).strip()
        stop = re.search(r"[。；;]", body)
        if stop and stop.end() > 8:
            body = body[:stop.end()]
        rows.append({"mark": raw[0], "text": body[:160],
                     "pos": m.start(), "loc": f"字符 {m.start()}"})
        if len(rows) >= limit * 3:      # 先收够再去重，避免前面全是重复标记
            break
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in rows:
        k = re.sub(r"\s+", "", r["text"])[:60]
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(r)
        if len(uniq) >= limit:
            break
    return {"total": len(uniq), "items": uniq,
            "truncated": len(uniq) >= limit,
            "note": "只抽取带 ★ / ▲ 符号的原文行，不代表已判定为强制或否决项，"
                    "是否实质性响应须对照评分办法与原件确认。"}


def chapter_focus(pid: str, dims: list[dict], star: dict,
                  limit: int = 12) -> list[dict]:
    """按章节标题给出应重点核对的维度，未识别的也照样列出来。"""
    by_key = {d["key"]: d for d in dims}
    try:
        secs = store.list_sections(pid)
    except Exception:
        secs = []
    out: list[dict] = []
    for s in secs[:limit]:
        title = str(s.get("title") or "")
        keys: list[str] = []
        for words, ks in FOCUS_RULES:
            if any(w in title for w in words):
                keys.extend(ks)
        keys = list(dict.fromkeys(keys))[:6]
        items: list[dict[str, Any]] = []
        for k in keys:
            if k == "star_params":
                n = int(star.get("total") or 0)
                items.append({"key": k, "label": "★▲ 强制技术参数",
                              "value": f"{n} 条（见专项表）" if n else "未识别",
                              "status": "found" if n else "missing",
                              "evidence": "", "loc": ""})
            elif k in by_key:
                items.append(by_key[k])
        if items:
            out.append({"title": title, "items": items})
    return out


def scan(pid: str) -> dict:
    """只读汇总：招标原文 + 章节目录 → 维度 / ★▲ 参数 / 按章重点。"""
    p = store.get_project(pid) or {}
    text = str(p.get("tender_text") or "")
    dims = extract_dimensions(text)
    star = extract_star_params(text)
    return {
        "dimensions": dims, "total": len(dims),
        "found": sum(1 for d in dims if d["status"] == "found"),
        "missing": sum(1 for d in dims if d["status"] == "missing"),
        "star_params": star, "chapter_focus": chapter_focus(pid, dims, star),
        "note": NOTE,
        "summary": f"已识别 {sum(1 for d in dims if d['status'] == 'found')}"
                   f"/{len(dims)} 项招标要素，★▲ 参数 {star['total']} 条",
    }
