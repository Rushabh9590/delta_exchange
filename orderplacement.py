"""
Order Placement Module for Delta Exchange.
Integrates:
  - python-rest-client: REST API client and order management
  - login.py: Authenticated session and environment credentials
  - websocket.py: Real-time market feed for price discovery & execution
"""

import sys
import os
import time
from decimal import Decimal

# Ensure dependencies from repo and parent are in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_PATH = os.path.join(BASE_DIR, "python-rest-client")
if REPO_PATH not in sys.path:
    sys.path.insert(0, REPO_PATH)

from delta_rest_client import (
    DeltaRestClient,
    OrderType,
    TimeInForce,
    create_order_format,
    cancel_order_format,
    round_by_tick_size
)
from login import get_delta_client, client as default_client
from websocket import DeltaWebSocketManager

# Cache product details for fast lookups
_PRODUCT_CACHE = {}


def get_client() -> DeltaRestClient:
    """Returns an authenticated DeltaRestClient instance."""
    return default_client or get_delta_client()


def get_product_details(symbol_or_id="BTCUSD", client: DeltaRestClient = None) -> dict:
    """
    Fetches and caches product specifications (id, symbol, tick_size, contract_value, etc.).
    """
    global _PRODUCT_CACHE
    if symbol_or_id in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[symbol_or_id]

    c = client or get_client()
    products = c.get_products()

    for p in products:
        p_sym = p.get("symbol")
        p_id = p.get("id")
        _PRODUCT_CACHE[p_sym] = p
        _PRODUCT_CACHE[p_id] = p
        _PRODUCT_CACHE[str(p_id)] = p

    if symbol_or_id in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[symbol_or_id]

    raise ValueError(f"Product '{symbol_or_id}' not found on Delta Exchange.")


def align_price(price: float, tick_size: float | str) -> float:
    """Rounds a price to match the product's valid tick size."""
    tick = float(tick_size)
    return float(round_by_tick_size(Decimal(str(price)), Decimal(str(tick))))


