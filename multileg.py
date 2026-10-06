"""
Multi-Leg & 1-Click Trading Engine for Delta Exchange.
Handles concurrent parallel order dispatch for options and futures strategies
(Straddles, Strangles, Spreads, Iron Condors, and Custom Multi-Leg Baskets).
"""

import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure python-rest-client is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_PATH = os.path.join(BASE_DIR, "python-rest-client")
if REPO_PATH not in sys.path:
    sys.path.insert(0, REPO_PATH)

from delta_rest_client import DeltaRestClient, OrderType, TimeInForce, round_by_tick_size
from login import get_delta_client

import json
import threading

_PRODUCT_CACHE = {}
_ALL_PRODUCTS_CACHE = []
_LAST_PRODUCTS_FETCH = 0
_MASTER_LOCK = threading.Lock()
_MASTER_CACHE_FILE = os.path.join(BASE_DIR, "master_products.json")
_AUTO_SYNC_THREAD = None
_AUTO_SYNC_RUNNING = False
_MASTER_STATUS = {
    "last_sync_timestamp": None,
    "last_sync_iso": None,
    "total_products": 0,
    "option_products": 0,
    "futures_products": 0,
    "status": "uninitialized",
    "error": None
}


def get_client() -> DeltaRestClient:
    """Returns an authenticated DeltaRestClient instance."""
    return get_delta_client()


