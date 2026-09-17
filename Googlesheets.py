import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe, get_as_dataframe
from datetime import date
import pandas as pd
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_client():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return gspread.authorize(creds)

def get_spreadsheet():
    return get_client().open_by_key(config.SPREADSHEET_ID)

def load_universe() -> pd.DataFrame | None:
    """
    Try to load the permanent Universe tab.
    Returns None if the tab does not exist or is empty.
    """
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("Universe")
        df = get_as_dataframe(ws, evaluate_formulas=False, header=0)
        df = df.dropna(how="all")          # remove completely empty rows
        if len(df) < 500:                  # safety check – real universe is ~2000+
            print("[SHEETS] Universe tab exists but looks empty / incomplete")
            return None
        print(f"[SHEETS] Loaded {len(df):,} instruments from existing 'Universe' tab")
        return df
    except gspread.WorksheetNotFound:
        print("[SHEETS] 'Universe' tab not found")
        return None

def write_universe(df: pd.DataFrame):
    """Write (or overwrite) the permanent Universe tab – done only once."""
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("Universe")
        sh.del_worksheet(ws)
    except gspread.WorksheetNotFound:
        pass

    ws = sh.add_worksheet(title="Universe", rows=len(df) + 50, cols=10)
    set_with_dataframe(ws, df, include_index=False, resize=True)

    # Freeze header + bold
    ws.format("A1:Z1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
    })
    ws.freeze(rows=1)

    print(f"[SHEETS] Saved {len(df):,} instruments → permanent tab 'Universe'")

def write_daily_results(df: pd.DataFrame):
    """Create a brand-new sheet named with today's date."""
    sh = get_spreadsheet()
    sheet_name = date.today().strftime("%Y-%m-%d")

    # Delete if already exists (safe for re-runs)
    try:
        old = sh.worksheet(sheet_name)
        sh.del_worksheet(old)
        print(f"[SHEETS] Deleted existing tab '{sheet_name}'")
    except gspread.WorksheetNotFound:
        pass

    rows = max(len(df) + 20, 50)
    cols = max(len(df.columns) + 2, 25)
    ws = sh.add_worksheet(title=sheet_name, rows=rows, cols=cols)

    set_with_dataframe(ws, df, include_index=False, resize=True)

    # Green header
    ws.format("A1:Z1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.15, "green": 0.55, "blue": 0.30},
    })
    ws.freeze(rows=1)

    print(f"[SHEETS] Wrote {len(df)} filtered stocks → new tab '{sheet_name}'")
    print(f"https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}")
