"""投标文件大纲生成：AI 生成目录树，失败时用标准结构兜底。"""
from __future__ import annotations

import json
import re

from ..config import LLMConfig
from .llm import LLMError, chat, parse_json

SYSTEM = (
    "你是投标文件编制专家，熟悉政府采购与工程招标的响应文件结构。"
    "你只输出 JSON，目录层级不超过 3 级，标题简洁、面向评委、与实际评分项对齐。"
)

TEMPLATE = """基于下面的招标文件信息，生成投标文件（响应文件）的章节目录。

要求：
1. 一级章节必须完整覆盖：投标函与承诺、资格证明、商务响应、技术方案、报价文件、附件；
2. 技术方案部分要针对本项目实际内容展开，不要用“方案一/方案二”这种空标题；
3. 有评分项时，技术方案章节要与评分项一一对应，每个节点用 scores 标注它响应了哪些评分项（引用评分项名称原文），没有对应的普通节点 scores 给空数组；
4. 评分办法里的每一项都必须出现在至少一个节点的 scores 里，不许遗漏；
5. 每个节点给一句 brief（编制要点，30 字内），供写正文时使用。

严格按此 JSON 输出：
{{"chapters": [{{"title": "第一章 投标函", "brief": "编制要点", "scores": [], "children": [{{"title": "1.1 投标函", "brief": "要点", "scores": []}}]}}]}}

评分办法（评分项名称必须与 scores 中的名称一致）：
{scoring}

项目信息：
{meta}

招标文件要点（节选）：
{excerpt}

我方为本项目提供的资料（用于细化方案章节；与招标文件冲突时以招标文件为准）：
{own}
"""

DEFAULT_STRUCTURE = [
    {"title": "第一章 投标函及投标函附录", "brief": "报价、工期、有效期三项必须与开标一览表一致",
     "children": [{"title": "1.1 投标函", "brief": "按格式填写，不得修改实质内容"},
                  {"title": "1.2 法定代表人身份证明", "brief": "附身份证复印件"},
                  {"title": "1.3 授权委托书", "brief": "非法人签署时必需"}]},
    {"title": "第二章 资格证明文件", "brief": "逐项对应资格要求，缺一项即废标",
     "children": [{"title": "2.1 营业执照", "brief": "经营范围需覆盖本项目"},
                  {"title": "2.2 资质证书", "brief": "按招标文件要求的等级提供"},
                  {"title": "2.3 财务状况报告", "brief": "近一年审计报告或银行资信证明"},
                  {"title": "2.4 类似业绩", "brief": "合同+验收证明，注意时间范围"}]},
    {"title": "第三章 商务响应", "brief": "偏离表是重点，负偏离需说明",
     "children": [{"title": "3.1 商务条款响应及偏离表", "brief": "逐条响应，避免负偏离"},
                  {"title": "3.2 售后服务承诺", "brief": "响应时间、质保期、备件"}]},
    {"title": "第四章 技术方案", "brief": "与评分项一一对应，突出可验证的指标",
     "children": [{"title": "4.1 项目理解与需求分析", "brief": "复述关键需求，体现读懂招标文件"},
                  {"title": "4.2 总体技术方案", "brief": "架构、技术路线、选型理由"},
                  {"title": "4.3 实施方案与进度计划", "brief": "里程碑、人员投入、风险应对"},
                  {"title": "4.4 质量保障措施", "brief": "质量目标、检查节点、验收标准"},
                  {"title": "4.5 售后服务与培训", "brief": "响应机制、培训计划"}]},
    {"title": "第五章 报价文件", "brief": "大小写金额一致，合价与单价可核算",
     "children": [{"title": "5.1 开标一览表", "brief": "总价、工期、有效期"},
                  {"title": "5.2 分项报价表", "brief": "单价×数量=合价，合计等于总价"}]},
    {"title": "第六章 附件", "brief": "招标文件要求的其他材料",
     "children": [{"title": "6.1 中小企业声明函", "brief": "符合条件时提供"},
                  {"title": "6.2 无重大违法记录声明", "brief": "按格式承诺"}]},
]


def default_outline() -> list[dict]:
    return json.loads(json.dumps(DEFAULT_STRUCTURE, ensure_ascii=False))


