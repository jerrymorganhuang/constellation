# Market Data Foundation

Constellation V1 stores ticker-level market data only; this phase does not merge duplicate share classes, change graph projection, or touch Neo4j/backend/frontend behavior.

## Sources

- **Daily bars:** Alpaca Market Data historical stock bars, defaulting to the free `iex` feed. IEX volume is not consolidated U.S. volume; it is sufficient here for relative liquidity comparisons among duplicate share classes.
- **Market cap:** Yahoo Finance via `yfinance`, used only for current `market_cap`.

Alpaca bars support adjustment modes such as `raw` and `all`, but a response contains only one adjusted view. The pipeline fetches raw bars for displayed close, volume, and raw-close dollar volume, then separately requests `adjustment=all` to populate `adjusted_close` when available. Return metrics use `adjusted_close`; if unavailable they fall back to raw `close` and rows are marked with a data source suffix documenting the limitation.

## Tables and indexes

`market_prices(ticker, trade_date, close, adjusted_close, volume, data_source, updated_at)` with primary key `(ticker, trade_date)`, plus indexes on `(ticker, trade_date)` and `(trade_date)`.

`market_snapshot(ticker, as_of_date, close, market_cap, return_1d, return_5d, return_30d, avg_volume_30d, avg_dollar_volume_30d, price_data_source, market_cap_data_source, price_updated_at, market_cap_updated_at, updated_at)`.

## Metrics

All return periods are trading-session offsets, not calendar days. `return_1d`, `return_5d`, and `return_30d` compare the latest adjusted close to T-1, T-5, and T-30 respectively. A 30-day return requires at least 31 observations. `avg_volume_30d` is the arithmetic mean of the latest 30 volumes. `avg_dollar_volume_30d` is the arithmetic mean of raw `close * volume` over the latest 30 sessions.

## Retention, failure behavior, and retries

After price upserts, each ticker is pruned deterministically to its latest 250 stored trading sessions. Fewer than 250 sessions are retained fully. Failed tickers do not delete existing price rows or snapshots. Market-cap failures never overwrite a previous valid market cap. Audit artifacts are written under `data/market_data/`: `market_price_failures.csv`, `market_cap_failures.csv`, and `market_update_summary.json`.

Retry examples:

```bash
python scripts/update_market_prices.py --incremental --retry-file data/market_data/market_price_failures.csv
python scripts/build_market_snapshot.py --market-cap-only --retry-file data/market_data/market_cap_failures.csv
```

## Credentials

The root `.env` is loaded from `~/constellation/.env`. Required keys are:

```dotenv
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
ALPACA_DATA_FEED=iex
```

The repository keeps `.env` ignored by Git. Fill credentials manually on the VM; never paste secrets into chat or commit them. `ALPACA_DATA_FEED=iex` is the expected V1 value. Feed precedence is deterministic: explicit CLI `--feed` > `ALPACA_DATA_FEED` loaded from the root `.env` > fallback `iex`. Supported feed values are `iex` and `sip`. `APCA_API_BASE_URL` is not required because the implementation calls Alpaca's official market-data endpoint directly.

## CLI

```bash
python scripts/update_market_prices.py --dry-run --limit 5
python scripts/update_market_prices.py --backfill
python scripts/update_market_prices.py --incremental
python scripts/build_market_snapshot.py
```

Use `--ticker`, `--limit`, `--batch-size`, `--start`, `--end`, `--feed`, `--retry-file`, `--skip-market-cap`, and `--market-cap-only` for bounded validation and retries.
