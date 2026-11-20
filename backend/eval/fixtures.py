"""真实标书样本接入与基线（不含任何伪造样本、不会自动把样本标成已复核）。

当前仓库里的 cases*.jsonl 全部是开发者构造的起步标注集，未经招投标领域专家复核，
因此所有评测报告都写明「不构成标书质量能力证明」。要把口径升级成能力证明，
必须把真实招标文件 + 真实投标文件（或已投标书）放进来、脱敏并逐条人工标注。

典型流程（全部本地完成，不上传）：
  1) python -m backend.eval.fixtures --init                      # 看空白模板 / 字段说明
  2) python -m backend.eval.fixtures --import-tender 招标书.docx --id real-001 [--org "本公司名"]
     python -m backend.eval.fixtures --import-bid 投标书.docx --id real-001
                                                               # 自动解析 + 脱敏，生成待复核样本
  3) python -m backend.eval.fixtures --annotate                 # 生成逐条标注候选（值一律 null）
     人工打开 fixtures/<id>.json，把 responded / covered 改成 true/false，
     补 expect_fail（['coverage','numbers','placeholders','empty'] 中已知存在的硬伤）、
     填 reviewer / reviewed_at 并把 reviewed 改成 true
  4) python -m backend.eval.fixtures --run                      # 跑同一套指标，算标注一致率
  5) python -m backend.eval.fixtures --baseline                 # 输出 baseline_report.json

脚本只生成模板与候选标注：reviewed 恒为 false，任何样本都不会被自动升级为
「专家复核样本」。0 真实样本时基线报告会明确写「无真实样本、不构成能力证明」。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

DIR = Path(__file__).with_name("fixtures")
BASELINE = Path(__file__).with_name("baseline_report.json")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = {
    "id": "样本编号",
    "source": "real | constructed（构造样本不计入真实证据）",
    "reviewed": "bool，人工复核完成才可为 true",
    "reviewer": "复核人 / 机构；为空即视为未复核",
    "reviewed_at": "YYYY-MM-DD",
    "desensitized": "bool，是否经过本地脱敏",
    "tender": "真实招标文件全文（纯文本）",
    "sections": "[{'title': 章节名, 'text': 正文}]",
    "sources": "['本项目资料 / 企业素材库原文']，用于数值有据核查",
    "expect_fail": "['coverage','numbers','placeholders','empty'] 人工标注的已知硬伤",
    "expect_substantive": "[{'req': 要求原文, 'responded': true/false}] 人工逐条标注",
    "expect_scoring": "[{'item': 评分项, 'covered': true/false}] 人工逐项标注",
    "annotation_status": "pending | in_review | complete（脚本只写 pending）",
}

TEMPLATE = {
    "id": "real-001",
    "source": "real",
    "reviewed": False,
    "reviewer": "",
    "reviewed_at": "",
    "desensitized": False,
    "annotation_status": "pending",
    "tender": "（粘贴真实招标文件全文；未填则本样本不参与评测）",
    "sections": [{"title": "（章节标题）", "text": "（真实投标正文）"}],
    "sources": [],
    "expect_fail": [],
    "expect_substantive": [],
    "expect_scoring": [],
}

PII_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "【手机号已脱敏】"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "【邮箱已脱敏】"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "【证件号已脱敏】"),
    (re.compile(r"(?<!\d)[0-9A-HJ-NPQRTUWXY]{18}(?!\d)"), "【统一社会信用代码已脱敏】"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "【账号已脱敏】"),
]
ORG_RE = re.compile(r"[\u4e00-\u9fa5]{2,18}(?:股份有限公司|有限责任公司|有限公司|集团公司)")
HEAD_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十\d]+[章节部分]|[一二三四五六七八九十]+、|\d+[.、]\s*[^\d])\s*(.{2,40})$")


def desensitize(text: str, orgs: list[str] | None = None,
                mask_orgs: bool = True) -> tuple[str, int]:
    """本地脱敏：手机号 / 邮箱 / 证件号 / 信用代码 / 银行账号 / 机构名。"""
    n = 0
    out = str(text or "")
    for pattern, repl in PII_RULES:
        out, k = pattern.subn(repl, out)
        n += k
    for org in orgs or []:
        if org and len(org) >= 2:
            out, k = re.subn(re.escape(org), "【投标人已脱敏】", out)
            n += k
    if mask_orgs:
        out, k = ORG_RE.subn("【机构已脱敏】", out)
        n += k
    return out, n


def split_sections(text: str, min_chars: int = 40) -> list[dict]:
    """把真实投标正文按标题切成章节；切不出来就整篇一章，不猜测内容。"""
    chunks: list[tuple[str, list[str]]] = []
    title, body = "投标文件正文（导入）", []
    for line in str(text or "").splitlines():
        s = line.strip()
        is_head = bool(HEAD_RE.match(s)) or (len(s) <= 20 and not re.search(
            r"[。；;：:，,、]$", s) and any(
                k in s for k in ("投标函", "技术方案", "商务", "报价", "资格", "资质",
                                 "服务", "售后", "实施", "组织", "承诺", "偏离", "响应")))
        if is_head:
            if body:
                chunks.append((title, body))
            title, body = s[:60], []
        else:
            body.append(line)
    if body:
        chunks.append((title, body))
    out = [{"title": t, "text": "\n".join(b).strip()} for t, b in chunks
           if len("".join(b).strip()) >= min_chars]
    return out or [{"title": "投标文件正文（导入）", "text": str(text or "").strip()}]


def _read_text(path: Path) -> str:
    """复用上传入口的解析器：docx / pdf / xlsx / csv / txt / md 全在本地解析。"""
    from backend.app import parsers
    data = path.read_bytes()
    return str(parsers.parse_bytes(path.name, data).get("text") or "")


def _fixture_path(sample_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", sample_id).strip("-")[:60]
    if not safe:
        raise ValueError("样本 id 不能为空")
    return DIR / (safe + ".json")


def import_file(path_str: str, sample_id: str, kind: str,
                orgs: list[str] | None = None,
                mask_orgs: bool = True) -> Path:
    """导入一份真实招标书 / 已投标书；只写待复核样本，不改动复核状态。"""
    src = Path(path_str)
    if not src.is_file():
        raise FileNotFoundError(f"找不到文件：{src}")
    raw = _read_text(src)
    if len(raw.strip()) < 40:
        raise ValueError("解析出的正文不足 40 字；请换有文字层的文件或直接粘贴")
    clean, hits = desensitize(raw, orgs, mask_orgs)
    DIR.mkdir(parents=True, exist_ok=True)
    out = _fixture_path(sample_id)
    d: dict[str, Any] = dict(TEMPLATE)
    if out.exists():
        try:
            d = {**TEMPLATE, **json.loads(out.read_text(encoding="utf-8"))}
        except (ValueError, OSError):
            pass
    else:
        d["id"] = sample_id
    d["source"] = "real"
    d["desensitized"] = bool(d.get("desensitized")) or bool(hits)
    d["desensitize_note"] = (f"本地脱敏累计替换 {hits} 处（本次）；"
                             "未脱敏内容请人工复核后再用于评测")
    d["reviewed"] = False      # 导入 / 重新导入都不自动升级复核状态
    d["annotation_status"] = "pending"
    if kind == "tender":
        d["tender"] = clean
        d["sources"] = [clean[:4000]]
    else:
        d["sections"] = split_sections(clean)
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def annotate_candidates() -> list[dict]:
    """生成人工标注候选；值一律 null，等人填 true/false，绝不自动判定。"""
    from backend.app import rules
    from backend.app.basecore.services import evaluate
    out: list[dict] = []
    for case in load_fixtures():
        path = DIR / case["_file"]
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        tender = str(d.get("tender") or "")
        existing_sub = {str(x.get("req")): x for x in (d.get("expect_substantive") or [])
                        if isinstance(x, dict)}
        for req in rules.extract_requirements(tender, limit=24):
            existing_sub.setdefault(req, {"req": req, "responded": None,
                                          "note": "请人工核对正文是否实质响应"})
        try:
            scoring = [str(r.get("item") or "") for r in evaluate.parse_scoring(tender)]
        except Exception:
            scoring = []
        if not scoring:
            # 基座解析不到的评分办法：按字面「N 分」给候选清单，仍须人工逐条核对
            for line in tender.splitlines():
                if "评分" not in line:
                    continue
                for seg in re.split(r"[；;]", line):
                    for m in re.finditer(
                            r"([\u4e00-\u9fa5A-Za-z0-9]{2,24})\s*[（(]?\s*\d+(?:\.\d+)?\s*分", seg):
                        scoring.append(m.group(1))
        existing_sc = {str(x.get("item")): x for x in (d.get("expect_scoring") or [])
                       if isinstance(x, dict)}
        for item in scoring:
            if item:
                existing_sc.setdefault(item, {"item": item, "covered": None,
                                              "note": "请人工核对正文是否覆盖该得分点"})
        d["expect_substantive"] = list(existing_sub.values())[:24]
        d["expect_scoring"] = list(existing_sc.values())[:24]
        d.setdefault("expect_fail", [])
        d["annotation_status"] = "pending"
        d["annotation_note"] = ("候选由规则抽取；只有把 responded / covered 全部改成 "
                                "true 或 false、填 reviewer / reviewed_at 并置 reviewed=true，"
                                "才算专家复核样本")
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        out.append({"id": d.get("id"), "file": path.name,
                    "substantive_candidates": len(d["expect_substantive"]),
                    "scoring_candidates": len(d["expect_scoring"])})
    return out


def _annotation_complete(case: dict) -> bool:
    ann = list(case.get("expect_substantive") or []) + list(case.get("expect_scoring") or [])
    if not ann:
        return False
    for x in ann:
        if not isinstance(x, dict):
            return False
        v = x.get("responded", x.get("covered", None))
        if not isinstance(v, bool):
            return False
    return isinstance(case.get("expect_fail"), list)


def load_fixtures() -> list[dict]:
    """只读加载；未填正文 / 格式不对的样本跳过，绝不猜测内容。"""
    if not DIR.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(DIR.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        tender = str(d.get("tender") or "")
        if len(tender) < 40 or tender.startswith("（粘贴"):
            continue
        secs = [s for s in (d.get("sections") or [])
                if isinstance(s, dict) and str(s.get("text") or "").strip()
                and not str(s.get("text") or "").startswith("（真实投标正文")]
        if not secs:
            continue
        d["sections"] = secs
        d["_file"] = path.name
        d["source"] = str(d.get("source") or "unspecified")
        d["annotation_complete"] = _annotation_complete(d)
        # 复核状态：显式布尔 + 复核人 + 真实来源 + 逐条标注全为人工判定
        d["expert_annotated"] = bool(
            d.get("reviewed") and str(d.get("reviewer") or "").strip()
            and d["source"] == "real" and d["annotation_complete"])
        out.append(d)
    return out


def status() -> dict:
    rows = load_fixtures()
    real = [r for r in rows if r["source"] == "real"]
    reviewed = [r for r in rows if r["expert_annotated"]]
    complete = [r for r in rows if r["annotation_complete"]]
    return {
        "fixtures_dir": str(DIR),
        "available": bool(rows),
        "count": len(rows),
        "real_samples": len(real),
        "desensitized": sum(1 for r in rows if r.get("desensitized")),
        "annotation_complete": len(complete),
        "expert_annotated": len(reviewed),
        "gold_reviewed": False,   # cases.jsonl / cases_quality.jsonl 仍是起步标注集
        "schema": SCHEMA,
        "next_steps": [
            "--import-tender <文件> --id <id> / --import-bid <文件> --id <id>（本地解析 + 脱敏）",
            "--annotate 生成逐条标注候选，人工把 responded / covered 改成 true/false",
            "人工填 reviewer / reviewed_at 并置 reviewed=true（脚本不会自动升级）",
            "--run 看标注一致率，--baseline 输出基线报告",
        ],
        "claim": ("尚无经专家复核的真实标书样本；所有分数仍为同源自测，"
                  "不构成标书质量能力证明" if not reviewed else
                  f"已有 {len(reviewed)} 份经标注的真实样本；结论口径需结合样本量另行说明"),
    }


def run_fixture(case: dict) -> dict:
    """用与主链路完全相同的指标跑一份样本；不写库、不调模型。"""
    from backend.app import quality, substantive
    secs = case.get("sections") or []
    tender = str(case.get("tender") or "")
    q = quality.assess(tender, secs, case.get("sources") or [])
    sub = substantive.summarize(tender, secs)
    exp_fail = set(case.get("expect_fail") or [])
    got_fail = {k for k, v in (q.get("flags") or {}).items() if not v}
    exp_sub = {str(x.get("req")): x.get("responded")
               for x in (case.get("expect_substantive") or []) if isinstance(x, dict)}
    got_sub = {x["req"]: bool(x["responded"])
               for x in sub["requirements"]["items"]}
    annotated_sub = {k: v for k, v in exp_sub.items() if isinstance(v, bool)}
    matched = sum(1 for k, v in annotated_sub.items()
                  if k in got_sub and got_sub[k] == v)
    exp_sc = {str(x.get("item")): x.get("covered")
              for x in (case.get("expect_scoring") or []) if isinstance(x, dict)}
    got_sc = {str(x.get("item")): bool(x.get("covered"))
              for x in sub["scoring"]["items"]}
    annotated_sc = {k: v for k, v in exp_sc.items() if isinstance(v, bool)}
    matched_sc = sum(1 for k, v in annotated_sc.items()
                     if k in got_sc and got_sc[k] == v)
    return {
        "id": case.get("id") or case.get("_file"),
        "source": case.get("source"),
        "expert_annotated": bool(case.get("expert_annotated")),
        "annotation_complete": bool(case.get("annotation_complete")),
        "desensitized": bool(case.get("desensitized")),
        "hard_checks": {"expected": sorted(exp_fail), "got": sorted(got_fail),
                        "matched": exp_fail == got_fail},
        "substantive": {
            "ratio": sub["requirements"]["ratio"],
            "total": sub["requirements"]["total"],
            "annotated": len(annotated_sub),
            "agreement": (round(matched / len(annotated_sub), 4)
                          if annotated_sub else None),
        },
        "scoring": {
            "available": sub["scoring"]["available"],
            "annotated": len(annotated_sc),
            "agreement": (round(matched_sc / len(annotated_sc), 4)
                          if annotated_sc else None),
        },
        "claim": ("未经专家复核，结果只是自动指标读数，不能宣称能力达标"
                  if not case.get("expert_annotated") else
                  "经人工标注样本上的自动指标一致率；仍需说明样本量与专家独立性"),
    }


def baseline() -> dict:
    """输出能力基线：把同源自测与真实样本严格分开，不混算成一个分数。"""
    st = status()
    reg_path = Path(__file__).with_name("report.json")
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            reg = {}
    else:
        try:
            from backend.eval.run_eval import evaluate
            reg = evaluate()
        except Exception as exc:
            reg = {"error": f"{type(exc).__name__}: {exc}"[:160]}
    rows = load_fixtures()
    runs = [run_fixture(r) for r in rows]
    reviewed_runs = [r for r in runs if r["expert_annotated"]]
    report = {
        "generated_at": date.today().isoformat(),
        "version": "1.0",
        "regression_smoke": {
            "source": "backend/eval/report.json（开发者构造起步集，同源自测）",
            "all_passed": reg.get("all_passed"),
            "layers": {k: (v.get("all_passed") if isinstance(v, dict) else None)
                       for k, v in (reg.get("layers") or {}).items()},
            "metrics": reg.get("metrics") or {},
            "claim": "只说明确定性层没被改坏，不是标书质量能力证明",
        },
        "real_samples": {
            "count": st["real_samples"],
            "desensitized": st["desensitized"],
            "annotation_complete": st["annotation_complete"],
            "expert_annotated": st["expert_annotated"],
            "runs": reviewed_runs,
            "unreviewed_runs": [r for r in runs if not r["expert_annotated"]],
            "status": ("no_real_samples" if not rows else
                       "no_expert_review" if not reviewed_runs else "baseline_available"),
        },
        "capability_proof_requirements": [
            "至少 1 份脱敏真实招标文件 + 对应已投标书（--import-tender / --import-bid）",
            "人工逐条标注硬性要求响应、评分项覆盖与已知硬伤（--annotate 只生成候选）",
            "填 reviewer / reviewed_at 并把 reviewed 置 true（脚本不自动升级）",
            "样本量与标注者独立性需另行说明；自动一致率仍不等于评标结论",
        ],
        "claim": (f"当前 {st['real_samples']} 份真实样本 / "
                  f"{st['expert_annotated']} 份经专家复核，基线仅含同源自测读数，"
                  "不构成标书质量能力证明" if not reviewed_runs else
                  f"已有 {len(reviewed_runs)} 份经人工标注的真实样本，"
                  "可作为初步基线；仍需说明样本量、领域与专家独立性"),
        "honest_limits": [
            "逐条实质响应是自动启发式：连响应词一起照抄要求仍可能判为已响应",
            "不评文采、方案优劣、评分办法实际得分、OCR 准确率",
            "报告不会把构造用例的通过率计入真实样本能力证明",
        ],
    }
    BASELINE.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return report


def init_template() -> Path:
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / "real-001.template.json"
    if not path.exists():
        path.write_text(json.dumps(TEMPLATE, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return path


def _arg(args: list[str], name: str, default: str = "") -> str:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main() -> int:
    args = sys.argv[1:]
    if "--init" in args:
        print("模板已生成：", init_template())
        print("也可以直接 --import-tender / --import-bid 导入本地文件；"
              "reviewed / reviewer 未填前，一律不计为专家复核样本。")
    sample_id = _arg(args, "--id", "real-001")
    orgs = [x for x in (_arg(args, "--org", "")).split(",") if x.strip()]
    mask = "--keep-orgs" not in args
    for flag, kind in (("--import-tender", "tender"), ("--import-bid", "bid")):
        if flag in args:
            path = _arg(args, flag)
            if not path:
                print(f"{flag} 需要跟一个本地文件路径")
                return 2
            try:
                out = import_file(path, sample_id, kind, orgs, mask)
                print(f"已导入待复核样本（source=real, reviewed=false）：{out}")
            except (OSError, ValueError) as exc:
                print("导入失败：", exc)
                return 2
    if "--annotate" in args:
        rows = annotate_candidates()
        print("已生成人工标注候选（值均为 null）：", json.dumps(rows, ensure_ascii=False))
    st = status()
    print(json.dumps({k: v for k, v in st.items() if k != "schema"},
                     ensure_ascii=False, indent=2))
    if "--run" in args:
        for case in load_fixtures():
            print(json.dumps(run_fixture(case), ensure_ascii=False))
    if "--baseline" in args:
        rep = baseline()
        print("基线报告：", BASELINE)
        print("口径：", rep["claim"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
