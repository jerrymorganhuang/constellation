#!/usr/bin/env python3
"""Shared Market Data Foundation utilities for Constellation V1.

Alpaca historical stock bars can be requested with adjustment=raw, split, dividend,
or all, but a single bars response contains one OHLCV set only. We therefore fetch
raw bars for displayed close/volume/dollar-volume and, when enabled, make a second
batched request with adjustment=all for adjusted_close. If the adjusted request is
unavailable, adjusted_close remains NULL and returns explicitly fall back to raw
close with this documented limitation.
"""
from __future__ import annotations

import csv, json, os, random, sqlite3, time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "constellation.db"
DEFAULT_COMPANIES = ROOT / "data" / "companies.csv"
ENV_PATH = ROOT / ".env"
AUDIT_DIR = ROOT / "data" / "market_data"
PRICE_FAILURES = AUDIT_DIR / "market_price_failures.csv"
CAP_FAILURES = AUDIT_DIR / "market_cap_failures.csv"
SUMMARY_JSON = AUDIT_DIR / "market_update_summary.json"
ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
REQUIRED_ALPACA_ENV = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
ALPACA_FEED_ENV = "ALPACA_DATA_FEED"
SUPPORTED_ALPACA_FEEDS = {"iex", "sip"}

class MarketDataError(RuntimeError): pass

@dataclass(frozen=True)
class PriceBar:
    ticker: str; trade_date: str; close: float; volume: int; data_source: str; adjusted_close: float|None=None

def utc_now() -> str: return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def ensure_env_placeholders(path: Path = ENV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    existing = {ln.split("=",1)[0].strip() for ln in lines if "=" in ln and not ln.lstrip().startswith("#")}
    add = [f"{k}=" for k in REQUIRED_ALPACA_ENV if k not in existing]
    if ALPACA_FEED_ENV not in existing:
        add.append(f"{ALPACA_FEED_ENV}=iex")
    if add:
        prefix = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + prefix + "\n".join(add) + "\n")

def load_root_env(path: Path = ENV_PATH) -> None:
    load_dotenv(dotenv_path=path, override=False)

def resolve_alpaca_feed(cli_feed: str | None = None, path: Path = ENV_PATH) -> str:
    load_root_env(path)
    feed = (cli_feed if cli_feed is not None else os.getenv(ALPACA_FEED_ENV, "iex")).strip().lower()
    if feed not in SUPPORTED_ALPACA_FEEDS:
        raise MarketDataError(f"Invalid Alpaca data feed {feed!r}. Supported values: iex, sip.")
    return feed

