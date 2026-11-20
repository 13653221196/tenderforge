"""企业线程、实际模型输入、原子绑定及逐要求支持关系的隔离回归。"""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# 无条件隔离；显式环境变量也不允许把本测试指向业务库。
_TEMP = tempfile.TemporaryDirectory(prefix="bidagent_context_")
os.environ["BIDAGENT_DATA"] = _TEMP.name
os.environ["BIDSTUDIO_DATA"] = _TEMP.name

from ..app import auth, chains, config, ctxpool, generation_context as gc, reqmatrix
from ..app import requirement_support as rs, store
from langchain_core.messages import AIMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult


class CapturingModel(BaseChatModel):
    seen: list = []

    @property
    def _llm_type(self):
        return "test-model"

    def _generate(self, messages, **kwargs):
        self.seen.append([{"role": m.type, "content": m.content} for m in messages])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="我方将按实施方案落实培训流程，设置验收记录并由项目经理逐项核对。"))])


class ContextEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def setUp(self):
        self.org = store.use_org("default")
        self.org.__enter__()
        self.pid = store.create_project("隔离验收")["id"]
        store.set_sections(self.pid,[{"title": "实施培训方案"}])
        self.sid = store.list_sections(self.pid)[0]["id"]

    def tearDown(self):
        store.delete_project(self.pid)
        self.org.__exit__(None,None,None)

    def generate(self, text="培训流程与实施方案由项目经理核对，逐项记录验收结果。"):
        store.add_doc("项目事实资料",text,project_id=self.pid)
        ctx = gc.GenerationContext(self.pid,self.sid,store.get_section(self.sid)["ver"])
        variables = {"name": "隔离验收", "title": "实施培训方案", "points": "培训流程",
                     "tender": "", "materials": text, "kb": "", "sample": ""}
        model = CapturingModel()
        with patch.object(chains,"build_model",return_value=model):
            out = chains.run("section",variables,pid=self.pid,generation=ctx)
        return ctx,out,model

    def requirement(self, text="我方须落实实施培训方案及流程"):
        store.add_requirements(self.pid,[{"text": text,"kind": "hard","source": "manual"}])
        return store.list_requirements(self.pid)[0]["id"]

    def test_thread_nested_identity_and_exception(self):
        def run_org(oid):
            tok = auth.set_current({"id": oid+"-user","org_id": oid})
            try:
                with store.use_org(oid):
                    p = store.create_project(oid)["id"]
                    with ThreadPoolExecutor(2) as nested:
                        def inner():
                            ctxpool.require_business_context(p)
                            return auth.org_id(),str(store.db_path()),store.get_project(p)["name"]
                        return ctxpool.submit(nested,
                            lambda: ctxpool.submit_business(nested,inner,project_id=p).result()).result()
            finally:
                auth.reset_current(tok)
        with ThreadPoolExecutor(3) as pool:
            rows = list(pool.map(run_org,("default","test-a","test-b")))
        self.assertEqual([r[0] for r in rows],["default","test-a","test-b"])
        self.assertEqual(len({r[1] for r in rows}),3)
        self.assertEqual([r[2] for r in rows],["default","test-a","test-b"])
        with ThreadPoolExecutor(1) as pool:
            with self.assertRaises(ValueError):
                ctxpool.submit_business(pool,lambda: (_ for _ in ()).throw(ValueError())).result()
            self.assertEqual(ctxpool.submit_business(pool,auth.org_id).result(),"default")

    def test_missing_and_mismatched_business_context(self):
        token = store._db_ctx.set(None)
        try:
            with self.assertRaises(RuntimeError):
                ctxpool.require_business_context(self.pid)
        finally:
            store._db_ctx.reset(token)
        token = auth.set_current({"id":"wrong-user","org_id":"another"})
        try:
            with self.assertRaises(RuntimeError):
                ctxpool.require_business_context(self.pid)
        finally:
            auth.reset_current(token)

    def test_actual_messages_redaction_and_commit(self):
        ctx,out,model = self.generate("培训联络邮箱 test@example.com，手机13812345678；按实施流程验收。")
        row = gc.list_runs(self.pid,self.sid)[0]
        self.assertEqual(row["data"]["messages"],model.seen[0])
        self.assertNotIn("13812345678",str(row["data"]))
        self.assertNotIn("test@example.com",str(row["data"]))
        self.assertEqual(row["status"],"succeeded")
        self.assertTrue(store.commit_generation(self.pid,self.sid,out,0,ctx.id)["ok"])
        sec = store.get_section(self.sid)
        self.assertEqual(sec["generation_run_id"],ctx.id)
        self.assertTrue(all(e["text_hash"] == gc.digest(out) for e in store.list_evidence(self.pid)))

    def test_conflict_cancel_empty_and_atomic_rollback(self):
        ctx,out,_ = self.generate()
        store.update_section(self.sid,"人工稿",0,self.pid)
        self.assertEqual(store.commit_generation(self.pid,self.sid,out,0,ctx.id)["error"],"conflict")
        self.assertEqual(store.get_section(self.sid)["text"],"人工稿")
        ctx2,out,_ = self.generate()
        self.assertEqual(store.commit_generation(self.pid,self.sid,out,1,ctx2.id,
                                               abort_check=lambda: True)["error"],"cancelled")
        self.assertFalse(store.commit_generation(self.pid,self.sid,"",1,ctx2.id)["ok"])
        ctx3,out,_ = self.generate()
        with store.conn() as c:
            c.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON evidence "
                      "BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        try:
            with self.assertRaises(Exception):
                store.commit_generation(self.pid,self.sid,out,1,ctx3.id)
            self.assertEqual(store.get_section(self.sid)["text"],"人工稿")
            self.assertEqual(store.get_section(self.sid)["ver"],1)
            self.assertEqual(gc.list_runs(self.pid,self.sid)[0]["status"],"succeeded")
        finally:
            with store.conn() as c:
                c.execute("DROP TRIGGER fail_evidence")

    def test_history_restore_without_retrieval(self):
        ctx,out,_ = self.generate()
        store.commit_generation(self.pid,self.sid,out,0,ctx.id)
        store.snapshot_sections(self.pid,"history","生成后",[self.sid])
        store.update_section(self.sid,"人工改稿",1,self.pid)
        self.assertEqual(store.get_section(self.sid)["generation_run_id"],"")
        store.restore_batch(self.pid,"history")
        self.assertEqual(store.get_section(self.sid)["generation_run_id"],ctx.id)
        self.assertEqual(store.get_section(self.sid)["text"],out)

    def test_no_unrelated_fact_and_confirmation_freshness(self):
        rid = self.requirement()
        text = "我方将按实施培训方案落实流程，逐项建立验收记录并由项目经理负责核对"
        store.update_section(self.sid,text,0,self.pid)
        store.add_fact(self.pid,"公司","营业执照","已登记",verification="verified:owner")
        before = reqmatrix.build(self.pid)["items"][0]
        self.assertFalse(before["evidence_ok"])
        rs.set_policy(self.pid,rid,{"type":"scheme"},"reviewer")
        ready = reqmatrix.build(self.pid)["items"][0]
        self.assertTrue(ready["evidence_ok"],ready)
        self.assertTrue(reqmatrix.confirm(self.pid,rid,"reviewer")["ok"])
        self.assertEqual(reqmatrix.build(self.pid)["items"][0]["status"],"confirmed")
        store.add_doc("无关材料","无关",project_id=self.pid)
        self.assertEqual(reqmatrix.build(self.pid)["items"][0]["status"],"confirmed")
        store.update_section(self.sid,text+"，补充培训记录",1,self.pid)
        self.assertEqual(reqmatrix.build(self.pid)["items"][0]["status"],"review")

    def test_fact_attachment_ownership_expiry_and_per_requirement_links(self):
        rid = self.requirement("须提供人员社保证明")
        fid = store.add_fact(self.pid,"张三","社保","12",unit="月",valid_until="2026-12-31",
                             verification="verified:reviewer")["id"]
        p = Path(_TEMP.name)/"proof.txt"
        p.write_text("张三社保证明",encoding="utf-8")
        aid = store.add_attachment(self.pid,"proof.txt",str(p),fact_id=fid)["id"]
        rs.set_policy(self.pid,rid,{"type":"personnel","entity":"张三","field":"社保",
                                   "unit":"月","baseline":"2026-11-14"},"reviewer")
        response = {"section_id":self.sid,"anchor":"","evidence":""}
        self.assertFalse(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])
        for kind,tid in (("fact",fid),("attachment",aid)):
            rs.add_link(self.pid,rid,{"target_type":kind,"target_id":tid,"relation":"supports"},"reviewer")
        self.assertTrue(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])
        other = store.create_project("另一项目")["id"]
        try:
            other_fid = store.add_fact(other,"张三","社保","12",unit="月")["id"]
            with self.assertRaises(ValueError):
                rs.add_link(self.pid,rid,{"target_type":"fact","target_id":other_fid},"reviewer")
        finally:
            store.delete_project(other)
        rs.set_policy(self.pid,rid,{"type":"personnel","entity":"李四","field":"社保",
                                   "unit":"年","baseline":"2027-01-01"},"reviewer")
        self.assertFalse(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])
        p.write_text("被替换的文件",encoding="utf-8")
        self.assertFalse(all(l["fresh"] for l in rs.list_links(self.pid,rid)))
        p.unlink()
        self.assertFalse(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])

    def test_na_requires_reason_and_keeps_version(self):
        rid = self.requirement("本采购包须提供施工人员")
        self.assertFalse(reqmatrix.set_na(self.pid,rid,"","reviewer")["ok"])
        self.assertTrue(reqmatrix.set_na(self.pid,rid,"澄清确认本包不含施工","reviewer")["ok"])
        self.assertEqual(reqmatrix.build(self.pid)["items"][0]["status"],"na")
        store.update_requirement(self.pid,rid,{"text":"新版要求须提供施工人员名单"})
        self.assertEqual(reqmatrix.build(self.pid)["items"][0]["status"],"review")

    def test_original_check_binds_file_version_and_cache(self):
        from ..app import delivery, health
        rid = self.requirement("须提交盖章原件表单")
        p = Path(_TEMP.name)/"form.txt"
        p.write_text("原件字段及签章核验记录",encoding="utf-8")
        aid = store.add_attachment(self.pid,"form.txt",str(p))["id"]
        rs.add_link(self.pid,rid,{"target_type":"attachment","target_id":aid,
                                 "relation":"supports"},"reviewer")
        rs.set_policy(self.pid,rid,{"type":"original","form_checked":True},"reviewer")
        response = {"section_id":self.sid}
        self.assertTrue(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])
        old = delivery.content_fingerprint(self.pid),health.content_key(self.pid)
        p.write_text("替换后的原件",encoding="utf-8")
        self.assertNotEqual(old,(delivery.content_fingerprint(self.pid),health.content_key(self.pid)))
        rs.add_link(self.pid,rid,{"target_type":"attachment","target_id":aid,
                                 "relation":"supports"},"reviewer")
        self.assertIn("原件字段与签章尚未核验",
                      rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["gaps"])
        rs.set_policy(self.pid,rid,{"type":"original","form_checked":True},"reviewer")
        self.assertTrue(rs.evaluate(self.pid,rs.requirement(self.pid,rid),response)["ok"])
        p.unlink()

    def test_outline_rebuild_keeps_matching_history(self):
        ctx,out,_ = self.generate()
        store.commit_generation(self.pid,self.sid,out,0,ctx.id)
        for apply in (lambda:store.set_sections(self.pid,[{"title":"实施培训方案"}]),
                      lambda:store.apply_outline_titles(self.pid,["实施培训方案"])):
            apply()
            sid = store.list_sections(self.pid)[0]["id"]
            self.assertEqual(store.get_section(sid)["generation_run_id"],ctx.id)
            self.assertEqual(gc.list_runs(self.pid,sid)[0]["id"],ctx.id)
        store.set_sections(self.pid,[{"title":"实施培训方案"}],replace_text=True)
        self.assertEqual(store.list_sections(self.pid)[0]["generation_run_id"],"")

    def test_truncation_candidates_and_failed_model(self):
        text = "原文技术参数逐项验收核对并登记实施流程与项目材料。" * 5
        store.add_doc("参数原件",text,project_id=self.pid)
        ctx = gc.GenerationContext(self.pid,self.sid,0)
        ctx.capture("section",{"materials":text,"sample":text},
                    {"materials":text[:40],"sample":""},[],"test")
        mapped = [s for s in ctx.sources if s.get("doc_id")]
        self.assertTrue(mapped)
        self.assertTrue(all(s["truncated"] for s in mapped))
        self.assertTrue(all(s["input_key"] == "materials" for s in mapped))
        ctx.finish(status="failed")
        self.assertFalse(store.commit_generation(self.pid,self.sid,"结果",0,ctx.id)["ok"])
        self.assertEqual(store.get_section(self.sid)["text"],"")
        example = gc.GenerationContext(self.pid,self.sid,0)
        example.capture("section",{"sample":text},{"sample":text},[],"test")
        example.finish("结果")
        store.commit_generation(self.pid,self.sid,"结果",0,example.id)
        rid = self.requirement()
        for e in store.list_evidence(self.pid):
            with self.assertRaises(ValueError):
                rs.add_link(self.pid,rid,{"target_type":"evidence","target_id":e["id"],
                                         "relation":"supports"},"reviewer")

    def test_attachment_references_and_container_validation(self):
        other = store.create_project("其他资料归属")["id"]
        try:
            did = store.add_doc("外项目证书","正文",project_id=other)["id"]
            p = Path(_TEMP.name)/"foreign.txt"
            p.write_text("证明",encoding="utf-8")
            aid = store.add_attachment(self.pid,"foreign.txt",str(p),doc_id=did)["id"]
            rid = self.requirement("须提交原件表单")
            rs.add_link(self.pid,rid,{"target_type":"attachment","target_id":aid,
                                     "relation":"supports"},"reviewer")
            rs.set_policy(self.pid,rid,{"type":"original","form_checked":True},"reviewer")
            self.assertFalse(rs.evaluate(self.pid,rs.requirement(self.pid,rid),{})["ok"])
            import zipfile
            bad = Path(_TEMP.name)/"bad.docx"
            with zipfile.ZipFile(bad,"w") as z:
                z.writestr("random.txt","不是Word原件")
            self.assertFalse(rs.file_readable({"path":str(bad)}))
            unknown = Path(_TEMP.name)/"unknown.bin"
            unknown.write_bytes(b"x")
            self.assertFalse(rs.file_readable({"path":str(unknown)}))
            for file in (p,bad,unknown):
                file.unlink()
        finally:
            store.delete_project(other)

    def test_migration_idempotent_and_shared_id_cache_isolation(self):
        from ..app import agent
        for oid in ("default","test-a","test-b"):
            with store.use_org(oid):
                store._apply_schema(store.db_path())
                store._apply_schema(store.db_path())
                with store.conn() as c:
                    cols = {r["name"] for r in c.execute("PRAGMA table_info(sections)")}
                self.assertIn("generation_run_id",cols)
                agent.LAST_AUTO[agent._cache_key("same-id")] = {"org":oid}
        for oid in ("default","test-a","test-b"):
            with store.use_org(oid):
                self.assertEqual(agent.last_auto("same-id").get("org"),oid)

    def test_selfcheck_selected_attempt_and_partial_preservation(self):
        from ..app import agent
        ctx,out,_ = self.generate()
        state = [ctx]
        results = [{"ok":False,"flags":{"a":False,"b":True}},
                   {"ok":False,"flags":{"a":True,"c":False}}]
        texts = ["第一轮采用稿，按培训实施方案完善流程",
                 "第二轮不采用稿，引入新的问题"]
        def fake_llm(pid,task,variables,**kwargs):
            attempt = kwargs["generation"]
            attempt.capture(task,variables,variables,[],{"id":"test"})
            text = texts.pop(0)
            attempt.finish(text)
            return text
        with patch.object(agent,"_llm",side_effect=fake_llm), \
             patch.object(agent,"_selfcheck",side_effect=results):
            selected,_,rounds,_ = agent._fix_loop(
                self.pid,store.get_project(self.pid),store.get_section(self.sid),
                out,{"ok":False,"flags":{"a":False,"b":False}},state)
        self.assertEqual(rounds,2)
        self.assertTrue(selected.startswith("第一轮采用"))
        self.assertNotEqual(state[0].id,ctx.id)
        self.assertTrue(store.commit_generation(self.pid,self.sid,selected,0,state[0].id)["ok"])
        self.assertEqual(store.get_section(self.sid)["generation_run_id"],state[0].id)
        runs = gc.list_runs(self.pid,self.sid)
        self.assertTrue(all(r["quality"] for r in runs if r["task"] == "rewrite" and r["status"] != "committed"))
        # 段落优化对旧绑定保守标待复核，不能抹掉全章历史。
        base,out,_ = self.generate()
        store.commit_generation(self.pid,self.sid,out,1,base.id)
        old_ids = {e["id"] for e in store.list_evidence(self.pid) if e["status"] == "candidate"}
        partial,out,_ = self.generate()
        store.commit_generation(self.pid,self.sid,out+"补充段落",2,partial.id,partial=True)
        current = store.list_evidence(self.pid)
        self.assertTrue(old_ids)
        self.assertTrue(all(e["status"] == "review" for e in current if e["id"] in old_ids))
        self.assertTrue(any(e["generation_run_id"] == partial.id and e["status"] == "candidate"
                            for e in current))

    def test_old_schema_copies_and_legacy_confirmation(self):
        for name in ("default-copy","a-copy","b-copy"):
            path = Path(_TEMP.name)/(name+".db")
            store.init_db(path)
            with store.conn(path) as c:
                c.execute("INSERT INTO projects(id,name,created_at) VALUES('old','旧项目',?)",(store.now(),))
                c.execute("INSERT INTO requirement_responses(id,project_id,req_id,status,reviewer,updated_at) "
                          "VALUES('old-response','old','old-req','confirmed','原复核人',?)",(store.now(),))
                for table in ("generation_runs","generation_sources",
                              "requirement_support_links","requirement_support_policies"):
                    c.execute(f"DROP TABLE {table}")
                for table,cols in {"sections":["generation_run_id"],
                                   "requirement_responses":["fingerprint"],
                                   "evidence":["generation_run_id","generation_source_id","section_ver",
                                               "text_hash","relation"]}.items():
                    for col in cols:
                        c.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            store._apply_schema(path)
            store._apply_schema(path)
            with store.conn(path) as c:
                row = dict(c.execute("SELECT * FROM requirement_responses WHERE id='old-response'").fetchone())
                self.assertEqual(row["status"],"review")
                self.assertEqual(row["reviewer"],"原复核人")
                self.assertEqual(row["fingerprint"],"")

    def test_candidate_batch_and_api_permissions(self):
        from ..app import main
        from fastapi.testclient import TestClient
        rid = self.requirement("项目经理须核对培训流程")
        fid = store.add_fact(self.pid,"项目经理","培训流程","逐条核对",
                             verification="verified:owner")["id"]
        self.assertTrue(rs.suggest_links(self.pid,rid))
        self.assertTrue(all(l["relation"] == "candidate" for l in rs.list_links(self.pid,rid)))
        rs.add_link(self.pid,rid,{"target_type":"fact","target_id":fid,"relation":"conflicts"},"owner")
        self.assertFalse(rs.suggest_links(self.pid,rid))
        self.assertEqual(rs.list_links(self.pid,rid)[0]["relation"],"conflicts")
        user = auth.create_user("default","test-reader-"+store.new_id(),"Reader-Test-2026")
        store.set_project_member(self.pid,user["id"],"reader")
        with TestClient(main.app) as client:
            self.assertEqual(client.post("/api/auth/login",json={
                "username":user["username"],"password":"Reader-Test-2026"}).status_code,200)
            base = f"/api/projects/{self.pid}/reqmatrix/{rid}"
            self.assertEqual(client.get(base+"/support").status_code,200)
            self.assertEqual(client.put(base+"/support-policy",json={"type":"scheme"}).status_code,403)
            self.assertEqual(client.post(base+"/support",json={"target_type":"fact","target_id":fid}).status_code,403)
            self.assertEqual(client.delete(base+"/support/"+rs.list_links(self.pid,rid)[0]["id"]).status_code,403)
            self.assertEqual(client.get(f"/api/projects/{self.pid}/sections/{self.sid}/generation-runs").status_code,200)
            store.set_project_member(self.pid,user["id"],"reviewer")
            response = client.put(base+"/support-policy",json={"type":"scheme","reviewer":"伪造身份"})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["policy"]["reviewer"],user["id"])
            other_username = "test-other-"+store.new_id()
            auth.create_org("跨企业验收",other_username,"Other-Test-2026")
            self.assertEqual(client.post("/api/auth/login",json={
                "username":other_username,"password":"Other-Test-2026"}).status_code,200)
            self.assertEqual(client.get(base+"/support").status_code,404)
            self.assertEqual(client.get(f"/api/projects/{self.pid}/sections/{self.sid}/generation-runs").status_code,404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
