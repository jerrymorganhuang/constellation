import csv
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT_DIR / "scripts" / "derive_people.py"
spec = importlib.util.spec_from_file_location("derive_people", SCRIPT_PATH)
derive_people = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = derive_people
assert spec.loader is not None
spec.loader.exec_module(derive_people)


class DerivePeopleTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE relationships (
                ticker TEXT,
                company_name TEXT,
                person_id TEXT,
                person_name TEXT,
                role TEXT,
                role_category TEXT,
                extraction_time TEXT
            )
            """
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.output_path = Path(self.tempdir.name) / "people.csv"

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def insert_relationship(self, person_id, person_name, ticker="NVDA", role="CEO"):
        self.connection.execute(
            """
            INSERT INTO relationships (
                ticker, company_name, person_id, person_name, role, role_category, extraction_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, f"{ticker} Company", person_id, person_name, role, "EXECUTIVE", "2026-01-01T00:00:00+00:00"),
        )
        self.connection.commit()

    def derive(self):
        return derive_people.derive(self.connection, self.output_path)

    def read_csv_rows(self):
        with self.output_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))

    def test_reads_from_canonical_relationships_and_does_not_require_raw(self):
        self.insert_relationship("JENSEN_HUANG", "Jensen Huang")

        summary = self.derive()

        self.assertEqual(summary["relationship_rows_read"], 1)
        self.assertEqual(summary["unique_people_rows_written"], 1)
        rows = self.connection.execute("SELECT person_id, person_name FROM people").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("JENSEN_HUANG", "Jensen Huang")])
        self.assertFalse(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'relationships_raw'"
            ).fetchone()
        )

    def test_deduplicates_by_person_id_and_selects_lexicographically_smallest_non_blank_name(self):
        self.insert_relationship("LISA_SU", "Lisa T. Su")
        self.insert_relationship("LISA_SU", "")
        self.insert_relationship("LISA_SU", "Dr. Lisa T. Su")
        self.insert_relationship("JENSEN_HUANG", "Jensen Huang")

        self.derive()

        rows = self.connection.execute("SELECT person_id, person_name FROM people ORDER BY person_id").fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("JENSEN_HUANG", "Jensen Huang"), ("LISA_SU", "Dr. Lisa T. Su")],
        )

    def test_uses_empty_name_when_all_names_are_blank(self):
        self.insert_relationship("UNKNOWN_PERSON", "")
        self.insert_relationship("UNKNOWN_PERSON", None)

        self.derive()

        row = self.connection.execute("SELECT person_name FROM people WHERE person_id = 'UNKNOWN_PERSON'").fetchone()
        self.assertEqual(row["person_name"], "")

    def test_blank_person_id_rows_are_skipped_and_csv_header_is_exact(self):
        self.insert_relationship("", "Blank Person")
        self.insert_relationship(None, "Null Person")
        self.insert_relationship("A_PERSON", "A Person")

        summary = self.derive()

        self.assertEqual(summary["skipped_blank_person_id_rows"], 2)
        self.assertEqual(self.read_csv_rows(), [["person_id", "person_name"], ["A_PERSON", "A Person"]])

    def test_person_id_is_sqlite_primary_key(self):
        self.insert_relationship("A_PERSON", "A Person")

        self.derive()

        columns = self.connection.execute("PRAGMA table_info(people)").fetchall()
        primary_keys = {row["name"]: row["pk"] for row in columns}
        self.assertEqual(primary_keys, {"person_id": 1, "person_name": 0})

    def test_repeated_runs_fully_replace_people_and_leave_relationships_unchanged(self):
        self.insert_relationship("OLD_PERSON", "Old Person")
        before_relationships = [tuple(row) for row in self.connection.execute("SELECT * FROM relationships").fetchall()]
        self.derive()

        self.connection.execute("DELETE FROM relationships WHERE person_id = 'OLD_PERSON'")
        self.insert_relationship("NEW_PERSON", "New Person")
        current_relationships = [tuple(row) for row in self.connection.execute("SELECT * FROM relationships").fetchall()]
        self.derive()

        people = self.connection.execute("SELECT person_id, person_name FROM people ORDER BY person_id").fetchall()
        self.assertEqual([tuple(row) for row in people], [("NEW_PERSON", "New Person")])
        after_relationships = [tuple(row) for row in self.connection.execute("SELECT * FROM relationships").fetchall()]
        self.assertNotEqual(before_relationships, after_relationships)
        self.assertEqual(after_relationships, current_relationships)

    def test_fails_clearly_when_relationships_table_or_required_columns_are_missing(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        self.addCleanup(empty.close)
        with self.assertRaisesRegex(ValueError, "relationships"):
            derive_people.derive(empty, self.output_path)

        missing_name = sqlite3.connect(":memory:")
        missing_name.row_factory = sqlite3.Row
        self.addCleanup(missing_name.close)
        missing_name.execute("CREATE TABLE relationships (person_id TEXT)")
        with self.assertRaisesRegex(ValueError, "person_name"):
            derive_people.derive(missing_name, self.output_path)


if __name__ == "__main__":
    unittest.main()
