"""
Interactive DM bot — replies only to whoever messages it directly, on
demand. Standalone from the group broadcast bot; nothing here is shared.

ARCHITECTURE: GitHub Actions doesn't run always-on servers, so this uses
Telegram long polling instead of a webhook: each scheduled run "listens"
for DM_POLL_WINDOW_SECONDS, then exits right before the next cron tick,
so coverage is close to continuous without needing separate hosting.

COST: this only makes financial sense on a PUBLIC repo (unlimited free
GitHub Actions minutes). On a private repo, a 240s window every 5 minutes
uses roughly 34,500 minutes/month against a 2,000/month free budget.
See README before you deploy.

MULTI-USER PROTECTIONS (see README for the full list of problems these
solve):
  - Every fetch is cached briefly so a burst of people asking /current
    at once only hits Binance/Bybit once.
  - A per-user cooldown stops one person's rapid taps from starving
    everyone else being processed in the same batch.
  - State is keyed by chat_id throughout, so users never see each other's
    in-progress /search flow.
  - Only private 1:1 chats are answered — group/channel traffic is
    ignored if this bot ever ends up added somewhere unexpected.
  - Abandoned /search prompts expire instead of accumulating forever.
  - Repeated Telegram polling failures DM you directly (if DM_ADMIN_CHAT_ID
    is set) instead of failing silently.
"""
import json
import logging
import os
import tempfile
import time

import requests

import settings
import aggregator
from time_utils import now_wat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

API_BASE = f"https://api.telegram.org/bot{settings.DM_BOT_TOKEN}"

_BREAKING_CHARS = ("_", "*", "`", "[", "]")


def _sanitize(text: str) -> str:
    """Strip characters that break Telegram's Markdown parser. Merchant
    nicknames are free text set by strangers on Binance/Bybit."""
    if not text:
        return "Unknown"
    cleaned = text
    for ch in _BREAKING_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    return cleaned or "Unknown"


WELCOME_TEXT = (
    "👋 *Welcome!*\n\n"
    "I check Binance and Bybit P2P live and show you the best trusted "
    f"{settings.ASSET}/{settings.FIAT} rates — only from merchants with a "
    "strong track record.\n\n"
    "Here's what I can do:\n"
    "• /current — best rates right now\n"
    "• /search <amount> — best merchants for a specific *naira* amount "
    "(e.g. /search 8000)\n\n"
    "You can also just type /search and I'll ask you for the amount.\n\n"
    "⚠️ I only show rates — I never touch your money. Always confirm the "
    "live price before you trade."
)


# ---------- state ----------

def _default_state() -> dict:
    return {
        "last_update_id": 0,
        "awaiting_amount": {},       # {chat_id: requested_at_unix_ts}
        "last_request_at": {},       # {chat_id: unix_ts} — per-user cooldown
        "rate_cache": None,          # {"fetched_at": ts, "snapshot": {...}}
        "consecutive_poll_failures": 0,
    }


def load_state() -> dict:
    if not os.path.exists(settings.DM_STATE_FILE):
        return _default_state()
    try:
        with open(settings.DM_STATE_FILE, "r") as f:
            data = json.load(f)
        # Backfill any keys added in later versions so an older state
        # file never crashes a fresh deploy.
        for key, value in _default_state().items():
            data.setdefault(key, value)
        return data
    except (json.JSONDecodeError, OSError):
        log.warning("DM state file unreadable, starting fresh.")
        return _default_state()


def save_state(state: dict) -> None:
    dir_name = os.path.dirname(settings.DM_STATE_FILE) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, settings.DM_STATE_FILE)


# ---------- Telegram I/O ----------

def send_message(chat_id, text: str) -> bool:
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code == 403:
            log.info("Chat %s has blocked the bot or is unreachable — skipping.", chat_id)
            return False
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error("Failed to send DM to %s: %s", chat_id, e)
        return False


def notify_admin(message: str) -> None:
    if settings.DM_ADMIN_CHAT_ID:
        send_message(settings.DM_ADMIN_CHAT_ID, f"⚠️ DM bot: {message}")


def _parse_amount(text: str):
    cleaned = text.replace(",", "").replace("₦", "").strip()
    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


