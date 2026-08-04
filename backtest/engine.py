"""
Portfolio backtest engine for the Bollinger-Band mean-reversion strategy.

What this file does, end to end:
  1. Loads every tradeable symbol's price history from Postgres and runs the
     strategy's signal function over each one (`combines_signal_price`).
  2. Walks forward day by day over the last N trading days, opening and closing
     simulated positions subject to per-trade sizing and portfolio-level risk
     caps (`portfolio_backtest`).
  3. Writes the resulting buy/sell orders into the `transactions` table with
     env='backtest', tagged by a run_id so a run can be re-run idempotently.

The `transactions` rows this produces are the raw order ledger; downstream
FIFO matching (match_fifo.py) pairs them into closed_trades for P&L stats.
"""

import os
import sys
import uuid
from datetime import date, timedelta

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from dotenv import load_dotenv
import pandas_market_calendars as mcal

# The strategy code lives in its own folder; add it to the import path so we can
# import the signal function and the DB price/symbol loaders directly.
# NOTE: this path is relative to the current working directory, so this script
# is expected to be run from the repo root (quant/).
sys.path.append(os.path.abspath("strategies/mean_reversion_bb"))
from signal_bt import generate_signals as bb_generate_signals
from signal_bt import load_price_data, load_all_symbols

# Per-leg cost model (brokerage, STT, exchange fees, GST, stamp duty, etc.).
from transaction_cost import leg_cost

load_dotenv()

# --- Backtest configuration ---
FUND = 500000                              # total notional capital the risk caps are measured against
MAX_PORTFOLIO_RISK = 0.4                   # max summed risk across all open positions, as a fraction of FUND
MAX_PER_TRADE_RISK = 0.03                  # target risk budget for a single trade (drives position size)
MAX_PERCENTAGE_ALLOCATION_PER_TRADE = 0.2  # max capital a single trade may deploy, as a fraction of FUND



def get_db_engine():
    """Builds a SQLAlchemy engine for the Postgres DB from the .env credentials."""
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)




def get_last_working_days(last_n_days: int = 220):
    """
    Returns the last `last_n_days` NSE trading days, ending today, as a sorted
    list of normalized (midnight) Timestamps.

    Uses the real NSE market calendar so weekends and exchange holidays are
    excluded automatically. We ask the calendar for a wider raw date range than
    we need (`buffer_days`) and then slice the tail, because calendar days are
    always more numerous than trading days.
    """
    nse = mcal.get_calendar("NSE")
    end_date = date.today()

    # Over-fetch calendar days: ~2x for weekends/holidays, +30 as a safety pad,
    # so that after dropping non-trading days we still have at least last_n_days.
    buffer_days = last_n_days * 2 + 30
    start_date = end_date - timedelta(days=buffer_days)

    schedule = nse.schedule(start_date=start_date.isoformat(), end_date=end_date.isoformat())
    trading_dates = sorted(schedule.index.normalize())

    return trading_dates[-last_n_days:]   # keep only the most recent N trading days

def get_portfolio_risk(open_positions: dict):
    """
    Current total portfolio risk = sum over all open positions of
    (per-share stop distance * quantity) / FUND, i.e. how much of the total
    capital is at risk if every open position hits its stop. Returned as a
    fraction of FUND (e.g. 0.25 == 25% of capital at risk).
    """
    total_risk = 0
    for pos in open_positions.values():
        # rupee risk on this position = |stop - entry| * qty; divide by FUND to normalise
        percentage_risk = abs(pos['stoploss']-pos['entry_price'])*pos['qty']/FUND
        total_risk += percentage_risk

    return total_risk



def should_allocate(open_positions: dict, new_trade_risk: float, turnover: float, cash_temp: float,
                    max_portfolio_risk: float = MAX_PORTFOLIO_RISK,
                    ):
    """
    Gate that decides whether a candidate trade is allowed to open. All three
    conditions must hold:
      1. Adding this trade's risk keeps total portfolio risk within the cap.
      2. The trade's turnover (price * qty) is within the per-trade allocation
         cap (FUND * MAX_PERCENTAGE_ALLOCATION_PER_TRADE).
      3. There is enough uninvested cash left to actually pay for it.
    Returns True only if the trade passes all three.
    """
    return ((get_portfolio_risk(open_positions) + new_trade_risk) <= max_portfolio_risk) and (turnover <= FUND*MAX_PERCENTAGE_ALLOCATION_PER_TRADE) and (turnover <= cash_temp)


def price_join_signal(price_df: pd.DataFrame, signal_df: pd.DataFrame) -> pd.DataFrame:
    """
    Joins the price_df and signal_df on the date index, ensuring that the resulting DataFrame has all dates from price_df,
    and fills missing signal values with 0 (no signal).
    """
    # left join keeps every price row; days with no signal become NaN then 0.
    combined_df = price_df.join(signal_df, how='left')
    combined_df['signal'] = combined_df['signal'].fillna(0)
    return combined_df

