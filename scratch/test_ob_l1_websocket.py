import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from websocket import DeltaWebSocketManager
import json

class TestObL1WebSocket(unittest.TestCase):
    def setUp(self):
        self.mgr = DeltaWebSocketManager(symbols=["BTCUSD", "C-BTC-70000-281026"], subscribe_l1=True)

    def test_ob_l1_message_parsing(self):
        # Delta Exchange ob_l1 message payload format
        msg = {
            "type": "ob_l1",
            "symbol": "BTCUSD",
            "bp": "68518.5",
            "bs": "120",
            "ap": "68519.0",
            "as": "45",
            "timestamp": 1775801092453559
        }
        mock_ws = MagicMock()
        self.mgr._on_message(mock_ws, json.dumps(msg))

        top = self.mgr.get_top_of_book("BTCUSD")
        self.assertEqual(top["bid"], 68518.5)
        self.assertEqual(top["bid_size"], 120.0)
        self.assertEqual(top["ask"], 68519.0)
        self.assertEqual(top["ask_size"], 45.0)
        self.assertEqual(top["bids"], [[68518.5, 120.0]])
        self.assertEqual(top["asks"], [[68519.0, 45.0]])

        # Test backwards compatibility with get_l2_orderbook
        l2 = self.mgr.get_l2_orderbook("BTCUSD")
        self.assertEqual(l2["bids"], [[68518.5, 120.0]])
        self.assertEqual(l2["asks"], [[68519.0, 45.0]])

    def test_ob_l1_option_symbol(self):
        msg = {
            "type": "ob_l1",
            "symbol": "C-BTC-70000-281026",
            "bp": "142.5",
            "bs": "10",
            "ap": "143.5",
            "as": "15",
            "timestamp": 1775801092453560
        }
        mock_ws = MagicMock()
        self.mgr._on_message(mock_ws, json.dumps(msg))

        top = self.mgr.get_top_of_book("C-BTC-70000-281026")
        self.assertEqual(top["bid"], 142.5)
        self.assertEqual(top["ask"], 143.5)
        self.assertEqual(top["bid_size"], 10.0)
        self.assertEqual(top["ask_size"], 15.0)

    def test_delta_l1_orderbook_real_wire(self):
        # Exact real wire message from Delta Exchange l1_orderbook channel
        msg = {
            "type": "l1_orderbook",
            "symbol": "C-BTC-81000-281026",
            "best_bid": "172.0",
            "best_ask": "175.0",
            "bid_qty": "15441",
            "ask_qty": "6027",
            "product_id": 160113,
            "timestamp": 1793168132117194
        }
        mock_ws = MagicMock()
        self.mgr._on_message(mock_ws, json.dumps(msg))

        top = self.mgr.get_top_of_book("C-BTC-81000-281026")
        self.assertEqual(top["bid"], 172.0)
        self.assertEqual(top["ask"], 175.0)
        self.assertEqual(top["bid_size"], 15441.0)
        self.assertEqual(top["ask_size"], 6027.0)
        self.assertEqual(top["bids"], [[172.0, 15441.0]])
        self.assertEqual(top["asks"], [[175.0, 6027.0]])

if __name__ == "__main__":
    unittest.main()
