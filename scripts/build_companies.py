#!/usr/bin/env python3
"""Build the Constellation Company Master table and CSV export.

The module uses rule-based public sources for universe membership and yfinance for
company metadata. It is intentionally narrow: one SQLite table and one CSV export.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import pandas as pd
try:
    import yfinance as yf
except ImportError:  # pragma: no cover - supports validation in minimal environments.
    yf = None

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "constellation.db"
CSV_PATH = DATA_DIR / "companies.csv"

EXPECTED_COLUMNS = [
    "ticker",
    "company_name",
    "universe",
    "sector",
    "industry",
    "description",
    "updated_at",
]

USER_AGENT = "ConstellationCompanyMaster/1.0 (+https://github.com/)"


@dataclass(frozen=True)
class UniverseSource:
    name: str
    fetcher: Callable[[], set[str]]


def normalize_ticker(value: object) -> str:
    """Normalize tickers consistently for storage and joins."""
    if value is None:
        return ""
    ticker = str(value).strip().upper()
    ticker = re.sub(r"\s+", "", ticker)
    # yfinance and many CSV providers use '-' for share classes; normalize to '.'.
    ticker = ticker.replace("-", ".")
    return ticker


def _read_html_tables(url: str) -> list[pd.DataFrame]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        html = response.read()
    return pd.read_html(io.BytesIO(html))


def _tickers_from_wikipedia_table(url: str, required_column: str) -> set[str]:
    for table in _read_html_tables(url):
        if required_column in table.columns:
            return {
                ticker
                for ticker in (normalize_ticker(v) for v in table[required_column].tolist())
                if ticker
            }
    raise RuntimeError(f"Could not find column {required_column!r} at {url}")


def fetch_spx() -> set[str]:
    return _tickers_from_wikipedia_table(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"
    )


def fetch_ndx() -> set[str]:
    return _tickers_from_wikipedia_table(
        "https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker"
    )


def _fetch_ishares_holdings(etf_ticker: str) -> set[str]:
    url = f"https://www.ishares.com/us/products/{_ISHARES_PRODUCT_IDS[etf_ticker]}/ishares-{etf_ticker.lower()}-etf/1467271812596.ajax?fileType=csv&fileName={etf_ticker}_holdings&dataType=fund"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=90) as response:
        text = response.read().decode("utf-8-sig", errors="replace")

    lines = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if line.startswith("Ticker,")), None
    )
    if header_idx is None:
        raise RuntimeError(f"Could not find holdings header for {etf_ticker}")
    reader = csv.DictReader(lines[header_idx:])
    tickers: set[str] = set()
    for row in reader:
        ticker = normalize_ticker(row.get("Ticker"))
        asset_class = (row.get("Asset Class") or "").strip().lower()
        if ticker and ticker != "-" and asset_class in {"equity", "stock"}:
            tickers.add(ticker)
    return tickers


_ISHARES_PRODUCT_IDS = {
    # SOXX: iShares Semiconductor ETF; IWB: Russell 1000 ETF; IWM: Russell 2000 ETF.
    "SOXX": "239705",
    "IWB": "239707",
    "IWM": "239710",
}


def fetch_soxx() -> set[str]:
    return _fetch_ishares_holdings("SOXX")


def fetch_russell1000() -> set[str]:
    return _fetch_ishares_holdings("IWB")


def fetch_russell2000() -> set[str]:
    return _fetch_ishares_holdings("IWM")


def fetch_universe_membership() -> dict[str, set[str]]:
    sources = [
        UniverseSource("SPX", fetch_spx),
        UniverseSource("NDX", fetch_ndx),
        UniverseSource("SOXX", fetch_soxx),
        UniverseSource("RUSSELL1000", fetch_russell1000),
        UniverseSource("RUSSELL2000", fetch_russell2000),
    ]
    membership: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        tickers = source.fetcher()
        if not tickers:
            raise RuntimeError(f"No tickers fetched for {source.name}")
        for ticker in tickers:
            membership[ticker].add(source.name)
        print(f"Fetched {len(tickers):,} {source.name} tickers")
    return dict(membership)


def fetch_metadata(tickers: list[str]) -> dict[str, dict[str, str | None]]:
    metadata: dict[str, dict[str, str | None]] = {}
    if yf is None:
        print("Warning: yfinance is not installed; metadata fields will be blank")
        return {
            ticker: {
                "company_name": None,
                "sector": None,
                "industry": None,
                "description": None,
            }
            for ticker in tickers
        }
    for index, ticker in enumerate(tickers, start=1):
        if index == 1 or index % 100 == 0 or index == len(tickers):
            print(f"Fetching company metadata {index:,}/{len(tickers):,}")
        try:
            info = yf.Ticker(ticker).get_info()
        except Exception as exc:  # noqa: BLE001 - keep rerunnable even when one symbol fails.
            print(f"Warning: yfinance metadata failed for {ticker}: {exc}")
            info = {}
        metadata[ticker] = {
            "company_name": info.get("longName") or info.get("shortName") or None,
            "sector": info.get("sector") or None,
            "industry": info.get("industry") or None,
            "description": info.get("longBusinessSummary") or None,
        }
    return metadata


def build_rows(
    membership: dict[str, set[str]], metadata: dict[str, dict[str, str | None]]
) -> list[dict[str, str | None]]:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, str | None]] = []
    for ticker in sorted(membership):
        meta = metadata.get(ticker, {})
        rows.append(
            {
                "ticker": ticker,
                "company_name": meta.get("company_name"),
                "universe": ";".join(sorted(membership[ticker])),
                "sector": meta.get("sector"),
                "industry": meta.get("industry"),
                "description": meta.get("description"),
                "updated_at": updated_at,
            }
        )
    return rows


def write_sqlite(rows: list[dict[str, str | None]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DROP TABLE IF EXISTS companies")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                ticker TEXT PRIMARY KEY,
                company_name TEXT,
                universe TEXT NOT NULL,
                sector TEXT,
                industry TEXT,
                description TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO companies (
                ticker, company_name, universe, sector, industry, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row[column] for column in EXPECTED_COLUMNS) for row in rows],
        )


def write_csv(rows: list[dict[str, str | None]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def validate_outputs() -> None:
    if not DB_PATH.exists():
        raise RuntimeError(f"SQLite database does not exist: {DB_PATH}")
    with sqlite3.connect(DB_PATH) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'companies'"
        ).fetchone()
        if not table_exists:
            raise RuntimeError("companies table does not exist")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(companies)")]
        missing = [column for column in EXPECTED_COLUMNS if column not in columns]
        if missing:
            raise RuntimeError(f"companies table missing columns: {missing}")
        empty_tickers = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE ticker IS NULL OR trim(ticker) = ''"
        ).fetchone()[0]
        if empty_tickers:
            raise RuntimeError("companies table contains empty ticker values")
        duplicate_tickers = conn.execute(
            "SELECT COUNT(*) FROM (SELECT ticker FROM companies GROUP BY ticker HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if duplicate_tickers:
            raise RuntimeError("companies table contains duplicate ticker values")
        empty_universe = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE universe IS NULL OR trim(universe) = ''"
        ).fetchone()[0]
        if empty_universe:
            raise RuntimeError("companies table contains empty universe values")

    if not CSV_PATH.exists():
        raise RuntimeError(f"CSV export does not exist: {CSV_PATH}")
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise RuntimeError(f"CSV columns do not match expected schema: {reader.fieldnames}")
        tickers = [row["ticker"] for row in reader]
    if tickers != sorted(tickers):
        raise RuntimeError("CSV export is not sorted by ticker")


def main() -> None:
    print("1. Fetch universe membership.")
    membership = fetch_universe_membership()
    tickers = sorted(membership)

    print("2. Fetch company metadata.")
    metadata = fetch_metadata(tickers)
    rows = build_rows(membership, metadata)

    print("3. Build/update data/constellation.db.")
    write_sqlite(rows)

    print("4. Export data/companies.csv.")
    write_csv(rows)

    validate_outputs()
    missing_metadata_count = sum(
        1
        for row in rows
        if not row["company_name"] or not row["sector"] or not row["industry"] or not row["description"]
    )
    universe_counts = {
        universe: sum(1 for memberships in membership.values() if universe in memberships)
        for universe in ["SPX", "NDX", "SOXX", "RUSSELL1000", "RUSSELL2000"]
    }

    print("5. Summary")
    print(f"total company count: {len(rows):,}")
    print("universe counts:")
    for universe, count in universe_counts.items():
        print(f"  {universe}: {count:,}")
    print(f"missing metadata count: {missing_metadata_count:,}")
    print(f"SQLite path: {DB_PATH}")
    print(f"CSV path: {CSV_PATH}")


if __name__ == "__main__":
    main()
