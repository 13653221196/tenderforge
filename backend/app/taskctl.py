"""C03 长任务控制：章节检查点、取消、失败章重试与中断恢复。

边界写清楚（不假装做不到的能力）：

- 取消是「协作式」的：在章节边界生效。已经发给模型的请求仍会在远端算完，
  结果返回后本系统不再写入正文；取消不等于撤销远端计算 / 退费。
- 恢复按检查点里的章节版本核对：任务开始后人工写过或改过的章节一律保留，
  绝不拿模型稿覆盖。
- 当前是单进程单线程池 + 进程内互斥锁；多进程部署前需要跨进程认领或锁，
  这一步尚未实现（见 store.concurrency_advice()）。
"""
from __future__ import annotations

from . import agent, store

DONE_STATES = frozenset({"written", "skipped", "manual", "human", "kept_human",
                         "undeliverable"})
RETRY_STATES = frozenset({"failed", "empty", "conflict", "cancelled", "interrupted"})
OPEN_STATES = frozenset({"pending", "running"}) | RETRY_STATES


def should_stop(jid: str) -> bool:
    """任务循环在每章边界读一次取消请求。"""
    return store.cancel_requested(jid)


def record(pid: str, jid: str, sid: str, status: str,
           ver: int = 0, note: str = "") -> None:
    store.save_checkpoint(pid, jid, sid, status, int(ver or 0), str(note or "")[:200])


def plan(pid: str, source_jid: str, mode: str = "resume") -> dict:
    """按检查点 + 当前正文 / 版本算出「哪些章可以继续、哪些必须保留人工稿」。"""
    cps = {c["section_id"]: c for c in store.list_checkpoints(pid, source_jid)}
    items: list[dict] = []
    for s in store.list_sections(pid):
        cp = cps.get(s["id"]) or {}
        status = str(cp.get("status") or "pending")
        base_ver = int(cp.get("ver") or 0)
        cur_ver = int(s.get("ver") or 0)
        has_text = bool(str(s.get("text") or "").strip())
        protected = bool(s.get("protected"))
        item = {"id": s["id"], "title": s.get("title") or "",
                "status": status, "base_ver": base_ver, "cur_ver": cur_ver,
                "has_text": has_text, "protected": protected,
                "has_checkpoint": bool(cp), "decision": "", "why": ""}
        if status in DONE_STATES:
            item["decision"], item["why"] = "done", "检查点显示这一章已处理"
        elif protected:
            item["decision"], item["why"] = "manual", "保护章节需人工填写"
        elif has_text:
            item["decision"], item["why"] = "kept_human", "已有正文（含任务后的人工补稿），不覆盖"
        elif cur_ver != base_ver and cp:
            item["decision"], item["why"] = "kept_human", "任务开始后章节版本变了，不覆盖"
        elif mode == "retry" and status in ("pending", "running"):
            item["decision"], item["why"] = "skip", "重试只处理失败 / 空稿 / 取消的章节"
        else:
            item["decision"], item["why"] = "resumable", "检查点显示未完成且正文仍为空"
        items.append(item)
    resumable = [x["id"] for x in items if x["decision"] == "resumable"]
    return {"source_job": source_jid, "mode": mode, "items": items,
            "resumable": resumable,
            "kept_human": [x["id"] for x in items if x["decision"] == "kept_human"],
            "done": [x["id"] for x in items if x["decision"] == "done"],
            "total": len(items),
            "note": "恢复按章节版本核对，任务开始后的人工稿一律保留；"
                    "取消不等于撤销已发出的远端模型计算"}


def report(pid: str, jid: str) -> dict:
    j = store.get_job(jid)
    cps = store.list_checkpoints(pid, jid)
    return {"job": j, "checkpoints": cps,
            "summary": {"total": len(cps),
                        "done": sum(1 for c in cps if c["status"] in DONE_STATES),
                        "open": sum(1 for c in cps if c["status"] in OPEN_STATES)},
            "recoverable": bool(j.get("recoverable")),
            "plan": plan(pid, jid, "resume"),
            "concurrency": store.concurrency_advice()}


def cancel(pid: str, jid: str) -> dict:
    j = store.get_job(jid)
    if not j or str(j.get("project_id") or "") != str(pid):
        raise ValueError("任务不存在或不属于这个项目")
    ok = store.request_cancel(jid)
    return {"ok": ok, "job_id": jid, "status": j.get("status"),
            "note": ("取消请求已记录：任务会在下一章边界停下，已发出的模型结果不再写入正文。"
                     "取消不等于撤销远端计算，也不退还在途请求"
                     if ok else "任务已经结束，没有可取消的执行")}


def start(pid: str, source_jid: str, mode: str = "resume") -> dict:
    """从检查点起一个新任务；同项目仍受原来的互斥约束。"""
    if mode not in ("resume", "retry"):
        raise ValueError("未知的恢复方式")
    j = store.get_job(source_jid)
    if not j or str(j.get("project_id") or "") != str(pid):
        raise ValueError("任务不存在或不属于这个项目")
    p = plan(pid, source_jid, mode)
    sids = p["resumable"]
    if not sids:
        return {"ok": False, "error": "没有可重试 / 恢复的章节", "plan": p}
    jid, busy = store.create_job_exclusive("compose", pid)
    if busy:
        return {"ok": False, "error": "本项目还有任务在跑，等它结束再恢复",
                "busy": busy, "plan": p}
    label = "失败章重试" if mode == "retry" else "中断恢复"
    store.update_job(jid, status="queued", step=f"{label}：{len(sids)} 章待处理")
    store.submit_job(agent.compose_subset, pid, jid, sids, source_jid, mode)
    return {"ok": True, "job_id": jid, "sections": len(sids),
            "kept_human": len(p["kept_human"]), "plan": p,
            "note": "按章节版本核对，人工稿不会覆盖；取消只在章节边界生效"}
