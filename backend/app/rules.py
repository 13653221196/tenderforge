"""可复现的规则检查：不依赖模型，问题一定带章节与段落锚点。

模型只做补充复核与措辞改写；判定结论由规则给出，便于审计。
"""
from __future__ import annotations

import re

from .config import PROTECTED_KEYWORDS

# 招标文件里典型的硬性要求句式
MUST_RE = re.compile(r"[^\n。；;]{0,60}(?:必须|须|应|不得|不低于|不少于|至少|须提供|应提供)[^\n。；;]{0,80}")

GENERIC_PROMISE = re.compile(r"(完全满足|全部响应|无条件满足|绝对保证|万无一失|行业领先|最优|一流水平)")
NEGATIVE = re.compile(r"(不满足|无法提供|不能提供|不予|不能承诺|暂不支持|不提供)")
PLACEHOLDER = re.compile(
    r"(XXX|xxx|待填|待补充|TODO|【.*?待.*?】|\?\?\?|需人工补充|待人工补充|"
    r"未提供\s*[/／、]\s*需人工补充)")
EMPTY_PROMISE = re.compile(r"(承诺|保证|确保)[^。；\n]{0,25}$", re.M)

REQUIRED_CHAPTERS = ("投标函", "技术方案", "商务", "报价", "资格", "服务")

# structured 章节的对照表会逐字抄录招标要求。这些「不满足」「国家电网
# 有限公司」「条款号 4.1」「90 天」是招标方原文，不是我方的响应、承诺、
# 无依据数字或身份信息。抄录列统一带这个标记，检查器只判定剩余的我方表述。
QUOTE_MARK = "〔引自招标文件〕"


def strip_quoted(text: str) -> str:
    """剔除「引自招标文件」的抄录单元格 / 整行引文，只留我方表述供检查。

    表格行按列剔除（其他列仍要检查）；普通行若整行是引文则整行剔除。
    """
    out: list[str] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if QUOTE_MARK not in s:
            out.append(line)
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            kept = [c for c in cells if QUOTE_MARK not in c]
            if kept:
                out.append(" ".join(kept))
    return "\n".join(out)

# 可核查信息标记：具体参数、附件/证明材料出处、流程节点。
# 短章节只要带这些，就不该按「字数」判问题；字数少本身不是废标风险。
SUBSTANCE_RE = re.compile(
    r"\d|附件|附表|见第|编号|响应表|检测报告|清单|台账|架构图|签字|验收|公章")


def _has_substance(text: str) -> bool:
    """短文本是否仍含可核查实质信息，或有至少两个展开分句。"""
    if SUBSTANCE_RE.search(text):
        return True
    return len(re.findall(r"[。；;]", text)) >= 2


def _paragraphs(text: str) -> list[tuple[int, int, str]]:
    """返回 (start, end, 段落文本)。"""
    out, pos = [], 0
    for block in re.split(r"(\n\s*\n)", text or ""):
        if block.strip():
            out.append((pos, pos + len(block), block.strip()))
        pos += len(block)
    return out


def _anchor_of(paragraph: str, pattern: re.Pattern) -> str:
    m = pattern.search(paragraph)
    if not m:
        return paragraph[:60]
    s = max(0, m.start() - 20)
    return paragraph[s:m.end() + 20][:120].replace("\n", " ")


PAGE_RE = re.compile(r"【第\s*(\d+)\s*页】")


def _page_at(text: str, pos: int) -> int:
    """按正文里的页码标记，把问题定位回原文物理页 / 原文页码。"""
    marks = list(PAGE_RE.finditer(text[:pos]))
    return int(marks[-1].group(1)) if marks else 0


def extract_requirements(tender_text: str, limit: int = 0) -> list[str]:
    """limit<=0 表示不截断：业务处理必须覆盖全部要求，展示分页另做。"""
    reqs: list[str] = []
    for line in tender_text.splitlines():
        line = line.strip()
        if len(line) < 6 or len(line) > 160:
            continue
        for m in MUST_RE.finditer(line):
            r = m.group(0).strip(" 　:：,，-")
            if len(r) >= 8 and r not in reqs:
                reqs.append(r)
    return reqs[:limit] if limit > 0 else reqs


