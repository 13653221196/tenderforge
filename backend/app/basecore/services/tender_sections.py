"""招标文件分段：把任意长度的招标文件拆成若干段，逐段送模型后合并。

真实招标文件（尤其央企/国网）常见 3–10 万字，一次塞不进模型上下文。
不能截断：合同条款、供货要求、投标文件格式往往排在后面，砍掉就漏项。
所以这里只做**分段**，不丢任何章节：

1. 用行首"第X章"切分正文（目录里的同名行靠倒序扫描剔除）；
2. 每章按行打包，单段不超过模型窗口，段首标注章节名（续段标"续"）；
3. 输出分段清单与覆盖率报告，供前端明示"分几段、覆盖了多少字"。

早期那个 `text[:40000]` 直接砍断、以及"按章节重要性取样"的做法都已废弃——
两者都会静默丢内容，而招标文件长度不是投标方能控制的。
"""
from __future__ import annotations

import re

CN_NUM = "一二三四五六七八九十百零〇"

# 行首章节标题：第三章  评标办法（综合评估法） / 第5章 供货要求
CHAPTER_RE = re.compile(rf"^第([{CN_NUM}\d]{{1,4}})章[\s、.．:：]*(.{{0,40}})$", re.M)

# 章节权重：命中即加权（可叠加），评标办法最重
WEIGHTS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("评标办法", "评分标准", "评分细则", "评审因素"), 40, "评标办法"),
    (("废标", "否决", "无效投标", "无效响应"), 30, "否决条款"),
    (("投标文件格式", "投标文件组成"), 28, "投标文件格式"),
    (("投标人须知",), 26, "投标人须知"),
    (("资格审查", "资格要求", "资格条件"), 24, "资格要求"),
    (("供货要求", "技术规范", "技术标准"), 22, "供货要求"),
    (("招标公告", "投标邀请书", "招标条件"), 18, "招标公告"),
    (("合同条款", "合同格式", "合同专用"), 12, "合同条款"),
    (("工程量清单", "货物清单", "报价要求"), 12, "清单报价"),
)

_CN_MAP = {c: i for i, c in enumerate("零一二三四五六七八九", 0)}


