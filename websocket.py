import sys
import os
import json
import time
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
    def __init__(self, symbols=None, ws_url=None, verbose=False):
        self.symbols = symbols or ["BTCUSD"]
        self.ws_url = ws_url or get_ws_url()
        self.verbose = verbose
        self.market_data = {}  # {symbol: {'ltp': float, 'mark_price': float, 'bid': float, 'ask': float, 'bids': [], 'asks': []}}
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

                    self.market_data[symbol]["volume"] = data.get("volume")
                    self.market_data[symbol]["timestamp"] = time.time()

                if self.verbose:
                    spot_disp = self.market_data[symbol].get("spot_price") or self.market_data[symbol].get("mark_price") or self.market_data[symbol].get("ltp")
                    print(f"[TICK] {symbol} | Spot/Index: {spot_disp} | LTP: {self.market_data[symbol].get('ltp')} | Mark: {self.market_data[symbol].get('mark_price')} | Bid: {self.market_data[symbol].get('bid')} | Ask: {self.market_data[symbol].get('ask')}", flush=True)

            elif msg_type == "l2_updates" and symbol:
                with self._lock:
                    if symbol not in self.market_data:
                        self.market_data[symbol] = {}
                    self.market_data[symbol]["bids"] = data.get("bids", [])
                    self.market_data[symbol]["asks"] = data.get("asks", [])
                    self.market_data[symbol]["timestamp"] = time.time()

                if self.verbose:
                    top_bid = data.get("bids", [["-", "-"]])[0]
                    top_ask = data.get("asks", [["-", "-"]])[0]
                    print(f"[L2] {symbol} | Best Bid: {top_bid} | Best Ask: {top_ask}", flush=True)

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
                            {"name": "mark_price", "symbols": to_add}
                        ]
                    }
                }
                self.ws_app.send(json.dumps(sub_payload))
                if self.verbose:
                    print(f"[WS DYNAMIC SUB] Subscribed to {to_add}", flush=True)
            except Exception as e:
                if self.verbose:
                    print(f"[WS SUB ERROR] {e}", flush=True)

    def get_latest_quote(self, symbol="BTCUSD"):
        """Returns the latest quote dictionary for the specified symbol."""
        with self._lock:
            return self.market_data.get(symbol, {}).copy()

    def wait_for_quote(self, symbol="BTCUSD", timeout=5.0):
        """Blocks until initial quote data is received or timeout is reached."""
        start = time.time()
        while time.time() - start < timeout:
            quote = self.get_latest_quote(symbol)
            if quote.get("ltp") is not None or quote.get("bid") is not None:
                return quote
            time.sleep(0.1)
        return self.get_latest_quote(symbol)


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
