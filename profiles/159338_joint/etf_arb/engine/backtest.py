"""事件驱动回测引擎。

负责接收外部生成的交易决策、校验输入并按时间顺序回放。

不负责逐分钟信号评估、IOPV 计算、价差计算或真实交易执行。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Protocol

from etf_arb.domain import Side, TradeDecision


# ============================================================
# Exceptions
# ============================================================


class BacktestError(ValueError):
    """回测输入或事件流不符合约定。"""


class BacktestConfigError(BacktestError):
    """回测配置不合法。"""


class BacktestEventOrderError(BacktestError):
    """交易决策事件存在重复或其他事件顺序问题。"""


class DuplicateDecisionError(BacktestEventOrderError):
    """同一 ETF 在同一时间出现重复决策。"""


# ============================================================
# ETF code normalization
# ============================================================


_MARKET_PREFIXES = {
    "SH": (
        "510", "511", "512", "513", "515", "516", "517", "518",
        "560", "561", "562", "563", "588", "589",
    ),
    "SZ": (
        "159", "160", "161", "162", "163", "165", "166", "167", "168",
    ),
}


def _normalize_etf_code(value: str) -> str:
    """规范 ETF 代码，并校验可选的交易所后缀。

    接受六位 ETF 代码，可带 .SH 或 .SZ 后缀。
    带后缀时必须与 ETF 代码前缀对应的市场一致。
    不会根据未知前缀猜测交易所。
    """

    if not isinstance(value, str):
        raise TypeError("ETF code must be a string.")

    text = value.strip().upper()
    suffix: str | None = None

    if "." in text:
        code, suffix = text.rsplit(".", 1)
        if suffix not in _MARKET_PREFIXES:
            raise ValueError(
                f"Unsupported ETF market suffix: {value!r}"
            )
    else:
        code = text

    if (
        len(code) != 6
        or not code.isascii()
        or not code.isdigit()
    ):
        raise ValueError(
            f"ETF code must contain six ASCII digits: {value!r}"
        )

    matching_markets = tuple(
        market
        for market, prefixes in _MARKET_PREFIXES.items()
        if code.startswith(prefixes)
    )
    if not matching_markets:
        raise ValueError(
            f"ETF code has an unsupported prefix: {value!r}"
        )

    if suffix is not None and suffix not in matching_markets:
        raise ValueError(
            f"ETF code and market suffix do not match: {value!r}"
        )

    return code


# ============================================================
# Configuration
# ============================================================


@dataclass(frozen=True)
class BacktestConfig:
    """回测配置，日期范围为闭区间。"""

    start_date: date
    end_date: date
    etf_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            if type(self.start_date) is not date:
                raise TypeError("start_date must be a date.")

            if type(self.end_date) is not date:
                raise TypeError("end_date must be a date.")

            if self.start_date > self.end_date:
                raise ValueError(
                    "start_date must not be after end_date."
                )

            codes = tuple(
                _normalize_etf_code(code)
                for code in self.etf_codes
            )

            if not codes:
                raise ValueError(
                    "At least one ETF code is required."
                )

            if len(set(codes)) != len(codes):
                raise ValueError(
                    "Duplicate ETF codes are not allowed."
                )

        except (TypeError, ValueError) as error:
            raise BacktestConfigError(str(error)) from error

        object.__setattr__(self, "etf_codes", codes)


# ============================================================
# Handler interface
# ============================================================


class DecisionHandler(Protocol):
    """后续执行模块的接入接口。

    handler 负责自己的事务边界。
    Engine 不承诺在 handler 失败时回滚已完成的处理。
    """

    def handle(self, decision: TradeDecision) -> None:
        ...


# ============================================================
# Result
# ============================================================


@dataclass(frozen=True)
class BacktestResult:
    """决策事件回放结果。

    所有持久化字段均使用不可变、可哈希的数据结构。
    decisions_by_etf 属性返回一个新的字典副本。
    """

    start_date: date
    end_date: date
    decisions: tuple[TradeDecision, ...]
    decision_counts: tuple[tuple[str, int], ...]
    eligible_count: int
    rejected_count: int
    dispatched_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decisions",
            tuple(self.decisions),
        )
        object.__setattr__(
            self,
            "decision_counts",
            tuple(
                (code, count)
                for code, count in self.decision_counts
            ),
        )

    @property
    def decision_count(self) -> int:
        return len(self.decisions)

    @property
    def decisions_by_etf(self) -> Mapping[str, int]:
        """每只 ETF 的决策数量，返回独立副本。"""
        return dict(self.decision_counts)


# ============================================================
# Decision validation
# ============================================================


def _normalize_decision(
    decision: TradeDecision,
) -> TradeDecision:
    """验证并规范化外部策略模块提供的决策。"""

    if not isinstance(decision, TradeDecision):
        raise TypeError(
            "Decision source must yield TradeDecision objects."
        )

    code = _normalize_etf_code(decision.etf_code)

    if not isinstance(decision.ts, datetime):
        raise TypeError(
            "TradeDecision.ts must be a datetime."
        )

    # 项目数据采用无时区的北京时间，不进行隐式转换。
    if decision.ts.tzinfo is not None:
        raise ValueError(
            "TradeDecision.ts must be timezone-naive."
        )

    if type(decision.eligible) is not bool:
        raise TypeError(
            "TradeDecision.eligible must be a bool."
        )

    units = decision.quantity_units
    if (
        isinstance(units, bool)
        or not isinstance(units, int)
        or units < 0
    ):
        raise ValueError(
            "quantity_units must be a non-negative integer."
        )

    if decision.eligible and units == 0:
        raise ValueError(
            "An eligible decision must contain at least one "
            "complete PCF application unit."
        )

    try:
        side = Side(decision.side)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Unsupported decision side: {decision.side!r}"
        ) from error

    # 必须先排除 str，避免将字符串逐字符拆分。
    raw_reasons = decision.reasons
    if isinstance(raw_reasons, str):
        raise TypeError(
            "Decision reasons must be a sequence of strings, "
            "not a string."
        )

    if not isinstance(raw_reasons, Sequence):
        raise TypeError(
            "Decision reasons must be a sequence of strings."
        )

    reasons = tuple(raw_reasons)
    if any(not isinstance(reason, str) for reason in reasons):
        raise TypeError(
            "Every decision reason must be a string."
        )

    for field_name in (
        "gross_edge",
        "estimated_cost",
        "estimated_net_edge",
    ):
        value = getattr(decision, field_name)

        if not isinstance(value, Decimal):
            raise TypeError(
                f"{field_name} must be a Decimal."
            )

        if not value.is_finite():
            raise ValueError(
                f"{field_name} must be finite."
            )

    return replace(
        decision,
        etf_code=code,
        side=side,
        reasons=reasons,
    )


# ============================================================
# Main entry point
# ============================================================


def run_backtest(
    config: BacktestConfig,
    decisions: Iterable[TradeDecision],
    *,
    handler: DecisionHandler | None = None,
) -> BacktestResult:
    """回放外部策略模块产生的交易决策。

    输入事件允许乱序；Engine 会在完整校验后按
    (ts, etf_code) 排序，再交给 handler。

    同一 ETF 在同一时间只能出现一条决策。

    handler 异常会向上传播。此前已经完成的 handler 调用
    不会由 Engine 自动回滚，因此 handler 应自行定义事务边界。

    本函数不重新计算信号、不读取行情、不生成订单，
    也不代表决策已经成交。
    """

    if not isinstance(config, BacktestConfig):
        raise BacktestConfigError(
            "config must be a BacktestConfig."
        )

    allowed_etfs = set(config.etf_codes)
    normalized: list[TradeDecision] = []
    seen_keys: set[tuple[datetime, str]] = set()

    # 第一阶段：完整校验输入，不调用 handler。
    for raw_decision in decisions:
        decision = _normalize_decision(raw_decision)

        if decision.etf_code not in allowed_etfs:
            raise BacktestError(
                f"ETF is outside configured universe: "
                f"ETF={decision.etf_code}, ts={decision.ts}"
            )

        if not (
            config.start_date
            <= decision.ts.date()
            <= config.end_date
        ):
            raise BacktestError(
                f"Decision is outside configured date range: "
                f"ETF={decision.etf_code}, ts={decision.ts}"
            )

        key = (decision.ts, decision.etf_code)
        if key in seen_keys:
            raise DuplicateDecisionError(
                f"Duplicate decision: "
                f"ETF={decision.etf_code}, ts={decision.ts}"
            )

        seen_keys.add(key)
        normalized.append(decision)

    # 第二阶段：同一时间戳的不同 ETF 不要求上游按代码排序。
    normalized.sort(key=lambda item: (item.ts, item.etf_code))

    # 第三阶段：统计并分发已通过资格检查的决策。
    counts = {code: 0 for code in config.etf_codes}
    eligible_count = 0
    rejected_count = 0
    dispatched_count = 0

    for decision in normalized:
        counts[decision.etf_code] += 1

        if not decision.eligible:
            rejected_count += 1
            continue

        eligible_count += 1

        if handler is not None:
            # handler 可能产生外部副作用；失败时不自动回滚。
            handler.handle(decision)
            dispatched_count += 1

    return BacktestResult(
        start_date=config.start_date,
        end_date=config.end_date,
        decisions=tuple(normalized),
        decision_counts=tuple(counts.items()),
        eligible_count=eligible_count,
        rejected_count=rejected_count,
        dispatched_count=dispatched_count,
    )


__all__ = [
    "BacktestConfig",
    "BacktestConfigError",
    "BacktestError",
    "BacktestEventOrderError",
    "BacktestResult",
    "DecisionHandler",
    "DuplicateDecisionError",
    "run_backtest",
]
