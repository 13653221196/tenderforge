"""可靠性回归：保存版本冲突、检查三态、暗标表格与文档属性、产出文件归属。

这些断言对应「可靠性整改」，跑法：

    python -m backend.eval.test_reliability

数据隔离：默认在系统临时目录下新建一个空数据目录（BIDAGENT_DATA），
跑完连目录一起删；已经显式设了 BIDAGENT_DATA 时不覆盖、也不删那个目录。
绝不写现有业务库。

退出码：全部通过 0，任一断言失败或测试自身抛错 1（不用 grep 判断结果）。
前端交互（慢网保存、切章阻断）无法在无浏览器环境下断言，见文件末尾的清单。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path

# 必须在 import app 模块之前落定数据目录：config 在导入时就绑定 DATA_DIR / DB_PATH
_OWNED_DATA = ""
if not (os.environ.get("BIDAGENT_DATA") or "").strip():
    _OWNED_DATA = tempfile.mkdtemp(prefix="bidagent_test_")
    os.environ["BIDAGENT_DATA"] = _OWNED_DATA

from ..app import auto_core, parsers, quality, settings, store  # noqa: E402


# ---------------- 测试脚手架 ----------------

TEMP_ROWS = ("messages", "agent_runs", "jobs", "section_versions", "checks",
             "evidence", "section_quality", "requirements", "requirement_sync",
             "requirement_suppressed", "artifacts", "outline_backups", "kb_docs",
             "source_pages", "sections", "originals", "tender_versions",
             "requirement_responses", "project_facts", "attachments",
             "manual_confirmations", "task_checkpoints", "project_rules",
             "project_members", "section_assignees")


def make_project(name: str, sections: list[dict]) -> str:
    p = store.create_project(name)
    pid = p["id"]
    store.set_sections(pid, sections)
    return pid


def drop_project(pid: str) -> None:
    with store.conn() as c:
        for t in TEMP_ROWS:
            try:
                c.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
            except Exception:
                pass
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


class Checker:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.passed = 0

    def ok(self, cond: bool, label: str, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}{('  → ' + detail) if detail else ''}")


# ---------------- A1：章节保存版本与冲突 ----------------

def test_section_version(chk: Checker) -> None:
    print("\n[A1] 章节保存：版本递增 + 冲突不覆盖")
    pid = make_project("自测-保存冲突", [{"title": "第一章 技术方案"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        r1 = store.update_section(sid, "稿A", base_ver=0)
        chk.ok(r1.get("ok") and r1.get("ver") == 1, "首次写入成功，版本 1", str(r1))

        # 另一个窗口拿着旧版本（0）来写：必须冲突，且不能覆盖稿A
        r2 = store.update_section(sid, "稿B", base_ver=0)
        chk.ok(r2.get("ok") is False and r2.get("error") == "conflict",
               "旧版本写入被拒（409 依据）", str(r2))
        chk.ok(r2.get("text") == "稿A", "冲突时返回服务端正文，供用户选择", str(r2.get("text")))
        chk.ok(store.get_section(sid)["text"] == "稿A", "冲突没有写坏库里的正文")

        # 拿对版本再写：成功
        r3 = store.update_section(sid, "稿A2", base_ver=1)
        chk.ok(r3.get("ok") and r3.get("ver") == 2, "按最新版本写入成功", str(r3))

        # 强制写（不带 base_ver）仍可用：生成 / 导入走这条
        r4 = store.update_section(sid, "AI 覆盖")
        chk.ok(r4.get("ok") and r4.get("ver") == 3, "无条件写（生成/导入）行为不变", str(r4))
    finally:
        drop_project(pid)


# ---------------- R02：并发保存只能有一个成功 ----------------

def test_section_atomic(chk: Checker) -> None:
    print("\n[R02] 并发保存：同一版本同时写，只能有一个成功")
    pid = make_project("自测-并发保存", [{"title": "第一章 技术方案"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, "初始稿", base_ver=0)      # ver -> 1
        results: list[dict] = []
        gate = threading.Barrier(2)

        def race(text: str) -> None:
            gate.wait()                                      # 两个线程同时冲到 UPDATE
            try:
                results.append(store.update_section(sid, text, base_ver=1))
            except Exception as exc:                          # 并发时直接抛错会变成 500
                results.append({"ok": False, "error": f"raised:{type(exc).__name__}: {exc}"})

        threads = [threading.Thread(target=race, args=(t,)) for t in ("甲稿", "乙稿")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        won = [r for r in results if r.get("ok")]
        lost = [r for r in results if not r.get("ok")]
        chk.ok(len(results) == 2, "两个并发请求都正常返回", str(results))
        chk.ok(len(won) == 1 and len(lost) == 1, "只有一个写成功，另一个被拒", str(results))
        chk.ok(all(r.get("error") == "conflict" for r in lost),
               "失败的一边是 conflict，不是异常或静默覆盖", str(lost))
        final = store.get_section(sid)
        chk.ok(int(final["ver"]) == 2, "版本只前进一步，没有双写", str(final["ver"]))
        chk.ok(final["text"] in ("甲稿", "乙稿"), "库里留下的是胜出的那一稿", final["text"])
    finally:
        drop_project(pid)


# ---------------- R07：章节必须属于路径上的项目 ----------------

def test_section_ownership(chk: Checker) -> None:
    print("\n[R07] 章节必须属于路径上的项目")
    from fastapi import HTTPException
    from ..app import main as app_main

    a = make_project("自测-归属A", [{"title": "第一章 A 的技术方案"}])
    b = make_project("自测-归属B", [{"title": "第一章 B 的技术方案"}])
    try:
        sid_b = store.get_project(b)["sections"][0]["id"]
        store.update_section(sid_b, "B 家正文", base_ver=0)
        before = store.get_section(sid_b)

        r = store.update_section(sid_b, "被 A 写进来", pid=a)
        chk.ok(r.get("error") == "not_in_project", "store 层拒绝跨项目写", str(r))
        chk.ok(r.get("text") == "", "拒绝时不回传别家正文", str(r.get("text")))
        after = store.get_section(sid_b)
        chk.ok(after["text"] == before["text"] and after["ver"] == before["ver"],
               "B 的正文与版本都没被改动", f"{after['text']} / v{after['ver']}")

        cases = [("保存", app_main.section_save, (a, sid_b, {"text": "x"})),
                 ("生成", app_main.section_generate, (a, sid_b)),
                 ("改写", app_main.section_rewrite, (a, sid_b, {"instruction": "x"}))]
        for label, fn, args in cases:
            try:
                fn(*args)
                chk.ok(False, f"{label}接口应拒绝别的项目的章节")
            except HTTPException as exc:
                chk.ok(exc.status_code == 404, f"{label}接口返回 404", str(exc.status_code))
            except Exception as exc:                        # 抛别的错也算不合格
                chk.ok(False, f"{label}接口应返回 404", f"{type(exc).__name__}: {exc}")

        sid_a = store.get_project(a)["sections"][0]["id"]
        ok = store.update_section(sid_a, "A 家正文", pid=a)
        chk.ok(ok.get("ok") is True, "正确的 pid + sid 组合照常写入", str(ok))
    finally:
        drop_project(a)
        drop_project(b)


# ---------------- A2：检查三态 ----------------

def test_check_unavailable(chk: Checker) -> None:
    print("\n[A2] 检查未完成不能算通过")
    pid = make_project("自测-检查异常", [{"title": "第一章 技术方案"}])
    real_assess = quality.assess
    real_issues = quality.export_issues
    real_stage = auto_core.run_stage
    try:
        def boom(*a, **k):
            raise RuntimeError("模拟检查模块异常")
        quality.assess = boom
        r = quality.assess_project(pid)
        chk.ok(r.get("status") == "unavailable", "异常 → status=unavailable", str(r.get("status")))
        chk.ok(r.get("all_passed") is False, "异常 → all_passed=False（不能是 True）",
               str(r.get("all_passed")))

        items = quality.export_issues(pid)
        blockers = [x for x in items if x.get("blocking")]
        chk.ok(bool(blockers) and any("未完成" in x["title"] for x in blockers),
               "导出门禁列出一条硬阻断「检查未完成」",
               str([x["title"] for x in blockers]))

        # 门禁整体判定：质量汇总抛错也必须 blocked
        quality.export_issues = boom
        auto_core.run_stage = lambda *a, **k: {"issues": [], "count": 0, "findings": 0}
        gate = auto_core.export_gate(pid)
        chk.ok(gate.get("blocked") is True, "质量汇总抛错 → 门禁 blocked=True",
               str(gate.get("blocked")))
        chk.ok(any("质量检查未完成" in x["title"] for x in gate.get("blockers") or []),
               "阻断项写明原因，不是静默放行")
    finally:
        quality.assess = real_assess
        quality.export_issues = real_issues
        auto_core.run_stage = real_stage
        drop_project(pid)


# ---------------- A4：暗标表格与文档属性 ----------------

def _docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts += [c.text for c in row.cells]
    return "\n".join(parts)


def _docx_core_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in ("docProps/core.xml", "docProps/app.xml"):
            if name in z.namelist():
                return z.read(name).decode("utf-8", "ignore")
    return ""


def test_blind_table(chk: Checker) -> None:
    print("\n[A4] 暗标：表格单元格 + 文档属性")
    from ..app import docx_export

    body = ("| 项目 | 内容 |\n| --- | --- |\n"
            "| 投标人 | 某某科技有限公司 |\n"
            "| 联系人 | 13800138000 |\n"
            "| 邮箱 | bid@example.com |\n")
    secs = [{"title": "第三章 商务响应表", "text": body}]

    blind = docx_export.build_docx("测试项目", secs, {"blind": True})
    text = _docx_text(blind)
    chk.ok("某某科技有限公司" not in text, "暗标表格里的公司名已隐去", text[:120])
    chk.ok("13800138000" not in text, "暗标表格里的手机号已隐去")
    chk.ok("bid@example.com" not in text, "暗标表格里的邮箱已隐去")

    normal = docx_export.build_docx("测试项目", secs, {})
    chk.ok("某某科技有限公司" in _docx_text(normal), "明标保留原内容，不受暗标替换影响")

    core = _docx_core_xml(blind)
    chk.ok("python-docx" not in core, "暗标清空文档属性作者（不出现 python-docx）", core[:200])


# ---------------- A3：产出文件归属 ----------------

def test_artifact_ownership(chk: Checker) -> None:
    print("\n[A3] 产出文件：登记后才可下载")
    pid = make_project("自测-产出归属", [{"title": "第一章 技术方案"}])
    try:
        exports = store.db_path().parent / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        f = exports / f"{pid}_selftest.docx"
        f.write_bytes(b"demo")

        chk.ok(store.get_artifact(f.name) == {}, "未登记的文件查不到（下载会 404）")
        store.record_artifact(pid, "review", f, name=f.name)
        art = store.get_artifact(f.name)
        chk.ok(art.get("project_id") == pid, "登记后可查到，且绑定项目")
        chk.ok(art.get("kind") == "review", "基座产出默认按审阅件登记")
        chk.ok(bool(art.get("content_hash")), "记录内容指纹，便于核对下载内容一致")

        f.unlink(missing_ok=True)
        chk.ok(not f.is_file(), "文件清理后记录仍在（接口按路径判 404）")
    finally:
        drop_project(pid)


# ---------------- 保护章节空着必须阻断 ----------------

def test_protected_empty(chk: Checker) -> None:
    print("\n[附加] 保护章节空着不能导出")
    pid = make_project("自测-保护章节", [
        {"title": "第一章 技术方案", "text": "本项目严格按照招标文件要求编制，覆盖技术方案、实施计划与售后保障。" * 3},
        {"title": "二、投标函", "text": "", "protected": True},
    ])
    try:
        status = {s["title"]: s for s in quality.section_status(pid)}
        chk.ok(not status.get("二、投标函", {}).get("deliverable", True),
               "空的投标函被标为不可交付")
        items = quality.export_issues(pid)
        chk.ok(any(x.get("blocking") and "投标函" in x["title"] for x in items),
               "导出时作为硬阻断列出，勾选确认也不能放行")
    finally:
        drop_project(pid)


# ---------------- A5：任务状态与「生成完成」的口径 ----------------

def test_job_states(chk: Checker) -> None:
    print("\n[A5] 任务：排队态、同项目互斥、部分完成不冒充完成")
    pid = make_project("自测-任务状态", [{"title": "第一章 技术方案"}])
    try:
        jid = store.create_job("compose", pid)
        chk.ok(store.get_job(jid)["status"] == "queued",
               "新任务先是「等待执行」，工作线程领取后才显示运行",
               str(store.get_job(jid)["status"]))
        jid2, busy = store.create_job_exclusive("compose", pid)
        chk.ok(jid2 == "" and busy.get("id") == jid,
               "同项目再点一次不新建任务，接上正在排队的那一个", str(busy))
        store.update_job(jid, status="failed", error="测试结束")
        jid3, busy3 = store.create_job_exclusive("compose", pid)
        chk.ok(bool(jid3) and not busy3, "上一个任务结束后可以正常起新任务")
        store.update_job(jid3, status="failed", error="测试结束")
    finally:
        drop_project(pid)


def test_compose_honesty(chk: Checker) -> None:
    print("\n[A5] 整本生成：空响应 / 版本冲突 / 成功分别计数")
    titles = ["第一章 技术方案", "第二章 售后服务", "第三章 项目实施"]
    pid = make_project("自测-整本口径", [{"title": t} for t in titles])
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles])
    # set_sections 只建结构不写正文，跳过逻辑要另写一章已有正文来验
    store.update_section(store.list_sections(pid)[0]["id"], "已有正文，应跳过")
    from ..app import agent
    real_llm, real_section = agent._llm, agent.generate_section
    real_stage = agent.auto_core.run_stage
    agent.auto_core.run_stage = lambda *a, **k: {}
    try:
        # ① 模型一个字都不返回：不能算写完
        agent._llm = lambda *a, **k: ""
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        j1 = store.get_job(jid)
        c1 = j1["result"].get("counts") or {}
        chk.ok(j1["status"] == "partial", "有空章 + 有跳过 → partial，而不是 done",
               f"{j1['status']} / {j1['step']}")
        chk.ok(j1["result"].get("generated") == 0, "模型空响应不计入「已写」",
               str(j1["result"].get("generated")))
        chk.ok(c1.get("empty") == 2 and c1.get("skipped") == 1,
               "空响应 2 章、跳过 1 章分别计数", str(c1))
        chk.ok(not any((s.get("text") or "").strip() for s in store.list_sections(pid)[1:]),
               "空响应没有在库里留下空稿或占位文字")

        # ② 生成期间用户改了这一章：保留用户的版本
        agent.generate_section = lambda pid_, sid, **kw: {
            "text": "", "conflict": True, "message": "生成期间被改过"}
        store.update_section(store.list_sections(pid)[1]["id"], "")
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        c2 = (store.get_job(jid)["result"].get("counts") or {})
        chk.ok(c2.get("conflict") == 2, "版本冲突单独计数，不混进失败", str(c2))
        chk.ok(store.get_job(jid)["status"] == "partial", "有冲突未落地 → partial")

        # ③ 正常写入：只有落库成功才算
        def fake_gen(pid_, sid, **kw):
            store.update_section(sid, "按招标文件要求编写的正文内容。" * 6)
            return {"text": "…", "quality": {"deliverable": True}}
        agent.generate_section = fake_gen
        for s in store.list_sections(pid)[1:]:
            store.update_section(s["id"], "")
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        j3 = store.get_job(jid)
        chk.ok(j3["status"] == "done" and j3["result"].get("generated") == 2,
               "真正落库的两章才计入完成", f"{j3['status']} / {j3['result'].get('generated')}")
        chk.ok(all((s.get("text") or "").strip() for s in store.list_sections(pid)),
               "完成后库里确有正文")
        hc = j3["result"].get("health") or {}
        chk.ok(bool(hc.get("checks")) and "自动体检" in str(j3["step"]),
               "整本生成结束自动出健康报告并写进任务结果")
    finally:
        agent._llm, agent.generate_section = real_llm, real_section
        agent.auto_core.run_stage = real_stage
        drop_project(pid)


# ---------------- R03：写操作统一保住人工新稿 ----------------

def test_write_conflict(chk: Checker) -> None:
    print("\n[R03] 生成 / 改写 / 恢复统一保护人工新稿与写任务互斥")
    from ..app import agent, main as app_main, provenance

    titles = ["第一章 技术方案", "第二章 售后服务", "第三章 项目实施"]
    pid = make_project("自测-R03写保护", [{"title": t} for t in titles])
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles])
    sids = [s["id"] for s in store.list_sections(pid)]
    sid = sids[0]

    saved = {name: getattr(agent, name) for name in
             ("_llm", "_selfcheck", "_fix_loop", "_record_quality")}
    saved_pv = {name: getattr(provenance, name) for name in
                ("gather", "attach", "save")}
    try:
        agent._selfcheck = lambda *a, **k: {"ok": True}
        agent._fix_loop = lambda pid_, p, sec, text, chk_, *state: (text, chk_, 0, True)
        agent._record_quality = lambda *a, **k: {"deliverable": True}
        provenance.gather = lambda *a, **k: []
        provenance.attach = lambda text, refs: text
        provenance.save = lambda *a, **k: None

        # ① 模型等待期间人工保存 B，模型返回 A 后 B 保留，结果为 conflict 且带候选稿
        store.update_section(sid, "初始稿", pid=pid)

        def slow_llm(*a, **k):
            from ..app import guard
            generation = k.get("generation")
            if generation:
                variables = a[2]
                generation.capture(a[1],variables,guard.guard_variables(variables),[],
                                   {"type":"test"})
                generation.finish("AI 生成稿 A")
            store.update_section(sid, "人工保存的新稿 B", pid=pid)
            return "AI 生成稿 A：按招标文件要求逐项响应，内容完整可核查。"

        agent._llm = slow_llm
        r = agent.generate_section(pid, sid)
        chk.ok(r.get("conflict") is True, "生成期间人工改过 → 明确报冲突，不说已生成成功",
               str(r)[:140])
        chk.ok("AI 生成稿 A" in (r.get("candidate") or ""),
               "未采用的模型候选稿原样退回给界面", str(r.get("candidate"))[:80])
        chk.ok(store.get_section(sid)["text"] == "人工保存的新稿 B",
               "人工稿 B 未被模型稿覆盖")
        chk.ok(not r.get("text"), "冲突时不把模型稿当成已落库正文")

        # ② 改写走同一规则
        store.update_section(sid, "待改写原文", pid=pid)
        r2 = agent.rewrite_section(pid, sid, "补充响应时限")
        chk.ok(r2.get("conflict") is True and "AI 生成稿 A" in (r2.get("candidate") or ""),
               "改写期间人工改过 → 同样报冲突并退回候选稿", str(r2)[:140])
        chk.ok(store.get_section(sid)["text"] == "人工保存的新稿 B",
               "改写冲突后正文仍是人工稿")

        # ③ 模型返回空：原稿完整，不先清空再写
        agent._llm = lambda *a, **k: ""
        before = store.get_section(sid)["text"]
        r3 = agent.generate_section(pid, sid)
        chk.ok(bool(r3.get("fallback")) and store.get_section(sid)["text"] == before,
               "模型空响应时原稿一字未动")

        # ④ 恢复快照推进版本，旧窗口拿旧版本保存被拒
        batch = store.new_id()
        store.snapshot_sections(pid, batch, "R03 恢复前", sids)
        store.update_section(sid, "恢复前刚改的正文", pid=pid)
        old_ver = int(store.get_section(sid)["ver"] or 0)
        rr = store.restore_batch(pid, batch)
        chk.ok(rr.get("ok") and not rr.get("partial"),
               "快照恢复成功且逐章报告", str(rr)[:140])
        chk.ok(int(store.get_section(sid)["ver"] or 0) == old_ver + 1,
               "普通恢复推进章节版本，旧窗口不再持有最新版")
        stale = store.update_section(sid, "旧窗口的旧稿", base_ver=old_ver, pid=pid)
        chk.ok(not stale.get("ok") and stale.get("error") == "conflict",
               "恢复后旧版本保存被拒绝", str(stale)[:120])

        # ⑤ 批量恢复部分成功：章节被删时跳过并说明，不谎报全部成功
        batch2 = store.new_id()
        store.snapshot_sections(pid, batch2, "R03 部分恢复", sids)
        with store.conn() as c:
            c.execute("DELETE FROM sections WHERE id=? AND project_id=?", (sids[2], pid))
        rp = store.restore_batch(pid, batch2)
        chk.ok(rp.get("partial") and rp.get("restored") == 2
               and len(rp.get("skipped") or []) == 1,
               "部分恢复逐章报告，不返回模糊的全部成功", str(rp)[:160])

        # ⑥ 目录恢复同时还原大纲、旧章节 id 明确失效
        store.set_sections(pid, [{"title": t} for t in titles])  # 补回上一步删掉的章节
        ob = store.new_id()
        old_ids = [s["id"] for s in store.list_sections(pid)]
        store.snapshot_outline(pid, ob, "R03 目录恢复")
        ro = store.restore_batch(pid, ob)
        new_ids = [s["id"] for s in store.list_sections(pid)]
        chk.ok(ro.get("outline") and ro.get("restored") == len(titles),
               "目录快照按原样重建全部章节", str(ro)[:120])
        chk.ok(all(x not in new_ids for x in old_ids) and len(ro.get("invalidated") or []) > 0,
               "旧章节 id 全部失效并明确回传")
        chk.ok(len(store.get_project(pid).get("outline") or []) == len(titles),
               "目录结构跟着快照一起还原")

        # ⑦ 单章写占项目写锁；正常完成与异常都要释放
        jid, busy = store.create_job_exclusive("section", pid)
        chk.ok(bool(jid) and not busy, "单章生成占用项目写任务")
        jid2, busy2 = store.create_job_exclusive("compose", pid)
        chk.ok(not jid2 and (busy2.get("kind") == "section"),
               "单章生成期间整本生成被挡住", str(busy2)[:100])
        store.update_job(jid, status="done", progress=100)
        chk.ok(store.active_job(pid) == {}, "写任务完成后释放锁")

        def boom():
            raise RuntimeError("模拟写入异常")

        try:
            app_main._exclusive_write(pid, "section", "异常释放测试", boom)
        except RuntimeError:
            pass
        chk.ok(store.active_job(pid) == {}, "写操作抛异常后同样释放锁")
    finally:
        for name, value in saved.items():
            setattr(agent, name, value)
        for name, value in saved_pv.items():
            setattr(provenance, name, value)
        drop_project(pid)


# ---------------- R10：任务计数与交付状态分开 ----------------

def test_task_status_honesty(chk: Checker) -> None:
    print("\n[R10] 任务计数按实际写入，交付状态不冒充完成")
    from ..app import agent, main as app_main

    titles = ["第一章 技术方案", "第二章 投标报价表", "第三章 项目实施"]
    pid = make_project("自测-R10任务状态", [{"title": t} for t in titles])
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles])
    sids = [s["id"] for s in store.list_sections(pid)]
    real_llm, real_stage = agent._llm, agent.auto_core.run_stage
    agent.auto_core.run_stage = lambda *a, **k: {}
    try:
        # 模型一个字不返回，但期间用户把第一章补上了：不能算 AI 生成成功
        def empty_with_human(*a, **k):
            store.update_section(sids[0], "用户自己补写的正文", pid=pid)
            return ""

        agent._llm = empty_with_human
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        job = store.get_job(jid)
        result = job.get("result") or {}
        counts = result.get("counts") or {}
        chk.ok(result.get("generated") == 0, "模型空响应 + 人工补稿，AI 生成成功数仍为 0",
               str(result.get("generated")))
        chk.ok(counts.get("human") == 1 and counts.get("empty") >= 1,
               "人工补稿与真空稿分别计数", str(counts))
        chk.ok(counts.get("manual") == 1, "保护章节计为待人工任务，不算静默完成",
               str(counts))
        delivery = result.get("delivery") or {}
        chk.ok(delivery.get("deliverable") is False and "待复核" in (delivery.get("label") or ""),
               "任务跑完不等于整本可交付", str(delivery)[:160])
        chk.ok((delivery.get("manual_required") or []), "待人工章节明确列出")
        chk.ok("不等于可交付" in (job.get("step") or ""), "任务卡文案写清交付状态",
               str(job.get("step"))[:120])

        # 项目状态：有文字只叫「正文已填写」
        items = app_main.projects("all").get("items") or []
        mine = next((x for x in items if x["id"] == pid), {})
        chk.ok(mine.get("state_label") in ("正文已填写", "进行中")
               and mine.get("deliverable") is False,
               "项目列表不把有文字显示成已可交付", str(mine)[:160])

        # 收录成稿留来源版本与核验状态，未完成稿不许当已审核
        kb = app_main.project_to_kb(pid, {})
        chk.ok(len(kb.get("source_hash") or "") == 16
               and kb.get("verification") in ("unreviewed", "incomplete"),
               "收录保留内容指纹与未核验状态", str(kb)[:180])
        doc = store.get_doc(kb["doc_id"])
        chk.ok((doc.get("verification") or "") == kb["verification"],
               "核验状态随知识库条目落库", str(doc.get("verification")))
        store.delete_doc(kb["doc_id"])
    finally:
        agent._llm, agent.auto_core.run_stage = real_llm, real_stage
        drop_project(pid)


# ---------------- R04：检查未完成沿调用链传播 ----------------

def test_check_chain(chk: Checker) -> None:
    print("\n[R04] 检查未完成不能在任何一层变成「通过」")
    from ..app import (agent, auto_core, core_bridge, quality, reqtable,
                       settings, store as st)

    titles = ["第一章 技术方案", "第二章 项目实施"]
    pid = make_project("自测-R04检查链", [{"title": t} for t in titles])
    sid = [s["id"] for s in st.list_sections(pid)][0]
    st.update_section(sid, "按招标文件要求逐项响应，内容完整可核查。" * 4, pid=pid)

    def boom(*a, **k):
        raise RuntimeError("模拟检查模块异常")

    saved = {
        "assess_section": quality.assess_section,
        "list_section_quality": st.list_section_quality,
        "hard_texts": reqtable.hard_texts,
        "list_docs": st.list_docs,
        "get_project": st.get_project,
        "core_run": core_bridge.run,
        "settings_load": settings.load,
    }
    try:
        # ① 单章自检出错不能返回 ok=true
        quality.assess_section = boom
        sc = agent._selfcheck(pid, {"title": titles[0]}, "正文")
        chk.ok(sc.get("ok") is False and sc.get("status") == "unavailable",
               "自检出错 → ok=False / unavailable", str(sc)[:120])
        rec = agent._record_quality(pid, sid, "正文", sc, 0, False)
        chk.ok(rec.get("deliverable") is False and "自检没有跑完" in (rec.get("message") or ""),
               "自检没跑完的章节明确不可交付", str(rec)[:120])
        quality.assess_section = saved["assess_section"]

        # ② 章节状态计算失败不能返回「零阻断」
        st.list_section_quality = boom
        states = quality.section_status(pid)
        chk.ok(states and states[0].get("unavailable"),
               "章节状态计算失败 → 显式 unavailable，不返回空列表")
        blockers = quality.section_blockers(pid)
        chk.ok(bool(blockers) and blockers[0]["blocking"],
               "章节状态失败落到导出门禁硬阻断", str(blockers)[:120])
        st.list_section_quality = saved["list_section_quality"]

        # ③ 要求表 / 资料读取失败：保留未检查范围，并落成硬阻断
        reqtable.hard_texts = boom
        a = quality.assess_project(pid)
        chk.ok(any(u["key"] == "requirements" for u in (a.get("unchecked") or []))
               and a.get("complete") is False,
               "要求表读取失败保留错误与未检查范围", str(a.get("unchecked"))[:150])
        ex = quality.export_issues(pid)
        chk.ok(any(x.get("blocking") and "检查范围未完成" in x["title"] for x in ex),
               "未检查范围落成不可勾选放行的硬阻断",
               str([x["title"] for x in ex])[:150])
        reqtable.hard_texts = saved["hard_texts"]

        st.list_docs = boom
        quality.project_sources(pid)
        chk.ok(bool(quality.source_error(pid)), "资料读取失败留痕，不静默当成「没有资料」")
        st.list_docs = saved["list_docs"]

        # ④ 结构检查失败保留错误
        st.get_project = boom
        si = quality.structure_issues(pid)
        chk.ok(si and si[0].get("unavailable"), "结构检查失败也标未完成，不返回空列表")
        st.get_project = saved["get_project"]

        # ⑤ 五态契约：通过 / 失败 / 不适用 / 缺输入 / 运行错误可区分
        cases = [
            ({"ok": True}, "pass"),
            ({"ok": False}, "fail"),
            ({"ok": False, "error": "炸了"}, "unavailable"),
            ({"ok": True, "skipped": True, "summary": "本项目不适用"}, "not_applicable"),
            ({"ok": True, "skipped": True, "summary": "未上传招标文件"}, "missing_input"),
        ]
        got = [auto_core._cap_status(x) for x, _ in cases]
        chk.ok(got == [x for _, x in cases], "五态状态契约互不混淆", str(got))

        # ⑥ 关掉自动编排、清空可选能力，底线检查仍然执行
        settings.load = lambda: {**saved["settings_load"](),
                                 "auto_enabled": False, "export_caps": []}
        caps = settings.export_caps()
        chk.ok(set(settings.REQUIRED_EXPORT_CAPS).issubset(set(caps)),
               "配置清空后四项必需检查仍在清单里", str(caps))
        chk.ok(settings.stage_enabled("export") is True,
               "导出门禁不受自动编排总开关影响")

        # ⑦ 必需检查（报价）报错 → 门禁硬阻断，勾选确认也不能换到导出令牌
        def fake_run(key, pid_, payload):
            if key == "quote":
                raise RuntimeError("报价基座检查炸了")
            return {"ok": True, "stats": [], "sections": [], "summary": "已检查"}

        core_bridge.run = fake_run
        gate = auto_core.export_gate(pid)
        chk.ok(gate.get("blocked") and any("必需检查未完成" in x["title"]
                                           for x in gate.get("blockers") or []),
               "必需检查报错 → 门禁 blocked 并写明原因",
               str([x["title"] for x in gate.get("blockers") or []])[:150])
        confirmed = [x["key"] for x in gate.get("issues") or []]
        tok = auto_core.issue_export_token(pid, gate["fingerprint"], confirmed)
        chk.ok(not tok.get("ok") and tok.get("blocked") is True,
               "逐条勾选高风险也不能换到导出令牌", str(tok)[:150])
        core_bridge.run = saved["core_run"]
        settings.load = saved["settings_load"]
    finally:
        quality.assess_section = saved["assess_section"]
        st.list_section_quality = saved["list_section_quality"]
        reqtable.hard_texts = saved["hard_texts"]
        st.list_docs = saved["list_docs"]
        st.get_project = saved["get_project"]
        core_bridge.run = saved["core_run"]
        settings.load = saved["settings_load"]
        drop_project(pid)


# ---------------- R05：有效文字判断与解析待核对 ----------------

def _pdf_bytes(texts: list[str]) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for t in texts:
        page = doc.new_page()
        if t:
            page.insert_text((60, 90), t, fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


def _xlsx_bytes(filled: bool) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    if filled:
        wb.active["A1"] = "投标报价"
        wb.active["B1"] = "人民币 125000 元"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx_bytes(mode: str) -> bytes:
    import docx
    d = docx.Document()
    if mode == "image":
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
        d.add_picture(io.BytesIO(buf.getvalue()))
    elif mode == "table":
        d.add_paragraph("技术方案响应表")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "指标"
        t.cell(0, 1).text = "承诺值"
        t.cell(1, 0).text = "响应时间"
        t.cell(1, 1).text = "2 小时内到场"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_parsing_review(chk: Checker) -> None:
    print("\n[R05] 有效文字、待核对状态与依据隔离")
    old_engine = parsers.ocr_engine
    parsers.ocr_engine = lambda: ("", None)  # 不依赖本机是否装了 OCR
    long_text = "The bidder shall provide a complete technical response. " * 8
    pid = make_project("自测-解析待核对", [{"title": "第一章 技术方案"}])
    try:
        def refuse(data: bytes, name: str) -> dict:
            try:
                parsers.parse_bytes(name, data)
            except parsers.ParseError as e:
                return e.detail
            return {}

        d = refuse(_pdf_bytes([""]), "scan.pdf")
        chk.ok(d.get("reason") == "scan_no_ocr", "无 OCR 的单页扫描 PDF 拒绝入库，理由准确", str(d))
        d = refuse(b"", "empty.txt")
        chk.ok(d.get("reason") == "empty", "空 TXT 拒绝入库", str(d))
        d = refuse(_xlsx_bytes(False), "empty.xlsx")
        chk.ok(d.get("reason") == "empty_workbook", "空工作簿（只有表名）拒绝入库", str(d))
        d = refuse(_docx_bytes("image"), "only-image.docx")
        chk.ok(d.get("reason") == "image_only_docx", "只有内嵌图片的 Word 拒绝入库并说明限制", str(d))

        mix = parsers.parse_bytes("mixed.pdf", _pdf_bytes([long_text, ""]))
        chk.ok(mix["parse_status"] == "needs_review" and mix["usable"] is False,
               "混合 PDF 标 needs_review 且不可直接当依据", str(mix.get("parse_status")))
        chk.ok(mix["scan_pages"] == 1 and mix["unread_pages"] == [2],
               "未识别页号与页数准确", str(mix.get("unread_pages")))
        chk.ok(0 < mix["effective_chars"] < len(mix["text"])
               and mix["effective_chars"] == sum(p["chars"] for p in mix["page_map"] if p["source"] != "scan-unread"),
               "有效字数只算真实正文，不含页码/占位标记",
               f"{mix['effective_chars']} vs {len(mix['text'])}")
        chk.ok("未识别" in (mix["review_note"] or ""), "待核对原因有说明", mix["review_note"])

        for name, data in (("normal.pdf", _pdf_bytes([long_text])),
                           ("table.docx", _docx_bytes("table")),
                           ("filled.xlsx", _xlsx_bytes(True))):
            r = parsers.parse_bytes(name, data)
            chk.ok(r["effective_chars"] > 0 and r["parse_status"] == "ok",
                   f"正常文件仍可导入：{name}", str(r.get("parse_status")))

        doc = store.add_doc("混合资料", mix["text"], scope="project", project_id=pid,
                            page_map=mix["page_map"], parse=mix)
        chk.ok(any(x["id"] == doc["id"] for x in store.pending_docs(pid)),
               "待核对状态持久化，刷新后仍可查到")
        chk.ok(all(x["id"] != doc["id"] for x in store.usable_docs(pid)),
               "未核对资料不进事实依据池")
        chk.ok(parsers.meta_from_attachment({"needs_review": True, "text": mix["text"]})
               ["parse_status"] == "needs_review", "老客户端只传 needs_review 也能正确落状态")

        issues = quality.export_issues(pid)
        sp = next((x for x in issues if x["key"] == "source_pending"), None)
        chk.ok(bool(sp and sp.get("blocking")), "解析未核对在导出门禁里硬阻断", str(sp)[:120])

        store.add_page_text(doc["id"], 2, "补录：本章要求提供 7x24 小时响应承诺", by="测试")
        cur = store.get_doc(doc["id"])
        chk.ok(not cur["needs_review"] and not cur["unread_pages"] and cur["confirmed_at"],
               "补录完最后一页自动解除待核对", str(cur.get("unread_pages")))
        chk.ok(any(x["id"] == doc["id"] for x in store.usable_docs(pid)),
               "补录完成后才进入依据池")
        chk.ok(not any(x["key"] == "source_pending" for x in quality.export_issues(pid)),
               "待核对解除后不再阻断导出")

        doc2 = store.add_doc("待人工确认", long_text, scope="project", project_id=pid,
                             parse={"needs_review": 1, "parse_status": "needs_review",
                                    "review_note": "正文过少", "effective_chars": 30})
        store.confirm_doc(doc2["id"], by="测试人工")
        d2 = store.get_doc(doc2["id"])
        chk.ok(bool(d2["confirmed_at"]) and d2["parse_status"] == "confirmed",
               "人工确认留痕（责任人与时间）")
        chk.ok(any(x["id"] == doc2["id"] for x in store.usable_docs(pid)),
               "人工确认后进入依据池")

        store.update_project(pid, tender_parse={
            "filename": "招标文件.pdf", "needs_review": True,
            "unread_pages": [7], "review_note": "第 7 页未识别"})
        quality.project_sources(pid)
        chk.ok(any("招标文件" in x for x in quality.unchecked_sources(pid)),
               "招标文件待核对也会沿检查链传播")

        org = store.add_original(pid, "scan.pdf", "/tmp/scan.pdf", 1234, "无有效文字")
        chk.ok(org["status"] == "pending" and store.list_originals(pid)[0]["id"] == org["id"],
               "未识别原件单独留存为待处理，不进依据")
    finally:
        parsers.ocr_engine = old_engine
        drop_project(pid)


def _docx_with_image_and_emf() -> bytes:
    """造一份「有正文 + 1 张位图 + 1 个 OLE 预览 .emf」的 Word。"""
    import docx
    from PIL import Image
    d = docx.Document()
    d.add_paragraph("第一章 投标人须提供完整的技术方案与逐条响应表。")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "指标"
    t.cell(0, 1).text = "承诺值"
    t.cell(1, 0).text = "响应时间"
    t.cell(1, 1).text = "2 小时内到场"
    img = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(img, format="PNG")
    d.add_picture(io.BytesIO(img.getvalue()))
    buf = io.BytesIO()
    d.save(buf)
    raw = buf.getvalue()
    # 真实国网文件里 .emf 是内嵌 Excel 的预览图，不应被当成待识别图片
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/media/image9.emf", b"\x01\x00\x00\x00ole-preview")
    return out.getvalue()


def test_docx_image_ocr(chk: Checker) -> None:
    """内嵌图片文字只供人工核对：绝不混进正文/检索/事实依据。"""
    print("\n[IMG] Word 内嵌图片 OCR 与正文隔离")
    from ..app import main as app_main
    marker = "加群二维码与开标直播群说明 2027年04月23日前有效"
    old_engine, old_ocr = parsers.ocr_engine, parsers._ocr_image_bytes
    parsers.ocr_engine = lambda: ("fake-engine", object())
    parsers._ocr_image_bytes = lambda raw, engine: marker
    pid = make_project("自测-图片核对", [{"title": "第一章 技术方案"}])
    try:
        r = parsers.parse_bytes("带图招标文件.docx", _docx_with_image_and_emf())
        chk.ok(r["image_ocr_count"] == 1 and marker in r["image_text"],
               "位图文字被摘出供人工核对")
        chk.ok(marker not in r["text"] and marker not in str(r["page_map"]),
               "图片文字不并入正文，也不进页码映射/检索")
        chk.ok("投标人须提供完整的技术方案" in r["text"],
               "Word 正文与表格照常完整解析")
        chk.ok("image9.emf" not in r["image_text"],
               "OLE 预览 .emf 不当作待识别图片")
        chk.ok("thumbnail" not in r["image_text"],
               "Word 文件缩略图不被当成文档插图识别")
        chk.ok(r["images"] == 1, "图片计数仍与正文里的实际插图一致", str(r["images"]))
        chk.ok(r["needs_review"] and not r["usable"],
               "有图片时仍标待核对，不因 OCR 就自动放行")
        chk.ok("未并入正文" in r["review_note"] and "对照原件" in r["review_note"],
               "待核对说明写明图片文字的用途与限制", r["review_note"])
        kept = parsers.meta_from_attachment(r)
        chk.ok(kept.get("image_text") == r["image_text"],
               "上传元数据保留图片文字，随招标文件状态落库")

        store.update_project(pid, tender_parse=dict(r, filename="带图招标文件.docx"))
        tp = app_main.parsing_pending(pid)["tender"]
        chk.ok(marker in (tp or {}).get("image_text", ""),
               "刷新后待核对页仍能查看图片文字，人工确认不是盲签")

        parsers.ocr_engine = lambda: ("", None)
        r2 = parsers.parse_bytes("带图招标文件.docx", _docx_with_image_and_emf())
        chk.ok(not r2["image_text"] and r2["needs_review"],
               "没有 OCR 引擎时如实说明未识别，不编造图片内容")
        chk.ok("没有可用的 OCR 引擎" in r2["review_note"],
               "无引擎原因写清楚，提示人工核对", r2["review_note"])
    finally:
        parsers.ocr_engine, parsers._ocr_image_bytes = old_engine, old_ocr
        drop_project(pid)


# ---------------- R06：冻结快照与不可变产物 ----------------

def test_snapshot_artifacts(chk: Checker) -> None:
    print("\n[R06] 检查/导出共用快照、确认令牌与产物不可变")
    from ..app import delivery, docx_export  # noqa: E402
    pid = make_project("自测-导出快照", [{"title": "第一章 技术方案"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, "响应内容：提供 7x24 小时服务，2 小时内到场。")
        snap = delivery.freeze(pid)
        fp = snap["fingerprint"]
        chk.ok(bool(fp) and fp == auto_core._fingerprint(pid)
               and fp == delivery.content_fingerprint(pid),
               "冻结快照与门禁/令牌共用同一指纹", fp)
        chk.ok(delivery.fingerprint(delivery.build_snapshot(pid)) == fp,
               "冻结指纹稳定，不随生成时间变化")
        chk.ok("sections" in snap and "requirements" in snap and "policy_fp" in snap
               and "template" in snap, "快照含正文、有效要求、资料、策略与模板版本")
        chk.ok(delivery.live_changed(snap) is False, "未改动时活数据与冻结版本一致")

        store.update_section(sid, "改成另一版正文，模拟检查期间有人手改")
        changed_fp = delivery.content_fingerprint(pid)
        chk.ok(changed_fp != fp and delivery.live_changed(snap) is True,
               "正文一改，指纹与活数据状态立即变化")
        old_token = "test-token-r06"
        auto_core._EXPORT_TOKENS[old_token] = (pid, fp, __import__("time").time() + 60)
        chk.ok(auto_core.verify_export_token(pid, changed_fp, old_token) is False,
               "内容变了，旧确认令牌失效（不会被消费后误用）")

        store.add_requirements(pid, [{"kind": "hard", "text": "必须提供三年质保承诺"}])
        chk.ok(delivery.content_fingerprint(pid) != changed_fp, "要求变更使指纹变化")
        fp2 = delivery.content_fingerprint(pid)
        store.add_doc("新增资质", "ISO9001 证书", scope="project", project_id=pid)
        chk.ok(delivery.content_fingerprint(pid) != fp2, "资料变更使指纹变化")

        old_load = settings.load
        settings.load = lambda: {"export_caps": ["quote"], "export_quality": False}
        try:
            chk.ok(delivery.content_fingerprint(pid) != delivery.fingerprint(snap),
                   "策略变更使指纹变化，旧确认随之失效")
        finally:
            settings.load = old_load

        check_snap = delivery.freeze(pid)
        data1 = b"# deliverable v1\n\nnormal body\n"
        a1 = delivery.register(pid, data1, "投标书.docx", snap=check_snap,
                               gate_fingerprint=fp, fmt="md",
                               check={"ok": True, "summary": "测试"})
        data2 = b"# deliverable v2\n\nnormal body\n"
        a2 = delivery.register(pid, data2, "投标书.docx", snap=check_snap,
                               gate_fingerprint=fp, fmt="md",
                               check={"ok": True, "summary": "测试"})
        chk.ok(a1["id"] != a2["id"] and a1["path"] != a2["path"],
               "同一秒同名两次导出：产物 ID 与路径都唯一，不互相覆盖")
        rows = store.list_artifacts(pid)
        chk.ok(len([x for x in rows if x["kind"] == "deliverable"]) == 2,
               "两条登记记录都保留，不因展示名相同被删", str(len(rows)))
        by_id = {x["id"]: x for x in rows}
        chk.ok(by_id[a1["id"]]["content_hash"] == delivery._sha(data1)
               and by_id[a2["id"]]["content_hash"] == delivery._sha(data2),
               "两份产物摘要各自匹配")
        chk.ok(by_id[a2["id"]]["is_current"] == 1 and by_id[a1["id"]]["is_current"] == 0
               and by_id[a2["id"]]["version"] > by_id[a1["id"]]["version"],
               "旧定稿标为历史版本，只有最新一份是当前定稿")

        ok1, _ = delivery.verify_download(pid, by_id[a1["id"]])
        chk.ok(ok1, "登记完好时允许回下载历史版本")
        Path(a1["path"]).write_bytes(b"tampered")
        ok2, why2 = delivery.verify_download(pid, by_id[a1["id"]])
        chk.ok(not ok2 and "摘要" in why2, "磁盘文件被改动后拒绝回下载", why2)
        chk.ok(not delivery.verify_download("other-project", by_id[a2["id"]])[0],
               "另一项目不能取得该产物")
        chk.ok(not delivery.verify_download(pid, {})[0], "未登记文件不能取得")
        store.mark_artifact_status(a2["id"], "missing")
        chk.ok(not delivery.verify_download(pid, store.get_artifact_by_id(a2["id"]))[0],
               "清理后记录标 missing，回下载被拒")

        rev = delivery.register(pid, b"<html>review</html>", "检查报告.html",
                                kind="review", snap=check_snap, fmt="md")
        chk.ok("审阅" in (rev.get("purpose") or "") and "审阅件-" in rev["name"],
               "审阅件在文件名与用途登记里标明，不会被当成正式交付件")

        good = delivery.inspect_document("# 标书\n\n正文完整。".encode("utf-8"),
                                        "md", check_snap)
        bad = delivery.inspect_document("正文 TODO 待补充 XXX".encode("utf-8"),
                                        "md", check_snap)
        chk.ok(good["checked"] and good["ok"] and not bad["ok"],
               "最终文件检查：正常件通过、含占位符的不能标成正式产物")
        docx_bytes = docx_export.build_docx("测试项目", check_snap["sections"],
                                            {"date": "2026 年 11 月 10 日"})
        docx_check = delivery.inspect_document(docx_bytes, "docx", check_snap)
        chk.ok(docx_check["checked"] and not any(
            x["code"] == "docx_open" for x in docx_check["issues"]),
            "Word 最终文件检查能打开并校验结构", str(docx_check["summary"]))
        broken = delivery.inspect_document(b"not a zip", "docx", check_snap)
        chk.ok(not broken["ok"] and any(x["code"] == "docx_open" for x in broken["issues"]),
               "打不开的 Word 不会被登记成正式交付件")
    finally:
        drop_project(pid)


# ---------------- R08：统一数据目录与启动检查 ----------------

def test_env_and_preflight(chk: Checker) -> None:
    print("\n[R08] 数据目录统一、.env 顺序与自检阻断")
    import socket
    import subprocess
    from ..app import config, preflight, runtime, settings  # noqa: E402
    from ..app import auto_core as _auto, core_bridge as _bridge  # noqa: E402
    from ..app.basecore import config as basecfg  # noqa: E402

    dirs = {str(config.DATA_DIR), str(settings.DATA), str(basecfg.DATA_DIR),
            str(_bridge.DATA), str(_auto.DATA), str(store.db_path().parent)}
    chk.ok(len(dirs) == 1, "主库/策略/基座/输出/认证库解析到同一个数据根目录", str(dirs))
    chk.ok(str(config.MODELS_FILE.parent) == str(config.DATA_DIR)
           and str(basecfg.DB_PATH.parent) == str(config.DATA_DIR),
           "模型、健康、预算与基座库都在数据根下")
    chk.ok(runtime.cli_args() == f"--host {runtime.HOST} --port {runtime.PORT}"
           and runtime.url().endswith(f":{runtime.PORT}"),
           "自检、启动脚本与真实服务共用同一 host/port", runtime.cli_args())

    # 干净子进程：仅在 .env 设 BIDAGENT_DATA / PORT，验证先加载 .env 再解析路径
    tmp = Path(tempfile.mkdtemp(prefix="bidagent_r08_"))
    envfile = tmp / "custom.env"
    envfile.write_text(f"BIDAGENT_DATA={tmp / 'fromenv'}\nBIDAGENT_PORT=5399\n",
                       encoding="utf-8")
    code = ("from backend.app import config, settings, auth, runtime, store\n"
            "from backend.app.basecore import config as b\n"
            "print('|'.join(str(x) for x in [config.DATA_DIR, settings.FILE.parent, "
            "auth.AUTH_DB.parent, b.DATA_DIR, store.db_path().parent]))\n"
            "print(runtime.cli_args())\n")
    base = dict(os.environ)
    base["BIDAGENT_ENV_FILE"] = str(envfile)
    for k in ("BIDAGENT_DATA", "BIDAGENT_PORT", "BIDAGENT_HOST"):
        base.pop(k, None)
    try:
        out = subprocess.check_output([sys.executable, "-c", code], env=base,
                                      text=True, stderr=subprocess.STDOUT)
        parts = [x.strip() for x in out.splitlines()[0].split("|")]
        chk.ok(len(set(parts)) == 1 and "fromenv" in parts[0],
               "仅在 .env 设置 BIDAGENT_DATA，所有模块落到该目录", str(set(parts))[:160])
        chk.ok("--port 5399" in out, ".env 里的端口也能被启动参数读到")
        env2 = dict(base)
        env2["BIDAGENT_DATA"] = str(tmp / "real")
        env2["BIDAGENT_PORT"] = "5401"
        out2 = subprocess.check_output([sys.executable, "-c", code], env=env2,
                                       text=True, stderr=subprocess.STDOUT)
        parts2 = [x.strip() for x in out2.splitlines()[0].split("|")]
        chk.ok(len(set(parts2)) == 1 and "real" in parts2[0]
               and "--port 5401" in out2,
               "真实环境变量覆盖 .env，且仍落到同一目录")
        chk.ok(all(str(Path(x)) != str(Path.cwd() / "data") for x in parts2),
               "独立测试目录不向业务 data 写策略、库或文件")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 端口占用必须是阻断，不能显示「可以启动」
    preflight._rows.clear()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    used = s.getsockname()[1]
    old_host, old_port = preflight.HOST, preflight.PORT
    preflight.HOST, preflight.PORT = "127.0.0.1", used
    try:
        preflight.check_port()
        row = preflight._rows[-1]
        chk.ok(row[0] == preflight.FAIL and "被占用" in row[1],
               "端口被占用标为阻断", str(row))
    finally:
        preflight.HOST, preflight.PORT = old_host, old_port
        preflight._rows.clear()
        s.close()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    free_port = free.getsockname()[1]
    free.close()
    preflight.HOST, preflight.PORT = "127.0.0.1", free_port
    try:
        preflight.check_port()
        chk.ok(preflight._rows[-1][0] == preflight.OK, "端口空闲时检查通过")
    finally:
        preflight.HOST, preflight.PORT = old_host, old_port
        preflight._rows.clear()

    # 模型：配置齐全 ≠ 已验证可连通
    preflight._rows.clear()
    old_file, old_probe = config.MODELS_FILE, preflight._probe_model
    tmp_model = Path(tempfile.mktemp(suffix=".json"))
    config.MODELS_FILE = tmp_model
    tmp_model.write_text(json.dumps({"models": [
        {"base_url": "http://127.0.0.1:1/v1", "api_key": "dummy", "model": "m"}]}),
        encoding="utf-8")
    preflight._probe_model = lambda base, key: (False, "离线")
    try:
        preflight.check_models()
        row = preflight._rows[-1]
        chk.ok("配置齐全" in row[2] and "未验证连通" in row[2],
               "模型配置齐全与已验证连通分开表述", str(row))
    finally:
        config.MODELS_FILE = old_file
        preflight._probe_model = old_probe
        preflight._rows.clear()
        tmp_model.unlink(missing_ok=True)


# ---------------- B 批：业务完整性（要求 / 事实 / 表单 / 招标版本） ----------------

def test_business_integrity(chk: Checker) -> None:
    from ..app import delivery, facts, forms, reqmatrix, reqtable, tender  # noqa: E402

    # ===== B01：60 条要求，关键条款放在后部，全部进入有效范围 =====
    print("\n[B01] 要求不截断、稳定 ID、逐条响应矩阵")
    pid = make_project("自测-B01完整要求", [{"title": "第一章 技术方案"}])
    try:
        reqs = [{"kind": "hard", "text": f"第{i}条：须提供相应服务与证明材料",
                 "source": "auto", "status": "active", "loc": f"第{i//10+1}章",
                 "source_version": "v1", "required": 1}
                for i in range(1, 60)]
        reqs.append({"kind": "hard", "text": "第60条（后部关键）：质保期不得低于七年且须逐项列明响应措施",
                     "source": "auto", "status": "active", "loc": "第8章附则",
                     "source_version": "v1", "required": 1})
        store.add_requirements(pid, reqs)
        items = reqtable.constraint_items(pid)
        chk.ok(len(items) == 60, "60 条要求全部进入有效范围（无 24/40 条截断）",
               f"actual={len(items)}")
        chk.ok(all(x["id"] and x["loc"] and x["source_version"] for x in items),
               "每条要求都有稳定 ID、原文位置与来源版本")
        chk.ok("第60条" in reqtable.constraints_text(pid)
               and "共 60 条" in reqtable.constraints_text(pid),
               "后部关键条款进入完整约束文本，未被 8 条 / 3000 字截断")
        ov = reqtable.outline_constraints(pid, size=12)
        chk.ok("共 60 条" in ov and "未处理 48 条" in ov,
               "大纲分批概览写明已处理/未处理范围，不冒充全部处理")
        by_ch = reqtable.constraints_for_chapter(pid, "技术方案")
        chk.ok("逐一响应" in by_ch or "完整清单见要求表" in by_ch,
               "逐章写作按相关度分配本批，不无限扩大提示词")
        batches = reqtable.constraint_batches(pid, size=12)
        chk.ok(batches["total"] == 60 and len(batches["batches"]) == 5
               and batches["unprocessed"] == 60,
               "分批记录总数与未处理范围", str(batches["total"]))

        # 照抄要求只是候选响应，不能自动变成证据充分
        sid = store.list_sections(pid)[0]["id"]
        store.update_section(sid, "第60条（后部关键）：质保期不得低于七年且须逐项列明响应措施。")
        rep = reqmatrix.build(pid)
        copied = next(x for x in rep["items"] if x["text"].startswith("第60条"))
        chk.ok(copied["candidate"] and copied["copy_only"]
               and not copied["evidence_ok"],
               "逐字照抄要求只算候选，不给证据充分", str(copied["status"]))
        blocks = reqmatrix.blocking(pid)
        chk.ok(len(blocks) == rep["pending"]
               and all(b["blocking"] and b["action"] and b["req_id"] for b in blocks),
               "每条未落实必需项都有补齐入口", str(len(blocks)))
        # 关键口径：即便起草进度过了 60%，只要还有必需项没逐条确认就不可交付
        chk.ok(not reqmatrix.confirm(pid, copied["id"], reviewer="自测复核")["ok"],
               "人工按钮不能绕过照抄响应和缺失支持关系")
        from ..app import requirement_support
        store.update_section(sid,"\n".join(
            f"第{i}条：我方将提供相应服务与证明材料，安排专人落实服务流程并记录核对结果"
            for i in range(1,43)))
        for x in rep["pending_items"]:
            requirement_support.set_policy(pid,x["id"],{"type":"scheme"},"自测复核")
        for x in rep["pending_items"][: max(1, int(rep["required"] * 0.7))]:
            reqmatrix.confirm(pid, x["id"], reviewer="自测复核")
        rep60 = reqmatrix.build(pid)
        chk.ok(rep60["draft_progress"] >= 0.6 and not rep60["deliverable"]
               and rep60["pending"] > 0,
               "起草进度过 60% 仍不可交付：最终达标看每条必需项",
               f"progress={rep60['draft_progress']} pending={rep60['pending']}")
        rid = next(x["id"] for x in rep60["pending_items"])
        chk.ok(reqmatrix.set_na(pid, rid, "")["ok"] is False,
               "标不适用不写理由会被拒绝")
        chk.ok(reqmatrix.set_na(pid, rid, "本包不含该设备，招标澄清第 3 条已确认")["ok"] is True,
               "写明理由后可标不适用并留复核记录")
        rep2 = reqmatrix.build(pid)
        chk.ok(rep2["na"] == 1 and rep2["pending"] == rep60["pending"] - 1,
               "不适用项从待落实清单移出且可追溯")
        # 停用 / 失效的要求不参与当前门禁
        store.update_requirement(pid, rid, {"status": "superseded"})
        chk.ok(all(x["id"] != rid for x in reqtable.constraint_items(pid)),
               "失效要求不继续参与当前门禁")
    finally:
        drop_project(pid)

    # ===== B02：实体、单位、范例、计算值与附件 =====
    print("\n[B02] 数值核对实体/单位、Decimal 报价与原件回跳")
    chk.ok(facts.verify_token("6万元", "单价 2万元，数量 3台", [])["ok"],
           "合法计算值（Decimal 核算）可核验")
    chk.ok(not facts.verify_token("6万元", "别的项目 2元 3元", [])["ok"],
           "同数值不同单位 / 其他项目金额不能直接放行")
    chk.ok(not facts.verify_token("100万元", "别的项目 100元", [])["ok"],
           "裸数字子串不算依据")
    example = {"kind": "example", "entity": "别家公司", "value": "500",
               "unit": "万元", "verification": "verified:人工"}
    chk.ok(not facts.verify_token("500万元", "", [example])["ok"],
           "历史标书范例不能证明当前企业金额")
    verified = {"kind": "enterprise_fact", "entity": "本公司", "value": "500",
                "unit": "万元", "verification": "verified:人工"}
    chk.ok(facts.verify_token("500万元", "", [verified])["ok"],
           "已核验的当前企业事实可作依据")
    q = facts.quote_check([{"name": "A", "qty": "2", "unit_price": "3.5", "amount": "7.00"},
                           {"name": "B", "qty": "1", "unit_price": "2", "amount": "3"}])
    chk.ok(not q["ok"] and q["total"] == "9.00" and q["lines"][0]["ok"]
           and q["rounding"].startswith("ROUND_HALF_UP"),
           "报价逐行核算并指出错误行，合计与舍入规则明确")
    q2 = facts.quote_check([{"name": "A", "qty": "3", "unit_price": "0.335"}])
    chk.ok(q2["ok"] and q2["lines"][0]["amount"] == "1.01",
           "Decimal 舍入规则一致（ROUND_HALF_UP / 2 位）", q2["lines"][0]["amount"])
    pid = make_project("自测-B02事实", [{"title": "第一章 技术方案"}])
    try:
        store.add_doc("别家公司整本标书", "业绩金额 800万元", scope="kb", kind="sample")
        store.add_doc("本项目资质", "注册资本 500万元", scope="project", project_id=pid)
        src = quality.project_sources(pid)
        chk.ok(all("800万元" not in str(x.get("snippet") or "") for x in src),
               "范例资料不进数值证据池")
        chk.ok(quality.example_excluded(pid) >= 1,
               "记录被排除的范例数量，界面可说明口径")
        att = store.add_attachment(pid, "授权书原件.pdf", "data/originals/x.pdf",
                                   doc_id="doc1", section_id="s1", page=3,
                                   note="授权书原件")
        idx = facts.attachment_index(pid)
        chk.ok(idx and idx[0]["id"] == att["id"] and idx[0]["traceable"]
               and idx[0]["page"] == 3,
               "提交附件可定位到原件 / 事实 / 页码")
    finally:
        drop_project(pid)

    # ===== B03：保护表单字段、占位符、报价、签章与短表单 =====
    print("\n[B03] 保护表单完成核验")
    todo = {"id": "f1", "title": "投标函", "protected": 1,
            "text": "投标人：TODO；项目名称：XXX"}
    f = forms.check_section("p", todo, confirmations=[], attachments=[])
    chk.ok(not f["ok"] and any("占位符" in g for g in f["gaps"]),
           "只有 TODO / XXX 的投标函不算完成")
    chk.ok(any("缺必填字段" in g for g in f["gaps"]),
           "缺必填字段逐项列出")
    short = {"id": "f2", "title": "投标函", "protected": 1,
             "text": "投标人：甲公司；项目名称：X；投标总价：100元；工期：30天；法定代表人：张三"}
    f2 = forms.check_section("p", short, confirmations=[], attachments=[])
    chk.ok(not f2["gaps"] and f2["short_form"],
           "短而完整的表单不套普通正文 60 字下限")
    chk.ok(not f2["ok"] and any("签章" in x for x in f2["pending"]),
           "签章无法自动确认时列待人工复核")
    conf = [{"target": "signature:f2", "content_hash": f2["content_hash"]}]
    f3 = forms.check_section("p", short, confirmations=conf, attachments=[])
    chk.ok(f3["ok"], "人工确认签章后短表单可完成")
    changed = dict(short, text=short["text"] + "（改动）")
    f4 = forms.check_section("p", changed, confirmations=conf, attachments=[])
    chk.ok(not f4["ok"] and any("版本已变化" in g for g in f4["gaps"]),
           "人工确认绑定正文内容版本，改动后失效")
    quote = {"id": "f3", "title": "开标一览表", "protected": 1,
             "text": "总价 100元，大写壹佰元\nA | 2 | 3 | 7"}
    fq = forms.check_section("p", quote, confirmations=[], attachments=[])
    chk.ok(not fq["ok"] and any("证明附件" in g for g in fq["gaps"]),
           "缺报价明细附件不算完成")
    chk.ok(any("Decimal" in g or "总价" in g for g in fq["gaps"]),
           "行列小计/总价关系用 Decimal 核对", str(fq["gaps"]))
    pid = make_project("自测-B03表单", [
        {"title": "投标函", "protected": 1}, {"title": "授权委托书", "protected": 1}])
    try:
        secs = store.list_sections(pid)
        store.update_section(secs[0]["id"], "投标人：TODO；项目名称：XXX")
        store.update_section(secs[1]["id"], "授权人：甲；被授权人：乙；有效期至 2027-01-01")
        issues = quality.export_issues(pid)
        chk.ok(any(i["key"].startswith("form-") for i in issues),
               "保护表单缺口进入导出门禁")
        chk.ok(any(i["key"].startswith("form-") and i["blocking"] for i in issues),
               "字段/占位符缺口硬阻断")
        pending = [i for i in issues if i["key"].startswith("form-")
                   and i.get("pending_targets")]
        chk.ok(bool(pending), "待人工复核项带确认入口（签章/有效期/报价结构）")
    finally:
        drop_project(pid)

    # ===== B04：原文件 / 补遗 / 澄清 / 整份替换 =====
    print("\n[B04] 招标文件版本链与旧要求失效")
    pid = make_project("自测-B04版本", [{"title": "第一章 技术方案"}])
    try:
        # 避免测试触发基座重抽（这里只验证版本关系与状态流转）
        orig_sync = reqtable.sync
        reqtable.sync = lambda *a, **k: {"added": 0}
        try:
            base = ("第一章 投标人须具备一级资质。"
                    "第二章 投标截止日期为 2026 年 12 月 1 日。"
                    "第三章 质保期三年。")
            r1 = tender.ingest(pid, "original", base, filename="招标文件.pdf")
            v1 = r1["version"]["id"]
            store.add_requirements(pid, [
                {"kind": "hard", "text": "投标人须具备一级资质", "source": "auto",
                 "status": "active", "source_version": v1, "required": 1},
                {"kind": "hard", "text": "人工补的一条关键要求", "source": "manual",
                 "status": "active", "source_version": v1, "required": 1},
            ])
            req_id = next(x["id"] for x in store.list_requirements(pid)
                          if x["text"] == "投标人须具备一级资质")
            manual_id = next(x["id"] for x in store.list_requirements(pid)
                             if x["text"] == "人工补的一条关键要求")
            for xid in (req_id, manual_id):
                store.save_req_response(pid, xid, status="confirmed",
                                        reviewer="张三", evidence="已逐条复核")
            old_fp = delivery.content_fingerprint(pid)
            r2 = tender.ingest(pid, "amend", "投标截止日期顺延至 2026 年 12 月 20 日。",
                               filename="补遗一.pdf")
            chk.ok(r2["version"]["version"] == 2 and r2["retired_requirements"] >= 1,
                   "补遗单独记版本，旧自动要求按来源版本转失效")
            merged = str(store.get_project(pid)["tender_text"])
            chk.ok("一级资质" in merged and "质保期三年" in merged
                   and "12 月 20 日" in merged,
                   "补遗只改期限，原其他条款保留")
            eff = reqtable.constraint_items(pid)
            chk.ok(not any(x["text"] == "投标人须具备一级资质" for x in eff),
                   "失效的自动要求不再参与当前门禁")
            manual = next((x for x in store.list_requirements(pid)
                           if x["text"] == "人工补的一条关键要求"), None)
            chk.ok(bool(manual) and "[新版待复核]" in str(manual.get("note") or ""),
                   "人工修订保留但明确标待复核，不静默沿用")
            chk.ok(store.get_req_response(pid, req_id)["status"] == "review",
                   "条款变更后旧的逐条确认降级为待复核")
            chk.ok(delivery.content_fingerprint(pid) != old_fp,
                   "条款变化使导出确认指纹失效")
            rep = reqmatrix.build(pid)
            chk.ok(any(x["status"] == "review" for x in rep["items"]),
                   "矩阵如实显示需要重新复核的项")
            r3 = tender.ingest(pid, "replace", "全新整份招标文件：质保期五年。",
                               filename="替换版.pdf")
            chk.ok(r3["version"]["version"] == 3
                   and "一级资质" not in str(store.get_project(pid)["tender_text"]),
                   "整份替换换掉当前正文，历史版本仍保留")
            chk.ok(len(tender.summary(pid)["items"]) == 3,
                   "版本链保留原文件 / 补遗 / 替换的顺序")
            r4 = tender.ingest(pid, "replace", "全新整份招标文件：质保期五年。")
            chk.ok(r4.get("unchanged") and len(tender.summary(pid)["items"]) == 3,
                   "重复保存相同正文不堆垃圾版本")
        finally:
            reqtable.sync = orig_sync
    finally:
        drop_project(pid)


# ---------------- A7：项目归档 / 收进知识库 / 删除 ----------------

def test_c01_bundle(chk: Checker) -> None:
    print("\n[C01] 暗标全包扫描 / 真实分册与投标包")
    import re
    from ..app import bidbundle, blindcheck, delivery, docx_export

    CO = "宏远建设集团有限公司"
    secs = [
        {"title": "投标函及法定代表人授权", "text": "我方承诺按招标文件要求履约，项目负责人张三。"},
        {"title": "分项报价表", "text": "| 名称 | 数量 | 单价 | 金额 |\n| --- | --- | --- | --- |\n| 设备A | 2 | 100 | 200 |"},
        {"title": "技术方案与实施组织", "text": "本项目分阶段实施，质量与安全责任落实到人。"},
    ]

    def _inject_creator(data: bytes, who: str) -> bytes:
        """模拟「正文洗干净了，文档属性还留着公司名」。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, \
                zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for it in src.infolist():
                raw = src.read(it.filename)
                if it.filename == "docProps/core.xml":
                    s = raw.decode("utf-8", "ignore")
                    tag = f"<dc:creator>{who}</dc:creator>"
                    if re.search(r"<dc:creator>[^<]*</dc:creator>", s):
                        s = re.sub(r"<dc:creator>[^<]*</dc:creator>", tag, s)
                    else:
                        s = s.replace("</cp:coreProperties>", tag + "</cp:coreProperties>")
                    raw = s.encode("utf-8")
                dst.writestr(it, raw)
        return buf.getvalue()

    pid = make_project("C01投标包", secs)
    try:
        ids = [s["id"] for s in store.list_sections(pid)]
        vols = [{"name": f"第{i + 1}册-" + str(s["title"])[:6],
                 "section_ids": [sid]} for i, (sid, s) in enumerate(
                     zip(ids, store.list_sections(pid)))]

        plan = bidbundle.plan(pid)
        chk.ok(plan["needs_confirmation"] is True, "分册规划只算预览，标明仍需人工确认")
        chk.ok(plan["total"] == 3 and plan["assigned"] == 3, "规划里全部章节都有归属，不漏章")
        chk.ok(not plan["rules"]["configured"], "没登记规则时规划如实显示未配置")

        blindcheck.save_rules(pid, {"enterprise_full": "", "persons": []})
        chk.ok(not blindcheck.rules_for(pid)["configured"], "只勾开关没填身份词不算已配置")
        rules = blindcheck.save_rules(pid, {
            "enterprise_full": CO, "persons": ["张三"], "contacts": ["13800138000"]})
        chk.ok(rules["configured"] and len(blindcheck._identity_items(rules)) >= 3,
               "登记后形成企业 / 人员 / 联系方式身份词典")

        dirty = _inject_creator(docx_export.build_docx("测试项目", secs, {"header": CO}), CO)
        scan = blindcheck.scan_docx(dirty, pid)
        parts = scan["checked_parts"]
        with zipfile.ZipFile(io.BytesIO(dirty)) as z:
            docxml = z.read("word/document.xml").decode("utf-8", "ignore")
        chk.ok("<w:tbl" in docxml and "word/document.xml" in parts,
               "表格所在部件（正文/标题/表格）进入扫描范围")
        chk.ok(any(n.startswith("word/header") for n in parts), "扫描覆盖页眉")
        chk.ok(any(n.startswith("word/footer") for n in parts), "扫描覆盖页脚")
        chk.ok(any(n.startswith("docProps/") for n in parts), "扫描覆盖文档属性")
        chk.ok(any(x.get("part", "").startswith("word/header")
                   for x in scan["issues"]), "页眉里的公司名被抓到并写出位置")
        chk.ok(any(x.get("part", "").startswith("docProps/")
                   for x in scan["issues"]), "文档属性里的身份信息被抓到")
        chk.ok(not scan["ok"] and bool(scan["blocking"]), "有身份信息命中就不判为暗标合规")
        chk.ok("已按本项目登记的规则扫描" in scan["claim"], "登记规则后结论写明按项目规则扫描")

        clean_secs = [dict(s, text=str(s["text"] or "").replace("张三", "项目负责人"))
                      for s in secs]
        clean = docx_export.build_docx("测试项目", clean_secs, {"blind": True, "header": ""})
        ok_scan = blindcheck.scan_docx(clean, pid)
        chk.ok(ok_scan["ok"] and ok_scan["configured"], "干净文件按项目规则扫描通过")

        pid2 = make_project("C01未配置规则", [{"title": "技术方案", "text": "正文内容"}])
        try:
            s2 = blindcheck.scan_docx(clean, pid2)
            chk.ok(not s2["configured"] and not s2["ok"], "没登记项目规则不能判为完全合规")
            chk.ok("完全暗标合规证明" in s2["claim"], "结论明确说明不构成完全暗标合规证明")
            chk.ok(any(x["code"] == "rules_missing" for x in s2["issues"]),
                   "扫描结果列出规则缺失，而不是静默认通过")
            ins = delivery.inspect_document(
                clean, "docx", {"pid": pid2, "sections": [], "name": "x"}, blind=True)
            chk.ok(any(x["code"] == "blind_rules_missing" for x in ins["issues"]),
                   "最终文件检查也提示未登记规则")
            chk.ok("PDF" in str(ins["capability_note"]), "最终文件检查写明 PDF 转换未实现")
        finally:
            drop_project(pid2)

        g, err = bidbundle._validate(pid, [{"name": "资格文件", "section_ids": ids[:2]}])
        chk.ok(g is None and "没有分册归属" in err, "漏章的分册方案被拒")
        g, err = bidbundle._validate(pid, [
            {"name": "A", "section_ids": ids}, {"name": "B", "section_ids": [ids[0]]}])
        chk.ok(g is None and "重复" in err, "同一章被分到两册被拒")
        g, err = bidbundle._validate(pid, vols)
        chk.ok(bool(g) and len(g) == 3 and not err, "逐册确认后的方案通过校验")

        snap = delivery.freeze(pid)
        r0 = bidbundle.build(pid, snap, vols)
        chk.ok(not r0["ok"] and "确认" in r0["error"], "未确认归属不生成文件（约束下沉到业务层）")
        chk.ok(not r0.get("data"), "未确认时没有任何可下载字节")

        r = bidbundle.build(pid, snap, vols, confirmed=True)
        chk.ok(r["ok"] and bool(r["data"]), "确认后生成真实投标包")
        with zipfile.ZipFile(io.BytesIO(r["data"])) as z:
            names = z.namelist()
            chk.ok(sum(1 for n in names if n.endswith(".docx")) == 3, "逐册各有一份 Word")
            for f in ("分册索引.txt", "逐条响应表.txt", "文件包清单.json"):
                chk.ok(f in names, f"包内含 {f}")
            man = json.loads(z.read("文件包清单.json").decode("utf-8"))
        chk.ok(len(man["volumes"]) == 3, "清单列出每一册")
        chk.ok(len(man["final_checks"]) == 3 and all(c["ok"] for c in man["final_checks"]),
               "清单记录逐册最终文件检查结果")
        chk.ok(any("PDF" in x for x in man["capability_note"]), "清单写明 PDF 转换未实现")
        chk.ok(any("平台专用格式" in x for x in man["capability_note"]),
               "清单写明平台专用格式未实现")
        chk.ok(any("目录域" in x for x in man["capability_note"]),
               "清单写明目录域需 Word 打开时更新")
        chk.ok(all(c["ok"] for c in r["checks"]), "每册都过了最终文件检查")

        store.update_section(ids[2], "XXX 待补充")
        snap2 = delivery.freeze(pid)
        r2 = bidbundle.build(pid, snap2, vols, confirmed=True)
        chk.ok(bool(r2["blocking"]), "有一册最终文件检查没过被记录")
        chk.ok(r2.get("data") == b"", "有一册没过就不交出 zip（整个包不发）")
        chk.ok(any("XXX" in str(x.get("detail", "")) for c in r2["blocking"]
                   for x in c["issues"]), "失败原因写明是正式文件里仍有占位符")
    finally:
        drop_project(pid)


