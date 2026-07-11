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


_FIAT_SYMBOLS = {"NGN": "₦", "JPY": "¥", "GBP": "£", "EUR": "€", "USD": "$"}  # CHF has no clean symbol, falls back to prefix


def _currency_symbol(fiat: str) -> str:
    return _FIAT_SYMBOLS.get(fiat, f"{fiat} ")


def _format_money(fiat: str, value: float) -> str:
    symbol = _currency_symbol(fiat)
    if fiat == "JPY":  # yen isn't used with decimal places in practice
        return f"{symbol}{value:,.0f}"
    return f"{symbol}{value:,.2f}"


def _command_list_text(is_admin: bool = False) -> str:
    admin_block = (
        "\n\nAdmin commands:\n"
        "• /authorize <chat ID> — grant access (or just /authorize, and I'll ask for the id)\n"
        "• /revoke <chat ID> — remove access (same — /revoke alone works too)\n"
        "• /pending — see who's inquired but isn't authorized yet\n"
        "• /users — see everyone currently authorized"
    ) if is_admin else ""
    fiat_list = ", ".join(settings.SUPPORTED_FIATS)
    return (
        "Here's what I can do:\n"
        "• /current currency — best rates right now (naira by default)\n"
        "• /search <amount> currency — best merchants for a specific "
        "amount (e.g. /search 8000, or /search 500 EUR)\n"
        "• /trend currency — see how the rate's moved over 24h/7d\n"
        "• /alert <BUY|SELL> <price> currency — get messaged the moment "
        "the rate crosses your target (e.g. /alert SELL 1650)\n"
        "• /alerts — see your active alerts\n"
        "• /unalert <number> — cancel one\n\n"
        f"I check: {fiat_list}."
        f"{admin_block}"
    )


def _build_welcome_text(name: str, is_admin: bool = False) -> str:
    greeting = f"👋 *Welcome back, {_sanitize(name)}!*" if name else "👋 *Welcome back!*"
    return (
        f"{greeting}\n\n"
        f"{_command_list_text(is_admin)}\n\n"
        "You can also just type /search and I'll ask you for the amount.\n\n"
        "⚠️ I only show rates — I never touch your money. Always confirm the "
        "live price before you trade."
    )


# Shown to anyone who isn't authorized yet — this IS the sales pitch,
# so /start, /current, /search all show the same thing until they've paid.
# >>> Replace the wallet address below with your real TRC20 USDT address <<<
def _build_paywall_text(name: str) -> str:
    greeting = f"👋 *Welcome, {_sanitize(name)}!*" if name else "👋 *Welcome!*"
    return (
        f"{greeting}\n\n"
        "I check Binance and Bybit P2P live and show you the best trusted "
        f"{settings.ASSET} rates — only from merchants with a strong track "
        "record. Try /current free, once, before you decide.\n\n"
        f"{_command_list_text(is_admin=False)}\n\n"
        "This is a paid tool: *$9.99/month*, paid in USDT (TRC20 network) to:\n"
        "`TAFHrQuCunTab2iK6vqfneKMLhJ3y4DmCD`\n\n"
        "Once you've sent it, message @Opps\\_io directly to confirm — you'll "
        "be activated within minutes."
    )


# ---------- state (operational — polling, cache, cooldowns) ----------

