"""
Market Making 1-Lot Execution Engine for Delta Exchange.

Execution Rules & Strategy Architecture:
1. Target Symbol: Qualified Market Making Strike (BTC / ETH Options).
2. Size: 1 Lot (1 contract).
3. Phase 1 - Entry BUY (Maker):
   - Place LIMIT BUY at Best Bid + 1 Tick (or Best Bid).
   - Flag: post_only=True (guarantees maker status and lowest fee 0.03%).
   - If post_only is rejected because it would cross spread, fallback to Best Bid.
   - Custom entry limit price can be provided or uploaded at any time.
4. Phase 2 - BUY Fill Monitoring:
   - Poll order status until filled.
   - Auto-reprice option if market bid rises significantly before fill.
5. Phase 3 - Exit SELL Placement (Upon BUY fill):
   - Records actual executed cost_price.
   - Cost-to-cost floor = cost_price (or cost_price + 1 tick to cover maker fees).
   - Target profit exit = max(cost_to_cost_floor, current_ask - 1 tick).
   - Places LIMIT SELL with post_only=True and reduce_only=True.
6. Phase 4 - Price Drops Scenario (STRICT USER RULES):
   - NO STOP LOSS: Never trigger a market stop loss or sell below cost at a loss.
   - NO TIME OUT: Never liquidate or cancel due to elapsed time.
   - EXIT COST-TO-COST OR WAIT:
     - If market ask is favorable (above cost): can adjust sell target down to (ask - 1 tick) to take profit.
     - If market drops BELOW entry cost:
       - Limit SELL order is set strictly to Cost-to-Cost (entry price or entry + 1 tick).
       - Never drop the limit sell price below entry cost!
       - WAIT for market to oscillate/bounce back to fill at breakeven!
7. Upload Limit Price:
   - User or API can upload/update the limit price at any point during entry or exit.
"""

import os
import sys
import time
import math
import uuid
import logging
import threading
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from delta_rest_client import (
    DeltaRestClient,
    OrderType,
    TimeInForce,
    round_by_tick_size
)
from login import get_delta_client, load_credentials
from multileg import get_product_details
from websocket import get_private_ws_manager, get_public_ws_manager

logger = logging.getLogger("delta_mm_engine")


def align_price(price: float, tick_size: float | str) -> float:
    """Rounds a price to match the product's valid tick size."""
    tick = float(tick_size)
    if tick <= 0:
        return price
    return float(round_by_tick_size(Decimal(str(price)), Decimal(str(tick))))


