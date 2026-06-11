#!/usr/bin/env python3
"""Build Constellation V0 SEC-to-graph CSVs for SOXX constituents.

This standalone pipeline:
- fetches the current SOXX holdings universe,
- maps each ticker to its SEC CIK,
- finds the latest DEF 14A proxy statement,
- downloads and caches the filing HTML,
- extracts CEO, CFO, and board-member relationships with deterministic rules, and
- writes graph-ready CSV files compatible with a future Neo4j/Cytoscape app.

Run:
    python build_constellation_soxx.py

Optional environment variable:
    CONSTELLATION_SEC_USER_AGENT="ConstellationV0 research@example.com"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = Path("data/constellation_v0")
SOXX_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239705/"
    "ishares-phlx-semiconductor-etf/1467271812596.ajax"
    "?fileType=csv&fileName=SOXX_holdings&dataType=fund"
)
SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"
DEFAULT_USER_AGENT = "ConstellationV0 research@example.com"
SEC_SLEEP_SECONDS = 0.13
SEC_USER_AGENT_ENV = "CONSTELLATION_SEC_USER_AGENT"

# Static safety net used only if the iShares holdings endpoint is unavailable.
# The live iShares CSV remains the default source of truth for the SOXX universe.
SOXX_FALLBACK_TICKERS = [
    "AMD",
    "AMAT",
    "AMKR",
    "ARM",
    "ASML",
    "AVGO",
    "COHR",
    "ENTG",
    "GFS",
    "INTC",
    "KLAC",
    "LRCX",
    "LSCC",
    "MCHP",
    "MPWR",
    "MRVL",
    "MU",
    "NVDA",
    "NXPI",
    "ON",
    "QCOM",
    "QRVO",
    "RMBS",
    "SMCI",
    "SWKS",
    "TER",
    "TSM",
    "TXN",
    "UMC",
    "WOLF",
]

HONORIFIC_PREFIXES = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "prof",
    "sir",
    "dame",
}
HONORIFIC_SUFFIXES = {
    "jr",
    "sr",
    "ii",
    "iii",
    "iv",
    "phd",
    "ph.d",
    "md",
    "m.d",
    "esq",
}
NAME_STOPWORDS = {
    "board of directors",
    "chief executive officer",
    "chief financial officer",
    "executive officer",
    "proxy statement",
    "annual meeting",
    "table of contents",
    "corporate governance",
    "audit committee",
    "compensation committee",
    "nominating committee",
    "stock ownership",
    "united states",
    "new york",
    "san jose",
    "silicon valley",
    "our board",
    "the board",
    "class i",
    "class ii",
    "class iii",
}
NAME_RE = re.compile(
    r"\b([A-Z][a-zA-Z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'’.-]+)){1,4})\b"
)
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: str
    company_name: str


@dataclass(frozen=True)
class Filing:
    filing_date: str
    accession: str
    primary_document: str
    filing_url: str


@dataclass(frozen=True)
class PersonRelationship:
    name: str
    relationship_type: str


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path):
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host": "www.sec.gov",
            }
        )
        self.data_session = requests.Session()
        self.data_session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host": "data.sec.gov",
            }
        )

    def get_json(self, url: str) -> dict:
        time.sleep(SEC_SLEEP_SECONDS)
        session = self.data_session if "data.sec.gov" in url else self.session
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def download_text_cached(self, url: str, cache_path: Path) -> str:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        time.sleep(SEC_SLEEP_SECONDS)
        response = self.session.get(url, timeout=45)
        response.raise_for_status()
        text = response.text
        cache_path.write_text(text, encoding="utf-8")
        return text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Constellation V0 SOXX SEC graph CSVs.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Output directory for CSVs and cache.")
    parser.add_argument("--tickers", help="Comma-separated ticker override for targeted runs.")
    parser.add_argument("--limit", type=int, help="Limit the number of companies processed.")
    parser.add_argument(
        "--user-agent",
        default=os.environ.get(SEC_USER_AGENT_ENV, DEFAULT_USER_AGENT),
        help="SEC-compliant User-Agent. Prefer setting CONSTELLATION_SEC_USER_AGENT.",
    )
    return parser.parse_args()


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def parse_soxx_holdings_csv(csv_text: str) -> list[str]:
    lines = csv_text.splitlines()
    header_index = next(
        i for i, line in enumerate(lines) if line.lower().startswith('"ticker",') or line.lower().startswith("ticker,")
    )
    rows = list(csv.DictReader(lines[header_index:]))
    tickers: list[str] = []
    for row in rows:
        ticker = normalize_ticker(row.get("Ticker", ""))
        asset_class = (row.get("Asset Class", "") or "").lower()
        name = (row.get("Name", "") or "").lower()
        if not ticker or ticker == "-":
            continue
        if asset_class and "equity" not in asset_class:
            continue
        if "cash" in name or "collateral" in name:
            continue
        tickers.append(ticker)
    return sorted(set(tickers))


def fetch_soxx_tickers(user_agent: str, cache_dir: Path) -> tuple[list[str], str]:
    """Fetch SOXX holdings from iShares, falling back to local cache when available."""
    cache_path = cache_dir / "soxx_holdings.csv"
    try:
        response = requests.get(
            SOXX_HOLDINGS_URL,
            headers={"User-Agent": user_agent},
            timeout=30,
        )
        response.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(response.text, encoding="utf-8")
        return parse_soxx_holdings_csv(response.text), "ishares_soxx_holdings_csv"
    except Exception as exc:
        if cache_path.exists():
            print(f"Warning: failed to fetch live SOXX holdings ({exc}); using cached holdings.", file=sys.stderr)
            return parse_soxx_holdings_csv(cache_path.read_text(encoding="utf-8", errors="replace")), "cached_ishares_soxx_holdings_csv"
        raise


def get_universe(ticker_override: str | None, limit: int | None, user_agent: str, cache_dir: Path) -> tuple[list[str], str]:
    if ticker_override:
        tickers = [normalize_ticker(t) for t in ticker_override.split(",") if t.strip()]
        source = "cli_override"
    else:
        try:
            tickers, source = fetch_soxx_tickers(user_agent, cache_dir)
        except Exception as exc:
            print(f"Warning: failed to fetch live SOXX holdings ({exc}); using embedded fallback.", file=sys.stderr)
            tickers = sorted(SOXX_FALLBACK_TICKERS)
            source = "embedded_fallback"
    if limit is not None:
        tickers = tickers[:limit]
    return tickers, source


def fetch_sec_ticker_map(client: SecClient, cache_dir: Path) -> dict[str, Company]:
    cache_path = cache_dir / "company_tickers.json"
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        data = client.get_json(SEC_TICKER_URL)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mapping: dict[str, Company] = {}
    for item in data.values():
        ticker = normalize_ticker(str(item["ticker"]))
        cik = str(item["cik_str"])
        mapping[ticker] = Company(ticker=ticker, cik=cik, company_name=item["title"])
    return mapping


def flatten_recent_filings(submissions: dict) -> list[dict[str, str]]:
    recent = submissions.get("filings", {}).get("recent", {})
    rows: list[dict[str, str]] = []
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        rows.append(
            {
                "form": form,
                "filingDate": recent.get("filingDate", [])[i],
                "accessionNumber": recent.get("accessionNumber", [])[i],
                "primaryDocument": recent.get("primaryDocument", [])[i],
            }
        )
    return rows


def iter_all_submission_rows(client: SecClient, cik: str) -> Iterable[dict[str, str]]:
    cik10 = cik.zfill(10)
    submissions = client.get_json(SEC_SUBMISSIONS_URL.format(cik10=cik10))
    yield from flatten_recent_filings(submissions)
    for older_file in submissions.get("filings", {}).get("files", []):
        name = older_file.get("name")
        if not name:
            continue
        older_url = f"https://data.sec.gov/submissions/{name}"
        older = client.get_json(older_url)
        yield from flatten_recent_filings({"filings": {"recent": older}})


def is_def14a_form(form: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", form.upper())
    return normalized == "DEF14A" or normalized.startswith("DEF14AA")


def find_latest_def14a(client: SecClient, company: Company) -> Filing | None:
    candidates: list[dict[str, str]] = []
    for row in iter_all_submission_rows(client, company.cik):
        if is_def14a_form(row.get("form", "")):
            candidates.append(row)
    if not candidates:
        return None
    latest = max(candidates, key=lambda row: row.get("filingDate", ""))
    accession = latest["accessionNumber"]
    accession_nodash = accession.replace("-", "")
    primary_document = latest["primaryDocument"]
    filing_url = f"{SEC_ARCHIVES_BASE}{int(company.cik)}/{accession_nodash}/{primary_document}"
    return Filing(
        filing_date=latest["filingDate"],
        accession=accession,
        primary_document=primary_document,
        filing_url=filing_url,
    )


def clean_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def normalize_name(name: str) -> str:
    cleaned = clean_text(name)
    cleaned = re.sub(r"^[•*\-–—\d.\s]+", "", cleaned)
    cleaned = re.sub(r"\s*,?\s*(?:Age|Director|Class|Independent).*$", "", cleaned, flags=re.I)
    tokens = [t.strip(" ,.;:()[]") for t in cleaned.split()]
    while tokens and tokens[0].strip(".").lower() in HONORIFIC_PREFIXES:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".,").lower() in HONORIFIC_SUFFIXES:
        tokens.pop()
    cleaned = " ".join(tokens)
    cleaned = re.sub(r"[^A-Za-z'’ .-]", "", cleaned)
    return clean_text(cleaned)


def canonical_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_name(name).lower())


def is_plausible_person_name(name: str) -> bool:
    normalized = normalize_name(name)
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered in NAME_STOPWORDS:
        return False
    parts = normalized.split()
    if len(parts) < 2 or len(parts) > 5:
        return False
    if any(part.lower() in {"and", "or", "the", "for", "with", "from"} for part in parts):
        return False
    if not all(re.match(r"^[A-Z][A-Za-z'’.-]*$|^[A-Z]\.$", part) for part in parts):
        return False
    return True


def extract_name_before_title(text: str, title_pattern: str) -> list[str]:
    names: list[str] = []
    pattern = re.compile(
        rf"([A-Z][A-Za-z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’.-]+)){{1,4}})"
        rf"\s*(?:,|–|-|—|\(|\sis\s|\sserves\sas\s|\swas\s)?\s*"
        rf"(?:our\s+|the\s+|as\s+)?{title_pattern}",
        re.I,
    )
    for match in pattern.finditer(text):
        candidate = normalize_name(match.group(1))
        if is_plausible_person_name(candidate):
            names.append(candidate)
    return names


def extract_name_after_title(text: str, title_pattern: str) -> list[str]:
    names: list[str] = []
    pattern = re.compile(
        rf"{title_pattern}\s*(?:is|was|:|,|-|–|—)?\s*"
        rf"([A-Z][A-Za-z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’.-]+)){{1,4}})",
        re.I,
    )
    for match in pattern.finditer(text):
        candidate = normalize_name(match.group(1))
        if is_plausible_person_name(candidate):
            names.append(candidate)
    return names


def most_common_name(names: list[str]) -> str | None:
    if not names:
        return None
    counts = Counter(canonical_name(name) for name in names)
    winner = counts.most_common(1)[0][0]
    for name in names:
        if canonical_name(name) == winner:
            return name
    return None


def extract_officers(text: str) -> list[PersonRelationship]:
    relationships: list[PersonRelationship] = []
    ceo_title = r"(?:President\s+and\s+)?Chief\s+Executive\s+Officer|CEO"
    cfo_title = r"Chief\s+Financial\s+Officer|CFO"
    ceo = most_common_name(extract_name_before_title(text, ceo_title) + extract_name_after_title(text, ceo_title))
    cfo = most_common_name(extract_name_before_title(text, cfo_title) + extract_name_after_title(text, cfo_title))
    if ceo:
        relationships.append(PersonRelationship(ceo, "CEO_OF"))
    if cfo and canonical_name(cfo) != canonical_name(ceo or ""):
        relationships.append(PersonRelationship(cfo, "CFO_OF"))
    return relationships


def table_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [clean_text(cell.get_text(" ")) for cell in tr.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def extract_board_from_tables(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []
    for table in soup.find_all("table"):
        table_text = clean_text(table.get_text(" "))
        lower = table_text.lower()
        if "director" not in lower:
            continue
        if not any(token in lower for token in ("nominee", "board", "election", "committee", "age")):
            continue
        rows = table_rows(table)
        if len(rows) < 2:
            continue
        header = [cell.lower() for cell in rows[0]]
        candidate_index = 0
        for idx, cell in enumerate(header):
            if "name" in cell or "nominee" in cell or "director" == cell.strip():
                candidate_index = idx
                break
        for row in rows[1:]:
            if candidate_index >= len(row):
                continue
            candidate = normalize_name(row[candidate_index])
            # Some filings use a first column with footnote marks and a second column for names.
            if not is_plausible_person_name(candidate) and len(row) > 1:
                candidate = normalize_name(row[1])
            if is_plausible_person_name(candidate):
                names.append(candidate)
    return names


def extract_board_from_text(text: str) -> list[str]:
    names: list[str] = []
    director_pattern = re.compile(
        r"([A-Z][A-Za-z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’.-]+)){1,4})"
        r"\s*(?:,|–|-|—)?\s*(?:Independent\s+)?Director\b",
        re.I,
    )
    for match in director_pattern.finditer(text):
        candidate = normalize_name(match.group(1))
        if is_plausible_person_name(candidate):
            names.append(candidate)

    nominee_sentence_pattern = re.compile(
        r"(?:nominees?|directors?)\s+(?:are|include|were)\s+(.{20,500}?)(?:\.|;)",
        re.I,
    )
    for sentence_match in nominee_sentence_pattern.finditer(text):
        phrase = sentence_match.group(1)
        for name_match in NAME_RE.finditer(phrase):
            candidate = normalize_name(name_match.group(1))
            if is_plausible_person_name(candidate):
                names.append(candidate)
    return names


def dedupe_names(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for name in names:
        normalized = normalize_name(name)
        key = canonical_name(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def parse_filing(html: str) -> list[PersonRelationship]:
    soup = BeautifulSoup(html, "lxml")
    text = clean_text(soup.get_text(" "))
    relationships = extract_officers(text)
    board_names = dedupe_names(extract_board_from_tables(soup) + extract_board_from_text(text))
    officer_keys = {canonical_name(rel.name) for rel in relationships}
    for name in board_names:
        if canonical_name(name) in officer_keys or is_plausible_person_name(name):
            relationships.append(PersonRelationship(name, "BOARD_OF"))
    return dedupe_relationships(relationships)


def dedupe_relationships(relationships: Iterable[PersonRelationship]) -> list[PersonRelationship]:
    seen: set[tuple[str, str]] = set()
    deduped: list[PersonRelationship] = []
    for rel in relationships:
        name = normalize_name(rel.name)
        key = (canonical_name(name), rel.relationship_type)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(PersonRelationship(name=name, relationship_type=rel.relationship_type))
    return deduped


def person_node_id(name: str) -> str:
    normalized = canonical_name(name)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_name(name).lower()).strip("_")
    return f"person:{slug}:{digest}"


def failure_reason(label: str, exc: Exception | None = None) -> str:
    if exc is None:
        return label
    detail = clean_text(str(exc))[:240]
    if detail:
        return f"{label}: {type(exc).__name__}: {detail}"
    return f"{label}: {type(exc).__name__}"


def write_outputs(
    output_dir: Path,
    companies: list[Company],
    edges: list[dict[str, str]],
    parse_log: list[dict[str, str]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir.mkdir(parents=True, exist_ok=True)
    company_df = pd.DataFrame(
        [
            {
                "node_id": f"company:{company.ticker}",
                "node_type": "Company",
                "ticker": company.ticker,
                "cik": company.cik,
                "company_name": company.company_name,
            }
            for company in companies
        ],
        columns=["node_id", "node_type", "ticker", "cik", "company_name"],
    )

    people_by_id: dict[str, dict[str, str]] = {}
    for edge in edges:
        people_by_id[edge["source_node_id"]] = {
            "node_id": edge["source_node_id"],
            "node_type": "Person",
            "name": edge["source_person"],
            "normalized_name": canonical_name(edge["source_person"]),
        }
    person_df = pd.DataFrame(
        sorted(people_by_id.values(), key=lambda row: row["node_id"]),
        columns=["node_id", "node_type", "name", "normalized_name"],
    )
    edge_df = pd.DataFrame(
        edges,
        columns=[
            "source_node_id",
            "target_node_id",
            "relationship_type",
            "source_person",
            "target_company",
            "ticker",
            "filing_date",
            "filing_url",
        ],
    )
    log_df = pd.DataFrame(
        parse_log,
        columns=[
            "ticker",
            "cik",
            "company_name",
            "status",
            "reason",
            "filing_date",
            "filing_url",
            "relationships_found",
        ],
    )

    company_df.to_csv(output_dir / "company_nodes.csv", index=False)
    person_df.to_csv(output_dir / "person_nodes.csv", index=False)
    edge_df.to_csv(output_dir / "edges.csv", index=False)
    log_df.to_csv(output_dir / "parse_log.csv", index=False)
    return company_df, person_df, edge_df, log_df


def print_summary(
    companies_processed: int,
    filings_found: int,
    parsed_successfully: int,
    company_nodes: int,
    person_nodes: int,
    relationship_edges: int,
    failures: Counter,
) -> None:
    print("\nConstellation V0 summary")
    print("========================")
    print(f"companies processed: {companies_processed}")
    print(f"DEF 14A filings found: {filings_found}")
    print(f"companies parsed successfully: {parsed_successfully}")
    print(f"company nodes: {company_nodes}")
    print(f"person nodes: {person_nodes}")
    print(f"relationship edges: {relationship_edges}")
    print("failures by reason:")
    if failures:
        for reason, count in sorted(failures.items()):
            print(f"  - {reason}: {count}")
    else:
        print("  - none: 0")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    cache_dir = output_dir / "cache"
    filings_cache_dir = cache_dir / "filings"
    client = SecClient(args.user_agent, cache_dir)

    if args.user_agent == DEFAULT_USER_AGENT:
        print(
            f"Warning: using the default User-Agent. For production SEC access, set {SEC_USER_AGENT_ENV} "
            "to an app/org name plus contact email.",
            file=sys.stderr,
        )

    tickers, universe_source = get_universe(args.tickers, args.limit, args.user_agent, cache_dir)
    print(f"Universe source: {universe_source}")
    print(f"SOXX tickers queued: {len(tickers)}")

    ticker_map = fetch_sec_ticker_map(client, cache_dir)
    companies: list[Company] = []
    edges: list[dict[str, str]] = []
    parse_log: list[dict[str, str]] = []
    failures: Counter = Counter()
    filings_found = 0
    parsed_successfully = 0

    for index, ticker in enumerate(tickers, start=1):
        print(f"[{index}/{len(tickers)}] {ticker}")
        company = ticker_map.get(ticker)
        if not company:
            failures["cik_not_found"] += 1
            parse_log.append(
                {
                    "ticker": ticker,
                    "cik": "",
                    "company_name": "",
                    "status": "failed",
                    "reason": "cik_not_found",
                    "filing_date": "",
                    "filing_url": "",
                    "relationships_found": "0",
                }
            )
            continue
        companies.append(company)
        try:
            filing = find_latest_def14a(client, company)
        except Exception as exc:
            failures["sec_submission_error"] += 1
            parse_log.append(
                {
                    "ticker": ticker,
                    "cik": company.cik,
                    "company_name": company.company_name,
                    "status": "failed",
                    "reason": failure_reason("sec_submission_error", exc),
                    "filing_date": "",
                    "filing_url": "",
                    "relationships_found": "0",
                }
            )
            continue
        if not filing:
            failures["def14a_not_found"] += 1
            parse_log.append(
                {
                    "ticker": ticker,
                    "cik": company.cik,
                    "company_name": company.company_name,
                    "status": "failed",
                    "reason": "def14a_not_found",
                    "filing_date": "",
                    "filing_url": "",
                    "relationships_found": "0",
                }
            )
            continue
        filings_found += 1
        cache_path = filings_cache_dir / ticker / f"{filing.accession.replace('-', '')}_{filing.primary_document}"
        try:
            html = client.download_text_cached(filing.filing_url, cache_path)
            relationships = parse_filing(html)
        except Exception as exc:
            failures["filing_parse_error"] += 1
            parse_log.append(
                {
                    "ticker": ticker,
                    "cik": company.cik,
                    "company_name": company.company_name,
                    "status": "failed",
                    "reason": failure_reason("filing_parse_error", exc),
                    "filing_date": filing.filing_date,
                    "filing_url": filing.filing_url,
                    "relationships_found": "0",
                }
            )
            continue
        if not relationships:
            failures["no_relationships_extracted"] += 1
            status = "failed"
            reason = "no_relationships_extracted"
        else:
            parsed_successfully += 1
            status = "success"
            reason = ""
        company_node_id = f"company:{company.ticker}"
        for rel in relationships:
            edges.append(
                {
                    "source_node_id": person_node_id(rel.name),
                    "target_node_id": company_node_id,
                    "relationship_type": rel.relationship_type,
                    "source_person": rel.name,
                    "target_company": company.company_name,
                    "ticker": company.ticker,
                    "filing_date": filing.filing_date,
                    "filing_url": filing.filing_url,
                }
            )
        parse_log.append(
            {
                "ticker": ticker,
                "cik": company.cik,
                "company_name": company.company_name,
                "status": status,
                "reason": reason,
                "filing_date": filing.filing_date,
                "filing_url": filing.filing_url,
                "relationships_found": str(len(relationships)),
            }
        )

    # Keep edge rows unique if the same person/title was discovered by multiple heuristics.
    unique_edges: dict[tuple[str, str, str], dict[str, str]] = {}
    for edge in edges:
        key = (edge["source_node_id"], edge["target_node_id"], edge["relationship_type"])
        unique_edges[key] = edge
    edges = sorted(unique_edges.values(), key=lambda row: (row["ticker"], row["relationship_type"], row["source_person"]))

    company_df, person_df, edge_df, _ = write_outputs(output_dir, companies, edges, parse_log)
    print_summary(
        companies_processed=len(tickers),
        filings_found=filings_found,
        parsed_successfully=parsed_successfully,
        company_nodes=len(company_df),
        person_nodes=len(person_df),
        relationship_edges=len(edge_df),
        failures=failures,
    )
    print(f"\nCSV outputs saved under: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
