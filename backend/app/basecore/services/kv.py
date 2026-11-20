"""键值缓存：Redis 可选，未配置或连不上时自动降级到进程内内存。

为什么要有这一层
----------------
登录失败限流、在线会话统计、短缓存这些东西，「有 Redis 更好、没有也不能少功能」。
所以这里做成可插拔后端：

    REDIS_URL 为空 / 没装 redis 包 / 连不上  →  MemoryBackend（进程内，带 TTL 与惰性淘汰）
    REDIS_URL 可用                          →  RedisBackend（多进程、多实例共享）

调用方只用 get / set / delete / incr / ttl，不关心后端是谁。
`health()` 会如实报告当前用的是哪个后端、为什么降级。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..config import get_settings

_CFG = get_settings()


class KVError(RuntimeError):
    """键值层自身出错。调用方一般应吞掉——缓存挂了不能影响主流程。"""


# ---------------- 内存后端 ----------------
class MemoryBackend:
    """进程内 KV，带 TTL。仅单进程有效，重启即清空。

    用途：没配 Redis 时的兜底；也用于测试。
    """

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        dead = [k for k, (_, exp) in self._data.items() if exp and exp <= now]
        for key in dead:
            self._data.pop(key, None)

    def get(self, key: str) -> str | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            value, exp = item
            if exp and exp <= now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        exp = time.time() + ttl if ttl else 0.0
        with self._lock:
            self._data[key] = (str(value), exp)

    def delete(self, *keys: str) -> int:
        with self._lock:
            return sum(1 for k in keys if self._data.pop(k, None) is not None)

    def incr(self, key: str, ttl: int | None = None) -> int:
        now = time.time()
        with self._lock:
            self._purge(now)
            raw, exp = self._data.get(key, ("0", 0.0))
            if exp and exp <= now:
                raw, exp = "0", 0.0
            try:
                value = int(raw) + 1
            except ValueError:
                value = 1
            if not exp and ttl:
                exp = now + ttl
            self._data[key] = (str(value), exp)
            return value

    def ttl(self, key: str) -> int:
        """>0 剩余秒；0 不存在；-1 存在但无过期。"""
        item = self._data.get(key)
        if not item:
            return 0
        _, exp = item
        if not exp:
            return -1
        left = int(exp - time.time())
        return left if left > 0 else 0

    def keys(self, prefix: str = "") -> list[str]:
        now = time.time()
        with self._lock:
            self._purge(now)
            return [k for k in self._data if k.startswith(prefix)]

    def ping(self) -> bool:
        return True

    def stats(self) -> dict:
        with self._lock:
            self._purge(time.time())
            return {"backend": self.name, "keys": len(self._data)}


# ---------------- Redis 后端 ----------------
class RedisBackend:
    """redis-py 同步客户端。未安装 redis 包时构造会抛 ImportError，由工厂捕获降级。"""

    name = "redis"

    def __init__(self, url: str, prefix: str = "") -> None:
        import redis  # 延迟导入：没装包也不影响程序启动

        self._prefix = prefix
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
        self._client.ping()  # 连不上就立刻抛，让工厂降级

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}" if self._prefix else key

    def get(self, key: str) -> str | None:
        return self._client.get(self._k(key))

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._client.set(self._k(key), str(value), ex=ttl or None)

    def delete(self, *keys: str) -> int:
        return int(self._client.delete(*[self._k(k) for k in keys]) or 0)

    def incr(self, key: str, ttl: int | None = None) -> int:
        k = self._k(key)
        pipe = self._client.pipeline()
        pipe.incr(k)
        if ttl:
            pipe.expire(k, ttl, nx=True)  # 只在第一次设置过期，避免每次失败都续期
        return int(pipe.execute()[0])

    def ttl(self, key: str) -> int:
        return int(self._client.ttl(self._k(key)))

    def keys(self, prefix: str = "") -> list[str]:
        pattern = f"{self._k(prefix)}*"
        out = []
        for item in self._client.scan_iter(match=pattern, count=200):
            out.append(item[len(self._prefix):] if self._prefix else item)
        return out

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    def stats(self) -> dict:
        info: dict[str, Any] = {"backend": self.name}
        try:
            raw = self._client.info("memory")
            info["used_memory_human"] = raw.get("used_memory_human", "")
            info["keys"] = int(self._client.dbsize())
        except Exception:  # noqa: BLE001
            pass
        return info


# ---------------- 工厂 ----------------
_backend: MemoryBackend | RedisBackend | None = None
_backend_lock = threading.Lock()
_degrade_reason = ""


def _build() -> MemoryBackend | RedisBackend:
    global _degrade_reason
    url = _CFG.redis_url
    if not url:
        _degrade_reason = "未配置 REDIS_URL，使用进程内内存（单进程有效，重启清空）"
        return MemoryBackend()
    try:
        return RedisBackend(url, _CFG.redis_prefix)
    except ImportError:
        _degrade_reason = "未安装 redis 包（pip install redis），已降级为内存"
        return MemoryBackend()
    except Exception as exc:  # noqa: BLE001
        _degrade_reason = f"Redis 连接失败（{type(exc).__name__}: {str(exc)[:80]}），已降级为内存"
        return MemoryBackend()


def backend() -> MemoryBackend | RedisBackend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _build()
    return _backend


def reset_backend(force: MemoryBackend | RedisBackend | None = None) -> None:
    """测试用：丢弃当前后端。传 force 可强制指定实现。"""
    global _backend, _degrade_reason
    with _backend_lock:
        _backend = force
        _degrade_reason = ""


# ---------------- 门面 ----------------
def get(key: str) -> str | None:
    try:
        return backend().get(key)
    except Exception:  # noqa: BLE001
        return None


def set(key: str, value: str, ttl: int | None = None) -> None:  # noqa: A001
    try:
        backend().set(key, value, ttl)
    except Exception:  # noqa: BLE001
        pass


def delete(*keys: str) -> int:
    try:
        return backend().delete(*keys)
    except Exception:  # noqa: BLE001
        return 0


def incr(key: str, ttl: int | None = None) -> int:
    try:
        return backend().incr(key, ttl)
    except Exception:  # noqa: BLE001
        return 0


def ttl(key: str) -> int:
    try:
        return backend().ttl(key)
    except Exception:  # noqa: BLE001
        return 0


def get_int(key: str, default: int = 0) -> int:
    raw = get(key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def get_json(key: str) -> Any | None:
    import json

    raw = get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    import json

    try:
        set(key, json.dumps(value, ensure_ascii=False), ttl)
    except (TypeError, ValueError):
        pass


def health() -> dict:
    """给 /api/system/health 用：报告真实后端与降级原因，不隐藏问题。"""
    be = backend()
    ok = False
    try:
        ok = bool(be.ping())
    except Exception:  # noqa: BLE001
        ok = False
    info: dict[str, Any] = {
        "enabled": _CFG.redis_enabled,
        "url": live_url(),
        "backend": be.name,
        "connected": ok,
        "degraded": be.name != "redis",
        "note": _degrade_reason,
    }
    try:
        info.update(be.stats())
    except Exception:  # noqa: BLE001
        pass
    return info


def live_url() -> str:
    return _CFG.redacted_url(_CFG.redis_url)


# ---------------- 登录限流 ----------------
def login_blocked(username: str, ip: str = "") -> int:
    """返回还需等待的秒数；0 表示可以尝试登录。

    两个维度、两套阈值：
      - 用户名：连续错 LOGIN_MAX_FAILS 次锁定（防盯住一个账号猛试）
      - IP：错到 5 倍才锁定（防撞库扫号），阈值放大是为了不误伤同一个
        出口 NAT 后面的整个办公室——一个人手抖几次不该让全员登不上。
    """
    limit = max(1, _CFG.login_max_fails)
    for key in _guard_keys(username, ip):
        cap = limit if key.startswith("login:user:") else limit * 5
        fails = get_int(key, 0)
        if fails >= cap:
            return max(1, ttl(key))
    return 0


def record_login_failure(username: str, ip: str = "") -> int:
    """记一次失败，返回该用户名累计失败次数。"""
    count = 0
    for key in _guard_keys(username, ip):
        value = incr(key, _CFG.login_lock_seconds)
        if key.startswith("login:user:"):
            count = value
    return count


def clear_login_failures(username: str, ip: str = "") -> None:
    """登录成功后清零。

    用户名计数必清；IP 计数也清（否则一次误锁要等 5 分钟才恢复，
    用户会以为系统坏了）。真正防爆破仍由用户名维度兜底。
    """
    delete(*_guard_keys(username, ip))


def _guard_keys(username: str, ip: str) -> list[str]:
    keys = [f"login:user:{(username or '').strip().lower()[:64]}"]
    if ip:
        keys.append(f"login:ip:{ip}")
    return keys


def login_lock_seconds() -> int:
    return _CFG.login_lock_seconds
