import json
import time
import websocket
import urllib.request

# Get Delta server time from REST API to check clock sync
req = urllib.request.urlopen("https://api.india.delta.exchange/v2/time")
res = json.loads(req.read().decode())
server_time_sec = res.get("result", {}).get("server_time")
local_time_sec = time.time()
clock_offset = server_time_sec - local_time_sec
print(f"Server time: {server_time_sec}, Local time: {local_time_sec}, Offset: {clock_offset*1000:.2f}ms")

def on_message(ws, message):
    recv_time = time.time()
    data = json.loads(message)
    if data.get("type") == "l1_orderbook":
        ts = data.get("timestamp")
        if ts:
            # timestamp is in microseconds
            ts_sec = ts / 1e6
            # latency with offset
            adjusted_now = recv_time + clock_offset
            latency_ms = (adjusted_now - ts_sec) * 1000
            sym = data.get("symbol")
            print(f"TICK: {sym:<22} | Exchange TS: {ts_sec:.4f} | Recv: {recv_time:.4f} | Data Latency: {latency_ms:.1f}ms")

def on_open(ws):
    msg = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "l1_orderbook", "symbols": ["BTCUSD", "C-BTC-81000-281026"]}
            ]
        }
    }
    ws.send(json.dumps(msg))

ws = websocket.WebSocketApp("wss://socket.india.delta.exchange", on_open=on_open, on_message=on_message)
import threading
t = threading.Thread(target=ws.run_forever, daemon=True)
t.start()
time.sleep(3)
ws.close()
