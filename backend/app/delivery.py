"""R06 统一导出协调：冻结快照 → 门禁 → 渲染 → 落盘登记 → 回下载校验。

以前的问题：检查时读一次活数据、渲染时再读一次；同名秒级文件互相覆盖；
登记记录会按展示名先删后插；落盘失败还照样把文件发出去。现在所有正式输出
共用一个入口，检查与渲染针对同一份冻结快照，产物按 企业/项目/产物ID 落盘，
登记失败就明确失败，回下载核对归属与内容摘要。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import auth, config, requirement_support, settings, store

# 渲染实现 / 模板 / 必要版本变更时手工递增：旧确认令牌随指纹一起失效
TEMPLATE_VERSION = "2026-11-10.1"

# 第三种产物类型：用户看完提醒后点「仍然导出」得到的草稿。
# 它经过了门禁、也允许下载，但没有被判定为可交付，绝不能混进「正式交付件」。
ACK_DRAFT_KIND = "acknowledged_draft"
ACK_DRAFT_PURPOSE = "知情导出草稿 · 已知悉风险 · 不可直接提交"

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def safe_name(name: str) -> str:
    s = _SAFE.sub("_", str(name or "output")).strip("._") or "output"
    return s[-120:]


def _sha(data: bytes | str) -> str:
    b = data.encode("utf-8", "ignore") if isinstance(data, str) else data
    return hashlib.sha256(b).hexdigest()


def _policy() -> dict:
    try:
        raw = settings.load()
        # 只留会影响导出判定与自动编排的策略，密钥本身不落库
        return {k: raw.get(k) for k in ("stages", "export_caps", "caps",
                                        "export_quality", "auto", "mode")
                if k in raw}
    except Exception:
        return {"error": "策略读取失败"}


def policy_fingerprint() -> str:
    return _sha(json.dumps(_policy(), sort_keys=True, ensure_ascii=False,
                           default=str))[:32]


def _doc_digest(d: dict) -> dict:
    return {"id": d.get("id"), "title": d.get("title"),
            "sha": _sha(str(d.get("content") or "")),
            "parse_status": d.get("parse_status") or "ok",
            "needs_review": int(d.get("needs_review") or 0),
            "confirmed_at": d.get("confirmed_at") or "",
            "verification": d.get("verification") or ""}


def build_snapshot(pid: str) -> dict:
    """一次性冻结正文、有效要求、资料、策略、模板与解析状态。"""
    p = store.get_project(pid) or {}
    secs = [{"id": s.get("id"), "title": s.get("title") or "",
             "text": s.get("text") or "", "ver": int(s.get("ver") or 0),
             "protected": int(s.get("protected") or 0)}
            for s in store.list_sections(pid)]
    try:
        # 只读表，不触发自动重抽：算指纹不应有写副作用
        reqs = store.list_requirements(pid)
    except Exception:
        reqs = [{"error": "要求表读取失败"}]
    try:
        outline = p.get("outline") or []
    except Exception:
        outline = []
    return {
        "pid": pid,
        "org": _org(),
        "name": p.get("name") or "",
        "tender_text": p.get("tender_text") or "",
        "tender_parse": p.get("tender_parse") or "",
        "sections": secs,
        "outline": outline if isinstance(outline, list) else [],
        "requirements": reqs,
        "requirement_support": requirement_support.snapshot(pid),
        "project_docs": [_doc_digest(d) for d in store.list_docs(project_id=pid)],
        "kb_docs": [_doc_digest(d) for d in store.list_docs(scope="kb")],
        "policy": _policy(),
        "policy_fp": policy_fingerprint(),
        "template": {"builder": "docx_export.build_docx", "version": TEMPLATE_VERSION,
                     "renderer": "markdown.v1"},
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _frozen_fields(snap: dict) -> dict:
    """指纹覆盖内容 + 策略 + 模板；时间、企业名等展示字段不参与。"""
    return {k: snap.get(k) for k in ("pid", "name", "tender_text", "tender_parse",
                                     "sections", "outline", "requirements",
                                     "project_docs", "kb_docs", "requirement_support", "policy_fp", "template")}


def fingerprint(snap: dict) -> str:
    body = json.dumps(_frozen_fields(snap), sort_keys=True, ensure_ascii=False,
                      default=str)
    return hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()[:32]


def content_fingerprint(pid: str) -> str:
    """auto_core 的确认令牌指纹：内容、要求、资料、策略和模板任一变化都变。"""
    return fingerprint(build_snapshot(pid))


def freeze(pid: str) -> dict:
    """检查与渲染共用的唯一输入。"""
    snap = build_snapshot(pid)
    snap["fingerprint"] = fingerprint(snap)
    snap["version"] = snap["fingerprint"][:12]
    return snap


def live_changed(snap: dict) -> bool:
    """渲染时活数据是否已被改动；只用来给产物加提示，不改变冻结版本的输出。"""
    try:
        return content_fingerprint(snap["pid"]) != snap["fingerprint"]
    except Exception:
        return False


def _org() -> str:
    try:
        return auth.org_id() or "default"
    except Exception:
        return "default"


def org_id() -> str:
    """当前企业归属（供产物登记使用）。"""
    return _org()


def output_path(pid: str, filename: str, *, org: str = "", kind: str = "review") -> Path:
    """每个产物一个独立目录：企业 / 项目 / 产物ID / 展示名。绝不被后续导出覆盖。"""
    aid = store.new_id()
    folder = (config.DATA_DIR / "exports" / safe_name(org or _org())
              / safe_name(pid) / aid)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = ("" if kind == "deliverable" else
              "知情草稿-" if kind == ACK_DRAFT_KIND else "审阅件-")
    return folder / (prefix + safe_name(filename))


def inspect_document(data: bytes, fmt: str, snap: dict, *, blind: bool = False) -> dict:
    """最终文件检查：可打开性、内部标记、占位符、身份信息、附件数量。

    检查没跑完或没过，调用方不得把它登记成正式交付件。
    """
    issues: list[dict] = []
    text = ""
    blind_scan: dict = {}
    if fmt == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    issues.append({"code": "docx_structure", "level": "高",
                                   "detail": "Word 结构不完整，可能打不开"})
                else:
                    xml = z.read("word/document.xml").decode("utf-8", "ignore")
                    text = re.sub(r"<[^>]+>", "", xml)
                    props = z.read("docProps/core.xml").decode("utf-8", "ignore") \
                        if "docProps/core.xml" in names else ""
                    for m in re.finditer(r"<dc:(?:creator|lastModifiedBy)>([^<]*)<", props):
                        if m.group(1).strip():
                            issues.append({"code": "docx_author", "level": "中",
                                           "detail": "文档属性里还有作者信息"})
                    # C01：格式项与目录域只按「文件里实际有什么」报告，不猜规则
                    if "TOC \\" not in xml and "TOC" not in xml:
                        issues.append({"code": "toc_field", "level": "提示",
                                       "detail": "未检测到目录域；目录页码需在 Word 中"
                                                 "手动更新域，本系统不自动刷新域结果"})
                    if "w:sectPr" not in xml:
                        issues.append({"code": "no_section", "level": "提示",
                                       "detail": "未检测到节属性（页边距/页码设置）"})
                    media = [n for n in names if n.startswith("word/media/")
                             or n.startswith("word/embeddings/")]
                    if media:
                        issues.append({"code": "docx_media", "level": "提示",
                                       "detail": f"含 {len(media)} 个图片/嵌入对象，"
                                                 "其中文字与盖章须人工核对（无法自动识别）"})
            # C01：暗标按最终二进制做全包扫描（正文/标题/表格/页眉页脚/属性/脚注/批注）
            if blind:
                from . import blindcheck
                blind_scan = blindcheck.scan_docx(data, snap.get("pid") or "")
                for x in blind_scan.get("issues") or []:
                    issues.append({"code": x["code"], "level": x["level"],
                                   "detail": x["detail"]})
                if not blind_scan.get("configured"):
                    issues.append({"code": "blind_rules_missing", "level": "中",
                                   "detail": "未登记本项目暗标规则，不能宣称完全暗标合规；"
                                             "本次仅通用模式扫描"})
        except Exception as exc:
            issues.append({"code": "docx_open", "level": "高",
                           "detail": f"文件无法解析为 Word（{type(exc).__name__}）"})
    elif fmt == "md":
        try:
            text = data.decode("utf-8")
        except Exception as exc:
            issues.append({"code": "md_decode", "level": "高", "detail": str(exc)[:120]})
    else:
        issues.append({"code": "unknown_format", "level": "高",
                       "detail": f"未知格式 {fmt}，未完成最终文件检查"})
    # 真的占位符 / 内部标注仍是硬伤，按原规则拦；系统自己生成的空壳单独记。
    hard_patterns = (
        ("XXX", "占位符 XXX"), ("TODO", "占位符 TODO"),
        ("待补充", "占位符「待补充」"), ("【需补充", "内部补录标记"),
        ("需人工补充", "内部待补标注"),
        ("依据清单", "内部依据标注"), ("AI 建议", "内部 AI 标注"),
    )
    for pat, label in hard_patterns:
        if pat in text:
            issues.append({"code": "placeholder", "level": "高",
                           "detail": f"正式文件里仍有{label}"})
    # 保护章节的「（未填写）」、structured 的「待人工核验」清单骨架，过去不在
    # 扫描表里，于是整本「还不能交」的模板稿照样拿到 X-File-Check: pass。
    # 这些是「内容还没填」的事实，不是文件结构损坏：如实列为提醒并让导出
    # 侧降级登记，但不把它们升级成「禁止下载」，契约仍是提醒后可下载。
    draft_patterns = (
        ("（未填写）", "保护章节空正文「（未填写）」"),
        ("待人工核验", "待人工核验的材料清单骨架"),
        ("人工核验后填写", "待人工补填的材料信息"),
        ("【说明】", "内部说明标注"),
    )
    for pat, label in draft_patterns:
        if pat in text:
            issues.append({"code": "draft_marker", "level": "中",
                           "detail": f"文件里仍有{label}，属于未填完的清单骨架"})
    draft_markers = [label for pat, label in draft_patterns if pat in text]
    if blind and not blind_scan:
        for s in snap.get("sections") or []:
            title = str(s.get("title") or "")
            if len(title) >= 4 and title in (snap.get("name") or ""):
                issues.append({"code": "blind_name", "level": "高",
                               "detail": "暗标文件里出现项目/企业可识别名称"})
    blocking = [x for x in issues if x["level"] == "高"]
    deliverable_ready = not blocking and not draft_markers
    return {"ok": not blocking, "checked": True, "fmt": fmt,
            "chars": len(text), "issues": issues, "blocking": blocking,
            "draft_markers": draft_markers,
            "deliverable_ready": deliverable_ready, "blind_scan": blind_scan,
            "capability_note": (
                "目录域由 Word 打开时更新，本系统不自动刷新域结果；"
                "PDF 转换与平台专用格式（如电子投标客户端专用格式）未实现，"
                "需要按招标平台要求另行处理"),
            "summary": (
                f"最终文件检查发现 {len(blocking)} 项硬伤，不能标成正式交付件"
                if blocking else
                f"文件可打开，但仍有 {len(draft_markers)} 类未填完的清单骨架，"
                "不能作为正式交付件" if draft_markers else
                "最终文件检查通过")}


def register(pid: str, data: bytes, filename: str, *, kind: str = "deliverable",
             snap: dict | None = None, gate_fingerprint: str = "",
             fmt: str = "docx", options: dict | None = None,
             check: dict | None = None) -> dict:
    """落盘并登记。任何一步失败都抛出，调用方必须明确返回失败，不发无追溯文件。"""
    org = (snap or {}).get("org") or _org()
    path = output_path(pid, filename, org=org, kind=kind)
    path.write_bytes(data)
    digest = _sha(data)
    item = store.record_artifact(
        pid, kind, path, name=path.name, content_hash=digest,
        gate_fingerprint=gate_fingerprint, org_id=org,
        snapshot_fingerprint=(snap or {}).get("fingerprint") or "",
        purpose=("正式交付" if kind == "deliverable" else
                 ACK_DRAFT_PURPOSE if kind == ACK_DRAFT_KIND else "审阅件，非交付件"),
        options=json.dumps(options or {}, ensure_ascii=False, sort_keys=True),
        file_check=json.dumps(check or {}, ensure_ascii=False),
    )
    item["content_hash"] = digest
    item["path"] = str(path)
    return item


def verify_download(pid: str, art: dict) -> tuple[bool, str]:
    """回下载校验：归属、登记状态、磁盘存在、内容摘要、路径越界。"""
    if not art or art.get("project_id") != pid:
        return False, "这条产出记录不属于当前项目"
    if art.get("status") in ("deleted", "missing"):
        return False, ("文件已被清理，记录标为缺失，请重新导出" if art["status"] == "missing"
                       else "记录已标记清理，请重新导出")
    org = art.get("org_id") or "default"
    root = (config.DATA_DIR / "exports" / safe_name(org) / safe_name(pid)).resolve()
    path = Path(str(art.get("path") or "")).resolve()
    if root not in path.parents:
        return False, "文件路径不在本项目的产出目录里"
    if not path.is_file():
        return False, "文件已不在磁盘上（可能被清理），请重新导出"
    if _sha(path.read_bytes()) != (art.get("content_hash") or ""):
        return False, "文件内容与登记摘要不一致（磁盘文件被改动），拒绝回下载"
    return True, ""