def test_c02_config_isolation(chk: Checker) -> None:
    print("\n[C02] 企业配置隔离：策略继承、密钥不复制、旧数据归属")
    import contextlib
    from ..app import auth, config, scope, settings

    @contextlib.contextmanager
    def as_org(oid: str):
        tok = auth.set_current({"org_id": oid, "role": "owner", "id": "u_" + oid})
        try:
            yield
        finally:
            auth.reset_current(tok)

    A, B = "c02_org_a", "c02_org_b"
    for o in (A, B):
        shutil.rmtree(scope.org_dir(o), ignore_errors=True)

    def _model(mid, key):
        return {"id": mid, "label": mid, "api_key": key, "base_url": "https://x",
                "model": "x", "ready": True, "provider": "custom"}

    try:
        with as_org(auth.DEFAULT_ORG):
            config.set_budget({"daily_token_limit": 111111})
            config.set_privacy({"max_chars": 9999})
            config.set_mode("live")
            settings.save({"auto_enabled": False})
            reg = config._read_registry()
            reg["models"] = [_model("platform", "PLATFORM-SECRET")]
            reg["active"] = "platform"
            config._write_registry(reg)

        with as_org(A):
            chk.ok(config.budget()["daily_token_limit"] == 111111
                   and scope.inherited("budget.json"),
                   "新企业预算明确继承平台默认，并标注继承")
            chk.ok(config.privacy()["max_chars"] == 9999, "隐私策略继承平台默认")
            chk.ok(config.MODE == "live", "运行模式继承平台默认且按企业动态解析")
            chk.ok(settings.load()["auto_enabled"] is False, "编排策略继承平台默认")
            areg = config._read_registry()
            chk.ok(not areg["models"] and "PLATFORM-SECRET" not in json.dumps(areg),
                   "新企业没有继承平台密钥")
            chk.ok(config._blank_registry()["models"] == [],
                   "环境变量里的平台密钥也不注入其他企业")
            chk.ok(scope.dir_for("originals") == scope.org_dir(A) / "originals",
                   "原件目录按企业分开")

        with as_org(A):
            config.set_budget({"daily_token_limit": 222222})
            config.set_privacy({"max_chars": 5555})
            config.set_mode("demo")
            settings.save({"auto_enabled": True})
            reg = config._read_registry()
            reg["models"] = [_model("orgA", "A-SECRET")]
            reg["active"] = "orgA"
            config._write_registry(reg)

        with as_org(B):
            chk.ok(config.budget()["daily_token_limit"] == 111111,
                   "A 改预算不影响 B")
            chk.ok(config.privacy()["max_chars"] == 9999, "A 改隐私策略不影响 B")
            chk.ok(config.MODE == "live", "A 改运行模式不影响 B")
            chk.ok(settings.load()["auto_enabled"] is False, "A 改编排策略不影响 B")
            chk.ok(scope.origin("models.json")["source"] == "none",
                   "还没读过注册表时，如实显示「本企业尚未配置」")
            breg = config._read_registry()
            for secret in ("A-SECRET", "PLATFORM-SECRET"):
                chk.ok(secret not in json.dumps(breg), f"B 里没有 {secret}")
            chk.ok(not breg["models"],
                   "B 自己建的空注册表里没有任何模型或密钥")

        with as_org(auth.DEFAULT_ORG):
            chk.ok(config.budget()["daily_token_limit"] == 111111,
                   "企业改动不回写平台默认")
            chk.ok("PLATFORM-SECRET" in json.dumps(config._read_registry()),
                   "平台自己的模型仍可用（旧数据迁移后可用）")
            chk.ok(scope.dir_for("originals") == config.DATA_DIR / "originals",
                   "默认企业沿用旧原件目录，旧数据不用搬家")

        chk.ok((scope.org_dir(A) / "budget.json").is_file()
               and (scope.org_dir(A) / "agent_config.json").is_file(),
               "A 的改动落在 A 自己的目录")
        chk.ok(not (scope.org_dir(B) / "budget.json").exists(),
               "B 没保存就不产生自己的策略文件")
        # 前面的子进程自检可能已经登记过一次；删掉登记再验迁移，模拟全新升级
        (scope.data_dir() / scope.OWNERSHIP_FILE).unlink(missing_ok=True)
        own = scope.migrate_legacy()
        chk.ok(own["owner"] == auth.DEFAULT_ORG, "旧公共配置显式登记归属默认企业")
        chk.ok("models.json" in own["files"], "迁移登记包含旧模型配置")
        chk.ok(not (scope.org_dir(B) / "models.json").exists()
               or "PLATFORM-SECRET" not in
               (scope.org_dir(B) / "models.json").read_text(encoding="utf-8", errors="ignore"),
               "迁移没有把任何企业的密钥复制到 B")
    finally:
        for o in (A, B):
            shutil.rmtree(scope.org_dir(o), ignore_errors=True)


