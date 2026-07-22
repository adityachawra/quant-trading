import os
import pandas as pd
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy import text


load_dotenv()

def get_db_engine():
    """Builds a connection to the Postgres database using credentials from .env."""
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")

    connection_string = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return sa.create_engine(connection_string)


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
    """Removes bonds/govt securities, rows with missing trading symbols,
    and categorizes remaining rows as EQUITY, ETF, or INDEX."""

    bond_pattern = r'SDL|GOI|SGB|%|T-BILL|GILT|GS \d'
    is_bond = df['name'].str.contains(bond_pattern, na=False, regex=True, case=False)
    df_clean = df[~is_bond].copy()

    # New: exclude rows where tradingsymbol is missing, blank, or just whitespace
    has_no_name = df_clean['name'].isna() | (df_clean['name'].str.strip() == '')
    print(f"Rows with missing/blank name: {has_no_name.sum()}")
    df_clean = df_clean[~has_no_name].copy()

    is_index = df_clean['segment'] == 'INDICES'
    is_etf = df_clean['name'].str.contains('ETF', na=False, case=False) & ~is_index

    df_clean['category'] = 'EQUITY'
    df_clean.loc[is_index, 'category'] = 'INDEX'
    df_clean.loc[is_etf, 'category'] = 'ETF'

    output_columns = ['instrument_token', 'tradingsymbol', 'name', 'segment',
                       'instrument_type', 'exchange', 'category']
    return df_clean[output_columns]

def save_instruments_to_postgres(df):
    """Upserts the filtered instrument list into the instruments table."""
    engine = get_db_engine()

    df = df.copy()
    df['updated_at'] = pd.Timestamp.now()

    # Step 1: dump the DataFrame into a temporary "staging" table.
    # if_exists='replace' means this staging table gets wiped and recreated fresh every run.
    df.to_sql('instruments_staging', engine, if_exists='replace', index=False)

    # Step 2: merge staging data into the real instruments table.
    # ON CONFLICT means: if a row with this exchange+tradingsymbol already exists,
    # UPDATE it with the new values instead of erroring out.
    upsert_query = text("""
        INSERT INTO instruments (instrument_token, tradingsymbol, name, segment,
                                   instrument_type, exchange, category, updated_at)
        SELECT instrument_token, tradingsymbol, name, segment,
               instrument_type, exchange, category, updated_at
        FROM instruments_staging
        ON CONFLICT (exchange, tradingsymbol)
        DO UPDATE SET
            instrument_token = EXCLUDED.instrument_token,
            name = EXCLUDED.name,
            segment = EXCLUDED.segment,
            instrument_type = EXCLUDED.instrument_type,
            category = EXCLUDED.category,
            updated_at = EXCLUDED.updated_at;
    """)

    with engine.begin() as conn:
        conn.execute(upsert_query)
        conn.execute(text("DROP TABLE instruments_staging;"))

    print(f"Upserted {len(df)} instruments into Postgres.")


def main():
    print("Fetching instruments from Kite...")
    raw_df = fetch_nse_instruments()
    print(f"Fetched {len(raw_df)} raw instruments")

    print("Filtering...")
    filtered_df = filter_instruments(raw_df)
    filtered_df.to_csv("data/instruments_filtered.csv", index=False)
    print(f"Saved {len(filtered_df)} filtered instruments to CSV")

    print("Saving to Postgres...")
    save_instruments_to_postgres(filtered_df)


if __name__ == "__main__":
    main()