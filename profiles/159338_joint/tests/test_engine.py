from datetime import date, datetime, timezone
from decimal import Decimal
import pickle
import unittest

from etf_arb.domain import Side, TradeDecision
from etf_arb.engine.backtest import (
    BacktestConfig,
    BacktestConfigError,
    BacktestError,
    BacktestResult,
    DuplicateDecisionError,
    run_backtest,
)


def make_decision(
    etf_code: str = "510300",
    ts: datetime | None = None,
    *,
    eligible: bool = True,
    quantity_units: int = 1,
    reasons: tuple[str, ...] = (),
    gross_edge: Decimal = Decimal("10"),
) -> TradeDecision:
    return TradeDecision(
        etf_code=etf_code,
        ts=ts or datetime(2026, 6, 12, 10, 0),
        eligible=eligible,
        side=Side.CREATE,
        quantity_units=quantity_units,
        gross_edge=gross_edge,
        estimated_cost=Decimal("2"),
        estimated_net_edge=Decimal("8"),
        reasons=reasons,
    )


class BacktestEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BacktestConfig(
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
            etf_codes=("510300.SH", "159338.SZ"),
        )

    def test_reasons_string_is_rejected(self) -> None:
        decision = make_decision()
        decision = TradeDecision(
            etf_code=decision.etf_code,
            ts=decision.ts,
            eligible=decision.eligible,
            side=decision.side,
            quantity_units=decision.quantity_units,
            gross_edge=decision.gross_edge,
            estimated_cost=decision.estimated_cost,
            estimated_net_edge=decision.estimated_net_edge,
            reasons="spread_too_narrow",  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(TypeError, "not a string"):
            run_backtest(self.config, [decision])

    def test_mismatched_market_suffix_is_rejected_in_config(self) -> None:
        with self.assertRaises(BacktestConfigError):
            BacktestConfig(
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                etf_codes=("510300.SZ",),
            )

    def test_non_etf_prefix_is_rejected_in_config(self) -> None:
        with self.assertRaises(BacktestConfigError):
            BacktestConfig(
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                etf_codes=("000001",),
            )

    def test_same_timestamp_decisions_are_sorted_by_etf_code(self) -> None:
        at = datetime(2026, 6, 12, 10, 0)
        result = run_backtest(
            self.config,
            [
                make_decision("159338.SZ", at),
                make_decision("510300.SH", at),
            ],
        )

        self.assertEqual(
            [decision.etf_code for decision in result.decisions],
            ["159338", "510300"],
        )
        self.assertEqual(result.decisions_by_etf["159338"], 1)
        self.assertEqual(result.decisions_by_etf["510300"], 1)

    def test_duplicate_decision_is_detected_even_if_input_is_unsorted(self) -> None:
        at = datetime(2026, 6, 12, 10, 0)
        with self.assertRaises(DuplicateDecisionError):
            run_backtest(
                self.config,
                [
                    make_decision("510300", at),
                    make_decision("159338", at),
                    make_decision("510300", at),
                ],
            )

    def test_result_is_hashable_and_pickleable(self) -> None:
        result = run_backtest(
            self.config,
            [make_decision("510300.SH")],
        )

        self.assertIsInstance(hash(result), int)
        restored = pickle.loads(pickle.dumps(result))
        self.assertEqual(restored, result)
        self.assertEqual(restored.decisions_by_etf["510300"], 1)

    def test_timezone_aware_decision_is_rejected(self) -> None:
        decision = make_decision(
            ts=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)
        )
        with self.assertRaisesRegex(ValueError, "timezone-naive"):
            run_backtest(self.config, [decision])

    def test_eligible_decision_requires_positive_pcf_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete PCF"):
            run_backtest(
                self.config,
                [make_decision(quantity_units=0)],
            )

    def test_non_decimal_amount_is_rejected(self) -> None:
        decision = make_decision(gross_edge=Decimal("10"))
        decision = TradeDecision(
            etf_code=decision.etf_code,
            ts=decision.ts,
            eligible=decision.eligible,
            side=decision.side,
            quantity_units=decision.quantity_units,
            gross_edge=10.0,  # type: ignore[arg-type]
            estimated_cost=decision.estimated_cost,
            estimated_net_edge=decision.estimated_net_edge,
            reasons=decision.reasons,
        )

        with self.assertRaisesRegex(TypeError, "gross_edge must be a Decimal"):
            run_backtest(self.config, [decision])

    def test_decision_outside_date_range_is_rejected(self) -> None:
        with self.assertRaises(BacktestError):
            run_backtest(
                self.config,
                [make_decision(ts=datetime(2026, 7, 1, 10, 0))],
            )

    def test_ineligible_decisions_are_counted_but_not_dispatched(self) -> None:
        class Handler:
            def __init__(self) -> None:
                self.received: list[TradeDecision] = []

            def handle(self, decision: TradeDecision) -> None:
                self.received.append(decision)

        handler = Handler()
        result = run_backtest(
            self.config,
            [
                make_decision("510300", eligible=False, quantity_units=0),
                make_decision("159338", eligible=True),
            ],
            handler=handler,
        )

        self.assertEqual(result.decision_count, 2)
        self.assertEqual(result.eligible_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.dispatched_count, 1)
        self.assertEqual(len(handler.received), 1)


if __name__ == "__main__":
    unittest.main()