def _default_state() -> dict:
    return {
        "last_update_id": 0,
        "awaiting_amount": {},
        "awaiting_admin_action": {},  # {chat_id: "authorize"|"revoke"} - bare command awaiting an id
        "last_request_at": {},
        "rate_cache": None,
        "consecutive_poll_failures": 0,
        "last_admin_notify_failure": None,  # {"at": ts, "target": str} - set when a proactive send to DM_ADMIN_CHAT_ID fails
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
        "authorized": {},         # {chat_id: {"authorized_at": ts}}
        "notified_admin": [],     # chat_ids already flagged to admin, so we don't spam
        "inquiry_sources": {},    # {chat_id: source} — captured from /start <source> links
        "free_preview_used": [],  # chat_ids who've already used their one free /current
        "known_names": {},        # {chat_id: display_name} — captured from every message seen
        "alerts": {},             # {chat_id: [{"direction","fiat","threshold","created_at"}]}
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

# Set right before send_message returns False, so a caller that cares
# (currently only _send_to_admin) can capture *why* a send failed, not
# just that it did. Read it immediately after calling send_message —
# it gets overwritten by the next call.
_last_send_error: str | None = None


def send_message(chat_id, text: str) -> bool:
    global _last_send_error
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code == 403:
            log.info("Chat %s has blocked the bot or is unreachable — skipping.", chat_id)
            _last_send_error = "chat blocked the bot, or is otherwise unreachable (403)"
            return False
        if not resp.ok:
            try:
                description = resp.json().get("description", resp.text[:200])
            except Exception:
                description = resp.text[:200]
            log.error("Telegram rejected sendMessage to %s (%d): %s", chat_id, resp.status_code, description)
            _last_send_error = f"HTTP {resp.status_code}: {description}"
            return False
        _last_send_error = None
        return True
    except Exception as e:
        log.error("Failed to send DM to %s: %s", chat_id, e)
        _last_send_error = f"network/request error: {e}"
        return False


def _fetch_chat_name(chat_id) -> str | None:
    """One-off lookup via Telegram's getChat, used as a fallback when we
    don't already have a name on file for this chat_id — e.g., someone
    authorized via a chat_id typed directly, who never sent a message
    that would've been captured by handle_message. Returns None if the
    lookup fails (the bot needs at least some prior contact with the
    chat for getChat to work — a truly cold, never-messaged chat_id
    still won't resolve)."""
    try:
        resp = requests.get(f"{API_BASE}/getChat", params={"chat_id": chat_id}, timeout=15)
        if not resp.ok:
            return None
        result = resp.json().get("result", {})
        return result.get("username") or result.get("first_name")
    except Exception as e:
        log.warning("getChat lookup failed for %s: %s", chat_id, e)
        return None


def _send_to_admin(state: dict, text: str) -> bool:
    """Sends a message to the configured admin chat_id, recording any
    failure (with the actual reason Telegram gave) so it can be surfaced
    the next time the admin interacts with the bot directly — a reply,
    which works regardless of whether DM_ADMIN_CHAT_ID itself is fine."""
    if not settings.DM_ADMIN_CHAT_ID:
        return False
    success = send_message(settings.DM_ADMIN_CHAT_ID, text)
    if success:
        state["last_admin_notify_failure"] = None
    else:
        state["last_admin_notify_failure"] = {
            "at": time.time(),
            "target": settings.DM_ADMIN_CHAT_ID,
            "reason": _last_send_error or "unknown",
        }
    return success


def notify_admin(state: dict, message: str) -> None:
    """Operational alerts — bot health, failures."""
    _send_to_admin(state, f"⚠️ DM bot: {message}")


def notify_admin_new_inquiry(state: dict, chat_key: str, display_name: str, source: str | None = None) -> bool:
    """Sales alerts — someone wants in. Kept visually distinct from
    operational alerts so it doesn't get lost among error logs.
    Returns True only if the notification actually reached the admin —
    the caller uses this to decide whether to mark the inquiry as
    'already notified.' If this returns False, the caller should NOT
    mark it notified, so a transient failure gets retried on their next
    message instead of silently going untracked forever."""
    if not settings.DM_ADMIN_CHAT_ID:
        return False
    source_line = f"\nCame from: {_sanitize(source)}" if source else ""
    success = _send_to_admin(state,
        f"💰 New inquiry: {_sanitize(display_name)} (chat ID {chat_key}){source_line}\n"
        f"Once they've paid: /authorize {chat_key}")
    if success:
        log.info("Notified admin about new inquiry from %s", chat_key)
    else:
        log.warning("Failed to notify admin about new inquiry from %s", chat_key)
    return success


def _parse_start_source(text: str):
    """Extracts the tracking tag from links like t.me/YourBot?start=twitter,
    which arrive as the message text '/start twitter'."""
    parts = text.split(maxsplit=1)
    if parts and parts[0] == "/start" and len(parts) == 2:
        return parts[1].strip()
    return None


def _parse_amount(text: str):
    cleaned = text.replace(",", "").strip()
    for symbol in _FIAT_SYMBOLS.values():
        cleaned = cleaned.replace(symbol, "")
    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


def _parse_fiat(text: str):
    """Returns the fiat code if text is a recognized currency, else None."""
    candidate = text.strip().upper()
    return candidate if candidate in settings.SUPPORTED_FIATS else None


def _parse_amount_and_fiat(text: str):
    """Parses '50000' or '50000 JPY' (or 'JPY 50000') into (amount, fiat,
    error). fiat defaults to settings.FIAT when not specified. error is
    None on success, or a user-facing message on failure."""
    parts = text.strip().split()
    if not parts:
        return None, None, None
    fiat = settings.FIAT
    amount = None
    for part in parts:
        maybe_fiat = _parse_fiat(part)
        if maybe_fiat:
            fiat = maybe_fiat
            continue
        maybe_amount = _parse_amount(part)
        if maybe_amount is not None:
            amount = maybe_amount
    if amount is None:
        fiats = ", ".join(settings.SUPPORTED_FIATS)
        return None, None, (f"That doesn't look like a number. Try something like "
                             f"50000, or 500 EUR. I check: {fiats}.")
    return amount, fiat, None


def _parse_current_fiat(text: str):
    """Parses '/current' or '/current JPY' (or '/trend JPY') into
    (fiat, error) — generic enough to reuse for any command that takes
    just an optional trailing currency code."""
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return settings.FIAT, None
    fiat = _parse_fiat(parts[1])
    if fiat is None:
        fiats = ", ".join(settings.SUPPORTED_FIATS)
        return None, f"I don't check {parts[1].strip()} yet. I check: {fiats}."
    return fiat, None


def _parse_alert(text: str):
    """Parses '/alert SELL 1650' or '/alert 165 BUY JPY' (any token order)
    into (direction, threshold, fiat, error). fiat defaults to
    settings.FIAT. error is a user-facing message, or None on success."""
    parts = text.split(maxsplit=1)
    tokens = parts[1].split() if len(parts) == 2 else []

    direction = None
    threshold = None
    fiat = settings.FIAT
    for tok in tokens:
        upper = tok.upper()
        if upper in ("BUY", "SELL"):
            direction = upper
            continue
        maybe_fiat = _parse_fiat(tok)
        if maybe_fiat:
            fiat = maybe_fiat
            continue
        maybe_price = _parse_amount(tok)
        if maybe_price is not None:
            threshold = maybe_price

    if direction is None or threshold is None:
        fiats = ", ".join(settings.SUPPORTED_FIATS)
        return None, None, None, (
            "I need a direction and a price. Try: /alert SELL 1650, or "
            f"/alert BUY 165 JPY. I check: {fiats}."
        )
    return direction, threshold, fiat, None


# ---------- formatting ----------

def _format_offer_full(offer: dict) -> str:
    fiat = offer.get("fiat", settings.FIAT)
    lines = [
        f"{_format_money(fiat, offer['price'])} per {settings.ASSET}",
        f"Merchant: {_sanitize(offer['merchant_name'])} "
        f"({offer['completion_rate']*100:.1f}% success, {offer['order_count']} trades)",
        f"Trade size: {_format_money(fiat, offer['min_limit'])} – "
        f"{_format_money(fiat, offer['max_limit'])} | {offer['platform']}",
    ]
    methods = offer.get("payment_methods") or []
    if methods:
        lines.append(f"Pays via: {', '.join(_sanitize(m) for m in methods[:4])}")
    if offer.get("link"):
        lines.append(f"Trade here: {offer['link']}")
    return "\n".join(lines)


def _format_offer_compact(rank: int, offer: dict) -> str:
    fiat = offer.get("fiat", settings.FIAT)
    link_line = f"\n   {offer['link']}" if offer.get("link") else ""
    return (
        f"{rank}. {_format_money(fiat, offer['price'])} — {_sanitize(offer['merchant_name'])} "
        f"({offer['completion_rate']*100:.1f}%, {offer['order_count']} trades, "
        f"{offer['platform']}){link_line}"
    )


# ---------- rate caching ----------

def _get_snapshot_cached(state: dict, fiat: str) -> dict:
    cache = state.get("rate_cache")
    if not isinstance(cache, dict):
        cache = {}
    state["rate_cache"] = cache

    # An already-deployed bot's cache may still be the old flat shape
    # ({"fetched_at":..., "snapshot":...} with no fiat key at all). That
    # won't match any real fiat code, so cache.get(fiat) just misses
    # cleanly below instead of crashing — the old keys just sit unused.
    entry = cache.get(fiat)
    if isinstance(entry, dict) and (time.time() - entry.get("fetched_at", 0)) <= settings.DM_RATE_CACHE_TTL_SECONDS:
        return entry["snapshot"]

    snapshot = aggregator.get_market_snapshot(fiat=fiat)
    cache[fiat] = {"fetched_at": time.time(), "snapshot": snapshot}
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

def reply_current(state: dict, chat_id, fiat: str, is_preview: bool = False):
    snapshot = _get_snapshot_cached(state, fiat)
    if not snapshot["buy"] and not snapshot["sell"]:
        send_message(chat_id, f"I couldn't find trusted {fiat} rates right now — try again shortly.")
        return

    lines = [f"📊 *{settings.ASSET}/{fiat} — best rates right now*\n"]
    if snapshot["sell"]:
        lines.append("🟢 *Best price to SELL your USDT*")
        lines.append(_format_offer_full(snapshot["sell"]))
        lines.append("")
    if snapshot["buy"]:
        lines.append("🔵 *Best price to BUY USDT*")
        lines.append(_format_offer_full(snapshot["buy"]))
    lines.append(f"\n⏱ {now_wat()}")
    if is_preview:
        lines.append("\n✅ That's real, live data — your one free look. Subscribe "
                      "for unlimited /current and /search access anytime.")
    else:
        lines.append("\n⚠️ Rates can change fast — please confirm before you trade.")
    send_message(chat_id, "\n".join(lines))


def reply_search(chat_id, amount: float, fiat: str):
    sell_matches = aggregator.get_top_matches("SELL", amount, fiat=fiat, limit=settings.DM_SEARCH_RESULT_LIMIT)
    buy_matches = aggregator.get_top_matches("BUY", amount, fiat=fiat, limit=settings.DM_SEARCH_RESULT_LIMIT)

    if not sell_matches and not buy_matches:
        send_message(chat_id,
            f"I couldn't find any trusted merchants for {_format_money(fiat, amount)} right now. "
            "Try a different amount, or check back shortly.")
        return

    lines = [f"🔍 *Merchants for {_format_money(fiat, amount)}*\n"]
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


# ---------- rate history (for /trend) ----------

def _default_history() -> dict:
    return {
        "samples": {},          # {fiat: [{"ts": epoch, "sell": price|None, "buy": price|None}]}
        "last_sampled_at": {},  # {fiat: epoch} - gates sampling to once per interval, per fiat
    }


def load_rate_history() -> dict:
    if not os.path.exists(settings.DM_RATE_HISTORY_FILE):
        return _default_history()
    try:
        with open(settings.DM_RATE_HISTORY_FILE, "r") as f:
            data = json.load(f)
        for key, value in _default_history().items():
            data.setdefault(key, value)
        return data
    except (json.JSONDecodeError, OSError):
        log.warning("Rate history file unreadable, starting fresh.")
        return _default_history()


def save_rate_history(history: dict) -> None:
    dir_name = os.path.dirname(settings.DM_RATE_HISTORY_FILE) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    with os.fdopen(fd, "w") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp_path, settings.DM_RATE_HISTORY_FILE)


