"""知识库检索：以 LangChain BaseRetriever 接口实现，中文 char-bigram 打分。

不依赖 embedding / 向量库，离线可跑；后续要换向量检索，只需替换本类，
上层的 LCEL 链和受控编排一行不改。

证据粒度是「段落 / 页」而不是整篇文档：每个命中都带 doc_id、页码、
字符区间和命中原文，供溯源、页码回跳与数值核对使用。
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from . import emb_bridge, store

PAGE_RE = re.compile(r"【第\s*(\d+)\s*页】")
CHUNK_SIZE = 320


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "").lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score(query: str, title: str, content: str) -> float:
    q = _grams(query)
    if not q:
        return 0.0
    body = _grams(content[:4000])
    title_g = _grams(title)
    overlap = len(q & body)
    title_hit = len(q & title_g)
    return overlap / (len(q) ** 0.5) + title_hit * 1.5


def split_spans(content: str) -> list[dict]:
    """把一篇资料切成带坐标的片段：先按【第 N 页】分页，再按段落合并到 ~320 字。

    返回 (start, end, text, page)；start/end 是全文字符区间，page=0 表示无页码标记。
    """
    marks = list(PAGE_RE.finditer(content or ""))
    segments = ([(m.end(), marks[i + 1].start() if i + 1 < len(marks) else len(content),
                  int(m.group(1))) for i, m in enumerate(marks)]
                if marks else [(0, len(content or ""), 0)])
    out: list[dict] = []
    for s0, e0, page in segments:
        body = (content or "")[s0:e0]
        pos, buf, buf_start = s0, [], s0
        for block in re.split(r"(\n\s*\n)", body):
            if block.strip():
                if not buf:
                    buf_start = pos
                buf.append(block)
                joined = "".join(buf)
                if len(joined) >= CHUNK_SIZE:
                    out.append({"start": buf_start, "end": buf_start + len(joined),
                                "text": joined.strip(), "page": page})
                    buf = []
            pos += len(block)
        if buf:
            joined = "".join(buf)
            out.append({"start": buf_start, "end": buf_start + len(joined),
                        "text": joined.strip(), "page": page})
    return [x for x in out if x["text"].strip()]


def _ref(span: dict) -> str:
    if span["page"]:
        return f"第 {span['page']} 页 · 字符 {span['start']}-{span['end']}"
    return f"字符 {span['start']}-{span['end']}"


def _to_documents(rows: list[dict], scope: str) -> list[Document]:
    docs: list[Document] = []
    for row in rows:
        # B02：整本标书范例只能学结构/写法，命中时要显式标注不得照抄事实
        is_example = str(row.get("kind") or "") == "sample"
        for sp in split_spans(row.get("content") or ""):
            docs.append(Document(
                page_content=sp["text"],
                metadata={"id": row["id"], "title": row["title"], "scope": scope,
                          "doc_id": row["id"], "page": sp["page"],
                          "start": sp["start"], "end": sp["end"], "ref": _ref(sp),
                          "is_example": is_example},
            ))
    return docs


# 企业素材库本来就是给全公司跨项目复用的；但带明确品类词的素材
# （例如「视频监控技术方案标准模板」）不该串进电缆附件项目当「本次依据」，
# 更不能进写作的检索池。没有招标文件时一律不列跨项目素材，避免凭空引用。
_GENERIC_TERMS = (
    "标准模板", "填写规范", "编写要点", "标准条款", "条款库", "技术方案",
    "响应表", "偏离表", "通用素材", "同类项目", "模板", "规范", "要点",
    "标准", "清单", "说明", "文件", "资料", "手册", "指南",
)


def _core_terms(title: str) -> list[str]:
    """去掉「模板 / 规范 / 要点」这类通用文档词，留下能标识品类的核心词。"""
    t = re.sub(r"[（(][^）)]*[）)]", "", str(title or ""))
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", t)
    parts: list[str] = []
    for piece in t.split():
        for generic in _GENERIC_TERMS:
            piece = piece.replace(generic, " ")
        parts.extend(x.strip() for x in piece.split() if len(x.strip()) >= 2)
    return parts


def _project_context(pid: str) -> str:
    """项目语境：项目名 + 招标原文 + 本项目资料标题。没有招标原文就返回空。"""
    if not pid:
        return ""
    try:
        with store.conn() as c:
            row = c.execute("SELECT name, tender_text FROM projects WHERE id=?",
                            (pid,)).fetchone()
        if not row:
            return ""
        tender = str(row["tender_text"] or "")[:30000]
        titles = " ".join(str(d.get("title") or "") for d in
                          store.list_docs(scope="project", project_id=pid, usable_only=True))
        # 只建了项目、还没导入招标文件时，没有任何可判断品类的依据
        if not tender.strip() and not titles.strip():
            return ""
        return str(row["name"] or "") + "\n" + tender + "\n" + titles
    except Exception:
        return ""


# 这些是纯业务通用素材，本来就该跨项目复用，不参与品类判定。
_GENERIC_BUSINESS = ("资质", "营业执照", "承诺", "认证", "财务", "法人", "授权",
                     "业绩", "售后", "服务", "质量", "通用", "公司", "企业",
                     "保密", "廉洁", "安全", "标准条款")


def project_kb_rows(pid: str = "") -> list[dict]:
    """与本项目语境相关的企业素材行（已过滤不可用件）。

    标书范例与纯通用素材仍可跨项目复用；带品类词的素材必须在本项目招标
    原文 / 项目名 / 本项目资料里找得到同类词才放行。
    """
    rows = store.list_docs(scope="kb", usable_only=True)
    if not pid:
        return rows
    ctx = _project_context(pid)
    if not ctx:
        return []
    out: list[dict] = []
    for r in rows:
        title = str(r.get("title") or "")
        # 范例是用户显式收录或指定参照的，且检索时已标注「只学写法、不当事实」
        if str(r.get("kind") or "") == "sample":
            out.append(r)
            continue
        if any(w in title for w in _GENERIC_BUSINESS):
            out.append(r)
            continue
        terms = _core_terms(title)
        # 核心词被通用写法词剥光的素材（如「同类项目偏离表编写要点」）过去
        # 走 `not terms` 无条件放行，就成了「本次依据」里甩不掉的串项目感。
        # 这类纯写法素材只有标题带明确通用业务词（上面已放行）才跨项目用；
        # 其余必须有能在本项目语境里命中的品类词，绝不凭空列为依据。
        if not terms:
            continue
        if any(t in ctx for t in terms):
            out.append(r)
    return out


def _all_docs(pid: str) -> list[Document]:
    docs = _to_documents(project_kb_rows(pid), "企业素材库")
    if pid:
        docs += _to_documents(store.list_docs(scope="project", project_id=pid, usable_only=True),
                              "我提供的资料")
    return docs


def _rank_docs(docs: list[Document], query: str) -> tuple[list[Document], Any]:
    """词法排名。开关关闭时 BidRetriever 与 retrieve() 共用，结果逐字一致。"""
    def rank(d: Document) -> float:
        base = _score(query, d.metadata.get("title", ""), d.page_content)
        # 本项目资料是本项目的事实口径，优先级高于可跨项目复用的企业素材库
        return base * (1.25 if d.metadata.get("scope") == "我提供的资料" else 1.0)

    return sorted(docs, key=rank, reverse=True), rank


class BidRetriever(BaseRetriever):
    """标准 LangChain Retriever，可直接用 `retriever | prompt | model` 组成链。"""

    project_id: str = ""
    k: int = 4

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        ranked, rank = _rank_docs(_all_docs(self.project_id), query)
        return [d for d in ranked[: self.k] if rank(d) > 0]


def retrieve(pid: str, query: str, k: int = 4) -> tuple[str, list[dict[str, Any]]]:
    """返回 (可注入提示词的依据文本, 带坐标的命中来源列表)。

    开关关闭（或百炼不可用）时走原来的纯词法路径，结果与改造前一致；打开时
    把词法名次与语义名次按 chunk_key 做 RRF 融合。向量只改变「挑哪几段」，
    注入提示词的仍是带 doc_id / 页码 / 字符区间的原文片段，hits 结构不变。
    语义召回但字面零命中的片段显式标注「须人工核对」，不作为已核实依据。
    """
    want = int(k)
    docs = BidRetriever(project_id=pid, k=want).invoke(query)
    semantic_only: set[str] = set()
    doc_key: dict[int, str] = {}
    if want > 0 and emb_bridge.ready("kb"):
        all_docs = _all_docs(pid)
        ranked, rank = _rank_docs(all_docs, query)
        chunks = emb_bridge.chunks_from_documents(all_docs)
        doc_key = {id(all_docs[i]): chunks[i]["chunk_key"]
                   for i in range(len(all_docs))}
        by_key = {chunks[i]["chunk_key"]: all_docs[i]
                  for i in range(len(all_docs))}
        dense = emb_bridge.rank_chunks("kb", chunks, query, pid=pid,
                                       top_k=max(want * 3, 12))
        if dense:
            lex_rows = [{"chunk_key": chunks[i]["chunk_key"]}
                        for i, d in enumerate(all_docs) if rank(d) > 0]
            dense_rows = [{"chunk_key": str(r["chunk_key"]), "score": r["score"]}
                          for r in dense]
            fused = emb_bridge.fuse([lex_rows, dense_rows], top_k=want)
            lex_keys = {r["chunk_key"] for r in lex_rows}
            dense_keys = {r["chunk_key"] for r in dense_rows}
            semantic_only = {r["chunk_key"] for r in fused
                             if r["chunk_key"] in dense_keys
                             and r["chunk_key"] not in lex_keys}
            picked = [by_key[r["chunk_key"]] for r in fused
                      if r["chunk_key"] in by_key]
            if picked:
                docs = picked
    hits: list[dict[str, Any]] = []
    lines: list[str] = []
    for d in docs:
        m = d.metadata
        text = d.page_content[:600].strip()
        example = bool(m.get("is_example"))
        sem_only = doc_key.get(id(d), "") in semantic_only
        hits.append({
            "title": m.get("title", ""), "scope": m.get("scope", ""),
            "snippet": text[:160], "text": text[:400],
            "doc_id": m.get("doc_id", ""), "page": int(m.get("page") or 0),
            "start": int(m.get("start") or 0), "end": int(m.get("end") or 0),
            "ref": m.get("ref") or _ref({"start": 0, "end": 0, "page": 0}),
            "is_example": example,
            "semantic_only": sem_only,
            "fact_usable": not example and not sem_only,
        })
        tag = "标书范例·只学写法，不得当作本项目事实或数值依据" if example else m.get("scope", "")
        if sem_only:
            tag = (tag + "·语义召回候选，须人工核对") if tag else "语义召回候选，须人工核对"
        lines.append(f"【{tag}·{m.get('title', '')}·{m.get('ref', '')}】\n{text}")
    return "\n\n".join(lines), hits


def materials_block(pid: str) -> str:
    """本项目我提供的资料：生成时优先级最高的事实依据。"""
    rows = store.list_docs(scope="project", project_id=pid, usable_only=True)
    if not rows:
        return "（未上传本项目资料）"
    return "\n\n".join(f"【{r['title']}】\n{r['content'][:900]}" for r in rows)
