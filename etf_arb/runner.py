"""Top-level configuration, repositories and deterministic backtest clock.

Runner owns the clock; Engine validates and dispatches decisions. Signal,
execution and settlement implementations are injected, not calculated here.
"""

from __future__ import annotations

import csv
import heapq
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from etf_arb.data import FilePCFRepository, ParquetMinuteCloseRepository, check_missing
from etf_arb.data.minute_close import MinuteCloseBar
from etf_arb.domain import PCF, TradeDecision
from etf_arb.engine.backtest import BacktestResult, run_backtest
from etf_arb.portfolio.ledger import PortfolioLedger
from etf_arb.rules import InMemoryRuleRepository, RuleRepository
from etf_arb.runner_config import RunnerConfig, RunnerConfigError, load_decisions, load_rules


class RunnerError(RuntimeError):
    pass


class DataQualityError(RunnerError):
    pass


@dataclass(frozen=True)
class RunnerResources:
    config: RunnerConfig
    pcf: FilePCFRepository
    market: ParquetMinuteCloseRepository
    rules: RuleRepository
    portfolio: PortfolioLedger


@dataclass(frozen=True)
class RunnerContext:
    now: datetime
    resources: RunnerResources
    pcfs: Mapping[str, PCF]
    schedule: Callable[[datetime, Callable, str], None] = field(repr=False)

    def get_close(self, security_code: str, *, at: datetime | None = None) -> MinuteCloseBar | None:
        """Same-day as-of close. No future prices or previous-day fallback."""
        until = self.now if at is None else at
        if not isinstance(until, datetime) or until.tzinfo is not None or until > self.now:
            raise RunnerError("Quote queries must use a naive timestamp at/before the runner clock")
        if until.date() != self.now.date():
            raise RunnerError("get_close is same-day only; request historical data explicitly from Data")
        bars = self.resources.market.get_closes(
            security_code, start=datetime.combine(until.date(), time.min), end=until,
        )
        return bars[-1] if bars else None


class DecisionSource(Protocol):
    def on_time(self, context: RunnerContext) -> Iterable[TradeDecision]:
        """Return externally calculated decisions for exactly context.now."""
        ...


class DecisionExecutor(Protocol):
    def handle(self, decision: TradeDecision, context: RunnerContext) -> None:
        """Execution owns transaction boundaries and may schedule later settlement."""
        ...


@dataclass(frozen=True)
class RunnerHooks:
    on_day_open: Callable[[RunnerContext], None] | None = None
    on_time: Callable[[RunnerContext], None] | None = None
    on_day_close: Callable[[RunnerContext], None] | None = None


@dataclass(frozen=True)
class RunnerEvent:
    ts: datetime
    kind: str
    label: str


@dataclass(frozen=True)
class RunnerResult:
    mode: str
    processed_dates: tuple[date, ...]
    events: tuple[RunnerEvent, ...]
    engine_result: BacktestResult
    output_dir: Path
    quality_summary: Mapping[str, object] | None


@dataclass
class _ContextHandler:
    executor: DecisionExecutor
    context: RunnerContext

    def handle(self, decision: TradeDecision) -> None:
        self.executor.handle(decision, self.context)


