"""投标报价表校验：算术、合计、大小写、漏项。

只做确定性计算，不调模型：
- 合价 = 数量 × 单价
- 明细合计 = 各行合价之和
- 明细合计 与 投标函总价 一致
- 大写金额 与 总价 一致
- 负数、零、漏报价、同名不同价、单价小数位异常

输入支持 xlsx / csv / 直接粘贴的表格文本（制表符或逗号分隔）。
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from pathlib import Path

from .checker import cn_amount
from .money import to_cn_amount

# 表头关键词 → 字段。顺序即优先级。
HEADER_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("no", ("序号", "项次", "编号", "no", "no.", "item")),
    ("name", ("名称", "品名", "项目", "货物", "服务", "内容", "description", "name")),
    ("spec", ("规格", "型号", "品牌", "参数", "spec", "model")),
    ("unit", ("单位", "unit", "计量")),
    ("qty", ("数量", "工程量", "qty", "quantity", "数 量")),
    ("price", ("单价", "综合单价", "price", "unit price")),
    ("amount", ("合价", "金额", "小计", "总价", "amount", "total")),
    ("remark", ("备注", "说明", "remark", "note")),
]

TOTAL_WORDS = ("合计", "总计", "小计", "总合计", "投标总价", "报价合计")


class QuoteError(RuntimeError):
    pass


def _norm_cell(value) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", " ", text)


def parse_number(value) -> float | None:
    """从单元格里抠出数字，支持 ¥ / 元 / 万元 / 千分位。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = _norm_cell(value)
    if not text:
        return None
    text = text.replace("¥", "").replace("￥", "").replace("元", "").replace(",", "").replace(" ", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if "万" in text:
        number *= 10000
    return number


def _decimal_places(value) -> int:
    text = _norm_cell(value)
    match = re.search(r"\.(\d+)", text)
    return len(match.group(1)) if match else 0


def _read_grid_from_xlsx(path: Path) -> list[list[str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook.worksheets[0]
        grid: list[list[str]] = []
        for row in sheet.iter_rows(values_only=True):
            grid.append([_norm_cell(cell) for cell in row])
            if len(grid) > 5000:
                break
        return grid
    finally:
        workbook.close()


def _read_grid_from_text(text: str) -> list[list[str]]:
    text = text.strip("\ufeff").strip()
    if not text:
        raise QuoteError("没有可解析的内容")
    # 分隔符要看表头所在的整段，不能只看第一行：真实标书里报价表前面
    # 常有「投标报价表」「第三章 报价明细」这类标题行，只看第一行会选成制表符，
    # 后面每行都被当成单列，明明有表头也报「未识别到报价表」。
    probe = "\n".join(text.splitlines()[:12])
    counts = {"\t": probe.count("\t"), ",": probe.count(","), ";": probe.count(";")}
    delimiter = max(counts, key=lambda d: counts[d])
    if counts[delimiter] == 0:
        delimiter = ","  # 没有分隔符也交给 _detect_header 给出明确的表头提示
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[_norm_cell(cell) for cell in row] for row in reader]


def _detect_header(grid: list[list[str]], require_price: bool = True) -> tuple[int, dict[str, int]]:
    """在前 12 行里找表头，返回 (行号, 字段→列号)。"""
    best = (-1, {}, 0)
    for idx, row in enumerate(grid[:12]):
        mapping: dict[str, int] = {}
        for col, cell in enumerate(row):
            low = cell.lower().replace(" ", "")
            if not low:
                continue
            for field, words in HEADER_MAP:
                if field in mapping:
                    continue
                if any(word.replace(" ", "") in low for word in words):
                    mapping[field] = col
                    break
        score = len(mapping)
        # 必须能定位到名称，默认还要能定位到单价或合价，否则不算表头
        usable = "name" in mapping and (not require_price or "price" in mapping or "amount" in mapping)
        if usable and score > best[2]:
            best = (idx, mapping, score)
    if best[0] < 0:
        raise QuoteError(
            "没有识别到表头。请确保首行包含「名称 / 数量 / 单价 / 合价」等列名，"
            "或直接把表格（含表头）粘贴进来。"
        )
    return best[0], best[1]


def _load_grid(*, file_path: Path | None = None, text: str = "") -> list[list[str]]:
    if file_path is not None:
        suffix = file_path.suffix.lower()
        if suffix == ".xlsx":
            return _read_grid_from_xlsx(file_path)
        if suffix in (".csv", ".txt"):
            return _read_grid_from_text(file_path.read_text("utf-8", "ignore"))
        raise QuoteError("报价表仅支持 xlsx / csv，或直接粘贴表格文本")
    return _read_grid_from_text(text)


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _is_total_row(name: str, no: str) -> bool:
    text = f"{name}{no}"
    return any(word in text for word in TOTAL_WORDS)


def analyze(
    *,
    file_path: Path | None = None,
    text: str = "",
    declared_total: float | None = None,
    uppercase_total: str = "",
) -> dict:
    if file_path is not None:
        suffix = file_path.suffix.lower()
        if suffix not in (".xlsx", ".csv", ".txt"):
            raise QuoteError("报价表仅支持 xlsx / csv，或直接粘贴表格文本")
    grid = _load_grid(file_path=file_path, text=text)

    if len(grid) < 2:
        raise QuoteError("表格至少要有表头行和一行数据")

    header_row, mapping = _detect_header(grid)

    items: list[dict] = []
    declared_in_table: float | None = None
    total_row_amount: float | None = None

    for raw in grid[header_row + 1 :]:
        if not any(cell.strip() for cell in raw):
            continue
        no = _cell(raw, mapping, "no")
        name = _cell(raw, mapping, "name")
        spec = _cell(raw, mapping, "spec")
        unit = _cell(raw, mapping, "unit")
        qty_raw = _cell(raw, mapping, "qty")
        price_raw = _cell(raw, mapping, "price")
        amount_raw = _cell(raw, mapping, "amount")
        remark = _cell(raw, mapping, "remark")

        if _is_total_row(name, no):
            value = parse_number(amount_raw) or parse_number(price_raw)
            if value is not None:
                declared_in_table = value if declared_in_table is None else declared_in_table
                total_row_amount = value
            continue

        qty = parse_number(qty_raw)
        price = parse_number(price_raw)
        amount = parse_number(amount_raw)
        if qty is None and price is None and amount is None and not name:
            continue

        flags: list[str] = []
        calc = None
        diff = None
        if qty is not None and price is not None:
            calc = round(qty * price, 2)
            if amount is not None:
                diff = round(amount - calc, 2)

        items.append(
            {
                "no": no,
                "name": name,
                "spec": spec,
                "unit": unit,
                "qty": qty,
                "price": price,
                "amount": amount,
                "calc": calc,
                "diff": diff,
                "remark": remark,
                "price_decimals": _decimal_places(price_raw),
                "flags": flags,
            }
        )

    if not items:
        raise QuoteError("表头下面没有解析到任何明细行")

    issues: list[dict] = []

    def add(code: str, level: str, title: str, detail: str, evidence: str = "") -> None:
        issues.append({"code": code, "level": level, "title": title, "detail": detail, "evidence": evidence[:160]})

    # Q01 合价与 数量×单价 不符
    bad = [row for row in items if row["diff"] is not None and abs(row["diff"]) > max(0.02, abs(row["calc"] or 0) * 0.0005)]
    for row in bad[:8]:
        row["flags"].append("Q01")
    if bad:
        samples = "；".join(
            f"{row['name'] or row['no']} 合价 {row['amount']:,.2f}，应为 {row['calc']:,.2f}（差 {row['diff']:+,.2f}）"
            for row in bad[:3]
        )
        add(
            "Q01",
            "高",
            f"{len(bad)} 行合价与「数量×单价」不一致",
            "合价算错是最常见的废标点，评标时按单价核算会直接推翻总价。逐行核对后再报。" + samples,
        )

    # Q02 漏报价
    missing = [
        row
        for row in items
        if (row["qty"] or 0) > 0 and ((row["price"] in (None, 0)) or (row["amount"] in (None, 0)))
    ]
    for row in missing:
        row["flags"].append("Q02")
    if missing:
        names = "、".join((row["name"] or row["no"])[:18] for row in missing[:5])
        add("Q02", "高", f"{len(missing)} 行未报价", f"数量不为零但没有单价或合价，被视为漏项或未响应：{names}。")

    # Q03 负数/零数量
    weird = [row for row in items if (row["qty"] is not None and row["qty"] <= 0) or (row["price"] is not None and row["price"] < 0)]
    for row in weird:
        row["flags"].append("Q03")
    if weird:
        names = "、".join((row["name"] or row["no"])[:18] for row in weird[:5])
        add("Q03", "中", f"{len(weird)} 行数量或单价异常", f"出现零、负数数量或负单价：{names}。")

    # Q04 同名同规格不同单价
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in items:
        if not row["name"] or row["price"] is None:
            continue
        groups.setdefault((row["name"], row["spec"]), []).append(row)
    conflicts = [(key, rows) for key, rows in groups.items() if len({round(r["price"], 4) for r in rows}) > 1]
    if conflicts:
        for _, rows in conflicts:
            for row in rows:
                row["flags"].append("Q04")
        detail = "；".join(
            f"{key[0][:16]}：" + "/".join(f"{r['price']:,.2f}" for r in rows) for key, rows in conflicts[:3]
        )
        add("Q04", "中", f"{len(conflicts)} 组同名条目单价不一致", "同一名称规格报出多个单价，容易被要求书面澄清。" + detail)

    # Q05 单价小数位
    decimals = [row for row in items if row["price_decimals"] > 2]
    if decimals:
        for row in decimals:
            row["flags"].append("Q05")
        add(
            "Q05",
            "低",
            f"{len(decimals)} 行单价超过 2 位小数",
            "多数招标文件要求单价保留两位小数，多余位数会导致系统导入或评审取整差异。",
        )

    # 合计
    sum_amount = round(sum(row["amount"] or 0 for row in items), 2)
    sum_calc = round(sum(row["calc"] if row["calc"] is not None else (row["amount"] or 0) for row in items), 2)

    # Q06 合计行与明细之和不一致
    if total_row_amount is not None and abs(total_row_amount - sum_amount) > max(0.02, abs(sum_amount) * 0.0005):
        add(
            "Q06",
            "高",
            "合计行与明细之和不一致",
            f"表内合计写 {total_row_amount:,.2f}，各行合价相加为 {sum_amount:,.2f}，差 {total_row_amount - sum_amount:+,.2f}。",
        )

    # Q07 与投标函总价一致
    if declared_total is not None:
        base = sum_calc if abs(sum_calc - sum_amount) > 0.01 else sum_amount
        if abs(base - declared_total) > max(0.02, abs(declared_total) * 0.0005):
            add(
                "Q07",
                "高",
                "明细合计与投标函总价不一致",
                f"投标函写 {declared_total:,.2f}，明细合计 {base:,.2f}，差 {declared_total - base:+,.2f}。"
                "三处金额（投标函、开标一览表、报价明细）必须完全一致。",
            )

    # Q08 大小写一致
    if uppercase_total.strip():
        upper_value = cn_amount(uppercase_total)
        compare = declared_total if declared_total is not None else (sum_calc or sum_amount)
        if upper_value is None:
            add("Q08", "中", "大写金额无法识别", f"“{uppercase_total.strip()[:40]}”未解析出数值，请检查是否写全（如“壹佰贰拾万元整”）。")
        elif compare is not None and abs(upper_value - compare) > max(0.02, abs(compare) * 0.0005):
            add(
                "Q08",
                "高",
                "大写金额与小写金额不一致",
                f"大写“{uppercase_total.strip()[:30]}”折算 {upper_value:,.2f} 元，小写 {compare:,.2f} 元，差 {upper_value - compare:+,.2f} 元。",
            )

    level_rank = {"高": 0, "中": 1, "低": 2}
    issues.sort(key=lambda item: level_rank.get(item["level"], 3))

    return {
        "columns": mapping,
        "header_row": header_row + 1,
        "rows": items,
        "summary": {
            "row_count": len(items),
            "sum_amount": sum_amount,
            "sum_calc": sum_calc,
            "table_total": total_row_amount,
            "declared_total": declared_total,
            "delta": round((declared_total if declared_total is not None else sum_amount) - sum_calc, 2),
        },
        "issues": issues,
        "notice": "校验只针对算术与一致性，不判断报价是否合理。修正后请同步更新投标函、开标一览表与电子报价文件。",
    }


# ---------------- 报价表填价生成 ----------------

HEADERS = ["序号", "名称", "规格", "单位", "数量", "单价", "合价", "备注"]


def build_items(*, file_path: Path | None = None, text: str = "") -> dict:
    """把招标方的报价清单（可只填了名称与数量、单价留空）解析成可填价的行。"""
    grid = _load_grid(file_path=file_path, text=text)
    if len(grid) < 2:
        raise QuoteError("表格至少要有表头行和一行数据")
    header_row, mapping = _detect_header(grid, require_price=False)
    if "name" not in mapping:
        raise QuoteError("没有识别到「名称」列，请确保表头包含「名称 / 数量 / 单价」等列名")

    rows: list[dict] = []
    for raw in grid[header_row + 1 :]:
        if not any(cell.strip() for cell in raw):
            continue
        no = _cell(raw, mapping, "no")
        name = _cell(raw, mapping, "name")
        if _is_total_row(name, no):
            continue
        qty = parse_number(_cell(raw, mapping, "qty"))
        price = parse_number(_cell(raw, mapping, "price"))
        amount = parse_number(_cell(raw, mapping, "amount"))
        spec = _cell(raw, mapping, "spec")
        unit = _cell(raw, mapping, "unit")
        remark = _cell(raw, mapping, "remark")
        if qty is None and price is None and amount is None and not name:
            continue
        if amount is None and qty is not None and price is not None:
            amount = round(qty * price, 2)
        rows.append(
            {"no": no, "name": name, "spec": spec, "unit": unit,
             "qty": qty, "price": price, "amount": amount, "remark": remark}
        )
    if not rows:
        raise QuoteError("表头下面没有解析到任何明细行")
    for i, row in enumerate(rows, 1):
        if not row["no"]:
            row["no"] = str(i)
    return {"rows": rows, "columns": mapping, "notice": "已读取清单，请填写单价（合价将自动计算）后导出。"}


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_number(value)


def compute(rows: list[dict], *, project_name: str = "", tax_rate: float | None = None) -> dict:
    """重算每一行合价、合计与中文大写金额（前端编辑后提交的行）。

    表内「数量 × 单价」按**不含税**口径；填了税率时再算：
    税额 = 不含税合计 × 税率，价税合计 = 不含税合计 + 税额，大写按**价税合计**。
    """
    out: list[dict] = []
    total = 0.0
    for i, row in enumerate(rows or [], 1):
        qty = _num(row.get("qty"))
        price = _num(row.get("price"))
        amount = _num(row.get("amount"))
        if qty is not None and price is not None:
            amount = round(qty * price, 2)
        if amount:
            total += amount
        out.append(
            {
                "no": str(row.get("no") or i),
                "name": str(row.get("name") or ""),
                "spec": str(row.get("spec") or ""),
                "unit": str(row.get("unit") or ""),
                "qty": qty,
                "price": price,
                "amount": amount,
                "remark": str(row.get("remark") or ""),
            }
        )
    total = round(total, 2)

    rate: float | None = None
    if tax_rate is not None:
        try:
            parsed = float(tax_rate)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0:
            rate = round(parsed, 4)

    if rate:
        tax_amount = round(total * rate / 100.0, 2)
        total_with_tax = round(total + tax_amount, 2)
    else:
        tax_amount = 0.0
        total_with_tax = total

    uppercase = ""
    if total_with_tax:
        try:
            uppercase = to_cn_amount(total_with_tax)
        except ValueError:
            uppercase = ""
    return {
        "rows": out,
        "summary": {
            "count": len(out),
            "total": total,
            "tax_rate": rate,
            "tax_amount": tax_amount,
            "total_with_tax": total_with_tax,
            "uppercase": uppercase,
        },
        "project": project_name,
        "notice": "" if out else "还没有明细行。",
    }


def _fmt_num(value) -> str:
    if value is None:
        return ""
    return f"{value:,.2f}" if isinstance(value, float) else str(value)


def _rows(result: dict) -> list[list[object]]:
    return [
        [r["no"], r["name"], r["spec"], r["unit"], r["qty"], r["price"], r["amount"], r.get("remark") or ""]
        for r in result.get("rows") or []
    ]


def _title(project_name: str) -> str:
    return f"{project_name}投标报价表" if project_name else "投标报价表"


def _footer(result: dict) -> str:
    summary = result.get("summary") or {}
    if summary.get("tax_rate"):
        return (
            f"不含税合计：{_fmt_num(summary.get('total'))} 元 ｜ "
            f"税率：{summary['tax_rate']:g}% ｜ 税额：{_fmt_num(summary.get('tax_amount'))} 元 ｜ "
            f"价税合计：{_fmt_num(summary.get('total_with_tax'))} 元（大写：{summary.get('uppercase') or '—'}）"
        )
    return f"合　计：{_fmt_num(summary.get('total'))} 元（大写：{summary.get('uppercase') or '—'}）"


def to_docx(out_path: Path, project_name: str, result: dict) -> Path:
    from . import table_export

    return table_export.to_docx(
        out_path,
        project_name or "投标报价表",
        title=_title(project_name),
        headers=HEADERS,
        rows=_rows(result),
        doc_type="投标报价表",
        footer=[_footer(result)],
    )


def to_pdf(out_path: Path, project_name: str, result: dict) -> Path:
    from . import table_export

    return table_export.to_pdf(
        out_path,
        project_name or "投标报价表",
        title=_title(project_name),
        headers=HEADERS,
        rows=_rows(result),
        doc_type="投标报价表",
        footer=[_footer(result)],
    )


def to_xlsx(out_path: Path, project_name: str, result: dict) -> Path:
    from . import table_export

    summary = result.get("summary") or {}
    if summary.get("tax_rate"):
        total_row = ["价税合计", "", "", "", "", "", summary.get("total_with_tax"), summary.get("uppercase") or ""]
    else:
        total_row = ["合计", "", "", "", "", "", summary.get("total"), summary.get("uppercase") or ""]
    return table_export.write_xlsx(
        out_path,
        sheet_title="投标报价表",
        headers=HEADERS,
        rows=_rows(result),
        widths=(6, 30, 20, 8, 10, 12, 14, 20),
        total_row=total_row,
    )

