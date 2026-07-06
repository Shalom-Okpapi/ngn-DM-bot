"""
Noones (formerly Paxful) client — PHASE 2, NOT ACTIVE BY DEFAULT.

Unlike Binance and Bybit, Noones requires an authenticated API key/secret
for every endpoint, even read-only ones. To enable: create an API key at
Noones (Settings -> Developer), set NOONES_API_KEY/NOONES_API_SECRET and
ENABLE_NOONES=true, and implement the HMAC request signing per their docs
(dev.noones.com) — a different auth scheme from Binance/Bybit.

Until then this returns an empty list so the bot runs fine on
Binance + Bybit alone.
"""
import logging

import settings

log = logging.getLogger(__name__)


def fetch_ads(trade_type: str, amount: float | None = None, rows: int = 10) -> list[dict]:
    if not settings.ENABLE_NOONES:
        return []

    if not settings.NOONES_API_KEY or not settings.NOONES_API_SECRET:
        log.warning("ENABLE_NOONES is true but NOONES_API_KEY/SECRET are missing. Skipping.")
        return []

    log.warning("Noones client is not implemented yet (needs HMAC request signing).")
    return []
