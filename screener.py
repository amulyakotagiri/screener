"""
NSE Swing Trade Screener
Runs daily via GitHub Actions at 5 PM IST
Syncs / reads NSE Universe sheet from Google Spreadsheet
Screens stocks using swing trade pullback & volume spike logic
Writes daily results to a new date-stamped sheet (YYYY-MM-DD)
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

ACCESS_TOKEN    = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
SPREADSHEET_ID  = os.environ.get("SPREADSHEET_ID", "10iZtgmYm3QqW52hDi52p2R5qmC5mAGLs0qKcXJub61Q").strip()
UPDATE_UNIVERSE = os.environ.get("UPDATE_UNIVERSE", "").strip().lower() in ("1", "true", "yes")

# Screener Filters
SMA_SHORT                      = 20
SMA_LONG                       = 40
SMA_LONG_TERM                  = 200
PULLBACK_LOOKBACK_DAYS         = 4
PULLBACK_BAND_PCT              = 0.02
VOLUME_SPIKE_MULTIPLIER        = 1.2
REQUIRE_CLOSE_ABOVE_PREV_HIGH  = True
MIN_AVG_VOLUME_20              = 200_000
MIN_PRICE                      = 100.0
MIN_DATA_DAYS                  = 200
FLAT_MA_LOOKBACK_DAYS          = 10
STRONGLY_RISING_THRESHOLD       = 0.15
FLAT_THRESHOLD                  = 0.05
STRONGLY_FALLING_THRESHOLD      = -0.15
HISTORY_DAYS                   = 400
API_REQUEST_DELAY              = 0.22  # Comply with Dhan 5 req/sec limit


# ====================== TOKEN MANAGEMENT ======================
def decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload to inspect expiration and claims without external libraries."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        return json.loads(payload_json)
    except Exception:
        return None


def generate_token_via_totp(client_id: str, pin: str, totp_secret: str) -> str | None:
    """Generate fresh Access Token using Dhan PIN and TOTP secret."""
    if not pyotp:
        print("[AUTH] pyotp library not installed, skipping TOTP auto-generation.")
        return None
    try:
        totp = pyotp.TOTP(totp_secret.replace(" ", ""))
        otp_code = totp.now()
        url = "https://auth.dhan.co/app/generateAccessToken"
        params = {
            "dhanClientId": client_id,
            "pin": pin,
            "totp": otp_code
        }
        resp = requests.post(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("accessToken") or data.get("access_token")
            if token:
                print(f"[AUTH] ✅ Successfully generated fresh Access Token via TOTP for client {client_id}!")
                return token
            print("[AUTH] TOTP response did not contain accessToken:", data)
        else:
            print(f"[AUTH] TOTP token generation failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[AUTH] Exception during TOTP token generation: {e}")
        return None


def verify_dhan_data_api(dhan_instance) -> tuple[bool, str]:
    """Test token validity using a live daily OHLCV query for benchmark stock (HDFC Bank: 1333)."""
    try:
        today = date.today().isoformat()
        past = (date.today() - timedelta(days=10)).isoformat()
        resp = dhan_instance.historical_daily_data(
            security_id="1333",
            exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            from_date=past,
            to_date=today
        )
        if resp and resp.get("status") == "success":
            return True, "HDFC Bank historical test query passed"
        remarks = resp.get("remarks", {}) if resp else "No response"
        return False, f"Test query failed: {remarks}"
    except Exception as e:
        return False, f"Connection error: {e}"


def renew_access_token(client_id: str, current_token: str) -> str | None:
    """
    Renew Dhan access token when close to expiry.
    NOTE: Dhan deactivates the old token when this succeeds, so only call when necessary.
    """
    url = "https://api.dhan.co/v2/RenewToken"
    headers = {
        "access-token": current_token,
        "dhanClientId": client_id,
        "Accept": "application/json"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("accessToken") or data.get("access_token")
            if new_token:
                print("[AUTH] ✅ Successfully renewed Dhan access token.")
                return new_token
        print(f"[AUTH] Renewal failed (HTTP {r.status_code}): {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[AUTH] Exception during token renewal: {e}")
        return None


def get_valid_token() -> str:
    """Obtain and validate an active Dhan access token."""
    if not CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID environment variable / secret is missing!")

    # 1. Check if hands-free PIN + TOTP auto-generation is available
    if PIN and TOTP_SECRET:
        print("[AUTH] Attempting automatic token generation using PIN + TOTP...")
        auto_token = generate_token_via_totp(CLIENT_ID, PIN, TOTP_SECRET)
        if auto_token:
            return auto_token
        print("[AUTH] Falling back to DHAN_ACCESS_TOKEN...")

    # 2. Validate provided DHAN_ACCESS_TOKEN
    if not ACCESS_TOKEN:
        raise RuntimeError(
            "No valid token found! Provide either:\n"
            "  1. DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (generated from web.dhan.co -> Access DhanHQ APIs)\n"
            "  2. Or DHAN_CLIENT_ID, DHAN_PIN, and DHAN_TOTP_SECRET for automatic daily login."
        )

    # Inspect token format
    claims = decode_jwt_payload(ACCESS_TOKEN)
    if not claims:
        print("[AUTH] ⚠️ WARNING: DHAN_ACCESS_TOKEN does not have standard 3-part JWT structure.")
        print("[AUTH] Ensure you copied the 'Access Token' (starts with 'ey...'), NOT the App ID or Secret.")

    now_ts = int(datetime.now(timezone.utc).timestamp())
    exp_ts = claims.get("exp") if claims else None

    if exp_ts:
        exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc).astimezone(IST)
        remaining_hours = (exp_ts - now_ts) / 3600.0

        if remaining_hours <= 0:
            print(f"[AUTH] ❌ Token EXPIRED on {exp_dt.strftime('%Y-%m-%d %I:%M %p IST')}.")
            print("[AUTH] Attempting renewal...")
            renewed = renew_access_token(CLIENT_ID, ACCESS_TOKEN)
            if renewed:
                return renewed
            raise RuntimeError(
                f"DHAN_ACCESS_TOKEN expired on {exp_dt.strftime('%Y-%m-%d %I:%M %p IST')} and could not be renewed. "
                "Please generate a fresh token from web.dhan.co -> Profile -> Access DhanHQ APIs."
            )
        else:
            print(f"[AUTH] Token active! Valid until {exp_dt.strftime('%Y-%m-%d %I:%M %p IST')} ({remaining_hours:.1f} hours left).")

            if remaining_hours < 1.0:
                print("[AUTH] Less than 1 hour remaining, attempting renewal...")
                renewed = renew_access_token(CLIENT_ID, ACCESS_TOKEN)
                if renewed:
                    return renewed

    return ACCESS_TOKEN


# ====================== GOOGLE SHEETS ======================
def get_google_spreadsheet():
    """Connect to Google Sheets using service account credentials with clear error guidance."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()

    if not creds_json:
        for candidate in ["service_account.json", "credentials.json"]:
            if os.path.exists(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        creds_json = f.read().strip()
                    print(f"[SHEETS] Using local credentials file: {candidate}")
                    break
                except Exception:
                    pass

    if not creds_json:
        print("\n" + "=" * 65)
        print("❌ CRITICAL ERROR: GOOGLE_CREDENTIALS secret is missing or empty!")
        print("=" * 65)
        print("To fix this in GitHub Actions:")
        print("  1. Go to your GitHub repository -> Settings -> Secrets and variables -> Actions")
        print("  2. Click 'New repository secret'")
        print("  3. Name: GOOGLE_CREDENTIALS")
        print("  4. Secret: Paste the ENTIRE JSON content of your Google Cloud Service Account key file.")
        print("=" * 65 + "\n")
        raise RuntimeError("GOOGLE_CREDENTIALS secret is missing or empty!")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    try:
        if creds_json.startswith("{"):
            creds_dict = json.loads(creds_json)
        else:
            with open(creds_json, "r", encoding="utf-8") as f:
                creds_dict = json.load(f)
    except Exception as e:
        print("\n" + "=" * 65)
        print(f"❌ ERROR: Failed to parse GOOGLE_CREDENTIALS JSON: {e}")
        print("=" * 65)
        raise

    client_email = creds_dict.get("client_email", "Unknown Service Account")
    print(f"[SHEETS] Authenticating with Service Account: {client_email}")

    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)

    print(f"[SHEETS] Opening Spreadsheet ID: {SPREADSHEET_ID}...")
    try:
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        print(f"[SHEETS] ✅ Successfully connected to spreadsheet: '{spreadsheet.title}'")
        return spreadsheet
    except gspread.exceptions.SpreadsheetNotFound:
        print("\n" + "=" * 65)
        print("❌ ERROR: Google Spreadsheet not found or permission denied!")
        print("=" * 65)
        print("Have you shared the spreadsheet with the service account email?")
        print(f"👉 Service Account Email: {client_email}")
        print(f"👉 Spreadsheet URL: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
        print("\nSteps to fix:")
        print("  1. Open your Google Sheet in browser.")
        print("  2. Click the 'Share' button in the top right.")
        print(f"  3. Add: {client_email}")
        print("  4. Grant 'Editor' permission and click 'Send'.")
        print("=" * 65 + "\n")
        raise
    except Exception as e:
        print("\n" + "=" * 65)
        print(f"❌ ERROR connecting to Google Sheets: {e}")
        print(f"Make sure {client_email} is added as an 'Editor' to:")
        print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
        print("=" * 65 + "\n")
        raise


def fetch_nse_equity_list() -> pd.DataFrame:
    """Download and extract all active NSE Equity instruments from Dhan scrip master."""
    print("[UNIVERSE] Downloading NSE instrument master from Dhan...")
    scrip_url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    df = pd.read_csv(scrip_url, low_memory=False)
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
        "Security ID": eq["SEM_SMST_SECURITY_ID"].astype(str).str.strip(),
        "Symbol":      eq["SEM_TRADING_SYMBOL"].astype(str).str.strip(),
        "Company Name": eq["SEM_CUSTOM_SYMBOL"].astype(str).str.strip()
                        if "SEM_CUSTOM_SYMBOL" in eq.columns
                        else eq["SEM_TRADING_SYMBOL"].astype(str).str.strip()
    })

    result = result.dropna(subset=["Security ID", "Symbol"])
    result = result[result["Symbol"].str.len() > 0]
    result = result[~result["Symbol"].str.upper().str.contains("TEST")]
    result = result.drop_duplicates(subset=["Security ID"]).sort_values("Symbol").reset_index(drop=True)
    print(f"[UNIVERSE] Found {len(result)} active NSE EQ equity stocks.")
    return result


