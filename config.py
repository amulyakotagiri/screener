import os
from dotenv import load_dotenv

load_dotenv()

# Dhan
DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# Google
GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "credentials/service_account.json"
)

# Single master spreadsheet
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

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
STRONGLY_RISING_THRESHOLD   = 0.15
FLAT_THRESHOLD              = 0.05
STRONGLY_FALLING_THRESHOLD  = -0.15

HISTORY_DAYS      = 450
REQUEST_INTERVAL  = 0.23
MAX_RETRIES       = 4
RETRY_DELAYS      = [1, 2, 4, 8]

CACHE_DIR = "data/cache"