# ---------- formatting ----------

def _format_offer_full(offer: dict) -> str:
    lines = [
        f"₦{offer['price']:,.2f} per {settings.ASSET}",
        f"Merchant: {_sanitize(offer['merchant_name'])} "
        f"({offer['completion_rate']*100:.1f}% success, {offer['order_count']} trades)",
        f"Trade size: ₦{offer['min_limit']:,.0f} – ₦{offer['max_limit']:,.0f} | {offer['platform']}",
    ]
    methods = offer.get("payment_methods") or []
    if methods:
        lines.append(f"Pays via: {', '.join(_sanitize(m) for m in methods[:4])}")
    if offer.get("link"):
        lines.append(f"Trade here: {offer['link']}")
    return "\n".join(lines)


def _format_offer_compact(rank: int, offer: dict) -> str:
    link_line = f"\n   {offer['link']}" if offer.get("link") else ""
    return (
        f"{rank}. ₦{offer['price']:,.2f} — {_sanitize(offer['merchant_name'])} "
        f"({offer['completion_rate']*100:.1f}%, {offer['order_count']} trades, "
        f"{offer['platform']}){link_line}"
    )


# ---------- rate caching (protects Binance/Bybit + speeds up replies under load) ----------

def _get_snapshot_cached(state: dict) -> dict:
    cache = state.get("rate_cache")
    if cache and (time.time() - cache["fetched_at"]) <= settings.DM_RATE_CACHE_TTL_SECONDS:
        return cache["snapshot"]
    snapshot = aggregator.get_market_snapshot()
    state["rate_cache"] = {"fetched_at": time.time(), "snapshot": snapshot}
    return snapshot


# ---------- per-user fairness ----------

def _check_cooldown(state: dict, chat_key: str) -> bool:
    """True if this chat may trigger a new fetch right now. Stops one
    user's rapid taps from starving everyone else in the same batch and
    from hammering Binance/Bybit."""
    last = state["last_request_at"].get(chat_key)
    now = time.time()
    if last and (now - last) < settings.DM_USER_COOLDOWN_SECONDS:
        return False
    state["last_request_at"][chat_key] = now
    return True


def _clean_expired_awaiting(state: dict) -> None:
    """Drop /search prompts nobody ever answered, so a random number
    typed long after is never misread as answering an old prompt."""
    now = time.time()
    expired = [
        chat_key for chat_key, requested_at in state["awaiting_amount"].items()
        if now - requested_at > settings.DM_AWAITING_AMOUNT_TTL_SECONDS
    ]
    for chat_key in expired:
        del state["awaiting_amount"][chat_key]


# ---------- command handlers ----------

def reply_current(state: dict, chat_id):
    snapshot = _get_snapshot_cached(state)
    if not snapshot["buy"] and not snapshot["sell"]:
        send_message(chat_id, "I couldn't find trusted rates right now — try again shortly.")
        return

    lines = [f"📊 *{settings.ASSET}/{settings.FIAT} — best rates right now*\n"]
    if snapshot["sell"]:
        lines.append("🟢 *Best price to SELL your USDT*")
        lines.append(_format_offer_full(snapshot["sell"]))
        lines.append("")
    if snapshot["buy"]:
        lines.append("🔵 *Best price to BUY USDT*")
        lines.append(_format_offer_full(snapshot["buy"]))
    lines.append(f"\n⏱ {now_wat()}")
    lines.append("\n⚠️ Rates can change fast — please confirm before you trade.")
    send_message(chat_id, "\n".join(lines))


def reply_search(chat_id, amount: float):
    sell_matches = aggregator.get_top_matches("SELL", amount, limit=settings.DM_SEARCH_RESULT_LIMIT)
    buy_matches = aggregator.get_top_matches("BUY", amount, limit=settings.DM_SEARCH_RESULT_LIMIT)

    if not sell_matches and not buy_matches:
        send_message(chat_id,
            f"I couldn't find any trusted merchants for ₦{amount:,.0f} right now. "
            "Try a different amount, or check back shortly.")
        return

    lines = [f"🔍 *Merchants for ₦{amount:,.0f}*\n"]
    if sell_matches:
        lines.append("🟢 *Sell your USDT to:*")
        for i, offer in enumerate(sell_matches, 1):
            lines.append(_format_offer_compact(i, offer))
        lines.append("")
    if buy_matches:
        lines.append("🔵 *Buy USDT from:*")
        for i, offer in enumerate(buy_matches, 1):
            lines.append(_format_offer_compact(i, offer))
    lines.append(f"\n⏱ {now_wat()}")
    lines.append("\n⚠️ Rates can change fast — please confirm before you trade.")
    send_message(chat_id, "\n".join(lines))


