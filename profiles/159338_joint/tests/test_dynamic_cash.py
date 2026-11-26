from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from etf_arb.data.minute_close import MinuteCloseBar
from etf_arb.domain import PCF, PCFComponent
from etf_arb.pricing.dynamic_cash import DynamicCashModel
from etf_arb.pricing.errors import PricingDataError


def write_holdings_xlsx(path, rows, etf_code="159338"):
    """Write the small OpenXML subset used by pricing and end-to-end lifecycle fixtures."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet = ET.Element("worksheet", xmlns=ns)
    block = ET.SubElement(sheet, "sheetData")
    header = ("etf代码", "股票代码", "零股数量", "日期", "零股数量2")
    contents = [(etf_code, symbol, qty, day, "999999") for symbol, qty, day in rows]
    for idx, values in enumerate([header, *contents], start=1):
        row = ET.SubElement(block, "row", r=str(idx))
        for col, val in zip("ABCDE", values):
            cell = ET.SubElement(row, "c", r=f"{col}{idx}", t="inlineStr")
            ET.SubElement(ET.SubElement(cell, "is"), "t").text = str(val)
    with ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{office_ns}"><sheets>'
                   f'<sheet name="{etf_code}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", ET.tostring(sheet, encoding="utf-8", xml_declaration=True))


class Prices:
    def __init__(self, ts, quotes):
        self.ts, self.quotes = ts, quotes

    def get(self, symbol, now, *, exact):
        assert exact
        return (MinuteCloseBar(self.ts, symbol, D(self.quotes[symbol]))
                if symbol in self.quotes else None)


class DynamicCashPricingTest(unittest.TestCase):
    day = date(2025, 1, 2)
    now = datetime(2025, 1, 2, 11, 23)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.book = self.folder / "holdings.xlsx"
        self.ref = self.folder / "references.csv"

    def test_full_snapshot_mixed_routes_missing_holdings_and_negative_exposure(self):
        # January 2, 2025 is 45659 in the Excel 1900 date system.
        write_holdings_xlsx(self.book, [
            ("000001", 127, 45659), ("600001", 75, self.day), ("000003", 8, self.day),
            ("000005", "3.5", self.day), ("159900", 999, self.day),
        ])
        self.ref.write_text(
            "time,thscode,preClose,close\n"
            "2025-01-02,000001.SZ,10,98\n2025-01-02,600001.SH,4,99\n"
            "2025-01-02,000003.SZ,12,96\n2025-01-02,000005.SZ,8,96\n"
            "2025-01-02,600002.SH,7,99\n", encoding="utf-8")
        model = DynamicCashModel.from_files(self.book, self.ref, "159338")
        pcf = PCF("159338", self.day, 100, (
            PCFComponent("159900", D(0), substitution_flag="2", is_cash_placeholder=True),
            PCFComponent("000001", D(100), substitution_flag="0"),
            PCFComponent("600001", D(100), substitution_flag="1"),
            PCFComponent("000003", D(80), substitution_flag="2", creation_cash_substitute=D(25)),
            PCFComponent("000004", D(0), substitution_flag="2", creation_cash_substitute=D(131)),
            PCFComponent("600002", D(10), substitution_flag="1"),
        ), estimated_cash_component=D(-30))
        self.assertEqual(model.required_symbols(pcf), {"000001.SZ", "600001.SH", "000003.SZ", "000005.SZ"})
        prices = Prices(self.now, {"000001.SZ": 11, "600001.SH": 6, "000003.SZ": 13, "000005.SZ": 10,
                                   "600002.SH": 6})
        v = model.value(pcf, self.now, prices)
        self.assertEqual(v.cash_adjustment, 27 - 50 + 8 + 7 + 10)
        self.assertEqual(v.estimated_cash, -28)
        self.assertEqual(v.mandatory_cash, 156)
        self.assertEqual(v.security_value, 1760)
        self.assertEqual(v.numerator, 1888)
        self.assertEqual(v.iopv, D("18.88"))
        self.assertEqual(v.price_count, 5)
        for bad_time in (self.now + timedelta(minutes=1), self.now - timedelta(minutes=1)):
            prices.ts = bad_time
            with self.assertRaisesRegex(PricingDataError, "missing_exact_valuation_price"):
                model.value(pcf, self.now, prices)

    def test_missing_input_has_no_fallback_to_daily_close_or_stale_snapshot(self):
        write_holdings_xlsx(self.book, [("000001", 100, self.day)])
        self.ref.write_text("time,thscode,preClose,close\n2025-01-02,000001.SZ,,88\n", encoding="utf-8")
        model = DynamicCashModel.from_files(self.book, self.ref, "159338")
        component = PCFComponent("000001", D(90), substitution_flag="1")
        pcf = PCF("159338", self.day, 100, (component,), estimated_cash_component=D(0))
        with self.assertRaisesRegex(PricingDataError, "missing_adjusted_preclose"):
            model.value(pcf, self.now, Prices(self.now, {"000001.SZ": 5}))
        tomorrow = self.day + timedelta(days=1)
        pcf = PCF("159338", tomorrow, 100, (component,), estimated_cash_component=D(0))
        with self.assertRaisesRegex(PricingDataError, "missing_odd_lot_holdings"):
            model.value(pcf, self.now + timedelta(days=1), Prices(self.now, {}))

    def test_historical_vendor_alias_and_zero_exposure(self):
        write_holdings_xlsx(self.book, [("300114", 105, self.day), ("600001", 10, self.day)])
        self.ref.write_text("time,thscode,preClose\n2025-01-02,302132.SZ,3\n", encoding="utf-8")
        model = DynamicCashModel.from_files(self.book, self.ref, "159338")
        pcf = PCF("159338", self.day, 100, (PCFComponent("300114", D(100), substitution_flag="1"),
                                           PCFComponent("600001", D(10), substitution_flag="0")),
                  estimated_cash_component=D(-2))
        self.assertTrue({"302132.SZ", "300114.SZ"} <= model.required_symbols(pcf))
        for alias in ("302132.SZ", "300114.SZ"):
            v = model.value(pcf, self.now, Prices(self.now, {alias: 4, "600001.SH": 8}))
            self.assertEqual(v.estimated_cash, 3)
            self.assertEqual(v.numerator, 483)

    def test_duplicate_holding_is_an_input_error(self):
        write_holdings_xlsx(self.book, [("000001", 100, self.day), ("000001", 10, self.day)])
        with self.assertRaisesRegex(ValueError, "duplicate holding"):
            DynamicCashModel.from_files(self.book, self.ref, "159338")
