"""
Transaction cost model for Indian equity trades (Zerodha rates, 2026).

Mirrors Zerodha's brokerage calculator formula so results can be validated
against it directly. Statutory charges (STT, exchange, SEBI, stamp duty, GST)
are the same at every broker; brokerage and DP are Zerodha-specific.

IMPORTANT: exchange transaction rates are indicative for 2026 and can change
with policy. The real Zerodha contract note is always the final word — this
module is a close estimate, to be reconciled against actual contract notes
during live trading (Phase 3).
"""

from dataclasses import dataclass
import pandas as pd


# --- Rate constants (equity, Zerodha, 2026) ---
# Kept as named constants at the top so they're easy to find and update
# when rates change, rather than being buried as "magic numbers" in the logic.

# Delivery
DELIVERY_BROKERAGE_RATE = 0.0            # ₹0 on delivery
DELIVERY_STT_RATE = 0.001               # 0.1%, BOTH buy and sell
DELIVERY_STAMP_DUTY_RATE = 0.00015      # 0.015%, BUY side only

# Intraday
INTRADAY_BROKERAGE_PER_ORDER = 20.0     # ₹20 or 0.03%, whichever is LOWER
INTRADAY_BROKERAGE_RATE = 0.0003        # 0.03%
INTRADAY_STT_RATE = 0.00025             # 0.025%, SELL side only
INTRADAY_STAMP_DUTY_RATE = 0.00003      # 0.003%, BUY side only

# Common to both
EXCHANGE_TXN_RATE = 0.0000345           # ~0.00345% of turnover (NSE equity)
SEBI_RATE = 10 / 1_00_00_000            # ₹10 per crore  (1 crore = 10,000,000)
GST_RATE = 0.18                         # 18%, applied to (brokerage + exchange + SEBI)
DP_CHARGE_PER_SELL = 15.34              # flat ₹15.34 incl. GST, per sell (delivery only)


@dataclass
class CostBreakdown:
    """Holds the itemized cost of one BUY+SELL round trip, so you can see
    exactly where the money went — not just a single total. Useful for the
    Phase 3 reconciliation against contract notes."""
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp_duty: float
    gst: float
    dp_charge: float
    total: float


def leg_cost(turnover: float, side: str, is_intraday: bool) -> dict:
    """
    Computes all charges for a SINGLE leg (one buy, or one sell).
    A round-trip trade calls this twice — once for entry, once for exit.

    turnover : the rupee value of this leg (price * quantity)
    side     : 'buy' or 'sell' — matters because STT/stamp duty differ by side
    is_intraday : True for intraday, False for delivery
    """
    if is_intraday:
        brokerage = min(INTRADAY_BROKERAGE_PER_ORDER, INTRADAY_BROKERAGE_RATE * turnover)
        stt = INTRADAY_STT_RATE * turnover if side == "sell" else 0.0
        stamp_duty = INTRADAY_STAMP_DUTY_RATE * turnover if side == "buy" else 0.0
    else:
        brokerage = DELIVERY_BROKERAGE_RATE * turnover   # 0 for delivery
        stt = DELIVERY_STT_RATE * turnover               # both sides for delivery
        stamp_duty = DELIVERY_STAMP_DUTY_RATE * turnover if side == "buy" else 0.0

    exchange_txn = EXCHANGE_TXN_RATE * turnover
    sebi = SEBI_RATE * turnover

    # GST applies ONLY to brokerage + exchange charges + SEBI — never to
    # STT, stamp duty, or the whole trade value. A very common mistake is
    # applying GST to the entire turnover, which massively overstates cost.
    gst = GST_RATE * (brokerage + exchange_txn + sebi)

    # DP charge: flat fee, only on the SELL side, only for delivery
    dp_charge = DP_CHARGE_PER_SELL if (side == "sell" and not is_intraday) else 0.0

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "sebi": sebi,
        "stamp_duty": stamp_duty,
        "gst": gst,
        "dp_charge": dp_charge,
    }


def round_trip_cost(entry_price: float, exit_price: float, quantity: int,
                    is_intraday: bool = False) -> CostBreakdown:
    """
    Total cost of a complete BUY-then-SELL round trip.

    Returns a CostBreakdown with every charge itemized plus the total,
    so you can see exactly what a trade cost and why.
    """
    buy_turnover = entry_price * quantity
    sell_turnover = exit_price * quantity

    buy = leg_cost(buy_turnover, side="buy", is_intraday=is_intraday)
    sell = leg_cost(sell_turnover, side="sell", is_intraday=is_intraday)

    # Sum each charge type across both legs
    brokerage = buy["brokerage"] + sell["brokerage"]
    stt = buy["stt"] + sell["stt"]
    exchange_txn = buy["exchange_txn"] + sell["exchange_txn"]
    sebi = buy["sebi"] + sell["sebi"]
    stamp_duty = buy["stamp_duty"] + sell["stamp_duty"]
    gst = buy["gst"] + sell["gst"]
    dp_charge = buy["dp_charge"] + sell["dp_charge"]

    total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst + dp_charge

    return CostBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp_duty=stamp_duty,
        gst=gst,
        dp_charge=dp_charge,
        total=total,
    )

def build_cost_report(closed_trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes a closed_trades DataFrame (already has entry/exit price, quantity,
    dates, holding_days) and adds a full itemized cost breakdown per trade,
    recomputed via round_trip_cost(). Nothing here is stored back in the
    database — this is purely a reporting/display layer.
    """
    breakdown_rows = []

    for row in closed_trades_df.itertuples():
        cost = round_trip_cost(
            entry_price=row.entry_price,
            exit_price=row.exit_price,
            quantity=row.quantity,
            is_intraday=False,
        )
        breakdown_rows.append({
            "symbol": row.symbol,
            "entry_time": row.entry_time,
            "exit_time": row.exit_time,
            "holding_days": row.holding_days,
            "brokerage": cost.brokerage,
            "stt": cost.stt,
            "exchange_txn": cost.exchange_txn,
            "sebi": cost.sebi,
            "stamp_duty": cost.stamp_duty,
            "gst": cost.gst,
            "dp_charge": cost.dp_charge,
            "total_cost": cost.total,
            "net_pnl": row.net_pnl,
        })

    return pd.DataFrame(breakdown_rows)


if __name__ == "__main__":
    # --- Validation trade: run THIS through Zerodha's online calculator too ---
    # Buy 40 shares at ₹2500 (₹1,00,000), sell at ₹2550 (₹1,02,000), delivery.
    result = round_trip_cost(entry_price=2500, exit_price=2550, quantity=40, is_intraday=False)

    print("Round-trip cost breakdown (delivery, ₹1,00,000 buy / ₹1,02,000 sell):")
    print(f"  Brokerage:       ₹{result.brokerage:.2f}")
    print(f"  STT:             ₹{result.stt:.2f}")
    print(f"  Exchange txn:    ₹{result.exchange_txn:.2f}")
    print(f"  SEBI:            ₹{result.sebi:.2f}")
    print(f"  Stamp duty:      ₹{result.stamp_duty:.2f}")
    print(f"  GST:             ₹{result.gst:.2f}")
    print(f"  DP charge:       ₹{result.dp_charge:.2f}")
    print(f"  --------------------------------")
    print(f"  TOTAL:           ₹{result.total:.2f}")