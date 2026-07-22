import os
import time
from datetime import date, timedelta

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
BACKFILL_START_DATE = date(2019, 1, 1)
CHUNK_SIZE_DAYS = 1900
RATE_LIMIT_SLEEP = 0.35
MAX_RETRIES = 3

TEST_MODE = False
TEST_SYMBOLS = ["NIFTYBEES", "RELIANCE", "TCS", "NIFTY 50", "BHEL"]


def get_db_engine():
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)


def get_kite_client():
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
    kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))
    return kite


def load_instrument_list(engine):
    if TEST_MODE:
        query = text("""
            SELECT instrument_token, tradingsymbol, category
            FROM instruments
            WHERE tradingsymbol = ANY(:symbols)
            ORDER BY tradingsymbol;
        """)
        with engine.connect() as conn:
            return pd.read_sql(query, conn, params={"symbols": TEST_SYMBOLS})

    query = text("""
        SELECT instrument_token, tradingsymbol, category
        FROM instruments
        WHERE category IN ('EQUITY', 'ETF', 'INDEX')
        ORDER BY tradingsymbol;
    """)
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def get_existing_max_date(engine, symbol):
    query = text("SELECT MAX(trade_date) FROM daily_ohlcv WHERE symbol = :symbol")
    with engine.connect() as conn:
        result = conn.execute(query, {"symbol": symbol}).scalar()
    return result


def fetch_symbol_history(kite, instrument_token, from_date, to_date):
    all_candles = []
    current_start = from_date

    while current_start <= to_date:
        current_end = min(current_start + timedelta(days=CHUNK_SIZE_DAYS), to_date)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                candles = kite.historical_data(
                    instrument_token, current_start, current_end, "day"
                )
                all_candles.extend(candles)
                break
            except Exception as e:
                wait_time = 2 ** attempt
                print(f"    Attempt {attempt} failed ({e}). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                if attempt == MAX_RETRIES:
                    print(f"    Giving up on chunk {current_start} to {current_end} after {MAX_RETRIES} attempts.")

        time.sleep(RATE_LIMIT_SLEEP)
        current_start = current_end + timedelta(days=1)

    return all_candles


def upsert_ohlcv(engine, df):
    if df.empty:
        return

    df.to_sql("ohlcv_staging", engine, if_exists="replace", index=False)

    upsert_query = text("""
        INSERT INTO daily_ohlcv (symbol, trade_date, open, high, low, close,
                                   volume, adj_close, source, ingested_at)
        SELECT symbol, trade_date, open, high, low, close,
               volume, adj_close, source, ingested_at
        FROM ohlcv_staging
        ON CONFLICT (symbol, trade_date)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adj_close = EXCLUDED.adj_close,
            source = EXCLUDED.source,
            ingested_at = EXCLUDED.ingested_at;
    """)

    with engine.begin() as conn:
        conn.execute(upsert_query)
        conn.execute(text("DROP TABLE ohlcv_staging;"))


def main():
    engine = get_db_engine()
    kite = get_kite_client()

    instruments = load_instrument_list(engine)
    total = len(instruments)
    print(f"Loaded {total} instruments to process.")

    today = date.today()

    for idx, row in enumerate(instruments.itertuples(), start=1):
        symbol = row.tradingsymbol
        token = row.instrument_token

        existing_max_date = get_existing_max_date(engine, symbol)

        if existing_max_date is not None and existing_max_date >= today - timedelta(days=1):
            print(f"[{idx}/{total}] {symbol}: already up to date, skipping.")
            continue

        from_date = BACKFILL_START_DATE if existing_max_date is None else existing_max_date + timedelta(days=1)

        print(f"[{idx}/{total}] {symbol}: fetching {from_date} to {today}...")

        candles = fetch_symbol_history(kite, token, from_date, today)

        if not candles:
            print(f"    No data returned for {symbol}.")
            continue

        df = pd.DataFrame(candles)
        df["symbol"] = symbol
        df["trade_date"] = pd.to_datetime(df["date"]).dt.date
        df["adj_close"] = df["close"]
        df["source"] = "kite"
        df["ingested_at"] = pd.Timestamp.now()

        df = df[["symbol", "trade_date", "open", "high", "low", "close",
                  "volume", "adj_close", "source", "ingested_at"]]

        df = df.drop_duplicates(subset=["symbol", "trade_date"], keep="last")

        upsert_ohlcv(engine, df)
        print(f"    Saved {len(df)} rows for {symbol}.")


if __name__ == "__main__":
    main()