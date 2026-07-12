"""
Central settings. Every os.getenv() call lives here, nowhere else.
This is a STANDALONE repo, separate from the group broadcast bot —
nothing here is shared/imported from that other project.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
DM_BOT_TOKEN = os.getenv("DM_BOT_TOKEN", "")
# Your own personal chat_id. Used for two things: (1) the bot DMs you if
# its own polling starts failing repeatedly, (2) it's the ONLY identity
# allowed to send /authorize, /revoke, /users, /pending commands. Get it
# by messaging this bot once, then visiting
# https://api.telegram.org/bot<DM_BOT_TOKEN>/getUpdates in a browser.
DM_ADMIN_CHAT_ID = os.getenv("DM_ADMIN_CHAT_ID", "")

DM_STATE_FILE = os.getenv("DM_STATE_FILE", "dm_state.json")
DM_AUTHORIZED_USERS_FILE = os.getenv("DM_AUTHORIZED_USERS_FILE", "authorized_users.json")

# Master switch for the paywall. If DM_ADMIN_CHAT_ID is empty while this
# is true, the bot falls back to open access rather than permanently
# locking everyone out (see dm_bot.py _is_authorized).
DM_REQUIRE_AUTHORIZATION = os.getenv("DM_REQUIRE_AUTHORIZATION", "true").lower() == "true"

# How long each job "listens" via Telegram long polling before exiting.
# Only push this close to your cron interval if the repo is PUBLIC —
# see README for why (private repos run out of free Actions minutes fast).
DM_POLL_WINDOW_SECONDS = int(os.getenv("DM_POLL_WINDOW_SECONDS", "240"))
DM_LONG_POLL_TIMEOUT = int(os.getenv("DM_LONG_POLL_TIMEOUT", "25"))

# Multi-user protections.
DM_SEARCH_RESULT_LIMIT = int(os.getenv("DM_SEARCH_RESULT_LIMIT", "3"))
DM_RATE_CACHE_TTL_SECONDS = int(os.getenv("DM_RATE_CACHE_TTL_SECONDS", "45"))
DM_USER_COOLDOWN_SECONDS = int(os.getenv("DM_USER_COOLDOWN_SECONDS", "3"))
DM_AWAITING_AMOUNT_TTL_SECONDS = int(os.getenv("DM_AWAITING_AMOUNT_TTL_SECONDS", "600"))
DM_MAX_ALERTS_PER_USER = int(os.getenv("DM_MAX_ALERTS_PER_USER", "5"))
# How often the background loop checks active alerts against live rates.
# Kept a bit above DM_RATE_CACHE_TTL_SECONDS so most checks hit fresh data.
DM_ALERT_CHECK_INTERVAL_SECONDS = int(os.getenv("DM_ALERT_CHECK_INTERVAL_SECONDS", "60"))

DM_RATE_HISTORY_FILE = os.getenv("DM_RATE_HISTORY_FILE", "rate_history.json")
# How often a rate sample gets recorded per currency, for /trend. Hourly
# is plenty for a 24h/7d comparison and keeps the history file tiny.
DM_TREND_SAMPLE_INTERVAL_SECONDS = int(os.getenv("DM_TREND_SAMPLE_INTERVAL_SECONDS", "3600"))
DM_TREND_RETENTION_SECONDS = int(os.getenv("DM_TREND_RETENTION_SECONDS", str(7 * 24 * 3600)))

# --- Market ---
ASSET = os.getenv("ASSET", "USDT")
FIAT = os.getenv("FIAT", "NGN")  # default currency when none is specified
# Every fiat the bot will accept in /current <fiat> and /search <amount> <fiat>.
# Tunable without a code change — add or remove currencies here.
SUPPORTED_FIATS = tuple(
    f.strip().upper() for f in os.getenv("SUPPORTED_FIATS", "NGN,JPY,CHF,GBP,EUR,USD").split(",") if f.strip()
)
MIN_TRADE_AMOUNT = float(os.getenv("MIN_TRADE_AMOUNT", "50000"))  # fallback for any fiat not listed below

# Default trade size used when nobody specifies an amount (e.g. bare
# /current). A single flat number means wildly different real amounts
# across currencies — 50,000 NGN is about $30, but 50,000 GBP would be
# about $63,000. Any currency not listed here falls back to MIN_TRADE_AMOUNT.
DEFAULT_TRADE_AMOUNTS = {
    "NGN": 50000.0,
    "JPY": 5000.0,
    "USD": 50.0,
    "EUR": 50.0,
    "GBP": 40.0,
    "CHF": 50.0,
}


def default_trade_amount(fiat: str) -> float:
    return DEFAULT_TRADE_AMOUNTS.get(fiat, MIN_TRADE_AMOUNT)


MIN_COMPLETION_RATE = float(os.getenv("MIN_COMPLETION_RATE", "0.95"))
MIN_ORDER_COUNT = int(os.getenv("MIN_ORDER_COUNT", "20"))

ENABLE_BINANCE = os.getenv("ENABLE_BINANCE", "true").lower() == "true"
ENABLE_BYBIT = os.getenv("ENABLE_BYBIT", "true").lower() == "true"
ENABLE_NOONES = os.getenv("ENABLE_NOONES", "false").lower() == "true"
NOONES_API_KEY = os.getenv("NOONES_API_KEY", "")
NOONES_API_SECRET = os.getenv("NOONES_API_SECRET", "")

# --- Market price tracking (/market) ---
# Separate from the P2P rate-checking above — this hits Binance's public
# SPOT market API (official, documented, no API key needed), not the P2P
# endpoints. Real market prices, not P2P merchant quotes.
# {display symbol: Binance spot trading pair}. USDC/USDT stands in for
# "USDT" itself, since a coin can't be meaningfully priced in itself —
# it's shown as a peg-stability check instead.
TRACKED_MARKET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "USDC": "USDCUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
}
DM_MARKET_CACHE_TTL_SECONDS = int(os.getenv("DM_MARKET_CACHE_TTL_SECONDS", "60"))
