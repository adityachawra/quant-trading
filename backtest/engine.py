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



def should_allocate(open_positions: dict, new_trade_risk: float, turnover: float,
                    max_portfolio_risk: float = MAX_PORTFOLIO_RISK,
                    ):

    return ((get_portfolio_risk(open_positions) + new_trade_risk) <= max_portfolio_risk) and (turnover <= FUND*MAX_PERCENTAGE_ALLOCATION_PER_TRADE)



def portfolio_backtest(engine, run_id: str, signal_fn, strategy_name: str, 
                       combined_signal_price: dict, signal_kwargs: dict = None, 
                       last_n_days: int = 220, fixed_investment: float = FUND):

    open_positions = {}
    txns = []
    
    for d in get_last_working_days(last_n_days):
        for symbol in combined_signal_price.keys():
            if combined_signal_price[symbol].loc[d, 'signal'] == 1:
                if d not in combined_signal_price[symbol].index: continue
                qty = int(MAX_PER_TRADE_RISK/combined_signal_price[symbol].loc[d, 'percentage_risk'])
                if qty < 1 or not should_allocate(open_positions, combined_signal_price[symbol].loc[d, 'percentage_risk']*qty, MAX_PORTFOLIO_RISK,combined_signal_price[symbol].loc[d, 'close']*qty ):
                    continue
                open_positions[symbol] = {
                    'entry_date': text(d),
                    'entry_price': combined_signal_price[symbol].loc[d, 'close'],
                    'stoploss': combined_signal_price[symbol].loc[d, 'stoploss'],
                    'target_price': combined_signal_price[symbol].loc[d, 'target_price'],
                    'qty': qty,
                    'side': 'buy'
                }
                fees = leg_cost(turnover = combined_signal_price[symbol].loc[d,'close']*qty, side = 'buy', is_intraday = False)
                total_fees = sum(fees.values())
                
                txns.append({
                    "env": "backtest", "run_id": run_id, "strategy": strategy_name,
                    "symbol": symbol, "side": "buy", "quantity": qty,
                    "signal_time": d, "order_time": d, "fill_time": d,
                    "signal_price": combined_signal_price[symbol].loc[d,'close'], "fill_price": combined_signal_price[symbol].loc[d,'close'],
                    "order_type": "market", "broker_order_id": None, "status": "filled",
                    "fees_total": total_fees, "slippage_bps": 0.0,
                    "notes": "forced close - open position at end of backtest window",
                })

            elif (combined_signal_price[symbol].loc[d, 'signal'] == -1) and (symbol in open_positions):
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
                del open_positions[symbol]


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