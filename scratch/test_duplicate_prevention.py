import unittest
from unittest.mock import MagicMock, patch
import json
import os
import sys

# Ensure workspace in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_execution import MMTradeCycle, DeltaMMEngine

MOCK_PROD = {
    "id": 12345,
    "symbol": "C-ETH-2450-211026",
    "tick_size": 0.1,
    "contract_value": 0.01,
    "quoting_asset": {"symbol": "USD"},
    "settling_asset": {"symbol": "USDT"}
}

class TestDuplicatePrevention(unittest.TestCase):

    @patch("mm_execution.get_product_details", return_value=MOCK_PROD)
    def test_reconcile_adopts_live_orders_and_cancels_duplicates(self, mock_gpd):
        """Verify reconcile_with_exchange adopts live orders and cancels extra duplicates."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"product_id": 12345, "tick_size": "0.1"}

        # Simulate 2 live SELL orders on exchange for the same position (duplicate bug on exchange)
        mock_client.get_live_orders.return_value = [
            {"id": 101, "product_id": 12345, "side": "sell", "state": "open", "size": 2, "unfilled_size": 2, "limit_price": "5.5"},
            {"id": 102, "product_id": 12345, "side": "sell", "state": "open", "size": 2, "unfilled_size": 2, "limit_price": "5.5"}
        ]
        # Simulate position = 2 lots
        mock_client.get_position.return_value = {"size": "2", "entry_price": "5.0"}

        cycle = MMTradeCycle(symbol="C-ETH-2450-211026", size=2, client=mock_client)
        cycle.buy_filled_qty = 2
        cycle.sell_order_id = None  # Lost or not persisted
        cycle.sell_placed_qty = 0

        resumed = cycle.reconcile_with_exchange()
        self.assertTrue(resumed)
        # Should adopt order 101
        self.assertEqual(cycle.sell_order_id, 101)
        self.assertEqual(cycle.sell_placed_qty, 2)
        self.assertEqual(cycle.sell_limit_price, 5.5)

        # Should cancel duplicate order 102!
        mock_client.cancel_order.assert_called_with(product_id=cycle.product_id, order_id=102)

    @patch("mm_execution.get_product_details", return_value=MOCK_PROD)
    def test_place_sell_exit_order_adopts_existing_live_order(self, mock_gpd):
        """Verify _place_sell_exit_order never calls place_order if a live order already exists."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"product_id": 12345, "tick_size": "0.1"}
        mock_client.get_position.return_value = {"size": "2", "entry_price": "5.0"}
        mock_client.get_live_orders.return_value = [
            {"id": 201, "product_id": 12345, "side": "sell", "state": "open", "size": 2, "unfilled_size": 2, "limit_price": "6.0"}
        ]

        cycle = MMTradeCycle(symbol="C-ETH-2450-211026", size=2, client=mock_client)
        cycle.buy_fill_price = 5.0
        cycle.buy_filled_qty = 2

        success = cycle._place_sell_exit_order(target_qty=2)
        self.assertTrue(success)
        self.assertEqual(cycle.sell_order_id, 201)
        self.assertEqual(cycle.sell_limit_price, 6.0)

        # CRITICAL: place_order must NOT have been called!
        mock_client.place_order.assert_not_called()

    @patch("mm_execution.get_product_details", return_value=MOCK_PROD)
    def test_alien_order_from_different_product_is_never_adopted(self, mock_gpd):
        """Verify orders belonging to a DIFFERENT product are NEVER adopted by this cycle."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"product_id": 12345, "tick_size": "0.1"}
        mock_client.get_position.return_value = {"size": "0"}
        # Alien order on product 99999 (e.g. ETH 2360) while this cycle is on product 12345 (ETH 2450)
        mock_client.get_live_orders.return_value = [
            {"id": 99901, "product_id": 99999, "side": "buy", "state": "open", "size": 3, "unfilled_size": 3, "limit_price": "0.05"}
        ]
        mock_client.place_order.return_value = {"id": 12301, "state": "open"}

        cycle = MMTradeCycle(symbol="C-ETH-2450-211026", size=3, client=mock_client)
        cycle.fetch_live_quote = MagicMock(return_value={"bid": 1.0, "ask": 2.0})

        # _phase_1_place_buy should ignore alien order 99901 and place its own fresh order on 12345!
        success = cycle._phase_1_place_buy()
        self.assertTrue(success)
        self.assertEqual(cycle.buy_order_id, 12301)
        self.assertNotEqual(cycle.buy_order_id, 99901)
        mock_client.place_order.assert_called_once()

    @patch("mm_execution.get_product_details", return_value=MOCK_PROD)
    def test_recovery_never_resurrects_duplicates_for_same_product(self, mock_gpd):
        """Verify recover_persisted_cycles never starts multiple cycles for the same contract."""
        mock_client = MagicMock()
        mock_client.get_position.return_value = {"size": "2", "entry_price": "5.0"}
        mock_client.get_live_orders.return_value = [
            {"id": 301, "product_id": 12345, "side": "sell", "state": "open", "size": 2, "unfilled_size": 2, "limit_price": "6.0"}
        ]

        with patch.object(DeltaMMEngine, "recover_persisted_cycles"):
            engine = DeltaMMEngine()
        engine.active_cycles = {}

        # Simulate 2 active cycles for the exact same contract
        active_trades = {
            "t_old": {
                "trade_id": "t_old",
                "symbol": "C-ETH-2450-211026",
                "product_id": 12345,
                "account": "main",
                "state": "SELL_OPEN",
                "size": 2,
                "total_size": 2,
                "buy_filled_qty": 2,
                "buy_unfilled_qty": 0,
                "sell_filled_qty": 0,
                "created_at": "2026-10-21T10:00:00Z"
            },
            "t_new": {
                "trade_id": "t_new",
                "symbol": "C-ETH-2450-211026",
                "product_id": 12345,
                "account": "main",
                "state": "SELL_OPEN",
                "size": 2,
                "total_size": 2,
                "buy_filled_qty": 2,
                "buy_unfilled_qty": 0,
                "sell_filled_qty": 0,
                "created_at": "2026-10-21T10:05:00Z"
            }
        }

        # Simulate 1 historical cycle for the same contract
        history = [
            {
                "trade_id": "t_hist",
                "symbol": "C-ETH-2450-211026",
                "product_id": 12345,
                "account": "main",
                "state": "COMPLETED_PROFIT",
                "size": 2,
                "total_size": 2,
                "buy_filled_qty": 2,
                "buy_unfilled_qty": 0,
                "sell_filled_qty": 2,
                "created_at": "2026-10-21T09:00:00Z"
            }
        ]

        with patch("mm_execution.load_all_trades_state", return_value={"active_trades": active_trades, "history": history}):
            with patch("mm_execution.get_delta_client", return_value=mock_client):
                with patch("mm_execution.save_all_trades_state"):
                    with patch.object(MMTradeCycle, "start_recovered"):
                        engine.recover_persisted_cycles()

        # EXACTLY 1 cycle must be active!
        self.assertEqual(len(engine.active_cycles), 1)
        # The newer cycle should be the one active
        self.assertIn("t_new", engine.active_cycles)
        self.assertNotIn("t_old", engine.active_cycles)
        self.assertNotIn("t_hist", engine.active_cycles)

if __name__ == "__main__":
    unittest.main()
