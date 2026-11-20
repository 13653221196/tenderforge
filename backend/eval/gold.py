"""评测样本加载：cases.jsonl 是唯一输入，与规则实现完全解耦。

与旧版的区别（旧版自证问题）：
- 旧版照着 rules.py 的正则反向拼样本，再用同一套规则打分，recall=1.0 是必然；
- 现在每条样本是「真实业务写法片段 + 人工标注的断言属性 + 证据片段」，
  评分看问题是否指向标注的证据位置，不看规则内部的 code / 触发词。
- 样本量小、标注未由领域专家复核，报告必须写明这一点；它只能做回归冒烟，
  不能当作真实标书上的能力证明。换成真实人工标注时，替换 cases.jsonl 即可。

字段：id / stratum / split / target / tender /
      must_report = ["kind|section_id|needle"] / evidence_span / annotation / reviewed
"""
from __future__ import annotations

import json
from pathlib import Path

CASES_FILE = Path(__file__).with_name("cases.jsonl")

REQUIRED_TITLES = ("投标函及投标报价总说明", "技术方案与逐条响应", "商务条款响应",
                   "项目组织与实施计划", "售后服务与运维保障", "资格证明与承诺文件")

NORMAL = (
    "本章依据招标文件要求逐项响应，关键参数在《技术规格响应表》中列明招标要求、投标响应、"
    "偏离情况与证明材料编号；实施过程设置需求确认、到货验收、联调测试、试运行与最终验收"
    "五个控制节点，每个节点形成书面记录并由双方签字确认，验收时提交加盖公章的对照表备查。"
)


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for raw in CASES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        c = json.loads(line)
        secs = [{"id": f"s{i + 1}", "title": t, "text": NORMAL, "protected": 0}
                for i, t in enumerate(REQUIRED_TITLES)]
        secs[1]["text"] = c.get("target") or ""
        c["sections"] = secs
        c["tender"] = c.get("tender") or ""
        cases.append(c)
    return cases


def case_copy(case: dict) -> dict:
    return json.loads(json.dumps(case, ensure_ascii=False))


# 兼容旧脚本的调用名，但语义已换成上面的独立标注集
def gold_cases() -> list[dict]:
    return load_cases()
