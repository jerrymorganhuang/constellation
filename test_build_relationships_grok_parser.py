import importlib.util
import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BUILD_RELATIONSHIPS_PATH = ROOT_DIR / "scripts" / "build_relationships_grok.py"
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("build_relationships_grok_parser", BUILD_RELATIONSHIPS_PATH)
build_relationships_grok = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build_relationships_grok
assert spec.loader is not None
spec.loader.exec_module(build_relationships_grok)


class RelationshipParserTest(unittest.TestCase):
    def test_parse_relationships_accepts_markdown_json_fence(self):
        raw_response = '''```json
{
  "companies": [
    {
      "ticker": "NVDA",
      "relationships": [
        {
          "person_name": "Jensen Huang",
          "role": "President and Chief Executive Officer",
          "role_category": "EXECUTIVE"
        }
      ]
    }
  ]
}
```'''

        rows, returned_tickers = build_relationships_grok.parse_relationships(raw_response)

        self.assertEqual(returned_tickers, {"NVDA"})
        self.assertEqual(
            rows,
            [
                {
                    "ticker": "NVDA",
                    "person_name": "Jensen Huang",
                    "person_key": "JENSEN_HUANG",
                    "role": "President and Chief Executive Officer",
                    "role_category": "EXECUTIVE",
                }
            ],
        )

    def test_parse_relationships_accepts_bare_json_with_surrounding_whitespace(self):
        raw_response = '  {"companies": [{"ticker": "AAPL", "relationships": []}]}\n'

        rows, returned_tickers = build_relationships_grok.parse_relationships(raw_response)

        self.assertEqual(rows, [])
        self.assertEqual(returned_tickers, {"AAPL"})


class RelationshipPersistenceTest(unittest.TestCase):
    def test_insert_and_export_persist_company_name_from_input_company_list(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "relationships_raw.csv"
            original_csv_path = build_relationships_grok.RELATIONSHIPS_CSV_PATH
            build_relationships_grok.RELATIONSHIPS_CSV_PATH = csv_path
            try:
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                build_relationships_grok.create_tables(connection)
                rows = [
                    {
                        "ticker": "NVDA",
                        "person_name": "Jensen Huang",
                        "person_key": "JENSEN_HUANG",
                        "role": "President and Chief Executive Officer",
                        "role_category": "EXECUTIVE",
                    }
                ]
                enriched_rows = build_relationships_grok.add_company_names(rows, [("NVDA", "NVIDIA Corporation")])

                build_relationships_grok.insert_relationships(connection, enriched_rows, "batch_1")
                build_relationships_grok.export_relationships_csv(connection)

                db_row = connection.execute("SELECT company_name FROM relationships_raw WHERE ticker = ?", ("NVDA",)).fetchone()
                with csv_path.open(newline="", encoding="utf-8") as handle:
                    csv_rows = list(csv.DictReader(handle))
            finally:
                build_relationships_grok.RELATIONSHIPS_CSV_PATH = original_csv_path

        self.assertEqual(db_row["company_name"], "NVIDIA Corporation")
        self.assertEqual(csv_rows[0]["company_name"], "NVIDIA Corporation")


class RetryCompaniesCsvTest(unittest.TestCase):
    def test_retry_companies_csv_keeps_latest_reason_per_ticker(self):
        with tempfile.TemporaryDirectory() as directory:
            retry_path = Path(directory) / "retry_companies.csv"
            retry_csv = build_relationships_grok.RetryCompaniesCsv(retry_path)

            retry_csv.reset()
            retry_csv.add_companies([("nvda", "NVIDIA Corporation")], "batch_1", "missing_from_response")
            retry_csv.add_companies([("NVDA", "NVIDIA Corporation")], "batch_2", "api_exception")

            with retry_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(retry_csv.count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "NVDA")
        self.assertEqual(rows[0]["company_name"], "NVIDIA Corporation")
        self.assertEqual(rows[0]["batch_id"], "batch_2")
        self.assertEqual(rows[0]["reason"], "api_exception")

    def test_load_retry_companies_csv_preserves_order_and_requires_names(self):
        with tempfile.TemporaryDirectory() as directory:
            retry_path = Path(directory) / "retry_companies.csv"
            retry_path.write_text(
                "ticker,company_name,batch_id,reason,created_at\n"
                " msft ,Microsoft Corporation,batch_1,api_exception,now\n"
                "AAPL,Apple Inc.,batch_2,missing_from_response,now\n",
                encoding="utf-8",
            )

            companies = build_relationships_grok.load_retry_companies_csv(retry_path)

        self.assertEqual(companies, [("MSFT", "Microsoft Corporation"), ("AAPL", "Apple Inc.")])

    def test_load_retry_companies_csv_rejects_missing_company_name_column(self):
        with tempfile.TemporaryDirectory() as directory:
            retry_path = Path(directory) / "retry_companies.csv"
            retry_path.write_text("ticker,batch_id,reason,created_at\nNVDA,batch_1,api_exception,now\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "company_name"):
                build_relationships_grok.load_retry_companies_csv(retry_path)


if __name__ == "__main__":
    unittest.main()