def test_c02_project_access(chk: Checker) -> None:
    print("\n[C02] 项目角色：服务端校验、只读不写、无访问权不下载")
    import contextlib
    from ..app import access, auth, store

    @contextlib.contextmanager
    def as_user(uid: str, role: str = "member", org: str = "default", **extra):
        tok = auth.set_current({"id": uid, "username": uid, "name": uid,
                                "role": role, "org_id": org, **extra})
        try:
            yield
        finally:
            auth.reset_current(tok)

    pid = make_project("C02权限", [{"title": "技术方案"}, {"title": "报价表"}])
    try:
        p = store.get_project(pid)
        chk.ok(p.get("owner_id") == "" and p.get("due_at") == ""
               and p.get("final_version") == "", "老项目自动补出协作字段且为空，旧数据可用")
        with as_user("legacy_user"):
            r = access.project_role(pid)
            chk.ok(r["legacy_open"] and r["role"] == "editor",
                   "没有成员名册的老项目按同企业可编辑兼容，并如实标注")
            chk.ok(access.can(pid, "write")[0], "老项目同企业成员仍可写")
        with as_user("org_owner", role="owner"):
            chk.ok(access.can(pid, "manage")[0], "企业管理员有项目负责人权限")
            chk.ok(auth.is_platform_admin(), "默认企业管理员同时是平台管理员")
        with as_user("other_admin", role="owner", org="org_x"):
            chk.ok(not auth.is_platform_admin(), "其他企业管理员不是平台管理员")

        for uid, role in (("u_lead", "lead"), ("u_editor", "editor"),
                          ("u_reviewer", "reviewer"), ("u_reader", "reader")):
            row = store.set_project_member(pid, uid, role)
            chk.ok(row["role"] == role, f"成员 {uid} 登记为 {role}")
        with as_user("stranger"):
            chk.ok(not access.can(pid, "read")[0], "不在成员名册里连查看都不行")
            chk.ok(not access.can(pid, "export")[0], "不在成员名册里不能下载")
            chk.ok(not access.guard(f"/api/projects/{pid}/export.docx", "GET")[0],
                   "无项目访问权下载 Word 被 403 拦住")

        for uid, can_write, can_export, can_manage in (
                ("u_lead", True, True, True), ("u_editor", True, True, False),
                ("u_reviewer", False, True, False), ("u_reader", False, False, False)):
            with as_user(uid):
                chk.ok(access.can(pid, "read")[0], f"{uid} 可以查看项目")
                chk.ok(access.can(pid, "write")[0] is can_write,
                       f"{uid} 写正文权限 = {can_write}")
                chk.ok(access.can(pid, "export")[0] is can_export,
                       f"{uid} 下载权限 = {can_export}")
                chk.ok(access.can(pid, "manage")[0] is can_manage,
                       f"{uid} 管理权限 = {can_manage}")

        with as_user("u_reader"):
            chk.ok(not access.guard(f"/api/projects/{pid}/section/s1", "PUT")[0],
                   "只读人员写章节接口被拦")
            chk.ok(not access.guard(f"/api/projects/{pid}/artifacts/a1/download", "GET")[0],
                   "只读人员下载产物被拦")
            chk.ok(access.guard(f"/api/projects/{pid}/bundle/scan", "POST")[0],
                   "只读人员仍可做不落盘的暗标扫描")
        with as_user("u_reviewer"):
            chk.ok(access.can(pid, "review")[0] and not access.can(pid, "write")[0],
                   "复核人员能复核，不能写正文")

        for path, method in (
                (f"/api/projects/{pid}/archive", "POST"),
                (f"/api/projects/{pid}/to-kb", "POST"),
                (f"/api/projects/{pid}/versions/b1/restore", "POST"),
                (f"/api/projects/{pid}", "DELETE"),
                (f"/api/projects/{pid}/team", "POST")):
            chk.ok(access.action_for(path, method) == (pid, "manage"),
                   f"{method} {path.split(pid)[-1]} 走管理权限校验")
        chk.ok(access.action_for(f"/api/projects/{pid}/export.docx", "GET")
               == (pid, "export"), "导出走下载权限校验")

        store.update_project(pid, owner_id="u_lead", due_at="2026-12-01",
                             final_version="v3")
        secs = store.list_sections(pid)
        store.assign_section(pid, secs[0]["id"], user_id="u_editor",
                             review_status="approved", reviewer="u_lead")
        rep = access.team_report(pid)
        chk.ok(rep["meta"]["due_at"] == "2026-12-01"
               and rep["meta"]["final_version"] == "v3",
               "项目负责人、截止时间、定稿版本随项目保存")
        chk.ok(rep["sections"][0]["assignee"] == "u_editor"
               and rep["sections"][0]["review_status"] == "approved",
               "章节分工与复核状态可读")
        row = next(x for x in store.list_section_assignees(pid)
                   if x["section_id"] == secs[0]["id"])
        chk.ok(row["reviewer"] == "u_lead" and bool(row["reviewed_at"]),
               "复核结论带责任人和时间")
        chk.ok(not rep["legacy_open"], "加了名册后切换为按名单授权")
        chk.ok(all(set(u) <= {"id", "name", "username", "role"}
                   for u in rep["users"]),
               "成员下拉只带展示字段，不外发账号敏感字段")
    finally:
        drop_project(pid)


def test_c03_long_tasks(chk: Checker) -> None:
    print("\n[C03] 长任务：取消边界、章节检查点、恢复保人工稿、重启状态")
    from ..app import agent, taskctl

    titles = [f"第{i + 1}章 技术方案" for i in range(5)]
    pid = make_project("C03长任务", [{"title": t} for t in titles])
    store.update_project(pid, outline=[{"title": t, "points": []} for t in titles])
    real_gen = agent.generate_section
    real_stage = agent.auto_core.run_stage
    agent.auto_core.run_stage = lambda *a, **k: {}
    state = {"cancel_jid": "", "calls": 0}
    try:
        def gen_ok(pid_, sid, **kw):
            store.update_section(sid, "按招标文件要求编写的正文内容。" * 6, pid=pid_)
            return {"text": "已写入", "quality": {"deliverable": True}}

        agent.generate_section = gen_ok
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        j = store.get_job(jid)
        chk.ok(j["status"] == "done", "正常整本生成仍标记完成", j["step"])
        cps = store.list_checkpoints(pid, jid)
        chk.ok(len(cps) == 5 and all(c["status"] == "written" for c in cps),
               "每章都留下检查点，且状态是已写入")
        chk.ok(bool(j["started_at"]) and bool(j["finished_at"]),
               "任务起止时间落库，排队时长可实测")
        chk.ok(not taskctl.plan(pid, jid, "resume")["resumable"],
               "全部写完时没有可恢复章节")

        for s in store.list_sections(pid):
            store.update_section(s["id"], "")

        def gen_cancel(pid_, sid, **kw):
            state["calls"] += 1
            if state["calls"] == 2:
                store.request_cancel(state["cancel_jid"])
            ab = kw.get("abort_check")
            if callable(ab) and ab():
                return {"text": "", "cancelled": True,
                        "message": "任务已取消，结果未写入"}
            store.update_section(sid, "模型稿内容。" * 8, pid=pid_)
            return {"text": "已写入", "quality": {"deliverable": True}}

        agent.generate_section = gen_cancel
        jid2 = store.create_job("compose", pid)
        state["cancel_jid"] = jid2
        state["calls"] = 0
        agent.compose(pid, jid2, False)
        j2 = store.get_job(jid2)
        chk.ok(j2["status"] == "cancelled" and j2["result"].get("cancelled"),
               "取消后任务如实标记已取消，不冒充完成", j2["step"])
        chk.ok("远端" in str(j2["result"].get("cancel_note") or ""),
               "明确说明取消不撤销已发出的远端模型计算")
        secs = store.list_sections(pid)
        chk.ok(not any(str(s.get("text") or "").strip() for s in secs[1:]),
               "取消后第 2 章起没有再写入正文，后续章没有继续调用模型")
        cps2 = {c["section_id"]: c["status"] for c in store.list_checkpoints(pid, jid2)}
        chk.ok(sum(1 for v in cps2.values() if v == "cancelled") >= 4,
               "未处理的章节都记为已取消，恢复时能看到")
        plan = taskctl.plan(pid, jid2, "resume")
        chk.ok(len(plan["resumable"]) >= 3, "取消过的章节可以从检查点恢复")

        agent.generate_section = gen_ok
        human_sid = plan["resumable"][0]
        human_text = "人工补稿：恢复任务绝不能覆盖这一版"
        store.update_section(human_sid, human_text, pid=pid)
        plan2 = taskctl.plan(pid, jid2, "resume")
        chk.ok(human_sid in plan2["kept_human"]
               and human_sid not in plan2["resumable"],
               "任务后的人工补稿被识别为「保留」，不进恢复清单")
        n_jid = store.create_job("compose", pid)
        # 故意把人工章塞回恢复清单，验证服务端仍不覆盖
        agent.compose_subset(pid, n_jid, [human_sid] + plan2["resumable"],
                             jid2, "resume")
        chk.ok(store.get_section(human_sid)["text"] == human_text,
               "恢复按章节版本核对，人工新稿没有被模型稿覆盖")
        chk.ok(any(c["section_id"] == human_sid and c["status"] == "kept_human"
                   for c in store.list_checkpoints(pid, n_jid)),
               "被保留的人工章写入检查点，说明原因")

        retry_plan = taskctl.plan(pid, jid2, "retry")
        chk.ok(retry_plan["mode"] == "retry"
               and set(retry_plan["resumable"]) <= set(plan["resumable"]),
               "重试只针对失败 / 空稿 / 取消章，不重跑已完成章节")
        live = store.create_job("compose", pid)
        store.update_job(live, status="running")
        c = taskctl.cancel(pid, live)
        chk.ok(c["ok"] and store.cancel_requested(live), "运行中的任务可以请求取消")
        chk.ok("远端计算" in c["note"], "取消说明里写明远端计算不会被撤销")
        store.update_job(live, status="cancelled")

        stale = store.create_job("compose", pid)
        store.update_job(stale, status="running", step="跑到一半")
        chk.ok(store.recover_stale_jobs() >= 1, "启动收尾会处理上一进程遗留任务")
        sj = store.get_job(stale)
        chk.ok(sj["status"] == "interrupted" and sj["recoverable"] == 1,
               "重启后旧 running 标为中断且可恢复，不假装仍在执行")
        chk.ok("可恢复" in str(sj.get("error") or ""), "中断原因写明检查点可恢复")
        chk.ok(not store.active_job(pid), "中断任务不再挡住同项目的新任务")

        m = store.concurrency_advice()
        chk.ok(m["current_workers"] == 1 and m["implemented"] is False,
               "并发建议如实写明当前还是单进程单线程")
        chk.ok("跨进程" in m["boundary"] and "samples" in m,
               "写明多进程部署前需跨进程认领 / 锁，且排队时间取自实测样本")
        a, busy = store.create_job_exclusive("compose", pid)
        b, busy2 = store.create_job_exclusive("compose", pid)
        chk.ok(bool(a) and not b and busy2.get("id") == a,
               "同项目冲突任务不会同时领到写权限")
        store.update_job(a, status="done")
    finally:
        agent.generate_section = real_gen
        agent.auto_core.run_stage = real_stage
        drop_project(pid)


# ---------------- C04：一致性备份 / 隔离恢复 / 恢复演练 ----------------

