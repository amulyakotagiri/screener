"""
NSE Swing Trade Screener
- Full NSE equity universe from Dhan instrument master
- Daily historical OHLCV data
- Controlled Dhan API rate
- Automatic retries for temporary failures
- Google Sheets output
- Designed for GitHub Actions / evening execution
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


# ============================================================
# CONFIG
# ============================================================

CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")

SPREADSHEET_ID = "10iZtgmYm3QqW52hDi52p2R5qmC5mAGLs0qKcXJub61Q"


# ---------------- SCREENING CONDITIONS ----------------

SMA_SHORT = 20
SMA_LONG = 40
SMA_LONG_TERM = 200

PULLBACK_LOOKBACK_DAYS = 4
PULLBACK_BAND_PCT = 0.02

VOLUME_SPIKE_MULTIPLIER = 1.2

REQUIRE_CLOSE_ABOVE_PREV_HIGH = True

MIN_AVG_VOLUME_20 = 200_000
MIN_PRICE = 100.0

MIN_DATA_DAYS = 200

FLAT_MA_LOOKBACK_DAYS = 10

STRONGLY_RISING_THRESHOLD = 0.15
FLAT_THRESHOLD = 0.05
STRONGLY_FALLING_THRESHOLD = -0.15

# Calendar days.
# 450 gives us enough room for 200+ trading sessions.
HISTORY_DAYS = 450


# Dhan Data API limit is currently 5 requests/second.
# We deliberately stay slightly below it.
REQUEST_INTERVAL = 0.23

MAX_RETRIES = 4

RETRY_DELAYS = [1, 2, 4, 8]


# ============================================================
# TOKEN
# ============================================================

def renew_access_token(client_id: str, current_token: str) -> str | None:

    url = "https://api.dhan.co/v2/RenewToken"

    headers = {
        "access-token": current_token,
        "dhanClientId": client_id,
        "Accept": "application/json"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        print(
            f"[TOKEN] Renew HTTP status: "
            f"{response.status_code}"
        )

        if response.status_code == 200:

            data = response.json()

            new_token = (
                data.get("accessToken")
                or data.get("access_token")
            )

            if new_token:

                print("[TOKEN] Renewed successfully")

                return new_token

            print(
                "[TOKEN] 200 response but "
                "no token returned"
            )

        else:

            print(
                "[TOKEN] Renewal failed:",
                response.text[:250]
            )

    except Exception as e:

        print(f"[TOKEN] Exception: {e}")

    return None


def get_valid_token() -> str:

    if not CLIENT_ID:
        raise RuntimeError(
            "Missing DHAN_CLIENT_ID"
        )

    if not ACCESS_TOKEN:
        raise RuntimeError(
            "Missing DHAN_ACCESS_TOKEN"
        )

    print("[TOKEN] Attempting token renewal...")

    renewed = renew_access_token(
        CLIENT_ID,
        ACCESS_TOKEN
    )

    if renewed:
        return renewed

    print(
        "[TOKEN] Renewal unavailable."
        " Using token from GitHub secret."
    )

    return ACCESS_TOKEN


# ============================================================
# GOOGLE SHEETS
# ============================================================

def write_to_google_sheet(df: pd.DataFrame):

    print("[SHEETS] Connecting...")

    creds_json = os.environ.get(
        "GOOGLE_CREDENTIALS"
    )

    if not creds_json:

        raise RuntimeError(
            "Missing GOOGLE_CREDENTIALS secret"
        )

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = (
        Credentials
        .from_service_account_info(
            creds_dict,
            scopes=scopes
        )
    )

    gc = gspread.authorize(credentials)

    spreadsheet = gc.open_by_key(
        SPREADSHEET_ID
    )

    sheet_name = date.today().strftime(
        "%Y-%m-%d"
    )

    # Delete today's sheet if it already exists.
    try:

        old_sheet = spreadsheet.worksheet(
            sheet_name
        )

        spreadsheet.del_worksheet(
            old_sheet
        )

        print(
            f"[SHEETS] Deleted old sheet: "
            f"{sheet_name}"
        )

    except gspread.WorksheetNotFound:

        pass

    rows = max(
        len(df) + 10,
        100
    )

    cols = max(
        len(df.columns) + 2,
        25
    )

    worksheet = spreadsheet.add_worksheet(
        title=sheet_name,
        rows=rows,
        cols=cols
    )

    set_with_dataframe(
        worksheet,
        df,
        include_index=False,
        include_column_header=True,
        resize=True
    )

    # Header formatting
    worksheet.format(
        "A1:Z1",
        {
            "textFormat": {
                "bold": True,
                "foregroundColor": {
                    "red": 1,
                    "green": 1,
                    "blue": 1
                }
            },
            "backgroundColor": {
                "red": 0.2,
                "green": 0.6,
                "blue": 0.3
            }
        }
    )

    print(
        f"[SHEETS] Wrote {len(df)} rows "
        f"→ '{sheet_name}'"
    )

    print(
        f"https://docs.google.com/"
        f"spreadsheets/d/{SPREADSHEET_ID}"
    )


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def fetch_nse_equity_list() -> pd.DataFrame:

    print(
        "\n[INSTRUMENTS] "
        "Downloading Dhan instrument master..."
    )

    url = (
        "https://images.dhan.co/api-data/"
        "api-scrip-master.csv"
    )

    df = pd.read_csv(
        url,
        low_memory=False
    )

    df.columns = [
        str(c).strip().upper()
        for c in df.columns
    ]

    print(
        f"[INSTRUMENTS] "
        f"Raw instruments: {len(df):,}"
    )

    required_columns = [
        "SEM_EXM_EXCH_ID",
        "SEM_SEGMENT",
        "SEM_INSTRUMENT_NAME",
        "SEM_SMST_SECURITY_ID",
        "SEM_TRADING_SYMBOL"
    ]

    missing = [
        col
        for col in required_columns
        if col not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Instrument master is missing "
            f"columns: {missing}"
        )

    # --------------------------------------------------------
    # NSE
    # --------------------------------------------------------

    exchange_mask = (
        df["SEM_EXM_EXCH_ID"]
        .astype(str)
        .str.upper()
        .str.strip()
        == "NSE"
    )

    # --------------------------------------------------------
    # Equity cash segment
    # --------------------------------------------------------

    segment_mask = (
        df["SEM_SEGMENT"]
        .astype(str)
        .str.upper()
        .str.strip()
        == "E"
    )

    # --------------------------------------------------------
    # Equity instruments
    # --------------------------------------------------------

    instrument_mask = (
        df["SEM_INSTRUMENT_NAME"]
        .astype(str)
        .str.upper()
        .str.strip()
        == "EQUITY"
    )

    mask = (
        exchange_mask
        & segment_mask
        & instrument_mask
    )

    eq = df.loc[mask].copy()

    print(
        f"[INSTRUMENTS] "
        f"NSE equity instruments before cleaning: "
        f"{len(eq):,}"
    )

    # IMPORTANT:
    #
    # We intentionally DO NOT filter:
    #
    # SEM_SERIES == EQ
    #
    # because the goal is the complete NSE equity
    # universe rather than only one exchange series.

    result = pd.DataFrame({

        "security_id":
            eq["SEM_SMST_SECURITY_ID"]
            .astype(str)
            .str.strip(),

        "symbol":
            eq["SEM_TRADING_SYMBOL"]
            .astype(str)
            .str.strip(),

        "name":
            (
                eq["SEM_CUSTOM_SYMBOL"]
                .astype(str)
                .str.strip()
                if "SEM_CUSTOM_SYMBOL" in eq.columns
                else
                eq["SEM_TRADING_SYMBOL"]
                .astype(str)
                .str.strip()
            )
    })

    # --------------------------------------------------------
    # Clean invalid rows
    # --------------------------------------------------------

    result = result.replace(
        {
            "security_id": {
                "nan": np.nan,
                "None": np.nan,
                "": np.nan
            },

            "symbol": {
                "nan": np.nan,
                "None": np.nan,
                "": np.nan
            }
        }
    )

    result = result.dropna(
        subset=[
            "security_id",
            "symbol"
        ]
    )

    result = result[
        result["symbol"].str.len() > 0
    ]

    result = result[
        result["security_id"].str.len() > 0
    ]

    # Security ID must be numeric
    result = result[
        result["security_id"]
        .str.fullmatch(r"\d+")
    ]

    # Remove duplicate security IDs
    result = result.drop_duplicates(
        subset=["security_id"]
    )

    # Remove duplicate symbols while keeping
    # the first valid security ID.
    result = result.drop_duplicates(
        subset=["symbol"],
        keep="first"
    )

    result = (
        result
        .sort_values("symbol")
        .reset_index(drop=True)
    )

    print(
        f"[INSTRUMENTS] "
        f"FINAL NSE EQUITY UNIVERSE: "
        f"{len(result):,}"
    )

    if len(result) < 1000:

        print(
            "\n[WARNING] The instrument master "
            "returned fewer than 1,000 NSE equities."
        )

        print(
            "This is an instrument-universe issue, "
            "not a screener issue."
        )

    return result


# ============================================================
# DHAN DAILY DATA
# ============================================================

def get_daily_ohlcv(
    security_id: str,
    from_date: str,
    to_date: str
) -> pd.DataFrame | None:

    """
    Fetch daily OHLCV for one security.

    Includes:
    - rate limiting
    - retries
    - malformed response protection
    """

    for attempt in range(MAX_RETRIES):

        try:

            response = dhan.historical_daily_data(
                security_id=str(security_id),
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=from_date,
                to_date=to_date
            )

            if not response:

                raise RuntimeError(
                    "Empty API response"
                )

            if response.get("status") != "success":

                raise RuntimeError(
                    str(response)
                )

            data = response.get("data") or {}

            if (
                not data
                or "close" not in data
                or len(data["close"]) == 0
            ):

                return None

            required = [
                "open",
                "high",
                "low",
                "close",
                "timestamp"
            ]

            if not all(
                key in data
                for key in required
            ):

                return None

            length = len(data["close"])

            df = pd.DataFrame({

                "open": data["open"],

                "high": data["high"],

                "low": data["low"],

                "close": data["close"],

                "volume": data.get(
                    "volume",
                    [0] * length
                ),

                "timestamp":
                    data["timestamp"]
            })

            # Dhan returns epoch timestamps.
            df["date"] = (
                pd.to_datetime(
                    df["timestamp"],
                    unit="s",
                    errors="coerce"
                )
                .dt.tz_localize(None)
            )

            df = (
                df
                .dropna(subset=["date"])
                .sort_values("date")
                .drop_duplicates(
                    subset=["date"],
                    keep="last"
                )
                .reset_index(drop=True)
            )

            numeric_columns = [
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]

            for column in numeric_columns:

                df[column] = pd.to_numeric(
                    df[column],
                    errors="coerce"
                )

            df = df.dropna(
                subset=[
                    "open",
                    "high",
                    "low",
                    "close"
                ]
            )

            return df

        except Exception as e:

            if attempt == MAX_RETRIES - 1:

                print(
                    f"[DATA ERROR] "
                    f"{security_id}: {e}"
                )

                return None

            delay = RETRY_DELAYS[
                min(
                    attempt,
                    len(RETRY_DELAYS) - 1
                )
            ]

            print(
                f"[RETRY] "
                f"{security_id} | "
                f"attempt {attempt + 1}/"
                f"{MAX_RETRIES} | "
                f"waiting {delay}s"
            )

            time.sleep(delay)

    return None


# ============================================================
# MA TREND
# ============================================================

def classify_ma20_trend(
    closes: pd.Series
):

    required = (
        SMA_SHORT
        + FLAT_MA_LOOKBACK_DAYS
    )

    if len(closes) < required:

        return "N/A", np.nan

    sma_series = []

    for i in range(
        FLAT_MA_LOOKBACK_DAYS - 1,
        -1,
        -1
    ):

        end = len(closes) - i

        start = end - SMA_SHORT

        if start < 0:

            return "N/A", np.nan

        sma_series.append(
            closes.iloc[start:end].mean()
        )

    y = np.array(
        sma_series,
        dtype=float
    )

    x = np.arange(
        len(y),
        dtype=float
    )

    slope = np.polyfit(
        x,
        y,
        1
    )[0]

    if y[-1] == 0:

        return "N/A", np.nan

    slope_pct = (
        slope
        / y[-1]
        * 100
    )

    if (
        slope_pct
        > STRONGLY_RISING_THRESHOLD
    ):

        label = "Strongly Rising"

    elif (
        slope_pct
        < STRONGLY_FALLING_THRESHOLD
    ):

        label = "Strongly Falling"

    elif (
        abs(slope_pct)
        <= FLAT_THRESHOLD
    ):

        label = "Flat"

    elif slope_pct > 0:

        label = "Rising"

    else:

        label = "Falling"

    return (
        label,
        round(slope_pct, 3)
    )


# ============================================================
# PULLBACK
# ============================================================

def check_pullback(
    df: pd.DataFrame
) -> bool:

    last_idx = len(df) - 1

    for i in range(
        PULLBACK_LOOKBACK_DAYS
    ):

        idx = last_idx - i

        if idx < SMA_LONG:

            break

        sma20 = (
            df["close"]
            .iloc[
                idx - SMA_SHORT + 1:
                idx + 1
            ]
            .mean()
        )

        sma40 = (
            df["close"]
            .iloc[
                idx - SMA_LONG + 1:
                idx + 1
            ]
            .mean()
        )

        band_low = (
            sma20
            * (1 - PULLBACK_BAND_PCT)
        )

        band_high = (
            sma20
            * (1 + PULLBACK_BAND_PCT)
        )

        if (
            band_low
            <= df["low"].iloc[idx]
            <= band_high
            and
            df["close"].iloc[idx]
            > sma40
        ):

            return True

    return False


# ============================================================
# MAIN SCREENER
# ============================================================

def run_screener():

    print("=" * 70)

    print(
        f"[{datetime.now()}] "
        "NSE SWING SCREENER STARTED"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Dhan connection
    # --------------------------------------------------------

    global dhan

    token = get_valid_token()

    context = DhanContext(
        CLIENT_ID,
        token
    )

    dhan = dhanhq(context)

    # --------------------------------------------------------
    # Dates
    # --------------------------------------------------------

    to_date = date.today()

    from_date = (
        to_date
        - timedelta(days=HISTORY_DAYS)
    )

    from_date_str = (
        from_date.isoformat()
    )

    to_date_str = (
        to_date.isoformat()
    )

    print(
        f"[DATA] "
        f"{from_date_str} → "
        f"{to_date_str}"
    )

    # --------------------------------------------------------
    # Get FULL NSE universe
    # --------------------------------------------------------

    instruments = (
        fetch_nse_equity_list()
    )

    total = len(instruments)

    print(
        "\n"
        + "=" * 70
    )

    print(
        f"[UNIVERSE] "
        f"{total:,} NSE equity instruments "
        "will be screened."
    )

    print(
        "=" * 70
    )

    if total == 0:

        raise RuntimeError(
            "No NSE equity instruments found."
        )

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    stats = {

        "total": total,

        "api_success": 0,

        "api_failed": 0,

        "insufficient_data": 0,

        "stale_data": 0,

        "low_volume": 0,

        "low_price": 0,

        "trend_failed": 0,

        "sma200_failed": 0,

        "pullback_failed": 0,

        "signal_failed": 0,

        "passed": 0
    }

    results = []

    start_time = time.time()

    # --------------------------------------------------------
    # SCREEN EACH STOCK
    # --------------------------------------------------------

    for i, row in instruments.iterrows():

        security_id = row[
            "security_id"
        ]

        symbol = row[
            "symbol"
        ]

        # IMPORTANT:
        #
        # Rate-limit EVERY Dhan request.
        #
        # Your old code slept only after a stock
        # passed the screening conditions.
        #
        # This is the correct location.

        if i > 0:

            time.sleep(
                REQUEST_INTERVAL
            )

        df = get_daily_ohlcv(
            security_id,
            from_date_str,
            to_date_str
        )

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

        if (
            (i + 1) % 100 == 0
            or i == 0
            or i == total - 1
        ):

            elapsed = (
                time.time()
                - start_time
            )

            processed = i + 1

            rate = (
                processed / elapsed
                if elapsed > 0
                else 0
            )

            remaining = (
                total - processed
            )

            eta_seconds = (
                remaining / rate
                if rate > 0
                else 0
            )

            print(
                f"[PROGRESS] "
                f"{processed:,}/{total:,} | "
                f"Passed: {len(results)} | "
                f"Rate: {rate:.2f}/sec | "
                f"ETA: "
                f"{eta_seconds / 60:.1f} min"
            )

        # ----------------------------------------------------
        # API failure
        # ----------------------------------------------------

        if df is None:

            stats["api_failed"] += 1

            continue

        stats["api_success"] += 1

        # ----------------------------------------------------
        # Minimum data
        # ----------------------------------------------------

        if len(df) < MIN_DATA_DAYS:

            stats[
                "insufficient_data"
            ] += 1

            continue

        # ----------------------------------------------------
        # Last available trading date
        # ----------------------------------------------------

        last_date = df[
            "date"
        ].iloc[-1]

        # We allow weekends/holidays,
        # but reject truly stale securities.

        days_old = (
            pd.Timestamp.now()
            - last_date
        ).days

        if days_old > 7:

            stats["stale_data"] += 1

            continue

        # ----------------------------------------------------
        # Series
        # ----------------------------------------------------

        closes = df["close"]
        opens = df["open"]
        highs = df["high"]
        lows = df["low"]
        volumes = df["volume"]

        last_close = closes.iloc[-1]

        last_open = opens.iloc[-1]

        last_high = highs.iloc[-1]

        last_low = lows.iloc[-1]

        last_volume = volumes.iloc[-1]

        prev_high = highs.iloc[-2]

        prev_close = closes.iloc[-2]

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        if last_close < MIN_PRICE:

            stats["low_price"] += 1

            continue

        # ----------------------------------------------------
        # Average volume
        # ----------------------------------------------------

        avg_vol_20 = (
            volumes.iloc[-20:].mean()
        )

        if (
            avg_vol_20
            < MIN_AVG_VOLUME_20
        ):

            stats["low_volume"] += 1

            continue

        # ----------------------------------------------------
        # SMA 20 / 40
        # ----------------------------------------------------

        sma20 = (
            closes
            .iloc[-SMA_SHORT:]
            .mean()
        )

        sma40 = (
            closes
            .iloc[-SMA_LONG:]
            .mean()
        )

        if not (
            last_close > sma40
            and
            sma20 > sma40
        ):

            stats["trend_failed"] += 1

            continue

        # ----------------------------------------------------
        # SMA 200
        # ----------------------------------------------------

        if len(closes) < SMA_LONG_TERM:

            stats["sma200_failed"] += 1

            continue

        sma200 = (
            closes
            .iloc[-SMA_LONG_TERM:]
            .mean()
        )

        if not (
            last_close > sma200
            and
            sma20 > sma200
        ):

            stats["sma200_failed"] += 1

            continue

        # ----------------------------------------------------
        # Pullback
        # ----------------------------------------------------

        if not check_pullback(df):

            stats[
                "pullback_failed"
            ] += 1

            continue

        # ----------------------------------------------------
        # Signal candle
        # ----------------------------------------------------

        is_green = (
            last_close > last_open
        )

        if REQUIRE_CLOSE_ABOVE_PREV_HIGH:

            close_ok = (
                last_close
                > prev_high
            )

        else:

            close_ok = (
                last_close
                > prev_close
            )

        vol_ok = (
            last_volume
            >
            VOLUME_SPIKE_MULTIPLIER
            * avg_vol_20
        )

        if not (
            is_green
            and close_ok
            and vol_ok
        ):

            stats[
                "signal_failed"
            ] += 1

            continue

        # ----------------------------------------------------
        # MA trend
        # ----------------------------------------------------

        ma_label, ma_slope = (
            classify_ma20_trend(
                closes
            )
        )

        # ----------------------------------------------------
        # STOCK PASSED
        # ----------------------------------------------------

        stats["passed"] += 1

        results.append({

            "Company":
                row["name"],

            "Symbol":
                symbol,

            "Security ID":
                security_id,

            "Last Bar Date":
                last_date.strftime(
                    "%Y-%m-%d"
                ),

            "Last Close":
                round(
                    last_close,
                    2
                ),

            "Open":
                round(
                    last_open,
                    2
                ),

            "High":
                round(
                    last_high,
                    2
                ),

            "Low":
                round(
                    last_low,
                    2
                ),

            "SMA 20":
                round(
                    sma20,
                    2
                ),

            "SMA 40":
                round(
                    sma40,
                    2
                ),

            "SMA 200":
                round(
                    sma200,
                    2
                ),

            "20 SMA > 40 SMA?":
                "YES",

            "20 SMA > 200 SMA?":
                "YES",

            "Close > 200 SMA?":
                "YES",

            "Pullback to 20 SMA?":
                "YES",

            "Signal Bar?":
                "YES",

            "20 MA Trend":
                ma_label,

            "20 MA Slope %/day":
                ma_slope,

            "Latest Volume":
                int(last_volume),

            "Avg Vol (20)":
                int(avg_vol_20),

            "Volume Spike?":
                "YES",

            "Days of Data":
                len(df)
        })

    # ========================================================
    # FINAL REPORT
    # ========================================================

    elapsed_minutes = (
        time.time()
        - start_time
    ) / 60

    print("\n")
    print("=" * 70)
    print("SCREENING COMPLETE")
    print("=" * 70)

    print(
        f"Total universe       : "
        f"{stats['total']:,}"
    )

    print(
        f"API successful       : "
        f"{stats['api_success']:,}"
    )

    print(
        f"API failed           : "
        f"{stats['api_failed']:,}"
    )

    print(
        f"Insufficient data    : "
        f"{stats['insufficient_data']:,}"
    )

    print(
        f"Stale data           : "
        f"{stats['stale_data']:,}"
    )

    print(
        f"Low price            : "
        f"{stats['low_price']:,}"
    )

    print(
        f"Low volume           : "
        f"{stats['low_volume']:,}"
    )

    print(
        f"Trend failed         : "
        f"{stats['trend_failed']:,}"
    )

    print(
        f"SMA 200 failed      : "
        f"{stats['sma200_failed']:,}"
    )

    print(
        f"Pullback failed      : "
        f"{stats['pullback_failed']:,}"
    )

    print(
        f"Signal failed        : "
        f"{stats['signal_failed']:,}"
    )

    print(
        f"PASSED               : "
        f"{stats['passed']:,}"
    )

    print(
        f"Total runtime        : "
        f"{elapsed_minutes:.2f} minutes"
    )

    print("=" * 70)

    # ========================================================
    # GOOGLE SHEETS
    # ========================================================

    if results:

        out_df = pd.DataFrame(
            results
        )

        out_df = (
            out_df
            .sort_values(
                [
                    "20 MA Trend",
                    "20 MA Slope %/day"
                ],
                ascending=[
                    True,
                    False
                ]
            )
            .reset_index(drop=True)
        )

        print(
            f"\nFound "
            f"{len(out_df)} "
            "matching stocks."
        )

        print(
            out_df[
                [
                    "Symbol",
                    "Last Close",
                    "20 MA Trend"
                ]
            ]
            .to_string(
                index=False
            )
        )

        write_to_google_sheet(
            out_df
        )

    else:

        print(
            "\nNo stocks met "
            "the criteria today."
        )

        empty_df = pd.DataFrame([
            {
                "Message":
                    "No stocks met the "
                    "swing trade criteria today."
            }
        ])

        write_to_google_sheet(
            empty_df
        )

    print(
        f"\n[{datetime.now()}] "
        "Finished."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    run_screener()