def require_alpaca_credentials(path: Path = ENV_PATH) -> tuple[str,str]:
    load_root_env(path)
    missing = [k for k in REQUIRED_ALPACA_ENV if not os.getenv(k, "").strip()]
    if missing:
        raise MarketDataError("Missing required Alpaca credential environment variable(s): " + ", ".join(missing) + f". Fill them manually in {path}.")
    return os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_prices (
      ticker TEXT NOT NULL, trade_date TEXT NOT NULL, close REAL, adjusted_close REAL,
      volume INTEGER, data_source TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY (ticker, trade_date));
    CREATE INDEX IF NOT EXISTS idx_market_prices_ticker_date ON market_prices(ticker, trade_date);
    CREATE INDEX IF NOT EXISTS idx_market_prices_trade_date ON market_prices(trade_date);
    CREATE TABLE IF NOT EXISTS market_snapshot (
      ticker TEXT PRIMARY KEY, as_of_date TEXT, close REAL, market_cap REAL,
      return_1d REAL, return_5d REAL, return_30d REAL, avg_volume_30d REAL,
      avg_dollar_volume_30d REAL, price_data_source TEXT, market_cap_data_source TEXT,
      price_updated_at TEXT, market_cap_updated_at TEXT, updated_at TEXT);
    """)

def canonical_tickers(companies: Path = DEFAULT_COMPANIES) -> list[str]:
    with companies.open(newline="") as f:
        rows = csv.DictReader(f); vals = []
        for r in rows:
            t = (r.get("ticker") or "").strip().upper()
            if t: vals.append(t)
    return list(dict.fromkeys(vals))

def valid_bar(b: PriceBar) -> bool:
    try: date.fromisoformat(b.trade_date)
    except ValueError: return False
    return bool(b.ticker.strip()) and (b.close is None or b.close > 0) and (b.volume is None or b.volume >= 0)

def upsert_prices(conn: sqlite3.Connection, bars: Iterable[PriceBar]) -> int:
    now = utc_now(); rows=[]
    for b in bars:
        t=b.ticker.strip().upper(); nb=PriceBar(t,b.trade_date,b.close,b.volume,b.data_source,b.adjusted_close)
        if valid_bar(nb): rows.append((t, nb.trade_date, nb.close, nb.adjusted_close, nb.volume, nb.data_source, now))
    with conn:
        conn.executemany("""INSERT INTO market_prices(ticker,trade_date,close,adjusted_close,volume,data_source,updated_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker,trade_date) DO UPDATE SET close=excluded.close,
        adjusted_close=excluded.adjusted_close, volume=excluded.volume, data_source=excluded.data_source, updated_at=excluded.updated_at""", rows)
    return len(rows)

def prune_prices(conn: sqlite3.Connection, tickers: Iterable[str], keep: int = 250) -> None:
    with conn:
        for t in tickers:
            conn.execute("""DELETE FROM market_prices WHERE ticker=? AND trade_date NOT IN (
                SELECT trade_date FROM market_prices WHERE ticker=? ORDER BY trade_date DESC LIMIT ?)""", (t,t,keep))

def compute_snapshot_for_ticker(conn: sqlite3.Connection, ticker: str) -> dict[str,Any] | None:
    rows = conn.execute("SELECT * FROM market_prices WHERE ticker=? ORDER BY trade_date", (ticker,)).fetchall()
    if not rows: return None
    vals=[dict(r) for r in rows]; latest=vals[-1]
    def px(i):
        v=vals[i]["adjusted_close"]
        return v if v is not None else vals[i]["close"]
    def ret(n):
        return (px(-1)/px(-(n+1))-1) if len(vals) >= n+1 and px(-(n+1)) else None
    last30=vals[-30:] if len(vals)>=30 else vals
    return {"ticker":ticker,"as_of_date":latest["trade_date"],"close":latest["close"],"return_1d":ret(1),"return_5d":ret(5),"return_30d":ret(30),
            "avg_volume_30d":(sum(r["volume"] for r in last30)/30 if len(vals)>=30 else None),
            "avg_dollar_volume_30d":(sum(r["close"]*r["volume"] for r in last30)/30 if len(vals)>=30 else None),
            "price_data_source":latest["data_source"],"price_updated_at":latest["updated_at"]}

def rebuild_snapshots(conn: sqlite3.Connection, tickers: Iterable[str], canonical: set[str]|None=None) -> int:
    now=utc_now(); count=0
    with conn:
        for t in tickers:
            if canonical is not None and t not in canonical: continue
            s=compute_snapshot_for_ticker(conn,t)
            if not s: continue
            old=conn.execute("SELECT market_cap,market_cap_data_source,market_cap_updated_at FROM market_snapshot WHERE ticker=?",(t,)).fetchone()
            mc=(old[0],old[1],old[2]) if old else (None,None,None)
            conn.execute("""INSERT INTO market_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET as_of_date=excluded.as_of_date, close=excluded.close,
            return_1d=excluded.return_1d, return_5d=excluded.return_5d, return_30d=excluded.return_30d,
            avg_volume_30d=excluded.avg_volume_30d, avg_dollar_volume_30d=excluded.avg_dollar_volume_30d,
            price_data_source=excluded.price_data_source, price_updated_at=excluded.price_updated_at, updated_at=excluded.updated_at""",
            (t,s['as_of_date'],s['close'],mc[0],s['return_1d'],s['return_5d'],s['return_30d'],s['avg_volume_30d'],s['avg_dollar_volume_30d'],s['price_data_source'],mc[1],s['price_updated_at'],mc[2],now)); count+=1
    return count

def append_failures(path: Path, failures: list[dict[str,Any]]) -> None:
    if not failures: return
    AUDIT_DIR.mkdir(parents=True, exist_ok=True); fields=["ticker","stage","reason","attempt_count","created_at"]
    seen=[]
    if path.exists():
        with path.open(newline="") as f: seen=list(csv.DictReader(f))
    keyed={(r['ticker'],r['stage'],r['reason']):r for r in seen}
    for f in failures:
        r={k:str(f.get(k,"")) for k in fields}; r.setdefault('created_at', utc_now()); keyed[(r['ticker'],r['stage'],r['reason'])]=r
    with path.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(sorted(keyed.values(), key=lambda r:(r['ticker'],r['stage'],r['reason'])))

def fetch_alpaca_bars(tickers:list[str], start:str, end:str, feed="iex", adjustment="raw", limit=10000, max_retries=3)->dict[str,list[dict[str,Any]]]:
    key,secret=require_alpaca_credentials(); headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret}
    params={"symbols":",".join(tickers),"timeframe":"1Day","start":start,"end":end,"feed":feed,"adjustment":adjustment,"limit":limit,"sort":"asc"}
    out={t:[] for t in tickers}; token=None; attempts=0
    while True:
        if token: params['page_token']=token
        for attempt in range(max_retries):
            r=requests.get(ALPACA_DATA_URL,headers=headers,params=params,timeout=30)
            if r.status_code in (429,500,502,503,504): time.sleep((2**attempt)+random.random()); continue
            if r.status_code>=400: raise MarketDataError(f"Alpaca bars request failed with HTTP {r.status_code}; check credentials/feed/entitlement.")
            break
        data=r.json();
        for sym,bars in data.get('bars',{}).items(): out.setdefault(sym.upper(),[]).extend(bars)
        token=data.get('next_page_token')
        if not token: return out

def to_price_bars(raw:dict[str,list[dict[str,Any]]], adjusted:dict[str,list[dict[str,Any]]]|None, source:str)->list[PriceBar]:
    adj_lookup={}
    if adjusted:
        for t,bars in adjusted.items():
            for b in bars: adj_lookup[(t.upper(), b['t'][:10])] = float(b['c'])
    out=[]
    for t,bars in raw.items():
        for b in bars:
            d=b['t'][:10]; out.append(PriceBar(t.upper(), d, float(b['c']), int(b.get('v') or 0), source, adj_lookup.get((t.upper(),d))))
    return out
