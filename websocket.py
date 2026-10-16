import sys
import os
import json
import time
import hmac
import hashlib
import threading
import importlib.util

# Robustly import third-party websocket-client package without local filename collision
ws_client = None
for p in sys.path:
    if os.path.abspath(p) == os.path.abspath(os.path.dirname(__file__)):
        continue
    candidate = os.path.join(p, "websocket", "__init__.py")
    if os.path.exists(candidate):
        spec = importlib.util.spec_from_file_location(
            "ws_lib", candidate,
            submodule_search_locations=[os.path.join(p, "websocket")]
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["ws_lib"] = mod
            spec.loader.exec_module(mod)
            ws_client = mod
            break

if ws_client is None:
    try:
        import websocket as ws_client
    except Exception:
        pass

from login import load_credentials, URL_INDIA_PROD, URL_GLOBAL_PROD

# Delta WebSocket Endpoints
WS_URL_INDIA = "wss://socket.india.delta.exchange"
WS_URL_GLOBAL = "wss://socket.delta.exchange"
WS_URL_TESTNET_INDIA = "wss://socket-ind.testnet.deltaex.org"
WS_URL_TESTNET_GLOBAL = "wss://testnet-api.delta.exchange"


def get_ws_url():
    """
    Determines the appropriate WebSocket endpoint based on .env configuration.
    """
    creds = load_credentials()
    base_url = creds.get("base_url", "")
    if "india" in base_url and "testnet" in base_url:
        return WS_URL_TESTNET_INDIA
    elif "india" in base_url:
        return WS_URL_INDIA
    elif "testnet" in base_url:
        return WS_URL_TESTNET_GLOBAL
    elif "delta.exchange" in base_url:
        return WS_URL_GLOBAL
    return WS_URL_INDIA


class DeltaWebSocketManager:
    """
    Thread-safe WebSocket Manager that streams live market data for Delta Exchange symbols.
    Maintains real-time price quotes, mark prices, and orderbooks.
    """
    def __init__(self, symbols=None, ws_url=None, verbose=False, on_tick=None):
        self.symbols = symbols or ["BTCUSD"]
        self.ws_url = ws_url or get_ws_url()
        self.verbose = verbose
        self.on_tick = on_tick
        self.market_data = {}  # {symbol: {'ltp': float, 'mark_price': float, 'bid': float, 'ask': float, 'bids': [], 'asks': []}}
        self.orderbooks = {}   # {symbol: {'bids': {float_price: float_size}, 'asks': {float_price: float_size}}}
        self._lock = threading.Lock()
        self.ws_app = None
        self._thread = None
        self.is_connected = False
        self.is_running = False

    def _on_open(self, ws):
        self.is_connected = True
        if self.verbose:
            print(f"[WS CONNECTED] Connected to {self.ws_url}")
        spot_symbols = list(set([s.replace("USD", "") for s in self.symbols if "USD" in s]))
        channels = [
            {"name": "v2/ticker", "symbols": self.symbols},
            {"name": "mark_price", "symbols": self.symbols},
            {"name": "l2_updates", "symbols": self.symbols}
        ]
        if spot_symbols:
            channels.append({"name": "spot_price", "symbols": spot_symbols})

        sub_payload = {
            "type": "subscribe",
            "payload": {
                "channels": channels
            }
        }
        ws.send(json.dumps(sub_payload))
        if self.verbose:
            print(f"[WS SUB] Subscribed to {self.symbols} & Spot: {spot_symbols}", flush=True)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type") or data.get("channel") or data.get("name")
            symbol = data.get("symbol") or (f"{data.get('underlying_asset_symbol')}USD" if data.get("underlying_asset_symbol") else None)

            if msg_type in ("v2/ticker", "ticker", "spot_price", "mark_price") and symbol:
                with self._lock:
                    if symbol not in self.market_data:
                        self.market_data[symbol] = {}
                    
                    quotes = data.get("quotes") or {}
                    close_val = data.get("close")
                    best_bid = quotes.get("best_bid")
                    spot_val = data.get("spot_price") or data.get("underlying_price")
                    mark_val = data.get("mark_price")

                    ltp = close_val if close_val is not None else best_bid
                    if ltp is not None:
                        self.market_data[symbol]["ltp"] = float(ltp)

                    if mark_val is not None:
                        try:
                            self.market_data[symbol]["mark_price"] = float(mark_val)
                        except (ValueError, TypeError):
                            pass

                    if spot_val is not None:
                        try:
                            self.market_data[symbol]["spot_price"] = float(spot_val)
                        except (ValueError, TypeError):
                            pass
                    elif mark_val is not None and "spot_price" not in self.market_data[symbol]:
                        self.market_data[symbol]["spot_price"] = self.market_data[symbol].get("mark_price")

                    if best_bid is not None:
                        try:
                            self.market_data[symbol]["bid"] = float(best_bid)
                        except (ValueError, TypeError):
                            pass

                    best_ask = quotes.get("best_ask")
                    if best_ask is not None:
                        try:
                            self.market_data[symbol]["ask"] = float(best_ask)
                        except (ValueError, TypeError):
                            pass

                    vol_val = data.get("volume")
                    if vol_val is not None:
                        try:
                            self.market_data[symbol]["volume"] = float(vol_val)
                        except (ValueError, TypeError):
                            pass

                    oi_val = data.get("oi") or data.get("oi_contracts") or data.get("oi_value") or data.get("open_interest")
                    if oi_val is not None:
                        try:
                            self.market_data[symbol]["oi"] = float(oi_val)
                        except (ValueError, TypeError):
                            pass

                    self.market_data[symbol]["timestamp"] = time.time()

                if self.on_tick:
                    try:
                        self.on_tick(symbol, self.market_data[symbol], data)
                    except Exception:
                        pass

                if self.verbose:
                    spot_disp = self.market_data[symbol].get("spot_price") or self.market_data[symbol].get("mark_price") or self.market_data[symbol].get("ltp")
                    print(f"[TICK] {symbol} | Spot/Index: {spot_disp} | LTP: {self.market_data[symbol].get('ltp')} | Mark: {self.market_data[symbol].get('mark_price')} | Bid: {self.market_data[symbol].get('bid')} | Ask: {self.market_data[symbol].get('ask')}", flush=True)

            elif msg_type == "l2_updates" and symbol:
                action = data.get("action")
                with self._lock:
                    if symbol not in self.orderbooks:
                        self.orderbooks[symbol] = {"bids": {}, "asks": {}}
                    if symbol not in self.market_data:
                        self.market_data[symbol] = {}

                    if action == "snapshot":
                        # Full snapshot: rebuild book
                        self.orderbooks[symbol]["bids"] = {}
                        self.orderbooks[symbol]["asks"] = {}
                        for item in data.get("bids", []):
                            try:
                                p, s = float(item[0]), float(item[1])
                                if s > 0:
                                    self.orderbooks[symbol]["bids"][p] = s
                            except (ValueError, TypeError, IndexError):
                                pass
                        for item in data.get("asks", []):
                            try:
                                p, s = float(item[0]), float(item[1])
                                if s > 0:
                                    self.orderbooks[symbol]["asks"][p] = s
                            except (ValueError, TypeError, IndexError):
                                pass
                    else:
                        # Incremental update: update or delete modified price levels
                        for item in data.get("bids", []):
                            try:
                                p, s = float(item[0]), float(item[1])
                                if s <= 0:
                                    self.orderbooks[symbol]["bids"].pop(p, None)
                                else:
                                    self.orderbooks[symbol]["bids"][p] = s
                            except (ValueError, TypeError, IndexError):
                                pass
                        for item in data.get("asks", []):
                            try:
                                p, s = float(item[0]), float(item[1])
                                if s <= 0:
                                    self.orderbooks[symbol]["asks"].pop(p, None)
                                else:
                                    self.orderbooks[symbol]["asks"][p] = s
                            except (ValueError, TypeError, IndexError):
                                pass

                    sorted_bids = sorted([[p, s] for p, s in self.orderbooks[symbol]["bids"].items()], key=lambda x: x[0], reverse=True)
                    sorted_asks = sorted([[p, s] for p, s in self.orderbooks[symbol]["asks"].items()], key=lambda x: x[0])

                    self.market_data[symbol]["bids"] = sorted_bids
                    self.market_data[symbol]["asks"] = sorted_asks
                    if sorted_bids:
                        self.market_data[symbol]["bid"] = sorted_bids[0][0]
                    if sorted_asks:
                        self.market_data[symbol]["ask"] = sorted_asks[0][0]
                    self.market_data[symbol]["timestamp"] = time.time()

                if self.verbose:
                    top_b = sorted_bids[0] if sorted_bids else ["-", "-"]
                    top_a = sorted_asks[0] if sorted_asks else ["-", "-"]
                    print(f"[L2] {symbol} | Best Bid: {top_b} | Best Ask: {top_a}", flush=True)

        except Exception as e:
            if self.verbose:
                print(f"[WS PARSE ERROR] {e}", flush=True)

    def _on_error(self, ws, error):
        if self.verbose:
            print(f"[WS ERROR] {error}")

    def _on_close(self, ws, code, msg):
        self.is_connected = False
        if self.verbose:
            print(f"[WS CLOSED] Code: {code}, Msg: {msg}")

    def start(self):
        """Starts WebSocket client in a background daemon thread."""
        if self.is_running:
            return
        if not ws_client or not hasattr(ws_client, "WebSocketApp"):
            raise RuntimeError("websocket-client library is not available.")

        self.is_running = True
        self.ws_app = ws_client.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self._thread = threading.Thread(target=self.ws_app.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        """Stops WebSocket client."""
        self.is_running = False
        if self.ws_app:
            self.ws_app.close()
        self.is_connected = False

    def subscribe_symbols(self, new_symbols):
        """Dynamically subscribes to new symbols while WebSocket is running."""
        if not new_symbols:
            return
        to_add = []
        with self._lock:
            for s in new_symbols:
                if s and s not in self.symbols:
                    self.symbols.append(s)
                    to_add.append(s)
        if to_add and self.is_connected and self.ws_app:
            try:
                sub_payload = {
                    "type": "subscribe",
                    "payload": {
                        "channels": [
                            {"name": "v2/ticker", "symbols": to_add},
                            {"name": "mark_price", "symbols": to_add},
                            {"name": "l2_updates", "symbols": to_add}
                        ]
                    }
                }
                self.ws_app.send(json.dumps(sub_payload))
                if self.verbose:
                    print(f"[WS DYNAMIC SUB] Subscribed to {to_add} (ticker, mark, l2)", flush=True)
            except Exception as e:
                if self.verbose:
                    print(f"[WS SUB ERROR] {e}", flush=True)

    def get_latest_quote(self, symbol="BTCUSD"):
        """Returns the latest quote dictionary for the specified symbol."""
        with self._lock:
            return self.market_data.get(symbol, {}).copy()

    def get_l2_orderbook(self, symbol):
        """Returns the latest in-memory bids and asks list from WebSocket."""
        with self._lock:
            data = self.market_data.get(symbol, {})
            return {
                "bids": list(data.get("bids", [])),
                "asks": list(data.get("asks", [])),
                "timestamp": data.get("timestamp", 0)
            }

    def wait_for_quote(self, symbol="BTCUSD", timeout=5.0):
        """Blocks until initial quote data is received or timeout is reached."""
        start = time.time()
        while time.time() - start < timeout:
            quote = self.get_latest_quote(symbol)
            if quote.get("ltp") is not None or quote.get("bid") is not None:
                return quote
            time.sleep(0.1)
        return self.get_latest_quote(symbol)


_public_ws_manager = None

def get_public_ws_manager(symbols=None, verbose=False) -> DeltaWebSocketManager:
    """
    Returns (and starts if not already running) the global public DeltaWebSocketManager.
    """
    global _public_ws_manager
    if _public_ws_manager is None:
        _public_ws_manager = DeltaWebSocketManager(symbols=symbols or ["BTCUSD"], verbose=verbose)
        _public_ws_manager.start()
    elif symbols:
        _public_ws_manager.subscribe_symbols(symbols)
    return _public_ws_manager


class DeltaPrivateWSManager:
    """
    Real-time Authenticated Private WebSocket Manager for Delta Exchange.
    Handles 'key-auth' handshake, auto-reconnect, keep-alive heartbeat,
    and private streams for 'orders' and 'positions'.
    Enables sub-50ms execution fill events.
    """
    def __init__(self, account=None, ws_url=None, verbose=False):
        self.account_name = (account or os.getenv("ACTIVE_ACCOUNT") or "main").strip().lower()
        creds = load_credentials(account=self.account_name)
        self.api_key = creds.get("api_key")
        self.api_secret = creds.get("api_secret")
        self.ws_url = ws_url or get_ws_url()
        self.verbose = verbose

        self.ws_app = None
        self._thread = None
        self._heartbeat_thread = None
        self.is_connected = False
        self.is_authenticated = False
        self.is_running = False
        self._lock = threading.Lock()

        # Listeners and cache
        self.order_listeners = {}        # {order_id: set([callback_func])}
        self.global_order_listeners = [] # list of callback(order)
        self.position_listeners = []     # list of callback(position)
        self.latest_orders = {}          # {order_id: order_dict}
        self.latest_positions = {}       # {product_id_or_symbol: position_dict}
        self._reconnect_delay = 3.0

    def _on_open(self, ws):
        self.is_connected = True
        if self.verbose:
            print(f"[PRIVATE WS] Connected to {self.ws_url} for account [{self.account_name.upper()}]. Authenticating...", flush=True)

        if not self.api_key or not self.api_secret:
            print(f"[PRIVATE WS ERROR] Missing API credentials for account [{self.account_name.upper()}]. Cannot authenticate.", flush=True)
            return

        try:
            timestamp = str(int(time.time()))
            sig_data = "GET" + timestamp + "/live"
            signature = hmac.new(
                bytes(self.api_secret, "utf-8"),
                bytes(sig_data, "utf-8"),
                hashlib.sha256
            ).hexdigest()

            auth_msg = {
                "type": "key-auth",
                "payload": {
                    "api-key": self.api_key,
                    "signature": signature,
                    "timestamp": timestamp
                }
            }
            ws.send(json.dumps(auth_msg))
            if self.verbose:
                print(f"[PRIVATE WS AUTH SENT] Sent key-auth for [{self.account_name.upper()}]", flush=True)
        except Exception as e:
            print(f"[PRIVATE WS AUTH ERROR] {e}", flush=True)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type") or data.get("channel") or data.get("name")

            if msg_type == "key-auth":
                if data.get("status") == "authenticated" or data.get("success") is True:
                    self.is_authenticated = True
                    if self.verbose:
                        print(f"[PRIVATE WS AUTH OK] Authenticated on [{self.account_name.upper()}]. Subscribing to private channels...", flush=True)
                    sub_payload = {
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {"name": "orders", "symbols": ["all"]},
                                {"name": "positions", "symbols": ["all"]}
                            ]
                        }
                    }
                    ws.send(json.dumps(sub_payload))
                else:
                    self.is_authenticated = False
                    print(f"[PRIVATE WS AUTH FAILED] {data}", flush=True)

            elif msg_type == "subscriptions":
                if self.verbose:
                    print(f"[PRIVATE WS SUB OK] Channels active: {data.get('channels')}", flush=True)

            elif msg_type in ("orders", "order"):
                self._handle_orders_message(data)

            elif msg_type in ("positions", "position"):
                self._handle_positions_message(data)

            elif msg_type == "ping":
                ws.send(json.dumps({"type": "pong"}))

        except Exception as e:
            if self.verbose:
                print(f"[PRIVATE WS MESSAGE ERROR] {e}", flush=True)

    def _handle_orders_message(self, data: dict):
        orders = []
        if "orders" in data and isinstance(data["orders"], list):
            orders.extend(data["orders"])
        elif "data" in data:
            if isinstance(data["data"], list):
                orders.extend(data["data"])
            elif isinstance(data["data"], dict):
                orders.append(data["data"])
        elif "order" in data and isinstance(data["order"], dict):
            orders.append(data["order"])
        elif data.get("id") is not None:
            orders.append(data)

        for o in orders:
            raw_id = o.get("id")
            if raw_id is None:
                continue
            try:
                order_id = int(raw_id)
            except (ValueError, TypeError):
                order_id = raw_id

            listeners_to_call = []
            globals_to_call = []

            with self._lock:
                self.latest_orders[order_id] = o
                if order_id in self.order_listeners:
                    listeners_to_call = list(self.order_listeners[order_id])
                globals_to_call = list(self.global_order_listeners)

            for cb in listeners_to_call:
                try:
                    cb(o)
                except Exception as cb_err:
                    print(f"[PRIVATE WS] Error in order {order_id} listener: {cb_err}", flush=True)

            for cb in globals_to_call:
                try:
                    cb(o)
                except Exception as gcb_err:
                    print(f"[PRIVATE WS] Error in global order listener: {gcb_err}", flush=True)

    def _handle_positions_message(self, data: dict):
        positions = []
        if "positions" in data and isinstance(data["positions"], list):
            positions.extend(data["positions"])
        elif "data" in data:
            if isinstance(data["data"], list):
                positions.extend(data["data"])
            elif isinstance(data["data"], dict):
                positions.append(data["data"])
        elif data.get("product_id") is not None or data.get("symbol") is not None:
            positions.append(data)

        with self._lock:
            for p in positions:
                key = p.get("symbol") or p.get("product_id")
                if key:
                    self.latest_positions[key] = p
            listeners = list(self.position_listeners)

        for cb in listeners:
            for p in positions:
                try:
                    cb(p)
                except Exception:
                    pass

    def _on_error(self, ws, error):
        if self.verbose:
            print(f"[PRIVATE WS ERROR] {error}", flush=True)

    def _on_close(self, ws, code, msg):
        self.is_connected = False
        self.is_authenticated = False
        if self.verbose:
            print(f"[PRIVATE WS CLOSED] Code: {code}, Msg: {msg}", flush=True)

    def _heartbeat_loop(self):
        """Sends a ping every 15 seconds to ensure connection freshness."""
        while self.is_running:
            try:
                if self.is_connected and self.ws_app:
                    self.ws_app.send(json.dumps({"type": "ping"}))
            except Exception:
                pass
            time.sleep(15)

    def _run_loop(self):
        """Auto-reconnecting run loop."""
        while self.is_running:
            try:
                if not ws_client or not hasattr(ws_client, "WebSocketApp"):
                    print("[PRIVATE WS] websocket-client library is not available.", flush=True)
                    break

                self.ws_app = ws_client.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                if self.verbose:
                    print(f"[PRIVATE WS EXCEPTION] {e}", flush=True)

            self.is_connected = False
            self.is_authenticated = False
            if not self.is_running:
                break
            time.sleep(self._reconnect_delay)

    def start(self):
        """Starts Private WebSocket in a background daemon thread."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"DeltaPrivateWS-{self.account_name}")
        self._thread.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name=f"DeltaPrivateHeartbeat-{self.account_name}")
        self._heartbeat_thread.start()

    def stop(self):
        """Stops Private WebSocket client."""
        self.is_running = False
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        self.is_connected = False
        self.is_authenticated = False

    def add_order_listener(self, order_id: int, callback):
        """
        Registers a callback function to be called immediately when an order update
        matching order_id arrives via WebSocket.
        """
        if not order_id:
            return
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            oid = order_id

        cached = None
        with self._lock:
            if oid not in self.order_listeners:
                self.order_listeners[oid] = set()
            self.order_listeners[oid].add(callback)
            cached = self.latest_orders.get(oid)

        # If already cached, invoke callback immediately
        if cached:
            try:
                callback(cached)
            except Exception:
                pass

    def remove_order_listener(self, order_id: int, callback=None):
        if not order_id:
            return
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            oid = order_id

        with self._lock:
            if oid in self.order_listeners:
                if callback:
                    self.order_listeners[oid].discard(callback)
                    if not self.order_listeners[oid]:
                        del self.order_listeners[oid]
                else:
                    del self.order_listeners[oid]

    def add_global_order_listener(self, callback):
        with self._lock:
            if callback not in self.global_order_listeners:
                self.global_order_listeners.append(callback)

    def remove_global_order_listener(self, callback):
        with self._lock:
            if callback in self.global_order_listeners:
                self.global_order_listeners.remove(callback)

    def add_position_listener(self, callback):
        with self._lock:
            if callback not in self.position_listeners:
                self.position_listeners.append(callback)

    def remove_position_listener(self, callback):
        with self._lock:
            if callback in self.position_listeners:
                self.position_listeners.remove(callback)

    def get_order(self, order_id: int) -> dict | None:
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            oid = order_id
        with self._lock:
            return self.latest_orders.get(oid)

    def get_position(self, key: str | int) -> dict | None:
        with self._lock:
            return self.latest_positions.get(key)

    def get_status(self) -> dict:
        return {
            "account": self.account_name,
            "is_connected": self.is_connected,
            "is_authenticated": self.is_authenticated,
            "is_running": self.is_running,
            "active_order_listeners": len(self.order_listeners),
            "tracked_orders_count": len(self.latest_orders),
            "tracked_positions_count": len(self.latest_positions),
        }


_private_ws_managers: dict[str, DeltaPrivateWSManager] = {}

def get_private_ws_manager(account=None, verbose=False) -> DeltaPrivateWSManager:
    """
    Returns (and starts if not already running) the singleton DeltaPrivateWSManager
    for the specified account ('main' or 'sub').
    """
    global _private_ws_managers
    acct = (account or os.getenv("ACTIVE_ACCOUNT") or "main").strip().lower()
    if acct not in _private_ws_managers:
        mgr = DeltaPrivateWSManager(account=acct, verbose=verbose)
        mgr.start()
        _private_ws_managers[acct] = mgr
    else:
        mgr = _private_ws_managers[acct]
        if not mgr.is_running:
            mgr.start()
    return mgr


def start_delta_websocket(symbols=None, ws_url=None):
    """
    Connects to Delta Exchange WebSocket and streams live data in foreground console.
    """
    manager = DeltaWebSocketManager(symbols=symbols, ws_url=ws_url, verbose=True)
    try:
        print("Starting Delta WebSocket live stream. Press Ctrl+C to stop.\n")
        manager.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping WebSocket client...")
        manager.stop()


if __name__ == "__main__":
    start_delta_websocket(["BTCUSD"])
