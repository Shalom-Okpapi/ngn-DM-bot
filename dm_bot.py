"""
Interactive DM bot — replies only to whoever messages it directly, on
demand. Standalone from the group broadcast bot; nothing here is shared.

ARCHITECTURE: see README for why this uses long polling instead of a
webhook, and why it needs a PUBLIC repo to be economical.

ACCESS CONTROL: usernames are not a payment wall — anyone who gets the
link from a paying customer can use it too. The actual gate lives here:
an allowlist of authorized chat_ids, checked before /current or /search
run. You (identified by DM_ADMIN_CHAT_ID) authorize people yourself by
sending /authorize <chat_id> from your own chat with the bot, after
they've paid. See README for the full flow.

MULTI-USER PROTECTIONS carried over from the earlier design:
  - Rate snapshots are cached briefly so a burst of people asking
    /current at once only hits Binance/Bybit once.
  - A per-user cooldown stops one person's rapid taps from starving
    everyone else being processed in the same batch.
  - State is keyed by chat_id throughout, so users never see each
    other's in-progress /search flow.
  - Only private 1:1 chats are answered.
  - Abandoned /search prompts expire instead of accumulating forever.
  - Repeated Telegram polling failures DM you directly.
"""
import json
import logging
import os
import re
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
    """Strip characters that break Telegram's Markdown parser."""
    if not text:
        return "Unknown"
    cleaned = text
    for ch in _BREAKING_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    return cleaned or "Unknown"


WELCOME_TEXT = (
    "👋 *Welcome back!*\n\n"
    "Here's what I can do:\n"
    "• /current — best rates right now\n"
    "• /search <amount> — best merchants for a specific *naira* amount "
    "(e.g. /search 8000)\n\n"
    "You can also just type /search and I'll ask you for the amount.\n\n"
    "⚠️ I only show rates — I never touch your money. Always confirm the "
    "live price before you trade."
)

# Shown to anyone who isn't authorized yet — this IS the sales pitch,
# so /start, /current, /search all show the same thing until they've paid.
# >>> Replace the wallet address below with your real TRC20 USDT address <<<
PAYWALL_TEXT = (
    "👋 *Welcome!*\n\n"
    "I check Binance and Bybit P2P live and show you the best trusted "
    f"{settings.ASSET}/{settings.FIAT} rates — only from merchants with a "
    "strong track record.\n\n"
    "• /current — best rates right now\n"
    "• /search <amount> — best merchants for your exact trade size, in naira\n\n"
    "This is a paid tool: *$22/month*, paid in USDT (TRC20 network) to:\n"
    "`TAFHrQuCunTab2iK6vqfneKMLhJ3y4DmCD`\n\n"
    "Once you've sent it, message me directly to confirm — you'll be "
    "activated within minutes."
)


# ---------- state (operational — polling, cache, cooldowns) ----------

def _default_state() -> dict:
    return {
        "last_update_id": 0,
        "awaiting_amount": {},
        "last_request_at": {},
        "rate_cache": None,
        "consecutive_poll_failures": 0,
    }


def load_state() -> dict:
    if not os.path.exists(settings.DM_STATE_FILE):
        return _default_state()
    try:
        with open(settings.DM_STATE_FILE, "r") as f:
            data = json.load(f)
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


# ---------- authorized users (business data — who's paid) ----------

def _default_auth() -> dict:
    return {
        "authorized": {},       # {chat_id: {"authorized_at": ts}}
        "notified_admin": [],   # chat_ids already flagged to admin, so we don't spam
    }


def load_authorized_users() -> dict:
    if not os.path.exists(settings.DM_AUTHORIZED_USERS_FILE):
        return _default_auth()
    try:
        with open(settings.DM_AUTHORIZED_USERS_FILE, "r") as f:
            data = json.load(f)
        for key, value in _default_auth().items():
            data.setdefault(key, value)
        return data
    except (json.JSONDecodeError, OSError):
        log.warning("Authorized-users file unreadable, starting fresh.")
        return _default_auth()


def save_authorized_users(auth_data: dict) -> None:
    dir_name = os.path.dirname(settings.DM_AUTHORIZED_USERS_FILE) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    with os.fdopen(fd, "w") as f:
        json.dump(auth_data, f, indent=2)
    os.replace(tmp_path, settings.DM_AUTHORIZED_USERS_FILE)


def _is_admin(chat_key: str) -> bool:
    return bool(settings.DM_ADMIN_CHAT_ID) and chat_key == str(settings.DM_ADMIN_CHAT_ID)


def _is_authorized(auth_data: dict, chat_key: str) -> bool:
    if not settings.DM_REQUIRE_AUTHORIZATION:
        return True
    if not settings.DM_ADMIN_CHAT_ID:
        # Nobody could ever grant access without an admin configured —
        # fail open rather than permanently locking everyone out by mistake.
        return True
    if _is_admin(chat_key):
        return True
    return chat_key in auth_data["authorized"]


