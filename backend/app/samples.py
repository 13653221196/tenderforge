"""标书范例：把一本写好的标书/竞标书按章节拆开存库，写作时按「同名章节」参考。

和通用素材的区别：
- 通用素材是片段，按内容相似度检索，命中哪段用哪段；
- 范例是整本，先按标题切成章，写作时找「同一章」——例如写「技术方案」，
  就拿范例里的「技术方案」整章作参照，学它的结构、层次、口径与篇幅，
  而不是拿零散段落拼。只学写法，不抄公司名、金额、人名和案例。
"""
from __future__ import annotations

import re

from . import store

# 与 agent.import_draft 同一套标题规则：编号标题、独占一行的短标题都算
HEAD_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百\d]+[章节部分篇]|"
    r"[一二三四五六七八九十]+[、.]|"
    r"[（(][一二三四五六七八九十\d]+[)）]|"
    r"\d+(\.\d+)*[.、\s])\s*(.{2,40})$"
)
CHAPTER_WORDS = ("投标函", "技术方案", "商务", "报价", "资格", "资质", "服务", "售后",
                 "实施", "组织", "承诺", "偏离", "响应", "项目概况", "授权", "业绩",
                 "附件", "投标一览表", "法定代表人", "技术规格", "项目团队", "方案",
                 "培训", "验收", "运维", "安全", "质量", "进度", "人员", "设备", "说明")

MIN_BID_CHARS = 3000   # 长文 + 多章，基本就是整本
MIN_CHAPTERS = 3
# 只有标书才这么排章，命中两个以上就按整本范例处理，不受字数限制
BID_WORDS = ("投标函", "投标书", "商务", "技术", "报价", "资格", "资质", "业绩", "售后",
             "服务", "实施方案", "组织", "承诺", "偏离", "响应", "概况", "授权", "团队")


def _is_heading(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) > 40:
        return False
    if HEAD_RE.match(s):
        return True
    if len(s) <= 20 and not re.search(r"[。；;：:，,、]$", s) and any(k in s for k in CHAPTER_WORDS):
        return True
    return False


def _level(title: str) -> int:
    """粗分层级：章 / 一、 / 1. 算一级，2.1、（一）算二级，用于目录展示与建目录取章。"""
    s = (title or "").strip()
    if re.match(r"^第[一二三四五六七八九十百\d]+[章节部分篇]", s):
        return 1
    if re.match(r"^[一二三四五六七八九十]+[、.]", s):
        return 1
    if re.match(r"^\d+\.\d+", s) or re.match(r"^[（(]", s):
        return 2
    if re.match(r"^\d+[.、\s]", s):
        return 1
    return 2


def split(text: str) -> list[dict]:
    """按标题切章，返回 [{idx,title,level,content}]；切不出标题时整篇算一章。"""
    lines = (text or "").splitlines()
    chunks: list[tuple[str, list[str]]] = []
    cur, buf = "正文", []
    for line in lines:
        if _is_heading(line):
            if "".join(buf).strip() or chunks:
                chunks.append((cur, buf))
            cur, buf = line.strip()[:60], []
        else:
            buf.append(line)
    chunks.append((cur, buf))
    out: list[dict] = []
    for i, (title, body) in enumerate(chunks):
        content = "\n".join(body).strip()
        if not content and i == 0:
            continue
        out.append({"idx": len(out), "title": title, "level": _level(title),
                    "content": content})
    # 有的标书章标题下面直接是小节（第三章 → 3.1、3.2），章自己的正文是空的。
    # 把紧跟其后的小节正文补进这一章，否则「写技术方案」时找不到范例。
    for i, ch in enumerate(out):
        if ch["content"] or ch["level"] != 1:
            continue
        parts = []
        for nxt in out[i + 1:]:
            if nxt["level"] == 1:
                break
            if nxt["content"]:
                parts.append(f"{nxt['title']}\n{nxt['content']}")
        if parts:
            ch["content"] = "\n\n".join(parts)
    return out


def looks_like_bid(text: str) -> bool:
    """整本标书（而不是片段）的判定。

    先看结构：能切出 3 个以上一级章节，且其中至少两个是标书常见章（投标函 / 技术 / 商务 /
    报价 / 售后…），就按整本范例处理——很多范例本身写得很短，不能只看字数。
    结构不像标书时，再看长度：超过 MIN_BID_CHARS 且分了多章，也算整本。
    """
    body = (text or "").strip()
    if len(body) < 400:
        return False
    chapters = split(body)
    top = [c for c in chapters if c["level"] == 1 and c["title"] != "正文"]
    if len(top) < MIN_CHAPTERS:
        return False
    hits = sum(1 for c in top if any(k in c["title"] for k in BID_WORDS))
    return hits >= 2 or len(body) >= MIN_BID_CHARS


