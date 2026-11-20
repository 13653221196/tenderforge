"""输出交付类技能：检查报告、版本对比。"""
from __future__ import annotations

import difflib
import re

from ._common import item, result, table


def check_report(ctx: dict) -> dict:
    """把各核查维度汇总成一份可直接交给同事评审的 Markdown 报告。"""
    from .check import (  # 延迟导入避免循环
        consistency_check,
        doc_integrity,
        pricing_logic,
        qualification_check,
        risk_score,
        selfcheck_list,
        validity_check,
    )

    tender = ctx.get("tender_text") or ""
    bid = ctx.get("bid_text") or ""
    project = (ctx.get("project_name") or ctx.get("project_id") or "未命名项目").strip()

    parts: list[str] = [f"# 投标文件检查报告\n", f"- 项目：{project}", ""]
    summary = risk_score({**ctx, "bid_text": bid or "占位"})
    parts.append(f"- 综合风险：{summary['stats'].get('风险值', '—')} 分（{summary['stats'].get('等级', '—')}）")
    parts.append(f"- 招标文件字数：{len(tender)}；投标文件字数：{len(bid)}\n")

    blocks = [
        ("资格性检查", qualification_check),
        ("投标有效期", validity_check),
        ("文件完整性", doc_integrity),
        ("跨章节一致性", consistency_check),
        ("报价逻辑", pricing_logic),
        ("废标自查", selfcheck_list),
    ]
    rows: list[list] = []
    for title, fn in blocks:
        data = fn(ctx)
        parts.append(f"## {title}\n")
        parts.append(f"{data.get('summary', '')}\n")
        rows.append([title, {"pass": "通过", "warn": "待修", "fail": "不通过", "info": "待确认"}.get(
            data.get("verdict", "info"), "—"), data.get("summary", "")[:40]])
        issues = [i for i in data.get("items") or [] if i.get("level") in ("high", "mid")]
        if issues:
            for issue in issues[:8]:
                line = f"- **[{issue['level']}] {issue['title']}**"
                if issue.get("detail"):
                    line += f"：{issue['detail']}"
                if issue.get("evidence"):
                    line += f"（依据：{issue['evidence'][:80]}）"
                if issue.get("advice"):
                    line += f" → 建议：{issue['advice']}"
                parts.append(line)
            parts.append("")

    parts.append("---\n")
    parts.append("本报告由规则引擎离线生成，未调用外部服务；签章、密封、原件等实物项仍需人工复核。")
    markdown = "\n".join(parts)

    data = result(
        f"已生成检查报告：6 个维度，风险 {summary['stats'].get('风险值', '—')} 分。",
        [],
        verdict="info",
        stats={"字数": len(markdown)},
        tables=[table("维度结论", ["维度", "结论", "摘要"], rows)],
        notice="报告正文在下方 Markdown 区域，可直接复制到 Word 或邮件。",
    )
    data["markdown"] = markdown
    # 兼容 Python 3.10/3.11：3.12 之前 f-string 表达式内不允许反斜杠，
    # 正则先算成变量再插值，行为不变。
    slug = re.sub(r'[^\\w\\u4e00-\\u9fa5]+', '_', project)[:30]
    data["filename"] = f"检查报告_{slug}.md"
    return data


def revision(ctx: dict) -> dict:
    """对比两版正文，输出新增/删除/修改统计。"""
    params = ctx.get("params") or {}
    old = (params.get("old_text") or "").strip()
    new = (ctx.get("bid_text") or "").strip()
    if not old:
        return result("未提供旧版本正文。",
                      [item("info", "需要一份旧稿", "在参数里粘贴上一版正文，再上传/粘贴当前版本即可对比。")],
                      verdict="info")
    if not new:
        return result("未提供当前版本正文。",
                      [item("mid", "缺少当前版本", "请上传或粘贴待对比的当前稿。")], verdict="info")

    old_lines = [l.strip() for l in old.split("\n") if l.strip()]
    new_lines = [l.strip() for l in new.split("\n") if l.strip()]
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    added, removed, changed = [], [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            changed.append([f"{i1 + 1}", old_lines[i1][:60], new_lines[j1][:60]])
        elif tag == "insert":
            added.extend(new_lines[j1:j2][:5])
        elif tag == "delete":
            removed.extend(old_lines[i1:i2][:5])

    ratio = matcher.ratio()
    items = [item("info", f"相似度 {ratio * 100:.1f}%",
                  f"原始 {len(old_lines)} 段，当前 {len(new_lines)} 段。",
                  advice="相似度低于 60% 通常意味着大改，建议复核前后逻辑口径是否一致。")]
    if added:
        items.append(item("low", f"新增 {len(added)} 段", "；".join(a[:30] for a in added[:3])))
    if removed:
        items.append(item("mid", f"删除 {len(removed)} 段", "；".join(r[:30] for r in removed[:3]),
                          advice="确认删减不是误删（尤其是承诺性条款）。"))
    for row in changed[:5]:
        items.append(item("low", f"改写：{row[1][:20]}…", f"改为：{row[2][:40]}"))

    return result(
        f"相似度 {ratio * 100:.1f}%；改写 {len(changed)} 处、新增 {len(added)} 段、删除 {len(removed)} 段。",
        items,
        verdict="info",
        stats={"相似度": f"{ratio * 100:.1f}%", "改写": len(changed), "新增": len(added), "删除": len(removed)},
        tables=[table("改写对照", ["行", "旧稿", "当前稿"], changed[:20])] if changed else [],
        notice="按段落比对，不追踪句内字词级改动。",
    )
