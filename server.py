"""
Delta Exchange Open Positions & Multi-Leg Trading Dashboard Server
Serves real-time positions, wallet balances, account metrics, and 1-click multi-leg execution.
"""

import os
import sys
import time
import json
import re
import queue
import threading
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, make_response, send_file, Response

# Suppress Werkzeug / Flask HTTP request access logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logger = logging.getLogger("server")

def _safe_str(val):
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)

class LogBroadcaster:
    """Thread-safe pub/sub broadcaster for Real-Time SSE live log streaming."""
    def __init__(self):
        self._listeners = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def broadcast(self, raw_line: str, source: str = None):
        if not raw_line:
            return
        raw = _safe_str(raw_line).strip()
        if not raw:
            return

        is_mm = ("[MM-" in raw) or (source == "mm")
        is_tunnel = (source == "tunnel")

        item = {
            "text": raw,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_mm": is_mm,
            "is_tunnel": is_tunnel,
            "source": "tunnel" if is_tunnel else ("mm" if is_mm else "server")
        }

        if is_mm:
            tid_m = re.search(r"\[MM-([a-zA-Z0-9_-]+)\]", raw)
            if tid_m:
                item["trade_id"] = tid_m.group(1)
            sym_m = re.search(r"\b([CP]-[A-Z]+-[\d\.]+-[\d]+)\b", raw)
            if sym_m:
                item["symbol"] = sym_m.group(1)
            # Ensure symbol tag is present in raw text for consistent display and searching
            sym = item.get("symbol")
            if sym and f"[{sym}]" not in raw:
                tid = item.get("trade_id")
                if tid and f"[MM-{tid[-6:]}]" in raw:
                    raw = raw.replace(f"[MM-{tid[-6:]}]", f"[MM-{tid[-6:]}] [{sym}]")
                else:
                    raw = f"[{sym}] " + raw
                item["text"] = raw

        with self._lock:
            if not self._listeners:
                return
            dead = []
            for q in self._listeners:
                try:
                    q.put_nowait(item)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(item)
                    except Exception:
                        pass
                except Exception:
                    dead.append(q)
            for d in dead:
                if d in self._listeners:
                    self._listeners.remove(d)

log_broadcaster = LogBroadcaster()

# In-memory console log buffer for live UI log streaming (holds up to 15,000 lines)
class StdoutBufferWrapper:
    def __init__(self, original_stream, maxlen=15000):
        self._stream = original_stream
        self.buffer = deque(maxlen=maxlen)

    def write(self, s):
        if s:
            try:
                s_str = _safe_str(s)
                for line in s_str.splitlines():
                    l_str = line.strip()
                    if l_str:
                        self.buffer.append(l_str)
                        log_broadcaster.broadcast(l_str)
            except Exception:
                pass
        try:
            return self._stream.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            return self._stream.flush()
        except Exception:
            pass

    def get_lines(self, count=250):
        raw = list(self.buffer)[-count:]
        return [_safe_str(x) for x in raw]

if not hasattr(sys.stdout, "get_lines"):
    sys.stdout = StdoutBufferWrapper(sys.stdout, maxlen=15000)
if not hasattr(sys.stderr, "get_lines"):
    sys.stderr = StdoutBufferWrapper(sys.stderr, maxlen=15000)

# Ensure project and python-rest-client paths are in sys.path
BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from login import get_delta_client, load_credentials, switch_account
from multileg import (
    get_all_products,
    get_product_details,
    execute_multi_leg_strategy,
    execute_single_leg_order,
    square_off_single_position,
    square_off_all_open_positions,
    get_options_expiries_and_strikes,
    get_option_chain_data,
    sync_master_scrips,
    get_master_status,
    start_master_sync_daemon,
    get_client
)
from mm_strikes import evaluate_mm_strikes, get_mm_config, start_mm_background_scanner
from mm_execution import mm_engine, rotate_log_file, set_log_broadcast_callback
from websocket import DeltaWebSocketManager
from telegram_notify import (
    get_telegram_config,
    save_telegram_config,
    send_test_alert,
    set_process_role,
    is_active_telegram_sender
)

# Connect MM execution log callback to push directly to SSE log broadcaster
set_log_broadcast_callback(lambda line: log_broadcaster.broadcast(line, source="mm"))

# Designate this process as server role (mm_strikes has priority when open)
set_process_role("server")

# Start background master scrip auto-sync daemon (adaptive 15m default, 45s during 5:30 PM IST rollover window)
start_master_sync_daemon(interval_seconds=900)

# Start 24/7 MM Telegram Alert scanner daemon (scans BTC & ETH continuously without requiring browser to be open)
start_mm_background_scanner(interval_seconds=4.0)

# Start 60-second log file rotation daemon (enforces 10 MB per file limit with 3 backups)
def _start_log_rotation_daemon():
    def _rotate_loop():
        while True:
            time.sleep(60.0)
            try:
                log_dir = BASE_DIR / "logs"
                if log_dir.exists():
                    for name in ["mm_trades.log", "python.log", "tunnel.log", "startup.log"]:
                        fp = log_dir / name
                        rotate_log_file(fp, max_bytes=10 * 1024 * 1024, backup_count=3)
            except Exception:
                pass

    t = threading.Thread(target=_rotate_loop, name="log_rotation_daemon", daemon=True)
    t.start()

# Start background Cloudflare tunnel log watcher to stream tunnel lines via SSE
def _start_tunnel_log_watcher():
    def _tail():
        tunnel_file = BASE_DIR / "logs" / "tunnel.log"
        last_pos = 0
        if tunnel_file.exists():
            try:
                last_pos = tunnel_file.stat().st_size
            except Exception:
                pass
        while True:
            time.sleep(1.0)
            try:
                if not tunnel_file.exists():
                    continue
                cur_size = tunnel_file.stat().st_size
                if cur_size < last_pos:
                    last_pos = 0
                if cur_size > last_pos:
                    with open(tunnel_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        new_lines = f.readlines()
                        last_pos = f.tell()
                    for nl in new_lines:
                        nl_str = nl.strip()
                        if nl_str:
                            log_broadcaster.broadcast(nl_str, source="tunnel")
            except Exception:
                pass

    t = threading.Thread(target=_tail, name="tunnel_log_watcher", daemon=True)
    t.start()

_start_log_rotation_daemon()
_start_tunnel_log_watcher()

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        res = make_response()
        origin = request.headers.get("Origin") or "*"
        res.headers["Access-Control-Allow-Origin"] = origin
        if origin != "*":
            res.headers["Access-Control-Allow-Credentials"] = "true"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Cf-Access-Jwt-Assertion, CF-Access-Client-Id, CF-Access-Client-Secret, Cookie"
        return res

@app.after_request
def add_cache_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Cf-Access-Jwt-Assertion, CF-Access-Client-Id, CF-Access-Client-Secret, Cookie"
    return response

# Cached client instance & WebSocket Manager
_client = None
_ws_manager = None
WS_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]


def get_ws_manager():
    global _ws_manager
    if _ws_manager is None:
        try:
            _ws_manager = DeltaWebSocketManager(symbols=WS_SYMBOLS, verbose=False)
            _ws_manager.start()
            print(f"[WS CONNECTED] Delta WebSocket stream active on server", flush=True)
            print(f"[WS SUBSCRIPTION] Subscribed to {len(WS_SYMBOLS)} symbols: {WS_SYMBOLS} & Spot", flush=True)
        except Exception as e:
            print(f"[WS WARNING] Server WebSocket initialization skipped: {e}", flush=True)
    return _ws_manager


def get_client(account=None, force_new=False, *args, **kwargs):
    global _client
    require_auth = kwargs.get("require_auth", True)
    current_acct = (account or os.getenv("ACTIVE_ACCOUNT") or "main").strip().lower()
    if _client is None or force_new or account is not None or getattr(_client, "account_name", None) != current_acct:
        _client = get_delta_client(account=current_acct, force_new=True, require_auth=require_auth)
    return _client



