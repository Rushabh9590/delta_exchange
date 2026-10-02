"""
Delta Exchange Open Positions & Trading Dashboard Server
Serves real-time positions, wallet balances, and account metrics.
"""

import os
import sys
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

app = Flask(__name__, static_folder=str(BASE_DIR))

# Cached client instance
_client = None


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

    product = pos.get("product") or {}
    contract_val = float(product.get("contract_value") or 1.0)
    contract_type = product.get("contract_type", "unknown")
    symbol = pos.get("product_symbol") or product.get("symbol") or f"Product #{pos.get('product_id')}"

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
    # For Options & Futures, LTP gives the executable market PnL; Mark gives theoretical mark PnL
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

    # Notional value
    notional_usd = underlying_qty * active_price if active_price > 0 else cost_basis

    # Type categorization
    type_badge = "FUTURES"
    if "call" in contract_type.lower():
        type_badge = "CALL"
    elif "put" in contract_type.lower():
        type_badge = "PUT"
    elif "perpetual" in contract_type.lower() or "swap" in contract_type.lower():
        type_badge = "PERPETUAL"

    return {
        "product_id": pos.get("product_id"),
        "symbol": symbol,
        "description": product.get("description") or product.get("short_description") or symbol,
        "contract_type": contract_type,
        "type_badge": type_badge,
        "strike_price": product.get("strike_price"),
        "settlement_time": product.get("settlement_time"),
        "underlying_asset": (product.get("underlying_asset") or {}).get("symbol", "BTC"),
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
        "realized_pnl": realized_pnl,
        "margin_mode": pos.get("margin_mode", "portfolio"),
        "margin": float(pos.get("margin") or 0),
        "liquidation_price": pos.get("liquidation_price"),
        "bankruptcy_price": pos.get("bankruptcy_price"),
        "auto_topup": pos.get("auto_topup", False),
        "updated_at": pos.get("updated_at"),
        "created_at": pos.get("created_at"),
        "raw": pos
    }


@app.route("/")
def serve_index():
    """Serves the frontend index.html."""
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

        # Filter active open positions (size != 0)
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

            # Consider open if size is non-zero
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

        # Find USD and INR balances
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

        # Calculate live conversion rate
        usd_bal = wallet_summary.get("usd_balance", 0)
        inr_bal = wallet_summary.get("inr_balance", 0)
        usd_to_inr_rate = round(inr_bal / usd_bal, 2) if (usd_bal > 0 and inr_bal > 0) else 85.0

        return jsonify({
            "success": True,
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"==================================================")
    print(f" Delta Exchange Open Positions Dashboard")
    print(f" Server running at: http://127.0.0.1:{port}")
    print(f"==================================================")
    app.run(host="0.0.0.0", port=port, debug=False)