def _prune_old_samples(history: dict) -> None:
    cutoff = time.time() - settings.DM_TREND_RETENTION_SECONDS
    for fiat, samples in history["samples"].items():
        history["samples"][fiat] = [s for s in samples if s["ts"] >= cutoff]


def sample_rate_history(state: dict, history: dict) -> None:
    """Records one rate sample per supported fiat, at most once every
    DM_TREND_SAMPLE_INTERVAL_SECONDS, so /trend has real data to compare
    against. Reuses the same cache /current and check_alerts use, so
    this rarely costs an extra fetch on its own."""
    now = time.time()
    for fiat in settings.SUPPORTED_FIATS:
        last = history["last_sampled_at"].get(fiat, 0)
        if now - last < settings.DM_TREND_SAMPLE_INTERVAL_SECONDS:
            continue
        snapshot = _get_snapshot_cached(state, fiat)
        sell_price = snapshot["sell"]["price"] if snapshot["sell"] else None
        buy_price = snapshot["buy"]["price"] if snapshot["buy"] else None
        if sell_price is None and buy_price is None:
            continue  # nothing usable right now — try again next cycle
        history["samples"].setdefault(fiat, []).append({"ts": now, "sell": sell_price, "buy": buy_price})
        history["last_sampled_at"][fiat] = now

    _prune_old_samples(history)


