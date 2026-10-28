import websocket
import json

ws_url = "wss://socket.india.delta.exchange"
sub = {
    "type": "subscribe",
    "payload": {
        "channels": [
            {"name": "l1_orderbook", "symbols": ["BTCUSD", "C-BTC-81000-281026"]}
        ]
    }
}

count = 0

def on_open(ws):
    print("Sending sub for l1_orderbook...")
    ws.send(json.dumps(sub))

def on_message(ws, msg):
    global count
    d = json.loads(msg)
    print("L1 MSG:", json.dumps(d))
    count += 1
    if count >= 6:
        ws.close()

ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
ws.run_forever()
