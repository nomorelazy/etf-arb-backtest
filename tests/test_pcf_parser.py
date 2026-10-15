import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from etf_arb.data.pcf import (
    FilePCFRepository,
    PCFParseError,
    parse_pcf_json,
    parse_pcf_xml,
)


SZ_XML = """<?xml version="1.0" encoding="utf-8"?>
<PCFFile xmlns="http://ts.szse.cn/Fund">
  <Version>1.0</Version><SecurityID>159338</SecurityID><SecurityIDSource>102</SecurityIDSource>
  <Symbol>A500</Symbol><FundManagementCompany>Manager</FundManagementCompany>
  <UnderlyingSecurityID>000510</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
  <CreationRedemptionUnit>3000000.00</CreationRedemptionUnit>
  <EstimateCashComponent>-8575.19</EstimateCashComponent><MaxCashRatio>0.80000</MaxCashRatio>
  <Publish>Y</Publish><Creation>Y</Creation><Redemption>Y</Redemption>
  <RecordNum>2</RecordNum><TotalRecordNum>2</TotalRecordNum>
  <TradingDay>20241015</TradingDay><PreTradingDay>20241014</PreTradingDay>
  <CashComponent>28024.81</CashComponent><NAVperCU>2860709.81</NAVperCU><NAV>0.9536</NAV>
  <Components>
    <Component><UnderlyingSecurityID>159900</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
      <UnderlyingSymbol>申赎现金</UnderlyingSymbol><ComponentShare>0.00</ComponentShare><SubstituteFlag>2</SubstituteFlag>
      <PremiumRatio>0.00000</PremiumRatio><DiscountRatio>0.00000</DiscountRatio>
      <CreationCashSubstitute>1814363.1000</CreationCashSubstitute><RedemptionCashSubstitute>1484478.9000</RedemptionCashSubstitute>
    </Component>
    <Component><UnderlyingSecurityID>000001</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
      <UnderlyingSymbol>PingAn</UnderlyingSymbol><ComponentShare>100</ComponentShare><SubstituteFlag>0</SubstituteFlag>
      <PremiumRatio>0.10000</PremiumRatio><DiscountRatio>0.08000</DiscountRatio>
      <CreationCashSubstitute>0</CreationCashSubstitute><RedemptionCashSubstitute>0</RedemptionCashSubstitute>
    </Component>
  </Components>
</PCFFile>"""

SH_XML = """<?xml version="1.0" encoding="utf-8"?>
<SSEPortfolioCompositionFile>
  <FundInstrumentID>512760</FundInstrumentID><CreationRedemptionUnit>2000000</CreationRedemptionUnit>
  <TradingDay>20260723</TradingDay><PreTradingDay>20260722</PreTradingDay>
  <NAVperCU>2509769.77</NAVperCU><NAV>1.2549</NAV><PreCashComponent>3905.77</PreCashComponent>
  <EstimatedCashComponent>-1236.23</EstimatedCashComponent><MaxCashRatio>0.5</MaxCashRatio>
  <CreationLimit>10000000000</CreationLimit><RedemptionLimit>1500000000</RedemptionLimit>
  <PublishIOPVFlag>1</PublishIOPVFlag><CreationRedemptionSwitch>1</CreationRedemptionSwitch>
  <CreationRedemptionMechanism>1</CreationRedemptionMechanism><RecordNumber>1</RecordNumber>
  <ComponentList>
    <Component><InstrumentID>001309</InstrumentID><InstrumentName>Test</InstrumentName><Quantity>100</Quantity>
      <SubstitutionFlag>1</SubstitutionFlag><CreationPremiumRate>0.1</CreationPremiumRate>
      <RedemptionDiscountRate>0.08</RedemptionDiscountRate><SubstitutionCashAmount>81000</SubstitutionCashAmount>
      <UnderlyingSecurityID>102</UnderlyingSecurityID>
    </Component>
  </ComponentList>
</SSEPortfolioCompositionFile>"""


