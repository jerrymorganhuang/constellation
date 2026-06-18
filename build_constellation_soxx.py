#!/usr/bin/env python3
"""Build Constellation V0 SEC-to-graph CSVs for SOXX constituents.

This standalone pipeline:
- fetches the current SOXX holdings universe,
- maps each ticker to its SEC CIK,
- finds the latest 10-K annual report,
- downloads and caches the filing HTML,
- parses Item 10 / Directors, Executive Officers and Corporate Governance,
- extracts CEO, CFO, executive-officer, and board-member relationships with deterministic rules, and
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
import logging
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
    "principal executive officer",
    "principal financial officer",
    "principal accounting officer",
    "annual report",
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
SEC_SECTION_AND_TAXONOMY_LABELS = {
    "business",
    "risk factors",
    "unresolved staff comments",
    "properties",
    "legal proceedings",
    "mine safety disclosures",
    "market for registrant common equity",
    "selected financial data",
    "management discussion and analysis",
    "quantitative and qualitative disclosures about market risk",
    "financial statements and supplementary data",
    "changes in and disagreements with accountants",
    "controls and procedures",
    "directors executive officers and corporate governance",
    "executive compensation",
    "security ownership of certain beneficial owners and management",
    "certain relationships and related transactions",
    "principal accountant fees and services",
    "exhibits and financial statement schedules",
    "form 10 k summary",
    "segment reporting",
    "income taxes",
    "revenue recognition",
    "goodwill",
    "share based compensation",
    "stock based compensation",
    "fair value measurements",
    "derivative instruments",
    "commitments and contingencies",
    "subsequent events",
}
NON_PERSON_ORG_TERMS = {
    "company",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "plc",
    "group",
    "holdings",
    "technologies",
    "semiconductor",
    "committee",
    "board",
    "section",
    "item",
}
DOCUMENT_NAME_TERMS = {
    "agreement",
    "plan",
    "act",
    "policy",
    "exhibit",
    "schedule",
    "compensation",
}
DOCUMENT_NAME_PHRASES = {
    "stock unit",
    "restricted stock",
    "restricted stock unit",
    "deferred restricted stock unit",
    "securities exchange act",
}
TITLE_ONLY_PHRASES = {
    "executive vice president",
    "chief financial officer",
    "chief executive officer",
    "president",
    "director",
}
TITLE_WORDS = {
    "acting",
    "assistant",
    "chief",
    "chair",
    "chairman",
    "chairperson",
    "co",
    "corporate",
    "director",
    "executive",
    "financial",
    "general",
    "independent",
    "interim",
    "lead",
    "legal",
    "officer",
    "president",
    "principal",
    "secretary",
    "senior",
    "treasurer",
    "vice",
}
SIGNATURE_TABLE_LABEL_TOKENS = {"title", "name", "date", "signature"}
NAME_CAPTURE_PATTERN = r"[A-Z][a-zA-Z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'’.-]+)){1,4}"
NAME_RE = re.compile(rf"\b({NAME_CAPTURE_PATTERN})\b")
XBRL_TAG_RE = re.compile(
    r"^(?:[a-z][a-z0-9-]*:)?[a-z][a-z0-9]*(?:_[a-z0-9]+)+$|^(?:us-gaap|dei|srt|country|currency):",
    re.I,
)
WHITESPACE_RE = re.compile(r"\s+")
LOGGER = logging.getLogger("constellation")

ITEM_HEADING_RE = re.compile(r"\bItem\s+([0-9]{1,2}[A-Z]?)\s*[.:\-–—]", re.I)
EXECUTIVE_OFFICER_CONTEXT_RE = re.compile(r"executive\s+officers?", re.I)
SIGNATURE_HEADING_RE = re.compile(r"\bSIGNATURES\b", re.I)
SIGNATURE_EXCHANGE_ACT_ANCHOR_RE = re.compile(
    r"pursuant\s+to\s+the\s+requirements\s+of\s+the\s+securities\s+exchange\s+act\s+of\s+1934",
    re.I,
)
SIGNATURE_TITLE_RE = re.compile(
    r"(?i:chief\s+executive\s+officer|chief\s+financial\s+officer|principal\s+executive\s+officer|"
    r"principal\s+financial\s+officer|\bCEO\b|\bCFO\b|\bdirector\b)"
)
SIGNATURE_SECTION_MAX_CHARS = 45000


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
    source: str = "unknown"


@dataclass(frozen=True)
class FilingExtraction:
    relationships: list[PersonRelationship]
    signature_relationships: list[PersonRelationship]
    item_10_relationships: list[PersonRelationship]


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
    parser.add_argument(
        "--signature-only",
        action="store_true",
        help=(
            "Temporary validation mode: extract only signature-page relationships and skip Item 10 "
            "extraction, fallback, and merge logic."
        ),
    )
    parser.add_argument(
        "--debug-signature-tickers",
        help=(
            "Comma-separated tickers for signature extraction debug logs. Writes "
            "data/debug_<ticker>.log files."
        ),
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


def is_10k_form(form: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", form.upper())
    return normalized == "10K"


def find_latest_10k(client: SecClient, company: Company) -> Filing | None:
    candidates: list[dict[str, str]] = []
    for row in iter_all_submission_rows(client, company.cik):
        if is_10k_form(row.get("form", "")):
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


def looks_like_xbrl_tag(value: str) -> bool:
    compact = clean_text(value)
    if XBRL_TAG_RE.search(compact):
        return True
    # XBRL concepts often appear as CamelCase labels without spaces. Real names in
    # this parser must be at least two separate tokens, so reject these before
    # normalization can turn them into person-looking words.
    return bool(re.fullmatch(r"[A-Za-z]+(?:[A-Z][a-z0-9]+){1,}", compact))


def is_sec_section_or_taxonomy_label(name: str) -> bool:
    raw = clean_text(name)
    lowered_raw = raw.lower().strip(" .:-–—")
    normalized = normalize_name(raw)
    lowered = normalized.lower()
    label = re.sub(r"\s+", " ", lowered)
    if lowered_raw in SEC_SECTION_AND_TAXONOMY_LABELS or label in SEC_SECTION_AND_TAXONOMY_LABELS:
        return True
    if re.match(r"^item\s+\d{1,2}[a-z]?\b", lowered_raw):
        return True
    if looks_like_xbrl_tag(raw):
        return True
    return False


def person_name_rejection_reason(name: str) -> str | None:
    """Return why a candidate is not a strict person-like name, or None if valid."""
    raw = clean_text(name)
    normalized = normalize_name(raw)
    if not normalized:
        return "empty_after_normalization"
    lowered = normalized.lower()
    lowered_raw = raw.lower().strip(" .:-–—")
    compact_lowered = re.sub(r"\s+", " ", lowered)
    if lowered in NAME_STOPWORDS or is_sec_section_or_taxonomy_label(raw):
        return "sec_section_or_stopword"
    for phrase in DOCUMENT_NAME_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", compact_lowered) or re.search(
            rf"\b{re.escape(phrase)}\b", lowered_raw
        ):
            return "document_name_phrase"
    parts = normalized.split()
    if len(parts) < 2:
        return "too_few_tokens"
    if len(parts) > 5:
        return "too_many_tokens"
    alpha_tokens = [part for part in parts if re.search(r"[A-Za-z]", part)]
    if len(alpha_tokens) < 2:
        return "too_few_alpha_tokens"
    token_words = [part.lower().strip(".,") for part in parts]
    if compact_lowered in TITLE_ONLY_PHRASES:
        return "title_only_phrase"
    if all(token in TITLE_WORDS for token in token_words):
        return "all_title_words"
    if any(token in DOCUMENT_NAME_TERMS for token in token_words):
        return "document_name_term"
    blocked_tokens = {"and", "or", "the", "for", "with", "from", "of", "in", "to", "by", "as"}
    if any(token in blocked_tokens for token in token_words):
        return "blocked_connector_token"
    if any(token in NON_PERSON_ORG_TERMS for token in token_words):
        return "organization_term"
    if not all(re.match(r"^[A-Z][a-zA-Z'’.-]*$|^[A-Z]\.$", part) for part in parts):
        return "invalid_name_casing_or_characters"
    # Names should not be made entirely of filing/accounting vocabulary. This keeps
    # title-cased 10-K headings such as "Legal Proceedings" out of person nodes.
    vocabulary_tokens = {token for label in SEC_SECTION_AND_TAXONOMY_LABELS for token in label.split()}
    if all(part.lower().strip(".") in vocabulary_tokens for part in parts):
        return "all_filing_vocabulary"
    return None


def log_rejected_person_candidate(name: str, reason: str, context: str = "") -> None:
    candidate = clean_text(name)
    if not candidate:
        return
    if context:
        LOGGER.info("Rejected person candidate (%s): %s [%s]", reason, candidate, context)
    else:
        LOGGER.info("Rejected person candidate (%s): %s", reason, candidate)


def is_plausible_person_name(
    name: str, *, log_rejection: bool = False, context: str = ""
) -> bool:
    reason = person_name_rejection_reason(name)
    if reason is None:
        return True
    if log_rejection:
        log_rejected_person_candidate(name, reason, context)
    return False


def is_valid_person_relationship(
    rel: PersonRelationship, *, log_rejection: bool = False, context: str = ""
) -> bool:
    valid_relationship_type = rel.relationship_type in {"CEO_OF", "CFO_OF", "EXECUTIVE_OFFICER_OF", "BOARD_OF"}
    if not valid_relationship_type:
        return False
    return is_plausible_person_name(rel.name, log_rejection=log_rejection, context=context or rel.relationship_type)


def extract_name_before_title(text: str, title_pattern: str) -> list[str]:
    names: list[str] = []
    pattern = re.compile(
        rf"({NAME_CAPTURE_PATTERN})"
        rf"\s*(?:,|–|-|—|\(|\sis\s|\sserves\sas\s|\swas\s)?\s*"
        rf"(?:our\s+|the\s+|as\s+)?(?i:{title_pattern})",
    )
    for match in pattern.finditer(text):
        candidate = normalize_name(match.group(1))
        if is_plausible_person_name(candidate):
            names.append(candidate)
    return names


def extract_name_after_title(text: str, title_pattern: str) -> list[str]:
    names: list[str] = []
    pattern = re.compile(
        rf"(?i:{title_pattern})\s*(?:is|was|:|,|-|–|—)?\s*"
        rf"({NAME_CAPTURE_PATTERN})",
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


def extract_item_section(text: str, item_number: str) -> str:
    """Return a 10-K item section from flattened filing text when headings are detectable."""
    matches = list(ITEM_HEADING_RE.finditer(text))
    for target_index, match in enumerate(matches):
        if match.group(1).upper() != item_number.upper():
            continue
        start = match.start()
        end = len(text)
        for next_match in matches[target_index + 1 :]:
            next_item = next_match.group(1).upper()
            if next_item in {"11", "12", "13", "14", "15"}:
                end = next_match.start()
                break
        section = text[start:end]
        # Skip table-of-contents snippets and keep searching for the real item body.
        if len(section) >= 500:
            return section
    return ""


def extract_context_sections(text: str, context_pattern: re.Pattern[str], window: int = 6000) -> str:
    sections: list[str] = []
    for match in context_pattern.finditer(text):
        start = max(0, match.start() - window // 4)
        end = min(len(text), match.end() + window)
        sections.append(text[start:end])
    return " ".join(sections)


def title_to_relationships(title: str) -> list[str]:
    lower = title.lower()
    relationships: list[str] = []
    if "chief executive officer" in lower or re.search(r"\bceo\b", lower):
        relationships.append("CEO_OF")
    if "chief financial officer" in lower or re.search(r"\bcfo\b", lower):
        relationships.append("CFO_OF")
    if "executive officer" in lower or any(
        token in lower
        for token in (
            "chief ",
            "president",
            "principal accounting officer",
            "general counsel",
            "corporate secretary",
            "treasurer",
            "executive vice president",
            "senior vice president",
        )
    ):
        relationships.append("EXECUTIVE_OFFICER_OF")
    return relationships


def extract_executive_officers_from_tables(soup: BeautifulSoup, source: str = "item_10") -> list[PersonRelationship]:
    relationships: list[PersonRelationship] = []
    for table in soup.find_all("table"):
        table_text = clean_text(table.get_text(" "))
        if not EXECUTIVE_OFFICER_CONTEXT_RE.search(table_text):
            continue
        rows = table_rows(table)
        if len(rows) < 2:
            continue
        header = [cell.lower() for cell in rows[0]]
        name_index = 0
        for idx, cell in enumerate(header):
            if "name" in cell or "executive officer" in cell:
                name_index = idx
        for row in rows[1:]:
            if name_index >= len(row):
                continue
            name = normalize_name(row[name_index])
            if not is_plausible_person_name(name) and len(row) > 1:
                name = normalize_name(row[1])
            if not is_plausible_person_name(name):
                continue
            title = " ".join(cell for idx, cell in enumerate(row) if idx != name_index)
            for relationship_type in title_to_relationships(title + " executive officer"):
                relationships.append(PersonRelationship(name, relationship_type, source))
    return relationships


def extract_executive_officers_from_text(text: str, source: str = "item_10") -> list[PersonRelationship]:
    relationships: list[PersonRelationship] = []
    officer_sections = extract_context_sections(text, EXECUTIVE_OFFICER_CONTEXT_RE, window=5000)
    if not officer_sections:
        return relationships
    row_pattern = re.compile(
        rf"({NAME_CAPTURE_PATTERN})"
        r"\s*(?:,|–|-|—|\()\s*"
        r"(?i:([^.;]{0,220}?(?:Chief|CEO|CFO|President|Vice President|General Counsel|Secretary|Treasurer|Executive Officer)[^.;]{0,220}))",
    )
    for match in row_pattern.finditer(officer_sections):
        name = normalize_name(match.group(1))
        if not is_plausible_person_name(name):
            continue
        title = clean_text(match.group(2))
        for relationship_type in title_to_relationships(title):
            relationships.append(PersonRelationship(name, relationship_type, source))
    return relationships


def extract_executive_officers(soup: BeautifulSoup, text: str, source: str = "item_10") -> list[PersonRelationship]:
    relationships = extract_executive_officers_from_tables(soup, source=source)
    relationships.extend(extract_executive_officers_from_text(text, source=source))
    return dedupe_relationships(relationships)


def extract_signature_section_text(full_text: str) -> str:
    """Return the terminal 10-K signature section as plain text."""
    matches = list(SIGNATURE_HEADING_RE.finditer(full_text))
    if not matches:
        return ""
    start = matches[-1].start()
    return full_text[start : start + SIGNATURE_SECTION_MAX_CHARS]


def normalize_signature_name(name: str) -> str:
    candidate = normalize_name(name)
    parts = candidate.split()
    title_starters = {"chair", "chairman", "chairperson", "chief", "president", "executive", "senior", "vice", "director"}
    for idx, part in enumerate(parts):
        if idx >= 2 and part.lower().strip(".") in title_starters:
            candidate = " ".join(parts[:idx])
            break
    letters = re.sub(r"[^A-Za-z]", "", candidate)
    if letters and letters.upper() == letters:
        candidate = " ".join(part if re.fullmatch(r"[A-Z]\.", part) else part.title() for part in candidate.split())
    return candidate


def signature_person_candidate_name(candidate_text: str, context: str) -> str:
    """Normalize and validate a signature person candidate, repairing trailing table labels only."""
    candidate = normalize_signature_name(candidate_text)
    parts = candidate.split()
    label_indexes = [
        idx
        for idx, part in enumerate(parts)
        if part.strip(".,:;()[]").lower() in SIGNATURE_TABLE_LABEL_TOKENS
    ]
    if label_indexes:
        if label_indexes == [len(parts) - 1]:
            candidate = " ".join(parts[:-1])
        else:
            log_rejected_person_candidate(candidate, "embedded_signature_table_label", context)
            return ""
    if is_plausible_person_name(candidate, log_rejection=True, context=context):
        return candidate
    return ""


def signature_candidate_names(text: str) -> list[str]:
    """Return plausible names in a signature text fragment, preserving proximity order."""
    names: list[str] = []
    slash_pattern = re.compile(rf"/s/\s*({NAME_CAPTURE_PATTERN})", re.I)
    for match in slash_pattern.finditer(text):
        candidate = signature_person_candidate_name(match.group(1), "signature_slash_candidate")
        if candidate:
            names.append(candidate)
    for match in NAME_RE.finditer(text):
        candidate = signature_person_candidate_name(match.group(1), "signature_text_candidate")
        if candidate:
            names.append(candidate)
    return names


def relationships_from_signature_name_and_title(
    name: str, title: str, source: str = "signature_free_text"
) -> list[PersonRelationship]:
    relationships: list[PersonRelationship] = []
    if not is_plausible_person_name(name, log_rejection=True, context="signature_relationship_candidate"):
        return relationships
    title_lower = title.lower()
    for relationship_type in title_to_relationships(title):
        # Signature pages identify the legal signers. Keep CEO/CFO precise and
        # do not broaden every president/VP signer into an executive-officer edge.
        if relationship_type in {"CEO_OF", "CFO_OF"}:
            relationships.append(PersonRelationship(name, relationship_type, source))
    if "principal executive officer" in title_lower and not any(rel.relationship_type == "CEO_OF" for rel in relationships):
        relationships.append(PersonRelationship(name, "CEO_OF", source))
    if "principal financial officer" in title_lower and not any(rel.relationship_type == "CFO_OF" for rel in relationships):
        relationships.append(PersonRelationship(name, "CFO_OF", source))
    if re.search(r"\bdirector\b", title, re.I):
        relationships.append(PersonRelationship(name, "BOARD_OF", source))
    return relationships


def extract_signature_relationships_from_text(signature_text: str) -> list[PersonRelationship]:
    """Extract signer relationships from the terminal SIGNATURES block text."""
    relationships: list[PersonRelationship] = []
    if not signature_text:
        return relationships
    for match in SIGNATURE_TITLE_RE.finditer(signature_text):
        lookback_start = max(0, match.start() - 220)
        lookahead_end = min(len(signature_text), match.end() + 180)
        nearby_before = signature_text[lookback_start : match.start()]
        names = signature_candidate_names(nearby_before)
        name = names[-1] if names else ""
        title_context = signature_text[match.start() : lookahead_end]
        if not name:
            # Some precision-friendly signature blocks put the typed name on the
            # line after the title/date, especially for power-of-attorney rows.
            after_names = signature_candidate_names(signature_text[match.end() : lookahead_end])
            name = after_names[0] if after_names else ""
        if not name:
            continue
        relationships.extend(relationships_from_signature_name_and_title(name, title_context, source="signature_free_text"))
    return dedupe_relationships(relationships)


def tables_after_signature_anchor(soup: BeautifulSoup) -> list:
    """Return HTML tables below the Exchange Act signature-page anchor."""
    tables: list = []
    seen: set[int] = set()
    after_anchor = False
    found_anchor = False
    for node in soup.descendants:
        if not after_anchor and isinstance(node, str) and SIGNATURE_EXCHANGE_ACT_ANCHOR_RE.search(node):
            after_anchor = True
            found_anchor = True
            continue
        if after_anchor and getattr(node, "name", None) == "table" and id(node) not in seen:
            seen.add(id(node))
            tables.append(node)
    return tables if found_anchor else []


def table_rows_with_cell_lines(table) -> list[list[list[str]]]:
    rows: list[list[list[str]]] = []
    for tr in table.find_all("tr"):
        row: list[list[str]] = []
        for cell in tr.find_all(["th", "td"]):
            raw_lines = cell.get_text("\n").splitlines()
            lines = [clean_text(line) for line in raw_lines]
            lines = [line for line in lines if line]
            if not lines:
                text = clean_text(cell.get_text(" "))
                lines = [text] if text else []
            if lines:
                row.append(lines)
        if row:
            rows.append(row)
    return rows


def flatten_signature_cell_lines(lines: list[str]) -> str:
    return clean_text(" ".join(lines))


def signature_table_column_pairs(rows: list[list[list[str]]]) -> tuple[list[tuple[int, int]], int]:
    """Return signature/title column pairs plus first data row for a signature table.

    Some SEC signature pages are laid out as repeated Signature/Title groups in
    the same row (for example two signers per row). Returning only the first
    Signature and first Title column lets a later CFO title on the right side of
    the row leak onto an unrelated left-side signer. Keep every header pair so
    each signer is matched only to the title in the same column group.
    """
    for row_idx, row in enumerate(rows[:3]):
        lowered = [flatten_signature_cell_lines(cell).lower() for cell in row]
        signature_indexes = [
            idx for idx, cell in enumerate(lowered) if "signature" in cell or cell in {"name", "signer"}
        ]
        title_indexes = [idx for idx, cell in enumerate(lowered) if "title" in cell or "capacity" in cell]
        pairs: list[tuple[int, int]] = []
        used_titles: set[int] = set()
        for signature_idx in signature_indexes:
            available_titles = [idx for idx in title_indexes if idx != signature_idx and idx not in used_titles]
            if not available_titles:
                continue
            # A title normally sits immediately to the right of its signer; if
            # not, use the closest title header to keep repeated groups local.
            title_idx = min(available_titles, key=lambda idx: (idx < signature_idx, abs(idx - signature_idx)))
            used_titles.add(title_idx)
            pairs.append((signature_idx, title_idx))
        if pairs:
            return pairs, row_idx + 1
    return [], 0


def signature_table_column_indexes(rows: list[list[list[str]]]) -> tuple[int | None, int | None, int]:
    """Return the first signature/title pair for compatibility with older tests."""
    pairs, first_data_row = signature_table_column_pairs(rows)
    if not pairs:
        return None, None, first_data_row
    signature_idx, title_idx = pairs[0]
    return signature_idx, title_idx, first_data_row


def extract_signature_name_from_cell(lines: list[str]) -> str:
    """Extract the signer from the signature column, preferring typed non-/s/ lines."""
    slash_candidates: list[str] = []
    typed_candidates: list[str] = []
    for line in lines:
        if re.search(r"(?i)\bsignature\b", line):
            continue
        line_without_slash = re.sub(r"(?i)^\s*/s/\s*", "", line)
        target = slash_candidates if line_without_slash != line else typed_candidates
        for candidate_text in [line_without_slash, *[m.group(1) for m in NAME_RE.finditer(line_without_slash)]]:
            candidate = signature_person_candidate_name(candidate_text, "signature_table_name_cell")
            if candidate:
                target.append(candidate)
    candidates = typed_candidates or slash_candidates
    return candidates[-1] if candidates else ""


def infer_signature_table_index_pairs(row: list[list[str]]) -> list[tuple[int, int]]:
    """Infer local signature/title pairs for a data row when no header is present."""
    name_indexes = [idx for idx, cell in enumerate(row) if extract_signature_name_from_cell(cell)]
    title_indexes = [idx for idx, cell in enumerate(row) if SIGNATURE_TITLE_RE.search(flatten_signature_cell_lines(cell))]
    pairs: list[tuple[int, int]] = []
    used_names: set[int] = set()
    for title_idx in title_indexes:
        available_names = [idx for idx in name_indexes if idx != title_idx and idx not in used_names]
        if not available_names:
            continue
        # Prefer the nearest name to the left of the title, which matches common
        # Signature | Title groups; fall back to the closest name on the right.
        name_idx = min(available_names, key=lambda idx: (idx > title_idx, abs(title_idx - idx)))
        used_names.add(name_idx)
        pairs.append((name_idx, title_idx))
    return pairs


def infer_signature_table_indexes(row: list[list[str]]) -> tuple[int | None, int | None]:
    """Infer the first signature/title pair for compatibility with older tests."""
    pairs = infer_signature_table_index_pairs(row)
    return pairs[0] if pairs else (None, None)



def extract_signature_relationships_from_table(table) -> list[PersonRelationship]:
    rows = table_rows_with_cell_lines(table)
    if not rows:
        return []
    table_text = clean_text(table.get_text(" "))
    if SIGNATURE_TITLE_RE.search(table_text) is None:
        return []

    relationships: list[PersonRelationship] = []
    column_pairs, first_data_row = signature_table_column_pairs(rows)
    for row in rows[first_data_row:]:
        row_pairs = column_pairs or infer_signature_table_index_pairs(row)
        for row_signature_idx, row_title_idx in row_pairs:
            if row_signature_idx >= len(row) or row_title_idx >= len(row):
                continue

            title = flatten_signature_cell_lines(row[row_title_idx])
            if not SIGNATURE_TITLE_RE.search(title):
                continue
            name = extract_signature_name_from_cell(row[row_signature_idx])
            if not name:
                continue
            relationships.extend(relationships_from_signature_name_and_title(name, title, source="signature_table"))
    return dedupe_relationships(relationships)


def extract_signature_relationships_from_tables(soup: BeautifulSoup) -> list[PersonRelationship]:
    """Extract signer relationships from signature tables below the Exchange Act anchor.

    Table parsing treats the anchor sentence only as a locator. Names come from
    the row's Signature column and roles come only from the same row's Title
    column, preventing prose or neighboring rows from leaking into relationships.
    """
    relationships: list[PersonRelationship] = []
    anchored_tables = tables_after_signature_anchor(soup)
    anchored_table_ids = {id(table) for table in anchored_tables}
    candidate_tables = anchored_tables or list(soup.find_all("table"))
    for table in candidate_tables:
        table_text = clean_text(table.get_text(" "))
        lower = table_text.lower()
        if id(table) not in anchored_table_ids and not ("/s/" in table_text or "signature" in lower):
            continue
        relationships.extend(extract_signature_relationships_from_table(table))
    return dedupe_relationships(relationships)



def format_debug_relationships(relationships: Iterable[PersonRelationship]) -> str:
    rels = list(relationships)
    if not rels:
        return "[]"
    return ", ".join(f"{rel.name} -> {rel.relationship_type} [source={rel.source}]" for rel in rels)


def write_signature_debug_log(ticker: str, html: str, debug_dir: Path = Path("data")) -> None:
    """Write an observation-only log for the existing SEC signature extraction path."""
    soup = BeautifulSoup(html, "html.parser")
    debug_path = debug_dir / f"debug_{ticker}.log"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        f"Signature extraction debug log for {ticker}",
        "",
        "This log is observation-only and does not change extraction behavior.",
        "",
    ]

    anchored_tables = tables_after_signature_anchor(soup)
    lines.append("1. Raw table rows detected after the Exchange Act anchor")
    if not anchored_tables:
        lines.append("  No tables detected after the Exchange Act anchor.")
    for table_index, table in enumerate(anchored_tables, start=1):
        rows = table_rows_with_cell_lines(table)
        lines.append(f"  Table {table_index}: {len(rows)} row(s)")
        for row_index, row in enumerate(rows, start=1):
            cells = [" | ".join(cell) for cell in row]
            lines.append(f"    Row {row_index}: {cells}")
    lines.append("")

    lines.append("2. Detected signer candidates")
    candidate_tables = anchored_tables or list(soup.find_all("table"))
    anchored_table_ids = {id(table) for table in anchored_tables}
    any_candidate_table = False
    for table_index, table in enumerate(candidate_tables, start=1):
        table_text = clean_text(table.get_text(" "))
        lower = table_text.lower()
        if id(table) not in anchored_table_ids and not ("/s/" in table_text or "signature" in lower):
            continue
        any_candidate_table = True
        rows = table_rows_with_cell_lines(table)
        column_pairs, first_data_row = signature_table_column_pairs(rows)
        for row_index, row in enumerate(rows[first_data_row:], start=first_data_row + 1):
            row_pairs = column_pairs or infer_signature_table_index_pairs(row)
            for signature_idx, _title_idx in row_pairs:
                if signature_idx < len(row):
                    name = extract_signature_name_from_cell(row[signature_idx])
                    lines.append(
                        f"  Table {table_index} row {row_index} cell {signature_idx}: "
                        f"{name or '[none]'} from {row[signature_idx]}"
                    )
    if not any_candidate_table:
        lines.append("  No candidate signature tables evaluated.")
    lines.append("")

    lines.append("3. Detected title candidates")
    for table_index, table in enumerate(candidate_tables, start=1):
        table_text = clean_text(table.get_text(" "))
        lower = table_text.lower()
        if id(table) not in anchored_table_ids and not ("/s/" in table_text or "signature" in lower):
            continue
        rows = table_rows_with_cell_lines(table)
        column_pairs, first_data_row = signature_table_column_pairs(rows)
        for row_index, row in enumerate(rows[first_data_row:], start=first_data_row + 1):
            row_pairs = column_pairs or infer_signature_table_index_pairs(row)
            for _signature_idx, title_idx in row_pairs:
                if title_idx < len(row):
                    title = flatten_signature_cell_lines(row[title_idx])
                    marker = "MATCH" if SIGNATURE_TITLE_RE.search(title) else "NO_MATCH"
                    lines.append(f"  Table {table_index} row {row_index} cell {title_idx}: {marker}: {title}")
    lines.append("")

    lines.append("4. Name-title pairings before relationship creation")
    pairings: list[tuple[str, str, int, int, int]] = []
    for table_index, table in enumerate(candidate_tables, start=1):
        table_text = clean_text(table.get_text(" "))
        lower = table_text.lower()
        if id(table) not in anchored_table_ids and not ("/s/" in table_text or "signature" in lower):
            continue
        rows = table_rows_with_cell_lines(table)
        if not rows or SIGNATURE_TITLE_RE.search(table_text) is None:
            continue
        column_pairs, first_data_row = signature_table_column_pairs(rows)
        for row_index, row in enumerate(rows[first_data_row:], start=first_data_row + 1):
            row_pairs = column_pairs or infer_signature_table_index_pairs(row)
            for signature_idx, title_idx in row_pairs:
                if signature_idx >= len(row) or title_idx >= len(row):
                    continue
                title = flatten_signature_cell_lines(row[title_idx])
                if not SIGNATURE_TITLE_RE.search(title):
                    continue
                name = extract_signature_name_from_cell(row[signature_idx])
                if not name:
                    continue
                pairings.append((name, title, table_index, row_index, signature_idx))
                lines.append(f"  Table {table_index} row {row_index}: {name} => {title}")
    if not pairings:
        lines.append("  No table pairings reached relationship creation.")
    lines.append("")

    lines.append("5. Relationships emitted from each pairing")
    for name, title, table_index, row_index, _signature_idx in pairings:
        emitted = relationships_from_signature_name_and_title(name, title, source="signature_table")
        lines.append(f"  Table {table_index} row {row_index}: {name} => {format_debug_relationships(emitted)}")
    if not pairings:
        lines.append("  No table relationships emitted.")
    lines.append("")

    lines.append("6. Final relationships emitted by extractor source")
    extraction = parse_filing_with_sources(html)
    for rel in extraction.relationships:
        lines.append(f"  {rel.name} -> {rel.relationship_type} [source={rel.source}]")
    if not extraction.relationships:
        lines.append("  No relationships emitted.")
    lines.append("")

    tracked_targets = {
        "QRVO": [("Robert Bruggeworth", "CFO_OF")],
        "OLED": [("Steven Abramson", "CFO_OF")],
        "COHR": [("Officer Pursuant", "CFO_OF"), ("R Anderson", "CFO_OF")],
    }
    normalized_ticker = normalize_ticker(ticker)
    if normalized_ticker in tracked_targets:
        lines.append("7. Requested CFO edge source trace")
        for target_name, target_type in tracked_targets[normalized_ticker]:
            matches = [
                rel
                for rel in extraction.relationships
                if rel.relationship_type == target_type and canonical_name(rel.name) == canonical_name(target_name)
            ]
            if matches:
                for rel in matches:
                    lines.append(f"  {target_name} -> {target_type}: emitted by {rel.source} as {rel.name}")
            else:
                lines.append(f"  {target_name} -> {target_type}: not emitted")

    debug_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def extract_signature_relationships(soup: BeautifulSoup, full_text: str) -> list[PersonRelationship]:
    table_relationships = extract_signature_relationships_from_tables(soup)
    if table_relationships:
        return table_relationships
    signature_text = extract_signature_section_text(full_text)
    return extract_signature_relationships_from_text(signature_text)


def extract_officers(text: str, source: str = "fallback") -> list[PersonRelationship]:
    relationships: list[PersonRelationship] = []
    ceo_title = r"(?:President\s+and\s+)?Chief\s+Executive\s+Officer|CEO"
    cfo_title = r"Chief\s+Financial\s+Officer|CFO"
    ceo = most_common_name(extract_name_before_title(text, ceo_title) + extract_name_after_title(text, ceo_title))
    cfo = most_common_name(extract_name_before_title(text, cfo_title) + extract_name_after_title(text, cfo_title))
    if ceo:
        relationships.append(PersonRelationship(ceo, "CEO_OF", source))
    if cfo and canonical_name(cfo) != canonical_name(ceo or ""):
        relationships.append(PersonRelationship(cfo, "CFO_OF", source))
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
        rf"({NAME_CAPTURE_PATTERN})"
        r"\s*(?:,|–|-|—)?\s*(?i:(?:Independent\s+)?Director\b)",
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


def parse_item_10_relationships(soup: BeautifulSoup, full_text: str) -> list[PersonRelationship]:
    item_10_text = extract_item_section(full_text, "10")
    item_10_or_full_text = item_10_text or full_text

    # Item 10 is the 10-K source for directors and corporate governance. Some 10-Ks
    # place the executive-officer table in Part I and cross-reference it from Item 10,
    # so executive-officer extraction may use the full 10-K text while still staying
    # within the single 10-K source.
    relationships = extract_officers(item_10_or_full_text, source="item_10")
    relationships.extend(extract_executive_officers(soup, full_text, source="item_10"))

    item_10_soup = BeautifulSoup(item_10_text, "lxml") if item_10_text else soup
    board_names = dedupe_names(extract_board_from_tables(item_10_soup) + extract_board_from_text(item_10_or_full_text))
    officer_keys = {canonical_name(rel.name) for rel in relationships}
    for name in board_names:
        if canonical_name(name) in officer_keys or is_plausible_person_name(name):
            relationships.append(PersonRelationship(name, "BOARD_OF", "item_10"))
    return dedupe_relationships(relationships)


def merge_primary_then_fallback(
    primary: Iterable[PersonRelationship], fallback: Iterable[PersonRelationship]
) -> list[PersonRelationship]:
    """Prefer primary-source rows, then add non-conflicting fallback rows."""
    merged: list[PersonRelationship] = []
    seen: set[tuple[str, str]] = set()
    for rel in list(primary) + list(fallback):
        if not is_valid_person_relationship(rel, log_rejection=True, context="merge_relationships"):
            continue
        key = (canonical_name(rel.name), rel.relationship_type)
        if key in seen:
            continue
        seen.add(key)
        merged.append(rel)
    return dedupe_relationships(merged)


def parse_filing_with_sources(html: str, signature_only: bool = False) -> FilingExtraction:
    soup = BeautifulSoup(html, "lxml")
    full_text = clean_text(soup.get_text(" "))
    signature_relationships = extract_signature_relationships(soup, full_text)
    if signature_only:
        return FilingExtraction(
            relationships=signature_relationships,
            signature_relationships=signature_relationships,
            item_10_relationships=[],
        )
    item_10_relationships = parse_item_10_relationships(soup, full_text)
    relationships = merge_primary_then_fallback(signature_relationships, item_10_relationships)
    return FilingExtraction(
        relationships=relationships,
        signature_relationships=signature_relationships,
        item_10_relationships=item_10_relationships,
    )


def parse_filing(html: str) -> list[PersonRelationship]:
    return parse_filing_with_sources(html).relationships


def dedupe_relationships(relationships: Iterable[PersonRelationship]) -> list[PersonRelationship]:
    seen: set[tuple[str, str]] = set()
    deduped: list[PersonRelationship] = []
    for rel in relationships:
        if not is_valid_person_relationship(rel, log_rejection=True, context="dedupe_relationships"):
            continue
        name = normalize_name(rel.name)
        key = (canonical_name(name), rel.relationship_type)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(PersonRelationship(name=name, relationship_type=rel.relationship_type, source=rel.source))
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


def configure_rejection_logging(output_dir: Path) -> None:
    """Persist rejected person-name candidates for extraction debugging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    log_path = (output_dir / "rejected_person_candidates.log").resolve()
    if not any(
        isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path
        for handler in LOGGER.handlers
    ):
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        LOGGER.addHandler(handler)


