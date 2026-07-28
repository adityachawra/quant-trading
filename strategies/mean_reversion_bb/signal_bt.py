import os
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()


def get_db_engine():
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)


def load_price_data(engine, symbol):
    """Pulls one symbol's daily price history from Postgres, sorted by date,
    returned as a DataFrame with a DatetimeIndex — exactly what generate_signals expects."""
    query = text("""
        SELECT trade_date, open, high, low, close, volume
        FROM daily_ohlcv
        WHERE symbol = :symbol
        ORDER BY trade_date;
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"symbol": symbol})

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    return df


def load_all_symbols(engine, categories=("EQUITY", "ETF")):
    """Returns the list of tradingsymbols to scan, restricted to the given
    categories. INDEX is excluded by default — you can't directly invest
    in an index, only in ETFs/derivatives tracking it."""
    query = text("""
        SELECT tradingsymbol
        FROM instruments
        WHERE category = ANY(:categories)
        ORDER BY tradingsymbol;
    """)
    with engine.connect() as conn:
        result = pd.read_sql(query, conn, params={"categories": list(categories)})
    return result["tradingsymbol"].tolist()


def generate_signals(price_df: pd.DataFrame, lookback: int = 20, num_std: float = 2.0) -> pd.Series:
    """
    Generates Bollinger Band mean-reversion signals.

    Parameters
    ----------
    price_df : pd.DataFrame
        Must have a DatetimeIndex and 'close', 'high', 'low' columns.
    lookback : int
        Number of periods for the rolling mean/std dev calculation.
    num_std : float
        Number of standard deviations for the band width.

    Returns
    -------
    pd.Series
        Same index as price_df. Values are one of:
            +1  = enter long
            -1  = exit long
             0  = no action
    """
    rolling_mean = price_df['close'].rolling(lookback).mean()
    rolling_std = price_df['close'].rolling(lookback).std()
    lower_band = rolling_mean - (num_std * rolling_std)

    signals = pd.Series(0, index=price_df.index)

    is_position = False
    watch_to_buy = False

    for i in range(len(price_df)):

        if is_position:
            stoploss = price_df.iloc[i]['close'] < lower_band.iloc[i] - (rolling_mean.iloc[i] - lower_band.iloc[i]) / 2
            if (price_df.iloc[i]['high'] > target_price) or stoploss:
                signals.iloc[i] = -1
                is_position = False

        else:
            if price_df.iloc[i]['low'] < lower_band.iloc[i]:
                watch_to_buy = True
            if watch_to_buy:
                if price_df.iloc[i]['close'] > lower_band.iloc[i]:
                    signals.iloc[i] = 1
                    watch_to_buy = False
                    is_position = True
                    target_price = rolling_mean.iloc[i]

    return signals


def _compute_trade_log(price_df: pd.DataFrame, signals: pd.Series, fixed_investment: float) -> list:
    """
    Shared core logic: walks through signals, returns a list of dicts,
    one per completed trade, with entry/exit dates and P&L in rupees.
    Used by both quick_eyeball_check (single symbol, verbose) and
    run_universe_scan (many symbols, aggregated).

    No compounding — every trade uses the same fixed_investment amount,
    so total P&L across trades is a plain sum, not a compounded product.
    This matches the "simple version" position-sizing decision: real
    position sizing (risk %, capital caps) comes later, in the actual
    backtest engine.
    """
    trades = []
    buy_price = None
    buy_date = None

    for i in range(len(signals)):
        if signals.iloc[i] == 1:
            buy_price = price_df.iloc[i]['close']
            buy_date = price_df.index[i]  # the DatetimeIndex value at this position

        elif signals.iloc[i] == -1 and buy_price is not None:
            sell_price = price_df.iloc[i]['close']
            sell_date = price_df.index[i]

            pct_return = (sell_price - buy_price) / buy_price
            pnl = fixed_investment * pct_return
            holding_days = (sell_date - buy_date).days

            trades.append({
                "entry_date": buy_date,
                "exit_date": sell_date,
                "entry_price": buy_price,
                "exit_price": sell_price,
                "pct_return": pct_return,
                "pnl": pnl,
                "holding_days": holding_days,
            })

            buy_price = None
            buy_date = None

    return trades


def quick_eyeball_check(
    price_df: pd.DataFrame,
    signals: pd.Series,
    fixed_investment: float = 10000,
    last_n_days: int = None,
    start_date: str = None,
    end_date: str = None,
):
    """
    Rough, approximate P&L using a fixed rupee amount per trade —
    no compounding, no real position sizing, no transaction costs.
    NOT the real backtest — just a sanity check before the actual
    backtest engine (with proper sizing and costs) is built.

    Prints a full trade-by-trade log (entry date, exit date, P&L),
    plus a total. Also returns that trade log as a DataFrame.

    Date window options (use ONE of these, not both):

    last_n_days : int, optional
        If given, only looks at the most recent N rows of price_df/signals.
        Example: last_n_days=252 -> roughly the last 1 trading year.

    start_date, end_date : str, optional
        If given, restricts to this date range instead. Both are optional —
        pass just start_date ("from here to the end"), just end_date
        ("from the beginning to here"), or both.

        FORMAT REQUIRED: a string like "2024-01-01" (YYYY-MM-DD).
        This must be parseable to match price_df's DatetimeIndex —
        load_price_data() already converts trade_date with
        pd.to_datetime() and sets it as the index, so pandas can match
        a plain string like "2024-01-01" against the real Timestamp
        values in the index automatically.
    """

    if last_n_days is not None:
        price_df = price_df.iloc[-last_n_days:]
        signals = signals.iloc[-last_n_days:]

    elif start_date is not None or end_date is not None:
        # .loc[start:end] on a DatetimeIndex is an INCLUSIVE slice —
        # both start_date and end_date themselves are included if present
        price_df = price_df.loc[start_date:end_date]
        signals = signals.loc[start_date:end_date]

    trades = _compute_trade_log(price_df, signals, fixed_investment)
    trades_df = pd.DataFrame(trades)

    window_desc = (
        f"last {last_n_days} days" if last_n_days is not None
        else f"{start_date or price_df.index.min().date()} to {end_date or price_df.index.max().date()}"
    )

    if trades_df.empty:
        print(f"[{window_desc}] No completed trades in this window.")
        return trades_df

    print(f"\n[{window_desc}] Trade log:")
    print(trades_df.to_string(index=False))

    total_pnl = trades_df["pnl"].sum()
    print(f"\n{len(trades_df)} trades, total P&L: ₹{total_pnl:.2f} "
          f"investing ₹{fixed_investment} per trade (no costs/sizing included)")

    return trades_df


def run_universe_scan(
    engine,
    lookback=20,
    num_std=2.0,
    fixed_investment=10000,
    min_rows=100,
    last_n_days=None,
    start_date=None,
    end_date=None,
):
    symbols = load_all_symbols(engine)
    print(f"Scanning {len(symbols)} symbols...")

    results = []

    for idx, symbol in enumerate(symbols, start=1):
        try:
            price_df = load_price_data(engine, symbol)

            if last_n_days is not None:
                price_df = price_df.iloc[-last_n_days:]
            elif start_date is not None or end_date is not None:
                price_df = price_df.loc[start_date:end_date]

            if len(price_df) < min_rows:
                continue

            signals = generate_signals(price_df, lookback=lookback, num_std=num_std)
            trades = _compute_trade_log(price_df, signals, fixed_investment)

            if not trades:
                continue

            trades_df = pd.DataFrame(trades)

            total_pnl = trades_df["pnl"].sum()
            first_entry = trades_df["entry_date"].min()
            last_exit = trades_df["exit_date"].max()

            # --- CAGR approximation ---
            # years_active: the actual span of time these trades occurred over.
            # Guard against a zero/near-zero span (e.g. a single trade closing
            # the same day it opened) which would make the exponent below
            # divide by ~zero and blow up to an absurd, meaningless number.
            years_active = (last_exit - first_entry).days / 365.25

            ending_value = fixed_investment + total_pnl

            if years_active > 0.05 and ending_value > 0:
                # Standard CAGR formula: (end/start)^(1/years) - 1
                cagr_pct = ((ending_value / fixed_investment) ** (1 / years_active) - 1) * 100
            else:
                # Too short a span to annualize meaningfully, or a total
                # wipeout (ending_value <= 0, where the formula is undefined
                # for non-integer exponents) -> leave as NaN rather than
                # printing a nonsense number
                cagr_pct = float("nan")

            results.append({
                "symbol": symbol,
                "num_trades": len(trades_df),
                "total_pnl": total_pnl,
                "avg_pnl_per_trade": trades_df["pnl"].mean(),
                "first_entry_date": first_entry,
                "last_exit_date": last_exit,
                "years_active": years_active,
                "cagr_pct": cagr_pct,
            })

        except Exception as e:
            print(f"  [{idx}/{len(symbols)}] {symbol}: skipped due to error ({e})")
            continue

        if idx % 200 == 0:
            print(f"  Processed {idx}/{len(symbols)} symbols...")

    results_df = pd.DataFrame(results)
    
    results_df = results_df.sort_values(
        by=["num_trades", "cagr_pct"],
        ascending=[False, False]
    )

    return results_df

def get_output_path(filename):
    """Builds a path relative to THIS script's own location on disk,
    regardless of what folder your terminal is currently cd'd into."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, filename)


def main():
    engine = get_db_engine()

    price_df = load_price_data(engine, symbol="NIFTYBEES")
    print(f"Loaded {len(price_df)} rows for NIFTYBEES")

    signals = generate_signals(price_df, lookback=50, num_std=2.0)
    quick_eyeball_check(price_df, signals, fixed_investment=10000, last_n_days=500)

    results_df = run_universe_scan(engine, lookback=50, num_std=2.0, fixed_investment=10000, last_n_days = 220)

    print("\nTop 20 by average P&L per trade:")
    print(results_df.head(20).to_string(index=False))

    output_path = get_output_path("universe_scan_results.csv")
    results_df.to_csv(output_path, index=False)

    # Confirm exactly where it went and when, so there's no ambiguity next time
    mod_time = os.path.getmtime(output_path)
    from datetime import datetime
    print(f"\nSaved to: {output_path}")
    print(f"Last modified: {datetime.fromtimestamp(mod_time)}")

if __name__ == "__main__":
    main()