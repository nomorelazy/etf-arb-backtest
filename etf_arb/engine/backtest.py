"""事件驱动回测编排接口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    etf_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date.")
        if not self.etf_codes:
            raise ValueError("At least one ETF code is required.")


def run_backtest(config: BacktestConfig) -> None:
    """待 P2 接通行情、决策、撮合和账本后实现。"""
    raise NotImplementedError("Backtest orchestration is planned for P2.")