def combines_signal_price(engine, signal_fn, signal_kwargs:dict = None) -> dict:
    """
    Runs the signal function over the whole tradeable universe.

    For every symbol returned by load_all_symbols, loads its price history,
    generates signals, and joins them together. Returns a dict mapping
    symbol -> combined (price + signal) DataFrame indexed by trade date.

    A per-symbol try/except means one bad symbol (missing data, too-short
    history, etc.) is skipped and logged rather than aborting the whole run.
    """
    symbols = load_all_symbols(engine)
    combined_df = {}
    for idx, symbol in enumerate(symbols, start=1):
        try:
            price_df = load_price_data(engine, symbol)

            signals = signal_fn(price_df, **signal_kwargs)

            combined_df[symbol] = price_join_signal(price_df, signals)

        except Exception as e:
            # Skip and report this symbol; keep processing the rest of the universe.
            print(f"  [{idx}/{len(symbols)}] {symbol}: skipped due to error ({e})")
            continue

    return combined_df


def portfolio_backtest(engine, run_id: str, strategy_name: str,
                       combined_signal_price: dict, reward_risk_ratio: float = 1.5,
                       last_n_days: int = 220, cash: float = FUND):
    """
    Simulates the strategy over the last `last_n_days` trading days and writes
    the resulting orders to the `transactions` table.

    Parameters
    ----------
    engine : SQLAlchemy engine to the Postgres DB.
    run_id : identifier for this backtest run; used so a re-run overwrites its
             own prior rows (delete-then-insert by run_id below) rather than
             duplicating them.
    strategy_name : label stored on every transaction row.
    combined_signal_price : dict of symbol -> price+signal DataFrame, as built
             by combines_signal_price().
    reward_risk_ratio : minimum expected-reward / expected-loss (after fees) a
             candidate entry must clear to be taken.
    last_n_days : length of the simulation window, in trading days.
    cash : starting cash for the simulation.

    Simulation model
    ----------------
    Iterates day by day (outer loop) and symbol by symbol (inner loop). For each
    (day, symbol):
      * If there's a fresh entry signal (signal == 1) and no open position in
        that symbol, size the trade, check reward:risk and the allocation gate,
        and if it passes, open the position and record a 'buy'.
      * Otherwise, if a position is already open and price has reached either the
        target or the stop, close it and record a 'sell'.
    Only long ('buy' then 'sell') trades are modelled.
    """
    open_positions = {}   # symbol -> dict describing the currently held position
    txns = []             # list of order rows to be written at the end
    account_snapshot = [] # list of daily snapshots of cash + open positions for reporting
    cash_temp = cash      # running cash balance during the simulation
    for d in get_last_working_days(last_n_days):
        for symbol in combined_signal_price.keys():
            # Skip symbols with no row on this date (holidays for that scrip, gaps, etc.).
            if d not in combined_signal_price[symbol].index: continue
            if combined_signal_price[symbol].loc[d, 'signal'] == 1:
                # --- ENTRY branch: a buy signal fired today ---
                if symbol in open_positions: continue   # already long this symbol; ignore repeat signal
                # Position size from the per-trade risk budget: qty such that
                # (per-share stop distance) * qty ~= MAX_PER_TRADE_RISK of FUND.
                # percentage_risk is the stop distance as a fraction of entry price.
                qty = int(MAX_PER_TRADE_RISK/combined_signal_price[symbol].loc[d, 'percentage_risk'])
                if qty < 1:
                    continue   # stop is too wide to afford even 1 share within the risk budget
                # Estimate entry-leg costs on this turnover (delivery, not intraday).
                fees = leg_cost(turnover = combined_signal_price[symbol].loc[d,'close']*qty, side = 'buy', is_intraday = False)
                total_fees = sum(fees.values())

                # Expected reward and loss in rupees, net of the entry fee.
                exp_profit = (combined_signal_price[symbol].loc[d, 'target_price'] - combined_signal_price[symbol].loc[d, 'close'])*qty - total_fees
                exp_loss = abs(combined_signal_price[symbol].loc[d, 'close'] - combined_signal_price[symbol].loc[d, 'stoploss'])*qty + total_fees
                reward_risk_ratio_temp = exp_profit/exp_loss
                # Reject trades whose reward:risk is below the required minimum.
                if reward_risk_ratio_temp < reward_risk_ratio: continue


                # Final portfolio-level gate (risk cap, allocation cap, cash on hand).
                if should_allocate(open_positions, combined_signal_price[symbol].loc[d, 'percentage_risk']*qty, combined_signal_price[symbol].loc[d, 'close']*qty, cash_temp, MAX_PORTFOLIO_RISK):
                    # Pay for the shares plus fees out of running cash.
                    cash_temp = cash_temp - combined_signal_price[symbol].loc[d,'close']*qty - total_fees

                    # Record the now-open position so later days can exit it.
                    open_positions[symbol] = {
                        'entry_date': d,
                        'entry_price': combined_signal_price[symbol].loc[d, 'close'],
                        'stoploss': combined_signal_price[symbol].loc[d, 'stoploss'],
                        'target_price': combined_signal_price[symbol].loc[d, 'target_price'],
                        'qty': qty,
                        'side': 'buy'
                    }
                    # Log the buy order (fill assumed at close, market order).
                    txns.append({
                        "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                        "symbol": symbol, "side": "buy", "quantity": qty,
                        "signal_time": d, "order_time": d, "fill_time": d,
                        "signal_price": combined_signal_price[symbol].loc[d,'close'], "fill_price": combined_signal_price[symbol].loc[d,'close'],
                        "order_type": "market", "broker_order_id": None, "status": "filled",
                        "fees_total": total_fees, "slippage_bps": 0.0,
                        "notes": "forced close - open position at end of backtest window",
                    })

                    




            # --- EXIT branch: hold an open position and price hit target OR stop ---
            elif (symbol in open_positions) and (open_positions[symbol].get('target_price')<=combined_signal_price[symbol].loc[d,'close'] or open_positions[symbol].get('stoploss')>=combined_signal_price[symbol].loc[d,'close']):
                # Estimate exit-leg costs on the sale turnover.
                fees = leg_cost(turnover = combined_signal_price[symbol].loc[d,'close']*open_positions[symbol]['qty'], side = 'sell', is_intraday = False)
                total_fees = sum(fees.values())

                # Log the sell order (fill assumed at close, market order).
                txns.append({
                    "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                    "symbol": symbol, "side": "sell", "quantity": open_positions[symbol]['qty'],
                    "signal_time": d, "order_time": d, "fill_time": d,
                    "signal_price": combined_signal_price[symbol].loc[d,'close'], "fill_price": combined_signal_price[symbol].loc[d,'close'],
                    "order_type": "market", "broker_order_id": None, "status": "filled",
                    "fees_total": total_fees, "slippage_bps": 0.0,
                    "notes": "forced close - open position at end of backtest window",
                })
                # Return proceeds to cash: realised P&L + the originally invested
                # principal, minus the exit fee. Then close out the position.
                diff = (combined_signal_price[symbol].loc[d,'close']-open_positions[symbol]['entry_price'])*open_positions[symbol]['qty']
                invested_amount = open_positions[symbol]['entry_price']*open_positions[symbol]['qty']
                cash_temp = cash_temp + diff + invested_amount - total_fees
                del open_positions[symbol]

        account_snapshot.append({
                                "snapshot_date": d,
                                "strategy": strategy_name,
                                "env": "backtest",
                                "cash": cash_temp,
                                "positions_value": sum([open_positions[s]['entry_price']*open_positions[s]['qty'] for s in open_positions]),
                                "total_equity": cash_temp + sum([open_positions[s]['entry_price']*open_positions[s]['qty'] for s in open_positions])
                            })
            

    # Nothing traded in the whole window -> report and exit without touching the DB.
    if not txns:
        print("No transactions generated.")
        return

    txns_df = pd.DataFrame(txns)
    account_snapshot_df = pd.DataFrame(account_snapshot)
    account_snapshot_df = account_snapshot_df.sort_values(by="snapshot_date").reset_index(drop=True)

    # Idempotent write: clear any prior rows for this (strategy, env, run_id)
    # first, then append the fresh set, so re-running a run_id replaces rather
    # than duplicates. Both statements share one transaction (engine.begin()),
    # so it's all-or-nothing.
    delete_query = text("""
        DELETE FROM transactions
        WHERE strategy = :strategy AND env = 'backtest' AND run_id = :run_id;
    """)
    
    with engine.begin() as conn:
        conn.execute(delete_query, {"strategy": strategy_name, "run_id": run_id})
        txns_df.to_sql("transactions", conn, if_exists="append", index=False)
        
        account_snapshot_df.to_sql("account_snapshot", conn, if_exists="append", index=False)

    print(f"\nWrote {len(txns_df)} transactions (strategy={strategy_name}, run_id={run_id}) to Postgres.")



def main():
    """Entry point: build signals for the universe, then run one backtest."""
    engine = get_db_engine()

    # run_id is date-stamped, so re-running on the same day overwrites that day's
    # rows (see the delete-then-insert in portfolio_backtest).
    run_id = f"bb_v1_{date.today().isoformat()}"

    # Build price+signal frames for every symbol with the chosen BB parameters.
    price_signal_df = combines_signal_price(engine, bb_generate_signals, signal_kwargs={'lookback': 50, 'num_std': 1.5})

    # Run the backtest: reward:risk >= 1.5, over the last 365 trading days, starting with 5,00,000 cash.
    portfolio_backtest(engine, run_id, "Mean_Z_Score_BB1", price_signal_df, 1.5, 365, 500000)


if __name__ == "__main__":
    main()
