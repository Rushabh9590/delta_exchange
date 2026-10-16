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
COLOR_BG_GREEN = "\033[42m\033[30m"
COLOR_BG_DARK = "\033[100m"


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

    underlying = os.getenv("MM_DEFAULT_UNDERLYING", "BTC").strip().upper()

    return {
        "min_bid_price": min_bid,
        "min_spread_ticks": min_ticks,
        "min_net_edge_pct": min_net_edge_pct,
        "underlying": underlying
    }


def calculate_mm_metrics(
    bid: float,
    ask: float,
    tick_size: float,
    min_bid_price: float = 0.3,
    min_spread_ticks: float = 5.0,
    min_net_edge_pct: float = 50.0
) -> dict:
    """
    Computes exact market making metrics for a strike/option quote per document specifications:
    1. Spread = Ask - Bid
    2. Mid = (Bid + Ask) / 2
    3. Net Edge = Spread - (2 * Tick Size)
    4. Net Edge % = (Net Edge / Mid) * 100 (0 if Mid <= 0)
    5. Spread Ticks = Spread / Tick Size
    6. Qualification check:
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
    client = None
) -> dict:
    """
    Scans live Delta Exchange option chain contracts, computes Market Making metrics,
    and returns categorized qualified strikes and overall statistics.
    """
    defaults = get_mm_config()
    min_bid = min_bid_price if min_bid_price is not None else defaults["min_bid_price"]
    min_ticks = min_spread_ticks if min_spread_ticks is not None else defaults["min_spread_ticks"]
    min_edge = min_net_edge_pct if min_net_edge_pct is not None else defaults["min_net_edge_pct"]
    asset = (underlying or defaults["underlying"]).strip().upper()

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

    c = client or get_delta_client()
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

            metrics = calculate_mm_metrics(
                bid=bid,
                ask=ask,
                tick_size=tick,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge
            )

            item = {
                "product_id": call_contract.get("product_id"),
                "symbol": call_contract.get("symbol"),
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
                "qualified": metrics["qualified"],
                "reasons_failed": metrics["reasons_failed"],
                "is_atm": (strike_val == atm_strike)
            }
            all_evaluated.append(item)
            if metrics["qualified"]:
                qualified_strikes.append(item)

        # Evaluate PUT
        put_contract = row.get("put")
        if put_contract:
            bid = put_contract.get("best_bid")
            ask = put_contract.get("best_ask")
            tick = float(put_contract.get("tick_size") or 0.1)

            metrics = calculate_mm_metrics(
                bid=bid,
                ask=ask,
                tick_size=tick,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge
            )

            item = {
                "product_id": put_contract.get("product_id"),
                "symbol": put_contract.get("symbol"),
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
                "qualified": metrics["qualified"],
                "reasons_failed": metrics["reasons_failed"],
                "is_atm": (strike_val == atm_strike)
            }
            all_evaluated.append(item)
            if metrics["qualified"]:
                qualified_strikes.append(item)

    # Sort qualified by Net Edge % descending, then by Spread Ticks descending
    qualified_strikes.sort(key=lambda x: (x["net_edge_pct"], x["spread_ticks"]), reverse=True)

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
        past_flashes = [
            f for f in recent_flashes 
            if f.get("symbol") not in active_symbols and (now_ts - f.get("timestamp", 0)) < 120
        ]
        if past_flashes:
            print(f"\n  {COLOR_BOLD}{COLOR_YELLOW}⚡ RECENT OPPORTUNITIES LOG (Flashed & disappeared within last 2 min):{COLOR_RESET}")
            print("  " + "-" * 110)
            f_hdr = f"  {'Time':<9} {'Symbol':<22} {'Type':<5} {'Strike':<8} {'Bid':<8} {'Ask':<8} {'Spread':<8} {'Ticks':<7} {'Edge %':<9} {'Seen Ago'}"
            print(f"  {COLOR_DIM}{f_hdr}{COLOR_RESET}")
            print("  " + "-" * 110)
            for f in past_flashes[-5:]:
                age_s = max(0, int(now_ts - f.get("timestamp", 0)))
                t_badge = f.get("type_badge", "CE")
                t_col = COLOR_CYAN if t_badge == "CE" else COLOR_MAGENTA
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
                    f"{COLOR_YELLOW}{age_s}s ago{COLOR_RESET}"
                )
                print(row)
            print("  " + "-" * 110)

    # Header Banner
    print("\n" + "=" * 115)
    if not is_multi:
        print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CRITERIA{COLOR_RESET}")
        print(f"{COLOR_DIM}  Doc Source: Nitin Joshi Framework | Time: {now_str} IST{COLOR_RESET}")
    else:
        print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKES: {COLOR_YELLOW}{underlying}{COLOR_CYAN}  (Time: {now_str} IST){COLOR_RESET}")
    print("=" * 115)

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
    print("-" * 115)

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
    print("-" * 115)

    # Table Header
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
        f"{'Status'}"
    )
    print(f"{COLOR_BOLD}{cols_header}{COLOR_RESET}")
    print("-" * 115)

    if not strikes_to_show:
        if not show_all:
            print(f"  {COLOR_YELLOW}No strikes met the strict qualification criteria at this moment.{COLOR_RESET}")
            print(f"  {COLOR_DIM}Tip: Try relaxing criteria with --min-bid 0.1 --min-ticks 2 --min-edge 20 or view all with --all{COLOR_RESET}")
        else:
            print(f"  {COLOR_RED}No options found for this expiry.{COLOR_RESET}")
        _render_recent_flashes()
        print("=" * 115 + "\n")
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

        # Color coding
        if is_qual:
            row_color = COLOR_GREEN
            status_badge = f"{COLOR_BG_GREEN} QUALIFIED {COLOR_RESET}"
        else:
            row_color = COLOR_DIM
            status_badge = f"{COLOR_DIM}NOT MET{COLOR_RESET}"

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
            f"{status_badge}"
        )
        print(row_str)

    print("=" * 115)
    _render_recent_flashes()
    print(f"{COLOR_DIM} Formula Reminder: Net Edge = Spread - (2*Tick) | Net Edge % = (Net Edge / Mid)*100{COLOR_RESET}\n")


def stream_terminal_websocket(
    underlying,
    expiry: str = None,
    min_bid: float = 0.3,
    min_ticks: float = 5.0,
    min_edge: float = 50.0,
    show_all: bool = False,
    auto_trade: bool = False,
    account: str = None,
    refresh_interval: float = 0.8
):
    """
    Subscribes all option contracts for the chosen underlying(s) and expiry directly to Delta's WebSocket
    and streams live tick-by-tick updates to the terminal without periodic REST polling.
    Supports single asset ('BTC' or 'ETH') or multiple assets (['BTC', 'ETH'] or 'ALL' / 'BOTH').
    When auto_trade is True, automatically executes 1-lot MM trades on top qualified strikes.
    """
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
        render_lock = threading.Lock()
        is_multi = len(underlying_data) > 1

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
                quotes = raw_data.get("quotes") or {}
                raw_bid = quotes.get("best_bid") if quotes.get("best_bid") is not None else quote_dict.get("bid")
                raw_ask = quotes.get("best_ask") if quotes.get("best_ask") is not None else quote_dict.get("ask")
                raw_ltp = raw_data.get("close") if raw_data.get("close") is not None else quote_dict.get("ltp")

                if raw_bid is not None:
                    contract["bid"] = float(raw_bid)
                if raw_ask is not None:
                    contract["ask"] = float(raw_ask)
                if raw_ltp is not None:
                    contract["ltp"] = float(raw_ltp)

                # Re-evaluate MM metrics in real-time
                tick = contract.get("tick_size") or (0.1 if asset == "BTC" else 0.05)
                b = contract.get("bid")
                a = contract.get("ask")
                if b is not None and a is not None and b > 0 and a > 0 and a >= b:
                    spread = a - b
                    mid = (b + a) / 2.0
                    net_edge = spread - (2.0 * tick)
                    net_edge_pct = (net_edge / mid * 100.0) if mid > 0 else 0.0
                    spread_ticks = spread / tick if tick > 0 else 0.0

                    reasons = []
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
                    contract["qualified"] = (len(reasons) == 0)
                    contract["reasons_failed"] = reasons

                    # Track flashes that qualify so user can see them even if they disappear quickly
                    if contract["qualified"]:
                        now_f = time.time()
                        last_f = contract.get("_last_flash_ts", 0)
                        if now_f - last_f > 3.0:
                            contract["_last_flash_ts"] = now_f
                            recent_flashes.append({
                                "symbol": contract.get("symbol"),
                                "type_badge": contract.get("type_badge"),
                                "strike": contract.get("strike"),
                                "bid": contract.get("bid"),
                                "ask": contract.get("ask"),
                                "spread": contract.get("spread"),
                                "spread_ticks": contract.get("spread_ticks"),
                                "net_edge_pct": contract.get("net_edge_pct"),
                                "time_str": datetime.now().strftime("%H:%M:%S"),
                                "timestamp": now_f,
                            })
                            if len(recent_flashes) > 20:
                                recent_flashes.pop(0)

            now = time.time()
            # Refresh terminal in-place with throttle to avoid Windows terminal flickering/tearing
            if now - last_render_time[0] >= refresh_interval:
                last_render_time[0] = now
                with render_lock:
                    for asset in underlying_data:
                        all_list = [c for a, c in all_contracts.values() if a == asset]
                        qual_list = [x for x in all_list if x.get("qualified")]
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
                                active_trade_holder[0] = mm_engine.start_trade(
                                    symbol=top_opp["symbol"],
                                    size=1,
                                    account=active_acct
                                )

                    os.system("cls" if os.name == "nt" else "clear")
                    print(f"{COLOR_GREEN}● [WEBSOCKET LIVE TICK-BY-TICK STREAM ACTIVE]{COLOR_RESET} Ticks: {tick_count[0]} | Subscribed: {len(symbols_to_sub)} contracts ({asset_title}) | Refresh: {refresh_interval}s")

                    # Print Auto-Trade Live Banner if active
                    if auto_trade:
                        acct_label = (account or os.getenv("ACTIVE_ACCOUNT", "main")).upper()
                        curr = active_trade_holder[0]
                        if curr and curr.is_running:
                            cost_str = f"${curr.buy_fill_price:.2f}" if curr.buy_fill_price else "Pending Buy Fill"
                            target_str = f"${curr.sell_limit_price:.2f}" if curr.sell_limit_price else "--"
                            print("=" * 115)
                            print(f" {COLOR_BOLD}{COLOR_GREEN}⚡ [AUTONOMOUS 1-LOT MM TRADE ACTIVE]{COLOR_RESET} Symbol: {COLOR_CYAN}{curr.symbol}{COLOR_RESET} | Account: {COLOR_YELLOW}[{acct_label}]{COLOR_RESET} (1 Lot)")
                            print(f"    State: {COLOR_BOLD}{curr.state}{COLOR_RESET} | Entry Cost: {cost_str} | Sell Target: {target_str} | Realized PnL: ${curr.realized_pnl:.5f} USD")
                            print(f"    Rule: {COLOR_BOLD}{COLOR_BG_GREEN} Strictly NO Stop-Loss, NO Timeout • Cost-to-Cost or Wait {COLOR_RESET} ({curr.status_message})")
                            print("=" * 115)
                        else:
                            print("=" * 115)
                            print(f" {COLOR_BOLD}{COLOR_GREEN}⚡ [AUTONOMOUS AUTO-TRADE ACTIVE]{COLOR_RESET} Listening on [{acct_label}]... Auto-executes 1-lot MM cycle when qualified strike appears.")
                            print("=" * 115)

                    for asset, res in underlying_data.items():
                        print_terminal_table(res, show_all=show_all, is_multi=is_multi, recent_flashes=recent_flashes)

        ws_manager = DeltaWebSocketManager(
            symbols=symbols_to_sub,
            verbose=False,
            on_tick=on_tick_received
        )
        ws_manager.start()

        rollover_triggered = False
        try:
            # Initial display
            for asset, res in underlying_data.items():
                print_terminal_table(res, show_all=show_all, is_multi=is_multi, recent_flashes=recent_flashes)

            while True:
                time.sleep(2)
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
            ws_manager.stop()
            if auto_trade and active_trade_holder[0] and active_trade_holder[0].is_running:
                print(f"[NOTICE] Active trade for {active_trade_holder[0].symbol} is currently in state '{active_trade_holder[0].state}'.")
            return

        # Stop previous ws_manager before rolling to next expiry
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
        default=defaults["underlying"] if defaults["underlying"] in ["BTC", "ETH"] else "BTC",
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
        default=0.8,
        help="Terminal screen refresh throttle interval in seconds (default: 0.8s)"
    )

    args = parser.parse_args()

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
        print(f" {COLOR_BOLD}{COLOR_GREEN}DELTA EXCHANGE AUTONOMOUS 1-LOT MM TRADING ENGINE{COLOR_RESET}")
        print(f" Underlyings:  {COLOR_CYAN}{', '.join(underlyings)}{COLOR_RESET}")
        print(f" Account:      {COLOR_YELLOW}{active_acct.upper()}{COLOR_RESET}")
        print(f" Criteria:     Bid >= {args.min_bid} | Ticks >= {args.min_ticks} | Edge >= {args.min_edge}%")
        print(f" Strategy:     {COLOR_GREEN}1-Lot Buy Maker -> Sell Maker (Strictly 1 Trade at a time){COLOR_RESET}")
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

                    print(f"\n⚡ {COLOR_BOLD}{COLOR_GREEN}[QUALIFIED OPPORTUNITY FOUND]{COLOR_RESET} {COLOR_CYAN}{sym}{COLOR_RESET}")
                    print(f"   Bid: ${bid} | Ask: ${ask} | Spread Ticks: {ticks:.1f} | Net Edge: {edge:.1f}%")
                    print(f"   Auto-launching 1-Lot MM Trade on [{active_acct.upper()}]...")

                    current_trade = mm_engine.start_trade(
                        symbol=sym,
                        size=1,
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

    for sym in underlyings:
        res = evaluate_mm_strikes(
            underlying=sym,
            expiry=scan_expiry,
            min_bid_price=args.min_bid,
            min_spread_ticks=args.min_ticks,
            min_net_edge_pct=args.min_edge,
            client=client
        )
        print_terminal_table(res, show_all=args.all, is_multi=is_multi)
        
        # Print tip on qualified strikes
        qual_count = len(res.get("qualified_strikes", []))
        if qual_count > 0:
            first_sym = res["qualified_strikes"][0]["symbol"]
            print(f"  {COLOR_DIM}[TIP] To execute 1-lot MM trade on top strike:{COLOR_RESET} {COLOR_GREEN}python mm_strikes.py --trade {first_sym}{COLOR_RESET}\n")


if __name__ == "__main__":
    run_cli()

