#!/usr/bin/env python3
"""Derive the minimal people table from canonical relationship rows."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import Any, Iterable

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "constellation.db"
PEOPLE_CSV_PATH = DATA_DIR / "people.csv"
PEOPLE_COLUMNS = ["person_id", "person_name"]


def require_database(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Database file not found: {path}")


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def require_relationships_schema(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "relationships"):
        raise ValueError("Required table not found: relationships")

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(relationships)").fetchall()}
    for required_column in PEOPLE_COLUMNS:
        if required_column not in columns:
            raise ValueError(f"Required column missing from relationships: {required_column}")


def fetch_relationship_people(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    require_relationships_schema(connection)
    return connection.execute("SELECT person_id, person_name FROM relationships").fetchall()


def derive_people_rows(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    names_by_person_id: dict[str, list[str]] = {}
    skipped_blank_person_id = 0

    for row in rows:
        person_id = row["person_id"] or ""
        if person_id == "":
            skipped_blank_person_id += 1
            continue
        person_name = row["person_name"] or ""
        names_by_person_id.setdefault(person_id, []).append(person_name)

    people_rows: list[dict[str, str]] = []
    for person_id in sorted(names_by_person_id):
        non_blank_names = [name for name in names_by_person_id[person_id] if name != ""]
        person_name = min(non_blank_names) if non_blank_names else ""
        people_rows.append({"person_id": person_id, "person_name": person_name})

    return people_rows, skipped_blank_person_id


def create_people_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE people (
            person_id TEXT PRIMARY KEY,
            person_name TEXT
        )
        """
    )


def replace_people(connection: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    connection.execute("DROP TABLE IF EXISTS people")
    create_people_table(connection)
    connection.executemany(
        "INSERT INTO people (person_id, person_name) VALUES (?, ?)",
        [[row[column] for column in PEOPLE_COLUMNS] for row in rows],
    )
    connection.commit()


def export_people_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PEOPLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def derive(connection: sqlite3.Connection, output_path: Path) -> dict[str, Any]:
    source_rows = fetch_relationship_people(connection)
    people_rows, skipped_blank_person_id = derive_people_rows(source_rows)
    replace_people(connection, people_rows)
    export_people_csv(people_rows, output_path)
    return {
        "relationship_rows_read": len(source_rows),
        "unique_people_rows_written": len(people_rows),
        "skipped_blank_person_id_rows": skipped_blank_person_id,
        "sqlite_table_name": "people",
        "output_csv_path": output_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), type=Path)
    parser.add_argument("--output", default=str(PEOPLE_CSV_PATH), type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    require_database(args.db)
    with sqlite3.connect(args.db) as connection:
        connection.row_factory = sqlite3.Row
        summary = derive(connection, args.output)
    print(f"relationship rows read: {summary['relationship_rows_read']}")
    print(f"unique people rows written: {summary['unique_people_rows_written']}")
    print(f"skipped blank person_id rows: {summary['skipped_blank_person_id_rows']}")
    print(f"SQLite table name: {summary['sqlite_table_name']}")
    print(f"CSV output path: {summary['output_csv_path']}")


if __name__ == "__main__":
    main()