def calculate_position_metrics(pos, ticker=None):
    """
    Parses and computes accurate entry-based trading metrics for a Delta position object.
    Uses True Unrealized PnL: (Current Price - Entry Price) * Size * Contract Value.
    """
    try:
        size = float(pos.get("size", 0))
    except (ValueError, TypeError):
        size = 0.0

    try:
        entry_price = float(pos.get("entry_price") or 0)
    except (ValueError, TypeError):
        entry_price = 0.0

    prod_id = pos.get("product_id")
    prod_sym = pos.get("product_symbol")

    # Resolve product specifications
    product = pos.get("product")
    if not product:
        try:
            if prod_sym or prod_id:
                product = get_product_details(prod_sym or prod_id)
        except Exception:
            product = {}
    product = product or {}

    symbol = prod_sym or product.get("symbol") or f"Product #{prod_id}"
    contract_val = float(product.get("contract_value") or (0.001 if "BTC" in symbol else 1.0))
    contract_type = product.get("contract_type") or ("call_options" if symbol.startswith("C-") else ("put_options" if symbol.startswith("P-") else "futures"))

    # Extract prices from ticker if available, fallback to position mark_price
    ticker = ticker or {}
    quotes = ticker.get("quotes") or {}

    try:
        mark_price = float(pos.get("mark_price") or ticker.get("mark_price") or 0)
    except (ValueError, TypeError):
        mark_price = 0.0

    try:
        ltp = float(ticker.get("close") or quotes.get("best_bid") or mark_price or entry_price)
    except (ValueError, TypeError):
        ltp = mark_price or entry_price

    best_bid = float(quotes.get("best_bid") or 0) if quotes.get("best_bid") is not None else None
    best_ask = float(quotes.get("best_ask") or 0) if quotes.get("best_ask") is not None else None

    # Side determination
    side = "LONG" if size > 0 else ("SHORT" if size < 0 else "FLAT")
    abs_size = abs(size)
    underlying_qty = abs_size * contract_val

    # Cost Basis (Total investment paid/received at entry)
    cost_basis = underlying_qty * entry_price

    # Accurate Entry-Based Unrealized PnL: (Price - Entry) * Size * Contract Value
    active_price = ltp if ltp > 0 else mark_price
    unrealized_pnl = (active_price - entry_price) * size * contract_val
    unrealized_pnl_mark = (mark_price - entry_price) * size * contract_val if mark_price > 0 else unrealized_pnl

    if cost_basis > 0:
        pnl_percentage = (unrealized_pnl / cost_basis) * 100.0
    else:
        pnl_percentage = 0.0

    try:
        realized_pnl = float(pos.get("realized_pnl") or 0)
    except (ValueError, TypeError):
        realized_pnl = 0.0

    notional_usd = underlying_qty * active_price if active_price > 0 else cost_basis

    # Type categorization
    type_badge = "FUTURES"
    if "call" in contract_type.lower() or symbol.startswith("C-"):
        type_badge = "CALL"
    elif "put" in contract_type.lower() or symbol.startswith("P-"):
        type_badge = "PUT"
    elif "perpetual" in contract_type.lower() or "swap" in contract_type.lower():
        type_badge = "PERPETUAL"

    u_asset = (product.get("underlying_asset") or {}).get("symbol")
    if not u_asset:
        parts = symbol.split("-")
        if len(parts) >= 2:
            u_asset = parts[1]
        else:
            u_asset = "BTC"

    strike_price = product.get("strike_price")
    if not strike_price:
        parts = symbol.split("-")
        if len(parts) >= 3 and parts[2].isdigit():
            strike_price = float(parts[2])

    desc = product.get("description") or product.get("short_description")
    if not desc:
        if type_badge in ["CALL", "PUT"]:
            desc = f"{u_asset} {strike_price or ''} {type_badge.capitalize()} Option"
        else:
            desc = f"{symbol} {type_badge}"

    return {
        "product_id": prod_id,
        "symbol": symbol,
        "description": desc,
        "contract_type": contract_type,
        "type_badge": type_badge,
        "strike_price": strike_price,
        "settlement_time": product.get("settlement_time"),
        "underlying_asset": u_asset,
        "quoting_asset": (product.get("quoting_asset") or {}).get("symbol", "USD"),
        "settling_asset": (product.get("settling_asset") or {}).get("symbol", "USD"),
        "side": side,
        "size": size,
        "abs_size": abs_size,
        "contract_value": contract_val,
        "underlying_qty": underlying_qty,
        "cost_basis": cost_basis,
        "entry_price": entry_price,
        "ltp": ltp,
        "mark_price": mark_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "notional_usd": notional_usd,
        "unrealized_pnl": round(unrealized_pnl, 4),
        "unrealized_pnl_mark": round(unrealized_pnl_mark, 4),
        "unrealized_pnl_pct": round(pnl_percentage, 2),
        "pnl_percentage": round(pnl_percentage, 2),
        "realized_pnl": realized_pnl,
        "margin_mode": str(pos.get("margin_mode") or "PORTFOLIO").upper(),
        "margin": float(pos.get("margin") or 0),
        "liquidation_price": pos.get("liquidation_price"),
        "bankruptcy_price": pos.get("bankruptcy_price"),
        "auto_topup": pos.get("auto_topup", False),
        "updated_at": pos.get("updated_at"),
        "created_at": pos.get("created_at"),
        "raw": pos
    }