class DeltaOrderManager:
    """
    High-level order manager combining REST trading operations with real-time WebSocket feeds.
    """
    def __init__(self, client: DeltaRestClient = None, symbols=None):
        self.client = client or get_client()
        self.symbols = symbols or ["BTCUSD"]
        self.ws_manager = DeltaWebSocketManager(symbols=self.symbols, verbose=False)
        self.ws_manager.start()

    def close(self):
        """Stops background WebSocket thread."""
        if self.ws_manager:
            self.ws_manager.stop()

    def get_live_quote(self, symbol="BTCUSD", timeout=4.0) -> dict:
        """
        Retrieves real-time quote (LTP, Mark Price, Best Bid/Ask) from WebSocket.
        Falls back to REST ticker if WebSocket data is not yet available.
        """
        quote = self.ws_manager.wait_for_quote(symbol, timeout=timeout)
        if quote and quote.get("ltp") is not None:
            return quote

        # Fallback to REST ticker
        try:
            ticker = self.client.get_ticker(symbol)
            quotes = ticker.get("quotes", {})
            return {
                "ltp": float(ticker.get("close") or quotes.get("best_bid", 0)),
                "mark_price": float(ticker.get("mark_price", 0)),
                "bid": float(quotes.get("best_bid", 0)) if quotes.get("best_bid") else None,
                "ask": float(quotes.get("best_ask", 0)) if quotes.get("best_ask") else None,
                "volume": ticker.get("volume"),
                "timestamp": time.time()
            }
        except Exception as e:
            print(f"[WARN] Failed to fetch REST ticker fallback: {e}")
            return {}

    def place_market_order(self, symbol="BTCUSD", size=1, side="buy"):
        """
        Places a Market Order.
        :param symbol: Contract symbol (e.g., 'BTCUSD').
        :param size: Number of contracts.
        :param side: 'buy' or 'sell'.
        """
        prod = get_product_details(symbol, self.client)
        product_id = prod["id"]
        side = side.lower()

        print(f"[ORDER] Placing MARKET {side.upper()} order for {symbol} (Product ID {product_id}), Size={size}")
        res = self.client.place_order(
            product_id=product_id,
            size=int(size),
            side=side,
            order_type=OrderType.MARKET
        )
        print(f"[SUCCESS] Market order placed: {res}")
        return res

    def place_limit_order(self, symbol="BTCUSD", size=1, side="buy", price=None, price_offset=0.0, time_in_force=TimeInForce.GTC, post_only=False):
        """
        Places a Limit Order.
        If price is None, calculates limit price based on live WebSocket quote.
        """
        prod = get_product_details(symbol, self.client)
        product_id = prod["id"]
        tick_size = prod.get("tick_size", "0.5")
        side = side.lower()

        if price is None:
            quote = self.get_live_quote(symbol)
            if side == "buy":
                base_price = quote.get("bid") or quote.get("ltp")
            else:
                base_price = quote.get("ask") or quote.get("ltp")

            if base_price is None:
                raise ValueError(f"Unable to determine live price for {symbol}. Please specify 'price' explicitly.")
            target_price = base_price + price_offset
        else:
            target_price = price

        valid_price = align_price(target_price, tick_size)

        print(f"[ORDER] Placing LIMIT {side.upper()} order for {symbol}: Size={size}, Price={valid_price}, TIF={time_in_force.value}")
        res = self.client.place_order(
            product_id=product_id,
            size=int(size),
            side=side,
            limit_price=str(valid_price),
            order_type=OrderType.LIMIT,
            time_in_force=time_in_force,
            post_only="true" if post_only else "false"
        )
        print(f"[SUCCESS] Limit order placed: {res}")
        return res

    def place_stop_loss_order(self, symbol="BTCUSD", size=1, side="sell", stop_price=None, trail_amount=None, is_trailing=False):
        """
        Places a Stop Loss or Trailing Stop Loss order.
        """
        prod = get_product_details(symbol, self.client)
        product_id = prod["id"]
        tick_size = prod.get("tick_size", "0.5")
        side = side.lower()

        if is_trailing:
            if trail_amount is None:
                raise ValueError("trail_amount is required for trailing stop loss orders.")
            print(f"[ORDER] Placing TRAILING STOP {side.upper()} for {symbol}: Size={size}, Trail Amount={trail_amount}")
            res = self.client.place_stop_order(
                product_id=product_id,
                size=int(size),
                side=side,
                order_type=OrderType.MARKET,
                trail_amount=str(trail_amount),
                isTrailingStopLoss=True
            )
        else:
            if stop_price is None:
                raise ValueError("stop_price is required for stop loss orders.")
            valid_stop = align_price(stop_price, tick_size)
            print(f"[ORDER] Placing STOP LOSS {side.upper()} for {symbol}: Size={size}, Trigger={valid_stop}")
            res = self.client.place_stop_order(
                product_id=product_id,
                size=int(size),
                side=side,
                order_type=OrderType.MARKET,
                stop_price=str(valid_stop)
            )

        print(f"[SUCCESS] Stop order placed: {res}")
        return res

    def cancel_order(self, symbol_or_id, order_id: int):
        """Cancels a specific open order."""
        if isinstance(symbol_or_id, int) or (isinstance(symbol_or_id, str) and symbol_or_id.isdigit()):
            product_id = int(symbol_or_id)
        else:
            prod = get_product_details(symbol_or_id, self.client)
            product_id = prod["id"]

        print(f"[ORDER] Cancelling order {order_id} (Product ID {product_id})")
        return self.client.cancel_order(product_id=product_id, order_id=int(order_id))

    def cancel_all_orders(self, symbol=None):
        """Cancels all open orders (optionally filtered by symbol)."""
        prod_id = None
        if symbol:
            prod = get_product_details(symbol, self.client)
            prod_id = prod["id"]

        payload = {"product_id": prod_id} if prod_id else {}
        print(f"[ORDER] Cancelling all orders {f'for {symbol}' if symbol else 'across all products'}...")
        return self.client.cancel_all_orders(payload=payload if payload else None)

    def get_open_orders(self, symbol=None):
        """Fetches active and pending orders."""
        query = {"states": "open,pending"}
        if symbol:
            prod = get_product_details(symbol, self.client)
            query["product_ids"] = str(prod["id"])
        return self.client.get_live_orders(query=query)

    def get_open_positions(self, symbol=None):
        """Fetches current open positions."""
        if symbol:
            prod = get_product_details(symbol, self.client)
            return self.client.get_position(product_id=prod["id"])
        else:
            # Query positions
            return self.client.request("GET", "/v2/positions", auth=True).json().get("result", [])

    def set_leverage(self, symbol="BTCUSD", leverage=10):
        """Sets leverage for a product."""
        prod = get_product_details(symbol, self.client)
        product_id = prod["id"]
        print(f"[LEVERAGE] Setting leverage to {leverage}x for {symbol} (Product {product_id})")
        return self.client.set_leverage(product_id=product_id, leverage=str(leverage))


