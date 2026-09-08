"""
NSE Swing Trade Screener
- Full NSE equity universe
- Live daily data
- Auto token renewal (best effort)
- Writes results to Google Spreadsheet
- Designed for GitHub Actions (5 PM IST daily)
"""

import os
import time
import json
from datetime import datetime, timedelta, date

import pandas as pd
import numpy as np
import requests
import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials
from dhanhq import DhanContext, dhanhq

# ====================== CONFIG ======================
CLIENT_ID      = os.environ.get("DHAN_CLIENT_ID", "")
ACCESS_TOKEN   = os.environ.get("DHAN_ACCESS_TOKEN", "")
SPREADSHEET_ID = "10iZtgmYm3QqW52hDi52p2R5qmC5mAGLs0qKcXJub61Q"

# Filters
SMA_SHORT     = 20
SMA_LONG      = 40
SMA_LONG_TERM = 200
PULLBACK_LOOKBACK_DAYS = 4
PULLBACK_BAND_PCT      = 0.02
VOLUME_SPIKE_MULTIPLIER = 1.2
REQUIRE_CLOSE_ABOVE_PREV_HIGH = True
MIN_AVG_VOLUME_20 = 200_000
MIN_PRICE         = 100.0
MIN_DATA_DAYS     = 200
FLAT_MA_LOOKBACK_DAYS = 10
STRONGLY_RISING_THRESHOLD  = 0.15
FLAT_THRESHOLD             = 0.05
STRONGLY_FALLING_THRESHOLD = -0.15
HISTORY_DAYS = 400

# ====================== TOKEN ======================
def renew_access_token(client_id: str, current_token: str) -> str | None:
    url = "https://api.dhan.co/v2/RenewToken"
    headers = {
        "access-token": current_token,
        "dhanClientId": client_id,
        "Accept": "application/json"
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
            print("[TOKEN] 200 but no token in response")
        else:
            print("[TOKEN] Renewal failed:", r.text[:250])
        return None
    except Exception as e:
        print(f"[TOKEN] Exception: {e}")
        return None


def get_valid_token() -> str:
    if not CLIENT_ID or not ACCESS_TOKEN:
        raise RuntimeError("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN")

    print("[TOKEN] Attempting auto-renewal...")
    renewed = renew_access_token(CLIENT_ID, ACCESS_TOKEN)
    if renewed:
        return renewed

    print("[TOKEN] Using token from secrets")
    return ACCESS_TOKEN


# ====================== GOOGLE SHEETS ======================
def write_to_google_sheet(df: pd.DataFrame):
    print("[SHEETS] Connecting...")

    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS secret")

    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)

    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    sheet_name = date.today().strftime("%Y-%m-%d")

    try:
        old = spreadsheet.worksheet(sheet_name)
        spreadsheet.del_worksheet(old)
        print(f"[SHEETS] Deleted old sheet: {sheet_name}")
    except gspread.WorksheetNotFound:
        pass

    worksheet = spreadsheet.add_worksheet(
        title=sheet_name,
        rows=max(len(df) + 30, 100),
        cols=25
    )

    set_with_dataframe(
        worksheet, df,
        include_index=False,
        include_column_header=True,
        resize=True
    )

    worksheet.format("A1:Z1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.3}
    })

    print(f"[SHEETS] Wrote {len(df)} rows → '{sheet_name}'")
    print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