def test_c04_backup(chk: Checker) -> None:
    print("\n[C04] 备份覆盖完整、SQLite 一致快照、只做隔离恢复")
    import sqlite3
    from ..app import config
    from ..ops import backup

    store.init_db()
    root = Path(config.DATA_DIR).resolve()
    work = Path(tempfile.mkdtemp(prefix="bidagent_bk_"))
    target = work / "restore"
    pid = make_project("自测-备份恢复", [{"title": "第一章 技术方案"}])
    extra = ""
    saved: dict[str, bytes | None] = {}
    had_auth = (root / "auth.db").is_file()
    try:
        for rel in ("models.json", "agent_config.json"):
            p = root / rel
            saved[rel] = p.read_bytes() if p.is_file() else None
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, "备份时的正文，不许被后续写入带进快照")

        # 造齐 C04 要求的五类：库 / 账号 / 原件 / 输出 / 配置
        (root / "originals" / pid).mkdir(parents=True, exist_ok=True)
        (root / "originals" / pid / "原件.txt").write_text("原件内容", encoding="utf-8")
        (root / "exports" / pid).mkdir(parents=True, exist_ok=True)
        (root / "exports" / pid / "投标书.docx").write_bytes(b"FAKE-DOCX")
        (root / "models.json").write_text('{"secret": "TEST-SECRET"}', encoding="utf-8")
        (root / "agent_config.json").write_text("{}", encoding="utf-8")
        (root / "orgs").mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(root / "orgs" / "acme.db")) as c:
            c.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT)")
            c.execute("INSERT INTO projects VALUES ('acme-1')")
        # 账号库可能已由 C02 测试建好：只建一张兜底表，不改任何账号数据
        with sqlite3.connect(str(root / "auth.db")) as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (id TEXT, name TEXT)")
            auth_rows = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        # 这两个不该进包：WAL 未落盘、临时文件
        (root / "app.db-wal").write_bytes(b"should-not-be-in-backup")
        (root / "tmp.json.tmp").write_text("x", encoding="utf-8")

        with store.conn() as c:
            before = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

        made = backup.create(root=root, out_dir=work, include_env=False)
        zp = made["path"]
        m = made["manifest"]
        paths = {e["path"] for e in m["entries"]}
        for want in ("data/app.db", "data/auth.db", "data/orgs/acme.db",
                     "data/models.json", "data/agent_config.json",
                     f"data/originals/{pid}/原件.txt",
                     f"data/exports/{pid}/投标书.docx"):
            chk.ok(want in paths, f"备份覆盖 {want}")
        chk.ok(all("app.db-wal" not in p and ".tmp" not in p for p in paths),
               "WAL / SHM / 临时文件不进备份包")
        chk.ok(not any(p.startswith("env/") for p in paths),
               "include_env=False 时不把本机真实 .env 密钥带进测试备份")
        dbs = [e for e in m["entries"] if e["kind"] == "sqlite"]
        chk.ok(len(dbs) >= 3 and all(e.get("row_counts") for e in dbs),
               "每个数据库都记了表行数，供恢复时对账")

        vr = backup.verify(zp)
        chk.ok(vr["ok"] and vr["checked"] == vr["total"],
               "逐文件 sha256 + 每个库 integrity_check 全部通过", str(vr["problems"]))
        chk.ok(all(d["integrity"] == "ok" for d in vr["databases"]),
               "快照是完整 SQLite 文件，不是复制的半截库")

        # 备份之后的新写入不能混进一致性快照
        extra = store.create_project("自测-备份后才建的")["id"]
        snap = work / "snap.db"
        with zipfile.ZipFile(zp) as z:
            snap.write_bytes(z.read("data/app.db"))
        with sqlite3.connect(str(snap)) as c:
            after = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        with store.conn() as c:
            live = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        chk.ok(after == before and live > before,
               "备份瞬间之后的新写入不在快照里（Online Backup 一致点）",
               f"snap={after} live={live} before={before}")

        chk.ok(not backup.restore(zp, root)["ok"] and
               backup.restore(zp, root)["code"] == "refuse_live",
               "拒绝恢复到生产数据目录")
        chk.ok(backup.restore(zp, root / "sub").get("code") == "refuse_live",
               "连生产数据目录的子目录也拒绝，避免混写")

        r = backup.restore(zp, target)
        chk.ok(r["ok"], "隔离恢复到空目录成功", str(r.get("bad")) or r.get("reason", ""))
        if r["ok"]:
            with sqlite3.connect(str(target / "data" / "app.db")) as c:
                good = (c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                        and c.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == before)
            chk.ok(good, "恢复出来的业务库可读、行数与备份时一致")
            with sqlite3.connect(str(target / "data" / "auth.db")) as c:
                auth_after = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            chk.ok(auth_after == auth_rows, "账号 / 组织 / 会话库一并恢复且行数对账")
            chk.ok((target / "data" / "models.json").read_text(encoding="utf-8")
                   == '{"secret": "TEST-SECRET"}', "配置文件按内容恢复，不是空壳")
        chk.ok(backup.restore(zp, target).get("code") == "target_not_empty",
               "目标目录已有数据就拒绝再恢复，绝不覆盖")

        # 篡改过的包必须在恢复前被挡住
        badzip = work / "tampered.zip"
        shutil.copy2(zp, badzip)
        with zipfile.ZipFile(badzip, "a") as z:
            z.writestr("data/rogue.txt", "x")
        bad = backup.verify(badzip)
        chk.ok(not bad["ok"] and any("未登记" in x for x in bad["problems"]),
               "备份包里多出未登记文件 → 校验失败")
        chk.ok(backup.restore(badzip, work / "never").get("code") == "bad_backup",
               "校验失败的包不会恢复任何文件")

        dr = backup.drill(zp)
        chk.ok(dr["restore_ok"] and dr["databases_opened"] and
               all(x["match_manifest"] for x in dr["databases_opened"].values()),
               "隔离恢复演练：临时目录恢复 + 开库对账通过")
        chk.ok("不等于业务验收" in dr["boundary"],
               "演练结论写明：能读回不等于业务验收通过")

        rep = Path(__file__).resolve().parents[2] / "docs" / "验收报告.md"
        text = rep.read_text(encoding="utf-8") if rep.is_file() else ""
        chk.ok("构造用例 PASS 不能替代业务验收" in text and "待业务验收" in text
               and "5—10 组" in text,
               "验收报告骨架如实标注构造用例不能替代真实样本与业务复核")
    finally:
        drop_project(pid)
        if extra:
            drop_project(extra)
        for rel in (f"originals/{pid}/原件.txt", f"exports/{pid}/投标书.docx",
                    "orgs/acme.db", "app.db-wal", "tmp.json.tmp"):
            try:
                (root / rel).unlink()
            except OSError:
                pass
        if not had_auth:
            try:
                (root / "auth.db").unlink()
            except OSError:
                pass
        for rel, raw in saved.items():
            try:
                if raw is None:
                    (root / rel).unlink()
                else:
                    (root / rel).write_bytes(raw)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)


# ---------------- 手动调整目录：加章 / 改名 / 删章 ----------------

def test_section_crud(chk: Checker) -> None:
    print("\n[目录] 手动加章 / 改名 / 删章：不重建目录，正文与版本不丢")
    from ..app import access
    pid = make_project("自测-手动调整目录", [
        {"title": "第一章 项目理解"}, {"title": "第二章 技术方案"},
        {"title": "第三章 服务保障"}])
    try:
        secs = store.list_sections(pid)
        a, b = secs[0], secs[1]
        store.update_section(a["id"], "第一章正文，改名后必须还在")
        store.update_project(pid, outline=[
            {"title": s["title"], "points": ["要点" + str(i)]}
            for i, s in enumerate(secs)])
        before_ver = store.get_section(a["id"]).get("ver")

        r = store.rename_section(pid, a["id"], "第一章 项目理解（修订）")
        chk.ok(r.get("ok") and r["text_kept"], "改名成功且原正文保留", str(r))
        row = store.get_section(a["id"])
        chk.ok(row["title"] == "第一章 项目理解（修订）"
               and row["text"].startswith("第一章正文"), "改名不改 id、不换正文")
        chk.ok(row.get("ver") == before_ver, "改名不推进正文版本（不等于重写）")
        outline = store.get_project(pid)["outline"]
        chk.ok(outline[0]["title"] == "第一章 项目理解（修订）"
               and outline[0].get("points") == ["要点0"],
               "目录结构同步改名且保留原有章节要点", str(outline[:1]))
        chk.ok(store.rename_section(pid, a["id"], "  ").get("error") == "invalid",
               "空标题不能改名")
        chk.ok(store.rename_section(pid, a["id"], b["title"]).get("error") == "duplicate",
               "改成已有同名标题被拒")
        chk.ok(store.rename_section(pid, "not-a-section", "X").get("error") == "not_in_project",
               "不存在的章节不能改名")

        ids0 = {s["id"] for s in store.list_sections(pid)}
        r = store.add_section(pid, "第四章 实施组织", after_sid=b["id"])
        chk.ok(r.get("ok") and r["seq"] == 2 and r["total"] == 4,
               "可在指定章之后插一章", str(r))
        new_id = r["id"]
        cur = store.list_sections(pid)
        chk.ok([s["seq"] for s in cur] == [0, 1, 2, 3], "插入后序号连续")
        chk.ok(ids0 <= {s["id"] for s in cur}, "加章不重建已有章节 id")
        chk.ok(store.get_project(pid)["outline"][2]["title"] == "第四章 实施组织",
               "加章同步目录结构")
        chk.ok(store.add_section(pid, "  ").get("error") == "invalid", "加章空标题被拒")
        chk.ok(store.add_section(pid, "第四章 实施组织").get("error") == "duplicate",
               "加同名章被拒")
        chk.ok(store.add_section(pid, "X" * 121).get("error") == "invalid", "超长标题被拒")
        chk.ok(store.add_section(pid, "第五章", after_sid="other").get("error")
               == "not_in_project", "插入位置不属本项目被拒")
        q = store.add_section(pid, "第六章 报价表")
        chk.ok(q.get("ok") and q.get("protected") is True, "报价类新章自动标为保护章节")
        store.update_section(new_id, "新章正文，删掉之前写的")

        # 给被删章挂上关联数据，验证只清这一章、要求本身不丢
        with store.conn() as c:
            c.execute("INSERT INTO evidence(id,project_id,section_id,created_at) "
                      "VALUES(?,?,?,?)", ("ev1", pid, new_id, "2026-01-01"))
            c.execute("INSERT INTO section_quality(section_id,project_id,updated_at) "
                      "VALUES(?,?,?)", (new_id, pid, "2026-01-01"))
            c.execute("INSERT INTO section_assignees(project_id,section_id,updated_at) "
                      "VALUES(?,?,?)", (pid, new_id, "2026-01-01"))
            c.execute("INSERT INTO requirement_responses"
                      "(id,project_id,req_id,section_id,status,updated_at) "
                      "VALUES(?,?,?,?,?,?)",
                      ("rr1", pid, "req1", new_id, "confirmed", "2026-01-01"))
        d = store.delete_section(pid, new_id)
        chk.ok(d.get("ok") and d.get("restorable") and d["chars"] > 0,
               "删章成功、记录被删正文并留可撤销快照", str(d))
        cur = store.list_sections(pid)
        chk.ok(new_id not in {s["id"] for s in cur}
               and [s["seq"] for s in cur] == list(range(len(cur))),
               "被删章消失且序号重排连续")
        chk.ok(all(store.get_section(s["id"])["text"] == s["text"] for s in cur),
               "删章不影响其它章节正文")
        chk.ok(all(o["title"] == s["title"]
                   for o, s in zip(store.get_project(pid)["outline"], cur)),
               "删章后目录结构与章节逐条一致")
        with store.conn() as c:
            ev = c.execute("SELECT COUNT(*) FROM evidence WHERE section_id=? AND status!='historical'",
                           (new_id,)).fetchone()[0]
            qn = c.execute("SELECT COUNT(*) FROM section_quality WHERE section_id=?",
                           (new_id,)).fetchone()[0]
            an = c.execute("SELECT COUNT(*) FROM section_assignees WHERE section_id=?",
                           (new_id,)).fetchone()[0]
            rr = c.execute("SELECT status,section_id FROM requirement_responses "
                           "WHERE id='rr1'").fetchone()
        chk.ok(ev == 0 and qn == 0 and an == 0, "被删章的依据退为历史，当前质量 / 分工清理")
        chk.ok(rr is not None and rr[0] == "missing" and not rr[1],
               "逐条响应退回「未落实」，不把要求本身删掉")
        chk.ok(store.delete_section(pid, "not-a-section").get("error") == "not_in_project",
               "不存在的章节不能删")

        rb = store.restore_batch(pid, d["batch_id"])
        restored = store.list_sections(pid)
        chk.ok(rb.get("ok") and any(
            s["title"] == "第四章 实施组织" and "新章正文" in (s["text"] or "")
            for s in restored), "版本与回退能整份撤销删除（含被删章正文）", str(rb)[:160])

        for path, method in (("/api/projects/P/sections", "POST"),
                             ("/api/projects/P/sections/S", "PUT"),
                             ("/api/projects/P/sections/S", "DELETE")):
            got_pid, act = access.action_for(path, method)
            chk.ok(got_pid == "P" and act == "write",
                   f"新接口 {method} {path} 按「写正文」校验权限")
        chk.ok("outline" in store.WRITE_JOB_KINDS, "调整目录纳入同项目写互斥")
    finally:
        drop_project(pid)


def test_req_table_delete(chk: Checker) -> None:
    """招标要求表：大项（整组）和小项（单条）都能删，删后不残留响应记录。"""
    from ..app import reqmatrix, reqtable
    orig_sync = reqtable.sync
    reqtable.sync = lambda *a, **k: {"added": 0}   # 不触发基座抽取，只测删除链
    pid = make_project("自测-要求表删除", [{"title": "第一章 技术方案"}])
    try:
        reqtable.add_row(pid, "hard", "投标人须具备一级资质，且提供原件核验")
        reqtable.add_row(pid, "hard", "项目团队须配置专职安全员一名")
        reqtable.add_row(pid, "rejection", "未按要求密封的投标文件将被拒收")
        rows = reqtable.list_rows(pid)
        small = next(r for r in rows if "一级资质" in r["text"])
        store.save_req_response(pid, small["id"], status="confirmed", reviewer="自测")

        chk.ok(reqtable.delete_row(pid, small["id"])["ok"], "小项可以删除")
        chk.ok(all("一级资质" not in r["text"] for r in reqtable.list_rows(pid)),
               "删掉的小项不再出现在要求表")
        chk.ok(not any(str(x.get("req_id")) == small["id"]
                       for x in store.list_req_responses(pid)),
               "删小项时逐条响应记录一起清掉，不残留")

        g = reqtable.delete_group(pid, "hard")
        chk.ok(g["ok"] and g["deleted"] == 1, "大项可以删除：整组要求一起删掉", str(g))
        sm = reqtable.summary(pid)
        chk.ok("hard" in sm["hidden_groups"], "删掉的大项记入已删除名单")
        chk.ok("hard" not in sm["labels"], "已删大项不再下发到界面，不显示空分组")
        chk.ok(not any(r["kind"] == "hard" for r in reqtable.list_rows(pid)),
               "大项下没有残留要求")
        chk.ok(reqtable.delete_row(pid, "rejection")["deleted"] == 1,
               "按分类标识删除也能删掉对应大项")
        chk.ok(not reqtable.delete_group(pid, "不存在")["ok"],
               "不存在的分类不会被误删")

        reqtable.add_row(pid, "hard", "新增一条后大项重新出现")
        chk.ok("hard" not in reqtable.summary(pid)["hidden_groups"],
               "重新在这一组新增要求时，大项自动恢复显示")
        store.clear_suppressed(pid)   # 「恢复被删要求」的核心动作
        sm2 = reqtable.summary(pid)
        chk.ok(sm2["hidden_groups"] == [], "点「恢复被删要求」后已删大项可找回")
        chk.ok(all(k in sm2["labels"] for k in ("hard", "rejection", "scoring")),
               "恢复后三个大项都还在")
        rep = reqmatrix.build(pid)
        chk.ok(all("一级资质" not in x["text"] for x in rep["items"]),
               "矩阵按删后的要求重算，不显示已删小项")
    finally:
        reqtable.sync = orig_sync
        drop_project(pid)


