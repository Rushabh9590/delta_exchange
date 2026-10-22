"""
Market Making Strike Selection Criteria Engine for Delta Exchange.
Based on the Market Making Strike Selection framework:
- Spread = Ask Price - Bid Price
- Mid Price = (Bid + Ask) / 2
- Net Edge = Spread - (2 * Tick Size)  [accounts for 2 ticks slippage/transaction cost]
- Net Edge % = (Net Edge / Mid) * 100
- Spread Ticks = Spread / Tick Size

Qualification / Strike Filtering Logic:
1. Bid Price >= Minimum Bid Price (e.g. 0.3)
2. Spread Ticks >= Minimum Spread Ticks (e.g. 5)
3. Net Edge % >= Minimum Net Edge Percentage (e.g. 50%)

Can be executed standalone via CLI:
    python mm_strikes.py
    python mm_strikes.py --underlying BTC --min-bid 0.3 --min-ticks 5 --min-edge 50
    python mm_strikes.py --watch --interval 5
"""

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        os.system("")  # Enables native Windows VT100 ANSI sequences
    except Exception:
        pass

# Ensure local python-rest-client package directory is accessible in sys.path
BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from login import get_delta_client, load_credentials
from multileg import (
    get_option_chain_data,
    get_options_expiries_and_strikes,
    is_expiry_expired,
    sync_master_scrips
)
from websocket import DeltaWebSocketManager
from telegram_notify import (
    check_strike_qual_alert,
    reset_strike_alert,
    get_telegram_config,
    get_recent_alerts,
    set_notify_secs_override,
    set_process_role,
    update_sender_heartbeat,
    is_active_telegram_sender
)
import threading

# Load environment configuration
load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

# ANSI terminal colors for CLI visualization
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_MAGENTA = "\033[95m"
COLOR_DIM = "\033[90m"
COLOR_WHITE = "\033[97m"
COLOR_BG_GREEN = "\033[42m\033[30m"
COLOR_BG_RED = "\033[41m\033[97m"
COLOR_BG_DARK = "\033[100m"

# Real-time continuous qualification duration tracker: symbol -> qualification start timestamp
_QUALIFIED_SINCE_MAP: dict[str, float] = {}

# Active Margin Mode Cache
_MM_MARGIN_MODE_CACHE = {"mode": "PORTFOLIO", "ts": 0}

def get_active_margin_mode() -> str:
    """
    Fetches the active account's Delta Exchange margin mode (PORTFOLIO, ISOLATED).
    Uses a 45-second cache to avoid hitting REST on every 0.8s tick.
    """
    now = time.time()
    if now - _MM_MARGIN_MODE_CACHE["ts"] < 45:
        return _MM_MARGIN_MODE_CACHE["mode"]
    try:
        client = get_delta_client()
        prof = client.request("GET", "/v2/profile", auth=True)
        if prof and prof.status_code == 200:
            m = prof.json().get("result", {}).get("margin_mode", "portfolio")
            _MM_MARGIN_MODE_CACHE["mode"] = str(m or "PORTFOLIO").upper()
            _MM_MARGIN_MODE_CACHE["ts"] = now
    except Exception:
        pass
    return _MM_MARGIN_MODE_CACHE["mode"]


