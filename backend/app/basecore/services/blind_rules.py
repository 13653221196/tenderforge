"""暗标实体识别与格式规则。

只做确定性判断：
- 证件号必须过校验位（身份证 GB 11643、统一社会信用代码 GB 32100），不过的一律不报；
- 企业名必须有强后缀，且去掉"第三方/专业/相关"这类泛称后仍剩专名；
- 招标人、代理机构、监理、供电公司等第三方默认不算泄漏（暗标查的是投标人自己）；
- 格式类只看"会不会暴露身份或显得不专业"：彩色字、字体字号过杂、隐藏文字、页眉页脚带字。
"""
from __future__ import annotations

import re

ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK = "10X98765432"

USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)

COMPANY_SUFFIX = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "集团",
    "设计研究院",
    "勘察设计院",
    "设计院",
    "研究院",
    "研究所",
    "事务所",
    "工程公司",
    "供电公司",
    "电力公司",
    "公司",
    "建工集团",
    "建设集团",
)

GENERIC_ORG = re.compile(
    "(第三方|专业|相关|其他|其它|外部|独立|当地|本地|知名|权威|合格|有资质|具备资质|具有资质|"
    "会计师|律师|审计|检测|监理|造价|咨询|设计|勘察|评估|测绘|鉴定|认证|质量|环境|安全|工程|技术|"
    "供电|供水|供电局|招标人|招标代理|采购人|业主)"
)
FUNCTION_WORDS = re.compile(
    "(通过|由于|经过|根据|按照|采用|负责|组织|实施|验收|施工|管理|以及|或者|并且|进行|完成|确保|"
    "满足|符合|达到|提供|使用|建立|邀请|委托|聘请|聘用|选择|选定|指定|配合|协调|联系|的|和|与|及|"
    "或|等|由|向|至|于|在|为|以|对|从|按|如|因|是|也|凡|把|将|让|使|被|给|同|跟)"
)
THIRD_PARTY_HINTS = (
    "招标人",
    "采购人",
    "招标代理",
    "代理机构",
    "监理",
    "供电公司",
    "供电局",
    "人民政府",
    "建设局",
    "发改委",
    "财政局",
    "公共资源交易中心",
    "设计院",
    "检测机构",
    "质检站",
    "第三方",
    "检测",
    "认证中心",
)

PATTERNS = {
    "身份证号": re.compile(r"(?<![0-9Xx])([0-9]{17}[0-9Xx])(?![0-9Xx])"),
    "统一社会信用代码": re.compile(r"(?<![0-9A-Za-z])([0-9A-HJ-NPQRTUWXY]{18})(?![0-9A-Za-z])"),
    "手机号": re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"),
    "固定电话": re.compile(r"(?<![0-9])(0[0-9]{2,3}[-\s]?[0-9]{7,8})(?![0-9])"),
    "电子邮箱": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "网址": re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE),
    "域名": re.compile(r"(?<![\w.@-])([A-Za-z0-9-]{2,}\.(?:com\.cn|cn\.com|com|cn|net|org|edu|gov))(?![\w.-])", re.IGNORECASE),
    "银行账号": re.compile(r"(?<![0-9])([0-9]{16,25})(?![0-9])"),
    "占位符": re.compile(r"[\[【（(]\s*(?:填写|输入|待填|此处填写|XXX+|xxx+|某某|某公司|公司名称|单位名称)[^\])）】]*[\])）】]|[_＿]{4,}"),
    "水印标记": re.compile(r"(版权所有|copyright|©|®|™)", re.IGNORECASE),
}

SELF_REFERENCES = ("我公司", "我司", "本公司", "我单位", "本单位", "我集团", "我集团", "我厂", "我企业")

COLOR_WHITE = {"FFFFFF"}
COLOR_NEAR_BLACK = ("00", "17", "1F", "33")


def id_card_valid(raw: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dXx]", raw or ""):
        return False
    total = sum(int(raw[i]) * ID_WEIGHTS[i] for i in range(17))
    return ID_CHECK[total % 11] == raw[17].upper()


def uscc_valid(raw: str) -> bool:
    if not re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", (raw or "").upper()):
        return False
    code = raw.upper()
    total = 0
    for i, ch in enumerate(code[:17]):
        total += USCC_CHARS.index(ch) * USCC_WEIGHTS[i]
    check = 31 - (total % 31)
    if check == 31:
        check = 0
    return USCC_CHARS[check] == code[17]


