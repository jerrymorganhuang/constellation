#!/usr/bin/env python3
"""Build the Constellation Company Master table and CSV export.

The module uses rule-based public sources for universe membership and yfinance for
company metadata. It is intentionally narrow: one SQLite table and one CSV export.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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
AUDIT_PATH = DATA_DIR / "universe_audit.json"
UNIVERSE_DIR = DATA_DIR / "universes"
SOURCES_PATH = UNIVERSE_DIR / "sources.yml"

EXPECTED_COLUMNS = [
    "ticker",
    "company_name",
    "universe",
    "sector",
    "industry",
    "description",
    "description_short",
    "updated_at",
]

SUPPORTED_UNIVERSES = ["SPX", "NDX", "SOXX", "RUSSELL1000", "RUSSELL2000"]

USER_AGENT = "ConstellationCompanyMaster/1.0 (+https://github.com/)"

SOURCE_METADATA = {
    "SPX": {
        "provider": "Wikipedia",
        "method": "html_table",
        "source_url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "expected_count": 500,
    },
    "NDX": {
        "provider": "Nasdaq",
        "method": "public_constituents",
        "source_url": "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
        "expected_count": 100,
    },
    "SOXX": {
        "provider": "BlackRock/iShares",
        "method": "holdings_csv",
        "source_url": "https://www.ishares.com/us/products/239705/ishares-soxx-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund",
        "expected_count": 30,
    },
    "RUSSELL1000": {
        "provider": "BlackRock/iShares",
        "method": "holdings_csv",
        "source_url": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
        "expected_count": 1000,
    },
    "RUSSELL2000": {
        "provider": "BlackRock/iShares",
        "method": "holdings_csv",
        "source_url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
        "expected_count": 2000,
    },
}


class UniverseStatus(str, Enum):
    FETCHED = "fetched"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class UniverseSource:
    name: str
    fetcher: Callable[[], set[str]]


@dataclass(frozen=True)
class UniverseFetchSummary:
    name: str
    count: int
    status: UniverseStatus
    fetched_at: str
    error: str | None = None

    @property
    def provider(self) -> str:
        return str(SOURCE_METADATA[self.name]["provider"])

    @property
    def method(self) -> str:
        return str(SOURCE_METADATA[self.name]["method"])

    @property
    def source_url(self) -> str:
        return str(SOURCE_METADATA[self.name]["source_url"])


def normalize_ticker(value: object) -> str:
    """Normalize tickers consistently for storage and joins."""
    if value is None:
        return ""
    ticker = str(value).strip().upper()
    ticker = re.sub(r"\s+", "", ticker)
    ticker = ticker.replace(".", "-")
    if ticker in {"-", "—", "N/A", "NA", "CASH", "USD"}:
        return ""
    return ticker


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def _read_html_tables(url: str) -> list[pd.DataFrame]:
    with urlopen(_request(url), timeout=60) as response:
        html = response.read()
    return pd.read_html(io.BytesIO(html))


def _tickers_from_wikipedia_table(url: str, required_column: str) -> set[str]:
    for table in _read_html_tables(url):
        if required_column in table.columns:
            return {ticker for ticker in (normalize_ticker(v) for v in table[required_column].tolist()) if ticker}
    raise RuntimeError(f"Could not find column {required_column!r} at {url}")


def fetch_spx() -> set[str]:
    return _tickers_from_wikipedia_table(str(SOURCE_METADATA["SPX"]["source_url"]), "Symbol")


def fetch_ndx() -> set[str]:
    with urlopen(_request(str(SOURCE_METADATA["NDX"]["source_url"])), timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("data", {}).get("data", {}).get("rows", [])
    tickers = {normalize_ticker(row.get("symbol")) for row in rows if isinstance(row, dict)}
    tickers.discard("")
    if not tickers:
        raise RuntimeError("Nasdaq-100 response did not include constituent symbols")
    return tickers


def _find_csv_header_index(lines: list[str], required_column: str) -> int | None:
    """Find a CSV header even when a provider adds notes or reorders columns."""
    for index, line in enumerate(lines):
        try:
            columns = next(csv.reader([line]))
        except csv.Error:
            continue
        normalized_columns = {column.strip().lower() for column in columns}
        if required_column.lower() in normalized_columns:
            return index
    return None


def _is_equity_holding(row: dict[str, str]) -> bool:
    searchable_fields = [
        row.get("Asset Class"),
        row.get("Market Value"),
        row.get("Name"),
        row.get("Sector"),
        row.get("Location"),
        row.get("Exchange"),
    ]
    asset_class = (row.get("Asset Class") or "").strip().lower()
    if asset_class and asset_class not in {"equity", "stock"}:
        return False
    text = " ".join(str(value or "") for value in searchable_fields).lower()
    excluded_terms = (
        "cash",
        "money market",
        "treasury",
        "collateral",
        "future",
        "futures",
        "swap",
        "option",
        "derivative",
        "receivable",
        "payable",
    )
    return not any(term in text for term in excluded_terms)


def _fetch_ishares_holdings(universe_name: str) -> set[str]:
    url = str(SOURCE_METADATA[universe_name]["source_url"])
    with urlopen(_request(url), timeout=90) as response:
        text = response.read().decode("utf-8-sig", errors="replace")

    lines = text.splitlines()
    header_idx = _find_csv_header_index(lines, "Ticker")
    if header_idx is None:
        raise RuntimeError(f"Could not find holdings header for {universe_name}")
    reader = csv.DictReader(lines[header_idx:])
    tickers: set[str] = set()
    for row in reader:
        ticker = normalize_ticker(row.get("Ticker"))
        if ticker and _is_equity_holding(row):
            tickers.add(ticker)
    if not tickers:
        raise RuntimeError(f"No equity tickers parsed for {universe_name}")
    return tickers


def fetch_soxx() -> set[str]:
    return _fetch_ishares_holdings("SOXX")


def fetch_russell1000() -> set[str]:
    return _fetch_ishares_holdings("RUSSELL1000")


def fetch_russell2000() -> set[str]:
    return _fetch_ishares_holdings("RUSSELL2000")


def fetch_universe_membership() -> tuple[dict[str, set[str]], list[UniverseFetchSummary]]:
    sources = [
        UniverseSource("SPX", fetch_spx),
        UniverseSource("NDX", fetch_ndx),
        UniverseSource("SOXX", fetch_soxx),
        UniverseSource("RUSSELL1000", fetch_russell1000),
        UniverseSource("RUSSELL2000", fetch_russell2000),
    ]
    membership: dict[str, set[str]] = defaultdict(set)
    summaries: list[UniverseFetchSummary] = []
    for source in sources:
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            tickers = source.fetcher()
            if not tickers:
                raise RuntimeError(f"No tickers fetched for {source.name}")
            status = UniverseStatus.FETCHED
            error = None
        except Exception as exc:  # noqa: BLE001 - providers can change format or block requests.
            tickers = set()
            status = UniverseStatus.SKIPPED
            error = str(exc)
            print(f"Warning: skipped {source.name}: {error}")
        for ticker in tickers:
            membership[ticker].add(source.name)
        summaries.append(UniverseFetchSummary(source.name, len(tickers), status, fetched_at, error))
        meta = SOURCE_METADATA[source.name]
        print(
            f"{status.value.title()} {len(tickers):,} {source.name} tickers "
            f"({meta['provider']} / {meta['method']})"
        )
    if not membership:
        write_universe_audit(summaries)
        raise RuntimeError("No universe membership could be fetched")
    return dict(membership), summaries


def fetch_metadata(tickers: list[str]) -> dict[str, dict[str, str | None]]:
    metadata: dict[str, dict[str, str | None]] = {}
    if yf is None:
        print("Warning: yfinance is not installed; metadata fields will be blank")
        return {ticker: {"company_name": None, "sector": None, "industry": None, "description": None} for ticker in tickers}
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


def derive_description_short(description: str | None) -> str | None:
    if not description:
        return None
    text = re.sub(r"\s+", " ", description).strip()
    if len(text) <= 300:
        return text
    target_min = 200
    target_max = 300
    sentence_endings = [match.end() for match in re.finditer(r"[.!?](?:\s|$)", text)]
    candidates = [end for end in sentence_endings if target_min <= end <= target_max]
    if candidates:
        return text[: candidates[-1]].strip()
    earlier = [end for end in sentence_endings if end < target_min]
    if earlier and earlier[-1] >= 120:
        return text[: earlier[-1]].strip()
    truncated = text[:target_max].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return f"{truncated.rstrip('.,;:')}..."


def build_rows(membership: dict[str, set[str]], metadata: dict[str, dict[str, str | None]]) -> list[dict[str, str | None]]:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, str | None]] = []
    for ticker in sorted(membership):
        meta = metadata.get(ticker, {})
        description = meta.get("description")
        rows.append(
            {
                "ticker": ticker,
                "company_name": meta.get("company_name"),
                "universe": ";".join(sorted(membership[ticker])),
                "sector": meta.get("sector"),
                "industry": meta.get("industry"),
                "description": description,
                "description_short": derive_description_short(description),
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
                description_short TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO companies (
                ticker, company_name, universe, sector, industry, description,
                description_short, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row[column] for column in EXPECTED_COLUMNS) for row in rows],
        )


def write_csv(rows: list[dict[str, str | None]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_universe_audit(summaries: list[UniverseFetchSummary]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    audit_rows = [
        {
            "universe": summary.name,
            "provider": summary.provider,
            "method": summary.method,
            "source_url": summary.source_url,
            "status": summary.status.value,
            "count": summary.count,
            "fetched_at": summary.fetched_at,
            "error": summary.error,
        }
        for summary in summaries
    ]
    AUDIT_PATH.write_text(json.dumps(audit_rows, indent=2) + "\n", encoding="utf-8")


def validate_sources_file() -> None:
    if not SOURCES_PATH.exists():
        raise RuntimeError(f"Universe sources metadata does not exist: {SOURCES_PATH}")
    text = SOURCES_PATH.read_text(encoding="utf-8")
    missing_universes = [universe for universe in SUPPORTED_UNIVERSES if not re.search(rf"^{universe}:", text, re.MULTILINE)]
    if missing_universes:
        raise RuntimeError(f"sources.yml missing supported universes: {missing_universes}")
    for csv_path in UNIVERSE_DIR.glob("*.csv"):
        relative = csv_path.relative_to(ROOT_DIR)
        if str(relative) not in text:
            raise RuntimeError(f"Checked-in universe CSV lacks documented provenance: {relative}")


def validate_outputs() -> None:
    validate_sources_file()
    if not DB_PATH.exists():
        raise RuntimeError(f"SQLite database does not exist: {DB_PATH}")
    with sqlite3.connect(DB_PATH) as conn:
        table_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'companies'").fetchone()
        if not table_exists:
            raise RuntimeError("companies table does not exist")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(companies)")]
        if columns != EXPECTED_COLUMNS:
            raise RuntimeError(f"companies table columns do not match expected schema: {columns}")
        empty_tickers = conn.execute("SELECT COUNT(*) FROM companies WHERE ticker IS NULL OR trim(ticker) = ''").fetchone()[0]
        if empty_tickers:
            raise RuntimeError("companies table contains empty ticker values")
        duplicate_tickers = conn.execute("SELECT COUNT(*) FROM (SELECT ticker FROM companies GROUP BY ticker HAVING COUNT(*) > 1)").fetchone()[0]
        if duplicate_tickers:
            raise RuntimeError("companies table contains duplicate ticker values")
        empty_universe = conn.execute("SELECT COUNT(*) FROM companies WHERE universe IS NULL OR trim(universe) = ''").fetchone()[0]
        if empty_universe:
            raise RuntimeError("companies table contains empty universe values")

    if not CSV_PATH.exists():
        raise RuntimeError(f"CSV export does not exist: {CSV_PATH}")
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise RuntimeError(f"CSV columns do not match expected schema: {reader.fieldnames}")
        tickers = [row["ticker"] for row in reader]
    if len(tickers) != len(set(tickers)):
        raise RuntimeError("CSV export contains duplicate tickers")
    if tickers != sorted(tickers):
        raise RuntimeError("CSV export is not sorted by ticker")
    if not AUDIT_PATH.exists():
        raise RuntimeError(f"Universe audit export does not exist: {AUDIT_PATH}")


def main() -> None:
    print("1. Fetch universe membership.")
    membership, universe_summaries = fetch_universe_membership()
    write_universe_audit(universe_summaries)
    tickers = sorted(membership)

    print("2. Fetch company metadata.")
    metadata = fetch_metadata(tickers)
    rows = build_rows(membership, metadata)

    print("3. Build/update data/constellation.db.")
    write_sqlite(rows)

    print("4. Export data/companies.csv.")
    write_csv(rows)

    validate_outputs()
    missing_metadata_count = sum(1 for row in rows if (not row["company_name"] or not row["sector"] or not row["industry"] or not row["description"]))
    print("5. Summary")
    print(f"total company count: {len(rows):,}")
    print("universe counts:")
    for summary in universe_summaries:
        status_note = summary.status.value
        if summary.error:
            status_note = f"{status_note}; {summary.error}"
        print(f"  {summary.name}: {summary.count:,} ({summary.provider} / {summary.method}; {status_note})")
    print(f"missing metadata count: {missing_metadata_count:,}")
    print(f"SQLite path: {DB_PATH}")
    print(f"CSV path: {CSV_PATH}")
    print(f"Audit path: {AUDIT_PATH}")


if __name__ == "__main__":
    main()
