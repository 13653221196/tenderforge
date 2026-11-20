"""受控智能体：主线由 Python 确定性编排，模型只在步骤内部自主。

两条链路共用一个大脑（招标文件 + 我提供的资料 + 企业素材库）：
  A 根据招标书写竞标书：解读 → 大纲 → 一键整本/逐章生成
  B 验已有竞标书：导入 → 体检（带定位）→ 按问题只改命中段落 → 复检 → 前后对比
循环轮数写死、保护章节不动、每步留痕，不把"改哪里 / 改几轮"交给模型。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import (auto_core, chains, config, generation_context, guard, outline_plan, provenance,
               quality, rag, reqtable, rules, samples, section_types, store,
               writing_context)

LAST_AUTO: dict[tuple[str, str], dict] = {}
# 最近一次目录展开计划（评分项数 / 格式目录命中 / 字数预算），供界面与回归核对
LAST_OUTLINE: dict[tuple[str, str], dict] = {}
OUTLINE_STAGE = "outline_plan"

def _cache_key(pid):
    return (str(store.db_path().resolve()), str(pid))


def outline_plan_for(pid: str) -> dict:
    """目录计划：进程内有就用，服务重启后从 auto_runs 留痕里读回来。"""
    plan = LAST_OUTLINE.get(_cache_key(pid))
    if plan:
        return plan
    try:
        data = (store.last_auto_run(pid, OUTLINE_STAGE) or {}).get("data") or {}
        if data:
            LAST_OUTLINE[_cache_key(pid)] = data
            return data
    except Exception:
        pass
    return {}

# 低于这个置信度的问题不自动改写，只提示人工核对
LOW_CONFIDENCE = 0.60
# 写章自检不合格时最多定向重写 2 轮；仍不合格就标「本章未达交付标准」并列入导出提醒
FIX_MAX_ROUNDS = 2
# 单章分段续写：一次调用写不完的长章，按段续写再拼接。
# 2400 tokens 约 1800—2200 汉字，所以每段目标 2200 字、最多 6 段（约 1.3 万字）。
SECTION_PART_CHARS = 2200
SECTION_MAX_PARTS = 6
# 定向重写会把全文交给模型；超过这个长度宁可拒绝并说明，也不截断改写丢内容。
FIX_TEXT_LIMIT = 9000
# 单章 AI 改写最多送这么多字；再长请分段改写，避免整章被截断后覆盖原稿。
REWRITE_TEXT_LIMIT = 12000

DEFAULT_CHAPTERS = [
    {"title": "投标函及投标报价总说明", "points": ["投标意愿", "报价依据", "投标有效期"]},
    {"title": "投标人资格与商务响应", "points": ["营业执照与资质", "类似业绩", "商务条款"]},
    {"title": "技术方案与逐条响应", "points": ["总体架构", "关键参数响应", "实施与验收"]},
    {"title": "项目组织与实施计划", "points": ["项目团队", "进度计划", "风险控制"]},
    {"title": "售后服务与运维保障", "points": ["响应时限", "服务内容", "培训计划"]},
    {"title": "资格证明与承诺文件", "points": ["资质清单", "授权书", "承诺书"]},
    {"title": "投标报价表", "points": ["按给定格式填报"]},
]


def _llm(pid: str, task: str, variables: dict, **kw):
    """主链路的模型调用统一带项目 id，便于用量与审计按项目归集。

    真实模型可能长时间不返回；主链路每步必须能快速失败并降级到本地规则，
    所以统一给一个较短的单次超时与输出上限，避免拖垮大脑的单步超时上限。

    raise_budget=True 时把「本次生成预算用尽」原样抛给调用方：长章续写要据此
    体面收尾（保留已写内容），而不是当成模型没返回。
    """
    raise_budget = bool(kw.pop("raise_budget", False))
    kw.setdefault("timeout", 40)
    kw.setdefault("max_tokens", 1200)
    try:
        return chains.run(task, variables, pid=pid, **kw)
    except Exception as exc:
        # 真实模型超时/报错不能把整条主链路打成 500：降级为「本次没有新内容」，
        # 章节与检查记录保持原样，用量里留一条失败原因供排查。
        budget_hit = isinstance(exc, chains.TaskBudgetExceeded)
        if budget_hit and raise_budget:
            raise
        store.add_usage(pid, actor="agent", task=task + ".fallback",
                        tool="lcel." + task,
                        model="本次生成预算用尽，已降级" if budget_hit else "模型未返回，已本地降级",
                        ok=False, output_text=f"{type(exc).__name__}: {str(exc)[:100]}")
        if kw.get("json_out") or task in ("interpret", "outline", "review"):
            return {"chapters": [], "issues": []}
        return ""


def _job(jid: str, **kw) -> None:
    if jid:
        store.update_job(jid, **kw)


def _meta(pid: str) -> tuple[dict, dict]:
    p = store.get_project(pid)
    try:
        meta = json.loads(p.get("summary") or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except (ValueError, TypeError):
        meta = {"summary": p.get("summary") or ""}
    return p, meta


# ---------------- 链路 A：写 ----------------

def interpret(pid: str) -> dict:
    p = store.get_project(pid)
    text = str(p.get("tender_text") or "")
    if len(text) > 11000:
        # 真实招标文件动辄数百页，评标办法 / 否决条款 / 前附表多在中后部。
        # 只截开头 12000 字会让解读看不到分值分布与实质性条款。
        iq = ("评标办法 评分标准 分值 资格条件 实质性要求 否决投标 "
              "投标人须知前附表 技术要求 商务条款 投标文件格式")
        sem, ckeys = writing_context.semantic_for(pid, text, iq, budget=8200)
        focused, _ = writing_context.retrieve_for_chapter(
            text, iq, budget=8200, semantic=sem, chunk_keys=ckeys)
        tender = (text[:1800] + "\n\n【按关键条款检索的招标原文】\n" + focused)[:11000]
    else:
        tender = text
    data = _llm(pid, "interpret", {
        "name": p["name"],
        "tender": tender,
    }, json_out=True, max_tokens=2400)
    if not data:
        data = {"summary": "解读未完成，请检查模型配置。", "must": [], "qualification": []}
    store.update_project(pid, summary=json.dumps(data, ensure_ascii=False))
    return data


def make_outline(pid: str) -> list[dict]:
    p, meta = _meta(pid)
    # 项目指定了范例时，按范例的章节结构来组织目录（标题可改，层级与颗粒度照学）
    ref_titles: list[str] = []
    if p.get("sample_doc_id"):
        ref_titles = samples.outline_titles(p["sample_doc_id"], 20)
    data = _llm(pid, "outline", {
        "name": p["name"],
        "summary": str(meta.get("summary", ""))[:800],
        "must": "；".join(meta.get("must", []))[:1500],
        "materials": (rag.materials_block(pid)
                     + auto_core.constraints(pid, outline=True))[:5200],
        "sample_titles": ("；".join(ref_titles) if ref_titles else "（没有指定范例）"),
    }, json_out=True)
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not chapters or not isinstance(chapters, list):
        chapters = DEFAULT_CHAPTERS
    clean = [{"title": str(c.get("title", ""))[:60],
              "points": c.get("points", []) if isinstance(c, dict) else []}
             for c in chapters if isinstance(c, dict) and c.get("title")]
    if not clean:
        clean = DEFAULT_CHAPTERS
    # 模型只给骨架，章节数与篇幅由招标文件的「投标文件格式」和评分办法决定：
    # 分值高的技术项各自成章，每章带目标字数，生成时按预算写。
    # 模型解读往往只给出两三个评分项，真正的评分要素在要求表里（人工可修订）。
    # 两者合并后按分值决定哪些得分点单独成章，别让高分项没有章节认领。
    try:
        extra_scoring = reqtable.scoring_rows(pid)
    except Exception:
        extra_scoring = []
    clean, plan = outline_plan.expand(
        clean, p.get("tender_text") or "", meta,
        extra_scoring=extra_scoring)
    LAST_OUTLINE[_cache_key(pid)] = plan
    # 落一份留痕：重启后 /api/projects/{pid}/outline 仍能给出分值篇幅依据
    try:
        store.save_auto_run(pid, OUTLINE_STAGE, "目录展开计划", plan)
    except Exception:
        pass
    # 已有章节（尤其是导入的竞标书）保留原标题，只刷新要点，避免正文按序号错位
    existing = store.list_sections(pid)
    if existing:
        for i, ex in enumerate(existing):
            if i < len(clean):
                clean[i]["title"] = ex["title"]
            else:
                clean.append({"title": ex["title"], "points": []})
    store.update_project(pid, outline=clean)
    store.set_sections(pid, clean)
    return clean


PROTECTED_MSG = ("证据/表单型章节（报价表 / 投标函 / 承诺函 / 资格证明 / 授权书 / 证照）"
                 "不允许自动生成或改写，请人工核验原件后填写；资格与商务响应类"
                 "只生成要求与材料的对照清单，不代替原件")


def _protected(sec: dict) -> bool:
    # 统一走分型：资格证明/授权/承诺函等证据型章节在生成链路里也不可碰，
    # 但全局 rules.is_protected 保持不变，验标脱敏与表单核验口径不受影响。
    return section_types.is_protected(sec)


def _selfcheck(pid: str, sec: dict, text: str) -> dict:
    """写章后立刻查可自动判定的硬伤；纯只读，不写库。"""
    try:
        p = store.get_project(pid)
        return quality.assess_section(
            p.get("tender_text") or "",
            {"title": sec.get("title", ""), "text": text,
             "protected": sec.get("protected")},
            quality.project_sources(pid))
    except Exception as exc:
        # 「没查成」不是「查过没问题」：明确标 unavailable，禁止把这一章算成可交付
        return {"ok": False, "status": "unavailable", "flags": {},
                "lines": [f"章节自检没有跑完（{type(exc).__name__}: {str(exc)[:100]}）"],
                "error": f"{type(exc).__name__}: {exc}"[:160],
                "unavailable": True}


def _fix_instruction(chk: dict, round_no: int) -> str:
    """按「当前还剩哪一类缺陷」生成定向重写指令，不把三类硬伤混成一句空话。"""
    flags = chk.get("flags") or {}
    parts: list[str] = []
    if not flags.get("numbers", True):
        parts.append("删除全部未在招标文件或给定资料里逐字出现的金额、比例、期限、数量；"
                     "不得自行杜撰任何数值，确需数字时只能原样引用依据原文")
    if not flags.get("placeholders", True):
        parts.append("删除全部 XXX / TODO / 待填 / 待补充 / 【需人工补充】/（依据： / {{ }} 等"
                     "占位符与内部标注，改成可核查的具体表述，不要留任何方括号占位")
    if not flags.get("empty", True):
        parts.append(f"本章正文不足 {quality.MIN_CHARS} 字，必须补足到至少 "
                     f"{quality.MIN_CHARS * 2} 字，写清措施、责任与验收口径，不得用套话凑数")
    if not flags.get("claims", True):
        parts.append("删除全部「我方已取得 / 已具备 / 已通过 / 持有」资质、证书、"
                     "资格预审结果、授权、业绩等既成事实表述；系统里没有已核验原件，"
                     "这些一律改成「按招标文件要求提供……（以随投标文件提交的原件为准）」")
    if round_no > 1:
        parts.append("这是第二次定向重写：上一版仍有硬伤，宁可写短而全的确定内容，"
                     "也不要再出现任何数值、占位符或空泛句")
    detail = "；".join(parts or [str(x) for x in (chk.get("lines") or [])])[:760]
    return ("上一稿经自动核查未达交付标准，必须逐条修好：" + detail +
            "。只输出修好的正文，不要解释、不要标题、不要写「依据与来源」。")


def _fix_loop(pid: str, p: dict, sec: dict, text: str,
              chk: dict, generation_state=None) -> tuple[str, dict, int, bool]:
    """定向重写 → 逐轮复检；只采纳「失败项变少且没引入新缺陷」的稿子。

    返回 (正文, 最后一次复检, 重写轮数, 是否达交付标准)。修不好也如实返回，
    不把「重写过」当成「已修好」。
    """
    current, cur_chk = text, chk
    rounds = 0
    # 自检本身没跑完时，重写多少轮都还是「没查过」：不空转，直接标不可交付
    if cur_chk.get("status") == "unavailable":
        return current, cur_chk, 0, False
    while not cur_chk.get("ok", True) and rounds < FIX_MAX_ROUNDS:
        rounds += 1
        # 定向重写也要看得见本章相关招标原文，否则「补足内容」容易补出编造条款
        tend, _ = writing_context.retrieve_for_chapter(
            p.get("tender_text") or "", str(sec.get("title") or ""), budget=2600)
        attempt = generation_context.GenerationContext(
            pid, sec["id"], int(sec.get("ver") or 0),
            parent_id=generation_state[0].id if generation_state else "")
        fixed = _llm(pid, "rewrite", {
            "name": p["name"], "title": sec["title"],
            "instruction": _fix_instruction(cur_chk, rounds),
            "materials": (section_types.guard_text(section_types.NARRATIVE)
                       + rag.materials_block(pid)
                       + auto_core.constraints(pid, chapter=sec["title"])
                       + "\n\n【本章相关招标原文，补写不得与之冲突】\n" + tend)[:6200],
            "sample": samples.reference_block(pid, sec["title"])[0][:1800],
            "text": provenance.strip_note(current)[:9000],
        }, timeout=30, max_tokens=2400, generation=attempt)
        if not str(fixed).strip():
            break
        cand = provenance.clean_for_export(str(fixed).strip())
        cand_chk = _selfcheck(pid, sec, cand)
        attempt.record_quality(cand_chk)
        if cand_chk.get("status") == "unavailable":
            break  # 复检没跑完，别把「重试过」当成「查过了」
        before = {k for k, v in (cur_chk.get("flags") or {}).items() if not v}
        after = {k for k, v in (cand_chk.get("flags") or {}).items() if not v}
        improved = cand_chk.get("ok") or (len(after) < len(before) and after <= before)
        if improved:
            current, cur_chk = cand, cand_chk
            if generation_state is not None:
                generation_state[0] = attempt
        if cur_chk.get("ok"):
            break
    return current, cur_chk, rounds, bool(cur_chk.get("ok", True))


def _record_quality(pid: str, sid: str, text: str, chk: dict,
                    rounds: int, deliverable: bool) -> dict:
    """把写章自检结论落库留痕；导出门禁仍按当前正文重算，不信任这条历史记录。"""
    try:
        store.save_section_quality(
            pid, sid, quality.text_hash(text), bool(chk.get("ok")),
            chk.get("flags") or {}, chk.get("lines") or [], rounds, deliverable)
    except Exception:
        pass
    return {
        "ok": bool(chk.get("ok", True)),
        "deliverable": bool(deliverable),
        "lines": chk.get("lines") or [],
        "rewritten": rounds > 0,
        "rounds": rounds,
        "message": ("" if deliverable else
                    "本章自检没有跑完，结果不可用；不能当作已通过，请重试或人工复核"
                    if chk.get("status") == "unavailable" else
                    "本章未达交付标准：定向重写 " + str(rounds) +
                    " 轮复检仍未通过，已标记；导出时会列为重点提醒，建议重写或手工修好正文"),
    }


def _written_essence(text: str, limit: int = 1200) -> str:
    """续写前把已写正文压成要点：小标题 + 带数字/承诺的句子。

    续写段只看结尾容易和开头写过的参数打架；这里让模型看到本章已经定下的
    结构和口径，要求沿用而不是重写。
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    heads = [ln.lstrip("#").strip() for ln in lines if ln.startswith("#")][:8]
    keys: list[str] = []
    for ln in lines:
        if ln.startswith("#"):
            continue
        if any(k in ln for k in ("满足", "承诺", "保证", "不低于", "不超过",
                                 "达到", "%", "％", "工作日", "小时")):
            keys.append(ln[:70])
        if len(keys) >= 6:
            break
    items = [f"- {h}" for h in heads if h] + [f"- {k}" for k in keys]
    return "\n".join(items)[:limit] or "（暂无）"