def sync_or_get_universe(spreadsheet) -> pd.DataFrame:
    """
    Ensure the 'Universe' sheet exists and is populated in Google Spreadsheet.
    Returns the DataFrame of universe stocks to screen.
    """
    universe_sheet = None
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "universe":
            universe_sheet = ws
            break

    # If sheet doesn't exist or UPDATE_UNIVERSE flag is True, populate from Dhan
    if universe_sheet is None or UPDATE_UNIVERSE:
        print("[UNIVERSE] Creating/refreshing 'Universe' worksheet in Google Sheets...")
        df_master = fetch_nse_equity_list()

        if universe_sheet is None:
            universe_sheet = spreadsheet.add_worksheet(
                title="Universe",
                rows=len(df_master) + 50,
                cols=5
            )
        else:
            universe_sheet.clear()

        set_with_dataframe(universe_sheet, df_master, include_index=False, include_column_header=True, resize=True)

        # Style header: Dark navy header with bold white text
        universe_sheet.format("A1:C1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "backgroundColor": {"red": 0.1, "green": 0.2, "blue": 0.35}
        })
        universe_sheet.freeze(rows=1)
        print(f"[UNIVERSE] ✅ 'Universe' sheet populated with {len(df_master)} stocks.")
        return df_master

    # Otherwise, read directly from existing Universe sheet
    try:
        raw_df = get_as_dataframe(universe_sheet, evaluate_formulas=True).dropna(how="all")
        # Normalize column names
        col_map = {}
        for col in raw_df.columns:
            clean_name = str(col).strip().lower().replace("_", " ")
            if "security" in clean_name or "scrip" in clean_name or "id" in clean_name:
                col_map[col] = "Security ID"
            elif "symbol" in clean_name or "trading" in clean_name:
                col_map[col] = "Symbol"
            elif "company" in clean_name or "name" in clean_name:
                col_map[col] = "Company Name"

        raw_df = raw_df.rename(columns=col_map)
        if "Security ID" in raw_df.columns and "Symbol" in raw_df.columns:
            raw_df["Security ID"] = raw_df["Security ID"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
            raw_df["Symbol"] = raw_df["Symbol"].astype(str).str.strip()
            if "Company Name" not in raw_df.columns:
                raw_df["Company Name"] = raw_df["Symbol"]
            valid_df = raw_df[raw_df["Security ID"].str.len() > 0].reset_index(drop=True)
            if len(valid_df) > 50:
                print(f"[UNIVERSE] Loaded {len(valid_df)} stocks from existing 'Universe' sheet.")
                return valid_df[["Security ID", "Symbol", "Company Name"]]

        print("[UNIVERSE] Existing 'Universe' sheet appears incomplete. Resyncing from Dhan...")
    except Exception as e:
        print(f"[UNIVERSE] Could not read existing 'Universe' sheet: {e}. Resyncing...")

    # Fallback: re-fetch and populate
    df_master = fetch_nse_equity_list()
    universe_sheet.clear()
    set_with_dataframe(universe_sheet, df_master, include_index=False, include_column_header=True, resize=True)
    universe_sheet.format("A1:C1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        "backgroundColor": {"red": 0.1, "green": 0.2, "blue": 0.35}
    })
    universe_sheet.freeze(rows=1)
    return df_master


