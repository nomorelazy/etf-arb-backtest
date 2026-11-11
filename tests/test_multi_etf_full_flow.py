import csv
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from etf_arb.costs import FeeRates
from etf_arb.full_flow import make_full_runner
from etf_arb.rules import ETFRule, InMemoryRuleRepository
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_full_flow import CLOCKS, DAYS
from test_missing_data import write_market


class MultiEtfFullFlowTests(unittest.TestCase):
    def test_shanghai_research_flow_uses_legacy_pcf_and_fund_specific_fills(self):
        for etf_code, etf_price, direction, cash_delta, expected_pnl in (
            ("512760", 3.4, "CREATE", D("-2"), D("3")),
            ("512880", 3.2, "REDEEM", D("2"), D("17")),
            ("515880", 3.4, "CREATE", D("-2"), D("3")),
        ):
            with self.subTest(etf_code=etf_code), TemporaryDirectory() as temp:
                root = Path(temp)
                pcfs = root / "pcf" / etf_code
                pcfs.mkdir(parents=True)
                for index, day in enumerate(DAYS):
                    previous = DAYS[index - 1] if index else day - timedelta(days=2)
                    local_midnight = datetime.combine(previous, time.min, timezone(timedelta(hours=8)))
                    previous_label = local_midnight.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    payload = {
                        "info": [["交易日", day.strftime("%Y%m%d"), "TradingDay"]],
                        "t-1": {"date": previous_label, "properties": [["现金差额", "12", "CashComponent"]]},
                        "t": {"properties": [
                            ["预估现金", "10", "EstimateCashComponent"],
                            ["申赎单位", "100", "CreationRedemptionUnit"],
                            ["申赎开关", "申购和赎回皆允许", "CreationRedemption"],
                        ]},
                        "securities": {"rows": [
                            ["600000", "沪市实物", "100", "允许", "10%", "0%", ""],
                            ["000001", "深市退补", "100", "深市退补", "10%", "8%", "200"],
                            ["600001", "固定替代", "100", "必须", "", "", "20"],
                        ]},
                    }
                    (pcfs / f"{day:%Y%m%d}.json").write_text(json.dumps(payload), encoding="utf-8")
                market = root / "market"
                clocks = tuple(sorted((*CLOCKS, time(14, 57))))
                write_market(market, [
                    (datetime.combine(day, clock), symbol, price)
                    for day in DAYS for clock in clocks
                    for symbol, price in (
                        (f"{etf_code}.SH", etf_price),
                        ("600000.SH", 1),
                        ("000001.SZ", 2.05 if clock == time(9, 31) else 2),
                    )
                ])
                config = RunnerConfig(
                    DAYS[0], DAYS[0], (etf_code,), pcfs.parent, market, root / "run", DAYS,
                    starting_cash=D("10000"), clock_times=clocks, mode="execute",
                    settlement_end_date=DAYS[-1],
                    simulation=SimulationConfig(
                        premium_threshold=D("0.002"), blocked_trade_minutes=(),
                        trading_sessions=(("09:30", "11:30"), ("13:00", "14:57")),
                        fees=FeeRates.zero(), security_rule_policy="assume_tradable",
                        settle_estimated_cash_at_application=True,
                        cash_difference_lag=0, cash_substitution_lag=2,
                    ),
                )
                rules = InMemoryRuleRepository([ETFRule(etf_code, DAYS[0], DAYS[-1], True, True)])
                runner = make_full_runner(config, rules=rules)
                summary = runner.run().financial_summary
                self.assertEqual(summary["etf_code"], etf_code)
                self.assertEqual(summary["opened_count"], 1)
                self.assertEqual(D(summary["final_net_pnl"]), expected_pnl)
                trade, = runner.executor.trades
                self.assertEqual(trade.decision.side.value, direction)
                self.assertEqual(trade.applied_at, datetime.combine(DAYS[0], time(9, 31)))
                self.assertEqual(trade.plan.physical, {"600000.SH": 100})
                self.assertEqual(trade.plan.manager, {"000001.SZ": 100})
                self.assertEqual(trade.plan.substitution_deposit, D("220") if direction == "CREATE" else D("0"))
                cash = trade.legs[0]
                self.assertEqual((cash.actual_component, cash.final, cash.paid_at.date()), (D("12"), cash_delta, DAYS[1]))
                proxy_fill, = [fill for fill in runner.executor.fills if fill["purpose"] == "MANAGER"]
                self.assertEqual((proxy_fill["symbol"], proxy_fill["ts"], proxy_fill["price"]),
                                 ("000001.SZ", trade.applied_at, D("2.05")))
                with (config.output_dir / "trade_attribution.csv").open(encoding="utf-8-sig", newline="") as stream:
                    attribution, = csv.DictReader(stream)
                self.assertEqual(D(attribution["actual_cash"]), D("12"))
                self.assertEqual(D(attribution["etf_price_effect"]), D("0"))
                self.assertEqual(D(attribution["net_pnl"]), expected_pnl)
