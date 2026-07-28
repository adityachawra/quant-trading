import os
import sys
import uuid
from datetime import date

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from dotenv import load_dotenv

sys.path.append(os.path.abspath("strategies/mean_reversion_bb"))
from signal_bt import generate_signals as bb_generate_signals
from signal_bt import load_price_data, load_all_symbols

from transaction_cost import leg_cost

load_dotenv()

FIXED_INVESTMENT = 10000


def get_db_engine():
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)


def simulate_symbol(price_df: pd.DataFrame, signals: pd.Series, symbol: str,
                     run_id: str, strategy_name: str,
                     fixed_investment: float = FIXED_INVESTMENT) -> list:
    """
    Walks one symbol's signals day by day, building transaction rows.
    Fill assumption: SAME-DAY CLOSE. strategy_name is now a parameter,
    not a hardcoded constant — this is what makes the resulting
    `transactions` rows correctly attributable to whichever strategy
    actually produced these signals.
    """
    rows = []
    quantity = 0
    is_position = False

    for i in range(len(signals)):
        signal_val = signals.iloc[i]
        trade_date = price_df.index[i]
        close_price = price_df.iloc[i]['close']

        if signal_val == 1 and not is_position:
            quantity = int(fixed_investment // close_price)
            if quantity == 0:
                continue

            actual_turnover = quantity * close_price
            fees = leg_cost(actual_turnover, side="buy", is_intraday=False)
            fees_total = sum(fees.values())

            rows.append({
                "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                "symbol": symbol, "side": "buy", "quantity": quantity,
                "signal_time": trade_date, "order_time": trade_date, "fill_time": trade_date,
                "signal_price": close_price, "fill_price": close_price,
                "order_type": "market", "broker_order_id": None, "status": "filled",
                "fees_total": fees_total, "slippage_bps": 0.0, "notes": None,
            })
            is_position = True

        elif signal_val == -1 and is_position:
            actual_turnover = quantity * close_price
            fees = leg_cost(actual_turnover, side="sell", is_intraday=False)
            fees_total = sum(fees.values())

            rows.append({
                "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                "symbol": symbol, "side": "sell", "quantity": quantity,
                "signal_time": trade_date, "order_time": trade_date, "fill_time": trade_date,
                "signal_price": close_price, "fill_price": close_price,
                "order_type": "market", "broker_order_id": None, "status": "filled",
                "fees_total": fees_total, "slippage_bps": 0.0, "notes": None,
            })
            is_position = False
            quantity = 0

    if is_position:
        last_date = price_df.index[-1]
        last_close = price_df.iloc[-1]['close']
        actual_turnover = quantity * last_close
        fees = leg_cost(actual_turnover, side="sell", is_intraday=False)
        fees_total = sum(fees.values())

        rows.append({
            "env": "backtest", "run_id": run_id, "strategy": strategy_name,
            "symbol": symbol, "side": "sell", "quantity": quantity,
            "signal_time": last_date, "order_time": last_date, "fill_time": last_date,
            "signal_price": last_close, "fill_price": last_close,
            "order_type": "market", "broker_order_id": None, "status": "filled",
            "fees_total": fees_total, "slippage_bps": 0.0,
            "notes": "forced close - open position at end of backtest window",
        })

    return rows


def run_backtest(engine, run_id: str, signal_fn, strategy_name: str,
                  signal_kwargs: dict = None, fixed_investment: float = FIXED_INVESTMENT,
                  min_rows: int = 50, last_n_days: int = None,
                  start_date: str = None, end_date: str = None):
    """
    Runs a backtest across every EQUITY + ETF symbol, using WHICHEVER
    signal function is passed in.

    ... (existing docstring for signal_fn, strategy_name, signal_kwargs, min_rows) ...

    Date window options (use ONE of these, not both) — same convention
    as quick_eyeball_check() and run_universe_scan() in signal.py:

    last_n_days : int, optional
        Restricts each symbol to its own most recent N rows before
        running the signal function on it.

    start_date, end_date : str, optional
        Restricts each symbol to this date range instead. FORMAT:
        "YYYY-MM-DD" string, matched against each symbol's DatetimeIndex.
    """
    signal_kwargs = signal_kwargs or {}
    symbols = load_all_symbols(engine)
    print(f"Running backtest [{strategy_name}] across {len(symbols)} symbols, run_id={run_id}")

    all_rows = []

    for idx, symbol in enumerate(symbols, start=1):
        try:
            price_df = load_price_data(engine, symbol)

            # --- apply the date window, same convention as elsewhere ---
            if last_n_days is not None:
                price_df = price_df.iloc[-last_n_days:]
            elif start_date is not None or end_date is not None:
                price_df = price_df.loc[start_date:end_date]

            if len(price_df) < min_rows:
                continue

            signals = signal_fn(price_df, **signal_kwargs)
            symbol_rows = simulate_symbol(price_df, signals, symbol, run_id,
                                           strategy_name, fixed_investment)
            all_rows.extend(symbol_rows)

        except Exception as e:
            print(f"  [{idx}/{len(symbols)}] {symbol}: skipped due to error ({e})")
            continue

        if idx % 200 == 0:
            print(f"  Processed {idx}/{len(symbols)} symbols, {len(all_rows)} transactions so far...")

    if not all_rows:
        print("No transactions generated.")
        return

    txns_df = pd.DataFrame(all_rows)

    delete_query = text("""
        DELETE FROM transactions
        WHERE strategy = :strategy AND env = 'backtest' AND run_id = :run_id;
    """)
    with engine.begin() as conn:
        conn.execute(delete_query, {"strategy": strategy_name, "run_id": run_id})
        txns_df.to_sql("transactions", conn, if_exists="append", index=False)

    print(f"\nWrote {len(txns_df)} transactions (strategy={strategy_name}, run_id={run_id}) to Postgres.")


def main():
    engine = get_db_engine()

    run_id = f"bb_v1_{date.today().isoformat()}"

    run_backtest(
        engine,
        run_id=run_id,
        signal_fn=bb_generate_signals,
        strategy_name="mean_reversion_bb",
        signal_kwargs={"lookback": 30, "num_std": 2.0},
        last_n_days=220
    )


if __name__ == "__main__":
    main()