class BacktestRunner:
    """Single-use runner. A failed callback is propagated, never silently skipped.

    Within one timestamp: day open, queued callbacks, clock/source, Engine
    dispatch, day close. Same-time callbacks created during an event execute
    after that event, so a callback cannot move the clock or phase backwards.
    Replay outputs describe decisions only and never claim fills or profits.
    """

    def __init__(
        self,
        config: RunnerConfig,
        *,
        source: DecisionSource | None = None,
        executor: DecisionExecutor | None = None,
        hooks: RunnerHooks | None = None,
        rules: RuleRepository | None = None,
        portfolio: PortfolioLedger | None = None,
        reporter: Callable[[RunnerResult, RunnerResources], None] | None = None,
    ) -> None:
        if not isinstance(config, RunnerConfig):
            raise TypeError("config must be RunnerConfig")
        if config.mode == "execute" and executor is None:
            raise RunnerConfigError("execute mode requires an explicit DecisionExecutor")
        if config.mode == "replay" and executor is not None:
            raise RunnerConfigError("An executor requires mode=execute; replay never executes trades")
        if config.mode == "execute" and rules is None and config.rules_path is None:
            raise RunnerConfigError("execute mode requires explicit historical rules")
        if rules is not None and config.rules_path is not None:
            raise RunnerConfigError("Pass rules or rules_path, not both")
        self.config, self.source, self.executor = config, source, executor
        self.hooks = RunnerHooks() if hooks is None else hooks
        self._rules, self._portfolio, self.reporter = rules, portfolio, reporter
        self.resources: RunnerResources | None = None
        self.state = "new"
        self._queue: list[tuple[datetime, int, int, str, str, Callable]] = []
        self._sequence = 0
        self._now: datetime | None = None
        self._phase = -1
        self._label = ""
        self._pcfs: Mapping[str, PCF] = MappingProxyType({})
        self._events: list[RunnerEvent] = []
        self._decisions: dict[datetime, list[TradeDecision]] = defaultdict(list)
        self._dispatch_times: set[datetime] = set()
        self._engine_batches: list[BacktestResult] = []

    @classmethod
    def from_json(cls, path: str | Path, **components) -> BacktestRunner:
        return cls(RunnerConfig.from_json(path), **components)

    def _context(self) -> RunnerContext:
        if self.resources is None or self._now is None:
            raise RunnerError("Runner context is only available during the event loop")
        return RunnerContext(self._now, self.resources, self._pcfs, self.schedule)

    def _push(self, at: datetime, phase: int, kind: str, label: str, callback: Callable) -> None:
        self._sequence += 1
        heapq.heappush(self._queue, (at, phase, self._sequence, kind, label, callback))

    def schedule(self, at: datetime, callback: Callable[[RunnerContext], None], label: str = "callback") -> None:
        """Schedule execution/settlement work within the explicit run calendar.

        For later settlement, extend settlement_end_date and trading_days.
        Events outside that horizon fail rather than being silently discarded.
        """
        if self.state != "running" or self._now is None:
            raise RunnerError("Events can only be scheduled from an active runner callback")
        if not isinstance(at, datetime) or at.tzinfo is not None or at < self._now:
            raise RunnerError("Cannot schedule an aware timestamp or an event in the past")
        if at.date() not in self.config.trading_days:
            raise RunnerError("Scheduled date is outside the run calendar; extend the settlement horizon if needed")
        if at == self._now and self._phase >= 90:
            raise RunnerError("The current day is closing; schedule work before close or on a later date")
        if not callable(callback):
            raise TypeError("callback must be callable")
        phase = max(10, self._phase) if at == self._now else 10
        self._push(at, phase, "CALLBACK", str(label), callback)

    def _initialize(self) -> None:
        config = self.config
        for root in (config.pcf_root, config.minute_root):
            if not root.is_dir():
                raise FileNotFoundError(f"Data directory does not exist: {root}")
        if config.output_dir.exists() and any(config.output_dir.iterdir()):
            raise RunnerError("output_dir is not empty; choose a fresh run directory")
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "config.json").write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rules = self._rules
        if rules is None:
            rules = InMemoryRuleRepository() if config.rules_path is None else load_rules(config.rules_path)
        if config.mode == "execute" or config.rules_path is not None or self._rules is not None:
            for day in config.trading_days:
                if day <= config.end_date:
                    for code in config.etf_codes:
                        rules.get(code, day)  # No fallback from missing historical ETF rules.
        self.resources = RunnerResources(
            config=config, pcf=FilePCFRepository(config.pcf_root),
            market=ParquetMinuteCloseRepository(
                config.minute_root, catalog_path=config.output_dir / "market_catalog.sqlite3",
                auto_scan=False,
            ),
            rules=rules, portfolio=PortfolioLedger() if self._portfolio is None else self._portfolio,
        )

    def _queue_decisions(self, values: Iterable[TradeDecision], *, at: datetime | None = None) -> None:
        # Engine owns all decision normalization, duplicate and universe checks.
        validated = run_backtest(self.config.engine_config, values)
        for decision in validated.decisions:
            if decision.ts.date() not in self.config.trading_days:
                raise RunnerError(f"Decision date is outside the calendar: {decision.ts}")
            if at is not None and decision.ts != at:
                raise RunnerError("DecisionSource must return decisions for exactly the current clock")
        for decision in validated.decisions:
            self._decisions[decision.ts].append(decision)
            if decision.ts not in self._dispatch_times:
                self._dispatch_times.add(decision.ts)
                self._push(decision.ts, 30, "DECISIONS", "engine.dispatch", self._dispatch)

    def _dispatch(self, context: RunnerContext) -> None:
        handler = None if self.executor is None else _ContextHandler(self.executor, context)
        # Validate the combined fixed/source batch before any handler is called.
        result = run_backtest(
            self.config.engine_config, self._decisions.pop(context.now), handler=handler,
        )
        self._engine_batches.append(result)

    def _day_open(self, context: RunnerContext) -> None:
        self._pcfs = MappingProxyType({
            code: context.resources.pcf.get(code, context.now.date()) for code in self.config.etf_codes
        })
        if self.hooks.on_day_open is not None:
            self.hooks.on_day_open(self._context())

    def _clock(self, context: RunnerContext) -> None:
        if self.hooks.on_time is not None:
            self.hooks.on_time(context)
        if self.source is not None and context.now.date() <= self.config.end_date:
            self._queue_decisions(self.source.on_time(context), at=context.now)

    def _day_close(self, context: RunnerContext) -> None:
        if self.hooks.on_day_close is not None:
            self.hooks.on_day_close(context)

    def run(self, decisions: Iterable[TradeDecision] | None = None) -> RunnerResult:
        if self.state != "new":
            raise RunnerError("BacktestRunner is single-use; construct another runner for a new run")
        # Reject a reused output directory before failure reporting can overwrite it.
        if self.config.output_dir.exists() and any(self.config.output_dir.iterdir()):
            raise RunnerError("output_dir is not empty; choose a fresh run directory")
        if decisions is not None and self.config.decisions_path is not None:
            raise RunnerConfigError("Pass decisions or decisions_path, not both")
        self.state = "running"
        quality = None
        try:
            values = load_decisions(self.config.decisions_path) if self.config.decisions_path else (() if decisions is None else decisions)
            self._queue_decisions(values)
            self._initialize()
            if self.config.quality_check:
                checked = check_missing(
                    pcf_root=self.config.pcf_root, minute_root=self.config.minute_root,
                    etf_codes=self.config.etf_codes, start_date=self.config.start_date,
                    end_date=self.config.settlement_end_date,
                    trading_days=self.config.trading_days, expected_times=self.config.clock_times,
                )
                checked.save(self.config.output_dir / "quality")
                quality = checked.summary()
                if not quality["passed"]:
                    raise DataQualityError("Data quality check failed; see quality/summary.json and missing.csv")
            assert self.resources is not None
            self.resources.market.scan()
            for day in self.config.trading_days:
                self._push(datetime.combine(day, time.min), 0, "DAY_OPEN", "day.open", self._day_open)
                for clock in self.config.clock_times:
                    self._push(datetime.combine(day, clock), 20, "CLOCK", "clock", self._clock)
                self._push(datetime.combine(day, time.max), 90, "DAY_CLOSE", "day.close", self._day_close)
            while self._queue:
                ts, phase, _, kind, label, callback = heapq.heappop(self._queue)
                self._now, self._phase, self._label = ts, phase, label
                callback(self._context())
                self._events.append(RunnerEvent(ts, kind, label))
            counts = {code: 0 for code in self.config.etf_codes}
            for batch in self._engine_batches:
                for code, count in batch.decision_counts:
                    counts[code] += count
            engine = BacktestResult(
                self.config.start_date, self.config.end_date,
                tuple(decision for batch in self._engine_batches for decision in batch.decisions),
                tuple(counts.items()),
                sum(batch.eligible_count for batch in self._engine_batches),
                sum(batch.rejected_count for batch in self._engine_batches),
                sum(batch.dispatched_count for batch in self._engine_batches),
            )
            result = RunnerResult(
                self.config.mode, self.config.trading_days, tuple(self._events), engine,
                self.config.output_dir, None if quality is None else MappingProxyType(quality),
            )
            if self.reporter is not None:
                self.reporter(result, self.resources)
            self.state = "completed"
            self._write_outputs(result)
            return result
        except Exception as error:
            self.state = "failed"
            if self.config.output_dir.is_dir():
                (self.config.output_dir / "run_summary.json").write_text(json.dumps({
                    "state": "failed", "error_type": type(error).__name__, "error": str(error),
                    "last_time": None if self._now is None else self._now.isoformat(),
                    "last_event": self._label, "successful_event_count": len(self._events),
                    "note": "Callbacks own transaction boundaries; earlier work is not rolled back.",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            raise

    def _write_outputs(self, result: RunnerResult) -> None:
        engine = result.engine_result
        summary = {
            "state": self.state, "mode": self.config.mode,
            "start_date": self.config.start_date.isoformat(), "end_date": self.config.end_date.isoformat(),
            "settlement_end_date": self.config.settlement_end_date.isoformat(),
            "processed_dates": [day.isoformat() for day in result.processed_dates],
            "event_count": len(result.events), "decision_count": engine.decision_count,
            "eligible_count": engine.eligible_count, "rejected_count": engine.rejected_count,
            "dispatched_count": engine.dispatched_count, "decisions_by_etf": engine.decisions_by_etf,
            "quality_check_performed": result.quality_summary is not None,
            "execution_handler_connected": self.executor is not None,
            "financial_results_available": False,
            "net_pnl": None,
            "note": "Runner dispatch counts are not fills. Financial reporting belongs to Execution/Settlement/Portfolio and the injected reporter.",
        }
        (self.config.output_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (self.config.output_dir / "event_log.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ts", "kind", "label"))
            writer.writerows((event.ts.isoformat(), event.kind, event.label) for event in result.events)
        with (self.config.output_dir / "decisions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            names = tuple(TradeDecision.__dataclass_fields__)
            writer = csv.DictWriter(handle, fieldnames=names)
            writer.writeheader()
            for decision in engine.decisions:
                row = {name: getattr(decision, name) for name in names}
                row.update(ts=decision.ts.isoformat(), side=decision.side.value,
                           reasons=json.dumps(decision.reasons, ensure_ascii=False))
                writer.writerow(row)


__all__ = [
    "BacktestRunner", "RunnerConfig", "RunnerConfigError", "RunnerContext", "RunnerResources",
    "RunnerHooks", "RunnerResult", "RunnerError", "DataQualityError", "DecisionSource", "DecisionExecutor",
]
