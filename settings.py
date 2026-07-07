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

# --- Market ---
ASSET = os.getenv("ASSET", "USDT")
FIAT = os.getenv("FIAT", "NGN")  # default currency when none is specified
# Every fiat the bot will accept in /current <fiat> and /search <amount> <fiat>.
# Tunable without a code change — add or remove currencies here.
SUPPORTED_FIATS = tuple(
    f.strip().upper() for f in os.getenv("SUPPORTED_FIATS", "NGN,JPY,CHF,GBP,EUR").split(",") if f.strip()
)
MIN_TRADE_AMOUNT = float(os.getenv("MIN_TRADE_AMOUNT", "50000"))
MIN_COMPLETION_RATE = float(os.getenv("MIN_COMPLETION_RATE", "0.95"))
MIN_ORDER_COUNT = int(os.getenv("MIN_ORDER_COUNT", "20"))

ENABLE_BINANCE = os.getenv("ENABLE_BINANCE", "true").lower() == "true"
ENABLE_BYBIT = os.getenv("ENABLE_BYBIT", "true").lower() == "true"
ENABLE_NOONES = os.getenv("ENABLE_NOONES", "false").lower() == "true"
NOONES_API_KEY = os.getenv("NOONES_API_KEY", "")
NOONES_API_SECRET = os.getenv("NOONES_API_SECRET", "")