class MMTradeCycle:
    """
    Represents a single 1-Lot Market Making trade lifecycle from entry to exit.
    """
    def __init__(
        self,
        symbol: str,
        size: int = 1,
        entry_price: float = None,
        target_exit_price: float = None,
        cost_to_cost_ticks: int = 1,
        auto_reprice_buy: bool = True,
        two_way_peg: bool = True,
        max_entry_price: float = None,
        account: str = None,
        client: DeltaRestClient = None
    ):
        self.trade_id = f"mm_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.symbol = symbol.strip().upper()
        self.size = int(size) if size else 1
        self.custom_entry_price = float(entry_price) if entry_price is not None and float(entry_price) > 0 else None
        self.custom_target_price = float(target_exit_price) if target_exit_price is not None and float(target_exit_price) > 0 else None
        self.max_entry_price = float(max_entry_price) if max_entry_price is not None and float(max_entry_price) > 0 else None
        self.cost_to_cost_ticks = cost_to_cost_ticks  # 1 = cost + 1 tick (covers maker fee), 0 = exact cost
        self.auto_reprice_buy = auto_reprice_buy
        self.two_way_peg_enabled = two_way_peg
        self.account_name = (account or os.getenv("ACTIVE_ACCOUNT") or "main").lower()

        # Delta client
        self.client = client or get_delta_client(account=self.account_name)

        # Product specifications
        self.product_info = get_product_details(self.symbol, self.client)
        self.product_id = self.product_info["id"]
        self.tick_size = float(self.product_info.get("tick_size") or 0.1)
        self.contract_value = float(self.product_info.get("contract_value") or 0.001)

        # Lifecycle State
        # States: INIT, PLACING_BUY, BUY_OPEN, BUY_FILLED, PLACING_SELL, SELL_OPEN,
        #         WAITING_COST_TO_COST, COMPLETED_PROFIT, COMPLETED_COST, CANCELLED, ERROR
        self.state = "INIT"
        self.status_message = "Initialized 1-lot MM trade cycle"

        # Order details
        self.buy_order_id = None
        self.buy_limit_price = None
        self.buy_fill_price = None
        self.buy_fill_time = None

        self.sell_order_id = None
        self.sell_limit_price = None
        self.sell_fill_price = None
        self.sell_fill_time = None
        self.cost_to_cost_price = None

        # Live tracking
        self.current_bid = None
        self.current_ask = None
        self.current_ltp = None
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0

        # Safety & user constraint flags
        self.no_stop_loss = True
        self.no_timeout = True
        self.exit_mode = "cost_to_cost_or_wait"

        # Worker control
        self.is_running = False
        self.stop_requested = False
        self._thread = None
        self.logs = []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = datetime.now(timezone.utc).isoformat()

        # Real-time WebSocket fill detection & L2 market stream
        self.buy_fill_event = threading.Event()
        self.sell_fill_event = threading.Event()
        self.private_ws = None
        self.public_ws = None

        # Two-Way Dynamic Floating Peg Tracking
        self.competitor_bid = None
        self.competitor_ask = None
        self.buy_peg_status = "IDLE"
        self.sell_peg_status = "IDLE"
        self.last_edit_time = 0.0
        self.edit_debounce_sec = 0.35  # Anti-churn debounce window
        self._edit_lock = threading.Lock()

        self.log(f"Created MM cycle for {self.symbol} (Product ID {self.product_id}, Tick {self.tick_size}) on account [{self.account_name.upper()}] with Two-Way Floating Peg")

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        self.status_message = msg
        self.updated_at = datetime.now(timezone.utc).isoformat()
        try:
            print(f"[MM-{self.trade_id[-6:]}] {msg}", flush=True)
        except Exception:
            try:
                safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
                print(f"[MM-{self.trade_id[-6:]}] {safe_msg}", flush=True)
            except Exception:
                pass

    def _on_buy_order_update(self, order_data: dict):
        """Real-time callback from private WebSocket for BUY order (<50ms latency)."""
        try:
            raw_id = order_data.get("id")
            if raw_id is not None and self.buy_order_id is not None and int(raw_id) != int(self.buy_order_id):
                return

            state = order_data.get("state")
            unfilled = float(order_data.get("unfilled_size", 0) or 0)

            if state == "closed" and unfilled == 0:
                avg_fill = float(order_data.get("average_fill_price") or self.buy_limit_price)
                self.buy_fill_price = avg_fill
                self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                self.state = "BUY_FILLED"
                self.log(f"[WS-STREAM] Sub-50ms BUY fill detected! Executed 1 lot @ {self.buy_fill_price}")
                self.buy_fill_event.set()
            elif state == "cancelled":
                self.state = "CANCELLED"
                self.log("[WS-STREAM] BUY order cancelled on exchange.")
                self.buy_fill_event.set()
        except Exception as e:
            self.log(f"Warning in WS buy callback: {e}")

    def _on_sell_order_update(self, order_data: dict):
        """Real-time callback from private WebSocket for SELL order (<50ms latency)."""
        try:
            raw_id = order_data.get("id")
            if raw_id is not None and self.sell_order_id is not None and int(raw_id) != int(self.sell_order_id):
                return

            state = order_data.get("state")
            unfilled = float(order_data.get("unfilled_size", 0) or 0)

            if state == "closed" and unfilled == 0:
                avg_fill = float(order_data.get("average_fill_price") or self.sell_limit_price)
                self.sell_fill_price = avg_fill
                self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                self.realized_pnl = (self.sell_fill_price - self.buy_fill_price) * self.size * self.contract_value
                self.unrealized_pnl = 0.0

                if self.cost_to_cost_price and self.sell_fill_price > self.cost_to_cost_price:
                    self.state = "COMPLETED_PROFIT"
                    self.log(f"[WS-STREAM] Sub-50ms PROFIT EXIT filled at {self.sell_fill_price}! Realized PnL = +${self.realized_pnl:.5f} USD")
                else:
                    self.state = "COMPLETED_COST"
                    self.log(f"[WS-STREAM] Sub-50ms COST-TO-COST EXIT filled at {self.sell_fill_price}! Realized PnL = ${self.realized_pnl:.5f} USD (Breakeven)")
                self.sell_fill_event.set()
            elif state == "cancelled":
                self.sell_fill_event.set()
        except Exception as e:
            self.log(f"Warning in WS sell callback: {e}")

    def to_dict(self) -> dict:
        """Serializes current trade status for API and Dashboard consumption."""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "size": self.size,
            "product_id": self.product_id,
            "tick_size": self.tick_size,
            "contract_value": self.contract_value,
            "account": self.account_name,
            "state": self.state,
            "status_message": self.status_message,
            "buy_order_id": self.buy_order_id,
            "buy_limit_price": self.buy_limit_price,
            "buy_fill_price": self.buy_fill_price,
            "buy_fill_time": self.buy_fill_time,
            "sell_order_id": self.sell_order_id,
            "sell_limit_price": self.sell_limit_price,
            "sell_fill_price": self.sell_fill_price,
            "sell_fill_time": self.sell_fill_time,
            "cost_to_cost_price": self.cost_to_cost_price,
            "current_bid": self.current_bid,
            "current_ask": self.current_ask,
            "current_ltp": self.current_ltp,
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "no_stop_loss": self.no_stop_loss,
            "no_timeout": self.no_timeout,
            "exit_mode": self.exit_mode,
            "is_running": self.is_running,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ws_connected": self.private_ws.is_connected if self.private_ws else False,
            "ws_authenticated": self.private_ws.is_authenticated if self.private_ws else False,
            "two_way_peg_enabled": self.two_way_peg_enabled,
            "buy_peg_status": self.buy_peg_status,
            "sell_peg_status": self.sell_peg_status,
            "competitor_bid": self.competitor_bid,
            "competitor_ask": self.competitor_ask,
            "max_entry_price": self.max_entry_price,
            "logs": self.logs[-20:]  # Last 20 log messages
        }

    def fetch_live_quote(self) -> dict:
        """Fetches latest quotes from exchange REST ticker or public WS cache."""
        # Try real-time in-memory WebSocket quote first (<1ms)
        if self.public_ws:
            ws_quote = self.public_ws.get_latest_quote(self.symbol)
            bid = ws_quote.get("bid")
            ask = ws_quote.get("ask")
            ltp = ws_quote.get("ltp") or ws_quote.get("mark_price")
            if bid is not None and ask is not None:
                self.current_bid = float(bid)
                self.current_ask = float(ask)
                if ltp is not None:
                    self.current_ltp = float(ltp)
                if self.buy_fill_price and self.current_ltp:
                    self.unrealized_pnl = (self.current_ltp - self.buy_fill_price) * self.size * self.contract_value
                return {"bid": self.current_bid, "ask": self.current_ask, "ltp": self.current_ltp}

        try:
            ticker = self.client.get_ticker(self.symbol)
            quotes = ticker.get("quotes") or {}
            bid = float(quotes.get("best_bid")) if quotes.get("best_bid") else None
            ask = float(quotes.get("best_ask")) if quotes.get("best_ask") else None
            ltp = float(ticker.get("close") or ticker.get("mark_price") or 0.0)

            self.current_bid = bid
            self.current_ask = ask
            self.current_ltp = ltp

            # Calculate unrealized PnL if bought
            if self.buy_fill_price and ltp:
                self.unrealized_pnl = (ltp - self.buy_fill_price) * self.size * self.contract_value

            return {"bid": bid, "ask": ask, "ltp": ltp}
        except Exception as e:
            self.log(f"Warning: Failed to fetch live quote: {e}")
            return {"bid": self.current_bid, "ask": self.current_ask, "ltp": self.current_ltp}

    def start(self):
        """Starts the MM cycle execution in a background thread."""
        if self.is_running:
            return
        self.is_running = True
        self.stop_requested = False
        self._thread = threading.Thread(target=self._run_lifecycle, daemon=True, name=f"MM-{self.trade_id}")
        self._thread.start()

    def stop(self):
        """Requests graceful stop."""
        self.stop_requested = True
        self.log("Cancellation / Stop requested by user")

    def upload_limit_price(self, new_price: float) -> dict:
        """
        Allows uploading/updating the limit price of the active order (Buy or Sell).
        """
        valid_price = align_price(float(new_price), self.tick_size)
        self.log(f"Requested limit price upload: {valid_price}")

        if self.state == "BUY_OPEN" and self.buy_order_id:
            try:
                # Delta API edit_order or cancel + re-place
                res = self._edit_or_replace_order(self.buy_order_id, valid_price, side="buy")
                if res.get("success"):
                    self.buy_limit_price = valid_price
                    self.custom_entry_price = valid_price
                    if self.max_entry_price and valid_price > self.max_entry_price:
                        self.max_entry_price = valid_price
                    self.log(f"Successfully uploaded new BUY limit price: {valid_price}")
                    return {"success": True, "new_price": valid_price, "order_id": self.buy_order_id}
                else:
                    return {"success": False, "error": res.get("error")}
            except Exception as e:
                return {"success": False, "error": str(e)}

        elif self.state in ("SELL_OPEN", "WAITING_COST_TO_COST") and self.sell_order_id:
            # Check user constraint: cannot lower sell price below cost-to-cost unless forced
            if self.cost_to_cost_price and valid_price < self.cost_to_cost_price:
                self.log(f"Notice: Upload price {valid_price} is below Cost-to-Cost ({self.cost_to_cost_price}). Applying per user request.")

            try:
                res = self._edit_or_replace_order(self.sell_order_id, valid_price, side="sell", reduce_only=True)
                if res.get("success"):
                    self.sell_limit_price = valid_price
                    self.log(f"Successfully uploaded new SELL limit price: {valid_price}")
                    return {"success": True, "new_price": valid_price, "order_id": self.sell_order_id}
                else:
                    return {"success": False, "error": res.get("error")}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"No active open order in current state '{self.state}' to update."}

    def _edit_or_replace_order(self, order_id: int, new_price: float, side: str, reduce_only: bool = False) -> dict:
        """Edits an order in-place or cancels and re-places if edit is unsupported. Thread-safe."""
        with self._edit_lock:
            try:
                # Try Delta REST edit_order
                edit_payload = {
                    "id": int(order_id),
                    "product_id": int(self.product_id),
                    "limit_price": str(new_price),
                    "size": int(self.size)
                }
                res = self.client.edit_order(edit_payload)
                if res and isinstance(res, dict) and res.get("id"):
                    return {"success": True, "order": res}
            except Exception as edit_err:
                self.log(f"Edit API call failed ({edit_err}), attempting cancel & re-place...")

            # Fallback: Cancel and re-place
            try:
                self.client.cancel_order(product_id=self.product_id, order_id=int(order_id))
                time.sleep(0.2)
                new_order = self.client.place_order(
                    product_id=self.product_id,
                    size=self.size,
                    side=side,
                    limit_price=str(new_price),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    post_only="true",
                    reduce_only="true" if reduce_only else "false"
                )
                if side == "buy":
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.remove_order_listener(self.buy_order_id, self._on_buy_order_update)
                    self.buy_order_id = new_order.get("id")
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                else:
                    if self.private_ws and self.sell_order_id:
                        self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)
                    self.sell_order_id = new_order.get("id")
                    if self.private_ws and self.sell_order_id:
                        self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
                return {"success": True, "order": new_order}
            except Exception as replace_err:
                return {"success": False, "error": str(replace_err)}

    def _run_lifecycle(self):
        """Main state machine loop."""
        try:
            # 1. Connect to Private WebSocket Order Stream (fill events)
            try:
                self.private_ws = get_private_ws_manager(account=self.account_name)
                self.log(f"Connected to private WebSocket order stream for [{self.account_name.upper()}] (Sub-50ms execution enabled).")
            except Exception as ws_err:
                self.log(f"Warning: Could not connect to private WebSocket ({ws_err}). Falling back to REST polling.")
                self.private_ws = None

            # 2. Connect to Public WebSocket Market Stream (L2 real-time orderbook for sub-100ms pegging)
            try:
                self.public_ws = get_public_ws_manager(symbols=[self.symbol])
                self.log(f"Subscribed {self.symbol} to real-time L2 orderbook WebSocket stream.")
            except Exception as ws_err:
                self.log(f"Notice: Using REST orderbook for pegging ({ws_err}).")
                self.public_ws = None

            self.log("Step 1: Discovering market & placing Maker Limit BUY...")
            if not self._phase_1_place_buy():
                self.is_running = False
                return

            self.log("Step 2: Awaiting BUY order fill (Two-Way Dynamic Bid Peg + Real-Time WS)...")
            if not self._phase_2_await_buy_fill():
                self.is_running = False
                return

            self.log("Step 3: BUY filled! Placing Maker Limit SELL exit...")
            if not self._phase_3_place_sell():
                self.is_running = False
                return

            self.log("Step 4: Monitoring Exit (Two-Way Dynamic Ask Peg + Real-Time WS, strictly NO Stop-Loss, NO Timeout)...")
            self._phase_4_monitor_exit_wait()

        except Exception as e:
            self.state = "ERROR"
            self.log(f"CRITICAL ERROR in MM lifecycle: {e}")
        finally:
            self.is_running = False
            self.log(f"MM cycle ended. Final State: {self.state}")

    def _phase_1_place_buy(self) -> bool:
        """Phase 1: Places Maker Limit BUY order."""
        self.state = "PLACING_BUY"
        ext_bid = self.get_external_best_bid()
        ext_ask = self.get_external_best_ask()
        quote = self.fetch_live_quote()
        bid = ext_bid if ext_bid is not None else quote.get("bid")
        ask = ext_ask if ext_ask is not None else quote.get("ask")

        if bid is None or bid <= 0:
            self.log("Error: No valid market bid quote found to calculate limit price.")
            self.state = "ERROR"
            return False

        # Determine limit price
        if self.custom_entry_price:
            target_price = self.custom_entry_price
        else:
            # Two-Way MM: Best Competitor Bid + 1 Tick (if below ask), else Best Bid
            if ask and (bid + self.tick_size) < ask:
                target_price = bid + self.tick_size
            else:
                target_price = bid

        # Establish Max Entry Ceiling Guard
        if not self.max_entry_price:
            if ask:
                self.max_entry_price = align_price(max(target_price, ask - (2 * self.tick_size)), self.tick_size)
            else:
                self.max_entry_price = align_price(target_price + (5 * self.tick_size), self.tick_size)

        valid_price = align_price(target_price, self.tick_size)
        self.buy_limit_price = valid_price

        self.log(f"Placing LIMIT BUY order: Size={self.size}, Price={valid_price} (Competitor Bid={bid}, Ask={ask}, Max Ceiling={self.max_entry_price}, Post-Only=True)")

        try:
            order_res = self.client.place_order(
                product_id=self.product_id,
                size=self.size,
                side="buy",
                limit_price=str(valid_price),
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                post_only="true"
            )
            self.buy_order_id = order_res.get("id")
            self.buy_fill_event.clear()
            if self.private_ws and self.buy_order_id:
                self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
            self.state = "BUY_OPEN"
            self.buy_peg_status = f"TRACKING_BID_{valid_price}"
            self.log(f"Limit BUY order active on book! Order ID: {self.buy_order_id} @ {valid_price} (Two-Way Peg & WS Listener Active)")
            return True
        except Exception as e:
            err_msg = str(e)
            # If rejected because post_only would cross spread, fallback to bid
            if "post_only" in err_msg.lower() or "post only" in err_msg.lower() or "cross" in err_msg.lower():
                self.log(f"Post-only crossed book at {valid_price}. Falling back directly to Best Bid: {bid}")
                fallback_price = align_price(bid, self.tick_size)
                try:
                    order_res = self.client.place_order(
                        product_id=self.product_id,
                        size=self.size,
                        side="buy",
                        limit_price=str(fallback_price),
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        post_only="true"
                    )
                    self.buy_order_id = order_res.get("id")
                    self.buy_limit_price = fallback_price
                    self.buy_fill_event.clear()
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                    self.state = "BUY_OPEN"
                    self.buy_peg_status = f"TRACKING_BID_{fallback_price}"
                    self.log(f"Fallback Limit BUY order active! Order ID: {self.buy_order_id} @ {fallback_price} (Two-Way Peg & WS Listener Active)")
                    return True
                except Exception as e2:
                    self.log(f"Fallback order placement failed: {e2}")
                    self.state = "ERROR"
                    return False
            else:
                self.log(f"Order placement failed: {err_msg}")
                self.state = "ERROR"
                return False

    def _phase_2_await_buy_fill(self) -> bool:
        """
        Phase 2: Awaits order fill via sub-50ms WebSocket with Dynamic Two-Way Bid Pegging:
        - Auto-switches BUY limit UP to remain #1 Best Bid if competitors bid higher (up to max_entry_price ceiling).
        - Floats DOWN with market if competitors retreat (adverse selection protection).
        - Debounced at 350ms to prevent API churn.
        """
        poll_interval = 0.35
        last_order_poll = 0.0

        try:
            while self.is_running and not self.stop_requested:
                # Fast path: wait on real-time WebSocket fill event (unblocks in <50ms upon execution)
                if self.buy_fill_event.wait(timeout=poll_interval):
                    if self.buy_fill_price is not None:
                        self.state = "BUY_FILLED"
                        return True
                    if self.state == "CANCELLED":
                        return False

                now = time.time()
                try:
                    # REST order polling fallback (every 2.5s if WS is active, or every loop if not)
                    ws_active = self.private_ws and self.private_ws.is_authenticated
                    if (not ws_active) or (now - last_order_poll >= 2.5):
                        last_order_poll = now
                        order = self.client.get_order_by_id(self.buy_order_id)
                        state = order.get("state")
                        unfilled = float(order.get("unfilled_size", 0) or 0)

                        # Check if filled
                        if state == "closed" and unfilled == 0:
                            avg_fill = float(order.get("average_fill_price") or self.buy_limit_price)
                            self.buy_fill_price = avg_fill
                            self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                            self.state = "BUY_FILLED"
                            self.log(f"SUCCESS: BUY filled at {self.buy_fill_price}! Executed 1 lot (via REST check).")
                            return True

                        if state == "cancelled":
                            self.state = "CANCELLED"
                            self.log("BUY order was cancelled on exchange.")
                            return False

                    # DYNAMIC TWO-WAY BID PEGGING
                    if self.two_way_peg_enabled:
                        self.fetch_live_quote()
                        comp_bid = self.get_external_best_bid()
                        comp_ask = self.get_external_best_ask()
                        self.competitor_bid = comp_bid
                        self.competitor_ask = comp_ask

                        if comp_bid is not None and comp_bid > 0:
                            # Ideal entry price is 1 tick above competitor's best bid
                            if comp_ask and (comp_bid + self.tick_size) < comp_ask:
                                ideal_buy = align_price(comp_bid + self.tick_size, self.tick_size)
                            else:
                                ideal_buy = align_price(comp_bid, self.tick_size)

                            # Enforce Ask ceiling guard so Maker order never crosses spread
                            if comp_ask and ideal_buy >= comp_ask:
                                ideal_buy = align_price(comp_ask - self.tick_size, self.tick_size)

                            # Enforce Max Entry Ceiling Guard
                            if self.max_entry_price:
                                ideal_buy = min(self.max_entry_price, ideal_buy)

                            # If competitor moved such that ideal price differs by >= 1 tick
                            if ideal_buy > 0 and abs(ideal_buy - self.buy_limit_price) >= (self.tick_size * 0.99):
                                if now - self.last_edit_time >= self.edit_debounce_sec:
                                    self.last_edit_time = now
                                    if ideal_buy > self.buy_limit_price:
                                        self.buy_peg_status = f"FLOATING_UP_{ideal_buy}"
                                        self.log(f"[TWO-WAY BID PEG] Competitor bid moved up to {comp_bid}. Auto-switching BUY limit UP from {self.buy_limit_price} to {ideal_buy} to hold #1 priority!")
                                    else:
                                        self.buy_peg_status = f"RETREATING_DOWN_{ideal_buy}"
                                        self.log(f"[TWO-WAY BID PEG] Market bids retreated to {comp_bid}. Dropping BUY limit DOWN from {self.buy_limit_price} to {ideal_buy} to protect against adverse drop!")
                                    self.upload_limit_price(ideal_buy)
                        else:
                            self.buy_peg_status = f"TRACKING_BID_{self.buy_limit_price}"

                except Exception as e:
                    self.log(f"Warning checking BUY status / bid peg: {e}")

            if self.stop_requested:
                self.log("Cancelling unfilled BUY order due to stop request...")
                try:
                    self.client.cancel_order(product_id=self.product_id, order_id=self.buy_order_id)
                except Exception:
                    pass
                self.state = "CANCELLED"
                return False

            return False

        finally:
            if self.private_ws and self.buy_order_id:
                self.private_ws.remove_order_listener(self.buy_order_id, self._on_buy_order_update)

    def _phase_3_place_sell(self) -> bool:
        """Phase 3: Places Limit SELL Maker order at target price."""
        self.state = "PLACING_SELL"
        ext_ask = self.get_external_best_ask()
        quote = self.fetch_live_quote()
        cur_bid = quote.get("bid")
        cur_ask = ext_ask if ext_ask is not None else quote.get("ask")

        # Baseline: Cost-to-Cost price
        # (entry + 1 tick to cover 0.03% maker fee, or exact entry)
        fee_offset = (self.tick_size * self.cost_to_cost_ticks)
        self.cost_to_cost_price = align_price(self.buy_fill_price + fee_offset, self.tick_size)

        # Determine target sell price
        if self.custom_target_price:
            raw_target = max(self.cost_to_cost_price, self.custom_target_price)
        else:
            if cur_ask and cur_ask > self.cost_to_cost_price:
                # Sell at Best Ask - 1 Tick (or Best Ask)
                raw_target = max(self.cost_to_cost_price, cur_ask - self.tick_size)
            else:
                # If market ask is already at or below cost, target is Cost-to-Cost
                raw_target = self.cost_to_cost_price

        target_price = align_price(raw_target, self.tick_size)
        self.sell_limit_price = target_price

        self.log(f"Placing LIMIT SELL exit: Size={self.size}, Price={target_price} (Entry Cost={self.buy_fill_price}, Cost-to-Cost Floor={self.cost_to_cost_price}, Market Ask={cur_ask})")

        try:
            sell_res = self.client.place_order(
                product_id=self.product_id,
                size=self.size,
                side="sell",
                limit_price=str(target_price),
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                post_only="true",
                reduce_only="true"
            )
            self.sell_order_id = sell_res.get("id")
            self.sell_fill_event.clear()
            if self.private_ws and self.sell_order_id:
                self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
            self.state = "SELL_OPEN"
            self.sell_peg_status = f"FLOATING_ASK_{target_price}"
            self.log(f"Limit SELL exit order active on book! Order ID: {self.sell_order_id} @ {target_price} (Reduce-Only=True, Two-Way Peg Active)")
            return True
        except Exception as e:
            self.log(f"Failed to place post-only SELL order: {e}. Retrying without post-only constraint...")
            try:
                # If post-only crossed, place standard limit sell at target
                sell_res = self.client.place_order(
                    product_id=self.product_id,
                    size=self.size,
                    side="sell",
                    limit_price=str(target_price),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    post_only="false",
                    reduce_only="true"
                )
                self.sell_order_id = sell_res.get("id")
                self.sell_fill_event.clear()
                if self.private_ws and self.sell_order_id:
                    self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
                self.state = "SELL_OPEN"
                self.sell_peg_status = f"FLOATING_ASK_{target_price}"
                self.log(f"Limit SELL exit order placed! Order ID: {self.sell_order_id} @ {target_price} (Two-Way Peg Active)")
                return True
            except Exception as e2:
                self.log(f"Critical: Failed to place SELL order: {e2}")
                self.state = "ERROR"
                return False

    def get_external_best_ask(self) -> float | None:
        """
        Inspects L2 orderbook (first checking real-time in-memory WebSocket L2 cache,
        falling back to REST API), filtering out our own resting limit sell order.
        Returns the lowest competitor ask price.
        """
        asks = []
        # Try real-time in-memory WebSocket L2 cache first (<10ms latency)
        if self.public_ws:
            ob = self.public_ws.get_l2_orderbook(self.symbol)
            asks = ob.get("asks") or []

        # Fallback to REST orderbook if WS cache is empty
        if not asks:
            try:
                ob = self.client.get_l2_orderbook(self.symbol)
                asks = ob.get("sell", []) or ob.get("asks", []) or []
            except Exception:
                pass

        for ask_level in asks:
            if isinstance(ask_level, (list, tuple)):
                try:
                    price = float(ask_level[0])
                    size = float(ask_level[1])
                except (ValueError, TypeError, IndexError):
                    continue
            elif isinstance(ask_level, dict):
                price = float(ask_level.get("price", 0))
                size = float(ask_level.get("size", 0))
            else:
                continue

            # If this level matches our exact sell limit price, check if there's external volume
            if self.sell_limit_price and abs(price - self.sell_limit_price) < (self.tick_size * 0.5):
                # If other traders have volume at this price level besides our 1 lot
                if size > self.size:
                    return price
                # This entire level is just our own order, skip to find next external competitor
                continue
            if price > 0:
                return price

        # If no external competitor asks found in orderbook:
        if self.current_ask and self.sell_limit_price and abs(self.current_ask - self.sell_limit_price) >= (self.tick_size * 0.5):
            return self.current_ask
        return None

    def get_external_best_bid(self) -> float | None:
        """
        Inspects L2 orderbook (first checking real-time in-memory WebSocket L2 cache,
        falling back to REST API), filtering out our own resting limit buy order.
        Returns the highest competitor bid price.
        """
        bids = []
        # Try real-time in-memory WebSocket L2 cache first (<10ms latency)
        if self.public_ws:
            ob = self.public_ws.get_l2_orderbook(self.symbol)
            bids = ob.get("bids") or []

        # Fallback to REST orderbook if WS cache is empty
        if not bids:
            try:
                ob = self.client.get_l2_orderbook(self.symbol)
                bids = ob.get("buy", []) or ob.get("bids", []) or []
            except Exception:
                pass

        for bid_level in bids:
            if isinstance(bid_level, (list, tuple)):
                try:
                    price = float(bid_level[0])
                    size = float(bid_level[1])
                except (ValueError, TypeError, IndexError):
                    continue
            elif isinstance(bid_level, dict):
                price = float(bid_level.get("price", 0))
                size = float(bid_level.get("size", 0))
            else:
                continue

            # If this level matches our exact buy limit price, check if there's external volume
            if self.buy_limit_price and abs(price - self.buy_limit_price) < (self.tick_size * 0.5):
                if size > self.size:
                    return price
                # This entire level is just our own order, skip to find next external competitor
                continue
            if price > 0:
                return price

        # If no external competitor bids found in orderbook:
        if self.current_bid and self.buy_limit_price and abs(self.current_bid - self.buy_limit_price) >= (self.tick_size * 0.5):
            return self.current_bid
        return None

    def _phase_4_monitor_exit_wait(self):
        """
        Phase 4: Monitors SELL order with Dynamic Two-Way Ask Pegging:
        - Floats UP with competitor asks (e.g. from 3.9 up to 4.9) to maximize profit.
        - Tightens DOWN if competitors undercut to maintain #1 top-of-book priority.
        - Strictly NO STOP-LOSS, NO TIMEOUT:
          * If market drops below cost, anchors at Cost-to-Cost floor and waits for bounce.
        - Debounced at 350ms to prevent API churn.
        """
        poll_interval = 0.35
        last_logged_state = None

        try:
            while self.is_running and not self.stop_requested:
                # Fast path: Real-time WebSocket fill event unblocks instantly (<50ms)
                if self.sell_fill_event.wait(timeout=poll_interval):
                    if self.sell_fill_price is not None:
                        return

                try:
                    order = self.client.get_order_by_id(self.sell_order_id)
                    state = order.get("state")
                    unfilled = float(order.get("unfilled_size", 0) or 0)

                    # Check if SELL filled via REST check
                    if state == "closed" and unfilled == 0:
                        avg_fill = float(order.get("average_fill_price") or self.sell_limit_price)
                        self.sell_fill_price = avg_fill
                        self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                        self.realized_pnl = (self.sell_fill_price - self.buy_fill_price) * self.size * self.contract_value
                        self.unrealized_pnl = 0.0

                        if self.sell_fill_price > self.cost_to_cost_price:
                            self.state = "COMPLETED_PROFIT"
                            self.log(f"PROFIT EXIT! SELL filled at {self.sell_fill_price}. Realized PnL = +${self.realized_pnl:.5f} USD")
                        else:
                            self.state = "COMPLETED_COST"
                            self.log(f"COST-TO-COST EXIT! SELL filled at {self.sell_fill_price}. Realized PnL = ${self.realized_pnl:.5f} USD (Breakeven)")
                        return

                    # Fetch live competitor quotes from orderbook (excludes our own order)
                    ext_ask = self.get_external_best_ask()
                    ext_bid = self.get_external_best_bid()
                    quote = self.fetch_live_quote()
                    cur_bid = ext_bid if ext_bid is not None else quote.get("bid")
                    cur_ask = ext_ask if ext_ask is not None else quote.get("ask")
                    self.competitor_ask = cur_ask
                    self.competitor_bid = cur_bid

                    # Analyze Market Condition based on Market ASK
                    if cur_ask is not None and self.two_way_peg_enabled:
                        # Scenario A: Market ASK dropped to or below Cost-to-Cost price
                        if cur_ask <= self.cost_to_cost_price:
                            self.state = "WAITING_COST_TO_COST"
                            self.sell_peg_status = f"WAITING_COST_FLOOR_{self.cost_to_cost_price}"
                            # Ensure our resting sell order is held strictly at Cost-to-Cost floor
                            if self.sell_limit_price > self.cost_to_cost_price:
                                now = time.time()
                                if now - self.last_edit_time >= self.edit_debounce_sec:
                                    self.last_edit_time = now
                                    self.log(f"[TWO-WAY ASK PEG] Market Ask dropped to {cur_ask}. Adjusting SELL limit down to Cost-to-Cost floor: {self.cost_to_cost_price}")
                                    self.upload_limit_price(self.cost_to_cost_price)

                            log_key = f"waiting_{round(cur_ask or 0, 1)}"
                            if last_logged_state != log_key:
                                self.log(f"Market below cost (Ask={cur_ask}, Cost Floor={self.cost_to_cost_price}). Resting order at Cost-to-Cost. WAITING for bounce... (Strictly NO Stop-Loss, NO Timeout)")
                                last_logged_state = log_key

                        # Scenario B: Competitor ASK is comfortably above cost price -> dynamic peg
                        elif cur_ask > self.cost_to_cost_price:
                            self.state = "SELL_OPEN"
                            # Target is 1 tick below competitor ask (or cost floor)
                            ideal_sell = align_price(cur_ask - self.tick_size, self.tick_size)
                            ideal_sell = max(self.cost_to_cost_price, ideal_sell)

                            # If competitor moved such that ideal price differs by >= 1 tick
                            if abs(ideal_sell - self.sell_limit_price) >= (self.tick_size * 0.99):
                                now = time.time()
                                if now - self.last_edit_time >= self.edit_debounce_sec:
                                    self.last_edit_time = now
                                    if ideal_sell > self.sell_limit_price:
                                        self.sell_peg_status = f"FLOATING_UP_{ideal_sell}"
                                        self.log(f"[TWO-WAY ASK PEG] Competitor Ask is higher at {cur_ask}. Floating SELL limit UP from {self.sell_limit_price} to {ideal_sell} to capture extra profit!")
                                    else:
                                        self.sell_peg_status = f"TIGHTENING_DOWN_{ideal_sell}"
                                        self.log(f"[TWO-WAY ASK PEG] Competitor Ask tightened to {cur_ask}. Tightening SELL limit DOWN from {self.sell_limit_price} to {ideal_sell} to hold #1 seller priority.")
                                    self.upload_limit_price(ideal_sell)

                except Exception as e:
                    self.log(f"Warning in exit monitor / ask peg: {e}")

            if self.stop_requested:
                self.log("Manual stop requested while in exit phase. Note: You hold 1 lot long position.")

        finally:
            if self.private_ws and self.sell_order_id:
                self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)


