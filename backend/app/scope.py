"""C02 企业作用域：配置 / 密钥 / 策略 / 原件 / 输出统一按企业落盘。

约定（对应评审 C02「企业配置隔离」）：

- 默认企业 ``default`` 沿用旧数据根目录，旧数据不用搬家就能用；
- 其他企业放 ``data/orgs/{org}/``，和物理分库 ``data/orgs/{org}.db`` 同一套企业标识；
- 策略类（agent_config / privacy / budget / mode）在企业没有自己的副本时
  **明确继承平台默认**；本企业一旦保存，就只改自己的文件；
- 密钥类（models / model_health）**绝不继承、绝不复制**：别的企业（含平台）
  的密钥不会出现在该企业里，需要各自录入；环境变量里的平台密钥同理；
- 旧公共文件由 ``migrate_legacy()`` 显式登记归属默认企业，不做静默复制。

所有路径都从 ``config.DATA_DIR`` 取（R08：.env 与环境变量一处生效）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_ORG = "default"

# 含密钥或密钥使用痕迹：只认本企业自己的文件
SECRET_FILES = frozenset({"models.json", "model_health.json"})
# 非敏感策略：企业没有副本时读平台默认，并在界面如实标注「继承」
INHERITABLE = frozenset({"agent_config.json", "privacy.json", "budget.json",
                         "mode.txt"})
ALL_FILES = ("models.json", "model_health.json", "privacy.json", "budget.json",
             "mode.txt", "agent_config.json")
OWNERSHIP_FILE = "config_ownership.json"


def data_dir() -> Path:
    """延迟取，避免 config 与 scope 相互导入形成环。"""
    from . import config
    return Path(config.DATA_DIR)


def org() -> str:
    """当前请求所属企业；不在请求里时退回默认企业（启动自检 / 脚本）。"""
    try:
        from . import auth
        return str(auth.org_id() or DEFAULT_ORG)
    except Exception:
        return DEFAULT_ORG


def is_platform(oid: str = "") -> bool:
    return (oid or org()) == DEFAULT_ORG


def platform_file(name: str) -> Path:
    return data_dir() / name


def org_dir(oid: str = "") -> Path:
    return data_dir() / "orgs" / (oid or org())


def org_file(name: str, oid: str = "", *, write: bool = False) -> Path:
    """本企业该文件的路径；write=True 时顺手建好目录。"""
    o = oid or org()
    p = platform_file(name) if o == DEFAULT_ORG else org_dir(o) / name
    if write:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def config_path(name: str, *, write: bool = False) -> Path:
    """读：策略类可回退平台默认；写：永远落在本企业，绝不改别的企业。"""
    own = org_file(name, write=write)
    if write or is_platform() or own.exists() or name not in INHERITABLE:
        return own
    return platform_file(name)


def inherited(name: str) -> bool:
    return (name in INHERITABLE and not is_platform()
            and not org_file(name).exists() and platform_file(name).exists())


def origin(name: str) -> dict:
    """给界面看的来源说明：本企业 / 继承平台 / 本企业尚未配置。"""
    o = org()
    if name in SECRET_FILES and not is_platform(o) and not org_file(name, o).exists():
        return {"source": "none", "org": o, "inherited": False,
                "note": "本企业尚未配置模型；为隔离密钥，未继承其他企业或环境变量的密钥"}
    if inherited(name):
        return {"source": "platform", "org": o, "inherited": True,
                "note": "继承平台默认策略；本企业一旦保存即独立，不影响其他企业"}
    return {"source": "org", "org": o, "inherited": False,
            "note": "本企业自己的配置"}


def status() -> dict:
    """设置 / 状态页展示：哪些是本企业的、哪些是继承的、哪些没配。"""
    o = org()
    return {
        "org": o, "platform": is_platform(o),
        "files": {n: {"source": origin(n)["source"],
                      "inherited": origin(n)["inherited"],
                      "exists": org_file(n, o).exists()}
                  for n in ALL_FILES},
        "note": ("模型与密钥按企业隔离，绝不复制给其他企业；"
                 "策略类可继承平台默认，本企业保存后独立"),
        "ownership": migrate_legacy(),
    }


def dir_for(kind: str, *, write: bool = False) -> Path:
    """原件 / 上传 / 缓存等目录：其他企业放进自己的企业目录。"""
    p = org_dir() / kind if not is_platform() else data_dir() / kind
    if write:
        p.mkdir(parents=True, exist_ok=True)
    return p


def migrate_legacy() -> dict:
    """把旧公共配置显式登记归属默认企业；不复制任何文件给其他企业。"""
    d = data_dir()
    f = d / OWNERSHIP_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    present = [n for n in ALL_FILES if (d / n).exists()]
    rec = {
        "owner": DEFAULT_ORG,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": present,
        "note": "旧公共配置归属默认企业；其他企业不复制任何密钥，密钥需各自录入。"
                "旧的 data/originals、data/exports 仍按项目 ID 读取，归属不变。",
    }
    try:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return rec
