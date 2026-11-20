"""跨线程池携带当前请求的上下文（企业 / 用户 / 分库）。

`contextvars` 不会随 `ThreadPoolExecutor.submit` 自动传播。裸 submit 的子线程里
`auth.current()` 为空、`store.db_path()` 回落 `data/app.db`、`scope.org()` 回落
`default`，多企业时会把 B 企业的资料写进默认企业库、并读到平台模型配置。

项目里所有自建线程池统一走这里；`store.submit_job` 的实现与本模块等价。
本模块只依赖标准库，不导入任何业务模块，避免循环引用。
"""
from __future__ import annotations

import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


def require_business_context(project_id: str = "") -> None:
    """业务线程必须有显式企业库；登录身份存在时还须与企业库一致。"""
    from . import auth, store
    if store._db_ctx.get() is None:
        raise RuntimeError("业务执行缺失企业数据库上下文")
    me = auth.current()
    if me and store.db_path().resolve() != store.org_db(auth.org_id()).resolve():
        raise RuntimeError("登录企业与业务数据库不一致")
    if project_id and not store.get_project(project_id):
        raise ValueError("项目不属于当前企业或已不存在")


def submit_business(pool: ThreadPoolExecutor, fn: Callable, *args: Any,
                    project_id: str = "", **kwargs: Any) -> Future:
    require_business_context(project_id)

    def checked():
        require_business_context(project_id)
        return fn(*args, **kwargs)
    return submit(pool, checked)


def submit(pool: ThreadPoolExecutor, fn: Callable, *args: Any, **kwargs: Any) -> Future:
    """提交任务时复制调用方的上下文，子线程里企业作用域保持不变。"""
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


def run_sync(fn: Callable, *args: Any, timeout: float | None = None,
             **kwargs: Any) -> Any:
    """在一次性线程里同步跑完（用于需要独立事件循环的场景），同样带上下文。"""
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bidagent-ctx") as pool:
        return pool.submit(ctx.run, fn, *args, **kwargs).result(timeout=timeout)
