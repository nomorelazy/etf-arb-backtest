import csv
import json
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from etf_arb.data.missing import check_missing, ifind_minute_grid


DAY = date(2025, 1, 2)
CLOCKS = (time(9, 30), time(9, 31))


def write_pcf(root, day=DAY, *, flag="1", mandatory=False, cash=True):
    folder = root / "159338"
    folder.mkdir(parents=True, exist_ok=True)
    optional_cash = "<EstimateCashComponent>0</EstimateCashComponent>" if cash else ""
    flag_field = f"<SubstituteFlag>{flag}</SubstituteFlag>" if flag is not None else ""
    extra = """
    <Component><UnderlyingSecurityID>000002</UnderlyingSecurityID>
      <ComponentShare>0</ComponentShare><SubstituteFlag>2</SubstituteFlag>
      <CreationCashSubstitute>100</CreationCashSubstitute>
      <RedemptionCashSubstitute>90</RedemptionCashSubstitute></Component>
    <Component><UnderlyingSecurityID>159900</UnderlyingSecurityID>
      <UnderlyingSymbol>申赎现金</UnderlyingSymbol><ComponentShare>0</ComponentShare>
      <SubstituteFlag>2</SubstituteFlag></Component>
    """ if mandatory else ""
    path = folder / f"{day:%Y%m%d}.xml"
    path.write_text(f"""<PCFFile><SecurityID>159338</SecurityID>
      <TradingDay>{day:%Y%m%d}</TradingDay><CreationRedemptionUnit>100</CreationRedemptionUnit>
      {optional_cash}<Components><Component><UnderlyingSecurityID>000001</UnderlyingSecurityID>
      <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
      <ComponentShare>100</ComponentShare>{flag_field}</Component>{extra}
      </Components></PCFFile>""", encoding="utf-8")
    return path


def write_market(root, rows, name="data.parquet"):
    path = root / "2025-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "time": pa.array([r[0] for r in rows], type=pa.timestamp("us")),
        "thscode": pa.array([r[1] for r in rows], type=pa.string()),
        "close": pa.array([r[2] for r in rows], type=pa.float64()),
    }), path)
    return path


def complete_rows(day=DAY):
    return [(datetime.combine(day, t), code, 1.0)
            for code in ("159338.SZ", "000001.SZ") for t in CLOCKS]


class MissingDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pcf, self.market = self.root / "pcf", self.root / "market"
        self.pcf.mkdir()
        self.market.mkdir()

    def check(self, **kwargs):
        options = dict(pcf_root=self.pcf, minute_root=self.market,
                       etf_codes=["159338"], start_date=DAY, end_date=DAY,
                       trading_days=[DAY], expected_times=CLOCKS)
        options.update(kwargs)
        return check_missing(**options)

    def test_complete_sources_duplicates_and_cash_only_rows(self):
        write_pcf(self.pcf, mandatory=True)
        path = write_market(self.market, complete_rows())
        write_market(self.market, complete_rows(), "duplicate.parquet")
        before = path.read_bytes()
        report = self.check()
        self.assertEqual(report.issues, ())
        self.assertTrue(report.summary()["passed"])
        self.assertEqual(report.security_day_checks, 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.market.rglob("*.sqlite3")))

    def test_missing_whole_day_requires_external_calendar(self):
        report = self.check()
        self.assertEqual({i.kind for i in report.issues}, {"MISSING_PCF", "MISSING_ETF_DAY"})
        self.assertEqual(report.skipped_component_days, 1)
        inferred = self.check(trading_days=None)
        self.assertEqual(inferred.checked_dates, ())
        self.assertFalse(inferred.summary()["passed"])
        self.assertEqual(inferred.issues[0].kind, "NO_REFERENCE_DATES")

    def test_pcf_dates_detect_day_missing_from_market(self):
        write_pcf(self.pcf)
        report = self.check(trading_days=None)
        self.assertEqual(report.checked_dates, (DAY,))
        self.assertEqual({i.kind for i in report.issues}, {"MISSING_ETF_DAY", "MISSING_COMPONENT_DAY"})

    def test_null_close_and_missing_minute_are_not_forward_filled(self):
        write_pcf(self.pcf)
        rows = complete_rows()
        rows[-1] = (rows[-1][0], rows[-1][1], None)
        # Older prices and a later price must not satisfy the missing 09:31 slot.
        rows.extend([(datetime(2025, 1, 1, 9, 31), "000001.SZ", 1.0),
                     (datetime(2025, 1, 2, 9, 32), "000001.SZ", 1.0)])
        write_market(self.market, rows)
        report = self.check()
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.kind, "MISSING_COMPONENT_MINUTES")
        self.assertEqual(issue.missing_times, ("09:31:00",))
        self.assertEqual(issue.missing_count, 1)

    def test_null_duplicate_with_another_valid_close_is_present(self):
        write_pcf(self.pcf)
        write_market(self.market, complete_rows())
        write_market(self.market, [(datetime(2025, 1, 2, 9, 31), "000001.SZ", None)], "null.parquet")
        self.assertEqual(self.check().issues, ())

    def test_zero_close_is_present_and_left_for_anomaly_check(self):
        write_pcf(self.pcf)
        rows = complete_rows()
        rows[-1] = (*rows[-1][:2], 0.0)
        write_market(self.market, rows)
        self.assertEqual(self.check().issues, ())

    def test_missing_required_fields_do_not_pass(self):
        write_pcf(self.pcf, flag=None, cash=False)
        write_market(self.market, complete_rows())
        report = self.check()
        fields = {i.field for i in report.issues if i.kind == "MISSING_PCF_FIELD"}
        self.assertEqual(fields, {"substitution_flag", "estimated_cash_component"})
        self.assertGreater(report.summary()["unchecked_issue_count"], 0)

    def test_bad_pcf_is_unchecked_not_misreported_as_absent(self):
        path = write_pcf(self.pcf)
        path.write_text("<broken", encoding="utf-8")
        write_market(self.market, complete_rows())
        report = self.check()
        self.assertEqual(report.issues[0].kind, "PCF_UNCHECKED")
        self.assertFalse(report.summary()["passed"])

    def test_unknown_flag_is_explicitly_unchecked(self):
        write_pcf(self.pcf, flag="unsupported")
        write_market(self.market, complete_rows())
        self.assertEqual(self.check().issues[0].kind, "COMPONENT_REQUIREMENT_UNCHECKED")

    def test_pcf_declared_total_detects_missing_component_rows(self):
        path = write_pcf(self.pcf)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "<Components>", "<TotalRecordNum>2</TotalRecordNum><Components>"
        ), encoding="utf-8")
        write_market(self.market, complete_rows())
        report = self.check()
        self.assertEqual(report.issues[0].kind, "MISSING_PCF_COMPONENT_ROWS")
        self.assertIn("declares 2 rows; parsed 1", report.issues[0].detail)

    def test_sz_record_num_alone_is_not_the_full_component_total(self):
        path = write_pcf(self.pcf)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "<Components>", "<RecordNum>2</RecordNum><Components>"
        ), encoding="utf-8")
        write_market(self.market, complete_rows())
        self.assertEqual(self.check().issues, ())

    def test_schema_missing_close_is_reported_and_coverage_unchecked(self):
        write_pcf(self.pcf)
        path = write_market(self.market, complete_rows())
        table = pq.read_table(path).drop(["close"])
        pq.write_table(table, path)
        report = self.check()
        self.assertIn("MISSING_COLUMN", {i.kind for i in report.issues})
        self.assertGreater(report.summary()["unchecked_issue_count"], 0)

    def test_report_export_and_observed_reference_are_explicit(self):
        write_pcf(self.pcf)
        write_market(self.market, complete_rows()[:-1])
        report = self.check(trading_days=None)
        summary_path, detail_path = report.save(self.root / "report")
        self.assertEqual(json.loads(summary_path.read_text(encoding="utf-8"))["calendar_source"], "observed")
        with detail_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["security_code"], "000001.SZ")
        self.assertEqual(rows[0]["missing_times"], "09:31:00")

    def test_explicit_calendar_does_not_check_weekends_and_respects_range(self):
        write_pcf(self.pcf)
        write_market(self.market, complete_rows())
        report = self.check(end_date=date(2025, 1, 5), trading_days=[DAY])
        self.assertEqual(report.checked_dates, (DAY,))
        self.assertEqual(report.issues, ())

    def test_source_minute_grid_is_not_execution_session_grid(self):
        grid = ifind_minute_grid()
        self.assertEqual(len(grid), 242)
        self.assertIn(time(11, 30), grid)
        self.assertIn(time(15), grid)
        self.assertNotIn(time(12), grid)

    def test_mandatory_cash_amount_missing_is_not_zero(self):
        path = write_pcf(self.pcf, mandatory=True)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "<CreationCashSubstitute>100</CreationCashSubstitute>", ""
            ), encoding="utf-8",
        )
        write_market(self.market, complete_rows())
        report = self.check()
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].field, "creation_cash_substitute")
        self.assertFalse(report.summary()["passed"])

    def test_duplicate_calendar_and_invalid_clock_grid_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.check(trading_days=[DAY, DAY])
        with self.assertRaisesRegex(ValueError, "increasing"):
            self.check(expected_times=tuple(reversed(CLOCKS)))

    def test_cross_market_mandatory_label_does_not_require_stock_quotes(self):
        path = write_pcf(self.pcf)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "<SubstituteFlag>1</SubstituteFlag>",
            "<SubstituteFlag>深市必须</SubstituteFlag>"
            "<CreationCashSubstitute>0</CreationCashSubstitute>"
            "<RedemptionCashSubstitute>0</RedemptionCashSubstitute>",
        ), encoding="utf-8")
        write_market(self.market, [r for r in complete_rows() if r[1] == "159338.SZ"])
        self.assertEqual(self.check().issues, ())

    def test_cli_reads_calendar_writes_outputs_and_returns_missing_status(self):
        from etf_arb.cli import main

        write_pcf(self.pcf)
        write_market(self.market, complete_rows())
        calendar = self.root / "days.txt"
        calendar.write_text("# reference\n2025-01-02\n", encoding="utf-8-sig")
        output = self.root / "cli_report"
        argv = [
            "etf-arb", "check-missing", "--pcf-root", str(self.pcf),
            "--minute-root", str(self.market), "--etf", "159338",
            "--start", "2025-01-02", "--end", "2025-01-02",
            "--trading-days", str(calendar), "--output", str(output),
        ]
        with patch("sys.argv", argv), patch("builtins.print"):
            result = main()
        # CLI's actual source grid has 242 slots; fixtures only supply two.
        self.assertEqual(result, 1)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["calendar_source"], "explicit")
        self.assertEqual(summary["missing_minute_count"], 480)
        self.assertTrue((output / "missing.csv").exists())


if __name__ == "__main__":
    unittest.main()
