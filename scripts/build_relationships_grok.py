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
MISSING_COMPANIES_CSV_PATH = DATA_DIR / "missing_companies.csv"
RETRY_COMPANIES_CSV_PATH = DATA_DIR / "retry_companies.csv"
DEBUG_BATCH_DIR = DATA_DIR / "debug" / "grok_relationship_batches"
EXTRACTION_METHOD = "grok_api"
VALID_ROLE_CATEGORIES = {"EXECUTIVE", "BOARD"}
INPUT_COST_PER_1M_TOKENS = 1.25
OUTPUT_COST_PER_1M_TOKENS = 2.50


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
            company_name TEXT,
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
    ensure_relationships_raw_company_name_column(connection)
    ensure_relationship_batch_usage_columns(connection)
    connection.commit()


def ensure_relationships_raw_company_name_column(connection: sqlite3.Connection) -> None:
    existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(relationships_raw)").fetchall()}
    if "company_name" not in existing_columns:
        connection.execute("ALTER TABLE relationships_raw ADD COLUMN company_name TEXT")


def ensure_relationship_batch_usage_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(relationship_batches)").fetchall()}
    columns = {
        "response_id": "TEXT",
        "model": "TEXT",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "cached_input_tokens": "INTEGER",
        "cost_usd": "REAL",
        "usage_json": "TEXT",
    }
    for column_name, column_type in columns.items():
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE relationship_batches ADD COLUMN {column_name} {column_type}")


def fetch_companies(connection: sqlite3.Connection, universe: str | None, ticker: str | None) -> list[tuple[str, str]]:
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
    rows = connection.execute(sql, params).fetchall()
    return [(str(row["ticker"]), str(row["company_name"])) for row in rows]


