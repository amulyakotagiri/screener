"""
NSE Swing Trade Screener
- Creates permanent "Universe" tab (full NSE EQ list)
- Screens all stocks daily
- Writes only passing stocks to a new date-stamped tab
"""

import os
import sys
import time
import json
import io
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import requests
import gspread
from google.oauth2.service_account import Credentials
from dhanhq import DhanContext, dhanhq

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Force unbuffered logs
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# ====================== CONFIG ======================
IST = timezone(timedelta(hours=5, minutes=30))

CLIENT_ID       = os.environ.get("DHAN_CLIENT_ID", "").strip()
ACCESS_TOKEN    = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
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

# ====================== TOKEN ======================
def get_valid_token() -> str:
    if not CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID secret is missing")
    if not ACCESS_TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN secret is missing")
    print(f"[AUTH] Client ID: {CLIENT_ID}", flush=True)
    print(f"[AUTH] Token length: {len(ACCESS_TOKEN)}", flush=True)
    return ACCESS_TOKEN

# ====================== GOOGLE SHEETS ======================
def get_google_spreadsheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if not creds_json:
        for f in ["service_account.json", "credentials.json"]:
            if os.path.exists(f):
                with open(f, encoding="utf-8") as fh:
                    creds_json = fh.read().strip()
                break
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS secret is missing")

    if creds_json.startswith("{"):
        creds_dict = json.loads(creds_json)
    else:
        with open(creds_json, encoding="utf-8") as f:
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
    print(f"[SHEETS] Connected to: {sh.title}", flush=True)
    return sh

def fetch_nse_equity_list() -> pd.DataFrame:
    print("[UNIVERSE] Downloading instrument master...", flush=True)
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(io.BytesIO(resp.content), low_memory=False)
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
    print(f"[UNIVERSE] Found {len(result)} NSE EQ stocks", flush=True)
    return result

def sync_or_get_universe(spreadsheet) -> pd.DataFrame:
    universe_sheet = None
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "universe":
            universe_sheet = ws
            break

    # Create or force-refresh Universe
    if universe_sheet is None or UPDATE_UNIVERSE:
        print("[UNIVERSE] Creating / refreshing Universe tab...", flush=True)
        df = fetch_nse_equity_list()

        if universe_sheet is None:
            universe_sheet = spreadsheet.add_worksheet(
                title="Universe", rows=len(df) + 50, cols=5
            )
        else:
            universe_sheet.clear()

        data = [["Security ID", "Symbol", "Company Name"]] + df.astype(str).values.tolist()
        universe_sheet.update(data, "A1")

        try:
            universe_sheet.format("A1:C1", {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.1, "green": 0.2, "blue": 0.35}
            })
            universe_sheet.freeze(rows=1)
        except Exception:
            pass

        print(f"[UNIVERSE] ✅ Universe tab now has {len(df)} stocks", flush=True)
        return df

    # Load existing Universe
    print("[UNIVERSE] Loading existing Universe tab...", flush=True)
    vals = universe_sheet.get_all_values()
    if len(vals) > 50:
        headers = [h.strip().lower() for h in vals[0]]
        sec_idx = next((i for i, h in enumerate(headers) if "security" in h or "id" in h), None)
        sym_idx = next((i for i, h in enumerate(headers) if "symbol" in h), None)
        name_idx = next((i for i, h in enumerate(headers) if "company" in h or "name" in h), None)

        if sec_idx is not None and sym_idx is not None:
            rows = []
            for r in vals[1:]:
                if len(r) > max(sec_idx, sym_idx):
                    sid = str(r[sec_idx]).strip()
                    sym = str(r[sym_idx]).strip()
                    name = str(r[name_idx]).strip() if name_idx is not None and len(r) > name_idx else sym
                    if sid and sym:
                        rows.append({"Security ID": sid, "Symbol": sym, "Company Name": name})
            if len(rows) > 50:
                print(f"[UNIVERSE] Loaded {len(rows)} stocks from existing tab", flush=True)
                return pd.DataFrame(rows)

    # Fallback if existing tab is bad
    print("[UNIVERSE] Existing tab incomplete → re-downloading...", flush=True)
    return fetch_nse_equity_list()

def write_daily_results(spreadsheet, df: pd.DataFrame):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"[SHEETS] Writing daily results to tab '{today}'...", flush=True)

    try:
        old = spreadsheet.worksheet(today)
        spreadsheet.del_worksheet(old)
    except gspread.WorksheetNotFound:
        pass

    ws = spreadsheet.add_worksheet(title=today, rows=max(len(df) + 30, 50), cols=25)

    if not df.empty:
        data = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
        ws.update(data, "A1")
        try:
            ws.format("A1:Z1", {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.13, "green": 0.55, "blue": 0.28}
            })
            ws.freeze(rows=1)
        except Exception:
            pass
    else:
        ws.update([["Message"], ["No stocks met the criteria today."]], "A1")

    print(f"[SHEETS] ✅ Wrote {len(df)} rows → '{today}'", flush=True)
    print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}", flush=True)

