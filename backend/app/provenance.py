"""证据绑定：来源由代码从实际注入的材料反查，不让模型自己编出处。

原则沿用「规则管判定、AI 管措辞」：模型只写正文；依据由检索层给出带坐标的
证据片段（doc_id / 页码 / 字符区间 / 原文），存进 evidence 表，界面点角标回跳。
内部工作标注（依据清单、AI 建议、需人工补充）绝不写进标书正文，也不会导出。

证据不足时正文保持干净，改由 evidence 表记一条 missing 待办 + 检查清单提示。
"""
from __future__ import annotations

import re

from . import rag, rules, store

NOTE_HEAD = "【依据与来源】"

# 只盯有业务含义的数值：金额、比例、期限、数量，忽略章节号等纯序号
NUM_RE = re.compile(
    r"(?<![\d.])\d{2,}(?:\.\d+)?\s*(?:万元|万|%|％|元|天|年|小时|分钟|台|套|项|个)")


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "").lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def strip_note(text: str) -> str:
    """去掉历史版本遗留在正文里的依据段，避免反复改写时叠加。"""
    return re.sub(r"\n\s*\n" + re.escape(NOTE_HEAD) + r"[\s\S]*$", "", text or "").rstrip()


# 旧版演示模板把「平台自述句」写进了正文（例：…；依据清单由系统附在末尾。）。
# 这类句子是本系统自己产出的内部工作说明，不是标书内容，导出前按行清掉。
# 关键边界：只删同时含「内部标注词 + 平台自述词」的整行，且该行不得含真实占位符。
# 用户正文里的 XXX / TODO / 待补充 / 需补充 必须原样保留 —— clean_for_export 同时
# 被单章自检和最终文件检查使用，若在这里把占位符抹掉，等于让所有检查静默漏报。
_INTERNAL_MARK = re.compile(r"依据清单|依据与来源|AI 建议")
_SYSTEM_SELF = re.compile(r"由系统|系统附|系统生成|系统给出|系统自动")
_REAL_PLACEHOLDER = re.compile(
    r"XXX|TODO|待填|待补充|待完善|需补充|需人工补充|【需补充|\[请填写\]|TBD", re.I)


def _drop_legacy_system_lines(s: str) -> str:
    keep: list[str] = []
    for ln in s.splitlines():
        if (_INTERNAL_MARK.search(ln) and _SYSTEM_SELF.search(ln)
                and not _REAL_PLACEHOLDER.search(ln)):
            continue
        keep.append(ln)
    return "\n".join(keep)


def clean_for_export(text: str) -> str:
    """正式交付的 Word 只能有标书正文，不能带内部工作标注。

    绝不删除占位符：未填完的内容要留在正文里，交由检查规则拦下并提示人工补录。
    """
    s = strip_note(text or "")
    s = re.sub(NOTE_HEAD + r"[\s\S]*$", "", s)
    s = re.sub(r"（依据：[^）]*）", "", s)
    s = re.sub(r"【(?:需人工补充|AI 建议)[^】]*】", "", s)
    s = _drop_legacy_system_lines(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def gather(pid: str, query: str, tender_text: str = "", k: int = 4) -> list[dict]:
    """人工查询候选来源。不能用于登记实际生成输入。"""
    out: list[dict] = []
    _, hits = rag.retrieve(pid, query, k=k)
    for h in hits:
        # 语义召回但字面零命中的片段只是候选，不伪装成已核实的字面依据
        if h.get("semantic_only"):
            continue
        snippet = (h.get("snippet") or "").strip()
        if not snippet:
            continue
        out.append({
            "scope": h.get("scope") or "资料",
            "title": h.get("title") or "未命名",
            "ref": h.get("ref") or "命中片段",
            "doc_id": h.get("doc_id") or "",
            "page": int(h.get("page") or 0),
            "start": int(h.get("start") or 0),
            "end": int(h.get("end") or 0),
            "snippet": snippet[:160],
        })
    # 招标文件：只把与本章标题/要点真正相关的硬性要求列为依据，并给出原文位置
    qg = _grams(query)
    for i, req in enumerate(rules.extract_requirements(tender_text), 1):
        if len(qg & _grams(req)) < 2:
            continue
        pos = (tender_text or "").find(req)
        out.append({
            "scope": "招标文件", "title": "硬性要求",
            "ref": f"第 {i} 条" + (f" · 字符 {pos}" if pos >= 0 else ""),
            "doc_id": "tender", "page": 0,
            "start": max(0, pos), "end": max(0, pos) + len(req),
            "snippet": req[:160],
        })
        if len(out) >= 6:
            break
    return out[:6]


def save(pid: str, section_id: str, sources: list[dict]) -> int:
    """把本章证据写库；无依据时记一条 missing 待办（不进正文）。"""
    return store.save_evidence(pid, section_id, sources)


def attach(text: str, sources: list[dict]) -> str:
    """保持兼容：只返回干净的标书正文，不再把依据清单拼进章节。

    证据由调用方另行 save() 入库；这里清掉历史版本可能残留的内部标注。
    """
    return clean_for_export(text)


def attach_paragraph(text: str, sources: list[dict]) -> str:
    """段落级改写同样只返回正文；角标与证据由界面从 evidence 表读取。"""
    return clean_for_export(text)


def source_ref(sources: list[dict]) -> str:
    """给问题用的短来源串。"""
    if not sources:
        return "未提供依据（需人工补充）"
    return "；".join(f"{s.get('scope', '')}·{s.get('title', '')}·{s.get('ref', '')}"
                    for s in sources[:2])


def verify_numbers(text: str, sources: list[dict], tender_text: str = "",
                   pid: str = "") -> list[str]:
    """正文数值必须在证据原文里带同一单位逐字出现，或由已核验事实 / 合法计算支撑。

    B02：以前只查裸数字子串，别的项目的金额、范例业绩、同数值不同单位都能蒙混过关。
    pid 传入时会同时核对企业事实表（实体 / 单位 / 有效期 / 人工核验状态）。
    """
    from . import facts as facts_mod
    evidence_text = (tender_text or "") + "\n" + "\n".join(
        str(s.get("snippet") or "") for s in sources)
    fact_rows = store.list_facts(pid) if pid else []
    bad: list[str] = []
    for m in NUM_RE.finditer(text or ""):
        token = m.group(0).strip()
        r = facts_mod.verify_token(token, evidence_text, fact_rows)
        if not r["ok"] and token not in bad:
            bad.append(token)
    return bad[:12]
