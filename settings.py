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
# Optional: your own personal chat_id. If set, the bot DMs you when its
# own polling starts failing repeatedly, so you find out without having
# to check the Actions log. Get your chat_id the same way as the group
# bot's — message this bot once, then visit
# https://api.telegram.org/bot<DM_BOT_TOKEN>/getUpdates in a browser.
DM_ADMIN_CHAT_ID = os.getenv("DM_ADMIN_CHAT_ID", "")

DM_STATE_FILE = os.getenv("DM_STATE_FILE", "dm_state.json")

# How long each job "listens" via Telegram long polling before exiting.
# Only push this close to your cron interval if the repo is PUBLIC —
# see README for why (private repos run out of free Actions minutes fast).
DM_POLL_WINDOW_SECONDS = int(os.getenv("DM_POLL_WINDOW_SECONDS", "240"))
DM_LONG_POLL_TIMEOUT = int(os.getenv("DM_LONG_POLL_TIMEOUT", "25"))

# Multi-user protections.
DM_SEARCH_RESULT_LIMIT = int(os.getenv("DM_SEARCH_RESULT_LIMIT", "3"))
# How long a fetched rate snapshot is reused before hitting Binance/Bybit
# again. Protects the exchanges (and your reply speed) if several people
# ask /current around the same time.
DM_RATE_CACHE_TTL_SECONDS = int(os.getenv("DM_RATE_CACHE_TTL_SECONDS", "45"))
# Minimum seconds between one person's requests. Stops one impatient user
# (or a bug on their end) from starving everyone else in the same batch.
DM_USER_COOLDOWN_SECONDS = int(os.getenv("DM_USER_COOLDOWN_SECONDS", "3"))
# If someone types /search with no amount and never answers, forget we
# were waiting after this long — otherwise a random number they type
# days later could get misread as answering an old prompt.
DM_AWAITING_AMOUNT_TTL_SECONDS = int(os.getenv("DM_AWAITING_AMOUNT_TTL_SECONDS", "600"))

# --- Market ---
ASSET = os.getenv("ASSET", "USDT")
FIAT = os.getenv("FIAT", "NGN")
MIN_TRADE_AMOUNT = float(os.getenv("MIN_TRADE_AMOUNT", "50000"))  # default amount for /current
MIN_COMPLETION_RATE = float(os.getenv("MIN_COMPLETION_RATE", "0.95"))
MIN_ORDER_COUNT = int(os.getenv("MIN_ORDER_COUNT", "20"))

ENABLE_BINANCE = os.getenv("ENABLE_BINANCE", "true").lower() == "true"
ENABLE_BYBIT = os.getenv("ENABLE_BYBIT", "true").lower() == "true"
ENABLE_NOONES = os.getenv("ENABLE_NOONES", "false").lower() == "true"
NOONES_API_KEY = os.getenv("NOONES_API_KEY", "")
NOONES_API_SECRET = os.getenv("NOONES_API_SECRET", "")
