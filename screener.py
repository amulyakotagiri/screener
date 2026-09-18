"""
NSE Swing Trade Screener
- Universe is fetched ONLY ONCE and stored permanently in Google Sheet tab "Universe"
- Every day a new dated tab is created with the filtered stocks
"""

import time
from datetime import datetime, timedelta, date
from pathlib import Path
import pandas as pd
import numpy as np
import requests
from dhanhq import DhanContext, dhanhq

import config
import Googlesheets as gs

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
        print(f"[TOKEN] Renewal failed: {r.text[:250]}")
    except Exception as e:
        print(f"[TOKEN] Exception: {e}")
    return None

def get_valid_token() -> str:
    if not config.DHAN_CLIENT_ID or not config.DHAN_ACCESS_TOKEN:
        raise RuntimeError("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN")
    print("[TOKEN] Attempting token renewal...")
    renewed = renew_access_token(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN)
    if renewed:
        return renewed
    print("[TOKEN] Using token from secret (renewal failed)")
    return config.DHAN_ACCESS_TOKEN

# ------------------------------------------------------------------
# Instrument master – only called when Universe tab is missing
# ------------------------------------------------------------------
def fetch_nse_equity_list() -> pd.DataFrame:
    print("\n[INSTRUMENTS] Downloading Dhan instrument master (ONE-TIME)...")
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip().upper() for c in df.columns]

    print(f"[INSTRUMENTS] Raw instruments: {len(df):,}")

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
# Daily OHLCV with cache + retries
# ------------------------------------------------------------------
def get_daily_ohlcv(dhan, security_id: str, from_date: str, to_date: str,
                    cache_dir: Path) -> pd.DataFrame | None:
    cache_file = cache_dir / f"{security_id}.parquet"
    if cache_file.exists():
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    for attempt in range(config.MAX_RETRIES):
        try:
            response = dhan.historical_daily_data(
                security_id=str(security_id),
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=from_date,
                to_date=to_date,
            )
            if not response or response.get("status") != "success":
                raise RuntimeError(str(response))

            data = response.get("data") or {}
            if not data or "close" not in data or len(data["close"]) == 0:
                return None

            length = len(data["close"])
            df = pd.DataFrame({
                "open": data["open"],
                "high": data["high"],
                "low": data["low"],
                "close": data["close"],
                "volume": data.get("volume", [0] * length),
                "timestamp": data["timestamp"],
            })
            df["date"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce").dt.tz_localize(None)
            df = (df.dropna(subset=["date"])
                    .sort_values("date")
                    .drop_duplicates(subset=["date"], keep="last")
                    .reset_index(drop=True))

            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["open", "high", "low", "close"])

            cache_dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_file, index=False)
            return df

        except Exception as e:
            if attempt == config.MAX_RETRIES - 1:
                print(f"[DATA ERROR] {security_id}: {e}")
                return None
            delay = config.RETRY_DELAYS[min(attempt, len(config.RETRY_DELAYS) - 1)]
            time.sleep(delay)
    return None

