#!/usr/bin/env python3
"""Normalize raw relationship rows into the canonical relationships snapshot."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "constellation.db"
RELATIONSHIPS_CSV_PATH = DATA_DIR / "relationships.csv"
COMPANIES_CSV_PATH = DATA_DIR / "companies.csv"
RELATIONSHIPS_COLUMNS = [
    "ticker",
    "company_name",
    "person_id",
    "person_name",
    "role",
    "role_category",
    "extraction_time",
]
PREFIXES = {"MR", "MS", "MRS", "DR", "SIR", "DAME"}
SUFFIXES = {"JR", "SR", "II", "III", "IV", "CPA", "CFA", "PHD", "MD", "ESQ"}


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def person_id_for(person_name: str) -> str:
    """Return the deterministic canonical person_id for a source person name."""
    cleaned = collapse_spaces(person_name)
    tokens = cleaned.split(" ")

    while tokens and re.sub(r"[^A-Za-z]", "", tokens[0]).upper() in PREFIXES:
        tokens.pop(0)
    while tokens and re.sub(r"[^A-Za-z]", "", tokens[-1]).upper() in SUFFIXES:
        tokens.pop()

    cleaned = " ".join(tokens)
    cleaned = re.sub(r"[^A-Za-z0-9\s]", "", cleaned)
    cleaned = collapse_spaces(cleaned).upper()
    return cleaned.replace(" ", "_")


def normalized_role(role: str, role_category: str) -> str:
    category = role_category.strip()
    if category == "EXECUTIVE":
        if re.search(r"\bCEO\b|Chief Executive Officer", role, flags=re.IGNORECASE):
            return "CEO"
        if re.search(r"\bCFO\b|Chief Financial Officer", role, flags=re.IGNORECASE):
            return "CFO"
    if category == "BOARD" and re.search(r"chair", role, flags=re.IGNORECASE):
        return "Chairman"
    return role.strip()


def create_relationships_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS relationships (
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
    connection.commit()


def raw_sort_key(row: sqlite3.Row | dict[str, Any]) -> tuple[str, int]:
    return (row["updated_at"] or row["created_at"] or "", int(row["id"] or 0))


def source_extraction_time(row: sqlite3.Row | dict[str, Any]) -> str:
    return row["updated_at"] or row["created_at"] or ""


def select_latest_snapshot_rows(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> tuple[list[sqlite3.Row | dict[str, Any]], int]:
    rows_by_ticker: dict[str, list[sqlite3.Row | dict[str, Any]]] = {}
    skipped = 0
    for row in rows:
        ticker = (row["ticker"] or "").strip().upper()
        if ticker:
            rows_by_ticker.setdefault(ticker, []).append(row)
        else:
            skipped += 1

    selected: list[sqlite3.Row | dict[str, Any]] = []
    for ticker_rows in rows_by_ticker.values():
        updated_at_values = [row["updated_at"] for row in ticker_rows if row["updated_at"]]
        if updated_at_values:
            latest_time = max(updated_at_values)
            selected.extend(row for row in ticker_rows if row["updated_at"] == latest_time)
        else:
            created_at_values = [row["created_at"] for row in ticker_rows if row["created_at"]]
            latest_time = max(created_at_values, default="")
            selected.extend(row for row in ticker_rows if (row["created_at"] or "") == latest_time)
    return selected, skipped


def load_company_master_tickers(companies_path: Path) -> set[str]:
    if not companies_path.exists():
        raise FileNotFoundError(f"Company Master file not found: {companies_path}")

    with companies_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "ticker" not in reader.fieldnames:
            raise ValueError(f"Company Master file must contain a ticker column: {companies_path}")
        return {
            (row.get("ticker") or "").strip().upper()
            for row in reader
            if (row.get("ticker") or "").strip()
        }


def filter_rows_to_company_master(
    rows: Iterable[sqlite3.Row | dict[str, Any]], company_master_tickers: set[str]
) -> tuple[list[sqlite3.Row | dict[str, Any]], set[str], int]:
    rows_list = list(rows)
    raw_snapshot_tickers = {
        (row["ticker"] or "").strip().upper()
        for row in rows_list
        if (row["ticker"] or "").strip()
    }
    excluded_tickers = raw_snapshot_tickers - company_master_tickers
    filtered_rows = [
        row
        for row in rows_list
        if (row["ticker"] or "").strip().upper() in company_master_tickers
    ]
    return filtered_rows, excluded_tickers, len(raw_snapshot_tickers)


def canonicalize_rows(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    canonical_by_key: dict[tuple[str, str, str, str], tuple[tuple[str, int], dict[str, str]]] = {}
    skipped = 0
    for row in rows:
        ticker = (row["ticker"] or "").strip().upper()
        company_name = (row["company_name"] or "").strip()
        person_name = collapse_spaces(row["person_name"] or "")
        role_category = (row["role_category"] or "").strip()
        role = (row["role"] or "").strip()
        if not ticker or not person_name or not role or not role_category:
            skipped += 1
            continue
        canonical_role = normalized_role(role, role_category)
        person_id = person_id_for(person_name)
        canonical = {
            "ticker": ticker,
            "company_name": company_name,
            "person_id": person_id,
            "person_name": person_name,
            "role": canonical_role,
            "role_category": role_category,
            "extraction_time": source_extraction_time(row),
        }
        key = (ticker, person_id, canonical_role, role_category)
        existing = canonical_by_key.get(key)
        row_key = raw_sort_key(row)
        if existing is None or row_key > existing[0]:
            canonical_by_key[key] = (row_key, canonical)
    return [value[1] for value in sorted(canonical_by_key.values(), key=lambda item: tuple(item[1][column] for column in RELATIONSHIPS_COLUMNS))], skipped


def fetch_raw_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM relationships_raw").fetchall()


def replace_relationships(connection: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    connection.execute("DROP TABLE IF EXISTS relationships")
    create_relationships_table(connection)
    connection.executemany(
        f"INSERT INTO relationships ({','.join(RELATIONSHIPS_COLUMNS)}) VALUES ({','.join('?' for _ in RELATIONSHIPS_COLUMNS)})",
        [[row[column] for column in RELATIONSHIPS_COLUMNS] for row in rows],
    )
    connection.commit()


def export_relationships_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RELATIONSHIPS_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def normalize(
    connection: sqlite3.Connection, output_path: Path, companies_path: Path = COMPANIES_CSV_PATH
) -> dict[str, Any]:
    company_master_tickers = load_company_master_tickers(companies_path)
    source_rows = fetch_raw_rows(connection)
    snapshot_rows, snapshot_skipped = select_latest_snapshot_rows(source_rows)
    filtered_rows, excluded_tickers, raw_snapshot_ticker_count = filter_rows_to_company_master(
        snapshot_rows, company_master_tickers
    )
    canonical_rows, canonical_skipped = canonicalize_rows(filtered_rows)
    canonical_tickers = {row["ticker"] for row in canonical_rows}
    replace_relationships(connection, canonical_rows)
    export_relationships_csv(canonical_rows, output_path)
    return {
        "source_row_count": len(source_rows),
        "selected_latest_snapshot_row_count": len(snapshot_rows),
        "company_master_ticker_count": len(company_master_tickers),
        "raw_snapshot_distinct_ticker_count": raw_snapshot_ticker_count,
        "excluded_ticker_count": len(excluded_tickers),
        "excluded_tickers": sorted(excluded_tickers),
        "canonical_ticker_count": len(canonical_tickers),
        "canonical_relationship_count": len(canonical_rows),
        "skipped_row_count": snapshot_skipped + canonical_skipped,
        "output_csv_path": output_path,
        "sqlite_table_name": "relationships",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), type=Path)
    parser.add_argument("--output", default=str(RELATIONSHIPS_CSV_PATH), type=Path)
    parser.add_argument("--companies", default=str(COMPANIES_CSV_PATH), type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with sqlite3.connect(args.db) as connection:
        connection.row_factory = sqlite3.Row
        summary = normalize(connection, args.output, args.companies)
    print(f"source row count: {summary['source_row_count']}")
    print(f"selected latest snapshot row count: {summary['selected_latest_snapshot_row_count']}")
    print(f"Company Master ticker count: {summary['company_master_ticker_count']}")
    print(f"raw snapshot distinct ticker count before Company Master filtering: {summary['raw_snapshot_distinct_ticker_count']}")
    print(f"excluded ticker count: {summary['excluded_ticker_count']}")
    print(f"sorted excluded ticker list: {summary['excluded_tickers']}")
    print(f"canonical ticker count: {summary['canonical_ticker_count']}")
    print(f"canonical relationship row count: {summary['canonical_relationship_count']}")
    print(f"skipped row count: {summary['skipped_row_count']}")
    print(f"output CSV path: {summary['output_csv_path']}")
    print(f"SQLite table name: {summary['sqlite_table_name']}")


if __name__ == "__main__":
    main()
