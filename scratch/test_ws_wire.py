import websocket
import json
import time

ws_url = "wss://socket.india.delta.exchange"
symbols = ["BTCUSD", "C-BTC-81000-281026", "P-BTC-81000-281026"]

received = []

def on_open(ws):
    print("WS Connected!")
    sub_msg = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "ob_l1", "symbols": symbols},
                {"name": "v2/ticker", "symbols": symbols}
            ]
        }
    }
    print("Sending:", json.dumps(sub_msg))
    ws.send(json.dumps(sub_msg))

def on_message(ws, msg):
    data = json.loads(msg)
    msg_type = data.get("type") or data.get("channel") or data.get("name")
    sym = data.get("symbol") or data.get("sy")
    print(f"RECEIVED MSG [{msg_type}] for symbol={sym}: {data}")
    received.append(data)
    if len(received) >= 8:
        ws.close()

def on_error(ws, err):
    print("WS Error:", err)

def on_close(ws, code, msg):
    print("WS Closed:", code, msg)

ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
ws.run_forever()
