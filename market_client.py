"""
Market price client — Binance's public SPOT market API.

Unlike binance_client.py/bybit_client.py (undocumented P2P endpoints,
individual merchant quotes), this uses Binance's OFFICIAL, documented
public market-data API: https://binance-docs.github.io/apidocs/spot/en/
No API key needed for these endpoints, and it's much less likely to
change without notice than the P2P scraping this bot otherwise relies on.

This is a genuinely different data source from the rest of the bot:
real, continuous spot market prices, not P2P merchant listings.
"""
import logging

from http_utils import get_with_retry

log = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com/api/v3"


def fetch_current(symbol: str) -> dict | None:
    """Current price + 24h change for a Binance spot symbol (e.g. 'BTCUSDT').
    Returns {"price": float, "change_24h_pct": float, "high_24h": float,
    "low_24h": float} or None if the request fails."""
    try:
        resp = get_with_retry(f"{BASE_URL}/ticker/24hr", params={"symbol": symbol}, timeout=15)
        data = resp.json()
        return {
            "price": float(data["lastPrice"]),
            "change_24h_pct": float(data["priceChangePercent"]),
            "high_24h": float(data["highPrice"]),
            "low_24h": float(data["lowPrice"]),
        }
    except Exception as e:
        log.error("Binance spot ticker request failed for %s: %s", symbol, e)
        return None


def fetch_price_days_ago(symbol: str, days: int) -> float | None:
    """Closing price approximately `days` ago, using daily candles.
    Returns None if the request fails or there's not enough history yet
    (a coin that only recently listed, for example)."""
    try:
        resp = get_with_retry(f"{BASE_URL}/klines", params={
            "symbol": symbol,
            "interval": "1d",
            "limit": days + 1,
        }, timeout=15)
        candles = resp.json()
        if not candles or len(candles) < days + 1:
            log.warning("Not enough kline history for %s to look back %d days.", symbol, days)
            return None
        oldest_candle = candles[0]
        return float(oldest_candle[4])  # index 4 = close price
    except Exception as e:
        log.error("Binance klines request failed for %s (%d days): %s", symbol, days, e)
        return None
