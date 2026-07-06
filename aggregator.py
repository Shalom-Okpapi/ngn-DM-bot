"""
Pulls offers from every enabled platform, filters out anything that fails
the trust/size checks, and picks the best usable rate(s).

Two entry points:
  - get_market_snapshot: single best BUY/SELL rate (used by /current)
  - get_top_matches: ranked list of merchants for one specific trade size
    (used by /search)
"""
import logging

import settings
import binance_client
import bybit_client
import noones_client

log = logging.getLogger(__name__)

PLATFORM_CLIENTS = []
if settings.ENABLE_BINANCE:
    PLATFORM_CLIENTS.append(binance_client)
if settings.ENABLE_BYBIT:
    PLATFORM_CLIENTS.append(bybit_client)
if settings.ENABLE_NOONES:
    PLATFORM_CLIENTS.append(noones_client)


def _passes_trust_filter(offer: dict) -> bool:
    """Trust check only — nothing to do with trade size."""
    if offer["completion_rate"] < settings.MIN_COMPLETION_RATE:
        return False
    if offer["order_count"] < settings.MIN_ORDER_COUNT:
        return False
    return True


def _fits_amount(offer: dict, amount: float) -> bool:
    """Can this merchant actually fulfill a trade of this exact size?"""
    if offer["min_limit"] > amount or offer["max_limit"] < amount:
        return False
    if offer["available"] * offer["price"] < amount:
        return False
    return True


def _fetch_trusted_offers(trade_type: str, amount: float) -> list[dict]:
    all_offers = []
    for client in PLATFORM_CLIENTS:
        try:
            all_offers.extend(client.fetch_ads(trade_type, amount=amount))
        except Exception as e:
            log.error("%s failed unexpectedly: %s", client.__name__, e)
    return [o for o in all_offers if _passes_trust_filter(o) and _fits_amount(o, amount)]


def get_best_rate(trade_type: str, amount: float | None = None) -> dict | None:
    """
    trade_type: "BUY" or "SELL" — "SELL" means "I want to sell my USDT for
    naira" (want the HIGHEST price), "BUY" means "I want to buy USDT with
    naira" (want the LOWEST price).
    """
    amount = settings.MIN_TRADE_AMOUNT if amount is None else amount
    trusted = _fetch_trusted_offers(trade_type, amount)
    if not trusted:
        log.warning("No trusted offers found for %s at ₦%s.", trade_type, amount)
        return None

    if trade_type == "SELL":
        return max(trusted, key=lambda o: o["price"])
    return min(trusted, key=lambda o: o["price"])


def get_top_matches(trade_type: str, amount: float, limit: int = 3) -> list[dict]:
    """Ranked list of trusted merchants that can fulfill this exact
    amount — lets a user pick from real options instead of one forced
    choice."""
    trusted = _fetch_trusted_offers(trade_type, amount)
    trusted.sort(key=lambda o: o["price"], reverse=(trade_type == "SELL"))
    return trusted[:limit]


def get_market_snapshot(amount: float | None = None) -> dict:
    return {
        "buy": get_best_rate("BUY", amount=amount),
        "sell": get_best_rate("SELL", amount=amount),
    }
