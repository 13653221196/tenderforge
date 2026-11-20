"""C01 暗标检测：标题 / 正文 / 表格 / 页眉页脚 / 文档属性 / 嵌入对象全覆盖。

边界写清楚：
- 没有登记项目规则（企业全称、简称、人员、联系方式、格式要求）时，
  只报告「已扫描到的确定性命中」，绝不宣称「完全暗标合规」；
- 图片与嵌入对象里的文字无法自动识别，一律列为待人工核对；
- 扫描的是最终 .docx 二进制本身，不是生成前的正文，避免「正文洗干净了、
  页眉页脚和属性还留着公司名」。
"""
from __future__ import annotations

import io
import re
import zipfile

from . import rules as app_rules, store

# 需要从 OOXML 包里逐字扫描的部件：正文、页眉页脚、脚注尾注、批注、
# 文档属性、自定义属性、表格与文本框都在这些 XML 里
TEXT_PARTS = (
    "word/document.xml", "word/footnotes.xml", "word/endnotes.xml",
    "word/comments.xml", "docProps/core.xml", "docProps/app.xml",
    "docProps/custom.xml",
)
TAG_RE = re.compile(r"<[^>]+>")
GENERIC_PATTERNS = (
    (re.compile(r"统一社会信用代码[:：]?\s*[0-9A-Z]{15,20}"), "统一社会信用代码"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"[\w.\-]+@[\w.\-]+\.[A-Za-z]{2,6}"), "邮箱"),
    (re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}"
                r"(?:有限公司|有限责任公司|股份有限公司|集团公司)"), "企业名称"),
    (re.compile(r"法定代表人[:：]\s*[\u4e00-\u9fa5]{2,8}"), "法定代表人"),
)


def default_rules() -> dict:
    return {
        "configured": False,
        "enterprise_full": "", "enterprise_short": [],
        "persons": [], "contacts": [], "addresses": [],
        "format": {"font": "", "size": "", "margin": "", "toc": "",
                   "cover": "", "page_no": "", "signature": "", "filename": ""},
        "volumes": {},
    }


def rules_for(pid: str) -> dict:
    raw = store.get_project_rules(pid)
    r = default_rules()
    if isinstance(raw, dict):
        for k in r:
            if k in raw:
                r[k] = raw[k]
    return r


def save_rules(pid: str, patch: dict) -> dict:
    r = rules_for(pid)
    for k, v in (patch or {}).items():
        if k not in r:
            continue
        r[k] = v
    # 填了身份词典才算「有项目规则」；只勾个开关不算
    identifiable = (str(r.get("enterprise_full") or "").strip()
                    or [x for x in (r.get("enterprise_short") or []) if str(x).strip()]
                    or [x for x in (r.get("persons") or []) if str(x).strip()]
                    or [x for x in (r.get("contacts") or []) if str(x).strip()])
    r["configured"] = bool(identifiable)
    return store.save_project_rules(pid, r)


def _identity_items(rules: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    full = str(rules.get("enterprise_full") or "").strip()
    if full:
        out.append(("企业全称", full))
    for key, label in (("enterprise_short", "企业简称"), ("persons", "人员姓名"),
                       ("contacts", "联系方式"), ("addresses", "地址")):
        for v in rules.get(key) or []:
            v = str(v).strip()
            # 单字 / 纯数字太容易误报，至少 2 个字符才进词典
            if len(v) >= 2:
                out.append((label, v))
    return out


def _part_text(data: bytes) -> str:
    """XML 里的文字：去标签但保留属性值里的文本（属性也可能藏身份信息）。"""
    s = data.decode("utf-8", "ignore")
    return TAG_RE.sub(" ", s) + " " + s


def scan_docx(data: bytes, pid: str = "") -> dict:
    """扫描最终文件；返回命中位置、未配置说明与待人工核对项。"""
    issues: list[dict] = []
    checked_parts: list[str] = []
    media: list[str] = []
    rules = rules_for(pid) if pid else default_rules()
    identity = _identity_items(rules)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            parts = [n for n in names
                     if n in TEXT_PARTS or re.fullmatch(r"word/(header|footer)\d*\.xml", n)]
            for name in sorted(parts):
                text = _part_text(z.read(name))
                checked_parts.append(name)
                label = ("页眉/页脚" if "/header" in name or "/footer" in name else
                         "文档属性" if name.startswith("docProps/") else
                         "表格/脚注/批注" if name != "word/document.xml" else "正文/标题/表格")
                # 逐字抄录的招标要求里出现招标方名称（如「国家电网有限公司」）
                # 不算投标人身份信息；引用标记附近 80 字内的命中按引文跳过。
                def quoted_at(pos: int) -> bool:
                    return app_rules.QUOTE_MARK in text[max(0, pos - 80):pos]

                for kind, value in identity:
                    hit = text.find(value)
                    if hit >= 0 and not quoted_at(hit):
                        issues.append({"code": "identity", "level": "高",
                                       "part": name, "where": label,
                                       "detail": f"{label}出现{kind}「{value}」"})
                # 通用正则只作为补充；已由词典命中的不重复报
                for pat, kind in GENERIC_PATTERNS:
                    m = pat.search(text)
                    while m and quoted_at(m.start()):
                        m = pat.search(text, m.end())
                    if m and not any(x["part"] == name and m.group(0) in x["detail"]
                                     for x in issues):
                        issues.append({"code": "identity_pattern", "level": "高",
                                       "part": name, "where": label,
                                       "detail": f"{label}出现{kind}「{m.group(0)[:40]}」"})
            media = [n for n in names if n.startswith("word/media/")
                     or n.startswith("word/embeddings/")]
    except Exception as exc:
        return {"ok": False, "checked": False, "configured": rules["configured"],
                "issues": [{"code": "scan_failed", "level": "高",
                            "detail": f"暗标扫描没跑完（{type(exc).__name__}）"}],
                "blocking": True, "checked_parts": [], "media": [],
                "summary": "暗标扫描未完成，不能判定合规"}
    if media:
        issues.append({"code": "media_manual", "level": "提示",
                       "part": ", ".join(media[:6]), "where": "图片/嵌入对象",
                       "detail": f"含 {len(media)} 个图片或嵌入对象，"
                                 "其中的文字、盖章、水印无法自动识别，须人工逐项核对"})
    blocking = [x for x in issues if x["level"] == "高"]
    # 没有项目规则时，即便没扫到命中也不能说「完全合规」
    if not rules["configured"]:
        issues.append({"code": "rules_missing", "level": "中",
                       "where": "项目规则",
                       "detail": "还没有登记本项目暗标规则（企业全称/简称/人员/联系方式/"
                                 "字体字号页边距与命名要求），本次只做了通用模式扫描，"
                                 "不能宣称完全暗标合规"})
    return {
        "ok": not blocking and rules["configured"],
        "checked": True, "configured": rules["configured"],
        "checked_parts": checked_parts, "media": media,
        "identity_terms": len(identity), "issues": issues, "blocking": blocking,
        "summary": ("暗标扫描未发现身份信息命中" if not blocking else
                    f"暗标扫描发现 {len(blocking)} 处身份信息，禁止作为暗标文件"),
        "claim": ("已按本项目登记的规则扫描" if rules["configured"] else
                  "仅通用模式扫描；未登记项目规则，结论不构成完全暗标合规证明"),
    }
