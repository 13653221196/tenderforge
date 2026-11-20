"""企业素材知识库：切片、索引、检索、引用溯源。

设计约束：
1. 零第三方依赖——中文用二元词（bigram）切分，检索用自实现 BM25；
2. 全部本地内存索引 + SQLite 落盘，素材不出本机；
3. 检索结果带来源（文档名 / 标题链），生成时注入，便于人工核对。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Iterable

# ---------- 分类 ----------
CATEGORIES: list[dict] = [
    {"id": "award", "label": "中标标书", "hint": "我方已中标的完整投标文件，作章节写法与结构参照"},
    {"id": "company", "label": "企业基础", "hint": "营业执照、公司章程、组织架构、简介"},
    {"id": "qualification", "label": "资质证书", "hint": "行业资质、体系认证、信用等级、许可"},
    {"id": "performance", "label": "类似业绩", "hint": "合同、中标通知、验收证明、用户评价"},
    {"id": "staff", "label": "人员团队", "hint": "项目经理与技术骨干简历、证书、社保"},
    {"id": "tech", "label": "技术方案", "hint": "既有方案、产品白皮书、架构与参数"},
    {"id": "service", "label": "服务承诺", "hint": "售后体系、响应时效、备件与培训"},
    {"id": "finance", "label": "财务资料", "hint": "审计报告、纳税证明、银行资信"},
    {"id": "other", "label": "其他素材", "hint": "上述之外的可引用材料"},
]
CATEGORY_IDS = {c["id"] for c in CATEGORIES}
CATEGORY_LABEL = {c["id"]: c["label"] for c in CATEGORIES}

# ---------- 切片参数 ----------
SOFT_MIN = 70         # 标题切换时，当前缓冲达到此长度才独立成片
MAX_CHUNK = 760       # 超过此长度强制断开
MAX_HEADING = 42      # 视为标题行的最大长度

HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百零〇]+[章编部分篇]"),
    re.compile(r"^第[一二三四五六七八九十百零〇]+[节条]"),
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),
    re.compile(r"^[（(][一二三四五六七八九十]+[）)]"),
    re.compile(r"^\d+(?:\.\d+){0,3}[、.\s]"),
    re.compile(r"^[★☆▲●◆■]"),
]
SENT_END = "。！？；!?;"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("\u3000", " ").replace("\xa0", " ")


def _clean_line(line: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", line.strip())


def is_heading(line: str) -> bool:
    """判断是否标题行：短、不以句末标点结尾、命中编号样式或无标点的短语行。"""
    if not line or len(line) > MAX_HEADING:
        return False
    # 表格行不是标题：Excel 台账/合同清单每行都带竖线，标题行几乎不会出现竖线
    if "|" in line or "｜" in line:
        return False
    if line[-1] in SENT_END + "，,：:":
        return False
    if any(p.match(line) for p in HEADING_PATTERNS):
        return True
    # 无标点且不含逗号的短行，多半是小标题
    if "，" not in line and "。" not in line and len(line) <= 22:
        return len(re.findall(r"[\u4e00-\u9fa5]", line)) >= 2
    return False


def heading_level(line: str) -> int:
    """按编号样式推断层级，用于维护标题栈。

    中文材料常见嵌套：第X章(1) → 一、(2) → 1.(3) → 1.1(4) → 1.1.1(5)。
    无编号的短标题固定按 4 级处理，即与上一个同级标题互相替换，不会串成假的长链。
    """
    if re.match(r"^第[一二三四五六七八九十百零〇]+[章编部分篇]", line):
        return 1
    if re.match(r"^第[一二三四五六七八九十百零〇]+[节条]", line):
        return 3
    if re.match(r"^[一二三四五六七八九十]+[、.．]", line):
        return 2
    if re.match(r"^[（(][一二三四五六七八九十]+[）)]", line):
        return 3
    match = re.match(r"^(\d+(?:\.\d+){0,3})[、.\s]", line)
    if match:
        return min(5, 3 + match.group(1).count("."))
    return 4


def _pack_tables(lines: list[str], group: int = 5) -> list[str]:
    """把连续的表格行打包成「表头 + 若干数据行」的块。

    Excel 台账一行往往只有十几个字（"2 | 某某局环网柜 | 128万元"），
    单行成片会被最短长度过滤掉；带上表头一起打包，检索时才说得清这行是什么。
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        if "|" not in lines[i] and "｜" not in lines[i]:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and ("|" in lines[j] or "｜" in lines[j]):
            j += 1
        block = [x for x in lines[i:j] if x]
        if len(block) <= 1:
            out.extend(block)
        else:
            header, body = block[0], block[1:]
            for k in range(0, len(body), group):
                piece = body[k : k + group]
                # 一块就是一个独立片段，用换行连起来交给 split_chunks 整体落片
                out.append("\n".join([header] + piece))
        i = j
    return out