# 公司名左边界：这些字出现即视为断点（虚词/介词/动词/标点）
CUT_CHARS = set(
    "及和与或由为在按据的是了也都还把被使让给于至等向对从经已并可应须需要等"
    "，。；：、（）()【】「」“”\"'!！?？/\\ \t\n"
)
# 指示词/介词前缀：'本项目中建三局…' → '中建三局…'
LEAD_INDICATOR = re.compile(
    r"^(?:本项目|本工程|本次|本标段|本包|本批|该项目|该工程|此项目|上述|前述|以下"
    r"|正文出现|出现|写明|注明|填写为|填为|写为|显示|通过|经过|由|经|本|我|贵|该|你|公司)"
)
# 角色词前缀：剥掉"投标人""单位名称""以下称"之类的引导语
ROLE_PREFIX = re.compile(
    r"^(?:投标人|供应商|制造商|代理商|中标人|承包人|响应人|申请人|单位名称|公司名称|企业名称|"
    r"名称|以下称|简称|以下简称|即|系)(?:为|是|：|:)?"
)


GENERIC_CORES = {
    "电力", "工程", "建设", "技术", "科技", "实业", "发展", "投资", "贸易", "检测",
    "咨询", "服务", "设备", "材料", "集团", "控股", "股份", "有限", "责任", "项目",
}


def _core_of(full: str, suffix: str) -> str:
    """从"…有限公司"整段里取出真正的字号（去掉虚词前缀与角色词）。"""
    core = full[: -len(suffix)]
    cut = -1
    for i in range(len(core) - 1, -1, -1):
        if core[i] in CUT_CHARS:
            cut = i
            break
    if cut >= 0:
        core = core[cut + 1 :]
    for _ in range(3):
        before = core
        core = LEAD_INDICATOR.sub("", core)
        core = ROLE_PREFIX.sub("", core)
        if core == before:
            break
    if len(core) > 10:
        core = core[-10:]
    return core


def frequent_orgs(text: str, min_count: int = 3) -> set[str]:
    """招标原文里反复出现的机构名。

    暗标要求下，投标人自己的字号不会写进招标文件；反复出现的基本是招标人、
    监管机构或被引用的第三方。用频次把这类名字从"泄漏候选"里摘出去，
    否则每份标书都会报"正文出现国家电网有限公司"。
    """
    from collections import Counter

    cnt: Counter[str] = Counter()
    for suffix in COMPANY_SUFFIX:
        for m in re.finditer(rf"[\u4e00-\u9fff]{{2,30}}{re.escape(suffix)}", text or ""):
            core = _core_of(m.group(0), suffix)
            if len(core) >= 2:
                cnt[core + suffix] += 1
    return {name for name, n in cnt.items() if n >= min_count}


def _company_hits(text: str, exclude: set[str] | None = None) -> list[tuple[str, int]]:
    """找疑似企业全称，返回 [(企业名, 起始位置)]。

    关键点：老实现用 `[\\u4e00-\\u9fff]{2,24}有限公司` 贪婪匹配，会把整句话
    当公司名（"产品按现行国家标准及国家电网有限公司"）。这里改成
    从后缀往前按虚词截断，再剥角色词前缀，得到真正的字号。
    """
    exclude = exclude or set()
    hits: list[tuple[str, int]] = []
    seen: set[str] = set()
    for suffix in COMPANY_SUFFIX:
        for m in re.finditer(rf"[\u4e00-\u9fff]{{2,30}}{re.escape(suffix)}", text):
            full = m.group(0)
            core = _core_of(full, suffix)
            if len(core) < 2 or core in GENERIC_CORES:
                continue
            body = core + suffix
            if body in seen:
                continue
            if any(body in ex or ex in body for ex in exclude if ex):
                continue  # 招标人/代理机构名称，出现在正文里是正常的
            if any(hint in body for hint in THIRD_PARTY_HINTS):
                continue
            seen.add(body)
            hits.append((body, m.end() - len(body)))
    # 长短名去重："华信建设集团" 已被 "华信建设集团有限公司" 覆盖，不重复报
    hits.sort(key=lambda x: -len(x[0]))
    kept: list[tuple[str, int]] = []
    for body, pos in hits:
        if any(body in longer for longer, _ in kept):
            continue
        kept.append((body, pos))
    return kept


def scan_text(text: str, exclude: set[str] | None = None) -> list[dict]:
    """扫一段文字里的可识别实体。exclude 传招标人/代理机构名称等不应报的名称。"""
    hits: list[dict] = []
    if not (text or "").strip():
        return hits
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, index: int, note: str = "") -> None:
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        hits.append(
            {
                "kind": kind,
                "value": value,
                "index": index,
                "note": note,
                "excerpt": text[max(0, index - 14) : index + len(value) + 14].replace("\n", " "),
            }
        )

    for m in PATTERNS["身份证号"].finditer(text):
        if id_card_valid(m.group(1)):
            add("身份证号", m.group(1), m.start(1), "校验位正确，属真实格式")

    for m in PATTERNS["统一社会信用代码"].finditer(text):
        if uscc_valid(m.group(1)):
            add("统一社会信用代码", m.group(1), m.start(1), "校验位正确")

    for kind in ("手机号", "电子邮箱", "网址", "域名", "水印标记"):
        for m in PATTERNS[kind].finditer(text):
            add(kind, m.group(0), m.start(), "")

    for m in PATTERNS["固定电话"].finditer(text):
        add("固定电话", m.group(1), m.start(1), "区号+号码格式")

    for m in PATTERNS["银行账号"].finditer(text):
        value = m.group(1)
        if id_card_valid(value) or uscc_valid(value):
            continue
        add("银行账号", value, m.start(1), "16~25 位数字，可能是账号")

    for m in PATTERNS["占位符"].finditer(text):
        add("占位符", m.group(0), m.start(), "投标前必须替换")

    for body, start in _company_hits(text, exclude):
        add("企业名称", body, start, "带公司后缀，疑似投标人自身")

    for word in SELF_REFERENCES:
        idx = text.find(word)
        if idx >= 0:
            add("自称", word, idx, "暗标中不应出现第一人称自称")

    return hits


