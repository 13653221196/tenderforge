"""全局配置：品牌名 + 运行参数。

所有运行参数都能用 `.env` 或环境变量覆盖，优先级：
    真实环境变量  >  .env 文件  >  代码默认值

.env 查找顺序（第一个存在的生效）：
    1. $BIDAGENT_ENV_FILE 指定的路径
    2. 项目根目录/.env          （推荐，和 启动.bat 同级）
    3. backend/.env

只读本项目自己的文件，不跨目录、不读其它项目的配置。

改品牌只改 APP_NAME / APP_SUBTITLE 两行。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

APP_NAME = "TenderForge"
APP_SUBTITLE = "标书锻造 · 投标文件智能编制工作台"
APP_VERSION = "0.2.0"

ROOT = Path(__file__).resolve().parents[3]


# ---------------- .env 加载（零依赖） ----------------
def _read_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE / export 前缀 / 引号 / 行尾注释。

    只实现常用子集，不引 python-dotenv，避免多一个运行时依赖。
    """
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return data
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # 行尾注释：前面带空白的 # 才算注释，避免密码里的 # 被截断
            for i, ch in enumerate(value):
                if ch == "#" and i and value[i - 1] in " \t":
                    value = value[:i].rstrip()
                    break
        if key:
            data[key] = value
    return data


