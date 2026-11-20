"""LLM 客户端：只走用户自己配置的 OpenAI 兼容地址，不内置任何厂商端点。

设计要点（借鉴两个开源项目的容错清单，代码全重写）：
1. 统一 /chat/completions 协议，支持 stream 与非 stream；
2. JSON 输出做五层容错：去代码围栏 → 截取首尾大括号 → 去尾逗号 → 单引号转双引号 → 截断即失败；
3. 网络错误按指数退避重试，超时不重试；
4. 所有错误信息都脱敏，不打印 api_key。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx

from ..config import LLMConfig


class LLMError(RuntimeError):
    pass


def _headers(cfg: LLMConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key.strip():
        headers["Authorization"] = f"Bearer {cfg.api_key.strip()}"
    return headers


# Codex 系列（gpt-5-codex / gpt-5.x-codex / codex-mini-latest）只提供
# /v1/responses，塞进 /v1/chat/completions 会直接 404，必须单独走一条协议。
_CODEX_HINT = re.compile(r"codex", re.I)
RESPONSES = "responses"
CHAT = "chat"


def wire_api(cfg: LLMConfig) -> str:
    """实际使用的协议。

    优先级：显式配置 > 地址里带 /responses > 模型名含 codex > 默认 chat。
    放在后端推断，是因为前端预设只回填地址和模型名，不会额外带协议字段。
    """
    explicit = (getattr(cfg, "wire_api", "") or "").strip().lower()
    if explicit in (CHAT, RESPONSES):
        return explicit
    if "responses" in (cfg.base_url or "").lower():
        return RESPONSES
    if _CODEX_HINT.search(cfg.model or ""):
        return RESPONSES
    return CHAT


def _base_url(cfg: LLMConfig) -> str:
    base = cfg.base_url.strip().rstrip("/")
    for tail in ("/chat/completions", "/responses"):
        if base.endswith(tail):
            base = base[: -len(tail)].rstrip("/")
    return base


def _endpoint(cfg: LLMConfig, path: str) -> str:
    return f"{_base_url(cfg)}{path}"


def _brief(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _payload(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    stream: bool,
    max_tokens: int | None = None,
    json_mode: bool = False,
    task: str = "",
) -> dict:
    """OpenAI 兼容请求体。

    reasoning_effort 只在解析出档位时下发——不认这个参数的接口会直接 400，
    chat() 收到相关报错会自动去掉它重试。
    """
    body: dict = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
        "stream": stream,
    }
    xb = dict(getattr(cfg, "extra_body", None) or {})
    for k, v in xb.items():
        if k not in body:
            body[k] = v
    effort = _effort(cfg, task)
    # 已明确关闭思考时不再下发 reasoning_effort，避免两个开关互相打架
    if effort and xb.get("enable_thinking") is not False:
        body["reasoning_effort"] = effort
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _as_text(content) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _responses_payload(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    stream: bool,
    max_tokens: int | None = None,
    json_mode: bool = False,
    task: str = "",
) -> dict:
    """Responses API 请求体。

    与 chat/completions 的三处硬差异：
    1. system 不是一条消息，而是顶层的 instructions；
    2. 对话放在 input 数组，文本要包成 {type: input_text|output_text}；
    3. 输出上限叫 max_output_tokens。温度不下发——gpt-5/codex 系列只认默认值。
    """
    instructions: list[str] = []
    items: list[dict] = []
    for m in messages or []:
        role = (m.get("role") or "user").strip()
        text = _as_text(m.get("content"))
        if role in ("system", "developer"):
            if text.strip():
                instructions.append(text.strip())
            continue
        part_type = "output_text" if role == "assistant" else "input_text"
        items.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": [{"type": part_type, "text": text}],
        })
    if not items:
        items = [{"role": "user", "content": [{"type": "input_text", "text": ""}]}]

    body: dict = {
        "model": cfg.model,
        "input": items,
        "max_output_tokens": max_tokens or cfg.max_tokens,
        "stream": stream,
    }
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    effort = _effort(cfg, task)
    if effort:
        body["reasoning"] = {"effort": effort}
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    xb = dict(getattr(cfg, "extra_body", None) or {})
    for k, v in xb.items():
        if k not in body:
            body[k] = v
    return body


def _responses_text(data: dict) -> str:
    """从 Responses 返回体里取出正文，跳过推理摘要项。"""
    pieces: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    pieces.append(part.get("text") or "")
    return "".join(pieces).strip()


def _responses_error(data: dict) -> str:
    """Responses 的失败信息藏在 error / incomplete_details 里。"""
    err = data.get("error")
    if isinstance(err, dict):
        return f"{err.get('code') or ''} {err.get('message') or ''}".strip()
    if isinstance(err, str) and err:
        return err
    detail = data.get("incomplete_details") or {}
    if isinstance(detail, dict) and detail.get("reason"):
        return f"响应不完整：{detail['reason']}"
    return ""


# 推理模型的输出预算上限：思考过程也计入 max_tokens，给不够就会「只有推理没有正文」
_BUDGET_CEILING = 32768

# 推理类模型的常见命名特征。这类模型会先输出一大段 reasoning_content，
# 各业务调用点写死的 max_tokens=1200/3000 会被思考吃光，正文变成空或被截断，
# 所以在这里统一把预算抬到配置值，而不是去改十几个调用点。
_REASONING_HINT = re.compile(r"(reason|thinking|thinker|qwq|o1-|o3-|o4-|v4-pro|r1\b|\bdeepseek-v[3-9])", re.I)


def _is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_HINT.search(model or ""))


# ---------------- 环节档位与预算 ----------------
# 环节 → 基础思考深度。读一份 8 万字的标书、定一套目录结构，错一次后面全歪，
# 所以给 high；正文写作与润色是纯输出任务，长链推理只会拖慢并推高费用。
TASK_EFFORT: dict[str, str] = {
    "interpret": "high",
    "outline": "high",
    "evaluate": "high",
    "write": "low",
    "rewrite": "low",
    "polish": "low",
    "extract": "low",
}

# 环节 → 输出预算。写作要能一次写完一个长章节（约 5000 汉字），
# 抽取只要装得下结构化的 JSON。custom 档位不查这张表，仍按调用点参数走。
TASK_BUDGET: dict[str, int] = {
    "interpret": 16384,
    "outline": 8192,
    "evaluate": 4096,
    "write": 8000,
    "rewrite": 8000,
    "polish": 4096,
    "extract": 3072,
}

# 档位只换模型，不改思考深度：思考深度是「环节需要多想」的问题，与快慢取舍无关。
# 早期版本让 fast 档把解读也降成 low，结果结构抽取变差，已去掉这层降级。
def _tier(cfg: LLMConfig) -> str:
    return (getattr(cfg, "tier", "") or "").strip().lower()


def _effort(cfg: LLMConfig, task: str = "") -> str:
    """实际下发的思考档位。

    custom（或未设档位）时完全按配置走，保持老行为；
    设了档位就由环节决定：解读/大纲/评估要 high，写作/润色/抽取给 low。
    """
    tier = _tier(cfg)
    if tier in ("", "custom"):
        return (getattr(cfg, "reasoning_effort", "") or "").strip()
    return TASK_EFFORT.get(task or "", "") or (cfg.reasoning_effort or "")


def _budget(cfg: LLMConfig, want: int | None, task: str = "") -> int:
    """实际下发的输出预算。

    custom：尊重调用点传的值，推理模型不低于配置值（老行为）。
    档位模式：按环节取预算，调用点写死的小值不再生效——
    那些 1200/3000 是为了避免推理模型把预算吃光而设的，档位已经统一处理了这件事。
    """
    tier = _tier(cfg)
    if tier in ("", "custom"):
        base = want or cfg.max_tokens
        if _is_reasoning_model(cfg.model):
            base = max(base, cfg.max_tokens)
        return min(base, _BUDGET_CEILING)
    return min(TASK_BUDGET.get(task or "", 0) or cfg.max_tokens, _BUDGET_CEILING)


async def chat(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    retries: int = 2,
    task: str = "",
) -> str:
    if not cfg.ready:
        raise LLMError("尚未配置 AI 接口（接口地址 / 模型名）")

    api = wire_api(cfg)
    budget = _budget(cfg, max_tokens, task)
    if api == RESPONSES:
        payload = _responses_payload(
            cfg, messages, stream=False, max_tokens=budget, json_mode=json_mode, task=task
        )
        budget_key, path = "max_output_tokens", "/responses"
    else:
        payload = _payload(
            cfg, messages, stream=False, max_tokens=budget, json_mode=json_mode, task=task
        )
        budget_key, path = "max_tokens", "/chat/completions"

    last_error = ""
    for attempt in range(retries + 1):
        payload[budget_key] = budget
        try:
            timeout = httpx.Timeout(cfg.timeout, connect=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(_endpoint(cfg, path), headers=_headers(cfg), json=payload)
            if resp.status_code >= 400:
                detail = _brief(resp.text)
                # 不认结构化输出参数的接口：去掉后重试，别让整条链路直接失败
                if json_mode and resp.status_code in (400, 422):
                    if api == RESPONSES and "text" in payload and "format" in detail:
                        payload.pop("text", None)
                        continue
                    if "response_format" in detail and "response_format" in payload:
                        payload.pop("response_format", None)
                        continue
                if "reasoning_effort" in detail and "reasoning_effort" in payload:
                    payload.pop("reasoning_effort", None)
                    continue
                if api == RESPONSES and "reasoning" in detail and "reasoning" in payload:
                    payload.pop("reasoning", None)
                    continue
                raise LLMError(f"接口返回 {resp.status_code}：{detail}")
            data = resp.json()
            if api == RESPONSES:
                text = _responses_text(data)
                detail = data.get("incomplete_details") or {}
                truncated = isinstance(detail, dict) and detail.get("reason") == "max_output_tokens"
                reason_only = any(
                    isinstance(i, dict) and i.get("type") == "reasoning"
                    for i in data.get("output") or []
                )
                message = {}
                if not text and not truncated and not reason_only:
                    why = _responses_error(data)
                    raise LLMError(f"接口未返回任何内容{f'：{why}' if why else ''}")
            else:
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError("接口未返回任何内容")
                message = choices[0].get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                text = (content or "").strip()
                truncated = (choices[0].get("finish_reason") or "") == "length"
            if truncated and attempt < retries and budget < _BUDGET_CEILING:
                # JSON 被截断时 parse_json 会报「无法解析」，先给一次更大预算的机会
                budget = min(budget * 2, _BUDGET_CEILING)
                last_error = f"输出被截断（max_output_tokens 用尽），已提高到 {budget} 重试"
                continue
            if not text:
                # 推理类模型（deepseek-v4-pro / reasoner / codex）把过程放在 reasoning，
                # 思考吃满预算时正文就是空的——直接报错会让整条链路退回规则抽取，
                # 所以先自动把预算翻倍重试，实在不行才抛。
                had_reasoning = bool((message.get("reasoning_content") or "").strip()) or (
                    api == RESPONSES
                    and any(isinstance(i, dict) and i.get("type") == "reasoning"
                            for i in data.get("output") or [])
                )
                if had_reasoning:
                    if attempt < retries and budget < _BUDGET_CEILING:
                        budget = min(budget * 2, _BUDGET_CEILING)
                        last_error = f"推理耗尽输出预算，已提高到 {budget} 重试"
                        continue
                    raise LLMError(
                        f"模型只输出了推理过程，正文为空（max_tokens={budget}）"
                        "——请调大 LLM_MAX_TOKENS，或把 LLM_REASONING_EFFORT 调低/改用非推理模型"
                    )
                raise LLMError("模型返回为空（可能触发了内容限制）")
            return text
        except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_error = f"请求超时：{exc.__class__.__name__}"
            break
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc.__class__.__name__}"
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
        except LLMError as exc:
            last_error = str(exc)
            if "接口返回 5" in last_error and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
    raise LLMError(last_error or "调用失败")


async def chat_stream(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    task: str = "",
) -> AsyncIterator[str]:
    if not cfg.ready:
        raise LLMError("尚未配置 AI 接口（接口地址 / 模型名）")

    api = wire_api(cfg)
    budget = _budget(cfg, max_tokens, task)
    if api == RESPONSES:
        payload = _responses_payload(cfg, messages, stream=True, max_tokens=budget, task=task)
        path = "/responses"
    else:
        payload = _payload(cfg, messages, stream=True, max_tokens=budget, task=task)
        path = "/chat/completions"
    think_chars = 0
    sent_chars = 0
    timeout = httpx.Timeout(cfg.timeout, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", _endpoint(cfg, path), headers=_headers(cfg), json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise LLMError(f"接口返回 {resp.status_code}：{_brief(body.decode('utf-8', 'ignore'))}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if api == RESPONSES:
                    kind = data.get("type") or ""
                    if kind == "response.output_text.delta":
                        piece = data.get("delta") or ""
                        if piece:
                            sent_chars += len(piece)
                            yield piece
                    elif kind.startswith("response.reasoning"):
                        think_chars += len(data.get("delta") or "")
                    elif kind == "response.completed":
                        break
                    elif kind in ("response.failed", "error"):
                        why = _responses_error(data) or _brief(json.dumps(data, ensure_ascii=False))
                        raise LLMError(f"流式返回失败：{why}")
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reason = delta.get("reasoning_content")
                if isinstance(reason, str) and reason:
                    think_chars += len(reason)
                piece = delta.get("content")
                if isinstance(piece, list):
                    piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
                if piece:
                    sent_chars += len(piece)
                    yield piece
    if not sent_chars:
        # 推理过程吃完预算：不静默返回空章节，让上层明确提示，避免存下一段空正文
        raise LLMError(
            f"模型只输出了推理过程，正文为空（已思考 {think_chars} 字，max_tokens={payload['max_tokens']}）"
            "——请调大 LLM_MAX_TOKENS，或把 LLM_REASONING_EFFORT 调低/改用非推理模型"
        )


_THINK_RE = re.compile(r"<think>[\s\S]*?</think>|<think>[\s\S]*$", re.I)
# 只保留无歧义的转义；\t \b \f 不放行——模型写 Windows 路径（C:\test）时
# 这三个会静默变成制表符/退格/换页，宁可当字面反斜杠
_ESCAPE_OK = set('"/\\nru')


def _balanced_objects(body: str) -> list[str]:
    """按花括号配对提取候选片段：模型爱在 JSON 前后夹说明文字。"""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(body[start : i + 1])
                    start = -1
    return out


def _fix_escapes(s: str) -> str:
    """把非法的反斜杠转义补成合法（模型输出 Windows 路径时常见）。"""
    buf = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _ESCAPE_OK:
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue
            buf.append("\\\\")
            i += 1
            continue
        buf.append(ch)
        i += 1
    return "".join(buf)


def parse_json(text: str) -> dict:
    """多层容错的 JSON 解析，失败抛 LLMError。

    依次尝试：剥 think 标签与围栏 → 原样 → 去尾逗号 → 单引号转双引号 →
    修复非法转义 → 平衡括号重新提取后再走一遍上述清洗。
    """
    raw = (text or "").strip()
    if not raw:
        raise LLMError("模型返回为空")
    raw = _THINK_RE.sub("", raw).strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()

    def attempt(body: str) -> dict | None:
        candidates = [
            body,
            re.sub(r",\s*([}\]])", r"\1", body),
            re.sub(r"'([^'\\]*)'", r'"\1"', re.sub(r",\s*([}\]])", r"\1", body)),
            _fix_escapes(re.sub(r",\s*([}\]])", r"\1", body)),
        ]
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        got = attempt(raw[start : end + 1])
        if got:
            return got

    # 兜底：平衡括号提取（模型在 JSON 外面包了文字、或截断多余尾部时）
    for piece in _balanced_objects(raw):
        got = attempt(piece)
        if got:
            return got
    raise LLMError("模型返回的 JSON 无法解析")


async def list_models(base_url: str, api_key: str, *, timeout: int = 20) -> dict:
    """列出该接口暴露的模型，供下拉选择。

    只请求用户自己填写的地址，不内置任何厂商端点。
    兼容三种常见返回：{"data":[{"id":..}]}、{"models":[{"name":..}]}、顶层数组。
    """
    base = (base_url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/responses", "/completions", "/embeddings"):
        if base.endswith(tail):
            base = base[: -len(tail)]
    if not base:
        raise LLMError("请先填写接口地址")

    headers = {"Content-Type": "application/json"}
    if (api_key or "").strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    try:
        timeout_obj = httpx.Timeout(timeout, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout_obj) as client:
            resp = await client.get(f"{base}/models", headers=headers)
        if resp.status_code >= 400:
            raise LLMError(f"接口返回 {resp.status_code}：{_brief(resp.text)}")
        data = resp.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"无法访问该地址（{exc.__class__.__name__}）") from exc
    except ValueError as exc:
        raise LLMError("返回内容不是 JSON，可能不是 OpenAI 兼容接口") from exc

    items = data if isinstance(data, list) else (data.get("data") or data.get("models") or [])
    models: list[str] = []
    for item in items:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                models.append(str(name))
    # 去重保序，过滤明显的非对话模型
    seen: set[str] = set()
    result: list[str] = []
    for name in models:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    if not result:
        raise LLMError("该接口没有返回任何模型，请手动填写模型名")
    result.sort()
    return {"ok": True, "count": len(result), "models": result}


async def test_connection(cfg: LLMConfig) -> dict:
    """以最小 token 成本探活，返回耗时与模型自述。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    reply = await chat(
        cfg,
        [{"role": "user", "content": "只回复两个字：正常"}],
        # 推理类模型会先花掉一部分 token 在思考上，探活给足余量，避免正文被挤空
        max_tokens=512,
        retries=0,
    )
    return {"ok": True, "latency_ms": int((loop.time() - started) * 1000), "reply": _brief(reply, 40)}
