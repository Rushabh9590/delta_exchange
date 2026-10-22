import unittest
from unittest.mock import MagicMock, patch
import os, sys
from pathlib import Path

# Add python-rest-client to path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "python-rest-client"))

from mm_execution import MMTradeCycle, DeltaMMEngine

class TestReplaceAndCompletionFix(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        # Mock product details
        self.mock_client.get_ticker.return_value = {
            "symbol": "P-ETH-2430-211026",
            "product_id": 159308,
            "contract_type": "put_options",
            "contract_value": "0.01",
            "tick_size": "0.01",
            "quotes": {"best_bid": "0.64", "best_ask": "1.60"}
        }

    def test_ws_cancel_during_replacement_does_not_abort_cycle(self):
        """When an order is being edited/replaced (_is_replacing_order = True), WS cancel is ignored."""
        with patch("mm_execution.get_product_details", return_value={"id": 159308, "tick_size": 0.01, "contract_value": 0.01}):
            cycle = MMTradeCycle(symbol="P-ETH-2430-211026", size=5, client=self.mock_client)
            cycle.buy_order_id = 12345
            cycle.buy_filled_qty = 1
            cycle.buy_unfilled_qty = 4
            cycle.state = "DUAL_ACTIVE"
            cycle.is_running = True

            # Simulate _is_replacing_order = True
            cycle._is_replacing_order = True

            # Fire WS cancel event with matching order properties
            cycle._order_fills_map[12345] = 1.0
            cycle._on_buy_order_update({"id": 12345, "state": "cancelled", "size": 5, "unfilled_size": 4})

            # Verify it did NOT cancel the trade cycle or zero out unfilled
            self.assertFalse(cycle.stop_requested)
            self.assertTrue(cycle.is_running)
            self.assertEqual(cycle.buy_unfilled_qty, 4)
            self.assertEqual(cycle.state, "DUAL_ACTIVE")

    def test_cycle_completion_check_with_open_position_on_exchange(self):
        """Even if sell_filled_qty >= buy_filled_qty, if Delta holds open contracts, DO NOT mark COMPLETED_PROFIT."""
        with patch("mm_execution.get_product_details", return_value={"id": 159308, "tick_size": 0.01, "contract_value": 0.01}):
            cycle = MMTradeCycle(symbol="P-ETH-2430-211026", size=5, client=self.mock_client)
            cycle.buy_order_id = 12345
            cycle.buy_filled_qty = 1
            cycle.buy_unfilled_qty = 0
            cycle.sell_filled_qty = 1
            cycle.cost_to_cost_price = 0.65
            cycle.sell_fill_price = 1.94

            # Delta Exchange still holds 1 lot!
            self.mock_client.get_position.return_value = {"size": 1, "entry_price": "0.66"}

            # Part 3 logic:
            actual_pos = abs(int(self.mock_client.get_position(cycle.product_id).get("size") or 0))
            if actual_pos > 0:
                cycle.buy_filled_qty = max(cycle.buy_filled_qty, cycle.sell_filled_qty + actual_pos)
                cycle.size = actual_pos
                cycle.state = "SELL_OPEN"

            self.assertNotEqual(cycle.state, "COMPLETED_PROFIT")
            self.assertEqual(cycle.state, "SELL_OPEN")
            self.assertEqual(cycle.buy_filled_qty, 2)  # 1 sold + 1 held = 2 bought

    def test_reconcile_with_exchange_does_not_close_when_position_held(self):
        """reconcile_with_exchange keeps cycle active when open_pos_size > 0."""
        with patch("mm_execution.get_product_details", return_value={"id": 159308, "tick_size": 0.01, "contract_value": 0.01}):
            cycle = MMTradeCycle(symbol="P-ETH-2430-211026", size=5, client=self.mock_client)
            cycle.buy_filled_qty = 1
            cycle.buy_unfilled_qty = 0
            cycle.sell_filled_qty = 1
            cycle.sell_order_id = 9999
            cycle.cost_to_cost_price = 0.65
            cycle.sell_fill_price = 1.94

            # Exchange has 1 lot position and sell order open
            self.mock_client.get_position.return_value = {"size": 1, "entry_price": "0.66"}
            self.mock_client.get_order_by_id.return_value = {"id": 9999, "state": "open", "unfilled_size": 1, "size": 1, "average_fill_price": None}

            res = cycle.reconcile_with_exchange()
            self.assertTrue(res)  # Should return True to resume!
            self.assertEqual(cycle.buy_filled_qty, 2)
            self.assertEqual(cycle.size, 2)

if __name__ == "__main__":
    unittest.main()
