import csv
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT_DIR / "scripts" / "normalize_relationships.py"
spec = importlib.util.spec_from_file_location("normalize_relationships", SCRIPT_PATH)
normalize_relationships = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = normalize_relationships
assert spec.loader is not None
spec.loader.exec_module(normalize_relationships)


class NormalizeRelationshipsTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE relationships_raw (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                person_name TEXT,
                person_key TEXT,
                role TEXT,
                role_category TEXT,
                company_name TEXT,
                batch_id TEXT,
                extraction_method TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

    def tearDown(self):
        self.connection.close()

    def insert_raw(self, **overrides):
        row = {
            "ticker": "NVDA",
            "person_name": "Jensen Huang",
            "person_key": "JENSEN_HUANG",
            "role": "President and Chief Executive Officer",
            "role_category": "EXECUTIVE",
            "company_name": "NVIDIA Corporation",
            "batch_id": "batch_1",
            "extraction_method": "grok_api",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": None,
        }
        row.update(overrides)
        columns = list(row.keys())
        self.connection.execute(
            f"INSERT INTO relationships_raw ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )
        self.connection.commit()

    def write_company_master(self, directory: tempfile.TemporaryDirectory, tickers=None, header="ticker"):
        tickers = ["AAPL", "MSFT", "NVDA"] if tickers is None else tickers
        companies_path = Path(directory.name) / "companies.csv"
        with companies_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([header, "company_name"])
            for ticker in tickers:
                writer.writerow([ticker, f"{ticker.strip()} Company"])
        return companies_path

    def normalize_to_temp_csv(self, company_tickers=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output_path = Path(directory.name) / "relationships.csv"
        companies_path = self.write_company_master(directory, company_tickers)
        summary = normalize_relationships.normalize(self.connection, output_path, companies_path)
        return output_path, summary

    def test_person_id_normalization_examples(self):
        cases = {
            "Jensen Huang": "JENSEN_HUANG",
            "Colette M. Kress": "COLETTE_M_KRESS",
            "A. Brooke Seawell": "A_BROOKE_SEAWELL",
            "Dr. Lisa T. Su": "LISA_T_SU",
            "Mr. Mark A. Stevens": "MARK_A_STEVENS",
            "John Smith, Jr.": "JOHN_SMITH",
            "Jane Doe III": "JANE_DOE",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_relationships.person_id_for(source), expected)

    def test_broad_ceo_titles_normalize_within_executive(self):
        cases = [
            "CEO",
            "Group CEO",
            "Co-CEO",
            "Interim CEO",
            "Acting CEO",
            "CEO, Schwab Asset Management",
            "CEO of Schwab Bank",
            "President and CEO",
            "President & CEO",
            "Chief Executive Officer",
            "President and Chief Executive Officer",
            "Chief Executive Officer, The Charles Schwab Corporation",
            "Chief Executive Officer - Americas",
            "Executive Chair and Chief Executive Officer",
            "  President   and   CEO.  ",
        ]
        for role in cases:
            with self.subTest(role=role):
                self.assertEqual(normalize_relationships.normalized_role(role, "EXECUTIVE"), "CEO")

    def test_broad_cfo_titles_normalize_within_executive(self):
        cases = [
            "CFO",
            "Group CFO",
            "Co-CFO",
            "Interim CFO",
            "Acting CFO",
            "CFO, Business Unit",
            "CFO of Subsidiary",
            "Executive Vice President and CFO",
            "Chief Financial Officer",
            "Chief Financial Officer, Company Name",
            "Chief Financial Officer - Americas",
            "  EVP   and   CFO;  ",
        ]
        for role in cases:
            with self.subTest(role=role):
                self.assertEqual(normalize_relationships.normalized_role(role, "EXECUTIVE"), "CFO")

    def test_ceo_cfo_only_normalized_within_executive(self):
        self.assertEqual(
            normalize_relationships.normalized_role("Chief Executive Officer", "BOARD"),
            "Chief Executive Officer",
        )
        self.assertEqual(
            normalize_relationships.normalized_role("Chief Financial Officer", "BOARD"),
            "Chief Financial Officer",
        )

    def test_chairman_only_normalized_within_board(self):
        self.assertEqual(normalize_relationships.normalized_role("Independent Chair of the Board", "BOARD"), "Chairman")
        self.assertEqual(
            normalize_relationships.normalized_role("Executive Chair and Chief Executive Officer", "EXECUTIVE"),
            "CEO",
        )

    def test_snapshot_latest_ticker_logic_uses_latest_updated_at(self):
        self.insert_raw(
            ticker="NVDA",
            person_name="Old Person",
            batch_id="same_batch",
            created_at="2026-06-30T00:00:00+00:00",
            updated_at="2026-06-30T12:00:00+00:00",
        )
        self.insert_raw(
            ticker="NVDA",
            person_name="New CEO",
            batch_id="same_batch",
            created_at="2026-07-01T00:00:00+00:00",
            updated_at="2026-07-01T12:00:00+00:00",
        )
        self.insert_raw(
            ticker="NVDA",
            person_name="New CFO",
            role="Chief Financial Officer",
            batch_id="different_batch",
            created_at="2026-07-01T00:00:00+00:00",
            updated_at="2026-07-01T12:00:00+00:00",
        )
        self.insert_raw(
            ticker="AAPL",
            person_name="Apple CEO",
            batch_id="apple",
            created_at="2026-01-15T00:00:00+00:00",
            updated_at="2026-01-15T12:00:00+00:00",
        )

        output_path, summary = self.normalize_to_temp_csv()

        rows = self.connection.execute("SELECT person_name, extraction_time FROM relationships ORDER BY person_name").fetchall()
        self.assertEqual([row["person_name"] for row in rows], ["Apple CEO", "New CEO", "New CFO"])
        self.assertNotIn("Old Person", [row["person_name"] for row in rows])
        self.assertEqual(
            {row["extraction_time"] for row in rows if row["person_name"].startswith("New")},
            {"2026-07-01T12:00:00+00:00"},
        )
        self.assertEqual(summary["selected_latest_snapshot_row_count"], 3)
        self.assertTrue(output_path.exists())

    def test_snapshot_latest_ticker_logic_falls_back_to_created_at_when_updated_at_is_null(self):
        self.insert_raw(ticker="MSFT", person_name="Old Person", created_at="2026-06-30T00:00:00+00:00", updated_at=None)
        self.insert_raw(ticker="MSFT", person_name="New CEO", created_at="2026-07-01T00:00:00+00:00", updated_at=None)
        self.insert_raw(
            ticker="MSFT",
            person_name="New CFO",
            role="Chief Financial Officer",
            created_at="2026-07-01T00:00:00+00:00",
            updated_at=None,
        )

        self.normalize_to_temp_csv()

        rows = self.connection.execute("SELECT person_name, extraction_time FROM relationships ORDER BY person_name").fetchall()
        self.assertEqual([row["person_name"] for row in rows], ["New CEO", "New CFO"])
        self.assertNotIn("Old Person", [row["person_name"] for row in rows])
        self.assertEqual({row["extraction_time"] for row in rows}, {"2026-07-01T00:00:00+00:00"})

    def test_dedup_keeps_newest_row(self):
        self.insert_raw(person_name="Dr. Lisa T. Su", role="CEO", created_at="2026-01-01T00:00:00+00:00", batch_id="latest")
        self.insert_raw(person_name="Lisa T Su", role="CEO", created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-02T00:00:00+00:00", batch_id="latest")

        self.normalize_to_temp_csv()

        rows = self.connection.execute("SELECT person_name, extraction_time FROM relationships").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_name"], "Lisa T Su")
        self.assertEqual(rows[0]["extraction_time"], "2026-01-02T00:00:00+00:00")

    def test_multiple_ceo_rows_for_one_ticker_may_be_retained(self):
        self.insert_raw(ticker="NVDA", person_name="CEO One", role="CEO")
        self.insert_raw(ticker="NVDA", person_name="CEO Two", role="CEO")

        self.normalize_to_temp_csv(company_tickers=["NVDA"])

        rows = self.connection.execute("SELECT person_name, role FROM relationships ORDER BY person_name").fetchall()
        self.assertEqual([(row["person_name"], row["role"]) for row in rows], [("CEO One", "CEO"), ("CEO Two", "CEO")])

    def test_batch_id_does_not_affect_snapshot_selection(self):
        self.insert_raw(
            ticker="NVDA",
            person_name="Old Batch Person",
            batch_id="zzzz",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T12:00:00+00:00",
        )
        self.insert_raw(
            ticker="NVDA",
            person_name="Latest Batch Person",
            batch_id="aaaa",
            created_at="2026-01-02T00:00:00+00:00",
            updated_at="2026-01-02T12:00:00+00:00",
        )

        self.normalize_to_temp_csv(company_tickers=["NVDA"])

        rows = self.connection.execute("SELECT person_name FROM relationships").fetchall()
        self.assertEqual([row["person_name"] for row in rows], ["Latest Batch Person"])

    def test_company_master_filter_excludes_raw_ticker_absent_from_canonical_output(self):
        self.insert_raw(ticker="NVDA", person_name="Valid CEO")
        self.insert_raw(ticker=" AARD ", person_name="Historical CEO", company_name="Aardvark Historical")

        output_path, summary = self.normalize_to_temp_csv(company_tickers=[" NVDA "])

        canonical_tickers = [
            row["ticker"]
            for row in self.connection.execute("SELECT ticker FROM relationships ORDER BY ticker")
        ]
        self.assertEqual(canonical_tickers, ["NVDA"])
        with output_path.open(newline="", encoding="utf-8") as handle:
            csv_tickers = [row["ticker"] for row in csv.DictReader(handle)]
        self.assertEqual(csv_tickers, ["NVDA"])
        self.assertEqual(summary["excluded_tickers"], ["AARD"])
        self.assertEqual(summary["excluded_ticker_count"], 1)

    def test_company_master_filter_preserves_excluded_raw_source_rows(self):
        self.insert_raw(ticker="NVDA", person_name="Valid CEO")
        self.insert_raw(ticker="ACNT", person_name="Historical CEO")

        self.normalize_to_temp_csv(company_tickers=["NVDA"])

        raw_tickers = [
            row["ticker"]
            for row in self.connection.execute("SELECT ticker FROM relationships_raw ORDER BY ticker")
        ]
        self.assertEqual(raw_tickers, ["ACNT", "NVDA"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relationships_raw WHERE ticker = 'ACNT'").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relationships WHERE ticker = 'ACNT'").fetchone()[0],
            0,
        )

    def test_company_master_filter_keeps_valid_master_tickers_and_reports_counts(self):
        self.insert_raw(ticker="NVDA", person_name="Valid CEO")
        self.insert_raw(ticker="MSFT", person_name="Valid CFO", role="Chief Financial Officer")
        self.insert_raw(ticker="AISP", person_name="Historical CEO")

        _, summary = self.normalize_to_temp_csv(company_tickers=["NVDA", " MSFT "])

        canonical_tickers = [
            row["ticker"]
            for row in self.connection.execute("SELECT ticker FROM relationships ORDER BY ticker")
        ]
        self.assertEqual(canonical_tickers, ["MSFT", "NVDA"])
        self.assertEqual(summary["company_master_ticker_count"], 2)
        self.assertEqual(summary["raw_snapshot_distinct_ticker_count"], 3)
        self.assertEqual(summary["canonical_ticker_count"], 2)
        self.assertEqual(summary["canonical_relationship_count"], 2)
        self.assertEqual(summary["excluded_tickers"], ["AISP"])

    def test_missing_company_master_file_fails_clearly(self):
        self.insert_raw()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        with self.assertRaisesRegex(FileNotFoundError, "Company Master file not found"):
            normalize_relationships.normalize(
                self.connection,
                Path(directory.name) / "relationships.csv",
                Path(directory.name) / "missing_companies.csv",
            )

    def test_missing_company_master_ticker_column_fails_clearly(self):
        self.insert_raw()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        companies_path = self.write_company_master(directory, header="symbol")

        with self.assertRaisesRegex(ValueError, "must contain a ticker column"):
            normalize_relationships.normalize(
                self.connection, Path(directory.name) / "relationships.csv", companies_path
            )

    def test_csv_header_exactly_matches_required_columns(self):
        self.insert_raw()

        output_path, _ = self.normalize_to_temp_csv()

        with output_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader)
        self.assertEqual(header, normalize_relationships.RELATIONSHIPS_COLUMNS)


if __name__ == "__main__":
    unittest.main()
