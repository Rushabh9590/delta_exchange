import sys
import os
import json
import time
import hmac
import hashlib
import threading
import importlib.util
import socket

# High-Performance Ultra-Low-Latency SIMD JSON Parser
try:
    import orjson
    def fast_json_loads(data):
        return orjson.loads(data)
    def fast_json_dumps(data):
        return orjson.dumps(data).decode("utf-8")
except ImportError:
    def fast_json_loads(data):
        return json.loads(data)
    def fast_json_dumps(data):
        return json.dumps(data)

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
    def __init__(self, symbols=None, ws_url=None, verbose=False, on_tick=None, subscribe_l1=True, subscribe_l2=None):
        self.symbols = symbols or ["BTCUSD"]
        self.ws_url = ws_url or get_ws_url()
        self.verbose = verbose
        self.on_tick = on_tick
        if subscribe_l2 is not None:
            self.subscribe_l1 = bool(subscribe_l2)
        else:
            self.subscribe_l1 = subscribe_l1
        self.subscribe_l2 = self.subscribe_l1  # Backwards compatibility alias
        self.market_data = {}  # {symbol: {'ltp': float, 'mark_price': float, 'bid': float, 'ask': float, 'bid_size': float, 'ask_size': float, 'bids': [], 'asks': []}}
        self._lock = threading.Lock()
        self.ws_app = None
        self._thread = None
        self.is_connected = False
        self.is_running = False

    def _on_open(self, ws):
        self.is_connected = True
        # TCP_NODELAY: Disable Nagle's algorithm for sub-millisecond packet delivery
        try:
            s = getattr(ws, "sock", None)
            raw_s = getattr(s, "sock", s)
            if hasattr(raw_s, "setsockopt"):
                raw_s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            elif hasattr(s, "setsockopt"):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        if self.verbose:
            print(f"[WS CONNECTED] Connected to {self.ws_url}")
        spot_symbols = list(set([s.replace("USD", "") for s in self.symbols if "USD" in s]))
        channels = [
            {"name": "v2/ticker", "symbols": self.symbols},
            {"name": "mark_price", "symbols": self.symbols}
        ]
        if self.subscribe_l1:
            channels.append({"name": "l1_orderbook", "symbols": self.symbols})
        if spot_symbols:
            channels.append({"name": "spot_price", "symbols": spot_symbols})

        sub_payload = {
            "type": "subscribe",
            "payload": {
                "channels": channels
            }
        }
        ws.send(fast_json_dumps(sub_payload))
        if self.verbose:
            print(f"[WS SUB] Subscribed to {self.symbols} (l1_orderbook L1 Top-of-Book) & Spot: {spot_symbols}", flush=True)

    def _on_message(self, ws, message):
        try:
            data = fast_json_loads(message)
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

                    if "quotes" in data:
                        raw_b = quotes.get("best_bid")
                        if raw_b is not None and str(raw_b).strip() not in ("", "null", "None"):
                            try:
                                self.market_data[symbol]["bid"] = float(raw_b)
                            except (ValueError, TypeError):
                                self.market_data[symbol]["bid"] = None
                        elif "best_bid" in quotes:
                            self.market_data[symbol]["bid"] = None

                        raw_a = quotes.get("best_ask")
                        if raw_a is not None and str(raw_a).strip() not in ("", "null", "None"):
                            try:
                                self.market_data[symbol]["ask"] = float(raw_a)
                            except (ValueError, TypeError):
                                self.market_data[symbol]["ask"] = None
                        elif "best_ask" in quotes:
                            self.market_data[symbol]["ask"] = None
                    else:
                        raw_b = data.get("best_bid")
                        if raw_b is not None and str(raw_b).strip() not in ("", "null", "None"):
                            try:
                                self.market_data[symbol]["bid"] = float(raw_b)
                            except (ValueError, TypeError):
                                self.market_data[symbol]["bid"] = None
                        elif "best_bid" in data:
                            self.market_data[symbol]["bid"] = None

                        raw_a = data.get("best_ask")
                        if raw_a is not None and str(raw_a).strip() not in ("", "null", "None"):
                            try:
                                self.market_data[symbol]["ask"] = float(raw_a)
                            except (ValueError, TypeError):
                                self.market_data[symbol]["ask"] = None
                        elif "best_ask" in data:
                            self.market_data[symbol]["ask"] = None

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

            elif msg_type in ("ob_l1", "l1_orderbook") or (msg_type == "l2_updates" and symbol):
                # High-speed Top-of-Book (L1) parser: zero dictionary sorting, zero CPU spikes
                target_sym = symbol or data.get("symbol") or data.get("sy")
                if target_sym:
                    raw_bp = data.get("best_bid") if data.get("best_bid") is not None else (data.get("bp") if data.get("bp") is not None else data.get("bid"))
                    raw_bs = data.get("bid_qty") if data.get("bid_qty") is not None else (data.get("bs") if data.get("bs") is not None else data.get("bid_size"))
                    raw_ap = data.get("best_ask") if data.get("best_ask") is not None else (data.get("ap") if data.get("ap") is not None else data.get("ask"))
                    raw_as = data.get("ask_qty") if data.get("ask_qty") is not None else (data.get("as") if data.get("as") is not None else data.get("ask_size"))

                    # Handle legacy/fallback l2_updates array format if received
                    if raw_bp is None and isinstance(data.get("bids"), list) and len(data["bids"]) > 0:
                        try:
                            raw_bp = data["bids"][0][0]
                            raw_bs = data["bids"][0][1]
                        except Exception:
                            pass
                    if raw_ap is None and isinstance(data.get("asks"), list) and len(data["asks"]) > 0:
                        try:
                            raw_ap = data["asks"][0][0]
                            raw_as = data["asks"][0][1]
                        except Exception:
                            pass

                    bid = None
                    ask = None
                    bid_sz = 0.0
                    ask_sz = 0.0

                    try:
                        if raw_bp not in (None, "", "-"):
                            bid = float(raw_bp)
                    except (ValueError, TypeError):
                        pass

                    try:
                        if raw_bs not in (None, "", "-"):
                            bid_sz = float(raw_bs)
                    except (ValueError, TypeError):
                        pass

                    try:
                        if raw_ap not in (None, "", "-"):
                            ask = float(raw_ap)
                    except (ValueError, TypeError):
                        pass

                    try:
                        if raw_as not in (None, "", "-"):
                            ask_sz = float(raw_as)
                    except (ValueError, TypeError):
                        pass

                    with self._lock:
                        if target_sym not in self.market_data:
                            self.market_data[target_sym] = {}

                        if bid is not None:
                            self.market_data[target_sym]["bid"] = bid
                            self.market_data[target_sym]["bid_size"] = bid_sz
                            self.market_data[target_sym]["bids"] = [[bid, bid_sz]]

                        if ask is not None:
                            self.market_data[target_sym]["ask"] = ask
                            self.market_data[target_sym]["ask_size"] = ask_sz
                            self.market_data[target_sym]["asks"] = [[ask, ask_sz]]

                        self.market_data[target_sym]["timestamp"] = time.time()

                    if self.on_tick:
                        try:
                            self.on_tick(target_sym, self.market_data[target_sym], data)
                        except Exception:
                            pass

                    if self.verbose:
                        print(f"[L1] {target_sym} | Best Bid: {bid} ({bid_sz}) | Best Ask: {ask} ({ask_sz})", flush=True)

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

    def _run_connection_loop(self):
        """Persistent connection loop that automatically reconnects if disconnected."""
        while self.is_running:
            try:
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
                    print(f"[WS RUN LOOP ERROR] {e}")
            self.is_connected = False
            if self.is_running:
                time.sleep(2)  # Reconnect backoff

    def start(self):
        """Starts WebSocket client in a resilient background daemon thread with auto-reconnect."""
        if self.is_running:
            return
        if not ws_client or not hasattr(ws_client, "WebSocketApp"):
            raise RuntimeError("websocket-client library is not available.")

        self.is_running = True
        self._thread = threading.Thread(target=self._run_connection_loop, daemon=True, name="DeltaWS-Manager")
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
                channels = [
                    {"name": "v2/ticker", "symbols": to_add},
                    {"name": "mark_price", "symbols": to_add}
                ]
                if self.subscribe_l1:
                    channels.append({"name": "l1_orderbook", "symbols": to_add})
                sub_payload = {
                    "type": "subscribe",
                    "payload": {
                        "channels": channels
                    }
                }
                self.ws_app.send(fast_json_dumps(sub_payload))
                if self.verbose:
                    print(f"[WS DYNAMIC SUB] Subscribed to {to_add} (ticker, mark{', l1_orderbook' if self.subscribe_l1 else ''})", flush=True)
            except Exception as e:
                if self.verbose:
                    print(f"[WS SUB ERROR] {e}", flush=True)

    def get_latest_quote(self, symbol="BTCUSD"):
        """Returns the latest quote dictionary for the specified symbol."""
        with self._lock:
            return self.market_data.get(symbol, {}).copy()

    def get_top_of_book(self, symbol):
        """Returns the latest top-of-book (L1) bid, ask, and sizes (<0.01ms latency)."""
        with self._lock:
            data = self.market_data.get(symbol, {})
            return {
                "bid": data.get("bid"),
                "ask": data.get("ask"),
                "bid_size": data.get("bid_size", 0.0),
                "ask_size": data.get("ask_size", 0.0),
                "bids": list(data.get("bids", [])),
                "asks": list(data.get("asks", [])),
                "timestamp": data.get("timestamp", 0)
            }

    def get_l2_orderbook(self, symbol):
        """Backwards compatibility: Returns the top of book formatted as bids/asks arrays."""
        return self.get_top_of_book(symbol)

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
        # TCP_NODELAY: Disable Nagle's algorithm for sub-50ms execution fill events
        try:
            s = getattr(ws, "sock", None)
            raw_s = getattr(s, "sock", s)
            if hasattr(raw_s, "setsockopt"):
                raw_s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            elif hasattr(s, "setsockopt"):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

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
            ws.send(fast_json_dumps(auth_msg))
            if self.verbose:
                print(f"[PRIVATE WS AUTH SENT] Sent key-auth for [{self.account_name.upper()}]", flush=True)
        except Exception as e:
            print(f"[PRIVATE WS AUTH ERROR] {e}", flush=True)

    def _on_message(self, ws, message):
        try:
            data = fast_json_loads(message)
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