# ====================== DATA + SCREENING ======================
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
                "volume": data.get("volume", [0] * len(data["close"])),
                "timestamp": data["timestamp"]
            })
            df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize(None)
            return df.sort_values("date").dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        except Exception:
            if attempt < retries:
                time.sleep(1)
                continue
            return None
    return None

def classify_ma20_trend(closes: pd.Series):
    if len(closes) < SMA_SHORT + FLAT_MA_LOOKBACK_DAYS:
        return "N/A", np.nan
    sma_series = [
        closes.iloc[len(closes) - i - SMA_SHORT : len(closes) - i].mean()
        for i in range(FLAT_MA_LOOKBACK_DAYS - 1, -1, -1)
    ]
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
        sma20 = df["close"].iloc[idx - SMA_SHORT + 1 : idx + 1].mean()
        sma40 = df["close"].iloc[idx - SMA_LONG + 1 : idx + 1].mean()
        low  = sma20 * (1 - PULLBACK_BAND_PCT)
        high = sma20 * (1 + PULLBACK_BAND_PCT)
        if low <= df["low"].iloc[idx] <= high and df["close"].iloc[idx] > sma40:
            return True
    return False

# ====================== MAIN ======================
def run_screener():
    now = datetime.now(IST)
    print("=" * 65, flush=True)
    print(f"[{now.strftime('%Y-%m-%d %I:%M %p IST')}] NSE Swing Screener started", flush=True)
    print("=" * 65, flush=True)

    global dhan
    token = get_valid_token()
    dhan = dhanhq(DhanContext(CLIENT_ID, token))

    spreadsheet = get_google_spreadsheet()
    universe = sync_or_get_universe(spreadsheet)
    total = len(universe)
    print(f"[SCREENER] Screening {total} stocks from Universe...", flush=True)

    to_date = now.date().isoformat()
    from_date = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
    print(f"[DATA] Range: {from_date} → {to_date}", flush=True)

    results = []
    for i, row in universe.iterrows():
        sec_id = str(row["Security ID"]).strip()
        symbol = str(row["Symbol"]).strip()
        name   = str(row.get("Company Name", symbol)).strip()

        if (i + 1) % 50 == 0 or (i + 1) == total or i == 0:
            print(f"[PROGRESS] {i+1}/{total} | Found: {len(results)}", flush=True)

        df = get_daily_ohlcv(sec_id, from_date, to_date)
        time.sleep(API_REQUEST_DELAY)

        if df is None or len(df) < MIN_DATA_DAYS:
            continue
        if (now.replace(tzinfo=None) - df["date"].iloc[-1]).days > 7:
            continue

        closes  = df["close"]
        opens   = df["open"]
        highs   = df["high"]
        volumes = df["volume"]

        last_close  = closes.iloc[-1]
        last_open   = opens.iloc[-1]
        last_high   = highs.iloc[-1]
        last_vol    = volumes.iloc[-1]
        prev_high   = highs.iloc[-2]
        avg_vol_20  = volumes.iloc[-20:].mean()

        if last_close < MIN_PRICE or avg_vol_20 < MIN_AVG_VOLUME_20:
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
        vol_ok   = last_vol > VOLUME_SPIKE_MULTIPLIER * avg_vol_20

        if not (is_green and close_ok and vol_ok):
            continue

        ma_label, ma_slope = classify_ma20_trend(closes)

        results.append({
            "Company": name,
            "Symbol": symbol,
            "Security ID": sec_id,
            "Last Bar Date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
            "Last Close": round(last_close, 2),
            "Open": round(last_open, 2),
            "High": round(last_high, 2),
            "Low": round(df["low"].iloc[-1], 2),
            "SMA 20": round(sma20, 2),
            "SMA 40": round(sma40, 2),
            "SMA 200": round(sma200, 2),
            "20 MA Trend": ma_label,
            "20 MA Slope %/day": ma_slope,
            "Latest Volume": int(last_vol),
            "Avg Vol (20)": int(avg_vol_20),
            "Days of Data": len(df)
        })

    if results:
        out = pd.DataFrame(results).sort_values(
            ["20 MA Trend", "20 MA Slope %/day"], ascending=[True, False]
        )
        print(f"\n✅ Found {len(out)} setups", flush=True)
        write_daily_results(spreadsheet, out)
    else:
        print("\nNo stocks met the criteria today", flush=True)
        write_daily_results(spreadsheet, pd.DataFrame([{"Message": "No stocks met the criteria today."}]))

    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %I:%M %p IST')}] Finished", flush=True)

if __name__ == "__main__":
    run_screener()
