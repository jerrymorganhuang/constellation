#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, sqlite3
from pathlib import Path
from typing import Any
from market_data_common import *
try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

def yahoo_market_cap(ticker: str) -> float | None:
    if yf is None: raise MarketDataError('yfinance is not installed')
    val = yf.Ticker(ticker).fast_info.get('market_cap')
    return float(val) if val and float(val) > 0 else None

def refresh_market_caps(conn, tickers, fetcher=yahoo_market_cap):
    failures=[]; now=utc_now()
    with conn:
        for t in tickers:
            try: mc=fetcher(t)
            except Exception as e:
                failures.append({'ticker':t,'stage':'market_cap','reason':type(e).__name__,'attempt_count':1,'created_at':now}); continue
            if not mc or mc <= 0:
                failures.append({'ticker':t,'stage':'market_cap','reason':'missing_or_invalid_market_cap','attempt_count':1,'created_at':now}); continue
            conn.execute("""INSERT INTO market_snapshot(ticker,market_cap,market_cap_data_source,market_cap_updated_at,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET market_cap=excluded.market_cap,
            market_cap_data_source=excluded.market_cap_data_source, market_cap_updated_at=excluded.market_cap_updated_at, updated_at=excluded.updated_at""",(t,mc,'yahoo_yfinance',now,now))
    append_failures(CAP_FAILURES, failures); return failures

def retry_tickers(path: str) -> list[str]:
    with open(path,newline='') as f: return list(dict.fromkeys(r['ticker'].strip().upper() for r in csv.DictReader(f) if r.get('ticker')))

def main():
    p=argparse.ArgumentParser(description='Build market_snapshot metrics and optionally refresh Yahoo market caps.')
    p.add_argument('--db',default=str(DEFAULT_DB)); p.add_argument('--companies',default=str(DEFAULT_COMPANIES)); p.add_argument('--ticker',action='append'); p.add_argument('--limit',type=int); p.add_argument('--retry-file'); p.add_argument('--market-cap-only',action='store_true'); p.add_argument('--skip-market-cap',action='store_true')
    args=p.parse_args(); ensure_env_placeholders(); conn=sqlite3.connect(args.db); conn.row_factory=sqlite3.Row; ensure_tables(conn)
    tickers = retry_tickers(args.retry_file) if args.retry_file else ([t.upper() for t in args.ticker] if args.ticker else canonical_tickers(Path(args.companies)))
    tickers=list(dict.fromkeys(tickers)); tickers=tickers[:args.limit] if args.limit else tickers
    rebuilt=0 if args.market_cap_only else rebuild_snapshots(conn,tickers,set(canonical_tickers(Path(args.companies))))
    cap_failures=[] if args.skip_market_cap else refresh_market_caps(conn,tickers)
    print(f'PASS' + (' with market-cap failures' if cap_failures else '') + f': snapshots_rebuilt={rebuilt} market_cap_failures={len(cap_failures)}')
if __name__=='__main__': main()
