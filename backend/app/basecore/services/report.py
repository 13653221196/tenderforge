"""检查报告导出：把体检/检查结果组装成正式 Word 报告。

复用 export_docx 的版式引擎——报告本身就是一组带 Markdown 表格的章节，
封面写"检查报告"，正文按 概要 → 问题清单 → 技能体检 → 废标核对 → 评标参考 展开。
"""
from __future__ import annotations

import json
from pathlib import Path

from .docx_export import export_docx
from .formats import resolve


def _esc(text: object) -> str:
    return str(text or "").replace("|", "丨").replace("\n", " ").strip()


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join([" --- " for _ in headers]) + "|"
    body = "\n".join("| " + " | ".join(_esc(c) for c in row) + " |" for row in rows)
    return "\n".join([head, sep, body]) if rows else "（无记录）"


def build_check_report(
    *,
    project_name: str,
    bidder: str = "",
    checks: list[dict],
    skills: list[dict] | None = None,
    rejections: list[dict] | None = None,
    evaluate: dict | None = None,
    blind: dict | None = None,
    out_path: Path,
    fmt: dict | None = None,
) -> Path:
    fmt = fmt or resolve()
    summary = {"高": 0, "中": 0, "低": 0}
    for item in checks:
        summary[item.get("level") or "中"] = summary.get(item.get("level") or "中", 0) + 1

    sections: list[dict] = []

    overview = [
        f"本报告由系统自动生成，检查对象为《{project_name}》投标文件。",
        "",
        f"共发现问题 **{len(checks)}** 项：高风险 **{summary.get('高', 0)}** 项、"
        f"中风险 **{summary.get('中', 0)}** 项、低风险 **{summary.get('低', 0)}** 项。",
        "",
        "| 风险等级 | 数量 | 说明 |",
        "| --- | --- | --- |",
        f"| 高 | {summary.get('高', 0)} | 可能导致废标或严重失分，必须处理 |",
        f"| 中 | {summary.get('中', 0)} | 影响评审印象，建议处理 |",
        f"| 低 | {summary.get('低', 0)} | 细节瑕疵，酌情处理 |",
    ]
    sections.append({"title": "第一章 检查概要", "level": 1, "content": "\n".join(overview)})

    if checks:
        rows = [[i + 1, c.get("level"), c.get("code"), c.get("title"), c.get("detail"), c.get("evidence")]
                for i, c in enumerate(checks)]
        issue_md = _md_table(["序号", "等级", "规则", "问题", "说明", "证据"], rows)
        sections.append({"title": "第二章 问题清单", "level": 1, "content": issue_md})

    if skills:
        rows = [[s.get("title") or s.get("name"), s.get("verdict"), s.get("score", ""),
                 _json_counts(s.get("counts")), s.get("summary")] for s in skills]
        skill_md = _md_table(["检查项", "结论", "评分", "高中低", "摘要"], rows)
        sections.append({"title": "第三章 技能体检", "level": 1, "content": skill_md})

    if blind:
        hits = blind.get("hits") or []
        fmts = blind.get("format_issues") or []
        parts = []
        if hits:
            rows = [[i + 1, h.get("kind"), h.get("word"), h.get("zone"), h.get("note")] for i, h in enumerate(hits[:40])]
            parts.append("### 暗标身份信息命中\n\n" + _md_table(["序号", "类型", "内容", "位置", "说明"], rows))
        if fmts:
            rows = [[i + 1, f.get("level"), f.get("title"), f.get("detail")] for i, f in enumerate(fmts[:20])]
            parts.append("### 暗标格式问题\n\n" + _md_table(["序号", "等级", "问题", "说明"], rows))
        if parts:
            sections.append({"title": "第四章 暗标合规", "level": 1, "content": "\n\n".join(parts)})

    if rejections:
        rows = [[i + 1, r.get("category"), r.get("clause"), r.get("status"), r.get("reason") or r.get("note")]
                for i, r in enumerate(rejections)]
        rej_md = _md_table(["序号", "分类", "条款", "判定", "理由/备注"], rows)
        sections.append({"title": "第五章 废标条款核对", "level": 1, "content": rej_md})

    if evaluate and evaluate.get("items"):
        rows = [[e.get("index"), e.get("item"), e.get("full"), e.get("score"), e.get("level"),
                 e.get("reason"), e.get("improve")] for e in evaluate["items"]]
        ev_md = _md_table(["序号", "评分项", "满分", "自评", "判断", "依据", "补分建议"], rows)
        ev_md += f"\n\n合计：**{evaluate.get('total', 0)} / {evaluate.get('full', 0)}**（{evaluate.get('rate', 0)}%）"
        sections.append({"title": "第六章 模拟评标参考", "level": 1, "content": ev_md})

    conclusion = [
        "请按以下顺序处理：",
        "",
        "1. 先修复第一章概要中的高风险问题，逐项核对后回填修复说明；",
        "2. 废标条款核对为 missing 的条目必须补齐材料或调整响应；",
        "3. 暗标合规命中的身份信息用「脱敏副本」功能一键处理；",
        "4. 全部处理完成后重新运行一键体检，确认问题清零。",
    ]
    sections.append({"title": "第七章 处理建议", "level": 1, "content": "\n".join(conclusion)})

    return export_docx(
        out_path=out_path,
        project_name=f"{project_name} 检查报告",
        sections=sections,
        doc_type="检查报告",
        bidder=bidder,
        date_text="",
        with_cover=True,
        with_toc=True,
        fmt=fmt,
    )


def _json_counts(counts: object) -> str:
    if isinstance(counts, str):
        try:
            counts = json.loads(counts)
        except json.JSONDecodeError:
            return counts
    if isinstance(counts, dict):
        return f"高{counts.get('high', 0)} 中{counts.get('mid', 0)} 低{counts.get('low', 0)}"
    return ""
