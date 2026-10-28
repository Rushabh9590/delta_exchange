import websocket
import json

ws_url = "wss://socket.india.delta.exchange"
sub = {
    "type": "subscribe",
    "payload": {
        "channels": [
            {"name": "l1_orderbook", "symbols": ["BTCUSD"]},
            {"name": "ob_l1", "symbols": ["BTCUSD"]},
            {"name": "v2/ticker", "symbols": ["BTCUSD"]}
        ]
    }
}

def on_open(ws):
    ws.send(json.dumps(sub))

def on_message(ws, msg):
    d = json.loads(msg)
    if d.get("type") == "subscriptions":
        print("SUBSCRIPTION RESPONSE:")
        print(json.dumps(d, indent=2))
        ws.close()

ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
ws.run_forever()
