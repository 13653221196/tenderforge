"""同义查询对照集：量出纯词法检索的基线命中率（方案第 8 节「批次 0」）。

用法：

    python -m backend.eval.vector_contrast

零费用、零外发：只做本地 char-2gram 比对，不调用任何模型、不写业务库、
不启服务、不建向量表。输出的是「按字面找不到的那些说法」——它们正是向量
检索理论上能补回的候选，也是决定要不要默认开启的依据。

混合检索（词法 + 百炼向量）的真实命中率、误引入率不能靠本脚本推算：必须在
填好密钥、灰度企业开启后用同一批对照集实测，并人工核对语义召回片段是否适用。
"""
from __future__ import annotations

import json

from ..app import rag

# (招标文件里的说法, 企业资料里的说法, 业务标签)
PAIRS: list[tuple[str, str, str]] = [
    ("项目负责人", "项目经理", "人员"),
    ("社会保险缴纳证明", "养老保险缴费凭证", "社保"),
    ("质量管理体系认证", "ISO9001 证书", "体系认证"),
    ("近三年类似业绩", "同类项目合同", "业绩"),
    ("投标保证金", "担保金额", "保证金"),
    ("法定代表人授权", "法人委托书", "授权"),
    ("售后服务响应时限", "故障到场时间", "服务"),
    ("营业执照副本", "企业法人营业执照", "执照"),
    ("财务状况报告", "年度审计报告", "财务"),
    ("依法缴纳税收记录", "完税证明", "税务"),
    ("无重大违法记录声明", "廉洁从业承诺书", "合规"),
    ("技术规格响应表", "参数偏离对照表", "响应表"),
    ("交货期", "供货周期", "交付"),
    ("质保期", "免费保修年限", "质保"),
    ("验收标准", "交付验收规范", "验收"),
    ("项目经理资质", "项目负责人资格证书", "资质"),
    ("安全生产许可证", "施工安全资质", "安全"),
    ("中小企业声明函", "小微企业认定材料", "政策"),
    ("信用记录查询", "企业征信报告", "信用"),
    ("投标有效期", "报价有效期限", "有效期"),
    ("履约保证金", "合同履约担保", "履约"),
    ("培训计划", "用户培训方案", "培训"),
    ("备品备件清单", "易损件清单", "备件"),
    ("实施方案", "施工组织设计", "实施"),
    ("保密协议", "信息安全承诺书", "保密"),
    ("知识产权承诺", "软件著作权声明", "知识产权"),
    ("第三方检测报告", "检验机构出具的报告", "检测"),
    ("原厂授权书", "制造商授权函", "授权"),
    ("分项报价明细表", "分项报价单", "报价"),
    ("废标条款", "否决投标情形", "废标"),
]


def lexical_hit(query: str, doc_text: str) -> bool:
    """与 rag._score 的字面口径一致：query 与正文的 2-gram 有交集即算命中。"""
    return bool(rag._grams(query) & rag._grams(doc_text))


def report() -> dict:
    rows = [{"label": label, "query": q, "doc": d, "lexical_hit": lexical_hit(q, d)}
            for q, d, label in PAIRS]
    hits = sum(1 for r in rows if r["lexical_hit"])
    missed = [r for r in rows if not r["lexical_hit"]]
    return {
        "total": len(rows),
        "lexical_hits": hits,
        "lexical_recall": round(hits / max(1, len(rows)), 3),
        "semantic_candidates": len(missed),
        "missed": missed,
        "rows": rows,
        "note": "仅本地词法基线；混合检索命中率须在配置百炼后实测，不能据此推算。",
    }


def main() -> int:
    r = report()
    assert r["total"] >= 30, "对照集至少 30 组"
    assert 0.0 <= r["lexical_recall"] <= 1.0
    assert r["lexical_hits"] + r["semantic_candidates"] == r["total"]
    print(f"纯词法基线：{r['lexical_hits']}/{r['total']} 命中，"
          f"命中率 {r['lexical_recall']:.1%}；字面找不到 {r['semantic_candidates']} 组")
    for m in r["missed"]:
        print(f"  [{m['label']}] {m['query']}  ↔  {m['doc']}")
    print(r["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
