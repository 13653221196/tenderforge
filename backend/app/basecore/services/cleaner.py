"""正文清洗与字数统计。

模型输出常见的三类瑕疵：token 化残渣（`z2日期`、`（z签字z`）、
重复字词（`的的`、`投标投标`）、格式凌乱（半角标点、连续空行、超长段）。

这里只做**确定性替换**，不改写语义；超出阈值的问题转成 issue 交人工判断。
需要改写语义时由 writer.rewrite_section 走模型，本模块不负责。
"""
from __future__ import annotations

import re
from typing import Any

CN_CHAR = re.compile(r"[\u4e00-\u9fff]")
WORD_UNIT = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")

# 重复的功能词：叠词（常常、一一、人人、渐渐）都是合法的，因此只收必然重复即错的高频功能字
DUP_FUNC = re.compile(r"(的|了|是|和|与|在|及|或|为|中|对|我|你|他|它|就|都|并|而|其)\1+")

# 连续重复的词语：投标投标文件 -> 投标文件；分段限制 2-10 字，避免吃掉合法的单字叠词
DUP_PHRASE = re.compile(r"([\u4e00-\u9fff]{2,10})\1+")

# 拉丁字母残渣：紧贴汉字/中文标点（含中间空格）的 z、z2、zz（模型分词残留）
LATIN_JUNK = re.compile(
    r"(?<=[\u4e00-\u9fff（(：:，、])[ \t]?[zZ]\d{0,2}[zZ]?(?=[\u4e00-\u9fff）)】\]，,。；;：:]|$)"
)

PLACEHOLDER = re.compile(r"(?<![A-Za-z])(?:X{3,}|x{3,}|×{3,}|＊{2,})(?![A-Za-z])")
PLACEHOLDER_HINT = re.compile(r"〔待填|\[待填\]|待补充|请补充|TBD|TODO")

# 半角标点夹在汉字之间，转全角；数字里的点（1.5）前面是数字，不会被命中
HALF_PUNCT = (
    (re.compile(r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])"), "，"),
    (re.compile(r"(?<=[\u4e00-\u9fff]);(?=[\u4e00-\u9fff])"), "；"),
    (re.compile(r"(?<=[\u4e00-\u9fff]):(?=[\u4e00-\u9fff])"), "："),
    (re.compile(r"(?<=[\u4e00-\u9fff])\?(?=[\u4e00-\u9fff])"), "？"),
    (re.compile(r"(?<=[\u4e00-\u9fff])!(?=[\u4e00-\u9fff])"), "！"),
)

# 连续标点（省略号、破折号是合法的，不在此列）
DUP_PUNCT = re.compile(r"([，。；：、！？])\1+")
# 中文标点后紧跟同类半角标点：，, -> ，
HALF_TAIL = re.compile(r"(?<=[，。；：、！？])[ \t]*[,.;:!?]")
HAN_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")
MANY_BLANK = re.compile(r"\n{3,}")
TRAIL_SPACE = re.compile(r"[ \t]+$", re.M)
EMPTY_HEADING = re.compile(r"^#{1,6}[ \t]*$", re.M)
# 行首 Markdown 标题标记与序号（“### 1.1 投标函” → “投标函”），用于比对是否重复了章节标题
HEAD_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]*")
TITLE_NUM = re.compile(r"^[（(]?[一二三四五六七八九十百]+[）)、.]|^\d+(?:\.\d+)*[.、]?\s*|^第[一二三四五六七八九十百]+[章节条]\s*")
BOLD_MARK = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")

SENT_SPLIT = re.compile(r"[。！？；\n]+")


def count_words(text: str) -> int:
    """中文字数：汉字按字计，英文/数字按词计——与 Word 的“字数”口径基本一致。"""
    if not text:
        return 0
    return len(CN_CHAR.findall(text)) + len(WORD_UNIT.findall(CN_CHAR.sub("", text)))


def count_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text or ""))


def sentence_list(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if len(s.strip()) >= 8]


def _apply(pattern: re.Pattern[str], repl: Any, text: str, kind: str, fixes: list[dict]) -> str:
    matches = pattern.findall(text)
    if not matches:
        return text
    new = pattern.sub(repl, text)
    count = len(matches)
    sample = matches[0]
    if isinstance(sample, tuple):
        sample = "".join(sample)
    fixes.append({"kind": kind, "count": count, "sample": str(sample)[:20]})
    return new


def _norm_head(line: str) -> str:
    """把一行标题压成可比对的纯文字：去 # 标记、去序号、去标点空白。"""
    body = HEAD_LINE.sub("", line.strip())
    body = TITLE_NUM.sub("", body.strip())
    return re.sub(r"[\s\u3000*#：:.、]+", "", body)


def strip_dup_heading(text: str, title: str) -> tuple[str, int]:
    """模型常在正文首行再写一遍章节标题（`### 1.1 投标函`），与模板标题重复，去掉。"""
    if not (text or "").strip() or not (title or "").strip():
        return text, 0
    want = _norm_head(title)
    if not want:
        return text, 0
    lines = text.split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None:
        return text, 0
    first = lines[idx]
    got = _norm_head(first)
    if not got:
        return text, 0
    # 首行是标题且与本节标题基本一致（"投标函正文" vs "投标函"，"1.1 投标函" vs "投标函"）才算重复
    same = got == want or want in got or got in want
    if not (same and (first.strip().startswith("#") or got == want)):
        return text, 0
    rest = lines[idx + 1 :]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(rest), 1


