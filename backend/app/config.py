"""配置：多模型注册表 + 运行模式。

- 环境变量只作为首次启动的默认来源，之后模型统一存在 data/models.json（本地，不外发）。
- 可以同时接入多个 OpenAI 兼容模型；界面切换当前模型，也可以按环节
  （规划 / 写作 / 复核 / 自由回答）分别指定模型，留空则跟随当前模型。
- 密钥只写本地文件，API 返回一律脱敏。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    """先加载 .env，再解析任何路径；真实环境变量优先，不覆盖。

    查找顺序：$BIDAGENT_ENV_FILE → 项目根/.env → backend/.env，第一个存在的生效。
    以前这函数写在 DATA_DIR 之后调用，.env 里配的 BIDAGENT_DATA 等于没生效。
    """
    explicit = os.environ.get("BIDAGENT_ENV_FILE", "").strip()
    candidates: list[Path] = [Path(explicit).expanduser()] if explicit else []
    candidates += [ROOT / ".env", ROOT / "backend" / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        break


_load_env()  # 必须在 DATA_DIR / DB_PATH 解析之前


def _resolve_data_dir() -> Path:
    """数据目录：$BIDAGENT_DATA 优先，否则用项目内 data/。

    与基座（basecore.config）保持同一套解析规则 —— 相对路径按项目根解析，
    这样本机、公司服务器、容器里改一个环境变量就能把数据挪走，不会两处各写一份。
    """
    raw = os.environ.get("BIDAGENT_DATA", "").strip()
    if not raw:
        return ROOT / "data"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


DATA_DIR = _resolve_data_dir()
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "app.db"
# C02：以下常量是「平台默认企业」的旧位置，保留给启动自检和旧数据兼容；
# 运行时读写统一走 scope.config_path()，按当前企业落盘，密钥不跨企业继承。
MODELS_FILE = DATA_DIR / "models.json"

ROLES = ("plan", "write", "review", "free")
ROLE_LABELS = {"plan": "智能体规划", "write": "写标书 / 改写",
               "review": "验标书 / 复核", "free": "对话回答"}

# 常见 OpenAI 兼容服务，仅作为预填值，用户可随时改。
# 顺序即前端下拉顺序：先把国内可直连的常用服务排在前面，境外 / 自定义放最后。
# 模型名会随服务商迭代（尤其带日期的），预填值过期时用户在界面上直接改即可。
PRESETS = [
    {"provider": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat"},
    {"provider": "dashscope", "label": "阿里云百炼（通义）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"provider": "siliconflow", "label": "硅基流动（聚合多模型）",
     "base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V3"},
    {"provider": "volcengine", "label": "火山方舟（豆包）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-2-1-pro-260915"},
    {"provider": "qianfan", "label": "百度千帆（文心）",
     "base_url": "https://qianfan.baidubce.com/v2", "model": "ernie-4.0-turbo-8k"},
    {"provider": "zhipu", "label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-plus"},
    {"provider": "moonshot", "label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k"},
    {"provider": "minimax", "label": "MiniMax", "base_url": "https://api.minimaxi.com/v1",
     "model": "MiniMax-M3"},
    {"provider": "stepfun", "label": "阶跃星辰 StepFun", "base_url": "https://api.stepfun.com/v1",
     "model": "step-3.7-flash"},
    {"provider": "hunyuan", "label": "腾讯混元",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "model": "hunyuan-turbo"},
    {"provider": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini"},
    # Codex 系列只有 /responses 端点，塞进 /chat/completions 会直接 404。
    # 地址里带 /responses 是给后端的明确信号，选它就会自动切协议。
    {"provider": "codex", "label": "OpenAI Codex（Responses 协议）",
     "base_url": "https://api.openai.com/v1/responses", "model": "gpt-5-codex"},
    {"provider": "gemini", "label": "Google Gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "model": "gemini-3-flash"},
    {"provider": "custom", "label": "自定义 / 本地服务", "base_url": "", "model": ""},
]


DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 4096
    timeout: int = 120
    id: str = ""
    label: str = ""
    # 接口协议：chat（默认）/ responses。Codex 系列只提供 responses。
    # 留空时由 _wire_api 按地址和模型名自动判断，前端不必额外传。
    wire_api: str = ""
    # 思考型模型的关闭参数（如 {"enable_thinking": False}），作为请求 body 顶层字段透传
    extra_body: dict | None = None

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def _int(value, default: int, low: int = 1, high: int = 600) -> int:
    try:
        return max(low, min(high, int(value)))
    except (ValueError, TypeError):
        return default


_CODEX_HINT = re.compile(r"codex", re.I)


def _wire_api(base_url: str, model: str, explicit: str = "") -> str:
    """判定接口协议：chat（/chat/completions）或 responses（/responses）。

    Codex 系列模型只提供 Responses，强行发到 chat/completions 会 404。
    前端预设只回填地址和模型名，所以这里按「地址带 /responses」和
    「模型名含 codex」两个信号自动判定，不依赖界面多传字段。
    """
    want = (explicit or "").strip().lower()
    if want in ("chat", "responses"):
        return want
    if "responses" in (base_url or "").lower():
        return "responses"
    if _CODEX_HINT.search(model or ""):
        return "responses"
    return "chat"


def _env_config() -> LLMConfig:
    raw = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    base = raw
    for tail in ("/chat/completions", "/responses"):
        if base.endswith(tail):
            base = base[: -len(tail)].rstrip("/")
    model = os.getenv("LLM_MODEL", "").strip()
    return LLMConfig(
        base_url=base,
        api_key=os.getenv("LLM_API_KEY", "").strip(),
        model=model,
        max_tokens=_int(os.getenv("LLM_MAX_TOKENS", "4096"), 4096, 256, 32768),
        timeout=_int(os.getenv("LLM_TIMEOUT", "120"), 120, 5, 300),
        id="env", label="环境变量默认模型",
        wire_api=_wire_api(raw, model, os.getenv("LLM_WIRE_API", "")),
    )


def _blank_registry() -> dict:
    # C02：环境变量里的平台级密钥只属于默认企业，不静默注入其他企业
    from . import scope
    if not scope.is_platform():
        return {"active": "", "roles": {r: "" for r in ROLES}, "models": []}
    env = _env_config()
    models = []
    if env.ready:
        models.append({
            "id": "env", "label": env.label, "provider": "env",
            "base_url": env.base_url, "api_key": env.api_key, "model": env.model,
            "max_tokens": env.max_tokens, "timeout": env.timeout, "source": "env",
        })
    return {"active": "env" if models else "", "roles": {r: "" for r in ROLES},
            "models": models}


def _read_registry() -> dict:
    from . import scope
    f = scope.config_path("models.json")
    if not f.exists():
        reg = _blank_registry()
        _write_registry(reg)
        return reg
    try:
        reg = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(reg, dict):
            raise ValueError
    except (ValueError, OSError, UnicodeError):
        return _blank_registry()
    reg.setdefault("active", "")
    reg.setdefault("roles", {})
    reg.setdefault("models", [])
    return reg


def _write_registry(reg: dict) -> None:
    from . import scope
    f = scope.config_path("models.json", write=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def _find(reg: dict, mid: str) -> dict | None:
    return next((m for m in reg["models"] if m.get("id") == mid), None)


def _to_config(m: dict | None) -> LLMConfig:
    if not m:
        return LLMConfig("", "", "", id="", label="")
    raw = str(m.get("base_url") or "").strip().rstrip("/")
    model = str(m.get("model") or "").strip()
    base = raw
    for tail in ("/chat/completions", "/responses"):
        if base.endswith(tail):
            base = base[: -len(tail)].rstrip("/")
    return LLMConfig(
        base_url=base,
        api_key=str(m.get("api_key") or "").strip(),
        model=model,
        max_tokens=_int(m.get("max_tokens"), 4096, 256, 32768),
        timeout=_int(m.get("timeout"), 120, 5, 300),
        id=str(m.get("id") or ""), label=str(m.get("label") or ""),
        wire_api=_wire_api(raw, model, m.get("wire_api")),
        extra_body=m.get("extra_body") if isinstance(m.get("extra_body"), dict) else None,
    )


def _public(m: dict) -> dict:
    """脱敏后的模型信息，密钥不出后端。"""
    key = str(m.get("api_key") or "")
    return {
        "id": m.get("id", ""), "label": m.get("label", ""),
        "provider": m.get("provider", "custom"), "base_url": m.get("base_url", ""),
        "model": m.get("model", ""), "max_tokens": m.get("max_tokens", 4096),
        "timeout": m.get("timeout", 120), "has_key": bool(key),
        "key_hint": ("*" * 4 + key[-4:]) if len(key) > 4 else ("已配置" if key else "未填写"),
        "ready": bool(m.get("base_url") and key and m.get("model")),
        "source": m.get("source", "user"),
        "wire_api": _wire_api(str(m.get("base_url") or ""), str(m.get("model") or ""),
                              m.get("wire_api")),
        "extra_body": m.get("extra_body") if isinstance(m.get("extra_body"), dict) else {},
    }


def registry_public() -> dict:
    reg = _read_registry()
    ids = {m.get("id") for m in reg["models"]}
    active = reg["active"] if reg["active"] in ids else ""
    return {
        "active": active,
        "roles": {r: (reg["roles"].get(r) or "") for r in ROLES},
        "role_labels": ROLE_LABELS,
        "models": [_public(m) for m in reg["models"]],
        "presets": PRESETS,
    }


def llm_config(role: str = "") -> LLMConfig:
    """当前生效的模型。role 有单独指定时优先用那一个，否则用全局当前模型。

    保持旧调用无参兼容；注册表为空时回落到环境变量。
    """
    reg = _read_registry()
    ids = {m.get("id") for m in reg["models"]}
    mid = ""
    if role in ROLES:
        bound = reg["roles"].get(role) or ""
        mid = bound if bound in ids else ""
    if not mid and reg["active"] in ids:
        mid = reg["active"]
    m = _find(reg, mid)
    if m:
        cfg = _to_config(m)
        if cfg.ready:
            return cfg
    # 注册表是唯一来源：删掉的模型不会再从环境变量偷偷回来
    return LLMConfig("", "", "")


def resolve(role: str = "") -> tuple[str, LLMConfig]:
    """返回（实际用的模型 id, 配置），供留痕显示。"""
    reg = _read_registry()
    ids = {m.get("id") for m in reg["models"]}
    mid = (reg["roles"].get(role) or "") if role in ROLES else ""
    if mid not in ids:
        mid = reg["active"] if reg["active"] in ids else ""
    cfg = _to_config(_find(reg, mid))
    return mid, cfg


def upsert_model(data: dict) -> dict:
    reg = _read_registry()
    mid = str(data.get("id") or "").strip()
    old = _find(reg, mid) if mid else None
    label = str(data.get("label") or "").strip() or str(data.get("model") or "未命名模型")
    key = str(data.get("api_key") or "").strip()
    if old and not key:
        key = str(old.get("api_key") or "")  # 编辑时不重复填密钥
    xb = data.get("extra_body")
    if not isinstance(xb, dict):
        xb = (old or {}).get("extra_body") or {}
    item = {
        "id": mid or uuid.uuid4().hex[:10],
        "label": label[:40],
        "provider": str(data.get("provider") or "custom")[:30],
        "base_url": str(data.get("base_url") or "").strip().rstrip("/"),
        "api_key": key,
        "model": str(data.get("model") or "").strip(),
        "max_tokens": _int(data.get("max_tokens"), 4096, 256, 32768),
        "timeout": _int(data.get("timeout"), 120, 5, 300),
        "source": "user",
        "extra_body": xb,
    }
    item["wire_api"] = _wire_api(
        item["base_url"], item["model"],
        data.get("wire_api") or (old or {}).get("wire_api", ""),
    )
    if not item["base_url"] or not item["model"] or not item["api_key"]:
        raise ValueError("地址、模型名、密钥都要填")
    if old:
        old.update(item)
    else:
        reg["models"].append(item)
        if not reg["active"]:
            reg["active"] = item["id"]
    _write_registry(reg)
    return {"ok": True, "id": item["id"]}


def delete_model(mid: str) -> dict:
    reg = _read_registry()
    if not _find(reg, mid):
        raise ValueError("模型不存在")
    reg["models"] = [m for m in reg["models"] if m.get("id") != mid]
    if reg["active"] == mid:
        reg["active"] = reg["models"][0]["id"] if reg["models"] else ""
    for r in ROLES:
        if reg["roles"].get(r) == mid:
            reg["roles"][r] = ""
    _write_registry(reg)
    return {"ok": True, "active": reg["active"]}


def set_active(mid: str) -> dict:
    reg = _read_registry()
    if mid and not _find(reg, mid):
        raise ValueError("模型不存在")
    reg["active"] = mid
    _write_registry(reg)
    return {"ok": True, "active": mid}


def set_role(role: str, mid: str) -> dict:
    if role not in ROLES:
        raise ValueError("未知环节")
    reg = _read_registry()
    if mid and not _find(reg, mid):
        raise ValueError("模型不存在")
    reg["roles"][role] = mid
    _write_registry(reg)
    return {"ok": True, "role": role, "id": mid}


def test_model(mid: str) -> dict:
    """用一条极短请求验证连通性；只返回耗时与错误摘要，不返回密钥。

    虽不含标书正文，仍是外部请求，必须受数据出域开关约束（main 层还会过
    guard.require_outbound 记审计；这里保留防御，避免被其它入口绕过）。
    """
    if not outbound_allowed():
        return {"ok": False, "latency_ms": 0,
                "error": "数据出域未确认或已关闭，请先到「数据出域」里确认后再测试"}
    reg = _read_registry()
    m = _find(reg, mid)
    if not m:
        raise ValueError("模型不存在")
    cfg = _to_config(m)
    if not cfg.ready:
        raise ValueError("该模型配置不完整")
    payload = json.dumps({
        "model": cfg.model,
        "messages": [{"role": "user", "content": "只回复 OK"}],
        "max_tokens": 200, "stream": False, "temperature": 0,
        **(cfg.extra_body or {}),
    }, ensure_ascii=False).encode("utf-8")
    api = _wire_api(cfg.base_url, cfg.model, cfg.wire_api)
    if api == "responses":
        # Codex 只认 Responses：system 走 instructions、对话走 input、上限叫 max_output_tokens，
        # 且不接受自定义 temperature，所以这条分支单独构造。
        payload = json.dumps({
            "model": cfg.model,
            "input": [{"role": "user",
                       "content": [{"type": "input_text", "text": "只回复 OK"}]}],
            "max_output_tokens": 200, "stream": False,
            **(cfg.extra_body or {}),
        }, ensure_ascii=False).encode("utf-8")
        url = cfg.base_url.rstrip("/") + "/responses"
    else:
        url = cfg.base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg.api_key},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", errors="ignore"))
        reply, reasoning = "", ""
        if api == "responses":
            for item in body.get("output") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "reasoning":
                    reasoning = "1"
                elif item.get("type") == "message":
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                            reply += str(part.get("text") or "")
            reply = reply.strip()[:40]
        else:
            try:
                msg = body["choices"][0]["message"]
                reply = str(msg.get("content") or "")[:40]
                reasoning = str(msg.get("reasoning_content") or "")
            except (KeyError, IndexError, TypeError, AttributeError):
                pass
        # 思考型模型把 token 全花在推理上时会返回 200 但正文为空；如实标注，不假装通过
        thinking_only = not reply and bool(reasoning)
        if thinking_only:
            reply = "（思考型模型：token 全用于推理，正文为空，请关闭思考或调大上限）"
        if mid == reg.get("active"):
            clear_model_error()
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "reply": reply, "thinking_only": thinking_only}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:160]
        err = f"HTTP {exc.code}: {detail}"
        if mid == reg.get("active"):
            record_model_error(err, source="model.test")
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": err}
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
        if mid == reg.get("active"):
            record_model_error(err, source="model.test")
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": err}


# ---------------- 真实模型健康状态 ----------------
# 「配置齐全」不等于「模型可用」：余额不足 / 密钥失效 / 网络不通时，界面必须
# 如实显示，不能让 live 模式假装跑在真实模型上、静默把结果降级成本地模板。
MODEL_HEALTH_FILE = DATA_DIR / "model_health.json"


def model_health() -> dict:
    from . import scope
    f = scope.config_path("model_health.json")
    try:
        h = json.loads(f.read_text(encoding="utf-8"))
        return h if isinstance(h, dict) else {"status": "unknown"}
    except (OSError, ValueError):
        return {"status": "unknown"}


def record_model_error(exc: BaseException | str, *, source: str = "") -> None:
    msg = " ".join(str(exc).split())[:200]
    low = msg.lower()
    if "402" in msg or "insufficient" in low:
        kind, hint = "账户余额不足", "请到模型服务商充值，或在「管理多个模型」里换一个可用账号"
    elif "401" in msg or "403" in msg or "api key" in low:
        kind, hint = "密钥或权限错误", "请检查 API Key 与模型权限"
    elif "timeout" in low or "timed out" in low:
        kind, hint = "模型响应超时", "稍后重试或换一个更快的模型"
    elif "llmerror" in low or "connect" in low or "network" in low:
        kind, hint = "模型服务不可达", "检查网络与接口地址"
    else:
        kind, hint = "模型调用失败", "可在「管理多个模型」里测试连通性"
    data = {"status": "error", "kind": kind, "hint": hint, "source": source,
            "detail": msg, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        from . import scope
        f = scope.config_path("model_health.json", write=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clear_model_error() -> None:
    try:
        from . import scope
        f = scope.config_path("model_health.json", write=True)
        f.write_text(json.dumps(
            {"status": "ok", "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------- 数据出域控制 ----------------
# 方案原本靠「物理断网」一票解决；改为外部模型后，必须由软件显式接管：
# 首次告知 → 用户确认 → 可随时关闭 → 关闭后所有模型调用自动降级为本地。
PRIVACY_FILE = DATA_DIR / "privacy.json"
_PRIVACY_DEFAULT = {
    "acknowledged": False,
    "allow_outbound": True,
    "scrub_sensitive": True,
    "max_chars": 12000,
    "updated_at": "",
}


def privacy() -> dict:
    from . import scope
    d = dict(_PRIVACY_DEFAULT)
    f = scope.config_path("privacy.json")
    if f.exists():
        try:
            d.update(json.loads(f.read_text(encoding="utf-8")) or {})
        except (ValueError, OSError):
            pass
    return d


def set_privacy(data: dict) -> dict:
    d = privacy()
    for key in ("acknowledged", "allow_outbound", "scrub_sensitive"):
        if key in data:
            d[key] = bool(data[key])
    if "max_chars" in data:
        try:
            d["max_chars"] = max(500, min(int(data["max_chars"]), 60000))
        except (ValueError, TypeError):
            pass
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    from . import scope
    f = scope.config_path("privacy.json", write=True)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def budget() -> dict:
    from . import scope
    d = dict(_BUDGET_DEFAULT)
    f = scope.config_path("budget.json")
    if f.exists():
        try:
            d.update(json.loads(f.read_text(encoding="utf-8")) or {})
        except (ValueError, OSError):
            pass
    return d


def set_budget(data: dict) -> dict:
    d = budget()
    if "enabled" in data:
        d["enabled"] = bool(data["enabled"])
    if "daily_token_limit" in data:
        try:
            d["daily_token_limit"] = max(1000, min(int(data["daily_token_limit"]), 10000000))
        except (ValueError, TypeError):
            pass
    if "per_task_token_limit" in data:
        try:
            d["per_task_token_limit"] = max(1000, min(int(data["per_task_token_limit"]), 2000000))
        except (ValueError, TypeError):
            pass
    from . import scope
    f = scope.config_path("budget.json", write=True)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def outbound_allowed() -> bool:
    p = privacy()
    return bool(p["acknowledged"]) and bool(p["allow_outbound"])


BUDGET_FILE = DATA_DIR / "budget.json"
# 整本生成的真实用量：每章按章检索的招标原文 + 资料 + 知识库 + 范例约 1 万 tokens；
# 长章分段续写后单章会调用 2—6 次，7~10 章整本约 25—40 万 tokens，再加定向重写。
# 这里按「一次整本生成」给额度，日预算仍是最终兜底；企业可在设置页单独收紧。
_BUDGET_DEFAULT = {"daily_token_limit": 1500000, "per_task_token_limit": 600000,
                   "enabled": True}


# 运行模式：demo = 内置演示模型；live = 外部 OpenAI 兼容模型。
# C02：按企业读；其他企业没有自己的 mode.txt 时继承平台默认，保存只写本企业。
# 用模块级 __getattr__ 让既有 config.MODE 调用点自动变成「当前企业的模式」。
_MODE_FILE = DATA_DIR / "mode.txt"       # 平台默认（旧数据位置，启动自检也用）


def mode() -> str:
    from . import scope
    f = scope.config_path("mode.txt")
    try:
        v = f.read_text(encoding="utf-8", errors="ignore").strip()
        return v if v in ("demo", "live") else "demo"
    except OSError:
        return "demo"


def set_mode(value: str) -> None:
    from . import scope
    f = scope.config_path("mode.txt", write=True)
    f.write_text(value, encoding="utf-8")


def __getattr__(name: str):
    """兼容既有 config.MODE 读取，改成按当前企业动态解析。"""
    if name == "MODE":
        return mode()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 受保护章节：自动改写绝不触碰
PROTECTED_KEYWORDS = ("报价", "投标函", "承诺函", "法人授权", "资质证书", "保证金", "开标一览")