# ------------------------------------------------------------------
# MA trend + Pullback
# ------------------------------------------------------------------
def classify_ma20_trend(closes: pd.Series):
    required = config.SMA_SHORT + config.FLAT_MA_LOOKBACK_DAYS
    if len(closes) < required:
        return "N/A", np.nan

    sma_series = []
    for i in range(config.FLAT_MA_LOOKBACK_DAYS - 1, -1, -1):
        end = len(closes) - i
        start = end - config.SMA_SHORT
        if start < 0:
            return "N/A", np.nan
        sma_series.append(closes.iloc[start:end].mean())

    y = np.array(sma_series, dtype=float)
    x = np.arange(len(y), dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    if y[-1] == 0:
        return "N/A", np.nan
    slope_pct = slope / y[-1] * 100

    if slope_pct > config.STRONGLY_RISING_THRESHOLD:
        label = "Strongly Rising"
    elif slope_pct < config.STRONGLY_FALLING_THRESHOLD:
        label = "Strongly Falling"
    elif abs(slope_pct) <= config.FLAT_THRESHOLD:
        label = "Flat"
    elif slope_pct > 0:
        label = "Rising"
    else:
        label = "Falling"
    return label, round(slope_pct, 3)

def check_pullback(df: pd.DataFrame) -> bool:
    last_idx = len(df) - 1
    for i in range(config.PULLBACK_LOOKBACK_DAYS):
        idx = last_idx - i
        if idx < config.SMA_LONG:
            break
        sma20 = df["close"].iloc[idx - config.SMA_SHORT + 1 : idx + 1].mean()
        sma40 = df["close"].iloc[idx - config.SMA_LONG + 1 : idx + 1].mean()
        band_low  = sma20 * (1 - config.PULLBACK_BAND_PCT)
        band_high = sma20 * (1 + config.PULLBACK_BAND_PCT)
        if (band_low <= df["low"].iloc[idx] <= band_high
                and df["close"].iloc[idx] > sma40):
            return True
    return False

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def run_screener():
    print("=" * 70)
    print(f"[{datetime.now()}] NSE SWING SCREENER STARTED")
    print("=" * 70)

    token = get_valid_token()
    dhan = dhanhq(DhanContext(config.DHAN_CLIENT_ID, token))

    to_date = date.today()
    from_date = to_date - timedelta(days=config.HISTORY_DAYS)
    from_date_str = from_date.isoformat()
    to_date_str = to_date.isoformat()
    print(f"[DATA] {from_date_str} → {to_date_str}")

    # Load permanent Universe (or create it once)
    instruments = gs.load_universe()
    if instruments is None:
        print("[UNIVERSE] First run – downloading full NSE list...")
        instruments = fetch_nse_equity_list()
        gs.write_universe(instruments)
    else:
        instruments = instruments[["security_id", "symbol", "name"]].copy()
        instruments["security_id"] = instruments["security_id"].astype(str)
        instruments["symbol"] = instruments["symbol"].astype(str)

    total = len(instruments)
    print(f"\n[UNIVERSE] Using {total:,} NSE equities")

    cache_dir = Path(config.CACHE_DIR) / to_date.isoformat()
    cache_dir.mkdir(parents=True, exist_ok=True)

    stats = {k: 0 for k in [
        "total", "api_success", "api_failed", "insufficient_data",
        "stale_data", "low_volume", "low_price", "trend_failed",
        "sma200_failed", "pullback_failed", "signal_failed", "passed"
    ]}
    stats["total"] = total
    results = []
    start_time = time.time()

    for i, row in instruments.iterrows():
        security_id = str(row["security_id"])
        symbol = str(row["symbol"])

        if i > 0:
            time.sleep(config.REQUEST_INTERVAL)

        df = get_daily_ohlcv(dhan, security_id, from_date_str, to_date_str, cache_dir)

        if (i + 1) % 100 == 0 or i == 0 or i == total - 1:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            print(f"[PROGRESS] {i+1:,}/{total:,} | Passed: {len(results)} | "
                  f"Rate: {rate:.2f}/s | ETA: {eta:.1f} min")

        if df is None:
            stats["api_failed"] += 1
            continue
        stats["api_success"] += 1

        if len(df) < config.MIN_DATA_DAYS:
            stats["insufficient_data"] += 1
            continue

        last_date = df["date"].iloc[-1]
        if (pd.Timestamp.now() - last_date).days > 7:
            stats["stale_data"] += 1
            continue

        closes  = df["close"]
        opens   = df["open"]
        highs   = df["high"]
        volumes = df["volume"]

        last_close  = closes.iloc[-1]
        last_open   = opens.iloc[-1]
        last_high   = highs.iloc[-1]
        last_volume = volumes.iloc[-1]
        prev_high   = highs.iloc[-2]

        if last_close < config.MIN_PRICE:
            stats["low_price"] += 1
            continue

        avg_vol_20 = volumes.iloc[-20:].mean()
        if avg_vol_20 < config.MIN_AVG_VOLUME_20:
            stats["low_volume"] += 1
            continue

        sma20 = closes.iloc[-config.SMA_SHORT:].mean()
        sma40 = closes.iloc[-config.SMA_LONG:].mean()
        if not (last_close > sma40 and sma20 > sma40):
            stats["trend_failed"] += 1
            continue

        if len(closes) < config.SMA_LONG_TERM:
            stats["sma200_failed"] += 1
            continue
        sma200 = closes.iloc[-config.SMA_LONG_TERM:].mean()
        if not (last_close > sma200 and sma20 > sma200):
            stats["sma200_failed"] += 1
            continue

        if not check_pullback(df):
            stats["pullback_failed"] += 1
            continue

        is_green = last_close > last_open
        close_ok = (last_close > prev_high) if config.REQUIRE_CLOSE_ABOVE_PREV_HIGH else (last_close > closes.iloc[-2])
        vol_ok   = last_volume > config.VOLUME_SPIKE_MULTIPLIER * avg_vol_20

        if not (is_green and close_ok and vol_ok):
            stats["signal_failed"] += 1
            continue

        ma_label, ma_slope = classify_ma20_trend(closes)

        stats["passed"] += 1
        results.append({
            "Company": row.get("name", symbol),
            "Symbol": symbol,
            "Security ID": security_id,
            "Last Bar Date": last_date.strftime("%Y-%m-%d"),
            "Last Close": round(last_close, 2),
            "Open": round(last_open, 2),
            "High": round(last_high, 2),
            "Low": round(df["low"].iloc[-1], 2),
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
            "Days of Data": len(df),
        })

    # Final report
    elapsed_min = (time.time() - start_time) / 60
    print("\n" + "=" * 70)
    print("SCREENING COMPLETE")
    print("=" * 70)
    for k, v in stats.items():
        print(f"{k:22}: {v:,}")
    print(f"{'Total runtime':22}: {elapsed_min:.2f} minutes")
    print("=" * 70)

    if results:
        out_df = pd.DataFrame(results)
        out_df = out_df.sort_values(
            ["20 MA Trend", "20 MA Slope %/day"],
            ascending=[True, False]
        ).reset_index(drop=True)
        print(f"\nFound {len(out_df)} matching stocks.")
        gs.write_daily_results(out_df)
    else:
        print("\nNo stocks met the criteria today.")
        empty = pd.DataFrame([{"Message": "No stocks met the swing trade criteria today."}])
        gs.write_daily_results(empty)

    print(f"\n[{datetime.now()}] Finished.")

if __name__ == "__main__":
    run_screener()