def write_daily_results(spreadsheet, df: pd.DataFrame):
    """Write screened results to today's date sheet (YYYY-MM-DD in IST)."""
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"[SHEETS] Writing daily results to worksheet '{today_str}'...")

    try:
        old = spreadsheet.worksheet(today_str)
        spreadsheet.del_worksheet(old)
        print(f"[SHEETS] Cleared previous sheet for '{today_str}'.")
    except gspread.WorksheetNotFound:
        pass

    worksheet = spreadsheet.add_worksheet(
        title=today_str,
        rows=max(len(df) + 30, 50),
        cols=max(len(df.columns) + 2, 20)
    )

    set_with_dataframe(worksheet, df, include_index=False, include_column_header=True, resize=True)

    # Format header: Forest green background with bold white text
    if not df.empty:
        col_letter = chr(ord('A') + min(len(df.columns) - 1, 25))
        worksheet.format(f"A1:{col_letter}1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "backgroundColor": {"red": 0.13, "green": 0.55, "blue": 0.28}
        })
        worksheet.freeze(rows=1)

    print(f"[SHEETS] ✅ Wrote {len(df)} rows → '{today_str}'")
    print(f"[SHEETS] Spreadsheet URL: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


# ====================== MARKET DATA & SCREENING ======================
def get_daily_ohlcv(security_id: str, from_date: str, to_date: str, retries: int = 2) -> pd.DataFrame | None:
    """Fetch historical daily OHLCV from Dhan with retry handling for rate limits."""
    for attempt in range(retries + 1):
        try:
            resp = dhan.historical_daily_data(
                security_id=str(security_id),
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=from_date,
                to_date=to_date
            )

            if not resp:
                return None

            status = resp.get("status")
            if status != "success":
                remarks = str(resp.get("remarks", ""))
                if "RL001" in remarks or "Too many requests" in remarks or "rate limit" in remarks.lower():
                    if attempt < retries:
                        time.sleep(1.5)
                        continue
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
            df = df.sort_values("date").dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
            return df
        except Exception:
            if attempt < retries:
                time.sleep(1.0)
                continue
            return None
    return None


def classify_ma20_trend(closes: pd.Series):
    """Classify 20 SMA slope and trend direction."""
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
    """Check if price pulled back into the 20 SMA band while holding above 40 SMA."""
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


# ====================== MAIN SCREENER ======================
def run_screener():
    now_ist = datetime.now(IST)
    print("=" * 65)
    print(f"[{now_ist.strftime('%Y-%m-%d %I:%M:%S %p IST')}] Starting Daily NSE Swing Screener")
    print("=" * 65)

    global dhan
    token = get_valid_token()
    ctx = DhanContext(CLIENT_ID, token)
    dhan = dhanhq(ctx)

    # Verify Data API access with benchmark stock
    valid_api, api_msg = verify_dhan_data_api(dhan)
    if valid_api:
        print(f"[AUTH] ✅ Dhan Data API connection confirmed ({api_msg}).")
    else:
        print(f"[AUTH] ⚠️ Dhan Data API notice: {api_msg}")
        print("[AUTH] Proceeding with current token...")

    # Connect to Google Spreadsheet
    print("[SHEETS] Connecting to Google Spreadsheet...")
    spreadsheet = get_google_spreadsheet()

    # Get or create the Universe sheet containing all NSE stocks
    universe_df = sync_or_get_universe(spreadsheet)
    total_stocks = len(universe_df)
    print(f"[SCREENER] Starting screening across {total_stocks} stocks from Universe sheet...")

    to_date   = now_ist.date().isoformat()
    from_date = (now_ist.date() - timedelta(days=HISTORY_DAYS)).isoformat()
    print(f"[DATA] Fetching daily OHLCV range: {from_date} → {to_date}")

    results = []

    for i, row in universe_df.iterrows():
        sec_id = str(row["Security ID"]).strip()
        symbol = str(row["Symbol"]).strip()
        name   = str(row.get("Company Name", symbol)).strip()

        if (i + 1) % 100 == 0 or (i + 1) == total_stocks:
            print(f"[PROGRESS] Checked {i+1}/{total_stocks} stocks | Setups Found: {len(results)}")

        df = get_daily_ohlcv(sec_id, from_date, to_date)
        if df is None or len(df) < MIN_DATA_DAYS:
            time.sleep(API_REQUEST_DELAY)
            continue

        last_date = df["date"].iloc[-1]
        # Ignore delisted/stale instruments not traded in last 7 days
        if (now_ist.replace(tzinfo=None) - last_date).days > 7:
            time.sleep(API_REQUEST_DELAY)
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
            time.sleep(API_REQUEST_DELAY)
            continue

        sma20 = closes.iloc[-SMA_SHORT:].mean()
        sma40 = closes.iloc[-SMA_LONG:].mean()
        if not (last_close > sma40 and sma20 > sma40):
            time.sleep(API_REQUEST_DELAY)
            continue

        if len(closes) < SMA_LONG_TERM:
            time.sleep(API_REQUEST_DELAY)
            continue
        sma200 = closes.iloc[-SMA_LONG_TERM:].mean()
        if not (last_close > sma200 and sma20 > sma200):
            time.sleep(API_REQUEST_DELAY)
            continue

        if not check_pullback(df):
            time.sleep(API_REQUEST_DELAY)
            continue

        is_green = last_close > last_open
        close_ok = (last_close > prev_high) if REQUIRE_CLOSE_ABOVE_PREV_HIGH else (last_close > prev_close)
        vol_ok   = last_volume > (VOLUME_SPIKE_MULTIPLIER * avg_vol_20)

        if not (is_green and close_ok and vol_ok):
            time.sleep(API_REQUEST_DELAY)
            continue

        ma_label, ma_slope = classify_ma20_trend(closes)

        results.append({
            "Company": name,
            "Symbol": symbol,
            "Security ID": sec_id,
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

        time.sleep(API_REQUEST_DELAY)

    if results:
        out_df = pd.DataFrame(results)
        print(f"\n✅ Finished! Found {len(results)} swing setups today:")
        print(out_df[["Symbol", "Last Close", "20 MA Trend"]].to_string(index=False))
        write_daily_results(spreadsheet, out_df)
    else:
        print("\nNo stocks met all criteria today.")
        empty_df = pd.DataFrame([{"Message": "No stocks met the swing trade criteria today."}])
        write_daily_results(spreadsheet, empty_df)

    print(f"\n[{datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')}] Screener run completed.")


if __name__ == "__main__":
    run_screener()
