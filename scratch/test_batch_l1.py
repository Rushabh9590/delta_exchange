import json
import time
import websocket

def on_message(ws, message):
    data = json.loads(message)
    mtype = data.get("type")
    if mtype == "l1_orderbook":
        print(f"L1: {data.get('symbol')} | Bid: {data.get('best_bid')} ({data.get('bid_qty')}) | Ask: {data.get('best_ask')} ({data.get('ask_qty')})")
    elif mtype == "subscriptions":
        print("Subscriptions confirmed:", len(data.get("channels", [])[0].get("symbols", [])))

def on_open(ws):
    print("Connected to Delta WS")
    syms = ["BTCUSD", "C-BTC-80000-281026", "C-BTC-81000-281026", "P-BTC-80000-281026", "P-BTC-81000-281026"]
    msg = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "l1_orderbook", "symbols": syms}
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
print("Test completed.")
