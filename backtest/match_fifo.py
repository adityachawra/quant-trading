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


def fetch_transactions(engine):
    """Pulls every successfully filled transaction, ordered so each
    (symbol, strategy, env, run_id) group is together and time-ordered within itself."""
    query = text("""
        SELECT txn_id, env, run_id, strategy, symbol, side, quantity,
               fill_time, fill_price, fees_total
        FROM transactions
        WHERE status = 'filled' AND fill_time IS NOT NULL
        ORDER BY symbol, strategy, env, COALESCE(run_id, ''), fill_time, txn_id;
    """)
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def fifo_match_group(group_df):
    """Takes all transactions for ONE (symbol, strategy, env, run_id) combination,
    already sorted by time, and pairs buys with sells FIFO-style."""
    open_lots = []
    closed = []

    for row in group_df.itertuples():
        if row.side == "buy":
            open_lots.append({
                "txn_id": row.txn_id,
                "remaining_qty": row.quantity,
                "orig_qty": row.quantity,
                "price": row.fill_price,
                "fees_total": row.fees_total,
                "time": row.fill_time,
            })

        elif row.side == "sell":
            sell_remaining = row.quantity
            sell_fee_per_share = (row.fees_total / row.quantity) if row.quantity else 0

            while sell_remaining > 0 and open_lots:
                lot = open_lots[0]
                matched_qty = min(lot["remaining_qty"], sell_remaining)

                entry_fee_share = lot["fees_total"] * (matched_qty / lot["orig_qty"])
                exit_fee_share = sell_fee_per_share * matched_qty
                fees_total = entry_fee_share + exit_fee_share

                gross_pnl = (row.fill_price - lot["price"]) * matched_qty
                net_pnl = gross_pnl - fees_total
                return_pct = net_pnl / (lot["price"] * matched_qty) if lot["price"] else None
                holding_days = (row.fill_time.date() - lot["time"].date()).days

                closed.append({
                    "env": row.env,
                    "run_id": row.run_id,
                    "strategy": row.strategy,
                    "symbol": row.symbol,
                    "entry_txn_id": lot["txn_id"],
                    "exit_txn_id": row.txn_id,
                    "entry_time": lot["time"],
                    "exit_time": row.fill_time,
                    "quantity": matched_qty,
                    "entry_price": lot["price"],
                    "exit_price": row.fill_price,
                    "gross_pnl": gross_pnl,
                    "fees_total": fees_total,
                    "net_pnl": net_pnl,
                    "return_pct": return_pct,
                    "holding_days": holding_days,
                })

                lot["remaining_qty"] -= matched_qty
                sell_remaining -= matched_qty
                if lot["remaining_qty"] == 0:
                    open_lots.pop(0)

    return closed


def rebuild_closed_trades(engine):
    """Rebuilds closed_trades from scratch, one (symbol, strategy, env, run_id)
    group at a time."""
    all_txns = fetch_transactions(engine)
    if all_txns.empty:
        print("No filled transactions found. Nothing to match.")
        return

    all_txns["run_id"] = all_txns["run_id"].fillna("")

    group_cols = ["symbol", "strategy", "env", "run_id"]
    grouped = all_txns.groupby(group_cols)

    total_closed = 0

    for group_key, group_df in grouped:
        symbol, strategy, env, run_id = group_key
        closed = fifo_match_group(group_df)

        if not closed:
            continue

        closed_df = pd.DataFrame(closed)
        closed_df["run_id"] = closed_df["run_id"].replace("", None)

        delete_query = text("""
            DELETE FROM closed_trades
            WHERE symbol = :symbol AND strategy = :strategy AND env = :env
              AND (run_id = :run_id OR (:run_id IS NULL AND run_id IS NULL));
        """)

        with engine.begin() as conn:
            conn.execute(delete_query, {
                "symbol": symbol, "strategy": strategy, "env": env,
                "run_id": run_id if run_id != "" else None,
            })
            closed_df.to_sql("closed_trades", conn, if_exists="append", index=False)

        total_closed += len(closed_df)
        print(f"{symbol} / {strategy} / {env} / run_id={run_id or 'NULL'}: {len(closed_df)} closed trades")

    print(f"\nTotal closed trades written: {total_closed}")


def main():
    engine = get_db_engine()
    rebuild_closed_trades(engine)


if __name__ == "__main__":
    main()