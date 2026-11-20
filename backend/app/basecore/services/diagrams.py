"""mermaid 配图：LLM 出图代码 → 本地渲染 PNG → 插入章节正文。

链路：npm 装一份 mermaid（UMD 包，约 5MB），渲染时把它整段内联进 HTML，
用本机 Chrome/Playwright 无头截图。不装 puppeteer、不联网。
mermaid 没装或渲染失败时自动降级为"无配图"，不影响正文生成。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..config import DATA_DIR, ROOT

MERMAID_JS = ROOT / "tools" / "mermaid" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
DIAGRAM_DIR = DATA_DIR / "diagrams"
DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
DIAGRAM_DIR_REL = "diagrams"

CHROME_CANDIDATES = [
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
]

KINDS: list[dict] = [
    {"id": "flowchart", "label": "架构/流程图", "hint": "系统架构、业务流程、组网拓扑，用 flowchart TD"},
    {"id": "sequence", "label": "时序图", "hint": "系统间调用时序，用 sequenceDiagram"},
    {"id": "state", "label": "状态图", "hint": "业务状态流转，用 stateDiagram-v2"},
]

HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body{margin:0;background:#fff;font-family:"Microsoft YaHei","PingFang SC",sans-serif}
  #box{display:inline-block;padding:10px;background:#fff}
  .mermaid{display:flex;justify-content:center}
</style></head>
<body><div id="box"><pre class="mermaid">%s</pre></div>
<script>%s</script>
<script>
  window.__err = '';
  (async () => {
    const api = window.mermaid || (window.__esbuild_esm_mermaid_nm || {}).mermaid;
    if (!api) { window.__err = 'mermaid 未加载'; document.title = 'ERR'; return; }
    api.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
                     flowchart: { htmlLabels: true, curve: 'basis' } });
    try {
      await api.run({ nodes: document.querySelectorAll('.mermaid') });
      document.title = 'OK';
    } catch (e) { window.__err = String(e && e.message || e); document.title = 'ERR'; }
  })();
</script></body></html>"""


class DiagramError(RuntimeError):
    pass


def available() -> bool:
    return MERMAID_JS.exists()


def _chrome_path() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


def sanitize_code(code: str) -> str:
    """剥掉围栏与解释文字，只留 mermaid 代码本体。"""
    text = (code or "").strip()
    fence = re.search(r"```(?:mermaid)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def render(code: str, out_name: str, *, theme: str = "neutral", scale: int = 2) -> dict:
    """渲染 mermaid 代码为 PNG，写到 data/diagrams/out_name。"""
    if not available():
        raise DiagramError("未安装 mermaid（tools/mermaid），无法渲染配图")
    code = sanitize_code(code)
    if not code:
        raise DiagramError("图代码为空")
    out = DIAGRAM_DIR / out_name
    out.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    html = HTML % (code.replace("</script>", "<\\/script>"), MERMAID_JS.read_text(encoding="utf-8", errors="ignore"))
    with sync_playwright() as p:
        launch_kw: dict = {"args": ["--no-sandbox", "--disable-gpu"]}
        chrome = _chrome_path()
        if chrome:
            launch_kw["executable_path"] = chrome
        browser = p.chromium.launch(**launch_kw)
        try:
            page = browser.new_page(device_scale_factor=max(1, min(scale, 3)))
            page.set_content(html, wait_until="load")
            deadline = time.time() + 20
            while time.time() < deadline:
                if page.title() in ("OK", "ERR"):
                    break
                page.wait_for_timeout(200)
            if page.title() == "ERR":
                msg = page.evaluate("window.__err") or "渲染失败"
                raise DiagramError(f"mermaid 语法错误：{str(msg)[:160]}")
            if page.title() != "OK":
                raise DiagramError("渲染超时（20 秒）")
            el = page.locator("#box")
            el.screenshot(path=str(out))
        finally:
            browser.close()

    return {"file": f"{DIAGRAM_DIR_REL}/{out.name}", "path": str(out), "bytes": out.stat().st_size}
