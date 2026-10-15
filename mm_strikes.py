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
from multileg import get_option_chain_data, get_options_expiries_and_strikes
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


def print_terminal_table(result: dict, show_all: bool = False):
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

    # Header Banner
    print("\n" + "=" * 115)
    print(f"{COLOR_BOLD}{COLOR_CYAN}  DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CRITERIA{COLOR_RESET}")
    print(f"{COLOR_DIM}  Doc Source: Nitin Joshi Framework | Time: {now_str} IST{COLOR_RESET}")
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
    print(f"{COLOR_DIM} Formula Reminder: Net Edge = Spread - (2*Tick) | Net Edge % = (Net Edge / Mid)*100{COLOR_RESET}\n")


def stream_terminal_websocket(
    underlying: str,
    expiry: str,
    min_bid: float,
    min_ticks: float,
    min_edge: float,
    show_all: bool = False
):
    """
    Subscribes all option contracts for the chosen underlying and expiry directly to Delta's WebSocket
    and streams live tick-by-tick updates to the terminal without periodic REST polling.
    """
    client = get_delta_client()
    print(f"\n{COLOR_CYAN}[INITIALIZING]{COLOR_RESET} Fetching option catalog for {underlying}...")
    init_res = evaluate_mm_strikes(
        underlying=underlying,
        expiry=expiry,
        min_bid_price=min_bid,
        min_spread_ticks=min_ticks,
        min_net_edge_pct=min_edge,
        client=client
    )

    all_contracts = {x["symbol"]: x for x in init_res.get("all_strikes", []) if x.get("symbol")}
    symbols_to_sub = list(all_contracts.keys())
    spot_symbol = f"{underlying}USD"
    if spot_symbol not in symbols_to_sub:
        symbols_to_sub.append(spot_symbol)

    if not symbols_to_sub:
        print(f"{COLOR_RED}[ERROR]{COLOR_RESET} No option contracts found for {underlying} expiry {expiry}.")
        return

    print(f"{COLOR_GREEN}[WEBSOCKET CONNECTING]{COLOR_RESET} Subscribing to {len(symbols_to_sub)} contracts live on Delta WebSocket...")

    tick_count = [0]
    last_render_time = [0.0]
    render_lock = threading.Lock()

    def on_tick_received(symbol, quote_dict, raw_data):
        tick_count[0] += 1
        sym_upper = symbol.upper()

        if sym_upper == spot_symbol:
            new_spot = quote_dict.get("spot_price") or quote_dict.get("mark_price") or quote_dict.get("ltp")
            if new_spot:
                init_res["spot_price"] = float(new_spot)

        if sym_upper in all_contracts:
            contract = all_contracts[sym_upper]
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
            tick = contract.get("tick_size") or 0.1
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

        now = time.time()
        # Refresh terminal in-place with slight throttle to avoid flicker
        if now - last_render_time[0] >= 0.25:
            last_render_time[0] = now
            with render_lock:
                all_list = list(all_contracts.values())
                qual_list = [x for x in all_list if x.get("qualified")]
                qual_list.sort(key=lambda x: (x["net_edge_pct"], x["spread_ticks"]), reverse=True)

                init_res["all_strikes"] = all_list
                init_res["qualified_strikes"] = qual_list
                init_res["summary"] = {
                    "total_scanned": len(all_list),
                    "total_qualified": len(qual_list),
                    "max_spread_ticks": max((x["spread_ticks"] for x in qual_list), default=0.0),
                    "max_net_edge_pct": max((x["net_edge_pct"] for x in qual_list), default=0.0),
                    "total_ticks": tick_count[0]
                }
                os.system("cls" if os.name == "nt" else "clear")
                print(f"{COLOR_GREEN}● [WEBSOCKET LIVE TICK-BY-TICK STREAM ACTIVE]{COLOR_RESET} Ticks: {tick_count[0]} | Subscribed: {len(symbols_to_sub)} contracts")
                print_terminal_table(init_res, show_all=show_all)

    ws_manager = DeltaWebSocketManager(
        symbols=symbols_to_sub,
        verbose=False,
        on_tick=on_tick_received
    )
    ws_manager.start()

    try:
        # Initial display
        print_terminal_table(init_res, show_all=show_all)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{COLOR_YELLOW}[WEBSOCKET STOPPED]{COLOR_RESET} Exiting live WebSocket market making stream.\n")
        ws_manager.stop()


def run_cli():
    """CLI entry point for running standalone Market Making Strike Selection scanner."""
    defaults = get_mm_config()

    parser = argparse.ArgumentParser(
        description="Market Making Strike Selection Criteria Scanner (Delta Exchange)"
    )
    parser.add_argument(
        "-u", "--underlying",
        default=defaults["underlying"],
        help=f"Underlying asset symbol (e.g. BTC, ETH, SOL, XRP). Default: {defaults['underlying']}"
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
        help=f"Minimum Net Edge Percentage threshold. Default: {defaults['min_net_edge_pct']}%"
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

    args = parser.parse_args()

    client = get_delta_client()

    if not args.watch:
        # One-shot scan
        res = evaluate_mm_strikes(
            underlying=args.underlying,
            expiry=args.expiry,
            min_bid_price=args.min_bid,
            min_spread_ticks=args.min_ticks,
            min_net_edge_pct=args.min_edge,
            client=client
        )
        print_terminal_table(res, show_all=args.all)
    else:
        # Live tick-by-tick WebSocket streaming
        stream_terminal_websocket(
            underlying=args.underlying,
            expiry=args.expiry,
            min_bid=args.min_bid,
            min_ticks=args.min_ticks,
            min_edge=args.min_edge,
            show_all=args.all
        )


if __name__ == "__main__":
    run_cli()
