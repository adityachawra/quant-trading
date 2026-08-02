import os
import sys
import uuid
from datetime import date, timedelta

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from dotenv import load_dotenv
import pandas_market_calendars as mcal

sys.path.append(os.path.abspath("strategies/mean_reversion_bb"))
from signal_bt import generate_signals as bb_generate_signals
from signal_bt import load_price_data, load_all_symbols

from transaction_cost import leg_cost

load_dotenv()

FUND = 10000
MAX_PORTFOLIO_RISK = 0.4
MAX_PER_TRADE_RISK = 0.03
MAX_PERCENTAGE_ALLOCATION_PER_TRADE = 0.2



def get_db_engine():
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)




def get_last_working_days(last_n_days: int = 220):
    nse = mcal.get_calendar("NSE")
    end_date = date.today()

    buffer_days = last_n_days * 2 + 30
    start_date = end_date - timedelta(days=buffer_days)

    schedule = nse.schedule(start_date=start_date.isoformat(), end_date=end_date.isoformat())
    trading_dates = sorted(schedule.index.normalize())

    return trading_dates[-last_n_days:]

def get_portfolio_risk(open_positions: dict):
    total_risk = 0
    for pos in open_positions.values():
        percentage_risk = abs(pos['stoploss']-pos['entry_price'])*pos['qty']/FUND
        total_risk += percentage_risk

    return total_risk



def should_allocate(open_positions: dict, new_trade_risk: float, turnover: float, cash_temp: float,
                    max_portfolio_risk: float = MAX_PORTFOLIO_RISK,
                    ):

    return ((get_portfolio_risk(open_positions) + new_trade_risk) <= max_portfolio_risk) and (turnover <= FUND*MAX_PERCENTAGE_ALLOCATION_PER_TRADE) and (turnover <= cash_temp)


def price_join_signal(price_df: pd.DataFrame, signal_df: pd.DataFrame) -> pd.DataFrame:
    """
    Joins the price_df and signal_df on the date index, ensuring that the resulting DataFrame has all dates from price_df,
    and fills missing signal values with 0 (no signal).
    """
    combined_df = price_df.join(signal_df, how='left')
    combined_df['signal'] = combined_df['signal'].fillna(0)
    return combined_df

def combines_signal_price(engine, signal_fn, signal_kwargs:dict = None) -> dict:

    symbols = load_all_symbols(engine)
    combined_df = {}
    for idx, symbol in enumerate(symbols, start=1):
        try:
            price_df = load_price_data(engine, symbol)

            signals = signal_fn(price_df, **signal_kwargs)

            combined_df[symbol] = price_join_signal(price_df, signals)

        except Exception as e:
            print(f"  [{idx}/{len(symbols)}] {symbol}: skipped due to error ({e})")
            continue

    return combined_df

    
def portfolio_backtest(engine, run_id: str, strategy_name: str, 
                       combined_signal_price: dict, reward_risk_ratio: float = 1.5, 
                       last_n_days: int = 220, cash: float = FUND):

    open_positions = {}
    txns = []
    cash_temp = cash
    for d in get_last_working_days(last_n_days):
        for symbol in combined_signal_price.keys():
            if d not in combined_signal_price[symbol].index: continue
            if combined_signal_price[symbol].loc[d, 'signal'] == 1:
                if symbol in open_positions: continue
                qty = int(MAX_PER_TRADE_RISK/combined_signal_price[symbol].loc[d, 'percentage_risk'])
                if qty < 1:  
                    continue
                fees = leg_cost(turnover = combined_signal_price[symbol].loc[d,'close']*qty, side = 'buy', is_intraday = False)
                total_fees = sum(fees.values())

                exp_profit = (combined_signal_price[symbol].loc[d, 'target_price'] - combined_signal_price[symbol].loc[d, 'close'])*qty - total_fees
                exp_loss = abs(combined_signal_price[symbol].loc[d, 'close'] - combined_signal_price[symbol].loc[d, 'stoploss'])*qty + total_fees
                reward_risk_ratio_temp = exp_profit/exp_loss
                if reward_risk_ratio_temp < reward_risk_ratio: continue
                

                if should_allocate(open_positions, combined_signal_price[symbol].loc[d, 'percentage_risk']*qty, combined_signal_price[symbol].loc[d, 'close']*qty, cash_temp, MAX_PORTFOLIO_RISK):
                    cash_temp = cash_temp - combined_signal_price[symbol].loc[d,'close']*qty - total_fees

                    open_positions[symbol] = {
                        'entry_date': d,
                        'entry_price': combined_signal_price[symbol].loc[d, 'close'],
                        'stoploss': combined_signal_price[symbol].loc[d, 'stoploss'],
                        'target_price': combined_signal_price[symbol].loc[d, 'target_price'],
                        'qty': qty,
                        'side': 'buy'
                    }
                    txns.append({
                        "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                        "symbol": symbol, "side": "buy", "quantity": qty,
                        "signal_time": d, "order_time": d, "fill_time": d,
                        "signal_price": combined_signal_price[symbol].loc[d,'close'], "fill_price": combined_signal_price[symbol].loc[d,'close'],
                        "order_type": "market", "broker_order_id": None, "status": "filled",
                        "fees_total": total_fees, "slippage_bps": 0.0,
                        "notes": "forced close - open position at end of backtest window",
                    })
                

            elif (symbol in open_positions) and (open_positions[symbol].get('target_price')<=combined_signal_price[symbol].loc[d,'close'] or open_positions[symbol].get('stoploss')>=combined_signal_price[symbol].loc[d,'close']):
                fees = leg_cost(turnover = combined_signal_price[symbol].loc[d,'close']*open_positions[symbol]['qty'], side = 'sell', is_intraday = False)
                total_fees = sum(fees.values())
                
                txns.append({
                    "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                    "symbol": symbol, "side": "sell", "quantity": open_positions[symbol]['qty'],
                    "signal_time": d, "order_time": d, "fill_time": d,
                    "signal_price": combined_signal_price[symbol].loc[d,'close'], "fill_price": combined_signal_price[symbol].loc[d,'close'],
                    "order_type": "market", "broker_order_id": None, "status": "filled",
                    "fees_total": total_fees, "slippage_bps": 0.0,
                    "notes": "forced close - open position at end of backtest window",
                })
                diff = (combined_signal_price[symbol].loc[d,'close']-open_positions[symbol]['entry_price'])*open_positions[symbol]['qty']
                invested_amount = open_positions[symbol]['entry_price']*open_positions[symbol]['qty']
                cash_temp = cash_temp + diff + invested_amount - total_fees
                del open_positions[symbol]

    if not txns:
        print("No transactions generated.")
        return

    txns_df = pd.DataFrame(txns)

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

    price_signal_df = combines_signal_price(engine, bb_generate_signals, signal_kwargs={'lookback': 35, 'num_std': 2})
    portfolio_backtest(engine, run_id, "Mean_Z_Score_BB", price_signal_df, 1.5, 220, FUND)


if __name__ == "__main__":
    main()