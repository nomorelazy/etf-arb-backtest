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

__all__ = [
    "FilePCFRepository",
    "MinuteCloseBar",
    "MinuteCloseConflictError",
    "MinuteCloseDataError",
    "MinuteCloseScanReport",
    "ParquetMinuteCloseRepository",
    "PCFParseError",
    "parse_pcf",
    "parse_pcf_json",
    "parse_pcf_xml",
]
