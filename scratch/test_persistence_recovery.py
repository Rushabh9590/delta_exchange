import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_execution
from mm_execution import MMTradeCycle, DeltaMMEngine, STATE_FILE_PATH, load_all_trades_state
import multileg


class TestMMPersistenceRecovery(unittest.TestCase):
    def setUp(self):
        # Clean state file for test isolation
        if os.path.exists(STATE_FILE_PATH):
            try:
                os.remove(STATE_FILE_PATH)
            except Exception:
                pass

        multileg._PRODUCT_CACHE["P-BTC-82800-211026"] = {
            "id": 99999,
            "tick_size": 0.1,
            "contract_value": 0.001,
            "symbol": "P-BTC-82800-211026"
        }
        multileg._PRODUCT_CACHE[99999] = multileg._PRODUCT_CACHE["P-BTC-82800-211026"]

        self.mock_client = MagicMock()
        self.mock_client.get_ticker.return_value = {
            "close": "0.9",
            "mark_price": "0.9",
            "quotes": {"best_bid": "0.8", "best_ask": "1.2"}
        }
        self.mock_client.get_l2_orderbook.return_value = {
            "buy": [{"price": "0.8", "size": "10"}],
            "sell": [{"price": "1.2", "size": "10"}]
        }
        self.mock_client.get_position.return_value = {
            "size": "25",
            "entry_price": "0.9"
        }
        self.mock_client.place_order.return_value = {
            "id": 1619397980,
            "product_id": 99999,
            "size": 50,
            "side": "buy",
            "limit_price": "0.9",
            "state": "open"
        }

    def tearDown(self):
        if os.path.exists(STATE_FILE_PATH):
            try:
                os.remove(STATE_FILE_PATH)
            except Exception:
                pass

    def test_persistence_and_recovery(self):
        # 1. Start engine and a trade
        engine = DeltaMMEngine()
        # Patch client provider
        with patch("mm_execution.get_delta_client", return_value=self.mock_client):
            trade = engine.start_trade(
                symbol="P-BTC-82800-211026",
                size=50,
                entry_price=0.9
            )

        trade_id = trade.trade_id
        self.assertIn(trade_id, engine.active_cycles)

        # 2. Simulate partial fill (25 lots bought @ 0.9)
        trade._on_buy_order_update({
            "id": trade.buy_order_id,
            "state": "open",
            "unfilled_size": "25",
            "size": "50",
            "average_fill_price": "0.9"
        })

        # Simulate engine placing SELL exit for 25 lots
        self.mock_client.place_order.return_value = {
            "id": 1619397987,
            "product_id": 99999,
            "size": 25,
            "side": "sell",
            "limit_price": "1.1",
            "state": "open"
        }
        trade._place_sell_exit_order(target_qty=25)

        self.assertEqual(trade.state, "DUAL_ACTIVE")
        self.assertEqual(trade.buy_filled_qty, 25)
        self.assertEqual(trade.sell_placed_qty, 25)
        self.assertEqual(trade.sell_order_id, 1619397987)

        # 3. Verify disk persistence has recorded the active state!
        self.assertTrue(os.path.exists(STATE_FILE_PATH), "State file must exist on disk")
        saved = load_all_trades_state()
        self.assertIn(trade_id, saved["active_trades"])
        saved_trade = saved["active_trades"][trade_id]
        self.assertEqual(saved_trade["state"], "DUAL_ACTIVE")
        self.assertEqual(saved_trade["buy_filled_qty"], 25)
        self.assertEqual(saved_trade["sell_order_id"], 1619397987)
        print("-> Trade state successfully persisted to .mm_trades_state.json!")

        # 4. SIMULATE SERVER RESTART:
        # Create a brand-new engine instance (as server.py does upon reboot)
        print("-> Simulating server reboot...")

        # Setup mock responses for exchange reconciliation:
        # - Account has 25 open long lots
        self.mock_client.get_position.return_value = {
            "product_id": 99999,
            "size": "25",
            "entry_price": "0.9"
        }
        # - BUY order has 25 unfilled lots open
        self.mock_client.get_order_by_id.side_effect = lambda oid: {
            1619397980: {"id": 1619397980, "state": "open", "unfilled_size": "25", "size": "50", "average_fill_price": "0.9"},
            1619397987: {"id": 1619397987, "state": "open", "unfilled_size": "25", "size": "25", "average_fill_price": "1.1"}
        }.get(oid)

        with patch("mm_execution.get_delta_client", return_value=self.mock_client), \
             patch("mm_execution.get_private_ws_manager", return_value=None), \
             patch("mm_execution.get_public_ws_manager", return_value=None):
            new_engine = DeltaMMEngine()

        # Verify trade was recovered into new_engine.active_cycles
        self.assertIn(trade_id, new_engine.active_cycles)
        recovered_trade = new_engine.active_cycles[trade_id]
        self.assertEqual(recovered_trade.symbol, "P-BTC-82800-211026")
        self.assertEqual(recovered_trade.buy_filled_qty, 25)
        self.assertEqual(recovered_trade.sell_placed_qty, 25)
        self.assertEqual(recovered_trade.sell_order_id, 1619397987)
        self.assertEqual(recovered_trade.state, "DUAL_ACTIVE")
        self.assertTrue(recovered_trade.is_running)
        print(f"-> Successfully recovered {trade_id}! State: {recovered_trade.state}, Running: {recovered_trade.is_running}")

        recovered_trade.stop()


if __name__ == "__main__":
    unittest.main()
