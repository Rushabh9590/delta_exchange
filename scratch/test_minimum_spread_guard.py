import os
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import unittest
from unittest.mock import MagicMock, patch
import time
from mm_execution import MMTradeCycle

class TestMinimumSpreadGuard(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.get_ticker.return_value = {"oi_reduce_only_mode": False}
        self.mock_client.get_live_orders.return_value = []
        self.mock_client.get_order_by_id.return_value = {
            "id": 1001,
            "product_id": 12345,
            "state": "open",
            "limit_price": "2.2",
            "size": 1,
            "unfilled_size": 1
        }
        self.mock_client.place_order.return_value = {"id": 1001, "state": "open"}
        self.mock_client.edit_order.return_value = {"id": 1001, "state": "open"}

    def test_trap_prevention_when_competitor_jumps_near_ask(self):
        """
        Replicates USER SCENARIO:
        Initial Bid 2.2, Ask 4.0. Competitor jumps to 3.9.
        Minimum 60% Spread Guard must BLOCK chasing to 4.0, preserving capital.
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.1, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=2.2,
                min_spread_pct=0.60,
                client=self.mock_client
            )
            cycle.get_external_best_bid = MagicMock(return_value=2.2)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 2.2, "ask": 4.0})

            # Phase 1: Place initial buy at 2.2
            res = cycle._phase_1_place_buy()
            self.assertTrue(res)
            self.assertEqual(cycle.buy_limit_price, 2.2)
            # Ceiling should be 4.0 - (1.8 * 0.60) = 2.92 -> 2.90
            self.assertEqual(cycle.max_entry_price, 2.9)
            self.mock_client.place_order.assert_called_once()
            self.mock_client.edit_order.reset_mock()

            # Predator jumps to 3.90 against 4.00 ask!
            cycle.get_external_best_bid = MagicMock(return_value=3.9)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 3.9, "ask": 4.0})

            # Run dynamic peg
            cycle.last_edit_time = 0.0
            cycle._peg_buy_order(time.time() + 10.0)

            # VERIFY: Guard was triggered, edit_order was NOT called, buy limit remained at 2.2!
            self.mock_client.edit_order.assert_not_called()
            self.assertEqual(cycle.buy_limit_price, 2.2)
            self.assertEqual(cycle.buy_peg_status, "GUARD_SPREAD_COLLAPSED")
            self.assertTrue(any("[60% SPREAD GUARD TRAP DETECTED]" in log for log in cycle.logs))
            print("-> test_trap_prevention_when_competitor_jumps_near_ask passed!")

    def test_live_60_percent_spread_calculation(self):
        """
        Verifies live backend calculation of 60% spread retention:
        - When spread is wide enough, allows outbidding up to 60% ceiling.
        - When competitor breaches 60% spread, blocks chasing.
        - When market ask moves live, ceiling dynamically adjusts.
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.1, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                entry_price=2.2,
                min_spread_pct=0.60,
                max_chase_ticks=10,
                client=self.mock_client
            )
            cycle.get_external_best_bid = MagicMock(return_value=2.2)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 2.2, "ask": 4.0})

            cycle._phase_1_place_buy()
            # Spread = 1.8. 60% = 1.08. Max entry ceiling = 4.0 - 1.08 = 2.92 -> 2.90
            self.assertEqual(cycle.max_entry_price, 2.9)

            # Case A: Competitor bids 2.50.
            # Remaining spread = 4.0 - 2.5 = 1.50 >= 1.08 (OK). Ideal buy = 2.60 <= 2.90.
            cycle.get_external_best_bid = MagicMock(return_value=2.5)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 2.5, "ask": 4.0})
            cycle.last_edit_time = 0.0
            cycle._peg_buy_order(time.time() + 10.0)

            self.mock_client.edit_order.assert_called_once()
            args = self.mock_client.edit_order.call_args[0][0]
            self.assertEqual(args["limit_price"], "2.6")
            self.assertEqual(cycle.buy_limit_price, 2.6)
            self.mock_client.edit_order.reset_mock()

            # Case B: Competitor bids 3.00.
            # Remaining spread = 4.0 - 3.0 = 1.00 < 1.08 (< 60% spread).
            # Chasing must be blocked!
            cycle.get_external_best_bid = MagicMock(return_value=3.0)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 3.0, "ask": 4.0})
            cycle.last_edit_time = 0.0
            cycle._peg_buy_order(time.time() + 20.0)

            self.mock_client.edit_order.assert_not_called()
            self.assertEqual(cycle.buy_limit_price, 2.6)
            self.assertEqual(cycle.buy_peg_status, "GUARD_SPREAD_COLLAPSED")

            # Case C: Ask moves UP to 4.50 live!
            # Live spread = 4.50 - 2.20 = 2.30. 60% spread = 1.38. Live ceiling = 4.50 - 1.38 = 3.12 -> 3.10.
            # Now competitor at 3.00 leaves remaining spread = 4.50 - 3.00 = 1.50 >= 1.38 (valid!).
            cycle.max_entry_price = None  # allow dynamic live ceiling
            cycle.get_external_best_bid = MagicMock(return_value=3.0)
            cycle.get_external_best_ask = MagicMock(return_value=4.5)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 3.0, "ask": 4.5})
            cycle.last_edit_time = 0.0
            cycle._peg_buy_order(time.time() + 30.0)

            self.mock_client.edit_order.assert_called_once()
            args = self.mock_client.edit_order.call_args[0][0]
            self.assertEqual(args["limit_price"], "3.1")
            self.assertEqual(cycle.buy_limit_price, 3.1)
            print("-> test_live_60_percent_spread_calculation passed!")

    def test_preflight_tight_spread_rejection(self):
        """
        Verifies that bot refuses to enter a strike where market spread is already collapsed (< 3 ticks).
        """
        with patch("mm_execution.get_product_details", return_value={"id": 12345, "tick_size": 0.1, "contract_value": 0.001}):
            cycle = MMTradeCycle(
                symbol="C-BTC-90000-261026",
                size=1,
                min_spread_pct=0.60,
                client=self.mock_client
            )
            cycle.get_external_best_bid = MagicMock(return_value=3.9)
            cycle.get_external_best_ask = MagicMock(return_value=4.0)
            cycle.fetch_live_quote = MagicMock(return_value={"bid": 3.9, "ask": 4.0})

            res = cycle._phase_1_place_buy()
            self.assertFalse(res)
            self.assertEqual(cycle.state, "REJECTED_TIGHT_SPREAD")
            self.mock_client.place_order.assert_not_called()
            self.assertTrue(any("too tight" in log for log in cycle.logs))
            print("-> test_preflight_tight_spread_rejection passed!")

if __name__ == "__main__":
    unittest.main()
