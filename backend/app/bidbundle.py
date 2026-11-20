"""C01 真实分册与投标包输出（不再只有规划结果）。

流程：用户确认分册归属 → 每册单独渲染 Word 并过最终文件检查 →
把原件附件、章节索引、逐条响应表、文件包清单一起打进 zip →
按 R06 的统一入口登记产物（正式件必须过门禁 + 每册检查）。

边界：
- 分册归属必须显式确认，未确认只返回规划预览，不生成文件；
- 目录域更新、PDF 转换、平台专用格式未实现，清单里明确写出；
- 图片 / 嵌入对象里的身份信息无法自动识别，逐册列待人工核对。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path

from . import blindcheck, delivery, docx_export, reqmatrix, store

VOLUME_RULES = (
    ("资格文件", ("资格", "资信", "资质", "营业执照", "财务", "业绩", "信誉", "证书")),
    ("报价文件", ("报价", "开标一览", "工程量清单", "分项报价", "投标报价")),
    ("商务文件", ("商务", "投标函", "法定代表人", "授权", "承诺", "声明", "保证金", "偏离")),
    ("技术文件", ("技术", "方案", "实施", "施工", "服务", "设计", "组织",
              "进度", "质量", "安全", "工艺", "措施", "售后", "培训", "验收")),
)
DEFAULT_VOLUME = "其他文件"


def classify(title: str) -> str:
    for name, words in VOLUME_RULES:
        if any(w in str(title or "") for w in words):
            return name
    return DEFAULT_VOLUME


def plan(pid: str) -> dict:
    """分册规划预览：全部章节都要有归属，界面让用户逐册确认或改。"""
    secs = store.list_sections(pid)
    groups: dict[str, list[dict]] = {}
    for s in secs:
        vol = classify(s.get("title") or "")
        groups.setdefault(vol, []).append({
            "id": s["id"], "title": s.get("title") or "",
            "chars": len(str(s.get("text") or "")),
            "protected": int(s.get("protected") or 0)})
    order = [n for n, _ in VOLUME_RULES if n in groups]
    if DEFAULT_VOLUME in groups:
        order.append(DEFAULT_VOLUME)
    volumes = [{"name": n, "sections": groups[n]} for n in order]
    rules = blindcheck.rules_for(pid)
    return {
        "volumes": volumes, "total": len(secs), "assigned": sum(len(v["sections"]) for v in volumes),
        "needs_confirmation": True,
        "rules": rules,
        "note": "这是启发式归属。请逐册核对并确认；确认后才生成文件。"
                "没有登记项目暗标规则时，不能宣称完全暗标合规。",
    }


def _validate(pid: str, volumes: list[dict]) -> tuple[list[dict] | None, str]:
    secs = {s["id"]: s for s in store.list_sections(pid)}
    if not volumes:
        return None, "没有分册方案"
    out, seen = [], set()
    for v in volumes:
        ids = [str(x) for x in (v.get("section_ids") or []) if str(x) in secs]
        name = str(v.get("name") or "").strip()[:40]
        if not name:
            return None, "有分册没写名称"
        dup = [i for i in ids if i in seen]
        if dup:
            return None, f"章节被重复分到多册：{len(dup)} 处"
        seen.update(ids)
        if ids:
            out.append({"name": name, "section_ids": ids})
    missing = [s["id"] for s in secs.values() if s["id"] not in seen]
    if missing:
        return None, f"还有 {len(missing)} 章没有分册归属"
    return out, ""


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(name or "")).strip()[:60] or "未命名"


def _index_doc(volumes: list[dict], by_id: dict) -> str:
    lines = ["投标文件分册索引", "", "本索引按确认的分册归属生成，与文件包内各册一一对应。", ""]
    for i, v in enumerate(volumes, 1):
        lines.append(f"第 {i} 册 · {v['name']}")
        for j, sid in enumerate(v["section_ids"], 1):
            s = by_id.get(sid) or {}
            lines.append(f"  {i}.{j} {s.get('title') or ''}")
        lines.append("")
    lines += ["目录域说明：各册目录使用 Word 自动域，打开时如提示更新域请点「是」；",
              "本系统不自动刷新域结果，也不生成 PDF 或平台专用格式。"]
    return "\n".join(lines)


def _response_table(pid: str) -> str:
    try:
        rep = reqmatrix.build(pid)
    except Exception as exc:
        return f"逐条响应表生成失败（{type(exc).__name__}），本次不附响应表，需人工复核。"
    lines = ["逐条响应表", "",
             f"共 {rep['total']} 条要求，必需 {rep['required']} 条；"
             f"已人工确认 {rep['confirmed']} 条，标不适用 {rep['na']} 条，待落实 {rep['pending']} 条。",
             "百分比仅表示起草进度，不代表可交付。", ""]
    for x in rep["items"]:
        lines.append(f"- [{x['id']}] ({x['kind']},{x['status']}) {x['text'][:80]}")
        if x.get("section_id"):
            lines.append(f"    响应章节：{x['section_id']}；证据：{(x.get('evidence') or '')[:80]}")
        else:
            lines.append("    未定位到响应章节，需人工补齐")
    return "\n".join(lines)


def build(pid: str, snap: dict, volumes: list[dict], *, blind: bool = False,
          include_attachments: bool = True, confirmed: bool = False) -> dict:
    """生成 zip 内容；未确认 / 有分册检查没过，都不交出可下载的文件。"""
    if not confirmed:
        # 下沉到业务层：就算以后新增别的导出入口，也拿不到未确认的文件
        return {"ok": False, "error": "未逐册确认分册归属，本次不生成投标包"}
    groups, err = _validate(pid, volumes)
    if err:
        return {"ok": False, "error": err}
    p = store.get_project(pid) or {}
    by_id = {s["id"]: s for s in store.list_sections(pid)}
    rules = blindcheck.rules_for(pid)
    buf = io.BytesIO()
    checks: list[dict] = []
    warnings: list[str] = []
    files: list[str] = []
    attachments: list[dict] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for no, v in enumerate(groups, 1):
            subset = [by_id[i] for i in v["section_ids"] if i in by_id]
            name = f"{no:02d}-{_safe(v['name'])}.docx"
            opts = {"blind": blind, "header": "" if blind else (p.get("name") or ""),
                    "date": snap.get("at", "")[:10],
                    "required_sections": [str(s.get("title") or "") for s in subset],
                    "toc": True}
            data = docx_export.build_docx(p.get("name") or "投标文件", subset, opts)
            chk = delivery.inspect_document(data, "docx", snap, blind=blind)
            checks.append({"volume": v["name"], "file": name, **chk})
            if not chk["ok"]:
                warnings.append(f"{v['name']} 最终文件检查未通过：{chk['summary']}")
            z.writestr(name, data)
            files.append(name)
        # 原件附件：只放本项目登记过、且磁盘上确实存在的原件
        if include_attachments:
            for a in store.list_attachments(pid):
                f = Path(str(a.get("path") or ""))
                if not f.is_file():
                    warnings.append(f"附件「{a.get('filename')}」已不在磁盘上，未打进包")
                    continue
                dest = f"附件/{_safe(a.get('filename') or f.name)}"
                z.write(f, dest)
                files.append(dest)
                attachments.append({"filename": a.get("filename"), "file": dest,
                                    "page": a.get("page") or 0,
                                    "fact_id": a.get("fact_id") or ""})
        z.writestr("分册索引.txt", _index_doc(groups, by_id))
        files.append("分册索引.txt")
        z.writestr("逐条响应表.txt", _response_table(pid))
        files.append("逐条响应表.txt")
        rules_note = ("已登记项目暗标规则，逐册做了全包扫描" if rules.get("configured")
                      else "未登记项目暗标规则：本次只做通用模式扫描，不能宣称完全暗标合规")
        manifest = {
            "project": p.get("name") or "", "snapshot": snap.get("fingerprint") or "",
            "blind": blind, "rules_configured": bool(rules.get("configured")),
            "volumes": [{"name": v["name"], "file": f"{i:02d}-{_safe(v['name'])}.docx",
                         "sections": len(v["section_ids"])}
                        for i, v in enumerate(groups, 1)],
            "files": files, "attachments": attachments,
            # 每册的空壳标记随清单带回：分册包也不能把「全是清单骨架」写成通过
            "final_checks": [{**{k: c[k] for k in ("volume", "file", "ok", "summary")},
                              "draft_markers": list(c.get("draft_markers") or [])}
                             for c in checks],
            "capability_note": [
                "目录域由 Word 打开时更新，本系统不自动刷新域结果",
                "PDF 转换未实现；需要 PDF 请按招标平台要求自行转换并核对",
                "平台专用格式（电子投标客户端导入格式）未实现",
                "图片 / 嵌入对象里的文字、盖章、水印无法自动识别，须人工逐项核对",
                rules_note,
            ],
            "warnings": warnings,
        }
        z.writestr("文件包清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    blocking = [c for c in checks if not c["ok"]]
    return {"ok": True,
            # 有一册没过最终检查：不交出字节，整个包不发
            "data": b"" if blocking else buf.getvalue(),
            "manifest": manifest, "checks": checks, "warnings": warnings,
            "blocking": blocking}