def clean_text(text: str, *, title: str = "") -> dict:
    """返回清洗后的正文 + 变更摘要 + 需要人工关注的 issue。"""
    raw = text or ""
    fixes: list[dict] = []
    out = raw

    out, dropped = strip_dup_heading(out, title)
    if dropped:
        fixes.append({"kind": "重复标题", "count": dropped, "sample": (title or "")[:20]})

    # 残渣与重复，各跑两轮以处理嵌套（“的的的”）
    for _ in range(2):
        out = _apply(LATIN_JUNK, "", out, "分词残渣", fixes)
        out = _apply(DUP_FUNC, r"\1", out, "重复字", fixes)
        out = _apply(DUP_PHRASE, r"\1", out, "重复词语", fixes)
    out = _apply(EMPTY_HEADING, "", out, "空标题", fixes)
    out = _apply(DUP_PUNCT, r"\1", out, "重复标点", fixes)
    out = _apply(HALF_TAIL, "", out, "重复标点", fixes)

    for pattern, repl in HALF_PUNCT:
        out = _apply(pattern, repl, out, "半角标点", fixes)

    out = _apply(HAN_GAP, "", out, "汉字间空格", fixes)
    out = _apply(TRAIL_SPACE, "", out, "行尾空格", fixes)
    out = _apply(MANY_BLANK, "\n\n", out, "多余空行", fixes)
    out = out.strip()
    if out and not out.endswith(("\n",)) :
        out = out.rstrip()

    # 合并同类项，按出现次数排序
    merged: dict[str, dict] = {}
    for f in fixes:
        key = f"{f['kind']}|{f['sample']}"
        if key in merged:
            merged[key]["count"] += f["count"]
        else:
            merged[key] = dict(f)
    fix_list = sorted(merged.values(), key=lambda x: -x["count"])

    stats = analyze(out)
    issues = lint(out, stats)
    return {
        "text": out,
        "changed": out != raw.strip(),
        "fixes": fix_list,
        "stats": stats,
        "issues": issues,
    }


def analyze(text: str) -> dict:
    paras = [p.strip() for p in (text or "").split("\n") if p.strip() and not p.strip().startswith("#")]
    sents = sentence_list(text)
    long_paras = [p for p in paras if len(p) > 300]
    return {
        "words": count_words(text),
        "chars": count_chars(text),
        "paras": len(paras),
        "sentences": len(sents),
        "avg_sentence": round(sum(len(s) for s in sents) / len(sents), 1) if sents else 0,
        "long_paras": len(long_paras),
    }


def lint(text: str, stats: dict | None = None) -> list[dict]:
    """不改正文，只报告需要人看的问题。"""
    stats = stats or analyze(text)
    issues: list[dict] = []

    ph = PLACEHOLDER.findall(text or "")
    hint = PLACEHOLDER_HINT.findall(text or "")
    if ph or hint:
        issues.append(
            {
                "level": "中",
                "title": "存在未填写的占位内容",
                "detail": f"占位符 {len(ph)} 处、待填标记 {len(hint)} 处，导出前须替换为实际内容。",
                "evidence": "、".join((ph + hint)[:8]),
            }
        )

    if stats["long_paras"]:
        issues.append(
            {
                "level": "低",
                "title": "段落过长",
                "detail": f"{stats['long_paras']} 个段落超过 300 字，评委阅读吃力，建议按语义拆段。",
                "evidence": "",
            }
        )

    # 完全重复的长句：同一段话反复出现，通常是模型复读
    sents = [s for s in sentence_list(text) if len(s) >= 15]
    seen: dict[str, int] = {}
    for s in sents:
        seen[s] = seen.get(s, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    if dups:
        top = sorted(dups.items(), key=lambda kv: -kv[1])[:3]
        issues.append(
            {
                "level": "中",
                "title": "句子重复出现",
                "detail": f"{len(dups)} 句话在本节内重复，建议合并或改写。",
                "evidence": "；".join(f"{k[:24]}…（{v} 次）" for k, v in top),
            }
        )

    if stats["avg_sentence"] > 60:
        issues.append(
            {
                "level": "低",
                "title": "句子偏长",
                "detail": f"平均句长 {stats['avg_sentence']} 字，超过 60 字读起来吃力，建议断句。",
                "evidence": "",
            }
        )
    return issues


def suggest(current: int, target: int) -> dict:
    """按达成率给出下一步动作建议。"""
    if target <= 0:
        return {"action": "none", "ratio": 0, "gap": 0, "label": "未设目标"}
    ratio = round(current / target, 2)
    gap = target - current
    if ratio < 0.7:
        return {"action": "expand", "ratio": ratio, "gap": gap, "label": f"仅达 {int(ratio * 100)}%，建议补写约 {gap} 字"}
    if ratio > 1.5:
        return {"action": "condense", "ratio": ratio, "gap": gap, "label": f"超出 {int(ratio * 100)}%，建议精简约 {-gap} 字"}
    return {"action": "none", "ratio": ratio, "gap": gap, "label": f"达标 {int(ratio * 100)}%"}
