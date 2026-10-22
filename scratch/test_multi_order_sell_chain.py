import unittest
from unittest.mock import MagicMock, patch
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mm_execution import MMTradeCycle

class TestMultiOrderSellChain(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.get_products.return_value = [
            {"id": 159114, "symbol": "P-BTC-82400-211026", "tick_size": "0.1", "contract_value": "0.001"}
        ]
        self.mock_client.get_ticker.return_value = {
            "quotes": {"best_bid": "3.5", "best_ask": "5.0"},
            "close": "4.0",
            "oi_reduce_only_mode": False
        }

    def test_chained_sell_order_execution(self):
        """
        Tests scenario where:
        - 50 lots bought @ 3.9
        - First SELL order placed for 49 lots (Order ID: 1001)
        - Order 1001 fills completely (49 lots sold)
        - Engine must immediately detect remaining 1 open lot and place Order ID 1002 for 1 lot
        - Order 1002 fills completely (1 lot sold)
        - Engine finishes cycle with 50 lots sold and COMPLETED_PROFIT
        """
        cycle = MMTradeCycle(
            symbol="P-BTC-82400-211026",
            size=50,
            account="sub",
            client=self.mock_client
        )
        cycle.buy_fill_price = 3.9
        cycle.buy_filled_qty = 50
        cycle.buy_unfilled_qty = 0
        cycle.cost_to_cost_price = 3.9

        # Simulate first sell order placement for 49 lots
        self.mock_client.get_position.return_value = {"size": "50"}
        self.mock_client.place_order.return_value = {"id": 1001, "state": "open"}

        placed = cycle._place_sell_exit_order(target_qty=49)
        self.assertTrue(placed)
        self.assertEqual(cycle.sell_order_id, 1001)
        self.assertEqual(cycle.sell_placed_qty, 49)
        self.assertEqual(cycle.sell_unfilled_qty, 49)

        # Delta returns order 1002 for subsequent placement
        self.mock_client.place_order.return_value = {"id": 1002, "state": "open"}
        self.mock_client.get_position.return_value = {"size": "1"}

        # Simulate WebSocket fill callback for Order 1001: 49 lots filled
        cycle._on_sell_order_update({
            "id": 1001,
            "size": 49,
            "unfilled_size": 0,
            "state": "closed",
            "average_fill_price": 5.0
        })

        # Order 1001 was 49 lots. Net open inventory was 50 - 49 = 1 lot.
        # Verify cycle placed a new order 1002 for 1 lot!
        self.assertEqual(cycle.sell_filled_qty, 49)
        self.assertEqual(cycle.sell_order_id, 1002)
        self.assertEqual(cycle.sell_placed_qty, 1)
        self.assertEqual(cycle.sell_unfilled_qty, 1)

        # Simulate WebSocket fill callback for Order 1002: 1 lot filled
        self.mock_client.get_position.return_value = {"size": "0"}
        cycle._on_sell_order_update({
            "id": 1002,
            "size": 1,
            "unfilled_size": 0,
            "state": "closed",
            "average_fill_price": 5.2
        })

        # Verify all 50 lots are now marked filled and cycle completed with profit!
        self.assertEqual(cycle.sell_filled_qty, 50)
        self.assertEqual(cycle.sell_unfilled_qty, 0)
        self.assertEqual(cycle.state, "COMPLETED_PROFIT")
        self.assertGreater(cycle.realized_pnl, 0.0)
        print("TEST PASSED: Chained sell order execution completed successfully!")

    def test_rest_check_chained_order(self):
        """
        Tests fallback REST poller detecting closed order and placing remaining lots.
        """
        cycle = MMTradeCycle(
            symbol="P-BTC-82400-211026",
            size=50,
            account="sub",
            client=self.mock_client
        )
        cycle.buy_fill_price = 3.9
        cycle.buy_filled_qty = 50
        cycle.buy_unfilled_qty = 0
        cycle.cost_to_cost_price = 3.9

        self.mock_client.get_position.return_value = {"size": "50"}
        self.mock_client.place_order.return_value = {"id": 2001, "state": "open"}
        cycle._place_sell_exit_order(target_qty=49)

        # REST returns closed for 2001
        self.mock_client.get_order_by_id.return_value = {
            "id": 2001,
            "size": 49,
            "unfilled_size": 0,
            "state": "closed",
            "average_fill_price": 4.5
        }
        self.mock_client.place_order.return_value = {"id": 2002, "state": "open"}
        self.mock_client.get_position.return_value = {"size": "1"}

        cycle._check_sell_order_status()

        self.assertEqual(cycle.sell_filled_qty, 49)
        self.assertEqual(cycle.sell_order_id, 2002)
        self.assertEqual(cycle.sell_placed_qty, 1)

        # REST returns closed for 2002
        self.mock_client.get_order_by_id.return_value = {
            "id": 2002,
            "size": 1,
            "unfilled_size": 0,
            "state": "closed",
            "average_fill_price": 4.6
        }
        self.mock_client.get_position.return_value = {"size": "0"}

        cycle._check_sell_order_status()

        self.assertEqual(cycle.sell_filled_qty, 50)
        self.assertEqual(cycle.sell_unfilled_qty, 0)
        self.assertEqual(cycle.state, "COMPLETED_PROFIT")
        print("TEST PASSED: REST polling chained sell order completed successfully!")

if __name__ == "__main__":
    unittest.main()
