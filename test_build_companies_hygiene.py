import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT_DIR = Path(__file__).resolve().parent
BUILD_COMPANIES_PATH = ROOT_DIR / "scripts" / "build_companies.py"

spec = importlib.util.spec_from_file_location("build_companies_hygiene", BUILD_COMPANIES_PATH)
build_companies = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build_companies
assert spec.loader is not None
spec.loader.exec_module(build_companies)


class CompanyHygieneTest(unittest.TestCase):
    def test_placeholder_tickers_filtered_before_metadata_fetch(self):
        membership = {"--": {"RUSSELL2000"}, "N/A": {"RUSSELL1000"}, "AAPL": {"SPX"}}

        clean_membership, excluded = build_companies.filter_invalid_tickers(membership)

        self.assertEqual(clean_membership, {"AAPL": {"SPX"}})
        self.assertEqual([row.ticker for row in excluded], ["--", "N/A"])
        self.assertEqual({row.reason for row in excluded}, {"invalid_placeholder_ticker"})

        fake_yf = Mock()
        fake_yf.Ticker.return_value.get_info.return_value = {"longName": "Apple Inc."}
        with patch.object(build_companies, "yf", fake_yf):
            build_companies.fetch_metadata(sorted(clean_membership))

        fake_yf.Ticker.assert_called_once_with("AAPL")

    def test_empty_company_identity_is_excluded(self):
        rows, excluded = build_companies.build_rows(
            {"BAD": {"RUSSELL2000"}},
            {"BAD": {"company_name": None, "sector": None, "industry": None, "description": None}},
        )

        self.assertEqual(rows, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0].ticker, "BAD")
        self.assertEqual(excluded[0].reason, "metadata_not_found_empty_company")

    def test_company_name_only_is_retained(self):
        rows, excluded = build_companies.build_rows(
            {"GOOD": {"SPX"}},
            {"GOOD": {"company_name": "Good Co", "sector": None, "industry": None, "description": None}},
        )

        self.assertEqual(excluded, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "GOOD")
        self.assertEqual(rows[0]["company_name"], "Good Co")


if __name__ == "__main__":
    unittest.main()