def is_protected(title: str) -> bool:
    return any(k in (title or "") for k in PROTECTED_KEYWORDS)


def rule_check(sections: list[dict], tender_text: str = "") -> list[dict]:
    issues: list[dict] = []
    seq = 0

    def add(code: str, level: str, title: str, detail: str, section_id: str = "",
            anchor: str = "", evidence: str = "", confidence: float = 0.95,
            source_ref: str = "", page: int = 0) -> None:
        nonlocal seq
        seq += 1
        issues.append({
            "code": f"R{seq:02d}", "level": level, "title": title, "detail": detail,
            "evidence": evidence or anchor[:80], "section_id": section_id, "anchor": anchor,
            "source": "规则", "confidence": confidence,
            "source_ref": source_ref or f"规则判定·{code}", "page": page,
        })

    full = "\n".join(s["text"] for s in sections)
    titles = [s["title"] for s in sections]

    # 1. 必需章节缺失
    for req in REQUIRED_CHAPTERS:
        if not any(req in t for t in titles):
            add("MISSING_CHAPTER", "高", f"缺少「{req}」相关章节",
                "招标文件通常要求完整响应，章节缺失可能被判响应不完整甚至废标。")

    # 2. 逐章扫描
    for sec in sections:
        text = sec.get("text") or ""
        sid = sec.get("id", "")
        stitle = sec.get("title", "")
        if not text.strip():
            add("EMPTY", "中", f"「{stitle}」正文为空",
                "章节无实质内容，需补充。", sid, "", "")
            continue
        if len(text) < 80 and not _has_substance(text):
            add("TOO_SHORT", "低", f"「{stitle}」描述不足且缺少可核查信息",
                f"当前仅 {len(text)} 字，且未见具体参数、附件出处或展开说明，建议补充。",
                sid, text[:60], text[:60])

        for start, end, para in _paragraphs(text):
            pg = _page_at(text, start)
            # 逐字抄录的招标原文不是我方表述；只对剩下的内容判定，
            # 否则「不得…」「不满足」等原文字样会变成负偏离误报。
            mine = strip_quoted(para)
            if not mine.strip():
                continue
            if PLACEHOLDER.search(mine):
                add("PLACEHOLDER", "高", "存在未填写的占位内容",
                    "占位符直接提交属于明显缺陷，须替换为真实内容。",
                    sid, _anchor_of(mine, PLACEHOLDER), page=pg)
            if NEGATIVE.search(mine) and not is_protected(stitle):
                add("NEGATIVE_DEVIATION", "高", "出现负偏离表述且未说明",
                    "「不满足/无法提供」等表述需补充偏离说明，否则可能扣分或废标。",
                    sid, _anchor_of(mine, NEGATIVE), page=pg)
            if GENERIC_PROMISE.search(mine):
                add("UNVERIFIABLE", "中", "承诺不可验证",
                    "空泛表述缺少可核查的指标或证明材料，建议给出具体参数与出处。",
                    sid, _anchor_of(mine, GENERIC_PROMISE), page=pg)

    # 3. 招标文件硬性要求响应情况
    requirements = extract_requirements(tender_text)
    no_resp = 0
    for req in requirements[:12]:
        if no_resp >= 4:  # 整本级问题最多 4 条，避免淹没可定位的段落问题
            break
        keys = re.findall(r"[\u4e00-\u9fa5]{2,6}", req)
        core = [k for k in keys if len(k) >= 2][:3]
        if len(core) < 2:
            continue
        hit = sum(1 for k in core if k in full)
        if hit == 0:
            add("NO_RESPONSE", "高", "招标要求未见响应",
                f"要求：「{req[:70]}」在投标文件中未找到对应表述。",
                "", "", req[:80], confidence=0.65,
                source_ref=f"招标文件硬性要求·{req[:36]}（关键词推断，建议人工核对）")
            no_resp += 1

    # 去重（同章节同锚点）
    seen, uniq = set(), []
    for it in issues:
        key = (it["section_id"], it["anchor"][:40], it["code"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq[:40]
