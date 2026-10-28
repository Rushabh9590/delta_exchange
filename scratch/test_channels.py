import websocket
import json

ws_url = "wss://socket.india.delta.exchange"
symbols = ["BTCUSD", "C-BTC-81000-281026"]

def on_open(ws):
    print("Testing channels...")
    sub_msg = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "l1_orderbook", "symbols": symbols},
                {"name": "l2_updates", "symbols": symbols}
            ]
        }
    }
    ws.send(json.dumps(sub_msg))

def on_message(ws, msg):
    data = json.loads(msg)
    msg_type = data.get("type") or data.get("channel") or data.get("name")
    print(f"RESPONSE [{msg_type}]: {data}")
    if msg_type != "subscriptions":
        ws.close()

ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
ws.run_forever()
