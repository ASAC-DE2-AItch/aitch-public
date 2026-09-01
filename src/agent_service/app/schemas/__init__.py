"""Agent Service 입출력 스키마 (Pydantic v2).

- alert.py   : fdc.alert 수신 계약 (AlertModel) — API Contract §3
- report.py  : 리포트 3종 (recipe/limit/manual option) — Contract §5 / 프롬프트 라이브러리 §1~§3
- (추후) brief.py : Supervisor Brief — Contract §6 / 프롬프트 라이브러리 §4
"""

from .alert import (
    AlertModel,
    PredictionContext,
    SpcFlag,
    SuspectWindow,
    Tttm,
    Violation,
)
from .report import (
    LimitOption,
    ManualOption,
    OptionReport,
    OptionReportBase,
    RecipeOption,
    make_report_id,
)

__all__ = [
    # alert
    "AlertModel",
    "Violation",
    "Tttm",
    "SuspectWindow",
    "PredictionContext",
    "SpcFlag",
    # report
    "OptionReport",
    "OptionReportBase",
    "RecipeOption",
    "LimitOption",
    "ManualOption",
    "make_report_id",
]
