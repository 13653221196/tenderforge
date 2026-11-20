"""技能实现注册表。

键必须与 ``skills/**/SKILL.md`` 的 ``name`` 一致；注册了实现即为「可执行」，
只有描述没有实现的技能会显示为「仅文档」。
"""
from __future__ import annotations

from .check import (
    ai_text_check,
    consistency_check,
    deposit_check,
    doc_integrity,
    ebid_submit_check,
    fit_score,
    joint_bid_check,
    mandatory_req_check,
    pricing_logic,
    qualification_check,
    risk_score,
    sample_report_check,
    selfcheck_list,
    signature_check,
    validity_check,
)
from .generate import mandatory_req_extract, structure_template
from .interpret import risk_alert, scoring_matrix
from .output import check_report, revision

RUNNERS = {
    # 招标解读
    "scoring_matrix": scoring_matrix,
    "risk_alert": risk_alert,
    # 标书生成
    "mandatory_req_extract": mandatory_req_extract,
    "structure_template": structure_template,
    # 标书检查
    "qualification_check": qualification_check,
    "deposit_check": deposit_check,
    "signature_check": signature_check,
    "validity_check": validity_check,
    "doc_integrity": doc_integrity,
    "mandatory_req_check": mandatory_req_check,
    "consistency_check": consistency_check,
    "ai_text_check": ai_text_check,
    "pricing_logic": pricing_logic,
    "fit_score": fit_score,
    "risk_score": risk_score,
    "selfcheck_list": selfcheck_list,
    "sample_report_check": sample_report_check,
    "joint_bid_check": joint_bid_check,
    "ebid_submit_check": ebid_submit_check,
    # 输出交付
    "check_report": check_report,
    "revision": revision,
}

__all__ = ["RUNNERS"]
