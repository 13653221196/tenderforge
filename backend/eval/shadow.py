"""影子运行：连续 3 个项目与既有结论并行比对，只出报告，不写业务库、不对外输出。

口径：只把「当前确定性规则层重算结果」与「上次留痕中同源的规则层结论」相比。
留痕里还有模型复核 / 本地基座补充的结论（source_ref 前缀不同），那两类本来就不在
规则层输出里，混在一起比会把一致率压成假阴性；单列展示，不做一致性判定。

运行：python backend/eval/shadow.py
输出：backend/eval/shadow.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import rules, store  # noqa: E402


def _key(issue: dict) -> tuple[str, str, str]:
    return (str(issue.get("title") or "")[:30],
            str(issue.get("section_id") or ""),
            str(issue.get("anchor") or "")[:20])


def _is_rule(issue: dict) -> bool:
    """留痕是否由确定性规则层产生（code R01… 或 source_ref 前缀「规则判定·」）。"""
    return bool(re.match(r"^R\d{2}$", str(issue.get("code") or ""))) or \
        str(issue.get("source_ref") or "").startswith("规则判定·")


def shadow(limit: int = 3) -> dict:
    projects = store.list_projects()[:limit]
    rows = []
    for p in projects:
        proj = store.get_project(p["id"])
        fresh = rules.rule_check(proj.get("sections") or [], proj.get("tender_text") or "")
        all_checks = store.list_checks(p["id"])
        last_round = max((c["round"] for c in all_checks), default=0)
        last_checks = [c for c in all_checks if c["round"] == last_round]
        stored = [c for c in last_checks if _is_rule(c)]
        other = len(last_checks) - len(stored)
        fmap = {_key(i): i for i in fresh}
        smap = {_key(i): i for i in stored}
        added = [fmap[k]["title"] for k in fmap.keys() - smap.keys()]
        removed = [smap[k]["title"] for k in smap.keys() - fmap.keys()]
        removed_refs = sorted({str(smap[k].get("source_ref") or "未标注")
                               for k in smap.keys() - fmap.keys()})
        same = len(fmap.keys() & smap.keys())
        has_baseline = bool(last_checks)
        rows.append({
            "project": p["name"], "project_id": p["id"],
            "sections": len(proj.get("sections") or []),
            "fresh_issues": len(fresh), "stored_issues": len(stored),
            "other_source_issues": other, "has_baseline": has_baseline,
            "same": same, "added": added[:8], "removed": removed[:8],
            "removed_refs": removed_refs,
            "agreement": (round(same / max(1, len(fmap.keys() | smap.keys())), 4)
                          if has_baseline else None),
        })
    report = {
        "projects": len(rows), "target_projects": limit,
        "sufficient_coverage": len(rows) >= limit,
        "comparable_projects": sum(1 for x in rows if x["has_baseline"]),
        "rows": rows,
        "passed": all(not x["has_baseline"] or (x["agreement"] or 0) >= 0.9 for x in rows),
        "note": "影子运行只读：未写回检查结果、未改写章节、未导出文件。"
                "「stored_issues」为上一次体检留痕中的规则层结论；"
                "「other_source_issues」为模型复核 / 本地基座补充结论，不参与一致性判定。"
                "从未体检过的项目标 has_baseline=false、agreement=null，不判为不一致。"
                "有基线的项目出现差异，通常来自体检后正文被改写、人工采纳，或规则层调整后"
                "旧留痕尚未刷新（见 removed_refs）；供人工归因，不自动改写任何内容。"
                "覆盖达标只说明项目数够，comparable_projects 才是有基线、真正可比的数量。",
    }
    out = ROOT / "backend" / "eval" / "shadow.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    r = shadow()
    print("== 影子运行（只读比对）==")
    for x in r["rows"]:
        agree = "无基线" if not x["has_baseline"] else f"{x['agreement']:.2f}"
        print(f"  {x['project'][:18]:<20} 规则 {x['fresh_issues']:>2} 条 / 留痕 {x['stored_issues']:>2} 条 "
              f"· 一致率 {agree} · 新增 {len(x['added'])} / 消失 {len(x['removed'])} "
              f"· 其他来源 {x['other_source_issues']} 条")
    print("覆盖项目:", r["projects"], "/", r["target_projects"],
          "| 有基线可比:", r["comparable_projects"],
          "| 覆盖达标:", "是" if r["sufficient_coverage"] else "否",
          "| 验收:", "通过" if r["passed"] else "需人工归因",
          "| 报告: backend/eval/shadow.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
