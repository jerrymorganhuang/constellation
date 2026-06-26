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
import time
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

try:
    from lxml import etree as LET
except ImportError:  # pragma: no cover - lxml is an optional recovery parser.
    LET = None

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

SUPPORTED_UNIVERSES = ["SPX", "NDX", "SOX", "RUSSELL1000", "RUSSELL2000"]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 ConstellationCompanyMaster/1.0"
DEBUG_DIR = DATA_DIR / "debug"

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv,application/json,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

NDX_URL = "https://www.nasdaq.com/solutions/global-indexes/nasdaq-100/companies"
SOX_EXPORT_URL_TEMPLATE = "https://indexes.nasdaqomx.com/Index/ExportWeightings/SOX?tradeDate={trade_date}T00:00:00.000&timeOfDay=SOD"
BLACKROCK_IWB_API_URL = "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=239707&component=fundDownload&userType=individual"
BLACKROCK_IWM_API_URL = "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=239710&component=fundDownload&userType=individual"

SOURCE_METADATA = {
    "SPX": {
        "provider": "Wikipedia",
        "method": "html_table",
        "source_url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "expected_count": 500,
    },
    "NDX": {
        "provider": "Nasdaq",
        "method": "static_html_table",
        "source_url": NDX_URL,
        "expected_count": 100,
    },
    "SOX": {
        "provider": "Nasdaq OMX",
        "method": "export_weightings",
        "source_url": SOX_EXPORT_URL_TEMPLATE,
        "expected_count": 30,
    },
    "RUSSELL1000": {
        "provider": "BlackRock/iShares",
        "method": "fund_document_api_holdings",
        "source_url": BLACKROCK_IWB_API_URL,
        "expected_count": 1000,
    },
    "RUSSELL2000": {
        "provider": "BlackRock/iShares",
        "method": "fund_document_api_holdings",
        "source_url": BLACKROCK_IWM_API_URL,
        "expected_count": 2000,
    },
}


class UniverseStatus(str, Enum):
    FETCHED = "fetched"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class FetchResponse:
    content: bytes
    final_url: str
    status_code: int | None
    content_type: str


@dataclass(frozen=True)
class UniverseResult:
    tickers: set[str]
    resolved_url: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class UniverseSource:
    name: str
    fetcher: Callable[[], UniverseResult]


@dataclass(frozen=True)
class UniverseFetchSummary:
    name: str
    count: int
    status: UniverseStatus
    fetched_at: str
    error: str | None = None
    resolved_url: str | None = None
    notes: str | None = None

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
    if value is None or pd.isna(value):
        return ""
    ticker = str(value).strip().upper()
    ticker = re.sub(r"\s+", "", ticker)
    ticker = ticker.replace(".", "-")
    if ticker in {"-", "—", "N/A", "NA", "NAN", "NONE", "CASH", "USD", "US DOLLAR"}:
        return ""
    return ticker


def _request(url: str, headers: dict[str, str] | None = None) -> Request:
    request_headers = dict(BROWSER_HEADERS)
    if headers:
        request_headers.update(headers)
    return Request(url, headers=request_headers)


