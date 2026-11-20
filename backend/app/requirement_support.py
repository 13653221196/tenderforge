"""要求级支持关系、材料核验及确认指纹。候选不会自动升级为支持。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import zipfile

from . import store
from .generation_context import digest

TYPES = ("scheme", "qualification", "personnel", "performance", "product",
         "commitment", "original", "unknown")
LABELS = dict(zip(TYPES, ("方案措施", "资格条件", "人员条件", "业绩条件",
                         "产品参数", "本项目承诺", "原件表单", "待分类")))
TABLES = {"fact": "project_facts", "evidence": "evidence", "attachment": "attachments"}


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS requirement_support_links (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, req_id TEXT NOT NULL,
      target_type TEXT NOT NULL, target_id TEXT NOT NULL,
      relation TEXT DEFAULT 'candidate', method TEXT DEFAULT 'manual',
      reviewer TEXT DEFAULT '', reviewed_at TEXT DEFAULT '',
      snapshot TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT NOT NULL,
      UNIQUE(project_id,req_id,target_type,target_id));
    CREATE TABLE IF NOT EXISTS requirement_support_policies (
      project_id TEXT NOT NULL, req_id TEXT NOT NULL, data TEXT NOT NULL,
      PRIMARY KEY(project_id,req_id));
    """)


def requirement(pid, rid):
    return next((r for r in store.list_requirements(pid) if r["id"] == rid), None)


def infer(text):
    for words, kind in [
        (("原件", "签章", "盖章", "表单"), "original"),
        (("社保", "人员", "工程师", "项目经理"), "personnel"),
        (("业绩", "合同金额", "类似项目"), "performance"),
        (("营业执照", "资格", "资质", "许可证"), "qualification"),
        (("型号", "参数", "检测", "规格"), "product"),
        (("承诺", "有效期", "响应时限"), "commitment"),
        (("方案", "措施", "流程", "组织", "培训", "实施", "服务"), "scheme"),
    ]:
        if any(w in text for w in words):
            return kind
    return "unknown"


def policy(pid, rid):
    req = requirement(pid,rid)
    if not req:
        raise ValueError("要求不属于当前项目")
    with store.conn() as c:
        row = c.execute("SELECT data FROM requirement_support_policies "
                        "WHERE project_id=? AND req_id=?", (pid,rid)).fetchone()
    return json.loads(row["data"]) if row else {
        "type": infer(req["text"]), "reviewed": False, "entity": "", "field": "",
        "unit": "", "scope": "", "baseline": "", "attachment_required": False}


def set_policy(pid, rid, data, reviewer):
    requirement_row = requirement(pid,rid)
    if not requirement_row:
        raise ValueError("要求不属于当前项目")
    kind = data.get("type", "unknown")
    if kind not in TYPES:
        raise ValueError("不支持的要求类型")
    out = {k: str(data.get(k) or "").strip()[:200]
           for k in ("entity", "field", "unit", "scope", "baseline")}
    if out["baseline"]:
        date.fromisoformat(out["baseline"])
    out.update(type=kind, reviewed=True, reviewer=reviewer,
               attachment_required=bool(data.get("attachment_required")),
               form_checked=bool(data.get("form_checked")),
               reviewed_at=store.now())
    out["form_version"] = digest([
        (l["target_id"],l["snapshot"]) for l in list_links(pid,rid)
        if l["target_type"] == "attachment" and l["relation"] == "supports" and l["fresh"]
    ]) if out["form_checked"] else ""
    with store.conn() as c:
        c.execute("INSERT INTO requirement_support_policies VALUES(?,?,?) "
                  "ON CONFLICT(project_id,req_id) DO UPDATE SET data=excluded.data",
                  (pid,rid,json.dumps(out,ensure_ascii=False)))
        invalidate(c,pid,rid)
    return out


def invalidate(c, pid, rid):
    c.execute("UPDATE requirement_responses SET status='review',updated_at=? "
              "WHERE project_id=? AND req_id=? AND status IN ('confirmed','na')",
              (store.now(),pid,rid))


def target(pid, kind, tid):
    if kind not in TABLES:
        raise ValueError("支撑类型须为事实、证据或附件")
    with store.conn() as c:
        r = c.execute(f"SELECT * FROM {TABLES[kind]} WHERE id=? AND project_id=?",
                      (tid,pid)).fetchone()
    if not r:
        raise ValueError("支撑对象不属于当前项目或已删除")
    out = dict(r)
    if kind == "attachment":
        p = Path(out["path"])
        out["file_hash"] = store._file_hash(p) if p.is_file() else ""
        out["fact_owned"] = True
        if out.get("fact_id"):
            with store.conn() as c:
                fact = c.execute("SELECT * FROM project_facts WHERE id=? AND project_id=?",
                                 (out["fact_id"],pid)).fetchone()
            out["fact_owned"] = bool(fact)
            out["fact_snapshot"] = dict(fact) if fact else {}
    if kind in ("fact", "evidence", "attachment"):
        did = out.get("source_doc_id") or out.get("doc_id")
        doc = store.get_doc(did) if did and did != "tender" else None
        out["source_hash"] = digest(doc.get("content", "")) if doc else ""
        out["source_owned"] = not did or did == "tender" or bool(
            doc and doc.get("project_id") == pid)
        out["source_valid_until"] = doc.get("valid_until","") if doc else ""
        if did == "tender":
            out["source_hash"] = digest(store.get_project(pid).get("tender_text", ""))
    return out


