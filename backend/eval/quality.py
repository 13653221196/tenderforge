"""兼容层：评测脚本继续 import backend.eval.quality，实现已收敛到 app/quality.py。

主链路与验收现在调用同一套核查逻辑，避免「评测查得出来、写标书时却不知道」。
"""
from backend.app.quality import (  # noqa: F401
    assess, assess_project, assess_section, defects, defect_lines,
    export_issues, project_sources,
    CHECK_KEYS, CHECK_LABELS, PLACEHOLDER_RE, MIN_CHARS,
    COVERAGE_TARGET, OVERLAP_HIT, HIGH_KEYS,
)
