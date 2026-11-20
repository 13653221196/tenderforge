"""B04 招标文件、补遗、澄清与整份替换的版本链。

上传与页面导入共用这一个入口：保存原件正文、版本顺序、有效标记；
旧自动要求转失效、人工修订标待复核；条款变化使正文、证据、检查与
导出确认进入待复核状态（导出令牌靠内容指纹自动失效，逐条响应靠版本比对降级）。
"""
from __future__ import annotations

from . import auto_core, reqtable, store

KIND_LABELS = {
    "original": "招标文件原文件", "amend": "补遗",
    "clarify": "澄清", "replace": "整份替换",
}


def ingest(pid: str, kind: str, text: str, filename: str = "", note: str = "",
           *, parse: dict | None = None) -> dict:
    kind = kind if kind in store.TENDER_KINDS else "original"
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "这一版没有正文内容"}
    p = store.get_project(pid) or {}
    # 编辑器里反复点「保存」但正文没变：不制造一堆内容相同的版本
    if kind == "replace" and text == str(p.get("tender_text") or "").strip():
        versions = store.list_tender_versions(pid)
        if versions:
            return {
                "ok": True, "unchanged": True,
                "version": {k: v for k, v in versions[-1].items() if k != "text"},
                "retired_requirements": 0, "stale_confirmations": 0,
                "requirements": {}, "current": store.current_tender_version(pid),
                "affected": "正文没有变化，未新增版本，也没有要求或确认失效",
                "items": [{k: v for k, v in v.items() if k != "text"} for v in versions],
            }
    prev_id = store.current_tender_version(pid)
    retired = store.retire_requirements_for_version(pid, prev_id) if prev_id else 0
    row = store.add_tender_version(pid, kind, text, filename=filename, note=note)
    label = KIND_LABELS.get(kind, "补充")
    if kind == "replace":
        merged = text
    elif kind == "original" and not str(p.get("tender_text") or "").strip():
        merged = text
    else:
        merged = (str(p.get("tender_text") or "")
                  + f"\n\n【第 {row['version']} 版·{label}·{filename or '未命名'}】\n" + text)
    fields: dict = {"tender_text": merged}
    if parse is not None:
        fields["tender_parse"] = dict(parse, filename=filename)
    store.update_project(pid, **fields)
    # 条款变了，之前逐条确认过的响应不能继续当作已落实
    stale_confirms = store.mark_responses_review(pid)
    # 只补新条款：原有条款与人工修订都保留，旧自动要求按来源版本转历史
    synced = reqtable.sync(pid)
    # 内容指纹含 tender_text 与要求表，旧导出确认令牌自然失效；
    # 阶段留痕缓存一并清掉，避免界面还显示上一版的检查结论
    auto_core.invalidate_cache()
    versions = store.list_tender_versions(pid)
    return {
        "ok": True,
        "version": {k: v for k, v in row.items() if k != "text"},
        "retired_requirements": retired,
        "stale_confirmations": stale_confirms,
        "requirements": synced,
        "current": row["id"],
        "affected": ("旧自动要求已转失效，人工修订标「新版待复核」；"
                     "相关正文、证据、检查与逐条响应确认需重新复核，导出确认令牌已失效"),
        "items": [{k: v for k, v in v.items() if k != "text"} for v in versions],
    }


def summary(pid: str) -> dict:
    rows = store.list_tender_versions(pid)
    return {
        "items": [{k: v for k, v in r.items() if k != "text"} for r in rows],
        "current": store.current_tender_version(pid),
        "labels": KIND_LABELS,
        "kinds": list(store.TENDER_KINDS),
    }