def split_chunks(text: str) -> list[dict]:
    """把整篇文本切成带标题链的片段。返回 [{heading, text}]。

    断片原则：标题变化即语义边界——只要当前缓冲够 SOFT_MIN 就断开，
    这样「一条业绩」「一项资质」能各自成片，检索时不会互相污染。
    """
    text = _norm(text)
    lines = _pack_tables([_clean_line(x) for x in text.splitlines()])
    stack: list[tuple[int, str]] = []

    def chain() -> str:
        return " › ".join(t for _, t in stack[-3:])

    chunks: list[dict] = []
    buf: list[str] = []
    buf_heading = ""

    def flush() -> None:
        nonlocal buf, buf_heading
        body = "\n".join(x for x in buf if x).strip()
        if body:
            chunks.append({"heading": buf_heading, "text": body})
        buf = []
        buf_heading = chain()

    for line in lines:
        if not line:
            continue
        # 打包好的表格块（多行且含竖线）自带完整上下文，单独成片
        if "\n" in line and ("|" in line or "｜" in line):
            if buf:
                flush()
            buf_heading = chain()
            buf.append(line)
            flush()
            continue
        if is_heading(line):
            level = heading_level(line)
            top = stack[-1][0] if stack else 9
            # 长度够，或者标题回到了同级/父级（说明上一段已讲完）就断开
            if buf and (len("\n".join(buf)) >= SOFT_MIN or level <= top):
                flush()
            stack = [(lv, txt) for lv, txt in stack if lv < level]
            stack.append((level, line))
            if not buf:
                buf_heading = chain()
            else:
                buf.append(line)  # 上一段太短且层级更深，把这个标题并进去当正文
            continue

        if not buf:
            buf_heading = chain()
        buf.append(line)
        if sum(len(x) for x in buf) >= MAX_CHUNK:
            flush()

    flush()

    # 全文太短而无片段的兜底
    if not chunks:
        body = "\n".join(x for x in lines if x).strip()
        if body:
            chunks = [{"heading": "", "text": body[: MAX_CHUNK * 2]}]
    # 正文片段太短没有检索价值；表格行整行就是一个事实（"张三 | 项目经理"），放宽阈值
    return [
        c for c in chunks
        if len(c["text"]) >= 30 or ("|" in c["text"] and len(c["text"]) >= 12)
    ]


# ---------- 分词 ----------
STOP_BIGRAMS = {
    "我们", "可以", "进行", "以及", "并且", "因此", "所以", "但是", "如果", "其中",
    "通过", "对于", "关于", "根据", "按照", "上述", "如下", "以下", "以上", "本项",
    "本次", "此项", "该项", "中的", "的是", "为了", "具有", "能够", "以及", "等。",
}
WORD_RE = re.compile(r"[a-z][a-z0-9\-\.]*|\d+(?:\.\d+)?|[\u4e00-\u9fa5]+")


def tokenize(text: str) -> list[str]:
    """中文按二元词，英文/数字按词。无词典依赖，招投标术语也能命中。"""
    text = _norm(text).lower()
    out: list[str] = []
    for match in WORD_RE.finditer(text):
        piece = match.group(0)
        if piece[0].isascii():
            if len(piece) >= 2:
                out.append(piece)
            continue
        if len(piece) == 1:
            continue
        for i in range(len(piece) - 1):
            bigram = piece[i:i + 2]
            if bigram in STOP_BIGRAMS:
                continue
            out.append(bigram)
    return out


# ---------- BM25 ----------
K1 = 1.5
B = 0.72
HEADING_BOOST = 3      # 标题词在词频里额外计 3 次
# 中标标书是"同题范文"，同类问题下它比零散素材更贴题，给一点名次倾斜
DOC_BOOST = {"award": 1.15, "performance": 1.12, "qualification": 1.08, "staff": 1.05}
# 这些分类只学写法，不当事实来源（照搬别家项目名/金额会出大事）
MODEL_CATEGORIES = {"award"}


