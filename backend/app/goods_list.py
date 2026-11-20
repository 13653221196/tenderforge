"""招标清单结构化：只抽文件里真实存在的列，抽不到就如实说没有。

设计来源是一次真实文件复核，而不是照抄同类产品的宣传清单：
- 国网《货物清单》xlsx 有 24 列表头、57 行物资，含「物资名称 / 物资描述 /
  单位 / 数量 / 分包编号 / 交货日期」，**没有预算列**；
- 主招标文件里 ★▲ 出现 0 次，`*` 全是乘号或截面写法（1*500、1024*768），
  **不是参数等级标记**；
- 「最高限价」出现 9 次，但明确写着「本项目无最高限价」。

所以这里不造预算、不把 `*` 当★参数、不把「无」读成一个金额。
抽不到的东西一律返回空并标注原因，让人工核对，绝不猜测填充。

用途：给「解读完不用再翻原文」提供可核对的清单，并把漏数量 / 限价含糊
这类容易废标的点接进导出提醒。
"""
from __future__ import annotations

import re

from . import store

MAX_ROWS = 400
# 表头别名按优先级：命中第一个就停，避免「项目名称」抢走「物资名称」
HEADER_ALIASES = (
    ("name", ("物资名称", "设备名称", "货物名称", "物料名称", "材料名称", "名称")),
    ("spec", ("物资描述", "规格型号", "技术参数", "规格", "型号", "扩展描述")),
    ("unit", ("计量单位", "单位")),
    ("qty", ("需求数量", "采购数量", "数量")),
    ("package", ("分包编号", "包名称", "标包", "包号")),
    ("delivery", ("最后一批交货日期", "交货日期", "交期", "交货时间")),
    ("budget", ("预算金额", "预算", "最高单价", "单价")),
)
# 只认这四种符号为参数等级；* / × 在招标文件里是乘号、截面或分辨率
GRADE_MARKS = "★▲☆△"
CAP_NONE = re.compile(r"无最高限价|最高限价\s*[|｜:：]?\s*无(?![\d])|不设最高限价")
CAP_AMOUNT = re.compile(r"最高限价[^。；\n]{0,40}?(\d[\d,.]*)\s*(万元|万|元)")
NUM = re.compile(r"^\d+(?:\.\d+)?$")


