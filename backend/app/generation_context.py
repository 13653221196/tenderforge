"""实际生成输入快照。来源只从调用前材料定位，不在生成后补检索。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import guard, store


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS generation_runs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_id TEXT NOT NULL,
      parent_id TEXT DEFAULT '', base_ver INTEGER NOT NULL, task TEXT NOT NULL,
      status TEXT NOT NULL, data TEXT NOT NULL, output_hash TEXT DEFAULT '',
      text_hash TEXT DEFAULT '', committed_ver INTEGER DEFAULT -1,
      quality TEXT DEFAULT '{}', created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_generation_section
      ON generation_runs(project_id,section_id,created_at);
    CREATE TABLE IF NOT EXISTS generation_sources (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
      data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_generation_sources ON generation_sources(run_id);
    """)
    for table, columns in {
        "sections": {"generation_run_id": "TEXT DEFAULT ''"},
        "evidence": {"generation_run_id": "TEXT DEFAULT ''",
                     "generation_source_id": "TEXT DEFAULT ''",
                     "section_ver": "INTEGER DEFAULT -1",
                     "text_hash": "TEXT DEFAULT ''",
                     "relation": "TEXT DEFAULT 'legacy'"},
        "requirement_responses": {"fingerprint": "TEXT DEFAULT ''"},
    }.items():
        known = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, ddl in columns.items():
            if col not in known:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    c.execute("UPDATE requirement_responses SET status='review' "
              "WHERE status IN ('confirmed','na') AND fingerprint=''")


@dataclass
class GenerationContext:
    pid: str
    sid: str
    base_ver: int
    parent_id: str = ""
    id: str = field(default_factory=store.new_id)
    sources: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    prepared: bool = False
    hints: list = field(default_factory=list)

    def capture(self, task, original, retained, messages, model):
        """messages 是 prompt 节点实际交给模型的 PromptValue。"""
        from . import auth, rag
        self.data = {
            "org_id": auth.org_id(), "user_id": auth.current().get("id",""),
            "task": task, "model": model, "attempt_id": self.id,
            "project_id": self.pid, "section_id": self.sid,
            "task_id": (store.active_job(self.pid) or {}).get("id",""),
            "prompt_hash": digest(messages),
            "messages": messages, "variables": retained,
            "tender_hash": digest(store.get_project(self.pid).get("tender_text", "")),
            "requirements_hash": digest(store.list_requirements(self.pid)),
            "budgets": {k: {"original_chars": len(str(v)),
                             "retained_chars": len(str(retained.get(k, ""))),
                             "truncated": len(str(retained.get(k, ""))) < len(str(v))}
                        for k, v in original.items()},
        }
        # 原文坐标始终依据原始资料；脱敏输入坐标另记，无法定位则为 null。
        candidates = [dict(d) for d in store.usable_docs(self.pid)]
        candidates.append({"id": "tender", "title": "招标原文",
                           "content": store.get_project(self.pid).get("tender_text", ""),
                           "kind": "tender"})
        for d in candidates:
            source_hash = digest(d.get("content", ""))
            for sp in rag.split_spans(d.get("content") or ""):
                raw = sp["text"]
                safe = str(guard.guard_variables({"source": raw}).get("source", ""))
                if len(safe.strip()) < 8:
                    continue
                for key, val in retained.items():
                    block = str(val)
                    # 完整或实际保留的连续前缀；不使用语义相似证明输入关系。
                    pos, used = block.find(safe), safe
                    if pos < 0:
                        pos = block.find(safe[:32])
                        if pos < 0:
                            continue
                        n = 0
                        while n < len(safe) and pos + n < len(block) and safe[n] == block[pos+n]:
                            n += 1
                        used = safe[:n]
                    if len(used) < 8:
                        continue
                    self.sources.append({
                        "doc_id": d["id"], "title": d["title"], "scope": d.get("scope", "招标文件"),
                        "source_hash": source_hash,
                        "start": sp["start"], "end": sp["end"], "page": sp["page"],
                        "original_coordinates": True, "input_key": key,
                        "input_start": pos, "input_end": pos+len(used),
                        "snippet": used, "truncated": len(used) < len(safe),
                        "is_example": d.get("kind") == "sample" or key == "sample",
                        "semantic_only": ("语义召回候选" in block[max(0,pos-160):pos]
                                          or (key in ("kb","evidence") and any(
                                              h.get("semantic_only") and h.get("doc_id") == d["id"]
                                              and h.get("start") == sp["start"] for h in self.hints))),
                        "support_state": "candidate",
                    })
        # 未能精确映射的输入仍可回查，明确坐标未知，不冒充原文证据。
        for key, val in retained.items():
            if key in ("materials", "kb", "tender", "sample", "evidence", "text") and str(val):
                self.sources.append({"title": key, "scope": "输入块", "doc_id": "",
                                     "input_key": key, "snippet": str(val),
                                     "start": None, "end": None, "page": 0,
                                     "is_example": key in ("sample", "text"),
                                     "support_state": "unmapped"})
        with store.conn() as c:
            c.execute("INSERT INTO generation_runs "
                      "(id,project_id,section_id,parent_id,base_ver,task,status,data,created_at)"
                      " VALUES(?,?,?,?,?,?,'prepared',?,?)",
                      (self.id,self.pid,self.sid,self.parent_id,self.base_ver,task,
                       json.dumps(self.data,ensure_ascii=False),store.now()))
            for source in self.sources:
                source["id"] = store.new_id()
                c.execute("INSERT INTO generation_sources VALUES(?,?,?,?)",
                          (source["id"],self.pid,self.id,json.dumps(source,ensure_ascii=False)))
        self.prepared = True

    def finish(self, output="", status="succeeded"):
        if self.prepared:
            with store.conn() as c:
                c.execute("UPDATE generation_runs SET status=?,output_hash=? WHERE id=?",
                          (status,digest(str(output)),self.id))

    def record_quality(self, result):
        if self.prepared:
            with store.conn() as c:
                c.execute("UPDATE generation_runs SET quality=? WHERE id=? AND project_id=?",
                          (json.dumps(result,ensure_ascii=False),self.id,self.pid))


def list_runs(pid, sid):
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM generation_runs WHERE project_id=? AND (section_id=? OR id IN "
            "(SELECT generation_run_id FROM sections WHERE id=? AND project_id=?)) "
            "ORDER BY created_at DESC,rowid DESC", (pid,sid,sid,pid))]
        for row in rows:
            row["data"] = json.loads(row["data"])
            row["quality"] = json.loads(row["quality"])
            row["sources"] = [json.loads(r["data"]) for r in c.execute(
                "SELECT data FROM generation_sources WHERE run_id=? AND project_id=?",
                (row["id"],pid))]
    return rows