def ingest(doc_id: str, text: str) -> int:
    """把整本范例按章拆开入库，返回章节数。"""
    chapters = split(text)
    store.save_chapters(doc_id, chapters)
    return len(chapters)


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"[\s\W_]+", "", (s or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _title_score(want: str, have: str) -> float:
    a, b = _bigrams(want), _bigrams(have)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def find_reference(pid: str, title: str, extra: str = "", limit: int = 2400) -> dict | None:
    """为某一章挑一段范例参考。

    优先用项目指定的那本范例；同一本书里按标题相似度挑章，挑不到再看正文相关度。
    返回 {doc_id, doc_title, chapter, content, score}，找不到返回 None。
    """
    docs = [d for d in store.list_docs(scope="kb", kind="sample", usable_only=True) if d.get("content")]
    if not docs:
        return None
    p = store.get_project(pid) or {}
    chosen = (p.get("sample_doc_id") or "").strip()
    if chosen:
        docs.sort(key=lambda d: 0 if d["id"] == chosen else 1)
    best: tuple[float, dict, dict] | None = None
    for doc in docs[:6]:
        chapters = store.list_chapters(doc["id"]) or [
            {"idx": 0, "title": doc["title"], "level": 1, "content": doc["content"]}]
        for ch in chapters:
            if not (ch.get("content") or "").strip():
                continue
            score = _title_score(title, ch.get("title") or "") * 2.0
            if extra:
                pool = _bigrams(extra)
                body = _bigrams((ch.get("content") or "")[:1500])
                if pool:
                    score += len(pool & body) / min(len(pool), 600)
            if doc["id"] == chosen:
                score += 0.25   # 本项目的参照范例优先
            if best is None or score > best[0]:
                best = (score, doc, ch)
    if not best or best[0] <= 0.02:
        return None
    score, doc, ch = best
    body = (ch.get("content") or "").strip()
    # 参考取「开头结构 + 中间要点」，太长没用还费 token
    excerpt = body[:limit] if len(body) <= limit else body[: limit - 200] + "\n……\n" + body[-200:]
    return {"doc_id": doc["id"], "doc_title": doc.get("title") or "",
            "chapter": ch.get("title") or "", "content": excerpt,
            "score": round(score, 3), "chars": len(body)}


def reference_block(pid: str, title: str, extra: str = "") -> tuple[str, dict | None]:
    """给提示词用的范例段落；明确标注只学写法，不许照抄。"""
    ref = find_reference(pid, title, extra)
    if not ref:
        return "（知识库里还没有可参考的标书范例）", None
    block = (
        f"【范例·{ref['doc_title']}·{ref['chapter']}】\n"
        f"{ref['content']}\n"
        "（上面是范例写法：学它的章节层次、小标题拆法、句式和详略程度；"
        "公司名、人名、金额、项目案例、证书编号一律不得照抄，必须换成本项目的事实。）"
    )
    return block, ref


def sweep_existing() -> dict:
    """升级后的整理：老库里按旧规则存成「通用素材」的整本标书，改成范例并拆章。

    只在当前企业的知识库还没拆过任何章时跑一次，之后用户自己改的类型不会被覆盖。
    """
    if store.chapter_counts():
        return {"skipped": True, "converted": 0}
    converted = 0
    for doc in store.list_docs(scope="kb", usable_only=True):
        if doc.get("kind") == "sample":
            continue
        text = doc.get("content") or ""
        if not looks_like_bid(text):
            continue
        store.update_doc(doc["id"], kind="sample")
        ingest(doc["id"], text)
        converted += 1
    return {"skipped": False, "converted": converted}


def outline_titles(doc_id: str, max_chapters: int = 30) -> list[str]:
    """取范例的章节目录，用来重建当前项目的目录结构。

    优先只取一级章节（第一章 / 一、 / 1.）：这是标书的章，写正文按章写；
    一级少于 3 个时退回用全部标题，避免有的范例只分了小节。
    """
    chapters = store.list_chapters(doc_id)
    if not chapters:
        doc = store.get_doc(doc_id)
        chapters = split((doc or {}).get("content") or "")
    valid = [c for c in chapters if c.get("title") and c["title"] != "正文"]
    top = [c["title"] for c in valid if c.get("level") == 1]
    titles = top if len(top) >= 3 else [c["title"] for c in valid]
    return titles[:max_chapters]
