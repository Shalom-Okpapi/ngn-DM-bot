"""
Pulls offers from every enabled platform, filters out anything that fails
the trust/size checks, and picks the best usable rate(s).
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
    if offer["completion_rate"] < settings.MIN_COMPLETION_RATE:
        return False
    if offer["order_count"] < settings.MIN_ORDER_COUNT:
        return False
    return True


def _fits_amount(offer: dict, amount: float) -> bool:
    if offer["min_limit"] > amount or offer["max_limit"] < amount:
        return False
    if offer["available"] * offer["price"] < amount:
        return False
    return True


def _fetch_trusted_offers(trade_type: str, amount: float, fiat: str) -> list[dict]:
    all_offers = []
    for client in PLATFORM_CLIENTS:
        try:
            raw = client.fetch_ads(trade_type, amount=amount, fiat=fiat)
            log.info("%s: fetched %d raw offer(s) for %s %s at %s.",
                      client.__name__, len(raw), trade_type, fiat, amount)
            all_offers.extend(raw)
        except Exception as e:
            log.error("%s failed unexpectedly: %s", client.__name__, e)

    trusted = [o for o in all_offers if _passes_trust_filter(o) and _fits_amount(o, amount)]

    # Per-platform breakdown so a quiet platform is diagnosable at a glance:
    # 0 raw means the fetch itself failed; raw > 0 but 0 trusted means it's
    # losing on trust score or size fit, not a bug.
    platforms_seen = {o["platform"] for o in all_offers}
    for platform in platforms_seen:
        raw_count = sum(1 for o in all_offers if o["platform"] == platform)
        trusted_count = sum(1 for o in trusted if o["platform"] == platform)
        log.info("%s (%s): %d raw -> %d passed trust+size filters.", platform, fiat, raw_count, trusted_count)

    return trusted


def get_best_rate(trade_type: str, amount: float | None = None, fiat: str | None = None) -> dict | None:
    amount = settings.MIN_TRADE_AMOUNT if amount is None else amount
    fiat = settings.FIAT if fiat is None else fiat
    trusted = _fetch_trusted_offers(trade_type, amount, fiat)
    if not trusted:
        log.warning("No trusted offers found for %s %s at %s.", trade_type, fiat, amount)
        return None
    if trade_type == "SELL":
        return max(trusted, key=lambda o: o["price"])
    return min(trusted, key=lambda o: o["price"])


def get_top_matches(trade_type: str, amount: float, fiat: str | None = None, limit: int = 3) -> list[dict]:
    fiat = settings.FIAT if fiat is None else fiat
    trusted = _fetch_trusted_offers(trade_type, amount, fiat)
    trusted.sort(key=lambda o: o["price"], reverse=(trade_type == "SELL"))
    return trusted[:limit]


def get_market_snapshot(amount: float | None = None, fiat: str | None = None) -> dict:
    return {
        "buy": get_best_rate("BUY", amount=amount, fiat=fiat),
        "sell": get_best_rate("SELL", amount=amount, fiat=fiat),
    }
