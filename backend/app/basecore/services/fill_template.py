"""投标文件格式模板套用：扫描待填位置 → 用事实表回填 → 保留原格式输出。

设计要点：
1. 只改占位符那一小段文字，不动段落样式、表格结构、页眉页脚，所以招标方给的
   格式模板（字体、字号、表格线）原样保留，这是"套用"而不是"重排"。
2. 回填依据是项目事实表 + 企业信息，全部可核对；匹配不到就留空并在报告里列出，
   绝不猜一个公司名或银行账号填进去。
3. 占位符按形态分 7 类，回填按语义分字段，两者解耦：同一种下划线既能填投标人
   名称也能填日期，靠上下文标签判断。
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from decimal import InvalidOperation
from pathlib import Path

from docx import Document

from . import money

# 7 类待填位置（按占位符形态）
KINDS: list[tuple[str, str, str]] = [
    ("blank", "下划线空白", "____ 或 ＿＿ 连续下划线，用于填名称、金额、日期"),
    ("pad", "括号空格", "（　　）括号内留全角空格，用于填单位、姓名"),
    ("mask", "占位符号", "××××、XXX、*** 这类显式占位"),
    ("bracket", "方括号标记", "【 】〔 〕［ ］[ ] 包住待填提示"),
    ("date", "日期空位", "年 月 日 已印好，只等填数字"),
    ("check", "勾选框", "□ ☐ 待勾选，用于选择项"),
    ("empty", "空白段落", "整段只有空格或下划线，用于手写填空"),
]

KIND_PATTERNS: list[tuple[str, str]] = [
    ("blank", r"[_＿﹍]{2,}"),
    ("pad", r"[（(][\s　\u3000]{1,10}[）)]"),
    ("mask", r"[×xXｘＸ＊*]{2,}"),
    ("bracket", r"【[^】]{0,14}】|〔[^〕]{0,14}〕|［[^］]{0,14}］|\[[^\]]{0,14}\]"),
    ("date", r"\s*[_＿﹍\d]{0,6}\s*年[_＿﹍\s]{1,8}月[_＿﹍\s]{1,8}日"),
    ("check", r"[□☐☑■√]"),
]

# 语义字段 → 别名（用于在上下文里找"这一空要填什么"）
FIELDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("bidder", "投标人名称", ("投标人名称", "投标人全称", "投标人（全称）", "投标人", "供应商名称",
                            "供应商", "投标单位", "响应人", "单位名称", "公司名称", "企业名称")),
    ("legal_person", "法定代表人", ("法定代表人", "法定代表", "法人代表", "法人")),
    ("agent_person", "授权代表", ("授权代表", "委托代理人", "被授权人", "受托人", "代理人", "经办人")),
    ("pm", "项目经理", ("项目经理", "项目负责人", "项目总监")),
    ("address", "地址", ("注册地址", "通讯地址", "办公地址", "单位地址", "详细地址", "地址")),
    ("phone", "联系电话", ("联系电话", "电话号码", "电话", "手机", "手机号", "联系方式")),
    ("fax", "传真", ("传真号码", "传真")),
    ("email", "电子邮箱", ("电子邮箱", "邮箱", "电子邮件", "E-mail", "Email", "email")),
    ("postcode", "邮政编码", ("邮政编码", "邮编")),
    ("bank", "开户银行", ("开户银行", "开户行", "开户金融机构")),
    ("account", "银行账号", ("银行账号", "帐号", "账号", "基本账户")),
    ("taxno", "纳税人识别号", ("纳税人识别号", "统一社会信用代码", "税号", "税务登记号")),
    ("project_name", "项目名称", ("项目名称", "工程名称", "招标项目名称", "采购项目名称", "标段名称")),
    ("tender_no", "项目编号", ("项目编号", "招标编号", "采购编号", "标段编号", "项目代码")),
    ("tenderer", "招标人", ("招标人", "采购人", "招标单位", "采购单位", "建设单位")),
    ("agent", "招标代理机构", ("招标代理机构", "采购代理机构", "代理机构")),
    ("price", "投标总价", ("投标总价", "投标报价", "总报价", "报价总额", "合计金额", "报价", "总价")),
    ("price_text", "投标总价（大写）", ("投标总价大写", "投标报价大写", "总价大写", "报价大写",
                                    "大写金额", "金额大写", "大写")),
    ("deposit", "投标保证金", ("投标保证金", "保证金金额", "保证金")),
    ("budget", "最高限价", ("最高限价", "招标控制价", "预算金额", "项目预算")),
    ("duration", "工期", ("计划工期", "工期", "交货期", "服务期", "履约期限")),
    ("warranty", "质保期", ("质保期", "保修期", "质量保证期")),
    ("validity", "投标有效期", ("投标有效期",)),
    ("qualification", "资质等级", ("资质等级", "资质类别", "企业资质", "资质")),
    ("date", "日期", ("日期", "时间", "签署日期", "填表日期", "年月日")),
]

FIELD_TITLE = {k: t for k, t, _ in FIELDS}

# 全量字段别名表（值恒为空），用于给"填不上"的空位判断它其实想要哪个字段，
# 好在报告里告诉用户该去事实表补哪几项。
GENERIC: dict[str, dict] = {
    key: {"key": key, "label": title, "value": "", "aliases": list(als), "source": "field"}
    for key, title, als in FIELDS
}

# 不允许自动填的字段：这些一旦填错就是废标，必须人工确认
RISKY = {"price", "price_text", "deposit", "budget", "validity", "duration"}

_CN_DIGITS = "〇一二三四五六七八九"


def cn_date(d: date) -> str:
    return f"{''.join(_CN_DIGITS[int(c)] for c in str(d.year))}年{_num_cn(d.month)}月{_num_cn(d.day)}日"


def _num_cn(n: int) -> str:
    if n <= 10:
        return "十" if n == 10 else _CN_DIGITS[n]
    if n < 20:
        return "十" + _CN_DIGITS[n % 10]
    return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")


def _norm(text: str) -> str:
    """归一化：去空白与常见标点、全角转半角、小写，便于别名比对。"""
    out = []
    for ch in text or "":
        code = ord(ch)
        if code == 0x3000:
            continue
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if ch.isspace():
            continue
        if ch in ":：,，.。;；、|-_/\\()（）[]【】":
            continue
        out.append(ch.lower())
    return "".join(out)


# ---------------- 扫描 ----------------
def _runs_text(paragraph) -> tuple[str, bool]:
    """返回 (run 拼接文本, 是否与 paragraph.text 一致)。偏移按 run 计算，必须用前者。"""
    parts = [r.text for r in paragraph.runs]
    joined = "".join(parts)
    return joined, joined == (paragraph.text or "")


def _clean_label(raw: str, limit: int = 34) -> str:
    s = re.sub(r"[_＿﹍×xX*＊\s　]+", " ", raw or "").strip(" ：:，,。.、·|/-—")
    s = re.sub(r"\s+", " ", s)
    return s[-limit:]


def _iter_targets(doc):
    """按文档结构遍历段落，产出 (paragraph, 位置描述, 前置文本种子)。"""
    for idx, para in enumerate(doc.paragraphs):
        yield para, f"正文第 {idx + 1} 段", ""

    for ti, table in enumerate(doc.tables):
        for ri, row in enumerate(table.rows):
            prev = ""
            seen: set[int] = set()
            for ci, cell in enumerate(row.cells):
                if id(cell._tc) in seen:
                    continue
                seen.add(id(cell._tc))
                for pi, para in enumerate(cell.paragraphs):
                    yield para, f"表格 {ti + 1} 第 {ri + 1} 行第 {ci + 1} 列", prev
                    if pi == 0:
                        prev = prev or cell.text

    for si, section in enumerate(doc.sections):
        for name, part in (("页眉", section.header), ("页脚", section.footer)):
            try:
                paras = part.paragraphs
            except Exception:
                continue
            for pi, para in enumerate(paras):
                yield para, f"{name}（第 {si + 1} 节）第 {pi + 1} 段", ""


def scan(path: str | Path, *, limit: int = 400) -> dict:
    """扫描模板里的全部待填位置。"""
    doc = Document(str(path))
    spots: list[dict] = []
    skipped: list[str] = []

    prev_text = ""
    for para, where, seed in _iter_targets(doc):
        text, aligned = _runs_text(para)
        if not text.strip():
            prev_text = prev_text or ""
            continue
        if not aligned:
            if re.search("|".join(p for _, p in KIND_PATTERNS), text):
                skipped.append(where)
            prev_text = text
            continue

        found = False
        for kind, pattern in KIND_PATTERNS:
            for m in re.finditer(pattern, text):
                start, end = m.span()
                token = m.group(0)
                if kind == "blank" and len(token) < 3:
                    continue
                # 已印好的真实日期不动它，只填带占位符的日期空位
                if kind == "date" and not re.search(r"[_＿﹍×\s　]", token):
                    continue
                head = text[:start]
                after = text[end:end + 16]
                hint_after = None
                m2 = re.match(r"^\s*[（(]([^）)]{1,14})[）)]", after)
                if m2:
                    hint_after = m2.group(1).strip()
                label = _clean_label(head) or _clean_label(seed) or _clean_label(prev_text)
                spots.append(
                    {
                        "id": f"s{len(spots) + 1}",
                        "kind": kind,
                        "kind_title": dict((k, t) for k, t, _ in KINDS)[kind],
                        "token": token,
                        "label": label,
                        "hint_after": hint_after,
                        "before_raw": text[max(0, start - 14):start],
                        "where": where,
                        "start": start,
                        "end": end,
                        "_para": para,
                    }
                )
                found = True
        # 整段只有空白/下划线，视为手写填空行
        if not found and re.fullmatch(r"[\s　_＿﹍]{4,}", text):
            spots.append(
                {
                    "id": f"s{len(spots) + 1}",
                    "kind": "empty",
                    "kind_title": "空白段落",
                    "token": text.strip()[:20],
                    "label": _clean_label(prev_text),
                    "where": where,
                    "start": 0,
                    "end": len(text),
                    "_para": para,
                }
            )
        prev_text = text
        if len(spots) >= limit:
            break

    by_kind: dict[str, int] = {}
    for s in spots:
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1

    return {
        "spots": spots,
        "stats": {"total": len(spots), "by_kind": by_kind, "skipped_paras": skipped[:20]},
    }


# ---------------- 规划（决定每个位置填什么） ----------------
def build_dictionary(facts: list[dict], meta: dict, enterprise: str = "", today: date | None = None) -> dict[str, dict]:
    d = today or date.today()
    values: dict[str, dict] = {}

    def put(key: str, label: str, value: str, source: str) -> None:
        if not (value or "").strip():
            return
        alias = [a for k, _, als in FIELDS if k == key for a in als]
        if label and label not in alias:
            alias = [label, *alias]
        values[key] = {"key": key, "label": label or FIELD_TITLE.get(key, key), "value": value.strip(),
                       "aliases": alias, "source": source}

    for fact in facts:
        key = fact.get("key") or ""
        if key in FIELD_TITLE:
            put(key, fact.get("label") or FIELD_TITLE[key], fact.get("value") or "", "facts")
        else:
            # 自定义事实：标签直接进别名表，供上下文匹配
            label = (fact.get("label") or "").strip()
            value = (fact.get("value") or "").strip()
            if label and value:
                values[fact.get("key") or label] = {
                    "key": fact.get("key") or label,
                    "label": label,
                    "value": value,
                    "aliases": [label],
                    "source": "facts",
                }

    if "bidder" not in values and enterprise:
        put("bidder", "投标人名称", enterprise, "enterprise")

    for key in ("project_name", "tender_no", "tenderer", "agent"):
        if key not in values and isinstance(meta.get(key), str):
            put(key, FIELD_TITLE[key], meta[key], "meta")

    values["date"] = {"key": "date", "label": "日期", "value": f"{d.year}年{d.month}月{d.day}日",
                      "aliases": ["日期", "时间", "年月日", "签署日期", "填表日期"], "source": "auto"}
    values["date_cn"] = {"key": "date_cn", "label": "日期（汉字）", "value": cn_date(d),
                         "aliases": ["日期", "年月日"], "source": "auto"}

    # 投标总价有值而大写缺失时，由系统换算，不靠模型写
    if "price" in values and "price_text" not in values:
        try:
            cn = money.to_cn_amount(values["price"]["value"])
            aliases = next(als for k, _, als in FIELDS if k == "price_text")
            values["price_text"] = {"key": "price_text", "label": FIELD_TITLE["price_text"],
                                    "value": cn, "aliases": list(aliases), "source": "auto"}
        except (ValueError, InvalidOperation):
            pass
    return values


def _match(context: str, dictionary: dict[str, dict]) -> tuple[str, int] | None:
    """在上下文里找最靠后、最长的字段别名：别名长度优先，其次越靠近空位越好。"""
    ctx = _norm(context)[-44:]
    if not ctx:
        return None
    best: tuple[str, int] | None = None
    best_score = 0
    for key, item in dictionary.items():
        for alias in item["aliases"]:
            a = _norm(alias)
            if len(a) < 2:
                continue
            pos = ctx.rfind(a)
            if pos < 0:
                continue
            score = len(a) * 100 - (len(ctx) - pos - len(a))
            if score > best_score:
                best_score = score
                best = (key, score)
    return best


def _format_value(key: str, value: str, kind: str, spot_label: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if key == "date_cn":
        return cn_date(date.today())
    return v


STRONG_HINTS: list[tuple[str, str]] = [
    # 空位紧跟货币符号，一定是小写金额（大写金额不会带￥）
    (r"[￥¥]\s*$", "price"),
    # 空位前是"人民币（大写）"
    (r"大写\s*[）)]?\s*[:：]?\s*$", "price_text"),
]


def _decide(spot: dict, dictionary: dict[str, dict]) -> str | None:
    """决定这个空位填哪个字段。优先级：后置标注 > 强提示 > 上下文别名匹配。"""
    hint = (spot.get("hint_after") or "").strip()
    if hint:
        hit = _match(hint, dictionary)
        if hit:
            return hit[0]
    before = spot.get("before_raw") or spot.get("label") or ""
    for pattern, key in STRONG_HINTS:
        if re.search(pattern, before) and key in dictionary:
            return key
    hit = _match(spot.get("label") or "", dictionary)
    return hit[0] if hit else None


def plan(spots: list[dict], dictionary: dict[str, dict]) -> dict:
    """规则匹配，产出 {spot_id: fill}；填不上的按所需字段聚合成缺失清单。"""
    filled: list[dict] = []
    pending: list[dict] = []
    wanted_count: dict[str, int] = {}

    for spot in spots:
        key = _decide(spot, dictionary)
        item = dictionary.get(key) if key else None
        value = _format_value(key or "", item["value"] if item else "", spot["kind"], spot.get("label") or "")
        if not key or not item or not value:
            want = _decide(spot, GENERIC)
            if want:
                wanted_count[want] = wanted_count.get(want, 0) + 1
            pending.append(
                {
                    **{k: v for k, v in spot.items() if k != "_para"},
                    "wanted": want or "",
                    "wanted_title": FIELD_TITLE.get(want, "") if want else "",
                }
            )
            continue
        filled.append(
            {
                **{k: v for k, v in spot.items() if k != "_para"},
                "field": key,
                "field_title": FIELD_TITLE.get(key) or item["label"],
                "value": value,
                "source": item["source"],
                "risky": key in RISKY,
            }
        )

    missing = sorted(
        ({"field": k, "title": FIELD_TITLE.get(k, k), "count": v} for k, v in wanted_count.items()),
        key=lambda x: -x["count"],
    )
    return {
        "filled": filled,
        "pending": pending,
        "missing": missing,
        "stats": {
            "total": len(spots),
            "filled": len(filled),
            "pending": len(pending),
            "rate": round(len(filled) / len(spots) * 100, 1) if spots else 0,
            "risky": len([f for f in filled if f["risky"]]),
        },
    }


# ---------------- 回填 ----------------
def _replace_span(paragraph, start: int, end: int, text: str) -> bool:
    """把段落 run 文本里的 [start, end) 换成 text，保留首个 run 的字体格式。"""
    runs = paragraph.runs
    pos = 0
    first = True
    for run in runs:
        t = run.text
        r_start, r_end = pos, pos + len(t)
        pos = r_end
        if r_end <= start or r_start >= end:
            continue
        local_s = max(start, r_start) - r_start
        local_e = min(end, r_end) - r_start
        if first:
            run.text = t[:local_s] + text + t[local_e:]
            first = False
        else:
            run.text = t[:local_s] + t[local_e:]
    return not first


def apply(path: str | Path, filled: list[dict], out_path: str | Path) -> dict:
    """按 filled 列表回填，输出到 out_path，原文件不动。"""
    doc = Document(str(path))

    # 重新遍历，按 (位置, 起点) 对齐——扫描阶段拿到的段落对象已随文档变化，
    # 统一用"位置 + 原始 token + 起点"三元组重新定位，避免错位。
    targets: dict[tuple[str, int], list[dict]] = {}
    for item in filled:
        targets.setdefault((item["where"], item["start"]), []).append(item)

    done = 0
    missed: list[str] = []
    prev_text = ""
    for para, where, _seed in _iter_targets(doc):
        text, aligned = _runs_text(para)
        if not text.strip():
            continue
        if not aligned:
            prev_text = text
            continue
        # 同一段内从后往前替换，前面的偏移不受影响
        edits = [(start, items) for (w, start), items in targets.items() if w == where]
        for start, items in sorted(edits, reverse=True):
            item = items[0]
            end = min(item["end"], len(text))
            if end <= start or text[start:end] != item["token"]:
                # 同段多个位置时 token 已变，退回按起点+长度校验
                if end <= start:
                    continue
            if _replace_span(para, start, end, item["value"]):
                done += 1
            else:
                missed.append(f"{where} → {item['field_title']}")
        prev_text = text

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return {"written": done, "missed": missed[:20], "path": str(out)}


def fill(path: str | Path, facts: list[dict], meta: dict, out_path: str | Path,
         enterprise: str = "", today: date | None = None) -> dict:
    """扫描 + 规划 + 回填，一步到位。"""
    result = scan(path)
    dictionary = build_dictionary(facts, meta, enterprise=enterprise, today=today)
    planned = plan(result["spots"], dictionary)
    applied = apply(path, planned["filled"], out_path)
    return {
        "scan_stats": result["stats"],
        "filled": planned["filled"],
        "pending": planned["pending"],
        "missing": planned["missing"],
        "stats": planned["stats"],
        "applied": applied,
        "dictionary": [
            {"key": v["key"], "label": v["label"], "value": v["value"], "source": v["source"]}
            for v in dictionary.values()
        ],
    }


def copy_as(path: str | Path, out_path: str | Path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(path), str(out))


# ---------------- AI 语义匹配（规则层填不上的空位） ----------------
AI_SYSTEM = (
    "你是投标模板填写助手。只输出 JSON。把占位空位匹配到给定字段，拿不准就给 unknown，绝不发明值。"
)

AI_TEMPLATE = """规则引擎没能匹配下列占位空位，请根据上下文把它们匹配到可用字段。