# Global singleton order manager instance
order_manager = None
try:
    order_manager = DeltaOrderManager()
except Exception:
    order_manager = None


# Module-level convenience functions
def place_market_order(symbol="BTCUSD", size=1, side="buy"):
    mgr = order_manager or DeltaOrderManager()
    return mgr.place_market_order(symbol=symbol, size=size, side=side)

def place_limit_order(symbol="BTCUSD", size=1, side="buy", price=None, price_offset=0.0, time_in_force=TimeInForce.GTC):
    mgr = order_manager or DeltaOrderManager()
    return mgr.place_limit_order(symbol=symbol, size=size, side=side, price=price, price_offset=price_offset, time_in_force=time_in_force)

def place_stop_loss_order(symbol="BTCUSD", size=1, side="sell", stop_price=None, trail_amount=None, is_trailing=False):
    mgr = order_manager or DeltaOrderManager()
    return mgr.place_stop_loss_order(symbol=symbol, size=size, side=side, stop_price=stop_price, trail_amount=trail_amount, is_trailing=is_trailing)

def get_open_orders(symbol=None):
    mgr = order_manager or DeltaOrderManager()
    return mgr.get_open_orders(symbol=symbol)

def cancel_order(symbol_or_id, order_id: int):
    mgr = order_manager or DeltaOrderManager()
    return mgr.cancel_order(symbol_or_id=symbol_or_id, order_id=order_id)


if __name__ == "__main__":
    print("=" * 60)
    print("Delta Exchange Order Placement & Market Stream Initialized")
    print("=" * 60)

    mgr = DeltaOrderManager(symbols=["BTCUSD"])
    try:
        # 1. Fetch and print live quote from WebSocket
        print("\n[1] Fetching live BTCUSD market quote via WebSocket...")
        quote = mgr.get_live_quote("BTCUSD", timeout=3.0)
        print(f"  -> BTCUSD LTP: {quote.get('ltp')}")
        print(f"  -> Best Bid:   {quote.get('bid')}")
        print(f"  -> Best Ask:   {quote.get('ask')}")
        print(f"  -> Mark Price: {quote.get('mark_price')}")

        # 2. Check open orders
        print("\n[2] Checking live open orders...")
        open_orders = mgr.get_open_orders("BTCUSD")
        print(f"  -> Open orders count: {len(open_orders) if isinstance(open_orders, list) else open_orders}")

        # 3. Check position
        print("\n[3] Checking current position for BTCUSD...")
        pos = mgr.get_open_positions("BTCUSD")
        print(f"  -> Position: {pos}")

        print("\n[INFO] orderplacement.py is ready for trading automation!")
    finally:
        mgr.close()
