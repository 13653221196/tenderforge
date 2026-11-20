"""技能引擎：把招标业务能力做成可装载、可启停、可安装的「技能」。

设计原则
--------
1. **文件即技能**：每个技能一个目录，内含 ``SKILL.md``（YAML 头 + Markdown 正文）。
   目录即分类，文件名即技能名，删掉目录即卸载内置技能。
2. **零外部依赖**：frontmatter 用手写解析器，不引入 PyYAML。
3. **执行与描述分离**：``SKILL.md`` 说「做什么、怎么判」，真正的判定逻辑在
   ``skill_defs.py`` 里按技能名注册；只有描述没有实现的技能不会被注册为可执行。
4. **状态落库**：启停、安装时间、运行次数存在 SQLite，重启后仍生效。
5. **安全边界**：导入的技能只解析元数据与正文，**不执行任何导入的代码**；
   自定义技能只能是 ``prompt`` / ``manual`` 两种模式，纯文本，无代码执行面。
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
CUSTOM_DIR = SKILLS_DIR / "_custom"

CATEGORY_LABEL = {
    "interpret": "招标解读",
    "generate": "标书生成",
    "check": "标书检查",
    "output": "输出交付",
    "custom": "自建技能",
}

MODE_LABEL = {
    "rule": "规则引擎",
    "prompt": "AI 提示词",
    "manual": "人工清单",
}

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class SkillError(RuntimeError):
    pass


# ---------------------------------------------------------------- 数据模型
@dataclass
class SkillMeta:
    name: str
    title: str = ""
    description: str = ""
    category: str = "custom"
    version: str = "1.0.0"
    mode: str = "manual"
    level: str = "基础"
    needs: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    body: str = ""
    path: str = ""
    builtin: bool = True

    def public(self) -> dict:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "category": self.category,
            "category_label": CATEGORY_LABEL.get(self.category, self.category),
            "version": self.version,
            "mode": self.mode,
            "mode_label": MODE_LABEL.get(self.mode, self.mode),
            "level": self.level,
            "needs": self.needs,
            "triggers": self.triggers,
            "body": self.body,
            "builtin": self.builtin,
        }


# ---------------------------------------------------------------- frontmatter
def _scalar(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",") if part.strip()]
    return text


def parse_manifest(text: str) -> tuple[dict, str]:
    """解析 ``---`` 包裹的 frontmatter。只支持 key: value 与短横线列表。"""
    raw = (text or "").replace("\r\n", "\n").lstrip("\ufeff")
    if not raw.startswith("---"):
        raise SkillError("SKILL.md 缺少 frontmatter（文件必须以 --- 开头）")
    end = raw.find("\n---", 3)
    if end == -1:
        raise SkillError("SKILL.md 的 frontmatter 未闭合（缺少结束的 ---）")
    head = raw[3:end]
    body = raw[end + 4 :].lstrip("\n")

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in head.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_list_key:
            meta.setdefault(current_list_key, []).append(_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            meta[key] = _scalar(value)
            current_list_key = None
        else:
            meta[key] = []
            current_list_key = key
    return meta, body


# ---------------------------------------------------------------- 扫描
logger = logging.getLogger(__name__)

_cache: dict[str, SkillMeta] = {}
_stamp: float = -1.0
# 最近一次扫描中读不到的 SKILL.md（路径 -> 原因）。
# 单个文件权限异常不再让整个技能页 500，但必须能被 /api/skills 回显出来，
# 否则「技能少了几项」会变成无声故障。
_scan_errors: dict[str, str] = {}


def _paths_safe() -> list[Path]:
    """列目录本身也可能因权限抛 OSError，统一在这里兜住。"""
    if not SKILLS_DIR.exists():
        _scan_errors.pop(str(SKILLS_DIR), None)
        return []
    try:
        paths = list(SKILLS_DIR.rglob("SKILL.md"))
    except OSError as exc:
        _scan_errors[str(SKILLS_DIR)] = f"目录不可读：{type(exc).__name__}: {exc}"
        logger.warning("技能目录不可读 %s: %s", SKILLS_DIR, exc)
        return []
    _scan_errors.pop(str(SKILLS_DIR), None)
    return paths


def _dir_stamp() -> float:
    latest = 0.0
    for path in _paths_safe():
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def scan(force: bool = False) -> dict[str, SkillMeta]:
    global _cache, _stamp
    stamp = _dir_stamp()
    if not force and _cache and stamp == _stamp:
        return _cache

    found: dict[str, SkillMeta] = {}
    for path in sorted(_paths_safe()):
        try:
            meta, body = parse_manifest(path.read_text(encoding="utf-8", errors="ignore"))
        except SkillError:
            # frontmatter 不合规：按设计跳过，不算故障
            _scan_errors.pop(str(path), None)
            continue
        except (OSError, UnicodeError) as exc:
            # 权限不足 / 属主不对 / 文件损坏：只跳过这一个，绝不拖垮整个接口
            _scan_errors[str(path)] = f"{type(exc).__name__}: {exc}"
            logger.warning("技能文件读取失败，已跳过 %s: %s", path, exc)
            continue
        _scan_errors.pop(str(path), None)
        name = str(meta.get("name") or path.parent.name).strip()
        if not NAME_RE.match(name):
            continue
        category = str(meta.get("category") or path.parent.parent.name or "custom")
        builtin = CUSTOM_DIR not in path.parents
        found[name] = SkillMeta(
            name=name,
            title=str(meta.get("title") or ""),
            description=str(meta.get("description") or ""),
            category=category,
            version=str(meta.get("version") or "1.0.0"),
            mode=str(meta.get("mode") or "manual"),
            level=str(meta.get("level") or "基础"),
            needs=None if meta.get("needs") is None else list(meta.get("needs") or []),  # type: ignore[arg-type]
            triggers=list(meta.get("triggers") or []),
            body=body,
            path=str(path),
            builtin=builtin,
        )
    _cache = found
    _stamp = stamp
    return _cache


def scan_errors() -> dict[str, str]:
    """最近一次扫描读不到的文件，供接口回显做诊断。"""
    return dict(_scan_errors)


def reload() -> int:
    return len(scan(force=True))


def get(name: str) -> SkillMeta:
    skills = scan()
    if name not in skills:
        raise SkillError(f"技能不存在：{name}")
    return skills[name]


# ---------------------------------------------------------------- 目录
def catalog(*, include_disabled: bool = True) -> list[dict]:
    from .skill_defs import RUNNERS
    from .skill_defs.generate import PROMPT_PARAMS

    rows = db.list_skill_states()
    skills = scan()
    out: list[dict] = []
    for name, meta in skills.items():
        state = rows.get(name) or {}
        data = meta.public()
        data.update(
            {
                "enabled": bool(state.get("enabled", 1)),
                "runnable": name in RUNNERS or meta.mode == "prompt",
                "runs": int(state.get("runs") or 0),
                "last_run": state.get("last_run") or "",
                "installed_at": state.get("installed_at") or "",
                "source": state.get("source") or ("builtin" if meta.builtin else "imported"),
                "params": PROMPT_PARAMS.get(name, []),
            }
        )
        if not include_disabled and not data["enabled"]:
            continue
        out.append(data)

    order = {"interpret": 0, "generate": 1, "check": 2, "output": 3, "custom": 4}
    out.sort(key=lambda x: (order.get(x["category"], 9), x["builtin"] is False, x["name"]))
    return out


def stats() -> dict:
    rows = catalog()
    return {
        "total": len(rows),
        "enabled": sum(1 for r in rows if r["enabled"]),
        "rule": sum(1 for r in rows if r["mode"] == "rule"),
        "prompt": sum(1 for r in rows if r["mode"] == "prompt"),
        "manual": sum(1 for r in rows if r["mode"] == "manual"),
        "custom": sum(1 for r in rows if not r["builtin"]),
        "runs": sum(r["runs"] for r in rows),
    }


def set_enabled(name: str, enabled: bool) -> dict:
    meta = get(name)
    db.set_skill_state(name, enabled=enabled, category=meta.category, version=meta.version)
    return {"ok": True, "name": name, "enabled": enabled}


def set_enabled_bulk(names: list[str], enabled: bool) -> dict:
    for name in names:
        try:
            meta = get(name)
        except SkillError:
            continue
        db.set_skill_state(name, enabled=enabled, category=meta.category, version=meta.version)
    return {"ok": True, "count": len(names)}


# ---------------------------------------------------------------- 安装 / 卸载
def _validate_import(meta: dict) -> tuple[str, str, str]:
    name = str(meta.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise SkillError("技能名只能用小写字母、数字、下划线，且以字母开头（3–40 位）")
    existing = scan().get(name)
    if existing and existing.builtin:
        raise SkillError(f"「{name}」是内置技能，不能覆盖；请换一个名字")
    mode = str(meta.get("mode") or "prompt").strip()
    if mode not in ("prompt", "manual"):
        raise SkillError("导入的技能只能是 prompt（AI 提示词）或 manual（人工清单），不支持代码执行")
    category = str(meta.get("category") or "custom").strip()
    if category not in CATEGORY_LABEL:
        category = "custom"
    return name, mode, category


def install(text: str, *, overwrite: bool = False) -> dict:
    """导入一个 SKILL.md。只登记元数据与正文，不执行任何代码。"""
    meta, body = parse_manifest(text)
    name, mode, category = _validate_import(meta)
    existing = scan().get(name)
    if existing and not overwrite:
        raise SkillError(f"「{name}」已存在，勾选覆盖后再导入")

    target = CUSTOM_DIR / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 统一改写 category，避免自定义技能散落到内置分类里
    raw = text.replace("\r\n", "\n").lstrip("\ufeff")
    if re.search(r"^category:", raw[:2000], re.M):
        raw = re.sub(r"^category:.*$", f"category: {category}", raw, count=1, flags=re.M)
    else:
        raw = raw.replace("---", f"---\ncategory: {category}", 1) if raw.startswith("---") else raw
    target.write_text(raw, encoding="utf-8")

    reload()
    db.set_skill_state(name, enabled=True, category=category, version=str(meta.get("version") or "1.0.0"),
                       source="imported")
    return {"ok": True, "name": name, "mode": mode, "path": str(target)}


def uninstall(name: str) -> dict:
    meta = get(name)
    if meta.builtin:
        raise SkillError("内置技能不能删除，可以停用")
    folder = Path(meta.path).parent
    if CUSTOM_DIR not in folder.parents and folder.parent != CUSTOM_DIR:
        raise SkillError("只能删除导入的技能")
    shutil.rmtree(folder, ignore_errors=True)
    reload()
    db.delete_skill_state(name)
    return {"ok": True, "name": name}


def export_manifest(name: str) -> str:
    meta = get(name)
    return Path(meta.path).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 运行
def bump_run(name: str) -> None:
    db.bump_skill_run(name)


def resolve_params(meta: SkillMeta, ctx: dict) -> None:
    """校验技能所需输入是否齐备。"""
    needs = meta.needs or ["tender"]
    if "tender" in needs and not (ctx.get("tender_text") or "").strip():
        raise SkillError("该技能需要招标文件正文，请先在项目里上传招标文件")
    if "bid" in needs and not (ctx.get("bid_text") or "").strip():
        raise SkillError("该技能需要投标文件正文，请上传一份 docx / pdf / txt")


async def execute(name: str, ctx: dict, cfg) -> dict:
    """执行一个技能，返回统一结构的结果。"""
    from fastapi.concurrency import run_in_threadpool

    meta = get(name)
    state = db.list_skill_states().get(name) or {}
    if not state.get("enabled", 1):
        raise SkillError(f"「{meta.title or name}」已停用，请先在技能库中启用")

    resolve_params(meta, ctx)

    if meta.mode == "prompt":
        from .skill_defs.prompt import run_prompt

        data = await run_prompt(meta, ctx, cfg)
    else:
        from .skill_defs import RUNNERS

        runner = RUNNERS.get(name)
        if runner is None:
            raise SkillError(f"「{meta.title or name}」只有文档说明，暂无可执行实现")
        data = await run_in_threadpool(runner, ctx)

    data.setdefault("skill", {"name": meta.name, "title": meta.title or meta.name,
                              "category": meta.category, "mode": meta.mode})
    bump_run(name)
    db.add_skill_run(
        name,
        project_id=str(ctx.get("project_id") or ""),
        verdict=str(data.get("verdict") or ""),
        score=data.get("score"),
        summary=str(data.get("summary") or "")[:300],
        payload=data,
    )
    return data
