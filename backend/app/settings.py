"""智能体策略配置：基座能力的唯一事实源。

以前阶段挂哪些能力写死在 auto_core.STAGES 里，界面只有一个「自动调用」布尔开关，
大脑自己挑的 base.* 又直接走 core_bridge.run，等于配置管不住一半调用。

现在所有路径（自动编排 / 大脑规划与执行 / 单项调试 / 导出门禁 / 界面）都读这一份配置：
- 停用某项能力 → 任何路径都不会再执行它；
- 阶段勾了哪些 → 自动编排与导出门禁用同一份清单；
- 允许大脑自动挑选的范围 → 规划器目录里只出现这些工具；
- 保存后立即生效，不重启服务（每次调用都重新读文件）。

配置文件只存策略，不存密钥；密钥仍在 data/models.json / .env。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import config, scope

# 与主库共用同一个解析后的数据根目录（.env / BIDAGENT_DATA 一处生效）
DATA = config.DATA_DIR
FILE = DATA / "agent_config.json"   # 平台默认企业的旧位置；运行时按企业取路径
_LOCK = threading.Lock()

# 阶段默认能力清单（唯一一份默认定义；运行时以配置文件为准）
STAGE_META: list[dict] = [
    {"key": "tender", "label": "导入招标文件", "trigger": "上传 / 粘贴招标文件后",
     "desc": "自动抽取硬性要求、废标条款、评分项，直接约束大纲与逐章写作"},
    {"key": "draft", "label": "导入已有竞标书", "trigger": "导入已有标书后",
     "desc": "自动跑全面体检、废标核对、暗标、雷同、事实一致性与报价校验"},
    {"key": "compose", "label": "一键成稿完成", "trigger": "整本生成结束",
     "desc": "成稿后立刻自动合规复检，并生成可提交的检查报告"},
    {"key": "check", "label": "每次体检", "trigger": "点体检 / 说「验标」 / 闭环每轮复检",
     "desc": "基座规则结论自动并入问题清单，闭环会直接按这些问题改写"},
    {"key": "optimize", "label": "优化闭环完成", "trigger": "改写 + 复检结束",
     "desc": "自动出复检报告与偏离表，作为交付前的合规附件"},
    {"key": "export", "label": "导出前门禁", "trigger": "下载 Word / Markdown 前",
     "desc": "占位符、废标点、报价算术、暗标信息最后一轮自动核对"},
]
DEFAULT_STAGES: dict[str, list[str]] = {
    "tender": ["interpret", "tender_audit", "requirements", "scoring",
               "rejection", "tender_sections"],
    "draft": ["import_bid", "check_full", "rejection", "requirements",
              "blind_text", "dupcheck", "facts", "verify_citations", "quote"],
    "compose": ["check_full", "rejection", "requirements", "facts",
                "verify_citations", "cleaner", "report"],
    "check": ["check_full", "rejection", "requirements", "blind_text", "facts",
              "verify_citations", "tender_audit", "quote"],
    "optimize": ["check_full", "report", "deviation"],
    "export": ["check_full", "rejection", "quote", "blind_text"],
}
DEFAULT_EXPORT_CAPS = list(DEFAULT_STAGES["export"])
# 正式导出的底线检查：设置页可以加，不能减（见 export_caps）
REQUIRED_EXPORT_CAPS = ("check_full", "rejection", "quote", "blind_text")


def _known_keys() -> set[str]:
    try:
        from . import core_bridge
        return {c["key"] for c in core_bridge.CAPABILITIES}
    except Exception:
        return set().union(*DEFAULT_STAGES.values())


def _default() -> dict:
    return {
        "version": 1,
        "auto_enabled": True,
        "disabled_caps": [],
        "brain_auto_allow": "*",          # "*" = 所有允许的本地基座；也可给能力 key 白名单
        "allow_model_caps_auto": False,   # 需要真实模型的 AI 增强能力默认不自动跑
        "stages": {k: list(v) for k, v in DEFAULT_STAGES.items()},
        "export_caps": list(DEFAULT_EXPORT_CAPS),
        "export_quality": True,   # 导出前列出中等生成质量缺陷（章节硬伤始终列为重点提醒）
    }


def _clean(raw: Any) -> dict:
    """读到的配置合到默认值上，丢弃不认识的字段与能力名。"""
    d = _default()
    if not isinstance(raw, dict):
        return d
    known = _known_keys()
    d["auto_enabled"] = bool(raw.get("auto_enabled", True))
    d["allow_model_caps_auto"] = bool(raw.get("allow_model_caps_auto", False))
    disabled = raw.get("disabled_caps")
    if isinstance(disabled, list):
        d["disabled_caps"] = sorted({str(x) for x in disabled if str(x) in known})
    allow = raw.get("brain_auto_allow")
    if isinstance(allow, list):
        d["brain_auto_allow"] = sorted({str(x) for x in allow if str(x) in known})
    elif allow in ("*", ""):
        d["brain_auto_allow"] = "*"
    stages = raw.get("stages")
    if isinstance(stages, dict):
        for key in DEFAULT_STAGES:
            v = stages.get(key)
            if isinstance(v, list):
                d["stages"][key] = list(dict.fromkeys(
                    str(x) for x in v if str(x) in known))
    exp = raw.get("export_caps")
    if isinstance(exp, list):
        d["export_caps"] = list(dict.fromkeys(
            str(x) for x in exp if str(x) in known))
    d["export_quality"] = bool(raw.get("export_quality", True))
    return d


def load() -> dict:
    """每次都读盘：界面保存后不重启也立即生效；按企业隔离，策略可继承平台默认。"""
    try:
        f = scope.config_path("agent_config.json")
        if f.exists():
            return _clean(json.loads(f.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        pass
    return _default()


def save(patch: dict) -> dict:
    """只接受白名单字段；未知字段静默忽略，避免把任意内容写进配置。"""
    with _LOCK:
        cur = load()
        allowed = ("auto_enabled", "disabled_caps", "brain_auto_allow",
                   "allow_model_caps_auto", "stages", "export_caps",
                   "export_quality")
        merged = {**cur, **{k: v for k, v in (patch or {}).items() if k in allowed}}
        # 兼容只提交 stages.export 的旧调用；前端同时给 export_caps 时以它为准
        patch_stages = (patch or {}).get("stages")
        if (isinstance(patch_stages, dict) and "export" in patch_stages
                and "export_caps" not in (patch or {})):
            merged["export_caps"] = patch_stages["export"]
        clean = _clean(merged)
        # 只写本企业文件；继承平台默认的企业在第一次保存时自然独立
        f = scope.config_path("agent_config.json", write=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
        return clean


# ---------------------------------------------------------------- 判定
def cap_enabled(key: str) -> bool:
    """停用 = 任何路径都不执行（大脑点名、单项调试、阶段、门禁一视同仁）。"""
    return key not in load().get("disabled_caps", [])


def cap_auto_allowed(key: str) -> bool:
    """自动路径（阶段 / 导出门禁 / 大脑自动挑选）能不能用这项能力。"""
    cfg = load()
    if key in cfg.get("disabled_caps", []):
        return False
    if not cfg.get("allow_model_caps_auto", False):
        try:
            from . import core_bridge
            if key in core_bridge.AI_NEED_MODEL_CAPS:
                return False
        except Exception:
            pass
    return True


def stage_caps(stage: str) -> list[str]:
    # 导出门禁项单独一项配置，避免「阶段清单」和「门禁清单」两处漂移
    if stage == "export":
        return export_caps()
    cfg = load()
    raw = (cfg.get("stages") or {}).get(stage)
    if not isinstance(raw, list):
        raw = DEFAULT_STAGES.get(stage, [])
    return [k for k in raw if cap_auto_allowed(k)]


def export_quality() -> bool:
    """导出前是否把生成质量硬伤列为待确认问题；默认开，可在设置页关。"""
    return bool(load().get("export_quality", True))


def export_caps() -> list[str]:
    """正式导出的必需检查清单：配置可以加项，不能减掉底线。

    这四项分别管占位符/硬伤、废标条款、报价算术、暗标信息。关掉自动编排、
    清空可选能力也必须照跑；跑不起来就硬阻断，不能变成「跳过 = 没问题」。
    """
    configured = [k for k in load().get("export_caps", []) if cap_auto_allowed(k)]
    return list(dict.fromkeys(list(REQUIRED_EXPORT_CAPS) + configured))


def auto_enabled() -> bool:
    return bool(load().get("auto_enabled", True))


def stage_enabled(stage: str) -> bool:
    # 导出门禁是安全底线，不受「自动调用」总开关影响
    if stage == "export":
        return True
    return auto_enabled() and bool(stage_caps(stage))


def tool_selectable(name: str) -> bool:
    """规划器 / 复盘器 / 执行器共用的工具准入判定。"""
    if name.startswith("base."):
        key = name[5:]
        if not cap_auto_allowed(key):
            return False
        allow = load().get("brain_auto_allow", "*")
        return allow == "*" or key in (allow if isinstance(allow, list) else [])
    if name.startswith("stage."):
        return stage_enabled(name[6:])
    return True                      # flow.* / ai.* 不属于基座策略


def public() -> dict:
    """给设置页用：配置 + 能力目录 + 阶段元数据 + 生效摘要。"""
    cfg = load()
    try:
        from . import core_bridge
        caps = [{"key": c["key"], "name": c["name"], "group": c["group"],
                 "availability": ("占位" if c["key"] in core_bridge.PLACEHOLDER_CAPS
                                  else "需模型" if c["key"] in core_bridge.AI_NEED_MODEL_CAPS
                                  else "可执行")}
                for c in core_bridge.CAPABILITIES]
        groups = list(core_bridge.GROUPS)
    except Exception:
        caps, groups = [], []
    stages = []
    for s in STAGE_META:
        key = s["key"]
        configured = (export_caps() if key == "export" else
                      list((cfg.get("stages") or {}).get(key, [])))
        stages.append({**s, "caps": stage_caps(key), "configured": configured,
                       "enabled": auto_enabled()})
    selectable = [k for k in (c["key"] for c in caps)
                  if tool_selectable("base." + k)]
    return {
        **cfg,
        "stage_config": {k: list(v) for k, v in (cfg.get("stages") or {}).items()},
        "caps": caps,
        "groups": groups,
        "stages": stages,
        "effective": {
            "auto_enabled": auto_enabled(),
            "disabled": len(cfg.get("disabled_caps", [])),
            "brain_selectable": len(selectable),
            "export_caps": export_caps(),
            "export_quality": export_quality(),
            "stage_sizes": {s["key"]: len(s["caps"]) for s in stages},
            "file": str(scope.config_path("agent_config.json")),
            "config_origin": scope.origin("agent_config.json"),
        },
    }
