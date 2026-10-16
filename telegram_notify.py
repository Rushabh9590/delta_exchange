"""
Telegram Notification Module for Delta Exchange Market Making Automation.
Sends real-time alerts when qualified strikes remain continuously qualified
for a user-configurable duration (default: 100 seconds).
"""

import os
import sys
import time
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv, set_key

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

logger = logging.getLogger("telegram_notify")

# In-memory and cross-process alert state tracking to prevent alert spamming:
_ALERTED_STRIKES: set = set()
_LOCK = threading.Lock()
_SHARED_ALERT_FILE = BASE_DIR / ".alert_history.json"


def _is_recently_alerted(symbol: str, cooldown_sec: int = 120) -> bool:
    """Checks if this symbol was already alerted in the last cooldown_sec seconds across any process."""
    try:
        if _SHARED_ALERT_FILE.exists():
            with open(_SHARED_ALERT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                ts = data.get(symbol, 0)
                if time.time() - ts < cooldown_sec:
                    return True
    except Exception:
        pass
    return False


def _record_alerted_symbol(symbol: str):
    """Records alert timestamp in shared JSON file to synchronize all background workers."""
    try:
        data = {}
        if _SHARED_ALERT_FILE.exists():
            try:
                with open(_SHARED_ALERT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        now = time.time()
        data[symbol] = now
        # Retain only entries from the last 2 hours
        data = {k: v for k, v in data.items() if now - v < 7200}
        with open(_SHARED_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


# Cached configuration dictionary
_CONFIG = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    "notify_secs": int(os.getenv("MM_QUAL_NOTIFY_SECS", "100")),
    "enabled": os.getenv("TELEGRAM_NOTIFY_ENABLED", "true").strip().lower() in ("true", "1", "yes")
}


RECENT_ALERTS: list[dict] = []


def get_telegram_config() -> dict:
    """
    Returns the current Telegram notification configuration.
    Dynamically syncs from .env so background processes and server always have live credentials.
    """
    global _CONFIG
    try:
        if ENV_PATH.exists():
            load_dotenv(dotenv_path=ENV_PATH, override=True)
    except Exception:
        pass

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip("'\"")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip("'\"")
    try:
        notify_secs = int(str(os.getenv("MM_QUAL_NOTIFY_SECS", "100")).strip().strip("'\""))
    except (ValueError, TypeError):
        notify_secs = 100

    raw_en = str(os.getenv("TELEGRAM_NOTIFY_ENABLED", "true")).strip().strip("'\"").lower()
    enabled = raw_en in ("true", "1", "yes")

    _CONFIG["bot_token"] = token
    _CONFIG["chat_id"] = chat_id
    _CONFIG["notify_secs"] = notify_secs
    _CONFIG["enabled"] = enabled

    masked = ""
    if token:
        masked = f"{token[:5]}...{token[-4:]}" if len(token) > 10 else "***"

    return {
        "bot_token": token,
        "chat_id": chat_id,
        "bot_token_masked": masked,
        "notify_secs": notify_secs,
        "enabled": enabled,
        "is_configured": bool(token and chat_id)
    }


def get_recent_alerts(limit: int = 5) -> list[dict]:
    """Returns list of recently dispatched alerts."""
    with _LOCK:
        return list(RECENT_ALERTS[-limit:])


def save_telegram_config(
    bot_token: str = None,
    chat_id: str = None,
    notify_secs: int = None,
    enabled: bool = None
) -> dict:
    """
    Updates the Telegram notification configuration in memory and persists to .env.
    """
    global _CONFIG
    cfg = get_telegram_config()

    if bot_token is not None:
        cfg["bot_token"] = str(bot_token).strip()
    if chat_id is not None:
        cfg["chat_id"] = str(chat_id).strip()
    if notify_secs is not None:
        try:
            cfg["notify_secs"] = max(5, int(notify_secs))
        except (ValueError, TypeError):
            pass
    if enabled is not None:
        cfg["enabled"] = bool(enabled)

    _CONFIG = cfg

    # Persist to .env file
    try:
        if ENV_PATH.exists():
            if bot_token is not None:
                set_key(str(ENV_PATH), "TELEGRAM_BOT_TOKEN", cfg["bot_token"])
            if chat_id is not None:
                set_key(str(ENV_PATH), "TELEGRAM_CHAT_ID", cfg["chat_id"])
            if notify_secs is not None:
                set_key(str(ENV_PATH), "MM_QUAL_NOTIFY_SECS", str(cfg["notify_secs"]))
            if enabled is not None:
                set_key(str(ENV_PATH), "TELEGRAM_NOTIFY_ENABLED", "true" if cfg["enabled"] else "false")
            load_dotenv(dotenv_path=ENV_PATH, override=True)
    except Exception as e:
        logger.warning(f"Failed to persist Telegram config to .env: {e}")

    return get_telegram_config()


def send_telegram_message(text: str, bot_token: str = None, chat_id: str = None) -> dict:
    """
    Sends an HTML-formatted message to the specified Telegram chat.
    """
    cfg = get_telegram_config()
    token = (bot_token or cfg["bot_token"]).strip()
    cid = (chat_id or cfg["chat_id"]).strip()

    if not token or not cid:
        return {
            "success": False,
            "error": "Telegram Bot Token and Chat ID must be configured."
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        resp = requests.post(url, json=payload, timeout=8)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return {"success": True, "data": data}
        else:
            err = data.get("description", f"HTTP {resp.status_code}")
            logger.error(f"Telegram API error: {err}")
            return {"success": False, "error": err}
    except Exception as e:
        logger.error(f"Failed to connect to Telegram: {e}")
        return {"success": False, "error": str(e)}


def send_test_alert(bot_token: str = None, chat_id: str = None) -> dict:
    """
    Sends an instant test message to Telegram to verify setup.
    """
    cfg = get_telegram_config()
    token = bot_token or cfg["bot_token"]
    cid = chat_id or cfg["chat_id"]
    notify_s = cfg["notify_secs"]
    now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    msg = (
        f"✅ <b>Delta Exchange Automation: Telegram Connected!</b>\n\n"
        f"Your Telegram alerts are properly configured.\n"
        f"Alert Threshold: <b>{notify_s}s continuous qualification</b>.\n"
        f"Underlyings: <b>BTC & ETH Option Chain</b>\n\n"
        f"🕒 <i>Test sent at {now_str} IST</i>"
    )

    return send_telegram_message(msg, bot_token=token, chat_id=cid)


def format_qual_duration_label(seconds: float | int) -> str:
    """Formats seconds into readable format, e.g. '100s (1m 40s)'"""
    total_sec = max(0, int(seconds))
    if total_sec < 60:
        return f"{total_sec}s"
    m, s = divmod(total_sec, 60)
    if m < 60:
        return f"{m}m {s:02d}s ({total_sec}s)"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s ({total_sec}s)"


def check_strike_qual_alert(strike: dict, qual_duration_sec: float | None):
    """
    Checks if a qualified strike has reached the notification threshold duration.
    Dispatches Telegram message in a background thread if criteria met.
    """
    if qual_duration_sec is None or qual_duration_sec < 0:
        return

    cfg = get_telegram_config()
    if not cfg["enabled"] or not cfg["is_configured"]:
        return

    threshold = cfg["notify_secs"]
    if qual_duration_sec < threshold:
        return

    symbol = strike.get("symbol")
    if not symbol:
        return

    # One-shot alert per qualification cycle (prevent alert spamming across processes)
    with _LOCK:
        if symbol in _ALERTED_STRIKES or _is_recently_alerted(symbol):
            return
        _ALERTED_STRIKES.add(symbol)
        _record_alerted_symbol(symbol)

    # Format message
    asset = strike.get("underlying") or ("BTC" if "BTC" in symbol else "ETH")
    expiry = strike.get("expiry", "--")
    strike_price = strike.get("strike", 0)
    opt_type = strike.get("option_type", "CALL")
    type_badge = strike.get("type_badge", "CE")
    bid = strike.get("bid", 0.0)
    ask = strike.get("ask", 0.0)
    spread = strike.get("spread", 0.0)
    spread_ticks = strike.get("spread_ticks", 0.0)
    net_edge = strike.get("net_edge", 0.0)
    net_edge_pct = strike.get("net_edge_pct", 0.0)
    now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    dur_str = format_qual_duration_label(qual_duration_sec)

    msg = (
        f"🚨 <b>DELTA MM STRIKE QUALIFIED ALERT</b> 🚨\n\n"
        f"<b>Asset:</b> {asset} | <b>Expiry:</b> {expiry}\n"
        f"<b>Symbol:</b> <code>{symbol}</code>\n"
        f"<b>Type:</b> {opt_type} ({type_badge}) | <b>Strike:</b> ${strike_price:,.0f}\n\n"
        f"⏱️ <b>Continuous Qual Time:</b> <b>{dur_str}</b> (Threshold: {threshold}s)\n"
        f"💵 <b>Best Bid:</b> ${bid:.2f} | <b>Best Ask:</b> ${ask:.2f}\n"
        f"📏 <b>Spread:</b> ${spread:.2f} ({spread_ticks:.1f} Ticks)\n"
        f"📈 <b>Net Edge:</b> +${net_edge:.2f} (<b>{net_edge_pct:.1f}%</b>)\n\n"
        f"🕒 <i>Detected at: {now_str} IST</i>\n"
        f"🤖 <i>Delta Market Maker Automation</i>"
    )

    def _async_send():
        res = send_telegram_message(msg)
        if res.get("success"):
            print(f"\n[TELEGRAM ALERT SENT] 🚀 {symbol} qualified for {int(qual_duration_sec)}s (>= {threshold}s) -> Message delivered to Telegram!\n", flush=True)
            with _LOCK:
                RECENT_ALERTS.append({
                    "symbol": symbol,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "duration_str": dur_str,
                    "edge_pct": net_edge_pct,
                    "ticks": spread_ticks
                })
        else:
            print(f"\n[TELEGRAM ALERT FAILED] {symbol}: {res.get('error')}\n", flush=True)
            with _LOCK:
                _ALERTED_STRIKES.discard(symbol)

    threading.Thread(target=_async_send, daemon=True, name=f"TG-Alert-{symbol}").start()


def reset_strike_alert(symbol: str):
    """
    Clears the strike from alerted set when it becomes non-qualified.
    Allows re-alerting if the strike re-qualifies in the future and stays for >= threshold.
    """
    if not symbol:
        return
    with _LOCK:
        _ALERTED_STRIKES.discard(symbol)