def test_export_ack(chk: Checker) -> None:
    """导出不符合只作提醒：可以照常下载，风险如实记录，严格模式仍保留。"""
    from ..app import auto_core, delivery
    from ..app import main as app_main
    pid = make_project("自测-知情导出", [{"title": "第一章 技术方案"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, "本方案 XXX 待补充，报价 TODO，尚未填写完整")
        gate = auto_core.export_gate(pid)
        chk.ok(bool(gate["issues"]), "检查如实列出不符合项", str(len(gate["issues"])))
        strict = auto_core.issue_export_token(
            pid, gate["fingerprint"],
            [x["key"] for x in gate["issues"] if x["level"] == "高"])
        chk.ok(not strict.get("ok"), "严格逐条确认路径仍按原规则拒绝硬伤")

        tok = auto_core.issue_ack_token(pid, gate["fingerprint"])
        chk.ok(tok.get("ok") and tok["acknowledged"]["issues"] == len(gate["issues"]),
               "知情导出令牌不受不符合项影响，并记下提醒数量")
        chk.ok(auto_core.consume_ack_token(pid, gate["fingerprint"], "假令牌") is None,
               "随便一个令牌不能当知情确认用")
        tok2 = auto_core.issue_ack_token(pid, gate["fingerprint"])
        snap = delivery.freeze(pid)
        g2, denied = app_main._check_export_gate(pid, tok2["token"], snap)
        chk.ok(denied is None and bool(g2.get("acknowledged")),
               "带知情令牌时，不符合项只作提醒，不再禁止导出")
        chk.ok(auto_core.consume_ack_token(pid, gate["fingerprint"], tok2["token"]) is None,
               "知情令牌一次性，不能拿同一枚反复导出")
        conf = app_main.export_confirm(
            pid, {"fingerprint": gate["fingerprint"], "acknowledge": True})
        chk.ok(conf.get("ok") and bool(conf.get("token")) and bool(conf.get("warning")),
               "确认接口支持知情导出，并把提醒写进返回说明")
        _, denied_plain = app_main._check_export_gate(pid, "", snap)
        chk.ok(denied_plain is not None,
               "不带令牌时仍按原规则列出问题，不静默放行")

        bad = ("# 测试\n\nXXX 待补充").encode("utf-8")
        plain = {k: v for k, v in g2.items() if k != "acknowledged"}
        r0 = app_main._deliver(pid, snap, plain, bad, "未确认.md", "md", "text/markdown")
        chk.ok(getattr(r0, "status_code", 200) == 409,
               "没有确认时，最终文件检查没过仍按原规则阻断")
        g3, denied3 = app_main._check_export_gate(
            pid, auto_core.issue_ack_token(pid, gate["fingerprint"])["token"], snap)
        chk.ok(denied3 is None, "重新确认后可以继续")
        r1 = app_main._deliver(pid, snap, g3, bad, "知情导出.md", "md", "text/markdown")
        chk.ok(getattr(r1, "status_code", 200) == 200,
               "知情导出时，最终文件检查没过也照常发文件")
        chk.ok(r1.headers.get("x-export-ack") == "1", "响应明确标出这是知情导出")
        chk.ok(r1.headers.get("x-file-check") == "acknowledged",
               "不把没通过的最终文件检查说成通过")
        art = store.get_artifact_by_id(r1.headers.get("x-artifact-id") or "")
        chk.ok("export_ack" in str(art.get("options") or ""),
               "产物登记留下知情导出标记，事后可追溯")
        chk.ok("acknowledged" in str(art.get("file_check") or ""),
               "产物里如实记录最终文件检查未通过")
        chk.ok(b"XXX" in r1.body, "文件正文保持原样，不偷偷改内容冒充已修好")

        import json as _json
        plain_msg = str(_json.loads(denied_plain.body).get("error") or "")
        chk.ok("禁止导出" not in plain_msg and "不能导出" not in plain_msg,
               "导出提醒文案不再写成禁止下载")
        chk.ok(all("不能导出" not in (x.get("title") or "")
                   and "不能导出" not in (x.get("detail") or "")
                   for x in gate["issues"]),
               "提醒清单里不再出现「不能导出」措辞")

        # 连「最终文件检查没跑完」也不能把已知情的用户挡在下载之外
        orig_inspect = delivery.inspect_document
        delivery.inspect_document = lambda *a, **k: {
            "checked": False, "ok": False, "issues": [], "blocking": [],
            "summary": "最终文件检查未完成"}
        try:
            tok4 = auto_core.issue_ack_token(pid, gate["fingerprint"])
            g4, denied4 = app_main._check_export_gate(pid, tok4["token"], snap)
            r2 = app_main._deliver(pid, snap, g4, "# 测试".encode("utf-8"),
                                   "检查未完成.md", "md", "text/markdown")
            chk.ok(denied4 is None and getattr(r2, "status_code", 200) == 200,
                   "最终文件检查没跑完，知情导出也照常发文件")
            chk.ok(r2.headers.get("x-file-check") == "acknowledged",
                   "检查没跑完不写成已通过")
            art2 = store.get_artifact_by_id(r2.headers.get("x-artifact-id") or "")
            chk.ok("file_check_completed" in str(art2.get("options") or ""),
                   "产物里如实记下这次检查没跑完")
        finally:
            delivery.inspect_document = orig_inspect

        tok3 = auto_core.issue_ack_token(pid, gate["fingerprint"])
        store.update_section(sid, "改过的正文，仍然留着 XXX 占位符")
        new_gate = auto_core.export_gate(pid)
        chk.ok(auto_core.consume_ack_token(pid, new_gate["fingerprint"], tok3["token"]) is None,
               "正文改过之后旧提醒令牌失效，要重新看提醒")
    finally:
        drop_project(pid)


def test_project_lifecycle(chk: Checker) -> None:
    print("\n[A7] 项目归档 / 收进知识库 / 删除")
    from ..app import main as app_main
    titles = ["第一章 技术方案", "第二章 售后服务", "第三章 项目实施"]
    pid = make_project("自测-项目生命周期", [{"title": t} for t in titles])
    did = ""
    try:
        by = {s["title"]: s["id"] for s in store.list_sections(pid)}
        store.update_section(by[titles[0]], "按招标文件要求编写的正文。" * 8)
        store.update_section(by[titles[1]], "售后服务承诺与响应时限说明。" * 8)
        st = store.project_stats()[pid]
        chk.ok(st["sections"] == 3 and st["written"] == 2 and st["open_chapters"] == 1,
               "进度统计：3 章写了 2 章、还差 1 章", str(st))

        app_main.project_archive(pid, {"on": True})
        chk.ok(not any(p["id"] == pid for p in store.list_projects()),
               "归档后不出现在在用列表")
        chk.ok(any(p["id"] == pid for p in store.list_projects(include_archived=True)),
               "归档后仍能在全量列表里找到")
        app_main.project_archive(pid, {"on": False})
        chk.ok(any(p["id"] == pid for p in store.list_projects()), "取消归档后回到列表")

        r = app_main.project_to_kb(pid, {})
        did = r.get("doc_id") or ""
        chk.ok(r.get("ok") and r.get("chapters") == 2, "整本收进知识库，空章跳过", str(r))
        chk.ok(len(r.get("empty") or []) == 1, "没写正文的章节单独列出来", str(r.get("empty")))
        chk.ok(store.kb_source_pids().get(pid) == did, "项目列表能识别「已收录」")
        r2 = app_main.project_to_kb(pid, {})
        chk.ok(r2.get("updated") is True and r2.get("doc_id") == did,
               "重复收录更新同一篇，不堆同名副本", str(r2.get("updated")))

        out = store.delete_project(pid)
        chk.ok(out.get("sections") == 3 and not store.get_project(pid),
               "删除项目时章节一并清掉", str(out))
        chk.ok(not any(p["id"] == pid for p in store.list_projects(include_archived=True)),
               "删除后全量列表也没有了")
        chk.ok(bool(store.get_doc(did)), "知识库里的成稿不随项目删除")
    finally:
        if did:
            store.delete_doc(did)
        drop_project(pid)


MANUAL = """
以下交互已用无头浏览器脚本跑过（脚本放 tmp/，不入库）：
  1. 保存稿A 期间继续输入稿B：saveSecNow 返回 false、secDirty 仍为 true、编辑框仍是 B；
     停手后 B 自动保存，secDirty 转 false。
  2. 让保存接口返回 500：点「下一章」停在本章，secDirty 保持 true。
  3. 保存失败时点「下载 Word」：不发起导出请求。

仍需人工过一遍：
  4. 同一章用另一个标签页改并保存 → 这边再保存应出现红色冲突条，
     「用我的版本覆盖」后两边内容一致；「换成服务端版本」后本地改为服务端内容。
  5. 输入后立刻点「下载 Word」→ 导出的文件内容包含刚输入的文字。
  6. 连点两下「一键写整本」→ 只应有一个任务卡，提示已接上原任务；
     整本生成进行中点某一章「生成本章」→ 应提示任务在跑，不并入第二个写任务。
"""


def test_writing_context(chk: Checker) -> None:
    """生成链路：按章拿到真正的招标条款，且整本生成不会因预算中途变空稿。"""
    print("\n[GEN] 按章招标上下文 / 跨章一致性 / 整本预算")
    from ..app import agent, chains, config, delivery, rules, writing_context

    # 一份「前部是公告、技术参数散在后部」的长招标文件
    head = "第一章 招标公告：本项目为信息化设备采购，投标人须知如下。\n" * 240
    mid = "第二章 投标人须知：投标截止时间与保证金按规定执行。\n" * 240
    tail = ("\n【第 31 页】\n"
            "5.3 技术规格要求：存储设备须支持录像保存不少于 90 天，可用性不低于 99.9%。\n"
            "5.4 投标人须提供三年维保承诺，故障响应不超过 30 分钟。\n"
            "5.5 评分办法：技术方案 40 分，售后服务 20 分。\n")
    long_text = head + mid + tail * 8

    clauses = writing_context.split_clauses(long_text)
    chk.ok(bool(clauses) and "招标公告" in clauses[0]["text"],
           "首个页标记之前的招标公告没有被切掉", f"{len(clauses)} 段")
    got, cov = writing_context.retrieve_for_chapter(
        long_text, "售后服务与运维保障 响应时限 录像保存 故障响应 30 分钟", budget=9000)
    chk.ok(not cov["full"] and cov["selected"] > 0,
           "超长招标文件走按章检索而不是整份截断", str(cov))
    chk.ok("90 天" in got and "30 分钟" in got,
           "检索命中散在文件后部的技术与服务条款")
    chk.ok("第一章 招标公告" not in got,
           "不再把文件开头的公告重复灌给本章")
    short, meta = writing_context.retrieve_for_chapter("须提供营业执照。", "资格")
    chk.ok(short == "须提供营业执照。" and meta["full"],
           "短招标文件整份注入，不因检索丢内容")

    pid = make_project("自测-按章生成", [{"title": "技术方案与逐条响应"},
                                    {"title": "售后服务与运维保障"},
                                    {"title": "项目组织与实施计划"}])
    store.update_project(pid, tender_text=long_text, outline=[
        {"title": s["title"], "points": ["响应时限", "服务内容"]}
        for s in store.list_sections(pid)])
    sid = store.list_sections(pid)[1]["id"]
    store.add_fact(pid, "本公司", "服务网点", "12", unit="处",
                   verification="verified:人工核验", source_loc="附件3")
    store.add_fact(pid, "本公司", "草稿业绩", "99", verification="unverified")

    captured: dict = {}
    real_llm, real_cons = agent._llm, agent.auto_core.constraints
    agent.auto_core.constraints = lambda *a, **k: "【本章相关要求】故障响应不超过 30 分钟"

    def cap(pid_, task, variables, **kw):
        captured.update(variables)
        captured["_kw"] = kw
        return "按招标文件要求逐条响应，措施、责任与验收口径均已明确。" * 20

    agent._llm = cap
    try:
        agent.generate_section(pid, sid, prev_brief="《技术方案与逐条响应》已写：总体架构")
    finally:
        agent._llm, agent.auto_core.constraints = real_llm, real_cons

    tender = str(captured.get("tender") or "")
    materials = str(captured.get("materials") or "")
    chk.ok("30 分钟" in tender and "第一章 招标公告" not in tender,
           "写章实际收到的招标原文是按本章检索的，不再是开头 6000 字",
           f"tender {len(tender)} 字")
    chk.ok("检索覆盖" in materials and "已写章节摘要" in materials,
           "覆盖说明与跨章摘要完整保留，没被长资料截掉")
    chk.ok("故障响应不超过 30 分钟" in materials,
           "本章要求清单没被覆盖说明或长资料挤掉")
    chk.ok("服务网点" in materials and "12处" in materials,
           "已核验企业事实进入写作上下文")
    chk.ok("草稿业绩" not in materials and "99" not in materials.split("企业事实")[-1][:200],
           "未核验事实不进提示词")
    chk.ok(int((captured.get("_kw") or {}).get("max_tokens") or 0) >= 2000,
           "单章输出上限已提高，不再被 1200 tokens 卡成短文")

    titles = ["技术方案与逐条响应", "售后服务与运维保障", "项目组织与实施计划"]
    items = [{"id": f"R{i}", "kind": "hard",
              "text": f"第{i}条 投标人须满足招标文件技术要求与验收口径"}
             for i in range(1, 61)]
    buckets = writing_context.assign_requirements(items, titles)
    assigned = [x["id"] for v in buckets.values() for x in v]
    chk.ok(len(assigned) == 60 and len(set(assigned)) == 60,
           "60 条要求每条都被恰好一章认领，没有遗漏也没有重复", str(len(assigned)))
    chk.ok("共 60 条" in writing_context.render_assignment(
        buckets[titles[0]], 60, titles[0]),
        "本章约束如实写出全表规模，不把部分分配冒充全部响应")

    # 占位文字：demo 稿不该留内部标注，检查层要能拦住真实模型写出来的同类文字
    demo = chains._demo_response(
        "section", "章节标题：技术方案\n章节要点：架构\n本项目我提供的资料：\n（无）\n"
                   "企业素材库命中：\n（无）")
    chk.ok("需人工补充" not in demo and "未提供" not in demo,
           "演示模型正文不再出现「未提供 / 需人工补充」内部标注")
    chk.ok(bool(rules.PLACEHOLDER.search("服务响应时限：需人工补充。")),
           "规则层能识别「需人工补充」占位文字")
    fc = delivery.inspect_document("服务响应时限：需人工补充。".encode("utf-8"),
                                   "md", {"sections": []}, blind=False)
    chk.ok(any(i["code"] == "placeholder" for i in fc["issues"]),
           "最终文件检查能拦下同类占位文字")

    # 旧默认 24000 会在真实模型下第 2~3 章耗尽预算，后面的章全变空稿
    cap_tokens = int(config.budget()["per_task_token_limit"])
    chk.ok(cap_tokens >= 7 * 12000,
           "整本生成预算足够写完全部章节，不会中途降级成空稿", str(cap_tokens))

    # compose 把已写章节摘要与本章认领的要求一起传给后续章节
    from ..app import reqtable
    store.add_requirements(pid, [
        {"kind": "hard", "text": f"第{i}条 投标人须满足招标技术要求",
         "source": "manual", "status": "active", "required": 1}
        for i in range(1, 9)])
    store.set_req_sync_hash(pid, reqtable.tender_hash(pid))
    seen: list[str] = []
    seen_req: list[str] = []
    real_gen, real_stage = agent.generate_section, agent.auto_core.run_stage
    agent.auto_core.run_stage = lambda *a, **k: {}

    def fake_gen(pid_, sid_, **kw):
        seen.append(str(kw.get("prev_brief") or ""))
        seen_req.append(str(kw.get("requirement_text") or ""))
        store.update_section(sid_, "按招标文件要求编写的正文内容。" * 6, pid=pid_)
        return {"text": "已写入", "quality": {"deliverable": True}}

    agent.generate_section = fake_gen
    try:
        for s in store.list_sections(pid):
            store.update_section(s["id"], "")
        jid = store.create_job("compose", pid)
        agent.compose(pid, jid, False)
        chk.ok(len(seen) >= 2 and any("《" in b for b in seen[1:]),
               "整本生成时后续章节拿到了前面已写章节的摘要", str(seen))
        chk.ok(any("本章认领的招标要求" in x for x in seen_req),
               "整本生成时每章拿到了按全表分配的要求，而非各自重挑前 12 条",
               str(seen_req)[:160])
    finally:
        agent.generate_section, agent.auto_core.run_stage = real_gen, real_stage

    # 验标书：AI 复核也不再只看招标文件开头 3000 字
    cap2: dict = {}
    real_check_llm, real_check_stage = agent._llm, agent.auto_core.run_stage
    agent.auto_core.run_stage = lambda *a, **k: {"issues": []}

    def cap_review(pid_, task, variables, **kw):
        cap2.update(variables)
        return {"issues": []}

    agent._llm = cap_review
    try:
        agent.check(pid)
    finally:
        agent._llm, agent.auto_core.run_stage = real_check_llm, real_check_stage
    chk.ok("90 天" in str(cap2.get("tender") or ""),
           "验标书的 AI 复核也能看到招标文件中后部的技术条款")
    drop_project(pid)


def test_real_world_guards(chk: Checker) -> None:
    print("\n[真实资料] 版本锁、分册空包、基座越权")
    import contextlib
    from fastapi import HTTPException
    from ..app import access, auth, bidbundle, main as app_main, store

    @contextlib.contextmanager
    def as_user(uid: str, role: str = "member", org: str = "default"):
        tok = auth.set_current({"id": uid, "username": uid, "name": uid,
                                "role": role, "org_id": org})
        try:
            yield
        finally:
            auth.reset_current(tok)

    pid = make_project("真实资料加固", [{"title": "技术方案"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, "初始稿", base_ver=0)

        for bad in ("abc", -1, "1.5"):
            try:
                app_main.section_save(pid, sid, {"text": "坏版本稿", "base_ver": bad})
                chk.ok(False, f"base_ver={bad!r} 应被拒绝")
            except HTTPException as exc:
                chk.ok(exc.status_code == 400, f"base_ver={bad!r} 返回 400",
                       str(exc.status_code))
        chk.ok(store.get_section(sid)["text"] == "初始稿", "非法版本号没有改动正文")

        try:
            app_main.section_save(pid, sid, {"text": "静默覆盖稿"})
            chk.ok(False, "不带 base_ver 的保存应被拒绝")
        except HTTPException as exc:
            chk.ok(exc.status_code == 400, "不带 base_ver 返回 400", str(exc.status_code))
        chk.ok(store.get_section(sid)["text"] == "初始稿", "无版本保存没有改动正文")

        r = app_main.section_save(pid, sid, {"text": "确认覆盖稿", "allow_overwrite": True})
        chk.ok(r.get("ok") and store.get_section(sid)["text"] == "确认覆盖稿",
               "显式 allow_overwrite 仍可无条件覆盖")
        v = store.get_section(sid)["ver"]
        chk.ok(app_main.section_save(pid, sid, {"text": "带版本稿", "base_ver": v}).get("ok"),
               "正确 base_ver 正常写入")
        stale = app_main.section_save(pid, sid, {"text": "旧稿", "base_ver": v})
        chk.ok(getattr(stale, "status_code", 0) == 409, "过期 base_ver 仍返回 409")

        real = (bidbundle.build, app_main.delivery.freeze,
                app_main._check_export_gate, app_main.delivery.register)
        ack_bundle = {
            "ok": True, "data": b"",
            "blocking": [{"volume": "技术文件", "file": "a.docx",
                          "summary": "未通过", "issues": []}],
            "manifest": {"warnings": [], "volumes": [], "final_checks": [],
                         "capability_note": []},
            "checks": [], "warnings": []}
        registered: list[int] = []
        bidbundle.build = lambda *a, **k: ack_bundle
        app_main.delivery.freeze = lambda _pid: {"name": "测试标书", "fingerprint": "fp"}
        app_main._check_export_gate = lambda *a, **k: (
            {"acknowledged": True, "fingerprint": "fp", "issues": []}, None)
        app_main.delivery.register = lambda *a, **k: (
            registered.append(1) or {"id": "art1", "content_hash": "h"})
        body = {"volumes": [{"name": "技术文件"}], "confirmed": True, "token": "t"}
        try:
            resp = app_main.bundle_export(pid, body)
            chk.ok(getattr(resp, "status_code", 0) == 409,
                   "分册检查未通过即使带知情令牌也返回 409")
            chk.ok(not registered, "0 字节空包不再登记成正式产物")
            ack_bundle["blocking"] = []
            ack_bundle["data"] = b"PK\x03\x04FAKEZIP"
            resp2 = app_main.bundle_export(pid, body)
            chk.ok(getattr(resp2, "body", b"") == b"PK\x03\x04FAKEZIP",
                   "检查通过的分册包正常下载")
            chk.ok(len(registered) == 1, "正常包只登记一次")
        finally:
            (bidbundle.build, app_main.delivery.freeze,
             app_main._check_export_gate, app_main.delivery.register) = real

        store.set_project_member(pid, "u_lead", "lead")
        with as_user("stranger"):
            for label, call in (
                    ("单项能力", lambda: app_main.core_run("check_full", {"pid": pid})),
                    ("自动阶段", lambda: app_main.auto_stage(
                        "check", {"pid": pid, "force": True}))):
                try:
                    call()
                    chk.ok(False, f"非项目成员调用基座{label}应被拒")
                except HTTPException as exc:
                    chk.ok(exc.status_code == 403, f"基座{label}对非成员返回 403",
                           str(exc.status_code))
        with as_user("u_lead"):
            chk.ok(access.can(pid, "read")[0], "项目负责人照常可用基座能力")

        # 抽取失败不能伪装成「已同步」，否则零要求会被矩阵判成可交付
        from ..app import reqmatrix, reqtable, tender
        real_run = reqtable.core_bridge.run

        def boom(*_a, **_k):
            raise RuntimeError("模拟基座不可用")

        reqtable.core_bridge.run = boom
        try:
            r = reqtable.sync(pid)
            chk.ok(r.get("ok") is False and bool(r.get("errors")),
                   "基座抽取抛异常时 sync 明确报失败")
            chk.ok(store.get_req_sync_hash(pid) != reqtable.tender_hash(pid),
                   "抽取失败不写同步标记，下次读路径会自动重试")
        finally:
            reqtable.core_bridge.run = real_run

        empty_pid = make_project("零要求校验", [])
        try:
            store.update_project(empty_pid, tender_text="投标人须提供营业执照，不得弄虚作假。")
            store.set_req_sync_hash(empty_pid, reqtable.tender_hash(empty_pid))
            rep = reqmatrix.build(empty_pid)
            chk.ok(rep.get("deliverable") is False and rep.get("empty_requirements"),
                   "有招标正文但零要求时不判可交付")
        finally:
            drop_project(empty_pid)

        from ..app.basecore.services import requirements as R
        built = R.build(
            {"must": ["投标人须提供营业执照", "不得转包本项目"],
             "qualification": ["营业执照", "类似项目业绩"],
             "scoring": [{"item": "技术方案", "score": "40", "rule": "按完整性评分"}]},
            "投标人不得提供虚假材料，须提供原件核查。\n"
            "1.1 | 资格条件 | 投标人须具有独立法人资格，不得挂靠。")
        texts = [str(x.get("text")) for x in built.get("items", [])]
        chk.ok(any("营业执照" in t for t in texts) and any("评分项" in t for t in texts),
               "解读的 must / qualification / scoring 都进了需求条目")
        chk.ok(any("独立法人资格" in t for t in texts),
               "前附表拍平成表格行后的实质性要求不再被整行丢弃")

        amend_pid = make_project("补遗不丢条款", [])
        try:
            base = ("投标人须提供有效的营业执照副本，不得提供虚假材料。\n"
                    "投标人须具有履行合同所必需的设备和专业技术能力，不得转包本项目。")
            store.update_project(amend_pid, tender_text=base)
            reqtable.sync(amend_pid)
            before = [x["text"] for x in store.list_requirements(amend_pid)
                      if x.get("status") == "active"]
            chk.ok(bool(before), "原版抽出了硬性要求", str(len(before)))
            tender.ingest(amend_pid, "amend",
                          "投标截止日期顺延至 2026 年 12 月 20 日，投标人不得逾期递交。",
                          filename="补遗1.pdf")
            after = {x["text"] for x in store.list_requirements(amend_pid)
                     if x.get("status") == "active"}
            chk.ok(all(t in after for t in before),
                   "补遗后未修改的旧条款仍是有效要求",
                   f"{sum(t in after for t in before)}/{len(before)}")
        finally:
            drop_project(amend_pid)
    finally:
        drop_project(pid)


def test_generation_upgrades(chk):
    """分型生成（防编造承诺）/ 评分办法章节树 / 按章检索区分度。"""
    print("\n[GEN2] 分型生成 / 评分办法章节树 / 检索区分度")
    from ..app import (agent, outline_plan, quality, section_types,
                       writing_context)

    # ---------- 1. 章节分型与无依据承诺识别 ----------
    kinds = {t: section_types.classify({"title": t}) for t in (
        "资格证明文件", "资格证明与承诺文件", "投标报价表",
        "投标人资格与商务响应", "商务偏差表", "技术方案与逐条响应",
        "售后服务与运维保障")}
    chk.ok(kinds["资格证明文件"] == "protected"
           and kinds["资格证明与承诺文件"] == "protected",
           "资格证明 / 承诺文件归入证据型保护章节，模型不碰")
    chk.ok(kinds["投标人资格与商务响应"] == "structured"
           and kinds["商务偏差表"] == "structured",
           "资格与偏差表归入清单型章节，不自由生成")
    chk.ok(kinds["技术方案与逐条响应"] == "narrative"
           and kinds["售后服务与运维保障"] == "narrative",
           "技术与服务方案仍允许模型起草")

    fake_claim = ("我方已取得资格预审结果通知书，具备相应资质证书，"
                  "现按招标文件要求提供营业执照副本与法定代表人授权书原件，"
                  "全部材料加盖公章后随投标文件提交，逐条响应评审要求。")
    claims = section_types.claim_violations(fake_claim)
    chk.ok(bool(claims), "能识别试点里出现的「我方已取得资格预审结果通知书」",
           str(claims[:1]))
    flags = quality.assess_section("", {"title": "技术方案", "text": fake_claim}, [])["flags"]
    chk.ok(flags.get("claims") is False, "无原件支撑的既成事实承诺进入章节自检阻断项")
    chk.ok("claims" in quality.BLOCK_KEYS and "承诺" in quality.BLOCK_LABELS["claims"],
           "导出门禁把这类承诺列为重点提醒")
    draft = section_types.structured_draft(
        "投标人资格与商务响应", "营业执照与资质、类似业绩",
        "【本章认领的招标要求】\n- 投标人须提供营业执照副本\n- 不得存在失信被执行人记录")
    chk.ok("待人工核验" in draft and not section_types.claim_violations(draft),
           "清单草稿标出需人工核验的原件，且不含既成事实承诺")
    chk.ok(not any(x in draft for x in ("待补充", "需人工补充", "TODO", "XXX")),
           "清单草稿不引入会触发占位符门禁的内部标记")
    chk.ok(quality.assess_section(
        "", {"title": "投标人资格与商务响应", "text": draft}, [])["ok"],
        "清单草稿自身能通过章节自检，不制造假不合格")

    # ---------- 2. 生成链路按型分路 ----------
    titles = ("投标人资格与商务响应", "技术方案与逐条响应", "资格证明文件")
    pid = make_project("分型生成验证", [{"title": t} for t in titles])
    try:
        store.update_project(
            pid, tender_text="投标人须提供营业执照副本，不得提供虚假材料，"
                             "并按招标文件要求逐条响应。",
            outline=[{"title": t, "points": ["逐条响应"]} for t in titles])
        secs = {s["title"]: s["id"] for s in store.list_sections(pid)}
        real_llm = agent._llm
        calls: list[tuple] = []
        long_text = ("按招标文件要求逐项响应，明确措施、责任与验收口径；"
                     "所有事实性表述以本项目资料与随投标文件提交的原件为准，"
                     "不改动前面定过的参数与承诺，未展示的条款不作推测，"
                     "涉及原件的事项以加盖公章的证明材料为准。")

        def fake_llm(*a, **k):
            calls.append((a[1] if len(a) > 1 else "", k.get("variables") or (a[2] if len(a) > 2 else {})))
            if k.get("generation"):
                v = a[2]
                k["generation"].capture(a[1],v,v,[],{"type":"test"})
                k["generation"].finish(long_text)
            return long_text

        agent._llm = fake_llm
        try:
            r = agent.generate_section(pid, secs["投标人资格与商务响应"])
            chk.ok(r.get("section_kind") == "structured" and not calls,
                   "资格清单章节不调用模型，直接产出确定性清单")
            chk.ok("待人工核验" in (r.get("text") or ""),
                   "生成的正文确实是要求与材料的对照清单")
            r2 = agent.generate_section(pid, secs["技术方案与逐条响应"])
            chk.ok(bool(calls) and r2.get("section_kind") == "narrative",
                   "方案章节照常交给模型起草")
            mats = str((calls[0][1] or {}).get("materials") or "")
            chk.ok("事实边界" in mats and "不得写" in mats,
                   "方案章节提示词注入了禁止编造资质 / 承诺的事实边界")
            rw = agent.rewrite_section(pid, secs["投标人资格与商务响应"], "优化措辞")
            chk.ok("error" in rw and "人工核验" in str(rw.get("error")),
                   "清单型章节拒绝模型改写，避免把待核验项润色成既成事实")
            pr = agent.generate_section(pid, secs["资格证明文件"])
            chk.ok("error" in pr, "资格证明类章节按证据型保护，不生成正文")
        finally:
            agent._llm = real_llm
    finally:
        drop_project(pid)

    # ---------- 3. 按评分办法与投标文件格式展开章节树 ----------
    tender_toc = (
        "第一章 投标邀请书\n本项目采购电缆附件，投标人须按要求递交文件。\n"
        "第三章 评标办法\n评分标准按综合评估法执行。\n"
        "第六章 投标文件格式\n目录（参考以下格式）\n"
        "序号 | 内容 | 上传电子商务平台维度 | 是否有格式要求 | 投标工具上传端口\n"
        "一 | 价格文件（（每包容量为30M）） |  |  | 端口\n"
        "（一） | 投标函及附件 | 按包 | √ | 端口\n"
        "（一） | 已标价货物清单行报价暨投标报价汇总表 | 按包 | √ | 端口\n"
        "二 | 商务文件（每包容量为10M） |  |  | 端口\n"
        "（一） | 法定代表人授权委托书 | 按分标 | √ | 端口\n"
        "（二） | 商务偏差表 | 按分标 | √ | 端口\n"
        "三 | 技术文件（每包容量为60M） |  |  | 端口\n"
        "（一） | 项目货物技术特性逐条逐项应答文件 | 按包 | √ | 端口\n"
        "四 | 资质业绩文件（每包容量为1M） |  |  | 端口\n"
        "1 | 财务信息审核结果凭证单（如有） | 按分标 | √ | 端口\n"
        "1.我方承诺在投标有效期内不修改、撤销投标文件。\n"
        "2.随同本投标函提交投标保证金一份（如有）。\n")
    fmt = outline_plan.format_titles(tender_toc)
    chk.ok(len(fmt) >= 4 and any("商务偏差表" in x["title"] for x in fmt),
           "能解析招标文件的表格版投标文件组成清单")
    chk.ok(all("我方" not in x["title"] and "承诺在" not in x["title"] for x in fmt),
           "不会把投标函正文里的「我方承诺…」误当成章节")
    chk.ok(any(x.get("volume") == "商务文件" for x in fmt),
           "组成项带上分册归属，容量括注已清洗")
    meta = {"scoring": [
        {"item": "技术参数响应", "score": "40", "rule": "逐项响应技术规范书"},
        {"item": "供货与交付方案", "score": "20", "rule": "交货期与包装"},
        {"item": "投标文件格式", "score": "5", "rule": "签署盖章齐全"}]}
    base = [{"title": t, "points": ["要点"]} for t in (
        "投标函及投标报价总说明", "技术方案与逐条响应", "资格证明与承诺文件")]
    chapters, plan = outline_plan.expand(base, tender_toc, meta)
    chk.ok(len(chapters) > len(base) and plan.get("used_scoring") == 3
           and plan.get("used_format", 0) >= 4,
           "骨架按评分项与招标格式目录展开，不再只有 6—8 章")
    by_title = {c["title"]: c for c in chapters}
    hi = next((c for c in chapters if c["title"].endswith("技术参数响应")), {})
    lo = next((c for c in chapters if c["title"].endswith("投标文件格式")), {})
    chk.ok(hi.get("words", 0) > lo.get("words", 0),
           "分值高的评分项拿到更大篇幅预算")
    chk.ok(all("kind" in c and "words" in c for c in chapters),
           "每章都带分型与篇幅预算，生成时可据此约束")
    chk.ok(by_title["资格证明与承诺文件"]["words"] == 0,
           "证据型保护章节不分配 AI 篇幅预算")
    plain, plain_plan = outline_plan.expand(base, "", {})
    chk.ok(len(plain) == len(base) and plain_plan["used_scoring"] == 0,
           "没有招标文件与评分项时安全回退到模型骨架")

    # 目录去重与占位标题：同名不同措辞的重复章（投标函 / 售后 / 资格）只留一章，
    # 招标文件模板里的「XX年…」占位前缀不再原样进目录。
    dup_base = [{"title": t, "points": []} for t in (
        "投标函及投标报价总说明", "投标人资格与商务响应", "技术方案与逐条响应",
        "售后服务与运维保障", "资格证明与承诺文件")]
    dup_fmt = ("第三章 投标文件格式\n"
               "一 | 商务文件\n"
               "1 | 投标函及附件\n"
               "2 | XX年财务信息审核结果凭证单\n"
               "3 | 售后服务承诺\n")
    dup_ch, dup_plan = outline_plan.expand(dup_base, dup_fmt, {})
    titles = [c["title"] for c in dup_ch]
    chk.ok(sum(1 for t in titles if outline_plan._lead_topic(t) == "投标函") == 1,
           "投标函重复章只保留一章", str(titles))
    chk.ok(sum(1 for t in titles if outline_plan._lead_topic(t) == "售后服务") == 1,
           "售后服务重复章只保留一章", str(titles))
    chk.ok(sum(1 for t in titles if outline_plan._lead_topic(t) == "资格") == 1,
           "资格证明重复章归并为一章", str(titles))
    chk.ok(not any("XX" in t or "xx" in t for t in titles)
           and any("财务信息审核结果凭证单" in t for t in titles),
           "占位前缀已从标题剥离，保留可读主体", str(titles))
    chk.ok(any("投标函" in x for x in dup_plan.get("merged_topics", [])),
           "合并掉的重复标题写进计划留痕", str(dup_plan.get("merged_topics")))
    chk.ok(any(section_types.classify({"title": t}) == section_types.PROTECTED
               for t in titles), "去重仍保留证据型保护章")

    outline_pid = make_project("章节树验证", [])
    try:
        store.update_project(outline_pid, tender_text=tender_toc,
                             summary=json.dumps(meta, ensure_ascii=False))
        real_llm = agent._llm
        agent._llm = lambda *a, **k: {"chapters": base}
        try:
            got = agent.make_outline(outline_pid)
        finally:
            agent._llm = real_llm
        saved = store.get_project(outline_pid).get("outline") or []
        chk.ok(len(got) > len(base) and len(saved) == len(got)
               and agent.LAST_OUTLINE.get(agent._cache_key(outline_pid), {}).get("chapters", 0) > len(base),
               "make_outline 已接上章节树，目录与展开计划一并落库")
        chk.ok(any(c.get("words") for c in saved),
               "落库目录保留每章目标字数，供生成时注入篇幅预算")
    finally:
        drop_project(outline_pid)

    # ---------- 4. 按章检索区分度 ----------
    common = ("评标办法按招标文件执行，否决投标情形详见前附表，"
              "投标人须知前附表另有规定，投标人应逐条响应。\n")
    themes = [
        ("售后服务", "故障响应 到场时限 培训场次 质保期 服务网点"),
        ("评标办法", "技术评分 商务评分 分值权重 评审因素 打分细则"),
        ("技术规范", "额定电压 电缆附件 试验报告 供货范围 技术参数"),
        ("否决条款", "串通投标 弄虚作假 失信被执行人 否决情形 废标"),
    ]
    chunks = []
    for page, (theme, words) in enumerate(themes, 1):
        chunk = f"【第 {page} 页】\n第{page}章 {theme}专项\n" + common * 26
        chunk += "".join(f"{w}专项条款：本条仅适用于{theme}，编号 Z{page}-{j}。\n"
                         for j, w in enumerate(words.split()) for _ in range(14))
        chunks.append(chunk)
    long_tender = "\n".join(chunks)
    chk.ok(len(long_tender) > 9000, "构造的长招标文件足以触发按章检索")
    q1 = "售后服务 运维保障 故障响应 到场时限 培训场次 质保期"
    q2 = "否决条款 串通投标 弄虚作假 失信被执行人 否决情形"
    s1, m1 = writing_context.retrieve_for_chapter(long_tender, q1, budget=9000)
    s2, m2 = writing_context.retrieve_for_chapter(long_tender, q2, budget=9000)
    g1, g2 = writing_context.grams(s1), writing_context.grams(s2)
    overlap = len(g1 & g2) / max(1, len(g1 | g2))
    chk.ok(overlap < 0.8, "不同主题章节拿到的招标原文不再几乎完全相同",
           f"Jaccard={overlap:.3f}")
    chk.ok(m1.get("stop_grams", 0) > 0 and "chapters" in m1 and "top_score" in m1,
           "检索如实上报高频词降权、覆盖章节数与最高相关度")
    chk.ok("故障响应" in s1 and "失信被执行人" in s2,
           "各自命中本章专属条款，而非统一返回开头公告")
    s0, m0 = writing_context.retrieve_for_chapter(
        long_tender, "ΩΞΨ 完全不相干的检索词", budget=9000)
    chk.ok(m0.get("query_hit") is False and "跨章均匀抽样" in (m0.get("note") or "")
           and m0.get("selected", 0) >= 2,
           "零命中时跨章抽样并如实标注，不再退化成取开头若干段")


def test_close_loop(chk: Checker) -> None:
    """第 4 步闭环：评分项质量 / 章节树合并 / 自动体检 / 多企业防雷同。"""
    print("\n[CLOSE] 评分项闸门 / 自动体检 / 多企业合规红线")
    import json as _json
    from ..app import (auth, auto_core, compliance, core_bridge, health,
                       outline_plan, reqtable)

    # ---------- 1. 评分项抽取：真实国文件的「条款号冒充分值」不再进表 ----------
    tender = (
        "第一章 投标人须知前附表\n"
        "2.4 | 招标文件的异议 | 补充：异议应按附表格式填写\n"
        "7.3 | 中标候选人履约能力审查 | 修改为：由评标委员会审查\n"
        "第三章 评标办法\n"
        "项目 | 评审要素 | 评审细则\n"
        "一、资源实力（17-45） | 生产试验设备（16-29） | 硫化机≥5套得【4】分\n"
        "一、资源实力（17-45） | 售后服务（1-5） | 20个网点以上得【5分】\n"
        "二、诚信评价（-115-0） | 不良行为处理（-30-0） | 不扣分\n"
    )
    items = reqtable.extract_scoring_items(tender)
    names = [x["item"] for x in items]
    by_item = {x["item"]: x["score"] for x in items}
    chk.ok("生产试验设备" in names and "售后服务" in names,
           "能从「项目 | 评审要素（区间） | 细则」评分表抽出真实要素")
    chk.ok(not any("异议" in n or "履约能力" in n for n in names)
           and not any(n.startswith("不良") for n in names),
           "前附表条款号与扣分区间不再冒充评分项")
    chk.ok(by_item.get("生产试验设备") == "29分",
           "区间分值取上限，供章节树按分值排序", str(by_item))
    chk.ok(not reqtable.valid_scoring("招标文件的异议", "2.4分")
           and reqtable.valid_scoring("售后服务", "5分"),
           "质量闸门拦住条款号、放行真实评分要素")

    sync_pid = make_project("评分闸门", [{"title": "技术方案"}])
    try:
        store.update_project(sync_pid, tender_text=tender)
        reqtable.sync(sync_pid)
        rows = reqtable.scoring_rows(sync_pid)
        chk.ok(bool(rows) and not any("异议" in str(r["item"]) for r in rows),
               "要求表里只剩真实评分项，条款号没有混进来", str(rows[:3]))
    finally:
        drop_project(sync_pid)

    # ---------- 2. 要求表评分项并入解读 meta ----------
    meta = {"scoring": [
        {"item": "技术方案", "score": "40", "rule": "按完整性评分"},
        {"item": "售后服务", "score": "2"}]}
    extra = [{"item": "生产试验设备", "score": "29分"},
             {"item": "售后服务", "score": "5分"},
             {"item": "应急保供", "score": "20分"}]
    merged = outline_plan.merge_scoring(meta, extra)
    m_by = {x["item"]: x for x in merged}
    chk.ok(len(merged) == 4 and m_by["技术方案"]["score"] == 40,
           "解读与要求表的评分项合并去重，高分项排在最前")
    chk.ok(m_by["售后服务"]["score"] == 5
           and m_by["售后服务"]["source"] == "requirements",
           "同名评分项以人工可修订的要求表分值为准")
    base = [{"title": "技术方案与逐条响应", "points": []}]
    chapters, plan = outline_plan.expand(base, tender, meta, extra_scoring=extra)
    chk.ok(plan.get("scoring_from_requirements", 0) >= 2
           and any("评分响应·" in c["title"] for c in chapters),
           "目录按合并后的评分项展开得分点章节")
    _, plain_plan = outline_plan.expand(base, tender, meta)
    chk.ok(plain_plan.get("scoring_from_requirements") == 0,
           "不传要求表评分项时保持原有目录行为")

    # ---------- 3. 自动体检：查过 / 没查成 / 停用三态 ----------
    pid = make_project("闭环体检", [{"title": "技术方案"}, {"title": "售后服务"}])
    try:
        secs = store.list_sections(pid)
        same = "按招标文件要求逐条响应，明确措施、责任与验收口径。" * 12
        for s in secs:
            store.update_section(s["id"], same)
        store.update_project(pid, tender_text=tender)
        h = health.assess(pid)
        chk.ok(len(h["checks"]) >= 7 and h["complete"],
               "生成后自动跑质量 / 矩阵 / 雷同 / 评标 / 事实 / 引用 / 合规")
        chk.ok("dupcheck" in h["checks"] and "compliance" in h["checks"],
               "雷同检查与多企业合规边界都进了自动体检")
        chk.ok(any("重复" in str(x.get("title") or "") for x in h["findings"]),
               "两章正文雷同能给出可定位的提示")
        health.save(pid, h)
        empty_pid = make_project("空章体检", [{"title": "技术方案"}])
        try:
            he = health.assess(empty_pid)
            chk.ok(not he["deliverable_ready"] and "尚不可交付" in he["summary"],
                   "空章不会被体检报告说成「可交付」，体检通过 ≠ 可交出去")
        finally:
            drop_project(empty_pid)
        real_assess, calls = health.assess, []

        def counting(p):
            calls.append(p)
            return real_assess(p)

        health.assess = counting
        try:
            health.latest(pid)
            store.update_section(secs[0]["id"], same + "新增一段不同的收尾说明。")
            health.latest(pid)
        finally:
            health.assess = real_assess
        chk.ok(len(calls) == 1,
               "体检结论按内容指纹缓存；正文变了才重算，不每次导出都重跑")
        gate = auto_core.export_gate(pid)
        chk.ok(any(str(x.get("key") or "").startswith("health-")
                   for x in gate["issues"]),
               "导出门禁复用自动体检结论，不另算一套")

        real_run = core_bridge.run

        def failing(key, p, payload=None):
            if key == "dupcheck":
                raise RuntimeError("模拟能力故障")
            return real_run(key, p, payload)

        core_bridge.run = failing
        try:
            bad = health.assess(pid)
        finally:
            core_bridge.run = real_run
        chk.ok(not bad["ok"] and "dupcheck" in bad["unavailable"]
               and any(x["blocking"] for x in bad["findings"]),
               "体检没跑完不算通过，也不写成「没有质量问题」")
    finally:
        drop_project(pid)

    # ---------- 4. 多企业合规红线：只比指纹，绝不读别家正文 ----------
    bid_text = tender + (
        "本标段采购 10kV 及以下电缆附件框架协议，投标人须逐条响应技术规范书。" * 12)
    fp1 = compliance.tender_fingerprint(bid_text)
    chk.ok(bool(fp1) and fp1 == compliance.tender_fingerprint(
        bid_text.replace("第一章", "第 一 章")),
        "招标指纹抗空白差异，不存原文")
    chk.ok(compliance.tender_fingerprint("太短") == "",
           "短文本不生成指纹，避免空项目互相误报")

    def in_org(org: str, fn):
        tok = auth.set_current({"id": "u", "username": "u", "role": "owner",
                                "org_id": org})
        try:
            with store.use_org(org):
                return fn(org)
        finally:
            auth.reset_current(tok)

    def first_org(_o):
        p = store.create_project("A 企业项目", bid_text)["id"]
        r = compliance.audit(p)
        chk.ok(r["risk"] == "none" and r["checked"],
               "只有一家企业使用这份招标文件时不误报")
        return p

    def second_org(_o):
        p = store.create_project("B 企业项目", bid_text)["id"]
        r = compliance.audit(p)
        chk.ok(r["risk"] == "high" and r["other_orgs"] >= 1,
               "第二家企业用同一份招标文件时命中合规红线", str(r["risk"]))
        issues = compliance.export_issues(p)
        chk.ok(any(x["level"] == "高" and x["blocking"] for x in issues),
               "合规红线列为导出重点提醒，知情导出也留痕")
        st = compliance.status(p)
        chk.ok("org_close_a" not in _json.dumps(st, ensure_ascii=False)
               and "A 企业项目" not in _json.dumps(st, ensure_ascii=False),
               "结论不泄露对方企业标识、项目名或正文")
        other = store.create_project(
            "B 企业另一标段", bid_text + "本标段编号 XYZ-2026 与上次完全不同。" * 30)["id"]
        chk.ok(compliance.audit(other)["risk"] != "high",
               "不同招标文件不误报同标段红线")
        return p

    in_org("org_close_a", first_org)
    in_org("org_close_b", second_org)


def test_readiness(chk: Checker) -> None:
    """一张「能不能交」卡：三态要诚实，没查完 / 没体检 / 过期都不能判绿。"""
    print("\n[READY] 一句话交付结论")
    from ..app import auth, readiness, store

    good_q = {"status": "pass", "complete": True, "all_passed": True,
              "undeliverable": []}
    good_h = {"deliverable_ready": True, "complete": True, "ok": True,
              "gate_issues": []}

    r = readiness.decide(good_q, good_h, [], health_state="fresh", chapters=8)
    chk.ok(r["level"] == "green" and r["action"]["act"] == "exportWord",
           "全查过且都达标 → 可以导出")
    chk.ok("知情确认" in r["hint"], "绿卡也写明知情确认不等于检查通过")

    q_bad = {**good_q, "undeliverable": [{"title": "技术响应"}]}
    r = readiness.decide(q_bad, good_h, [], health_state="fresh", chapters=8)
    chk.ok(r["level"] == "red" and r["action"]["tab"] == "write",
           "有未达标章节 → 还不能交并指向正文页")
    chk.ok("1 章未达交付标准" in r["reason"], "红卡说清具体原因")

    r = readiness.decide({"status": "unavailable", "complete": False},
                         good_h, [], health_state="fresh")
    chk.ok(r["level"] == "red" and "没跑完" in r["reason"],
           "检查没跑完不能当作没问题")

    r = readiness.decide(good_q, {}, [], health_state="missing", chapters=8)
    chk.ok(r["level"] == "yellow" and r["action"]["act"] == "check",
           "没做整本体检 → 先跑体检，不误判为可以交")

    r = readiness.decide(good_q, good_h, [], health_state="stale", chapters=8)
    chk.ok(r["level"] != "green" and "过期" in r["reason"],
           "正文改过后的旧体检结论不能充数")

    r = readiness.decide(good_q, {**good_h, "deliverable_ready": False},
                         [], health_state="fresh")
    chk.ok(r["level"] == "red", "体检没判定可交付就不能交")

    gate = [{"level": "高", "blocking": True, "title": "同一招标多家企业",
             "detail": "疑似串标红线"}]
    r = readiness.decide(good_q, good_h, gate, health_state="fresh")
    chk.ok(r["level"] == "yellow" and len(r["findings"]) == 1,
           "门禁重点提醒按现行策略为「需确认」，不是硬阻断")

    r = readiness.decide(good_q, good_h,
                         [{"level": "高", "blocking": True,
                           "title": "门禁没查完", "detail": "异常"}],
                         health_state="fresh")
    chk.ok(r["level"] == "yellow", "门禁异常也不会被写成通过")

    tok = auth.set_current({"id": "ready-test", "username": "ready-test",
                            "role": "owner", "org_id": "default"})
    try:
        with store.use_org("default"):
            pid = store.create_project(
                "readiness 冒烟", "第一章 招标公告\n投标人须提供营业执照并逐条响应。")["id"]
            try:
                real = readiness.assess(pid)
                chk.ok(real["level"] in ("red", "yellow", "green")
                       and real["action"] and real["hint"]
                       and real["health_state"] in ("fresh", "stale", "missing"),
                       "真实项目上只读汇总返回完整结论")
                chk.ok(real["checks"]["health"] in ("fresh", "stale", "missing"),
                       "体检状态三态如实上报，不偷偷重跑")
            finally:
                store.delete_project(pid)
    finally:
        auth.reset_current(tok)


def test_engineering_state(chk: Checker) -> None:
    """工程 P0：令牌跨进程共享、台账并发写不丢、目录计划重启可恢复。"""
    print("\n[STATE] 跨进程状态 / 台账并发 / 目录计划留痕")
    import threading as _threading
    import time as _time
    from ..app import agent, auth, auto_core, compliance, store

    tok = auth.set_current({"id": "state-test", "username": "state-test",
                            "role": "owner", "org_id": "default"})
    try:
        with store.use_org("default"):
            pid = store.create_project(
                "state 冒烟", "第一章 招标公告\n"
                + "投标人须逐条响应技术规范书并提供营业执照与检测报告。" * 10)["id"]
            try:
                t = "state-token-1"
                auto_core._EXPORT_TOKENS[t] = (pid, "fp-1", _time.time() + 60)
                chk.ok(t in store.token_keys("export"), "导出确认令牌落入企业库而非进程内存")
                chk.ok(auto_core._EXPORT_TOKENS[t][0] == pid, "仍可按原 dict 方式读取令牌")
                chk.ok(auto_core.verify_export_token(pid, "fp-1", t) is True,
                       "令牌验证通过")
                chk.ok(auto_core.verify_export_token(pid, "fp-1", t) is False,
                       "令牌用完即焚")

                t2 = "state-token-2"
                auto_core._ACK_TOKENS[t2] = (pid, "fp-2", _time.time() + 60,
                                             {"mode": "acknowledged"})
                got = []

                def consume():
                    if store.token_pop("ack", t2) is not None:
                        got.append(1)

                threads = [_threading.Thread(target=consume) for _ in range(8)]
                for x in threads:
                    x.start()
                for x in threads:
                    x.join()
                chk.ok(len(got) == 1, "并发消费同一个令牌只成功一次（原子）")

                t3 = "state-token-3"
                auto_core._ACK_TOKENS[t3] = (pid, "fp-3", _time.time() + 60,
                                             {"high": 2})
                ack = auto_core.consume_ack_token(pid, "fp-3", t3)
                chk.ok(ack and ack["high"] == 2, "知情令牌的风险摘要不丢")
                chk.ok(auto_core.consume_ack_token(pid, "fp-3", t3) is None,
                       "知情令牌同样只用一次")

                fp = compliance.tender_fingerprint(
                    (store.get_project(pid) or {}).get("tender_text") or "")
                chk.ok(bool(fp), "构造的招标正文能生成不可逆指纹")

                def reg(i):
                    compliance.register(fp, f"org{i}", f"pid{i}")

                reg_threads = [_threading.Thread(target=reg, args=(i,))
                               for i in range(8)]
                for x in reg_threads:
                    x.start()
                for x in reg_threads:
                    x.join()
                orgs = {e.get("org") for e in compliance._load()["entries"]
                        if isinstance(e, dict) and e.get("fp") == fp}
                chk.ok(all(f"org{i}" in orgs for i in range(8)),
                       "并发登记 8 家企业一条不丢（跨进程锁 + 原子替换）")

                plan = {"chapters": 7, "scoring_items": 3}
                store.save_auto_run(pid, agent.OUTLINE_STAGE, "目录展开计划", plan)
                agent.LAST_OUTLINE.pop(agent._cache_key(pid), None)
                chk.ok(agent.outline_plan_for(pid).get("chapters") == 7,
                       "进程内缓存清空后仍能从留痕恢复目录计划")
            finally:
                store.delete_project(pid)
    finally:
        auth.reset_current(tok)


def test_ack_draft_artifact_kind(chk: Checker) -> None:
    """P0-1：知情导出的产物不能登记成正式交付件；下载行为一点不变。

    导出契约是「提醒 + 一次性知情令牌仍可下载」，这里只改登记口径：
    用户看过提醒仍下载的，单独标成知情导出草稿，不冒充正式交付件。
    """
    from ..app import delivery
    from ..app import main as app_main

    pid = make_project("自测-知情草稿登记", [{"title": "第一章 技术方案"}])
    try:
        snap = delivery.freeze(pid)
        clean_gate = {"fingerprint": snap.get("fingerprint") or "",
                      "blocked": False, "issues": []}
        body = ("# 测试\n\n本方案按招标文件要求逐条响应，措施可核查。\n").encode("utf-8")
        r_clean = app_main._deliver(pid, snap, clean_gate, body, "干净.md", "md",
                                    "text/markdown")
        chk.ok(getattr(r_clean, "status_code", 200) == 200, "无提醒导出照常下载")
        art_clean = store.get_artifact_by_id(r_clean.headers.get("x-artifact-id") or "")
        chk.ok(art_clean.get("kind") == "deliverable",
               "无知情令牌 + 门禁无硬性否决 + 文件检查通过才登记正式交付件")
        chk.ok(r_clean.headers.get("x-artifact-kind") == "deliverable",
               "响应头如实给出产物类型")

        ack_gate = {**clean_gate, "acknowledged": {"issues": 3, "reason": "已知悉"}}
        r_ack = app_main._deliver(pid, snap, ack_gate, body, "知情.md", "md",
                                  "text/markdown")
        chk.ok(getattr(r_ack, "status_code", 200) == 200,
               "知情导出下载行为不变，不因降级登记而拒绝")
        chk.ok(b"# " in r_ack.body, "知情导出正文照原样发放，不偷改内容")
        art_ack = store.get_artifact_by_id(r_ack.headers.get("x-artifact-id") or "")
        chk.ok(art_ack.get("kind") == delivery.ACK_DRAFT_KIND,
               "知情导出单独登记为知情导出草稿")
        chk.ok("不可直接提交" in str(art_ack.get("purpose") or ""),
               "产物用途明确写着不可直接提交")
        chk.ok(r_ack.headers.get("x-artifact-purpose") == "acknowledged-draft",
               "响应头不把知情草稿说成正式交付")
        chk.ok(str(art_ack.get("name") or "").startswith("知情草稿-"),
               "文件名前缀也能一眼分辨不是正式交付件")
        dels = [a for a in store.list_artifacts(pid) if a.get("kind") == "deliverable"]
        chk.ok(len(dels) == 1 and dels[0]["id"] == art_clean["id"],
               "知情草稿不挤进正式交付件列表，交付件仍是原来那一份")
    finally:
        drop_project(pid)


def test_file_check_draft_markers(chk: Checker) -> None:
    """P0-2：系统自己生成的空壳不再拿到 X-File-Check: pass。

    保护章节的「（未填写）」、structured 的「待人工核验」清单骨架，过去不在
    占位符扫描表里；知情导出也不许把「用户看过提醒」写成「检查已通过」。
    """
    from ..app import delivery
    from ..app import main as app_main

    pid = make_project("自测-文件检查语义", [{"title": "第一章 技术方案"}])
    try:
        snap = delivery.freeze(pid)
        clean = ("# 测试\n\n本方案按招标文件要求逐条响应，措施可核查。\n").encode("utf-8")
        shell = ("# 测试\n\n（未填写）\n\n"
                 "1. 招标要求：按招标文件格式提交原件。\n"
                 "   响应材料：待人工核验原件后填写材料名称与页码。\n").encode("utf-8")
        c = delivery.inspect_document(clean, "md", snap)
        chk.ok(bool(c.get("ok")) and not c.get("draft_markers"),
               "干净文件的检查结论不受新规则影响")
        s = delivery.inspect_document(shell, "md", snap)
        chk.ok(bool(s.get("ok")) and s.get("deliverable_ready") is False,
               "文件能打开，但空章 / 待核验骨架不再被判为可交付")
        chk.ok(len(s.get("draft_markers") or []) >= 2, "如实列出命中的空壳标记")
        chk.ok(any("未填写" in str(x.get("detail") or "") for x in s.get("issues") or []),
               "检查清单点名保护章节空正文")
        chk.ok(any(x.get("code") == "draft_marker" for x in s.get("issues") or []),
               "空壳单列为内容提醒，不冒充文件结构硬伤")

        gate = {"fingerprint": snap.get("fingerprint") or "", "blocked": True,
                "issues": [{"key": "e0", "level": "高"}]}
        r0 = app_main._deliver(pid, snap, gate, shell, "空壳.md", "md", "text/markdown")
        chk.ok(getattr(r0, "status_code", 200) == 200,
               "空壳文件下载行为不变，不因如实标注变成禁止下载")
        chk.ok(r0.headers.get("x-file-check") == "draft",
               "头部如实标 draft，不再写 pass")
        chk.ok(r0.headers.get("x-artifact-kind") == "review",
               "空壳文件不登记为正式交付件")
        r1 = app_main._deliver(pid, snap, {**gate, "acknowledged": {"issues": 1}},
                               shell, "空壳-知情.md", "md", "text/markdown")
        chk.ok(getattr(r1, "status_code", 200) == 200,
               "知情令牌仍照常下载，导出契约没有被改成禁止下载")
        chk.ok(r1.headers.get("x-file-check") == "acknowledged",
               "空壳文件不再拿 pass")
        chk.ok(r1.headers.get("x-gate-blocked") == "1",
               "门禁是否存在硬性否决单独报出")

        # 即便最终文件检查本身通过，知情导出也不能写成「已通过检查」
        orig = delivery.inspect_document
        delivery.inspect_document = lambda *a, **k: {
            "ok": True, "checked": True, "issues": [], "blocking": [],
            "draft_markers": [], "summary": "最终文件检查通过"}
        try:
            r2 = app_main._deliver(
                pid, snap, {"fingerprint": gate["fingerprint"], "blocked": False,
                            "issues": [], "acknowledged": {"issues": 0}},
                clean, "干净-知情.md", "md", "text/markdown")
            chk.ok(getattr(r2, "status_code", 200) == 200, "干净文件的知情导出照样下载")
            chk.ok(r2.headers.get("x-file-check") == "acknowledged",
                   "知情导出不冒充「已通过检查」")
            chk.ok(r2.headers.get("x-artifact-purpose") == "acknowledged-draft",
                   "检查通过也仍登记为知情草稿")
        finally:
            delivery.inspect_document = orig
    finally:
        drop_project(pid)


def test_consistent_counts_and_wording(chk: Checker) -> None:
    """P0-3：逐条响应 0/0 口径修正、四处数字同源、文案与导出契约一致。"""
    from pathlib import Path

    from ..app import auto_core, health

    pid = make_project("自测-计数口径", [{"title": "第一章 技术方案"}])
    try:
        store.add_requirements(pid, [{
            "kind": "hard", "text": "投标人须提供营业执照复印件并加盖公章",
            "required": 1, "source": "manual", "status": "active"}])
        h = health.assess(pid)
        cov = (h.get("checks") or {}).get("coverage") or {}
        chk.ok(cov.get("status") == "fail",
               "矩阵 0 条而必需要求大于 0 时，逐条响应判失败而不是 checked")
        chk.ok("未逐条落实" in str(cov.get("detail") or ""),
               "结论里如实给出未落实条数")
        chk.ok(any(x.get("key") == "health_matrix_stale" and x.get("blocking")
                   for x in h.get("findings") or []),
               "必需要求未逐条落实进入重点提醒")
        hm = h.get("metrics") or {}
        chk.ok(int(hm.get("matrix_uncovered") or 0) >= 1,
               "不再把 0 条矩阵算成「待响应 0 条」")

        g = auto_core.export_gate(pid)
        cur = (health.latest(pid) or {}).get("metrics") or {}
        chk.ok(g.get("matrix_required") == int(cur.get("requirements") or 0)
               and g.get("matrix_rows") == int(cur.get("matrix_rows") or 0),
               "门禁与同一时点的体检报告引用同一套条数")
        chk.ok(g.get("sections_undeliverable") == int(cur.get("undeliverable_sections") or 0),
               "未达标章数与体检报告同源")
        chk.ok(g.get("issues_total") == len(g.get("issues") or [])
               and g.get("blockers_total") == len(g.get("blockers") or []),
               "提醒条目数单独命名，不与章数 / 条数混用")

        root = Path(__file__).resolve().parents[2]
        for rel, bad in (("backend/app/agent.py", "复检通过前不允许导出"),
                         ("backend/app/chat.py", "导出会被拦下")):
            src = (root / rel).read_text(encoding="utf-8")
            chk.ok(bad not in src, f"{rel} 不再出现与「仍可下载」相反的措辞")
    finally:
        drop_project(pid)


def test_structured_tables_in_docx(chk: Checker) -> None:
    """P1-5：structured 章节的对照表要还原成真正的 Word 表格。

    真实试点里招标文件的投标文件格式明确要求偏差表 / 逐条应答表 /
    单价分析表，导出的 Word 却一个表格都没有，全被渲染成文字清单。
    """
    import io as _io
    from docx import Document as _Document

    from ..app import docx_export, section_types

    reqs = ("【本章认领的招标要求】\n"
            "- 投标人须提供营业执照副本并加盖公章\n"
            "- 技术参数 | 须逐条响应招标文件要求\n"
            "- 投标有效期不少于 90 天")
    draft = section_types.structured_draft("商务偏差表", "原件核验、偏离说明", reqs)
    chk.ok("| --- |" in draft, "structured 草稿输出 Markdown 表格分隔行")
    chk.ok("｜" in draft, "要求原文里的竖线被转义，不会切断表格列")
    chk.ok("待人工核验" in draft, "表格仍如实标出待人工核验，不编造材料名称")
    # 抄录的招标原文数字仍会被旧的 numbers 规则误判（P1-6 修），
    # 这里先确认表格化本身没有引入占位符 / 既成事实承诺 / 空章问题。
    sec_flags = quality.assess_section(
        "", {"title": "商务偏差表", "text": draft}, [])["flags"]
    chk.ok(sec_flags.get("placeholders") and sec_flags.get("claims")
           and sec_flags.get("empty"),
           "表格化没有引入占位符 / 既成事实承诺 / 空章问题")

    data = docx_export.build_docx(
        "表格还原测试", [{"title": "商务偏差表", "text": draft}],
        {"required_sections": ["商务偏差表"], "header": "", "date": "2026-11-14"})
    doc = _Document(_io.BytesIO(data))
    chk.ok(len(doc.tables) >= 1, "导出的 Word 还原出真实表格，不再是 0 个")
    if doc.tables:
        t = doc.tables[0]
        chk.ok(len(t.columns) == 5, "对照表五列结构完整")
        head = " ".join(c.text for c in t.rows[0].cells)
        chk.ok("招标要求" in head and "核验状态" in head,
               "表头是「要求 → 材料 → 核验」的对照关系")
        body = " ".join(c.text for r in t.rows[1:] for c in r.cells)
        chk.ok("营业执照" in body, "招标要求原文进了表格")
        chk.ok("待人工核验" in body, "待核验状态随表格一起保留")
    # 报价 / 单价类章节列头另给金额栏，但金额一律留空由人填写
    quote = section_types.structured_draft("分项报价表", "", reqs)
    chk.ok("金额（人工填写）" in quote and "不得由系统推算" in quote,
           "报价类表格给金额栏，同时声明系统不推算金额")


def test_owner_obligation_and_quoted(chk: Checker) -> None:
    """P1-6：招标方义务不进必需要求；抄录原文不被当成我方表述。"""
    from ..app import (blindcheck, core_bridge, docx_export, health, reqtable,
                       rules, section_types)

    chk.ok(reqtable.is_owner_obligation("招标人不得向他人泄露投标人的商业秘密"),
           "招标人自身义务被识别")
    chk.ok(reqtable.is_owner_obligation("评标委员会应当按照评标标准和方法评标"),
           "评标委员会自身义务被识别")
    chk.ok(not reqtable.is_owner_obligation("投标人须提供营业执照副本并加盖公章"),
           "投标人义务不会被误剔除")

    pid = make_project("自测-主体与引文", [{"title": "商务偏差表"}])
    try:
        store.add_requirements(pid, [
            {"kind": "hard", "text": "投标人须提供营业执照副本并加盖公章",
             "required": 1, "source": "manual"},
            {"kind": "hard", "text": "招标人不得向他人泄露投标人的商业秘密",
             "required": 1, "source": "manual"},
            {"kind": "hard", "text": "评标委员会应当按照评标标准和方法评标",
             "required": 1, "source": "manual"}])
        hard = reqtable.hard_texts(pid)
        chk.ok(len(hard) == 1 and "营业执照" in hard[0],
               "effective() 不再把招标方义务当成投标人必需要求")
        h = health.assess(pid)
        chk.ok(int((h.get("metrics") or {}).get("requirements") or 0) == 1,
               "体检的必需要求条数与要求表同口径")

        reqs = ("【本章认领的招标要求】\n"
                "- 不满足招标文件第 4.1 条要求的作废标处理\n"
                "- 国家电网有限公司技术规范书须逐条响应\n"
                "- 投标有效期不少于 90 天")
        draft = section_types.structured_draft("商务偏差表", "原件核验", reqs)
        chk.ok(rules.QUOTE_MARK in draft, "抄录列带引用标记")
        mine = rules.strip_quoted(draft)
        chk.ok("待人工核验" in mine, "剔除引文后我方待填列仍保留，不是整章跳过")
        chk.ok("国家电网有限公司" not in mine and "90 天" not in mine,
               "引文里的招标方名称与数字不进入我方表述")
        issues = rules.rule_check([{"id": "s1", "title": "商务偏差表", "text": draft}])
        chk.ok(not any("负偏离" in str(x.get("title") or "") for x in issues),
               "招标原文的「不满足」不再误报负偏离")
        chk.ok(quality.assess_section(
            "", {"title": "商务偏差表", "text": draft}, [])["ok"],
            "条款号 / 投标有效期不再被判成「无依据数字」")

        sid = store.list_sections(pid)[0]["id"]
        store.update_section(sid, draft)
        ctx = core_bridge._ctx(pid)
        chk.ok("国家电网有限公司" not in ctx["bid_text"],
               "基座暗标 / 引用溯源拿到的正文已剔除招标方引文")

        data = docx_export.build_docx(
            "引文暗标测试", [{"title": "商务偏差表", "text": draft}],
            {"blind": False, "header": "", "date": "2026-11-14"})
        scan = blindcheck.scan_docx(data, pid)
        chk.ok(not any("企业名称" in str(x.get("detail") or "")
                       for x in scan.get("issues") or []),
               "最终文件暗标扫描不把招标方名称当成投标人身份信息")
        # 反证：去掉引用标记后同一个公司名仍要被抓到，说明是「按引文排除」
        # 而不是把暗标规则整体关掉
        plain = draft.replace(rules.QUOTE_MARK, "")
        data_plain = docx_export.build_docx(
            "引文暗标反证", [{"title": "商务偏差表", "text": plain}],
            {"blind": False, "header": "", "date": "2026-11-14"})
        scan_plain = blindcheck.scan_docx(data_plain, pid)
        chk.ok(any("企业名称" in str(x.get("detail") or "")
                   for x in scan_plain.get("issues") or []),
               "没有引用标记时同一名称仍被抓到，暗标规则没有被整体关掉")
    finally:
        drop_project(pid)


def test_state_project_summaries(chk: Checker) -> None:
    """/api/state 每次刷新都拉，不能把每本招标全文一起读出来、一起下发。"""
    from backend.app import main as app_main
    long_text = "招标要求与评分办法逐条响应。" * 25000
    store.create_project("state 摘要校验", long_text)
    rows = store.list_project_summaries()
    chk.ok(bool(rows), "摘要查询至少返回一行")
    chk.ok(all("tender_text" not in p for p in rows), "摘要查询不读招标全文列")
    st = app_main.state()
    items = st.get("projects") or []
    chk.ok(bool(items), "state 至少返回一个项目摘要")
    chk.ok(all("tender_text" not in p for p in items), "state 不再下发项目招标全文")
    chk.ok(all("sections" not in p and "summary" not in p for p in items),
           "state 不再下发章节 / 解读大字段")
    chk.ok(all({"id", "name", "created_at"} <= set(p) for p in items),
           "侧栏需要的 id / 名称 / 时间仍在")
    chk.ok(len(json.dumps(st, ensure_ascii=False)) < 200000,
           "state 响应体不随招标全文字数膨胀")
    detail = store.get_project(next(p["id"] for p in items
                                   if p["name"] == "state 摘要校验"))
    chk.ok(len(str(detail.get("tender_text") or "")) > 100000,
           "按需详情仍能取到完整招标原文，摘要化没有删数据")


def test_end_to_end_export(chk: Checker) -> None:
    """端到端冒烟：冻结快照 → 渲染 Word → 最终文件检查 → 回读正文。"""
    print("\n[E2E] 冻结快照 / Word 渲染 / 最终文件检查")
    import io as _io
    import zipfile as _zip
    from docx import Document as _Document
    from ..app import auth, delivery, docx_export, readiness, rules, store

    chk.ok(rules.PLACEHOLDER.search(
        "未提供保单查询途径或提供途径招标人无法查询的") is None,
        "招标文件原文里的「未提供…」不算占位符，试点指标不误报")
    chk.ok(rules.PLACEHOLDER.search(
        "资质证明：未提供 / 需人工补充") is not None,
        "真正待补的占位内容仍会被拦住")

    tok = auth.set_current({"id": "e2e-test", "username": "e2e-test",
                            "role": "owner", "org_id": "default"})
    try:
        with store.use_org("default"):
            pid = store.create_project(
                "导出冒烟", "第一章 招标公告\n投标人须逐条响应技术规范书。")["id"]
            try:
                store.set_sections(pid, [{"title": "第一章 技术响应"},
                                         {"title": "第二章 服务方案"}])
                secs = store.list_sections(pid)
                store.update_section(
                    secs[0]["id"],
                    "我方按招标文件技术规范书逐条响应，产品符合 10kV 电缆附件要求。" * 3)
                store.update_section(
                    secs[1]["id"],
                    "提供 7×24 小时技术支持，响应时间不超过 30 分钟。" * 3)
                snap = delivery.freeze(pid)
                outline = [s["title"] for s in store.list_sections(pid)]
                data = docx_export.build_docx(
                    snap["name"], snap["sections"],
                    {"required_sections": outline, "header": snap["name"],
                     "date": snap["at"][:10], "snapshot_version": snap["version"]})
                chk.ok(len(data) > 5000, "正式导出链路产出非空 Word 字节")
                with _zip.ZipFile(_io.BytesIO(data)) as z:
                    chk.ok("word/document.xml" in z.namelist(),
                           "Word 包结构完整，可被 Word 打开")
                doc = _Document(_io.BytesIO(data))
                body = "\n".join(p.text for p in doc.paragraphs)
                chk.ok("第一章 技术响应" in body and "7×24" in body,
                       "回读 docx 能读到章节标题与正文，不是空壳")
                insp = delivery.inspect_document(data, "docx", snap)
                chk.ok(insp["checked"] and not insp["blocking"],
                       "最终文件检查跑完且对干净稿子没有硬伤")
                chk.ok(readiness.assess(pid)["level"] != "green",
                       "没做整本体检时不把导出冒烟误判为可交付")
            finally:
                store.delete_project(pid)
    finally:
        auth.reset_current(tok)


def test_gate_digest(chk: Checker) -> None:
    """导出提醒聚合：上百条按类收拢，重点项优先，明细一条不丢。"""
    print("\n[GATE] 导出提醒聚合")
    from ..app import auto_core

    def issue(k, title, level="中", **kw):
        return {"key": k, "title": title, "level": level,
                "detail": kw.pop("detail", "说明"), **kw}

    items = [
        issue("required-0", "必需检查未完成 · 报价", "高", blocking=True),
        issue("health-incomplete", "自动体检有项目没跑完，不能当作已通过",
              "高", blocking=True),
        issue("compliance-0", "同一招标文件已用于多家企业投标", "高", blocking=True),
        issue("q0", "第 3 章本章未达交付标准", "高", blocking=True),
        issue("form-0", "保护表单待核验 · 投标函", "高", blocking=True),
        issue("source_pending", "资料解析待核对，不能当作已核查", "高", blocking=True),
        issue("facts-0", "待补数值 · 产能", "中"),
        issue("health-0", "自动体检 · 模拟评标提示", "中"),
        issue("e0", "某条一般检查提示", "中"),
    ]
    items += [issue(f"req-{i}", f"必需要求未逐条落实 · 要求{i}", "高",
                    blocking=True) for i in range(60)]
    before = dict(items[0])
    d = auto_core.group_export_issues(items)
    groups = d["groups"]
    chk.ok(items[0] == before, "聚合不改动原始清单，逐条确认与留痕口径不变")
    chk.ok(sum(g["count"] for g in groups) == len(items),
           "分组数量合计等于原始清单，一条不丢", str([g["count"] for g in groups]))
    chk.ok(sum(len(g["items"]) for g in groups) == len(items),
           "展开明细里能找到全部原始提醒")
    chk.ok(len(groups) >= 7, "不同来源的提醒能分开，不都挤进「其他」",
           str([g["label"] for g in groups]))
    chk.ok(any(g["key"] == "requirements" and g["count"] == 60 for g in groups),
           "60 条同类要求收成一组，不再平铺 60 张卡")
    chk.ok("69 项提醒" in d["summary"] and "最多的是" in d["summary"],
           "一句话说清总数与最大类", d["summary"])
    chk.ok(len(d["top"]) <= 6 and len({x["group_label"] for x in d["top"]}) >= 3,
           "重点项最多 6 条且覆盖多类，不是 6 条同类要求")
    chk.ok(d["top"] and d["top"][0].get("group_label") == "检查没跑完",
           "检查没跑完排在最前，不埋在几十条要求后面")
    empty = auto_core.group_export_issues([])
    chk.ok(empty["groups"] == [] and empty["top"] == []
           and "0 项提醒" in empty["summary"], "空清单也给出结论，不报错")

    bl = auto_core.business_level
    chk.ok(bl({"blocking": True}) == "硬性否决", "阻断项归类为硬性否决")
    chk.ok(bl({"level": "高"}) == "硬性扣分", "高风险非阻断归类为硬性扣分")
    chk.ok(bl({"level": "中"}, "requirements") == "硬性扣分",
           "必需要求未落实影响评分，不只是参考")
    chk.ok(bl({"level": "中"}, "other") == "参考项", "一般提示归类为参考项")
    impact = d["impact"]
    chk.ok(sum(impact.values()) == len(items),
           "四类后果覆盖全部提醒，不多不少", str(impact))
    chk.ok(impact["加分项"] == 0 and "不自动判定加分项" in d["impact_note"],
           "不硬凑加分项，并如实说明系统没查加分机会")
    chk.ok(all(x.get("business_level") in auto_core.BUSINESS_LEVELS
               for g in groups for x in g["items"]),
           "展开的每条明细都带业务后果标签")
    chk.ok(all(x.get("business_level") in auto_core.BUSINESS_LEVELS
               for x in d["top"]), "重点项也带业务后果标签")

    pid = make_project("门禁聚合", [])
    try:
        gate = auto_core.export_gate(pid)
        chk.ok(isinstance(gate.get("groups"), list) and "summary" in gate,
               "真实 export_gate 返回聚合摘要")
        chk.ok(sum(g["count"] for g in gate["groups"]) == len(gate["issues"]),
               "门禁聚合与 issues 全量一致")
    finally:
        drop_project(pid)


def test_kb_boundary(chk: Checker) -> None:
    """知识库边界：跨企业读不到、项目资料不能绕过项目权限、范例不混用素材。"""
    print("\n[KB] 知识库与范例边界")
    import contextlib
    from ..app import auth, main as app_main, store

    @contextlib.contextmanager
    def as_org(oid: str):
        tok = auth.set_current({"org_id": oid, "role": "owner", "id": "u_" + oid})
        try:
            with store.use_org(oid):
                yield
        finally:
            auth.reset_current(tok)

    A, B = "kb_test_org_a", "kb_test_org_b"
    pid = ""
    try:
        with as_org(A):
            sample = store.add_doc("A 企业范例", "第一章 投标函\n正文",
                                   scope="kb", kind="sample")
            material = store.add_doc("A 通用素材", "素材正文",
                                     scope="kb", kind="material")
            pid = make_project("A 项目", [])
            pdoc = store.add_doc("A 项目资料", "招标文件正文",
                                 scope="project", project_id=pid)

        with as_org(B):
            chk.ok(store.get_doc(sample["id"]) is None,
                   "B 企业按 id 也读不到 A 企业范例（物理分库）")
            chk.ok(all(d["id"] != sample["id"] for d in store.list_docs(scope="kb")),
                   "B 企业知识库清单不含 A 企业条目")
            try:
                app_main.kb_get(sample["id"])
                chk.ok(False, "跨企业读范例必须被拦住")
            except Exception as exc:
                chk.ok(getattr(exc, "status_code", 0) == 404,
                       "跨企业读范例返回 404，不泄露正文")

        with as_org(A):
            for name, call in (
                ("读取", lambda: app_main.kb_get(pdoc["id"])),
                ("改名", lambda: app_main.kb_update(pdoc["id"], {"title": "越权改名"})),
                ("删除", lambda: app_main.kb_delete(pdoc["id"])),
            ):
                try:
                    call()
                    chk.ok(False, f"项目资料不能被知识库接口{name}")
                except Exception as exc:
                    chk.ok(getattr(exc, "status_code", 0) == 404,
                           f"项目资料走知识库接口{name}时返回 404")
            chk.ok((store.get_doc(pdoc["id"]) or {}).get("title") == "A 项目资料",
                   "被拦之后项目资料原样未变")
            for name, call in (
                ("设为参照范例", lambda: app_main.set_sample(
                    pid, {"doc_id": material["id"]})),
                ("按它建目录", lambda: app_main.outline_from_sample(
                    pid, {"doc_id": material["id"]})),
            ):
                try:
                    call()
                    chk.ok(False, f"通用素材不能{name}")
                except Exception as exc:
                    chk.ok(getattr(exc, "status_code", 0) == 400,
                           f"通用素材{name}被拦住")
            r = app_main.set_sample(pid, {"doc_id": sample["id"]})
            chk.ok(bool(r.get("ok")) and "note" in r,
                   "整本标书范例可正常指定，并如实标注核验状态")
            chk.ok((store.get_project(pid) or {}).get("sample_doc_id") == sample["id"],
                   "项目最终参照的是整本范例，不是通用素材")
            store.delete_doc(sample["id"])
            store.delete_doc(material["id"])
            drop_project(pid)
    finally:
        for o in (A, B):
            try:
                store.org_db(o).unlink(missing_ok=True)
            except OSError:
                pass


def test_cross_process_claim(chk: Checker) -> None:
    """写任务认领靠数据库事务，不靠只有本进程能看见的 threading.Lock。"""
    print("\n[LOCK] 跨进程写任务认领")
    import threading
    from ..app import store

    class _NoLock:
        """模拟另一个进程：它有自己的锁，看不见本进程的锁。"""
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    pid = make_project("跨进程认领 A", [])
    pid2 = make_project("跨进程认领 B", [])
    real_lock = store._ACTIVE_JOB_LOCK
    try:
        a, busy = store.create_job_exclusive("compose", pid)
        chk.ok(bool(a) and not busy, "第一个写任务能正常认领")
        b, busy2 = store.create_job_exclusive("section", pid)
        chk.ok(not b and busy2.get("id") == a,
               "同项目已有写任务时返回占用它的那个任务")
        store.update_job(a, status="done")

        store._ACTIVE_JOB_LOCK = _NoLock()   # 两个「进程」各持一把锁
        barrier = threading.Barrier(2)
        out: list = []

        def claim():
            try:
                barrier.wait(timeout=5)
                out.append(store.create_job_exclusive("compose", pid))
            except Exception as exc:
                out.append(("ERR", {"error": str(exc)[:120]}))

        ts = [threading.Thread(target=claim) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        store._ACTIVE_JOB_LOCK = real_lock
        got = [x for x in out if x and x[0] and x[0] != "ERR"]
        chk.ok(len(out) == 2 and len(got) == 1,
               "两个进程各持锁时，同项目仍然只有一个能拿到写权限",
               str(out)[:220])
        loser = [x for x in out if x and not x[0] and x[1]]
        chk.ok(bool(got) and len(loser) == 1
               and loser[0][1].get("id") == got[0][0],
               "没抢到的一方拿到占用任务，不是静默失败", str(out)[:220])
        c, busy3 = store.create_job_exclusive("compose", pid2)
        chk.ok(bool(c) and not busy3, "不同项目的写任务互不阻塞")
        for j in [x[0] for x in got] + ([c] if c else []):
            store.update_job(j, status="done")
        m = store.concurrency_advice()
        chk.ok(m.get("claim_atomic") is True and "跨进程" in m["boundary"],
               "并发边界说明如实区分已实现与未实现的部分")
        chk.ok(m.get("implemented") is False,
               "整体仍不宣称可以靠加进程直接扩容")
    finally:
        store._ACTIVE_JOB_LOCK = real_lock
        drop_project(pid)
        drop_project(pid2)


def test_demo_honesty(chk: Checker) -> None:
    """演示模式不能被当成真生成；内部标注不能混进正式 Word。"""
    print("\n[DEMO] 演示模式诚实性与内部标注清理")
    from ..app import chains, provenance, readiness

    good_q = {"status": "pass", "complete": True, "all_passed": True,
              "undeliverable": []}
    good_h = {"deliverable_ready": True, "complete": True, "ok": True,
              "gate_issues": []}
    live = readiness.decide(good_q, good_h, [], mode="live", chapters=8)
    chk.ok(live["level"] == "green", "接了真实模型时判定不变")
    demo = readiness.decide(good_q, good_h, [], mode="demo", chapters=8)
    chk.ok(demo["level"] == "red" and "演示" in demo["reason"],
           "演示 / 未接模型时不判绿，并写明模板稿不能投标")
    chk.ok(not (demo["level"] == "green"),
           "演示模式的结论不把用户直接推向下载")

    pid = make_project("演示模式结论", [])
    try:
        r = readiness.assess(pid)
        chk.ok(r.get("mode") in ("demo", "live"),
               "只读接口如实返回当前生成模式")
        chk.ok(r["mode"] != "demo" or r["level"] == "red",
               "没模型时整本结论不被判成可交")
    finally:
        drop_project(pid)

    src = ("一、编制依据\n本章逐条响应招标要求，正文内容完整。\n"
           "所有事实性表述均取自本项目资料与企业素材库；依据清单由系统附在末尾。\n"
           "\n【依据与来源】\n· doc1 p3")
    clean = provenance.clean_for_export(src)
    chk.ok("依据清单" not in clean and "【依据与来源】" not in clean,
           "导出前清掉系统自述句与旧版依据段")
    chk.ok("本章逐条响应招标要求" in clean,
           "清理只删内部标注，保留标书正文")

    # 清理函数同时被单章自检和最终文件检查使用：绝不能顺手把真实占位符删掉，
    # 否则未填完的稿子会静默通过所有检查，比留着占位符更危险。
    mixed = ("一、技术方案\nXXX 待补充\n"
             "所有事实性表述均取自本项目资料与企业素材库；依据清单由系统附在末尾。\n"
             "【需补充】资质证明\n")
    kept = provenance.clean_for_export(mixed)
    chk.ok("XXX 待补充" in kept and "【需补充】资质证明" in kept,
           "真实占位符原样保留，交检查规则拦下")
    chk.ok("依据清单" not in kept,
           "只删平台自述句，不误删同一段里的正文")
    chk.ok("一、技术方案" in kept,
           "清理不整段丢掉标书正文")

    s = chains._demo_response("section", "章节标题：技术方案\n章节要点：逐条响应")
    chk.ok("依据清单" not in s and "AI 建议" not in s,
           "演示模板源头不再写出内部标注")

    # 「本项目我提供的资料」段里同时有事实边界 / 资料原文 / 本章认领要求 /
    # 已核验企业事实。demo 只能引用资料原文，绝不能把写作指令复述进标书正文。
    from ..app import section_types as _st
    human = (
        "章节标题：技术方案\n章节要点：逐条响应\n本项目我提供的资料：\n"
        + _st.guard_text(_st.NARRATIVE) + "\n"
        + "【授权与资质清单.docx】\n"
          "投标人：华辰智能科技有限公司\n"
          "服务响应 30 分钟内到达现场，提供 3 场培训。\n"
        + "【本章认领的招标要求（全表共 85 条，本章 7 条，须逐条明确响应；"
          "其余条目由其他章节响应，不要在本章重复展开，也不要臆造未展示的条款）】\n"
          "- [R1·硬性] 投标人须提供营业执照副本并加盖公章\n"
        + "【已核验的企业事实，可直接引用，不得改写数值或扩大适用范围】\n"
          "- 公司·注册资本：5000 万元（来源：附件 1）\n"
        + "企业素材库命中：\n（无）")
    facts = chains._material_facts(human)
    chk.ok(any("华辰智能科技有限公司" in f for f in facts),
           "资料原文块里的事实仍能被 demo 引用")
    chk.ok(not any("不得" in f or "本章认领" in f or "注册资本" in f for f in facts),
           "事实边界、要求标注、企业事实块都不再被当成项目资料", str(facts))
    body = chains._demo_response("section", human)
    for bad in ("不得编造证书编号", "不得写「我方已取得", "本章认领的招标要求",
                "不要在本章重复展开", "以随附《服务承诺与网点清单》"):
        chk.ok(bad not in body, "导出的 demo 正文不含提示词：" + bad)
    empty = chains._demo_response(
        "section", "章节标题：技术方案\n章节要点：逐条响应\n"
        "本项目我提供的资料：\n（未上传本项目资料）\n企业素材库命中：\n（无）")
    chk.ok("四、本项目资料依据" not in empty,
           "没有本项目资料时不伪造「资料依据」段落")


def test_certificate_expiry(chk: Checker) -> None:
    """证照有效期：填了才查、过期列高风险、不破坏「知情令牌仍可下载」契约。"""
    print("\n[CERT] 证照有效期与到期预警")
    import contextlib
    from datetime import date, timedelta
    from ..app import auth, certificates, health
    from ..app import main as app_main

    today = date.today()
    past = (today - timedelta(days=10)).isoformat()
    soon = (today + timedelta(days=20)).isoformat()
    far = (today + timedelta(days=400)).isoformat()
    st = certificates.expiry_status
    chk.ok(st("", today=today) == "unknown", "没填有效期不判状态，也不打扰用户")
    chk.ok(st(past, today=today) == "expired", "已过期能判出来")
    chk.ok(st(soon, today=today) == "expiring", "60 天内到期能提前提醒")
    chk.ok(st(far, today=today) == "ok", "有效期充足判为有效")
    chk.ok(st("2026/13/01", today=today) == "invalid", "日期格式坏了如实标出来")

    pid = make_project("自测-证照有效期", [{"title": "第一章 技术方案"}])
    try:
        old = store.add_doc("ISO9001 证书", "证书正文", scope="kb", valid_until=past)
        near = store.add_doc("检测报告", "报告正文", scope="project",
                             project_id=pid, valid_until=soon)
        good = store.add_doc("营业执照", "执照正文", scope="kb", valid_until=far)
        blank = store.add_doc("通用素材", "素材正文", scope="kb")
        bad = store.add_doc("填写错的证书", "证书正文", scope="kb",
                            valid_until="不是日期")
        store.add_fact(pid, "本公司", "资质等级", "甲级", valid_until=past)

        chk.ok(store.get_doc(old["id"])["valid_until"] == past,
               "资料有效期落库，刷新后仍在")
        chk.ok(store.update_doc(old["id"], valid_until=far)["valid_until"] == far,
               "有效期可更正")

        r = certificates.scan(pid, today=today)
        chk.ok(r["total"] == 5 and blank["id"] not in [x["id"] for x in r["items"]],
               "没填有效期的普通素材不进清单，不制造噪音", str(r["total"]))
        chk.ok(r["items"][0]["status"] == "expired", "过期的排在最前")
        chk.ok(r["counts"]["expiring"] == 1 and "已过期" in r["summary"],
               "汇总如实给出过期与临期数量", r["summary"])

        issues = certificates.export_issues(pid)
        expired = [x for x in issues if x["key"].startswith("certificate-expired")]
        chk.ok(len(expired) == 1 and all(x["level"] == "高" and x["blocking"]
                                         for x in expired),
               "过期证照在门禁里是高风险重点提醒", str(len(expired)))
        chk.ok(any(x["key"].startswith("certificate-expiring") and x["level"] == "中"
                   for x in issues), "临期证照只作中等提醒")
        chk.ok(any(x["key"].startswith("certificate-invalid") for x in issues),
               "日期填错也提示更正，不静默跳过")
        chk.ok(not any(x["id"] == good["id"] for x in r["items"]
                       if x["status"] == "expired"), "有效证照不误报")

        gate = auto_core.export_gate(pid)
        chk.ok(any(str(x.get("key", "")).startswith("certificate-expired")
                   for x in gate["issues"]), "门禁能看到证照过期提醒")
        ack = auto_core.issue_ack_token(pid, gate["fingerprint"])
        chk.ok(bool(ack.get("ok")),
               "过期仍按既定契约：知情令牌可下载，不改成硬禁止")
        chk.ok(len(health.assess(pid)["checks"]) == 8,
               "接了有效期检查不改动原有 8 项整本体检口径")

        d = app_main.kb_add({"title": "接口建的证书", "content": "正文",
                             "valid_until": soon})
        chk.ok(d.get("valid_until") == soon, "知识库新增接口能带有效期")
        d2 = app_main.kb_update(d["id"], {"valid_until": past})
        chk.ok(d2.get("valid_until") == past, "知识库编辑接口能更正有效期")

        @contextlib.contextmanager
        def as_org(oid: str):
            tok = auth.set_current({"org_id": oid, "role": "owner", "id": "u_" + oid})
            try:
                with store.use_org(oid):
                    yield
            finally:
                auth.reset_current(tok)

        with as_org("cert_org_b"):
            rb = certificates.scan(pid, today=today)
            chk.ok(all(x["title"] != "营业执照" for x in rb["items"]),
                   "B 企业查不到 A 企业知识库里的证照（物理分库）")
    finally:
        drop_project(pid)


def test_quote_gate_detail(chk: Checker) -> None:
    """报价校验本来就在必需能力里，补的是「错在哪一行」要进门禁明细。"""
    print("\n[QUOTE] 报价校验明细接入导出门禁")
    from ..app import core_bridge

    bad_table = ("名称,数量,单价,合价\n"
                 "电缆附件 A,2,100,300\n"
                 "电缆附件 B,1,50,50\n"
                 "合计,,,350\n")
    good_table = ("名称,数量,单价,合价\n"
                  "电缆附件 A,2,100,200\n"
                  "电缆附件 B,1,50,50\n"
                  "合计,,,250\n")
    pid = make_project("自测-报价门禁", [{"title": "投标报价表"}])
    try:
        sid = store.get_project(pid)["sections"][0]["id"]
        store.update_section(sid, bad_table)

        r = core_bridge.run("quote", pid)
        chk.ok(any(sec.get("title") == "issues" and sec.get("rows")
                   for sec in r.get("sections") or []),
               "报价基座确实给出了逐条问题明细")
        detail, n = auto_core._issues_from("quote", "报价表校验", r, pid)
        chk.ok(n >= 1 and any("合价" in x["title"] and x["level"] == "高"
                              for x in detail),
               "合价算错能映射成一条高风险提醒，不再只说「检查未通过」",
               str([x["title"] for x in detail]))

        gate = auto_core.export_gate(pid)
        hits = [x for x in gate["issues"]
                if "[报价表校验]" in str(x.get("title") or "")]
        chk.ok(any(x["level"] == "高" and "合价" in str(x.get("title") or "")
                   for x in hits), "导出门禁逐条列出报价硬伤", str(len(hits)))
        ran = auto_core.run_stage("export", pid, force=True)["ran"]
        q = next((x for x in ran if x["key"] == "quote"), None)
        chk.ok(q and q["status"] == "fail" and not q["ok"],
               "明细有高风险时报价检查不显示成「通过」", str(q and q["status"]))

        store.update_section(sid, good_table)
        clean = core_bridge.run("quote", pid)
        clean_detail, _ = auto_core._issues_from("quote", "报价表校验", clean, pid)
        chk.ok(not any(x["level"] == "高" for x in clean_detail),
               "报价改对后不再有高风险明细")
        ran2 = auto_core.run_stage("export", pid, force=True)["ran"]
        q2 = next((x for x in ran2 if x["key"] == "quote"), None)
        chk.ok(q2 and q2["status"] != "fail", "改对后报价检查恢复为通过/不适用",
               str(q2 and q2["status"]))

        store.update_section(sid, "本章还没有填报价表。")
        missing = core_bridge.run("quote", pid)
        chk.ok(bool(missing.get("skipped")) and "无法校验" in str(missing.get("summary")),
               "没有真实报价表时如实说「无法校验」")
        chk.ok(auto_core._cap_status(missing) == "missing_input",
               "缺报价表判为缺输入，不冒充「不适用」或「已通过」")
        chk.ok(not auto_core._issues_from("quote", "报价表校验", missing, pid)[0],
               "缺输入时不编造报价问题，也不用示例金额冒充校验结果")
    finally:
        drop_project(pid)


def test_goods_list(chk: Checker) -> None:
    """货物清单与限价：抽文件里真有的列，没有的不编造。"""
    print("\n[GOODS] 货物清单结构化与限价核对")
    from ..app import goods_list
    from ..app import main as app_main

    header = ("分标编号 | 包名称 | 分包编号 | 项目单位 | 需求单位 | 项目名称 | "
              "工程电压等级 | 物资名称 | 物资描述 | 单位 | 数量 | 最后一批交货日期")
    row1 = ("SL265C | 包1 | AH100 | 甲公司 |  | 项目X | 10kV | "
            "10kV电缆终端A | 3×240 冷缩 | 套 | 505 | 2026-12-01")
    row2 = ("SL265C | 包1 | AH100 | 甲公司 |  | 项目X | 10kV | "
            "1kV电缆终端B | 4×70 冷缩 | 套 |  | 2026-12-01")
    row3 = ("SL265C | 包2 | AH200 | 甲公司 |  | 项目X | 10kV | "
            "电缆中间接头C | 3×150 冷缩 | 套 | 120 | 2026-12-01")
    text = "\n".join(("【工作表：测试】", header, row1, row2, row3))

    r = goods_list.extract(text)
    chk.ok(r["found"] and r["total"] == 3, "清单行数抽对", str(r["total"]))
    chk.ok(r["columns"].get("name") == 7,
           "「项目名称」不会抢走「物资名称」列", str(r["columns"]))
    chk.ok(r["rows"][0]["name"].startswith("10kV电缆终端")
           and r["rows"][0]["qty"] == "505" and r["rows"][0]["unit"] == "套",
           "物资名称 / 数量 / 单位按列对齐", str(r["rows"][0])[:120])
    chk.ok(r["missing_qty_count"] == 1 and "1kV电缆终端B" in r["missing_qty"][0],
           "数量为空的行单独标出来", str(r["missing_qty"]))
    chk.ok(not r["has_budget"], "没有预算列就如实说没有，不编造金额")

    chk.ok(not goods_list.extract("这是一段普通正文，没有任何表格。")["found"],
           "没有清单时不硬凑结果")
    chk.ok(goods_list.extract("名称 | 数量\n" + "A | 1")["found"],
           "简单两列清单也能识别")
    chk.ok(sum(goods_list.extract("★关键参数 1*500 1024*768")["grade_marks"].values()) == 1,
           "★算强制参数标记，乘号 * 不算")
    chk.ok(goods_list.extract_cap("本项目无最高限价，按总价报价。")["status"] == "none",
           "明确「无最高限价」不会被读成一个金额")
    chk.ok(goods_list.extract_cap("3.2.4 | 最高限价 | 无")["status"] == "none",
           "前附表的「最高限价 | 无」也能认出来")
    chk.ok(goods_list.extract_cap("最高限价为 350 万元，超出作废。")["status"] == "amount",
           "有明确金额才抽限价")
    chk.ok(goods_list.extract_cap("最高限价在投标人须知前附表中载明。")["status"] == "unclear",
           "只提到限价没给数值时标含糊，不猜")
    chk.ok(goods_list.extract_cap("正文没有提到这件事。")["status"] == "absent",
           "没提限价就标未提及")

    pid = make_project("自测-货物清单", [{"title": "第一章 技术方案"}])
    try:
        store.update_project(pid, tender_text=text + "\n本项目无最高限价。")
        s = goods_list.scan(pid)
        chk.ok(s["found"] and s["total"] == 3 and s["cap"]["status"] == "none",
               "整项目扫描能读到招标文件正文里的清单与限价")
        issues = goods_list.export_issues(pid)
        chk.ok(any(x["key"] == "goods-missing-qty" and x["level"] == "中"
                   for x in issues), "漏数量在门禁里是可核对的提醒")
        chk.ok(not any(x["key"].startswith("goods-cap") for x in issues),
               "明确无最高限价时不制造噪音")
        chk.ok(app_main.goods_scan(pid)["total"] == 3, "只读接口能返回清单")

        store.update_project(pid, tender_text=text + "\n最高限价在前附表载明。")
        chk.ok(any(x["key"] == "goods-cap-unclear"
                   for x in goods_list.export_issues(pid)),
               "限价读不明确时提示人工核对")
        gate = auto_core.export_gate(pid)
        chk.ok(any(str(x.get("key", "")).startswith("goods-")
                   for x in gate["issues"]), "清单提醒接进导出门禁")
    finally:
        drop_project(pid)


def test_cap_and_multi_table(chk: Checker) -> None:
    """限价算术比对 + 多来源总价交叉核对：只用确定值，缺输入一律不猜。"""
    print("\n[CAP] 限价与多表总价核对")
    from ..app import auto_core, quote_gate
    from ..app import main as app_main

    table = "\n".join((
        "第一章 投标报价明细",
        "名称,数量,单价,合价",
        "电缆附件A,2,100,200",
        "电缆附件B,1,50,50",
        "合计,,,250",
    ))
    pid = make_project("自测-限价核对", [])
    try:
        def write(parts: list[tuple[str, str]]) -> None:
            # set_sections 只按标题继承旧正文，写正文必须逐章 update_section
            store.set_sections(pid, [{"title": t} for t, _ in parts], replace_text=True)
            for row, (_, txt) in zip(store.get_project(pid)["sections"], parts):
                store.update_section(row["id"], txt)

        write([("报价明细", table)])
        store.update_project(pid, tender_text="本项目无最高限价。")
        r = quote_gate.scan(pid)
        chk.ok(r["quote_found"] and r["total"] == 250,
               "能从报价表算出合计", str(r["total"]))
        chk.ok(r["cap_verdict"] == "none" and not r["issues"],
               "明确无限价时不判超限、不制造噪音", r["summary"])

        store.update_project(pid, tender_text="最高限价为 0.02 万元，超出作废。")
        r = quote_gate.scan(pid)
        chk.ok(r["cap_yuan"] == 200.0, "万元限价折算成元", str(r["cap_yuan"]))
        chk.ok(r["cap_verdict"] == "over"
               and any(x["key"] == "quote-over-cap" and x["level"] == "高"
                       for x in r["issues"]), "合计超过限价时给出高风险提醒")
        chk.ok("评审口径" in "".join(str(x.get("detail") or "") for x in r["issues"]),
               "结论写明口径边界，不冒充评审判定")

        store.update_project(pid, tender_text="最高限价为 0.05 万元。")
        r = quote_gate.scan(pid)
        chk.ok(r["cap_verdict"] == "within"
               and not any(x["key"] == "quote-over-cap" for x in r["issues"]),
               "未超限时如实判通过，不虚报", r["summary"])

        store.update_project(pid, tender_text="最高限价在投标人须知前附表中载明。")
        r = quote_gate.scan(pid)
        chk.ok(r["cap_verdict"] == "unclear" and r["cap_yuan"] is None,
               "限价含糊时不猜金额")

        store.update_project(pid, tender_text="本项目无最高限价。")
        write([("报价明细", table),
               ("投标函", "投标函总价：人民币 300 元")])
        r = quote_gate.scan(pid)
        chk.ok(any(d["value"] == 300 for d in r["declarations"]),
               "能抽到投标函总价声明", str(r["declarations"]))
        chk.ok(r["cross_verdict"] == "mismatch"
               and any(x["key"] == "quote-total-mismatch" for x in r["issues"]),
               "投标函与明细合计不一致时列为高风险")

        table2 = (table.replace("电缆附件B,1,50,50", "电缆附件B,1,150,150")
                       .replace("合计,,,250", "合计,,,350"))
        write([("报价明细", table),
               ("开标一览表", table2)])
        r = quote_gate.scan(pid)
        chk.ok(len(r["tables"]) >= 2, "正文里的第二张报价表也能单独解析",
               str(len(r["tables"])))
        chk.ok(any(x["key"] == "quote-total-mismatch" for x in r["issues"]),
               "多张报价表总价不一致时报警")

        write([("报价明细", table),
               ("投标函", "投标总价（大写）：贰佰伍拾元整")])
        r = quote_gate.scan(pid)
        chk.ok(any(d["label"] == "大写金额" and abs(d["value"] - 250) < 0.02
                   for d in r["declarations"]),
               "中文大写金额能折算并与小写核对", str(r["declarations"]))
        chk.ok(not any(x["key"] == "quote-total-mismatch" for x in r["issues"]),
               "大小写一致时不误报")

        write([("技术方案", "本章描述实施方案。")])
        store.update_project(pid, tender_text="最高限价为 0.02 万元。")
        r = quote_gate.scan(pid)
        chk.ok(r["cap_verdict"] == "no_quote"
               and any(x["key"] == "quote-cap-no-table" for x in r["issues"]),
               "没有报价表时如实说无法判定，不用示例金额冒充")
        chk.ok(not any(x["key"] == "quote-over-cap" for x in r["issues"]),
               "无报价表时绝不判超限")
        chk.ok(any(x["key"] == "quote-cap-no-table"
                   for x in auto_core.export_gate(pid)["issues"]),
               "新结论接进导出门禁")
        chk.ok(app_main.quote_gate_scan(pid)["cap_verdict"] == "no_quote",
               "只读接口可访问")
    finally:
        drop_project(pid)


def test_certificate_front(chk: Checker) -> None:
    """证照状态前置到「能不能交」卡，但不改变原有结论口径。"""
    print("\n[CERT-VIEW] 证照状态前置可见")
    from ..app import main as app_main
    from datetime import date, timedelta

    pid = make_project("自测-证照前置", [])
    try:
        store.add_fact(pid, "本公司", "资质证书", "甲级",
                       valid_until=(date.today() - timedelta(days=1)).isoformat())
        r = app_main.project_readiness(pid)
        chk.ok("已过期" in str(r.get("cert_hint") or ""),
               "进项目就能看到证照过期，不必等到导出", str(r.get("cert_hint")))
        chk.ok(int((r.get("cert_counts") or {}).get("expired") or 0) >= 1,
               "状态计数与有效期汇总一致")
        chk.ok(r["level"] in ("red", "yellow", "green")
               and "action" in r and "findings" in r,
               "前置提示没有改动原有三态结论与动作")
    finally:
        drop_project(pid)


def test_brief(chk: Checker) -> None:
    """解读汇总：只汇总已有抽取结果，不补示例值、不宣称已核对。"""
    print("\n[BRIEF] 招标解读汇总")
    from ..app import brief, reqtable
    from ..app import main as app_main
    from datetime import date, timedelta

    tender = ("分标编号 | 物资名称 | 单位 | 数量\n"
              "SL1 | 10kV电缆终端A | 套 | 505\n"
              "SL1 | 1kV电缆终端B | 套 | 120\n"
              "本项目无最高限价。投标人须提供营业执照。")
    pid = make_project("自测-解读汇总", [{"title": "技术方案"}])
    try:
        empty = brief.scan(pid)
        chk.ok("还没有导入招标文件正文" in "".join(empty["lines"]),
               "没有招标原文时如实说没有，不编造")
        chk.ok("不是已确认事实" in empty["note"]
               and empty["goods"]["total"] == 0,
               "空项目不补示例清单，且明确标注待人工核对")

        store.update_project(pid, tender_text=tender)
        past = (date.today() - timedelta(days=3)).isoformat()
        store.add_fact(pid, "本公司", "营业执照有效期", "有效", valid_until=past)
        r = brief.scan(pid)
        chk.ok(r["tender_chars"] > 0 and not r["errors"],
               "汇总能跑完且不留内部错误", str(r["errors"]))
        chk.ok(r["goods"]["found"] and r["goods"]["total"] == 2,
               "清单行数来自真实抽取", str(r["goods"]["total"]))
        chk.ok(r["goods"]["cap_status"] == "none",
               "限价状态来自 goods_list，不另造一套判定")
        chk.ok(r["quote"]["cap_verdict"] == "none",
               "报价结论沿用 quote_gate 口径")
        # 企业知识库是跨项目共享的，别的测试留下的证照也会进来，
        # 所以只断言本次登记的那条确实出现在汇总里。
        chk.ok(r["certificates"]["expired"] >= 1
               and any("营业执照有效期" in str(x["title"])
                       for x in r["certificates"]["items"]),
               "过期证照在汇总里就能看到", str(r["certificates"]["expired"]))
        chk.ok(any("抽出" in x and "条要求" in x for x in r["lines"]),
               "要求条数写进一句话结论")
        chk.ok("不是已确认事实" in r["note"]
               and "人工核对" in r["note"], "结论不冒充已核对无误")
        chk.ok(app_main.brief_scan(pid)["goods"]["total"] == 2,
               "只读接口可访问")
        for i in range(8):
            reqtable.add_row(pid, "scoring", f"评审要素{i + 1}", score="5")
        many = brief.scan(pid)
        chk.ok(many["scoring"]["total"] == 8
               and len(many["scoring"]["rows"]) == 6,
               "评分要素总数报全量，明细只预览前 6 条",
               f"{many['scoring']['total']}/{len(many['scoring']['rows'])}")
    finally:
        drop_project(pid)


def test_outline_confirm_gate(chk: Checker) -> None:
    """P1：目录没确认不放行；确认后放行；目录一变立即失效；老项目升级不锁死。"""
    import sqlite3
    from fastapi import HTTPException
    from ..app import chat, main as app_main
    print("\n[P1] 目录确认闸门")
    pid = make_project("P1 目录闸门", [
        {"title": "第一章 投标函", "text": ""},
        {"title": "第二章 技术方案", "text": ""},
        {"title": "第三章 报价表", "text": ""},
    ])
    try:
        store.update_project(pid, tender_text="第一章 招标公告\n投标人须提供营业执照并逐条响应。")
        secs = store.list_sections(pid)
        chk.ok(not store.outline_confirmed(pid), "新项目默认未确认目录")
        for label, fn in (
            ("整本生成", lambda: app_main.compose(pid, {})),
            ("单章生成", lambda: app_main.section_generate(pid, secs[0]["id"])),
            ("单章改写", lambda: app_main.section_rewrite(pid, secs[1]["id"], {})),
        ):
            try:
                fn()
                chk.ok(False, label + " 未确认时被拦")
            except HTTPException as ex:
                chk.ok(ex.status_code == 409 and "确认" in str(ex.detail),
                       label + " 未确认时返回 409 并提示确认")
        r = chat.handle(pid, "写整本标书", action="compose")
        chk.ok(r.get("action") == "outline_unconfirmed" and not r.get("job_id"),
               "对话入口同样被拦，不会偷偷起后台任务")

        empty = make_project("P1 空目录", [])
        try:
            chk.ok(not store.confirm_outline(empty)["ok"], "空目录不允许确认")
        finally:
            drop_project(empty)

        ok = store.confirm_outline(pid, by="tester")
        chk.ok(ok["ok"] and ok["chapters"] == 3, "确认后记录章节数与确认时间")
        chk.ok(store.outline_confirmed(pid)
               and bool(str(store.get_project(pid).get("outline_confirmed_at") or "")),
               "确认位已落库且可读")
        try:
            app_main._require_outline_confirmed(pid)
            chk.ok(True, "确认后闸门放行")
        except HTTPException:
            chk.ok(False, "确认后闸门放行")

        moved = store.move_section(pid, secs[0]["id"], 1)
        now_rows = store.list_sections(pid)
        chk.ok(moved["ok"] and moved["to"] == 1 and now_rows[1]["id"] == secs[0]["id"],
               "下移只换顺序，章节 id 与正文不动")
        chk.ok(now_rows[1]["title"] == "第一章 投标函"
               and store.get_project(pid)["outline"][1]["title"] == "第一章 投标函",
               "章节列表与 projects.outline 顺序同步")
        edge = store.move_section(pid, now_rows[0]["id"], -1)
        chk.ok(not edge["ok"] and edge["error"] == "boundary", "已经到头时明确报错")
        chk.ok(not store.outline_confirmed(pid), "调整顺序后确认自动失效")

        for label, fn in (
            ("改章节名", lambda: store.rename_section(pid, secs[0]["id"], "第一章 投标函（修订）")),
            ("新增章节", lambda: store.add_section(pid, "第四章 服务承诺")),
            ("套用旧目录", lambda: store.apply_outline_titles(pid, ["甲", "乙"])),
            ("重建目录", lambda: store.set_sections(pid, [{"title": "甲"}, {"title": "乙"}])),
        ):
            store.confirm_outline(pid)
            fn()
            chk.ok(not store.outline_confirmed(pid), label + "后确认自动失效")

        # 模拟升级前的老库：补列只在第一次发生，已写过正文的项目自动视为已确认
        legacy_dir = Path(tempfile.mkdtemp(prefix="bidagent_p1_legacy_"))
        legacy = legacy_dir / "legacy.db"
        try:
            lc = sqlite3.connect(legacy)
            lc.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    tender_text TEXT DEFAULT '', summary TEXT DEFAULT '',
                    outline TEXT DEFAULT '[]', created_at TEXT NOT NULL);
                CREATE TABLE sections (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, title TEXT NOT NULL, text TEXT DEFAULT '',
                    protected INTEGER DEFAULT 0, updated_at TEXT NOT NULL);
                INSERT INTO projects(id,name,created_at) VALUES('old','老项目','2026-01-01 09:00:00');
                INSERT INTO sections(id,project_id,seq,title,text,updated_at)
                    VALUES('s1','old',0,'第一章','已有正文','2026-01-01 09:00:00');
                INSERT INTO projects(id,name,created_at) VALUES('blank','空项目','2026-01-01 09:00:00');
                INSERT INTO sections(id,project_id,seq,title,text,updated_at)
                    VALUES('s2','blank',0,'第一章','','2026-01-01 09:00:00');
            """)
            lc.commit()
            lc.close()
            store._apply_schema(legacy)
            lc = sqlite3.connect(legacy)
            got = dict(lc.execute("SELECT id, outline_confirmed_at FROM projects").fetchall())
            lc.close()
            chk.ok(bool(got.get("old")) and not got.get("blank"),
                   "升级补列时：老项目已写正文视为已确认，空项目仍需确认")
            store._apply_schema(legacy)       # 再跑一次迁移，不能把人工清空的状态填回去
            lc = sqlite3.connect(legacy)
            lc.execute("UPDATE projects SET outline_confirmed_at='' WHERE id='old'")
            lc.commit()
            lc.close()
            store._apply_schema(legacy)
            lc = sqlite3.connect(legacy)
            again = lc.execute("SELECT outline_confirmed_at FROM projects WHERE id='old'").fetchone()[0]
            lc.close()
            chk.ok(not str(again or ""), "重复执行迁移不会偷偷恢复已清空的确认位")
        finally:
            shutil.rmtree(legacy_dir, ignore_errors=True)
    finally:
        drop_project(pid)


def test_enterprise_assets(chk: Checker) -> None:
    """P2：五分类台账 / 有效期五态 / CRUD / 旧来源不迁移 / 导出提醒仍只是提醒。"""
    from datetime import date, timedelta
    from ..app import access, assets, certificates, main as app_main
    print("\n[P2] 企业资产台账")

    def wipe() -> None:
        with store.conn() as c:
            c.execute("DELETE FROM assets")

    wipe()
    today = date.today()
    expired = (today - timedelta(days=3)).isoformat()
    expiring = (today + timedelta(days=20)).isoformat()
    ok_date = (today + timedelta(days=400)).isoformat()
    try:
        clean, err = assets.validate({"title": "  ISO9001 认证 ",
                                      "category": "qualification",
                                      "valid_until": ok_date})
        chk.ok(not err and clean["title"] == "ISO9001 认证", "新增校验清洗资产名称")
        _, err = assets.validate({"title": "x", "category": "not_a_category"})
        chk.ok(bool(err), "不是五类之一时拒绝登记")
        _, err = assets.validate({"title": "x", "valid_until": "2026/01/01"})
        chk.ok(bool(err), "日期格式不对时拒绝登记")
        chk.ok(not assets.validate({"title": "x", "valid_until": ""})[1],
               "有效期留空允许（不打扰）")

        a1 = store.add_asset(**clean)
        a2 = store.add_asset(category="personnel", title="一级建造师 张某",
                             valid_until=expired)
        store.add_asset(category="performance", title="同类项目合同", valid_until="")
        store.add_asset(category="testing", title="型式试验报告", valid_until=expiring)
        a5 = store.add_asset(category="award", title="省级优质工程奖",
                             valid_until=ok_date)
        rep = assets.scan(today=today)
        chk.ok(rep["total"] == 5
               and all(rep["categories"][c]["count"] == 1 for c in store.ASSET_CATEGORIES),
               "五类资产计数各 1")
        chk.ok(rep["counts"]["expired"] == 1 and rep["counts"]["expiring"] == 1
               and rep["counts"]["unknown"] == 1,
               "有效期复用同一套五态判定")
        chk.ok(bool(assets.hint(rep)), "有到期风险时给出一行提示")

        row = store.update_asset(a2["id"], title="一级建造师 李某", valid_until=ok_date)
        chk.ok(row["id"] == a2["id"] and row["title"] == "一级建造师 李某",
               "编辑只改字段，资产 id 不变")
        v = store.verify_asset(a2["id"], "tester")
        chk.ok(str(v["verification"]).startswith("verified:"), "人工核验状态落库")
        chk.ok(store.delete_asset(a5["id"]) and not store.delete_asset(a5["id"]),
               "删除成功且重复删除不报错")
        chk.ok(store.update_asset("no-such-id", title="x") is None,
               "编辑不存在的资产返回 None")

        pid = make_project("P2 资产与旧来源", [{"title": "第一章", "text": ""}])
        try:
            store.add_doc("营业执照", "内容", scope="project", project_id=pid,
                          valid_until=ok_date)
            store.add_fact(pid, "公司", "注册资本", "1000", unit="万元",
                           valid_until=expired)
            cc = certificates.scan(pid, today=today)
            origins = {x["origin"] for x in cc["items"]}
            chk.ok({"企业资产", "本项目资料", "企业事实"} <= origins,
                   "资产作为第四来源并列，旧 facts / kb_docs 一条不迁移也不丢")
            store.add_asset(category="qualification", title="已过期资质",
                            valid_until=expired)
            issues = certificates.export_issues(pid)
            chk.ok(any("已过期" in i["title"] and "企业资产" in i["detail"]
                       for i in issues),
                   "过期资产进入导出提醒（契约不变：知情令牌仍可下载）")

            chk.ok(access.action_for(f"/api/projects/{pid}/assets", "GET")[1] == "read"
                   and access.action_for(f"/api/projects/{pid}/assets", "POST")[1] == "write",
                   "资产接口按项目角色做读写权限校验")
            chk.ok(app_main.assets_list(pid)["total"] >= 1, "GET 资产台账接口可用")
            added = app_main.assets_add(pid, {"title": "接口新增", "category": "award"})
            aid = added["item"]["id"]
            app_main.assets_update(pid, aid, {"title": "接口改名"})
            chk.ok(store.get_asset(aid)["title"] == "接口改名", "PUT 编辑接口可用")
            chk.ok(app_main.assets_verify(pid, aid)["item"]["verification"].startswith("verified:"),
                   "POST 核验接口可用")
            app_main.assets_delete(pid, aid)
            chk.ok(store.get_asset(aid) is None, "DELETE 删除接口可用")
        finally:
            drop_project(pid)
        chk.ok(a1["id"] != a2["id"] and store.get_asset(a1["id"]) is not None,
               "资产不绑项目，跨项目仍然存在")
    finally:
        wipe()


def test_asset_gap(chk: Checker) -> None:
    """P3：条款×资产反查只列缺件候选，不判得分；无命中不编造，停用条款不参与。"""
    from datetime import date, timedelta
    from ..app import asset_match, main as app_main, reqtable
    print("\n[P3] 得分材料反查")

    def wipe() -> None:
        with store.conn() as c:
            c.execute("DELETE FROM assets")

    wipe()
    today = date.today()
    good = (today + timedelta(days=300)).isoformat()
    expired = (today - timedelta(days=5)).isoformat()
    # 不设 tender_text：避免 ensure_synced 自动重抽覆盖这里手工登记的要求
    pid = make_project("P3 得分反查", [{"title": "第一章", "text": ""}])
    blank = make_project("P3 无命中", [{"title": "第一章", "text": ""}])
    try:
        cats = asset_match.categories_of("须提供 ISO9001 认证证书及类似业绩合同复印件")
        chk.ok("qualification" in cats and "performance" in cats,
               "一条条款能同时关联两类材料")
        reqtable.add_row(blank, "hard", "投标文件须用 A4 纸装订并加盖骑缝章")
        g0 = asset_match.scan(blank)
        chk.ok(g0["total"] == 0 and g0["rows"] == [] and not g0["hint"],
               "没有可对照材料类型时返回空清单，不编造条目")
        chk.ok(g0["no_requirements"] is False and bool(g0["note"]),
               "空清单也如实带上口径说明")

        for kind, text, score in (
            ("scoring", "投标人具有 ISO9001 认证证书的得 2 分", "2"),
            ("hard", "须提供类似业绩合同复印件及中标通知书", ""),
            ("hard", "项目负责人须具备建造师证书与社保证明", ""),
            ("scoring", "提供第三方检测报告的得 1 分", "1"),
            ("scoring", "获得省级优质工程奖的加分", ""),
        ):
            reqtable.add_row(pid, kind, text, score=score)
        g1 = asset_match.scan(pid, today=today)
        chk.ok(g1["total"] >= 5 and g1["missing"] >= 5,
               "五类条款在没有任何资产时全部列为缺材料候选")
        chk.ok(all("awarded_score" not in x and "points" not in x
                   for x in g1["rows"])
               and "不判定能否得分" in g1["note"],
               "输出不含得分判定字段，明确不判定能否得分")
        chk.ok(bool(g1["hint"]) and "只列候选" in g1["hint"],
               "缺件提示注明只是候选、不代表失分")

        q = store.add_asset(category="qualification", title="ISO9001 证书",
                            valid_until=good)
        store.verify_asset(q["id"], "tester")
        store.add_asset(category="performance", title="旧项目合同",
                        valid_until=expired)
        store.add_asset(category="personnel", title="建造师证 张某",
                        valid_until=good)
        g2 = asset_match.scan(pid, today=today)
        chk.ok(g2["missing"] < g1["missing"], "登记资产后缺件数下降")
        chk.ok(g2["counts"]["candidate"] >= 1 and g2["counts"]["expired"] >= 1
               and g2["counts"]["unverified"] >= 1,
               "已核验 / 过期 / 未核验三种状态分别列出")
        row = next(x for x in g2["rows"] if x["status"] == "candidate")
        chk.ok(row["matched_assets"] and row["needed_material"]
               and row["loc"] is not None,
               "候选条目带已有材料、需要材料与原文位置")
        chk.ok(g2["assets_total"] == 3 and len(store.list_assets()) == 3,
               "反查全程只读，不擅自补录资产")

        reqtable.add_row(pid, "hard", "须提供 ISO9001 认证证书（停用条款）")
        rid = next(r["id"] for r in store.list_requirements(pid)
                   if "停用条款" in str(r.get("text") or ""))
        reqtable.update_row(pid, rid, {"status": "disabled"})
        chk.ok(all("停用条款" not in x["text"] for x in asset_match.scan(pid)["rows"]),
               "已停用的要求不进缺件清单")

        chk.ok(app_main.asset_gap_scan(pid)["total"] == asset_match.scan(pid)["total"],
               "GET /asset-gap 接口与内部结论一致")
        rd = app_main.project_readiness(pid)
        chk.ok("asset_gap" in rd and "asset_gap_hint" in rd,
               "能不能交卡片带上缺件计数与提示")
        chk.ok(rd["asset_gap"].get("missing") == asset_match.scan(pid)["missing"],
               "首页摘要与完整清单计数一致")
        chk.ok(rd.get("level") in ("red", "yellow", "green"),
               "缺件候选不改 readiness 的判定口径，仍按原三态")
    finally:
        drop_project(pid)
        drop_project(blank)
        wipe()


def test_interpret_dimensions(chk: Checker) -> None:
    """P4：15+ 维度只切原文、未识别不编造；★▲ 参数与货物清单分开；按章重点。"""
    from ..app import brief, tender_dims
    from ..app.basecore.services import interpret
    print("\n[P4] 招标要素维度扩展")
    tender = (
        "项目名称：国网某批次物资采购\n项目编号：SG-2026-001\n"
        "招标人：国网某省电力公司\n"
        "投标保证金：人民币 5 万元，须于投标截止前到账。\n"
        "投标截止时间：2026 年 12 月 1 日 9:30；开标时间：同日 10:00。\n"
        "投标有效期 90 天。付款方式：验收合格后 30 日内支付 90%。\n"
        "质保期 24 个月。工期 60 日历天。\n"
        "评标办法：综合评分法。否决投标：未逐条响应的作废标处理。\n"
        "★ 电缆终端局部放电量不大于 10pC。\n"
        "▲ 供货周期不得超过 45 天。\n"
        "分标编号 | 物资名称 | 单位 | 数量\n"
        "SL1 | 10kV电缆终端A | 套 | 505\n"
        "SL2 | 1kV电缆终端B | 套 | 120\n"
    )
    pid = make_project("P4 维度", [{"title": "技术方案与逐条响应"},
                                   {"title": "投标报价表"}])
    try:
        store.update_project(pid, tender_text=tender)
        d = tender_dims.scan(pid)
        chk.ok(d["total"] >= 15, "维度数达到 15 项以上")
        by = {x["key"]: x for x in d["dimensions"]}
        for k in ("bid_bond", "opening_time", "payment", "duration",
                  "warranty", "bid_validity", "eval_method", "reject"):
            chk.ok(by[k]["status"] == "found" and "未识别" not in by[k]["value"],
                   f"{k} 命中并切出原文片段")
        chk.ok("5 万元" in by["bid_bond"]["value"], "保证金值保留原文写法，不换算")
        chk.ok(by["contact"]["status"] == "missing"
               and by["contact"]["value"] == "未识别",
               "原文没写到的维度如实标未识别，不补示例值")
        chk.ok(all(x["pos"] >= 0 and x["loc"] for x in d["dimensions"]
                   if x["status"] == "found"), "命中维度带原文位置")
        star = d["star_params"]
        chk.ok(star["total"] == 2 and any("10pC" in x["text"] for x in star["items"]),
               "★▲ 参数独立抽取且保留数值")
        chk.ok(all(x["mark"] in ("★", "▲") for x in star["items"]),
               "参数标记单独保留，不与货物清单混列")
        focus = d["chapter_focus"]
        tech = next((g for g in focus if "技术方案" in g["title"]), None)
        chk.ok(len(focus) == 2 and tech
               and any(x["key"] == "star_params" for x in tech["items"]),
               "按章节标题给出专项维度，技术章关联 ★▲ 参数")

        r = brief.scan(pid)
        chk.ok(r["dimensions"]["total"] == d["total"]
               and r["dimensions"]["found"] == d["found"], "汇总卡带上维度统计")
        chk.ok(r["star_params"]["total"] == 2 and r["goods"]["total"] == 2,
               "★▲ 参数与货物清单分开，两边数字不串")
        chk.ok("不是已确认事实" in r["note"] and not r["errors"],
               "原有汇总口径与无错状态不变")
        chk.ok(any("招标要素" in x for x in r["lines"]), "一句话结论补充维度行")
        chk.ok(bool(r["dimensions_note"]) and bool(r["chapter_focus"]),
               "口径说明与按章重点一并返回")

        blank = make_project("P4 空原文", [{"title": "第一章"}])
        try:
            b = tender_dims.scan(blank)
            chk.ok(b["found"] == 0 and b["star_params"]["total"] == 0
                   and all(x["value"] == "未识别" for x in b["dimensions"]),
                   "空原文全部标未识别，不编造任何值")
        finally:
            drop_project(blank)

        labels = [x["label"] for x in interpret._placeholders()["basic"]]
        chk.ok(len(labels) >= 12
               and all(k in labels for k in ("投标有效期", "付款方式",
                                             "质保期", "投标文件份数")),
               "AI 解读 basic 补齐四个维度且原有字段不丢")
        chk.ok(all(x["value"] == "" for x in interpret._placeholders()["basic"]),
               "占位值全为空，不填示例")
        rule = interpret.from_rules(tender, "未启用 AI")
        chk.ok(all(k in [x["label"] for x in rule["basic"]]
                   for k in ("投标有效期", "付款方式")),
               "规则回退路径也带同样字段")
    finally:
        drop_project(pid)


def test_deploy_initial_password(chk: Checker) -> None:
    """发布到远端空库时初始密码可由部署方指定；本地行为不变。"""
    from ..app import auth
    print("\n[DEPLOY] 远端空库初始管理员密码")
    old = os.environ.get("BIDAGENT_INIT_ADMIN_PASSWORD")
    try:
        os.environ["BIDAGENT_INIT_ADMIN_PASSWORD"] = "Deploy-Pass-2026"
        chk.ok(auth._initial_password() == "Deploy-Pass-2026",
               "部署方可指定首次初始化的管理员密码")
        os.environ["BIDAGENT_INIT_ADMIN_PASSWORD"] = "123"
        weak = auth._initial_password()
        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        chk.ok(weak != "123" and len(weak) == 12
               and all(c in alphabet for c in weak),
               "短于 8 位的弱口令被忽略，回退随机 12 位")
        os.environ.pop("BIDAGENT_INIT_ADMIN_PASSWORD", None)
        r = auth._initial_password()
        chk.ok(len(r) == 12 and all(c in alphabet for c in r),
               "未配置时仍随机生成，本地/自有服务器行为不变")
    finally:
        if old is None:
            os.environ.pop("BIDAGENT_INIT_ADMIN_PASSWORD", None)
        else:
            os.environ["BIDAGENT_INIT_ADMIN_PASSWORD"] = old


def main() -> int:
    # 和启动时一样先跑增量迁移，否则老库缺 ver / artifacts 会直接报错
    store.init_db()
    chk = Checker()
    print("=" * 68)
    print("可靠性回归（临时项目，跑完即清理）")
    print(f"数据目录：{store.db_path().parent}"
          f"{'（临时，跑完删除）' if _OWNED_DATA else '（外部指定，不删除）'}")
    print("=" * 68)
    try:
        test_section_version(chk)
        test_section_atomic(chk)
        test_section_ownership(chk)
        test_check_unavailable(chk)
        test_blind_table(chk)
        test_artifact_ownership(chk)
        test_protected_empty(chk)
        test_job_states(chk)
        test_compose_honesty(chk)
        test_write_conflict(chk)
        test_task_status_honesty(chk)
        test_check_chain(chk)
        test_parsing_review(chk)
        test_docx_image_ocr(chk)
        test_snapshot_artifacts(chk)
        test_env_and_preflight(chk)
        test_business_integrity(chk)
        test_c01_bundle(chk)
        test_c02_config_isolation(chk)
        test_c02_project_access(chk)
        test_c03_long_tasks(chk)
        test_c04_backup(chk)
        test_section_crud(chk)
        test_req_table_delete(chk)
        test_export_ack(chk)
        test_ack_draft_artifact_kind(chk)
        test_file_check_draft_markers(chk)
        test_consistent_counts_and_wording(chk)
        test_structured_tables_in_docx(chk)
        test_owner_obligation_and_quoted(chk)
        test_project_lifecycle(chk)
        test_writing_context(chk)
        test_real_world_guards(chk)
        test_generation_upgrades(chk)
        test_close_loop(chk)
        test_readiness(chk)
        test_engineering_state(chk)
        test_end_to_end_export(chk)
        test_gate_digest(chk)
        test_kb_boundary(chk)
        test_cross_process_claim(chk)
        test_demo_honesty(chk)
        test_certificate_expiry(chk)
        test_quote_gate_detail(chk)
        test_goods_list(chk)
        test_cap_and_multi_table(chk)
        test_certificate_front(chk)
        test_brief(chk)
        test_outline_confirm_gate(chk)
        test_enterprise_assets(chk)
        test_asset_gap(chk)
        test_interpret_dimensions(chk)
        test_deploy_initial_password(chk)
        test_state_project_summaries(chk)
        test_kb_project_scope(chk)
        test_empty_project_guards(chk)
    except Exception as exc:  # 测试自身写错也要显式记为失败，不能静默退出 0
        chk.failed.append(f"测试自身异常：{type(exc).__name__}: {exc}")
        print(f"\n  FAIL  测试自身异常：{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        print("\n" + "=" * 68)
        print(f"断言 {chk.passed + len(chk.failed)} 项：通过 {chk.passed}，失败 {len(chk.failed)}")
        for x in chk.failed:
            print("  - " + x)
        if _OWNED_DATA:
            shutil.rmtree(_OWNED_DATA, ignore_errors=True)
    print(MANUAL)
    return 1 if chk.failed else 0


def test_kb_project_scope(chk: Checker) -> None:
    """依据池按项目语境过滤：电缆附件项目不再拿视频监控模板当「本次依据」。"""
    print("\n[KB-SCOPE] 依据池项目相关性")
    from ..app import chat, rag, store

    pid = make_project("电缆附件采购", [])
    added: list[str] = []
    try:
        store.update_project(pid, tender_text=(
            "本项目采购 10kV 电缆附件、冷缩终端与中间接头，投标人须提供型式试验报告；"
            "投标有效期 90 天，售后服务与质保期按招标文件执行。"))
        for title in ("视频监控技术方案标准模板", "售后服务标准条款库",
                      "公司资质与承诺", "同类项目偏离表编写要点"):
            added.append(str(store.add_doc(title, "素材正文", scope="kb",
                                           kind="material")["id"]))
        titles = [r["title"] for r in rag.project_kb_rows(pid)]
        chk.ok("视频监控技术方案标准模板" not in titles,
               "带品类词但与本项目无关的素材不进依据池", str(titles))
        chk.ok("售后服务标准条款库" in titles and "公司资质与承诺" in titles,
               "通用业务素材仍可跨项目复用，不被误杀", str(titles))
        chk.ok("同类项目偏离表编写要点" not in titles,
               "核心词被剥光的写法素材不再无条件跨项目混入依据池", str(titles))
        srcs = chat._sources(pid)
        chk.ok(not any("视频监控" in s for s in srcs),
               "「本次依据」不再出现别的品类素材", str(srcs))

        pid2 = make_project("园区视频监控", [])
        try:
            store.update_project(pid2, tender_text=(
                "本项目采购 400 万像素网络摄像机、NVR 与视频监控平台，含 90 天存储。"))
            chk.ok("视频监控技术方案标准模板"
                   in [r["title"] for r in rag.project_kb_rows(pid2)],
                   "同品类项目的相关素材仍能被检索")
        finally:
            drop_project(pid2)

        pid3 = make_project("新投标项目", [])
        try:
            chk.ok(rag.project_kb_rows(pid3) == [],
                   "还没导入招标文件时不拿跨项目素材充当依据")
        finally:
            drop_project(pid3)
    finally:
        for did in added:
            store.delete_doc(did)
        drop_project(pid)


def test_empty_project_guards(chk: Checker) -> None:
    """零正文项目：检查不凭空报高风险，写了正文后规则层照常工作。"""
    print("\n[EMPTY] 零正文项目的检查")
    from ..app import agent, main as app_main, store

    pid = make_project("空项目复核", [])
    try:
        issues = agent.check(pid, 1)
        chk.ok(issues == [], "没有正文时不生成凭空的缺章高风险", str(issues))
        r = app_main.do_check(pid)
        chk.ok(r.get("empty") is True and bool(r.get("reason")),
               "检查接口如实说明还没有正文", str(r.get("reason")))
        chk.ok(r.get("total") == 0, "空项目检查条数为 0")
        chk.ok(store.list_checks(pid, 1) == [],
               "没有把假问题写进检查轮次")

        store.set_sections(pid, [{"title": "技术方案与逐条响应"}])
        sec = store.list_sections(pid)[0]
        store.update_section(sec["id"],
                             "本项目按招标文件技术规范书逐条响应，措施、责任与验收口径均已明确。" * 3)
        chk.ok(len(agent.check(pid, 2)) > 0, "写了正文后检查恢复正常工作")
        chk.ok(app_main.do_check(pid).get("empty") is False,
               "有正文后不再标记为空项目")
    finally:
        drop_project(pid)


if __name__ == "__main__":
    sys.exit(main())