class PCFParserTests(unittest.TestCase):
    def test_shenzhen_xml_preserves_source_specific_fields(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "20241015.xml"
            path.write_text(SZ_XML, encoding="utf-8")
            pcf = parse_pcf_xml(path)
        self.assertEqual(pcf.etf_code, "159338")
        self.assertEqual(pcf.exchange, "SZ")
        self.assertEqual(pcf.effective_date, date(2024, 10, 15))
        self.assertEqual(pcf.creation_unit, 3000000)
        self.assertEqual(pcf.estimated_cash_component, Decimal("-8575.19"))
        self.assertEqual(pcf.components[0].security_code, "159900")
        self.assertTrue(pcf.components[0].is_cash_placeholder)
        self.assertEqual(pcf.components[1].quantity, 100)
        self.assertEqual(pcf.components[1].creation_premium_rate, Decimal("0.10000"))
        self.assertEqual(pcf.components[1].redemption_discount_rate, Decimal("0.08000"))
        self.assertIsNone(pcf.creation_cash_adjustment)

    def test_shanghai_xml_does_not_use_underlying_security_id_as_stock_code(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "20260723.xml"
            path.write_text(SH_XML, encoding="utf-8")
            from etf_arb.data.pcf import parse_pcf_xml

            pcf = parse_pcf_xml(path)
        self.assertEqual(pcf.exchange, "SH")
        self.assertEqual(pcf.components[0].security_code, "001309")
        self.assertEqual(pcf.components[0].market_code, "102")
        self.assertEqual(pcf.creation_unit, 2000000)
        self.assertEqual(pcf.pre_cash_component, Decimal("3905.77"))

    def test_legacy_json_is_supported_and_percent_is_normalized(self) -> None:
        payload = {
            "info": [["Trading day", "20190612", "TradingDay"]],
            "t-1": {"properties": [["cash", "-2", "CashComponent"], ["navcu", "100", "NAVperCU"]]},
            "t": {
                "properties": [
                    ["estimated", "-1", "EstimateCashComponent"],
                    ["unit", "1000000", "CreationRedemptionUnit"],
                    ["max cash ratio", "50%", "MaxCashRatio"],
                    ["publish IOPV", "Yes", "Publish"],
                ]
            },
            "securities": {
                "rows": [["002049", "Test", "2100", "1", "10.000%", "95109.000"]]
            },
        }
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "512760"
            folder.mkdir()
            path = folder / "20190612.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            pcf = parse_pcf_json(path)
        self.assertEqual(pcf.etf_code, "512760")
        self.assertEqual(pcf.effective_date, date(2019, 6, 12))
        self.assertEqual(pcf.components[0].substitution_rate, Decimal("0.100"))
        self.assertEqual(pcf.estimated_cash_component, Decimal("-1"))
        self.assertEqual(pcf.pre_cash_component, Decimal("-2"))
        self.assertEqual(pcf.max_cash_ratio, Decimal("0.5"))
        self.assertEqual(pcf.publish_flag, "Yes")

    def test_repository_requires_exact_date_and_skips_status_file(self) -> None:
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "159338"
            folder.mkdir()
            (folder / "_fetch_status.json").write_text("{}", encoding="utf-8")
            (folder / "20241015.xml").write_text(SZ_XML, encoding="utf-8")
            repository = FilePCFRepository(temp)
            pcf = repository.get("159338", date(2024, 10, 15))
            self.assertEqual(pcf.source_path, str(folder / "20241015.xml"))
            self.assertIs(repository.get("159338", date(2024, 10, 15)), pcf)
            with self.assertRaises(FileNotFoundError):
                repository.get("159338", date(2024, 10, 16))

    def test_repository_rejects_file_code_mismatch(self) -> None:
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "159338"
            folder.mkdir()
            (folder / "20241015.xml").write_text(SZ_XML.replace("159338", "159339", 1), encoding="utf-8")
            with self.assertRaises(PCFParseError):
                FilePCFRepository(temp).get("159338", date(2024, 10, 15))


if __name__ == "__main__":
    unittest.main()
