"""C04｜一致性备份、隔离恢复与恢复演练。

对应评审要求：「备份覆盖数据库、账号、原件、输出和配置；使用 SQLite 一致性
备份方法，进行隔离恢复演练」。

命令（在项目根执行，用系统 python，不需要 venv）：

    python -m backend.ops.backup create  [--out DIR] [--no-env]
    python -m backend.ops.backup verify  BACKUP.zip
    python -m backend.ops.backup restore BACKUP.zip --target DIR
    python -m backend.ops.backup drill   BACKUP.zip [--report FILE]

设计边界，写清楚免得误用：

- 数据库不直接 copy 文件。WAL 模式下 copy ``app.db`` 会漏掉还没 checkpoint
  的写入，等于拿到一个半截库；这里用 SQLite Online Backup API
  (``Connection.backup``) 生成事务一致快照，``-wal`` / ``-shm`` 不进包。
- 恢复是**隔离恢复**：只写到一个空的临时/演练目录，绝不写回生产数据目录，
  也不覆盖目标里已有的库。要真正切流量，由管理员手工决定，脚本不代劳。
- 这是单机本地磁盘的一致性快照。备份瞬间之后的新写入不在包里；多进程、
  多机或共享存储部署需要停机窗口或改用存储层快照，本脚本不宣称覆盖。
- 备份包含 ``models.json`` / ``.env`` 里的密钥，必须按密钥管理存放，不要发群里。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from ..app import config

MANIFEST = "manifest.json"
VERSION = 1
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git"})
SKIP_SUFFIXES = ("-wal", "-shm", ".tmp", ".json.tmp", ".pyc")
SECRET_HINT = ("备份包含模型密钥与账号数据（models.json / auth.db / .env），"
               "请按密钥管理存放，不要外发")


def data_dir() -> Path:
    """统一从 config 取，.env 与环境变量一处生效（R08）。"""
    return Path(config.DATA_DIR).resolve()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_db(path: Path) -> bool:
    return path.suffix.lower() == ".db"


def _skip(path: Path) -> bool:
    return (any(p in SKIP_DIRS for p in path.parts)
            or any(str(path).endswith(s) for s in SKIP_SUFFIXES))


def _snapshot_db(path: Path) -> bytes:
    """用 Online Backup API 取一致快照；返回完整 .db 文件字节。"""
    work = Path(tempfile.mkdtemp(prefix="bidagent_bksnap_"))
    out = work / "snapshot.db"
    src = dst = None
    try:
        src = sqlite3.connect(str(path), timeout=30)
        dst = sqlite3.connect(str(out), timeout=30)
        src.backup(dst)          # WAL 下同样是某一事务边界的完整快照
        dst.close()
        dst = None
        src.close()
        src = None
        return out.read_bytes()
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        shutil.rmtree(work, ignore_errors=True)


def _db_summary(data: bytes) -> dict:
    """快照的表清单与行数，作为恢复演练的对账依据（不含任何正文或账号内容）。"""
    work = Path(tempfile.mkdtemp(prefix="bidagent_bksum_"))
    f = work / "x.db"
    try:
        f.write_bytes(data)
        with sqlite3.connect(str(f)) as c:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            rows = {}
            for n in names:
                try:
                    rows[n] = c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                except sqlite3.Error:
                    rows[n] = -1
        return {"integrity": integrity, "tables": len(names), "row_counts": rows}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _collect(root: Path, include_env: bool = True) -> list[dict]:
    """返回 [(zip 内路径, 实际源文件 or bytes, kind)]。"""
    items: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or _skip(p):
            continue
        rel = "data/" + p.relative_to(root).as_posix()
        items.append({"path": rel, "source": p,
                      "kind": "sqlite" if _is_db(p) else "file"})
    if include_env:
        for rel, p in (("env/.env", config.ROOT / ".env"),
                       ("env/backend.env", config.ROOT / "backend" / ".env")):
            if p.is_file():
                items.append({"path": rel, "source": p, "kind": "file"})
    return items


def create(*, out_dir: Path | None = None, include_env: bool = True,
           root: Path | None = None) -> dict:
    """生成一份带 manifest 的备份包；返回 {"path", "manifest", "manifest_bytes"}。"""
    root = Path(root).resolve() if root else data_dir()
    dest = Path(out_dir).resolve() if out_dir else Path.cwd()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_path = dest / f"bidagent-backup-{stamp}.zip"
    entries: list[dict] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for it in _collect(root, include_env):
            if it["kind"] == "sqlite":
                data = _snapshot_db(it["source"])
                info = _db_summary(data)
            else:
                data = it["source"].read_bytes()
                info = {}
            e = {"path": it["path"], "kind": it["kind"],
                 "sha256": _sha(data), "size": len(data), **info}
            entries.append(e)
            z.writestr(it["path"], data)
        manifest = {
            "version": VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_data_dir": str(root),
            "method": "SQLite Online Backup API 一致性快照 + 其余文件原样打包",
            "entries": entries,
            "databases": [e["path"] for e in entries if e["kind"] == "sqlite"],
            "excluded": ["*-wal", "*-shm", "*.tmp", "__pycache__"],
            "warnings": [SECRET_HINT,
                         "备份瞬间之后的写入不在包内；需要更严的恢复点请停机后备份"],
            "capability_note": [
                "单机本地磁盘快照；多进程 / 多机 / 共享存储需停机窗口或存储层快照",
                "恢复只写隔离目录，脚本不会回写生产数据目录",
            ],
        }
        raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        z.writestr(MANIFEST, raw)
    return {"path": zip_path, "manifest": manifest, "manifest_bytes": raw}


def _safe_rel(name: str) -> str:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name:
        raise ValueError(f"备份包里的路径不安全：{name}")
    return p.as_posix()


def verify(path: Path, *, manifest: dict | None = None) -> dict:
    """逐条核对 sha256，并对每个数据库快照跑 integrity_check。"""
    path = Path(path)
    problems: list[str] = []
    checked = 0
    dbs: list[dict] = []
    with zipfile.ZipFile(path) as z:
        raw = z.read(MANIFEST)
        m = manifest or json.loads(raw.decode("utf-8"))
        declared = {e["path"] for e in m["entries"]}
        actual = {n for n in z.namelist() if n != MANIFEST}
        for extra in sorted(actual - declared):
            problems.append(f"包里有 manifest 未登记的文件：{extra}")
        for e in m["entries"]:
            try:
                data = z.read(e["path"])
            except KeyError:
                problems.append(f"缺文件：{e['path']}")
                continue
            if _sha(data) != e["sha256"] or len(data) != e["size"]:
                problems.append(f"内容摘要不符：{e['path']}")
                continue
            checked += 1
            if e["kind"] == "sqlite":
                info = _db_summary(data)
                if info["integrity"] != "ok":
                    problems.append(f"SQLite 完整性检查失败：{e['path']}")
                if info["row_counts"] != e.get("row_counts"):
                    problems.append(f"表行数与 manifest 不符：{e['path']}")
                dbs.append({"path": e["path"], **info})
    return {"ok": not problems, "checked": checked,
            "total": len(m["entries"]), "databases": dbs,
            "problems": problems}


def restore(path: Path, target: Path, *, root: Path | None = None) -> dict:
    """隔离恢复到空目录；绝不回写生产库、绝不覆盖目标里已有的库。"""
    path, target = Path(path), Path(target).resolve()
    live = Path(root).resolve() if root else data_dir()
    if (target == live or live.is_relative_to(target)
            or target.is_relative_to(live)):
        return {"ok": False, "code": "refuse_live",
                "reason": "拒绝恢复到生产数据目录、其上级或其子目录；恢复只能写隔离目录"}
    vr = verify(path)
    if not vr["ok"]:
        return {"ok": False, "code": "bad_backup",
                "reason": "备份包校验失败，未恢复任何文件", "verify": vr}
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            return {"ok": False, "code": "target_not_empty",
                    "reason": "目标目录非空；为避免覆盖现有数据，请换一个空目录"}
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        m = json.loads(z.read(MANIFEST).decode("utf-8"))
        for e in m["entries"]:
            rel = _safe_rel(e["path"])
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(e["path"]))
    # 恢复后再验一次：落盘过程没有写坏库、没有写错文件
    checks = []
    for e in m["entries"]:
        f = target / _safe_rel(e["path"])
        good = f.is_file() and _sha(f.read_bytes()) == e["sha256"]
        if good and e["kind"] == "sqlite":
            with sqlite3.connect(str(f)) as c:
                good = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        checks.append({"path": e["path"], "ok": good})
    bad = [c["path"] for c in checks if not c["ok"]]
    return {"ok": not bad, "code": "" if not bad else "restore_mismatch",
            "target": str(target), "files": len(checks), "bad": bad,
            "verify": vr,
            "start_hint": (f"用 BIDAGENT_DATA={target / 'data'} 启动一个隔离实例，"
                           "确认无误后再由管理员决定是否切换；脚本不会自动切流量"),
            "note": "恢复结果只证明文件与数据库能读回，不等于业务验收通过"}


def drill(path: Path, *, report: Path | None = None, root: Path | None = None) -> dict:
    """隔离恢复演练：临时目录恢复 → 打开库对账 → 清理，输出可归档的报告。"""
    work = Path(tempfile.mkdtemp(prefix="bidagent_drill_"))
    try:
        r = restore(path, work, root=root)
        opened: dict = {}
        if r.get("ok"):
            with zipfile.ZipFile(path) as z:
                m = json.loads(z.read(MANIFEST).decode("utf-8"))
            for e in m["entries"]:
                if e["kind"] != "sqlite":
                    continue
                f = work / _safe_rel(e["path"])
                with sqlite3.connect(str(f)) as c:
                    names = [x[0] for x in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'")]
                    now = {}
                    for n in names:
                        try:
                            now[n] = c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                        except sqlite3.Error:
                            now[n] = -1
                opened[e["path"]] = {
                    "integrity": "ok" if now == e.get("row_counts") else "mismatch",
                    "tables": len(names),
                    "match_manifest": now == e.get("row_counts"),
                }
        out = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backup": str(Path(path).resolve()),
            "restore_ok": bool(r.get("ok")),
            "restore_code": r.get("code", ""),
            "files_restored": r.get("files", 0),
            "verify": {"checked": r.get("verify", {}).get("checked", 0),
                       "problems": r.get("verify", {}).get("problems", [])},
            "databases_opened": opened,
            "conclusion": ("隔离恢复演练通过：数据库一致、文件摘要全部匹配"
                           if r.get("ok") and all(
                               x["match_manifest"] for x in opened.values())
                           else "隔离恢复演练未通过，见 restore_code / verify.problems"),
            "boundary": "这只证明备份能读回，不等于业务验收通过；真实项目的业务验收须由业务人员用脱敏样本复核，见 docs/验收报告.md",
        }
        if report:
            rp = Path(report)
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TenderForge：备份 / 隔离恢复 / 恢复演练")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="生成一致性备份包")
    c.add_argument("--out", default="", help="备份包输出目录，默认当前目录")
    c.add_argument("--no-env", action="store_true", help="不打包项目根 .env")
    v = sub.add_parser("verify", help="校验备份包")
    v.add_argument("backup")
    r = sub.add_parser("restore", help="隔离恢复到空目录")
    r.add_argument("backup")
    r.add_argument("--target", required=True)
    d = sub.add_parser("drill", help="隔离恢复演练（临时目录，跑完即删）")
    d.add_argument("backup")
    d.add_argument("--report", default="", help="把演练报告写成 JSON 文件")
    a = ap.parse_args(argv)
    if a.cmd == "create":
        out = create(out_dir=Path(a.out) if a.out else None, include_env=not a.no_env)
        print(f"备份完成：{out['path']}")
        print(f"文件 {len(out['manifest']['entries'])} 个，"
              f"数据库 {len(out['manifest']['databases'])} 个 → "
              f"{', '.join(out['manifest']['databases']) or '(无)'}")
        print(SECRET_HINT)
        return 0
    if a.cmd == "verify":
        res = verify(Path(a.backup))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    if a.cmd == "restore":
        res = restore(Path(a.backup), Path(a.target))
        print(json.dumps({k: v for k, v in res.items() if k != "verify"},
                         ensure_ascii=False, indent=2))
        if res.get("ok"):
            print(res["start_hint"])
        return 0 if res["ok"] else 1
    res = drill(Path(a.backup), report=Path(a.report) if a.report else None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["restore_ok"] and res["verify"]["checked"] else 1


if __name__ == "__main__":
    sys.exit(main())
