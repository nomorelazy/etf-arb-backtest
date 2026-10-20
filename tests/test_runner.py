import json
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from etf_arb.domain import Side, TradeDecision
from etf_arb.engine.backtest import DuplicateDecisionError
from etf_arb.portfolio.ledger import LedgerEvent, LedgerEventType
from etf_arb.rules import ETFRule, InMemoryRuleRepository
from etf_arb.runner import (
    BacktestRunner, DataQualityError, RunnerConfig, RunnerConfigError, RunnerError, RunnerHooks,
)
from etf_arb.runner_config import load_decisions, load_rules
from test_missing_data import CLOCKS, DAY, complete_rows, write_market, write_pcf


NEXT_DAY = date(2025, 1, 3)


def decision(day=DAY, *, clock=CLOCKS[0], code="159338", eligible=True):
    return TradeDecision(code, datetime.combine(day, clock), eligible, Side.CREATE, 1,
                         Decimal("10"), Decimal("2"), Decimal("8"), ())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pcf, self.market = self.root / "pcf", self.root / "market"
        self.pcf.mkdir()
        self.market.mkdir()
        write_pcf(self.pcf)
        write_pcf(self.pcf, NEXT_DAY)
        write_market(self.market, complete_rows() + complete_rows(NEXT_DAY))
        self.config = RunnerConfig(
            DAY, NEXT_DAY, ("159338.SZ",), self.pcf, self.market, self.root / "run",
            (DAY, NEXT_DAY), clock_times=CLOCKS,
        )
        self.rules = InMemoryRuleRepository([
            ETFRule("159338", DAY, NEXT_DAY, True, True)
        ])

    def write_config(self, **overrides):
        raw = self.config.to_dict()
        raw.update(overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_replay_initializes_repositories_runs_clock_and_writes_honest_outputs(self):
        seen = []
        runner = BacktestRunner(self.config, hooks=RunnerHooks(
            on_day_open=lambda c: seen.append(("open", c.now.date(), c.pcfs["159338"].effective_date)),
            on_day_close=lambda c: seen.append(("close", c.now.date(), c.pcfs["159338"].effective_date)),
        ))
        result = runner.run([decision(NEXT_DAY, eligible=False), decision()])
        self.assertEqual(runner.state, "completed")
        self.assertEqual(result.engine_result.decision_count, 2)
        self.assertEqual(result.engine_result.eligible_count, 1)
        self.assertEqual(result.engine_result.rejected_count, 1)
        self.assertEqual(result.engine_result.dispatched_count, 0)
        self.assertEqual(seen, [("open", DAY, DAY), ("close", DAY, DAY),
                                ("open", NEXT_DAY, NEXT_DAY), ("close", NEXT_DAY, NEXT_DAY)])
        self.assertEqual([e.ts for e in result.events], sorted(e.ts for e in result.events))
        self.assertEqual(result.quality_summary["passed"], True)
        summary = json.loads((self.config.output_dir / "run_summary.json").read_text())
        self.assertFalse(summary["financial_results_available"])
        self.assertIsNone(summary["net_pnl"])
        for name in ("config.json", "event_log.csv", "decisions.csv", "market_catalog.sqlite3"):
            self.assertTrue((self.config.output_dir / name).exists())
        self.assertFalse(list(self.market.rglob("*.sqlite3")))

    def test_context_close_query_cannot_see_next_minute_or_previous_day(self):
        observations = []
        def clock(context):
            bar = context.get_close("159338.SZ")
            self.assertLessEqual(bar.time, context.now)
            observations.append(bar.time)
            with self.assertRaises(RunnerError):
                context.get_close("159338.SZ", at=datetime(2025, 1, 3, 9, 32))
            if context.now.date() == NEXT_DAY:
                with self.assertRaises(RunnerError):
                    context.get_close("159338.SZ", at=datetime(2025, 1, 2, 9, 30))
        BacktestRunner(self.config, hooks=RunnerHooks(on_time=clock)).run()
        self.assertEqual(observations, [datetime.combine(d, t) for d in (DAY, NEXT_DAY) for t in CLOCKS])

    def test_clock_source_decisions_are_dispatched_without_signal_recalculation(self):
        class Source:
            def on_time(self, context):
                return [decision(context.now.date(), clock=context.now.time())]
        result = BacktestRunner(self.config, source=Source()).run()
        self.assertEqual(result.engine_result.decision_count, 4)
        self.assertEqual(result.engine_result.dispatched_count, 0)

    def test_source_cannot_submit_future_decisions(self):
        class Source:
            def on_time(self, context):
                return [decision(NEXT_DAY)]
        runner = BacktestRunner(self.config, source=Source())
        with self.assertRaisesRegex(RunnerError, "exactly the current clock"):
            runner.run()
        self.assertEqual(runner.state, "failed")

    def test_combined_source_and_fixed_duplicates_fail_before_execution(self):
        calls = []
        class Source:
            def on_time(self, context):
                return [decision()]
        class Executor:
            def handle(self, value, context):
                calls.append(value)
        runner = BacktestRunner(replace(self.config, mode="execute"), source=Source(),
                                executor=Executor(), rules=self.rules)
        with self.assertRaises(DuplicateDecisionError):
            runner.run([decision()])
        self.assertEqual(calls, [])

    def test_scheduled_settlement_runs_before_next_day_clock_and_tail_has_no_new_signals(self):
        order, source_times = [], []
        class Source:
            def on_time(self, context):
                source_times.append(context.now)
                return []
        class Executor:
            def handle(self, value, context):
                order.append(("decision", context.now))
                def immediate(c):
                    order.append(("immediate", c.now))
                def settle(c):
                    order.append(("settle", c.now))
                    c.resources.portfolio.record(LedgerEvent(c.now, LedgerEventType.CASH_SETTLED, "manual"))
                context.schedule(context.now, immediate, "immediate")
                context.schedule(datetime(2025, 1, 3, 9, 30), settle, "settlement")
        config = replace(self.config, end_date=DAY, settlement_end_date=NEXT_DAY, mode="execute")
        runner = BacktestRunner(config, source=Source(), executor=Executor(), rules=self.rules,
                                hooks=RunnerHooks(on_time=lambda c: order.append(("clock", c.now))))
        result = runner.run([decision()])
        self.assertEqual(result.engine_result.dispatched_count, 1)
        self.assertTrue(all(t.date() == DAY for t in source_times))
        tail = [name for name, ts in order if ts == datetime(2025, 1, 3, 9, 30)]
        self.assertEqual(tail, ["settle", "clock"])
        first = [name for name, ts in order if ts == decision().ts]
        self.assertEqual(first, ["clock", "decision", "immediate"])
        self.assertEqual(runner.resources.portfolio.events[0].ts.date(), NEXT_DAY)

    def test_past_aware_and_out_of_calendar_callbacks_are_rejected(self):
        def clock(context):
            for at in (datetime(2025, 1, 1, 9, 30), datetime(2025, 1, 4, 9, 30),
                       datetime(2025, 1, 2, 9, 30, tzinfo=timezone.utc)):
                with self.assertRaises(RunnerError):
                    context.schedule(at, lambda c: None)
        runner = BacktestRunner(self.config, hooks=RunnerHooks(on_time=clock))
        runner.run()
        with self.assertRaises(RunnerError):
            runner.schedule(datetime(2025, 1, 3, 10), lambda c: None)

    def test_missing_data_blocks_all_event_hooks_and_preserves_quality_report(self):
        write_market(self.market, complete_rows()[:-1] + complete_rows(NEXT_DAY))
        calls = []
        runner = BacktestRunner(self.config, hooks=RunnerHooks(on_day_open=lambda c: calls.append(c)))
        with self.assertRaises(DataQualityError):
            runner.run([decision()])
        self.assertEqual(calls, [])
        self.assertEqual(runner.state, "failed")
        summary = json.loads((self.config.output_dir / "run_summary.json").read_text())
        self.assertEqual(summary["state"], "failed")
        self.assertTrue((self.config.output_dir / "quality" / "missing.csv").exists())

    def test_malformed_fixed_decisions_fail_before_repository_and_hook_effects(self):
        calls = []
        runner = BacktestRunner(self.config, hooks=RunnerHooks(on_time=lambda c: calls.append(c)))
        with self.assertRaises(DuplicateDecisionError):
            runner.run([decision(), decision()])
        self.assertEqual(calls, [])
        self.assertFalse(self.config.output_dir.exists())

    def test_runner_is_single_use_and_cannot_overwrite_an_existing_run(self):
        runner = BacktestRunner(self.config)
        runner.run()
        saved = (self.config.output_dir / "run_summary.json").read_bytes()
        with self.assertRaisesRegex(RunnerError, "single-use"):
            runner.run()
        with self.assertRaisesRegex(RunnerError, "not empty"):
            BacktestRunner(self.config).run()
        self.assertEqual((self.config.output_dir / "run_summary.json").read_bytes(), saved)

    def test_callback_failure_propagates_and_writes_failure_instead_of_success(self):
        def fail(context):
            raise RuntimeError("settlement failed")
        runner = BacktestRunner(self.config, hooks=RunnerHooks(on_time=fail))
        with self.assertRaisesRegex(RuntimeError, "settlement failed"):
            runner.run()
        summary = json.loads((self.config.output_dir / "run_summary.json").read_text())
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["last_time"], "2025-01-02T09:30:00")

    def test_execute_requires_executor_and_historical_rules(self):
        with self.assertRaisesRegex(RunnerConfigError, "DecisionExecutor"):
            BacktestRunner(replace(self.config, mode="execute"))
        with self.assertRaisesRegex(RunnerConfigError, "historical rules"):
            BacktestRunner(replace(self.config, mode="execute"), executor=object())
        with self.assertRaisesRegex(RunnerConfigError, "mode=execute"):
            BacktestRunner(self.config, executor=object())
        with self.assertRaisesRegex(RunnerConfigError, "not both"):
            BacktestRunner(replace(self.config, rules_path=self.root / "rules.json"), rules=self.rules)

    def test_config_paths_calendar_and_decimal_are_loaded_relative_to_config(self):
        (self.root / "days.txt").write_text("# test\n2025-01-02\n2025-01-03\n", encoding="utf-8-sig")
        path = self.write_config(pcf_root="pcf", minute_root="market", output_dir="relative_run",
                                 trading_days="days.txt", starting_cash="123.45")
        config = RunnerConfig.from_json(path)
        self.assertEqual(config.pcf_root, self.pcf)
        self.assertEqual(config.output_dir, self.root / "relative_run")
        self.assertEqual(config.starting_cash, Decimal("123.45"))
        self.assertEqual(config.trading_days, (DAY, NEXT_DAY))
        self.assertEqual(config.etf_codes, ("159338",))

    def test_invalid_config_calendar_and_output_paths_are_rejected(self):
        with self.assertRaisesRegex(RunnerConfigError, "Unknown"):
            RunnerConfig.from_json(self.write_config(typo=True))
        for changes in ({"trading_days": (DAY, DAY)}, {"trading_days": ()},
                        {"starting_cash": Decimal("NaN")}, {"quality_check": "true"},
                        {"clock_times": tuple(reversed(CLOCKS))}, {"output_dir": self.market / "reports"}):
            with self.subTest(changes=changes), self.assertRaises(RunnerConfigError):
                replace(self.config, **changes)

    def test_reporter_receives_completed_results_and_original_resources(self):
        calls = []
        def report(result, resources):
            calls.append((result.engine_result.decision_count, resources.config.starting_cash))
        BacktestRunner(self.config, reporter=report).run([decision()])
        self.assertEqual(calls, [(1, self.config.starting_cash)])

    def test_cli_reads_manual_decisions_and_reports_replay(self):
        from etf_arb.cli import main
        decision_file = self.root / "decisions.json"
        decision_file.write_text(json.dumps([{
            "etf_code": "159338", "ts": "2025-01-02T09:30:00", "eligible": True,
            "side": "CREATE", "quantity_units": 1, "gross_edge": "10",
            "estimated_cost": "2", "estimated_net_edge": "8", "reasons": [],
        }]), encoding="utf-8")
        config_path = self.write_config(decisions_path="decisions.json")
        with patch("sys.argv", ["etf-arb", "backtest", "--config", str(config_path)]), patch("builtins.print") as printed:
            result = main()
        self.assertEqual(result, 0)
        self.assertTrue(any("decision replay" in str(call) for call in printed.call_args_list))
        summary = json.loads((self.config.output_dir / "run_summary.json").read_text())
        self.assertEqual(summary["decision_count"], 1)
        self.assertEqual(summary["dispatched_count"], 0)

    def test_rule_file_loads_effective_dates_and_refuses_numeric_boolean(self):
        path = self.root / "rules.json"
        row = dict(etf_code="159338", effective_from="2025-01-02", effective_to="2025-01-03",
                   creation_enabled=True, redemption_enabled=True, creation_fee="12.34")
        path.write_text(json.dumps({"etf_rules": [row]}), encoding="utf-8")
        repository = load_rules(path)
        self.assertEqual(repository.get("159338", DAY).creation_fee, Decimal("12.34"))
        row["creation_enabled"] = 1
        path.write_text(json.dumps({"etf_rules": [row]}), encoding="utf-8")
        with self.assertRaises(RunnerConfigError):
            load_rules(path)
        row["creation_enabled"] = True
        row["creation_cutoff"] = "14:00:00+08:00"
        path.write_text(json.dumps({"etf_rules": [row]}), encoding="utf-8")
        with self.assertRaisesRegex(RunnerConfigError, "naive"):
            load_rules(path)


if __name__ == "__main__":
    unittest.main()
