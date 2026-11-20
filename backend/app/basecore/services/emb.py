"""Embedding 向量化：接入 OpenAI 兼容 /embeddings 接口（千问 / bge / OpenAI）。

设计：
- 配置存 settings["embedding"]：{provider, base_url, api_key, model, dimensions}；
  未配置时上层自动退回纯 BM25。
- 不同平台的批量上限和维度参数不一样：千问（百炼）单次最多 10 条、支持 dimensions
  降维，OpenAI 支持 dimensions，bge 系列维度固定。按 provider 查表决定分批与参数，
  不支持的参数自动去掉重试一次，避免换个平台就全线 400。
- 向量以 float32 二进制存 SQLite（kb_chunks.emb），检索时懒索引：缺向量的分块分批补齐。
- 换模型/维度会让旧向量失效（长度不同算不出余弦），保存配置时由上层清空重跑。
- 检索融合用 RRF（Reciprocal Rank Fusion），只用名次不用分值，天然免调权。
"""

from __future__ import annotations

import math
import struct

import httpx

from .. import db

DEFAULT_DIM = 1024
BATCH = 32          # 每次请求最多几条文本（默认，按 provider 覆盖）
MAX_INDEX_PER_QUERY = 256   # 单次检索最多补多少个分块的向量，防首次全量卡住

# 平台预设：前端"向量平台"下拉直接用这张表，用户只需填密钥。
# dims 为空 = 该模型维度固定，不给 dimensions 参数。
PROVIDERS: list[dict] = [
    {
        "id": "dashscope",
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "text-embedding-v3",
        "batch": 10,
        "dims": [1024, 768, 512],
        "default_dim": 1024,
        "hint": "单次最多 10 条文本；text-embedding-v3 支持 1024/768/512 维，选 1024 效果最好。",
    },
    {
        "id": "qianwen",
        "label": "千问 AI 平台（MaaS）",
        "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "model": "qwen3.7-text-embedding",
        "batch": 20,
        "dims": [1024, 768, 512],
        "default_dim": 1024,
        "hint": "实测单次最多 20 条；1024 维最稳。同平台还有 text-embedding-flash（更快、精度略低）。",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动（bge 系列）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-m3",
        "batch": 32,
        "dims": [],
        "default_dim": 1024,
        "hint": "bge-m3 固定 1024 维，中文长文本表现稳定，价格便宜。",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "text-embedding-3-small",
        "batch": 32,
        "dims": [1536, 1024, 512],
        "default_dim": 1536,
        "hint": "text-embedding-3-small 默认 1536 维，可降到 1024 省存储。",
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "batch": 16,
        "dims": [],
        "default_dim": 1024,
        "hint": "任何兼容 /v1/embeddings 的服务，地址填到 /v1 这一级。",
    },
]
PROVIDER_MAP = {p["id"]: p for p in PROVIDERS}


def preset(provider: str) -> dict:
    return PROVIDER_MAP.get(provider or "", PROVIDER_MAP["custom"])


class EmbedConfig:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        enabled: bool = False,
        provider: str = "",
        dimensions: int = 0,
    ):
        self.provider = (provider or "").strip() or _guess_provider(base_url, model)
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.dimensions = int(dimensions or 0)
        self.enabled = bool(enabled and self.base_url and self.model)

    def ready(self) -> bool:
        return self.enabled

    @property
    def batch(self) -> int:
        return int(preset(self.provider).get("batch") or BATCH)

    @property
    def supports_dims(self) -> bool:
        return bool(preset(self.provider).get("dims"))


def _guess_provider(base_url: str, model: str) -> str:
    """老配置没存 provider 时按地址/模型名反推，免去手工迁移。"""
    url = (base_url or "").lower()
    name = (model or "").lower()
    if "qianwen" in url or "maas." in url:
        return "qianwen"
    if "dashscope" in url or "aliyuncs" in url:
        return "dashscope"
    if "siliconflow" in url or name.startswith("baai/"):
        return "siliconflow"
    if "openai.com" in url:
        return "openai"
    return "custom"


