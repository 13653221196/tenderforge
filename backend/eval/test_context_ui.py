"""隔离服务及 Playwright 交互验收，不使用业务数据或真实模型。

通过 webapp-testing/scripts/with_server.py 启动本模块 --serve，再运行 --test。
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile


def serve():
    with tempfile.TemporaryDirectory(prefix="bidagent_context_ui_") as data:
        os.environ["BIDAGENT_DATA"] = data
        os.environ["BIDSTUDIO_DATA"] = data
        os.environ["BIDAGENT_INIT_ADMIN_PASSWORD"] = "Context-UI-Test-2026"
        from ..app import main, store, generation_context as gc
        with store.use_org("default"):
            pid = store.create_project("上下文关联界面验收")["id"]
            store.set_sections(pid,[{"title":"实施培训方案"},{"title":"投标函"}])
            sid = store.list_sections(pid)[0]["id"]
            text = "我方将按实施培训方案落实流程，逐项建立验收记录并由项目经理负责核对"
            store.add_doc("本项目培训资料",text,project_id=pid)
            ctx = gc.GenerationContext(pid,sid,0)
            ctx.capture("section",{"materials":text},{"materials":text},
                        [{"role":"human","content":text}],{"id":"ui-fixture"})
            ctx.finish(text)
            assert store.commit_generation(pid,sid,text,0,ctx.id)["ok"]
            store.add_requirements(pid,[{"text":"我方须落实实施培训方案及流程",
                                         "kind":"hard","source":"manual"}])
            store.add_fact(pid,"项目经理","培训职责","核对验收记录",
                           verification="verified:owner")
        import uvicorn
        # helper 托管子进程输出，避免 Windows 管道被逐请求访问日志写满。
        uvicorn.run(main.app,host="127.0.0.1",port=18765,
                    log_level="warning",access_log=False)


def test():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,channel=os.environ.get("BIDAGENT_TEST_BROWSER") or None)
        page = browser.new_page()
        errors = []
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.goto("http://127.0.0.1:18765")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#username")).to_be_visible()
        page.locator("#username").fill("admin")
        page.locator("#password").fill("Context-UI-Test-2026")
        page.locator("#submit").click()
        page.wait_for_url("http://127.0.0.1:18765/")
        page.wait_for_load_state("networkidle")
        # 先检查已渲染 DOM，再按实际按钮操作。
        if page.locator("#privacyModal").is_visible():
            page.locator('[data-act="privacyAck"][data-allow="0"]').click()
            expect(page.locator("#privacyModal")).to_be_hidden()
        assert "上下文关联界面验收" in page.content()
        pid = page.get_by_text("上下文关联界面验收",exact=True).first.locator("..").get_attribute("data-pid")
        page.evaluate("(pid) => selectProject(pid)",pid)
        page.wait_for_load_state("networkidle")
        page.evaluate("switchTab('requirements')")
        page.wait_for_load_state("networkidle")
        expect(page.locator('[data-act="reqSupport"]')).to_be_visible()
        rid = page.locator('[data-act="reqSupport"]').get_attribute("data-rid")
        page.locator('[data-act="reqSupport"]').click()
        page.wait_for_load_state("networkidle")
        card = page.locator("[data-support-rid]")
        expect(card).to_be_visible()
        card.locator('[data-field="type"]').select_option("scheme")
        card.locator('[data-act="reqSupportPolicy"]').click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#reqMatrixPanel")).to_contain_text("证据齐")
        # 故意延迟刷新响应，模拟用户在请求期间继续选择材料和填写理由。
        held = []
        support_url = f"**/reqmatrix/{rid}/support"
        def hold_support(route):
            held.append((route,route.fetch()))
            page.evaluate("window.uiSupportHeld = true")
        page.route(support_url,hold_support)
        with page.expect_request(lambda req:req.method == "GET" and req.url.endswith(f"/{rid}/support")):
            page.locator('[data-act="reqSupport"]').click()
        page.wait_for_function("window.uiSupportHeld === true")
        assert len(held) == 1
        card = page.locator("[data-support-rid]")
        target = card.locator('[data-field="target"] option').nth(1).get_attribute("value")
        card.locator('[data-field="target"]').select_option(target)
        expect(card.locator('[data-field="target"]')).to_have_value(target)
        card.locator('[data-field="relation"]').select_option("candidate")
        card.locator('[data-field="note"]').fill("界面验收候选关联")
        note_node = card.locator('[data-field="note"]').element_handle()
        held[0][0].fulfill(response=held[0][1])
        page.unroute(support_url)
        page.wait_for_load_state("networkidle")
        assert note_node.evaluate("(node) => node.isConnected")
        expect(card.locator('[data-field="note"]')).to_have_value("界面验收候选关联")
        expect(card.locator('[data-field="note"]')).to_be_focused()
        expect(card.locator('[data-field="target"]')).to_have_value(target)
        page.evaluate("loadReqMatrix()")
        expect(card.locator('[data-field="note"]')).to_have_value("界面验收候选关联")
        expect(card.locator('[data-field="note"]')).to_be_focused()
        expect(card.locator('[data-field="target"]')).to_have_value(target)
        with page.expect_response(lambda r:r.url.endswith("/support") and r.request.method == "POST") as linked:
            card.locator('[data-act="reqSupportAdd"]').click()
        assert linked.value.status == 200,linked.value.text()
        page.wait_for_load_state("networkidle")
        expect(page.locator("[data-support-rid]")).to_contain_text("界面验收候选关联")
        with page.expect_response(lambda r:r.request.method == "DELETE" and "/support/" in r.url) as deleted:
            page.locator('[data-act="reqSupportDelete"]').click()
        assert deleted.value.status == 200,deleted.value.text()
        page.wait_for_load_state("networkidle")
        expect(page.locator('[data-act="reqSupportDelete"]')).to_have_count(0)
        with page.expect_response(lambda r:r.url.endswith("/confirm") and r.request.method == "POST") as confirmed:
            page.locator('[data-act="reqMatrixConfirm"]').click()
        assert confirmed.value.status == 200,confirmed.value.text()
        page.wait_for_load_state("networkidle")
        expect(page.locator('[data-act="reqMatrixConfirm"]')).to_have_count(0)
        page.evaluate("switchTab('write')")
        expect(page.locator('[data-act="showEvidence"]')).to_be_visible()
        page.locator('[data-act="showEvidence"]').click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#evidenceModal")).to_be_visible()
        expect(page.locator("#evidenceBody")).to_contain_text("当前正文")
        expect(page.locator("#evidenceBody")).to_contain_text("ui-fixture")
        page.locator("#evidenceBody details.card > summary").click()
        page.get_by_text("本次实际输入",exact=True).click()
        expect(page.locator("#evidenceBody")).to_contain_text('"role": "human"')
        page.locator('[data-act="closeEvidence"]').click()
        # 保存失败时切页必须被阻断，本地输入不能消失。
        page.route("**/api/projects/*/section/*",lambda route: route.fulfill(
            status=503,json={"detail":"隔离验收：保存暂不可用"}))
        editor = page.locator('[data-role="secText"]')
        editor.fill(editor.input_value()+"，界面新增记录")
        page.evaluate("switchTab('check')")
        expect(page.locator("#tab-write")).to_have_class("panel on")
        expect(editor).to_have_value(re.compile("界面新增记录"))
        export_requests, downloads = [], []
        page.on("request",lambda req: export_requests.append(req.url) if "/export" in req.url else None)
        page.on("download",lambda item: downloads.append(item))
        page.evaluate("ACT.exportWord()")
        assert not export_requests and not downloads
        expect(page.locator("#exportModal")).to_be_hidden()
        page.unroute("**/api/projects/*/section/*")
        page.locator('[data-act="saveSec"]').click()
        expect(page.locator("#edState")).to_contain_text("已保存")
        page.evaluate("switchTab('requirements')")
        expect(page.locator("#reqMatrixPanel")).to_contain_text("待重新复核")
        assert not errors,errors
        page.screenshot(path=os.path.join(tempfile.gettempdir(),"bidagent-context-ui.png"),
                        full_page=True)
        browser.close()
        print("PASS: 登录、类型核对、关联草稿保留及新增/删除、确认、历史输入、保存失败阻断切页和下载、确认失效；无页面异常")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve",action="store_true")
    parser.add_argument("--test",action="store_true")
    args = parser.parse_args()
    serve() if args.serve else test()