def add_link(pid, rid, data, reviewer):
    if not requirement(pid,rid):
        raise ValueError("要求不属于当前项目")
    kind, tid = data.get("target_type"), str(data.get("target_id") or "")
    obj = target(pid,kind,tid)
    relation = data.get("relation", "candidate")
    if relation not in ("candidate", "supports", "conflicts"):
        raise ValueError("支持关系无效")
    if relation == "supports" and not reviewer:
        raise ValueError("支持关系必须有复核责任人")
    if kind == "evidence" and relation == "supports":
        with store.conn() as c:
            src = c.execute("SELECT data FROM generation_sources WHERE id=? AND project_id=?",
                            (obj.get("generation_source_id"),pid)).fetchone()
        meta = json.loads(src["data"]) if src else {}
        if (not meta.get("doc_id") or meta.get("support_state") != "candidate"
                or meta.get("is_example") or meta.get("semantic_only")
                or obj.get("relation") != "input"):
            raise ValueError("范例、语义候选或未知历史依据不能直接确认为支持")
    link = {"id": store.new_id(), "project_id": pid, "req_id": rid,
            "target_type": kind, "target_id": tid, "relation": relation,
            "method": "manual", "reviewer": reviewer, "reviewed_at": store.now(),
            "snapshot": digest(obj), "note": str(data.get("note") or "")[:600],
            "created_at": store.now()}
    with store.conn() as c:
        c.execute("DELETE FROM requirement_support_links WHERE project_id=? AND req_id=? "
                  "AND target_type=? AND target_id=?", (pid,rid,kind,tid))
        c.execute(f"INSERT INTO requirement_support_links({','.join(link)}) "
                  f"VALUES({','.join('?' for _ in link)})",tuple(link.values()))
        invalidate(c,pid,rid)
    return link


def list_links(pid, rid):
    if not requirement(pid,rid):
        raise ValueError("要求不属于当前项目")
    with store.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM requirement_support_links "
                                          "WHERE project_id=? AND req_id=? ORDER BY id",(pid,rid))]
    for row in rows:
        try:
            row["target"] = target(pid,row["target_type"],row["target_id"])
            row["fresh"] = digest(row["target"]) == row["snapshot"]
        except (ValueError, OSError):
            row["target"], row["fresh"] = {}, False
    return rows


def delete_link(pid, rid, lid):
    with store.conn() as c:
        n = c.execute("DELETE FROM requirement_support_links WHERE id=? AND project_id=? "
                      "AND req_id=?", (lid,pid,rid)).rowcount
        if n:
            invalidate(c,pid,rid)
    return bool(n)


def file_readable(row):
    """附件内容检查；缺失、空文件和损坏容器均失败。"""
    p = Path(row.get("path") or "")
    if not p.is_file() or not p.stat().st_size:
        return False
    try:
        if p.suffix.lower() in (".docx", ".xlsx", ".pptx", ".zip"):
            with zipfile.ZipFile(p) as z:
                expected = {".docx":"word/document.xml", ".xlsx":"xl/workbook.xml",
                            ".pptx":"ppt/presentation.xml"}.get(p.suffix.lower())
                return bool(z.namelist()) and z.testzip() is None and (
                    not expected or expected in z.namelist())
        if p.suffix.lower() == ".pdf":
            import fitz
            with fitz.open(p) as doc:
                return len(doc) > 0 and not doc.is_encrypted
        if p.suffix.lower() in (".txt", ".csv", ".md"):
            return bool(p.read_text(encoding="utf-8-sig").strip())
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
            from PIL import Image
            with Image.open(p) as image:
                image.verify()
            return True
        return False
    except Exception:
        return False


