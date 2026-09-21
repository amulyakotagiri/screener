"""
NSE Swing Trade Screener
Runs daily via GitHub Actions at 5 PM IST
"""

import os
import time
import json
import base64
from datetime import datetime, timedelta, date, timezone
import pandas as pd
import numpy as np
import requests
import gspread
from gspread_dataframe import set_with_dataframe, get_as_dataframe
from google.oauth2.service_account import Credentials
from dhanhq import DhanContext, dhanhq

try:
    import pyotp
except ImportError:
    pyotp = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ====================== CONFIG ======================
IST = timezone(timedelta(hours=5, minutes=30))

CLIENT_ID       = os.environ.get("DHAN_CLIENT_ID", "").strip()
ACCESS_TOKEN    = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
PIN             = os.environ.get("DHAN_PIN", "").strip()
TOTP_SECRET     = os.environ.get("DHAN_TOTP_SECRET", "").strip()
SPREADSHEET_ID  = os.environ.get("SPREADSHEET_ID", "").strip()
UPDATE_UNIVERSE = os.environ.get("UPDATE_UNIVERSE", "").strip().lower() in ("1", "true", "yes")

# Screener parameters
SMA_SHORT               = 20
SMA_LONG                = 40
SMA_LONG_TERM           = 200
PULLBACK_LOOKBACK_DAYS  = 4
PULLBACK_BAND_PCT       = 0.02
VOLUME_SPIKE_MULTIPLIER = 1.2
REQUIRE_CLOSE_ABOVE_PREV_HIGH = True
MIN_AVG_VOLUME_20       = 200_000
MIN_PRICE               = 100.0
MIN_DATA_DAYS           = 200
FLAT_MA_LOOKBACK_DAYS   = 10
STRONGLY_RISING_THRESHOLD  = 0.15
FLAT_THRESHOLD             = 0.05
STRONGLY_FALLING_THRESHOLD = -0.15
HISTORY_DAYS            = 400
API_REQUEST_DELAY       = 0.22

# ====================== TOKEN HELPERS ======================
def decode_jwt_payload(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode())
    except Exception:
        return None

