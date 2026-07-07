"""
Binance P2P client.

Uses the public (undocumented) endpoint the Binance P2P web app itself calls.
This is NOT official Binance API — it can change or get rate-limited without
notice. If this starts returning empty results, check the response body
first (we log it on failure) before assuming the code is broken.
"""
import logging

import settings
from http_utils import post_with_retry

log = logging.getLogger(__name__)

URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def fetch_ads(trade_type: str, amount: float | None = None, fiat: str | None = None, rows: int = 10) -> list[dict]:
    """
    trade_type: "BUY" or "SELL" (from the perspective of the ad poster).
    amount: trade size in the target fiat. Defaults to settings.MIN_TRADE_AMOUNT.
    fiat: which currency to search against (e.g. "NGN", "JPY"). Defaults
    to settings.FIAT. Must be one of settings.SUPPORTED_FIATS to be
    reachable from the bot's commands — this client itself will happily
    query any string Binance accepts.
    """
    amount = settings.MIN_TRADE_AMOUNT if amount is None else amount
    fiat = settings.FIAT if fiat is None else fiat
    body = {
        "page": 1,
        "rows": rows,
        "payTypes": [],
        "publisherType": None,
        "asset": settings.ASSET,
        "tradeType": trade_type,
        "fiat": fiat,
        "transAmount": amount,
        "merchantCheck": True,
    }

    try:
        resp = post_with_retry(URL, json=body, headers=HEADERS, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error("Binance P2P request failed after retry (fiat=%s): %s", fiat, e)
        return []

    if not data.get("success") or not data.get("data"):
        log.warning("Binance P2P returned no usable data for fiat=%s: %s", fiat, data.get("message"))
        return []

    offers = []
    for item in data["data"]:
        adv = item.get("adv", {})
        merchant = item.get("advertiser", {})
        try:
            methods = [
                m.get("tradeMethodName") or m.get("identifier")
                for m in (adv.get("tradeMethods") or [])
            ]
            methods = [m for m in methods if m]

            adv_no = adv.get("advNo", "")
            link = (f"https://c2c.binance.com/en/adv?code={adv_no}" if adv_no
                    else f"https://p2p.binance.com/en/advertiserDetail?advertiserNo={merchant.get('userNo', '')}")

            offers.append({
                "platform": "Binance",
                "trade_type": trade_type,
                "fiat": fiat,
                "price": float(adv["price"]),
                "merchant_name": merchant.get("nickName", "Unknown"),
                "completion_rate": float(merchant.get("monthFinishRate") or 0),
                "order_count": int(merchant.get("monthOrderCount") or 0),
                "min_limit": float(adv.get("minSingleTransAmount") or 0),
                "max_limit": float(adv.get("maxSingleTransAmount") or 0),
                "available": float(adv.get("surplusAmount") or 0),
                "payment_methods": methods,
                "adv_no": adv_no,
                "link": link,
            })
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Skipping malformed Binance offer: %s", e)
            continue

    return offers
