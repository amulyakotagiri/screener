"""
NSE Swing Trade Screener
Runs daily via GitHub Actions at 5 PM IST
"""

import os
import sys
import time
import json
import io
from datetime import datetime, timedelta, date, timezone
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

# ====================== TOKEN (SIMPLE) ======================
def get_valid_token() -> str:
    if not CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID secret is missing")
    if not ACCESS_TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN secret is missing")

    print(f"[AUTH] Using Client ID: {CLIENT_ID}", flush=True)
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
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(io.BytesIO(resp.content), low_memory=False)
    df.columns = [c.strip().upper() for c in df.columns]

    mask = (
        (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE") &
        (df["SEM_SEGMENT"].astype(str).str.upper() == "E") &
        (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "EQUITY")