# ====================== INSTRUMENTS ======================
def fetch_nse_equity_list() -> pd.DataFrame:
    print("Downloading NSE instrument master...")
    df = pd.read_csv(
        "https://images.dhan.co/api-data/api-scrip-master.csv",
        low_memory=False
    )
    df.columns = [c.strip().upper() for c in df.columns]

    mask = (
        (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE") &
        (df["SEM_SEGMENT"].astype(str).str.upper() == "E") &
        (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "EQUITY")
    )
    if "SEM_SERIES" in df.columns:
        mask = mask & (df["SEM_SERIES"].astype(str).str.upper() == "EQ")

    eq = df.loc[mask].copy()

    result = pd.DataFrame({
        "security_id": eq["SEM_SMST_SECURITY_ID"].astype(str).str.strip(),
        "symbol":      eq["SEM_TRADING_SYMBOL"].astype(str).str.strip(),
        "name":        eq["SEM_CUSTOM_SYMBOL"].astype(str).str.strip()
                       if "SEM_CUSTOM_SYMBOL" in eq.columns
                       else eq["SEM_TRADING_SYMBOL"].astype(str).str.strip()
    })

    result = result.dropna(subset=["security_id", "symbol"])
    result = result[result["symbol"].str.len() > 0]
    result = result.drop_duplicates(subset=["security_id"])
    print(f"Found {len(result)} NSE equity instruments")
    return result.reset_index(drop=True)


# ====================== DATA ======================
def get_daily_ohlcv(security_id: str, from_date: str, to_date: str) -> pd.DataFrame | None:
    try:
        resp = dhan.historical_daily_data(
            security_id=str(security_id),
            exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            from_date=from_date,
            to_date=to_date
        )

        if not resp or resp.get("status") != "success":
            return None

        data = resp.get("data") or {}
        if not data or "close" not in data or len(data["close"]) == 0:
            return None

        df = pd.DataFrame({
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data.get("volume", [0] * len(data["close"])),
            "timestamp": data["timestamp"]
        })
        df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize(None)
        df = (
            df.sort_values("date")
              .dropna(subset=["open", "high", "low", "close"])
              .reset_index(drop=True)
        )
        return df
    except Exception:
        return None


def classify_ma20_trend(closes: pd.Series):
    if len(closes) < SMA_SHORT + FLAT_MA_LOOKBACK_DAYS:
        return "N/A", np.nan

    sma_series = []
    for i in range(FLAT_MA_LOOKBACK_DAYS - 1, -1, -1):
        end = len(closes) - i
        start = end - SMA_SHORT
        if start < 0:
            return "N/A", np.nan
        sma_series.append(closes.iloc[start:end].mean())

    y = np.array(sma_series)
    slope = np.polyfit(np.arange(len(y)), y, 1)[0]
    slope_pct = (slope / y[-1]) * 100

    if slope_pct > STRONGLY_RISING_THRESHOLD:
        label = "Strongly Rising"
    elif slope_pct < STRONGLY_FALLING_THRESHOLD:
        label = "Strongly Falling"
    elif abs(slope_pct) <= FLAT_THRESHOLD:
        label = "Flat"
    elif slope_pct > 0:
        label = "Rising"
    else:
        label = "Falling"
    return label, round(slope_pct, 3)


def check_pullback(df: pd.DataFrame) -> bool:
    last_idx = len(df) - 1
    for i in range(PULLBACK_LOOKBACK_DAYS):
        idx = last_idx - i
        if idx < SMA_LONG:
            break
        sma20 = df["close"].iloc[idx - SMA_SHORT + 1 : idx + 1].mean()
        sma40 = df["close"].iloc[idx - SMA_LONG + 1 : idx + 1].mean()
        band_low  = sma20 * (1 - PULLBACK_BAND_PCT)
        band_high = sma20 * (1 + PULLBACK_BAND_PCT)
        if band_low <= df["low"].iloc[idx] <= band_high and df["close"].iloc[idx] > sma40:
            return True
    return False


# ====================== MAIN ======================
def run_screener():
    print("=" * 60)
    print(f"[{datetime.now()}] NSE Swing Screener started")
    print("=" * 60)

    global dhan
    token = get_valid_token()
    ctx = DhanContext(CLIENT_ID, token)
    dhan = dhanhq(ctx)

    to_date   = date.today().isoformat()
    from_date = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    print(f"Data range: {from_date} → {to_date}")

    instruments = fetch_nse_equity_list()
    results = []
    total = len(instruments)

    for i, row in instruments.iterrows():
        if (i + 1) % 100 == 0:
            print(f"Progress: {i+1}/{total} | Found: {len(results)}")

        df = get_daily_ohlcv(row["security_id"], from_date, to_date)
        if df is None or len(df) < MIN_DATA_DAYS:
            continue

        last_date = df["date"].iloc[-1]
        if (datetime.now() - last_date).days > 5:
            continue

        closes  = df["close"]
        opens   = df["open"]
        highs   = df["high"]
        lows    = df["low"]
        volumes = df["volume"]

        last_close  = closes.iloc[-1]
        last_open   = opens.iloc[-1]
        last_high   = highs.iloc[-1]
        last_low    = lows.iloc[-1]
        last_volume = volumes.iloc[-1]
        prev_high   = highs.iloc[-2]
        prev_close  = closes.iloc[-2]

        avg_vol_20 = volumes.iloc[-20:].mean()
        if avg_vol_20 < MIN_AVG_VOLUME_20 or last_close < MIN_PRICE:
            continue

        sma20 = closes.iloc[-SMA_SHORT:].mean()
        sma40 = closes.iloc[-SMA_LONG:].mean()
        if not (last_close > sma40 and sma20 > sma40):
            continue

        if len(closes) < SMA_LONG_TERM:
            continue
        sma200 = closes.iloc[-SMA_LONG_TERM:].mean()
        if not (last_close > sma200 and sma20 > sma200):
            continue

        if not check_pullback(df):
            continue

        is_green = last_close > last_open
        close_ok = (last_close > prev_high) if REQUIRE_CLOSE_ABOVE_PREV_HIGH else (last_close > prev_close)
        vol_ok   = last_volume > VOLUME_SPIKE_MULTIPLIER * avg_vol_20

        if not (is_green and close_ok and vol_ok):
            continue

        ma_label, ma_slope = classify_ma20_trend(closes)

        results.append({
            "Company": row["name"],
            "Symbol": row["symbol"],
            "Security ID": row["security_id"],
            "Last Bar Date": last_date.strftime("%Y-%m-%d"),
            "Last Close": round(last_close, 2),
            "Open": round(last_open, 2),
            "High": round(last_high, 2),
            "Low": round(last_low, 2),
            "SMA 20": round(sma20, 2),
            "SMA 40": round(sma40, 2),
            "SMA 200": round(sma200, 2),
            "20 SMA > 40 SMA?": "YES",
            "20 SMA > 200 SMA?": "YES",
            "Close > 200 SMA?": "YES",
            "Pullback to 20 SMA?": "YES",
            "Signal Bar?": "YES",
            "20 MA Trend": ma_label,
            "20 MA Slope %/day": ma_slope,
            "Latest Volume": int(last_volume),
            "Avg Vol (20)": int(avg_vol_20),
            "Volume Spike?": "YES",
            "Days of Data": len(df)
        })
        time.sleep(0.12)

    if results:
        out_df = pd.DataFrame(results)
        print(f"\nFound {len(results)} setups")
        print(out_df[["Symbol", "Last Close", "20 MA Trend"]].to_string(index=False))
        write_to_google_sheet(out_df)
    else:
        print("\nNo stocks met the criteria today.")
        write_to_google_sheet(pd.DataFrame([{"Message": "No stocks met the swing trade criteria today."}]))

    print(f"\n[{datetime.now()}] Finished.")


if __name__ == "__main__":
    run_screener()
