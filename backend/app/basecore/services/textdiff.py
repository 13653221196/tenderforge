"""正文版本对比：段落级 diff + 改动段内的字级高亮。

版本快照存的是整篇正文，用户要看的是「这一版和现在差在哪」。
做法：
1. 按空行/换行把正文切成段落（长段再按句号切），段落级 SequenceMatcher 求差异；
2. 相邻的「删除 + 新增」配对做字级 diff，只标出真正改动的字，而不是整段红绿；
3. 输出结构化的 blocks，前端直接渲染，不需要自己算 diff。

不调模型，纯确定性计算。
"""
from __future__ import annotations

import difflib
import re

# 一段超过这个长度就按句子拆，避免整段被当成一个不可分的块
_SPLIT_LIMIT = 160


def paragraphs(text: str) -> list[str]:
    """把正文切成便于对比的粒度：先按行，过长行再按句。"""
    out: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) <= _SPLIT_LIMIT:
            out.append(stripped)
            continue
        # 长段按句号/分号切，保留标点
        for sent in re.findall(r"[^。；;！!？?]+[。；;！!？?]?", stripped):
            piece = sent.strip()
            if piece:
                out.append(piece)
    return out


def _merge(segs: list[dict]) -> list[dict]:
    """合并相邻同类片段，否则页面会被切成「2」「8」这类碎片。"""
    out: list[dict] = []
    for seg in segs:
        if not seg["text"]:
            continue
        if out and out[-1]["op"] == seg["op"]:
            out[-1]["text"] += seg["text"]
        else:
            out.append({"op": seg["op"], "text": seg["text"]})
    return out


def _char_diff(before: str, after: str) -> tuple[list[dict], list[dict]]:
    """字级对比，返回 (before 片段, after 片段)，片段带 keep/del/add 标记。"""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    left: list[dict] = []
    right: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            if i2 > i1:
                left.append({"op": "keep", "text": before[i1:i2]})
                right.append({"op": "keep", "text": after[j1:j2]})
        elif tag == "delete":
            left.append({"op": "del", "text": before[i1:i2]})
        elif tag == "insert":
            right.append({"op": "add", "text": after[j1:j2]})
        else:  # replace
            left.append({"op": "del", "text": before[i1:i2]})
            right.append({"op": "add", "text": after[j1:j2]})
    return _merge(left), _merge(right)


def compare(before: str, after: str, *, context: int = 2, limit: int = 400) -> dict:
    """对比两版正文。

    before = 历史快照，after = 当前正文（或另一版）。
    context 控制未改动段落保留几段作上下文，其余折叠成「省略 N 段」。
    """
    old = paragraphs(before)
    new = paragraphs(after)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)

    blocks: list[dict] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # 逐段拆开，折叠「未改动区」时才能按段计数（整块 60 段一起保留太占地方）
            for line in old[i1:i2]:
                blocks.append({"op": "keep", "lines": [line]})
            continue
        pairs = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        # 成对的段落做字级对比，能看清「改了哪几个字」
        for k in range(pairs):
            left_seg, right_seg = _char_diff(old[i1 + k], new[j1 + k])
            blocks.append({"op": "change", "left": left_seg, "right": right_seg})
            added += 1
            removed += 1
        for line in old[i1 + pairs : i2]:
            blocks.append({"op": "del", "left": [{"op": "del", "text": line}], "right": []})
            removed += 1
        for line in new[j1 + pairs : j2]:
            blocks.append({"op": "add", "left": [], "right": [{"op": "add", "text": line}]})
            added += 1

    # 只保留改动附近 context 段上下文，其余折叠，避免整篇糊在页面上
    kept: list[dict] = []
    changed_idx = [i for i, b in enumerate(blocks) if b["op"] != "keep"]
    if changed_idx:
        keep_range: set[int] = set()
        for idx in changed_idx:
            for d in range(-context, context + 1):
                if 0 <= idx + d < len(blocks):
                    keep_range.add(idx + d)
        hidden = 0
        for i, block in enumerate(blocks):
            if i in keep_range:
                if hidden:
                    kept.append({"op": "skip", "count": hidden})
                    hidden = 0
                kept.append(block)
            else:
                hidden += 1
        if hidden:
            kept.append({"op": "skip", "count": hidden})
    else:
        kept = blocks[:limit]

    return {
        "summary": {
            "added": added,
            "removed": removed,
            "changed": len(changed_idx),
            "words_before": len(before or ""),
            "words_after": len(after or ""),
            "identical": not changed_idx,
        },
        "blocks": kept[:limit],
        "truncated": len(kept) > limit,
    }