class BM25Index:
    def __init__(self, chunks: list[dict]):
        self.rows = chunks
        self.tfs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for row in chunks:
            tokens = tokenize(row["text"])
            tokens.extend(tokenize(row.get("heading") or "") * HEADING_BOOST)
            tf = Counter(tokens)
            self.tfs.append(tf)
            self.lens.append(len(tokens) or 1)
            for term in tf:
                df[term] += 1
        self.df = df
        self.n = max(1, len(chunks))
        self.avg = sum(self.lens) / self.n if self.lens else 1.0

    def search(self, query: str, top_k: int = 8, min_score: float = 0.0) -> list[dict]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        weights = Counter(q_tokens)
        scored: list[tuple[float, int]] = []
        for idx, tf in enumerate(self.tfs):
            score = 0.0
            norm = K1 * (1 - B + B * self.lens[idx] / self.avg)
            for term, weight in weights.items():
                freq = tf.get(term)
                if not freq:
                    continue
                idf = math.log(1 + (self.n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                score += weight * idf * (freq * (K1 + 1)) / (freq + norm)
            if score > 0:
                cat = self.rows[idx].get("category") or ""
                score *= DOC_BOOST.get(cat, 1.0)
                scored.append((score, idx))
        scored.sort(reverse=True)
        if not scored:
            return []
        best = scored[0][0]
        floor = max(min_score, best * 0.32)
        out: list[dict] = []
        for score, idx in scored[:top_k]:
            if score < floor:
                break
            row = dict(self.rows[idx])
            row["score"] = round(score, 3)
            out.append(row)
        return out


_INDEX_CACHE: dict[str, dict] = {}  # ent_id -> {"version": str, "index": BM25Index}


def _grams(text: str) -> list[str]:
    t = re.sub(r"[^\u4e00-\u9fa5a-z0-9]", "", _norm(text).lower())
    return [t[i : i + 2] for i in range(len(t) - 1)]


def heading_search(chunks: list[dict], query: str, top_k: int = 6) -> list[dict]:
    """按标题链找「同主题章节」：写「4.3 生产与检测能力」时，命中中标标书里同名的节。

    只看标题不看正文——正文语义交给向量路。用 heading 被 query 覆盖的比例打分，
    分数不够的不放行，避免"技术方案"这种通用标题把所有章节都召回来。
    """
    q = set(_grams(query))
    if len(q) < 3:
        return []
    scored: list[tuple[float, dict]] = []
    for row in chunks:
        head = row.get("heading") or ""
        if len(head) < 4:
            continue
        hg = set(_grams(head))
        if len(hg) < 3:
            continue
        inter = q & hg
        cover = len(inter) / len(hg)
        if len(inter) >= 3 and cover >= 0.5:
            boost = DOC_BOOST.get(row.get("category") or "", 1.0)
            scored.append((cover * boost, row))
    scored.sort(key=lambda t: -t[0])
    return [{**row, "score": round(score, 3)} for score, row in scored[:top_k]]


def build_index(ent: str, chunks: list[dict], version: str) -> BM25Index:
    """按企业+版本号缓存索引，素材变动时由调用方递增版本。"""
    slot = _INDEX_CACHE.get(ent)
    if slot is None or slot["version"] != version or slot["index"] is None:
        slot = {"version": version, "index": BM25Index(chunks)}
        _INDEX_CACHE[ent] = slot
    return slot["index"]  # type: ignore[return-value]


def invalidate(ent: str = "") -> None:
    if ent:
        _INDEX_CACHE.pop(ent, None)
    else:
        _INDEX_CACHE.clear()


# ---------- 素材注入 block ----------
def _render_hits(hits: list[dict], budget: int, tag: str) -> str:
    parts: list[str] = []
    used = 0
    for idx, hit in enumerate(hits, 1):
        head = hit.get("heading") or ""
        label = f"[{tag}{idx}] {hit.get('doc_title') or '未命名'}"
        if head:
            label += f" › {head}"
        body = hit["text"].strip()
        block = f"{label}\n{body}"
        if used + len(block) > budget:
            block = block[: max(0, budget - used)]
        if len(block) < 40:
            break
        parts.append(block)
        used += len(block)
        if used >= budget:
            break
    return "\n\n".join(parts)


def build_material_block(hits: list[dict], limit: int = 3400, include_model: bool = True) -> str:
    """把命中的素材拼成提示词片段。

    事实素材（资质/业绩/人员/方案）与范文参考（中标标书）必须分区：前者可以原样引用，
    后者只能学写法——混在一起模型会把别家项目名、金额直接搬过来。
    include_model=False 时只返回事实素材，供引用核查使用（范文里的事实不属于本项目）。
    """
    if not hits:
        return ""
    facts = [h for h in hits if (h.get("category") or "") not in MODEL_CATEGORIES]
    models = [h for h in hits if (h.get("category") or "") in MODEL_CATEGORIES] if include_model else []
    if not facts and not models:
        return ""
    if facts and models:
        fact_budget = int(limit * 0.62)
        model_budget = limit - fact_budget
    else:
        fact_budget = model_budget = limit

    blocks: list[str] = []
    fact_text = _render_hits(facts, fact_budget, "素材")
    if fact_text:
        blocks.append("【事实素材】\n" + fact_text)
    model_text = _render_hits(models, model_budget, "范文")
    if model_text:
        blocks.append("【范文参考（中标标书原文，只学写法）】\n" + model_text)
    return "\n\n".join(blocks)


MATERIAL_RULES = (
    "可用材料分两类，规则完全不同：\n"
    "· 事实素材：本企业已归档材料，是本节事实的唯一来源。项目名称、合同金额、日期、"
    "证书编号、人员姓名与职务、技术参数必须原样使用，不得改写、四舍五入或换算单位。\n"
    "· 范文参考：本企业以往中标标书的原文，只用来学章节结构、论证顺序、表达口径和术语。"
    "里面的项目名称、客户名称、金额、日期、人名、参数都属于别的项目，绝不能写进本节，"
    "也不能改头换面移植。\n"
    "-----\n"
    "{materials}\n"
    "-----\n"
    "引用规则（硬性）：\n"
    "1. 事实素材里没有的事实一律写成〔待填：具体项〕，严禁自行编造业绩、资质、奖项、"
    "客户名称或测试数据；\n"
    "2. 正文中不要出现“素材1”“范文1”“如上文”这类指代，直接把内容自然融入句子；\n"
    "3. 命中范文时，可以沿用它的写法和结构，但必须换成本项目、本企业的真实信息；\n"
    "4. 论证所依据的事实必须能回溯到某条事实素材或招标文件。"
)


# ---------- 引用溯源核查 ----------
NUMBER_RE = re.compile(r"[0-9][0-9,\.]{1,20}%?")
ORG_SUFFIX = r"(?:股份有限公司|有限责任公司|有限公司|集团|研究院|设计院|大学|医院|银行|中心)"
ORG_RE = re.compile(r"[\u4e00-\u9fa5]{2,12}" + ORG_SUFFIX)
# 句子里的动词/介词常被正则一起吃进来，逐层剥掉后再比对
ORG_PREFIX = re.compile(
    r"^(?:承接|签订|签署|中标|委托|采购|服务|供货|建设|施工|实施|完成|承担|负责|"
    r"另|在|于|由|与|和|及|向|为|经|本|该|我|其|并|已|曾|共|同|年|月|日)"
)


def _strip_org(name: str) -> str:
    prev = ""
    while prev != name:
        prev = name
        name = ORG_PREFIX.sub("", name)
    return name


def _plain(text: str) -> str:
    return re.sub(r"[,\s，]", "", _norm(text))


def verify_citations(draft: str, allowed_text: str, *, min_len: int = 3) -> list[dict]:
    """核查正文中出现的数字与机构名是否能在素材/招标文件里找到。

    返回可疑项：{value, kind, excerpt}。仅提示，不做阻断——有些数字是计算得出的。
    """
    pool = _plain(allowed_text)
    pool_nums = set(NUMBER_RE.findall(pool))
    seen: set[str] = set()
    suspects: list[dict] = []

    for match in NUMBER_RE.finditer(_norm(draft)):
        raw = match.group(0)
        digits = re.sub(r"[^0-9.]", "", raw)
        if len(digits) < min_len or digits in seen:
            continue
        # 序号/章节号不算事实陈述
        if re.fullmatch(r"(19|20)\d{2}", digits) is None and len(digits) <= 2:
            continue
        seen.add(digits)
        if digits in pool_nums or digits in pool:
            continue
        start = max(0, match.start() - 28)
        suspects.append({
            "value": raw,
            "kind": "数字",
            "excerpt": draft[start: match.end() + 18].replace("\n", " "),
        })

    for match in ORG_RE.finditer(_norm(draft)):
        name = _strip_org(match.group(0))
        if len(name) < 4 or name in seen:
            continue
        seen.add(name)
        if _plain(name) in pool:
            continue
        start = max(0, match.start() - 20)
        suspects.append({
            "value": name,
            "kind": "机构名",
            "excerpt": draft[start: match.end() + 14].replace("\n", " "),
        })
    return suspects[:20]


def coverage_stats(hits: Iterable[dict]) -> dict:
    hits = list(hits)
    docs = {h.get("doc_id") for h in hits}
    return {
        "hits": len(hits),
        "docs": len(docs),
        "chars": sum(len(h.get("text") or "") for h in hits),
    }


# ---------- 素材完整度（知识库总览页） ----------
# 权重合计 100：核心五类是资格性审查与评分的地基，缺一类就直接卡住成稿质量。
MATERIAL_PLAN: list[dict] = [
    {"id": "award", "weight": 24, "need": 1, "must": True,
     "why": "写作时参照已中标标书的章节结构与表述，比通用模板贴合得多"},
    {"id": "performance", "weight": 20, "need": 3, "must": True,
     "why": "业绩是资格性审查与商务评分的高频得分点，通常要 3 份以上"},
    {"id": "qualification", "weight": 18, "need": 2, "must": True,
     "why": "营业执照、行业资质、体系认证会被资格条款直接引用"},
    {"id": "staff", "weight": 14, "need": 1, "must": True,
     "why": "人员配置评分需要简历、证书与社保支撑"},
    {"id": "finance", "weight": 10, "need": 1, "must": True,
     "why": "财务状况评分与资格证明要用审计报告或纳税证明"},
    {"id": "company", "weight": 6, "need": 1, "must": False,
     "why": "企业基本情况章节的基础信息"},
    {"id": "tech", "weight": 5, "need": 1, "must": False,
     "why": "既有方案与产品参数可以直接复用，少从零起草"},
    {"id": "service", "weight": 3, "need": 1, "must": False,
     "why": "售后体系与响应时效，服务承诺章节常用"},
]

# 文件名关键词 → 分类，用于上传时自动归类（先命中先返回，award 排最前）
GUIDE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("award", ("中标标书", "投标文件", "中标通知", "技术标", "商务标", "标书", "响应文件")),
    ("performance", ("业绩", "合同", "验收", "用户评价", "履约", "中标通知", "项目清单")),
    ("qualification", ("营业执照", "资质", "证书", "认证", "许可", "体系", "iso", "aaa")),
    ("staff", ("简历", "人员", "项目经理", "社保", "职称", "建造师", "团队")),
    ("finance", ("财务", "审计", "纳税", "资产负债", "利润表", "银行资信", "完税")),
    ("service", ("售后", "服务承诺", "响应时效", "备件", "培训", "运维")),
    ("tech", ("技术方案", "白皮书", "参数", "说明书", "规格", "产品", "设备")),
    ("company", ("公司简介", "企业简介", "章程", "组织架构", "简介")),
]


def guess_category(name: str) -> str:
    """按文件名猜分类，猜不到归其他。用户仍可在上传后改。"""
    low = (name or "").lower()
    for cid, keys in GUIDE_HINTS:
        if any(k in low for k in keys):
            return cid
    return "other"


def readiness(stats: dict) -> dict:
    """把素材统计换算成「成稿准备度」：分数、等级、缺口清单。"""
    detail = stats.get("category_detail") or {}
    rows: list[dict] = []
    score = 0.0
    gaps: list[dict] = []
    for plan in MATERIAL_PLAN:
        cid = plan["id"]
        got = int((detail.get(cid) or {}).get("docs") or 0)
        need = plan["need"]
        if got <= 0:
            ratio = 0.0
        elif got >= need:
            ratio = 1.0
        else:
            ratio = 0.5 + 0.5 * got / need
        score += plan["weight"] * ratio
        state = "good" if got >= need else ("thin" if got else "empty")
        rows.append({
            "id": cid,
            "label": CATEGORY_LABEL.get(cid, cid),
            "docs": got,
            "chunks": int((detail.get(cid) or {}).get("chunks") or 0),
            "chars": int((detail.get(cid) or {}).get("chars") or 0),
            "need": need,
            "must": plan["must"],
            "state": state,
            "why": plan["why"],
        })
        if got < need:
            gaps.append({
                "id": cid,
                "label": CATEGORY_LABEL.get(cid, cid),
                "must": plan["must"],
                "lack": need - got,
                "text": (
                    f"再补 {need - got} 份{CATEGORY_LABEL.get(cid, cid)}"
                    if got else f"还没有{CATEGORY_LABEL.get(cid, cid)}"
                ),
                "why": plan["why"],
            })
    score = round(score)
    level = "充足" if score >= 85 else "基本可用" if score >= 60 else "偏薄" if score >= 35 else "严重不足"
    # 必缺项优先提示，可选类排在后面
    gaps.sort(key=lambda g: (not g["must"], -g["lack"]))
    return {"score": score, "level": level, "rows": rows, "gaps": gaps}
