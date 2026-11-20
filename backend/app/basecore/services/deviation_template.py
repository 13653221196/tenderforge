"""按招标方给的表格式偏离表模板回填。

招标文件常给一张空表：列是「序号 / 招标要求 / 投标响应 / 偏离情况 / 说明」，
格式（字体、边框、列宽、页眉）都是定死的。直接用自己生成的表会格式不符，
评分时容易被扣"未按格式编制"。

做法：模板只负责"长什么样"，内容全部来自已生成的偏离表结果。
- 认表头：按关键词匹配列，至少要有「招标要求」和「投标响应」两列，认不出就明确报错，不猜；
- 清数据：只删表头以下的行，不动表头、不动表格外的任何内容；
- 写数据：复制模板数据行的格式（字体/对齐/行高）逐行填，没有数据行才新建。

模板与列映射存在 data/deviation_templates/<project>-<kind>.docx|.json，
一个项目一套，按表别（技术/商务）分开。
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.shared import Pt

TEMPLATE_DIR_NAME = "deviation_templates"

# 列名关键词：长的优先匹配，避免「招标要求」被「要求」这种短词抢走
COLUMN_KEYS: dict[str, tuple[str, ...]] = {
    "seq": ("序号", "编号", "no.", "no", "序"),
    "requirement": ("招标文件要求", "招标要求", "技术要求", "采购要求", "招标内容", "需求", "条款", "规格", "指标", "要求"),
    "response": ("投标响应", "投标人响应", "响应内容", "投标文件响应", "应答", "响应"),
    "status": ("偏离情况", "偏离说明", "偏差", "偏离", "结论"),
    "note": ("说明", "备注", "注释", "原因"),
}
REQUIRED = ("requirement", "response")
LABELS = {
    "seq": "序号",
    "requirement": "招标要求",
    "response": "投标响应",
    "status": "偏离情况",
    "note": "说明",
}


class TemplateError(RuntimeError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", (text or "").replace("\u3000", " ")).lower()


def detect_columns(names: list[str]) -> dict[str, int]:
    """把表头文字映射到字段下标。一个列只归一个字段。"""
    norm = [_norm(n) for n in names]
    taken: set[int] = set()
    out: dict[str, int] = {}
    for key in ("seq", "requirement", "response", "status", "note"):
        best, best_len = None, 0
        for idx, name in enumerate(norm):
            if not name or idx in taken:
                continue
            for kw in COLUMN_KEYS[key]:
                if kw in name and len(kw) > best_len:
                    best, best_len = idx, len(kw)
        if best is not None:
            out[key] = best
            taken.add(best)
    return out


def scan(path: str | Path) -> dict:
    """扫模板找偏离表：返回 {table, header_row, columns, header_names, rows}。"""
    doc = Document(str(path))
    best: dict | None = None
    for ti, table in enumerate(doc.tables):
        rows = table.rows
        # 表头可能在第 1 行，也可能上面还有一行合并的大标题
        for ri in range(min(3, len(rows))):
            names = [c.text.strip() for c in rows[ri].cells]
            cols = detect_columns(names)
            if not all(k in cols for k in REQUIRED):
                continue
            score = len(cols) * 10 + (5 if "status" in cols else 0) + (3 if "note" in cols else 0) + (2 if ri == 0 else 0)
            if best is None or score > best["score"]:
                best = {
                    "table": ti,
                    "header_row": ri,
                    "columns": cols,
                    "header_names": names,
                    "score": score,
                    "tables": len(doc.tables),
                    "rows": max(0, len(rows) - ri - 1),
                    "cols": len(names),
                }
    if best is None:
        raise TemplateError(
            "没认出偏离表表头：模板里需要有一行同时包含「招标要求」和「投标响应」两列，"
            "常见写法是 序号 / 招标要求 / 投标响应 / 偏离情况 / 说明。"
        )
    best.pop("score", None)
    return best


def summarize(mapping: dict) -> str:
    """人能看懂的一句识别结果。"""
    cols = mapping.get("columns") or {}
    parts = [f"{LABELS[k]}→第{v + 1}列" for k, v in sorted(cols.items(), key=lambda kv: kv[1])]
    return f"表格 {mapping.get('table', 0) + 1}，表头在第 {mapping.get('header_row', 0) + 1} 行：" + "、".join(parts)


def _clear_cell(cell) -> None:
    """清空单元格文字但保留第一个 run 的格式。"""
    paragraphs = cell.paragraphs
    first = paragraphs[0]
    for run in first.runs[1:]:
        run.text = ""
    if first.runs:
        first.runs[0].text = ""
    for extra in paragraphs[1:]:
        extra._element.getparent().remove(extra._element)


def _set_cell(cell, text: str) -> None:
    _clear_cell(cell)
    para = cell.paragraphs[0]
    if para.runs:
        para.runs[0].text = text
    else:
        run = para.add_run(text)
        # 模板数据行为空时新建的 run 会掉字体，给个稳妥的默认值
        run.font.size = Pt(10.5)
        run.font.name = "宋体"


def fill_rows(table, header_row: int, columns: dict[str, int], rows: list[dict]) -> int:
    """清空表头以下内容，按 rows 逐行回填。返回写入行数。"""
    trs = table._tbl.tr_lst
    data = trs[header_row + 1 :]
    proto = deepcopy(data[0]) if data else None
    for tr in data:
        table._tbl.remove(tr)

    width = len(table.columns)
    for item in rows:
        if proto is not None:
            tr = deepcopy(proto)
            table._tbl.append(tr)
            cells = table.rows[-1].cells
        else:
            cells = table.add_row().cells
        for key, idx in columns.items():
            if idx >= width or idx >= len(cells):
                continue
            value = item.get(key)
            _set_cell(cells[idx], "" if value is None else str(value))
        # 模板有 5 列而我们只映射到 4 列时，剩余格留空，避免残留示例文字
        for idx in range(len(cells)):
            if idx in columns.values():
                continue
            _clear_cell(cells[idx])
    return len(rows)


# ---------- 模板存档 ----------
def dir_for(data_dir: Path) -> Path:
    path = Path(data_dir) / TEMPLATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def paths(data_dir: Path, project_id: str, kind: str) -> tuple[Path, Path]:
    base = dir_for(data_dir) / f"{project_id}-{kind}"
    return base.with_suffix(".docx"), base.with_suffix(".json")


def save(data_dir: Path, project_id: str, kind: str, raw: bytes, mapping: dict) -> dict:
    docx_path, json_path = paths(data_dir, project_id, kind)
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    docx_path.write_bytes(raw)
    json_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "kind": kind, **mapping, "summary": summarize(mapping)}


def load(data_dir: Path, project_id: str, kind: str) -> dict | None:
    docx_path, json_path = paths(data_dir, project_id, kind)
    if not docx_path.exists() or not json_path.exists():
        return None
    try:
        mapping = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mapping["summary"] = summarize(mapping)
    return mapping


def remove(data_dir: Path, project_id: str, kind: str) -> bool:
    docx_path, json_path = paths(data_dir, project_id, kind)
    hit = False
    for path in (docx_path, json_path):
        if path.exists():
            path.unlink()
            hit = True
    return hit


def fill(data_dir: Path, project_id: str, kind: str, out_path: Path, rows: list[dict]) -> dict:
    """用存档模板回填并另存到 out_path。模板缺失抛 TemplateError。"""
    found = load(data_dir, project_id, kind)
    if not found:
        raise TemplateError("这个项目还没有上传偏离表模板")
    docx_path, _ = paths(data_dir, project_id, kind)
    doc = Document(str(docx_path))
    tables = doc.tables
    ti = int(found.get("table") or 0)
    if ti >= len(tables):
        raise TemplateError("模板结构变了：原来的表格不见了，请重新上传")
    table = tables[ti]
    header_row = int(found.get("header_row") or 0)
    columns = {k: int(v) for k, v in (found.get("columns") or {}).items()}
    written = fill_rows(table, header_row, columns, rows)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return {"ok": True, "rows": written, "summary": found.get("summary", "")}
