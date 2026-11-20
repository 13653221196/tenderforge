"""企业分库内的向量索引：只在开关打开时才会被创建和写入。

设计边界（对应 docs/向量检索接入方案.md 第 2、3、6 节）：

- 表建在 **当前企业物理分库**（store.db_path()），不是 basecore 的全局库；
  物理隔离优先，少写一个 WHERE 也读不到别家数据。
- 只存向量与证据坐标，**不存资料正文**：正文始终从 kb_docs / 招标原文读，
  注入提示词的必须是可回跳的原文，不能用向量或缓存文本顶替。
- 调用方必须用 ``keys`` 白名单限定本次可参与召回的片段（来自
  ``rag._all_docs(usable_only=True)`` 或当前项目招标原文切片），避免把别的
  项目、待核对件、标书范例当成本项目事实。
- 二进制格式与 basecore/services/emb.py 的 to_blob/from_blob 完全兼容
  （小端 float32），但本模块刻意不导入 basecore：它的 db.py 绑定全局
  basecore.db，导入即带来跨企业配置耦合。

默认 enabled=false 时，主链路不会调用本模块的任何函数，零副作用、零费用。
"""
from __future__ import annotations

import hashlib
import struct
import threading
from typing import Any

from . import store

TABLE = "kb_vectors"

DDL = """
CREATE TABLE IF NOT EXISTS kb_vectors (
    chunk_key  TEXT PRIMARY KEY,
    source     TEXT NOT NULL DEFAULT 'kb',
    doc_id     TEXT DEFAULT '',
    start_pos  INTEGER DEFAULT 0,
    end_pos    INTEGER DEFAULT 0,
    page       INTEGER DEFAULT 0,
    text_hash  TEXT DEFAULT '',
    emb        BLOB,
    model_tag  TEXT DEFAULT '',
    dim        INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_vectors_tag ON kb_vectors(model_tag, dim, source);
"""

_SCHEMA_READY: set[str] = set()
_LOCK = threading.Lock()


def chunk_key(source: str, doc_id: str, start: int, end: int, text: str) -> str:
    """片段唯一键：来源 + 文档 + 全文字符区间 + 正文指纹。

    同一篇文档会被切成多个片段，只用 doc_id 做融合键会互相覆盖；正文指纹
    保证同一坐标下正文变了（重新解析 / 重新上传）旧向量立即失效。
    """
    digest = hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]
    raw = f"{source}|{doc_id}|{int(start)}|{int(end)}|{digest}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def make_chunk(source: str, doc_id: str, start: int, end: int,
               text: str, page: int = 0) -> dict:
    """把一个带坐标的原文片段整理成索引行；text 只在灌库时用，不落库。"""
    body = str(text or "")
    return {
        "chunk_key": chunk_key(source, doc_id, start, end, body),
        "source": source,
        "doc_id": str(doc_id or ""),
        "start_pos": int(start or 0),
        "end_pos": int(end or 0),
        "page": int(page or 0),
        "text_hash": hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()[:16],
        "text": body,
    }


def ensure_schema() -> None:
    """幂等建表；每个企业库每个进程只做一次，老库不重建、不动原有数据。"""
    key = str(store.db_path().resolve())
    if key in _SCHEMA_READY:
        return
    with _LOCK:
        if key in _SCHEMA_READY:
            return
        with store.conn() as c:
            c.executescript(DDL)
        _SCHEMA_READY.add(key)


def reset_cache() -> None:
    """切换企业 / 隔离测试时主动忘记建表缓存。"""
    with _LOCK:
        _SCHEMA_READY.clear()


def missing(chunks: list[dict], model_tag: str, dim: int) -> list[dict]:
    """返回还没有「当前模型 + 维度」向量的片段；换模型即视为全部缺失。"""
    ensure_schema()
    if not chunks:
        return []
    seen: set[str] = set()
    keys = [str(x["chunk_key"]) for x in chunks]
    with store.conn() as c:
        for i in range(0, len(keys), 400):
            part = keys[i:i + 400]
            if not part:
                continue
            marks = ",".join("?" for _ in part)
            sql = (f"SELECT chunk_key FROM {TABLE} "
                   f"WHERE model_tag=? AND dim=? AND chunk_key IN ({marks})")
            for r in c.execute(sql, (model_tag, int(dim), *part)):
                seen.add(str(r["chunk_key"]))
    return [x for x in chunks if x["chunk_key"] not in seen]