def load_env() -> Path | None:
    """加载本项目自己的 .env 到 os.environ（不覆盖已存在的真实环境变量）。

    候选顺序：$BIDAGENT_ENV_FILE（兼容旧名 $BIDSTUDIO_ENV_FILE） → 项目根/.env
    → backend/.env。刻意不读当前工作目录，避免 systemd / 计划任务下读到意外文件。
    """
    explicit = (os.environ.get("BIDAGENT_ENV_FILE")
                or os.environ.get("BIDSTUDIO_ENV_FILE") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates += [ROOT / ".env", ROOT / "backend" / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for key, value in _read_dotenv(path).items():
            os.environ.setdefault(key, value)
        return path
    return None


ENV_FILE = load_env()


# ---------------- 读取工具 ----------------
def _env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量，支持多个别名（新名在前、通用名在后）。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_int(default: int, *names: str) -> int:
    raw = _env(*names, default="")
    try:
        return int(float(raw)) if raw else default
    except (TypeError, ValueError):
        return default


def _env_float(default: float, *names: str) -> float:
    raw = _env(*names, default="")
    try:
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_bool(default: bool, *names: str) -> bool:
    raw = _env(*names, default="").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on", "是")


def _env_list(*names: str, default: list[str] | None = None) -> list[str]:
    raw = _env(*names, default="")
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def resolve_sqlite_path(raw_url: str, data_dir: Path) -> Path:
    """把 DATABASE_URL 解析成 SQLite 文件路径。

    支持：
        sqlite:///data/app.db        → 相对项目根
        sqlite:///./data/app.db      → 相对项目根
        sqlite:////abs/path/app.db   → 绝对路径
        sqlite:///C:/bid/app.db      → Windows 绝对路径
        留空                          → data_dir/app.db
    非 sqlite 的 URL 直接报错，避免"以为成功了其实没生效"。
    """
    url = (raw_url or "").strip()
    if not url:
        return data_dir / "app.db"
    if "://" not in url:
        # 允许直接写文件路径
        path = Path(url).expanduser()
        return path if path.is_absolute() else (ROOT / path)
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme != "sqlite":
        raise RuntimeError(
            f"当前版本只支持 SQLite（DATABASE_URL 收到 {scheme}://）。"
            "如需 PostgreSQL / MySQL，请参考 docs/配置说明.md 的「换数据库」一节做适配。"
        )
    # sqlite:///rel 去掉开头的斜杠；sqlite:////abs 保留四位里的第四个斜杠后的绝对路径
    body = rest
    if body.startswith("/"):
        body = body[1:]
    if body in ("", ":memory:"):
        return data_dir / "app.db"
    path = Path(body).expanduser()
    if path.is_absolute() or (len(body) > 1 and body[1] == ":"):
        return path
    return ROOT / path


@dataclass(frozen=True)
class Settings:
    """运行参数快照。改这些不用动代码，改 .env 后重启即可。"""

    env_file: str = ""
    env: str = "local"
    host: str = "127.0.0.1"
    port: int = 5199
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    database_url: str = ""
    db_timeout: int = 30
    db_busy_timeout: int = 5000
    db_wal: bool = True
    redis_url: str = ""
    redis_prefix: str = "bidagent:"
    redis_db: int = 0
    cache_ttl: int = 60
    session_ttl_hours: int = 168
    cors_origins: list[str] = field(default_factory=list)
    max_upload_mb: int = 100
    tender_text_limit: int = 40000
    admin_username: str = "admin"
    # 不设默认口令：本项目账号体系由 auth.py 负责，基座自带的 admin 口令留空即不可登录
    admin_password: str = ""
    login_max_fails: int = 8
    login_lock_seconds: int = 300
    trust_proxy: bool = False
    log_level: str = "info"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 输出预算：推理模型（deepseek-v4-pro 这类）的思考过程也吃 max_tokens，
    # 长文解读留 4096 会被推理吃光、正文返回空字符串，这里默认给足。
    llm_max_tokens: int = 16384
    # 推理档位：low / high / max，留空表示不下发（兼容非推理模型与不认该参数的接口）
    llm_reasoning_effort: str = ""
    # 单次调用超时（秒）：8 万字解读 + 推理，120 秒不够
    llm_timeout: int = 300
    # 速度档位：fast / quality / custom。留空等同 custom，即完全按手填的模型与参数走
    llm_tier: str = ""

    @property
    def db_path(self) -> Path:
        return resolve_sqlite_path(self.database_url, self.data_dir)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    def public(self) -> dict:
        """给 /api/system/health 用的脱敏快照。

        db_path 解析失败（比如 DATABASE_URL 写成了 postgres://）时不能让自检接口崩，
        留空由上层显示原始 URL，方便看出配错在哪。
        """
        try:
            db_path = str(self.db_path)
        except RuntimeError:
            db_path = ""
        return {
            "env": self.env,
            "env_file": self.env_file,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "database": {
                "url": self.redacted_url(self.database_url) or f"sqlite:///{self.db_path.as_posix()}",
                "path": db_path,
                "wal": self.db_wal,
                "timeout": self.db_timeout,
            },
            "redis": {
                "enabled": self.redis_enabled,
                "url": self.redacted_url(self.redis_url),
                "prefix": self.redis_prefix,
            },
            "session_ttl_hours": self.session_ttl_hours,
            "cors_origins": self.cors_origins,
            "max_upload_mb": self.max_upload_mb,
            "tender_text_limit": self.tender_text_limit,
            "login_max_fails": self.login_max_fails,
            "login_lock_seconds": self.login_lock_seconds,
        }

    @staticmethod
    def redacted_url(url: str) -> str:
        """把 URL 里的密码换成 ***，避免健康检查把密钥回给前端。"""
        if not url or "@" not in url:
            return url
        head, _, tail = url.rpartition("@")
        if "://" not in head:
            return url
        scheme, _, cred = head.partition("://")
        if ":" in cred:
            user = cred.split(":", 1)[0]
            return f"{scheme}://{user}:***@{tail}"
        return url


_settings: Settings | None = None


def get_settings() -> Settings:
    """读取一次并缓存；测试里可用 reset_settings() 重新读环境变量。"""
    global _settings
    if _settings is None:
        data_dir = Path(_env("BIDAGENT_DATA", "BIDSTUDIO_DATA", "DATA_DIR", default=str(ROOT / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir
        _settings = Settings(
            env_file=str(ENV_FILE or ""),
            env=_env("BIDAGENT_ENV", "BIDSTUDIO_ENV", "APP_ENV", default="local"),
            host=_env("BIDAGENT_HOST", "BIDSTUDIO_HOST", "HOST", default="127.0.0.1"),
            port=_env_int(5210, "BIDAGENT_PORT", "BIDSTUDIO_PORT", "PORT"),
            data_dir=data_dir,
            database_url=_env("BIDAGENT_DATABASE_URL", "BIDSTUDIO_DATABASE_URL", "DATABASE_URL", default=""),
            db_timeout=_env_int(30, "BIDAGENT_DB_TIMEOUT", "BIDSTUDIO_DB_TIMEOUT", "DB_TIMEOUT"),
            db_busy_timeout=_env_int(5000, "BIDAGENT_DB_BUSY_TIMEOUT", "BIDSTUDIO_DB_BUSY_TIMEOUT", "DB_BUSY_TIMEOUT"),
            db_wal=_env_bool(True, "BIDAGENT_DB_WAL", "BIDSTUDIO_DB_WAL", "DB_WAL"),
            redis_url=_env("BIDAGENT_REDIS_URL", "BIDSTUDIO_REDIS_URL", "REDIS_URL", default=""),
            redis_prefix=_env("BIDAGENT_REDIS_PREFIX", "BIDSTUDIO_REDIS_PREFIX", "REDIS_PREFIX", default="bidagent:"),
            redis_db=_env_int(0, "BIDAGENT_REDIS_DB", "BIDSTUDIO_REDIS_DB", "REDIS_DB"),
            cache_ttl=_env_int(60, "BIDAGENT_CACHE_TTL", "BIDSTUDIO_CACHE_TTL", "CACHE_TTL"),
            session_ttl_hours=_env_int(168, "BIDAGENT_SESSION_TTL_HOURS", "BIDSTUDIO_SESSION_TTL_HOURS", "SESSION_TTL_HOURS"),
            cors_origins=_env_list(
                "BIDAGENT_CORS_ORIGINS", "BIDSTUDIO_CORS_ORIGINS",
                "CORS_ORIGINS",
                default=["http://127.0.0.1:5180", "http://localhost:5180"],
            ),
            max_upload_mb=_env_int(100, "BIDAGENT_MAX_UPLOAD_MB", "BIDSTUDIO_MAX_UPLOAD_MB", "MAX_UPLOAD_MB"),
            tender_text_limit=_env_int(40000, "BIDAGENT_TENDER_TEXT_LIMIT", "BIDSTUDIO_TENDER_TEXT_LIMIT", "TENDER_TEXT_LIMIT"),
            admin_username=_env("BIDAGENT_ADMIN_USERNAME", "BIDSTUDIO_ADMIN_USERNAME", "ADMIN_USERNAME", default="admin"),
            admin_password=_env("BIDAGENT_ADMIN_PASSWORD", "BIDSTUDIO_ADMIN_PASSWORD", "ADMIN_PASSWORD", default=""),
            login_max_fails=_env_int(8, "BIDAGENT_LOGIN_MAX_FAILS", "BIDSTUDIO_LOGIN_MAX_FAILS", "LOGIN_MAX_FAILS"),
            login_lock_seconds=_env_int(300, "BIDAGENT_LOGIN_LOCK_SECONDS", "BIDSTUDIO_LOGIN_LOCK_SECONDS", "LOGIN_LOCK_SECONDS"),
            trust_proxy=_env_bool(False, "BIDAGENT_TRUST_PROXY", "BIDSTUDIO_TRUST_PROXY", "TRUST_PROXY"),
            log_level=_env("BIDAGENT_LOG_LEVEL", "BIDSTUDIO_LOG_LEVEL", "LOG_LEVEL", default="info"),
            llm_base_url=_env("BIDAGENT_LLM_BASE_URL", "BIDSTUDIO_LLM_BASE_URL", "LLM_BASE_URL", default=""),
            llm_api_key=_env("BIDAGENT_LLM_API_KEY", "BIDSTUDIO_LLM_API_KEY", "LLM_API_KEY", default=""),
            llm_model=_env("BIDAGENT_LLM_MODEL", "BIDSTUDIO_LLM_MODEL", "LLM_MODEL", default=""),
            llm_max_tokens=_env_int(16384, "BIDAGENT_LLM_MAX_TOKENS", "BIDSTUDIO_LLM_MAX_TOKENS", "LLM_MAX_TOKENS"),
            llm_reasoning_effort=_env("BIDAGENT_LLM_REASONING_EFFORT", "BIDSTUDIO_LLM_REASONING_EFFORT", "LLM_REASONING_EFFORT", default=""),
            llm_timeout=_env_int(300, "BIDAGENT_LLM_TIMEOUT", "BIDSTUDIO_LLM_TIMEOUT", "LLM_TIMEOUT"),
            llm_tier=_env("BIDAGENT_LLM_TIER", "BIDSTUDIO_LLM_TIER", "LLM_TIER", default=""),
        )
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_settings() -> None:
    """测试用：丢弃缓存，下次 get_settings() 重新读环境变量。"""
    global _settings
    _settings = None


# ---------------- 模块级常量（向后兼容，其它模块直接 import） ----------------
settings = get_settings()
DATA_DIR = settings.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
KB_DIR = DATA_DIR / "kb"
KB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "basecore.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = settings.max_upload_mb
TENDER_TEXT_LIMIT = settings.tender_text_limit


class LLMConfig(BaseModel):
    """OpenAI 兼容接口配置。默认值可用 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 预置。"""

    base_url: str = settings.llm_base_url
    api_key: str = settings.llm_api_key
    model: str = settings.llm_model
    temperature: float = 0.3
    max_tokens: int = settings.llm_max_tokens
    timeout: int = settings.llm_timeout
    reasoning_effort: str = settings.llm_reasoning_effort
    tier: str = settings.llm_tier
    # 接口协议：chat = /chat/completions（默认），responses = /responses。
    # Codex 系列（gpt-5-codex / codex-mini-latest）只提供 Responses，塞进
    # chat/completions 会直接 404，所以必须能区分。留空表示按地址/模型名自动判断。
    wire_api: str = ""
    # 由 bidagent 注册表透传的请求体附加字段（如 {"enable_thinking": False}）
    extra_body: dict = {}

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())


# ---------------- 速度档位 ----------------
# 档位只回答一个问题：**用哪个模型、思考多深**。
# 输出预算不在这里，它按环节给（见 services/llm.py 的 TASK_BUDGET）——
# 因为「解读要装下 20 条评分项」和「写一章正文」需要的输出长度本来就不同。
LLM_TIERS: dict[str, dict] = {
    "fast": {
        "id": "fast",
        "label": "快速出稿",
        "model": "deepseek-flash",
        "effort": "low",
        "max_tokens": 8192,
        "timeout": 180,
        "desc": "用 Flash 模型跑全流程，适合初稿与批量出稿；解读与大纲仍是高思考档。",
    },
    "quality": {
        "id": "quality",
        "label": "高质量",
        "model": "deepseek-v4-pro",
        "effort": "high",
        "max_tokens": 16384,
        "timeout": 300,
        "desc": "用 Pro 模型跑全流程，适合要交出去的终稿；正文写作仍走低思考档，不空等。",
    },
    "custom": {
        "id": "custom",
        "label": "自定义",
        "model": "",
        "effort": "",
        "max_tokens": 0,
        "timeout": 0,
        "desc": "模型、推理档位、输出预算全部按你填的走，系统不做任何覆盖。",
    },
}

# 属于档位管理的模型名。切档位只在这几个之间换，不会把用户填的别家模型改掉
TIER_MODELS: set[str] = {t["model"] for t in LLM_TIERS.values() if t["model"]}


def tier_spec(tier: str) -> dict | None:
    return LLM_TIERS.get((tier or "").strip().lower())


def apply_tier(cfg: LLMConfig) -> LLMConfig:
    """把档位翻译成实际下发的模型、推理档位与超时。

    只在两种情况下改模型名：用户没填，或当前模型本身就是档位模型（fast ↔ quality
    切来切去）。如果用户填的是别家的模型（例如 qwen-plus），强行改成 deepseek-*
    只会让请求 400，所以保持原样、只调整思考档位。
    """
    tier = (getattr(cfg, "tier", "") or "").strip().lower()
    spec = tier_spec(tier)
    if not spec or tier == "custom":
        return cfg
    data = cfg.model_dump()
    current = (data.get("model") or "").strip()
    if spec["model"] and (not current or current in TIER_MODELS):
        data["model"] = spec["model"]
    if spec["max_tokens"]:
        data["max_tokens"] = spec["max_tokens"]
    if spec["timeout"]:
        data["timeout"] = spec["timeout"]
    data["reasoning_effort"] = spec["effort"]
    return LLMConfig(**data)


# 预设只填「接口地址 + 常用模型名」，密钥一律留空由用户自己填。
LLM_PRESETS: list[dict] = [
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "hint": "在 platform.deepseek.com 生成密钥填到下框；deepseek-v4-pro 质量最好，deepseek-flash 更快更省",
        "models": ["deepseek-v4-pro", "deepseek-flash", "deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "hint": "国内可直连，除 DeepSeek 外还托管 Qwen、GLM 等模型",
        "models": ["deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1", "Qwen/Qwen2.5-72B-Instruct"],
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "hint": "通义千问的 OpenAI 兼容模式，模型名以 qwen 开头",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "",
        "hint": "长文本处理见长，适合整本招标文件通读",
        "models": ["moonshot-v1-32k", "moonshot-v1-128k"],
    },
    {
        "id": "ollama",
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "hint": "装好 Ollama 后无需密钥，模型名用 `ollama list` 里的名字",
        "models": [
            "qwen2.5:7b-instruct",
            "qwen2.5:14b-instruct",
            "qwen2.5:32b-instruct",
            "deepseek-r1:14b",
            "deepseek-r1:32b",
            "glm4:9b",
            "llama3.1:8b",
        ],
    },
    {
        "id": "vllm",
        "name": "本地 vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "hint": "vLLM 默认 8000 端口，模型名取启动时 --served-model-name 的值",
        "models": [],
    },
    {
        "id": "lmstudio",
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "",
        "hint": "开启本地服务器后使用，模型名在界面上可直接复制",
        "models": [],
    },
    {
        "id": "xinference",
        "name": "Xinference",
        "base_url": "http://127.0.0.1:9997/v1",
        "api_key": "",
        "hint": "本地部署的推理服务，模型名与启动的 model uid 一致",
        "models": [],
    },
    {
        "id": "gateway",
        "name": "自建网关 / 中转",
        "base_url": "http://127.0.0.1:3000/v1",
        "api_key": "",
        "hint": "如自建的 OpenAI 兼容网关，地址与密钥都填你自己的",
        "models": [],
    },
    {
        "id": "custom",
        "name": "自定义",
        "base_url": "",
        "api_key": "",
        "hint": "任意兼容 /chat/completions 与 /models 的服务",
        "models": [],
    },
]
