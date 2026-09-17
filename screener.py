print("=" * 70)
print("DEBUG: screener.py started")
print("=" * 70)

import sys
print("Python version:", sys.version)

try:
    import config
    print("✓ config imported")
    print("  DHAN_CLIENT_ID set:", bool(config.DHAN_CLIENT_ID))
    print("  DHAN_ACCESS_TOKEN set:", bool(config.DHAN_ACCESS_TOKEN))
    print("  SPREADSHEET_ID set:", bool(config.SPREADSHEET_ID))
    print("  GOOGLE_CREDENTIALS_FILE:", config.GOOGLE_CREDENTIALS_FILE)
except Exception as e:
    print("✗ FAILED to import config:", e)
    raise

try:
    import Googlesheets as gs
    print("✓ Googlesheets imported")
except Exception as e:
    print("✗ FAILED to import Googlesheets:", e)
    raise

try:
    from dhanhq import DhanContext, dhanhq
    print("✓ dhanhq imported")
except Exception as e:
    print("✗ FAILED to import dhanhq:", e)
    raise

import time
from datetime import datetime, timedelta, date
from pathlib import Path
import pandas as pd
import numpy as np
import requests

print("✓ All other imports successful")
print("-" * 70)

# ------------------------------------------------------------------
# Token helpers
# ------------------------------------------------------------------
def renew_access_token(client_id: str, current_token: str) -> str | None:
    url = "https://api.dhan.co/v2/RenewToken"
    headers = {
        "access-token": current_token,
        "dhanClientId": client_id,
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(f"[TOKEN] Renew HTTP status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("accessToken") or data.get("access_token")
            if new_token:
                print("[TOKEN] Renewed successfully")
                return new_token
        print(f"[TOKEN] Renewal failed: {r.text[:200]}")
    except Exception as e:
        print(f"[TOKEN] Exception: {e}")
    return None

def get_valid_token() -> str:
    if not config.DHAN_CLIENT_ID or not config.DHAN_ACCESS_TOKEN:
        raise RuntimeError("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in secrets")
    print("[TOKEN] Attempting token renewal...")
    renewed = renew_access_token(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN)
    return renewed or config.DHAN_ACCESS_TOKEN

# ------------------------------------------------------------------
# Instrument master
# ------------------------------------------------------------------
def fetch_nse_equity_list() -> pd.DataFrame:
    print("\n[INSTRUMENTS] Downloading Dhan instrument master (ONE-TIME)...")
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip().upper() for c in df.columns]

    print(f"[INSTRUMENTS] Raw instruments downloaded: {len(df):,}")

    mask = (
        (df["SEM_EXM_EXCH_ID"].astype(str).str.upper().str.strip() == "NSE")
        & (df["SEM_SEGMENT"].astype(str).str.upper().str.strip() == "E")
        & (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper().str.strip() == "EQUITY")
    )
    eq = df.loc[mask].copy()
    print(f"[INSTRUMENTS] After NSE + Equity filter: {len(eq):,}")

    result = pd.DataFrame({
        "security_id": eq["SEM_SMST_SECURITY_ID"].astype(str).str.strip(),
        "symbol":      eq["SEM_TRADING_SYMBOL"].astype(str).str.strip(),
        "name":        eq.get("SEM_CUSTOM_SYMBOL", eq["SEM_TRADING_SYMBOL"]).astype(str).str.strip(),
    })

    result = result.replace({
        "security_id": {"nan": np.nan, "": np.nan},
        "symbol": {"nan": np.nan, "": np.nan}
    })
    result = result.dropna(subset=["security_id", "symbol"])
    result = result[result["security_id"].str.fullmatch(r"\d+")]
    result = result.drop_duplicates(subset=["security_id"])
    result = result.drop_duplicates(subset=["symbol"], keep="first")
    result = result.sort_values("symbol").reset_index(drop=True)

    print(f"[INSTRUMENTS] FINAL NSE EQUITY UNIVERSE: {len(result):,}")
    return result

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def run_screener():
    print("=" * 70)
    print(f"[{datetime.now()}] NSE SWING SCREENER STARTED")
    print("=" * 70)

    token = get_valid_token()
    print("[TOKEN] Token obtained")
    dhan = dhanhq(DhanContext(config.DHAN_CLIENT_ID, token))
    print("[DHAN] Client created")

    to_date = date.today()
    from_date = to_date - timedelta(days=config.HISTORY_DAYS)
    print(f"[DATA] Date range: {from_date} → {to_date}")

    # 1. Load or create Universe
    print("\n[UNIVERSE] Trying to load existing Universe tab...")
    instruments = gs.load_universe()

    if instruments is None:
        print("[UNIVERSE] No existing Universe – downloading full list now...")
        instruments = fetch_nse_equity_list()
        print("[UNIVERSE] Writing to Google Sheet...")
        gs.write_universe(instruments)
    else:
        print("[UNIVERSE] Using existing list from Google Sheet")
        instruments = instruments[["security_id", "symbol", "name"]].copy()
        instruments["security_id"] = instruments["security_id"].astype(str)
        instruments["symbol"] = instruments["symbol"].astype(str)

    total = len(instruments)
    print(f"\n[UNIVERSE] Ready to screen {total:,} stocks")
    print("If you see this line, the Universe step succeeded.")
    print("The rest of the screener will now start (this takes time)...")

    # For now we stop here so you can confirm the Universe was created
    # Comment out the next line once you confirm the Universe tab exists
    print("\n>>> DEBUG STOP – Universe step completed successfully <<<")
    print(">>> Check your Google Sheet for the 'Universe' tab <<<")
    return

    # ---------- the rest of the original screening code would continue here ----------

if __name__ == "__main__":
    run_screener()