def _find_reference_sample(samples: list, hours_ago: float):
    """Finds a sample from approximately `hours_ago` in the past. Returns
    None if nothing is within a reasonable tolerance of that target —
    better to say 'not enough data yet' than compare against a sample
    that's nowhere near the claimed period."""
    if not samples:
        return None
    target_ts = time.time() - (hours_ago * 3600)
    closest = min(samples, key=lambda s: abs(s["ts"] - target_ts))
    tolerance = max(hours_ago * 3600 * 0.25, 3600)  # within 25% of target age, or 1h, whichever is bigger
    if abs(closest["ts"] - target_ts) > tolerance:
        return None
    return closest


def _trend_line(label: str, current: float, samples: list, hours: float, fiat: str, key: str) -> str:
    ref = _find_reference_sample(samples, hours)
    ref_price = ref.get(key) if ref else None
    if ref_price is None:
        return f"{label}: not enough history yet"
    change = current - ref_price
    pct = (change / ref_price * 100) if ref_price else 0
    arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
    return f"{label}: {_format_money(fiat, ref_price)} ({arrow} {abs(pct):.1f}%)"


def reply_trend(chat_id, fiat: str, history: dict):
    samples = history.get("samples", {}).get(fiat, [])
    if not samples:
        send_message(chat_id, f"I don't have any {fiat} history yet — I just started tracking it. "
                                "Check back in a few hours as it builds up.")
        return

    latest = samples[-1]
    lines = [f"📈 *{settings.ASSET}/{fiat} trend*\n"]

    if latest.get("sell") is not None:
        lines.append("🟢 *SELL*")
        lines.append(f"Now: {_format_money(fiat, latest['sell'])}")
        lines.append(_trend_line("24h ago", latest["sell"], samples, 24, fiat, "sell"))
        lines.append(_trend_line("7d ago", latest["sell"], samples, 24 * 7, fiat, "sell"))
        lines.append("")

    if latest.get("buy") is not None:
        lines.append("🔵 *BUY*")
        lines.append(f"Now: {_format_money(fiat, latest['buy'])}")
        lines.append(_trend_line("24h ago", latest["buy"], samples, 24, fiat, "buy"))
        lines.append(_trend_line("7d ago", latest["buy"], samples, 24 * 7, fiat, "buy"))

    lines.append(f"\n⏱ {now_wat()}")
    send_message(chat_id, "\n".join(lines))