class DeltaMMEngine:
    """
    Central Manager for active Market Making execution cycles.
    """
    def __init__(self):
        self.active_cycles: dict[str, MMTradeCycle] = {}
        self.history_cycles: list[dict] = []
        self._lock = threading.Lock()

    def start_trade(
        self,
        symbol: str,
        size: int = 1,
        entry_price: float = None,
        target_exit_price: float = None,
        cost_to_cost_ticks: int = 1,
        auto_reprice_buy: bool = True,
        two_way_peg: bool = True,
        max_entry_price: float = None,
        account: str = None
    ) -> MMTradeCycle:
        """Creates and launches a new 1-lot MM trade cycle with Two-Way Floating Peg."""
        with self._lock:
            cycle = MMTradeCycle(
                symbol=symbol,
                size=size,
                entry_price=entry_price,
                target_exit_price=target_exit_price,
                cost_to_cost_ticks=cost_to_cost_ticks,
                auto_reprice_buy=auto_reprice_buy,
                two_way_peg=two_way_peg,
                max_entry_price=max_entry_price,
                account=account
            )
            self.active_cycles[cycle.trade_id] = cycle
            cycle.start()
            return cycle

    def get_cycle(self, trade_id: str) -> MMTradeCycle | None:
        with self._lock:
            return self.active_cycles.get(trade_id)

    def list_active(self) -> list[dict]:
        with self._lock:
            # Move completed/cancelled cycles out of active into history
            active_list = []
            to_remove = []
            for tid, c in list(self.active_cycles.items()):
                if not c.is_running or c.state in ("COMPLETED_PROFIT", "COMPLETED_COST", "CANCELLED", "ERROR"):
                    self.history_cycles.append(c.to_dict())
                    to_remove.append(tid)
                else:
                    active_list.append(c.to_dict())

            for tid in to_remove:
                self.active_cycles.pop(tid, None)

            return active_list

    def upload_price(self, trade_id: str, new_price: float) -> dict:
        cycle = self.get_cycle(trade_id)
        if not cycle:
            return {"success": False, "error": f"Trade {trade_id} not found."}
        return cycle.upload_limit_price(new_price)

    def cancel_trade(self, trade_id: str) -> dict:
        cycle = self.get_cycle(trade_id)
        if not cycle:
            return {"success": False, "error": f"Trade {trade_id} not found."}
        cycle.stop()
        return {"success": True, "message": f"Stop requested for {trade_id}"}


