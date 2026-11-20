"""招标要求表：自动抽取只是初稿，人工修订才是唯一事实源。

以前硬性要求 / 废标条款 / 评分项只存在基座结果和 projects.summary 里，
页面只能看，抽错一条，后面的大纲约束、逐章写作、覆盖度、导出门禁会一起错。

现在：导入招标文件时自动落表（source=auto）；页面可增删改 / 停用，
改过的行标为 manual；自动重抽只补新行，绝不覆盖人工行。
大纲 / 写作 / 覆盖度 / 评分项覆盖 / 导出门禁统一读 effective()，
表为空时才回落到原来的自动抽取，保证旧项目不受影响。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from . import core_bridge, rules, store

KINDS = ("hard", "rejection", "scoring")
KIND_LABELS = {"hard": "硬性要求", "rejection": "废标条款", "scoring": "评分项"}
FIELD_BY_KIND = {
    "hard": ("text", "title", "item", "detail", "值"),
    "rejection": ("clause", "text", "item", "detail", "值"),
    "scoring": ("item", "text", "name", "title", "值"),
}
SCORE_FIELDS = ("score", "分值", "分数")
_SYNCING: set[tuple[str, str]] = set()   # 企业库与项目共同防止懒补齐重入

# 招标文件第三章里大量条款约束的是招标方自己：「招标人不得泄露…」
# 「评标委员会应当…」。它们不是投标人要交材料、要逐条响应的要求。
OWNER_NOTE = "owner_obligation · 招标方义务，非投标人须响应项"
OWNER_RE = re.compile(
    r"(?:招标人|采购人|招标代理机构|评标委员会|评审委员会|招标方|采购方)"
    r"[^。；;\n]{0,12}(?:不得|应|须|应当|负责|有权|必须|不予|禁止)")


def is_owner_obligation(text: str, note: str = "") -> bool:
    """判断某条要求是不是「招标方自己的义务」，而不是投标人的必需要求。"""
    if str(note or "").startswith("owner_obligation"):
        return True
    return bool(OWNER_RE.search(str(text or "")))


def tender_hash(pid: str) -> str:
    p = store.get_project(pid) or {}
    text = str(p.get("tender_text") or "")
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()


def ensure_synced(pid: str) -> None:
    """招标书变了就自动补一次要求表，覆盖所有写入路径（REST / 对话 / 导入）。

    靠 tender_hash 标记：人工增删改后只要招标书没变就不再重抽，删空的行也不会被回填。
    这样对话框上传招标书时，约束 / 覆盖度 / 门禁同样能吃到「人工修订优先」的表。
    """
    try:
        h = tender_hash(pid)
        if not (store.get_project(pid) or {}).get("tender_text"):
            return
        key = (str(store.db_path().resolve()),str(pid))
        if key in _SYNCING or store.get_req_sync_hash(pid) == h:
            return
        _SYNCING.add(key)
        try:
            sync(pid)
        finally:
            _SYNCING.discard(key)
    except Exception:
        # 懒补齐失败不能拖垮读路径；上层仍会回落到自动抽取
        return


def _clean(value: Any, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _rows(result: dict) -> list[dict]:
    out: list[dict] = []
    for sec in result.get("sections") or []:
        if sec.get("title") in ("items", "rows", "basic", "scoring", "clauses", "否决条款"):
            out.extend(sec.get("rows") or [])
    return [r for r in out if isinstance(r, dict)]


def _row_text(row: dict, kind: str) -> tuple[str, str]:
    text = ""
    for field in FIELD_BY_KIND[kind]:
        text = _clean(row.get(field))
        if text and not text.startswith("{"):
            break
    score = ""
    for field in SCORE_FIELDS:
        score = _clean(row.get(field), 30)
        if score:
            break
    return text, score


# ---------------- 评分项抽取质量闸门 ----------------
# 真实国网文件暴露的假阳性：前附表行「2.4 | 招标文件的异议 | 补充：…」被
# parse_scoring 当成「招标文件的异议 2.4 分」；评分细则里的「【10分】」又被
# 拼到行首货物名称上，抽出 102 条同名噪音。这些假评分项一旦进目录，就会
# 生成「技术方案·中标候选人履约能力审查」这类根本不存在的章节。
# 真正的评分表是「项目 | 评审要素（分值区间） | 评审细则」，这里按这个结构
# 重抽，并对基座结果做严格过滤；不改动基座本身，避免影响模拟评标口径。
_SCORE_RANGE = re.compile(
    r"[（(]\s*(-?\d{1,3}(?:\.\d+)?)\s*[-~—至]\s*(-?\d{1,3}(?:\.\d+)?)\s*分?\s*[）)]")
_SCORE_SINGLE = re.compile(r"[（(]\s*(\d{1,3}(?:\.\d+)?)\s*分\s*[）)]")
_GROUP_PREFIX = re.compile(r"^[一二三四五六七八九十]+\s*[、.．]")
_BAD_SCORING = ("异议", "须知", "前附表", "条款", "澄清", "补充的其他内容",
                "签订合同", "中标通知", "开标时间", "公示媒介", "纪律要求",
                "构成招标文件", "采用电子招标投标", "确定中标人", "文件组成",
                "开标仪式", "评审要素", "评审细则", "评分标准", "分值构成")
_SCORE_HEAD = re.compile(r"^\d+(?:\.\d+)?\s*分?$")


def _clean_item(raw: Any) -> str:
    s = re.sub(r"[（(][^）)]{0,44}[）)]", "", str(raw or ""))
    s = re.sub(r"^[一二三四五六七八九十\d]{1,3}\s*[、.．)）]\s*", "", s.strip())
    s = re.sub(r"\s+", "", s).strip(" :：、|·-\"'“”")
    return s[:30]


def valid_scoring(item: str, score: Any) -> bool:
    """只让像「评分要素 × 分值」的行进要求表，条款号冒充分值一律不收。"""
    t = _clean_item(item)
    if not (2 <= len(t) <= 30) or any(k in t for k in _BAD_SCORING):
        return False
    if any(ch in t for ch in ("【", "】", "|", "。", "；", "{", "}")):
        return False
    if re.fullmatch(r"[\d\s.分（）()-]+", t):
        return False
    m = re.search(r"\d+(?:\.\d+)?", str(score or ""))
    if not m:
        return False
    v = float(m.group(0))
    return 0.5 <= v <= 100


def _score_value(score: Any) -> float:
    m = re.search(r"\d+(?:\.\d+)?", str(score or ""))
    return float(m.group(0)) if m else 0.0


def extract_scoring_items(tender_text: str, *, limit: int = 24) -> list[dict]:
    """从「项目 | 评审要素（a-b） | 评审细则」型评分表抽真实评分项。

    也兼容「技术方案（30分）」这种单值写法。扣分项区间（如 -115-0）不是
    写作依据，直接跳过；同一要素在多行细则里重复出现时按最高分保留一条。
    """
    out: dict[str, dict] = {}
    for raw in str(tender_text or "").splitlines():
        line = str(raw or "").strip()
        if not line or len(line) > 3000 or "评分" in line and "|" not in line and "分" not in line:
            continue
        cells = [x.strip() for x in line.split("|")] if "|" in line else [line]
        # 表格行优先取「评审要素」列（索引 1），纯文本行取行首
        cand = cells[1] if len(cells) >= 2 and cells[1] else cells[0]
        m = _SCORE_RANGE.search(cand) or _SCORE_SINGLE.search(cand)
        if not m and len(cells) >= 2:
            cand = cells[0]
            m = _SCORE_RANGE.search(cand) or _SCORE_SINGLE.search(cand)
        if not m:
            continue
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.lastindex and m.lastindex >= 2 else float(m.group(1))
        if lo < 0 or hi < 0 or hi > 100:      # 扣分 / 异常区间不作写章依据
            continue
        name = _clean_item(cand[:m.start()])
        if _GROUP_PREFIX.match(str(cand).strip()):
            continue                            # 「一、履约能力评价（20-28）」是大项不是要素
        if not valid_scoring(name, hi):
            continue
        rule = ""
        if len(cells) >= 3:
            rule = _clean(cells[2], 160)
        row = {"item": name, "score": f"{hi:g}分", "rule": rule}
        old = out.get(name)
        if not old or _score_value(row["score"]) > _score_value(old["score"]):
            out[name] = row
    ranked = sorted(out.values(), key=lambda r: -_score_value(r["score"]))
    return ranked[:max(1, limit)]


def _clean_scoring_rows(rows: list[dict], tender_text: str) -> list[dict]:
    """基座评分结果 + 表格重抽结果合并；人工修订行由调用方另外保留。"""
    merged: dict[str, dict] = {}
    for r in list(rows) + extract_scoring_items(tender_text):
        text = _clean(r.get("text") or r.get("item") or "", 160)
        score = _clean(r.get("score") or "", 30)
        if not valid_scoring(text, score):
            continue
        name = _clean_item(text)
        item = {"kind": "scoring", "text": text, "score": score,
                "note": _clean(r.get("rule") or r.get("note") or "", 160),
                "source": "auto", "status": "active"}
        old = merged.get(name)
        if not old or _score_value(score) > _score_value(old["score"]):
            merged[name] = item
    return sorted(merged.values(), key=lambda r: -_score_value(r["score"]))


def sync(pid: str, restore: bool = False) -> dict:
    """从招标书重抽三类要求；只补自动行，人工新增 / 改过 / 删过的行不复活。

    restore=True 只恢复「被整行删除」的自动要求；人工修订行仍保留修订版。
    """
    added: dict[str, int] = {}
    errors: list[str] = []
    if restore:
        store.clear_suppressed(pid)
    for cap, kind in (("requirements", "hard"), ("rejection", "rejection"),
                      ("scoring", "scoring")):
        try:
            result = core_bridge.run(cap, pid, {})
        except Exception as exc:
            errors.append(f"{cap}: {type(exc).__name__}")
            result = {}
        if isinstance(result, dict):
            if result.get("error") == "cap_disabled":
                continue  # 用户在设置里主动停用，不算抽取失败
            if result.get("ok") is False or result.get("error"):
                errors.append(f"{cap}: {str(result.get('error') or 'failed')[:80]}")
                result = {}
        rows = []
        for row in _rows(result):
            if kind == "scoring":
                continue  # 评分项走下方质量闸门：基座把前附表条款号当成过分值
            text, score = _row_text(row, kind)
            if not text or len(text) < 4:
                continue
            version = store.current_tender_version(pid)
            rows.append({"kind": kind, "text": text, "score": score,
                         "source": "auto", "status": "active",
                         "loc": str(row.get("loc") or row.get("page") or "")[:120],
                         "source_version": version,
                         "required": 1 if kind in ("hard", "rejection") else 0})
        # 基座没抽到硬性要求时，用本地确定性规则兜底，不让表空着
        if kind == "hard" and not rows:
            p = store.get_project(pid) or {}
            version = store.current_tender_version(pid)
            rows = [{"kind": "hard", "text": _clean(t), "score": "",
                     "source": "auto", "status": "active",
                     "source_version": version, "required": 1}
                    for t in rules.extract_requirements(p.get("tender_text") or "")]
        if kind == "scoring":
            p = store.get_project(pid) or {}
            tender = str(p.get("tender_text") or "")
            rows = _clean_scoring_rows([], tender)
            if not rows:
                # 没有「评审要素（a-b）」型评分表时，才退回基座的评分矩阵结果，
                # 且仍然过一遍闸门，不让条款号 / 细则里的【N分】混进要求表。
                base = [dict(text=_row_text(r, "scoring")[0],
                             score=_row_text(r, "scoring")[1])
                        for r in _rows(result)]
                rows = _clean_scoring_rows(base, "")
            version = store.current_tender_version(pid)
            for r in rows:
                r["loc"] = "评分办法"
                r["source_version"] = version
                r["required"] = 0
        # 按主语把招标方义务标出来：它们会出现在要求表里供人核对，但
        # required=0、也不进 effective() / 逐条响应 / 必需要求计数。
        for r in rows:
            if is_owner_obligation(str(r.get("text") or "")):
                r["note"] = OWNER_NOTE
                r["required"] = 0
        # 补遗 / 澄清后旧条款仍在新全文里：先原地恢复，再补真正的新条款。
        # 否则 retire + 按历史文本去重会让「没改过的旧要求」凭空消失。
        texts = [str(r.get("text") or "").strip() for r in rows]
        texts = [t for t in texts if t]
        revived = (store.reactivate_requirements(
            pid, texts, store.current_tender_version(pid)) if texts else 0)
        added[kind] = store.add_requirements(pid, rows) + revived
    if errors:
        # 抽取失败不能写同步 hash：否则 ensure_synced 会以为已经同步过，
        # 零要求在矩阵里还会算出「可交付」。留着标记，下次读路径自动重试。
        return {"ok": False, "added": added,
                "total": len(store.list_requirements(pid)), "errors": errors,
                "note": "本次抽取有失败项，未写入同步标记，下次会自动重试"}
    store.set_req_sync_hash(pid, tender_hash(pid))
    return {"ok": True, "added": added, "total": len(store.list_requirements(pid)),
            "note": "人工新增 / 改过的行标为 manual，重抽不会覆盖"}


def list_rows(pid: str) -> list[dict]:
    ensure_synced(pid)
    return store.list_requirements(pid)


def add_row(pid: str, kind: str, text: str, score: str = "", note: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError("kind 只能是 hard / rejection / scoring")
    text = _clean(text, 300)
    if len(text) < 2:
        raise ValueError("要求内容不能为空")
    # 用户在这一组里新增要求，等于把这个大项放回来
    store.show_requirement_group(pid, kind)
    n = store.add_requirements(pid, [{"kind": kind, "text": text,
                                      "score": _clean(score, 30),
                                      "note": _clean(note, 300),
                                      "source": "manual", "status": "active"}])
    return {"ok": bool(n), "added": n}


def update_row(pid: str, rid: str, patch: dict) -> dict:
    ok = store.update_requirement(pid, rid, patch or {})
    return {"ok": ok, "error": "" if ok else "要求不存在或没有可修改字段"}


def delete_row(pid: str, rid: str) -> dict:
    """删一条具体要求（小项）；传分类标识则删整组（大项）。两类都允许删。"""
    if str(rid) in KINDS:
        return delete_group(pid, str(rid))
    ok = store.delete_requirement(pid, rid)
    return {"ok": ok, "deleted": 1 if ok else 0,
            "error": "" if ok else "这条要求不存在或已被删除"}


def delete_group(pid: str, kind: str) -> dict:
    """删掉一个大项（整组要求）；删过的文本进抑制名单，重抽不会立刻补回。"""
    if kind not in KINDS:
        return {"ok": False, "deleted": 0, "error": "没有这个分类"}
    rows = [r for r in store.list_requirements(pid) if r.get("kind") == kind]
    n = sum(1 for r in rows if store.delete_requirement(pid, str(r["id"])))
    store.hide_requirement_group(pid, kind)
    return {"ok": True, "deleted": n, "group": kind, "hidden": True,
            "label": KIND_LABELS[kind],
            "note": "这个大项已从要求表移除；需要找回时点「恢复被删要求」"}


def effective(pid: str) -> dict[str, list[dict]]:
    """启用中的要求；同文本 manual 优先于 auto，避免重抽后出现重复口径。"""
    ensure_synced(pid)
    rows = [r for r in store.list_requirements(pid)
            # 停用、失效（被新版条款撤销）都不参与当前门禁；标不适用的单独走复核流程
            if (r.get("status") or "active") in ("active", "na")
            # 招标方 / 评标委员会自己的义务不是投标人须响应的要求。
            # 读时过滤，旧项目里已落库的噪声行也立刻不再算必需要求。
            and not is_owner_obligation(str(r.get("text") or ""),
                                        str(r.get("note") or ""))]
    seen: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda x: 0 if x.get("source") == "manual" else 1):
        key = (str(r.get("kind") or "hard"), _clean(r.get("text")))
        if key[1] and key not in seen:
            seen[key] = r
    out: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in seen.values():
        out.setdefault(str(r.get("kind") or "hard"), []).append(r)
    return out


def hard_texts(pid: str) -> list[str]:
    """供逐条响应覆盖 / 实质响应使用；全部生效要求，不再截断到前 24 条。"""
    return [_clean(r.get("text"), 160) for r in effective(pid)["hard"]
            if _clean(r.get("text"))]


def scoring_rows(pid: str) -> list[dict]:
    return [{"item": _clean(r.get("text"), 160),
             "score": _clean(r.get("score"), 30)}
            for r in effective(pid)["scoring"] if _clean(r.get("text"))]


def constraint_items(pid: str) -> list[dict]:
    """带稳定 ID、原文位置、来源版本与人工确认状态的完整要求清单。"""
    eff = effective(pid)
    out = []
    for kind in KINDS:
        for r in eff.get(kind, []):
            out.append({
                "id": r.get("id"), "kind": kind, "text": _clean(r.get("text"), 300),
                "required": int(r.get("required") if r.get("required") is not None else 1),
                "loc": str(r.get("loc") or ""),
                "source_version": str(r.get("source_version") or ""),
                "confirmed_at": str(r.get("confirmed_at") or ""),
                "status": str(r.get("status") or "active"),
            })
    return out


def constraint_batches(pid: str, size: int = 12) -> dict:
    """分批：超长要求清单按批交给逐章写作，记录已处理/未处理范围。"""
    items = constraint_items(pid)
    size = max(1, int(size or 12))
    batches = [{"index": i // size + 1, "items": items[i:i + size]}
               for i in range(0, len(items), size)]
    return {"total": len(items), "size": size, "batches": batches,
            "processed": 0, "unprocessed": len(items),
            "note": "processed/unprocessed 由调用方按已写章节回填，不能用批次数冒充完成度"}


def outline_constraints(pid: str, size: int = 12) -> str:
    """大纲阶段的分批概览：本批要求 + 总数 + 未处理范围。

    大纲只需要知道有哪些类型的要求来排章节，不需要一次吞下几百条；
    其余要求在逐章写作时按相关度分配，这里明确写出「还有多少条没进本批」，
    不把「没看完」伪装成「全部处理过」。
    """
    items = constraint_items(pid)
    if not items:
        return ""
    size = max(1, int(size or 12))
    one = items[:size]
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    kinds = "、".join(f"{KIND_LABELS.get(k, k)} {v} 条" for k, v in by_kind.items())
    lines = [f"- [{it['id']}] {it['text'][:110]}" for it in one]
    return ("\n\n【招标要求分批概览：共 " + str(len(items)) + " 条（" + kinds + "）】\n"
            + "\n".join(lines)
            + "\n【本批 " + str(len(one)) + "/" + str(len(items)) + " 条，未处理 "
            + str(len(items) - len(one)) + " 条；其余要求会在逐章写作时按章节相关度分配，"
            + "完整清单见要求表与逐条响应矩阵，不得只响应本批】")


def constraints_for_chapter(pid: str, chapter_title: str, limit: int = 12) -> str:
    """相关章节分配：按章标题与要求文本的字面相关度挑本批，不无限扩大 prompt。"""
    items = constraint_items(pid)
    title = re.sub(r"\W", "", str(chapter_title or ""))
    tg = {title[i:i + 2] for i in range(max(0, len(title) - 1))}
    scored = []
    for it in items:
        text = re.sub(r"\W", "", it["text"])
        ig = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        score = len(tg & ig) / max(1, len(tg)) if tg else 0
        scored.append((score + (0.2 if it["kind"] == "hard" else 0), it))
    scored.sort(key=lambda x: -x[0])
    picked = [it for s, it in scored[:max(1, limit)] if s > 0] or [it for _, it in scored[:limit]]
    if not picked:
        return ""
    return ("【本章相关要求，逐条响应；完整清单见要求表，共 " + str(len(items)) + " 条】\n"
            + "\n".join(f"- [{it['id']}] {it['text']}" for it in picked))


def constraints_text(pid: str) -> str:
    """写作 / 大纲共用的完整约束文本：人工修订版优先，不再只拼前 8 条 / 3000 字。

    逐章写作请改用 constraints_for_chapter 按相关度分批喂，避免无限扩大 prompt。
    """
    items = constraint_items(pid)
    if not items:
        return ""
    lines = []
    for kind, label in (("hard", "必须逐条响应的硬性要求"),
                        ("rejection", "会导致废标的条款"),
                        ("scoring", "评分项")):
        vals = [f"[{it['id']}] {it['text'][:110]}" for it in items if it["kind"] == kind]
        if vals:
            lines.append("【" + label + f"（共 {len(vals)} 条，人工修订优先）】" + "；".join(vals))
    return ("\n\n【招标要求表，写作必须逐条响应，不得与之冲突；共 "
            + str(len(items)) + " 条】\n" + "\n".join(lines))


def summary(pid: str) -> dict:
    rows = list_rows(pid)
    eff = effective(pid)
    hidden = set(store.hidden_requirement_groups(pid))
    return {
        "total": len(rows),
        "effective": sum(len(v) for v in eff.values()),
        "manual": sum(1 for r in rows if r.get("source") == "manual"),
        "auto": sum(1 for r in rows if r.get("source") == "auto"),
        "disabled": sum(1 for r in rows if (r.get("status") or "active") == "disabled"),
        "by_kind": {k: {"rows": sum(1 for r in rows if r.get("kind") == k),
                        "effective": len(v)} for k, v in eff.items()},
        "hidden_groups": sorted(hidden),
        # 被删掉的大项不再下发到界面，界面上不会继续显示一个空分组
        "labels": {k: v for k, v in KIND_LABELS.items() if k not in hidden},
        "claim": "人工修订优先；表为空时回落到基座自动抽取",
    }
