"""向量检索编排层：按企业开关、灌库、语义召回、失败降级。

只复用 basecore/services/emb.py 的纯函数（preset / EmbedConfig / embed /
rrf_fuse），**不调用**它的 get_config() / save_config() —— 那两个读全局
basecore.db 的 settings 表，会把各企业的密钥与配置混在一起，破坏主应用的
物理分库隔离。本模块的配置走 scope.config_path()，与业务库同一企业上下文。

硬边界：
- 默认 enabled=false，demo 模式恒禁用；关闭、未配置、失败、额度用尽一律
  返回 None，由调用方退回纯词法，**绝不把异常抛进生成链路**。
- 向量只影响召回顺序；返回的是坐标与分数，正文始终由调用方从原文回取。
- 单次检索最多补 MAX_INDEX_PER_QUERY 个片段，避免首次全量灌库卡住写标书。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from . import config, ctxpool, guard, scope, store, vector_store
from .basecore.services import emb as _emb

CONFIG_FILE = "embedding.json"
MAX_INDEX_PER_QUERY = 256
MIN_SCORE = 0.35
CALL_TIMEOUT_SEC = 90

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "provider": "dashscope",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "model": "text-embedding-v3",
    "dimensions": 1024,
    "min_score": MIN_SCORE,
    "kb_enabled": True,          # 企业素材库 / 项目资料召回
    "tender_enabled": True,      # 几十万字招标原文召回
}
EDITABLE = ("enabled", "provider", "base_url", "api_key", "model",
            "dimensions", "min_score", "kb_enabled", "tender_enabled")


def _path(*, write: bool = False):
    return scope.config_path(CONFIG_FILE, write=write)


def load() -> dict:
    """读取本企业配置；文件不存在 / 损坏时一律回落默认值。"""
    cfg = dict(DEFAULTS)
    try:
        p = _path()
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in EDITABLE:
                    if k in raw:
                        cfg[k] = raw[k]
    except (OSError, ValueError, TypeError):
        pass
    cfg["dimensions"] = int(cfg.get("dimensions") or 1024)
    try:
        cfg["min_score"] = float(cfg.get("min_score") or MIN_SCORE)
    except (TypeError, ValueError):
        cfg["min_score"] = MIN_SCORE
    cfg["enabled"] = bool(cfg.get("enabled"))
    return cfg


def public() -> dict:
    """接口返回一律脱敏：密钥只给「有没有填」和尾四位，不回明文。"""
    cfg = load()
    key = str(cfg.get("api_key") or "")
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    out["api_key_configured"] = bool(key)
    out["api_key_masked"] = ("*" * 8 + key[-4:]) if len(key) > 4 else ("*" * 8 if key else "")
    out["provider_label"] = str(_emb.preset(str(cfg.get("provider") or "")).get("label") or "自定义")
    out["supported_dims"] = list(_emb.preset(str(cfg.get("provider") or "")).get("dims") or [])
    return out


def save(patch: dict) -> dict:
    """保存配置；模型 / 维度 / 平台一变，旧向量全部失效，下次懒补重建。"""
    old = load()
    cfg = dict(old)
    for k in EDITABLE:
        if k not in patch:
            continue
        v = patch[k]
        if k in ("enabled", "kb_enabled", "tender_enabled"):
            cfg[k] = bool(v)
        elif k in ("dimensions",):
            cfg[k] = int(v or 1024)
        elif k == "min_score":
            cfg[k] = float(v or MIN_SCORE)
        else:
            cfg[k] = str(v or "").strip()
    # 密钥留空 = 沿用已保存的密钥，避免界面回填脱敏值把密钥冲掉
    if "api_key" in patch and not str(patch["api_key"] or "").strip():
        cfg["api_key"] = old["api_key"]
    p = _path(write=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tag_before = model_tag(old)
    dim_before = int(old["dimensions"])
    tag_after = model_tag(cfg)
    dim_after = int(cfg["dimensions"])
    if (tag_before, dim_before) != (tag_after, dim_after):
        vector_store.invalidate(tag_after, dim_after)
    # 关闭开关不清空向量：留着缓存下次开启即可复用，也不会产生任何写操作
    return public()


def model_tag(cfg: dict | None = None) -> str:
    c = cfg or load()
    return f"{c.get('provider') or 'custom'}:{c.get('model') or ''}:{int(c.get('dimensions') or 1024)}"


def reason() -> str:
    """给设置页看的「为什么现在是关的」，不泄露密钥。"""
    cfg = load()
    if not cfg["enabled"]:
        return "开关已关闭：当前为纯词法检索，零费用"
    if config.MODE != "live":
        return "演示模式不调用外部 embedding，已自动降级为纯词法"
    if not config.outbound_allowed():
        return "数据出域未确认，已自动降级为纯词法"
    if not str(cfg.get("api_key") or "").strip():
        return "尚未填写 API Key，已自动降级为纯词法"
    try:
        if guard.budget_exceeded():
            return "当日用量预算已用尽，已自动降级为纯词法"
    except Exception:
        pass
    return ""


def ready(source: str = "") -> bool:
    cfg = load()
    if not cfg["enabled"] or bool(reason()):
        return False
    if source == "kb" and not cfg["kb_enabled"]:
        return False
    if source == "tender" and not cfg["tender_enabled"]:
        return False
    if not str(cfg.get("base_url") or "").strip() or not str(cfg.get("model") or "").strip():
        return False
    return True


def _cfg_obj(cfg: dict) -> Any:
    return _emb.EmbedConfig(
        base_url=str(cfg.get("base_url") or ""),
        api_key=str(cfg.get("api_key") or ""),
        model=str(cfg.get("model") or ""),
        enabled=True,
        provider=str(cfg.get("provider") or ""),
        dimensions=int(cfg.get("dimensions") or 1024),
    )


def _embed_sync(cfg_obj: Any, texts: list[str]) -> list[list[float]]:
    """emb.embed 是 async；主链路在同步线程里，用独立线程跑一次事件循环。

    不复用全局 loop，避免在 uvicorn 或后台任务里撞上「已在运行的事件循环」。
    """
    def job():
        return asyncio.run(_emb.embed(cfg_obj, texts))

    return ctxpool.run_sync(job, timeout=CALL_TIMEOUT_SEC)


def _audit(pid: str, ok: bool, n_chunks: int, elapsed_ms: int, note: str) -> None:
    try:
        store.add_usage(pid, actor="emb_bridge", task="embedding", tool="bailian.embeddings",
                        model="百炼向量检索" if ok else "已降级（纯词法）",
                        tokens_in=int(n_chunks * 160), tokens_out=0,
                        elapsed_ms=elapsed_ms, ok=ok,
                        input_text=f"chunks={n_chunks}", output_text=note[:160])
    except Exception:
        pass


def test_connection() -> dict:
    """设置页「测通」：只发一条极短文本，不触碰任何业务资料。"""
    cfg = load()
    if not str(cfg.get("api_key") or "").strip():
        return {"ok": False, "error": "请先填写 API Key"}
    t0 = time.time()
    try:
        guard.require_outbound("", "embedding.test")
        vecs = _embed_sync(_cfg_obj(cfg), ["连接测试"])
        dim = len(vecs[0]) if vecs and vecs[0] else 0
        return {"ok": bool(dim), "dim": dim,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "" if dim else "接口返回空向量"}
    except guard.OutboundBlocked as exc:
        return {"ok": False, "dim": 0, "latency_ms": 0, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "dim": 0, "latency_ms": int((time.time() - t0) * 1000),
                "error": str(exc)[:300]}


def rank_chunks(source: str, chunks: list[dict], query: str, *,
                pid: str = "", top_k: int = 12) -> list[dict] | None:
    """给一批带坐标的原文片段算语义排名；失败 / 关闭一律返回 None。

    chunks 每项至少含 chunk_key / text（make_chunk 产出）。返回按相似度降序的
    ``[{chunk_key, score, ...坐标}]``，供调用方与词法名次做 RRF 融合。
    """
    if not ready(source) or not chunks or not str(query or "").strip():
        return None
    cfg = load()
    tag = model_tag(cfg)
    dim = int(cfg["dimensions"])
    t0 = time.time()
    try:
        vector_store.ensure_schema()
        todo = vector_store.missing(chunks, tag, dim)[:MAX_INDEX_PER_QUERY]
        if todo:
            # 片段最长 320 字（知识库）/ 480 字（招标原文），远低于 8192 token 上限
            vecs = _embed_sync(_cfg_obj(cfg), [str(x.get("text") or "")[:2000] for x in todo])
            if len(vecs) == len(todo):
                vector_store.upsert(todo, vecs, tag, dim)
        qvec = _embed_sync(_cfg_obj(cfg), [str(query)[:2000]])[0]
        rows = vector_store.search(
            qvec, model_tag=tag, dim=dim,
            keys={str(x["chunk_key"]) for x in chunks}, top_k=top_k)
        rows = [r for r in rows if float(r["score"]) >= float(cfg["min_score"])]
        _audit(pid, True, len(todo), int((time.time() - t0) * 1000),
               f"source={source} hits={len(rows)}")
        return rows
    except Exception as exc:
        # 401 / 404 / 超时 / 额度 / 维度不匹配：一律静默退回纯词法
        _audit(pid, False, 0, int((time.time() - t0) * 1000),
               f"source={source} fallback={type(exc).__name__}: {str(exc)[:100]}")
        return None


def fuse(rankings: list[list[dict]], *, top_k: int = 8) -> list[dict]:
    """用 emb.rrf_fuse 按 chunk_key 融合多路名次；只用名次，不用分数。"""
    return _emb.rrf_fuse(rankings, key="chunk_key", top_k=top_k)


def chunks_from_documents(docs: list[Any]) -> list[dict]:
    """LangChain Document → 索引片段；只收坐标，正文不落库。"""
    out: list[dict] = []
    for d in docs:
        m = getattr(d, "metadata", {}) or {}
        out.append(vector_store.make_chunk(
            "kb", str(m.get("doc_id") or m.get("id") or ""),
            int(m.get("start") or 0), int(m.get("end") or 0),
            str(getattr(d, "page_content", "") or ""), int(m.get("page") or 0)))
    return out


def chunks_from_clauses(clauses: list[dict], pid: str) -> list[dict]:
    """招标原文切片 → 索引片段；同一项目内按 (start,end,正文指纹) 去重。"""
    return [vector_store.make_chunk(
        "tender", f"tender:{pid}", int(c.get("start") or 0), int(c.get("end") or 0),
        str(c.get("text") or ""), int(c.get("page") or 0)) for c in clauses]


def status() -> dict:
    cfg = load()
    tag = model_tag(cfg)
    dim = int(cfg["dimensions"])
    try:
        coverage = vector_store.stats(tag, dim)
    except Exception:
        coverage = {"total": 0, "current_model": 0, "by_source": []}
    return {**public(), "ready": ready(), "reason": reason(),
            "mode": config.MODE, "model_tag": tag, "coverage": coverage,
            # 平台预设给前端下拉用：换平台要能同时改地址、模型名和维度，
            # 只让用户填 Key 等于把「接哪家」写死在默认值里。
            "providers": [
                {"id": p.get("id", ""), "label": p.get("label", ""),
                 "base_url": p.get("base_url", ""), "model": p.get("model", ""),
                 "dims": list(p.get("dims") or []),
                 "default_dim": int(p.get("default_dim") or 1024),
                 "hint": p.get("hint", "")}
                for p in _emb.PROVIDERS
            ]}
