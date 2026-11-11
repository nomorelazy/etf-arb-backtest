"""Configuration and external input loading for BacktestRunner (no strategy logic)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from etf_arb.data.missing import ifind_minute_grid
from etf_arb.costs import FeeRates
from etf_arb.domain import Side, TradeDecision
from etf_arb.engine.backtest import BacktestConfig
from etf_arb.data.snapshot import FULL_FLOW_ETFS
from etf_arb.rules import ETFRule, InMemoryRuleRepository, SecurityTradingRule
from etf_arb.simulation_config import SimulationConfig


class RunnerConfigError(ValueError):
    """Runner configuration or an external input has an invalid shape."""


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_float=Decimal)


@dataclass(frozen=True)
class RunnerConfig:
    start_date: date
    end_date: date
    etf_codes: tuple[str, ...]
    pcf_root: Path
    minute_root: Path
    output_dir: Path
    trading_days: tuple[date, ...]
    starting_cash: Decimal = Decimal("1000000")
    mode: str = "replay"
    quality_check: bool = True
    clock_times: tuple[time, ...] = field(default_factory=ifind_minute_grid)
    decisions_path: Path | None = None
    rules_path: Path | None = None
    settlement_end_date: date | None = None
    simulation: SimulationConfig | None = None
    settlement_business_days: tuple[date, ...] | None = None
    cash_payment_dates_path: Path | None = None

    def __post_init__(self) -> None:
        engine = BacktestConfig(self.start_date, self.end_date, self.etf_codes)
        object.__setattr__(self, "etf_codes", engine.etf_codes)
        for name in ("pcf_root", "minute_root", "output_dir", "decisions_path", "rules_path", "cash_payment_dates_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser().resolve())
        until = self.end_date if self.settlement_end_date is None else self.settlement_end_date
        if type(until) is not date or until < self.end_date:
            raise RunnerConfigError("settlement_end_date must be on/after end_date")
        object.__setattr__(self, "settlement_end_date", until)
        days = tuple(self.trading_days)
        if any(type(day) is not date for day in days) or not days:
            raise RunnerConfigError("An explicit nonempty trading_days calendar is required")
        if tuple(sorted(set(days))) != days:
            raise RunnerConfigError("trading_days must be increasing and unique")
        selected = tuple(day for day in days if self.start_date <= day <= until)
        if not any(day <= self.end_date for day in selected):
            raise RunnerConfigError("No trading days in the opening date interval")
        object.__setattr__(self, "trading_days", selected)
        if self.settlement_business_days is not None:
            business_days = tuple(self.settlement_business_days)
            if not business_days or any(type(d) is not date for d in business_days) or tuple(sorted(set(business_days))) != business_days:
                raise RunnerConfigError("settlement_business_days must be increasing unique dates")
            object.__setattr__(self, "settlement_business_days", business_days)
        clocks = tuple(self.clock_times)
        if not clocks or any(not isinstance(t, time) or t.tzinfo is not None for t in clocks):
            raise RunnerConfigError("clock_times must contain naive Beijing-local times")
        if tuple(sorted(set(clocks))) != clocks or clocks[0] == time.min or clocks[-1] == time.max:
            raise RunnerConfigError("clock_times must be increasing, unique, and inside the day")
        object.__setattr__(self, "clock_times", clocks)
        if not isinstance(self.starting_cash, Decimal) or not self.starting_cash.is_finite() or self.starting_cash <= 0:
            raise RunnerConfigError("starting_cash must be a finite positive Decimal")
        if self.mode not in {"replay", "execute"}:
            raise RunnerConfigError("mode must be replay or execute")
        if self.simulation is not None:
            if not isinstance(self.simulation, SimulationConfig) or self.mode != "execute":
                raise RunnerConfigError("simulation requires SimulationConfig and mode=execute")
            if len(self.etf_codes) != 1 or self.etf_codes[0] not in FULL_FLOW_ETFS:
                raise RunnerConfigError(
                    f"Full-flow execution requires one ETF per run from {', '.join(FULL_FLOW_ETFS)}"
                )
            if self.decisions_path is not None:
                raise RunnerConfigError("Built-in simulation computes decisions; omit decisions_path")
        if type(self.quality_check) is not bool:
            raise RunnerConfigError("quality_check must be a bool")
        for root in (self.pcf_root, self.minute_root):
            if self.output_dir == root or self.output_dir.is_relative_to(root):
                raise RunnerConfigError("output_dir must be outside the raw data directories")

    @property
    def engine_config(self) -> BacktestConfig:
        return BacktestConfig(self.start_date, self.end_date, self.etf_codes)

    @classmethod
    def from_json(cls, path: str | Path) -> RunnerConfig:
        config_path = Path(path).expanduser().resolve()
        raw = read_json(config_path)
        if not isinstance(raw, dict):
            raise RunnerConfigError("Runner JSON must be an object")
        unknown = set(raw) - {item.name for item in fields(cls)}
        if unknown:
            raise RunnerConfigError(f"Unknown runner settings: {sorted(unknown)}")
        values = dict(raw)
        try:
            for key in ("start_date", "end_date", "settlement_end_date"):
                if values.get(key) is not None:
                    values[key] = date.fromisoformat(values[key])
            for key in ("pcf_root", "minute_root", "output_dir", "decisions_path", "rules_path", "cash_payment_dates_path"):
                if values.get(key) is not None:
                    source = Path(values[key]).expanduser()
                    values[key] = source if source.is_absolute() else config_path.parent / source
            days = values["trading_days"]
            if isinstance(days, str):
                day_path = Path(days).expanduser()
                if not day_path.is_absolute():
                    day_path = config_path.parent / day_path
                days = [line.strip() for line in day_path.read_text(encoding="utf-8-sig").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
            if not isinstance(days, list):
                raise RunnerConfigError("trading_days must be a date array or a calendar text-file path")
            values["trading_days"] = tuple(date.fromisoformat(day) for day in days)
            if values.get("settlement_business_days") is not None:
                business = values["settlement_business_days"]
                if isinstance(business, str):
                    business_path = Path(business)
                    if not business_path.is_absolute():
                        business_path = config_path.parent / business_path
                    business = [line.strip() for line in business_path.read_text(encoding="utf-8-sig").splitlines()
                                if line.strip() and not line.lstrip().startswith("#")]
                if not isinstance(business, list):
                    raise RunnerConfigError("settlement_business_days must be a date array or calendar path")
                values["settlement_business_days"] = tuple(date.fromisoformat(day) for day in business)
            if not isinstance(values.get("etf_codes"), list):
                raise RunnerConfigError("etf_codes must be an array")
            values["etf_codes"] = tuple(values["etf_codes"])
            if "clock_times" in values:
                values["clock_times"] = tuple(time.fromisoformat(value) for value in values["clock_times"])
            if "starting_cash" in values:
                values["starting_cash"] = Decimal(str(values["starting_cash"]))
            if values.get("simulation") is not None:
                values["simulation"] = SimulationConfig(**values["simulation"])
            return cls(**values)
        except (TypeError, ValueError, KeyError, ArithmeticError) as error:
            raise RunnerConfigError(f"Invalid runner config {config_path}: {error}") from error

    def to_dict(self) -> dict[str, object]:
        def value(item):
            if isinstance(item, (SimulationConfig, FeeRates)):
                return {key.name: value(getattr(item, key.name)) for key in fields(item)}
            if isinstance(item, (date, time)):
                return item.isoformat()
            if isinstance(item, (Path, Decimal)):
                return str(item)
            if isinstance(item, tuple):
                return [value(part) for part in item]
            return item
        return {item.name: value(getattr(self, item.name)) for item in fields(self)}


def load_decisions(path: str | Path) -> tuple[TradeDecision, ...]:
    """Load externally computed decisions. Runner does not infer eligibility or costs."""
    raw = read_json(path)
    if not isinstance(raw, list):
        raise RunnerConfigError("Decisions JSON must be an array")
    result = []
    for row in raw:
        if not isinstance(row, dict):
            raise RunnerConfigError("Each decision must be an object")
        values = dict(row)
        try:
            values["ts"] = datetime.fromisoformat(values["ts"])
            values["side"] = Side(values["side"])
            for name in ("gross_edge", "estimated_cost", "estimated_net_edge"):
                if isinstance(values[name], bool):
                    raise ValueError(f"{name} cannot be a bool")
                values[name] = Decimal(str(values[name]))
            reasons = values.get("reasons", [])
            if not isinstance(reasons, list):
                raise ValueError("reasons must be an array")
            values["reasons"] = tuple(reasons)
            result.append(TradeDecision(**values))
        except (TypeError, ValueError, KeyError, ArithmeticError) as error:
            raise RunnerConfigError(f"Invalid decision: {error}") from error
    return tuple(result)


def load_rules(path: str | Path) -> InMemoryRuleRepository:
    """Load explicit historical rule records; no default market rules are invented."""
    raw = read_json(path)
    if not isinstance(raw, dict) or set(raw) - {"etf_rules", "security_rules"}:
        raise RunnerConfigError("Rules JSON accepts etf_rules and security_rules arrays")
    repository = InMemoryRuleRepository()
    for key, model in (("etf_rules", ETFRule), ("security_rules", SecurityTradingRule)):
        rows = raw.get(key, [])
        if not isinstance(rows, list):
            raise RunnerConfigError(f"{key} must be an array")
        for row in rows:
            if not isinstance(row, dict):
                raise RunnerConfigError("Each rule must be an object")
            values = dict(row)
            for name, value in values.items():
                if value is None:
                    continue
                if name in {"effective_from", "effective_to"}:
                    values[name] = date.fromisoformat(value)
                elif name in {"creation_cutoff", "redemption_cutoff", "trading_start", "trading_end"}:
                    values[name] = time.fromisoformat(value)
                    if values[name].tzinfo is not None:
                        raise RunnerConfigError(f"{name} must be a naive Beijing-local time")
                elif name.endswith("enabled") or name in {"can_sell_same_day"}:
                    if type(value) is not bool:
                        raise RunnerConfigError(f"{name} must be a bool")
            repository.add(model(**values))
    return repository
