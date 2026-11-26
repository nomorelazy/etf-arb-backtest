import csv
import json
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from etf_arb.domain import PCF, PCFComponent
from etf_arb.report.limit_value import build_limit_value, limit_value_from_run, save_limit_value
from etf_arb.report.signal_risk import RiskEventPrices


class SignalLimitValueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.t = datetime(2025, 1, 2, 10, 45)
        self.prior = datetime(2024, 12, 31, 15)
        self.config = dict(etf_codes=["159338"], minute_root=self.root, pcf_root=self.root)
        self.prices = RiskEventPrices(self.root)
        self.feed = self.root / "report_prior_closes.csv"
        self.header = "previous_date,symbol,close,source_url\n"
        self.components = (
            PCFComponent("002245", D(100), "本地", "2", creation_cash_substitute=D(4321), market_code="102"),
            PCFComponent("600000", D(20), "跨市", "1", market_code="101"),
            PCFComponent("000001", D(0), "零股", "2", creation_cash_substitute=D(8000), market_code="102"),
        )
        self.pcf = PCF("159338", self.t.date(), 1000, self.components, pre_trading_day=self.prior.date())
        self.trade = dict(trade_id="T1", side="CREATE", opened_at=self.t.isoformat(" "),
                          quantity_units="2", etf_shares="2000", etf_symbol="159338.SZ")

    def build(self, signals=None, trades=None):
        with patch("etf_arb.report.limit_value.FilePCFRepository.get", return_value=self.pcf):
            return build_limit_value(self.config, trades if trades is not None else [self.trade],
                                     signals if signals is not None else [dict(ts=self.t, iopv="2")],
                                     prices=self.prices)

    def test_market_values_direction_quantity_and_verified_prior(self):
        self.feed.write_text(self.header+"2024-12-31,002245.SZ,10.69,https://source.example/1\n"
                             +"2024-12-31,600000.SH,20,https://source.example/2\n")
        self.prices.values.update({("002245.SZ", self.t): D("11.76"), ("600000.SH", self.t): D("9"),
                                   ("600000.SH", self.prior): D("10")})
        result = self.build(trades=[self.trade, dict(self.trade, trade_id="T2", side="REDEEM")])
        create, redeem = result["rows"]
        self.assertEqual(create["basket_value"], 4000)
        self.assertEqual(create["all"]["up"]["value"], D(2352))
        self.assertEqual(create["all"]["down"]["value"], D(360))
        self.assertEqual(create["all"]["total"]["weight"], D(2712)/4000)
        self.assertEqual(create["local_directional"]["value"], 2352)
        self.assertEqual(redeem["local_directional"]["value"], 0)
        for row, sz, sh in ((create, 2352, 0), (redeem, 0, 360)):
            groups = row["directional_by_market"]
            self.assertEqual(groups["SZ"]["value"], sz)
            self.assertEqual(groups["SH"]["value"], sh)
            self.assertEqual(groups["total"]["value"], sz + sh)
            self.assertEqual(groups["total"]["weight"], D(sz + sh) / row["basket_value"])
        self.assertEqual([s["pcf_route"] for s in redeem["hit_stocks"]], ["固定现金替代", "跨市场现金代理"])
        self.assertEqual([s["directional"] for s in redeem["hit_stocks"]], [False, True])
        save_limit_value(self.root, result)
        with (self.root / "limit_value.csv").open(encoding="utf-8-sig", newline="") as f:
            exported = list(csv.DictReader(f))
        self.assertEqual(D(exported[1]["sh_directional_weight"]), D(360)/4000)
        self.assertEqual(D(exported[1]["local_directional_weight"]), 0)
        self.assertEqual(create["all"]["component_count"], 2)
        self.assertEqual(create["local_directional"]["component_count"], 1)
        self.assertEqual(create["hit_stocks"][0]["reference_source"], "verified_historical_previous_close_feed")
        self.assertEqual(create["hit_stocks"][1]["reference_source"], "local_exact_previous_close")
        self.assertEqual(len(result["prior_closes_sha256"]), 64)

    def test_missing_exact_signal_or_iopv_preserves_partial_amounts(self):
        self.prices.values.update({("002245.SZ", self.t - timedelta(minutes=1)): D("11"),
                                   ("002245.SZ", self.prior): D(10),
                                   ("600000.SH", self.t): D(11), ("600000.SH", self.prior): D(10)})
        row, = self.build(signals=[])["rows"]
        self.assertIsNone(row["all"]["total"]["value"])
        self.assertIsNone(row["all"]["total"]["known_weight"])
        self.assertEqual(row["all"]["total"]["known_value"], 440)
        self.assertEqual(row["all"]["unknown_count"], 1)
        self.assertEqual(row["unknown_stocks"][0]["reason"], "missing_exact_close")
        self.assertIsNone(row["unknown_stocks"][0]["price"])
        self.assertFalse(row["directional_by_market"]["SZ"]["complete"])
        self.assertEqual(row["directional_by_market"]["SZ"]["unknown_count"], 1)
        self.assertIsNone(row["directional_by_market"]["total"]["value"])
        self.assertIsNone(row["directional_by_market"]["SH"]["weight"])
        self.assertEqual(row["directional_by_market"]["SH"]["value"], 440)
        self.assertEqual(row["directional_by_market"]["total"]["known_value"], 440)


    def test_official_bounds_precede_prior_and_open_proxy_never_leaks_to_earlier_signal(self):
        bounds = self.root / "official.csv"
        bounds.write_text("date,symbol,upper_limit,lower_limit\n2025-01-02,600000.SH,5,3\n")
        self.config["daily_price_limits_path"] = str(bounds)
        self.feed.write_text(self.header+"2024-12-30,002245.SZ,250,https://source.example/stale\n"
                             +"2024-12-31,600000.SH,300,https://source.example/other\n")
        early, opening = self.t.replace(hour=9,minute=29), self.t.replace(hour=9,minute=30)
        self.prices.values.update({("002245.SZ", opening): D(10), ("002245.SZ", early): D(11),
                                   ("002245.SZ", self.t): D(11), ("600000.SH", early): D(5),
                                   ("600000.SH", self.t): D(5)})
        first, second = self.build(trades=[dict(self.trade,opened_at=early),dict(self.trade,trade_id="T2")])["rows"]
        self.assertIsNone(first["unknown_stocks"][0]["upper_limit"])
        self.assertIsNone(first["unknown_stocks"][0]["reference_price"])
        self.assertEqual(first["unknown_stocks"][0]["reason"], "opening_proxy_not_yet_available")
        self.assertEqual(first["hit_stocks"][0]["source"], "provided")
        self.assertEqual(second["hit_stocks"][0]["reference_source"], "same_day_0930_close_proxy")
        self.assertEqual(second["all"]["up"]["value"], 2400)

    def test_offline_v1_snapshot_upgrades_directional_evidence_from_saved_stocks(self):
        self.prices.values.update({("002245.SZ", self.t): D(11), ("002245.SZ", self.prior): D(10),
                                   ("600000.SH", self.t): D(11), ("600000.SH", self.prior): D(10)})
        data = self.build()
        old = json.loads(json.dumps(data, default=str))
        old["schema_version"] = 1
        del old["rows"][0]["directional_by_market"]
        for stock in old["rows"][0]["hit_stocks"]:
            del stock["directional"], stock["pcf_route"]
        path = self.root / "limit_value.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        offline = dict(self.config, minute_root=self.root / "offline", pcf_root=self.root / "offline")
        with patch("etf_arb.report.limit_value.build_limit_value", side_effect=AssertionError("offline rebuild")):
            restored = limit_value_from_run(self.root, offline, [self.trade], [dict(ts=self.t, iopv=2)])
        self.assertEqual(restored["schema_version"], 2)
        self.assertEqual(restored["rows"][0]["directional_by_market"], data["rows"][0]["directional_by_market"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old)

    def test_missing_pcf_keeps_basket_without_assuming_empty_constituents(self):
        with patch("etf_arb.report.limit_value.FilePCFRepository.get", side_effect=FileNotFoundError):
            row, = build_limit_value(self.config, [self.trade], [dict(ts=self.t, iopv=2)])["rows"]
        self.assertEqual(row["basket_value"], 4000)
        self.assertEqual(row["coverage_issue"], "missing_pcf")
        self.assertIsNone(row["all"]["total"]["known_value"])
        self.assertIsNone(row["all"]["component_count"])
        self.assertIsNone(row["directional_by_market"]["total"]["known_value"])
        self.assertIsNone(row["directional_by_market"]["SZ"]["unknown_count"])


    def test_saved_snapshot_offline_and_changed_hash_or_trade_rebuild(self):
        signals = [dict(ts=self.t, iopv="2")]
        self.feed.write_text(self.header+"2024-12-31,002245.SZ,10.69,https://source.example/1\n")
        data = self.build()
        outputs = self.root / "run"
        save_limit_value(outputs, data)
        with patch("etf_arb.report.limit_value.build_limit_value", return_value={"rebuilt":True}) as rebuild:
            detached = dict(self.config, minute_root=self.root / "offline", pcf_root=self.root / "offline")
            self.assertEqual(limit_value_from_run(outputs, detached, [self.trade], signals)["rows"][0]["trade_id"], "T1")
            rebuild.assert_not_called()
            for field, value in (("quantity_units","3"), ("side","REDEEM"), ("trade_id","T2"),
                                 ("etf_shares","3100"), ("opened_at",self.prior)):
                with self.subTest(field=field):
                    self.assertTrue(limit_value_from_run(outputs, detached, [dict(self.trade, **{field:value})],
                                                         signals)["rebuilt"])
            self.assertTrue(limit_value_from_run(outputs, self.config, [self.trade], [dict(ts=self.t,iopv="5")])["rebuilt"])
            self.feed.write_text(self.header)
            self.assertTrue(limit_value_from_run(outputs, self.config, [self.trade], signals)["rebuilt"])
