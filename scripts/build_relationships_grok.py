#!/usr/bin/env python3
"""Build Relationship Layer raw rows with Grok batch extraction."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grok_client import DEFAULT_MODEL, build_user_prompt, extract_relationships_raw

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "constellation.db"
RELATIONSHIPS_CSV_PATH = DATA_DIR / "relationships_raw.csv"
DEBUG_BATCH_DIR = DATA_DIR / "debug" / "grok_relationship_batches"
EXTRACTION_METHOD = "grok_api"
VALID_ROLE_CATEGORIES = {"EXECUTIVE", "BOARD"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def person_key(person_name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9\s]", "", person_name.upper())
    return re.sub(r"\s+", "_", normalized.strip())


def batch_id_for(companies: list[tuple[str, str]], model: str) -> str:
    payload = json.dumps({"model": model, "tickers": [ticker for ticker, _ in companies]}, ensure_ascii=False, separators=(",", ":"))
    return "grok_rel_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS relationships_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            person_name TEXT,
            person_key TEXT,
            role TEXT,
            role_category TEXT,
            batch_id TEXT,
            extraction_method TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS relationship_batches (
            batch_id TEXT PRIMARY KEY,
            tickers TEXT,
            status TEXT,
            raw_request TEXT,
            raw_response TEXT,
            error_message TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_relationships_raw_unique
        ON relationships_raw (ticker, person_key, role, role_category);
        """
    )
    connection.commit()


def fetch_companies(connection: sqlite3.Connection, universe: str | None, ticker: str | None, limit: int | None) -> list[tuple[str, str]]:
    where: list[str] = []
    params: list[Any] = []
    if universe:
        where.append("universe = ?")
        params.append(universe.upper())
    if ticker:
        where.append("ticker = ?")
        params.append(ticker.upper())

    sql = "SELECT ticker, company_name FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY ticker, company_name ORDER BY ticker"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    return [(str(row["ticker"]), str(row["company_name"])) for row in rows]


def chunks(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def is_completed(connection: sqlite3.Connection, batch_id: str) -> bool:
    row = connection.execute("SELECT status FROM relationship_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return bool(row and row["status"] == "success")


def upsert_batch(connection: sqlite3.Connection, batch_id: str, tickers: list[str], status: str, raw_request: str, raw_response: str | None, error_message: str | None) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO relationship_batches (batch_id, tickers, status, raw_request, raw_response, error_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
            tickers = excluded.tickers,
            status = excluded.status,
            raw_request = excluded.raw_request,
            raw_response = excluded.raw_response,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
        """,
        (batch_id, json.dumps(tickers, ensure_ascii=False), status, raw_request, raw_response, error_message, now, now),
    )


def parse_relationships(raw_response: str) -> list[dict[str, str]]:
    payload = json.loads(raw_response)
    rows: list[dict[str, str]] = []
    for company in payload.get("companies", []):
        ticker = str(company.get("ticker", "")).strip().upper()
        for relationship in company.get("relationships", []):
            name = str(relationship.get("person_name", "")).strip()
            role = str(relationship.get("role", "")).strip()
            role_category = str(relationship.get("role_category", "")).strip().upper()
            if not ticker or not name or not role or role_category not in VALID_ROLE_CATEGORIES:
                continue
            rows.append({"ticker": ticker, "person_name": name, "person_key": person_key(name), "role": role, "role_category": role_category})
    return rows


def insert_relationships(connection: sqlite3.Connection, rows: list[dict[str, str]], batch_id: str) -> None:
    now = utc_now()
    connection.executemany(
        """
        INSERT INTO relationships_raw (ticker, person_name, person_key, role, role_category, batch_id, extraction_method, created_at, updated_at)
        VALUES (:ticker, :person_name, :person_key, :role, :role_category, :batch_id, :extraction_method, :created_at, :updated_at)
        ON CONFLICT(ticker, person_key, role, role_category) DO UPDATE SET
            person_name = excluded.person_name,
            batch_id = excluded.batch_id,
            extraction_method = excluded.extraction_method,
            updated_at = excluded.updated_at
        """,
        [dict(row, batch_id=batch_id, extraction_method=EXTRACTION_METHOD, created_at=now, updated_at=now) for row in rows],
    )


def export_relationships_csv(connection: sqlite3.Connection) -> None:
    RELATIONSHIPS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT id, ticker, person_name, person_key, role, role_category, batch_id, extraction_method, created_at, updated_at
        FROM relationships_raw
        ORDER BY ticker, person_key, role_category, role
        """
    ).fetchall()
    with RELATIONSHIPS_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "ticker", "person_name", "person_key", "role", "role_category", "batch_id", "extraction_method", "created_at", "updated_at"])
        writer.writerows([tuple(row) for row in rows])


def write_debug(batch_id: str, raw_request: str, raw_response: str | None, error_message: str | None) -> None:
    DEBUG_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"batch_id": batch_id, "raw_request": raw_request, "raw_response": raw_response, "error_message": error_message}
    (DEBUG_BATCH_DIR / f"{batch_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build raw company relationships with Grok API batches.")
    parser.add_argument("--universe", help="Filter companies by universe.")
    parser.add_argument("--ticker", help="Process one ticker.")
    parser.add_argument("--limit", type=int, help="Limit number of companies processed.")
    parser.add_argument("--batch-size", type=int, default=10, help="Companies per Grok request.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Grok model name.")
    parser.add_argument("--resume", action="store_true", help="Skip batches already marked success.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned batches without calling Grok.")
    args = parser.parse_args()

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")

    with connect() as connection:
        create_tables(connection)
        companies = fetch_companies(connection, args.universe, args.ticker, args.limit)
        company_batches = chunks(companies, args.batch_size)
        print(f"Planning {len(companies)} companies in {len(company_batches)} batch(es).")
        for index, batch in enumerate(company_batches, start=1):
            batch_id = batch_id_for(batch, args.model)
            tickers = [ticker for ticker, _ in batch]
            raw_request = build_user_prompt(batch)
            print(f"Batch {index}/{len(company_batches)} {batch_id}: {', '.join(tickers)}")
            if args.resume and is_completed(connection, batch_id):
                print(f"Batch {index}/{len(company_batches)} skipped; already successful.")
                continue
            if args.dry_run:
                continue
            try:
                raw_response = extract_relationships_raw(batch, model=args.model)
                rows = parse_relationships(raw_response)
                insert_relationships(connection, rows, batch_id)
                upsert_batch(connection, batch_id, tickers, "success", raw_request, raw_response, None)
                connection.commit()
                export_relationships_csv(connection)
                write_debug(batch_id, raw_request, raw_response, None)
                print(f"Batch {index}/{len(company_batches)} complete: inserted/updated {len(rows)} relationship row(s).")
            except Exception as error:  # noqa: BLE001 - failure details must be persisted per batch.
                message = traceback.format_exc()
                upsert_batch(connection, batch_id, tickers, "failed", raw_request, None, message)
                connection.commit()
                write_debug(batch_id, raw_request, None, message)
                print(f"Batch {index}/{len(company_batches)} failed: {error}")
                print(message, file=sys.stderr, end="")


if __name__ == "__main__":
    main()