@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/positions", methods=["GET"])
def get_positions():
    """
    Fetches open margined positions from Delta Exchange.
    """
    try:
        client = get_client()
        response = client.request("GET", "/v2/positions/margined", auth=True)
        raw_data = response.json()

        if not raw_data.get("success", True) and "error" in raw_data:
            return jsonify({
                "success": False,
                "error": raw_data.get("error", "Failed to fetch positions")
            }), 400

        result = raw_data.get("result", [])
        if isinstance(result, dict):
            raw_positions = [result]
        elif isinstance(result, list):
            raw_positions = result
        else:
            raw_positions = []

        enriched_positions = []
        total_unrealized_pnl = 0.0
        total_realized_pnl = 0.0
        total_notional = 0.0
        longs_count = 0
        shorts_count = 0

        for pos in raw_positions:
            try:
                size_val = float(pos.get("size", 0))
            except (ValueError, TypeError):
                size_val = 0

            if size_val != 0:
                sym = pos.get("product_symbol") or (pos.get("product") or {}).get("symbol")
                ticker = None
                if sym:
                    try:
                        ticker = client.get_ticker(sym)
                    except Exception:
                        ticker = None
                metrics = calculate_position_metrics(pos, ticker=ticker)
                enriched_positions.append(metrics)
                total_unrealized_pnl += metrics["unrealized_pnl"]
                total_realized_pnl += metrics["realized_pnl"]
                total_notional += metrics["notional_usd"]
                if metrics["side"] == "LONG":
                    longs_count += 1
                elif metrics["side"] == "SHORT":
                    shorts_count += 1

        summary = {
            "total_open_positions": len(enriched_positions),
            "longs_count": longs_count,
            "shorts_count": shorts_count,
            "total_unrealized_pnl": round(total_unrealized_pnl, 4),
            "total_realized_pnl": round(total_realized_pnl, 4),
            "total_notional_usd": round(total_notional, 2),
            "all_positions_count": len(raw_positions)
        }

        return jsonify({
            "success": True,
            "summary": summary,
            "positions": enriched_positions,
            "raw_count": len(raw_positions)
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/wallet", methods=["GET"])
def get_wallet():
    """
    Fetches wallet balances from Delta Exchange.
    """
    try:
        client = get_client()
        balances = client.get_all_wallet_balances()

        usd_wallet = next((b for b in balances if b.get("asset_symbol") == "USD"), None)
        inr_wallet = next((b for b in balances if b.get("asset_symbol") == "INR"), None)

        wallet_summary = {
            "usd_balance": float(usd_wallet.get("balance", 0)) if usd_wallet else 0.0,
            "usd_available": float(usd_wallet.get("available_balance", 0)) if usd_wallet else 0.0,
            "inr_balance": float(usd_wallet.get("balance_inr", 0)) if (usd_wallet and usd_wallet.get("balance_inr")) else 0.0,
            "inr_available": float(usd_wallet.get("available_balance_inr", 0)) if (usd_wallet and usd_wallet.get("available_balance_inr")) else 0.0,
            "portfolio_margin": float(usd_wallet.get("portfolio_margin", 0)) if usd_wallet else 0.0,
            "blocked_margin": float(usd_wallet.get("blocked_margin", 0)) if usd_wallet else 0.0,
            "raw_balances": balances
        }

        return jsonify({
            "success": True,
            "wallet": wallet_summary
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard():
    """
    Consolidated endpoint for positions and wallet data in a single request.
    """
    try:
        client = get_client(require_auth=False)

        # Positions
        raw_positions = []
        try:
            pos_res = client.request("GET", "/v2/positions/margined", auth=True).json()
            raw_positions = pos_res.get("result", [])
            if isinstance(raw_positions, dict):
                raw_positions = [raw_positions]
            elif not isinstance(raw_positions, list):
                raw_positions = []
        except Exception:
            raw_positions = []

        enriched_positions = []
        total_unrealized_pnl = 0.0
        total_realized_pnl = 0.0
        total_notional = 0.0
        longs_count = 0
        shorts_count = 0

        for pos in raw_positions:
            try:
                size_val = float(pos.get("size", 0))
            except (ValueError, TypeError):
                size_val = 0

            if size_val != 0:
                sym = pos.get("product_symbol") or (pos.get("product") or {}).get("symbol")
                ticker = None
                if sym:
                    try:
                        ticker = client.get_ticker(sym)
                    except Exception:
                        ticker = None
                metrics = calculate_position_metrics(pos, ticker=ticker)
                enriched_positions.append(metrics)
                total_unrealized_pnl += metrics["unrealized_pnl"]
                total_realized_pnl += metrics["realized_pnl"]
                total_notional += metrics["notional_usd"]
                if metrics["side"] == "LONG":
                    longs_count += 1
                elif metrics["side"] == "SHORT":
                    shorts_count += 1

        # Wallet
        try:
            balances = client.get_all_wallet_balances()
            usd_wallet = next((b for b in balances if b.get("asset_symbol") == "USD"), None)
            wallet_summary = {
                "usd_balance": float(usd_wallet.get("balance", 0)) if usd_wallet else 0.0,
                "usd_available": float(usd_wallet.get("available_balance", 0)) if usd_wallet else 0.0,
                "inr_balance": float(usd_wallet.get("balance_inr", 0)) if (usd_wallet and usd_wallet.get("balance_inr")) else 0.0,
                "inr_available": float(usd_wallet.get("available_balance_inr", 0)) if (usd_wallet and usd_wallet.get("available_balance_inr")) else 0.0,
                "portfolio_margin": float(usd_wallet.get("portfolio_margin", 0)) if usd_wallet else 0.0,
                "blocked_margin": float(usd_wallet.get("blocked_margin", 0)) if usd_wallet else 0.0
            }
        except Exception:
            wallet_summary = {
                "usd_balance": 0.0, "usd_available": 0.0, "inr_balance": 0.0, "inr_available": 0.0,
                "portfolio_margin": 0.0, "blocked_margin": 0.0
            }

        creds = load_credentials()
        base_url = creds.get("base_url", "https://api.india.delta.exchange")
        env_label = "India Production" if "api.india" in base_url else ("Testnet" if "testnet" in base_url else "Global Production")

        # Live conversion rate
        usd_bal = wallet_summary.get("usd_balance", 0)
        inr_bal = wallet_summary.get("inr_balance", 0)
        usd_to_inr_rate = round(inr_bal / usd_bal, 2) if (usd_bal > 0 and inr_bal > 0) else 85.0

        # Delta Exchange official server timestamp
        delta_server_ts = None
        try:
            btc_ticker = client.get_ticker("BTCUSD")
            if btc_ticker and btc_ticker.get("timestamp"):
                delta_server_ts = float(btc_ticker.get("timestamp")) / 1e6
        except Exception:
            delta_server_ts = None

        return jsonify({
            "success": True,
            "server_timestamp": delta_server_ts if delta_server_ts else time.time(),
            "environment": env_label,
            "base_url": base_url,
            "usd_to_inr_rate": usd_to_inr_rate,
            "margin_mode": get_current_margin_mode(),
            "summary": {
                "total_open_positions": len(enriched_positions),
                "longs_count": longs_count,
                "shorts_count": shorts_count,
                "total_unrealized_pnl": round(total_unrealized_pnl, 4),
                "total_realized_pnl": round(total_realized_pnl, 4),
                "total_notional_usd": round(total_notional, 2),
                "all_positions_count": len(raw_positions)
            },
            "wallet": wallet_summary,
            "positions": enriched_positions
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ==============================================================================
# MULTI-LEG & 1-CLICK TRADING API ROUTES
# ==============================================================================

@app.route("/api/products/search", methods=["GET"])
def search_products():
    """
    Returns available tradable products filtered by underlying (BTC, ETH, etc.) or contract type.
    """
    try:
        client = get_client()
        underlying = request.args.get("underlying", "").upper()
        contract_type = request.args.get("contract_type", "").lower()
        query = request.args.get("q", "").lower()

        all_prods = get_all_products(client)
        matched = []

        for p in all_prods:
            sym = p.get("symbol", "")
            c_type = (p.get("contract_type") or "").lower()
            u_asset = (p.get("underlying_asset") or {}).get("symbol", "").upper()

            if underlying and u_asset != underlying and underlying not in sym.upper():
                continue
            if contract_type and contract_type not in c_type:
                continue
            if query and query not in sym.lower() and query not in str(p.get("id")):
                continue

            matched.append({
                "id": p.get("id"),
                "symbol": sym,
                "contract_type": p.get("contract_type"),
                "strike_price": float(p.get("strike_price") or 0) if p.get("strike_price") else None,
                "settlement_time": p.get("settlement_time"),
                "tick_size": float(p.get("tick_size") or 0.5),
                "contract_value": float(p.get("contract_value") or 1.0),
                "underlying_asset": u_asset,
                "state": p.get("state", "active")
            })

        return jsonify({
            "success": True,
            "count": len(matched),
            "products": matched[:200]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/ticker", methods=["GET"])
def get_ticker_live():
    """
    Ultra-fast live ticker endpoint served directly from memory WebSocket cache or REST fallback.
    """
    try:
        symbol = request.args.get("symbol", "").upper()
        underlying = request.args.get("underlying", "").upper()
        if not symbol and underlying:
            symbol = f"{underlying}USD"
        if not symbol:
            symbol = "BTCUSD"

        # 1. Check WebSocket memory cache first (0ms instantaneous response)
        ws_mgr = get_ws_manager()
        ws_quote = ws_mgr.get_latest_quote(symbol) if ws_mgr else {}
        if ws_quote and (ws_quote.get("ltp") or ws_quote.get("spot_price") or ws_quote.get("mark_price")):
            ltp = float(ws_quote.get("ltp") or 0.0)
            mark_price = float(ws_quote.get("mark_price") or 0.0)
            spot_price = float(ws_quote.get("spot_price") or 0.0)
            underlying_price = spot_price if spot_price > 0 else (ltp if ltp > 0 else mark_price)
            contract_val = 0.001 if "BTC" in symbol else (0.01 if "ETH" in symbol else 1.0)
            now_ts = ws_quote.get("timestamp") or time.time()

            return jsonify({
                "success": True,
                "symbol": symbol,
                "underlying_price": underlying_price,
                "spot_price": spot_price if spot_price > 0 else underlying_price,
                "futures_price": ltp if ltp > 0 else (mark_price if mark_price > 0 else spot_price),
                "ltp": ltp,
                "mark_price": mark_price,
                "contract_value": contract_val,
                "timestamp": now_ts,
                "server_timestamp": now_ts,
                "source": "websocket"
            })

        # 2. REST API fallback if quote not yet populated
        client = get_client()
        ticker = client.get_ticker(symbol)
        if not ticker:
            return jsonify({"success": False, "error": "Ticker not found"}), 404

        ltp = float(ticker.get("close") or 0.0)
        mark_price = float(ticker.get("mark_price") or 0.0)
        spot_price = float(ticker.get("spot_price") or 0.0)
        contract_value = float(ticker.get("contract_value") or (0.001 if "BTC" in symbol else (0.01 if "ETH" in symbol else 1.0)))
        underlying_price = spot_price if spot_price > 0 else (ltp if ltp > 0 else mark_price)

        delta_raw_ts = ticker.get("timestamp")
        delta_server_ts = float(delta_raw_ts) / 1e6 if delta_raw_ts else time.time()

        return jsonify({
            "success": True,
            "symbol": symbol,
            "underlying_price": underlying_price,
            "spot_price": spot_price if spot_price > 0 else underlying_price,
            "futures_price": ltp if ltp > 0 else (mark_price if mark_price > 0 else spot_price),
            "ltp": ltp,
            "mark_price": mark_price,
            "contract_value": contract_value,
            "timestamp": delta_server_ts,
            "server_timestamp": delta_server_ts,
            "source": "rest"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tickers/batch", methods=["GET", "POST"])
def get_tickers_batch():
    """
    Fetches live quotes/tickers for a list of symbols (e.g. multi-leg symbols).
    """
    try:
        symbols_param = request.args.get("symbols", "")
        symbols = []
        if request.is_json:
            data = request.get_json(silent=True) or {}
            symbols = data.get("symbols", [])
        if not symbols and symbols_param:
            symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]

        if not symbols:
            return jsonify({"success": True, "tickers": {}})

        ws_mgr = get_ws_manager()
        client = get_client()
        result = {}

        if ws_mgr and hasattr(ws_mgr, "subscribe_symbols"):
            ws_mgr.subscribe_symbols(symbols)

        # Pre-fetch bulk tickers from REST for fallback if needed
        rest_tickers_map = {}
        try:
            bulk = client.get_tickers() or []
            for t in bulk:
                if t and t.get("symbol"):
                    rest_tickers_map[t["symbol"].upper()] = t
        except Exception:
            pass

        for sym in symbols:
            sym_upper = sym.upper()
            ws_quote = ws_mgr.get_latest_quote(sym_upper) if ws_mgr else {}
            rest_ticker = rest_tickers_map.get(sym_upper) or {}
            quotes = rest_ticker.get("quotes") or {}

            # Prioritize ws_quote then fallback to rest_ticker
            ltp = None
            if ws_quote.get("ltp") is not None and float(ws_quote.get("ltp") or 0) > 0:
                ltp = float(ws_quote["ltp"])
            elif rest_ticker.get("close") is not None:
                ltp = float(rest_ticker["close"])
            elif quotes.get("best_bid") is not None:
                ltp = float(quotes["best_bid"])

            mark_price = None
            if ws_quote.get("mark_price") is not None and float(ws_quote.get("mark_price") or 0) > 0:
                mark_price = float(ws_quote["mark_price"])
            elif rest_ticker.get("mark_price") is not None:
                mark_price = float(rest_ticker["mark_price"])

            bid = None
            if ws_quote.get("bid") is not None and float(ws_quote.get("bid") or 0) > 0:
                bid = float(ws_quote["bid"])
            elif quotes.get("best_bid") is not None:
                bid = float(quotes["best_bid"])

            ask = None
            if ws_quote.get("ask") is not None and float(ws_quote.get("ask") or 0) > 0:
                ask = float(ws_quote["ask"])
            elif quotes.get("best_ask") is not None:
                ask = float(quotes["best_ask"])

            vol = None
            if ws_quote.get("volume") is not None and float(ws_quote.get("volume") or 0) > 0:
                vol = float(ws_quote["volume"])
            elif rest_ticker.get("volume") is not None:
                vol = float(rest_ticker["volume"])

            oi = None
            if ws_quote.get("oi") is not None and float(ws_quote.get("oi") or 0) > 0:
                oi = float(ws_quote["oi"])
            elif rest_ticker.get("oi") is not None or rest_ticker.get("oi_value") is not None:
                oi = float(rest_ticker.get("oi") or rest_ticker.get("oi_value") or 0.0)

            result[sym_upper] = {
                "symbol": sym_upper,
                "ltp": ltp,
                "mark_price": mark_price or 0.0,
                "bid": bid,
                "ask": ask,
                "volume": vol or 0.0,
                "oi": oi or 0.0,
                "source": "websocket" if ws_quote.get("ltp") else "rest"
            }

        return jsonify({"success": True, "tickers": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/master/status", methods=["GET"])
def get_master_scrip_status():
    """
    Returns current master scrips cache status, product counts, and last exchange sync timestamp.
    """
    try:
        status = get_master_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/master/refresh", methods=["GET", "POST"])
def refresh_master_scrips():
    """
    Forces immediate fresh download and sync of master scrips from Delta Exchange.
    """
    try:
        client = get_client(require_auth=False)
        res = sync_master_scrips(client=client, force=True)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/options/expiries", methods=["GET"])
def get_options_expiries():
    """
    Fetches available expiration dates, strikes, contract value, and underlying asset prices.
    """
    try:
        client = get_client(require_auth=False)
        underlying = request.args.get("underlying", "BTC").strip().upper()
        if underlying not in ["BTC", "ETH"]:
            return jsonify({
                "success": False,
                "error": f"Option contracts are only available for BTC and ETH on Delta Exchange. '{underlying}' has no option contracts.",
                "underlying": underlying,
                "expiries": []
            }), 400

        data = get_options_expiries_and_strikes(underlying=underlying, client=client)
        return jsonify({
            "success": True,
            **data
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/options/chain", methods=["GET"])
def get_options_chain():
    """
    Fetches full live option chain matrix (Calls & Puts) for specified underlying and expiry.
    """
    try:
        client = get_client(require_auth=False)
        underlying = request.args.get("underlying", "BTC").strip().upper()
        if underlying not in ["BTC", "ETH"]:
            return jsonify({
                "success": False,
                "error": f"Option contracts are only available for BTC and ETH on Delta Exchange. '{underlying}' has no option contracts.",
                "underlying": underlying,
                "chain": [],
                "expiries": []
            }), 400

        expiry = request.args.get("expiry", None)
        data = get_option_chain_data(underlying=underlying, expiry=expiry, client=client)

        # Enrich with WebSocket cache quotes if available without overwriting non-null data
        ws_mgr = get_ws_manager()
        if ws_mgr and data.get("chain"):
            symbols_to_sub = []
            for row in data["chain"]:
                for side_key in ("call", "put"):
                    contract = row.get(side_key)
                    if contract and contract.get("symbol"):
                        sym = contract["symbol"]
                        symbols_to_sub.append(sym)
                        q = ws_mgr.get_latest_quote(sym)
                        if q:
                            if q.get("ltp") is not None and float(q.get("ltp") or 0) > 0:
                                contract["ltp"] = float(q["ltp"])
                            if q.get("mark_price") is not None and float(q.get("mark_price") or 0) > 0:
                                contract["mark_price"] = float(q["mark_price"])
                            if q.get("bid") is not None and float(q.get("bid") or 0) > 0:
                                contract["best_bid"] = float(q["bid"])
                            if q.get("ask") is not None and float(q.get("ask") or 0) > 0:
                                contract["best_ask"] = float(q["ask"])
                            if q.get("volume") is not None and float(q.get("volume") or 0) > 0:
                                contract["volume"] = float(q["volume"])
                            if q.get("oi") is not None and float(q.get("oi") or 0) > 0:
                                contract["open_interest"] = float(q["oi"])

            if hasattr(ws_mgr, "subscribe_symbols") and symbols_to_sub:
                ws_mgr.subscribe_symbols(symbols_to_sub)

        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/strikes", methods=["GET"])
def get_market_making_strikes():
    """
    Fetches live option chain, applies Market Making Strike Selection Criteria
    (Spread, Mid, Net Edge, Net Edge %, Spread Ticks), and returns qualified strikes.
    Supports underlying="ALL", "BTC", or "ETH".
    """
    try:
        underlying = request.args.get("underlying", "ALL").strip().upper()
        if underlying not in ["ALL", "BTC", "ETH"]:
            return jsonify({
                "success": False,
                "error": f"Option contracts are only available for ALL, BTC and ETH on Delta Exchange. '{underlying}' has no option contracts.",
                "underlying": underlying,
                "all_strikes": [],
                "qualified_strikes": [],
                "expiries": []
            }), 400

        expiry = request.args.get("expiry", None)

        def _parse_float(val, fallback=None):
            try:
                return float(val) if val is not None and str(val).strip() != "" else fallback
            except (ValueError, TypeError):
                return fallback

        min_bid = _parse_float(request.args.get("min_bid"))
        min_ticks = _parse_float(request.args.get("min_ticks"))
        min_edge = _parse_float(request.args.get("min_edge"))

        client = get_client(require_auth=False)

        if underlying == "ALL":
            btc_data = evaluate_mm_strikes(
                underlying="BTC",
                expiry=expiry,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge,
                client=client,
                check_alerts=False
            )
            eth_data = evaluate_mm_strikes(
                underlying="ETH",
                expiry=expiry,
                min_bid_price=min_bid,
                min_spread_ticks=min_ticks,
                min_net_edge_pct=min_edge,
                client=client,
                check_alerts=False
            )

            all_strikes = (btc_data.get("all_strikes") or []) + (eth_data.get("all_strikes") or [])
            qualified_strikes = (btc_data.get("qualified_strikes") or []) + (eth_data.get("qualified_strikes") or [])
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

            btc_sum = btc_data.get("summary") or {}
            eth_sum = eth_data.get("summary") or {}
            summary = {
                "total_scanned": btc_sum.get("total_scanned", 0) + eth_sum.get("total_scanned", 0),
                "total_qualified": len(qualified_strikes),
                "max_spread_ticks": max(btc_sum.get("max_spread_ticks", 0), eth_sum.get("max_spread_ticks", 0)),
                "max_net_edge_pct": max(btc_sum.get("max_net_edge_pct", 0), eth_sum.get("max_net_edge_pct", 0)),
            }

            merged_expiries = []
            seen_exp = {}
            for exp in (btc_data.get("expiries") or []) + (eth_data.get("expiries") or []):
                e_val = exp.get("expiry")
                if e_val:
                    if e_val not in seen_exp:
                        exp_copy = dict(exp)
                        seen_exp[e_val] = exp_copy
                        merged_expiries.append(exp_copy)
                    else:
                        seen_exp[e_val]["total_strikes"] = seen_exp[e_val].get("total_strikes", 0) + exp.get("total_strikes", 0)

            data = {
                "success": True,
                "underlying": "ALL",
                "selected_expiry": btc_data.get("selected_expiry") or eth_data.get("selected_expiry"),
                "spot_price": btc_data.get("spot_price", 0.0),
                "btc_spot": btc_data.get("spot_price", 0.0),
                "eth_spot": eth_data.get("spot_price", 0.0),
                "criteria": btc_data.get("criteria"),
                "summary": summary,
                "qualified_strikes": qualified_strikes,
                "all_strikes": all_strikes,
                "expiries": merged_expiries,
                "timestamp": time.time()
            }
            return jsonify(data)

        data = evaluate_mm_strikes(
            underlying=underlying,
            expiry=expiry,
            min_bid_price=min_bid,
            min_spread_ticks=min_ticks,
            min_net_edge_pct=min_edge,
            client=client,
            check_alerts=False
        )
        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/config", methods=["GET"])
def get_market_making_config():
    """
    Returns default Market Making criteria from .env configuration.
    """
    try:
        cfg = get_mm_config()
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# TELEGRAM NOTIFICATIONS CONFIGURATION API
# ==============================================================================

@app.route("/api/telegram/config", methods=["GET"])
def get_telegram_settings():
    """
    Returns current Telegram alert configuration and threshold.
    """
    try:
        cfg = get_telegram_config()
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/telegram/config", methods=["POST"])
def update_telegram_settings():
    """
    Updates Telegram alert threshold (seconds), enabled status, and credentials.
    """
    try:
        data = request.get_json() or {}
        bot_token = data.get("bot_token")
        chat_id = data.get("chat_id")
        notify_secs = data.get("notify_secs")
        enabled = data.get("enabled")

        updated = save_telegram_config(
            bot_token=bot_token if bot_token is not None else None,
            chat_id=chat_id if chat_id is not None else None,
            notify_secs=notify_secs,
            enabled=enabled
        )
        return jsonify({"success": True, "config": updated})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/telegram/test", methods=["POST"])
def test_telegram_settings():
    """
    Sends an instant verification message to Telegram chat.
    """
    try:
        data = request.get_json() or {}
        bot_token = data.get("bot_token")
        chat_id = data.get("chat_id")
        res = send_test_alert(bot_token=bot_token, chat_id=chat_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# USER PROFILE & AUTOMATION SETTINGS API
# ==============================================================================

@app.route("/api/settings/profile", methods=["GET"])
def get_profile_settings():
    """
    Returns user profile trading settings: BTC & ETH auto-trade lot sizes and Telegram settings.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)
        
        try:
            lot_btc = max(1, int(os.getenv("MM_LOT_SIZE_BTC", "1")))
        except (ValueError, TypeError):
            lot_btc = 1

        try:
            lot_eth = max(1, int(os.getenv("MM_LOT_SIZE_ETH", "1")))
        except (ValueError, TypeError):
            lot_eth = 1

        tg_cfg = get_telegram_config()
        
        return jsonify({
            "success": True,
            "lot_size_btc": lot_btc,
            "lot_size_eth": lot_eth,
            "telegram": tg_cfg
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/profile", methods=["POST"])
def update_profile_settings():
    """
    Updates user profile: saves BTC & ETH lot sizes and Telegram settings to .env.
    """
    try:
        from dotenv import set_key
        env_file = str(BASE_DIR / ".env")
        data = request.get_json() or {}
        
        lot_btc = data.get("lot_size_btc")
        lot_eth = data.get("lot_size_eth")
        
        if lot_btc is not None:
            try:
                val_btc = max(1, int(lot_btc))
                set_key(env_file, "MM_LOT_SIZE_BTC", str(val_btc))
                os.environ["MM_LOT_SIZE_BTC"] = str(val_btc)
            except (ValueError, TypeError):
                pass
                
        if lot_eth is not None:
            try:
                val_eth = max(1, int(lot_eth))
                set_key(env_file, "MM_LOT_SIZE_ETH", str(val_eth))
                os.environ["MM_LOT_SIZE_ETH"] = str(val_eth)
            except (ValueError, TypeError):
                pass

        # Update Telegram settings if provided
        tg_updated = save_telegram_config(
            bot_token=data.get("bot_token"),
            chat_id=data.get("chat_id"),
            notify_secs=data.get("notify_secs"),
            enabled=data.get("enabled")
        )

        try:
            cur_btc = max(1, int(os.getenv("MM_LOT_SIZE_BTC", "1")))
        except (ValueError, TypeError):
            cur_btc = 1

        try:
            cur_eth = max(1, int(os.getenv("MM_LOT_SIZE_ETH", "1")))
        except (ValueError, TypeError):
            cur_eth = 1

        return jsonify({
            "success": True,
            "lot_size_btc": cur_btc,
            "lot_size_eth": cur_eth,
            "telegram": tg_updated
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# ACCOUNT SWITCHING API (Main vs Sub Account)
# ==============================================================================

# Margin Mode Caching
_MARGIN_MODE_CACHE = {"mode": None, "account": None, "ts": 0}

def get_current_margin_mode(account=None, force_refresh=False):
    """
    Returns the active account's Delta Exchange margin mode (e.g. PORTFOLIO, ISOLATED).
    Uses a 30s TTL cache to avoid repetitive REST roundtrips.
    """
    global _MARGIN_MODE_CACHE
    creds = load_credentials()
    active_acct = account or creds.get("active_account", "main")
    now = time.time()

    if not force_refresh and _MARGIN_MODE_CACHE["mode"] and _MARGIN_MODE_CACHE["account"] == active_acct and (now - _MARGIN_MODE_CACHE["ts"] < 30):
        return _MARGIN_MODE_CACHE["mode"]

    try:
        client = get_client(account=active_acct)
        prof = client.request("GET", "/v2/profile", auth=True)
        if prof and prof.status_code == 200:
            p_data = prof.json().get("result", {})
            mode = str(p_data.get("margin_mode") or "PORTFOLIO").upper()
            _MARGIN_MODE_CACHE = {"mode": mode, "account": active_acct, "ts": now}
            return mode
    except Exception as e:
        logger.warning(f"Error fetching profile margin mode: {e}")

    return _MARGIN_MODE_CACHE.get("mode") or "PORTFOLIO"


@app.route("/api/account/status", methods=["GET"])
def get_account_status():
    """
    Returns current active account ('main' or 'sub'), margin mode, masked API key, and wallet balances.
    """
    try:
        creds = load_credentials()
        active_acct = creds.get("active_account", "main")
        api_key = creds.get("api_key") or ""
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"

        client = get_client(account=active_acct, force_new=True)
        balances = []
        try:
            balances = client.get_all_wallet_balances() or []
        except Exception as b_err:
            print(f"[WARN] Failed to fetch balances for account status: {b_err}")

        usd_balance = 0.0
        for b in balances:
            if b.get("asset_symbol") == "USD" or b.get("asset_id") == 14:
                usd_balance = float(b.get("available_balance", 0) or 0)
                break

        margin_mode = get_current_margin_mode(active_acct)

        return jsonify({
            "success": True,
            "active_account": active_acct,
            "margin_mode": margin_mode,
            "api_key_masked": masked_key,
            "base_url": creds.get("base_url"),
            "usd_available": usd_balance,
            "balances": balances
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/account/switch", methods=["POST"])
def switch_active_account():
    """
    Switches active account between 'main' and 'sub' on the fly.
    """
    try:
        data = request.get_json() or {}
        target = data.get("account", "").strip().lower()
        if target not in ("main", "sub"):
            return jsonify({"success": False, "error": "Account must be 'main' or 'sub'."}), 400

        global _client, _MARGIN_MODE_CACHE
        _client = switch_account(target, update_env_file=True)
        _MARGIN_MODE_CACHE["ts"] = 0  # Invalidate cache on account switch

        return get_account_status()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/account/margin-mode", methods=["GET", "POST"])
def account_margin_mode():
    """
    Gets or switches the Delta Exchange account margin mode (portfolio vs isolated).
    """
    try:
        creds = load_credentials()
        active_acct = creds.get("active_account", "main")
        client = get_client(account=active_acct)

        if request.method == "POST":
            data = request.get_json() or {}
            target_mode = (data.get("margin_mode") or "").strip().lower()
            if target_mode not in ("portfolio", "isolated", "cross"):
                return jsonify({"success": False, "error": "Margin mode must be 'portfolio', 'cross', or 'isolated'."}), 400

            prof_res = client.request("GET", "/v2/profile", auth=True).json().get("result", {})
            sub_id = prof_res.get("id")

            try:
                res = client.change_margin_mode(target_mode, subaccount_user_id=sub_id)
                new_mode = target_mode.upper()
                global _MARGIN_MODE_CACHE
                _MARGIN_MODE_CACHE = {"mode": new_mode, "account": active_acct, "ts": time.time()}
                return jsonify({"success": True, "margin_mode": new_mode, "active_account": active_acct, "data": res})
            except Exception as m_err:
                err_msg = str(m_err)
                if "same_margin_mode" in err_msg:
                    new_mode = target_mode.upper()
                    _MARGIN_MODE_CACHE = {"mode": new_mode, "account": active_acct, "ts": time.time()}
                    return jsonify({"success": True, "margin_mode": new_mode, "active_account": active_acct, "message": f"Already set to {new_mode} margin mode."})
                return jsonify({"success": False, "error": err_msg}), 400
        else:
            mode = get_current_margin_mode(active_acct, force_refresh=True)
            return jsonify({"success": True, "margin_mode": mode, "active_account": active_acct})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# 1-LOT MARKET MAKING TRADE EXECUTION API
# ==============================================================================

@app.route("/api/mm/trade/start", methods=["POST"])
def start_market_making_trade():
    """
    Starts a 1-lot Market Making cycle for a qualified strike:
    - Step 1: Place Limit BUY (Maker / post-only)
    - Step 2: On BUY fill, place Limit SELL (Maker)
    - Constraint: STRICTLY NO STOP-LOSS, NO TIMEOUT -> EXIT COST-TO-COST OR WAIT
    """
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol")
        if not symbol:
            return jsonify({"success": False, "error": "Contract 'symbol' is required."}), 400

        size = int(data.get("size") or 1)
        entry_price = float(data.get("entry_price")) if data.get("entry_price") else None
        target_exit_price = float(data.get("target_exit_price")) if data.get("target_exit_price") else None
        two_way_peg = data.get("two_way_peg", True)
        min_spread_pct = float(data.get("min_spread_pct", 0.60)) if data.get("min_spread_pct") else 0.60
        min_spread_ticks = float(data.get("min_spread_ticks")) if data.get("min_spread_ticks") is not None and float(data.get("min_spread_ticks")) > 0 else None
        min_spread = float(data.get("min_spread")) if data.get("min_spread") is not None and float(data.get("min_spread")) > 0 else None
        max_chase_ticks = int(data.get("max_chase_ticks")) if data.get("max_chase_ticks") is not None and int(data.get("max_chase_ticks")) >= 0 else None
        account = data.get("account")

        cycle = mm_engine.start_trade(
            symbol=symbol,
            size=size,
            entry_price=entry_price,
            target_exit_price=target_exit_price,
            two_way_peg=two_way_peg,
            max_entry_price=max_entry_price,
            min_spread_pct=min_spread_pct,
            min_spread_ticks=min_spread_ticks,
            min_spread=min_spread,
            max_chase_ticks=max_chase_ticks,
            account=account
        )
        return jsonify({"success": True, "trade": cycle.to_dict()})
    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/trade/active", methods=["GET"])
def get_active_market_making_trades():
    """
    Returns list of all active MM trade cycles and their live state.
    """
    try:
        trades = mm_engine.list_active()
        return jsonify({"success": True, "trades": trades})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/trade/upload-price", methods=["POST"])
def upload_trade_limit_price():
    """
    Allows user to upload/update the limit price of an active order.
    """
    try:
        data = request.get_json() or {}
        trade_id = data.get("trade_id")
        price = data.get("price")
        side = data.get("side")
        if not trade_id or price is None:
            return jsonify({"success": False, "error": "'trade_id' and 'price' are required."}), 400

        res = mm_engine.upload_price(trade_id, float(price), side=side)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/trade/resume-peg", methods=["POST"])
def resume_trade_peg():
    """
    Allows user to resume dynamic two-way floating peg (clearing manual price override).
    """
    try:
        data = request.get_json() or {}
        trade_id = data.get("trade_id")
        side = data.get("side")
        if not trade_id:
            return jsonify({"success": False, "error": "'trade_id' is required."}), 400

        res = mm_engine.resume_peg(trade_id, side=side)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/trade/cancel", methods=["POST"])
def cancel_market_making_trade():
    """
    Cancels an active MM trade cycle and closes open positions at market order.
    """
    try:
        data = request.get_json() or {}
        trade_id = data.get("trade_id")
        if not trade_id:
            return jsonify({"success": False, "error": "'trade_id' is required."}), 400

        market_close = data.get("market_close", True)
        res = mm_engine.cancel_trade(trade_id, market_close=market_close)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mm/trade/cancel-all", methods=["POST"])
def cancel_all_market_making_trades():
    """
    Stops all active MM trade cycles and immediately closes any open positions at market order.
    """
    try:
        data = request.get_json() or {}
        market_close = data.get("market_close", True)
        res = mm_engine.cancel_all_trades(market_close=market_close)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def collect_all_mm_logs(trade_filter: str = "ALL", search_query: str = "", limit: int = 1000):
    """
    Collects, deduplicates, and chronologically orders MM trade logs from:
    1. Persistent logs/mm_trades.log
    2. Server console logs/python.log (harvesting any [MM-xxxxxx] events from past runs)
    3. Active & historical cycles from mm_engine and .mm_trades_state.json
    Returns: (list_of_log_objects, list_of_trades_metadata, total_count)
    """
    logs_map = {}
    trades_metadata = {}

    def _ingest(line, default_tid="", default_sym="", status=None):
        if not line:
            return
        raw = line.strip()
        dt_str = ""
        dt_m = re.match(r"^\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]\s*", raw)
        if dt_m:
            dt_str = dt_m.group(1)
            raw = raw[len(dt_m.group(0)):].strip()
        else:
            ts_m = re.match(r"^\[(\d{2}:\d{2}:\d{2})\]\s*", raw)
            if ts_m:
                dt_str = ts_m.group(1)
                raw = raw[len(ts_m.group(0)):].strip()

        tid = default_tid
        mm_m = re.match(r"^\[MM-([a-zA-Z0-9_-]+)\]\s*", raw)
        if mm_m:
            tid = mm_m.group(1)
            raw = raw[len(mm_m.group(0)):].strip()
        elif not tid:
            mm_any = re.search(r"\[MM-([a-zA-Z0-9_-]+)\]", raw)
            if mm_any:
                tid = mm_any.group(1)

        sym = default_sym
        sym_m = re.search(r"\b([CP]-[A-Z]+-[\d\.]+-[\d]+)\b", raw)
        if sym_m:
            sym = sym_m.group(1)
            raw = re.sub(r"\[" + re.escape(sym) + r"\]\s*", "", raw).strip()

        parts = []
        if dt_str:
            parts.append(f"[{dt_str}]")
        if tid:
            parts.append(f"[MM-{tid[-6:]}]")
        if sym:
            parts.append(f"[{sym}]")
        parts.append(raw)
        normalized_text = " ".join(parts)

        key = f"{dt_str}_{tid}_{raw}"
        if key in logs_map:
            if sym and not logs_map[key].get("symbol"):
                logs_map[key]["symbol"] = sym
            return

        if dt_str and len(dt_str) == 8:
            today_str = datetime.now().strftime("%Y-%m-%d")
            sort_val = f"{today_str} {dt_str}"
        else:
            sort_val = dt_str or "0000-00-00 00:00:00"

        logs_map[key] = {
            "trade_id": tid,
            "symbol": sym,
            "status": status or "history",
            "timestamp": dt_str,
            "sort_key": sort_val,
            "text": normalized_text
        }

    # 1. State file & memory
    try:
        with mm_engine._lock:
            for tid, cycle in mm_engine.active_cycles.items():
                trades_metadata[tid] = {"trade_id": tid, "symbol": cycle.symbol, "status": "active"}
                for l in cycle.logs:
                    _ingest(l, default_tid=tid, default_sym=cycle.symbol, status="active")
            for h in mm_engine.history_cycles:
                tid = h.get("trade_id", "")
                sym = h.get("symbol", "")
                if tid:
                    trades_metadata[tid] = {"trade_id": tid, "symbol": sym, "status": "history"}
                for l in h.get("logs", []):
                    _ingest(l, default_tid=tid, default_sym=sym, status="history")
    except Exception:
        pass

    try:
        state_file = BASE_DIR / ".mm_trades_state.json"
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8", errors="replace") as f:
                sdata = json.load(f)
            for h in sdata.get("history", []):
                tid = h.get("trade_id", "")
                sym = h.get("symbol", "")
                if tid and tid not in trades_metadata:
                    trades_metadata[tid] = {"trade_id": tid, "symbol": sym, "status": "history"}
                for l in h.get("logs", []):
                    _ingest(l, default_tid=tid, default_sym=sym, status="history")
    except Exception:
        pass

    # 2. logs/mm_trades.log (and rotated backup mm_trades.log.1)
    for mm_f in [BASE_DIR / "logs" / "mm_trades.log.1", BASE_DIR / "logs" / "mm_trades.log"]:
        if mm_f.exists():
            try:
                with open(mm_f, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        _ingest(line)
            except Exception:
                pass

    # 3. logs/python.log (and rotated backup python.log.1)
    for py_f in [BASE_DIR / "logs" / "python.log.1", BASE_DIR / "logs" / "python.log"]:
        if py_f.exists():
            try:
                with open(py_f, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "[MM-" in line:
                            _ingest(line)
            except Exception:
                pass

    # Populate any discovered trades into trades_metadata
    for item in logs_map.values():
        tid = item.get("trade_id")
        sym = item.get("symbol")
        if tid and tid not in trades_metadata:
            trades_metadata[tid] = {
                "trade_id": tid,
                "symbol": sym or "Option",
                "status": item.get("status", "history")
            }
        elif tid and sym and trades_metadata.get(tid, {}).get("symbol") in ["", "Option", None]:
            trades_metadata[tid]["symbol"] = sym

    # Fill in symbol for items if missing but in metadata
    for item in logs_map.values():
        tid = item.get("trade_id")
        if tid and not item.get("symbol") and tid in trades_metadata:
            item["symbol"] = trades_metadata[tid]["symbol"]
        sym = item.get("symbol")
        if sym and sym not in item["text"]:
            if tid and f"[MM-{tid[-6:]}]" in item["text"]:
                item["text"] = item["text"].replace(f"[MM-{tid[-6:]}]", f"[MM-{tid[-6:]}] [{sym}]")
            else:
                item["text"] = f"[{sym}] " + item["text"]

    # Convert to list and sort chronologically
    all_items = list(logs_map.values())
    all_items.sort(key=lambda x: x["sort_key"])

    total_count = len(all_items)

    # Filter by trade
    if trade_filter and trade_filter != "ALL":
        tf = trade_filter.lower().strip()
        all_items = [
            it for it in all_items
            if (it.get("trade_id", "").lower().endswith(tf) or tf in it.get("trade_id", "").lower())
        ]

    # Filter by search across text, symbol, trade_id, and status
    if search_query:
        sq = search_query.lower().strip()
        all_items = [
            it for it in all_items
            if (
                sq in (it.get("text") or "").lower()
                or sq in (it.get("symbol") or "").lower()
                or sq in (it.get("trade_id") or "").lower()
                or sq in (it.get("status") or "").lower()
            )
        ]

    # Limit
    if limit > 0 and len(all_items) > limit:
        all_items = all_items[-limit:]

    trades_list = list(trades_metadata.values())
    return all_items, trades_list, total_count


@app.route("/api/system/logs", methods=["GET"])
def get_system_logs():
    """
    Returns real-time and historical execution logs (MM trades, server console, and tunnel output).
    Supports high limit / full history, trade filtering, and text search across old logs.
    """
    try:
        source = request.args.get("source", "all").lower()
        limit_arg = request.args.get("limit", "1000")
        try:
            limit = int(limit_arg)
        except Exception:
            limit = 1000
        if limit < 0:
            limit = 0

        search_query = (request.args.get("search") or "").strip()
        trade_filter = (request.args.get("trade_id") or "ALL").strip()

        # 1. MM trade logs (active + historical across all runs)
        mm_items, trades_list, total_mm_count = collect_all_mm_logs(
            trade_filter=trade_filter,
            search_query=search_query,
            limit=limit
        )

        # 2. Server console logs (from logs/python.log or in-memory stdout wrapper)
        server_logs = []
        py_log_path = BASE_DIR / "logs" / "python.log"
        total_server_lines = 0
        if py_log_path.exists():
            try:
                with open(py_log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = [line.strip() for line in f if line.strip()]
                    total_server_lines = len(all_lines)
                    if search_query:
                        sq = search_query.lower()
                        all_lines = [l for l in all_lines if sq in l.lower()]
                    if limit > 0:
                        server_logs = all_lines[-limit:]
                    else:
                        server_logs = all_lines[-50000:]
            except Exception:
                pass

        if not server_logs and hasattr(sys.stdout, "get_lines"):
            all_lines = sys.stdout.get_lines(count=15000)
            total_server_lines = len(all_lines)
            if search_query:
                sq = search_query.lower()
                all_lines = [l for l in all_lines if sq in l.lower()]
            if limit > 0:
                server_logs = all_lines[-limit:]
            else:
                server_logs = all_lines

        # 3. Cloudflare tunnel logs
        tunnel_logs = []
        tunnel_path = BASE_DIR / "logs" / "tunnel.log"
        total_tunnel_lines = 0
        if tunnel_path.exists():
            try:
                with open(tunnel_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = [line.strip() for line in f if line.strip()]
                    total_tunnel_lines = len(all_lines)
                    if search_query:
                        sq = search_query.lower()
                        all_lines = [l for l in all_lines if sq in l.lower()]
                    if limit > 0:
                        tunnel_logs = all_lines[-limit:]
                    else:
                        tunnel_logs = all_lines[-10000:]
            except Exception:
                pass

        tunnel_url = None
        url_file = BASE_DIR / "logs" / "latest_tunnel_url.txt"
        if url_file.exists():
            try:
                tunnel_url = url_file.read_text().strip()
            except Exception:
                pass

        safe_mm_items = [
            {
                "trade_id": _safe_str(it.get("trade_id")),
                "symbol": _safe_str(it.get("symbol")),
                "status": _safe_str(it.get("status")),
                "timestamp": _safe_str(it.get("timestamp")),
                "text": _safe_str(it.get("text"))
            }
            for it in mm_items
        ]
        safe_trades_list = [
            {
                "trade_id": _safe_str(t.get("trade_id")),
                "symbol": _safe_str(t.get("symbol")),
                "status": _safe_str(t.get("status"))
            }
            for t in trades_list
        ]
        safe_server_logs = [_safe_str(x) for x in server_logs]
        safe_tunnel_logs = [_safe_str(x) for x in tunnel_logs]

        return jsonify({
            "success": True,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mm_logs": safe_mm_items,
            "trades_list": safe_trades_list,
            "total_mm_count": total_mm_count,
            "server_logs": safe_server_logs,
            "total_server_count": total_server_lines,
            "tunnel_logs": safe_tunnel_logs,
            "total_tunnel_count": total_tunnel_lines,
            "tunnel_url": _safe_str(tunnel_url) if tunnel_url else None
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/system/logs/download", methods=["GET"])
def download_system_logs():
    """
    Downloads raw log file as attachment (mm_trades.log, python.log, or tunnel.log).
    """
    try:
        source = request.args.get("source", "mm").lower()
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

        if source in ("mm", "all"):
            items, _, _ = collect_all_mm_logs(trade_filter="ALL", search_query="", limit=0)
            content = "\n".join([item["text"] for item in items])
            filename = f"delta_mm_execution_logs_{now_str}.log"
            return Response(
                content,
                mimetype="text/plain; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        elif source == "server":
            py_log = BASE_DIR / "logs" / "python.log"
            filename = f"delta_server_console_{now_str}.log"
            if py_log.exists():
                return send_file(py_log, as_attachment=True, download_name=filename, mimetype="text/plain")
            elif hasattr(sys.stdout, "get_lines"):
                content = "\n".join(sys.stdout.get_lines(count=15000))
                return Response(
                    content,
                    mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"}
                )
            return Response("No server console logs available.", mimetype="text/plain")

        elif source == "tunnel":
            tunnel_log = BASE_DIR / "logs" / "tunnel.log"
            filename = f"delta_cloudflare_tunnel_{now_str}.log"
            if tunnel_log.exists():
                return send_file(tunnel_log, as_attachment=True, download_name=filename, mimetype="text/plain")
            return Response("No tunnel logs available.", mimetype="text/plain")

        return jsonify({"success": False, "error": "Invalid log source"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/system/logs/stream", methods=["GET"])
def stream_system_logs():
    """
    Real-Time Server-Sent Events (SSE) log stream.
    Delivers sub-millisecond execution logs to the UI without HTTP polling overhead.
    """
    source = request.args.get("source", "all").lower()
    trade_filter = (request.args.get("trade_id") or "ALL").strip()
    search_query = (request.args.get("search") or "").strip().lower()

    q = log_broadcaster.subscribe()

    def generate():
        # Handshake event to confirm SSE connection
        handshake_payload = json.dumps({
            "status": "connected",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        yield f"event: handshake\ndata: {handshake_payload}\n\n"
        try:
            while True:
                try:
                    item = q.get(timeout=2.0)
                except queue.Empty:
                    # Keep-alive heartbeat comment every 2 seconds
                    yield ": heartbeat\n\n"
                    continue

                item_is_mm = item.get("is_mm", False)
                item_is_tunnel = item.get("is_tunnel", False)

                # Tab source filtering
                if source == "mm" and not item_is_mm:
                    continue
                elif source == "server" and (item_is_mm or item_is_tunnel):
                    continue
                elif source == "tunnel" and not item_is_tunnel:
                    continue

                # Trade ID filtering for MM
                if item_is_mm and trade_filter != "ALL":
                    tf = trade_filter.lower()
                    it_tid = item.get("trade_id", "").lower()
                    if not (it_tid.endswith(tf) or tf.endswith(it_tid) or tf in it_tid):
                        continue

                # Text search filtering across text, symbol, trade_id, and status
                if search_query:
                    txt = (item.get("text") or "").lower()
                    sym = (item.get("symbol") or "").lower()
                    tid = (item.get("trade_id") or "").lower()
                    st = (item.get("status") or "").lower()
                    if search_query not in txt and search_query not in sym and search_query not in tid and search_query not in st:
                        continue

                yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            pass
        finally:
            log_broadcaster.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


@app.route("/api/ws/private/status", methods=["GET"])
def get_private_ws_status():
    """
    Returns the real-time private WebSocket stream status (connection, auth, active listeners).
    """
    try:
        account = request.args.get("account")
        from websocket import get_private_ws_manager
        mgr = get_private_ws_manager(account=account)
        return jsonify({"success": True, "status": mgr.get_status()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/multi-leg", methods=["POST"])
def place_multi_leg():
    """
    Executes all legs of a multi-leg strategy simultaneously in 1 click.
    """
    try:
        data = request.get_json() or {}
        legs = data.get("legs", [])

        if not legs or not isinstance(legs, list):
            return jsonify({"success": False, "error": "No trading legs provided."}), 400

        client = get_client()
        result = execute_multi_leg_strategy(legs, client=client, parallel=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/single", methods=["POST"])
def place_single_order():
    """
    Executes a single manual order (Market / Limit).
    """
    try:
        data = request.get_json() or {}
        client = get_client()
        result = execute_single_leg_order(data, client=client)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/square-off", methods=["POST"])
def square_off_pos():
    """
    Squares off a single open position at market.
    """
    try:
        data = request.get_json() or {}
        identifier = data.get("product_id") or data.get("symbol")
        if not identifier:
            return jsonify({"success": False, "error": "Product ID or Symbol required."}), 400

        client = get_client()
        res = square_off_single_position(identifier, client=client)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/square-off-all", methods=["POST"])
def square_off_all():
    """
    Squares off ALL currently open positions at market in 1 click.
    """
    try:
        client = get_client()
        res = square_off_all_open_positions(client=client)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"==================================================", flush=True)
    print(f" Delta Exchange Open Positions & Multi-Leg Server", flush=True)
    print(f" Local Web UI:    http://127.0.0.1:{port}", flush=True)
    print(f" Wi-Fi LAN UI:    http://10.90.1.60:{port}", flush=True)
    print(f"==================================================", flush=True)
    # Start server WebSocket stream manager
    get_ws_manager()
    app.run(host="0.0.0.0", port=port, debug=False)