def _looks_like_chat_id(text: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", text.strip()))


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
    """Operational alerts — bot health, failures."""
    if settings.DM_ADMIN_CHAT_ID:
        send_message(settings.DM_ADMIN_CHAT_ID, f"⚠️ DM bot: {message}")


def notify_admin_new_inquiry(chat_key: str, display_name: str) -> None:
    """Sales alerts — someone wants in. Kept visually distinct from
    operational alerts so it doesn't get lost among error logs."""
    if settings.DM_ADMIN_CHAT_ID:
        send_message(settings.DM_ADMIN_CHAT_ID,
            f"💰 New inquiry: {_sanitize(display_name)} (chat_id {chat_key})\n"
            f"Once they've paid: /authorize {chat_key}")


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


# ---------- rate caching ----------

def _get_snapshot_cached(state: dict) -> dict:
    cache = state.get("rate_cache")
    if cache and (time.time() - cache["fetched_at"]) <= settings.DM_RATE_CACHE_TTL_SECONDS:
        return cache["snapshot"]
    snapshot = aggregator.get_market_snapshot()
    state["rate_cache"] = {"fetched_at": time.time(), "snapshot": snapshot}
    return snapshot


# ---------- per-user fairness ----------

def _check_cooldown(state: dict, chat_key: str) -> bool:
    last = state["last_request_at"].get(chat_key)
    now = time.time()
    if last and (now - last) < settings.DM_USER_COOLDOWN_SECONDS:
        return False
    state["last_request_at"][chat_key] = now
    return True


def _clean_expired_awaiting(state: dict) -> None:
    now = time.time()
    expired = [
        chat_key for chat_key, requested_at in state["awaiting_amount"].items()
        if now - requested_at > settings.DM_AWAITING_AMOUNT_TTL_SECONDS
    ]
    for chat_key in expired:
        del state["awaiting_amount"][chat_key]


# ---------- command handlers (paid features) ----------

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


# ---------- admin commands ----------

def handle_admin_command(auth_data: dict, chat_id, text: str) -> bool:
    """Returns True if this was an admin command and has been handled."""
    parts = text.split(maxsplit=1)
    command = parts[0]

    if command == "/authorize" and len(parts) == 2:
        target = parts[1].strip()
        if not _looks_like_chat_id(target):
            send_message(chat_id, f"That doesn't look like a chat_id: {target}")
            return True
        auth_data["authorized"][target] = {"authorized_at": time.time()}
        auth_data["notified_admin"] = [c for c in auth_data["notified_admin"] if c != target]
        send_message(chat_id, f"✅ Authorized {target}.")
        send_message(target, "🎉 You're all set! Try /current or /search <amount> whenever you like.")
        return True

    if command == "/revoke" and len(parts) == 2:
        target = parts[1].strip()
        if not _looks_like_chat_id(target):
            send_message(chat_id, f"That doesn't look like a chat_id: {target}")
            return True
        removed = auth_data["authorized"].pop(target, None)
        send_message(chat_id, f"{'✅ Revoked' if removed else '⚠️ Was not authorized:'} {target}")
        return True

    if command == "/users":
        ids = list(auth_data["authorized"].keys())
        send_message(chat_id, f"Authorized users ({len(ids)}): {', '.join(ids) if ids else 'none yet'}")
        return True

    if command == "/pending":
        ids = auth_data.get("notified_admin", [])
        send_message(chat_id, f"Pending inquiries ({len(ids)}): {', '.join(ids) if ids else 'none'}")
        return True

    return False


# ---------- routing ----------

def handle_message(state: dict, auth_data: dict, chat_id, text: str, display_name: str):
    text = (text or "").strip()
    chat_key = str(chat_id)
    _clean_expired_awaiting(state)

    if _is_admin(chat_key) and handle_admin_command(auth_data, chat_id, text):
        return

    if not _is_authorized(auth_data, chat_key):
        if chat_key not in auth_data["notified_admin"]:
            auth_data["notified_admin"].append(chat_key)
            notify_admin_new_inquiry(chat_key, display_name)
        send_message(chat_id, PAYWALL_TEXT)
        return

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
                return
            state["awaiting_amount"].pop(chat_key, None)
            reply_search(chat_id, amount)
        else:
            send_message(chat_id, "That doesn't look like a number. Try something like 50000.")
        return

    send_message(chat_id, "I didn't quite get that. Try /current or /search <amount>, "
                            "or /start to see everything I can do.")


# ---------- polling loop ----------

def poll_once(state: dict, auth_data: dict) -> bool:
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
    if updates:
        log.info("Received %d update(s).", len(updates))

    for update in updates:
        state["last_update_id"] = update["update_id"]
        message = update.get("message")
        if not message:
            continue
        chat_type = message.get("chat", {}).get("type")
        if chat_type != "private":
            log.info("Ignoring non-private message (chat type: %s).", chat_type)
            continue
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        from_user = message.get("from", {}) or {}
        display_name = from_user.get("username") or from_user.get("first_name") or "unknown"
        log.info("Message from chat_id %s (%s): %r", chat_id, display_name, text)
        if chat_id is not None:
            handle_message(state, auth_data, chat_id, text, display_name)

    return bool(updates)


def main():
    if not settings.DM_BOT_TOKEN:
        log.error("DM_BOT_TOKEN is not set — nothing to do.")
        return

    if settings.DM_REQUIRE_AUTHORIZATION and not settings.DM_ADMIN_CHAT_ID:
        log.warning("DM_REQUIRE_AUTHORIZATION is on but DM_ADMIN_CHAT_ID is empty — "
                    "nobody could ever be authorized, so the paywall is bypassed "
                    "(open access) until DM_ADMIN_CHAT_ID is set.")

    state = load_state()
    auth_data = load_authorized_users()
    deadline = time.monotonic() + settings.DM_POLL_WINDOW_SECONDS

    while time.monotonic() < deadline:
        had_updates = poll_once(state, auth_data)
        if had_updates:
            save_state(state)
            save_authorized_users(auth_data)

    save_state(state)
    save_authorized_users(auth_data)
    log.info("Polling window closed, exiting cleanly for next cron tick.")


if __name__ == "__main__":
    main()
