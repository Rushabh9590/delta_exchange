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
from dotenv import load_dotenv, set_key, dotenv_values

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

# Single-Sender process coordination:
# When both server.py and mm_strikes.py are running, strictly 1 process sends Telegram alerts.
_PROCESS_ROLE: str | None = None
_TG_SENDER_LOCK_FILE = BASE_DIR / ".tg_sender.lock"
_LOCK = threading.Lock()


def set_process_role(role: str):
    """Sets role for current process ('mm_strikes' or 'server')."""
    global _PROCESS_ROLE
    _PROCESS_ROLE = str(role).strip().lower()


def get_process_role() -> str:
    """Returns the current process role, auto-detecting from sys.argv if not set."""
    global _PROCESS_ROLE
    if _PROCESS_ROLE:
        return _PROCESS_ROLE
    script_name = Path(sys.argv[0]).name.lower() if sys.argv and sys.argv[0] else ""
    if "server" in script_name:
        _PROCESS_ROLE = "server"
    else:
        _PROCESS_ROLE = "mm_strikes"
    return _PROCESS_ROLE


def update_sender_heartbeat(role: str | None = None):
    """Updates the heartbeat in the sender lock file to signal this process is alive and active."""
    current_role = role or get_process_role()
    try:
        data = {
            "role": current_role,
            "pid": os.getpid(),
            "timestamp": time.time(),
            "script": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
        }
        with open(_TG_SENDER_LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def is_active_telegram_sender(role: str | None = None) -> bool:
    """
    Determines if the current process is authorized to send Telegram alerts.
    Rule:
    - If mm_strikes.py is running (heartbeat < 6.0s old), ONLY mm_strikes sends.
    - If mm_strikes.py is NOT running or stopped, server.py takes over and sends.
    - If only mm_strikes.py is running, mm_strikes sends.
    Guarantees strictly 1 process sends messages when both .py files are running.
    """
    current_role = role or get_process_role()
    now = time.time()

    if current_role == "mm_strikes":
        update_sender_heartbeat("mm_strikes")
        return True

    # Current process is 'server':
    try:
        if _TG_SENDER_LOCK_FILE.exists():
            with open(_TG_SENDER_LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            owner = data.get("role")
            hb = float(data.get("timestamp", 0) or 0)
            # If mm_strikes is active (heartbeat updated within 6.0s), server yields!
            if owner == "mm_strikes" and (now - hb) < 6.0:
                return False
    except Exception:
        pass

    # mm_strikes is inactive or stopped: server takes over
    update_sender_heartbeat("server")
    return True


# In-memory and cross-process alert state tracking:
# A strike alerts strictly ONCE per continuous qualification cycle.
# It is NEVER alerted again while it remains continuously qualified.
# It resets ONLY when it actually loses qualification (reset_strike_alert).
_ALERTED_STRIKES: set = set()
_SHARED_ALERT_FILE = BASE_DIR / ".alert_history.json"


def _load_alert_history() -> dict:
    try:
        if _SHARED_ALERT_FILE.exists():
            with open(_SHARED_ALERT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_alert_history(data: dict):
    try:
        with open(_SHARED_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def is_strike_alerted(symbol: str) -> bool:
    """
    Checks if this strike has already been alerted during its current continuous qualification cycle.
    A strike that is already alerted must NEVER send duplicate alerts as long as it stays qualified.
    """
    if not symbol:
        return True
    with _LOCK:
        if symbol in _ALERTED_STRIKES:
            return True
        data = _load_alert_history()
        if symbol in data:
            _ALERTED_STRIKES.add(symbol)
            return True
    return False


def record_strike_alerted(symbol: str, qual_duration_sec: float):
    """
    Records that this strike has been alerted for its continuous qualification cycle.
    Persists across all processes so neither server nor mm_strikes sends duplicate alerts.
    """
    if not symbol:
        return
    with _LOCK:
        _ALERTED_STRIKES.add(symbol)
        data = _load_alert_history()
        now = time.time()
        data[symbol] = {
            "alerted_at": now,
            "qual_duration_sec": qual_duration_sec,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        _save_alert_history(data)


def reset_strike_alert(symbol: str):
    """
    Clears the strike from alerted state ONLY when it actually disqualifies (loses qualification).
    This allows a new alert ONLY if the strike re-qualifies in the future and stays >= threshold.
    """
    if not symbol:
        return
    with _LOCK:
        _ALERTED_STRIKES.discard(symbol)
        data = _load_alert_history()
        if symbol in data:
            data.pop(symbol, None)
            _save_alert_history(data)


# Cached configuration dictionary and CLI override
_NOTIFY_SECS_OVERRIDE = None
_CONFIG = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    "notify_secs": int(os.getenv("MM_QUAL_NOTIFY_SECS", "100")),
    "enabled": os.getenv("TELEGRAM_NOTIFY_ENABLED", "true").strip().lower() in ("true", "1", "yes")
}


def set_notify_secs_override(secs: int | None):
    """Overrides the notification threshold in memory for the current process session."""
    global _NOTIFY_SECS_OVERRIDE
    _NOTIFY_SECS_OVERRIDE = max(5, int(secs)) if secs is not None else None


RECENT_ALERTS: list[dict] = []


def get_telegram_config() -> dict:
    """
    Returns the current Telegram notification configuration.
    Dynamically syncs directly from .env on every check so background processes
    and server immediately pick up changes made in .env or the frontend modal.
    """
    global _CONFIG
    env_vals = {}
    try:
        if ENV_PATH.exists():
            env_vals = dotenv_values(str(ENV_PATH))
            load_dotenv(dotenv_path=ENV_PATH, override=True)
    except Exception:
        pass

    token = str(env_vals.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip().strip("'\"")
    chat_id = str(env_vals.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")).strip().strip("'\"")

    raw_secs = env_vals.get("MM_QUAL_NOTIFY_SECS") or os.getenv("MM_QUAL_NOTIFY_SECS", "100")
    try:
        notify_secs = max(5, int(str(raw_secs).strip().strip("'\"")))
    except (ValueError, TypeError):
        notify_secs = 100

    if _NOTIFY_SECS_OVERRIDE is not None:
        notify_secs = _NOTIFY_SECS_OVERRIDE

    raw_en = str(env_vals.get("TELEGRAM_NOTIFY_ENABLED") or os.getenv("TELEGRAM_NOTIFY_ENABLED", "true")).strip().strip("'\"").lower()
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
    Guarantees:
    1. Single-Sender Check: only 1 authorized process (mm_strikes priority, server backup) sends.
    2. One-Shot Alert: each strike alerts strictly once per continuous qualification cycle.
    3. Respects dynamic threshold from .env or frontend modal.
    """
    if qual_duration_sec is None or qual_duration_sec < 0:
        return

    # Check single-sender leadership: only 1 process sends messages when both are running
    if not is_active_telegram_sender():
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

    # One-shot alert per continuous qualification cycle:
    # If already alerted during this qualification session, NEVER send duplicate alerts!
    if is_strike_alerted(symbol):
        return

    # Mark as alerted immediately (in-memory + persistent shared file)
    record_strike_alerted(symbol, qual_duration_sec)

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
            # Revert alert state so retry can happen on failure
            reset_strike_alert(symbol)

    threading.Thread(target=_async_send, daemon=True, name=f"TG-Alert-{symbol}").start()