def handle_message(state: dict, chat_id, text: str):
    text = (text or "").strip()
    chat_key = str(chat_id)
    _clean_expired_awaiting(state)

    if text.startswith("/start") or text == "/help":
        state["awaiting_amount"].pop(chat_key, None)
        send_message(chat_id, WELCOME_TEXT)
        return

    if text == "/current":
        state["awaiting_amount"].pop(chat_key, None)
        if not _check_cooldown(state, chat_key):
            send_message(chat_id, "One moment — still working on your last request.")
            return
        reply_current(state, chat_id)
        return

    if text.startswith("/search"):
        parts = text.split(maxsplit=1)
        amount = _parse_amount(parts[1]) if len(parts) == 2 else None
        if amount:
            if not _check_cooldown(state, chat_key):
                send_message(chat_id, "One moment — still working on your last request.")
                return
            state["awaiting_amount"].pop(chat_key, None)
            reply_search(chat_id, amount)
        else:
            state["awaiting_amount"][chat_key] = time.time()
            send_message(chat_id, "How much do you want to trade, in naira? "
                                    "Just reply with a number, like 50000.")
        return

    if chat_key in state["awaiting_amount"]:
        amount = _parse_amount(text)
        if amount:
            if not _check_cooldown(state, chat_key):
                send_message(chat_id, "One moment — still working on your last request.")
                return  # leave the prompt active so they can just resend
            state["awaiting_amount"].pop(chat_key, None)
            reply_search(chat_id, amount)
        else:
            send_message(chat_id, "That doesn't look like a number. Try something like 50000.")
        return

    send_message(chat_id, "I didn't quite get that. Try /current or /search <amount>, "
                            "or /start to see everything I can do.")


# ---------- polling loop ----------

def poll_once(state: dict) -> bool:
    """One getUpdates call. Returns True if any updates were processed."""
    try:
        resp = requests.get(f"{API_BASE}/getUpdates", params={
            "offset": state["last_update_id"] + 1,
            "timeout": settings.DM_LONG_POLL_TIMEOUT,
            "allowed_updates": '["message"]',
        }, timeout=settings.DM_LONG_POLL_TIMEOUT + 10)
        resp.raise_for_status()
        data = resp.json()
        state["consecutive_poll_failures"] = 0
    except Exception as e:
        state["consecutive_poll_failures"] = state.get("consecutive_poll_failures", 0) + 1
        log.error("getUpdates failed (failure #%d): %s", state["consecutive_poll_failures"], e)
        if state["consecutive_poll_failures"] == 3:
            notify_admin("getUpdates has failed 3 times in a row — "
                         "check DM_BOT_TOKEN and the Actions log.")
        time.sleep(5)
        return False

    updates = data.get("result", [])
    for update in updates:
        state["last_update_id"] = update["update_id"]
        message = update.get("message")
        if not message:
            continue
        # Only ever respond in private 1:1 chats — ignore group/channel
        # traffic if this bot ever ends up added somewhere unexpected.
        if message.get("chat", {}).get("type") != "private":
            continue
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        if chat_id is not None:
            handle_message(state, chat_id, text)

    return bool(updates)


def main():
    if not settings.DM_BOT_TOKEN:
        log.error("DM_BOT_TOKEN is not set — nothing to do.")
        return

    state = load_state()
    deadline = time.monotonic() + settings.DM_POLL_WINDOW_SECONDS

    while time.monotonic() < deadline:
        had_updates = poll_once(state)
        if had_updates:
            save_state(state)

    save_state(state)
    log.info("Polling window closed, exiting cleanly for next cron tick.")


if __name__ == "__main__":
    main()
