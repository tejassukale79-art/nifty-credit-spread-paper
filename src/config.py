"""Central configuration for the NIFTY credit-spread algo system."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SPOT_DIR = DATA_DIR / "spot"
OPT_DIR = DATA_DIR / "options"
RESULTS_DIR = ROOT / "results"

# token: env var (CI / GitHub Actions) takes precedence over the local file
_token_file = CONFIG_DIR / "token.txt"
TOKEN = os.environ.get("UPSTOX_TOKEN", "").strip() or \
    (_token_file.read_text().strip() if _token_file.exists() else "")
BASE_URL = "https://api.upstox.com"
NIFTY_KEY = "NSE_INDEX|Nifty 50"

# ---- backtest window ----
# Expired-option archive starts with the 2024-10-03 expiry (contract data from ~2024-09-02).
SPOT_START = "2024-08-16"        # extra history for the 800-min alpha warm-up
BACKTEST_START = "2024-09-27"    # first day fully inside option-data coverage
BACKTEST_END = "2026-07-14"      # last completed weekly expiry

# ---- strategy parameters (from the strategy spec; ambiguous items are
# marked TUNABLE and can be changed without re-downloading data) ----
ALPHA_LOOKBACK = 800             # minutes, ts_rank window for alpha
ALPHA2_LOOKBACK = 300            # minutes, ts_rank window for alpha2
RET_MINUTES = 5                  # trailing price-change horizon (minutes)
LONG_TH = 0.8                    # both alphas above -> credit put spread
SHORT_TH = 0.2                   # both alphas below -> credit call spread
VOL_RATIO_WINDOW = 15            # best fit to Dhan live entries (sweep 2026-07-20)
OPT_VOL_WINDOW = 240             # best fit to Dhan live entries (sweep 2026-07-20)
STRIKE_STEP = 50
WING_POINTS = 400                # wing distance for the spread

ENTRY_START = "10:15"
ENTRY_END = "14:15"
SQUARE_OFF = "15:15"

# Stop-loss: exit when mark-to-market loss >= SL_PCT_OF_MARGIN * margin.
# Margin is estimated as the spread's regulatory max loss (width - credit) * lot,
# which is what brokers block for a defined-risk spread.
SL_PCT_OF_MARGIN = 0.25          # TUNABLE: spec says "based on margin requirements" without a number

# ---- costs (per leg, Indian F&O) ----
BROKERAGE_PER_ORDER = 20.0       # flat per order (Upstox)
STT_SELL = 0.001                 # 0.1% of sell-side premium turnover
EXCH_TXN = 0.0003503             # NSE options transaction charge on premium
SEBI = 0.000001
STAMP_BUY = 0.00003
GST = 0.18                       # on brokerage + exchange txn + SEBI
SLIPPAGE = 0.05                  # rupees per leg per fill (tick = 0.05)