占位空位（label 是空位前的上下文，hint 是空位后的括号标注）：
{spots}

可用字段（key | 名称 | 当前值）：
{fields}

输出 JSON：
{{"mapping": {{"s12": "field_key", "s15": "unknown"}}}}

要求：
- key 只能用上面给出的字段 key，没有合适字段就给 "unknown"；
- 金额、保证金、工期、有效期这类关键数字由人工确认，不要匹配；
- 同一空位只能映射一个字段。
"""


async def ai_match(cfg, pending: list[dict], dictionary: dict[str, dict]) -> dict[str, str]:
    """返回 {spot_id: field_key}；RISKY 字段与 unknown 不参与。"""
    from .llm import chat, parse_json

    if not getattr(cfg, "ready", False) or not pending:
        return {}
    lines = []
    for spot in pending[:60]:
        hint = f"（后标注：{spot['hint_after']}）" if spot.get("hint_after") else ""
        lines.append(f"- {spot['id']}：label=「{(spot.get('label') or '')[:20]}」{hint} 形态={spot['kind']}")
    fields = "\n".join(
        f"- {v['key']} | {v['label']} | {v['value'][:24]}"
        for v in dictionary.values()
        if v["key"] not in RISKY and (v.get("value") or "").strip()
    )
    try:
        raw = await chat(
            cfg,
            [{"role": "system", "content": AI_SYSTEM},
             {"role": "user", "content": AI_TEMPLATE.format(spots="\n".join(lines), fields=fields or "（无）")}],
            json_mode=True,
            max_tokens=1200, task="extract",
        )
        data = parse_json(raw)
        mapping = data.get("mapping") if isinstance(data.get("mapping"), dict) else {}
        out: dict[str, str] = {}
        for spot_id, key in mapping.items():
            key = str(key).strip()
            spot_ids = {s["id"] for s in pending}
            if key in dictionary and key not in RISKY and str(spot_id) in spot_ids:
                out[str(spot_id)] = key
        return out
    except Exception:  # noqa: BLE001 — AI 匹配失败就保持规则层结果
        return {}


def merge_ai(planned: dict, mapping: dict[str, str], spots: list[dict], dictionary: dict[str, dict]) -> dict:
    """把 AI 匹配结果并入 plan：新增 filled、从 pending 移除。"""
    if not mapping:
        return planned
    by_id = {s["id"]: s for s in spots}
    filled_ids = {f["id"] for f in planned["filled"]}
    for spot_id, key in mapping.items():
        if spot_id in filled_ids:
            continue
        spot = by_id.get(spot_id)
        item = dictionary.get(key)
        if not spot or not item or not (item.get("value") or "").strip():
            continue
        planned["pending"] = [p for p in planned["pending"] if p.get("id") != spot_id]
        planned["filled"].append({
            **{k: v for k, v in spot.items() if k != "_para"},
            "field": key,
            "field_title": item["label"],
            "value": item["value"].strip(),
            "source": "ai",
            "risky": False,
        })
    planned["stats"] = {
        "total": planned["stats"]["total"],
        "filled": len(planned["filled"]),
        "pending": len(planned["pending"]),
        "rate": round(len(planned["filled"]) / max(1, planned["stats"]["total"]) * 100, 1),
        "risky": len([f for f in planned["filled"] if f["risky"]]),
        "ai": len(mapping),
    }
    return planned
