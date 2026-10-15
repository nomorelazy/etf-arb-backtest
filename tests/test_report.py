from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from etf_arb.report.metrics import calculate_summary
from etf_arb.report.models import ClosedTrade, EquityPoint
from etf_arb.report.render import save_report


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.equity = [
            EquityPoint(date(2025, 1, 2), Decimal("100")),
            EquityPoint(date(2025, 1, 3), Decimal("110")),
            EquityPoint(date(2025, 1, 6), Decimal("88")),
            EquityPoint(date(2025, 1, 7), Decimal("110")),
        ]
        self.trades = [
            ClosedTrade("cycle-1", Decimal("10")),
            ClosedTrade("cycle-2", Decimal("-5")),
            ClosedTrade("cycle-3", Decimal("0")),
        ]

    def test_metrics_follow_documented_conventions(self) -> None:
        summary = calculate_summary(
            self.equity,
            self.trades,
            starting_cash=Decimal("100"),
            periods_per_year=3,
        )
        self.assertEqual(summary.total_return, Decimal("0.1"))
        self.assertAlmostEqual(float(summary.annualized_return), 0.1)
        self.assertAlmostEqual(float(summary.max_drawdown), 0.2)
        self.assertAlmostEqual(float(summary.return_drawdown_ratio), 0.5)
        self.assertAlmostEqual(float(summary.win_rate), 0.5)
        self.assertAlmostEqual(float(summary.profit_loss_ratio), 2.0)
        self.assertEqual(summary.trade_count, 3)
        self.assertIsNotNone(summary.sharpe_ratio)
        self.assertIsNotNone(summary.annualized_volatility)

    def test_starting_equity_must_be_explicit_first_point(self) -> None:
        with self.assertRaisesRegex(ValueError, "first equity point"):
            calculate_summary(
                self.equity[1:],
                self.trades,
                starting_cash=Decimal("100"),
            )

    def test_report_writes_chart_html_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = save_report(
                self.equity,
                self.trades,
                temp_dir,
                starting_cash=Decimal("100"),
                periods_per_year=3,
            )
            output = Path(temp_dir)
            for name in ("pnl_report.png", "report.html", "metrics.csv", "equity.csv"):
                self.assertTrue((output / name).is_file(), name)
            html = (output / "report.html").read_text(encoding="utf-8")
            self.assertIn("收益回撤比", html)
            self.assertEqual(summary.trade_count, 3)

    def test_cli_builds_report_from_csv_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            equity_path = root / "equity.csv"
            trades_path = root / "trades.csv"
            equity_path.write_text(
                "date,equity\n2025-01-02,100\n2025-01-03,110\n2025-01-06,88\n2025-01-07,110\n",
                encoding="utf-8",
            )
            trades_path.write_text(
                "trade_id,net_pnl\ncycle-1,10\ncycle-2,-5\ncycle-3,0\n",
                encoding="utf-8",
            )
            output_dir = root / "out"
            args = [
                "etf-arb", "report", "--equity", str(equity_path),
                "--trades", str(trades_path), "--starting-cash", "100",
                "--periods-per-year", "3", "--output", str(output_dir),
            ]
            with patch("sys.argv", args), redirect_stdout(StringIO()):
                from etf_arb.cli import main

                self.assertEqual(main(), 0)
            self.assertTrue((output_dir / "report.html").is_file())
            self.assertTrue((output_dir / "pnl_report.png").is_file())


if __name__ == "__main__":
    unittest.main()
