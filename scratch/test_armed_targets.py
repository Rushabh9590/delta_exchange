import unittest
from unittest.mock import MagicMock, patch
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "python-rest-client"))

from armed_targets import ArmedTargetManager, align_price


class TestArmedTargetManager(unittest.TestCase):
    def setUp(self):
        self.mgr = ArmedTargetManager()
        self.mgr._is_running = False  # disable background thread for test

    def test_align_price(self):
        self.assertEqual(align_price(9.04, 0.1), 9.0)
        self.assertEqual(align_price(9.06, 0.1), 9.1)
        self.assertEqual(align_price(18.0, 0.5), 18.0)
        self.assertEqual(align_price(18.2, 0.5), 18.0)
        self.assertEqual(align_price(18.3, 0.5), 18.5)

    @patch("armed_targets.get_delta_client")
    def test_create_order_with_ladder(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_products.return_value = [
            {"id": 12345, "symbol": "P-BTC-76800-271026", "tick_size": 0.1, "contract_value": 0.001}
        ]
        mock_client.place_order.return_value = {
            "id": 99901,
            "state": "open",
            "product_id": 12345,
            "side": "buy",
            "size": 10
        }
        mock_get_client.return_value = mock_client

        targets = [
            {"size": 1, "price": 18.0, "side": "sell"},
            {"size": 1, "price": 36.0, "side": "sell"},
            {"size": 1, "price": 72.0, "side": "sell"}
        ]

        res = self.mgr.create_order_with_ladder(
            symbol="P-BTC-76800-271026",
            side="buy",
            size=10,
            order_type="limit",
            limit_price=9.0,
            time_in_force="gtc",
            post_only=True,
            targets=targets
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["entry_order_id"], 99901)
        self.assertEqual(res["targets_count"], 3)
        self.assertEqual(res["cycle"]["status"], "ARMED_WAITING_FILL")

        # Test fill transition
        mock_client.place_order.side_effect = lambda **kwargs: {"id": 88800 + int(kwargs.get("size", 1)), "state": "open"}
        cycle = self.mgr.active_cycles[res["cycle_id"]]
        self.mgr._submit_target_ladder(cycle, mock_client)

        self.assertEqual(cycle["status"], "TARGETS_SUBMITTED")
        for t in cycle["targets"]:
            self.assertEqual(t["status"], "PLACED")
            self.assertIsNotNone(t["order_id"])

        print("[TEST PASS] ArmedTargetManager ladder creation, state machine, and target execution verified!")


if __name__ == "__main__":
    unittest.main()
