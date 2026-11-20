"""多企业使用的合规边界：同一标段不得为多个投标人生成标书。

《招标投标法实施条例》第 40 条把「不同投标人的投标文件异常一致」列为
视为串通投标的情形之一；2026 年起围串标识别在部分省市已全覆盖。系统要给
多家企业用，就不能只做「每家企业看不到别家数据」，还得在平台侧识别
「同一份招标文件被多家企业拿来生成标书」这一红线。

隔离边界（重要）：
- 物理分库仍然存在，本模块**绝不打开别家企业的数据库、绝不读取别家正文**；
- 平台索引只登记招标正文的不可逆指纹与企业 ID，不含项目 id / 时间；
  各企业自己的明细只落在自己的企业目录，别家读不到；
- 不记项目名、投标人名称、章节正文或任何资料内容；
- 跨项目正文雷同只在**本企业当前库内**比对，返回相似度不返回对方项目名。

结论是「提醒 + 留痕」，不代替法律判断：知情导出仍可下载，但风险随产物登记，
不会被写成「已通过合规检查」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, scope, store
from .basecore.services import dupcheck

# 平台级索引：只登记 (招标指纹, 企业 ID)，用于「同一标段出现几家企业」的
# 数量提醒。不存项目 id、时间、项目名、投标人名称或任何正文。
FILE = config.DATA_DIR / "compliance" / "tender_registry.json"
# 本企业明细（含项目 id / 时间）只落在自己的企业目录，别家读不到。
ORG_FILE_NAME = "tender_projects.json"
_LOCK = threading.Lock()


def org_file(oid: str) -> Path:
    if scope.is_platform(oid):
        return config.DATA_DIR / "compliance" / ORG_FILE_NAME
    return scope.org_dir(oid) / "compliance" / ORG_FILE_NAME
# 真实招标文件动辄数万字；120 字足以排除「一句话占位项目」互相误报，
# 也不会把构造用例 / 短版邀请书排除在合规比对之外
_MIN_TENDER_CHARS = 120
_DICE_HIGH = 0.75
_MAX_LOCAL_PROJECTS = 12
_MAX_SECTIONS = 8

SAME_TENDER_NOTE = (
    "检测到其他企业已就同一份招标文件建立项目。若这些企业是同一标段的"
    "不同投标人，由本系统批量生成标书可能构成《招标投标法实施条例》第 40 条"
    "所指的投标文件异常一致。请确认本项目只代表一个投标主体、且与其他"
    "投标人的编制过程相互独立；必要时停止生成并人工复核。")


def tender_fingerprint(text: str) -> str:
    """招标正文指纹：去空白与页标记，抗同一文件的不同排版；不存原文。"""
    s = re.sub(r"【第\d+页】", "", str(text or ""))
    s = re.sub(r"[\s\u3000]+", "", s)
    if len(s) < _MIN_TENDER_CHARS:
        return ""
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:32]


def _index_entries(rows: list) -> list[dict]:
    """旧版台账带 pid / 时间；读取与回写时一律降成 (fp, org) 两个字段。"""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in rows or []:
        if not isinstance(e, dict):
            continue
        fp, org = str(e.get("fp") or ""), str(e.get("org") or "default")
        if not fp or (fp, org) in seen:
            continue
        seen.add((fp, org))
        out.append({"fp": fp, "org": org})
    return out[-2000:]


def _load() -> dict:
    try:
        raw = json.loads(FILE.read_text("utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            return {"version": 2, "entries": _index_entries(raw["entries"])}
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 2, "entries": []}


def _save(data: dict) -> None:
    """原子替换，且每个写者用独立临时名：多进程同时写不会抢同一个 .tmp。"""
    clean = {"version": 2, "entries": _index_entries(data.get("entries") or [])}
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(f"{FILE.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(clean, ensure_ascii=False), "utf-8")
        tmp.replace(FILE)
    finally:
        try:
            tmp.unlink()          # replace 成功后文件已不存在，忽略即可
        except OSError:
            pass


def _org_load(oid: str) -> dict:
    p = org_file(oid)
    try:
        raw = json.loads(p.read_text("utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "entries": []}


def _org_save(oid: str, data: dict) -> None:
    p = org_file(oid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(p)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _file_lock():
    """跨进程互斥锁：O_EXCL 建锁文件 + 短自旋；进程崩溃留下的陈旧锁自动清。

    Windows / Linux 都可用，不依赖 fcntl。拿不到锁时仍尽力登记（宁可偶发
    覆盖，也不能漏掉一条红线记录），正常部署下 5 秒自旋足够等完别的进程。
    """
    FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = FILE.with_suffix(".lock")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd, lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    return None, lock


def register(fp: str, org_id: str, pid: str) -> None:
    """登记一次「某企业某项目用了哪份招标文件」，重复登记不新增行。

    明细（fp / pid / 时间）只写本企业目录；平台索引只留 (fp, org)，
    用于跨企业数量提醒，不再把各家的项目 id 集中放在一个全局文件里。
    """
    if not fp:
        return
    org = str(org_id or "default")
    with _LOCK:
        fd, lock = _file_lock()
        try:
            mine = _org_load(org)
            rows = [e for e in mine["entries"] if isinstance(e, dict)]
            if not any(e.get("fp") == fp and e.get("pid") == str(pid) for e in rows):
                rows.append({"fp": fp, "pid": str(pid),
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                mine["entries"] = rows[-2000:]
                try:
                    _org_save(org, mine)
                except OSError:
                    pass
            # 每次登记都回写一遍索引：顺便把旧版台账里残留的 pid / 时间清洗掉
            data = _load()
            idx = data["entries"]
            if not any(e.get("fp") == fp and e.get("org") == org for e in idx):
                idx.append({"fp": fp, "org": org})
            data["entries"] = idx[-2000:]
            try:
                _save(data)
            except OSError:
                pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    lock.unlink()
                except OSError:
                    pass


def _local_duplicates(pid: str, fp: str) -> tuple[list[dict], int]:
    """本企业库内：同一招标指纹的其它项目数 + 跨项目正文雷同段落。

    只查当前企业库，别的企业的正文一个字都不读。
    """
    mine = [s for s in store.list_sections(pid)
            if len(str(s.get("text") or "").strip()) > 80]
    mine_norm = [dupcheck.normalize(str(s.get("text") or "")[:1500])
                 for s in mine[:_MAX_SECTIONS]]
    same_fp_projects = 0
    pairs: list[dict] = []
    others = [p for p in store.list_projects()
              if str(p.get("id") or "") != pid][:_MAX_LOCAL_PROJECTS]
    for other in others:
        other_fp = tender_fingerprint(str(other.get("tender_text") or ""))
        if fp and other_fp == fp:
            same_fp_projects += 1
        if not mine_norm:
            continue   # 本项目还没写正文时，同标段统计照做，雷同比对跳过
        rows = [s for s in store.list_sections(str(other["id"]))
                if len(str(s.get("text") or "").strip()) > 80][:_MAX_SECTIONS]
        best = 0.0
        for r in rows:
            n = dupcheck.normalize(str(r.get("text") or "")[:1500])
            for m in mine_norm:
                try:
                    best = max(best, dupcheck.dice(m, n))
                except Exception:
                    continue
        if best >= _DICE_HIGH:
            pairs.append({"similarity": round(best, 3),
                          "same_tender": bool(fp and other_fp == fp),
                          "other_project": "本企业另一项目"})
    pairs.sort(key=lambda x: -x["similarity"])
    return pairs[:5], same_fp_projects


def audit(pid: str, *, do_register: bool = True) -> dict:
    """只读结论 + 一次台账登记；任何异常都不能被当成「检查通过」。"""
    out: dict[str, Any] = {"pid": pid, "risk": "none", "checked": False,
                           "other_orgs": 0, "same_org_projects": 0,
                           "duplicate_pairs": [], "issues": [],
                           "fingerprint": ""}
    try:
        p = store.get_project(pid) or {}
        fp = tender_fingerprint(str(p.get("tender_text") or ""))
        out["fingerprint"] = fp
        if not fp:
            out["note"] = "本项目还没有可用的招标文件正文，未做同标段比对"
            out["checked"] = True
            return out
        org_id = scope.org()
        if do_register:
            register(fp, org_id, pid)
        other_orgs = {str(e.get("org") or "") for e in _load()["entries"]
                      if isinstance(e, dict) and e.get("fp") == fp
                      and str(e.get("org") or "") != org_id}
        pairs, same_org = _local_duplicates(pid, fp)
        out.update({"checked": True, "other_orgs": len(other_orgs),
                    "same_org_projects": same_org, "duplicate_pairs": pairs})
        if other_orgs:
            out["risk"] = "high"
            out["issues"].append({
                "key": "compliance_same_tender", "level": "高", "blocking": True,
                "title": "合规红线 · 同一招标文件出现多个企业项目",
                "detail": SAME_TENDER_NOTE,
                "action": "确认投标主体唯一性，必要时停止生成并交由合规人员复核",
                "evidence": "平台匿名招标指纹台账（不含任何正文）"})
        elif same_org or any(x.get("same_tender") for x in pairs):
            out["risk"] = "medium"
            out["issues"].append({
                "key": "compliance_same_org", "level": "中", "blocking": False,
                "title": "本企业存在同一招标文件的其它项目",
                "detail": (f"本企业另有 {same_org} 个项目使用同一招标文件。"
                           "若代表不同投标人，须立即停止并人工确认；同一投标主体的"
                           "历史版本可忽略本提醒。"),
                "action": "核对项目归属与投标主体", "evidence": "本企业项目列表"})
        for i, pair in enumerate(pairs):
            if pair.get("same_tender") and out["risk"] == "medium":
                continue   # 已由上面一条覆盖，不重复报
            out["issues"].append({
                "key": f"compliance_dup_{i}", "level": "中", "blocking": False,
                "title": "与旧标书存在高度相似段落",
                "detail": (f"本企业另一项目与本项目正文相似度约 "
                           f"{pair['similarity'] * 100:.0f}%。复用自有模板属正常，"
                           "但同一标段不同投标人的正文高度一致会被视为异常一致，"
                           "请核对是否为不同投标主体。"),
                "action": "人工核对雷同段落与项目归属",
                "evidence": "本企业库内章节 Dice 相似度"})
        if not out["issues"]:
            out["note"] = "未发现同标段多企业生成或跨项目高雷同正文"
        return out
    except Exception as exc:
        out["issues"] = [{
            "key": "compliance_unavailable", "level": "高", "blocking": True,
            "title": "合规检查未完成，不能当作已通过",
            "detail": (f"防雷同比对没有跑完（{type(exc).__name__}: {str(exc)[:120]}）；"
                       "多企业使用场景下不能把没查过写成没问题。"),
            "action": "重试体检", "evidence": "backend/app/compliance.py::audit"}]
        out["risk"] = "unknown"
        return out


def export_issues(pid: str) -> list[dict]:
    """接入导出门禁：红线永远列为重点提醒，知情导出也留痕。"""
    return list(audit(pid).get("issues") or [])


def status(pid: str) -> dict:
    """界面只读展示；不暴露对方企业 ID、项目名或任何正文。"""
    r = audit(pid, do_register=False)
    return {k: r.get(k) for k in ("risk", "checked", "other_orgs",
                                  "same_org_projects", "duplicate_pairs",
                                  "issues", "note", "fingerprint")}
