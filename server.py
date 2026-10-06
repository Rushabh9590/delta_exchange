"""
Delta Exchange Open Positions & Multi-Leg Trading Dashboard Server
Serves real-time positions, wallet balances, account metrics, and 1-click multi-leg execution.
"""

import os
import sys
import time
import logging
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

# Suppress Werkzeug / Flask HTTP request access logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Ensure project and python-rest-client paths are in sys.path
BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from login import get_delta_client, load_credentials
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
    start_master_sync_daemon
)
from websocket import DeltaWebSocketManager

# Start background master scrip auto-sync daemon (adaptive 15m default, 45s during 5:30 PM IST rollover window)
start_master_sync_daemon(interval_seconds=900)

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

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


def get_client():
    global _client
    if _client is None:
        _client = get_delta_client()
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
        client = get_client()

        # Positions
        pos_res = client.request("GET", "/v2/positions/margined", auth=True).json()
        raw_positions = pos_res.get("result", [])
        if isinstance(raw_positions, dict):
            raw_positions = [raw_positions]
        elif not isinstance(raw_positions, list):
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
        client = get_client()
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
        client = get_client()
        underlying = request.args.get("underlying", "BTC").upper()
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
        client = get_client()
        underlying = request.args.get("underlying", "BTC").upper()
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
