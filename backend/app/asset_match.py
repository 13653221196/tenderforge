"""P3 得分材料反查：把评分 / 硬性条款与企业资产台账做确定性对照。

只回答两个问题：**这条条款可能需要什么材料、本企业现在有没有**。
刻意不做三件事：

- 不判定「有这个证能得 X 分」——分值分配、评标口径、原件真伪与采购人裁量
  都不是程序该下的结论，模型也不该替业务人员猜；
- 不把「没匹配到关键词」当作「这条条款不需要材料」——无命中就返回空清单，
  如实说「未识别到可对照的材料类型」；
- 不自动补录资产、不改要求表、不调模型、不写库。

无命中返回空清单 + 说明，绝不编造条目。
"""
from __future__ import annotations

import re
from datetime import date

from . import assets, certificates, reqtable, store

MAX_ROWS = 40

# 关键词尽量具体，避免「证书」「报告」这类泛词把全部条款都算成缺件
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "qualification": ("营业执照", "资质证书", "资质等级", "许可证", "体系认证",
                      "ISO9001", "ISO14001", "认证证书", "资格要求", "投标资格",
                      "安全生产许可证", "备案证明"),
    "personnel": ("项目经理", "建造师", "技术负责人", "职称证书", "社保", "人员配备",
                  "上岗证", "安全员", "项目负责人", "专业人员"),
    "performance": ("类似业绩", "同类业绩", "业绩证明", "业绩合同", "中标通知书",
                    "验收证明", "履约证明", "项目案例", "合同复印件"),
    "testing": ("检测报告", "检验报告", "型式试验", "第三方检测", "测试报告",
                "产品认证", "检验合格证"),
    "award": ("获奖", "奖项", "荣誉证书", "优质工程", "鲁班奖", "奖状"),
}
NEEDED_MATERIAL = {
    "qualification": "资质 / 证照原件",
    "personnel": "人员证书与社保材料",
    "performance": "业绩合同 / 中标通知书 / 验收证明",
    "testing": "检测 / 检验报告",
    "award": "获奖 / 荣誉证明",
}
KIND_LABELS = {"rejection": "废标条款", "hard": "硬性要求", "scoring": "评分项"}
STATUS_LABELS = {
    "missing": "缺材料候选", "expired": "材料过期待更新",
    "unverified": "有材料待核验", "candidate": "已有候选材料",
}
STATUS_ORDER = {"missing": 0, "expired": 1, "unverified": 2, "candidate": 3}

NOTE = ("按条款关键词与企业资产台账做确定性比对，只列候选材料，"
        "不判定能否得分、不计算分值；请对照评分办法与原件逐条人工确认。")


def _key(text: object) -> str:
    return re.sub(r"\s+", "", str(text or ""))[:40]


def categories_of(text: str) -> list[str]:
    """一条条款最多关联两类材料，按命中的具体关键词多少排序。"""
    s = str(text or "")
    hits: list[tuple[int, str]] = []
    for cat, words in CATEGORY_KEYWORDS.items():
        n = sum(1 for w in words if w in s)
        if n:
            hits.append((n, cat))
    hits.sort(key=lambda x: (-x[0], list(CATEGORY_KEYWORDS).index(x[1])))
    return [c for _, c in hits[:2]]


def _valid_pairs(group: list[dict], today: date) -> tuple[list[dict], list[str]]:
    """拆出可用件与过期 / 日期写错的件，供状态判定与提醒。"""
    usable, bad = [], []
    for a in group:
        st = certificates.expiry_status(a.get("valid_until"), today=today)
        (bad if st in ("expired", "invalid") else usable).append(a)
    return usable, [str(a.get("title") or "") for a in bad]


def scan(pid: str, *, today: date | None = None) -> dict:
    """只读反查：输出缺件候选清单，绝不输出得分结论。"""
    t = today or date.today()
    try:
        items = reqtable.constraint_items(pid)
        scoring = reqtable.scoring_rows(pid)
    except Exception as exc:
        return {"rows": [], "total": 0, "truncated": False, "counts": {},
                "missing": 0, "expired": 0, "hint": "",
                "assets_total": 0, "no_requirements": True, "note": NOTE,
                "error": f"要求表没读出来（{type(exc).__name__}）"}
    score_map = {_key(r.get("item")): str(r.get("score") or "")
                 for r in scoring}
    all_assets = store.list_assets()
    by_cat: dict[str, list[dict]] = {c: [] for c in store.ASSET_CATEGORIES}
    for a in all_assets:
        by_cat.setdefault(str(a.get("category") or ""), []).append(a)

    rows: list[dict] = []
    for r in items:
        text = str(r.get("text") or "")
        if not text:
            continue
        for cat in categories_of(text):
            group = by_cat.get(cat) or []
            usable, bad = _valid_pairs(group, t)
            if not group:
                status, matched = "missing", []
            elif not usable:
                status, matched = "expired", []
            else:
                verified = [a for a in usable
                            if str(a.get("verification") or "").startswith("verified")]
                status = "candidate" if verified else "unverified"
                matched = [str(a.get("title") or "") for a in usable[:3]]
            rows.append({
                "requirement_id": str(r.get("id") or ""),
                "kind": str(r.get("kind") or ""),
                "kind_label": KIND_LABELS.get(str(r.get("kind") or ""), "要求"),
                "text": text[:220], "score": score_map.get(_key(text), ""),
                "loc": str(r.get("loc") or ""),
                "source_version": str(r.get("source_version") or ""),
                "category": cat, "category_label": assets.LABELS.get(cat, "未分类"),
                "needed_material": NEEDED_MATERIAL.get(cat, "相关证明材料"),
                "status": status, "status_label": STATUS_LABELS[status],
                "matched_assets": matched, "expired_assets": bad[:3],
                "note": NOTE,
            })
    rows.sort(key=lambda x: (STATUS_ORDER[x["status"]],
                             x["kind"] != "scoring", x["text"]))
    counts = {k: sum(1 for x in rows if x["status"] == k) for k in STATUS_LABELS}
    parts = ([f"缺 {counts['missing']} 项材料候选"] if counts["missing"] else []) \
        + ([f"{counts['expired']} 项材料过期"] if counts["expired"] else [])
    return {
        "rows": rows[:MAX_ROWS], "total": len(rows),
        "truncated": len(rows) > MAX_ROWS, "counts": counts,
        "missing": counts["missing"], "expired": counts["expired"],
        "candidate": counts["candidate"] + counts["unverified"],
        "hint": ("得分材料反查：" + "、".join(parts)
                 + "，只列候选，不代表一定失分") if parts else "",
        "assets_total": len(all_assets),
        "no_requirements": not items,
        "note": NOTE, "error": "",
    }