def generate_token_via_totp(client_id: str, pin: str, totp_secret: str) -> str | None:
    if not pyotp:
        return None
    try:
        totp = pyotp.TOTP(totp_secret.replace(" ", ""))
        otp = totp.now()
        resp = requests.post(
            "https://auth.dhan.co/app/generateAccessToken",
            params={"dhanClientId": client_id, "pin": pin, "totp": otp},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("accessToken") or data.get("access_token")
            if token:
                print("[AUTH] Generated fresh token via TOTP")
                return token
        print(f"[AUTH] TOTP generation failed: {resp.text[:150]}")
    except Exception as e:
        print(f"[AUTH] TOTP exception: {e}")
    return None

def renew_access_token(client_id: str, current_token: str) -> str | None:
    """Only call this while the current token is still valid."""
    try:
        r = requests.get(
            "https://api.dhan.co/v2/RenewToken",
            headers={
                "access-token": current_token,
                "dhanClientId": client_id,
                "Accept": "application/json"
            },
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("accessToken") or data.get("access_token")
            if new_token:
                print("[AUTH] Token renewed successfully")
                return new_token
        print(f"[AUTH] Renewal failed (HTTP {r.status_code}): {r.text[:150]}")
    except Exception as e:
        print(f"[AUTH] Renewal exception: {e}")
    return None

def get_valid_token() -> str:
    if not CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID is missing")

    # Optional: auto-generate via PIN + TOTP
    if PIN and TOTP_SECRET:
        print("[AUTH] Trying TOTP auto-login...")
        auto = generate_token_via_totp(CLIENT_ID, PIN, TOTP_SECRET)
        if auto:
            return auto

    if not ACCESS_TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN is missing")

    claims = decode_jwt_payload(ACCESS_TOKEN)
    now_ts = int(datetime.now(timezone.utc).timestamp())

    if claims and "exp" in claims:
        exp_ts = claims["exp"]
        remaining_hours = (exp_ts - now_ts) / 3600

        exp_ist = datetime.fromtimestamp(exp_ts, tz=timezone.utc).astimezone(IST)
        print(f"[AUTH] Token valid until {exp_ist.strftime('%Y-%m-%d %I:%M %p IST')} "
              f"({remaining_hours:.1f} h left)")

        # Renew only while token is still alive and < 3 hours remaining
        if 0 < remaining_hours < 3:
            print("[AUTH] Token close to expiry → attempting safe renewal...")
            renewed = renew_access_token(CLIENT_ID, ACCESS_TOKEN)
            if renewed:
                return renewed
            print("[AUTH] Renewal failed, continuing with current token")

        if remaining_hours <= 0:
            raise RuntimeError(
                f"Token already expired on {exp_ist.strftime('%Y-%m-%d %I:%M %p IST')}. "
                "Generate a new token from web.dhan.co"
            )

    return ACCESS_TOKEN

# ====================== GOOGLE SHEETS ======================
def get_google_spreadsheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if not creds_json:
        for f in ["service_account.json", "credentials.json"]:
            if os.path.exists(f):
                with open(f) as fh:
                    creds_json = fh.read().strip()
                break

    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS secret is missing")

    if creds_json.startswith("{"):
        creds_dict = json.loads(creds_json)
    else:
        with open(creds_json) as f:
            creds_dict = json.load(f)

    credentials = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(SPREADSHEET_ID)
    print(f"[SHEETS] Connected to: {sh.title}")
    return sh

def fetch_nse_equity_list() -> pd.DataFrame:
    print("[UNIVERSE] Downloading instrument master...")
    df = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master.csv", low_memory=False)
    df.columns = [c.strip().upper() for c in df.columns]

    mask = (
        (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE") &
        (df["SEM_SEGMENT"].astype(str).str.upper() == "E") &
        (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "EQUITY")
    )
    if "SEM_SERIES" in df.columns:
        mask &= (df["SEM_SERIES"].astype(str).str.upper() == "EQ")

    eq = df.loc[mask].copy()
    result = pd.DataFrame({
        "Security ID": eq["SEM_SMST_SECURITY_ID"].astype(str).str.strip(),
        "Symbol": eq["SEM_TRADING_SYMBOL"].astype(str).str.strip(),
        "Company Name": eq.get("SEM_CUSTOM_SYMBOL", eq["SEM_TRADING_SYMBOL"]).astype(str).str.strip()
    })
    result = (result.dropna(subset=["Security ID", "Symbol"])
                    .drop_duplicates(subset=["Security ID"])
                    .sort_values("Symbol")
                    .reset_index(drop=True))
    print(f"[UNIVERSE] {len(result)} NSE EQ stocks")
    return result

def sync_or_get_universe(spreadsheet) -> pd.DataFrame:
    universe_sheet = None
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "universe":
            universe_sheet = ws
            break

    if universe_sheet is None or UPDATE_UNIVERSE:
        print("[UNIVERSE] Creating / refreshing Universe sheet...")
        df = fetch_nse_equity_list()
        if universe_sheet is None:
            universe_sheet = spreadsheet.add_worksheet(title="Universe", rows=len(df)+50, cols=5)
        else:
            universe_sheet.clear()
        set_with_dataframe(universe_sheet, df, include_index=False, resize=True)
        universe_sheet.format("A1:C1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.1, "green": 0.2, "blue": 0.35}
        })
        universe_sheet.freeze(rows=1)
        return df

    raw = get_as_dataframe(universe_sheet).dropna(how="all")
    col_map = {}
    for c in raw.columns:
        cl = str(c).lower()
        if "security" in cl or "id" in cl:
            col_map[c] = "Security ID"
        elif "symbol" in cl:
            col_map[c] = "Symbol"
        elif "company" in cl or "name" in cl:
            col_map[c] = "Company Name"
    raw = raw.rename(columns=col_map)

    if "Security ID" in raw.columns and "Symbol" in raw.columns:
        raw["Security ID"] = raw["Security ID"].astype(str).str.replace(r"\.0$", "", regex=True)
        if "Company Name" not in raw.columns:
            raw["Company Name"] = raw["Symbol"]
        valid = raw[raw["Security ID"].str.len() > 0]
        if len(valid) > 50:
            print(f"[UNIVERSE] Loaded {len(valid)} stocks from existing sheet")
            return valid[["Security ID", "Symbol", "Company Name"]]

    # fallback
    return fetch_nse_equity_list()

def write_daily_results(spreadsheet, df: pd.DataFrame):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        old = spreadsheet.worksheet(today)
        spreadsheet.del_worksheet(old)
    except gspread.WorksheetNotFound:
        pass

    ws = spreadsheet.add_worksheet(title=today, rows=max(len(df)+30, 50), cols=25)
    set_with_dataframe(ws, df, include_index=False, resize=True)
    if not df.empty:
        ws.format("A1:Z1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.13, "green": 0.55, "blue": 0.28}
        })
        ws.freeze(rows=1)
    print(f"[SHEETS] Wrote {len(df)} rows → '{today}'")
    print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")

# ====================== DATA & SCREENING ======================
def get_daily_ohlcv(security_id: str, from_date: str, to_date: str, retries=2) -> pd.DataFrame | None:
    for attempt in range(retries + 1):
        try:
            resp = dhan.historical_daily_data(
                security_id=str(security_id),
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=from_date,
                to_date=to_date
            )
            if not resp or resp.get("status") != "success":
                remarks = str(resp.get("remarks", "")) if resp else ""
                if "rate" in remarks.lower() and attempt < retries:
                    time.sleep(1.5)
                    continue
                return None

            data = resp.get("data") or {}
            if not data or "close" not in data:
                return None

            df = pd.DataFrame({
                "open": data["open"], "high": data["high"],
                "low": data["low"], "close": data["close"],
                "volume": data.get("volume", [0]*len(data["close"])),
                "timestamp": data["timestamp"]
            })
            df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize(None)
            return df.sort_values("date").dropna(subset=["open","high","low","close"]).reset_index(drop=True)
        except Exception:
            if attempt < retries:
                time.sleep(1)
                continue
            return None
    return None

def classify_ma20_trend(closes: pd.Series):
    if len(closes) < SMA_SHORT + FLAT_MA_LOOKBACK_DAYS:
        return "N/A", np.nan
    sma_series = [closes.iloc[len(closes)-i-SMA_SHORT : len(closes)-i].mean()
                  for i in range(FLAT_MA_LOOKBACK_DAYS-1, -1, -1)]
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
    last = len(df) - 1
    for i in range(PULLBACK_LOOKBACK_DAYS):
        idx = last - i
        if idx < SMA_LONG:
            break
        sma20 = df["close"].iloc[idx-SMA_SHORT+1:idx+1].mean()
        sma40 = df["close"].iloc[idx-SMA_LONG+1:idx+1].mean()
        low, high = sma20*(1-PULLBACK_BAND_PCT), sma20*(1+PULLBACK_BAND_PCT)
        if low <= df["low"].iloc[idx] <= high and df["close"].iloc[idx] > sma40:
            return True
    return False

# ====================== MAIN ======================
def run_screener():
    now = datetime.now(IST)
    print("=" * 65)
    print(f"[{now.strftime('%Y-%m-%d %I:%M %p IST')}] NSE Swing Screener started")
    print("=" * 65)

    global dhan
    token = get_valid_token()
    dhan = dhanhq(DhanContext(CLIENT_ID, token))

    spreadsheet = get_google_spreadsheet()
    universe = sync_or_get_universe(spreadsheet)
    total = len(universe)
    print(f"[SCREENER] Screening {total} stocks...")

    to_date = now.date().isoformat()
    from_date = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()

    results = []
    for i, row in universe.iterrows():
        sec_id = str(row["Security ID"]).strip()
        symbol = str(row["Symbol"]).strip()
        name = str(row.get("Company Name", symbol)).strip()

        if (i + 1) % 100 == 0 or (i + 1) == total:
            print(f"[PROGRESS] {i+1}/{total} | Found: {len(results)}")

        df = get_daily_ohlcv(sec_id, from_date, to_date)
        time.sleep(API_REQUEST_DELAY)

        if df is None or len(df) < MIN_DATA_DAYS:
            continue
        if (now.replace(tzinfo=None) - df["date"].iloc[-1]).days > 7:
            continue

        closes, opens, highs, volumes = df["close"], df["open"], df["high"], df["volume"]
        last_close, last_open, last_high, last_vol = closes.iloc[-1], opens.iloc[-1], highs.iloc[-1], volumes.iloc[-1]
        prev_high = highs.iloc[-2]
        avg_vol = volumes.iloc[-20:].mean()

        if last_close < MIN_PRICE or avg_vol < MIN_AVG_VOLUME_20:
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
        close_ok = last_close > prev_high if REQUIRE_CLOSE_ABOVE_PREV_HIGH else last_close > closes.iloc[-2]
        vol_ok = last_vol > VOLUME_SPIKE_MULTIPLIER * avg_vol
        if not (is_green and close_ok and vol_ok):
            continue

        ma_label, ma_slope = classify_ma20_trend(closes)
        results.append({
            "Company": name, "Symbol": symbol, "Security ID": sec_id,
            "Last Bar Date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
            "Last Close": round(last_close, 2),
            "Open": round(last_open, 2), "High": round(last_high, 2),
            "Low": round(df["low"].iloc[-1], 2),
            "SMA 20": round(sma20, 2), "SMA 40": round(sma40, 2), "SMA 200": round(sma200, 2),
            "20 MA Trend": ma_label, "20 MA Slope %/day": ma_slope,
            "Latest Volume": int(last_vol), "Avg Vol (20)": int(avg_vol),
            "Days of Data": len(df)
        })

    if results:
        out = pd.DataFrame(results).sort_values(["20 MA Trend", "20 MA Slope %/day"], ascending=[True, False])
        print(f"\nFound {len(out)} setups")
        write_daily_results(spreadsheet, out)
    else:
        print("\nNo stocks met the criteria today")
        write_daily_results(spreadsheet, pd.DataFrame([{"Message": "No stocks met the criteria today."}]))

    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %I:%M %p IST')}] Finished")

if __name__ == "__main__":
    run_screener()