def write_outputs(
    output_dir: Path,
    companies: list[Company],
    edges: list[dict[str, str]],
    parse_log: list[dict[str, str]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Final precision gate before node creation: no Person node or edge is written
    # unless the source value still validates as a real person-like name.
    edges = [
        edge
        for edge in edges
        if is_plausible_person_name(
            edge.get("source_person", ""),
            log_rejection=True,
            context=f"write_outputs:{edge.get('ticker', '')}",
        )
    ]
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
            "relationship_source",
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
            "signature_relationships_found",
            "signature_success",
            "item_10_relationships_found",
            "item_10_success",
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
    signature_successes: int = 0,
    item_10_successes: int = 0,
    signature_only: bool = False,
) -> None:
    print("\nConstellation V0 summary")
    print("========================")
    print(f"companies processed: {companies_processed}")
    print(f"10-K filings found: {filings_found}")
    print(f"companies parsed successfully: {parsed_successfully}")
    print(f"company nodes: {company_nodes}")
    print(f"person nodes: {person_nodes}")
    print(f"relationship edges: {relationship_edges}")
    signature_rate = (signature_successes / filings_found * 100) if filings_found else 0.0
    item_10_rate = (item_10_successes / filings_found * 100) if filings_found else 0.0
    print(f"signature-page extraction successes: {signature_successes}/{filings_found} ({signature_rate:.1f}%)")
    if signature_only:
        print("Item 10 extraction successes: skipped (--signature-only)")
    else:
        print(f"Item 10 extraction successes: {item_10_successes}/{filings_found} ({item_10_rate:.1f}%)")
    print("failures by reason:")
    if failures:
        for reason, count in sorted(failures.items()):
            print(f"  - {reason}: {count}")
    else:
        print("  - none: 0")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    configure_rejection_logging(output_dir)
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
    debug_signature_tickers = {
        normalize_ticker(ticker) for ticker in (args.debug_signature_tickers or "").split(",") if ticker.strip()
    }
    print(f"Universe source: {universe_source}")
    print(f"SOXX tickers queued: {len(tickers)}")
    if args.signature_only:
        print("Signature-only validation mode: Item 10 extraction, fallback, and merge are disabled.")

    ticker_map = fetch_sec_ticker_map(client, cache_dir)
    companies: list[Company] = []
    edges: list[dict[str, str]] = []
    parse_log: list[dict[str, str]] = []
    failures: Counter = Counter()
    filings_found = 0
    parsed_successfully = 0
    signature_successes = 0
    item_10_successes = 0

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
            filing = find_latest_10k(client, company)
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
            failures["10k_not_found"] += 1
            parse_log.append(
                {
                    "ticker": ticker,
                    "cik": company.cik,
                    "company_name": company.company_name,
                    "status": "failed",
                    "reason": "10k_not_found",
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
            if ticker in debug_signature_tickers:
                write_signature_debug_log(ticker, html)
            extraction = parse_filing_with_sources(html, signature_only=args.signature_only)
            relationships = [
                rel
                for rel in extraction.relationships
                if is_valid_person_relationship(
                    rel, log_rejection=True, context=f"{ticker}:relationships"
                )
            ]
            signature_relationships = [
                rel
                for rel in extraction.signature_relationships
                if is_valid_person_relationship(
                    rel, log_rejection=True, context=f"{ticker}:signature"
                )
            ]
            item_10_relationships = [
                rel
                for rel in extraction.item_10_relationships
                if is_valid_person_relationship(
                    rel, log_rejection=True, context=f"{ticker}:item_10"
                )
            ]
            if signature_relationships:
                signature_successes += 1
            if item_10_relationships:
                item_10_successes += 1
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
                    "relationship_source": rel.source,
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
                "signature_relationships_found": str(len(signature_relationships)),
                "signature_success": "yes" if signature_relationships else "no",
                "item_10_relationships_found": str(len(item_10_relationships)),
                "item_10_success": "yes" if item_10_relationships else "no",
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
        signature_successes=signature_successes,
        item_10_successes=item_10_successes,
        signature_only=args.signature_only,
    )
    print(f"\nCSV outputs saved under: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
