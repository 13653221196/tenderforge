"""分层回归评测（只读，不写业务库）。

运行：python backend/eval/run_eval.py
输出：backend/eval/report.json

五层：
1. rules      —— 用 cases.jsonl 的人工标注断言，检查问题是否指向标注的证据位置；
                 评分不看规则内部的 code / 触发词，避免「自己出题自己判卷」。
2. grounding  —— 检索切块坐标可复原、命中带页码与字符区间、数值必须能逐字引用。
3. trajectory —— 真实模型端到端链路，由 e2e_live.py 单独跑；这里只汇总其报告。
4. quality    —— 生成正文的四项自动硬指标（词面覆盖 / 数值有据 / 占位符 / 空章）。
5. substantive—— 逐条实质响应（词面 + 响应表述 + 要求数值）与评分项覆盖。
6. controls   —— 招标要求表人工修订优先、章节未达交付标准的硬阻断契约。

重要：这些数字只说明「本次改动没把确定性层改坏」，不说明真实标书上的能力。
所有样本都是未经领域专家复核的起步集；真实标书样本请用
`python -m backend.eval.fixtures --import-tender <文件> --id <id>` 接入、人工复核，
再跑 `--baseline` 出能力基线。0 真实样本时报告会明确写「不构成能力证明」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import provenance, rag, rules, store, substantive  # noqa: E402
from backend.eval import fixtures, quality  # noqa: E402
from backend.eval.gold import load_cases  # noqa: E402

TARGETS = {"recall": 0.90, "false_positive_rate": 0.15, "localization": 0.85}
Q_TARGETS = {"recall": 0.90, "false_positive_rate": 0.15}
Q_CASES = Path(__file__).with_name("cases_quality.jsonl")
S_CASES = Path(__file__).with_name("cases_substantive.jsonl")
OUT = Path(__file__).with_name("report.json")


def _hit(issues: list[dict], spec: str) -> dict | None:
    """一条标注断言是否被满足：看问题指向哪个章节、证据里有没有标注的原文片段。"""
    parts = spec.split("|", 2)
    kind, sec = parts[0], parts[1]
    needle = parts[2] if len(parts) > 2 else ""
    for i in issues:
        if kind == "no_response":
            if i.get("section_id"):
                continue
            blob = " ".join(str(i.get(k) or "") for k in
                            ("title", "detail", "evidence", "source_ref"))
        else:
            if i.get("section_id") != sec:
                continue
            blob = " ".join(str(i.get(k) or "") for k in
                            ("title", "detail", "evidence", "anchor", "source_ref"))
        if needle and needle not in blob:
            continue
        return i
    return None


def layer_rules() -> dict:
    cases = load_cases()
    tp = fn = fp = located = reported = 0
    misses: list[dict] = []
    by_stratum: dict[str, dict] = {}
    by_split: dict[str, dict] = {}

    def tally(key: str, bucket: dict, hit: bool, is_false_positive: bool = False) -> None:
        row = bucket.setdefault(key, {"cases": 0, "passed": 0, "false_positives": 0})
        row["cases"] += 1
        if hit:
            row["passed"] += 1
        if is_false_positive:
            row["false_positives"] += 1

    for case in cases:
        issues = rules.rule_check(case["sections"], case["tender"])
        reported += len(issues)
        specs = case.get("must_report") or []
        if specs:
            matched = [_hit(issues, s) for s in specs]
            ok = all(matched)
            if ok:
                tp += 1
                # 定位：至少一条命中的问题落在标注期望的章节（整本级问题不限章节）
                if any(m.get("section_id") == s.split("|")[1]
                       for m, s in zip(matched, specs) if m and s.split("|")[1]):
                    located += 1
                elif any(s.startswith("no_response") for s in specs):
                    located += 1
            else:
                fn += 1
                if len(misses) < 12:
                    misses.append({"id": case["id"], "expect": specs,
                                   "annotation": case.get("annotation"),
                                   "got": [(i.get("title"), i.get("section_id"))
                                           for i in issues[:6]]})
            tally(case["stratum"], by_stratum, ok)
            tally(case["split"], by_split, ok)
        else:
            bad = [i for i in issues if i.get("section_id") == "s2"]
            fp += len(bad)
            ok = not bad
            if bad and len(misses) < 12:
                misses.append({"id": case["id"], "expect": "不应报",
                               "annotation": case.get("annotation"),
                               "got": [i.get("title") for i in bad[:6]]})
            tally(case["stratum"], by_stratum, ok, bool(bad))
            tally(case["split"], by_split, ok, bool(bad))

    metrics = {
        "cases": len(cases), "should_report": tp + fn,
        "true_positive": tp, "false_negative": fn, "false_positive": fp,
        "total_reported": reported,
        "recall": round(tp / max(1, tp + fn), 4),
        "false_positive_rate": round(fp / max(1, tp + fp), 4),
        "localization": round(located / max(1, tp), 4),
    }
    verdict = {
        "recall": metrics["recall"] >= TARGETS["recall"],
        "false_positive_rate": metrics["false_positive_rate"] <= TARGETS["false_positive_rate"],
        "localization": metrics["localization"] >= TARGETS["localization"],
    }
    return {"metrics": metrics, "passed": verdict, "all_passed": all(verdict.values()),
            "by_stratum": by_stratum, "by_split": by_split, "sample_misses": misses}


def layer_grounding() -> dict:
    """检索/证据层：坐标可复原、命中带页码、数值只能逐字引用依据。"""
    material = ("【第 1 页】企业资质\n营业执照有效期内，安防工程一级。\n"
                "【第 2 页】同类项目业绩\n2024 年某园区视频监控项目，合同金额 386 万元，已验收。\n"
                "售后服务承诺：故障响应 20 分钟，现场到达 2 小时。\n")
    spans = rag.split_spans(material)
    coord_ok = bool(spans) and all(material[s["start"]:s["end"]].strip() == s["text"]
                                   for s in spans)
    has_page = any(s["page"] == 2 for s in spans)

    orig = store.list_docs
    store.list_docs = lambda scope="", project_id="": (
        [{"id": "doc-1", "title": "企业资质.txt", "content": material}]
        if scope in ("kb", "project") else [])
    try:
        _, hits = rag.retrieve("evl", "同类项目业绩 386 万元 故障响应", k=4)
    finally:
        store.list_docs = orig
    with_coord = bool(hits) and all(
        all(k in h for k in ("doc_id", "page", "start", "end", "ref")) for h in hits)
    # 与 hits 同构的证据：数值核对只认检索实际拿到的原文片段
    evidence = [{"snippet": h.get("snippet", ""), "ref": h.get("ref", "")} for h in hits]
    unsupported = provenance.verify_numbers("合同金额 999 万元，故障响应 5 分钟。",
                                            evidence, "")
    supported = provenance.verify_numbers("合同金额 386 万元，故障响应 20 分钟。",
                                          evidence, "")
    checks = {
        "坐标可复原": coord_ok, "命中带页码": has_page,
        "命中带坐标": with_coord,
        "证据外数值被抓到": any("999" in x for x in unsupported),
        "依据内数值不误报": not any("386" in x for x in supported),
    }
    return {"checks": checks, "all_passed": all(checks.values()),
            "unsupported_sample": unsupported[:4], "hits": len(hits)}


def layer_trajectory() -> dict:
    p = Path(__file__).with_name("e2e_report.json")
    if not p.exists():
        return {"status": "not_run",
                "reason": "真实模型端到端链路需服务运行中，执行 python backend/eval/e2e_live.py"}
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
        return {"status": "report_available", "all_passed": bool(r.get("all_passed")),
                "model_live_verified": bool(r.get("model_live_verified")),
                "model_probe": r.get("model_probe") or {},
                "claim": r.get("claim") or "",
                "checks": len(r.get("checks") or []),
                "degraded": [c["name"] for c in (r.get("checks") or [])
                             if c.get("degraded")]}
    except (ValueError, OSError) as exc:
        return {"status": "unreadable", "reason": str(exc)[:120]}


def load_quality_cases() -> list[dict]:
    return [json.loads(line) for line in Q_CASES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def layer_quality() -> dict:
    """生成正文的四项自动硬指标：带已知缺陷的样本必须被抓到，干净样本不能误报。"""
    cases = load_quality_cases()
    tp = fn = fp_cases = 0
    by_stratum: dict[str, dict] = {}
    misses: list[dict] = []
    for c in cases:
        r = quality.assess(c.get("tender") or "", c.get("sections") or [],
                           c.get("sources") or [])
        got = {k for k, v in r["flags"].items() if not v}
        exp = set(c.get("expect_fail") or [])
        missing, extra = exp - got, got - exp
        row = by_stratum.setdefault(c.get("stratum") or "unknown",
                                    {"cases": 0, "passed": 0, "false_positives": 0})
        row["cases"] += 1
        if not missing and not extra:
            row["passed"] += 1
        if extra:
            fp_cases += 1
            row["false_positives"] += 1
            if len(misses) < 10:
                misses.append({"id": c["id"], "expect": sorted(exp),
                               "got": sorted(got), "kind": "误报"})
        if exp:
            if not missing:
                tp += 1
            else:
                fn += 1
                if len(misses) < 10:
                    misses.append({"id": c["id"], "expect": sorted(exp),
                                   "got": sorted(got), "kind": "漏报"})
    metrics = {
        "cases": len(cases), "defective": tp + fn, "detected": tp, "missed": fn,
        "false_positive_cases": fp_cases,
        "recall": round(tp / max(1, tp + fn), 4),
        "false_positive_rate": round(fp_cases / max(1, len(cases)), 4),
    }
    passed = {"recall": metrics["recall"] >= Q_TARGETS["recall"],
              "false_positive_rate":
                  metrics["false_positive_rate"] <= Q_TARGETS["false_positive_rate"]}
    return {
        "annotation": {
            "source": "manual_starter_unreviewed",
            "cases_file": "backend/eval/cases_quality.jsonl",
            "cases": len(cases), "reviewed": False,
            "independence": "样本由本层开发者按业务写法构造，未由第三方专家标注，属同源自测",
            "checks": list(quality.CHECK_LABELS),
        },
        "metrics": metrics, "targets": Q_TARGETS, "passed": passed,
        "all_passed": all(passed.values()), "by_stratum": by_stratum,
        "sample_misses": misses,
        "scope": "生成正文的四项自动硬指标回归冒烟",
        "claim": "只验证可自动核查的硬指标；词面覆盖不等于实质响应，不构成专家评标结论",
    }


def layer_substantive() -> dict:
    """逐条实质响应 + 评分项覆盖的起步回归；已知局限单列，不混入通过率。"""
    cases = [json.loads(line) for line in S_CASES.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    checked, passed, misses = 0, 0, []
    known: list[dict] = []
    for c in cases:
        tender, secs = c.get("tender") or "", c.get("sections") or []
        got = [x["responded"] for x in
               substantive.responded_requirements(tender, secs)["items"]]
        if c.get("known_limitation"):
            known.append({"id": c["id"], "note": c.get("note") or "",
                          "business_expect": c.get("business_expect"),
                          "auto_result": got})
            continue
        checked += 1
        sc_ok, sc_got = True, None
        if "expect_scoring_covered" in c:
            sc_got = substantive.scoring_coverage(tender, secs)["covered"]
            sc_ok = sc_got == c["expect_scoring_covered"]
        ok = got == list(c.get("expect") or []) and sc_ok
        if ok:
            passed += 1
        else:
            misses.append({"id": c["id"], "expect": c.get("expect"), "got": got,
                           "scoring": sc_got})
    return {
        "annotation": {
            "source": "manual_starter_unreviewed",
            "cases_file": "backend/eval/cases_substantive.jsonl",
            "cases": len(cases), "reviewed": False,
            "independence": "开发者构造的起步集，未经专家标注，属同源自测",
            "method": "词面命中 + 明确响应表述 + 要求数值逐字出现；不评语义与方案优劣",
        },
        "metrics": {"cases": checked, "passed": passed,
                    "agreement": round(passed / max(1, checked), 4),
                    "known_limitations": len(known)},
        "known_limitations": known,
        "sample_misses": misses,
        "all_passed": checked > 0 and passed == checked,
        "scope": "逐条实质响应自动启发式与评分项覆盖的回归冒烟",
        "claim": "照抄要求仍可能被判为已响应；未经专家复核，不构成标书质量能力证明",
    }


def layer_controls() -> dict:
    """要求表人工修订优先 + 章节硬伤硬阻断；用桩函数，不碰业务库。"""
    from backend.app import quality as app_quality, reqtable
    checks: dict[str, bool] = {}
    orig = (store.list_requirements, store.get_project,
            store.list_sections, store.list_section_quality,
            app_quality.project_sources)
    try:
        store.list_requirements = lambda pid: [
            {"id": "a", "project_id": pid, "kind": "hard", "text": "自动要求原文",
             "score": "", "source": "auto", "status": "active", "note": ""},
            {"id": "b", "project_id": pid, "kind": "hard", "text": "自动要求原文",
             "score": "", "source": "manual", "status": "active", "note": ""},
            {"id": "c", "project_id": pid, "kind": "hard", "text": "人工停用",
             "score": "", "source": "manual", "status": "disabled", "note": ""},
        ]
        eff = reqtable.effective("eval")
        checks["人工修订优先且停用生效"] = (
            len(eff["hard"]) == 1 and eff["hard"][0]["source"] == "manual"
            and "人工修订版" in reqtable.constraints_text("eval"))
        r = app_quality.assess(
            "", [{"title": "技术方案与逐条响应",
                  "text": "我们提供 ISO 认证证书并加盖公章，随投标文件一并提交，接受招标人核查。"}],
            [], ["招标文件须提交近三年审计报告与社保缴纳证明"],
            [{"item": "技术方案完整性", "score": "10"}])
        checks["要求表进入覆盖度/实质响应/评分项"] = (
            r["coverage"]["total"] == 1 and not r["flags"]["coverage"]
            and r["substantive"]["requirements"]["total"] == 1
            and r["substantive"]["scoring"]["available"] is True)

        dirty = {"id": "s1", "title": "技术方案与逐条响应",
                 "text": "本章提供 XXX 占位内容，故障响应 99.9% 达标，TODO 待补充。"
                         "我们承诺按招标文件要求提供全部证明材料并接受核查，材料随投标文件一并提交。"}
        clean = {"id": "s2", "title": "售后服务与运维保障",
                 "text": "我们提供完整的售后服务方案并加盖公章，建立服务台账，"
                         "安排专人负责响应与记录，随投标文件一并提交，接受招标人核查与验收。"
                         "服务内容、责任分工与验收口径均按招标文件条款逐条落实。"}
        app_quality.project_sources = lambda pid: []
        store.get_project = lambda pid: {"id": pid, "tender_text": "",
                                        "sections": [dirty, clean]}
        store.list_sections = lambda pid: [dirty, clean]
        store.list_section_quality = lambda pid: []
        blockers = app_quality.section_blockers("eval")
        checks["占位符/无据数值章节被硬阻断"] = (
            len(blockers) == 1 and blockers[0]["blocking"]
            and blockers[0]["section_id"] == "s1")
        store.get_project = lambda pid: {"id": pid, "tender_text": "",
                                        "sections": [clean]}
        store.list_sections = lambda pid: [clean]
        checks["干净章节不被误阻断"] = not app_quality.section_blockers("eval")
    except Exception as exc:  # noqa: BLE001
        checks["控制层异常"] = False
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = ""
    finally:
        (store.list_requirements, store.get_project,
         store.list_sections, store.list_section_quality,
         app_quality.project_sources) = orig
    return {"checks": checks, "all_passed": all(checks.values()),
            "error": error,
            "scope": "配置与门禁契约的确定性回归，不含模型与真实标书",
            "claim": "只验证人工修订优先生效、未达标章节会硬阻断，不构成能力证明"}


def evaluate() -> dict:
    r = layer_rules()
    g = layer_grounding()
    t = layer_trajectory()
    q = layer_quality()
    s = layer_substantive()
    c = layer_controls()
    fx = fixtures.status()
    cases = load_cases()
    limitations = [
        "样本为起步标注集，未经招投标领域专家复核，不能外推到真实标书",
        "样本量小，按 stratum / split 的分数仅供参考，不构成统计结论",
        "评分与规则实现解耦（看证据位置而非内部 code），但仍是同一套被测规则的回归测试",
        "真实模型端到端链路见 trajectory 层 / e2e_live.py",
        "质量层样本为开发者构造的起步集，未由第三方专家标注，属同源自测；"
        "词面覆盖不等于实质响应，文采 / 方案优劣 / 评分办法得分未覆盖",
        "第 5 层逐条实质响应是自动启发式：连响应词一起照抄要求仍可能判为已响应，"
        "已单列为已知局限，不混入通过率",
        f"当前真实标书样本 {fx['real_samples']} 份、脱敏 {fx['desensitized']} 份、"
        f"标注完成 {fx['annotation_complete']} 份、经专家复核 {fx['expert_annotated']} 份；"
        "能力证明口径不成立（--import-tender / --import-bid → --annotate → 人工复核 → --baseline）",
    ]
    if t.get("status") == "not_run":
        limitations.append("live 端到端尚未运行：python backend/eval/e2e_live.py")
    elif not t.get("model_live_verified"):
        err = str((t.get("model_probe") or {}).get("error") or "未知原因")[:120]
        limitations.append(f"live 验收时真实模型不可用（{err}），只验证了降级与门禁契约")
    return {
        "version": "5.0",
        "annotation": {
            "source": "manual_starter_unreviewed",
            "cases_file": "backend/eval/cases.jsonl",
            "cases": len(cases),
            "quality_cases": len(load_quality_cases()),
            "substantive_cases": s["annotation"]["cases"],
            "reviewed": any(c.get("reviewed") for c in cases),
            "splits": sorted({c.get("split") for c in cases}),
            "strata": sorted({c.get("stratum") for c in cases}),
        },
        "metrics": r["metrics"], "targets": TARGETS,
        "passed": r["passed"],
        "all_passed": (r["all_passed"] and g["all_passed"]
                       and q["all_passed"] and s["all_passed"]
                       and c["all_passed"]),
        "layers": {"rules": {k: v for k, v in r.items() if k != "metrics"},
                   "grounding": g, "trajectory": t, "quality": q,
                   "substantive": s, "controls": c},
        "evidence": {
            "fixtures_dir": fx["fixtures_dir"],
            "real_samples": fx["real_samples"],
            "desensitized": fx["desensitized"],
            "annotation_complete": fx["annotation_complete"],
            "expert_annotated": fx["expert_annotated"],
            "gold_reviewed": fx["gold_reviewed"],
            "baseline_report": str(fixtures.BASELINE),
            "how_to": ("python -m backend.eval.fixtures --import-tender <文件> --id <id> / "
                       "--import-bid <文件> --id <id> → --annotate → 人工复核 → --baseline"),
            "claim": fx["claim"],
        },
        "sample_misses": r["sample_misses"],
        "scope": ("确定性规则层 + 检索证据层 + 生成正文自动硬指标 + 逐条实质响应的回归冒烟；"
                  "模型生成质量的文采 / 方案优劣 / 评分得分、OCR 准确率未覆盖"),
        "limitations": limitations,
        "claim": "回归冒烟通过，不构成真实标书上的能力证明",
    }


def main() -> int:
    report = evaluate()
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    m = report["metrics"]
    print("== 分层回归评测 ==")
    print(f"标注用例 {m['cases']} 条（起步集，未专家复核）| 应报 {m['should_report']} | 报出 {m['total_reported']}")
    for key, label in (("recall", "检出召回"), ("false_positive_rate", "误报率"),
                       ("localization", "定位准确率")):
        mark = "PASS" if report["passed"][key] else "FAIL"
        print(f"  {label:<8} {m[key]:.4f}  目标 "
              f"{'≥' if key != 'false_positive_rate' else '≤'} {TARGETS[key]:.2f}  {mark}")
    print("  证据层:", "PASS" if report["layers"]["grounding"]["all_passed"] else "FAIL",
          report["layers"]["grounding"]["checks"])
    q = report["layers"]["quality"]["metrics"]
    print("  质量层:", "PASS" if report["layers"]["quality"]["all_passed"] else "FAIL",
          f"缺陷检出 {q['detected']}/{q['defective']}，误报样本 {q['false_positive_cases']}/{q['cases']}"
          "（自动硬指标，不构成能力证明）")
    s = report["layers"]["substantive"]["metrics"]
    print("  实质响应层:", "PASS" if report["layers"]["substantive"]["all_passed"] else "FAIL",
          f"{s['passed']}/{s['cases']} 一致，已知局限 {s['known_limitations']} 项（自动启发式）")
    c = report["layers"]["controls"]
    print("  控制层:", "PASS" if c["all_passed"] else "FAIL", c["checks"], c["error"])
    e = report["evidence"]
    print("  能力证明:", f"真实样本 {e['real_samples']} 份 / 标注完成 {e['annotation_complete']} 份 / 专家复核 {e['expert_annotated']} 份 —",
          e["claim"])
    print("  基线报告:", e["baseline_report"])
    print("  轨迹层:", report["layers"]["trajectory"]["status"])
    print("结论:", "回归冒烟通过（不构成真实标书能力证明）"
          if report["all_passed"] else "未通过，看 sample_misses")
    print("报告:", OUT)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
