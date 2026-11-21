"""项目手填资料：把企业自身的可核查事实（资质、业绩、团队、设备、参数）录成一份资料。

存在 kb_docs 里（scope=project, kind=material），这样目录、单章、整本、检查
各条生成链路通过 rag.materials_block 自动读到，不需要每处单独接线。

content 用固定小标题 + 「键：值」的纯文本，模型读得懂，人也能直接看；
parse 按同一套小标题反解回表单，保证「填 → 存 → 再编辑」不丢字段。
"""
from __future__ import annotations

import re

from . import store

TITLE = "企业资料（手填）"

# (key, 表单标签, 提示, 是否多行, 小标题)
FIELDS: tuple[tuple[str, str, str, bool, str], ...] = (
    ("company", "企业名称", "与营业执照完全一致", False, "企业基本信息"),
    ("credit", "统一社会信用代码", "18 位；没有可留空", False, "企业基本信息"),
    ("base", "注册地与办公地", "注册地址 / 实际办公或厂房地址 / 面积", True, "企业基本信息"),
    ("qual", "资质与证书", "每行一条：名称 + 等级 + 证书编号 + 有效期", True, "资质与证书"),
    ("perf", "类似业绩", "每行一条：项目名称 + 合同金额 + 年份 + 甲方单位", True, "类似业绩"),
    ("team", "拟投入团队", "每行一条：姓名 + 拟任职务 + 执业资格或证书编号", True, "拟投入团队"),
    ("equip", "设备与产能", "每行一条：设备名称 + 数量 + 关键规格", True, "设备与产能"),
    ("tech", "技术参数承诺", "每行一条：指标 + 承诺值，尽量与招标要求的数字对齐", True, "技术参数承诺"),
    ("service", "售后与响应", "服务网点、响应时限、备件供应、质保期限", True, "售后与响应"),
)

_BY_SECTION = {f[4]: f[0] for f in FIELDS}
_BY_LABEL = {f[1]: f[0] for f in FIELDS}
_SECTION_FIRST: dict[str, str] = {}
for _f in FIELDS:
    _SECTION_FIRST.setdefault(_f[4], _f[0])


def dump(values: dict) -> str:
    """字段 → 纯文本。空的字段不写，避免模型把空标签当事实。"""
    blocks: list[str] = []
    order: list[str] = []
    for key, label, _hint, _multi, section in FIELDS:
        val = str(values.get(key) or "").strip()
        if not val:
            continue
        if section not in order:
            order.append(section)
    for section in order:
        lines = [f"【{section}】"]
        # 该小标题在表单里只有一个字段时不重复写标签，直接给内容，读起来像资料而不是表单
        lone = len([f for f in FIELDS if f[4] == section]) == 1
        for key, label, _hint, _multi, sec in FIELDS:
            if sec != section:
                continue
            val = str(values.get(key) or "").strip()
            if not val:
                continue
            lines.append(val if lone else f"{label}：{val}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse(content: str) -> dict:
    """纯文本 → 字段。认不出小标题时按「标签：值」全篇兜底。"""
    out: dict[str, str] = {}
    section = ""
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"【(.+)】", line)
        if m:
            section = m.group(1).strip()
            continue
        label, sep, val = line.partition("：")
        if not sep:
            # 单字段小标题下的裸内容行，归到该小标题的第一个字段
            key = _SECTION_FIRST.get(section)
            if key:
                prev = out.get(key)
                out[key] = (prev + "\n" + line) if prev else line
            continue
        key = _BY_LABEL.get(label.strip())
        if not key:
            continue
        val = val.strip()
        if not val:
            continue
        prev = out.get(key)
        # 多行字段的续行（换行写的一行条）拼回去
        out[key] = (prev + "\n" + val) if prev else val
    return out


def filled_count(values: dict) -> int:
    return sum(1 for f in FIELDS if str(values.get(f[0]) or "").strip())


def get(pid: str) -> dict:
    """返回 {doc_id, values, filled, total, content}；没填过时 doc_id 为空。"""
    rows = store.list_docs(scope="project", project_id=pid)
    doc = next((r for r in rows if str(r.get("title") or "") == TITLE), None)
    values = parse(doc.get("content") or "") if doc else {}
    return {
        "doc_id": doc.get("id") if doc else "",
        "values": values,
        "filled": filled_count(values),
        "total": len(FIELDS),
        "content": doc.get("content") if doc else "",
        "updated_at": doc.get("created_at") if doc else "",
        # 表单定义由后端给，前端不重复维护一份字段表
        "fields": [{"key": k, "label": lb, "hint": h, "multiline": m}
                   for k, lb, h, m, _s in FIELDS],
    }


def save(pid: str, values: dict) -> dict:
    """按字段保存。全空时删掉这份资料，避免留一条空文档污染资料块。"""
    clean = {}
    for key, *_rest in FIELDS:
        val = str(values.get(key) or "").strip()
        if val:
            clean[key] = val[:4000]
    content = dump(clean)
    cur = get(pid)
    if not content:
        if cur["doc_id"]:
            store.delete_doc(cur["doc_id"])
        return {"doc_id": "", "values": {}, "filled": 0, "total": len(FIELDS),
                "content": "", "cleared": True}
    if cur["doc_id"]:
        store.update_doc(cur["doc_id"], content=content, needs_review=0,
                         parse_status="ok", verification="manual")
    else:
        doc = store.add_doc(TITLE, content, scope="project", project_id=pid,
                            kind="material", verification="manual",
                            parse={"parse_status": "ok", "effective_chars": len(content)})
        # 手填资料是投标人自己写的，不需要再做原件核对
        store.update_doc(doc["id"], needs_review=0)
    return get(pid)