def evaluate(pid, req, response):
    pol = policy(pid,req["id"])
    links = list_links(pid,req["id"])
    gaps, usable = [], {"fact": [], "evidence": [], "attachment": []}
    if not pol.get("reviewed") or pol["type"] == "unknown":
        gaps.append("要求支撑类型尚未人工核对")
    for link in links:
        obj = link["target"]
        if link["relation"] == "conflicts":
            gaps.append("有关联材料冲突")
        if link["relation"] != "supports" or not link["fresh"] or not link["reviewer"]:
            continue
        kind = link["target_type"]
        until = obj.get("source_valid_until") or obj.get("valid_until")
        if until:
            try:
                if not pol.get("baseline"):
                    gaps.append("有效期缺少项目开标或评审基准日期")
                    continue
                if date.fromisoformat(until) < date.fromisoformat(pol["baseline"]):
                    gaps.append("关联证明已过期")
                    continue
            except ValueError:
                gaps.append("有效期格式不可核验")
                continue
        if kind == "fact":
            if obj.get("kind") == "example" or not obj.get("verification","").startswith("verified"):
                continue
            if not obj.get("source_owned"):
                continue
            if not pol.get("entity") or not pol.get("field"):
                continue
            if any(str(pol.get(k) or "") != str(obj.get(k) or "")
                   for k in ("entity","field","unit","scope")):
                continue
            until = obj.get("valid_until")
            if until:
                if not pol.get("baseline"):
                    gaps.append("有效期缺少项目开标或评审基准日期")
                    continue
                try:
                    if date.fromisoformat(until) < date.fromisoformat(pol["baseline"]):
                        gaps.append("事实证明已过期")
                        continue
                except ValueError:
                    gaps.append("有效期格式不可核验")
                    continue
            if pol["type"] == "commitment" and obj.get("kind") != "project_commitment":
                continue
        elif kind == "attachment":
            if not obj.get("source_owned") or not obj.get("fact_owned"):
                gaps.append("附件引用的事实或资料已删除或不属于本项目")
                continue
            if not file_readable(obj):
                gaps.append("附件缺失、为空或无法打开")
                continue
        elif kind == "evidence":
            sec = store.get_section(obj.get("section_id",""))
            if (not obj.get("source_owned") or obj.get("relation") != "input"
                    or obj.get("status") in ("historical","review")
                    or digest(sec.get("text","")) != obj.get("text_hash")):
                continue
        usable[kind].append(obj)
    fact_needed = pol["type"] in ("qualification","personnel","performance","product","commitment")
    att_needed = pol["type"] in ("qualification","personnel","performance","original") or pol.get("attachment_required")
    if fact_needed and not usable["fact"]:
        gaps.append("缺本条要求对应的已核验事实（主体、字段、单位及范围须一致）")
    if att_needed and not usable["attachment"]:
        gaps.append("缺本条要求对应的有效证明附件")
    if pol["type"] == "product" and not (usable["evidence"] or usable["attachment"]):
        gaps.append("缺产品规格或检测来源")
    if pol["type"] == "original":
        # 原件提交不能以正文承诺或一个链接替代字段、签章检查。
        version = digest([(l["target_id"],l["snapshot"]) for l in links
                          if l["target_type"] == "attachment"
                          and l["relation"] == "supports" and l["fresh"]])
        form_ok = (bool(usable["attachment"]) and pol.get("form_checked", False)
                   and pol.get("form_version") == version)
        if not form_ok:
            gaps.append("原件字段与签章尚未核验")
    linked = [l for l in links if l["relation"] == "supports"]
    if any(not l["fresh"] for l in linked):
        gaps.append("关联材料已变化，须重新核对关联")
    sec = store.get_section(response.get("section_id",""))
    stamp = digest({"requirement": {k: req.get(k) for k in ("id","text","kind","source_version","required","status")},
                    "response": {k: response.get(k) for k in ("section_id","anchor","evidence")},
                    "body": digest(sec.get("text","")), "policy": pol,
                    "links": links})
    return {"ok": not gaps, "gaps": gaps, "links": links, "policy": pol,
            "fingerprint": stamp,
            "attachments": [a["id"] for a in usable["attachment"]]}


def snapshot(pid):
    """缓存及导出令牌也绑定当前材料内容；读取失败向上抛出。"""
    with store.conn() as c:
        data = {table: [dict(r) for r in c.execute(
            f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", (pid,))]
                for table in ("requirement_support_links", "requirement_support_policies",
                              "project_facts", "evidence", "manual_confirmations")}
    data["targets"] = [target(pid,kind,obj["id"])
                       for kind,rows in (("fact",store.list_facts(pid)),
                                         ("attachment",store.list_attachments(pid)),
                                         ("evidence",store.list_evidence(pid)))
                       for obj in rows]
    return data


def suggest_links(pid, rid):
    """词面建议只登记候选，保留已有人工关系，不替用户确认。"""
    import re
    req = requirement(pid,rid)
    if not req:
        raise ValueError("要求不属于当前项目")
    existing = {(l["target_type"],l["target_id"]) for l in list_links(pid,rid)}
    def grams(text):
        flat = re.sub(r"\W","",str(text))
        return {flat[i:i+2] for i in range(len(flat)-1)}
    wanted = grams(req["text"])
    proposed = []
    for kind, objects in (("fact",store.list_facts(pid)),
                          ("attachment",store.list_attachments(pid)),
                          ("evidence",store.list_evidence(pid))):
        for obj in objects:
            if (kind,obj["id"]) in existing:
                continue
            if kind == "evidence" and obj.get("status") in ("historical","review","missing"):
                continue
            text = " ".join(str(obj.get(k) or "") for k in
                            ("entity","field","value","filename","note","title","snippet"))
            if len(wanted & grams(text)) / max(1,len(wanted)) < 0.3:
                continue
            link = add_link(pid,rid,{"target_type":kind,"target_id":obj["id"],
                                     "relation":"candidate","note":"词面相关候选，须逐条人工核对"},"")
            with store.conn() as c:
                c.execute("UPDATE requirement_support_links SET method='suggested' WHERE id=?",
                          (link["id"],))
            proposed.append(link["id"])
    return proposed
