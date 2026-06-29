#!/usr/bin/env python3
"""Derive distinct people from raw relationship rows."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "constellation.db"
PEOPLE_CSV_PATH = DATA_DIR / "people.csv"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_people_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            person_name TEXT,
            person_key TEXT PRIMARY KEY,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    connection.commit()


def derive_people(connection: sqlite3.Connection) -> int:
    now = utc_now()
    rows = connection.execute(
        """
        SELECT person_key, MIN(person_name) AS person_name
        FROM relationships_raw
        WHERE person_key IS NOT NULL AND person_key != ''
        GROUP BY person_key
        ORDER BY person_key
        """
    ).fetchall()
    connection.executemany(
        """
        INSERT INTO people (person_name, person_key, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(person_key) DO UPDATE SET
            person_name = excluded.person_name,
            updated_at = excluded.updated_at
        """,
        [(row["person_name"], row["person_key"], now, now) for row in rows],
    )
    connection.commit()
    return len(rows)


def export_people_csv(connection: sqlite3.Connection) -> None:
    PEOPLE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT person_name, person_key, created_at, updated_at
        FROM people
        ORDER BY person_key
        """
    ).fetchall()
    with PEOPLE_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["person_name", "person_key", "created_at", "updated_at"])
        writer.writerows([tuple(row) for row in rows])


def main() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        create_people_table(connection)
        count = derive_people(connection)
        export_people_csv(connection)
        print(f"Derived {count} people into people table and {PEOPLE_CSV_PATH.relative_to(ROOT_DIR)}.")


if __name__ == "__main__":
    main()
