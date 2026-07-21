import os
import pandas as pd
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()


def fetch_nse_instruments():
    """Fetches all NSE instruments from Kite and returns them as a DataFrame."""
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
    kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))

    instruments = kite.instruments(exchange="NSE")
    df = pd.DataFrame(instruments)

    os.makedirs("data/cache", exist_ok=True)
    df.to_csv("data/cache/nse_instruments_raw.csv", index=False)

    return df


def filter_instruments(df):
    """Removes bonds/govt securities and categorizes remaining rows as EQUITY, ETF, or INDEX."""
    bond_pattern = r'SDL|GOI|SGB|%|T-BILL|GILT|GS \d'
    is_bond = df['name'].str.contains(bond_pattern, na=False, regex=True, case=False)
    df_no_bonds = df[~is_bond].copy()

    is_index = df_no_bonds['segment'] == 'INDICES'
    is_etf = df_no_bonds['name'].str.contains('ETF', na=False, case=False) & ~is_index

    df_no_bonds['category'] = 'EQUITY'
    df_no_bonds.loc[is_index, 'category'] = 'INDEX'
    df_no_bonds.loc[is_etf, 'category'] = 'ETF'

    output_columns = ['instrument_token', 'tradingsymbol', 'name', 'segment',
                       'instrument_type', 'exchange', 'category']
    return df_no_bonds[output_columns]


def main():
    print("Fetching instruments from Kite...")
    raw_df = fetch_nse_instruments()
    print(f"Fetched {len(raw_df)} raw instruments")

    print("Filtering...")
    filtered_df = filter_instruments(raw_df)
    filtered_df.to_csv("data/cache/instruments_filtered.csv", index=False)
    print(f"Saved {len(filtered_df)} filtered instruments to data/instruments_filtered.csv")


if __name__ == "__main__":
    main()