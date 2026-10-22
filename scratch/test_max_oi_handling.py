import os
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import unittest
from unittest.mock import MagicMock, patch
import mm_strikes
import mm_execution
from mm_execution import MMTradeCycle, DeltaMMEngine

class TestMaxOIHandling(unittest.TestCase):
    def test_calculate_mm_metrics_max_oi(self):
        """Verify that calculate_mm_metrics marks contract unqualified if oi_reduce_only_mode is True."""
        res = mm_strikes.calculate_mm_metrics(
            bid=1.0,
            ask=2.0,
            tick_size=0.1,
            min_bid_price=0.3,
            min_spread_ticks=5,
            min_net_edge_pct=20,
            oi_reduce_only_mode=True
        )
        self.assertFalse(res["qualified"])
        self.assertTrue(any("Max OI Breached" in r for r in res["reasons_failed"]))

    def test_engine_start_trade_blocks_reduce_only(self):
        """Verify that DeltaMMEngine.start_trade raises ValueError for contracts with oi_reduce_only_mode."""
        engine = DeltaMMEngine()
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"symbol": "TEST-BREACHED", "oi_reduce_only_mode": True}

        with patch("mm_execution.get_delta_client", return_value=mock_client):
            with self.assertRaises(ValueError) as ctx:
                engine.start_trade(symbol="TEST-BREACHED", size=1)
            self.assertIn("REDUCE-ONLY", str(ctx.exception))
            self.assertIn("Maximum Open Interest", str(ctx.exception))

    def test_phase_1_place_buy_preflight_max_oi(self):
        """Verify MMTradeCycle._phase_1_place_buy sets REJECTED_MAX_OI if ticker has oi_reduce_only_mode."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"symbol": "TEST-BREACHED", "oi_reduce_only_mode": True}

        with patch("mm_execution.get_product_details", return_value={"id": 9999, "tick_size": 0.1, "contract_value": 0.001}):
            cycle = MMTradeCycle(symbol="TEST-BREACHED", size=1, client=mock_client)
            ok = cycle._phase_1_place_buy()
            self.assertFalse(ok)
            self.assertEqual(cycle.state, "REJECTED_MAX_OI")
            self.assertTrue(any("MAX OI BREACHED" in log for log in cycle.logs))

    def test_phase_1_place_buy_catch_exchange_error(self):
        """Verify MMTradeCycle._phase_1_place_buy handles exchange rejection error {'reason': 'max_oi_breached'}."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = {"symbol": "TEST-BREACHED", "oi_reduce_only_mode": False}
        mock_client.place_order.side_effect = Exception("{'code': 'only_reduce_only_orders_allowed', 'context': {'reason': 'max_oi_breached'}}")

        with patch("mm_execution.get_product_details", return_value={"id": 9999, "tick_size": 0.1, "contract_value": 0.001}):
            cycle = MMTradeCycle(symbol="TEST-BREACHED", size=1, client=mock_client)
            # Mock live quote so it proceeds to order placement
            cycle.get_external_best_bid = MagicMock(return_value=1.0)
            cycle.get_external_best_ask = MagicMock(return_value=2.0)
            ok = cycle._phase_1_place_buy()
            self.assertFalse(ok)
            self.assertEqual(cycle.state, "REJECTED_MAX_OI")
            self.assertTrue(any("MAX OI BREACHED" in log for log in cycle.logs))

if __name__ == "__main__":
    unittest.main()