def scan_doc(doc, zones, exclude: set[str] | None = None) -> list[dict]:
    """扫全文档（正文/表格/页眉页脚），返回带位置的实体清单。"""
    results: list[dict] = []
    for zone, locator, para in zones:
        for hit in scan_text(para.text, exclude):
            hit["zone"] = zone + (f"（{locator}）" if locator else "")
            results.append(hit)
            if len(results) >= 600:
                return results
    return results


def format_issues(doc, xml) -> list[dict]:
    """格式与痕迹类问题（不涉及具体文字）。"""
    issues: list[dict] = []

    header_text = []
    for idx, section in enumerate(doc.sections, 1):
        for label, part in (("页眉", section.header), ("页脚", section.footer)):
            text = "".join(p.text for p in part.paragraphs).strip()
            if text and label == "页眉":
                header_text.append((idx, text[:24]))
    if header_text:
        issues.append(
            {
                "level": "中",
                "type": "页眉",
                "title": f"{len(header_text)} 处页眉带文字",
                "detail": "；".join(f"第 {i} 节：{t}" for i, t in header_text[:3]),
                "fix": "暗标通常要求页眉留空或只保留招标文件规定的字样。",
            }
        )

    fonts: set[str] = set()
    sizes: set[float] = set()
    colored = 0
    hidden = 0
    for para in doc.paragraphs:
        if para.paragraph_format is not None and para._p.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}vanish") is not None:
            hidden += 1
        for run in para.runs:
            if run.font.size:
                sizes.add(round(run.font.size.pt, 1))
            if run.font.name:
                fonts.add(run.font.name)
            color = run.font.color
            if color is not None and color.rgb is not None:
                hex_color = str(color.rgb).upper()
                if hex_color not in COLOR_WHITE and not hex_color.startswith(COLOR_NEAR_BLACK):
                    colored += 1

    if len(fonts) > 4:
        issues.append(
            {
                "level": "低",
                "type": "字体",
                "title": f"正文用了 {len(fonts)} 种西文字体",
                "detail": "、".join(sorted(fonts)[:6]),
                "fix": "暗标一般要求全文统一字体，混用容易被判格式不合规。",
            }
        )
    if len(sizes) > 6:
        issues.append(
            {
                "level": "低",
                "type": "字号",
                "title": f"正文出现 {len(sizes)} 种字号",
                "detail": "、".join(f"{s}pt" for s in sorted(sizes)[:8]),
                "fix": "统一为招标文件规定字号，标题层级单独设置。",
            }
        )
    if colored:
        issues.append(
            {
                "level": "中",
                "type": "彩色文字",
                "title": f"{colored} 处文字不是黑色",
                "detail": "彩色字在黑白打印后可能变成浅灰，也容易被认为做了标记。",
                "fix": "全选正文改为自动/黑色。",
            }
        )
    if hidden:
        issues.append(
            {
                "level": "高",
                "type": "隐藏文字",
                "title": f"{hidden} 段含隐藏文字",
                "detail": "隐藏文字打印不出来，但评审用电子版能查到，属于重大隐患。",
                "fix": "Word「开始-显示/隐藏编辑标记」后删除，或用脱敏副本。",
            }
        )

    revisions = len(xml.findall(".//w:ins", NS)) + len(xml.findall(".//w:del", NS))
    comments = len(xml.findall(".//w:commentReference", NS))
    if revisions:
        issues.append(
            {
                "level": "高",
                "type": "修订痕迹",
                "title": f"{revisions} 处修订未接受",
                "detail": "修订记录会暴露修改人与修改过程。",
                "fix": "审阅 → 接受所有修订 → 关闭修订模式，重新导出。",
            }
        )
    if comments:
        issues.append(
            {
                "level": "中",
                "type": "批注",
                "title": f"{comments} 处批注未删除",
                "detail": "批注含作者信息。",
                "fix": "审阅 → 删除所有批注。",
            }
        )
    return issues


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