def _write_section_body(pid: str, p: dict, sec: dict, *, points: str,
                        ctx: dict, materials: str, kb: str, sample_block: str,
                        hits: list, target_words: int, base_ver: int,
                        generation, abort_check=None, brief: str = "") -> str:
    """按篇幅预算分段写一章；长章续写而不是一次硬凑。

    每次调用只写一段，续写只带上一段结尾，避免重复；预算用尽就保留已写内容。
    返回拼接后的全文（可能短于目标，绝不为了凑字数编造）。
    """
    total = max(0, int(target_words or 0))
    parts = (1 if total <= SECTION_PART_CHARS else
             min(SECTION_MAX_PARTS, -(-total // SECTION_PART_CHARS)))
    text = ""
    for i in range(parts):
        if i and callable(abort_check) and abort_check():
            break
        run = generation
        if i:
            # generation_runs 用 id 做主键，续写段必须单开一条并挂到首段下
            run = generation_context.GenerationContext(
                pid, sec["id"], base_ver, parent_id=generation.id)
            run.hints = hits
        words = min(SECTION_PART_CHARS, max(600, total - len(text))) if total else 1600
        ask = brief or "（无，按章节要点与招标要求写）"
        if i == 0:
            variables = {
                "name": p["name"], "title": sec["title"],
                "points": points or "按招标文件要求逐项响应",
                "words": words, "brief": ask,
                "tender": ctx["tender"], "materials": materials,
                "kb": kb[:3500], "sample": sample_block[:3000],
            }
            task = "section"
        else:
            variables = {
                "name": p["name"], "title": sec["title"],
                "points": points or "按招标文件要求逐项响应",
                "words": words, "brief": ask, "tail": text[-1200:],
                "written": _written_essence(text),
                "tender": ctx["tender"], "materials": materials,
                "kb": kb[:2000],
            }
            task = "section_continue"
        try:
            part = _llm(pid, task, variables, max_tokens=2400, generation=run,
                        raise_budget=True)
        except chains.TaskBudgetExceeded:
            # 预算用尽：已写的段落保留，交给上层标记「未写满」，不重试
            break
        part = str(part or "").strip()
        if not part:
            break
        text = (text + "\n\n" + part) if text else part
        if total and len(text) >= total:
            break
    return text


def generate_section(pid: str, sid: str, snapshot: bool = True,
                     expect_ver: int | None = None,
                     abort_check=None, prev_brief: str = "",
                     requirement_text: str = "", brief: str = "") -> dict:
    """生成单章。

    expect_ver 有值时为乐观写：生成开始前看到的版本和落库时不一致，就不覆盖，
    返回 {"conflict": True}。整本生成用它防止「生成途中人工改过」被模型稿冲掉。
    abort_check 是取消回调：模型请求返回后再查一次，已取消就不写入正文。
    prev_brief 是整本生成时前面已写章节的摘要，用来避免重复展开与口径冲突。
    requirement_text 是整本生成时按全表分配给本章的要求；单章生成留空，
    由本函数自己按章取相关要求。
    brief 是用户对这一章的口述要求（写什么、包含哪些点），只影响本章内容。
    """
    p = store.get_project(pid)
    sec = next((s for s in store.list_sections(pid) if s["id"] == sid), None)
    if not sec:
        return {"error": "章节不存在"}
    if _protected(sec):
        return {"error": PROTECTED_MSG}
    kind = section_types.classify(sec)
    # 生成要几十秒，开始时就把版本记下来：落库时版本对不上，说明期间有人手改过
    base_ver = int(expect_ver if expect_ver is not None else (sec.get("ver") or 0))
    chapter = next((c for c in p.get("outline", []) if c.get("title") == sec["title"]), {})
    points = "、".join(chapter.get("points", [])) if isinstance(chapter.get("points"), list) else str(chapter.get("points", ""))
    try:
        target_words = int(chapter.get("words") or 0)
    except (TypeError, ValueError):
        target_words = 0
    # 本章要响应的招标要求：整本生成用全局分配结果（每条要求都有章节认领），
    # 单章生成回退到按章取相关条款；两者都参与招标原文与资料检索
    related = requirement_text or auto_core.constraints(pid, chapter=sec["title"])
    query = sec["title"] + " " + points + " " + related[:300] + " " + (brief or "")[:200]
    kb, hits = rag.retrieve(pid, query)
    # 范例参照：知识库里整本标书按章拆过，这里找「同一章」学结构与写法
    sample_block, ref = samples.reference_block(pid, sec["title"], points)
    if ref:
        hits = [{"title": f"{ref['doc_title']} · {ref['chapter']}", "scope": "标书范例",
                 "snippet": (ref["content"] or "")[:160], "text": (ref["content"] or "")[:400],
                 "doc_id": ref["doc_id"], "page": 0, "start": 0, "end": 0,
                 "ref": "范例章节（学结构，不照抄事实）"}] + hits
    # 招标原文按本章相关度检索后注入，不再每章都塞同一份文件开头 6000 字；
    # notes 里含检索覆盖说明、已核验企业事实与已写章节摘要，必须完整保留。
    ctx = writing_context.for_chapter(
        pid, p, sec["title"], points, requirements=related,
        prev_brief=prev_brief)
    # notes（检索覆盖说明 / 企业事实 / 已写章节摘要）必须在末尾完整保留；
    # 先给它留出位置，再把资料与本章认领的要求尽量装满剩余预算。
    notes = ctx["notes"]
    if target_words and kind != section_types.PROTECTED:
        notes += (f"\n\n【本章篇幅预算】按评分办法与章节性质分配约 {target_words} 字。"
                  "按「章节要点」与「本章认领的招标要求」逐条展开；宁可写短而全，"
                  "也不要为凑字数重复内容或引入未展示的条款。")
    # 事实边界放在最前面且完整保留：模型不能拿「资料里没有」当「可以推测」。
    # 预算里先扣掉边界与末尾说明，再让资料与要求清单尽量装满剩余空间。
    boundary = section_types.guard_text(kind)
    room = max(2400, 9000 - len(notes) - len(boundary) - 200)
    head = (rag.materials_block(pid) + "\n" + related)[:room]
    materials = boundary + "\n" + head + notes
    generation = generation_context.GenerationContext(pid, sid, base_ver)
    generation.hints = hits
    if kind == section_types.STRUCTURED:
        # 资格/商务响应类不调模型：只生成「要求 → 材料 → 待核验」的清单骨架，
        # 避免模型写出「我方已取得资格预审结果通知书」这种无法核查的虚标承诺。
        consumed = {"title": sec["title"], "points": points,
                    "requirements": related, "facts": ctx["facts"]}
        generation.capture("structured", consumed, consumed, [], {"type": "deterministic"})
        text = section_types.structured_draft(
            consumed["title"], consumed["points"], consumed["requirements"], consumed["facts"])
        generation.finish(text, "succeeded" if str(text or "").strip() else "failed")
    else:
        text = _write_section_body(
            pid, p, sec, points=points, ctx=ctx, materials=materials, kb=kb,
            sample_block=sample_block, hits=hits, target_words=target_words,
            base_ver=base_ver, generation=generation, abort_check=abort_check,
            brief=brief)
    # C03：取消发生在模型等待期间时，返回的内容也不落库。
    # 远端计算已经发生，取消不撤销它；这里只保证不写进本项目正文。
    if callable(abort_check) and abort_check():
        generation.finish(text, "cancelled")
        return {"text": "", "cancelled": True, "candidate": str(text or ""),
                "sources": hits,
                "message": "任务已取消：本章模型返回的内容没有写入正文；"
                           "远端计算已发生，不会被撤销"}
    if not str(text).strip():
        return {"text": "", "sources": hits, "used_materials": [], "fallback": True,
                "message": "模型未返回正文（可能超时，或思考型模型把 token 全用于推理），本章保持原样；可稍后重试或换一个模型"}
    refs = generation.sources
    text = provenance.attach(str(text).strip(), refs)
    chk = _selfcheck(pid, sec, text)
    generation.record_quality(chk)
    if kind == section_types.STRUCTURED:
        # 清单骨架不该交给模型定向重写：重写会把「待人工核验」润色成既成事实。
        # 不合格就如实标记，由人补原件，不制造「重写过所以改好了」的假象。
        rounds, deliverable = 0, bool(chk.get("ok"))
    elif len(text) > FIX_TEXT_LIMIT:
        # 长章交给模型整篇重写会被截断，反而丢内容；这里只如实标记，交人工处理
        rounds, deliverable = 0, bool(chk.get("ok"))
    else:
        state = [generation]
        text, chk, rounds, deliverable = _fix_loop(pid, p, sec, text, chk, state)
        generation = state[0]
    batch_id = store.new_id() if snapshot else ""
    if snapshot:
        store.snapshot_sections(pid, batch_id, "生成本章前", [sid])
    if text:
        res = store.commit_generation(pid, sid, text, base_ver, generation.id,
                                      quality=chk, abort_check=abort_check)
        if not res.get("ok"):
            why = res.get("error")
            if why == "cancelled":
                return {"text": "", "cancelled": True, "candidate": text}
            if why == "conflict":
                # 人工稿优先：模型稿原样退回给界面看，绝不写成「已生成成功」
                return {"text": "", "conflict": True, "sources": hits,
                        "candidate": text, "server_text": res.get("text") or "",
                        "message": "生成期间这一章被改过，本次结果没有写入，保留你手上的版本"}
            if why == "busy":
                return {"text": "", "error": "数据库正忙，本次结果没有写入，正文保持原样；请稍后重试",
                        "candidate": text}
            return {"text": "", "error": "章节不存在"}
    used = [m["title"] for m in store.list_docs(scope="project", project_id=pid, usable_only=True)]
    return {"text": text, "sources": hits, "used_materials": used,
            "batch_id": batch_id, "section_kind": kind,
            "section_reason": section_types.REASON.get(kind, ""),
            "quality": _record_quality(pid, sid, text, chk, rounds, deliverable)}


def rewrite_section(pid: str, sid: str, instruction: str) -> dict:
    p = store.get_project(pid)
    sec = next((s for s in store.list_sections(pid) if s["id"] == sid), None)
    if not sec:
        return {"error": "章节不存在"}
    if _protected(sec):
        return {"error": PROTECTED_MSG}
    kind = section_types.classify(sec)
    if kind == section_types.STRUCTURED:
        return {"error": "资料清单型章节须人工核验原件后编辑，不允许模型改写："
                         "改写会把「待人工核验」润色成既成事实，形成虚标风险",
                "batch_id": ""}
    base_ver = int(sec.get("ver") or 0)  # 改写开始前定版本，期间人工改过就不覆盖
    existing = str(sec.get("text") or "")
    if not existing.strip():
        # 空章：当作「按描述写这一章」，走完整的分章生成链路（含篇幅预算与分段落续写）
        return generate_section(pid, sid, brief=instruction, expect_ver=base_ver)
    if len(existing) > REWRITE_TEXT_LIMIT:
        return {"text": "", "batch_id": "",
                "error": f"这一章已有 {len(existing):,} 字，超过单次 AI 改写上限 "
                         f"{REWRITE_TEXT_LIMIT:,} 字。请分段改写或先删减，"
                         "避免整章被截断后覆盖原稿。"}
    generation = generation_context.GenerationContext(pid, sid, base_ver)
    sample_block, ref = samples.reference_block(pid, sec["title"], instruction or "")
    related = auto_core.constraints(pid, chapter=sec["title"])
    rq = sec["title"] + " " + (instruction or "") + " " + related[:300]
    sem, ckeys = writing_context.semantic_for(
        pid, p.get("tender_text") or "", rq, budget=6000)
    tend, cover = writing_context.retrieve_for_chapter(
        p.get("tender_text") or "", rq, budget=6000,
        semantic=sem, chunk_keys=ckeys)
    # 改写前同样要看得见本章相关招标原文，不能只对着旧正文和资料改措辞
    boundary = section_types.guard_text(kind)
    head = (rag.materials_block(pid) + related
            + "\n\n【本章相关招标原文，改写不得与之冲突】\n" + tend)[:max(3600, 5200 - len(boundary))]
    materials = (boundary + "\n" + head
                 + ("\n\n" + cover["note"] if cover.get("note") else ""))
    text = _llm(pid, "rewrite", {
        "name": p["name"],
        "title": sec["title"],
        "instruction": instruction or "补充可核查的依据，删除空泛承诺",
        "materials": materials,
        "sample": sample_block[:2600],
        "text": provenance.strip_note(sec.get("text") or "")[:REWRITE_TEXT_LIMIT],
    }, max_tokens=2400, generation=generation)
    if not str(text).strip():
        return {"text": "", "batch_id": "", "fallback": True,
                "message": "模型未返回正文（可能超时，或思考型模型把 token 全用于推理），本章保持原样；可稍后重试或换一个模型"}
    batch_id = store.new_id()
    store.snapshot_sections(pid, batch_id, "按资料改写本章前", [sid])
    refs = generation.sources
    text = provenance.attach(str(text).strip(), refs)
    chk = _selfcheck(pid, sec, text)
    state = [generation]
    text, chk, rounds, deliverable = _fix_loop(pid, p, sec, text, chk, state)
    generation = state[0]
    if text:
        res = store.commit_generation(pid, sid, text, base_ver, generation.id, quality=chk)
        if not res.get("ok"):
            if res.get("error") == "conflict":
                return {"text": "", "batch_id": batch_id, "conflict": True,
                        "candidate": text, "server_text": res.get("text") or "",
                        "message": "改写期间这一章被改过，本次结果没有写入，保留你手上的版本"}
            if res.get("error") == "busy":
                return {"text": "", "batch_id": batch_id,
                        "error": "数据库正忙，本次结果没有写入，正文保持原样；请稍后重试",
                        "candidate": text}
            return {"text": "", "batch_id": batch_id, "error": "章节不存在"}
    return {"text": text, "batch_id": batch_id,
            "quality": _record_quality(pid, sid, text, chk, rounds, deliverable)}


def compose(pid: str, jid: str, overwrite: bool = False) -> None:
    """一键生成整本：按大纲顺序跑，单章失败记录并继续。

    只有正文真正落库的章节才计入「已写」；模型没返回、生成期间被人工改过、
    异常中断、未达交付标准分别单独计数，任务状态据此标 done / partial / failed，
    不再把「跑完了循环」当成「整本写完了」。
    """
    from . import taskctl
    _job(jid, status="running", progress=0, step="准备生成",
         started_at=store.now(), finished_at="", cancel_requested=0,
         recoverable=0)
    try:
        store.reset_task_tokens(pid)
        p = store.get_project(pid)
        if not p.get("outline"):
            make_outline(pid)
        sections = store.list_sections(pid)
        # 整本生成前一次性存全部待写章节，撤销时整本一起回退
        batch_id = store.new_id()
        targets = [s["id"] for s in sections
                   if not _protected(s)]
        store.snapshot_sections(pid, batch_id, "一键成稿前", targets)
        blocked: set[str] = set()
        if overwrite:
            for s in sections:
                if _protected(s):
                    continue
                # 清空也是写：版本对不上说明有人刚改过这一章，别先把它抹掉
                r = store.update_section(s["id"], "", base_ver=int(s.get("ver") or 0), pid=pid)
                if not r.get("ok"):
                    blocked.add(s["id"])
            sections = store.list_sections(pid)
        total = len(sections) or 1
        counts = {"written": 0, "skipped": 0, "empty": 0, "conflict": 0,
                  "failed": 0, "undeliverable": 0, "human": 0, "manual": 0,
                  "cancelled": 0}
        report: list[dict] = []
        failed: list[str] = []
        undeliverable: list[str] = []
        # 跨章一致性：把已写章节的标题与开头摘给后续章节，避免每章都从
        # 「编制依据」重写一遍、或对同一参数给出互相矛盾的口径
        briefs: list[str] = []
        # 要求表按全表一次性分配到章节：每条要求至少有一章负责响应，
        # 不再让每章各自按字面相关度重复挑同一批前 12 条
        assignment = writing_context.assigned_requirements(
            pid, [s["title"] for s in sections
                  if not _protected(s)])
        manual_titles = [sec["title"] for sec in sections
                         if _protected(sec)]
        for sec in sections:
            if sec["id"] in blocked:
                counts["conflict"] += 1
                taskctl.record(pid, jid, sec["id"], "conflict", sec.get("ver"),
                               "覆盖前发现这一章刚被改过，未清空、未重写")
                report.append({"title": sec["title"], "outcome": "conflict",
                               "note": "覆盖前发现这一章刚被改过，未清空、未重写"})
            elif _protected(sec):
                # 保护章节是待人工任务，不是「跳过 = 完成」
                counts["manual"] += 1
                taskctl.record(pid, jid, sec["id"], "manual", sec.get("ver"),
                               "保护章节需人工填写")
                report.append({"title": sec["title"], "outcome": "manual",
                               "note": "报价/投标函/承诺/资质类保护章节，需人工填写与核验，不参与 AI 生成"})
        cancelled_at = -1
        for i, sec in enumerate(sections):
            # C03：取消只在章节边界生效；已经发出的模型请求由 generate_section
            # 的 abort_check 兜住，返回后也不写入正文
            if taskctl.should_stop(jid):
                cancelled_at = i
                break
            if sec["id"] in blocked:
                continue
            if _protected(sec):
                continue
            if (sec.get("text") or "").strip():
                counts["skipped"] += 1
                briefs.append(f"《{sec['title']}》已写：{(sec.get('text') or '')[:90]}")
                taskctl.record(pid, jid, sec["id"], "skipped", sec.get("ver"),
                               "已有正文，未覆盖")
                report.append({"title": sec["title"], "outcome": "skipped",
                               "note": "已有正文，未覆盖"})
                continue
            taskctl.record(pid, jid, sec["id"], "running", sec.get("ver"),
                           "整本生成开始")
            _job(jid, progress=int(i / total * 100),
                 step=f"正在生成 {i + 1}/{total}：{sec['title']}")
            try:
                r = generate_section(pid, sec["id"], snapshot=False,
                                     expect_ver=sec.get("ver"),
                                     abort_check=lambda: taskctl.should_stop(jid),
                                     prev_brief="\n".join(briefs[-6:]),
                                     requirement_text=assignment.get(sec["title"], ""))
            except Exception as exc:  # 单章失败不中断整本
                counts["failed"] += 1
                taskctl.record(pid, jid, sec["id"], "failed", sec.get("ver"),
                               str(exc)[:120])
                msg = f"{sec['title']}: {str(exc)[:80]}"
                failed.append(msg)
                report.append({"title": sec["title"], "outcome": "failed", "note": str(exc)[:120]})
                continue
            r = r if isinstance(r, dict) else {}
            if r.get("cancelled"):
                counts["cancelled"] += 1
                taskctl.record(pid, jid, sec["id"], "cancelled", sec.get("ver"),
                               "模型返回后收到取消，结果未写入正文")
                report.append({"title": sec["title"], "outcome": "cancelled",
                               "note": "已取消，模型返回的内容没有写入；远端计算不会被撤销"})
                cancelled_at = i + 1
                break
            if r.get("conflict"):
                counts["conflict"] += 1
                taskctl.record(pid, jid, sec["id"], "conflict", sec.get("ver"),
                               "生成期间被人工改过，保留人工稿")
                report.append({"title": sec["title"], "outcome": "conflict",
                               "note": "生成期间你改过这一章，已保留你的版本"})
                continue
            if r.get("error"):
                counts["failed"] += 1
                taskctl.record(pid, jid, sec["id"], "failed", sec.get("ver"),
                               str(r["error"])[:120])
                failed.append(f"{sec['title']}: {str(r['error'])[:80]}")
                report.append({"title": sec["title"], "outcome": "failed",
                               "note": str(r["error"])[:120]})
                continue
            # 先看本次模型有没有真的产出，再看库里有没有正文。
            # 模型空响应期间用户自己补了稿，库里非空也不能算「AI 生成成功」。
            candidate = (r.get("text") or "").strip()
            saved = (store.get_section(sec["id"]).get("text") or "").strip()
            if not candidate:
                if saved:
                    counts["human"] += 1
                    taskctl.record(pid, jid, sec["id"], "human", sec.get("ver"),
                                   "模型未返回，章节已有人工补稿")
                    report.append({"title": sec["title"], "outcome": "human",
                                   "note": "模型未返回正文，但章节已有正文（人工补稿），未计入 AI 生成"})
                else:
                    counts["empty"] += 1
                    taskctl.record(pid, jid, sec["id"], "empty", sec.get("ver"),
                                   "模型未返回正文，本章为空")
                    report.append({"title": sec["title"], "outcome": "empty",
                                   "note": r.get("message") or "模型未返回正文，本章为空"})
                continue
            if not saved:
                counts["empty"] += 1
                taskctl.record(pid, jid, sec["id"], "empty", sec.get("ver"),
                               "模型给了稿但没能落库")
                report.append({"title": sec["title"], "outcome": "empty",
                               "note": "模型给了稿但没能落库，本章仍为空"})
                continue
            counts["written"] += 1
            briefs.append(f"《{sec['title']}》已写：{saved[:90]}")
            q = r.get("quality") or {}
            if q and not q.get("deliverable", True):
                counts["undeliverable"] += 1
                undeliverable.append(sec["title"])
                taskctl.record(pid, jid, sec["id"], "undeliverable", sec.get("ver"),
                               "已写入但未达交付标准")
                report.append({"title": sec["title"], "outcome": "undeliverable",
                               "note": "已写入，但未达交付标准，导出前需重写或人工补齐"})
            else:
                taskctl.record(pid, jid, sec["id"], "written", sec.get("ver"),
                               f"已写入 {len(saved)} 字")
                report.append({"title": sec["title"], "outcome": "written",
                               "note": f"{len(saved)} 字"})
            _job(jid, progress=int((i + 1) / total * 100))
        if cancelled_at >= 0:
            for sec in sections[cancelled_at:]:
                if sec["id"] in blocked or _protected(sec):
                    continue
                taskctl.record(pid, jid, sec["id"], "cancelled", sec.get("ver"),
                               "收到取消请求，后续章节未调用模型、未写入正文")
        try:
            auto = auto_core.run_stage("compose", pid)
            auto_card = auto_core.as_card(auto)
        except Exception:
            auto_card = {}
        # 整本写完立刻出健康报告：雷同 / 模拟评标 / 事实 / 引用 / 合规边界
        # 都在这次跑完并留痕，导出门禁复用同一份结论，不另算一套。
        from . import health
        try:
            health_report = health.assess(pid)
            health.save(pid, health_report)
        except Exception as exc:
            health_report = {
                "ok": False, "complete": False, "checks": {}, "metrics": {},
                "blocking": [{"title": "自动体检未完成",
                              "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}],
                "summary": f"自动体检未完成（{type(exc).__name__}），需人工复核"}
        health_card = {
            "ok": bool(health_report.get("ok")),
            "complete": bool(health_report.get("complete")),
            "deliverable_ready": bool(health_report.get("deliverable_ready")),
            "summary": health_report.get("summary") or "",
            "checks": health_report.get("checks") or {},
            "metrics": health_report.get("metrics") or {},
            "blocking": [{"title": str(x.get("title") or ""),
                          "detail": str(x.get("detail") or "")[:200]}
                         for x in (health_report.get("blocking") or [])[:5]],
        }
        # 统一计数口径：任务内的 undeliverable 只数「这次写过的章」，而体检 /
        # readiness / 门禁说的是「整本」。两套数字并列返回、各自标名，文案一律
        # 用整本口径，用户才不会看到「本次 1 章」和「整本 7 章」打架。
        try:
            whole_bad = sum(1 for s in quality.section_status(pid)
                            if not s.get("deliverable"))
        except Exception:
            whole_bad = int((health_report.get("metrics") or {})
                            .get("undeliverable_sections") or 0)
        hm = health_report.get("metrics") or {}
        matrix_required = int(hm.get("requirements") or 0)
        matrix_rows = int(hm.get("matrix_rows") or 0)
        used = store.task_tokens(pid)
        budget = config.budget()
        cap = int(budget.get("per_task_token_limit") or 24000)
        degraded = bool(budget.get("enabled")) and used >= cap
        # 有没写成的章节就不能算完成：写过的标 partial，一章没写成的标 failed
        bad = (counts["failed"] + counts["empty"] + counts["conflict"]
               + counts["undeliverable"])
        cancelled = cancelled_at >= 0
        if cancelled:
            status = "cancelled"
        elif not bad:
            status = "done"
        elif counts["written"] or counts["skipped"] or counts["human"]:
            status = "partial"
        else:
            status = "failed"
        parts = [f"已写 {counts['written']} 章"]
        for key, label in (("skipped", "跳过已有正文"), ("empty", "模型未返回"),
                           ("conflict", "被人工改过"), ("failed", "出错"),
                           ("undeliverable", "未达交付标准"),
                           ("human", "人工补稿"), ("manual", "保护章节待人工"),
                           ("cancelled", "已取消")):
            if counts[key]:
                parts.append(f"{label} {counts[key]} 章")
        step = ("整本生成已取消（后续章节未再调用模型）：" if cancelled else
                "整本生成完成：" if status == "done" else
                "整本生成部分完成：" if status == "partial" else
                "整本生成失败：") + "，".join(parts)
        # 任务跑完 ≠ 整本可交付：质量、要求逐条响应、附件与人工复核另有状态，
        # 生成任务不负责（也不能）宣布「已可交付」。
        delivery = {
            "deliverable": False,
            "label": "已取消 · 正文未按计划完成" if cancelled else
                     "正文已填写 · 待复核" if status != "failed" else "正文未完成",
            "reason": ("任务被取消，后续章节没有生成；取消不等于撤销已发出的远端模型计算"
                       if cancelled else
                       "质量复检、要求逐条响应、附件与人工复核都有独立状态，"
                       "即使任务跑完也不能据此判定整本可交付"),
            "manual_required": manual_titles,
            "pending_checks": ["质量复检", "要求逐条响应", "附件与原件", "人工复核"],
        }
        if not cancelled and status != "failed":
            step += f"；交付状态：{delivery['label']}（不等于可交付）"
        facts = []
        if whole_bad:
            facts.append(f"{whole_bad} 章未达交付标准")
        if matrix_required and matrix_rows < matrix_required:
            facts.append(f"必需要求 {matrix_required} 条 / 逐条响应矩阵仅 {matrix_rows} 条")
        empty_n = counts["empty"] + counts["failed"] + counts["conflict"]
        if empty_n:
            facts.append(f"{empty_n} 章没写成")
        if health_card["blocking"]:
            health_brief = "有重点提醒，见健康报告"
        elif not health_card["complete"]:
            health_brief = "有项目没查成"
        elif facts:
            health_brief = "已跑完，但整本仍" + "、".join(facts)
        else:
            health_brief = "已跑完，无重点提醒"
        step += "；自动体检：" + health_brief
        _job(jid, status=status, progress=100, step=step,
             finished_at=store.now(),
             error="" if status in ("done", "cancelled") else
                   ("没有章节写成功：" + "，".join(p for p in parts)
                    if status == "failed" else ""),
             result={"generated": counts["written"], "skipped": counts["skipped"],
                     "failed": failed, "counts": counts, "chapters": report,
                     "status": status, "delivery": delivery,
                     "manual_required": manual_titles,
                     "batch_id": batch_id, "auto": auto_card,
                     "health": health_card,
                     "cancelled": cancelled,
                     "cancel_note": ("取消只在章节边界生效；已经发出的模型请求"
                                     "不撤销远端计算，结果返回后不写入正文"
                                     if cancelled else ""),
                     "undeliverable": undeliverable,
                     "undeliverable_total": whole_bad,
                     "matrix_required": matrix_required,
                     "matrix_rows": matrix_rows,
                     "counts_note": ("undeliverable 只数本次写入的章；"
                                     "undeliverable_total / 矩阵条数是整本口径"),
                     "task_tokens": used, "budget_degraded": degraded,
                     # 导出契约是「提醒 + 确认后仍可下载」，旧文案写「不允许导出」
                     # 与实际行为相反，用户无法判断真实约束。
                     "manual_followup": (
                         ((f"整本现有 {whole_bad} 章未达交付标准" if whole_bad else
                           "有章节未达交付标准")
                          + "，请在「写标书」里重写或手工补齐；导出时会列出重点提醒，"
                            "确认后仍可下载，但文件不可直接提交投标")
                         if undeliverable else
                         "单任务 token 用尽，后续章节降级为摘要 + 人工补充；"
                         "请到「用量」核对后手工补齐" if degraded else
                         "有章节没写成，可在「写标书」里对空章节点「生成本章」重试"
                         if bad else "")})
    except Exception as exc:
        _job(jid, status="failed", error=str(exc)[:300], step="生成失败",
             finished_at=store.now())


def compose_subset(pid: str, jid: str, sids: list[str], source_jid: str = "",
                   mode: str = "resume") -> None:
    """C03：按检查点重试失败章 / 恢复中断任务。

    绝不覆盖任务开始后的人工稿：章节有正文、版本对不上、或生成期间被改过，
    一律保留；取消请求在章节边界生效，模型返回后也不写入。
    """
    from . import taskctl
    # 恢复/重试是一次新的写任务，不能继承上次跑剩的 token 计数
    store.reset_task_tokens(pid)
    label = "失败章重试" if mode == "retry" else "中断恢复"
    _job(jid, status="running", progress=0, step=f"{label}：准备",
         started_at=store.now(), finished_at="", cancel_requested=0,
         recoverable=0)
    counts = {"written": 0, "failed": 0, "empty": 0, "conflict": 0,
              "kept_human": 0, "cancelled": 0}
    report: list[dict] = []
    cancelled = False
    try:
        total = len(sids) or 1
        for i, sid in enumerate(sids):
            if taskctl.should_stop(jid):
                cancelled = True
                for rest in sids[i:]:
                    taskctl.record(pid, jid, rest, "cancelled", 0,
                                   "收到取消请求，后续章节未调用模型")
                break
            sec = store.get_section(sid)
            if not sec or str(sec.get("project_id") or "") != str(pid):
                counts["failed"] += 1
                report.append({"id": sid, "outcome": "failed", "note": "章节不存在或不属于本项目"})
                continue
            base_ver = int(sec.get("ver") or 0)
            if str(sec.get("text") or "").strip():
                counts["kept_human"] += 1
                taskctl.record(pid, jid, sid, "kept_human", base_ver,
                               "任务开始后已有正文，保留人工稿，不覆盖")
                report.append({"id": sid, "title": sec.get("title"),
                               "outcome": "kept_human", "note": "保留人工稿"})
                continue
            _job(jid, progress=int(i / total * 100),
                 step=f"{label} {i + 1}/{total}：{sec.get('title') or ''}")
            taskctl.record(pid, jid, sid, "running", base_ver, f"{label}开始")
            try:
                r = generate_section(pid, sid, snapshot=False,
                                     expect_ver=base_ver,
                                     abort_check=lambda: taskctl.should_stop(jid))
            except Exception as exc:
                r = {"error": str(exc)[:120]}
            r = r if isinstance(r, dict) else {}
            if r.get("cancelled"):
                counts["cancelled"] += 1
                taskctl.record(pid, jid, sid, "cancelled", base_ver,
                               "模型返回后收到取消，结果未写入正文")
                cancelled = True
                for rest in sids[i + 1:]:
                    taskctl.record(pid, jid, rest, "cancelled", 0,
                                   "收到取消请求，后续章节未调用模型")
                break
            if r.get("conflict"):
                outcome = "conflict"
            elif r.get("error") or not str(r.get("text") or "").strip():
                outcome = "failed"
            elif not str(store.get_section(sid).get("text") or "").strip():
                outcome = "empty"
            else:
                outcome = "written"
            counts[outcome] += 1
            taskctl.record(pid, jid, sid, outcome, base_ver,
                           str(r.get("message") or r.get("error") or label))
            report.append({"id": sid, "title": sec.get("title"),
                           "outcome": outcome,
                           "note": str(r.get("message") or r.get("error") or "")[:120]})
            _job(jid, progress=int((i + 1) / total * 100))
        status = ("cancelled" if cancelled else
                  "done" if counts["written"] and not (
                      counts["failed"] + counts["empty"] + counts["conflict"]) else
                  "partial" if counts["written"] or counts["kept_human"] else
                  "failed")
        _job(jid, status=status, progress=100, finished_at=store.now(),
             step=f"{label}结束：写入 {counts['written']} 章、保留人工稿 "
                  f"{counts['kept_human']} 章、失败 {counts['failed']} 章"
                  + ("（已取消）" if cancelled else ""),
             result={"mode": mode, "source_job": source_jid, "counts": counts,
                     "chapters": report, "status": status,
                     "cancelled": cancelled,
                     "note": "按章节版本核对，人工稿不覆盖；取消不撤销远端计算"})
    except Exception as exc:
        _job(jid, status="failed", error=str(exc)[:300], step=f"{label}失败",
             finished_at=store.now())


# ---------------- 链路 B：验 ----------------

def _draft_text(sections: list[dict]) -> str:
    return "\n\n".join(f"【{s['title']}】\n{(s.get('text') or '')[:1500]}" for s in sections)


def check(pid: str, round_no: int = 1) -> list[dict]:
    p = store.get_project(pid)
    sections = p.get("sections", [])
    # 一份正文都没有时，通用规则会凭空报「缺少技术方案 / 报价 / 资格章节」
    # 等 6 条高风险，还写进检查轮次与导出门禁；这些条目既不可采纳也修不了。
    # 这时该提示用户先导入招标文件并写正文，而不是给一张假的问题清单。
    if not any(str(s.get("text") or "").strip() for s in sections):
        store.save_checks(pid, round_no, [])
        LAST_AUTO.pop(_cache_key(pid), None)
        return []
    issues = rules.rule_check(sections, p.get("tender_text") or "")

    # 模型复核：只补充措辞/可核查性问题，判定仍以规则为准。
    # 招标文件不再只截开头 3000 字：按硬性/废标/评分条款加权取相关片段，
    # 否则中后部的技术参数与评分办法在 AI 复核里等于不存在。
    try:
        rq = "硬性要求 废标 否决 评分办法 技术参数 商务条款 服务承诺 资格 报价 响应"
        sem, ckeys = writing_context.semantic_for(
            pid, p.get("tender_text") or "", rq, budget=9000)
        tend, _ = writing_context.retrieve_for_chapter(
            p.get("tender_text") or "", rq, budget=9000,
            semantic=sem, chunk_keys=ckeys)
        data = _llm(pid, "review", {
            "tender": tend,
            "draft": guard.draft_without_protected(sections,
                                                   per_section=2500)[:12000],
        }, json_out=True)
    except Exception:
        data = {}
    for idx, item in enumerate((data.get("issues") or [])[:4] if isinstance(data, dict) else []):
        if not isinstance(item, dict) or not item.get("title"):
            continue
        name = str(item.get("section", ""))
        sec = next((s for s in sections if name and (name in s["title"] or s["title"] in name)), None)
        if sec is None:
            sec = next((s for s in sections if not rules.is_protected(s["title"])), None)
        anchor = str(item.get("evidence", ""))[:120]
        if sec and anchor and anchor not in (sec.get("text") or ""):
            first = re.split(r"\n\s*\n", sec.get("text") or "")
            anchor = next((p[:120] for p in first if p.strip()), "")
        issues.append({
            "code": f"AI{idx + 1:02d}",
            "level": item.get("level") if item.get("level") in ("高", "中", "低") else "中",
            "title": str(item["title"])[:60],
            "detail": str(item.get("detail", ""))[:300],
            "evidence": anchor[:80],
            "section_id": sec["id"] if sec else "",
            "anchor": anchor,
            "source": "模型复核（AI 建议）",
            "confidence": 0.55,   # 低于自动改写门槛，需人工「采纳」后才进入闭环
            "source_ref": "模型复核·AI 建议，需人工核对后采纳",
        })
    # 基座能力自动编排：本地规则结论并入问题清单，随后闭环会按这些问题改写
    auto_report = {}
    try:
        auto_report = auto_core.run_stage("check", pid)
        auto_issues = [dict(i) for i in auto_report.get("issues", [])]
        # 同一段证据已在原规则里报过就不重复
        known = {re.sub(r"\W", "", (i.get("title") or ""))[:24] for i in issues}
        for it in auto_issues:
            k = re.sub(r"\W", "", it.get("title") or "")[:24]
            if k and k in known:
                continue
            known.add(k)
            it.setdefault("confidence", 0.90)
            it.setdefault("source_ref", "本地基座规则·" + str(it.get("code") or "自动核查"))
            it.setdefault("source", "基座")
            issues.append(it)
    except Exception as exc:
        auto_report = {"error": str(exc)[:120]}

    # 证据级定位：问题回跳到本章证据所在页；金额/比例/期限必须能在依据里逐字找到
    ev_by_sec: dict[str, list[dict]] = {}
    for ev in store.list_evidence(pid):
        if ev.get("status") == "bound":
            ev_by_sec.setdefault(ev.get("section_id") or "", []).append(ev)
    for it in issues:
        evs = ev_by_sec.get(it.get("section_id") or "", [])
        if evs and not it.get("page"):
            it["page"] = next((int(e["page"]) for e in evs if e.get("page")), 0)
    for n, s in enumerate(sections, 1):
        evs = ev_by_sec.get(s["id"], [])
        if not evs:
            continue
        bad = provenance.verify_numbers(s.get("text") or "", evs,
                                        p.get("tender_text") or "")
        if bad:
            issues.append({
                "code": f"E{n:02d}", "level": "中",
                "title": "正文数值未在依据中出现，需人工核对",
                "detail": f"「{s['title']}」出现 {'、'.join(bad[:4])}，"
                          "未在本章依据或招标书中找到；金额、比例、期限、数量不得由模型自行生成。",
                "evidence": bad[0], "section_id": s["id"], "anchor": bad[0],
                "source": "证据校验", "confidence": 0.85,
                "source_ref": "证据校验·数值需逐字引用原文",
                "page": next((int(e["page"]) for e in evs if e.get("page")), 0),
            })
    store.save_checks(pid, round_no, issues)
    LAST_AUTO[_cache_key(pid)] = auto_report
    # 读回库里的记录：带 id / 人工采纳状态 / 置信度，供逐条采纳与改写过滤使用
    return store.list_checks(pid, round_no)


def last_auto(pid: str) -> dict:
    """最近一次体检时自动跑过的基座报告，供对话 / 页面展示。"""
    return LAST_AUTO.get(_cache_key(pid)) or {}


def _find_paragraph(text: str, anchor: str) -> tuple[int, list[str], str] | None:
    parts = re.split(r"(\n\s*\n)", text or "")
    blocks = [p for p in parts if p.strip()]
    key = (anchor or "").strip()[:18]
    for i, block in enumerate(blocks):
        if key and key in block:
            return i, blocks, block
    if key:
        for i, block in enumerate(blocks):
            if any(seg and seg in block for seg in re.findall(r"[\u4e00-\u9fa5]{4,10}", key)[:3]):
                return i, blocks, block
    return None


def _apply_fix(pid: str, issue: dict) -> dict:
    sections = store.list_sections(pid)
    sec = next((s for s in sections if s["id"] == issue.get("section_id")), None)
    base = {"code": issue.get("code"), "title": issue.get("title"),
            "section": sec["title"] if sec else "", "status": "skipped"}
    if issue.get("status") == "ignored":
        base["status"] = "ignored"
        return base
    if issue.get("status") != "accepted":
        # 人工采纳是唯一闸门：没点「采纳」的问题一律不许自动改写正文
        base["status"] = "not_accepted"
        return base
    if float(issue.get("confidence") or 0.90) < LOW_CONFIDENCE:
        base["status"] = "low_confidence"
        return base
    if not sec or _protected(sec):
        base["status"] = "protected"
        return base
    anchor = (issue.get("anchor") or issue.get("evidence") or "").strip()
    if not anchor:
        return base
    found = _find_paragraph(sec.get("text") or "", anchor)
    if not found:
        return base
    i, blocks, old = found
    _, hits = rag.retrieve(pid, issue.get("title", "") + " " + anchor[:40])
    evidence = "\n".join(f"【{h['scope']}·{h['title']}】{h['snippet']}" for h in hits)[:2500]
    generation = generation_context.GenerationContext(pid, sec["id"], int(sec.get("ver") or 0))
    generation.hints = hits
    new = str(_llm(pid, "fix", {
        "title": sec["title"],
        "issue_title": issue.get("title", ""),
        "level": issue.get("level", "中"),
        "issue_detail": issue.get("detail", ""),
        "anchor": provenance.strip_note(old)[:900],
        "evidence": evidence or rag.materials_block(pid)[:1500],
    }, generation=generation)).strip()
    if not new:
        return base
    srcs = generation.sources
    new = provenance.attach_paragraph(new, srcs)
    blocks[i] = new
    res = store.commit_generation(pid, sec["id"], "\n\n".join(b.strip() for b in blocks),
                                  int(sec.get("ver") or 0), generation.id, partial=True)
    if not res.get("ok"):
        base.update({"status": "conflict",
                     "note": "改写期间这一章被人工改过，未覆盖" if res.get("error") == "conflict"
                             else "章节已不存在"})
        return base
    base.update({"status": "fixed", "before": old[:220], "after": new[:220],
                 "sources": hits, "source_ref": provenance.source_ref(srcs)})
    return base


def optimize(pid: str, jid: str, max_rounds: int = 2) -> None:
    """体检 → 按问题只改命中段落 → 复检 → 前后对比。最多 2 轮，写死。"""
    _job(jid, status="running", progress=0, step="准备改写")
    try:
        store.reset_task_tokens(pid)
        before = check(pid, 1)
        _job(jid, progress=15, step=f"初检完成，发现 {len(before)} 个问题")
        # 闭环前整本留快照：无论后面改到哪一章，「撤销本轮」都能完整恢复
        batch_id = store.new_id()
        targets = [s["id"] for s in store.list_sections(pid)
                   if not _protected(s)]
        store.snapshot_sections(pid, batch_id, "优化闭环改写前", targets)
        fixes: list[dict] = []
        current = before
        used_rounds = 0
        for round_no in range(1, max_rounds + 1):
            # 只改人工点过「采纳」的问题。规则 / 基座问题置信度再高也不自动改写，
            # 避免用户在没确认的情况下被 AI 改掉正文。
            pool = [i for i in current
                    if i.get("status") == "accepted" and i.get("section_id")
                    and (i.get("anchor") or i.get("evidence"))]
            # 高风险优先；同风险里基座自动判定的问题优先，避免只改到简化规则那几条
            pool.sort(key=lambda i: (0 if i.get("level") == "高" else 1,
                                     0 if i.get("auto") else 1))
            candidates = pool[:10]
            if not candidates:
                break
            used_rounds = round_no
            for k, issue in enumerate(candidates):
                try:
                    fixes.append(_apply_fix(pid, issue))
                except Exception as exc:  # 单条改写失败不拖垮整轮，如实记下来
                    fixes.append({"code": issue.get("code"), "title": issue.get("title"),
                                  "section": issue.get("section", ""), "status": "error",
                                  "note": str(exc)[:120]})
                _job(jid, progress=min(85, 15 + round_no * 30 + k * 3),
                     step=f"第 {round_no} 轮：按问题改写 {k + 1}/{len(candidates)}")
            current = check(pid, round_no + 1)
        try:
            auto_card = auto_core.as_card(auto_core.run_stage("optimize", pid))
        except Exception:
            auto_card = {}
        accepted = sum(1 for i in before if i.get("status") == "accepted")
        errored = [f for f in fixes if f.get("status") in ("error", "conflict")]
        result = {
            "before": before, "after": current, "fixes": fixes,
            "rounds": used_rounds,
            "reduced": max(0, len(before) - len(current)),
            "fixed_count": sum(1 for f in fixes if f["status"] == "fixed"),
            "protected_count": sum(1 for f in fixes if f["status"] == "protected"),
            "error_count": len(errored),
            "accepted_count": accepted,
            "gate": "自动改写只处理人工点过「采纳」的问题",
            "batch_id": batch_id,
            "auto": auto_card,
        }
        hint = ("（没有已采纳的问题，正文未改动；请先在检查清单里点「采纳」）"
                if not accepted else "")
        status = "partial" if errored else "done"
        _job(jid, status=status, progress=100,
             step=(f"闭环完成：问题 {len(before)} → {len(current)}{hint}"
                   + (f"，{len(errored)} 条改写未落地，见清单" if errored else "")),
             result=result)
    except Exception as exc:
        _job(jid, status="failed", error=str(exc)[:300], step="优化失败")


# ---------------- 导入已有竞标书 ----------------

HEAD_RE = re.compile(r"^\s*(第[一二三四五六七八九十\d]+[章节部分]|[一二三四五六七八九十]+、|\d+[.、]\s*[^\d])\s*(.{2,40})$")
CHAPTER_WORDS = ("投标函", "技术方案", "商务", "报价", "资格", "资质", "服务", "售后",
                 "实施", "组织", "承诺", "偏离", "响应", "项目概况", "授权", "业绩",
                 "附件", "投标一览表", "法定代表人", "技术规格", "项目团队")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 40:
        return False
    if HEAD_RE.match(s):
        return True
    # 无编号的短标题行（如「投标函」「技术方案」独占一行）
    if len(s) <= 20 and not re.search(r"[。；;：:，,、]$", s) and any(k in s for k in CHAPTER_WORDS):
        return True
    return False


def import_draft(pid: str, text: str) -> dict:
    """把已有竞标书按标题切成章节；识别不出的放到「正文（导入）」。"""
    old_protected = [s for s in store.list_sections(pid) if s["protected"]]
    lines = (text or "").splitlines()
    chunks: list[tuple[str, list[str]]] = []
    cur_title, cur_lines = "投标文件正文（导入）", []
    for line in lines:
        if _is_heading(line):
            if cur_lines:
                chunks.append((cur_title, cur_lines))
            cur_title, cur_lines = line.strip(), []
        else:
            cur_lines.append(line)
    if cur_lines:
        chunks.append((cur_title, cur_lines))
    # 导入内容没覆盖的保护章节（报价表等）原样保留，避免被清空
    for sec in old_protected:
        if not any(sec["title"] == t or "报价" in sec["title"] and "报价" in t for t, _ in chunks):
            chunks.append((sec["title"], [sec.get("text") or ""]))
    # 保护章节即使导入内容里没有正文也要留下，不能因为空而被过滤掉
    fixed: list[tuple[str, list[str]]] = []
    for t, body in chunks:
        if not "".join(body).strip() and any(k in t for k in ("报价", "投标函", "承诺", "资质", "授权")):
            body = ["（原文未提供，请人工补充；本章为保护章节，自动改写不会触碰）"]
        if "".join(body).strip():
            fixed.append((t, body))
    chunks = fixed
    chapters = [{"title": t[:60], "points": []} for t, _ in chunks]
    if not chapters:
        return {"error": "未识别到内容"}
    store.update_project(pid, outline=chapters)
    store.set_sections(pid, chapters, replace_text=True)
    sections = store.list_sections(pid)
    by_title = {s["title"]: s for s in sections}
    for t, body in chunks:
        sec = by_title.get(t[:60])
        if sec:
            store.update_section(sec["id"], "\n".join(body).strip(), pid=pid)
    return {"chapters": len(chapters)}
