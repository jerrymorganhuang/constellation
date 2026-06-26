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


if __name__ == "__main__":
    unittest.main()
