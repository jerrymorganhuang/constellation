#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json, sqlite3
from datetime import date, timedelta
from pathlib import Path
from market_data_common import *


def parse_args():
    p=argparse.ArgumentParser(description='Update Constellation market price history from Alpaca daily bars.')
    g=p.add_mutually_exclusive_group(); g.add_argument('--backfill',action='store_true'); g.add_argument('--incremental',action='store_true'); g.add_argument('--dry-run',action='store_true')
    p.add_argument('--db',default=str(DEFAULT_DB)); p.add_argument('--companies',default=str(DEFAULT_COMPANIES)); p.add_argument('--ticker',action='append'); p.add_argument('--limit',type=int)
    p.add_argument('--batch-size',type=int,default=100); p.add_argument('--start'); p.add_argument('--end'); p.add_argument('--feed',default=None); p.add_argument('--retry-file')
    p.add_argument('--no-adjusted',action='store_true',help='Store raw closes only; returns will fall back to raw close until adjusted_close is populated.')
    return p.parse_args()

def selected_tickers(args):
    if args.retry_file:
        with open(args.retry_file,newline='') as f: vals=[r['ticker'].strip().upper() for r in csv.DictReader(f) if r.get('ticker')]
    elif args.ticker: vals=[t.strip().upper() for t in args.ticker]
    else: vals=canonical_tickers(Path(args.companies))
    vals=list(dict.fromkeys(vals)); return vals[:args.limit] if args.limit else vals

def default_dates(args):
    end=args.end or date.today().isoformat()
    if args.start: return args.start,end
    days=430 if args.backfill or args.dry_run else 14
    return (date.fromisoformat(end)-timedelta(days=days)).isoformat(), end

def main():
    args=parse_args(); ensure_env_placeholders(); feed=resolve_alpaca_feed(args.feed); print(f'Alpaca data feed: {feed}'); tickers=selected_tickers(args); start,end=default_dates(args)
    if not tickers: raise SystemExit('No tickers selected from canonical company universe/retry file.')
    status='PASS'; failures=[]; written=0
    try: require_alpaca_credentials()
    except MarketDataError as e: raise SystemExit(str(e))
    if args.dry_run: print('DRY RUN: no database writes will be performed')
    db=sqlite3.connect(args.db); db.row_factory=sqlite3.Row; ensure_tables(db); before=db.total_changes
    for i in range(0,len(tickers),args.batch_size):
        batch=tickers[i:i+args.batch_size]
        try:
            raw=fetch_alpaca_bars(batch,start,end,feed,'raw')
            adjusted=None if args.no_adjusted else fetch_alpaca_bars(batch,start,end,feed,'all')
            bars=to_price_bars(raw, adjusted, f'alpaca_{feed}_raw' + ('' if adjusted else '_adjusted_unavailable'))
            if args.dry_run:
                print(json.dumps({'feed':feed,'adjustment_behavior':'raw fetched for close/volume; adjustment=all fetched separately for adjusted_close' if adjusted else 'raw only; adjusted_close unavailable','tickers':batch,'bar_count':len(bars),'sample':[b.__dict__ for b in bars[:10]]}, indent=2))
            else:
                written+=upsert_prices(db,bars); prune_prices(db,batch); rebuild_snapshots(db,batch,set(canonical_tickers(Path(args.companies))))
            missing=[t for t in batch if not raw.get(t)]
            failures += [{'ticker':t,'stage':'price','reason':'no_bars_returned','attempt_count':1,'created_at':utc_now()} for t in missing]
        except Exception as e:
            status='PARTIAL'; failures += [{'ticker':t,'stage':'price','reason':type(e).__name__,'attempt_count':1,'created_at':utc_now()} for t in batch]
            print(f'Batch failed for {batch[0]}..: {type(e).__name__}')
    if args.dry_run and db.total_changes != before: raise SystemExit('Dry run modified SQLite unexpectedly')
    append_failures(PRICE_FAILURES, failures)
    if failures and status=='PASS': status='PARTIAL'
    AUDIT_DIR.mkdir(parents=True, exist_ok=True); SUMMARY_JSON.write_text(json.dumps({'status':status,'tickers':len(tickers),'price_rows_upserted':written,'failures':len(failures),'dry_run':args.dry_run,'start':start,'end':end}, indent=2, sort_keys=True)+"\n")
    print(f'{status}: tickers={len(tickers)} price_rows_upserted={written} failures={len(failures)} summary={SUMMARY_JSON}')
if __name__=='__main__': main()