# ---------- rate alerts ----------

def check_alerts(state: dict, auth_data: dict) -> None:
    """Checks every active alert against live rates and fires + removes
    any that have crossed their threshold. One-shot: a triggered alert
    is removed rather than repeated, so it doesn't spam every cycle the
    rate stays past the target. Groups fetches by fiat via the same
    cache /current uses, so N alerts on the same currency cost one fetch,
    not N — and share data with anyone's recent /current call too."""
    all_alerts = auth_data.get("alerts", {})
    if not any(all_alerts.values()):
        return

    needed_fiats = {alert["fiat"] for user_alerts in all_alerts.values() for alert in user_alerts}
    snapshots = {fiat: _get_snapshot_cached(state, fiat) for fiat in needed_fiats}

    for chat_key, user_alerts in list(all_alerts.items()):
        still_active = []
        for alert in user_alerts:
            snapshot = snapshots.get(alert["fiat"], {})
            offer = snapshot.get("sell") if alert["direction"] == "SELL" else snapshot.get("buy")
            price = offer["price"] if offer else None

            if price is None:
                still_active.append(alert)  # couldn't check this cycle — try again next time
                continue

            crossed = (
                (alert["direction"] == "SELL" and price >= alert["threshold"]) or
                (alert["direction"] == "BUY" and price <= alert["threshold"])
            )
            if crossed:
                verb = "sell" if alert["direction"] == "SELL" else "buy"
                send_message(chat_key, (
                    f"🔔 *Alert triggered!*\n\n"
                    f"You can now {verb} {settings.ASSET} at "
                    f"{_format_money(alert['fiat'], price)} — your target was "
                    f"{_format_money(alert['fiat'], alert['threshold'])}.\n\n"
                    f"Use /current {alert['fiat']} to see the live merchant.\n\n"
                    "This alert is used up — set a new one with /alert if you want to keep watching."
                ))
            else:
                still_active.append(alert)
        all_alerts[chat_key] = still_active


