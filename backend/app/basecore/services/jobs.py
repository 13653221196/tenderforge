"""轻量后台任务队列：线程池 + SQLite 状态表。

设计（对标易标任务中心 / BidMaster task_manager，clean-room 重写）：
- 一个任务 = jobs 表一行（kind/payload/status/progress/step/result/error）；
- 状态机：queued → running → done / error；运行中可 pausing → paused → running（恢复），
  任意时刻可 canceling → canceled；
- 控制流全部走数据库状态，不存内存标志：任务在阶段间调用 checkpoint()，
  发现 canceling 就抛 JobCanceled 收尾，发现 pausing 就把状态写成 paused 并轮询等待；
  好处是服务重启后状态仍然自洽（启动时把 running/pausing 统一标为 error 中断）；
- 执行函数（runner）由 main.py 启动时注册进 RUNNERS，避免循环 import。
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .. import db

RUNNERS: dict[str, dict] = {}  # kind -> {"fn": fn, "label": str}
_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()

MAX_WORKERS = 2


class JobCanceled(RuntimeError):
    """任务被用户取消，runner 捕获后直接收尾。"""


def register(kind: str, fn: Callable, label: str = "") -> None:
    RUNNERS[kind] = {"fn": fn, "label": label or kind}


def runner_fn(kind: str) -> Callable | None:
    item = RUNNERS.get(kind)
    return item["fn"] if item else None


def start() -> None:
    """进程启动：恢复中断任务并开线程池。"""
    global _pool
    db.recover_jobs()
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="job")


def submit(kind: str, title: str, payload: dict, enterprise_id: str = "") -> dict:
    if kind not in RUNNERS:
        raise ValueError(f"未知任务类型：{kind}")
    job = db.create_job(kind=kind, title=title, payload=payload, enterprise_id=enterprise_id)
    _dispatch(job["id"], kind)
    return job


def _dispatch(job_id: str, kind: str) -> None:
    with _lock:
        pool = _pool
    if pool is None:
        start()
        with _lock:
            pool = _pool
    from ... import ctxpool
    ctxpool.submit(pool, _run, job_id, kind)


def resume(job_id: str) -> None:
    """恢复已暂停的任务：状态改回 running，等待中的线程轮询到后继续。"""
    job = db.get_job(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job["status"] != "paused":
        raise ValueError("只有已暂停的任务可以恢复")
    db.update_job(job_id, status="running")


def _run(job_id: str, kind: str) -> None:
    job = db.get_job(job_id)
    if not job or job["status"] not in ("queued", "running"):
        return
    db.update_job(job_id, status="running", step="开始")
    item = RUNNERS[kind]
    fn = item["fn"]

    def report(progress: int, step: str = "") -> None:
        db.update_job(job_id, progress=max(0, min(100, int(progress))), step=step)

    def checkpoint() -> None:
        """协作式控制点：取消/暂停都在这里生效。"""
        cur = db.get_job(job_id)
        if not cur:
            raise JobCanceled("任务记录丢失")
        status = cur["status"]
        if status in ("canceling", "canceled"):
            db.update_job(job_id, status="canceled", step="已取消")
            raise JobCanceled("已取消")
        if status == "pausing":
            db.update_job(job_id, status="paused", step=(cur["step"] or "") + "（已暂停）")
            # 轮询等待恢复/取消；服务重启时该线程随进程退出，状态由 recover_jobs 收拾
            import time

            while True:
                time.sleep(1.0)
                cur = db.get_job(job_id)
                if not cur:
                    raise JobCanceled("任务记录丢失")
                if cur["status"] in ("canceling", "canceled"):
                    db.update_job(job_id, status="canceled", step="已取消")
                    raise JobCanceled("已取消")
                if cur["status"] == "running":
                    return
                # 仍是 paused → 继续等

    try:
        result = fn(job_id, job.get("payload") or {}, report, checkpoint)
        cur = db.get_job(job_id)
        if cur and cur["status"] in ("running", "pausing"):
            db.update_job(
                job_id, status="done", progress=100, step="完成",
                result=result if isinstance(result, str) else _dumps(result),
            )
    except JobCanceled:
        pass  # checkpoint 已经把状态写好了
    except Exception as exc:  # noqa: BLE001
        cur = db.get_job(job_id)
        if cur and cur["status"] not in ("canceled", "canceling"):
            db.update_job(job_id, status="error", error=str(exc)[:800], step="失败",
                          trace=traceback.format_exc(limit=6)[:1500])


def _dumps(data) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)