def _response_content_type(headers: Any) -> str:
    return str(headers.get("Content-Type") or headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _snippet(content: bytes, limit: int = 300) -> str:
    return content[:limit].decode("utf-8", errors="replace").replace("\x00", "\\0")


def _debug_slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")[:120] or "response"


def _write_debug_response(label: str, response: FetchResponse | None, error: Exception | None = None) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = DEBUG_DIR / f"{timestamp}_{_debug_slug(label)}"
    meta_path = base.with_suffix(".json")
    raw_path = base.with_suffix(".bin")
    if response is not None:
        raw_path.write_bytes(response.content)
    meta = {
        "label": label,
        "status_code": response.status_code if response else None,
        "content_type": response.content_type if response else None,
        "final_url": response.final_url if response else None,
        "snippet": _snippet(response.content) if response else None,
        "error": str(error) if error else None,
        "raw_path": str(raw_path.relative_to(ROOT_DIR)) if response is not None else None,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta_path


def _format_fetch_debug(response: FetchResponse | None, error: Exception | None = None) -> str:
    parts = []
    if response is not None:
        parts.extend([
            f"status_code={response.status_code}",
            f"content_type={response.content_type or '<none>'}",
            f"final_url={response.final_url}",
            f"snippet={_snippet(response.content)!r}",
        ])
    if error is not None:
        parts.append(f"error={error}")
    return "; ".join(parts)


def _download_response(url: str, timeout: int = 90, headers: dict[str, str] | None = None) -> FetchResponse:
    try:
        with urlopen(_request(url, headers), timeout=timeout) as response:
            content = response.read()
            return FetchResponse(content, response.geturl(), getattr(response, "status", None), _response_content_type(response.headers))
    except HTTPError as exc:
        content = exc.read()
        response = FetchResponse(content, exc.geturl(), exc.code, _response_content_type(exc.headers))
        _write_debug_response("http_error", response, exc)
        raise RuntimeError(f"HTTP fetch failed for {url}: {_format_fetch_debug(response, exc)}") from exc
    except URLError as exc:
        _write_debug_response("url_error", None, exc)
        raise RuntimeError(f"HTTP fetch failed for {url}: {_format_fetch_debug(None, exc)}") from exc


def _download(url: str, timeout: int = 90, headers: dict[str, str] | None = None) -> tuple[bytes, str]:
    response = _download_response(url, timeout=timeout, headers=headers)
    return response.content, response.final_url


def _download_with_retries(url: str, timeout: int = 120, attempts: int = 3, headers: dict[str, str] | None = None, label: str = "fetch") -> FetchResponse:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            return _download_response(url, timeout=timeout, headers=headers)
        except Exception as exc:  # noqa: BLE001 - retries are diagnostic/backoff only.
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"{label} failed after {attempts} attempts; " + " | ".join(errors))


def _read_html_tables(url: str) -> tuple[list[pd.DataFrame], str]:
    response = _download_with_retries(url, timeout=120, attempts=3, label="html_table")
    return pd.read_html(io.BytesIO(response.content)), response.final_url


def _tickers_from_wikipedia_table(url: str, required_column: str) -> UniverseResult:
    tables, resolved_url = _read_html_tables(url)
    for table in tables:
        if required_column in table.columns:
            tickers = {ticker for ticker in (normalize_ticker(v) for v in table[required_column].tolist()) if ticker}
            return UniverseResult(tickers, resolved_url=resolved_url)
    raise RuntimeError(f"Could not find column {required_column!r} at {url}")


def fetch_spx() -> UniverseResult:
    return _tickers_from_wikipedia_table(str(SOURCE_METADATA["SPX"]["source_url"]), "Symbol")


def fetch_ndx() -> UniverseResult:
    response = _download_with_retries(NDX_URL, timeout=180, attempts=4, label="NDX")
    html = response.content
    resolved_url = response.final_url
    html_text = html.decode("utf-8", errors="replace")
    discovered_headers: list[list[str]] = []
    try:
        tables = pd.read_html(io.BytesIO(html))
    except ValueError:
        tables = []
    for table in tables:
        discovered_headers.append([str(column) for column in table.columns])
        columns = {str(column).strip().lower(): column for column in table.columns}
        if "symbol" in columns and "company name" in columns:
            tickers = {ticker for ticker in (normalize_ticker(v) for v in table[columns["symbol"]].tolist()) if ticker}
            if tickers:
                last_updated = _extract_last_updated_text(html_text)
                return UniverseResult(tickers, resolved_url=resolved_url, notes=last_updated)
    tickers = _extract_ndx_with_beautifulsoup(html_text)
    if tickers:
        last_updated = _extract_last_updated_text(html_text)
        return UniverseResult(tickers, resolved_url=resolved_url, notes=last_updated)
    discovered_headers.extend(_ndx_table_headers(html_text))
    debug_path = _write_debug_response("ndx_table_not_found", response)
    raise RuntimeError(
        "Nasdaq-100 static HTML table with Symbol and Company Name columns was not found; "
        f"headers={discovered_headers}; debug={debug_path.relative_to(ROOT_DIR)}"
    )


def _extract_last_updated_text(html: str) -> str | None:
    match = re.search(r"Last\s+updated[^<\n\r]*", html, flags=re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _normalize_header_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _ndx_table_headers(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[list[str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        cells = rows[0].find_all(["th", "td"])
        discovered.append([re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip() for cell in cells])
    return discovered


def _extract_ndx_with_beautifulsoup(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = rows[0].find_all(["th", "td"])
        headers = [_normalize_header_text(cell.get_text(" ", strip=True)) for cell in first_cells]
        if len(headers) < 2 or headers[0] != "symbol" or headers[1] != "company name":
            continue
        tickers: set[str] = set()
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            ticker = normalize_ticker(cells[0].get_text(" ", strip=True))
            if ticker:
                tickers.add(ticker)
        if tickers:
            return tickers
    return set()


def _dataframe_tickers(df: pd.DataFrame, preferred_columns: tuple[str, ...] = ("ticker", "symbol")) -> set[str]:
    columns = {_normalize_header_text(column): column for column in df.columns}
    column = next((columns[_normalize_header_text(name)] for name in preferred_columns if _normalize_header_text(name) in columns), None)
    if column is None:
        raise RuntimeError(f"No ticker column found in columns: {list(df.columns)}")
    tickers: set[str] = set()
    for _, row in df.iterrows():
        row_dict = {str(k): "" if pd.isna(v) else str(v) for k, v in row.to_dict().items()}
        ticker = normalize_ticker(row[column])
        if ticker and _is_equity_holding(row_dict):
            tickers.add(ticker)
    return tickers



def _is_spreadsheetml_response(content: bytes) -> bool:
    text_start = content[:8192].decode("utf-8-sig", errors="replace")
    return (
        "urn:schemas-microsoft-com:office:spreadsheet" in text_start
        and re.search(r"<(?:[A-Za-z0-9_.-]+:)?Workbook\b", text_start) is not None
    )


def _is_excel_response(response: FetchResponse, url: str) -> bool:
    lower_url = url.lower()
    content_type = response.content_type
    text_start = response.content[:500].decode("utf-8", errors="replace").lower()
    return (
        lower_url.endswith((".xls", ".xlsx"))
        or response.content[:2] == b"PK"
        or response.content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        or _is_spreadsheetml_response(response.content)
        or "spreadsheet" in content_type
        or "excel" in content_type
        or content_type in {"application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    )



def _spreadsheetml_cell_text(cell: ET.Element) -> str:
    data = cell.find("{urn:schemas-microsoft-com:office:spreadsheet}Data")
    if data is not None:
        return "" if data.text is None else str(data.text)
    return "".join(cell.itertext()).strip()


def _decode_spreadsheetml_content(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8-sig", errors="replace")


def _remove_invalid_xml_control_chars(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)


def _escape_bare_xml_ampersands(text: str) -> str:
    return re.sub(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z_:][A-Za-z0-9_.:-]*;)", "&amp;", text)


def _sanitize_spreadsheetml_xml(text: str) -> str:
    return _escape_bare_xml_ampersands(_remove_invalid_xml_control_chars(text))


def _parse_spreadsheetml_root(content: bytes) -> tuple[ET.Element, list[str]]:
    notes: list[str] = []
    text = _decode_spreadsheetml_content(content)
    try:
        return ET.fromstring(text), ["parser_used=ElementTree raw"]
    except ParseError as exc:
        original_error = str(exc)
        notes.append("raw XML parse failed")
        notes.append("sanitized XML parse attempted")
        notes.append(f"original_parse_error={original_error}")

    sanitized_text = _sanitize_spreadsheetml_xml(text)
    try:
        root = ET.fromstring(sanitized_text)
        notes.append("parser_used=ElementTree sanitized")
        return root, notes
    except ParseError as sanitized_exc:
        if LET is None:
            raise RuntimeError(
                f"SpreadsheetML XML parse failed after sanitization; original_parse_error={original_error}; "
                f"sanitized_parse_error={sanitized_exc}"
            ) from sanitized_exc
        parser = LET.XMLParser(recover=True, resolve_entities=False)
        lxml_root = LET.fromstring(sanitized_text.encode("utf-8"), parser=parser)
        recovered_text = LET.tostring(lxml_root, encoding="unicode")
        notes.append("parser_used=lxml recover")
        root = ET.fromstring(recovered_text)
        return root, notes


def _read_spreadsheetml_workbook(content: bytes) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    root, parse_notes = _parse_spreadsheetml_root(content)
    spreadsheet_ns = "urn:schemas-microsoft-com:office:spreadsheet"
    office_ns = "urn:schemas-microsoft-com:office:office"
    worksheets: dict[str, pd.DataFrame] = {}
    worksheet_names: list[str] = []
    for index, worksheet in enumerate(root.findall(f"{{{spreadsheet_ns}}}Worksheet"), start=1):
        name = worksheet.get(f"{{{spreadsheet_ns}}}Name") or worksheet.get(f"{{{office_ns}}}Name") or f"Sheet{index}"
        worksheet_names.append(name)
        table = worksheet.find(f"{{{spreadsheet_ns}}}Table")
        rows: list[list[str]] = []
        if table is not None:
            for row in table.findall(f"{{{spreadsheet_ns}}}Row"):
                values: list[str] = []
                for cell in row.findall(f"{{{spreadsheet_ns}}}Cell"):
                    cell_index = cell.get(f"{{{spreadsheet_ns}}}Index")
                    if cell_index and cell_index.isdigit():
                        while len(values) < int(cell_index) - 1:
                            values.append("")
                    values.append(_spreadsheetml_cell_text(cell))
                rows.append(values)
        width = max((len(row) for row in rows), default=0)
        padded_rows = [row + [""] * (width - len(row)) for row in rows]
        worksheets[name] = pd.DataFrame(padded_rows)
    if not worksheets:
        raise RuntimeError("SpreadsheetML workbook did not contain readable worksheets")
    return worksheets, worksheet_names, parse_notes


def _detect_ticker_column_name(df: pd.DataFrame, preferred_columns: tuple[str, ...] = ("ticker", "symbol")) -> str:
    columns = {_normalize_header_text(column): column for column in df.columns}
    column = next((columns[_normalize_header_text(name)] for name in preferred_columns if _normalize_header_text(name) in columns), None)
    if column is None:
        raise RuntimeError(f"No ticker column found in columns: {list(df.columns)}")
    return str(column)


def _read_spreadsheetml_holdings(content: bytes, label: str) -> tuple[pd.DataFrame, str]:
    worksheets, worksheet_names, parse_notes = _read_spreadsheetml_workbook(content)
    selected_name: str | None = None
    selected_df: pd.DataFrame | None = None
    if "holdings" in {name.lower() for name in worksheet_names}:
        names = {name.lower(): name for name in worksheet_names}
        selected_name = names["holdings"]
        selected_df = worksheets[selected_name]
    else:
        scan_errors: list[str] = []
        for name, sheet_df in worksheets.items():
            try:
                _promote_detected_header(sheet_df, {"ticker"}, f"{label} {name}")
            except Exception as exc:  # noqa: BLE001 - continue scanning workbook sheets.
                scan_errors.append(f"{name}: {exc}")
                continue
            selected_name = name
            selected_df = sheet_df
            break
        if selected_df is None:
            raise RuntimeError(f"SpreadsheetML workbook has no worksheet containing a Ticker-like column; worksheets={worksheet_names}; scan_errors={scan_errors}")
    note_parts = ["workbook_type=spreadsheetml_xml", *parse_notes, f"worksheet_names={worksheet_names}", f"selected_worksheet={selected_name}"]
    return selected_df, "; ".join(note_parts)

def _read_excel_response(content: bytes, preferred_sheet: str | None = None) -> pd.DataFrame:
    read_kwargs: dict[str, Any] = {"header": None}
    if preferred_sheet:
        try:
            return pd.read_excel(io.BytesIO(content), sheet_name=preferred_sheet, **read_kwargs)
        except ValueError:
            pass
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, **read_kwargs)
    if not sheets:
        raise RuntimeError("Excel workbook did not contain readable sheets")
    if preferred_sheet and preferred_sheet.lower() in {name.lower(): name for name in sheets}:
        names = {name.lower(): name for name in sheets}
        return sheets[names[preferred_sheet.lower()]]
    return next(iter(sheets.values()))


def _read_delimited_response(content: bytes, required_column: str = "Ticker") -> pd.DataFrame:
    text = content.decode("utf-8-sig", errors="replace")
    if "\x00" in text[:1000]:
        raise RuntimeError("text response contains NUL bytes; likely binary/encoded data")
    lines = text.splitlines()
    header_idx = _find_csv_header_index(lines, required_column)
    csv_text = "\n".join(lines[header_idx:]) if header_idx is not None else text
    return pd.read_csv(io.StringIO(csv_text), sep=None, engine="python", header=None)


def _read_tabular_response(response: FetchResponse, url: str, required_column: str = "Ticker", preferred_sheet: str | None = None) -> pd.DataFrame:
    if _is_excel_response(response, url):
        try:
            return _read_excel_response(response.content, preferred_sheet=preferred_sheet)
        except Exception:
            text_start = response.content[:1000].decode("utf-8", errors="replace").lower()
            if "<table" in text_start:
                tables = pd.read_html(io.BytesIO(response.content), header=None)
                if tables:
                    return tables[0]
            if "\x00" not in text_start and any(delimiter in text_start for delimiter in [",", "\t", ";"]):
                return _read_delimited_response(response.content, required_column=required_column)
            raise
    text_start = response.content[:500].decode("utf-8", errors="replace").lower()
    if "<html" in text_start or not response.content.strip():
        debug_path = _write_debug_response("unexpected_tabular_response", response)
        raise RuntimeError(f"empty/HTML response instead of table; {_format_fetch_debug(response)}; debug={debug_path.relative_to(ROOT_DIR)}")
    return _read_delimited_response(response.content, required_column=required_column)


def _header_score(values: list[object], accepted_labels: set[str]) -> int:
    normalized = {_normalize_header_text(value) for value in values if not pd.isna(value)}
    return sum(1 for label in accepted_labels if label in normalized)


def _promote_detected_header(df: pd.DataFrame, labels: set[str], label: str) -> tuple[pd.DataFrame, int, list[str], list[list[str]]]:
    preview = df.head(20).fillna("").astype(str).values.tolist()
    best_index: int | None = None
    best_score = 0
    for index, row in df.head(20).iterrows():
        values = row.tolist()
        score = _header_score(values, labels)
        if score > best_score:
            best_score = score
            best_index = int(index)
    if best_index is None or best_score == 0:
        raise RuntimeError(f"{label} header row not found in first 20 rows; preview={preview}")
    columns = [re.sub(r"\s+", " ", str(value)).strip() for value in df.iloc[best_index].tolist()]
    promoted = df.iloc[best_index + 1 :].copy()
    promoted.columns = columns
    promoted = promoted.dropna(how="all")
    return promoted, best_index, columns, preview

def fetch_sox() -> UniverseResult:
    errors: list[str] = []
    for offset in range(8):
        trade_date = date.today() - timedelta(days=offset)
        trade_date_text = trade_date.isoformat()
        url = SOX_EXPORT_URL_TEMPLATE.format(trade_date=trade_date_text)
        try:
            response = _download_with_retries(url, timeout=120, attempts=2, label=f"SOX {trade_date_text}")
            resolved_url = response.final_url
            raw_df = _read_tabular_response(response, resolved_url, required_column="Ticker")
            df, header_index, columns, preview = _promote_detected_header(
                raw_df,
                {"ticker", "symbol", "security symbol", "index share", "name", "weight"},
                "SOX",
            )
            tickers = _dataframe_tickers(df, preferred_columns=("ticker", "symbol", "security symbol"))
            if not tickers:
                raise RuntimeError("no SOX tickers parsed")
            return UniverseResult(
                tickers,
                resolved_url=resolved_url,
                notes=f"tradeDate={trade_date_text}; header_row={header_index}; columns={columns}; preview_first_20={preview}",
            )
        except Exception as exc:  # noqa: BLE001 - try recent prior trading dates.
            errors.append(f"{trade_date_text}: {exc}")
    raise RuntimeError("No valid SOX weighting export found in the last 8 calendar days; " + "; ".join(errors))


def _is_equity_holding(row: dict[str, str]) -> bool:
    normalized = {str(k).strip().lower(): str(v or "").strip() for k, v in row.items()}
    asset_class = normalized.get("asset class", "").lower()
    if asset_class and asset_class not in {"equity", "stock"}:
        return False
    text = " ".join(normalized.values()).lower()
    excluded_terms = ("cash", "money market", "treasury", "collateral", "future", "futures", "swap", "option", "derivative", "receivable", "payable")
    return not any(term in text for term in excluded_terms)


def _extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            urls.extend(_extract_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_extract_urls(item))
    elif isinstance(value, str):
        urls.extend(re.findall(r"https?://[^\s\"'<>]+", value))
        if re.search(r"\.(csv|xls|xlsx)(?:\?|$)", value, flags=re.IGNORECASE) or "fileType=" in value:
            urls.append(urljoin("https://www.blackrock.com", value))
    return list(dict.fromkeys(urls))


def _json_content_type(content_type: str) -> bool:
    return content_type == "application/json" or content_type.endswith("+json") or "json" in content_type


def _is_json_like_response(response: FetchResponse) -> bool:
    text = response.content[:200].decode("utf-8-sig", errors="replace").lstrip()
    return _json_content_type(response.content_type) or text.startswith(("{", "["))


def _load_json_response(response: FetchResponse, label: str) -> Any:
    text = response.content.decode("utf-8-sig", errors="replace")
    if not _json_content_type(response.content_type) and not text.lstrip().startswith(("{", "[")):
        debug_path = _write_debug_response(label, response)
        raise RuntimeError(f"non-JSON response; {_format_fetch_debug(response)}; debug={debug_path.relative_to(ROOT_DIR)}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        debug_path = _write_debug_response(label, response, exc)
        raise RuntimeError(f"JSON parse failed; {_format_fetch_debug(response, exc)}; debug={debug_path.relative_to(ROOT_DIR)}") from exc


def _resolve_blackrock_holdings(api_url: str) -> tuple[str, FetchResponse | None]:
    response = _download_with_retries(api_url, timeout=120, attempts=3, label="BlackRock document API")
    if not _is_json_like_response(response):
        if _is_excel_response(response, response.final_url) or response.content.lstrip().lower().startswith((b"ticker", b"security")):
            return response.final_url, response
        debug_path = _write_debug_response("blackrock_document_api_unexpected", response)
        raise RuntimeError(f"BlackRock fund document API returned unsupported direct content; {_format_fetch_debug(response)}; debug={debug_path.relative_to(ROOT_DIR)}")
    payload = _load_json_response(response, "blackrock_document_api")
    urls = _extract_urls(payload)
    candidates = [url for url in urls if re.search(r"(holdings|fund|download|csv|xls|xlsx|fileType=)", url, flags=re.IGNORECASE)] or urls
    if not candidates:
        debug_path = _write_debug_response("blackrock_no_holdings_url", response)
        raise RuntimeError(f"BlackRock fund document API did not include a holdings download URL; debug={debug_path.relative_to(ROOT_DIR)}")
    return candidates[0], None


def _read_holdings_file(response: FetchResponse, url: str) -> tuple[pd.DataFrame, str]:
    try:
        if _is_spreadsheetml_response(response.content):
            return _read_spreadsheetml_holdings(response.content, "BlackRock holdings")
        return _read_tabular_response(response, url, required_column="Ticker", preferred_sheet="Holdings"), "workbook_type=standard_tabular"
    except Exception as exc:  # noqa: BLE001 - include raw response diagnostics.
        debug_path = _write_debug_response("blackrock_holdings_parse_failed", response, exc)
        raise RuntimeError(f"Could not parse BlackRock holdings file; {_format_fetch_debug(response, exc)}; debug={debug_path.relative_to(ROOT_DIR)}") from exc


def _find_csv_header_index(lines: list[str], required_column: str) -> int | None:
    for index, line in enumerate(lines):
        try:
            columns = next(csv.reader([line]))
        except csv.Error:
            continue
        normalized_columns = {column.strip().lower() for column in columns}
        if required_column.lower() in normalized_columns:
            return index
    return None


def _fetch_blackrock_holdings(universe_name: str) -> UniverseResult:
    api_url = str(SOURCE_METADATA[universe_name]["source_url"])
    holdings_url, direct_response = _resolve_blackrock_holdings(api_url)
    response = direct_response or _download_with_retries(holdings_url, timeout=180, attempts=3, label=f"{universe_name} holdings")
    resolved_url = response.final_url
    raw_df, parse_notes = _read_holdings_file(response, resolved_url)
    df, header_index, columns, _preview = _promote_detected_header(raw_df, {"ticker"}, f"{universe_name} BlackRock holdings")
    ticker_column = _detect_ticker_column_name(df, preferred_columns=("ticker",))
    tickers = _dataframe_tickers(df, preferred_columns=("ticker",))
    if not tickers:
        raise RuntimeError(f"No equity tickers parsed for {universe_name}")
    mode = "direct_document_api_response" if direct_response else "resolved_holdings_url"
    return UniverseResult(tickers, resolved_url=resolved_url, notes=f"{mode}; holdings_url={holdings_url}; {parse_notes}; header_row={header_index}; ticker_column={ticker_column}; columns={columns}")


def fetch_russell1000() -> UniverseResult:
    return _fetch_blackrock_holdings("RUSSELL1000")


def fetch_russell2000() -> UniverseResult:
    return _fetch_blackrock_holdings("RUSSELL2000")


def fetch_universe_membership() -> tuple[dict[str, set[str]], list[UniverseFetchSummary]]:
    sources = [
        UniverseSource("SPX", fetch_spx),
        UniverseSource("NDX", fetch_ndx),
        UniverseSource("SOX", fetch_sox),
        UniverseSource("RUSSELL1000", fetch_russell1000),
        UniverseSource("RUSSELL2000", fetch_russell2000),
    ]
    membership: dict[str, set[str]] = defaultdict(set)
    summaries: list[UniverseFetchSummary] = []
    for source in sources:
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        resolved_url = None
        notes = None
        try:
            result = source.fetcher()
            tickers = result.tickers
            resolved_url = result.resolved_url
            notes = result.notes
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
        summaries.append(UniverseFetchSummary(source.name, len(tickers), status, fetched_at, error, resolved_url, notes))
        meta = SOURCE_METADATA[source.name]
        print(f"{status.value.title()} {len(tickers):,} {source.name} tickers ({meta['provider']} / {meta['method']})")
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
        rows.append({"ticker": ticker, "company_name": meta.get("company_name"), "universe": ";".join(sorted(membership[ticker])), "sector": meta.get("sector"), "industry": meta.get("industry"), "description": description, "description_short": derive_description_short(description), "updated_at": updated_at})
    return rows


def write_sqlite(rows: list[dict[str, str | None]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DROP TABLE IF EXISTS companies")
        conn.execute("""
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
            """)
        conn.executemany("""
            INSERT INTO companies (
                ticker, company_name, universe, sector, industry, description,
                description_short, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [tuple(row[column] for column in EXPECTED_COLUMNS) for row in rows])


def write_csv(rows: list[dict[str, str | None]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_universe_audit(summaries: list[UniverseFetchSummary]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    audit_rows = [{"universe": summary.name, "provider": summary.provider, "method": summary.method, "source_url": summary.source_url, "resolved_url": summary.resolved_url, "status": summary.status.value, "count": summary.count, "fetched_at": summary.fetched_at, "error": summary.error, "notes": summary.notes} for summary in summaries]
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
