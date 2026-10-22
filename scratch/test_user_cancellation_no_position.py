import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mm_execution import MMTradeCycle
import multileg

class TestUserCancellationNoPosition(unittest.TestCase):
    def setUp(self):
        multileg._PRODUCT_CACHE[88888] = {
            "id": 88888,
            "tick_size": 0.1,
            "contract_value": 0.001,
            "symbol": "P-BTC-80400-211026"
        }
        multileg._PRODUCT_CACHE["P-BTC-80400-211026"] = multileg._PRODUCT_CACHE[88888]
        self.mock_client = MagicMock()
        self.mock_client.get_ticker.return_value = {
            "close": "0.4",
            "mark_price": "0.4",
            "quotes": {"best_bid": "0.3", "best_ask": "0.5"}
        }
        self.mock_client.get_l2_orderbook.return_value = {
            "buy": [{"price": "0.3", "size": "10"}],
            "sell": [{"price": "0.5", "size": "10"}]
        }

    def test_no_position_on_exchange_aborts_sell(self):
        """When user closed/cancelled position on Delta Exchange (pos=0), engine must NOT place sell order."""
        # Exchange returns position size 0
        self.mock_client.get_position.return_value = {"size": "0", "entry_price": None}

        cycle = MMTradeCycle(
            symbol="P-BTC-80400-211026",
            size=50,
            account="sub",
            entry_price=0.4,
            client=self.mock_client
        )
        cycle.buy_filled_qty = 50
        cycle.buy_fill_price = 0.4
        cycle.is_running = True

        # Try to place sell exit
        res = cycle._place_sell_exit_order(target_qty=50)

        # Must return False and stop running cleanly without calling place_order for sell
        self.assertFalse(res)
        self.assertFalse(cycle.is_running)
        self.assertIn(cycle.state, ("CANCELLED", "COMPLETED_PROFIT", "COMPLETED_COST"))
        self.mock_client.place_order.assert_not_called()
        print("-> test_no_position_on_exchange_aborts_sell passed!")

    def test_no_position_for_reduce_only_exception_handled_cleanly(self):
        """When place_order throws no_position_for_reduce_only, cycle stops cleanly without infinite retries."""
        self.mock_client.get_position.return_value = {"size": "50", "entry_price": "0.4"}
        # Delta throws no_position_for_reduce_only
        self.mock_client.place_order.side_effect = Exception("{'code': 'no_position_for_reduce_only'}")

        cycle = MMTradeCycle(
            symbol="P-BTC-80400-211026",
            size=50,
            account="sub",
            entry_price=0.4,
            client=self.mock_client
        )
        cycle.buy_filled_qty = 50
        cycle.buy_fill_price = 0.4
        cycle.is_running = True

        res = cycle._place_sell_exit_order(target_qty=50)

        self.assertFalse(res)
        self.assertFalse(cycle.is_running)
        self.assertTrue(cycle.stop_requested)
        print("-> test_no_position_for_reduce_only_exception_handled_cleanly passed!")

    def test_reconcile_when_delta_position_zero(self):
        """When reconciling with exchange upon reboot, if position is 0 and no buy order, do NOT resume."""
        self.mock_client.get_position.return_value = {"size": "0"}
        self.mock_client.get_order_by_id.return_value = {"state": "closed", "unfilled_size": "0"}

        cycle = MMTradeCycle(
            symbol="P-BTC-80400-211026",
            size=50,
            account="sub",
            entry_price=0.4,
            client=self.mock_client
        )
        cycle.buy_filled_qty = 50
        cycle.buy_order_id = 1619468941

        needs_resume = cycle.reconcile_with_exchange()

        self.assertFalse(needs_resume)
        self.assertFalse(cycle.is_running)
        print("-> test_reconcile_when_delta_position_zero passed!")

if __name__ == "__main__":
    unittest.main()
