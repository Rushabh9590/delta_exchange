import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import unittest
from unittest.mock import MagicMock, patch
import time
from mm_execution import MMTradeCycle

class TestMMRetreatBug(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.get_ticker.return_value = {"oi_reduce_only_mode": False}
        self.mock_client.get_live_orders.return_value = []
        self.mock_client.get_order_by_id.return_value = {
            "id": 1001,
            "product_id": 12345,
            "state": "open",
            "limit_price": "0.41",
            "size": 1,
            "unfilled_size": 1
        }
        self.mock_client.place_order.return_value = {"id": 1001, "state": "open"}
        self.mock_client.edit_order.return_value = {"id": 1001, "state": "open"}

    def test_retreat_down_when_competitor_drops_to_30(self):
        """
        User scenario MM-ad4007:
        Our buy is at 0.41 (top of book).
        Competitor bid retreats down to 0.30.
        Public WS only has our order at 0.41.
        L2 REST orderbook has 0.41 (our order) and 0.30 (competitor).
        The bot must find 0.30 and retreat our buy limit down to 0.31!
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.01, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=0.30,
                client=self.mock_client
            )
            cycle.buy_order_id = 1001
            cycle.buy_limit_price = 0.41
            cycle.buy_unfilled_qty = 1
            cycle.total_size = 1
            cycle.buy_filled_qty = 0
            cycle.tick_size = 0.01
            cycle.current_bid = 0.41
            cycle.max_entry_price = 0.50

            # WebSocket only has our order at top of book
            cycle.public_ws = MagicMock()
            cycle.public_ws.get_top_of_book.return_value = {
                "bids": [[0.41, 1.0]],
                "asks": [[0.60, 1.0]]
            }

            # REST L2 orderbook has our order at 0.41 and competitor at 0.30
            self.mock_client.get_l2_orderbook.return_value = {
                "buy": [{"price": "0.41", "size": 1}, {"price": "0.30", "size": 5}],
                "sell": [{"price": "0.60", "size": 1}]
            }

            cycle.fetch_live_quote = MagicMock(return_value={"bid": 0.41, "ask": 0.60})

            # Check that get_external_best_bid discovers 0.30!
            ext_bid = cycle.get_external_best_bid()
            self.assertEqual(ext_bid, 0.30)

            # Run dynamic pegging
            cycle.last_edit_time = 0.0
            cycle.upload_limit_price = MagicMock()
            cycle._peg_buy_order(now=100.0)

            # Verify that bot retreated down to 0.31!
            self.assertEqual(cycle.buy_peg_status, "RETREATING_DOWN_0.31")
            cycle.upload_limit_price.assert_called_once_with(0.31, side="buy", is_manual=False)
            print("-> test_retreat_down_when_competitor_drops_to_30 passed!")

    def test_retreat_to_initial_entry_when_all_competitor_bids_vanish(self):
        """
        When all competitors vanish completely, bot retreats to initial entry price.
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.01, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=0.30,
                client=self.mock_client
            )
            cycle.buy_order_id = 1001
            cycle.buy_limit_price = 0.41
            cycle.buy_unfilled_qty = 1
            cycle.total_size = 1
            cycle.buy_filled_qty = 0
            cycle.tick_size = 0.01
            cycle.current_bid = 0.41

            # Both WS and REST orderbook only contain our own order
            cycle.public_ws = MagicMock()
            cycle.public_ws.get_top_of_book.return_value = {
                "bids": [[0.41, 1.0]],
                "asks": [[0.60, 1.0]]
            }
            self.mock_client.get_l2_orderbook.return_value = {
                "buy": [{"price": "0.41", "size": 1}],
                "sell": [{"price": "0.60", "size": 1}]
            }

            cycle.fetch_live_quote = MagicMock(return_value={"bid": 0.41, "ask": 0.60})

            cycle.last_edit_time = 0.0
            cycle.upload_limit_price = MagicMock()
            cycle._peg_buy_order(now=100.0)

            # Must drop back down to initial_entry_price (0.30)
            self.assertEqual(cycle.buy_peg_status, "RETREATING_DOWN_0.3")
            cycle.upload_limit_price.assert_called_once_with(0.30, side="buy", is_manual=False)
            print("-> test_retreat_to_initial_entry_when_all_competitor_bids_vanish passed!")

    def test_sell_floats_up_when_competitor_moves_to_60(self):
        """
        User scenario on SELL side:
        Our sell is at 0.50 (top of ask book).
        Competitor ask is at 0.60.
        Public WS only sees our order at 0.50.
        L2 REST orderbook discovers competitor at 0.60.
        The bot must float SELL order UP to 0.59 to capture extra profit!
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.01, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=0.30,
                client=self.mock_client
            )
            cycle.buy_filled_qty = 1
            cycle.sell_order_id = 2001
            cycle.sell_placed_qty = 1
            cycle.sell_unfilled_qty = 1
            cycle.sell_limit_price = 0.50
            cycle.cost_to_cost_price = 0.32
            cycle.tick_size = 0.01
            cycle.current_ask = 0.50

            # Public WS only shows our order at 0.50
            cycle.public_ws = MagicMock()
            cycle.public_ws.get_top_of_book.return_value = {
                "bids": [[0.30, 1.0]],
                "asks": [[0.50, 1.0]]
            }

            # L2 REST orderbook has our order at 0.50 and competitor at 0.60
            self.mock_client.get_l2_orderbook.return_value = {
                "sell": [{"price": "0.50", "size": 1}, {"price": "0.60", "size": 5}],
                "buy": [{"price": "0.30", "size": 1}]
            }

            cycle.upload_limit_price = MagicMock()
            cycle.last_edit_time = 0.0

            # Verify discovery
            ext_ask = cycle.get_external_best_ask()
            self.assertEqual(ext_ask, 0.60)

            # Run sell peg
            cycle._peg_sell_order(now=100.0)
            self.assertEqual(cycle.sell_peg_status, "FLOATING_UP_0.59")
            cycle.upload_limit_price.assert_called_once_with(0.59, side="sell", is_manual=False)
            print("-> test_sell_floats_up_when_competitor_moves_to_60 passed!")

    def test_cost_to_cost_ticks_default_and_calculation(self):
        """
        Verify that cost_to_cost_ticks defaults to 2 ticks, and calculates:
        buy_price + Delta 0.06% fees + 2 ticks profit buffer.
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.01, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=0.30,
                client=self.mock_client
            )
            self.assertEqual(cycle.cost_to_cost_ticks, 2)
            floor, fee_usd, fee_ticks, profit_buffer = cycle.calculate_cost_to_cost_price(0.30)
            # 0.30 buy + 2 ticks buffer (0.02) + fee ceiling tick (0.01) = 0.33
            self.assertEqual(profit_buffer, 0.02)
            self.assertGreaterEqual(floor, 0.32)
            print("-> test_cost_to_cost_ticks_default_and_calculation passed!")

    def test_manual_sell_price_floats_up_on_market_bounce(self):
        """
        Scenario requested by user:
        - Cost-to-cost floor is 0.36.
        - User puts manual sell price 0.29 (below floor).
        - Market ask is 0.30 -> manual price stays held at 0.29.
        - Market ask moves up to 0.38 -> bot floats sell up to 0.37 (0.38 - 1 tick),
          clears manual_sell_override, and resumes two-way dynamic pegging.
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.01, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=0.33,
                client=self.mock_client
            )
            cycle.buy_filled_qty = 1
            cycle.sell_order_id = 2001
            cycle.sell_placed_qty = 1
            cycle.sell_unfilled_qty = 1
            cycle.cost_to_cost_price = 0.36
            cycle.tick_size = 0.01

            # User sets manual sell price = 0.29
            cycle.manual_sell_override = True
            cycle.manual_sell_price = 0.29
            cycle.sell_limit_price = 0.29
            cycle.upload_limit_price = MagicMock()
            cycle.last_edit_time = 0.0

            # 1. Market ask is at 0.30 (competitor). Ideal sell = 0.29, which is <= manual price.
            cycle.get_external_best_ask = MagicMock(return_value=0.30)
            cycle._peg_sell_order(now=100.0)
            # Must stay at manual price 0.29 without editing
            self.assertEqual(cycle.sell_peg_status, "MANUAL_PRICE_0.29")
            cycle.upload_limit_price.assert_not_called()
            self.assertTrue(cycle.manual_sell_override)

            # 2. Market ask bounces up to 0.38!
            cycle.get_external_best_ask = MagicMock(return_value=0.38)
            cycle.last_edit_time = 0.0
            cycle._peg_sell_order(now=200.0)

            # Must float up to 0.37 and unlock manual override
            self.assertEqual(cycle.sell_peg_status, "FLOATING_UP_0.37")
            cycle.upload_limit_price.assert_called_once_with(0.37, side="sell", is_manual=False)
            self.assertFalse(cycle.manual_sell_override)
            self.assertIsNone(cycle.manual_sell_price)
            print("-> test_manual_sell_price_floats_up_on_market_bounce passed!")

if __name__ == "__main__":
    unittest.main()