def upsert(chunks: list[dict], vectors: list[list[float]],
           model_tag: str, dim: int) -> int:
    """写入向量；只写坐标与二进制，不写正文。"""
    ensure_schema()
    rows: list[tuple] = []
    for ch, vec in zip(chunks, vectors):
        if not vec or len(vec) != int(dim):
            continue
        blob = struct.pack(f"<{len(vec)}f", *vec)
        rows.append((ch["chunk_key"], ch.get("source", "kb"), ch.get("doc_id", ""),
                     int(ch.get("start_pos") or 0), int(ch.get("end_pos") or 0),
                     int(ch.get("page") or 0), ch.get("text_hash", ""), blob,
                     model_tag, int(dim), store.now()))
    if not rows:
        return 0
    with store.conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO {TABLE}(chunk_key,source,doc_id,start_pos,"
            "end_pos,page,text_hash,emb,model_tag,dim,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _from_blob(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob)) if n else []


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / ((na * nb) ** 0.5) if na and nb else 0.0


def search(query_vec: list[float], *, model_tag: str, dim: int,
           keys: set[str] | None = None, top_k: int = 8) -> list[dict[str, Any]]:
    """按余弦取最相近的片段；keys 是本次允许参与召回的白名单。

    返回 ``[{chunk_key, source, doc_id, start_pos, end_pos, page, score}]``，
    只包含坐标与分数，**不含正文**，调用方必须回原文档取原文再注入提示词。
    """
    ensure_schema()
    if not query_vec:
        return []
    if keys is not None and not keys:
        return []
    out: list[dict[str, Any]] = []
    key_list = sorted(keys) if keys is not None else []
    with store.conn() as c:
        batches: list[list[str]] = ([key_list[i:i + 400]
                                    for i in range(0, len(key_list), 400)]
                                   if key_list else [[]])
        for part in batches:
            if part:
                marks = ",".join("?" for _ in part)
                sql = (f"SELECT chunk_key,source,doc_id,start_pos,end_pos,page,emb "
                       f"FROM {TABLE} WHERE model_tag=? AND dim=? "
                       f"AND chunk_key IN ({marks})")
                rows = c.execute(sql, (model_tag, int(dim), *part)).fetchall()
            else:
                sql = (f"SELECT chunk_key,source,doc_id,start_pos,end_pos,page,emb "
                       f"FROM {TABLE} WHERE model_tag=? AND dim=?")
                rows = c.execute(sql, (model_tag, int(dim))).fetchall()
            for r in rows:
                score = _cosine(query_vec, _from_blob(r["emb"]))
                if score <= 0:
                    continue
                out.append({
                    "chunk_key": str(r["chunk_key"]), "source": str(r["source"] or ""),
                    "doc_id": str(r["doc_id"] or ""),
                    "start_pos": int(r["start_pos"] or 0),
                    "end_pos": int(r["end_pos"] or 0),
                    "page": int(r["page"] or 0), "score": round(score, 4),
                })
    out.sort(key=lambda x: (-x["score"], x["chunk_key"]))
    return out[:max(1, int(top_k))]


def stats(model_tag: str = "", dim: int = 0) -> dict:
    """设置页显示的覆盖率；不返回任何向量或正文，表不存在时不建表。"""
    with store.conn() as c:
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone()
        if not exists:
            return {"total": 0, "current_model": 0, "by_source": []}
        total = int(c.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
        if model_tag:
            hit = int(c.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE model_tag=? AND dim=?",
                (model_tag, int(dim))).fetchone()[0])
            by_source = [dict(r) for r in c.execute(
                f"SELECT source,COUNT(*) AS n FROM {TABLE} "
                "WHERE model_tag=? AND dim=? GROUP BY source",
                (model_tag, int(dim)))]
        else:
            hit, by_source = total, []
    return {"total": total, "current_model": hit, "by_source": by_source}


def invalidate(model_tag: str = "", dim: int = 0) -> int:
    """换模型 / 换维度 / 关闭开关时清空向量，下次检索懒补重建。"""
    ensure_schema()
    with store.conn() as c:
        if model_tag:
            cur = c.execute(
                f"DELETE FROM {TABLE} WHERE NOT (model_tag=? AND dim=?)",
                (model_tag, int(dim)))
        else:
            cur = c.execute(f"DELETE FROM {TABLE}")
        return int(cur.rowcount or 0)
