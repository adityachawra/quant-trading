# Quant Trading — Data Infrastructure & Backtesting Engine

Personal project building a systematic quant trading pipeline for Indian equities (NSE), following Ernest P. Chan's *Quantitative Trading* (2nd ed.), adapted for Indian retail markets. Currently in Phase 2 of a 90-day roadmap: infrastructure is built, first strategy (Bollinger Band mean reversion) is running through a working backtest engine.

## What this project does

- Ingests and maintains ~7 years of daily OHLCV price history for the full NSE equity/ETF/index universe (~3,800 instruments) via the Zerodha Kite Connect API
- Stores everything in a normalized PostgreSQL database (Docker-containerized)
- Runs trading strategy signals against that data through a modular, strategy-agnostic backtesting engine
- Models real Indian equity transaction costs (STT, GST, stamp duty, exchange/SEBI charges, brokerage), validated against Zerodha's own brokerage calculator
- Tracks every simulated/live trade through an append-only transaction ledger, with FIFO matching to reconstruct closed-trade P&L

## Tech stack

- **Language:** Python 3.11
- **Database:** PostgreSQL 16, via Docker Compose
- **DB GUI:** DBeaver
- **Package/env management:** `uv`
- **Broker/data API:** Zerodha Kite Connect
- **Key libraries:** pandas, NumPy, SQLAlchemy, psycopg, matplotlib, python-dotenv, Jupyter

## Setup

### Prerequisites
- Docker Desktop
- Python 3.11+ (managed via `uv`)
- A Zerodha account with Kite Connect API access (₹500/month subscription for historical data)

### 1. Start the database
From the parent folder (outside this repo, where `docker-compose.yml` lives):
```bash
docker compose up -d
```

### 2. Set up the Python environment
```bash
uv sync
```

### 3. Configure environment variables
Copy `.env.example` to `.env` and fill in real values:
```bash
cp .env.example .env
```
Required variables: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `KITE_API_KEY`, `KITE_API_SECRET`. `KITE_ACCESS_TOKEN` is generated daily (see below), not set manually.

### 4. Generate a daily access token
Kite's access tokens expire every day at 6 AM IST. Before running any script that touches live data:
```bash
uv run python data/generate_access_token.py
```
This opens a browser for you to log in (Zerodha username, password, TOTP), then automatically captures and saves the token to `.env`.

## Database schema

Six tables in Postgres:

| Table | Purpose |
|---|---|
| `instruments` | Reference list of tradeable NSE stocks/ETFs/indices |
| `daily_ohlcv` | Raw daily price history |
| `corporate_actions` | Split/bonus/dividend reference (currently deferred/unpopulated) |
| `transactions` | Append-only ledger of every order (backtest, paper, or live) |
| `closed_trades` | Matched entry/exit trade pairs, derived from `transactions` via FIFO |
| `account_snapshot` | Daily equity marks (not yet populated — needed for Sharpe/drawdown) |

## Running the pipeline

```bash
# 1. Refresh the tradeable instrument universe
uv run python data/build_instrument_list.py

# 2. Pull/update daily price history for the whole universe
uv run python data/fetch_daily_ohlcv.py

# 3. Run a backtest
uv run python backtest/engine.py

# 4. Match trades into closed positions
uv run python backtest/match_fifo.py
```

## Current status

- ✅ Infrastructure: Docker, Postgres, Kite Connect integration, full universe data pipeline
- ✅ Bollinger Band mean-reversion signal implemented and validated visually
- ✅ Backtest engine functional: writes real, cost-modeled transactions across the full universe
- ✅ Transaction cost model validated against Zerodha's live calculator
- 🚧 Known issue: instrument list occasionally accumulates stale entries for delisted/renamed securities — fix in progress (moving to a lifecycle-tracked cleanup on each instrument list refresh)
- ⬜ Not yet built: `account_snapshot` population, walk-forward validation, paper trading, live execution

## Roadmap

Following a structured plan: infrastructure → single strategy end-to-end → live trading with small capital → iteration. Not yet live — currently mid-Phase 2 (strategy backtesting).

## Notes

- All data sourced from Zerodha Kite Connect, chosen over free alternatives (e.g. yfinance) for corporate-action adjustment consistency between backtest and live data
- Costs modeled: brokerage, STT, GST, stamp duty, exchange transaction charges, SEBI charges, DP charges — delivery-based equity trading
- This is a learning project — built while learning to code, documented deliberately for that reason## Database schema

Six tables in Postgres:

| Table | Purpose |
|---|---|
| `instruments` | Reference list of tradeable NSE stocks/ETFs/indices |
| `daily_ohlcv` | Raw daily price history |
| `corporate_actions` | Split/bonus/dividend reference (currently deferred/unpopulated) |
| `transactions` | Append-only ledger of every order (backtest, paper, or live) |
| `closed_trades` | Matched entry/exit trade pairs, derived from `transactions` via FIFO |
| `account_snapshot` | Daily equity marks (not yet populated — needed for Sharpe/drawdown) |

## Running the pipeline

```bash
# 1. Refresh the tradeable instrument universe
uv run python data/build_instrument_list.py

# 2. Pull/update daily price history for the whole universe
uv run python data/fetch_daily_ohlcv.py

# 3. Run a backtest
uv run python backtest/engine.py

# 4. Match trades into closed positions
uv run python backtest/match_fifo.py
```

## Current status

- ✅ Infrastructure: Docker, Postgres, Kite Connect integration, full universe data pipeline
- ✅ Bollinger Band mean-reversion signal implemented and validated visually
- ✅ Backtest engine functional: writes real, cost-modeled transactions across the full universe
- ✅ Transaction cost model validated against Zerodha's live calculator
- 🚧 Known issue: instrument list occasionally accumulates stale entries for delisted/renamed securities — fix in progress (moving to a lifecycle-tracked cleanup on each instrument list refresh)
- ⬜ Not yet built: `account_snapshot` population, walk-forward validation, paper trading, live execution

## Roadmap

Following a structured 90-day plan: infrastructure → single strategy end-to-end → live trading with small capital → iteration. Not yet live — currently mid-Phase 2 (strategy backtesting).

## Notes

- All data sourced from Zerodha Kite Connect, chosen over free alternatives (e.g. yfinance) for corporate-action adjustment consistency between backtest and live data
- Costs modeled: brokerage, STT, GST, stamp duty, exchange transaction charges, SEBI charges, DP charges — delivery-based equity trading
- This is a learning project — built while learning to code, documented deliberately for that reason