def _clean(nodes: list, level: int = 1) -> list[dict]:
    if level > 3 or not isinstance(nodes, list):
        return []
    cleaned: list[dict] = []
    for node in nodes[:24]:
        if not isinstance(node, dict):
            continue
        title = str(node.get("title", "")).strip()
        if not title:
            continue
        scores = node.get("scores") or []
        if not isinstance(scores, list):
            scores = []
        scores = [str(s).strip()[:60] for s in scores if str(s).strip()][:4]
        children = _clean(node.get("children") or [], level + 1)
        cleaned.append({
            "title": title[:60],
            "brief": str(node.get("brief", ""))[:80],
            "scores": scores,
            "children": children,
        })
    return cleaned


async def build_outline(cfg: LLMConfig, meta: dict, tender_text: str, own_materials: str = "") -> dict:
    if not cfg.ready:
        return {"chapters": default_outline(), "source": "template"}

    meta_text = json.dumps(meta, ensure_ascii=False)[:2000] if meta else "（未解读）"
    scoring = meta.get("scoring") or []
    scoring_text = "\n".join(
        f"- {row.get('item') or ''}（{row.get('score') or '分值未标注'}）：{row.get('rule') or ''}"
        for row in scoring[:20]
        if isinstance(row, dict)
    ) or "（招标解读里没有评分办法）"
    prompt = TEMPLATE.format(
        meta=meta_text, excerpt=tender_text[:12000], scoring=scoring_text,
        own=(own_materials or "（未提供）")[:1200],
    )
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=3000, task="outline",
        )
        data = parse_json(raw)
        chapters = _clean(data.get("chapters") or [])
        if len(chapters) < 3:
            raise LLMError("生成的大纲章节过少")
        return {"chapters": chapters, "source": "ai"}
    except LLMError as exc:
        return {"chapters": default_outline(), "source": "template", "notice": f"AI 生成失败（{exc}），已使用标准结构，可手动调整。"}


# ---------------- 评分覆盖率 ----------------
def _norm_name(text: str) -> str:
    return re.sub(r"[\s（）()：:，,。.、\-—]", "", text or "")


def _name_hit(name: str, label: str) -> bool:
    """评分项名称与节点标注的匹配：互相包含，或 2-gram 重合过半。"""
    a, b = _norm_name(name), _norm_name(label)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    if len(a) >= 4 and len(b) >= 4:
        grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
        grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
        if grams_a and grams_b:
            return len(grams_a & grams_b) / len(grams_a | grams_b) >= 0.45
    return False


def coverage(scoring: list[dict], nodes: list[dict]) -> dict:
    """按大纲节点 scores 标注计算评分项覆盖情况。

    节点没有 scores 标注时，退回用标题模糊匹配兜底——避免 AI 漏标导致全部显示未覆盖。
    """
    labels: list[tuple[str, str]] = []  # (标注/标题, 节点标题)

    def walk(items: list) -> None:
        for node in items or []:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "")
            marked = False
            for label in node.get("scores") or []:
                if str(label).strip():
                    labels.append((str(label).strip(), title))
                    marked = True
            if not marked and title:
                labels.append((title, title))  # 标题兜底
            walk(node.get("children"))

    walk(nodes)

    items: list[dict] = []
    missing: list[dict] = []
    hit_labels: set[tuple[str, str]] = set()
    for row in scoring or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("item") or "").strip()
        if not name:
            continue
        hits = [(label, title) for label, title in labels if _name_hit(name, label)]
        for pair in hits:
            hit_labels.add(pair)
        entry = {"item": name, "score": row.get("score") or "", "sections": [t for _, t in hits]}
        items.append(entry)
        if not hits:
            missing.append(entry)
    extra = [label for label, _ in labels if not any(_name_hit(name, label) for name in
              [str(r.get("item") or "") for r in scoring or [] if isinstance(r, dict)])]
    total = len(items)
    covered = total - len(missing)
    return {
        "total": total,
        "covered": covered,
        "missing": missing,
        "rate": round(covered / total * 100, 1) if total else 0,
        "items": items,
        "extra_labels": extra[:10],
        "notice": "" if total else "招标解读里没有评分办法，先在「招标解读」生成评分项再检查覆盖。",
    }