def load_master_from_disk():
    """Loads master scrips from local JSON cache if available."""
    global _ALL_PRODUCTS_CACHE, _PRODUCT_CACHE, _LAST_PRODUCTS_FETCH, _MASTER_STATUS
    if os.path.exists(_MASTER_CACHE_FILE):
        try:
            with open(_MASTER_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                products = data.get("products", [])
                if products:
                    _ALL_PRODUCTS_CACHE = products
                    _LAST_PRODUCTS_FETCH = data.get("timestamp", time.time())
                    _MASTER_STATUS["last_sync_timestamp"] = _LAST_PRODUCTS_FETCH
                    _MASTER_STATUS["last_sync_iso"] = datetime.fromtimestamp(_LAST_PRODUCTS_FETCH, tz=timezone.utc).isoformat()
                    _MASTER_STATUS["total_products"] = len(products)
                    _MASTER_STATUS["option_products"] = sum(1 for p in products if "option" in (p.get("contract_type") or "").lower())
                    _MASTER_STATUS["futures_products"] = _MASTER_STATUS["total_products"] - _MASTER_STATUS["option_products"]
                    _MASTER_STATUS["status"] = "loaded_from_disk"
                    for p in products:
                        p_sym = p.get("symbol")
                        p_id = p.get("id")
                        if p_sym:
                            _PRODUCT_CACHE[p_sym] = p
                        if p_id is not None:
                            _PRODUCT_CACHE[p_id] = p
                            _PRODUCT_CACHE[str(p_id)] = p
        except Exception as e:
            pass


def save_master_to_disk(products: list):
    """Saves master scrips to local JSON cache for offline/instant availability."""
    try:
        now = time.time()
        payload = {
            "timestamp": now,
            "iso_time": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "total_count": len(products),
            "products": products
        }
        temp_file = _MASTER_CACHE_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        if os.path.exists(_MASTER_CACHE_FILE):
            os.remove(_MASTER_CACHE_FILE)
        os.rename(temp_file, _MASTER_CACHE_FILE)
    except Exception as e:
        pass


def sync_master_scrips(client: DeltaRestClient = None, force=False) -> dict:
    """
    Downloads the freshest master scrips from Delta Exchange API.
    Updates in-memory caches and persists to disk.
    """
    global _ALL_PRODUCTS_CACHE, _PRODUCT_CACHE, _LAST_PRODUCTS_FETCH, _MASTER_STATUS
    with _MASTER_LOCK:
        now = time.time()
        if not force and _ALL_PRODUCTS_CACHE and (now - _LAST_PRODUCTS_FETCH < 60):
            return {
                "success": True,
                "cached": True,
                **_MASTER_STATUS
            }

        c = client or get_client()
        try:
            products = c.get_products()
            if isinstance(products, list) and len(products) > 0:
                _ALL_PRODUCTS_CACHE = products
                _LAST_PRODUCTS_FETCH = now
                _PRODUCT_CACHE.clear()
                for p in products:
                    p_sym = p.get("symbol")
                    p_id = p.get("id")
                    if p_sym:
                        _PRODUCT_CACHE[p_sym] = p
                    if p_id is not None:
                        _PRODUCT_CACHE[p_id] = p
                        _PRODUCT_CACHE[str(p_id)] = p

                opt_count = sum(1 for p in products if "option" in (p.get("contract_type") or "").lower())
                _MASTER_STATUS = {
                    "last_sync_timestamp": now,
                    "last_sync_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                    "total_products": len(products),
                    "option_products": opt_count,
                    "futures_products": len(products) - opt_count,
                    "status": "synced_from_exchange",
                    "error": None
                }
                save_master_to_disk(products)
                return {
                    "success": True,
                    "cached": False,
                    **_MASTER_STATUS
                }
            else:
                raise ValueError("Exchange returned empty product list.")
        except Exception as e:
            _MASTER_STATUS["error"] = str(e)
            _MASTER_STATUS["status"] = "sync_failed"
            return {
                "success": False,
                "error": str(e),
                **_MASTER_STATUS
            }


def get_master_status() -> dict:
    """Returns the current master scrips status and metrics."""
    return {
        "success": True,
        **_MASTER_STATUS
    }


def _background_master_sync_loop(interval_seconds=900):
    """
    Intelligent background daemon for master scrip synchronization:
    - Normal schedule: Runs every 15 minutes (avoids rate limits and excessive bandwidth).
    - Daily Rollover Window (11:58 UTC - 12:15 UTC / 5:28 PM - 5:45 PM IST):
      Increases polling to every 45 seconds to instantly capture new daily/weekly contracts upon release.
    """
    global _AUTO_SYNC_RUNNING
    while _AUTO_SYNC_RUNNING:
        try:
            sync_master_scrips(force=True)
        except Exception:
            pass

        # Check if we are currently in the 12:00 UTC (5:30 PM IST) daily contract rollover window
        now_utc = datetime.now(timezone.utc)
        is_rollover_window = (now_utc.hour == 11 and now_utc.minute >= 58) or (now_utc.hour == 12 and now_utc.minute <= 15)
        
        sleep_time = 45 if is_rollover_window else interval_seconds
        time.sleep(sleep_time)


def start_master_sync_daemon(interval_seconds=900):
    """Starts the background auto-sync thread for master scrips with adaptive timing."""
    global _AUTO_SYNC_THREAD, _AUTO_SYNC_RUNNING
    if _AUTO_SYNC_RUNNING:
        return
    _AUTO_SYNC_RUNNING = True
    load_master_from_disk()
    # Initial sync in background
    _AUTO_SYNC_THREAD = threading.Thread(target=_background_master_sync_loop, args=(interval_seconds,), daemon=True)
    _AUTO_SYNC_THREAD.start()


def get_all_products(client: DeltaRestClient = None, force_refresh=False) -> list:
    """
    Fetches and caches all available trading products from Delta Exchange.
    """
    global _ALL_PRODUCTS_CACHE, _LAST_PRODUCTS_FETCH
    now = time.time()
    if force_refresh or not _ALL_PRODUCTS_CACHE or (now - _LAST_PRODUCTS_FETCH > 60):
        sync_master_scrips(client, force=force_refresh)
    return _ALL_PRODUCTS_CACHE


def get_product_details(symbol_or_id="BTCUSD", client: DeltaRestClient = None) -> dict:
    """
    Fetches and caches product specifications.
    """
    global _PRODUCT_CACHE
    if symbol_or_id in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[symbol_or_id]

    get_all_products(client, force_refresh=True)

    if symbol_or_id in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[symbol_or_id]

    raise ValueError(f"Product '{symbol_or_id}' not found on Delta Exchange.")


def align_price(price: float, tick_size: float | str) -> float:
    """Rounds a price to match the product's valid tick size."""
    try:
        tick = float(tick_size)
        if tick <= 0:
            return round(price, 4)
        return float(round_by_tick_size(Decimal(str(price)), Decimal(str(tick))))
    except Exception:
        return round(price, 4)


def execute_single_leg_order(leg: dict, client: DeltaRestClient = None) -> dict:
    """
    Executes a single leg order against Delta Exchange REST API.
    :param leg: dict {symbol, side, size, order_type, limit_price, ...}
    :return: standardized result dictionary
    """
    c = client or get_client()
    leg_idx = leg.get("leg_index", 1)
    symbol = leg.get("symbol", "").strip()
    product_id = leg.get("product_id")
    side = str(leg.get("side", "buy")).lower()
    order_type_str = str(leg.get("order_type", "market")).lower()

    try:
        size = int(leg.get("size", 1))
    except (ValueError, TypeError):
        size = 1

    if size <= 0:
        return {
            "leg_index": leg_idx,
            "symbol": symbol,
            "product_id": product_id,
            "side": side.upper(),
            "size": size,
            "success": False,
            "error": "Order size must be greater than 0"
        }

    try:
        # Resolve product specifications
        if not product_id and symbol:
            prod = get_product_details(symbol, c)
            product_id = prod["id"]
        elif product_id and not symbol:
            prod = get_product_details(product_id, c)
            symbol = prod.get("symbol", f"Product #{product_id}")
        elif product_id:
            prod = get_product_details(product_id, c)
        else:
            raise ValueError("Either symbol or product_id must be provided for the leg.")

        tick_size = prod.get("tick_size", "0.5")

        if order_type_str == "limit":
            limit_price = leg.get("limit_price")
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("Limit price is required for Limit orders.")
            valid_price = align_price(float(limit_price), tick_size)

            tif_val = leg.get("time_in_force", "gtc").lower()
            tif = TimeInForce.GTC
            if tif_val == "ioc":
                tif = TimeInForce.IOC
            elif tif_val == "fok":
                tif = TimeInForce.FOK

            res = c.place_order(
                product_id=product_id,
                size=size,
                side=side,
                limit_price=str(valid_price),
                order_type=OrderType.LIMIT,
                time_in_force=tif,
                post_only="true" if leg.get("post_only") else "false"
            )
        else:
            # Market order
            res = c.place_order(
                product_id=product_id,
                size=size,
                side=side,
                order_type=OrderType.MARKET
            )

        order_id = res.get("id") if isinstance(res, dict) else None
        state = res.get("state") if isinstance(res, dict) else "placed"

        return {
            "leg_index": leg_idx,
            "symbol": symbol,
            "product_id": product_id,
            "side": side.upper(),
            "size": size,
            "order_type": order_type_str.upper(),
            "limit_price": leg.get("limit_price"),
            "success": True,
            "order_id": order_id,
            "state": state,
            "response": res,
            "error": None
        }

    except Exception as e:
        return {
            "leg_index": leg_idx,
            "symbol": symbol,
            "product_id": product_id,
            "side": side.upper(),
            "size": size,
            "order_type": order_type_str.upper(),
            "limit_price": leg.get("limit_price"),
            "success": False,
            "order_id": None,
            "response": None,
            "error": str(e)
        }


def execute_multi_leg_strategy(legs: list, client: DeltaRestClient = None, parallel: bool = True) -> dict:
    """
    Executes multiple trading legs simultaneously with 1-click execution.
    Uses ThreadPoolExecutor for concurrent execution to minimize slippage.
    """
    if not legs or not isinstance(legs, list):
        return {
            "success": False,
            "error": "No trading legs provided.",
            "total_legs": 0,
            "results": []
        }

    c = client or get_client()

    for idx, leg in enumerate(legs):
        leg["leg_index"] = idx + 1

    results = [None] * len(legs)

    if parallel and len(legs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(legs), 8)) as executor:
            future_to_idx = {
                executor.submit(execute_single_leg_order, leg, c): idx
                for idx, leg in enumerate(legs)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    results[idx] = res
                except Exception as exc:
                    results[idx] = {
                        "leg_index": idx + 1,
                        "symbol": legs[idx].get("symbol", ""),
                        "success": False,
                        "error": str(exc)
                    }
    else:
        for idx, leg in enumerate(legs):
            results[idx] = execute_single_leg_order(leg, c)

    success_count = sum(1 for r in results if r and r.get("success"))
    failed_count = len(results) - success_count

    if failed_count == 0:
        overall_status = "SUCCESS"
    elif success_count > 0:
        overall_status = "PARTIAL"
    else:
        overall_status = "FAILED"

    return {
        "success": success_count > 0,
        "overall_status": overall_status,
        "total_legs": len(legs),
        "successful_legs": success_count,
        "failed_legs": failed_count,
        "results": results
    }


def square_off_single_position(product_id_or_symbol, client: DeltaRestClient = None) -> dict:
    """
    Squares off a single open position at market.
    """
    c = client or get_client()
    prod = get_product_details(product_id_or_symbol, c)
    product_id = prod["id"]
    symbol = prod.get("symbol", str(product_id))

    # Fetch position
    pos_res = c.get_position(product_id=product_id)
    if not pos_res or float(pos_res.get("size", 0)) == 0:
        all_pos = c.request("GET", "/v2/positions/margined", auth=True).json().get("result", [])
        pos_res = next((p for p in all_pos if str(p.get("product_id")) == str(product_id)), None)

    if not pos_res:
        return {"success": False, "error": f"No open position found for {symbol} (Product {product_id})"}

    try:
        size = float(pos_res.get("size", 0))
    except (ValueError, TypeError):
        size = 0.0

    if size == 0:
        return {"success": False, "error": f"Position for {symbol} is already 0 (flat)."}

    opposing_side = "sell" if size > 0 else "buy"
    close_size = int(abs(size))

    try:
        res = c.place_order(
            product_id=product_id,
            size=close_size,
            side=opposing_side,
            order_type=OrderType.MARKET
        )
        return {
            "success": True,
            "symbol": symbol,
            "product_id": product_id,
            "closed_size": close_size,
            "side": opposing_side.upper(),
            "response": res
        }
    except Exception as e:
        return {
            "success": False,
            "symbol": symbol,
            "product_id": product_id,
            "error": str(e)
        }


def square_off_all_open_positions(client: DeltaRestClient = None) -> dict:
    """
    Squares off ALL currently open positions simultaneously at market in 1 click.
    """
    c = client or get_client()
    try:
        pos_res = c.request("GET", "/v2/positions/margined", auth=True).json()
        raw_positions = pos_res.get("result", [])
        if isinstance(raw_positions, dict):
            raw_positions = [raw_positions]
        elif not isinstance(raw_positions, list):
            raw_positions = []
    except Exception as e:
        return {"success": False, "error": f"Failed to retrieve positions: {e}", "results": []}

    open_positions = [p for p in raw_positions if float(p.get("size", 0)) != 0]

    if not open_positions:
        return {
            "success": True,
            "message": "No open positions to square off.",
            "total_squared_off": 0,
            "results": []
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(len(open_positions), 8)) as executor:
        future_to_pos = {
            executor.submit(square_off_single_position, p.get("product_id"), c): p
            for p in open_positions
        }
        for future in as_completed(future_to_pos):
            try:
                res = future.result()
                results.append(res)
            except Exception as exc:
                p = future_to_pos[future]
                results.append({
                    "success": False,
                    "product_id": p.get("product_id"),
                    "symbol": p.get("product_symbol"),
                    "error": str(exc)
                })

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": success_count > 0,
        "total_attempted": len(open_positions),
        "total_squared_off": success_count,
        "failed_count": len(open_positions) - success_count,
        "results": results
    }