def get_config() -> EmbedConfig:
    raw = db.get_setting("embedding", {}) or {}
    return EmbedConfig(**raw)


def save_config(
    base_url: str,
    api_key: str,
    model: str,
    enabled: bool,
    provider: str = "",
    dimensions: int = 0,
) -> EmbedConfig:
    cfg = EmbedConfig(
        base_url=base_url, api_key=api_key, model=model,
        enabled=enabled, provider=provider, dimensions=dimensions,
    )
    db.set_setting("embedding", {
        "provider": cfg.provider,
        "base_url": cfg.base_url, "api_key": cfg.api_key, "model": cfg.model,
        "dimensions": cfg.dimensions, "enabled": cfg.enabled,
    })
    return cfg


async def embed(cfg: EmbedConfig, texts: list[str]) -> list[list[float]]:
    """批量向量化，返回与 texts 等长的向量列表。失败抛异常，由调用方决定降级。"""
    if not cfg.ready():
        raise ValueError("embedding 未配置")
    url = cfg.base_url if cfg.base_url.endswith("/embeddings") else cfg.base_url + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    payload_base: dict = {"model": cfg.model}
    if cfg.dimensions and cfg.supports_dims:
        payload_base["dimensions"] = cfg.dimensions
    size = max(1, cfg.batch)
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        i = 0
        while i < len(texts):
            batch = texts[i : i + size]
            body = {**payload_base, "input": batch}
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code >= 400 and "dimensions" in body:
                # 有些兼容服务不认 dimensions，去掉再试一次，别让降维参数卡死整条链路
                body.pop("dimensions", None)
                resp = await client.post(url, headers=headers, json=body)
            if resp.status_code >= 400 and len(batch) > 1 and _batch_too_large(resp.text):
                # 平台批量上限各不相同（官方百炼 10、代理平台 20、有的更小），
                # 遇到超限就折半重发本批，不写死数字，也不会卡住整次索引
                size = max(1, len(batch) // 2)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(_explain(resp.status_code, resp.text, batch, cfg))
            data = resp.json()["data"]
            data.sort(key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
            i += len(batch)
    return out


def _batch_too_large(text: str) -> bool:
    low = (text or "").lower()
    return ("batch size" in low or "batch_size" in low) and (
        "invalid" in low or "larger" in low or "too" in low or "exceed" in low or "limit" in low
    )


def _explain(code: int, text: str, batch: list[str], cfg: EmbedConfig) -> str:
    """把平台原始报错翻译成人能看懂的下一步动作。"""
    raw = (text or "")[:200]
    if code in (401, 403):
        return f"密钥无效或无权限（{code}），请检查 API Key 是否属于当前平台：{raw}"
    if code == 429:
        return f"请求过于频繁或额度用尽（429），稍后重试或检查账户余额：{raw}"
    if code == 400 and _batch_too_large(text):
        return (f"平台拒绝该批量（本次 {len(batch)} 条），已自动折半仍失败，"
                f"请把每批条数调到 {max(1, len(batch) // 2)} 以下：{raw}")
    if code == 400 and "dimension" in raw.lower():
        return f"该模型不支持所选向量维度，请改用平台默认维度：{raw}"
    return f"embedding 接口返回 {code}: {raw}"


def to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def from_blob(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def rrf_fuse(rankings: list[list[dict]], *, k: int = 60, top_k: int = 8, key: str = "id") -> list[dict]:
    """多路召回的 RRF 融合：score = Σ 1/(k + rank)。保留来源名次信息。"""
    fused: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            rid = str(row.get(key) or row.get("chunk_id") or "")
            if not rid:
                continue
            item = fused.setdefault(rid, {**row, "score": 0.0, "via": []})
            item["score"] += 1.0 / (k + rank)
            item["via"].append(rank)
    out = sorted(fused.values(), key=lambda d: -d["score"])[:top_k]
    for row in out:
        row["score"] = round(row["score"], 4)
    return out
