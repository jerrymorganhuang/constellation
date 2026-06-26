import importlib.util
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BUILD_COMPANIES_PATH = ROOT_DIR / "scripts" / "build_companies.py"

spec = importlib.util.spec_from_file_location("build_companies", BUILD_COMPANIES_PATH)
build_companies = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build_companies
assert spec.loader is not None
spec.loader.exec_module(build_companies)


SPREADSHEET_NAMESPACE = 'xmlns="urn:schemas-microsoft-com:office:spreadsheet"'


class BlackRockSpreadsheetMLDetectorTest(unittest.TestCase):
    def assert_detects_spreadsheetml(self, xml: str):
        self.assertTrue(build_companies._is_spreadsheetml_response(xml.encode("utf-8")))

    def test_detects_plain_workbook_with_xml_declaration(self):
        self.assert_detects_spreadsheetml(f'<?xml version="1.0"?><Workbook {SPREADSHEET_NAMESPACE}></Workbook>')

    def test_detects_prefixed_workbook_with_xml_declaration(self):
        self.assert_detects_spreadsheetml(
            '<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"></ss:Workbook>'
        )

    def test_detects_leading_whitespace(self):
        self.assert_detects_spreadsheetml(f'\n  \t<Workbook {SPREADSHEET_NAMESPACE}></Workbook>')

    def test_detects_utf8_bom(self):
        content = f'\ufeff<Workbook {SPREADSHEET_NAMESPACE}></Workbook>'.encode("utf-8")
        self.assertTrue(build_companies._is_spreadsheetml_response(content))

    def test_detects_processing_instruction_before_workbook(self):
        self.assert_detects_spreadsheetml(
            f'<?mso-application progid="Excel.Sheet"?><Workbook {SPREADSHEET_NAMESPACE}></Workbook>'
        )

    def test_detects_xml_comment_before_workbook(self):
        self.assert_detects_spreadsheetml(f'<!-- BlackRock holdings --><Workbook {SPREADSHEET_NAMESPACE}></Workbook>')


class BlackRockSpreadsheetMLParsingTest(unittest.TestCase):
    def test_sanitizes_malformed_spreadsheetml_and_extracts_tickers(self):
        xml = '''<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
  <Worksheet ss:Name="Holdings">
    <Table>
      <Row>
        <Cell><Data ss:Type="String">Ticker</Data></Cell>
        <Cell><Data ss:Type="String">Name</Data></Cell>
        <Cell><Data ss:Type="String">Asset Class</Data></Cell>
      </Row>
      <Row>
        <Cell><Data ss:Type="String">BRK.B</Data></Cell>
        <Cell><Data ss:Type="String">Berkshire & Hathaway\x08 Inc</Data></Cell>
        <Cell><Data ss:Type="String">Equity</Data></Cell>
      </Row>
    </Table>
  </Worksheet>
</Workbook>'''
        raw_df, notes = build_companies._read_spreadsheetml_holdings(xml.encode("utf-8"), "BlackRock holdings")
        df, _, _, _ = build_companies._promote_detected_header(raw_df, {"ticker"}, "BlackRock holdings")

        self.assertEqual(build_companies._dataframe_tickers(df), {"BRK-B"})
        self.assertIn("raw XML parse failed", notes)
        self.assertIn("sanitized XML parse attempted", notes)
        self.assertIn("parser_used=ElementTree sanitized", notes)
        self.assertIn("original_parse_error=", notes)


if __name__ == "__main__":
    unittest.main()