def _cells(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    return [c.strip() for c in line.split("|")]


def _header(cells: list[str]) -> dict[str, int]:
    """同一字段可能命中多列，取匹配词最具体的那列。

    真实国网清单里既有「项目名称」也有「物资名称」，若按列顺序先命中
    「名称」就会把整列项目名当成物资名，所以不能先到先得。
    """
    cand: dict[str, list[tuple[int, str, bool]]] = {}
    for col, cell in enumerate(cells):
        c = re.sub(r"\s+", "", cell)
        if not c:
            continue
        for field, words in HEADER_ALIASES:
            for word in words:
                if word in c:
                    # 完全相等优先（「单位」胜过「项目单位」），
                    # 其次才是更长的匹配词（「物资名称」胜过「项目名称」）
                    cand.setdefault(field, []).append((col, word, c == word))
                    break
    return {f: max(v, key=lambda x: (x[2], len(x[1]), -x[0]))[0]
            for f, v in cand.items()}


def extract(text: str) -> dict:
    """从一段表格文本里抽货物清单；没有清单时返回空结果而不是编造。"""
    body = str(text or "")
    marks = {c: body.count(c) for c in GRADE_MARKS}
    lines = [l for l in body.splitlines() if l.strip()]
    best: tuple[int, dict[str, int]] | None = None
    for i, line in enumerate(lines[:200]):
        cells = _cells(line)
        if not cells or len(cells) < 2:
            continue
        mapping = _header(cells)
        if "name" not in mapping or "qty" not in mapping:
            continue
        # 表头本身不能带具体数值，否则会把「数量 505」这类数据行当表头
        if NUM.fullmatch(re.sub(r"\s+", "", cells[mapping["qty"]])):
            continue
        if best is None or len(mapping) > len(best[1]):
            best = (i, mapping)
    if best is None:
        return {"found": False, "rows": [], "total": 0, "missing_qty": [],
                "columns": {}, "has_budget": False,
                "grade_marks": marks,
                "note": "没有识别到含「名称 + 数量」的货物清单表头"}
    idx, mapping = best
    need = max(mapping.values()) + 1
    rows: list[dict] = []
    missing: list[str] = []
    for line in lines[idx + 1:]:
        cells = _cells(line)
        if cells is None or len(cells) < need:
            if rows:
                break  # 表格结束，不把后面的其它表格接进来
            continue
        name = cells[mapping["name"]].strip()
        if not name or name == re.sub(r"\s+", "", "名称"):
            continue
        if any(w in name for w in ("合计", "总计", "小计")):
            continue
        row = {k: cells[col].strip() for k, col in mapping.items()
               if col < len(cells)}
        qty = str(row.get("qty") or "").replace(",", "").strip()
        if not NUM.fullmatch(qty):
            missing.append(name[:60])
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return {
        "found": True, "rows": rows, "total": len(rows),
        "missing_qty": missing[:20], "missing_qty_count": len(missing),
        "columns": {k: v for k, v in mapping.items()},
        "has_budget": "budget" in mapping,
        "grade_marks": {c: body.count(c) for c in GRADE_MARKS},
        "note": (f"货物清单 {len(rows)} 行"
                 + ("；未见预算列，金额需按报价表另行核对" if "budget" not in mapping else "")),
    }


def extract_cap(text: str) -> dict:
    """最高限价：明确「无」就返回 none；抽到金额才返回 amount；含糊一律 unclear。"""
    body = str(text or "")
    spots = [m.start() for m in re.finditer("最高限价", body)]
    if not spots:
        return {"status": "absent", "value": "", "note": "招标文件未提到最高限价"}
    contexts = [body[max(0, s - 60):s + 120] for s in spots]
    none_hits = sum(1 for c in contexts if CAP_NONE.search(c))
    amounts = [f"{m.group(1)}{m.group(2)}"
               for c in contexts for m in CAP_AMOUNT.finditer(c)]
    amounts = list(dict.fromkeys(amounts))
    if none_hits and not amounts:
        return {"status": "none", "value": "",
                "note": "招标文件明确本项目无最高限价"}
    if amounts and not none_hits:
        return {"status": "amount", "value": amounts[0],
                "note": f"识别到最高限价 {amounts[0]}，请对照前附表原文确认"}
    if amounts and none_hits:
        return {"status": "unclear", "value": amounts[0],
                "note": "同一份文件里既写了「无最高限价」又出现金额，需人工确认以哪条为准"}
    return {"status": "unclear", "value": "",
            "note": "提到最高限价但没读到明确数值或「无」，请人工核对前附表"}


def scan(pid: str) -> dict:
    """只读汇总：优先取清单行最多的那份资料，不写库、不调模型。"""
    texts: list[tuple[str, str]] = []
    try:
        p = store.get_project(pid) or {}
        if str(p.get("tender_text") or "").strip():
            texts.append(("招标文件正文", str(p["tender_text"])))
    except Exception:
        pass
    try:
        for d in store.list_docs(project_id=pid):
            if str(d.get("content") or "").strip():
                texts.append((str(d.get("title") or "项目资料"), str(d["content"])))
    except Exception:
        pass
    best: dict = {"found": False, "rows": [], "total": 0}
    source = ""
    for title, text in texts:
        r = extract(text)
        if r["found"] and r["total"] > best.get("total", 0):
            best, source = r, title
    cap = extract_cap("\n".join(t for _, t in texts))
    return {"found": best["found"], "source": source,
            "total": best.get("total", 0), "rows": best.get("rows", [])[:MAX_ROWS],
            "columns": best.get("columns", {}),
            "missing_qty": best.get("missing_qty", []),
            "missing_qty_count": best.get("missing_qty_count", 0),
            "has_budget": bool(best.get("has_budget")),
            "grade_marks": best.get("grade_marks", {c: 0 for c in GRADE_MARKS}),
            "note": best.get("note", ""), "cap": cap,
            "summary": (f"{source}抽出 {best.get('total', 0)} 行货物清单；"
                        f"限价：{cap['note']}" if best["found"]
                        else f"未识别到货物清单；限价：{cap['note']}")}


def export_issues(pid: str) -> list[dict]:
    """接入导出门禁：只管清单本身；限价的算术比对统一交给 quote_gate。"""
    try:
        r = scan(pid)
    except Exception as exc:
        return [{"key": "goods_error", "title": "货物清单没核对完",
                 "level": "高", "blocking": True, "action": "重试体检",
                 "detail": (f"清单检查未能完成（{type(exc).__name__}: "
                            f"{str(exc)[:120]}），本轮不能当作已核对。"),
                 "evidence": "backend/app/goods_list.py::scan"}]
    out: list[dict] = []
    if r["found"] and r["missing_qty_count"]:
        out.append({
            "key": "goods-missing-qty", "level": "中", "blocking": False,
            "action": "核对清单", "source": "货物清单",
            "title": f"货物清单有 {r['missing_qty_count']} 行没读到数量",
            "detail": (f"{r['source']}共 {r['total']} 行，其中 "
                       f"{r['missing_qty_count']} 行的「数量」为空或不是数字，"
                       "例如：" + "、".join(r["missing_qty"][:3])
                       + "。数量是报价与供货的基础，缺项可能构成未实质响应，请对照原件补齐。"),
            "evidence": f"goods_list:{r['source']}"})
    cap = r.get("cap") or {}
    if cap.get("status") == "unclear":
        out.append({
            "key": "goods-cap-unclear", "level": "中", "blocking": False,
            "action": "核对限价", "source": "最高限价",
            "title": "最高限价没读明确，需人工确认",
            "detail": cap.get("note", "") + "。系统不会猜一个金额来判定是否超限。",
            "evidence": "goods_list::extract_cap"})
    # 明确写了限价金额时，由 quote_gate 拿去和报价合计做算术比对，
    # 这里不再重复提醒，避免同一条限价在门禁里出现两句不同结论。
    marks = r.get("grade_marks") or {}
    if sum(v for v in marks.values() if isinstance(v, int)):
        out.append({
            "key": "goods-grade-marks", "level": "中", "blocking": False,
            "action": "逐项响应", "source": "强制参数",
            "title": "招标文件里有★▲类强制参数标记",
            "detail": ("识别到 " + "、".join(f"{k} {v} 处" for k, v in marks.items() if v)
                       + "。这类参数通常要求逐项填写具体数值，"
                         "用「响应 / 满足」代替具体应答可能被视为未实质响应。"),
            "evidence": "goods_list::grade_marks"})
    return out