def format_qual_duration(seconds: float | int | None) -> str:
    """
    Formats continuous qualification duration into Style 2A:
      - Under 60s: '45s'
      - 1m to 59m: '2m 14s'
      - >= 1 hour: '1h 02m 15s'
      - Not qualified / None: '--'
    """
    if seconds is None or seconds < 0:
        return "--"
    total_sec = int(seconds)
    if total_sec < 60:
        return f"{total_sec}s"
    minutes, secs = divmod(total_sec, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m {secs:02d}s"


_MM_SCANNER_RUNNING = False
_MM_SCANNER_LOCK = threading.Lock()


def start_mm_background_scanner(interval_seconds: float = 4.0):
    """
    Launches an automated background daemon scanner thread that periodically
    evaluates BTC and ETH option chains for qualified strikes and dispatches
    Telegram alerts 24/7 without requiring the browser or any web tab to be open.
    Yields if mm_strikes.py is running to guarantee strictly 1 active sender.
    """
    global _MM_SCANNER_RUNNING
    with _MM_SCANNER_LOCK:
        if _MM_SCANNER_RUNNING:
            return
        _MM_SCANNER_RUNNING = True

    def _worker():
        print(f"{COLOR_GREEN}[MM TELEGRAM SCANNER DAEMON]{COLOR_RESET} Background scanner active (Interval: {interval_seconds}s). 24/7 Telegram alerts running without browser.", flush=True)
        time.sleep(2.0)
        while True:
            try:
                if is_active_telegram_sender():
                    cfg = get_telegram_config()
                    if cfg.get("enabled") and cfg.get("is_configured"):
                        for asset in ["BTC", "ETH"]:
                            try:
                                evaluate_mm_strikes(underlying=asset, check_alerts=True)
                            except Exception:
                                pass
            except Exception:
                pass
            time.sleep(interval_seconds)

    t = threading.Thread(target=_worker, daemon=True, name="MM-Telegram-Scanner-Daemon")
    t.start()


def get_mm_config() -> dict:
    """
    Loads default Market Making filter criteria from .env or fallback values.
    """
    try:
        min_bid = float(os.getenv("MM_MIN_BID_PRICE", "0.3"))
    except (ValueError, TypeError):
        min_bid = 0.3

    try:
        min_ticks = float(os.getenv("MM_MIN_SPREAD_TICKS", "5.0"))
    except (ValueError, TypeError):
        min_ticks = 5.0

    try:
        min_net_edge_pct = float(os.getenv("MM_MIN_NET_EDGE_PCT", "50.0"))
    except (ValueError, TypeError):
        min_net_edge_pct = 50.0

    underlying = os.getenv("MM_DEFAULT_UNDERLYING", "ALL").strip().upper()

    try:
        lot_size_btc = max(1, int(os.getenv("MM_LOT_SIZE_BTC", "1")))
    except (ValueError, TypeError):
        lot_size_btc = 1

    try:
        lot_size_eth = max(1, int(os.getenv("MM_LOT_SIZE_ETH", "1")))
    except (ValueError, TypeError):
        lot_size_eth = 1

    try:
        refresh_sec = float(os.getenv("MM_REFRESH_INTERVAL", "0.25"))
    except (ValueError, TypeError):
        refresh_sec = 0.25

    return {
        "min_bid_price": min_bid,
        "min_spread_ticks": min_ticks,
        "min_net_edge_pct": min_net_edge_pct,
        "underlying": underlying,
        "lot_size_btc": lot_size_btc,
        "lot_size_eth": lot_size_eth,
        "refresh_interval": refresh_sec
    }


def get_auto_trade_lot_size(asset: str) -> int:
    """Returns the configured auto-trade lot size for the given underlying asset (BTC or ETH)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)
    except Exception:
        pass
    cfg = get_mm_config()
    asset_upper = str(asset).upper()
    if "ETH" in asset_upper:
        return cfg.get("lot_size_eth", 1)
    return cfg.get("lot_size_btc", 1)


def calculate_mm_metrics(
    bid: float,
    ask: float,
    tick_size: float,
    min_bid_price: float = 0.3,
    min_spread_ticks: float = 5.0,
    min_net_edge_pct: float = 50.0,
    oi_reduce_only_mode: bool = False
) -> dict:
    """
    Computes exact market making metrics for a strike/option quote per document specifications:
    1. Spread = Ask - Bid
    2. Mid = (Bid + Ask) / 2
    3. Net Edge = Spread - (2 * Tick Size)
    4. Net Edge % = (Net Edge / Mid) * 100 (0 if Mid <= 0)
    5. Spread Ticks = Spread / Tick Size
    6. Qualification check:
       - Not in exchange reduce-only mode (Max OI breached)
       - Bid >= min_bid_price
       - Spread Ticks >= min_spread_ticks
       - Net Edge % >= min_net_edge_pct
    """
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return {
            "has_quotes": False,
            "bid": bid if bid is not None else 0.0,
            "ask": ask if ask is not None else 0.0,
            "spread": 0.0,
            "mid_price": 0.0,
            "tick_size": tick_size or 0.1,
            "net_edge": 0.0,
            "net_edge_pct": 0.0,
            "spread_ticks": 0.0,
            "qualified": False,
            "reasons_failed": ["No valid bid/ask quotes"]
        }

    tick = float(tick_size) if (tick_size and float(tick_size) > 0) else 0.1
    spread = ask - bid
    mid = (bid + ask) / 2.0
    net_edge = spread - (2.0 * tick)
    net_edge_pct = (net_edge / mid * 100.0) if mid > 0 else 0.0
    spread_ticks = spread / tick if tick > 0 else 0.0

    reasons = []
    if oi_reduce_only_mode:
        reasons.append("Delta Exchange Max OI Breached (Reduce-Only Mode)")
    if bid < min_bid_price:
        reasons.append(f"Bid {bid:.4f} < {min_bid_price}")
    if spread_ticks < min_spread_ticks:
        reasons.append(f"Spread Ticks {spread_ticks:.1f} < {min_spread_ticks}")
    if net_edge_pct < min_net_edge_pct:
        reasons.append(f"Net Edge % {net_edge_pct:.1f}% < {min_net_edge_pct}%")

    qualified = (len(reasons) == 0)

    return {
        "has_quotes": True,
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread": round(spread, 4),
        "mid_price": round(mid, 4),
        "tick_size": tick,
        "net_edge": round(net_edge, 4),
        "net_edge_pct": round(net_edge_pct, 2),
        "spread_ticks": round(spread_ticks, 1),
        "qualified": qualified,
        "reasons_failed": reasons
    }


def evaluate_mm_strikes(
    underlying: str = "BTC",
    expiry: str = None,
    min_bid_price: float = None,
    min_spread_ticks: float = None,
    min_net_edge_pct: float = None,
    client = None,
    check_alerts: bool = True
) -> dict:
    """
    Scans live Delta Exchange option chain contracts, computes Market Making metrics,
    and returns categorized qualified strikes and overall statistics.
    """
    defaults = get_mm_config()
    min_bid = min_bid_price if min_bid_price is not None else defaults["min_bid_price"]
    min_ticks = min_spread_ticks if min_spread_ticks is not None else defaults["min_spread_ticks"]
    min_edge = min_net_edge_pct if min_net_edge_pct is not None else defaults["min_net_edge_pct"]
    default_asset = defaults["underlying"] if defaults["underlying"] in ["BTC", "ETH"] else "BTC"
    asset = (underlying or default_asset).strip().upper()

    if asset not in ["BTC", "ETH"]:
        return {
            "success": False,
            "error": f"Option contracts are only available for BTC and ETH on Delta Exchange. '{asset}' has no option contracts.",
            "underlying": asset,
            "selected_expiry": None,
            "spot_price": 0.0,
            "atm_strike": None,
            "criteria": {
                "min_bid_price": min_bid,
                "min_spread_ticks": min_ticks,
                "min_net_edge_pct": min_edge
            },
            "summary": {
                "total_scanned": 0,
                "total_qualified": 0,
                "max_spread_ticks": 0.0,
                "max_net_edge_pct": 0.0
            },
            "qualified_strikes": [],
            "all_strikes": [],
            "expiries": [],
            "timestamp": time.time()
        }

    c = client or get_delta_client(require_auth=False)
    chain_data = get_option_chain_data(underlying=asset, expiry=expiry, client=c)

    selected_expiry = chain_data.get("selected_expiry")
    spot_price = float(chain_data.get("spot_price") or 0.0)
    atm_strike = chain_data.get("atm_strike")
    expiries = chain_data.get("expiries", [])
    raw_rows = chain_data.get("chain", [])

    all_evaluated = []
    qualified_strikes = []

    for row in raw_rows:
        strike_val = row.get("strike")

        # Evaluate CALL
        call_contract = row.get("call")
        if call_contract:
            bid = call_contract.get("best_bid")
            ask = call_contract.get("best_ask")
            tick = float(call_contract.get("tick_size") or 0.1)

            call_oi_reduce = bool(call_contract.get("oi_reduce_only_mode", False))
            metrics = calculate_mm_metrics(
                bid=bid,
                ask=ask,
                tick_size=tick,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge,
                oi_reduce_only_mode=call_oi_reduce
            )

            call_sym = call_contract.get("symbol")
            now_eval = time.time()
            if metrics["qualified"]:
                if call_sym not in _QUALIFIED_SINCE_MAP:
                    _QUALIFIED_SINCE_MAP[call_sym] = now_eval
                call_dur = now_eval - _QUALIFIED_SINCE_MAP[call_sym]
                call_time_str = format_qual_duration(call_dur)
            else:
                _QUALIFIED_SINCE_MAP.pop(call_sym, None)
                call_dur = None
                call_time_str = "--"

            item = {
                "product_id": call_contract.get("product_id"),
                "symbol": call_sym,
                "underlying": asset,
                "option_type": "CALL",
                "type_badge": "CE",
                "strike": strike_val,
                "expiry": selected_expiry,
                "tick_size": metrics["tick_size"],
                "ltp": call_contract.get("ltp"),
                "mark_price": call_contract.get("mark_price"),
                "bid": metrics["bid"],
                "ask": metrics["ask"],
                "mid_price": metrics["mid_price"],
                "spread": metrics["spread"],
                "spread_ticks": metrics["spread_ticks"],
                "net_edge": metrics["net_edge"],
                "net_edge_pct": metrics["net_edge_pct"],
                "volume": call_contract.get("volume", 0.0),
                "open_interest": call_contract.get("open_interest", 0.0),
                "oi_reduce_only_mode": call_oi_reduce,
                "qualified": metrics["qualified"],
                "qual_duration_sec": call_dur,
                "qual_time_str": call_time_str,
                "reasons_failed": metrics["reasons_failed"],
                "is_atm": (strike_val == atm_strike)
            }
            all_evaluated.append(item)
            if metrics["qualified"]:
                qualified_strikes.append(item)
                if check_alerts:
                    check_strike_qual_alert(item, call_dur)
            else:
                if check_alerts:
                    reset_strike_alert(call_sym)

        # Evaluate PUT
        put_contract = row.get("put")
        if put_contract:
            bid = put_contract.get("best_bid")
            ask = put_contract.get("best_ask")
            tick = float(put_contract.get("tick_size") or 0.1)

            put_oi_reduce = bool(put_contract.get("oi_reduce_only_mode", False))
            metrics = calculate_mm_metrics(
                bid=bid,
                ask=ask,
                tick_size=tick,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge,
                oi_reduce_only_mode=put_oi_reduce
            )

            put_sym = put_contract.get("symbol")
            now_eval = time.time()
            if metrics["qualified"]:
                if put_sym not in _QUALIFIED_SINCE_MAP:
                    _QUALIFIED_SINCE_MAP[put_sym] = now_eval
                put_dur = now_eval - _QUALIFIED_SINCE_MAP[put_sym]
                put_time_str = format_qual_duration(put_dur)
            else:
                _QUALIFIED_SINCE_MAP.pop(put_sym, None)
                put_dur = None
                put_time_str = "--"

            item = {
                "product_id": put_contract.get("product_id"),
                "symbol": put_sym,
                "underlying": asset,
                "option_type": "PUT",
                "type_badge": "PE",
                "strike": strike_val,
                "expiry": selected_expiry,
                "tick_size": metrics["tick_size"],
                "ltp": put_contract.get("ltp"),
                "mark_price": put_contract.get("mark_price"),
                "bid": metrics["bid"],
                "ask": metrics["ask"],
                "mid_price": metrics["mid_price"],
                "spread": metrics["spread"],
                "spread_ticks": metrics["spread_ticks"],
                "net_edge": metrics["net_edge"],
                "net_edge_pct": metrics["net_edge_pct"],
                "volume": put_contract.get("volume", 0.0),
                "open_interest": put_contract.get("open_interest", 0.0),
                "oi_reduce_only_mode": put_oi_reduce,
                "qualified": metrics["qualified"],
                "qual_duration_sec": put_dur,
                "qual_time_str": put_time_str,
                "reasons_failed": metrics["reasons_failed"],
                "is_atm": (strike_val == atm_strike)
            }
            all_evaluated.append(item)
            if metrics["qualified"]:
                qualified_strikes.append(item)
                if check_alerts:
                    check_strike_qual_alert(item, put_dur)
            else:
                if check_alerts:
                    reset_strike_alert(put_sym)

    # Sort qualified strikes: oldest qualified first (longest qual_duration_sec at top),
    # newly qualified strikes placed at the bottom!
    qualified_strikes.sort(
        key=lambda x: (
            x.get("qual_duration_sec") or 0.0,
            x.get("net_edge_pct", 0),
            x.get("spread_ticks", 0)
        ),
        reverse=True
    )

    max_ticks = max((x["spread_ticks"] for x in qualified_strikes), default=0.0)
    max_edge = max((x["net_edge_pct"] for x in qualified_strikes), default=0.0)

    return {
        "success": True,
        "underlying": asset,
        "selected_expiry": selected_expiry,
        "spot_price": spot_price,
        "atm_strike": atm_strike,
        "criteria": {
            "min_bid_price": min_bid,
            "min_spread_ticks": min_ticks,
            "min_net_edge_pct": min_edge
        },
        "summary": {
            "total_scanned": len(all_evaluated),
            "total_qualified": len(qualified_strikes),
            "max_spread_ticks": max_ticks,
            "max_net_edge_pct": max_edge
        },
        "qualified_strikes": qualified_strikes,
        "all_strikes": all_evaluated,
        "expiries": expiries,
        "timestamp": time.time()
    }


def print_terminal_table(result: dict, show_all: bool = False, is_multi: bool = False, recent_flashes: list = None):
    """
    Renders a formatted, colored ASCII table of selected strikes on terminal.
    """
    underlying = result.get("underlying", "BTC")
    expiry = result.get("selected_expiry", "--")
    spot = result.get("spot_price", 0.0)
    atm = result.get("atm_strike", "--")
    criteria = result.get("criteria", {})
    summary = result.get("summary", {})
    strikes_to_show = result.get("all_strikes", []) if show_all else result.get("qualified_strikes", [])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _render_recent_flashes():
        if not recent_flashes:
            return
        now_ts = time.time()
        active_symbols = {s.get("symbol") for s in result.get("qualified_strikes", [])}
        latest_by_sym = {}
        for f in recent_flashes:
            sym = f.get("symbol")
            if sym and f.get("underlying", underlying) == underlying and sym not in active_symbols and (now_ts - f.get("timestamp", 0)) < 120:
                latest_by_sym[sym] = f
        past_flashes = sorted(latest_by_sym.values(), key=lambda x: x.get("timestamp", 0))
        if past_flashes:
            print(f"\n  {COLOR_BOLD}{COLOR_YELLOW}⚡ RECENT OPPORTUNITIES LOG (Flashed & disappeared within last 2 min):{COLOR_RESET}")
            print("  " + "-" * 126)
            f_hdr = f"  {'Time':<9} {'Symbol':<22} {'Type':<5} {'Strike':<8} {'Bid':<8} {'Ask':<8} {'Spread':<8} {'Ticks':<7} {'Edge %':<9} {'Was Qual':<12} {'Seen Ago'}"
            print(f"  {COLOR_DIM}{f_hdr}{COLOR_RESET}")
            print("  " + "-" * 126)
            for f in past_flashes[-5:]:
                age_s = max(0, int(now_ts - f.get("timestamp", 0)))
                t_badge = f.get("type_badge", "CE")
                t_col = COLOR_CYAN if t_badge == "CE" else COLOR_MAGENTA
                was_qual = f.get("was_qual_str", "<1s")
                row = (
                    f"  {f.get('time_str'):<9} "
                    f"{f.get('symbol'):<22} "
                    f"{t_col}{t_badge:<5}{COLOR_RESET} "
                    f"{f.get('strike', 0):<8.0f} "
                    f"{f.get('bid', 0):<8.2f} "
                    f"{f.get('ask', 0):<8.2f} "
                    f"{f.get('spread', 0):<8.2f} "
                    f"{f.get('spread_ticks', 0):<7.1f} "
                    f"{COLOR_GREEN}{f.get('net_edge_pct', 0):>6.1f}%{COLOR_RESET}   "
                    f"{COLOR_CYAN}{was_qual:<12}{COLOR_RESET} "
                    f"{COLOR_YELLOW}{age_s}s ago{COLOR_RESET}"
                )
                print(row)
            print("  " + "-" * 126)

    # Header Banner (Width: 128 characters)
    active_acct = (load_credentials().get("active_account") or "SUB").upper()
    margin_mode = get_active_margin_mode()

    print("\n" + "=" * 128)
    if not is_multi:
        print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CRITERIA  |  {COLOR_WHITE}Acct: {COLOR_YELLOW}{active_acct}{COLOR_CYAN}  |  {COLOR_WHITE}Margin Mode: {COLOR_GREEN}{margin_mode}{COLOR_RESET}")
        print(f"{COLOR_DIM}  Doc Source: Nitin Joshi Framework | Time: {now_str} IST{COLOR_RESET}")
    else:
        print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKES: {COLOR_YELLOW}{underlying}{COLOR_CYAN}  |  {COLOR_WHITE}Acct: {COLOR_YELLOW}{active_acct}{COLOR_CYAN}  |  {COLOR_WHITE}Margin Mode: {COLOR_GREEN}{margin_mode}{COLOR_CYAN}  (Time: {now_str} IST){COLOR_RESET}")
    print("=" * 128)

    # Criteria Strip
    print(
        f"  {COLOR_BOLD}Asset:{COLOR_RESET} {COLOR_YELLOW}{underlying}{COLOR_RESET}  |  "
        f"{COLOR_BOLD}Expiry:{COLOR_RESET} {COLOR_YELLOW}{expiry}{COLOR_RESET}  |  "
        f"{COLOR_BOLD}Spot:{COLOR_RESET} ${spot:,.2f}  |  "
        f"{COLOR_BOLD}ATM Strike:{COLOR_RESET} {atm}"
    )
    print(
        f"  {COLOR_BOLD}Filter Criteria:{COLOR_RESET} "
        f"Bid >= {COLOR_GREEN}{criteria.get('min_bid_price')}{COLOR_RESET}  |  "
        f"Spread Ticks >= {COLOR_GREEN}{criteria.get('min_spread_ticks')}{COLOR_RESET}  |  "
        f"Net Edge % >= {COLOR_GREEN}{criteria.get('min_net_edge_pct')}%{COLOR_RESET}"
    )
    print("-" * 128)

    # Summary Stats
    total_scanned = summary.get("total_scanned", 0)
    total_qual = summary.get("total_qualified", 0)
    max_ticks = summary.get("max_spread_ticks", 0.0)
    max_edge = summary.get("max_net_edge_pct", 0.0)

    print(
        f"  {COLOR_BOLD}Scanned:{COLOR_RESET} {total_scanned} Contracts  |  "
        f"{COLOR_BOLD}QUALIFIED:{COLOR_RESET} {COLOR_BG_GREEN} {total_qual} Opportunities {COLOR_RESET}  |  "
        f"{COLOR_BOLD}Max Spread Ticks:{COLOR_RESET} {COLOR_CYAN}{max_ticks:.1f}{COLOR_RESET}  |  "
        f"{COLOR_BOLD}Top Net Edge:{COLOR_RESET} {COLOR_GREEN}{max_edge:.2f}%{COLOR_RESET}"
    )
    print("-" * 128)

    # Table Header with Qual Time (Style 2A) column
    cols_header = (
        f"{'Symbol':<22} "
        f"{'Type':<5} "
        f"{'Strike':<9} "
        f"{'Bid ($)':<9} "
        f"{'Ask ($)':<9} "
        f"{'Spread':<9} "
        f"{'Tick':<6} "
        f"{'Ticks':<8} "
        f"{'Mid ($)':<9} "
        f"{'Net Edge':<9} "
        f"{'Edge %':<9} "
        f"{'Qual Time':<12} "
        f"{'Status'}"
    )
    print(f"{COLOR_BOLD}{cols_header}{COLOR_RESET}")
    print("-" * 128)

    if not strikes_to_show:
        if not show_all:
            print(f"  {COLOR_YELLOW}No strikes met the strict qualification criteria at this moment.{COLOR_RESET}")
            print(f"  {COLOR_DIM}Tip: Try relaxing criteria with --min-bid 0.1 --min-ticks 2 --min-edge 20 or view all with --all{COLOR_RESET}")
        else:
            print(f"  {COLOR_RED}No options found for this expiry.{COLOR_RESET}")
        _render_recent_flashes()
        print("=" * 128 + "\n")
        return

    # Rows
    for s in strikes_to_show:
        is_qual = s.get("qualified", False)
        type_str = s.get("type_badge", "CE")
        sym = s.get("symbol", "")
        strike_val = s.get("strike", 0.0)
        bid = s.get("bid", 0.0)
        ask = s.get("ask", 0.0)
        spread = s.get("spread", 0.0)
        tick = s.get("tick_size", 0.1)
        ticks = s.get("spread_ticks", 0.0)
        mid = s.get("mid_price", 0.0)
        net_edge = s.get("net_edge", 0.0)
        edge_pct = s.get("net_edge_pct", 0.0)
        qual_time_str = s.get("qual_time_str") or "--"
        if not is_qual:
            qual_time_str = "--"

        # Color coding
        if is_qual:
            row_color = COLOR_GREEN
            status_badge = f"{COLOR_BG_GREEN} QUALIFIED {COLOR_RESET}"
            qual_display = f"{COLOR_YELLOW}{qual_time_str:<12}{COLOR_RESET}"
        elif s.get("oi_reduce_only_mode"):
            row_color = COLOR_RED
            status_badge = f"{COLOR_BG_RED} ⛔ MAX OI {COLOR_RESET}"
            qual_display = f"{COLOR_DIM}{qual_time_str:<12}{COLOR_RESET}"
        else:
            row_color = COLOR_DIM
            status_badge = f"{COLOR_DIM}NOT MET{COLOR_RESET}"
            qual_display = f"{COLOR_DIM}{qual_time_str:<12}{COLOR_RESET}"

        type_color = COLOR_CYAN if type_str == "CE" else COLOR_MAGENTA

        row_str = (
            f"{sym:<22} "
            f"{type_color}{type_str:<5}{COLOR_RESET} "
            f"{strike_val:<9.0f} "
            f"{bid:<9.2f} "
            f"{ask:<9.2f} "
            f"{spread:<9.2f} "
            f"{tick:<6.2f} "
            f"{COLOR_YELLOW if ticks >= criteria.get('min_spread_ticks', 5) else ''}{ticks:<8.1f}{COLOR_RESET} "
            f"{mid:<9.2f} "
            f"{net_edge:<9.2f} "
            f"{row_color}{edge_pct:>6.1f}%{COLOR_RESET}   "
            f"{qual_display} "
            f"{status_badge}"
        )
        print(row_str)

    print("=" * 128)
    _render_recent_flashes()
    print(f"{COLOR_DIM} Formula Reminder: Net Edge = Spread - (2*Tick) | Net Edge % = (Net Edge / Mid)*100 | Qual Time: Style 2A continuous uptime{COLOR_RESET}\n")


def print_combined_terminal_table(underlying_data: dict, show_all: bool = False, recent_flashes: list = None):
    """
    Renders a unified, single ASCII table combining strikes from multiple underlyings (e.g. BTC and ETH).
    Displays dual-asset metrics side-by-side in header, merges and ranks opportunities by Net Edge %,
    and fits cleanly on a single screen without vertical scrolling.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assets_list = list(underlying_data.keys())
    assets_str = " & ".join(assets_list)

    # Aggregate qualified and all strikes
    all_qual_strikes = []
    all_scanned_strikes = []
    for asset_name, r in underlying_data.items():
        all_qual_strikes.extend(r.get("qualified_strikes", []))
        all_scanned_strikes.extend(r.get("all_strikes", []))

    strikes_to_show = list(all_scanned_strikes if show_all else all_qual_strikes)

    # Rank: qualified first (if show_all), then qual_duration_sec desc (oldest at top, newest at bottom), then net_edge_pct desc
    strikes_to_show.sort(
        key=lambda x: (
            x.get("qualified", False),
            x.get("qual_duration_sec") or 0.0,
            x.get("net_edge_pct", 0),
            x.get("spread_ticks", 0)
        ),
        reverse=True
    )

    total_scanned = sum(r.get("summary", {}).get("total_scanned", 0) for r in underlying_data.values())
    total_qual = len(all_qual_strikes)
    qual_breakdown = ", ".join(f"{len(r.get('qualified_strikes', []))} {a}" for a, r in underlying_data.items())
    max_ticks = max((x["spread_ticks"] for x in all_qual_strikes), default=0.0)
    max_edge = max((x["net_edge_pct"] for x in all_qual_strikes), default=0.0)

    first_res = next(iter(underlying_data.values()))
    criteria = first_res.get("criteria", {})

    def _render_recent_flashes():
        if not recent_flashes:
            return
        now_ts = time.time()
        active_symbols = {s.get("symbol") for s in all_qual_strikes}
        latest_by_sym = {}
        for f in recent_flashes:
            sym = f.get("symbol")
            if sym and sym not in active_symbols and (now_ts - f.get("timestamp", 0)) < 120:
                latest_by_sym[sym] = f
        past_flashes = sorted(latest_by_sym.values(), key=lambda x: x.get("timestamp", 0))
        if past_flashes:
            print(f"\n  {COLOR_BOLD}{COLOR_YELLOW}⚡ RECENT OPPORTUNITIES LOG (Flashed & disappeared within last 2 min):{COLOR_RESET}")
            print("  " + "-" * 126)
            f_hdr = f"  {'Time':<9} {'Asset':<6} {'Symbol':<22} {'Type':<5} {'Strike':<8} {'Bid':<8} {'Ask':<8} {'Spread':<8} {'Ticks':<7} {'Edge %':<9} {'Was Qual':<11} {'Seen Ago'}"
            print(f"  {COLOR_DIM}{f_hdr}{COLOR_RESET}")
            print("  " + "-" * 126)
            for f in past_flashes[-6:]:
                age_s = max(0, int(now_ts - f.get("timestamp", 0)))
                t_badge = f.get("type_badge", "CE")
                t_col = COLOR_CYAN if t_badge == "CE" else COLOR_MAGENTA
                f_asset = f.get("underlying") or ("BTC" if "BTC" in f.get("symbol", "") else "ETH")
                a_col = COLOR_YELLOW if f_asset == "BTC" else COLOR_MAGENTA
                was_qual = f.get("was_qual_str", "<1s")
                row = (
                    f"  {f.get('time_str'):<9} "
                    f"{a_col}{f_asset:<6}{COLOR_RESET} "
                    f"{f.get('symbol'):<22} "
                    f"{t_col}{t_badge:<5}{COLOR_RESET} "
                    f"{f.get('strike', 0):<8.0f} "
                    f"{f.get('bid', 0):<8.2f} "
                    f"{f.get('ask', 0):<8.2f} "
                    f"{f.get('spread', 0):<8.2f} "
                    f"{f.get('spread_ticks', 0):<7.1f} "
                    f"{COLOR_GREEN}{f.get('net_edge_pct', 0):>6.1f}%{COLOR_RESET}   "
                    f"{COLOR_CYAN}{was_qual:<11}{COLOR_RESET} "
                    f"{COLOR_YELLOW}{age_s}s ago{COLOR_RESET}"
                )
                print(row)
            print("  " + "-" * 126)

    # 1. Header Banner (Width: 128 characters)
    active_acct = (load_credentials().get("active_account") or "SUB").upper()
    margin_mode = get_active_margin_mode()

    print("\n" + "=" * 128)
    print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKES: {COLOR_YELLOW}{assets_str}{COLOR_CYAN}  |  {COLOR_WHITE}Acct: {COLOR_YELLOW}{active_acct}{COLOR_CYAN}  |  {COLOR_WHITE}Margin Mode: {COLOR_GREEN}{margin_mode}{COLOR_CYAN}  (Time: {now_str} IST){COLOR_RESET}")
    print("=" * 128)

    # 2. Side-by-side Dual Asset Strip (Fits comfortably within 126 cols)
    asset_parts = []
    for a_name, r in underlying_data.items():
        s = r.get("spot_price", 0.0)
        atm = r.get("atm_strike", "--")
        exp = r.get("selected_expiry", "--")
        col = COLOR_YELLOW if a_name == "BTC" else (COLOR_MAGENTA if a_name == "ETH" else COLOR_CYAN)
        asset_parts.append(f"{COLOR_BOLD}{col}{a_name:<3}{COLOR_RESET}  |  {COLOR_BOLD}Spot:{COLOR_RESET} ${s:,.2f}  |  {COLOR_BOLD}ATM:{COLOR_RESET} {atm}  |  {COLOR_BOLD}Exp:{COLOR_RESET} {COLOR_YELLOW}{exp}{COLOR_RESET}")
    print("  " + "    ||    ".join(asset_parts))

    # 3. Filter Criteria
    print(
        f"  {COLOR_BOLD}Filter Criteria:{COLOR_RESET} "
        f"Bid >= {COLOR_GREEN}{criteria.get('min_bid_price')}{COLOR_RESET}  |  "
        f"Spread Ticks >= {COLOR_GREEN}{criteria.get('min_spread_ticks')}{COLOR_RESET}  |  "
        f"Net Edge % >= {COLOR_GREEN}{criteria.get('min_net_edge_pct')}%{COLOR_RESET}"
    )
    print("-" * 128)

    # 4. Summary Stats
    print(
        f"  {COLOR_BOLD}Total Scanned:{COLOR_RESET} {total_scanned} Contracts  |  "
        f"{COLOR_BOLD}QUALIFIED:{COLOR_RESET} {COLOR_BG_GREEN} {total_qual} Opportunities ({qual_breakdown}) {COLOR_RESET}  |  "
        f"{COLOR_BOLD}Max Spread Ticks:{COLOR_RESET} {COLOR_CYAN}{max_ticks:.1f}{COLOR_RESET}  |  "
        f"{COLOR_BOLD}Top Net Edge:{COLOR_RESET} {COLOR_GREEN}{max_edge:.2f}%{COLOR_RESET}"
    )
    print("-" * 128)

    # 5. Table Header with Asset column
    cols_header = (
        f"{'Asset':<6} "
        f"{'Symbol':<22} "
        f"{'Type':<5} "
        f"{'Strike':<8} "
        f"{'Bid ($)':<8} "
        f"{'Ask ($)':<8} "
        f"{'Spread':<8} "
        f"{'Tick':<6} "
        f"{'Ticks':<7} "
        f"{'Mid ($)':<8} "
        f"{'Net Edge':<9} "
        f"{'Edge %':<9} "
        f"{'Qual Time':<11} "
        f"{'Status'}"
    )
    print(f"{COLOR_BOLD}{cols_header}{COLOR_RESET}")
    print("-" * 128)

    # 6. Table Rows
    if not strikes_to_show:
        if not show_all:
            print(f"  {COLOR_YELLOW}No strikes met the strict qualification criteria across {assets_str} at this moment.{COLOR_RESET}")
            print(f"  {COLOR_DIM}Tip: Try relaxing criteria with --min-bid 0.1 --min-ticks 2 --min-edge 20 or view all with --all{COLOR_RESET}")
        else:
            print(f"  {COLOR_RED}No options found for these expiries.{COLOR_RESET}")
        print("=" * 128)
        _render_recent_flashes()
        print(f"{COLOR_DIM} Formula Reminder: Net Edge = Spread - (2*Tick) | Net Edge % = (Net Edge / Mid)*100 | Qual Time: Style 2A continuous uptime{COLOR_RESET}\n")
        return

    for s in strikes_to_show:
        is_qual = s.get("qualified", False)
        asset_label = s.get("underlying") or ("BTC" if "BTC" in s.get("symbol", "") else "ETH")
        asset_col = COLOR_YELLOW if asset_label == "BTC" else COLOR_MAGENTA
        type_str = s.get("type_badge", "CE")
        sym = s.get("symbol", "")
        strike_val = s.get("strike", 0.0)
        bid = s.get("bid", 0.0)
        ask = s.get("ask", 0.0)
        spread = s.get("spread", 0.0)
        tick = s.get("tick_size") or (0.1 if asset_label == "BTC" else 0.01)
        ticks = s.get("spread_ticks", 0.0)
        mid = s.get("mid_price", 0.0)
        net_edge = s.get("net_edge", 0.0)
        edge_pct = s.get("net_edge_pct", 0.0)
        qual_time_str = s.get("qual_time_str") or "--"
        if not is_qual:
            qual_time_str = "--"

        # Color coding
        if is_qual:
            row_color = COLOR_GREEN
            status_badge = f"{COLOR_BG_GREEN} QUALIFIED {COLOR_RESET}"
            qual_display = f"{COLOR_YELLOW}{qual_time_str:<11}{COLOR_RESET}"
        elif s.get("oi_reduce_only_mode"):
            row_color = COLOR_RED
            status_badge = f"{COLOR_BG_RED} ⛔ MAX OI {COLOR_RESET}"
            qual_display = f"{COLOR_DIM}{qual_time_str:<11}{COLOR_RESET}"
        else:
            row_color = COLOR_DIM
            status_badge = f"{COLOR_DIM}NOT MET{COLOR_RESET}"
            qual_display = f"{COLOR_DIM}{qual_time_str:<11}{COLOR_RESET}"

        type_color = COLOR_CYAN if type_str == "CE" else COLOR_MAGENTA
        ticks_col = COLOR_YELLOW if ticks >= criteria.get('min_spread_ticks', 5) else ''

        row_str = (
            f"{asset_col}{asset_label:<6}{COLOR_RESET} "
            f"{sym:<22} "
            f"{type_color}{type_str:<5}{COLOR_RESET} "
            f"{strike_val:<8.0f} "
            f"{bid:<8.2f} "
            f"{ask:<8.2f} "
            f"{spread:<8.2f} "
            f"{tick:<6.2f} "
            f"{ticks_col}{ticks:<7.1f}{COLOR_RESET} "
            f"{mid:<8.2f} "
            f"{net_edge:<9.2f} "
            f"{row_color}{edge_pct:>6.1f}%{COLOR_RESET}   "
            f"{qual_display} "
            f"{status_badge}"
        )
        print(row_str)

    print("=" * 128)
    _render_recent_flashes()
    print(f"{COLOR_DIM} Formula Reminder: Net Edge = Spread - (2*Tick) | Net Edge % = (Net Edge / Mid)*100 | Qual Time: Style 2A continuous uptime{COLOR_RESET}\n")


def stream_terminal_websocket(
    underlying,
    expiry: str = None,
    min_bid: float = 0.3,
    min_ticks: float = 5.0,
    min_edge: float = 50.0,
    show_all: bool = False,
    auto_trade: bool = False,
    account: str = None,
    refresh_interval: float = None
):
    """
    Subscribes all option contracts for the chosen underlying(s) and expiry directly to Delta's WebSocket
    and streams live tick-by-tick updates to the terminal without periodic REST polling.
    Supports single asset ('BTC' or 'ETH') or multiple assets (['BTC', 'ETH'] or 'ALL' / 'BOTH').
    When auto_trade is True, automatically executes 1-lot MM trades on top qualified strikes.
    """
    defaults = get_mm_config()
    eff_refresh = refresh_interval if refresh_interval is not None else defaults.get("refresh_interval", 0.25)
    eff_refresh = max(0.05, float(eff_refresh))
    if isinstance(underlying, str):
        if underlying.upper() in ["ALL", "BOTH"]:
            assets = ["BTC", "ETH"]
        else:
            assets = [underlying.upper()]
    elif isinstance(underlying, (list, tuple, set)):
        assets = [str(u).upper() for u in underlying]
    else:
        assets = ["BTC"]

    active_trade_holder = [None]
    if auto_trade:
        from mm_execution import mm_engine

    target_expiry = expiry

    while True:
        # Check if requested expiry has already expired (past 17:30 IST)
        if target_expiry and is_expiry_expired(target_expiry):
            print(f"\n{COLOR_YELLOW}[EXPIRY ROLLOVER]{COLOR_RESET} Expiry '{target_expiry}' expired at 17:30 IST. Auto-rolling to next active expiry...")
            target_expiry = None

        client = get_delta_client(account=account)
        underlying_data = {}
        all_contracts = {}
        symbols_to_sub = []
        recent_flashes = []
        active_expiries = {}

        # Refresh master scrips to catch newly launched Delta option contracts
        try:
            sync_master_scrips(client=client, force=True)
        except Exception:
            pass

        for asset in assets:
            print(f"\n{COLOR_CYAN}[INITIALIZING]{COLOR_RESET} Fetching active option catalog for {asset}...")
            init_res = evaluate_mm_strikes(
                underlying=asset,
                expiry=target_expiry,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge,
                client=client
            )

            if not init_res.get("success", True):
                print(f"{COLOR_RED}[ERROR]{COLOR_RESET} {init_res.get('error', f'Failed to fetch {asset} options')}")
                continue

            strikes = init_res.get("all_strikes", [])
            if not strikes:
                print(f"{COLOR_RED}[WARNING]{COLOR_RESET} No active option contracts found for {asset}.")
                continue

            underlying_data[asset] = init_res
            active_expiries[asset] = init_res.get("selected_expiry")

            for x in strikes:
                sym = x.get("symbol")
                if sym:
                    all_contracts[sym] = (asset, x)
                    symbols_to_sub.append(sym)

            spot_symbol = f"{asset}USD"
            if spot_symbol not in symbols_to_sub:
                symbols_to_sub.append(spot_symbol)

        if not underlying_data:
            print(f"{COLOR_RED}[ERROR]{COLOR_RESET} No active option contracts found to stream. Retrying in 5s...")
            time.sleep(5)
            continue

        exp_labels = [f"{a}:{exp}" for a, exp in active_expiries.items() if exp]
        asset_title = " & ".join(underlying_data.keys())
        print(f"{COLOR_GREEN}[WEBSOCKET CONNECTING]{COLOR_RESET} Subscribing {len(symbols_to_sub)} contracts live ({asset_title} | Expiry: {', '.join(exp_labels)})...")

        tick_count = [0]
        last_render_time = [0.0]
        _is_first_render = [True]
        render_lock = threading.Lock()
        is_multi = len(underlying_data) > 1

        def _render_screen():
            update_sender_heartbeat("mm_strikes")
            now_render = time.time()
            for asset in underlying_data:
                all_list = [c for a, c in all_contracts.values() if a == asset]

                # Sanity check: Ensure contracts with missing or crossed quotes are never qualified
                for x in all_list:
                    b = x.get("bid")
                    a = x.get("ask")
                    if b is None or a is None or b <= 0 or a <= 0 or a < b:
                        if x.get("qualified"):
                            x["qualified"] = False
                            x["spread"] = 0.0
                            x["mid_price"] = 0.0
                            x["net_edge"] = 0.0
                            x["net_edge_pct"] = 0.0
                            x["spread_ticks"] = 0.0
                            x["qual_duration_sec"] = None
                            x["qual_time_str"] = "--"
                            x_sym = x.get("symbol")
                            _QUALIFIED_SINCE_MAP.pop(x_sym, None)
                            reset_strike_alert(x_sym)

                qual_list = [x for x in all_list if x.get("qualified")]

                # Live timer update on qualified strikes so seconds/minutes increment smoothly
                for x in qual_list:
                    x_sym = x.get("symbol")
                    if x_sym in _QUALIFIED_SINCE_MAP:
                        x["qual_duration_sec"] = now_render - _QUALIFIED_SINCE_MAP[x_sym]
                        x["qual_time_str"] = format_qual_duration(x["qual_duration_sec"])
                        # Real-time Telegram alert check (>= threshold duration)
                        check_strike_qual_alert(x, x["qual_duration_sec"])
                for x in all_list:
                    if not x.get("qualified"):
                        x["qual_time_str"] = "--"
                        reset_strike_alert(x.get("symbol"))

                qual_list.sort(key=lambda x: (x["net_edge_pct"], x["spread_ticks"]), reverse=True)

                underlying_data[asset]["all_strikes"] = all_list
                underlying_data[asset]["qualified_strikes"] = qual_list
                underlying_data[asset]["summary"] = {
                    "total_scanned": len(all_list),
                    "total_qualified": len(qual_list),
                    "max_spread_ticks": max((x["spread_ticks"] for x in qual_list), default=0.0),
                    "max_net_edge_pct": max((x["net_edge_pct"] for x in qual_list), default=0.0),
                    "total_ticks": tick_count[0]
                }

            # Auto-trading trigger logic on qualified strike
            if auto_trade:
                if active_trade_holder[0] is not None and not active_trade_holder[0].is_running:
                    active_trade_holder[0] = None

                if active_trade_holder[0] is None:
                    combined_qual = []
                    for d in underlying_data.values():
                        combined_qual.extend(d.get("qualified_strikes", []))
                    if combined_qual:
                        combined_qual.sort(key=lambda x: (x.get("net_edge_pct", 0), x.get("spread_ticks", 0)), reverse=True)
                        top_opp = combined_qual[0]
                        active_acct = (account or os.getenv("ACTIVE_ACCOUNT", "main")).lower()
                        opp_asset = top_opp.get("underlying") or ("BTC" if "BTC" in top_opp["symbol"] else "ETH")
                        trade_size = get_auto_trade_lot_size(opp_asset)
                        active_trade_holder[0] = mm_engine.start_trade(
                            symbol=top_opp["symbol"],
                            size=trade_size,
                            account=active_acct
                        )

            if _is_first_render[0]:
                os.system("cls" if os.name == "nt" else "clear")
                _is_first_render[0] = False
            else:
                sys.stdout.write("\033[H\033[J")
                sys.stdout.flush()

            print(f"{COLOR_GREEN}● [WEBSOCKET LIVE TICK-BY-TICK STREAM ACTIVE]{COLOR_RESET} Ticks: {tick_count[0]} | Subscribed: {len(symbols_to_sub)} contracts ({asset_title}) | Refresh: {eff_refresh}s")

            tg_cfg = get_telegram_config()
            if tg_cfg.get("is_configured") and tg_cfg.get("enabled"):
                tg_status = f"{COLOR_GREEN}ACTIVE (Threshold: {tg_cfg['notify_secs']}s){COLOR_RESET}"
            elif tg_cfg.get("is_configured"):
                tg_status = f"{COLOR_YELLOW}PAUSED (Disabled in Settings){COLOR_RESET}"
            else:
                tg_status = f"{COLOR_DIM}SETUP REQUIRED (Bot Token/Chat ID missing){COLOR_RESET}"

            recent_alerts = get_recent_alerts(limit=2)
            recent_str = ""
            if recent_alerts:
                recent_items = [f"{a['symbol']} ({a['duration_str']} at {a['time']})" for a in recent_alerts]
                recent_str = f" | {COLOR_GREEN}🚀 Sent: {', '.join(recent_items)}{COLOR_RESET}"

            print(f" ✈️ Telegram Alerts: {tg_status}{recent_str}")

            # Print Auto-Trade Live Banner if active
            if auto_trade:
                acct_label = (account or os.getenv("ACTIVE_ACCOUNT", "main")).upper()
                curr = active_trade_holder[0]
                if curr and curr.is_running:
                    cost_str = f"${curr.buy_fill_price:.2f}" if curr.buy_fill_price else "Pending Buy Fill"
                    target_str = f"${curr.sell_limit_price:.2f}" if curr.sell_limit_price else "--"
                    tot = getattr(curr, "total_size", curr.size)
                    b_f = getattr(curr, "buy_filled_qty", 0)
                    s_f = getattr(curr, "sell_filled_qty", 0)
                    lot_info = f"({curr.size} Lot{'s' if curr.size > 1 else ''})"
                    if curr.state == "BUY_PARTIAL":
                        lot_info = f"{COLOR_YELLOW}[PARTIAL BUY: {b_f}/{tot} Lots Filled]{COLOR_RESET}"
                    elif curr.state == "SELL_PARTIAL":
                        lot_info = f"{COLOR_YELLOW}[PARTIAL EXIT: {s_f}/{curr.size} Lots Sold]{COLOR_RESET}"
                    elif tot > 1:
                        lot_info = f"({tot} Lots)"

                    print("=" * 128)
                    print(f" {COLOR_BOLD}{COLOR_GREEN}⚡ [AUTONOMOUS MM TRADE ACTIVE]{COLOR_RESET} Symbol: {COLOR_CYAN}{curr.symbol}{COLOR_RESET} | Account: {COLOR_YELLOW}[{acct_label}]{COLOR_RESET} {lot_info}")
                    print(f"    State: {COLOR_BOLD}{curr.state}{COLOR_RESET} | Entry Cost: {cost_str} | Sell Target: {target_str} | Realized PnL: ${curr.realized_pnl:.5f} USD")
                    print(f"    Rule: {COLOR_BOLD}{COLOR_BG_GREEN} Strictly NO Stop-Loss, NO Timeout • Cost-to-Cost or Wait {COLOR_RESET} ({curr.status_message})")
                    print("=" * 128)
                else:
                    print("=" * 128)
                    btc_sz = get_auto_trade_lot_size('BTC')
                    eth_sz = get_auto_trade_lot_size('ETH')
                    print(f" {COLOR_BOLD}{COLOR_GREEN}⚡ [AUTONOMOUS AUTO-TRADE ACTIVE]{COLOR_RESET} Listening on [{acct_label}]... Auto-executes MM cycle using configured lot sizes (BTC: {btc_sz} lot / ETH: {eth_sz} lot) when qualified strike appears.")
                    print("=" * 128)

            if is_multi:
                print_combined_terminal_table(underlying_data, show_all=show_all, recent_flashes=recent_flashes)
            else:
                for asset, res in underlying_data.items():
                    print_terminal_table(res, show_all=show_all, is_multi=False, recent_flashes=recent_flashes)

        def on_tick_received(symbol, quote_dict, raw_data):
            tick_count[0] += 1
            sym_upper = symbol.upper()

            for asset in underlying_data:
                if sym_upper == f"{asset}USD":
                    new_spot = quote_dict.get("spot_price") or quote_dict.get("mark_price") or quote_dict.get("ltp")
                    if new_spot:
                        underlying_data[asset]["spot_price"] = float(new_spot)

            if sym_upper in all_contracts:
                asset, contract = all_contracts[sym_upper]
                c_sym = contract.get("symbol")
                quotes = raw_data.get("quotes") if isinstance(raw_data.get("quotes"), dict) else {}

                # 1. Update Bid cleanly (set to None if empty, null, or missing)
                if "best_bid" in quotes:
                    rb = quotes.get("best_bid")
                    contract["bid"] = float(rb) if (rb is not None and str(rb).strip() not in ("", "null", "None")) else None
                elif "bid" in quote_dict:
                    rb = quote_dict.get("bid")
                    contract["bid"] = float(rb) if (rb is not None and str(rb).strip() not in ("", "null", "None")) else None
                elif "best_bid" in raw_data:
                    rb = raw_data.get("best_bid")
                    contract["bid"] = float(rb) if (rb is not None and str(rb).strip() not in ("", "null", "None")) else None

                # 2. Update Ask cleanly (set to None if empty, null, or missing)
                if "best_ask" in quotes:
                    ra = quotes.get("best_ask")
                    contract["ask"] = float(ra) if (ra is not None and str(ra).strip() not in ("", "null", "None")) else None
                elif "ask" in quote_dict:
                    ra = quote_dict.get("ask")
                    contract["ask"] = float(ra) if (ra is not None and str(ra).strip() not in ("", "null", "None")) else None
                elif "best_ask" in raw_data:
                    ra = raw_data.get("best_ask")
                    contract["ask"] = float(ra) if (ra is not None and str(ra).strip() not in ("", "null", "None")) else None

                raw_ltp = raw_data.get("close") if raw_data.get("close") is not None else quote_dict.get("ltp")
                if raw_ltp is not None and str(raw_ltp).strip() not in ("", "null", "None"):
                    try:
                        contract["ltp"] = float(raw_ltp)
                    except (ValueError, TypeError):
                        pass

                if "oi_reduce_only_mode" in raw_data:
                    contract["oi_reduce_only_mode"] = bool(raw_data.get("oi_reduce_only_mode"))
                elif "oi_reduce_only_mode" in quote_dict:
                    contract["oi_reduce_only_mode"] = bool(quote_dict.get("oi_reduce_only_mode"))

                # Re-evaluate MM metrics in real-time
                tick = contract.get("tick_size") or (0.1 if asset == "BTC" else 0.01)
                b = contract.get("bid")
                a = contract.get("ask")
                now_ts = time.time()
                update_sender_heartbeat("mm_strikes")

                if b is not None and a is not None and b > 0 and a > 0 and a >= b:
                    spread = a - b
                    mid = (b + a) / 2.0
                    net_edge = spread - (2.0 * tick)
                    net_edge_pct = (net_edge / mid * 100.0) if mid > 0 else 0.0
                    spread_ticks = spread / tick if tick > 0 else 0.0

                    reasons = []
                    if contract.get("oi_reduce_only_mode"):
                        reasons.append("Delta Exchange Max OI Breached (Reduce-Only Mode)")
                    if b < min_bid:
                        reasons.append(f"Bid < {min_bid}")
                    if spread_ticks < min_ticks:
                        reasons.append(f"Spread Ticks < {min_ticks}")
                    if net_edge_pct < min_edge:
                        reasons.append(f"Net Edge % < {min_edge}%")

                    contract["spread"] = round(spread, 4)
                    contract["mid_price"] = round(mid, 4)
                    contract["net_edge"] = round(net_edge, 4)
                    contract["net_edge_pct"] = round(net_edge_pct, 2)
                    contract["spread_ticks"] = round(spread_ticks, 1)

                    is_qual = (len(reasons) == 0)
                    contract["qualified"] = is_qual
                    contract["reasons_failed"] = reasons

                    # Real-time continuous qualification duration tracking (Style 2A)
                    if is_qual:
                        if c_sym not in _QUALIFIED_SINCE_MAP:
                            _QUALIFIED_SINCE_MAP[c_sym] = now_ts
                        elapsed_s = now_ts - _QUALIFIED_SINCE_MAP[c_sym]
                        contract["qual_duration_sec"] = elapsed_s
                        contract["qual_time_str"] = format_qual_duration(elapsed_s)
                        check_strike_qual_alert(contract, elapsed_s)
                    else:
                        was_dur = (now_ts - _QUALIFIED_SINCE_MAP.pop(c_sym)) if c_sym in _QUALIFIED_SINCE_MAP else None
                        if was_dur is not None:
                            contract["was_qual_str"] = format_qual_duration(was_dur)
                            # Update the symbol's flash record with its final duration and exit timestamp
                            for f in recent_flashes:
                                if f.get("symbol") == c_sym:
                                    f["was_qual_str"] = contract["was_qual_str"]
                                    f["timestamp"] = now_ts
                                    f["time_str"] = datetime.now().strftime("%H:%M:%S")
                                    break
                        contract["qual_duration_sec"] = None
                        contract["qual_time_str"] = "--"
                        reset_strike_alert(c_sym)

                    # Track flashes that qualify so user can see them even if they disappear quickly
                    if contract["qualified"]:
                        now_f = time.time()
                        last_f = contract.get("_last_flash_ts", 0)
                        if now_f - last_f > 1.0:
                            contract["_last_flash_ts"] = now_f
                            flash_entry = {
                                "symbol": contract.get("symbol"),
                                "underlying": asset,
                                "type_badge": contract.get("type_badge"),
                                "strike": contract.get("strike"),
                                "bid": contract.get("bid"),
                                "ask": contract.get("ask"),
                                "spread": contract.get("spread"),
                                "spread_ticks": contract.get("spread_ticks"),
                                "net_edge_pct": contract.get("net_edge_pct"),
                                "was_qual_str": contract.get("qual_time_str", "1s"),
                                "time_str": datetime.now().strftime("%H:%M:%S"),
                                "timestamp": now_f,
                            }
                            # Update existing entry for this symbol in-place so only the last log is maintained
                            existing_idx = next((i for i, f in enumerate(recent_flashes) if f.get("symbol") == contract.get("symbol")), None)
                            if existing_idx is not None:
                                recent_flashes[existing_idx] = flash_entry
                            else:
                                recent_flashes.append(flash_entry)
                                if len(recent_flashes) > 20:
                                    recent_flashes.pop(0)
                else:
                    # Invalid, empty, or crossed quotes -> IMMEDIATELY DISQUALIFY
                    contract["spread"] = 0.0
                    contract["mid_price"] = 0.0
                    contract["net_edge"] = 0.0
                    contract["net_edge_pct"] = 0.0
                    contract["spread_ticks"] = 0.0
                    contract["qualified"] = False
                    contract["reasons_failed"] = ["No valid quotes" if (b is None or a is None) else "Crossed or zero quotes"]
                    was_dur = (now_ts - _QUALIFIED_SINCE_MAP.pop(c_sym)) if c_sym in _QUALIFIED_SINCE_MAP else None
                    if was_dur is not None:
                        contract["was_qual_str"] = format_qual_duration(was_dur)
                        for f in recent_flashes:
                            if f.get("symbol") == c_sym:
                                f["was_qual_str"] = contract["was_qual_str"]
                                f["timestamp"] = now_ts
                                f["time_str"] = datetime.now().strftime("%H:%M:%S")
                                break
                    contract["qual_duration_sec"] = None
                    contract["qual_time_str"] = "--"
                    reset_strike_alert(c_sym)

        # Low-latency non-blocking network socket manager:
        # 1. subscribe_l2=False avoids CPU-heavy 20-level orderbook dictionary sorting across 100+ option contracts
        # 2. on_tick_received processes in-memory state updates only (<0.05ms) so socket thread never pauses
        ws_manager = DeltaWebSocketManager(
            symbols=symbols_to_sub,
            verbose=False,
            on_tick=on_tick_received,
            subscribe_l2=False
        )
        ws_manager.start()

        # Background REST Reconciliation Worker (runs every 3.5s)
        # Guarantees that if any WebSocket packet is dropped or connection stalls,
        # live REST tickers reconcile memory immediately so data is NEVER frozen/wrong
        stop_audit = threading.Event()
        def _rest_audit_worker():
            while not stop_audit.is_set():
                time.sleep(3.5)
                if stop_audit.is_set():
                    break
                try:
                    raw_tickers = client.get_tickers() or []
                    for t in raw_tickers:
                        sym = (t.get("symbol") or "").upper()
                        if sym in all_contracts or any(sym == f"{asset}USD" for asset in underlying_data):
                            q = t.get("quotes") or {}
                            rb = q.get("best_bid")
                            ra = q.get("best_ask")
                            b = float(rb) if (rb is not None and str(rb).strip() not in ("", "null", "None")) else None
                            a = float(ra) if (ra is not None and str(ra).strip() not in ("", "null", "None")) else None
                            quote_dict = {
                                "bid": b,
                                "ask": a,
                                "ltp": t.get("close"),
                                "mark_price": t.get("mark_price"),
                                "spot_price": t.get("spot_price") or t.get("underlying_price")
                            }
                            on_tick_received(sym, quote_dict, t)
                except Exception:
                    pass

        audit_thread = threading.Thread(target=_rest_audit_worker, daemon=True, name="MM-REST-Audit")
        audit_thread.start()

        rollover_triggered = False
        try:
            # Initial display
            if is_multi:
                print_combined_terminal_table(underlying_data, show_all=show_all, recent_flashes=recent_flashes)
            else:
                for asset, res in underlying_data.items():
                    print_terminal_table(res, show_all=show_all, is_multi=False, recent_flashes=recent_flashes)

            while True:
                time.sleep(min(0.03, eff_refresh / 4))
                now_check = time.time()
                # Continuous smooth timer refresh even when market ticks are quiet
                if now_check - last_render_time[0] >= eff_refresh:
                    last_render_time[0] = now_check
                    with render_lock:
                        _render_screen()
                # Auto-check if any currently streamed expiry has reached expiration (17:30 IST / 12:00 UTC)
                for asset, exp_str in active_expiries.items():
                    if exp_str and is_expiry_expired(exp_str):
                        print(f"\n\n{'=' * 90}")
                        print(f" {COLOR_BOLD}{COLOR_YELLOW}🔄 [EXPIRY ROLLOVER DETECTED]{COLOR_RESET} Expiry '{exp_str}' for {asset} expired at 17:30 IST!")
                        print(f"    Auto-rolling stream to next active upcoming expiry...")
                        print(f"{'=' * 90}\n")
                        rollover_triggered = True
                        break

                if rollover_triggered:
                    break

        except KeyboardInterrupt:
            print(f"\n{COLOR_YELLOW}[WEBSOCKET STOPPED]{COLOR_RESET} Exiting live WebSocket market making stream.\n")
            stop_audit.set()
            ws_manager.stop()
            if auto_trade and active_trade_holder[0] and active_trade_holder[0].is_running:
                print(f"[NOTICE] Active trade for {active_trade_holder[0].symbol} is currently in state '{active_trade_holder[0].state}'.")
            return

        # Stop previous ws_manager and audit worker before rolling to next expiry
        stop_audit.set()
        ws_manager.stop()
        time.sleep(1)
        target_expiry = None  # Ensure next iteration picks next active upcoming expiry


def run_cli():
    """CLI entry point for running standalone Market Making Strike Selection scanner."""
    defaults = get_mm_config()

    parser = argparse.ArgumentParser(
        description="Market Making Strike Selection Criteria Scanner (Delta Exchange)"
    )
    parser.add_argument(
        "-u", "--underlying",
        type=str.upper,
        choices=["BTC", "ETH", "ALL", "BOTH"],
        default=defaults["underlying"] if defaults["underlying"] in ["BTC", "ETH", "ALL", "BOTH"] else "ALL",
        help=f"Underlying asset symbol (BTC, ETH, or ALL to scan both). Default: {defaults['underlying']}"
    )
    parser.add_argument(
        "-b", "--both",
        action="store_true",
        help="Scan and watch both BTC and ETH simultaneously"
    )
    parser.add_argument(
        "-e", "--expiry",
        default=None,
        help="Expiration date (e.g. 280325). Default: nearest active expiry"
    )
    parser.add_argument(
        "--min-bid",
        type=float,
        default=defaults["min_bid_price"],
        help=f"Minimum Bid Price threshold. Default: {defaults['min_bid_price']}"
    )
    parser.add_argument(
        "--min-ticks",
        type=float,
        default=defaults["min_spread_ticks"],
        help=f"Minimum Spread in Ticks threshold. Default: {defaults['min_spread_ticks']}"
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=defaults["min_net_edge_pct"],
        help=f"Minimum Net Edge Percentage threshold. Default: {defaults['min_net_edge_pct']}%%"
    )
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="Display all scanned strikes instead of only qualified opportunities"
    )
    parser.add_argument(
        "-w", "--watch",
        action="store_true",
        help="Continuously stream live tick-by-tick market making evaluation from WebSocket"
    )
    parser.add_argument(
        "-t", "--trade",
        type=str,
        default=None,
        help="Execute 1-lot MM trade on a specific strike (e.g. --trade P-BTC-79400-151026)"
    )
    parser.add_argument(
        "--auto-trade",
        action="store_true",
        help="Automatically scan and trade qualified opportunities as soon as they appear"
    )
    parser.add_argument(
        "--account",
        choices=["main", "sub"],
        default=None,
        help="Account to execute trade on ('main' or 'sub'). Default: ACTIVE_ACCOUNT from .env"
    )
    parser.add_argument(
        "--price",
        type=float,
        default=None,
        help="Upload/specify custom entry Limit Buy price"
    )
    parser.add_argument(
        "--target",
        type=float,
        default=None,
        help="Upload/specify custom target Limit Sell exit price"
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=defaults.get("refresh_interval", 0.25),
        help=f"Terminal screen refresh throttle interval in seconds (default: {defaults.get('refresh_interval', 0.25)}s)"
    )
    parser.add_argument(
        "--threshold", "--notify-secs",
        dest="notify_secs",
        type=int,
        default=None,
        help="Continuous qualification duration threshold (in seconds) for Telegram alerts (overrides .env)"
    )

    args = parser.parse_args()

    set_process_role("mm_strikes")
    update_sender_heartbeat("mm_strikes")

    if args.notify_secs is not None:
        set_notify_secs_override(args.notify_secs)

    # Direct 1-Lot MM Trade Execution on specified strike
    if args.trade:
        from mm_execution import mm_engine
        print("\n" + "=" * 70)
        print(f" {COLOR_BOLD}{COLOR_GREEN}DELTA EXCHANGE 1-LOT MARKET MAKING EXECUTION{COLOR_RESET}")
        print(f" Symbol:       {COLOR_CYAN}{args.trade}{COLOR_RESET}")
        print(f" Size:         {COLOR_BOLD}1 Lot (1 Contract){COLOR_RESET}")
        print(f" Account:      {COLOR_YELLOW}{(args.account or os.getenv('ACTIVE_ACCOUNT', 'main')).upper()}{COLOR_RESET}")
        print(f" Strategy:     {COLOR_GREEN}1st BUY Maker (Post-Only) -> 2nd SELL Maker (Post-Only){COLOR_RESET}")
        print(f" Price Drops:  {COLOR_BOLD}{COLOR_BG_GREEN} STRICTLY NO STOP LOSS, NO TIMEOUT -> EXIT COST-TO-COST OR WAIT {COLOR_RESET}")
        print("=" * 70 + "\n")

        trade = mm_engine.start_trade(
            symbol=args.trade,
            size=1,
            entry_price=args.price,
            target_exit_price=args.target,
            account=args.account
        )

        try:
            while trade.is_running:
                time.sleep(2)
            print("\n" + "=" * 70)
            print(f" {COLOR_BOLD}MM Trade Final Result:{COLOR_RESET} {trade.state}")
            print(f" Realized PnL: ${trade.realized_pnl:.5f} USD")
            print("=" * 70)
        except KeyboardInterrupt:
            print("\n[STOP] User interrupted. Cancelling trade...")
            trade.stop()
        return

    watch_both = args.both or (args.underlying in ["ALL", "BOTH"])
    underlyings = ["BTC", "ETH"] if watch_both else [args.underlying]

    client = get_delta_client(account=args.account)

    # 1. Live WebSocket Watch (with or without --auto-trade)
    if args.watch:
        stream_terminal_websocket(
            underlying=underlyings,
            expiry=args.expiry,
            min_bid=args.min_bid,
            min_ticks=args.min_ticks,
            min_edge=args.min_edge,
            show_all=args.all,
            auto_trade=args.auto_trade,
            account=args.account,
            refresh_interval=args.refresh
        )
        return

    # 2. Standalone Autonomous Auto-Trader (REST scanner loop)
    if args.auto_trade:
        from mm_execution import mm_engine
        active_acct = (args.account or os.getenv("ACTIVE_ACCOUNT", "main")).lower()
        print("\n" + "=" * 70)
        btc_sz = get_auto_trade_lot_size('BTC')
        eth_sz = get_auto_trade_lot_size('ETH')
        print(f" {COLOR_BOLD}{COLOR_GREEN}DELTA EXCHANGE AUTONOMOUS MM TRADING ENGINE{COLOR_RESET}")
        print(f" Underlyings:  {COLOR_CYAN}{', '.join(underlyings)}{COLOR_RESET}")
        print(f" Account:      {COLOR_YELLOW}{active_acct.upper()}{COLOR_RESET}")
        print(f" Lot Sizes:    {COLOR_BOLD}BTC: {btc_sz} lot | ETH: {eth_sz} lot{COLOR_RESET}")
        print(f" Criteria:     Bid >= {args.min_bid} | Ticks >= {args.min_ticks} | Edge >= {args.min_edge}%")
        print(f" Strategy:     {COLOR_GREEN}Buy Maker -> Sell Maker (Strictly 1 Trade at a time){COLOR_RESET}")
        print(f" Price Drops:  {COLOR_BOLD}{COLOR_BG_GREEN} STRICTLY NO STOP LOSS, NO TIMEOUT -> EXIT COST-TO-COST OR WAIT {COLOR_RESET}")
        print("=" * 70 + "\n")

        current_trade = None
        target_expiry = args.expiry
        try:
            while True:
                # If trade is actively running, monitor it
                if current_trade and current_trade.is_running:
                    time.sleep(2)
                    continue

                if current_trade and not current_trade.is_running:
                    print(f"\n[CYCLE COMPLETED] {current_trade.symbol} finished with state: {current_trade.state} | Realized PnL: ${current_trade.realized_pnl:.5f} USD")
                    print(f"[BOT] Searching for next qualified opportunity...\n")
                    current_trade = None

                # Check if target expiry expired (17:30 IST / 12:00 UTC)
                if target_expiry and is_expiry_expired(target_expiry):
                    print(f"\n[EXPIRY ROLLOVER] Expiry '{target_expiry}' expired at 17:30 IST. Auto-rolling to next active expiry...")
                    target_expiry = None

                # Scan for qualified strikes
                all_qual = []
                for u in underlyings:
                    try:
                        res = evaluate_mm_strikes(
                            underlying=u,
                            expiry=target_expiry,
                            min_bid_price=args.min_bid,
                            min_spread_ticks=args.min_ticks,
                            min_net_edge_pct=args.min_edge,
                            client=client
                        )
                        qs = res.get("qualified_strikes", [])
                        all_qual.extend(qs)
                    except Exception:
                        pass

                if all_qual:
                    # Rank by Net Edge % descending
                    all_qual.sort(key=lambda x: x.get("net_edge_pct", 0), reverse=True)
                    best = all_qual[0]
                    sym = best["symbol"]
                    edge = best.get("net_edge_pct", 0)
                    ticks = best.get("spread_ticks", 0)
                    bid = best.get("bid", 0)
                    ask = best.get("ask", 0)

                    trade_size = get_auto_trade_lot_size("BTC" if "BTC" in sym else "ETH")
                    print(f"\n⚡ {COLOR_BOLD}{COLOR_GREEN}[QUALIFIED OPPORTUNITY FOUND]{COLOR_RESET} {COLOR_CYAN}{sym}{COLOR_RESET}")
                    print(f"   Bid: ${bid} | Ask: ${ask} | Spread Ticks: {ticks:.1f} | Net Edge: {edge:.1f}%")
                    print(f"   Auto-launching {trade_size}-Lot MM Trade on [{active_acct.upper()}]...")

                    current_trade = mm_engine.start_trade(
                        symbol=sym,
                        size=trade_size,
                        account=active_acct
                    )
                else:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    print(f"\r[{now_str}] Scanning {', '.join(underlyings)}... No qualified strikes meeting criteria (Bid>={args.min_bid}, Ticks>={args.min_ticks}, Edge>={args.min_edge}%). Waiting...", end="", flush=True)

                time.sleep(4)
        except KeyboardInterrupt:
            print("\n\n[STOP] Auto-Trader stopped by user.")
            if current_trade and current_trade.is_running:
                print(f"[INFO] Trade for {current_trade.symbol} is currently in state '{current_trade.state}'.")
        return

    # 3. One-shot scan
    is_multi = len(underlyings) > 1
    scan_expiry = args.expiry
    if scan_expiry and is_expiry_expired(scan_expiry):
        print(f"{COLOR_YELLOW}[NOTICE]{COLOR_RESET} Expiry '{scan_expiry}' expired at 17:30 IST. Auto-evaluating next active expiry...")
        scan_expiry = None

    if is_multi:
        combined_data = {}
        for sym in underlyings:
            res = evaluate_mm_strikes(
                underlying=sym,
                expiry=scan_expiry,
                min_bid_price=args.min_bid,
                min_spread_ticks=args.min_ticks,
                min_net_edge_pct=args.min_edge,
                client=client
            )
            combined_data[sym] = res
        print_combined_terminal_table(combined_data, show_all=args.all)
    else:
        for sym in underlyings:
            res = evaluate_mm_strikes(
                underlying=sym,
                expiry=scan_expiry,
                min_bid_price=args.min_bid,
                min_spread_ticks=args.min_ticks,
                min_net_edge_pct=args.min_edge,
                client=client
            )
            print_terminal_table(res, show_all=args.all, is_multi=False)
        
        # Print tip on qualified strikes
        qual_count = len(res.get("qualified_strikes", []))
        if qual_count > 0:
            first_sym = res["qualified_strikes"][0]["symbol"]
            print(f"  {COLOR_DIM}[TIP] To execute 1-lot MM trade on top strike:{COLOR_RESET} {COLOR_GREEN}python mm_strikes.py --trade {first_sym}{COLOR_RESET}\n")


if __name__ == "__main__":
    run_cli()