def get_options_expiries_and_strikes(underlying="BTC", client: DeltaRestClient = None) -> dict:
    """
    Fetches available expiration dates, strikes, contract value, and live underlying prices for an asset.
    """
    c = client or get_client()
    all_prods = get_all_products(c)

    # Fetch underlying ticker for spot & mark price & contract value
    spot_price = 0.0
    mark_price = 0.0
    ltp = 0.0
    contract_value = 0.001 if underlying == "BTC" else (0.01 if underlying == "ETH" else 1.0)

    try:
        ticker = c.get_ticker(f"{underlying}USD")
        if ticker:
            spot_price = float(ticker.get("spot_price") or 0.0)
            mark_price = float(ticker.get("mark_price") or 0.0)
            ltp = float(ticker.get("close") or 0.0)
            if ticker.get("contract_value"):
                contract_value = float(ticker.get("contract_value"))
    except Exception as e:
        print(f"[WARN] Failed to fetch ticker for {underlying}USD: {e}")

    now_utc = datetime.now(timezone.utc)
    expiries_set = set()
    strikes_map = {}

    for p in all_prods:
        # Only live active contracts
        state = p.get("state", "live")
        if state not in ["live", "active"] and state is not None:
            continue

        c_type = (p.get("contract_type") or "").lower()
        if "call" not in c_type and "put" not in c_type:
            continue

        u_asset = (p.get("underlying_asset") or {}).get("symbol", "").upper()
        sym = p.get("symbol", "").upper()
        if underlying not in u_asset and f"-{underlying}-" not in sym:
            continue

        settlement = p.get("settlement_time")
        if settlement:
            try:
                clean_settle = str(settlement).replace("Z", "+00:00")
                settle_dt = datetime.fromisoformat(clean_settle)
                if settle_dt.tzinfo is None:
                    settle_dt = settle_dt.replace(tzinfo=timezone.utc)
                if settle_dt <= now_utc:
                    continue
            except Exception:
                pass

        strike = float(p.get("strike_price") or 0) if p.get("strike_price") else None

        if p.get("contract_value"):
            try:
                contract_value = float(p.get("contract_value"))
            except Exception:
                pass

        parts = sym.split("-")
        exp_str = None
        if len(parts) >= 4:
            exp_str = parts[-1]
        elif settlement:
            exp_str = str(settlement)

        if exp_str:
            # If exp_str is DDMMYY format (e.g. 021026), check if expired at 12:00 UTC
            if len(exp_str) == 6 and exp_str.isdigit():
                try:
                    exp_dt = datetime.strptime(exp_str, "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    if exp_dt <= now_utc:
                        continue
                except Exception:
                    pass

            expiries_set.add(exp_str)
            if exp_str not in strikes_map:
                strikes_map[exp_str] = set()
            if strike:
                strikes_map[exp_str].add(strike)

    def parse_expiry_date(exp):
        try:
            return datetime.strptime(exp, "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
        except Exception:
            try:
                clean_exp = str(exp).replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_exp)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)

    def format_expiry_label(exp):
        try:
            dt = datetime.strptime(exp, "%d%m%y")
            return dt.strftime("%d %b %Y")
        except Exception:
            return exp

    # Sort expiries chronologically by date
    sorted_exp_list = sorted(list(expiries_set), key=parse_expiry_date)
    formatted_expiries = []

    underlying_price = spot_price if spot_price > 0 else (ltp if ltp > 0 else mark_price)
    futures_price = ltp if ltp > 0 else (mark_price if mark_price > 0 else spot_price)

    for exp in sorted_exp_list:
        strikes = sorted(list(strikes_map.get(exp, [])))
        if strikes:
            # Calculate specific ATM strike for THIS particular expiry date
            expiry_atm = None
            if underlying_price > 0:
                expiry_atm = min(strikes, key=lambda x: abs(x - underlying_price))
            else:
                expiry_atm = strikes[len(strikes) // 2]

            # Calculate approximate strike interval
            step = None
            if len(strikes) >= 2:
                step = strikes[1] - strikes[0]

            formatted_expiries.append({
                "expiry": exp,
                "label": format_expiry_label(exp),
                "strikes": strikes,
                "atm_strike": expiry_atm,
                "min_strike": min(strikes),
                "max_strike": max(strikes),
                "total_strikes": len(strikes),
                "strike_step": step
            })

    # Default ATM strike from first available expiry
    atm_strike = formatted_expiries[0]["atm_strike"] if formatted_expiries else None

    return {
        "underlying": underlying,
        "underlying_price": underlying_price,
        "spot_price": spot_price if spot_price > 0 else underlying_price,
        "futures_price": futures_price,
        "mark_price": mark_price,
        "ltp": ltp,
        "contract_value": contract_value,
        "asset_unit": underlying,
        "atm_strike": atm_strike,
        "expiries": formatted_expiries,
        "master_sync": get_master_status()
    }


def get_option_chain_data(underlying="BTC", expiry=None, client: DeltaRestClient = None) -> dict:
    """
    Fetches full live option chain matrix (Calls & Puts) for specified underlying asset and expiry.
    """
    c = client or get_client()
    exp_info = get_options_expiries_and_strikes(underlying=underlying, client=c)
    expiries = exp_info.get("expiries", [])

    selected_expiry = expiry
    if not selected_expiry and expiries:
        selected_expiry = expiries[0]["expiry"]

    spot_price = exp_info.get("spot_price", 0.0)
    atm_strike = exp_info.get("atm_strike", None)

    # Find matching expiry object
    exp_obj = next((e for e in expiries if e["expiry"] == selected_expiry), None)
    if not exp_obj and expiries:
        exp_obj = expiries[0]
        selected_expiry = exp_obj["expiry"]

    strikes = exp_obj["strikes"] if exp_obj else []
    expiry_atm = exp_obj["atm_strike"] if exp_obj else atm_strike

    all_prods = get_all_products(c)

    # Fetch live tickers across all option products for instantaneous quote matrix
    live_tickers = {}
    try:
        raw_tickers = c.get_tickers() or []
        for t in raw_tickers:
            if t and t.get("symbol"):
                live_tickers[t["symbol"].upper()] = t
    except Exception as e:
        print(f"[WARN] Failed to fetch bulk tickers in option chain: {e}")

    # Map products by (strike, call/put)
    chain_map = {}
    for s in strikes:
        chain_map[s] = {
            "strike": s,
            "is_atm": (s == expiry_atm),
            "call": None,
            "put": None
        }

    for p in all_prods:
        sym = (p.get("symbol") or "").upper()
        if not sym.endswith(f"-{selected_expiry}"):
            continue
        if f"-{underlying}-" not in sym:
            continue

        c_type = (p.get("contract_type") or "").lower()
        is_call = "call" in c_type or sym.startswith("C-")
        is_put = "put" in c_type or sym.startswith("P-")
        if not is_call and not is_put:
            continue

        strike = float(p.get("strike_price") or 0) if p.get("strike_price") else None
        if not strike or strike not in chain_map:
            continue

        t = live_tickers.get(sym) or {}
        quotes = t.get("quotes") or {}
        greeks = t.get("greeks") or {}

        close_val = t.get("close")
        mark_val = t.get("mark_price")
        best_bid = quotes.get("best_bid")
        best_ask = quotes.get("best_ask")
        vol_val = t.get("volume")
        oi_val = t.get("oi") or t.get("oi_value")

        ltp = float(close_val) if close_val is not None else None
        mark_price = float(mark_val) if mark_val is not None else 0.0
        bid_price = float(best_bid) if best_bid is not None else None
        ask_price = float(best_ask) if best_ask is not None else None
        volume = float(vol_val) if vol_val is not None else 0.0
        oi = float(oi_val) if oi_val is not None else 0.0

        prod_data = {
            "product_id": p.get("id"),
            "symbol": sym,
            "contract_type": "call" if is_call else "put",
            "strike": strike,
            "expiry": selected_expiry,
            "is_itm": (strike < spot_price) if is_call else (strike > spot_price),
            "tick_size": float(p.get("tick_size") or 0.1),
            "contract_value": float(p.get("contract_value") or exp_info.get("contract_value", 0.001)),
            "ltp": ltp,
            "mark_price": mark_price,
            "best_bid": bid_price,
            "best_ask": ask_price,
            "volume": volume,
            "open_interest": oi,
            "greeks": greeks
        }

        if is_call:
            chain_map[strike]["call"] = prod_data
        else:
            chain_map[strike]["put"] = prod_data

    rows = [chain_map[s] for s in sorted(strikes)]

    return {
        "success": True,
        "underlying": underlying,
        "selected_expiry": selected_expiry,
        "spot_price": spot_price,
        "atm_strike": expiry_atm,
        "contract_value": exp_info.get("contract_value", 0.001),
        "expiries": expiries,
        "chain": rows,
        "total_strikes": len(rows)
    }