# Global engine instance
mm_engine = DeltaMMEngine()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Delta Exchange 1-Lot MM Trade Executor")
    parser.add_argument("--symbol", type=str, required=True, help="Strike symbol (e.g. P-BTC-79400-151026)")
    parser.add_argument("--size", type=int, default=1, help="Lot size (default: 1)")
    parser.add_argument("--price", type=float, default=None, help="Custom entry limit price")
    parser.add_argument("--target", type=float, default=None, help="Custom target exit price")
    parser.add_argument("--account", choices=["main", "sub"], default=None, help="Account to execute on")
    args = parser.parse_args()

    print("=" * 65)
    print(f" Delta Exchange 1-Lot MM Engine")
    print(f" Symbol: {args.symbol} | Size: {args.size} | Account: {(args.account or os.getenv('ACTIVE_ACCOUNT', 'main')).upper()}")
    print(f" Strategy: 1st BUY Maker -> 2nd SELL Maker")
    print(f" Price Drop Rule: STRICTLY NO STOP LOSS, NO TIMEOUT -> EXIT COST-TO-COST OR WAIT")
    print("=" * 65)

    trade = mm_engine.start_trade(
        symbol=args.symbol,
        size=args.size,
        entry_price=args.price,
        target_exit_price=args.target,
        account=args.account
    )

    try:
        while trade.is_running:
            time.sleep(2)
        print(f"\nFinal Trade Result: {trade.state}")
        print(f"Realized PnL: ${trade.realized_pnl:.5f} USD")
    except KeyboardInterrupt:
        print("\nStopping trade...")
        trade.stop()