# ---------- admin commands ----------

def _do_authorize(auth_data: dict, admin_chat_id, target: str) -> None:
    auth_data["authorized"][target] = {"authorized_at": time.time()}
    auth_data["notified_admin"] = [c for c in auth_data["notified_admin"] if c != target]
    if target not in auth_data["known_names"]:
        fetched = _fetch_chat_name(target)
        if fetched:
            auth_data["known_names"][target] = fetched
    send_message(admin_chat_id, f"✅ Authorized {target}.")
    send_message(target, f"🎉 *You're all set!*\n\n{_command_list_text(is_admin=False)}")


def _do_revoke(auth_data: dict, admin_chat_id, target: str) -> None:
    removed = auth_data["authorized"].pop(target, None)
    # Clear their "already notified" flag too, so if they message again
    # after being revoked, that counts as a fresh inquiry rather than
    # being silently skipped as something you've already seen.
    auth_data["notified_admin"] = [c for c in auth_data["notified_admin"] if c != target]
    send_message(admin_chat_id, f"{'✅ Revoked' if removed else '⚠️ Was not authorized:'} {target}")


def handle_admin_command(state: dict, auth_data: dict, chat_id, text: str) -> bool:
    """Returns True if this was an admin command and has been handled."""
    chat_key = str(chat_id)

    # If a proactive send to DM_ADMIN_CHAT_ID failed recently (e.g. new
    # inquiry alerts), surface it here — replying to whoever's messaging
    # right now always works, even if the configured admin ID doesn't.
    failure = state.get("last_admin_notify_failure")
    if failure and (time.time() - failure.get("at", 0)) < 86400:
        send_message(chat_id, (
            "⚠️ A proactive notification recently failed to send.\n"
            f"Target: `{failure.get('target')}`\n"
            f"Reason: {failure.get('reason', 'unknown')}"
        ))
        state["last_admin_notify_failure"] = None

    parts = text.split(maxsplit=1)
    command = parts[0]

    if command == "/authorize":
        if len(parts) == 2:
            target = parts[1].strip()
            if not _looks_like_chat_id(target):
                send_message(chat_id, f"That doesn't look like a chat_id: {target}")
                return True
            state["awaiting_admin_action"].pop(chat_key, None)
            _do_authorize(auth_data, chat_id, target)
        else:
            state["awaiting_amount"].pop(chat_key, None)  # clear any stale /search prompt
            state["awaiting_admin_action"][chat_key] = "authorize"
            send_message(chat_id, "Which chat_id do you want to authorize? Reply with the number.")
        return True

    if command == "/revoke":
        if len(parts) == 2:
            target = parts[1].strip()
            if not _looks_like_chat_id(target):
                send_message(chat_id, f"That doesn't look like a chat_id: {target}")
                return True
            state["awaiting_admin_action"].pop(chat_key, None)
            _do_revoke(auth_data, chat_id, target)
        else:
            state["awaiting_amount"].pop(chat_key, None)  # clear any stale /search prompt
            state["awaiting_admin_action"][chat_key] = "revoke"
            send_message(chat_id, "Which chat_id do you want to revoke? Reply with the number.")
        return True

    if command == "/users":
        ids = list(auth_data["authorized"].keys())
        if not ids:
            send_message(chat_id, "Authorized users (0): none yet")
        else:
            names = auth_data.setdefault("known_names", {})
            for cid in ids:
                if cid not in names:
                    fetched = _fetch_chat_name(cid)
                    if fetched:
                        names[cid] = fetched
            lines = [f"{cid} ({_sanitize(names.get(cid, 'unknown'))})" for cid in ids]
            send_message(chat_id, f"Authorized users ({len(ids)}):\n" + "\n".join(lines))
        return True

    if command == "/pending":
        ids = auth_data.get("notified_admin", [])
        if not ids:
            send_message(chat_id, "Pending inquiries (0): none")
        else:
            sources = auth_data.get("inquiry_sources", {})
            names = auth_data.get("known_names", {})
            lines = [
                f"{cid} ({_sanitize(names.get(cid, 'unknown'))}, "
                f"{_sanitize(sources.get(cid, 'unknown source'))})"
                for cid in ids
            ]
            send_message(chat_id, f"Pending inquiries ({len(ids)}):\n" + "\n".join(lines))
        return True

    # Not a recognized command — if we're waiting on a chat_id to complete
    # a bare /authorize or /revoke from a moment ago, treat this as the
    # answer. If they instead typed a different command, drop the pending
    # action and let it fall through to the normal handler.
    pending_action = state["awaiting_admin_action"].get(chat_key)
    if pending_action:
        if text.startswith("/"):
            state["awaiting_admin_action"].pop(chat_key, None)
            return False
        target = text.strip()
        if not _looks_like_chat_id(target):
            send_message(chat_id, f"That doesn't look like a chat_id: {target}")
            return True  # keep waiting — don't clear the pending action
        state["awaiting_admin_action"].pop(chat_key, None)
        if pending_action == "authorize":
            _do_authorize(auth_data, chat_id, target)
        else:
            _do_revoke(auth_data, chat_id, target)
        return True

    return False


