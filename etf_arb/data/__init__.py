"""输入数据适配与校验。"""

from etf_arb.data.pcf import (
    FilePCFRepository,
    PCFParseError,
    parse_pcf,
    parse_pcf_json,
    parse_pcf_xml,
)
from etf_arb.data.minute_close import (
    MinuteCloseBar,
    MinuteCloseConflictError,
    MinuteCloseDataError,
    MinuteCloseScanReport,
    ParquetMinuteCloseRepository,
)
from etf_arb.data.missing import MissingIssue, MissingReport, check_missing, ifind_minute_grid

__all__ = [
    "FilePCFRepository",
    "MinuteCloseBar",
    "MissingIssue",
    "MissingReport",
    "MinuteCloseConflictError",
    "MinuteCloseDataError",
    "MinuteCloseScanReport",
    "ParquetMinuteCloseRepository",
    "PCFParseError",
    "parse_pcf",
    "parse_pcf_json",
    "parse_pcf_xml",
    "check_missing",
    "ifind_minute_grid",
]