def cn_to_int(s: str) -> int:
    """'三'→3, '十一'→11, '二十'→20, '5'→5。认不出返回 0。"""
    s = s.strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN_MAP:
            cur = _CN_MAP[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            return 0
    return total + cur


def _norm_title(t: str) -> str:
    return re.sub(r"[\s　]+", " ", t or "").strip(" .．、:：")


def split_chapters(text: str) -> list[dict]:
    """切章。返回 [{no, title, start, end, body}]，按原文顺序。"""
    hits: list[tuple[int, int, str]] = []
    for m in CHAPTER_RE.finditer(text):
        no = cn_to_int(m.group(1))
        if no <= 0:
            continue
        hits.append((m.start(), no, _norm_title(m.group(2)) or f"第{no}章"))
    if not hits:
        return [{"no": 1, "title": "全文", "start": 0, "end": len(text), "body": text}]

    # 倒序扫描挑出"正文"那一套章节序列：目录里的同名标题在前面，
    # 要求章号与位置同时递减，才能把目录那套全部排除。
    picked: list[tuple[int, int, str]] = []
    want = 10**9
    last_pos = len(text) + 1
    for pos, no, title in reversed(hits):
        if no <= want and pos < last_pos:
            picked.append((pos, no, title))
            want = no - 1
            last_pos = pos
    picked.reverse()
    if not picked:
        return [{"no": 1, "title": "全文", "start": 0, "end": len(text), "body": text}]

    out: list[dict] = []
    # 首个章节标题之前的内容（封面、目录、前言）单列；哪怕很短也不丢
    if picked[0][0] > 0:
        head = text[: picked[0][0]]
        if head.strip():
            out.append({"no": 0, "title": "封面与目录", "start": 0, "end": picked[0][0], "body": head})
    for i, (pos, no, title) in enumerate(picked):
        end = picked[i + 1][0] if i + 1 < len(picked) else len(text)
        out.append({"no": no, "title": title, "start": pos, "end": end, "body": text[pos:end]})
    return out


def score_chapter(title: str, body: str) -> tuple[int, list[str]]:
    """给章节打分，同时返回命中的标签。标题命中加倍。"""
    score = 0
    tags: list[str] = []
    head = title or ""
    for words, weight, tag in WEIGHTS:
        if any(w in head for w in words):
            score += weight * 2
            tags.append(tag)
        elif any(w in body[:3000] for w in words):
            score += weight
            tags.append(tag)
    if not score:
        score = 2  # 兜底分，保证还有预算时会带一点上下文
    return score, tags


def _chapter_label(ch: dict, part: int = 0, total: int = 0) -> str:
    head = f"【第{ch['no']}章 {ch['title']}】" if ch["no"] else f"【{ch['title']}】"
    if total > 1:
        head += f"（{part}/{total} 段）"
    return head


def _pack_lines(body: str, room: int) -> list[str]:
    """按行把正文打包成若干段，每段不超过 room 字。"""
    room = max(500, room)
    segs: list[str] = []
    cur: list[str] = []
    size = 0
    for line in body.splitlines(keepends=True):
        # 单行超长（通常是没换行的表格整段），硬切成多行
        while len(line) > room:
            if cur:
                segs.append("".join(cur))
                cur, size = [], 0
            segs.append(line[:room])
            line = line[room:]
        if size + len(line) > room and cur:
            segs.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        segs.append("".join(cur))
    return segs


def plan(text: str, limit: int, *, pack: bool = True) -> tuple[list[dict], dict]:
    """把全文切成不超过 limit 的段，**不丢任何章节**。

    pack=True 时把相邻的短章并进同一段（段内仍保留各章标题），
    避免"封面 148 字"这类碎段单独发一次请求。

    返回 (chunks, report)：
      chunks: [{text, chapters, chars}]
      report: 兼容旧 sample 报告的字段，另加 mode / chunks / coverage
    """
    text = text or ""
    n = len(text)
    report = {
        "total_chars": n,
        "limit": limit,
        "kept_chars": n,
        "truncated": False,
        "mode": "full",
        "chunks": 1,
        "coverage": 100.0,
        "kept": [],
        "dropped": [],
        "note": "",
    }
    if n <= limit:
        report["kept"] = [{"title": "全文", "chars": n, "of": n}]
        report["note"] = f"原文 {n:,} 字，未超单次窗口，已全文送模型。"
        return [{"text": text, "chapters": [], "chars": n}], report

    chapters = split_chapters(text)
    per_chapter: list[dict] = []
    kept: list[dict] = []
    for ch in chapters:
        body = ch["body"]
        label = _chapter_label(ch)
        room = max(500, limit - len(label) - 16)
        segs = _pack_lines(body, room)
        if not segs:
            continue
        for i, seg in enumerate(segs, 1):
            head = _chapter_label(ch, i, len(segs)) if len(segs) > 1 else label
            per_chapter.append(
                {"text": head + "\n" + seg, "chapters": [ch["title"]], "chars": len(seg)}
            )
        kept.append({"title": ch["title"], "chars": len(body), "of": len(body)})

    chunks: list[dict] = []
    if pack:
        for piece in per_chapter:
            if chunks and len(chunks[-1]["text"]) + len(piece["text"]) + 2 <= limit:
                chunks[-1]["text"] += "\n\n" + piece["text"]
                chunks[-1]["chapters"] += piece["chapters"]
                chunks[-1]["chars"] += piece["chars"]
            else:
                chunks.append({**piece, "chapters": list(piece["chapters"])})
    else:
        chunks = per_chapter

    if not chunks:  # 极端兜底：切章失败时按长度硬切，仍然不丢内容
        raw = _pack_lines(text, limit)
        chunks = [{"text": s, "chapters": [], "chars": len(s)} for s in raw]
        kept = [{"title": "全文", "chars": n, "of": n}]

    report.update(
        {
            "mode": "multi",
            "chunks": len(chunks),
            "kept_chars": sum(c["chars"] for c in chunks),
            "kept": kept,
        }
    )
    report["note"] = (
        f"原文 {n:,} 字，超过单次窗口（{limit:,} 字），已自动分 {len(chunks)} 段"
        f"逐段解读并合并，全文 {n:,} 字全部覆盖，未丢弃任何章节。"
    )
    return chunks, report


def sample(text: str, limit: int) -> tuple[str, dict]:
    """【已废弃】按章节重要性取样，会丢章节内容。

    保留仅供旧测试与回滚使用；解读、要素分解、技能执行都已改用 plan()。
    """
    text = text or ""
    n = len(text)
    report = {
        "total_chars": n,
        "limit": limit,
        "kept_chars": n,
        "truncated": False,
        "kept": [],
        "dropped": [],
        "note": "",
    }
    if n <= limit:
        report["note"] = "原文未超长，已全文送模型。"
        return text, report

    chapters = split_chapters(text)
    for ch in chapters:
        ch["len"] = len(ch["body"])
        ch["score"], ch["tags"] = score_chapter(ch["title"], ch["body"])

    # 单章封顶：不让某一章（通常是"投标人须知"）吃掉大半预算，
    # 否则评标办法/资格要求会被挤掉——这正是真实文件最容易翻车的地方。
    cap = max(3000, int(limit * 0.40))
    take: dict[int, int] = {}
    budget = limit

    # 必留：首章（招标公告/投标邀请书）承载项目名称、招标人、资格门槛，
    # 缺了它基本信息和资格判断全废，先给保底配额。
    for i, ch in enumerate(chapters):
        if ch["no"] == 1 and any(w in ch["title"] for w in ("邀请书", "公告")):
            # 保底 20% 预算、最多 6000 字：够装项目名称/招标人/资格门槛
            keep = min(ch["len"], int(limit * 0.2), 6000, budget)
            take[i] = keep
            budget -= keep
            break

    # 其余按分数分配，同分按原顺序，保证确定性
    order = sorted(range(len(chapters)), key=lambda i: (-chapters[i]["score"], i))
    for i in order:
        if budget <= 200:  # 预算见底就停
            break
        ch = chapters[i]
        want = min(ch["len"] - take.get(i, 0), cap)
        if want <= 0:
            continue
        got = min(want, budget)
        take[i] = take.get(i, 0) + got
        budget -= got

    parts: list[str] = []
    for i, ch in enumerate(chapters):
        got = take.get(i, 0)
        label = f"【第{ch['no']}章 {ch['title']}】" if ch["no"] else f"【{ch['title']}】"
        if got <= 0:
            report["dropped"].append({"title": ch["title"], "chars": ch["len"]})
            continue
        body = ch["body"][:got]
        if got < ch["len"]:
            body += f"\n…（本章共 {ch['len']} 字，此处截取前 {got} 字）"
            report["truncated"] = True
        parts.append(label + "\n" + body)
        report["kept"].append(
            {"title": ch["title"], "chars": got, "of": ch["len"], "score": ch["score"], "tags": ch["tags"]}
        )

    out = "\n\n".join(parts)
    report["kept_chars"] = len(out)
    report["truncated"] = True
    full_kept = [k["title"] for k in report["kept"] if k["chars"] >= k["of"]]
    part_kept = [f"{k['title']}（留 {k['chars']}/{k['of']} 字）" for k in report["kept"] if k["chars"] < k["of"]]
    drop_names = "、".join(d["title"] for d in report["dropped"][:6])
    segs = [f"原文 {n} 字，超出模型单次窗口，已按章节重要性取样 {len(out)} 字"]
    if full_kept:
        segs.append("完整保留：" + "、".join(full_kept[:6]))
    if part_kept:
        segs.append("部分保留：" + "、".join(part_kept[:6]))
    if drop_names:
        segs.append("整章略过：" + drop_names)
    segs.append("被略过的章节请用「18 项要素分解」或原文核对补齐")
    report["note"] = "；".join(segs) + "。"
    return out, report