# ---------- routing ----------

def handle_message(state: dict, auth_data: dict, history: dict, chat_id, text: str, display_name: str, greeting_name: str):
    text = (text or "").strip()
    chat_key = str(chat_id)
    _clean_expired_awaiting(state)

    # Keep a record of who each chat_id belongs to, so /users and /pending
    # can show a name instead of a bare number. Refreshed on every message
    # so it stays current if someone changes their Telegram name/username.
    auth_data["known_names"][chat_key] = display_name

    if _is_admin(chat_key) and handle_admin_command(state, auth_data, chat_id, text):
        return

    source = _parse_start_source(text)
    if source and chat_key not in auth_data["inquiry_sources"]:
        auth_data["inquiry_sources"][chat_key] = source

    if not _is_authorized(auth_data, chat_key):
        if chat_key not in auth_data["notified_admin"]:
            sent = notify_admin_new_inquiry(state, chat_key, display_name, auth_data["inquiry_sources"].get(chat_key))
            if sent:
                auth_data["notified_admin"].append(chat_key)

        # One real, live /current before the paywall — makes the pitch's
        # "try /current and see" claim literally true instead of a bait
        # and switch. Still cooldown-protected like any other fetch.
        if text.startswith("/current") and chat_key not in auth_data["free_preview_used"]:
            fiat, error = _parse_current_fiat(text)
            if error:
                send_message(chat_id, error)
                return
            if not _check_cooldown(state, chat_key):
                send_message(chat_id, "One moment — still working on your last request.")
                return
            auth_data["free_preview_used"].append(chat_key)
            reply_current(state, chat_id, fiat, is_preview=True)
            return

        send_message(chat_id, _build_paywall_text(greeting_name))
        return

    if text.startswith("/start") or text == "/help":
        state["awaiting_amount"].pop(chat_key, None)
        state["awaiting_admin_action"].pop(chat_key, None)
        send_message(chat_id, _build_welcome_text(greeting_name, _is_admin(chat_key)))
        return

    if text.startswith("/current"):
        state["awaiting_amount"].pop(chat_key, None)
        fiat, error = _parse_current_fiat(text)
        if error:
            send_message(chat_id, error)
            return
        if not _check_cooldown(state, chat_key):
            send_message(chat_id, "One moment — still working on your last request.")
            return
        reply_current(state, chat_id, fiat)
        return

    if text.startswith("/trend"):
        state["awaiting_amount"].pop(chat_key, None)
        fiat, error = _parse_current_fiat(text)
        if error:
            send_message(chat_id, error)
            return
        # No cooldown here — /trend only reads already-stored history,
        # it never triggers a live Binance/Bybit fetch on its own.
        reply_trend(chat_id, fiat, history)
        return

    if text.startswith("/search"):
        parts = text.split(maxsplit=1)
        amount, fiat, error = _parse_amount_and_fiat(parts[1]) if len(parts) == 2 else (None, None, None)
        if error:
            send_message(chat_id, error)
            return
        if amount:
            if not _check_cooldown(state, chat_key):
                send_message(chat_id, "One moment — still working on your last request.")
                return
            state["awaiting_amount"].pop(chat_key, None)
            reply_search(chat_id, amount, fiat)
        else:
            fiats = ", ".join(settings.SUPPORTED_FIATS)
            state["awaiting_amount"][chat_key] = time.time()
            send_message(chat_id, "How much do you want to trade, and in which currency? "
                                    f"Reply like '50000' (naira by default) or '500 EUR'. I check: {fiats}.")
        return

    if text == "/alerts":
        user_alerts = auth_data["alerts"].get(chat_key, [])
        if not user_alerts:
            send_message(chat_id, "No active alerts. Set one with /alert SELL 1650 "
                                    "(or /alert BUY 165 JPY).")
        else:
            lines = [f"{i}. {a['direction']} {_format_money(a['fiat'], a['threshold'])}"
                     for i, a in enumerate(user_alerts, 1)]
            send_message(chat_id, "🔔 *Your active alerts:*\n" + "\n".join(lines) +
                                    "\n\nCancel one with /unalert <number>, or /unalert all.")
        return

    if text.startswith("/alert"):
        direction, threshold, fiat, error = _parse_alert(text)
        if error:
            send_message(chat_id, error)
            return
        user_alerts = auth_data["alerts"].setdefault(chat_key, [])
        if len(user_alerts) >= settings.DM_MAX_ALERTS_PER_USER:
            send_message(chat_id, f"You've hit the limit of {settings.DM_MAX_ALERTS_PER_USER} "
                                    "active alerts. Cancel one with /unalert <number> first.")
            return
        user_alerts.append({
            "direction": direction, "fiat": fiat, "threshold": threshold, "created_at": time.time(),
        })
        verb = "at or above" if direction == "SELL" else "at or below"
        send_message(chat_id, f"🔔 Alert set: I'll message you the moment you can "
                                f"{direction.lower()} {settings.ASSET} {verb} "
                                f"{_format_money(fiat, threshold)}.")
        return

    if text.startswith("/unalert"):
        user_alerts = auth_data["alerts"].get(chat_key, [])
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            send_message(chat_id, "Which one? Try /unalert 1 (see /alerts for numbers), "
                                    "or /unalert all.")
            return
        arg = parts[1].strip().lower()
        if arg == "all":
            auth_data["alerts"][chat_key] = []
            send_message(chat_id, "All your alerts are cancelled.")
            return
        try:
            idx = int(arg) - 1
            if idx < 0:
                raise ValueError
            removed = user_alerts.pop(idx)
            send_message(chat_id, f"Cancelled: {removed['direction']} "
                                    f"{_format_money(removed['fiat'], removed['threshold'])}")
        except (ValueError, IndexError):
            send_message(chat_id, f"I don't see alert #{arg}. Check /alerts for the current list.")
        return

    if chat_key in state["awaiting_amount"]:
        amount, fiat, error = _parse_amount_and_fiat(text)
        if amount is None:
            send_message(chat_id, error or "That doesn't look like a number. Try something like 50000.")
            return  # keep waiting — don't clear the pending prompt
        if not _check_cooldown(state, chat_key):
            send_message(chat_id, "One moment — still working on your last request.")
            return
        state["awaiting_amount"].pop(chat_key, None)
        reply_search(chat_id, amount, fiat)
        return

    send_message(chat_id, "I didn't quite get that. Try /current or /search <amount>, "
                            "or /start to see everything I can do.")


