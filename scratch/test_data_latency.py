import websocket
import json
import time

clock_offset = None
min_diff = None
rtt = 20.0  # initial estimate

def on_message(ws, message):
    global min_diff, clock_offset, rtt
    recv_time_ms = time.time() * 1000.0
    data = json.loads(message)
    mtype = data.get("type")
    
    if mtype == "pong":
        # pong response
        pass
        
    elif mtype == "l1_orderbook":
        ts = data.get("timestamp")
        if ts:
            ts_ms = ts / 1000.0
            raw_diff = recv_time_ms - ts_ms
            if min_diff is None or raw_diff < min_diff:
                min_diff = raw_diff
                clock_offset = min_diff - (rtt / 2.0)
            
            data_latency_ms = raw_diff - clock_offset
            sym = data.get("symbol")
            print(f"L1 DATA ARRIVAL: {sym:<20} | WebSocket Data Latency: {data_latency_ms:.1f} ms | Bid: {data.get('best_bid')} | Ask: {data.get('best_ask')}")

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
