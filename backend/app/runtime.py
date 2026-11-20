"""R08 启动参数唯一来源：自检、启动脚本与真实服务读写同一处配置。

优先级：真实环境变量 > .env > 默认值。.env 由 app.config / basecore.config
在导入时加载，两者都遵守「不覆盖已有真实环境变量」。

注意：这里统一的是「环境隔离」（一台机器 / 一个部署实例的数据根目录与端口），
不等于「企业隔离」（多企业共享部署时各自的模型、密钥、策略与权限），后者见 C02。
"""
from __future__ import annotations

import os
from . import config  # noqa: F401  触发 .env 加载（必须在读环境变量之前）
from .basecore import config as basecfg

HOST = (os.environ.get("BIDAGENT_HOST") or os.environ.get("HOST")
        or basecfg.settings.host or "127.0.0.1").strip()


def _port() -> int:
    raw = os.environ.get("BIDAGENT_PORT") or os.environ.get("PORT") or ""
    try:
        return int(raw) if raw.strip() else int(basecfg.settings.port)
    except (ValueError, AttributeError):
        return 5210


PORT = _port()
DATA_DIR = config.DATA_DIR


def cli_args() -> str:
    """给 uvicorn 的参数，启动脚本直接用，避免自检与真实服务端口不一致。"""
    return f"--host {HOST} --port {PORT}"


def url() -> str:
    return f"http://{HOST}:{PORT}"


def summary() -> str:
    return (f"host={HOST} port={PORT} data={DATA_DIR} "
            f"env_file={basecfg.ENV_FILE or '（无 .env）'}")