# ---------- polling loop ----------

def poll_once(state: dict, auth_data: dict, history: dict) -> bool:
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
            notify_admin(state, "getUpdates has failed 3 times in a row — "
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
        greeting_name = from_user.get("first_name") or from_user.get("username") or ""
        log.info("Message from chat_id %s (%s): %r", chat_id, display_name, text)
        if chat_id is not None:
            handle_message(state, auth_data, history, chat_id, text, display_name, greeting_name)

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
    history = load_rate_history()
    deadline = time.monotonic() + settings.DM_POLL_WINDOW_SECONDS
    last_alert_check = 0.0

    while time.monotonic() < deadline:
        had_updates = poll_once(state, auth_data, history)

        if time.monotonic() - last_alert_check >= settings.DM_ALERT_CHECK_INTERVAL_SECONDS:
            check_alerts(state, auth_data)
            sample_rate_history(state, history)  # internally gated to once/hour per fiat
            last_alert_check = time.monotonic()
            save_authorized_users(auth_data)  # alerts may have triggered/been removed
            save_state(state)  # rate_cache may have been refreshed
            save_rate_history(history)

        if had_updates:
            save_state(state)
            save_authorized_users(auth_data)

    save_state(state)
    save_authorized_users(auth_data)
    save_rate_history(history)
    log.info("Polling window closed, exiting cleanly for next cron tick.")


if __name__ == "__main__":
    main()
