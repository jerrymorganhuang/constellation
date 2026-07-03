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

    def normalize_to_temp_csv(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output_path = Path(directory.name) / "relationships.csv"
        summary = normalize_relationships.normalize(self.connection, output_path)
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

    def test_ceo_cfo_only_normalized_within_executive(self):
        self.assertEqual(normalize_relationships.normalized_role("President and Chief Executive Officer", "EXECUTIVE"), "CEO")
        self.assertEqual(normalize_relationships.normalized_role("Chief Financial Officer", "EXECUTIVE"), "CFO")
        self.assertEqual(
            normalize_relationships.normalized_role("Chief Executive Officer and Director", "BOARD"),
            "Chief Executive Officer and Director",
        )

    def test_chairman_only_normalized_within_board(self):
        self.assertEqual(normalize_relationships.normalized_role("Independent Chair of the Board", "BOARD"), "Chairman")
        self.assertEqual(
            normalize_relationships.normalized_role("Executive Chair and Chief Executive Officer", "EXECUTIVE"),
            "CEO",
        )

    def test_snapshot_latest_ticker_logic(self):
        self.insert_raw(ticker="NVDA", person_name="Old Person", batch_id="old", created_at="2026-01-01T00:00:00+00:00")
        self.insert_raw(ticker="NVDA", person_name="New CEO", batch_id="new", created_at="2026-02-01T00:00:00+00:00")
        self.insert_raw(ticker="NVDA", person_name="New CFO", role="Chief Financial Officer", batch_id="new", created_at="2026-02-01T00:00:00+00:00")
        self.insert_raw(ticker="AAPL", person_name="Apple CEO", batch_id="apple", created_at="2026-01-15T00:00:00+00:00")

        output_path, summary = self.normalize_to_temp_csv()

        names = [row["person_name"] for row in self.connection.execute("SELECT person_name FROM relationships ORDER BY person_name")]
        self.assertEqual(names, ["Apple CEO", "New CEO", "New CFO"])
        self.assertEqual(summary["selected_latest_snapshot_row_count"], 3)
        self.assertTrue(output_path.exists())

    def test_dedup_keeps_newest_row(self):
        self.insert_raw(person_name="Dr. Lisa T. Su", role="CEO", created_at="2026-01-01T00:00:00+00:00", batch_id="latest")
        self.insert_raw(person_name="Lisa T Su", role="CEO", created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-02T00:00:00+00:00", batch_id="latest")

        self.normalize_to_temp_csv()

        rows = self.connection.execute("SELECT person_name, extraction_time FROM relationships").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_name"], "Lisa T Su")
        self.assertEqual(rows[0]["extraction_time"], "2026-01-02T00:00:00+00:00")

    def test_csv_header_exactly_matches_required_columns(self):
        self.insert_raw()

        output_path, _ = self.normalize_to_temp_csv()

        with output_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader)
        self.assertEqual(header, normalize_relationships.RELATIONSHIPS_COLUMNS)


if __name__ == "__main__":
    unittest.main()