def chunks(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def load_retry_companies_csv(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        required_columns = {"ticker", "company_name"}
        missing_columns = sorted(required_columns - fieldnames)
        if missing_columns:
            raise ValueError(f"--retry-file must include columns: {', '.join(sorted(required_columns))}; missing: {', '.join(missing_columns)}")

        companies: list[tuple[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker", "")).strip().upper()
            company_name = str(row.get("company_name", "")).strip()
            if not ticker or not company_name:
                raise ValueError(f"--retry-file row {line_number} must include non-empty ticker and company_name")
            companies.append((ticker, company_name))
        return companies


def is_completed(connection: sqlite3.Connection, batch_id: str) -> bool:
    row = connection.execute("SELECT status FROM relationship_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return bool(row and row["status"] == "success")


def upsert_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    tickers: list[str],
    status: str,
    raw_request: str,
    raw_response: str | None,
    error_message: str | None,
    metadata: Any | None = None,
    cost_usd: float | None = None,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO relationship_batches (
            batch_id, tickers, status, raw_request, raw_response, error_message,
            response_id, model, input_tokens, output_tokens, total_tokens, cached_input_tokens, cost_usd, usage_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
            tickers = excluded.tickers,
            status = excluded.status,
            raw_request = excluded.raw_request,
            raw_response = excluded.raw_response,
            error_message = excluded.error_message,
            response_id = excluded.response_id,
            model = excluded.model,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            total_tokens = excluded.total_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cost_usd = excluded.cost_usd,
            usage_json = excluded.usage_json,
            updated_at = excluded.updated_at
        """,
        (
            batch_id,
            json.dumps(tickers, ensure_ascii=False),
            status,
            raw_request,
            raw_response,
            error_message,
            getattr(metadata, "response_id", None),
            getattr(metadata, "model", None),
            getattr(metadata, "input_tokens", None),
            getattr(metadata, "output_tokens", None),
            getattr(metadata, "total_tokens", None),
            getattr(metadata, "cached_input_tokens", None),
            cost_usd,
            getattr(metadata, "usage_json", None),
            now,
            now,
        ),
    )


def calculate_cost_usd(input_tokens: int | None, output_tokens: int | None) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens * INPUT_COST_PER_1M_TOKENS / 1_000_000 + output_tokens * OUTPUT_COST_PER_1M_TOKENS / 1_000_000


def format_metric(value: int | float | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def normalize_json_response(raw_response: str) -> str:
    """Return model JSON text, accepting optional Markdown code fences.

    Grok usually follows the prompt and returns a bare JSON object, but some
    otherwise-valid batches have been observed wrapped in a fenced code block
    such as ```json ... ```. json.loads() fails on the opening backtick, so
    unwrap only when the entire response is a single fenced block.
    """
    text = raw_response.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].startswith("```") or lines[-1].strip() != "```":
        return text

    fence_language = lines[0][3:].strip().lower()
    if fence_language not in {"", "json"}:
        return text

    return "\n".join(lines[1:-1]).strip()


def parse_relationships(raw_response: str) -> tuple[list[dict[str, str]], set[str]]:
    payload = json.loads(normalize_json_response(raw_response))
    rows: list[dict[str, str]] = []
    returned_tickers: set[str] = set()
    for company in payload.get("companies", []):
        ticker = str(company.get("ticker", "")).strip().upper()
        if ticker:
            returned_tickers.add(ticker)
        for relationship in company.get("relationships", []):
            name = str(relationship.get("person_name", "")).strip()
            role = str(relationship.get("role", "")).strip()
            role_category = str(relationship.get("role_category", "")).strip().upper()
            if not ticker or not name or not role or role_category not in VALID_ROLE_CATEGORIES:
                continue
            rows.append({"ticker": ticker, "person_name": name, "person_key": person_key(name), "role": role, "role_category": role_category})
    return rows, returned_tickers


def add_company_names(rows: list[dict[str, str]], companies: list[tuple[str, str]]) -> list[dict[str, str]]:
    ticker_to_company_name = company_name_by_ticker(companies)
    return [dict(row, company_name=ticker_to_company_name.get(row["ticker"], "")) for row in rows]


def insert_relationships(connection: sqlite3.Connection, rows: list[dict[str, str]], batch_id: str) -> None:
    now = utc_now()
    connection.executemany(
        """
        INSERT INTO relationships_raw (ticker, person_name, person_key, role, role_category, company_name, batch_id, extraction_method, created_at, updated_at)
        VALUES (:ticker, :person_name, :person_key, :role, :role_category, :company_name, :batch_id, :extraction_method, :created_at, :updated_at)
        ON CONFLICT(ticker, person_key, role, role_category) DO UPDATE SET
            person_name = excluded.person_name,
            company_name = excluded.company_name,
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
        SELECT id, ticker, person_name, person_key, role, role_category, company_name, batch_id, extraction_method, created_at, updated_at
        FROM relationships_raw
        ORDER BY ticker, person_key, role_category, role
        """
    ).fetchall()
    with RELATIONSHIPS_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "ticker", "person_name", "person_key", "role", "role_category", "company_name", "batch_id", "extraction_method", "created_at", "updated_at"])
        writer.writerows([tuple(row) for row in rows])


def initialize_missing_companies_csv() -> None:
    MISSING_COMPANIES_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MISSING_COMPANIES_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "batch_id", "reason", "created_at"])


def append_missing_companies_csv(missing_tickers: list[str], batch_id: str) -> None:
    if not missing_tickers:
        return
    created_at = utc_now()
    with MISSING_COMPANIES_CSV_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows((ticker, batch_id, "missing_from_response", created_at) for ticker in missing_tickers)


def company_name_by_ticker(companies: list[tuple[str, str]]) -> dict[str, str]:
    return {ticker: company_name for ticker, company_name in companies}


class RetryCompaniesCsv:
    header = ["ticker", "company_name", "batch_id", "reason", "created_at"]

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows_by_ticker: dict[str, dict[str, str]] = {}

    def reset(self) -> None:
        self.rows_by_ticker.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()

    def add_companies(self, companies: list[tuple[str, str]], batch_id: str, reason: str) -> None:
        if not companies:
            return
        created_at = utc_now()
        for ticker, company_name in companies:
            normalized_ticker = ticker.strip().upper()
            self.rows_by_ticker[normalized_ticker] = {
                "ticker": normalized_ticker,
                "company_name": company_name,
                "batch_id": batch_id,
                "reason": reason,
                "created_at": created_at,
            }
        self._write()

    def _write(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.header)
            writer.writeheader()
            writer.writerows(self.rows_by_ticker.values())

    @property
    def count(self) -> int:
        return len(self.rows_by_ticker)


def write_debug(
    batch_id: str,
    raw_request: str,
    raw_response: str | None,
    error_message: str | None,
    response_json: str | None = None,
) -> None:
    DEBUG_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": batch_id,
        "raw_request": raw_request,
        "raw_response": raw_response,
        "response_json": response_json,
        "error_message": error_message,
    }
    (DEBUG_BATCH_DIR / f"{batch_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build raw company relationships with Grok API batches.")
    parser.add_argument("--universe", help="Filter companies by universe.")
    parser.add_argument("--ticker", help="Process one ticker.")
    parser.add_argument("--limit", type=int, help="Limit number of companies processed after offset.")
    parser.add_argument("--offset", type=int, default=0, help="Number of companies to skip before applying limit.")
    parser.add_argument("--batch-size", type=int, default=5, help="Companies per Grok request.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Grok model name.")
    parser.add_argument("--resume", action="store_true", help="Skip batches already marked success.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned batches without calling Grok.")
    parser.add_argument("--retry-file", type=Path, help="Load ticker and company_name rows from retry CSV instead of normal universe/offset/limit selection.")
    args = parser.parse_args()

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    if args.offset < 0:
        raise ValueError("--offset must be non-negative")

    with connect() as connection:
        create_tables(connection)
        if args.retry_file:
            companies = load_retry_companies_csv(args.retry_file)
            if args.offset != 0 or args.limit is not None:
                print("Retry-file mode bypasses --offset and --limit.")
        else:
            companies = fetch_companies(connection, args.universe, args.ticker)
            companies = companies[args.offset :]
            if args.limit is not None:
                companies = companies[: args.limit]
        company_batches = chunks(companies, args.batch_size)
        total_relationships = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_cost_usd = 0.0
        successful_batches = 0
        partial_batches = 0
        failed_batches = 0
        missing_ticker_count = 0
        failed_company_count = 0
        initialize_missing_companies_csv()
        retry_companies_csv = RetryCompaniesCsv(RETRY_COMPANIES_CSV_PATH)
        retry_companies_csv.reset()
        if args.retry_file:
            print(f"Planning {len(companies)} companies from retry file {args.retry_file} in {len(company_batches)} batches.")
        else:
            print(f"Planning {len(companies)} companies in {len(company_batches)} batch(es) with offset={args.offset}, limit={args.limit}.")
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
                result = extract_relationships_raw(batch, model=args.model)
                raw_response = result.raw_response
                if raw_response is None or raw_response.strip() == "":
                    metadata = result.metadata
                    response_json = result.response_json
                    cost_usd = calculate_cost_usd(metadata.input_tokens, metadata.output_tokens)
                    error_message = "Grok returned no final text"
                    upsert_batch(connection, batch_id, tickers, "failed", raw_request, raw_response, error_message, metadata, cost_usd)
                    connection.commit()
                    write_debug(batch_id, raw_request, raw_response, error_message, response_json)
                    failed_batches += 1
                    failed_company_count += len(tickers)
                    retry_companies_csv.add_companies(batch, batch_id, "no_final_text")
                    if metadata.input_tokens is not None:
                        total_input_tokens += metadata.input_tokens
                    if metadata.output_tokens is not None:
                        total_output_tokens += metadata.output_tokens
                    if metadata.total_tokens is not None:
                        total_tokens += metadata.total_tokens
                    if cost_usd is not None:
                        total_cost_usd += cost_usd
                    print(f"Batch {index}/{len(company_batches)} {batch_id} failed: {error_message}")
                    continue
                metadata = result.metadata
                response_json = result.response_json
                cost_usd = calculate_cost_usd(metadata.input_tokens, metadata.output_tokens)
                try:
                    rows, returned_tickers = parse_relationships(raw_response)
                except json.JSONDecodeError as error:
                    preview = raw_response[:500].replace("\n", "\\n")
                    error_message = f"invalid_json: {error}; raw_response_preview={preview!r}"
                    upsert_batch(connection, batch_id, tickers, "failed", raw_request, raw_response, error_message, metadata, cost_usd)
                    connection.commit()
                    write_debug(batch_id, raw_request, raw_response, error_message, response_json)
                    failed_batches += 1
                    failed_company_count += len(tickers)
                    retry_companies_csv.add_companies(batch, batch_id, "invalid_json")
                    if metadata.input_tokens is not None:
                        total_input_tokens += metadata.input_tokens
                    if metadata.output_tokens is not None:
                        total_output_tokens += metadata.output_tokens
                    if metadata.total_tokens is not None:
                        total_tokens += metadata.total_tokens
                    if cost_usd is not None:
                        total_cost_usd += cost_usd
                    print(f"Batch {index}/{len(company_batches)} failed: invalid_json")
                    continue
                except Exception as error:  # noqa: BLE001 - parser failures must be persisted per batch.
                    error_message = f"parse_exception: {error}"
                    upsert_batch(connection, batch_id, tickers, "failed", raw_request, raw_response, error_message, metadata, cost_usd)
                    connection.commit()
                    write_debug(batch_id, raw_request, raw_response, error_message, response_json)
                    failed_batches += 1
                    failed_company_count += len(tickers)
                    retry_companies_csv.add_companies(batch, batch_id, "parse_exception")
                    if metadata.input_tokens is not None:
                        total_input_tokens += metadata.input_tokens
                    if metadata.output_tokens is not None:
                        total_output_tokens += metadata.output_tokens
                    if metadata.total_tokens is not None:
                        total_tokens += metadata.total_tokens
                    if cost_usd is not None:
                        total_cost_usd += cost_usd
                    print(f"Batch {index}/{len(company_batches)} failed: parse_exception")
                    continue
                missing_tickers = sorted(set(tickers) - returned_tickers)
                status = "partial" if missing_tickers else "success"
                error_message = f"Missing tickers: {', '.join(missing_tickers)}" if missing_tickers else None
                rows = add_company_names(rows, batch)
                insert_relationships(connection, rows, batch_id)
                upsert_batch(connection, batch_id, tickers, status, raw_request, raw_response, error_message, metadata, cost_usd)
                connection.commit()
                export_relationships_csv(connection)
                write_debug(batch_id, raw_request, raw_response, None, response_json)
                append_missing_companies_csv(missing_tickers, batch_id)
                ticker_to_company_name = company_name_by_ticker(batch)
                retry_companies_csv.add_companies(
                    [(ticker, ticker_to_company_name[ticker]) for ticker in missing_tickers],
                    batch_id,
                    "missing_from_response",
                )
                if status == "partial":
                    partial_batches += 1
                    missing_ticker_count += len(missing_tickers)
                else:
                    successful_batches += 1
                total_relationships += len(rows)
                if metadata.input_tokens is not None:
                    total_input_tokens += metadata.input_tokens
                if metadata.output_tokens is not None:
                    total_output_tokens += metadata.output_tokens
                if metadata.total_tokens is not None:
                    total_tokens += metadata.total_tokens
                if cost_usd is not None:
                    total_cost_usd += cost_usd
                print(
                    f"Batch {index}/{len(company_batches)} {batch_id}: {', '.join(tickers)}\n"
                    f"status={status}, relationships={len(rows)}, missing_tickers={len(missing_tickers)}, "
                    f"input_tokens={format_metric(metadata.input_tokens)}, "
                    f"output_tokens={format_metric(metadata.output_tokens)}, "
                    f"total_tokens={format_metric(metadata.total_tokens)}, cost_usd={format_metric(cost_usd)}"
                )
            except Exception as error:  # noqa: BLE001 - failure details must be persisted per batch.
                message = traceback.format_exc()
                upsert_batch(connection, batch_id, tickers, "failed", raw_request, None, message)
                connection.commit()
                write_debug(batch_id, raw_request, None, message)
                failed_batches += 1
                failed_company_count += len(tickers)
                retry_companies_csv.add_companies(batch, batch_id, "api_exception")
                print(f"Batch {index}/{len(company_batches)} failed: {error}")
                print(message, file=sys.stderr, end="")
        print("Final run summary:")
        print(f"Successful batches: {successful_batches}")
        print(f"Partial batches: {partial_batches}")
        print(f"Failed batches: {failed_batches}")
        print(f"Failed company count: {failed_company_count}")
        print(f"Missing ticker count: {missing_ticker_count}")
        print(f"Retry ticker count: {retry_companies_csv.count}")
        print(f"Retry companies file: {RETRY_COMPANIES_CSV_PATH.relative_to(ROOT_DIR)}")
        print(f"Total relationships: {total_relationships}")
        print(f"Total input tokens: {total_input_tokens}")
        print(f"Total output tokens: {total_output_tokens}")
        print(f"Total tokens: {total_tokens}")
        print(f"Total estimated cost USD: {total_cost_usd:.6f}")


if __name__ == "__main__":
    main()
