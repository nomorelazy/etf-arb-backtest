"""Environment 与 Condition 合并后的唯一交易决策入口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from etf_arb.domain import Side, TradeDecision


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    reason: str


class DecisionGate:
    def evaluate(
        self,
        *,
        etf_code: str,
        ts: datetime,
        side: Side,
        quantity_units: int,
        gross_edge: Decimal,
        estimated_cost: Decimal,
        min_net_edge: Decimal,
        checks: Sequence[GateCheck],
    ) -> TradeDecision:
        failed_reasons = [check.reason for check in checks if not check.passed]
        net_edge = gross_edge - estimated_cost
        if quantity_units <= 0:
            failed_reasons.append("Planned申赎单位数必须大于零。")
        if net_edge < min_net_edge:
            failed_reasons.append("估算净价差低于策略门槛。")

        return TradeDecision(
            etf_code=etf_code,
            ts=ts,
            eligible=not failed_reasons,
            side=side,
            quantity_units=quantity_units,
            gross_edge=gross_edge,
            estimated_cost=estimated_cost,
            estimated_net_edge=net_edge,
            reasons=tuple(failed_reasons),
        )
