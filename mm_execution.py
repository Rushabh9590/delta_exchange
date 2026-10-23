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
   - Cost-to-cost floor = cost_price + Delta Exchange Maker fee (0.03% buy + 0.03% sell = 0.06% roundtrip) + 1 tick buffer.
   - Target profit exit = max(cost_to_cost_floor, current_ask - 1 tick).
   - Places LIMIT SELL with post_only=True and reduce_only=True.
6. Phase 4 - Price Drops Scenario (STRICT USER RULES):
   - NO STOP LOSS: Never trigger a market stop loss or sell below cost at a loss.
   - NO TIME OUT: Never liquidate or cancel due to elapsed time.
   - EXIT COST-TO-COST OR WAIT:
     - If market ask is favorable (above cost): can adjust sell target down to (ask - 1 tick) to take profit.
     - If market drops BELOW entry cost:
       - Limit SELL order is set strictly to Cost-to-Cost (entry price + 0.06% fees + 1 tick).
       - Never drop the limit sell price below entry cost!
       - WAIT for market to oscillate/bounce back to fill at breakeven!
7. Upload Limit Price:
   - User or API can upload/update the limit price at any point during entry or exit.
"""

import os
import sys
import json
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

STATE_FILE_PATH = os.path.join(str(BASE_DIR), ".mm_trades_state.json")
_STATE_LOCK = threading.Lock()
MM_LOG_FILE = BASE_DIR / "logs" / "mm_trades.log"
MAX_LOG_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per file limit
LOG_BACKUP_COUNT = 3                   # Retain up to 3 rotated backups (.1, .2, .3)
_LOG_BROADCAST_CALLBACK = None

def set_log_broadcast_callback(cb):
    """Registers an SSE log broadcaster callback for real-time live streaming."""
    global _LOG_BROADCAST_CALLBACK
    _LOG_BROADCAST_CALLBACK = cb

def rotate_log_file(file_path, max_bytes: int = MAX_LOG_FILE_BYTES, backup_count: int = LOG_BACKUP_COUNT) -> bool:
    """
    Safely rotates file_path if its size exceeds max_bytes.
    Rotates existing backups (.2 -> .3, .1 -> .2) and moves file_path to .1.
    Handles Windows PermissionError (locked handles) via safe copy-truncate.
    """
    try:
        p = Path(file_path)
        if not p.exists() or p.stat().st_size < max_bytes:
            return False

        for i in range(backup_count - 1, 0, -1):
            sfn = p.with_name(f"{p.name}.{i}")
            dfn = p.with_name(f"{p.name}.{i + 1}")
            if sfn.exists():
                try:
                    if dfn.exists():
                        dfn.unlink()
                    sfn.rename(dfn)
                except Exception:
                    pass

        target = p.with_name(f"{p.name}.1")
        try:
            if target.exists():
                target.unlink()
            p.rename(target)
            return True
        except PermissionError:
            # File is held open by another process on Windows: fallback to copy & truncate
            try:
                import shutil
                shutil.copyfile(str(p), str(target))
                with open(p, "w", encoding="utf-8") as f:
                    f.truncate(0)
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False

def append_to_mm_log(line: str):
    """Safely appends a real-time MM execution log line to persistent disk file with size-limit rotation."""
    try:
        MM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_file(MM_LOG_FILE, max_bytes=MAX_LOG_FILE_BYTES, backup_count=LOG_BACKUP_COUNT)
        with open(MM_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass

    if _LOG_BROADCAST_CALLBACK:
        try:
            _LOG_BROADCAST_CALLBACK(line)
        except Exception:
            pass


def save_all_trades_state(active_dict: dict, history_list: list = None):
    """Atomically writes all active MM trade cycles and history to disk."""
    with _STATE_LOCK:
        try:
            active_data = {}
            for tid, cycle in list(active_dict.items()):
                if hasattr(cycle, "to_persistence_dict"):
                    active_data[tid] = cycle.to_persistence_dict()
                elif isinstance(cycle, dict):
                    active_data[tid] = cycle

            data = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active_trades": active_data,
                "history": (history_list or [])[-50:]
            }
            tmp_path = STATE_FILE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, STATE_FILE_PATH)
        except Exception as e:
            print(f"[MM-PERSISTENCE] Error saving trade state: {e}", flush=True)


def load_all_trades_state() -> dict:
    """Safely loads persisted trade cycles from disk."""
    with _STATE_LOCK:
        if not os.path.exists(STATE_FILE_PATH):
            return {"active_trades": {}, "history": []}
        try:
            with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"active_trades": {}, "history": []}
                return json.loads(content)
        except Exception as e:
            print(f"[MM-PERSISTENCE] Error reading trade state ({e}). Returning empty state.", flush=True)
            return {"active_trades": {}, "history": []}


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
        client: DeltaRestClient = None,
        trade_id: str = None
    ):
        self.trade_id = trade_id or f"mm_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.symbol = symbol.strip().upper()
        self.total_size = int(size) if size else 1
        self.size = self.total_size
        self.buy_filled_qty = 0
        self.buy_unfilled_qty = self.total_size
        self.sell_filled_qty = 0
        self.sell_unfilled_qty = 0
        self.weighted_buy_price = 0.0
        self.weighted_sell_price = 0.0
        self.custom_entry_price = float(entry_price) if entry_price is not None and float(entry_price) > 0 else None
        self.custom_target_price = float(target_exit_price) if target_exit_price is not None and float(target_exit_price) > 0 else None
        self.max_entry_price = float(max_entry_price) if max_entry_price is not None and float(max_entry_price) > 0 else None
        self.cost_to_cost_ticks = cost_to_cost_ticks  # profit buffer ticks added on top of Delta 0.06% roundtrip fees (default: 1 tick)
        self.auto_reprice_buy = auto_reprice_buy
        self.two_way_peg_enabled = two_way_peg
        self.account_name = (account or os.getenv("ACTIVE_ACCOUNT") or "main").lower()
        self.on_state_change = None

        # Delta client
        self.client = client or get_delta_client(account=self.account_name)

        # Product specifications
        self.product_info = get_product_details(self.symbol, self.client)
        self.product_id = self.product_info["id"]
        self.tick_size = float(self.product_info.get("tick_size") or 0.1)
        self.contract_value = float(self.product_info.get("contract_value") or 0.001)

        # Lifecycle State
        # States: INIT, PLACING_BUY, BUY_OPEN, BUY_PARTIAL, DUAL_ACTIVE, BUY_FILLED, PLACING_SELL, SELL_OPEN, SELL_PARTIAL,
        #         WAITING_COST_TO_COST, COMPLETED_PROFIT, COMPLETED_COST, CANCELLED, ERROR
        self.state = "INIT"
        self.status_message = f"Initialized {self.total_size}-lot MM trade cycle"

        # Order details
        self.buy_order_id = None
        self.buy_limit_price = None
        self.buy_fill_price = None
        self.buy_fill_time = None
        self.buy_placed_qty = self.total_size

        self.sell_order_id = None
        self.sell_limit_price = None
        self.sell_fill_price = None
        self.sell_fill_time = None
        self.sell_placed_qty = 0
        self.cost_to_cost_price = None

        # Manual price overrides (when user edits price in dashboard UI)
        self.manual_buy_override = False
        self.manual_buy_price = None
        self.manual_sell_override = False
        self.manual_sell_price = None

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
        self._is_replacing_order: bool = False
        self._order_fills_map: dict[int, float] = {}

        self.log(f"Created MM cycle for {self.symbol} (Product ID {self.product_id}, Tick {self.tick_size}) on account [{self.account_name.upper()}] with Two-Way Floating Peg")

    def _persist_state(self):
        """Notifies the engine to persist active trade state to disk."""
        if hasattr(self, "on_state_change") and callable(self.on_state_change):
            try:
                self.on_state_change(self)
            except Exception:
                pass

    def log(self, msg: str):
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{dt_str}] {msg}"
        self.logs.append(entry)
        self.status_message = msg
        self.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist_state()
        full_line = f"[{dt_str}] [MM-{self.trade_id[-6:]}] [{self.symbol}] {msg}"
        append_to_mm_log(full_line)
        try:
            print(full_line, flush=True)
        except Exception:
            try:
                safe_msg = full_line.encode("ascii", errors="replace").decode("ascii")
                print(safe_msg, flush=True)
            except Exception:
                pass

    def _on_buy_order_update(self, order_data: dict):
        """Real-time callback from private WebSocket for BUY order (<50ms latency)."""
        try:
            raw_id = order_data.get("id")
            if raw_id is not None and self.buy_order_id is not None and int(raw_id) != int(self.buy_order_id):
                return

            state = order_data.get("state")
            raw_unfilled = order_data.get("unfilled_size")
            order_sz = float(order_data.get("size") or self.total_size)
            if raw_unfilled is not None:
                unfilled = float(raw_unfilled)
                this_order_filled = max(0.0, order_sz - unfilled)
            else:
                unfilled = float(self.buy_unfilled_qty)
                this_order_filled = float(self.buy_filled_qty)
            avg_fill = float(order_data.get("average_fill_price") or self.buy_limit_price or 0.0)

            oid = int(raw_id) if raw_id is not None else (int(self.buy_order_id) if self.buy_order_id else None)
            prev_filled = self._order_fills_map.get(oid, 0.0) if oid else 0.0
            increment = max(0.0, this_order_filled - prev_filled)

            if increment > 0:
                if oid:
                    self._order_fills_map[oid] = this_order_filled
                self.buy_filled_qty += int(increment)
                self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                if avg_fill > 0:
                    if self.buy_fill_price and self.buy_filled_qty > int(increment):
                        prev_qty = self.buy_filled_qty - int(increment)
                        self.buy_fill_price = ((self.buy_fill_price * prev_qty) + (avg_fill * increment)) / self.buy_filled_qty
                    else:
                        self.buy_fill_price = avg_fill
                    self.weighted_buy_price = self.buy_fill_price
                self.buy_fill_time = datetime.now(timezone.utc).isoformat()

                if self.buy_filled_qty >= self.total_size or unfilled == 0 or state == "closed":
                    if unfilled == 0 or state == "closed":
                        self.total_size = self.buy_filled_qty
                        self.size = self.buy_filled_qty
                        self.buy_unfilled_qty = 0
                        self.buy_order_id = None
                    if self.sell_filled_qty > 0 and self.sell_filled_qty < self.buy_filled_qty:
                        self.state = "DUAL_ACTIVE"
                    elif self.sell_filled_qty >= self.buy_filled_qty and self.buy_filled_qty > 0:
                        self.is_running = False
                        self.stop_requested = True
                        if self.cost_to_cost_price and self.sell_fill_price and self.sell_fill_price > self.cost_to_cost_price:
                            self.state = "COMPLETED_PROFIT"
                        else:
                            self.state = "COMPLETED_COST"
                    else:
                        self.state = "BUY_FILLED"
                    self.log(f"[WS-STREAM] Sub-50ms BUY FULLY FILLED! Executed {self.buy_filled_qty}/{self.total_size} lots @ {self.buy_fill_price}")
                    self.buy_fill_event.set()
                else:
                    self.state = "DUAL_ACTIVE"
                    self.log(f"[WS-STREAM] Sub-50ms PARTIAL BUY fill! Executed {self.buy_filled_qty}/{self.total_size} lots @ {self.buy_fill_price}. ({self.buy_unfilled_qty} lots remaining to buy). Activating immediate profit booking on {self.buy_filled_qty} lots!")
                    self.buy_fill_event.set()

            if state == "cancelled":
                if getattr(self, "_is_replacing_order", False):
                    self.log(f"[WS-STREAM] BUY order {raw_id} cancelled during limit price adjustment/re-placement. Continuing active MM cycle.")
                    return

                # Check if position still exists on Delta Exchange
                actual_pos = 0
                try:
                    p = self.client.get_position(self.product_id)
                    actual_pos = abs(int(p.get("size") or 0)) if p else 0
                except Exception:
                    actual_pos = self.buy_filled_qty

                if actual_pos == 0:
                    self.state = "CANCELLED"
                    self.stop_requested = True
                    self.is_running = False
                    self.log(f"[WS-STREAM] BUY order cancelled on exchange and position is 0. Stopping trade cycle cleanly.")
                    self.buy_fill_event.set()
                elif actual_pos > 0:
                    self.size = actual_pos
                    self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                    self.buy_unfilled_qty = 0
                    self.state = "DUAL_ACTIVE" if self.sell_order_id else "BUY_FILLED"
                    self.log(f"[WS-STREAM] BUY order cancelled on exchange with {actual_pos} lots held. Continuing profit booking on active position!")
                    self.buy_fill_event.set()
                else:
                    self.state = "CANCELLED"
                    self.stop_requested = True
                    self.is_running = False
                    self.log("[WS-STREAM] BUY order cancelled on exchange with 0 fills. Stopping trade cycle.")
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
            raw_unfilled = order_data.get("unfilled_size")
            order_sz = float(order_data.get("size") or self.sell_placed_qty or self.size)
            if raw_unfilled is not None:
                unfilled = float(raw_unfilled)
                this_order_filled = max(0.0, order_sz - unfilled)
            else:
                unfilled = float(self.sell_unfilled_qty)
                this_order_filled = float(self.sell_filled_qty)
            avg_fill = float(order_data.get("average_fill_price") or self.sell_limit_price or 0.0)

            oid = int(raw_id) if raw_id is not None else (int(self.sell_order_id) if self.sell_order_id else None)
            prev_filled = self._order_fills_map.get(oid, 0.0) if oid else 0.0
            increment = max(0.0, this_order_filled - prev_filled)

            if increment > 0:
                if oid:
                    self._order_fills_map[oid] = this_order_filled
                self.sell_filled_qty += int(increment)
                self.sell_unfilled_qty = max(0, self.buy_filled_qty - self.sell_filled_qty)
                if avg_fill > 0:
                    self.sell_fill_price = avg_fill
                    self.weighted_sell_price = avg_fill
                self.sell_fill_time = datetime.now(timezone.utc).isoformat()

                # Add incremental realized PnL
                chunk_pnl = (self.sell_fill_price - (self.buy_fill_price or 0.0)) * increment * self.contract_value
                self.realized_pnl += chunk_pnl

                is_buy_finished = (self.buy_order_id is None or self.buy_unfilled_qty == 0 or self.buy_filled_qty >= self.total_size)
                is_all_sold = (self.buy_filled_qty > 0 and self.sell_filled_qty >= self.buy_filled_qty)

                if (self.sell_filled_qty >= self.total_size) or (is_all_sold and is_buy_finished):
                    actual_pos = 0
                    try:
                        p = self.client.get_position(self.product_id)
                        actual_pos = abs(int(p.get("size") or 0)) if p else 0
                    except Exception:
                        actual_pos = 0

                    if actual_pos > 0:
                        self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                        self.size = actual_pos
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        self.sell_unfilled_qty = actual_pos
                        self.state = "SELL_OPEN"
                        self.log(f"[WS-STREAM] Sell order filled, but Delta holds {actual_pos} open long lots! Placing fresh SELL exit order...")
                        self._place_sell_exit_order(target_qty=actual_pos)
                        self.sell_fill_event.set()
                    else:
                        self.unrealized_pnl = 0.0
                        self.is_running = False
                        self.stop_requested = True
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        self.sell_unfilled_qty = 0
                        if self.cost_to_cost_price and self.sell_fill_price and self.sell_fill_price > self.cost_to_cost_price:
                            self.state = "COMPLETED_PROFIT"
                            self.log(f"[WS-STREAM] Sub-50ms PROFIT EXIT FULLY FILLED at {self.sell_fill_price}! Executed all {self.sell_filled_qty} lots. Total Realized PnL = +${self.realized_pnl:.5f} USD")
                        else:
                            self.state = "COMPLETED_COST"
                            self.log(f"[WS-STREAM] Sub-50ms COST-TO-COST EXIT FULLY FILLED at {self.sell_fill_price}! Executed all {self.sell_filled_qty} lots. Total Realized PnL = ${self.realized_pnl:.5f} USD (Breakeven)")
                        self.sell_fill_event.set()
                else:
                    if self.buy_unfilled_qty > 0 and self.buy_order_id:
                        self.state = "DUAL_ACTIVE"
                    else:
                        self.state = "SELL_PARTIAL"
                    self.log(f"[WS-STREAM] Sub-50ms PARTIAL SELL EXIT! {self.sell_filled_qty}/{self.buy_filled_qty} lots sold @ {self.sell_fill_price}. Incremental Realized PnL = +${chunk_pnl:.5f} USD. ({self.sell_unfilled_qty} lots remaining to sell)")

                    if unfilled == 0 or state == "closed":
                        open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                        if open_inv > 0:
                            self.sell_order_id = None
                            self.sell_placed_qty = 0
                            self.sell_unfilled_qty = open_inv
                            self.log(f"[WS-STREAM] Sell order {raw_id} fully filled for its size ({int(order_sz)} lots), but {open_inv} open long lots remain! Placing new SELL exit order for {open_inv} lots immediately...")
                            self._place_sell_exit_order(target_qty=open_inv)
                        else:
                            self.sell_order_id = None
                            self.sell_placed_qty = 0
                            self.sell_unfilled_qty = 0

                    self.sell_fill_event.set()

            # Handle closed or unfilled == 0 if event arrived without new increment
            elif unfilled == 0 or state == "closed":
                open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                if open_inv > 0 and (oid is None or self.sell_order_id == oid):
                    self.sell_order_id = None
                    self.sell_placed_qty = 0
                    self.sell_unfilled_qty = open_inv
                    self.log(f"[WS-STREAM] Sell order {oid} is closed, but {open_inv} open long lots remain! Placing fresh SELL exit order...")
                    self._place_sell_exit_order(target_qty=open_inv)
                    self.sell_fill_event.set()

            if state == "cancelled":
                if getattr(self, "_is_replacing_order", False) or getattr(self, "_cancelling_for_reprice", False):
                    self.log(f"[WS-STREAM] SELL order {raw_id} cancelled during limit price adjustment/re-placement. Continuing active MM cycle.")
                    return

                # If user cancelled sell order directly on exchange and position is 0, conclude cleanly
                try:
                    p = self.client.get_position(self.product_id)
                    actual_pos = abs(int(p.get("size") or 0)) if p else 0
                    if actual_pos == 0:
                        self.log(f"[WS-STREAM] SELL order cancelled on exchange and Delta position is 0. Concluding MM trade cycle cleanly.")
                        self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                        self.stop_requested = True
                        self.is_running = False
                except Exception:
                    pass
                self.sell_fill_event.set()
        except Exception as e:
            self.log(f"Warning in WS sell callback: {e}")

    def to_dict(self) -> dict:
        """Serializes current trade status for API and Dashboard consumption."""
        # Ensure fresh bid & ask are available for UI monitor
        bid = self.current_bid
        ask = self.current_ask
        if bid is None or ask is None:
            try:
                if getattr(self, "public_ws", None):
                    wq = self.public_ws.get_latest_quote(self.symbol)
                    if wq.get("bid"): bid = float(wq["bid"])
                    if wq.get("ask"): ask = float(wq["ask"])
                if (bid is None or ask is None):
                    from websocket import get_public_ws_manager
                    pws = get_public_ws_manager([self.symbol])
                    if pws:
                        wq2 = pws.get_latest_quote(self.symbol)
                        if wq2.get("bid"): bid = float(wq2["bid"])
                        if wq2.get("ask"): ask = float(wq2["ask"])
                if bid is None and self.competitor_bid:
                    bid = float(self.competitor_bid)
                if ask is None and self.competitor_ask:
                    ask = float(self.competitor_ask)
                if bid is None and self.buy_limit_price:
                    bid = float(self.buy_limit_price)
                if ask is None and self.sell_limit_price:
                    ask = float(self.sell_limit_price)
                if bid is not None:
                    self.current_bid = bid
                if ask is not None:
                    self.current_ask = ask
            except Exception:
                pass

        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "size": self.size,
            "total_size": self.total_size,
            "buy_filled_qty": self.buy_filled_qty,
            "buy_unfilled_qty": self.buy_unfilled_qty,
            "sell_filled_qty": self.sell_filled_qty,
            "sell_unfilled_qty": self.sell_unfilled_qty,
            "buy_placed_qty": getattr(self, "buy_placed_qty", self.total_size),
            "sell_placed_qty": self.sell_placed_qty,
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

    def to_persistence_dict(self) -> dict:
        """Serializes complete internal state for atomic disk persistence and server recovery."""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "size": self.size,
            "total_size": self.total_size,
            "buy_filled_qty": self.buy_filled_qty,
            "buy_unfilled_qty": self.buy_unfilled_qty,
            "sell_filled_qty": self.sell_filled_qty,
            "sell_unfilled_qty": self.sell_unfilled_qty,
            "buy_placed_qty": getattr(self, "buy_placed_qty", self.total_size),
            "sell_placed_qty": self.sell_placed_qty,
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
            "cost_to_cost_ticks": self.cost_to_cost_ticks,
            "custom_entry_price": self.custom_entry_price,
            "custom_target_price": self.custom_target_price,
            "max_entry_price": self.max_entry_price,
            "auto_reprice_buy": self.auto_reprice_buy,
            "two_way_peg_enabled": self.two_way_peg_enabled,
            "manual_buy_override": getattr(self, "manual_buy_override", False),
            "manual_buy_price": getattr(self, "manual_buy_price", None),
            "manual_sell_override": getattr(self, "manual_sell_override", False),
            "manual_sell_price": getattr(self, "manual_sell_price", None),
            "current_bid": self.current_bid,
            "current_ask": self.current_ask,
            "current_ltp": self.current_ltp,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "logs": self.logs[-20:]
        }

    @classmethod
    def from_persistence_dict(cls, data: dict, client: DeltaRestClient = None) -> "MMTradeCycle":
        """Instantiates and reconstructs an MMTradeCycle from persisted disk data."""
        cycle = cls(
            symbol=data["symbol"],
            size=int(data.get("total_size", data.get("size", 1))),
            entry_price=data.get("custom_entry_price"),
            target_exit_price=data.get("custom_target_price"),
            cost_to_cost_ticks=int(data.get("cost_to_cost_ticks", 1)),
            auto_reprice_buy=bool(data.get("auto_reprice_buy", True)),
            two_way_peg=bool(data.get("two_way_peg_enabled", True)),
            max_entry_price=data.get("max_entry_price"),
            account=data.get("account", "main"),
            client=client,
            trade_id=data.get("trade_id")
        )
        cycle.size = int(data.get("size", cycle.total_size))
        cycle.buy_filled_qty = int(data.get("buy_filled_qty", 0))
        cycle.buy_unfilled_qty = int(data.get("buy_unfilled_qty", max(0, cycle.total_size - cycle.buy_filled_qty)))
        cycle.sell_filled_qty = int(data.get("sell_filled_qty", 0))
        cycle.sell_unfilled_qty = int(data.get("sell_unfilled_qty", 0))
        cycle.buy_placed_qty = int(data.get("buy_placed_qty", cycle.total_size))
        cycle.sell_placed_qty = int(data.get("sell_placed_qty", 0))
        cycle.buy_order_id = data.get("buy_order_id")
        cycle.buy_limit_price = float(data["buy_limit_price"]) if data.get("buy_limit_price") is not None else None
        cycle.buy_fill_price = float(data["buy_fill_price"]) if data.get("buy_fill_price") is not None else None
        cycle.buy_fill_time = data.get("buy_fill_time")
        cycle.sell_order_id = data.get("sell_order_id")
        cycle.sell_limit_price = float(data["sell_limit_price"]) if data.get("sell_limit_price") is not None else None
        cycle.sell_fill_price = float(data["sell_fill_price"]) if data.get("sell_fill_price") is not None else None
        cycle.sell_fill_time = data.get("sell_fill_time")
        cycle.cost_to_cost_price = float(data["cost_to_cost_price"]) if data.get("cost_to_cost_price") is not None else None
        cycle.manual_buy_override = bool(data.get("manual_buy_override", False))
        cycle.manual_buy_price = float(data["manual_buy_price"]) if data.get("manual_buy_price") is not None else None
        cycle.manual_sell_override = bool(data.get("manual_sell_override", False))
        cycle.manual_sell_price = float(data["manual_sell_price"]) if data.get("manual_sell_price") is not None else None
        cycle.current_bid = float(data["current_bid"]) if data.get("current_bid") is not None else None
        cycle.current_ask = float(data["current_ask"]) if data.get("current_ask") is not None else None
        cycle.current_ltp = float(data["current_ltp"]) if data.get("current_ltp") is not None else None
        cycle.realized_pnl = float(data.get("realized_pnl", 0.0))
        cycle.unrealized_pnl = float(data.get("unrealized_pnl", 0.0))
        cycle.state = data.get("state", "INIT")
        cycle.status_message = data.get("status_message", "Recovered from persistence")
        cycle.created_at = data.get("created_at", cycle.created_at)
        cycle.logs = data.get("logs", [])
        cycle._order_fills_map = {}
        if cycle.buy_order_id and cycle.buy_filled_qty > 0:
            cycle._order_fills_map[int(cycle.buy_order_id)] = float(cycle.buy_filled_qty)
        if cycle.sell_order_id and cycle.sell_filled_qty > 0:
            cycle._order_fills_map[int(cycle.sell_order_id)] = float(cycle.sell_filled_qty)
        return cycle


    def reconcile_with_exchange(self) -> bool:
        """
        Reconciles the trade cycle against live Delta Exchange API state upon server recovery.
        1. Queries Delta Exchange for all active live orders on this product.
        2. Adopts existing live BUY orders and/or cancels redundant duplicate BUY orders.
        3. Confirms active position directly with Delta Exchange.
        4. Adopts existing live SELL exit orders and/or cancels redundant duplicate SELL orders.
        5. Synchronizes fill quantities, prices, and determines clean state transition.
        Returns True if the trade is still active and should resume running;
        Returns False if the trade is already concluded or cancelled.
        """
        self.log(f"[RECOVERY] Reconciling {self.trade_id} ({self.symbol}) with Delta Exchange...")
        try:
            self.fetch_live_quote()
        except Exception:
            pass

        # Step 0: Fetch all live open/pending orders for this contract from Delta Exchange
        live_orders = []
        try:
            live_orders = self.client.get_live_orders(query={"product_ids": str(self.product_id)}) or []
        except Exception as e:
            self.log(f"[RECOVERY] Warning fetching live orders for product {self.product_id}: {e}")

        # CRITICAL: Always filter by self.product_id to guarantee we NEVER adopt an order from another contract!
        live_buys = [
            o for o in live_orders 
            if int(o.get("product_id") or 0) == int(self.product_id) 
            and o.get("side") == "buy" 
            and o.get("state") in ("open", "pending")
        ]
        live_sells = [
            o for o in live_orders 
            if int(o.get("product_id") or 0) == int(self.product_id) 
            and o.get("side") == "sell" 
            and o.get("state") in ("open", "pending")
        ]

        # Step 1: Check BUY order on exchange
        if live_buys:
            # Pick matching order or primary live buy
            matching_buys = [o for o in live_buys if self.buy_order_id and str(o.get("id")) == str(self.buy_order_id)]
            primary_buy = matching_buys[0] if matching_buys else live_buys[0]
            self.buy_order_id = primary_buy.get("id")
            self.buy_limit_price = float(primary_buy.get("limit_price") or self.buy_limit_price or 0.0)
            self.buy_unfilled_qty = int(float(primary_buy.get("unfilled_size") or 0))
            order_sz = int(float(primary_buy.get("size") or self.total_size))
            self.buy_placed_qty = order_sz
            c_filled = max(0, order_sz - self.buy_unfilled_qty)
            if c_filled > self.buy_filled_qty:
                self.buy_filled_qty = c_filled
                avg_fill = float(primary_buy.get("average_fill_price") or self.buy_limit_price or 0.0)
                if avg_fill > 0:
                    self.buy_fill_price = avg_fill
            self.log(f"[RECOVERY] Adopted live BUY order {self.buy_order_id} from exchange: {self.buy_filled_qty}/{self.total_size} filled @ {self.buy_fill_price} ({self.buy_unfilled_qty} lots unfilled)")

            # Cancel any duplicate extra BUY orders on exchange
            for extra in live_buys:
                if str(extra.get("id")) != str(self.buy_order_id):
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=extra.get("id"))
                        self.log(f"[RECOVERY] Cancelled duplicate redundant BUY order {extra.get('id')} on Delta.")
                    except Exception:
                        pass
        elif self.buy_order_id:
            try:
                b_order = self.client.get_order_by_id(int(self.buy_order_id))
                if b_order:
                    b_state = b_order.get("state")
                    unfilled = float(b_order.get("unfilled_size", 0) or 0)
                    order_sz = float(b_order.get("size") or self.total_size)
                    c_filled = max(0.0, order_sz - unfilled)
                    avg_fill = float(b_order.get("average_fill_price") or self.buy_limit_price or 0.0)

                    if c_filled > self.buy_filled_qty:
                        self.buy_filled_qty = int(c_filled)
                        self.buy_unfilled_qty = int(unfilled)
                        if avg_fill > 0:
                            self.buy_fill_price = avg_fill
                        self.log(f"[RECOVERY] BUY order updated from exchange: {self.buy_filled_qty}/{self.total_size} filled @ {self.buy_fill_price}")

                    if b_state in ("cancelled", "closed"):
                        self.buy_unfilled_qty = 0
            except Exception as e:
                self.log(f"[RECOVERY] Warning checking BUY order {self.buy_order_id}: {e}")

        # Step 2: Check active account position on Delta
        open_pos_size = 0
        try:
            pos = self.client.get_position(self.product_id)
            if pos:
                open_pos_size = abs(int(pos.get("size") or 0))
                if open_pos_size > 0:
                    self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + open_pos_size)
                    entry_px = float(pos.get("entry_price") or 0.0)
                    if entry_px > 0 and (not self.buy_fill_price or self.buy_fill_price <= 0):
                        self.buy_fill_price = entry_px
                    self.log(f"[RECOVERY] Delta confirmed active long position: {open_pos_size} lots @ {self.buy_fill_price}")
        except Exception as e:
            self.log(f"[RECOVERY] Warning checking position: {e}")

        # If Delta confirms 0 open position on exchange:
        if open_pos_size == 0:
            has_live_buy = bool(live_buys)
            if not has_live_buy and self.buy_order_id and self.buy_unfilled_qty > 0:
                try:
                    b_order = self.client.get_order_by_id(int(self.buy_order_id))
                    has_live_buy = b_order and b_order.get("state") in ("open", "pending")
                except Exception:
                    pass

            if not has_live_buy:
                # Cancel any dangling sell orders
                for s in live_sells:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=s.get("id"))
                    except Exception:
                        pass
                self.sell_order_id = None
                self.sell_placed_qty = 0
                if self.buy_filled_qty > 0:
                    self.sell_filled_qty = self.buy_filled_qty
                    self.unrealized_pnl = 0.0
                    self.state = "COMPLETED_PROFIT" if (self.cost_to_cost_price and self.sell_fill_price and self.sell_fill_price > self.cost_to_cost_price) else "COMPLETED_COST"
                    self.log(f"[RECOVERY] Delta position is 0. Trade {self.trade_id} was completed/closed on exchange! Realized PnL: ${self.realized_pnl:.5f}")
                else:
                    self.state = "CANCELLED"
                    self.log(f"[RECOVERY] Delta confirmed no open position or buy order. Trade {self.trade_id} cancelled.")
                self.is_running = False
                self.stop_requested = True
                return False

        # Step 3: Check SELL exit order on exchange
        if live_sells:
            # Pick matching order or primary live sell
            matching_sells = [o for o in live_sells if self.sell_order_id and str(o.get("id")) == str(self.sell_order_id)]
            primary_sell = matching_sells[0] if matching_sells else live_sells[0]
            self.sell_order_id = primary_sell.get("id")
            self.sell_limit_price = float(primary_sell.get("limit_price") or self.sell_limit_price or 0.0)
            self.sell_unfilled_qty = int(float(primary_sell.get("unfilled_size") or 0))
            self.sell_placed_qty = int(float(primary_sell.get("size") or open_pos_size))
            s_filled = max(0, self.sell_placed_qty - self.sell_unfilled_qty)
            if s_filled > self.sell_filled_qty:
                inc = s_filled - self.sell_filled_qty
                self.sell_filled_qty = s_filled
                s_avg_fill = float(primary_sell.get("average_fill_price") or self.sell_limit_price or 0.0)
                if s_avg_fill > 0:
                    self.sell_fill_price = s_avg_fill
                chunk_pnl = (self.sell_fill_price - (self.buy_fill_price or 0.0)) * inc * self.contract_value
                self.realized_pnl += chunk_pnl
            self.log(f"[RECOVERY] Adopted live SELL order {self.sell_order_id} from exchange: {self.sell_placed_qty} lots @ {self.sell_limit_price} ({self.sell_unfilled_qty} lots unfilled)")

            # Cancel any duplicate extra SELL orders on exchange
            for extra in live_sells:
                if str(extra.get("id")) != str(self.sell_order_id):
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=extra.get("id"))
                        self.log(f"[RECOVERY] Cancelled duplicate redundant SELL order {extra.get('id')} on Delta.")
                    except Exception:
                        pass
        elif self.sell_order_id:
            try:
                s_order = self.client.get_order_by_id(int(self.sell_order_id))
                if s_order:
                    s_state = s_order.get("state")
                    s_unfilled = float(s_order.get("unfilled_size", 0) or 0)
                    s_order_sz = float(s_order.get("size") or self.sell_placed_qty or self.size)
                    s_filled = max(0.0, s_order_sz - s_unfilled)
                    s_avg_fill = float(s_order.get("average_fill_price") or self.sell_limit_price or 0.0)

                    if s_filled > self.sell_filled_qty:
                        inc = s_filled - self.sell_filled_qty
                        self.sell_filled_qty = int(s_filled)
                        if s_avg_fill > 0:
                            self.sell_fill_price = s_avg_fill
                        chunk_pnl = (self.sell_fill_price - (self.buy_fill_price or 0.0)) * inc * self.contract_value
                        self.realized_pnl += chunk_pnl
                        self.log(f"[RECOVERY] SELL exit fill detected: {self.sell_filled_qty} lots sold @ {self.sell_fill_price}. PnL = +${chunk_pnl:.5f}")

                    if s_state == "closed":
                        self.sell_unfilled_qty = 0
                        self.sell_filled_qty = int(s_order_sz)
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                    elif s_state == "cancelled":
                        self.log(f"[RECOVERY] SELL exit order {self.sell_order_id} was cancelled on exchange. Resetting for immediate re-placement.")
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
            except Exception as e:
                self.log(f"[RECOVERY] Warning checking SELL order {self.sell_order_id}: {e}")

        # Step 4: Calculate open inventory
        open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
        self.size = self.buy_filled_qty if self.buy_filled_qty > 0 else self.total_size

        # Check if trade already fully finished
        if open_pos_size == 0 and (self.sell_filled_qty >= self.total_size or (self.buy_unfilled_qty == 0 and self.buy_filled_qty > 0 and self.sell_filled_qty >= self.buy_filled_qty)):
            self.unrealized_pnl = 0.0
            if self.cost_to_cost_price and self.sell_fill_price and self.sell_fill_price > self.cost_to_cost_price:
                self.state = "COMPLETED_PROFIT"
            else:
                self.state = "COMPLETED_COST"
            self.log(f"[RECOVERY] Cycle fully finished. Final State: {self.state}, Realized PnL: ${self.realized_pnl:.5f}")
            return False

        if self.state == "CANCELLED" and open_inv == 0:
            return False

        # Determine active state
        if open_inv > 0 and self.buy_unfilled_qty > 0:
            self.state = "DUAL_ACTIVE"
        elif open_inv > 0:
            if self.cost_to_cost_price and self.sell_limit_price and self.sell_limit_price <= self.cost_to_cost_price:
                self.state = "WAITING_COST_TO_COST"
            else:
                self.state = "SELL_OPEN"
        elif self.buy_order_id and self.buy_unfilled_qty > 0:
            self.state = "BUY_OPEN"

        # Calculate cost-to-cost floor if we have filled buy price
        if self.buy_fill_price and not self.cost_to_cost_price:
            self.cost_to_cost_price, _, _, _ = self.calculate_cost_to_cost_price(self.buy_fill_price)

        self.log(f"[RECOVERY] Trade {self.trade_id} reconciled successfully! Active State: [{self.state}], Open Inv: {open_inv} lots, Buy Unfilled: {self.buy_unfilled_qty} lots, Sell Placed: {self.sell_placed_qty} lots.")
        return True

    def start_recovered(self):
        """Starts the MM cycle for a recovered trade directly entering the dual-sided execution loop."""
        if self.is_running:
            return
        self.is_running = True
        self.stop_requested = False
        self._thread = threading.Thread(target=self._run_recovered_lifecycle, daemon=True, name=f"MM-REC-{self.trade_id}")
        self._thread.start()

    def _run_recovered_lifecycle(self):
        """Lifecycle loop for recovered trades."""
        try:
            # 1. Connect to Private WebSocket
            try:
                self.private_ws = get_private_ws_manager(account=self.account_name)
                if self.private_ws:
                    self.log(f"Recovered cycle connected to private WebSocket order stream for [{self.account_name.upper()}].")
                    if self.buy_order_id and self.buy_unfilled_qty > 0:
                        self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                    if self.sell_order_id:
                        self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
            except Exception as ws_err:
                self.log(f"Warning: Could not connect to private WebSocket on recovery ({ws_err}).")
                self.private_ws = None

            # 2. Connect to Public WebSocket Market Stream
            try:
                self.public_ws = get_public_ws_manager(symbols=[self.symbol])
                self.log(f"Recovered cycle subscribed {self.symbol} to real-time L2 orderbook stream.")
            except Exception:
                self.public_ws = None

            self.log(f"Step 2: Resuming Option A Simultaneous Dual-Side Market Making Engine in state [{self.state}]...")
            self._run_dual_sided_loop()
        except Exception as e:
            self.state = "ERROR"
            self.log(f"CRITICAL ERROR in recovered MM lifecycle: {e}")
        finally:
            self.is_running = False
            self.log(f"Recovered MM cycle ended. Final State: {self.state}")
            self._persist_state()

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

    def stop(self, market_close: bool = True) -> dict:
        """
        Stops the MM trade cycle immediately:
        1. Sets stop_requested = True and is_running = False.
        2. Cancels any open BUY and SELL orders for this product on Delta Exchange.
        3. If market_close=True and an open position is held, executes an immediate MARKET order
           with reduce_only=True to square off the position at market.
        4. Updates trade state to CANCELLED and persists state.
        """
        self.stop_requested = True
        self.is_running = False
        self.log("Cancellation / Stop requested by user.")

        cancelled_orders = []
        # Step 1: Cancel active buy order if any
        if self.buy_order_id:
            try:
                self.client.cancel_order(product_id=self.product_id, order_id=int(self.buy_order_id))
                cancelled_orders.append(f"BUY {self.buy_order_id}")
            except Exception as e:
                self.log(f"Notice cancelling buy order {self.buy_order_id}: {e}")
            self.buy_order_id = None
            self.buy_unfilled_qty = 0

        # Step 2: Cancel active sell order if any
        if self.sell_order_id:
            try:
                self.client.cancel_order(product_id=self.product_id, order_id=int(self.sell_order_id))
                cancelled_orders.append(f"SELL {self.sell_order_id}")
            except Exception as e:
                self.log(f"Notice cancelling sell order {self.sell_order_id}: {e}")
            self.sell_order_id = None
            self.sell_placed_qty = 0
            self.sell_unfilled_qty = 0

        # Also cancel any dangling orders for this product on Delta Exchange
        try:
            self.client.cancel_all_orders(payload={"product_id": self.product_id})
        except Exception:
            pass

        # Unregister WebSocket listeners
        if self.private_ws:
            try:
                if self.buy_order_id:
                    self.private_ws.remove_order_listener(self.buy_order_id, self._on_buy_order_update)
                if self.sell_order_id:
                    self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)
            except Exception:
                pass

        closed_lots = 0
        market_fill_price = None

        # Step 3: Check position on Delta Exchange and square off with MARKET order
        if market_close:
            actual_pos = 0.0
            try:
                p = self.client.get_position(self.product_id)
                actual_pos = float(p.get("size", 0)) if p else 0.0
            except Exception:
                actual_pos = 0.0

            # Fallback to margined positions check if get_position returned 0 but we hold inventory
            if actual_pos == 0.0:
                try:
                    all_pos = self.client.request("GET", "/v2/positions/margined", auth=True).json().get("result", [])
                    if isinstance(all_pos, list):
                        p_match = next((p for p in all_pos if str(p.get("product_id")) == str(self.product_id)), None)
                        if p_match:
                            actual_pos = float(p_match.get("size", 0))
                except Exception:
                    pass

            # Fallback to internal inventory if Delta API had any transient latency
            if actual_pos == 0.0 and (self.buy_filled_qty > self.sell_filled_qty):
                open_inv = self.buy_filled_qty - self.sell_filled_qty
                time.sleep(0.1)
                try:
                    p2 = self.client.get_position(self.product_id)
                    actual_pos = float(p2.get("size", 0)) if p2 else 0.0
                except Exception:
                    actual_pos = float(open_inv)

            if abs(actual_pos) > 0:
                closed_lots = int(abs(actual_pos))
                opposing_side = "sell" if actual_pos > 0 else "buy"
                self.log(f"[STOP MARKET EXIT] Held position: {actual_pos} lots on {self.symbol}. Placing MARKET {opposing_side.upper()} order for {closed_lots} lots...")
                try:
                    market_order = self.client.place_order(
                        product_id=self.product_id,
                        size=closed_lots,
                        side=opposing_side,
                        order_type=OrderType.MARKET,
                        reduce_only="true"
                    )
                    avg_fill = float(market_order.get("average_fill_price") or 0.0)
                    if avg_fill <= 0:
                        quote = self.fetch_live_quote()
                        avg_fill = float(quote.get("bid" if opposing_side == "sell" else "ask") or quote.get("ltp") or 0.0)
                    market_fill_price = avg_fill

                    if actual_pos > 0:
                        self.sell_filled_qty = self.buy_filled_qty
                        self.sell_fill_price = avg_fill
                        self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                        if self.buy_fill_price and avg_fill > 0:
                            chunk_pnl = (avg_fill - self.buy_fill_price) * closed_lots * self.contract_value
                            self.realized_pnl += chunk_pnl
                            self.log(f"[STOP MARKET EXIT] Closed {closed_lots} lots @ MARKET ${avg_fill:.4f}! Realized PnL: ${chunk_pnl:.5f} USD.")
                    else:
                        self.log(f"[STOP MARKET EXIT] Closed {closed_lots} lots @ MARKET ${avg_fill:.4f}!")
                except Exception as m_err:
                    err_str = str(m_err).lower()
                    if "no_position_for_reduce_only" in err_str:
                        self.log(f"[STOP MARKET EXIT] Position on Delta Exchange is already flat (0).")
                        closed_lots = 0
                    else:
                        self.log(f"[STOP MARKET EXIT] Error placing market exit order: {m_err}")

        self.state = "CANCELLED"
        if closed_lots > 0:
            self.status_message = f"Stopped by user. Closed {closed_lots} lots at MARKET (${market_fill_price:.2f})"
        else:
            self.status_message = "Stopped by user (0 open positions)"

        self._persist_state()
        return {
            "success": True,
            "trade_id": self.trade_id,
            "state": self.state,
            "closed_lots": closed_lots,
            "market_fill_price": market_fill_price,
            "realized_pnl": round(self.realized_pnl, 6),
            "cancelled_orders": cancelled_orders
        }

    def upload_limit_price(self, new_price: float, side: str = None, is_manual: bool = False) -> dict:
        """
        Allows uploading/updating the limit price of the active order (Buy or Sell).
        If is_manual=True, locks the price so dynamic pegging will not revert it.
        """
        if self.stop_requested or self.state in ("CANCELLED", "ERROR", "COMPLETED_PROFIT", "COMPLETED_COST"):
            return {"success": False, "error": f"Trade already finished or cancelled ({self.state})"}

        valid_price = align_price(float(new_price), self.tick_size)
        self.log(f"Requested limit price upload: {valid_price} (side: {side or 'auto'}, manual: {is_manual})")

        target_side = (side or "").lower().strip()
        if not target_side:
            if self.sell_order_id and (self.buy_filled_qty > self.sell_filled_qty or self.state in ("SELL_OPEN", "DUAL_ACTIVE", "WAITING_COST_TO_COST")):
                target_side = "sell"
            elif self.buy_order_id:
                target_side = "buy"

        if target_side == "buy" and self.buy_order_id:
            try:
                # Delta API edit_order or cancel + re-place
                res = self._edit_or_replace_order(self.buy_order_id, valid_price, side="buy")
                if res.get("success"):
                    self.buy_limit_price = valid_price
                    self.custom_entry_price = valid_price
                    if is_manual:
                        self.manual_buy_override = True
                        self.manual_buy_price = valid_price
                        self.buy_peg_status = f"MANUAL_PRICE_{valid_price}"
                    if self.max_entry_price and valid_price > self.max_entry_price:
                        self.max_entry_price = valid_price
                    self.log(f"Successfully uploaded new BUY limit price: {valid_price}{' (MANUAL LOCK)' if is_manual else ''}")
                    return {"success": True, "new_price": valid_price, "order_id": self.buy_order_id, "side": "buy", "manual": is_manual}
                else:
                    return {"success": False, "error": res.get("error")}
            except Exception as e:
                return {"success": False, "error": str(e)}

        elif target_side == "sell" and self.sell_order_id:
            # Check user constraint: cannot lower sell price below cost-to-cost unless forced
            if self.cost_to_cost_price and valid_price < self.cost_to_cost_price:
                self.log(f"Notice: Upload price {valid_price} is below Cost-to-Cost ({self.cost_to_cost_price}). Applying per user request.")

            try:
                res = self._edit_or_replace_order(self.sell_order_id, valid_price, side="sell", reduce_only=True)
                if res.get("success"):
                    self.sell_limit_price = valid_price
                    if is_manual:
                        self.manual_sell_override = True
                        self.manual_sell_price = valid_price
                        self.sell_peg_status = f"MANUAL_PRICE_{valid_price}"
                    self.log(f"Successfully uploaded new SELL limit price: {valid_price}{' (MANUAL LOCK)' if is_manual else ''}")
                    return {"success": True, "new_price": valid_price, "order_id": self.sell_order_id, "side": "sell", "manual": is_manual}
                else:
                    return {"success": False, "error": res.get("error")}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"No active open order matching side '{target_side or 'any'}' in current state '{self.state}' to update."}

    def resume_auto_peg(self, side: str = None) -> dict:
        """Resumes automatic pegging by clearing manual price overrides."""
        target_side = (side or "").lower().strip()
        cleared = []
        if not target_side or target_side == "buy":
            self.manual_buy_override = False
            self.manual_buy_price = None
            cleared.append("buy")
        if not target_side or target_side == "sell":
            self.manual_sell_override = False
            self.manual_sell_price = None
            cleared.append("sell")
        self.log(f"Manual price override cleared for {', '.join(cleared)}. Resuming dynamic pegging.")
        return {"success": True, "message": f"Resumed auto-peg for {', '.join(cleared)}"}

    def _edit_or_replace_order(self, order_id: int, new_price: float, side: str, reduce_only: bool = False) -> dict:
        """Edits an order in-place or cancels and re-places if edit is unsupported. Thread-safe."""
        with self._edit_lock:
            # Check if cycle was already cancelled or stopped
            if self.stop_requested or self.state in ("CANCELLED", "ERROR"):
                return {"success": False, "error": f"Trade already terminated with state '{self.state}'"}

            # 1. First, check if the current order was already closed/filled or cancelled on Delta
            try:
                cur_order = self.client.get_order_by_id(int(order_id)) if order_id else None
                if cur_order:
                    cur_pid = cur_order.get("product_id")
                    if cur_pid and int(cur_pid) != int(self.product_id):
                        self.log(
                            f"[MM ALIEN ORDER DISOWNED] Order {order_id} belongs to product {cur_pid} "
                            f"({cur_order.get('product_symbol')}), NOT this cycle's product {self.product_id} ({self.symbol}). "
                            f"Disowning alien order from this cycle..."
                        )
                        if side == "buy":
                            self.buy_order_id = None
                            self.buy_unfilled_qty = self.total_size
                        else:
                            self.sell_order_id = None
                            self.sell_unfilled_qty = self.size
                        order_id = 0
                    else:
                        st = cur_order.get("state")
                        if st == "closed":
                            avg_fill = float(cur_order.get("average_fill_price") or new_price)
                            oid = int(order_id)
                            order_sz = float(cur_order.get("size") or (getattr(self, "buy_placed_qty", self.total_size) if side == "buy" else (self.sell_placed_qty or self.size)))
                            unfilled = float(cur_order.get("unfilled_size") or 0)
                            this_order_filled = max(0.0, order_sz - unfilled)
                            prev_filled = self._order_fills_map.get(oid, 0.0)
                            increment = max(0.0, this_order_filled - prev_filled)

                            if side == "sell":
                                if increment > 0:
                                    self._order_fills_map[oid] = this_order_filled
                                    self.sell_filled_qty += int(increment)
                                    self.sell_unfilled_qty = max(0, self.buy_filled_qty - self.sell_filled_qty)
                                self.sell_fill_price = avg_fill
                                self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                                self.sell_fill_event.set()
                                self.sell_order_id = None
                                self.sell_placed_qty = 0
                                self.sell_unfilled_qty = 0
                                open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                                if open_inv > 0:
                                    self.log(f"Order {order_id} already executed/filled on exchange at {avg_fill}. Placing new SELL exit order for remaining {open_inv} lots...")
                                    self._place_sell_exit_order(target_qty=open_inv)
                                else:
                                    self.log(f"Order {order_id} already executed/filled on exchange at {avg_fill}!")
                            else:
                                if increment > 0:
                                    self._order_fills_map[oid] = this_order_filled
                                    self.buy_filled_qty += int(increment)
                                    self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                                self.buy_fill_price = avg_fill
                                self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                                self.buy_fill_event.set()
                                self.buy_order_id = None
                                self.buy_placed_qty = 0
                                self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                                if self.buy_filled_qty > 0 and self.buy_unfilled_qty == 0:
                                    self.total_size = self.buy_filled_qty
                                    self.size = self.buy_filled_qty
                                self.log(f"Order {order_id} already executed/filled on exchange at {avg_fill}! (Total bought: {self.buy_filled_qty}/{self.total_size} lots)")
                            return {"success": False, "filled": True, "error": f"Order {order_id} already executed/filled on exchange at {avg_fill}"}
                        elif st == "cancelled":
                            if side == "buy":
                                actual_pos = 0
                                try:
                                    p = self.client.get_position(self.product_id)
                                    actual_pos = abs(int(p.get("size") or 0)) if p else 0
                                except Exception:
                                    pass
                                if actual_pos == 0:
                                    self.state = "CANCELLED"
                                    self.stop_requested = True
                                    self.log(f"BUY order {order_id} was cancelled on exchange. Stopping trade cycle (will not re-place).")
                                    return {"success": False, "cancelled": True, "error": "Order cancelled on exchange"}
                                else:
                                    self.buy_unfilled_qty = 0
                                    self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                                    self.size = actual_pos
                                    self.log(f"BUY order {order_id} was cancelled on exchange with {actual_pos} lots held. Continuing profit booking.")
                                    return {"success": False, "cancelled": True, "error": "Order cancelled with active position"}
            except Exception:
                pass

            # Determine exact active quantity for fallback re-place
            if side == "buy":
                active_size = self.buy_unfilled_qty if self.buy_unfilled_qty > 0 else (self.total_size - self.buy_filled_qty)
                order_total_size = int(getattr(self, "buy_placed_qty", None) or self.total_size)
            else:
                active_size = (self.size - self.sell_filled_qty) if self.size > self.sell_filled_qty else self.size
                order_total_size = int(getattr(self, "sell_placed_qty", None) or self.size)
            active_size = max(1, int(active_size))
            order_total_size = max(1, int(order_total_size))

            # If cur_order was retrieved, ensure order_total_size is at least the existing order size on exchange
            if cur_order and cur_order.get("size"):
                try:
                    cur_sz = int(float(cur_order.get("size")))
                    if cur_sz > 0:
                        order_total_size = max(order_total_size, cur_sz)
                except Exception:
                    pass

            # 2. Try Delta REST edit_order (only if order_id is valid and belongs to this product)
            # IMPORTANT: For Delta Exchange PUT /v2/orders, 'size' represents the CUMULATIVE TOTAL SIZE
            # of the order on the book, NOT the remaining unfilled contracts.
            # Passing active_size (unfilled size) will shrink/truncate the order size and drop lots!
            if order_id:
                try:
                    edit_payload = {
                        "id": int(order_id),
                        "product_id": int(self.product_id),
                        "limit_price": str(new_price),
                        "size": int(order_total_size)
                    }
                    res = self.client.edit_order(edit_payload)
                    if res and isinstance(res, dict) and res.get("id"):
                        return {"success": True, "order": res}
                except Exception as edit_err:
                    err_text = str(edit_err)
                    self.log(f"Edit API call failed ({err_text}), checking order status...")

                    # If user stopped the trade or trade was cancelled, do not attempt to re-place
                    if self.stop_requested or self.state in ("CANCELLED", "ERROR"):
                        return {"success": False, "error": f"Trade in state '{self.state}'"}

                    # If Delta rejected edit with 'open_order_not_found', check if order was cancelled or closed
                    if "open_order_not_found" in err_text or "order_not_found" in err_text:
                        try:
                            cur_order = self.client.get_order_by_id(int(order_id))
                            if cur_order:
                                cur_pid = cur_order.get("product_id")
                                if cur_pid and int(cur_pid) != int(self.product_id):
                                    self.log(
                                        f"[MM ALIEN ORDER DISOWNED] Order {order_id} belongs to product {cur_pid} "
                                        f"({cur_order.get('product_symbol')}), NOT this cycle's product {self.product_id} ({self.symbol}). "
                                        f"Disowning alien order and placing a fresh order on {self.symbol}..."
                                    )
                                    if side == "buy":
                                        self.buy_order_id = None
                                        self.buy_unfilled_qty = self.total_size
                                    else:
                                        self.sell_order_id = None
                                        self.sell_unfilled_qty = self.size
                                    order_id = 0
                                    # Fall through to Step 3 fresh placement
                                else:
                                    st = cur_order.get("state")
                                    if st == "cancelled":
                                        if side == "buy":
                                            actual_pos = 0
                                            try:
                                                p = self.client.get_position(self.product_id)
                                                actual_pos = abs(int(p.get("size") or 0)) if p else 0
                                            except Exception:
                                                pass
                                            if actual_pos == 0:
                                                self.state = "CANCELLED"
                                                self.stop_requested = True
                                                self.log(f"BUY order {order_id} was cancelled on exchange. Will NOT re-place.")
                                                return {"success": False, "cancelled": True, "error": "Order was cancelled on exchange"}
                                            else:
                                                self.buy_unfilled_qty = 0
                                                self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                                                self.size = actual_pos
                                                return {"success": False, "cancelled": True, "error": "Order cancelled with active position"}
                                    elif st == "closed":
                                        avg_fill = float(cur_order.get("average_fill_price") or new_price)
                                        oid = int(order_id)
                                        order_sz = float(cur_order.get("size") or (getattr(self, "buy_placed_qty", self.total_size) if side == "buy" else (self.sell_placed_qty or self.size)))
                                        unfilled = float(cur_order.get("unfilled_size") or 0)
                                        this_order_filled = max(0.0, order_sz - unfilled)
                                        prev_filled = self._order_fills_map.get(oid, 0.0)
                                        increment = max(0.0, this_order_filled - prev_filled)

                                        if side == "buy":
                                            if increment > 0:
                                                self._order_fills_map[oid] = this_order_filled
                                                self.buy_filled_qty += int(increment)
                                                self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                                            self.buy_fill_price = avg_fill
                                            self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                                            self.buy_fill_event.set()
                                            self.buy_order_id = None
                                            self.buy_placed_qty = 0
                                            self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                                            if self.buy_filled_qty > 0 and self.buy_unfilled_qty == 0:
                                                self.total_size = self.buy_filled_qty
                                                self.size = self.buy_filled_qty
                                            self.log(f"Order {order_id} already closed on exchange at {avg_fill}! (Total bought: {self.buy_filled_qty}/{self.total_size} lots)")
                                        else:
                                            if increment > 0:
                                                self._order_fills_map[oid] = this_order_filled
                                                self.sell_filled_qty += int(increment)
                                                self.sell_unfilled_qty = max(0, self.buy_filled_qty - self.sell_filled_qty)
                                            self.sell_fill_price = avg_fill
                                            self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                                            self.sell_fill_event.set()
                                            self.sell_order_id = None
                                            self.sell_placed_qty = 0
                                            self.sell_unfilled_qty = 0
                                            open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                                            if open_inv > 0:
                                                self.log(f"Order {order_id} already closed on exchange. Placing new SELL exit for remaining {open_inv} lots...")
                                                self._place_sell_exit_order(target_qty=open_inv)
                                        return {"success": False, "filled": True, "error": f"Order {order_id} already closed on exchange at {avg_fill}"}
                                    elif st in ("open", "pending"):
                                        # Order is open, but edit was rejected; fall through to Step 3 cancel & re-place
                                        pass
                            else:
                                # Order genuinely not found on exchange
                                if side == "buy":
                                    actual_pos = 0
                                    try:
                                        p = self.client.get_position(self.product_id)
                                        actual_pos = abs(int(p.get("size") or 0)) if p else 0
                                    except Exception:
                                        pass
                                    if actual_pos == 0:
                                        self.state = "CANCELLED"
                                        self.stop_requested = True
                                        self.log(f"BUY order {order_id} is no longer open on exchange. User cancellation detected. Stopping trade.")
                                        return {"success": False, "cancelled": True, "error": "BUY order not found on exchange"}
                                    else:
                                        self.buy_unfilled_qty = 0
                                        self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                                        self.size = actual_pos
                                        return {"success": False, "cancelled": True, "error": "BUY order not found with active position"}
                        except Exception as check_e:
                            self.log(f"Notice: Error verifying order status after edit failure ({check_e})")

            # 3. Fallback: Cancel and re-place
            # Final check before re-placing: never re-place a cancelled trade
            if self.stop_requested or self.state in ("CANCELLED", "ERROR"):
                return {"success": False, "error": f"Cannot re-place: trade is in state '{self.state}'"}

            self._is_replacing_order = True
            try:
                if order_id:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=int(order_id))
                    except Exception:
                        pass
                    time.sleep(0.15)

                if self.stop_requested or self.state in ("CANCELLED", "ERROR"):
                    return {"success": False, "error": f"Cannot re-place: trade is in state '{self.state}'"}

                new_order = None
                post_only_success = False

                # Step 3a: Attempt maker post-only limit order
                try:
                    new_order = self.client.place_order(
                        product_id=self.product_id,
                        size=active_size,
                        side=side,
                        limit_price=str(new_price),
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        post_only="true",
                        reduce_only="true" if reduce_only else "false"
                    )
                    # Check if Delta accepted then immediately auto-cancelled due to crossing
                    if new_order and new_order.get("state") == "cancelled" and new_order.get("cancellation_reason") == "immediate_execution_post_only":
                        post_only_success = False
                    else:
                        post_only_success = True
                except Exception as po_err:
                    post_only_success = False
                    po_str = str(po_err).lower()
                    if "no_position_for_reduce_only" in po_str:
                        self.log(f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). Position was already closed on exchange. Ending cycle.")
                        self.stop_requested = True
                        self.is_running = False
                        self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                        return {"success": False, "error": "no_position_for_reduce_only"}
                    if "max_oi_breached" in po_str or "only_reduce_only_orders_allowed" in po_str:
                        self.log(f"[MM-{self.trade_id[-6:]}] Delta Exchange Max OI Breached on {self.symbol}. Cannot re-place BUY order.")
                        self.stop_requested = True
                        self.is_running = False
                        self.state = "REJECTED_MAX_OI"
                        return {"success": False, "error": "max_oi_breached"}
                    if not (reduce_only or side == "sell"):
                        return {"success": False, "error": str(po_err)}

                # Step 3b: If post-only rejected on an EXIT order, execute with post_only=false for immediate fill!
                if not post_only_success and (reduce_only or side == "sell"):
                    self.log(f"Notice: Exit price {new_price} crosses resting bid. Executing taker/marketable limit fill (post_only=False)...")
                    try:
                        new_order = self.client.place_order(
                            product_id=self.product_id,
                            size=active_size,
                            side=side,
                            limit_price=str(new_price),
                            order_type=OrderType.LIMIT,
                            time_in_force=TimeInForce.GTC,
                            post_only="false",
                            reduce_only="true" if reduce_only else "false"
                        )
                    except Exception as po_err2:
                        if "no_position_for_reduce_only" in str(po_err2).lower():
                            self.log(f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). Ending cycle.")
                            self.stop_requested = True
                            self.is_running = False
                            self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                        return {"success": False, "error": str(po_err2)}

                if not new_order or not new_order.get("id"):
                    return {"success": False, "error": "Failed to re-place order"}

                # If trade was cancelled while placing, cancel the newly placed order immediately!
                if self.stop_requested or self.state in ("CANCELLED", "ERROR"):
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=new_order.get("id"))
                    except Exception:
                        pass
                    return {"success": False, "error": "Trade was cancelled during replacement"}

                # Update listener IDs and placed quantities
                if side == "buy":
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.remove_order_listener(self.buy_order_id, self._on_buy_order_update)
                    self.buy_order_id = new_order.get("id")
                    self.buy_placed_qty = active_size
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                else:
                    if self.private_ws and self.sell_order_id:
                        self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)
                    self.sell_order_id = new_order.get("id")
                    self.sell_placed_qty = active_size
                    if self.private_ws and self.sell_order_id:
                        self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)

                # Check if newly placed order filled immediately
                if new_order.get("state") == "closed":
                    avg_fill = float(new_order.get("average_fill_price") or new_price)
                    if side == "sell":
                        self.sell_fill_price = avg_fill
                        self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                        self.sell_fill_event.set()
                    else:
                        self.buy_fill_price = avg_fill
                        self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                        self.buy_fill_event.set()
                    self.log(f"Re-placed {side.upper()} order executed immediately on exchange at {avg_fill}!")

                return {"success": True, "order": new_order}
            except Exception as replace_err:
                return {"success": False, "error": str(replace_err)}
            finally:
                self._is_replacing_order = False

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

            self.log(f"Step 1: Discovering market & placing Maker Limit BUY for {self.total_size} lots...")
            if not self._phase_1_place_buy():
                self.is_running = False
                return

            self.log("Step 2: Activating Option A Simultaneous Dual-Side Market Making Engine...")
            self._run_dual_sided_loop()

        except Exception as e:
            self.state = "ERROR"
            self.log(f"CRITICAL ERROR in MM lifecycle: {e}")
        finally:
            self.is_running = False
            self.log(f"MM cycle ended. Final State: {self.state}")

    def _phase_1_place_buy(self) -> bool:
        """Phase 1: Places Maker Limit BUY order."""
        self.state = "PLACING_BUY"

        # Pre-flight check: Verify if exchange is in reduce-only mode (Max OI breached)
        try:
            ticker = self.client.get_ticker(self.symbol)
            if ticker and ticker.get("oi_reduce_only_mode"):
                self.state = "REJECTED_MAX_OI"
                self.log(
                    f"[MAX OI BREACHED] Cannot place BUY order on {self.symbol}: Exchange Maximum Open Interest (OI) limit breached. "
                    f"Delta Exchange has placed this contract in Reduce-Only mode. New positions cannot be opened. "
                    f"Please select an adjacent strike."
                )
                return False
        except Exception as e_chk:
            self.log(f"Notice: Ticker OI check failed ({e_chk}), proceeding to order placement.")

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

        # Verify if an open BUY order ALREADY exists on Delta Exchange before placing
        try:
            live_orders = self.client.get_live_orders(query={"product_ids": str(self.product_id)}) or []
            # CRITICAL: Filter by product_id to guarantee only orders for THIS product are adopted
            live_buys = [
                o for o in live_orders 
                if int(o.get("product_id") or 0) == int(self.product_id) 
                and o.get("side") == "buy" 
                and o.get("state") in ("open", "pending")
            ]
            if live_buys:
                primary_buy = live_buys[0]
                self.buy_order_id = primary_buy.get("id")
                self.buy_limit_price = float(primary_buy.get("limit_price") or valid_price)
                self.buy_unfilled_qty = int(float(primary_buy.get("unfilled_size") or self.size))
                self.buy_placed_qty = int(float(primary_buy.get("size") or self.size))
                self.log(f"Adopted existing live BUY order {self.buy_order_id} @ {self.buy_limit_price}. Avoided duplicate placement!")
                for extra in live_buys[1:]:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=extra.get("id"))
                        self.log(f"Cancelled duplicate redundant BUY order {extra.get('id')} on Delta.")
                    except Exception:
                        pass
                self.buy_fill_event.clear()
                if self.private_ws and self.buy_order_id:
                    self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                self.state = "BUY_OPEN"
                self.buy_peg_status = f"TRACKING_BID_{self.buy_limit_price}"
                self._persist_state()
                return True
        except Exception as le:
            self.log(f"Notice checking live buy orders: {le}")

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
            self.buy_placed_qty = self.size
            self.buy_fill_event.clear()
            if self.private_ws and self.buy_order_id:
                self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
            self.state = "BUY_OPEN"
            self.buy_peg_status = f"TRACKING_BID_{valid_price}"
            self.log(f"Limit BUY order active on book! Order ID: {self.buy_order_id} @ {valid_price} (Two-Way Peg & WS Listener Active)")
            return True
        except Exception as e:
            err_msg = str(e)
            if "max_oi_breached" in err_msg.lower() or "only_reduce_only_orders_allowed" in err_msg.lower():
                self.state = "REJECTED_MAX_OI"
                self.log(
                    f"[MAX OI BREACHED] Order placement rejected by Delta Exchange: Maximum Open Interest reached on {self.symbol}. "
                    f"Delta Exchange only permits reduce-only (closing) orders. Fresh position entry is prohibited. "
                    f"Please select an adjacent strike."
                )
                return False

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
                    self.buy_placed_qty = self.size
                    self.buy_limit_price = fallback_price
                    self.buy_fill_event.clear()
                    if self.private_ws and self.buy_order_id:
                        self.private_ws.add_order_listener(self.buy_order_id, self._on_buy_order_update)
                    self.state = "BUY_OPEN"
                    self.buy_peg_status = f"TRACKING_BID_{fallback_price}"
                    self.log(f"Fallback Limit BUY order active! Order ID: {self.buy_order_id} @ {fallback_price} (Two-Way Peg & WS Listener Active)")
                    return True
                except Exception as e2:
                    err_msg2 = str(e2)
                    if "max_oi_breached" in err_msg2.lower() or "only_reduce_only_orders_allowed" in err_msg2.lower():
                        self.state = "REJECTED_MAX_OI"
                        self.log(f"[MAX OI BREACHED] Order placement rejected by Delta Exchange: Maximum Open Interest reached on {self.symbol}.")
                        return False
                    self.log(f"Fallback order placement failed: {e2}")
                    self.state = "ERROR"
                    return False
            else:
                self.log(f"Order placement failed: {err_msg}")
                self.state = "ERROR"
                return False

    def _run_dual_sided_loop(self):
        """
        Option A: Simultaneous Dual-Side Market Making Engine:
        - Maintains the active BUY order on the bid side (pegging competitor bids).
        - The instant any partial BUY fill occurs (e.g. 25 of 50 lots), immediately calculates
          cost-to-cost floor and places a reduce-only LIMIT SELL exit order for the filled quantity!
        - Simultaneously manages both sides:
          1. BUY side floats with bids to acquire remaining lots.
          2. SELL side floats with asks to take profit on filled lots (or anchors at cost floor if price drops).
        - If additional lots fill on BUY, expands the active SELL exit order quantity.
        - When all lots are bought and sold, finishes with COMPLETED_PROFIT or COMPLETED_COST.
        """
        poll_interval = 0.35
        last_buy_poll = 0.0
        last_sell_poll = 0.0

        try:
            while self.is_running and not self.stop_requested:
                now = time.time()

                # ----------------------------------------------------
                # PART 1: Monitor BUY Leg & Dynamic Bid Pegging
                # ----------------------------------------------------
                if self.buy_order_id and self.buy_unfilled_qty > 0 and self.state != "CANCELLED":
                    ws_active = self.private_ws and self.private_ws.is_authenticated
                    if (not ws_active) or (now - last_buy_poll >= 2.5):
                        last_buy_poll = now
                        self._check_buy_order_status()

                    if self.two_way_peg_enabled and self.buy_unfilled_qty > 0 and self.state != "CANCELLED":
                        self._peg_buy_order(now)

                # ----------------------------------------------------
                # PART 2: Immediate Profit Booking on Filled Inventory
                # ----------------------------------------------------
                open_inventory = max(0, self.buy_filled_qty - self.sell_filled_qty)

                # Reset stale / closed sell order id if unfilled size is 0
                if self.sell_order_id and self.sell_unfilled_qty <= 0:
                    self.sell_order_id = None
                    self.sell_placed_qty = 0

                if open_inventory > 0:
                    self.size = self.buy_filled_qty

                    # If no active SELL exit order exists, place one immediately!
                    if self.sell_order_id is None:
                        self.log(f"[DUAL-SIDE MM] Immediate Profit Booking Triggered! We hold {open_inventory} filled lots. Placing Limit SELL exit order...")
                        self._place_sell_exit_order(target_qty=open_inventory)

                    # If SELL order exists, check if its quantity needs expanding
                    elif self.sell_placed_qty < open_inventory:
                        self.log(f"[DUAL-SIDE MM] Additional buy fill detected! Expanding SELL exit order from {self.sell_placed_qty} to {open_inventory} lots...")
                        self._resize_sell_exit_order(new_qty=open_inventory)

                    # Manage active SELL order status & Ask Pegging
                    if self.sell_order_id:
                        ws_active = self.private_ws and self.private_ws.is_authenticated
                        if (not ws_active) or (now - last_sell_poll >= 2.5):
                            last_sell_poll = now
                            self._check_sell_order_status()

                        if self.two_way_peg_enabled and self.sell_unfilled_qty > 0:
                            self._peg_sell_order(now)

                # ----------------------------------------------------
                # PART 3: Cycle Completion Check
                # ----------------------------------------------------
                # A cycle is ready for completion check if:
                # 1. All total_size lots were bought and sold: (sell_filled_qty >= total_size)
                # 2. OR all actually filled lots were sold (sell_filled_qty >= buy_filled_qty > 0)
                #    and the buy order is no longer open / active (buy_order_id is None or buy_unfilled_qty == 0)
                # 3. OR open inventory is 0 and no buy or sell orders exist
                open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                is_buy_done = (self.buy_order_id is None or self.buy_unfilled_qty == 0 or self.buy_filled_qty >= self.total_size)
                is_inventory_flat = (self.buy_filled_qty > 0 and open_inv == 0)

                if (self.sell_filled_qty >= self.total_size) or (is_inventory_flat and is_buy_done):
                    actual_pos = 0
                    try:
                        pos = self.client.get_position(self.product_id)
                        actual_pos = abs(int(pos.get("size") or 0)) if pos else 0
                    except Exception:
                        actual_pos = 0

                    if actual_pos > 0:
                        self.log(f"[DUAL-SIDE MM] Delta Exchange still holds {actual_pos} open long contracts! Resuming exit management.")
                        self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                        self.size = actual_pos
                        self.state = "SELL_OPEN"
                        if not self.sell_order_id:
                            self._place_sell_exit_order(target_qty=actual_pos)
                    else:
                        self.unrealized_pnl = 0.0
                        self.is_running = False
                        self.stop_requested = True
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        self.sell_unfilled_qty = 0
                        done_qty = self.buy_filled_qty if self.buy_filled_qty > 0 else self.total_size
                        if self.cost_to_cost_price and self.sell_fill_price and self.sell_fill_price > self.cost_to_cost_price:
                            self.state = "COMPLETED_PROFIT"
                            self.log(f"[DUAL-SIDE MM] COMPLETE! All {done_qty} executed lots bought and sold with PROFIT! Total Realized PnL = +${self.realized_pnl:.5f} USD.")
                        else:
                            self.state = "COMPLETED_COST"
                            self.log(f"[DUAL-SIDE MM] COMPLETE! All {done_qty} executed lots bought and sold at Cost Breakeven. Total Realized PnL = ${self.realized_pnl:.5f} USD.")
                        return

                # If BUY was cancelled with 0 fills and no inventory
                if self.state == "CANCELLED" and self.buy_filled_qty == 0:
                    self.log("[DUAL-SIDE MM] BUY order cancelled with 0 fills. Stopping trade cycle.")
                    return

                # Event-driven wait: wakes up sub-50ms when any WS order event triggers
                self.buy_fill_event.wait(timeout=poll_interval)
                self.buy_fill_event.clear()
                self.sell_fill_event.wait(timeout=0.05)
                self.sell_fill_event.clear()

            # Handle user stop request
            if self.stop_requested:
                self.log("Cycle stop requested. Cancelling any open orders...")
                if self.buy_order_id and self.buy_unfilled_qty > 0:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=self.buy_order_id)
                    except Exception:
                        pass
                    self.buy_unfilled_qty = 0

                if self.sell_order_id:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=self.sell_order_id)
                    except Exception:
                        pass
                    self.sell_order_id = None
                    self.sell_placed_qty = 0
                    self.sell_unfilled_qty = 0

                open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                if open_inv > 0:
                    self.log(f"[STOP MARKET CLOSE] Holding {open_inv} open long lots. Closing position with MARKET SELL order...")
                    try:
                        mkt_order = self.client.place_order(
                            product_id=self.product_id,
                            size=open_inv,
                            side="sell",
                            order_type=OrderType.MARKET,
                            reduce_only="true"
                        )
                        avg_fill = float(mkt_order.get("average_fill_price") or 0.0)
                        self.sell_filled_qty = self.buy_filled_qty
                        self.sell_fill_price = avg_fill if avg_fill > 0 else (self.sell_fill_price or self.buy_fill_price or 0.0)
                        self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                        if self.buy_fill_price and avg_fill > 0:
                            self.realized_pnl += (avg_fill - self.buy_fill_price) * open_inv * self.contract_value
                        self.log(f"[STOP MARKET CLOSE] Successfully closed {open_inv} lots at MARKET @ {avg_fill}!")
                    except Exception as e:
                        self.log(f"[STOP MARKET CLOSE] Error placing market sell order: {e}")
                self.state = "CANCELLED"

        finally:
            if self.private_ws:
                if self.buy_order_id:
                    self.private_ws.remove_order_listener(self.buy_order_id, self._on_buy_order_update)
                if self.sell_order_id:
                    self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)

    def _check_buy_order_status(self):
        """Fallback REST poller to verify BUY order state and fill progress."""
        if not self.buy_order_id:
            return
        try:
            oid = int(self.buy_order_id)
            order = self.client.get_order_by_id(oid)
            if not order:
                return
            state = order.get("state")
            unfilled = float(order.get("unfilled_size", 0) or 0)
            order_sz = float(order.get("size") or self.total_size)
            this_order_filled = max(0.0, order_sz - unfilled)
            avg_fill = float(order.get("average_fill_price") or self.buy_limit_price or 0.0)

            prev_filled = self._order_fills_map.get(oid, 0.0)
            increment = max(0.0, this_order_filled - prev_filled)

            if increment > 0:
                self._order_fills_map[oid] = this_order_filled
                self.buy_filled_qty += int(increment)
                self.buy_unfilled_qty = max(0, self.total_size - self.buy_filled_qty)
                if avg_fill > 0:
                    if self.buy_fill_price and self.buy_filled_qty > int(increment):
                        prev_qty = self.buy_filled_qty - int(increment)
                        self.buy_fill_price = ((self.buy_fill_price * prev_qty) + (avg_fill * increment)) / self.buy_filled_qty
                    else:
                        self.buy_fill_price = avg_fill
                    self.weighted_buy_price = self.buy_fill_price
                self.buy_fill_time = datetime.now(timezone.utc).isoformat()
                if self.buy_unfilled_qty > 0:
                    self.state = "DUAL_ACTIVE"
                elif self.sell_filled_qty > 0 and self.sell_filled_qty < self.total_size:
                    self.state = "DUAL_ACTIVE"
                else:
                    self.state = "BUY_FILLED"
                self.log(f"BUY fill detected via REST check: {self.buy_filled_qty}/{self.total_size} lots filled @ {self.buy_fill_price}. ({self.buy_unfilled_qty} lots remaining to buy)")
                self.buy_fill_event.set()

            if state == "closed" or (order and unfilled == 0 and this_order_filled > 0):
                self.buy_order_id = None
                self.buy_unfilled_qty = 0
                if self.buy_filled_qty > 0:
                    self.total_size = self.buy_filled_qty
                    self.size = self.buy_filled_qty

            if state == "cancelled":
                if getattr(self, "_is_replacing_order", False):
                    return
                actual_pos = 0
                try:
                    p = self.client.get_position(self.product_id)
                    actual_pos = abs(int(p.get("size") or 0)) if p else 0
                except Exception:
                    actual_pos = self.buy_filled_qty

                if actual_pos > 0:
                    self.buy_unfilled_qty = 0
                    self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                    self.size = actual_pos
                    self.state = "DUAL_ACTIVE" if self.sell_order_id else "BUY_FILLED"
                    self.log(f"BUY order cancelled on exchange with {actual_pos} lots held. Continuing to sell/exit active position.")
                else:
                    self.state = "CANCELLED"
                    self.stop_requested = True
                    self.log("BUY order cancelled on exchange with 0 fills. Stopping trade cycle.")
                self.buy_fill_event.set()
        except Exception as e:
            self.log(f"Notice: Failed to fetch BUY order status ({e})")

    def _check_sell_order_status(self):
        """Fallback REST poller to verify SELL order state and exit fill progress."""
        if not self.sell_order_id:
            return
        try:
            oid = int(self.sell_order_id)
            order = self.client.get_order_by_id(oid)
            if not order:
                return
            state = order.get("state")
            unfilled = float(order.get("unfilled_size", 0) or 0)
            order_sz = float(order.get("size") or self.sell_placed_qty or self.size)
            this_order_filled = max(0.0, order_sz - unfilled)
            avg_fill = float(order.get("average_fill_price") or self.sell_limit_price or 0.0)

            prev_filled = self._order_fills_map.get(oid, 0.0)
            increment = max(0.0, this_order_filled - prev_filled)

            if increment > 0:
                self._order_fills_map[oid] = this_order_filled
                self.sell_filled_qty += int(increment)
                self.sell_unfilled_qty = max(0, self.buy_filled_qty - self.sell_filled_qty)
                if avg_fill > 0:
                    self.sell_fill_price = avg_fill
                    self.weighted_sell_price = avg_fill
                self.sell_fill_time = datetime.now(timezone.utc).isoformat()

                chunk_pnl = (self.sell_fill_price - (self.buy_fill_price or 0.0)) * increment * self.contract_value
                self.realized_pnl += chunk_pnl
                if self.buy_unfilled_qty > 0:
                    self.state = "DUAL_ACTIVE"
                elif self.sell_filled_qty >= self.buy_filled_qty:
                    actual_pos = 0
                    try:
                        pos = self.client.get_position(self.product_id)
                        actual_pos = abs(int(pos.get("size") or 0)) if pos else 0
                    except Exception:
                        actual_pos = 0
                    if actual_pos > 0:
                        self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                        self.size = actual_pos
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        self.sell_unfilled_qty = actual_pos
                        self.state = "SELL_OPEN"
                        self.log(f"Sell order filled, but Delta holds {actual_pos} open long lots! Placing fresh SELL exit order...")
                        self._place_sell_exit_order(target_qty=actual_pos)
                    else:
                        if self.cost_to_cost_price and self.sell_fill_price > self.cost_to_cost_price:
                            self.state = "COMPLETED_PROFIT"
                        else:
                            self.state = "COMPLETED_COST"
                else:
                    self.state = "SELL_PARTIAL"
                self.log(f"SELL fill detected via REST check: {self.sell_filled_qty}/{self.buy_filled_qty} lots sold @ {self.sell_fill_price}. Incremental Realized PnL = +${chunk_pnl:.5f} USD. ({self.sell_unfilled_qty} lots remaining to sell)")
                self.sell_fill_event.set()

            # If order is closed or unfilled size is 0, check if open inventory remains
            if state == "closed" or unfilled == 0:
                open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                if open_inv > 0 and (oid is None or self.sell_order_id == oid):
                    self.sell_order_id = None
                    self.sell_placed_qty = 0
                    self.sell_unfilled_qty = open_inv
                    self.log(f"Sell order {oid} finished for {int(order_sz)} lots, but {open_inv} open long lots remain! Placing fresh SELL exit order...")
                    self._place_sell_exit_order(target_qty=open_inv)

            if state == "cancelled":
                try:
                    pos = self.client.get_position(self.product_id)
                    open_pos_size = abs(int(pos.get("size") or 0)) if pos else 0
                    if open_pos_size == 0:
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        return
                    else:
                        self.log(f"Notice: SELL order was cancelled on exchange, but position is still {open_pos_size} lots. Re-placing SELL exit...")
                        self.sell_order_id = None
                        self.sell_placed_qty = 0
                        self._place_sell_exit_order(target_qty=open_pos_size)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Notice: Failed to fetch SELL order status ({e})")

    def _place_sell_exit_order(self, target_qty: int) -> bool:
        """Places initial Limit SELL exit order to begin profit booking on filled inventory."""
        if target_qty <= 0:
            return False

        # Verify live position with Delta Exchange before placing reduce-only sell
        try:
            pos = self.client.get_position(self.product_id)
            actual_pos = abs(int(pos.get("size") or 0)) if pos else 0
        except Exception as pe:
            self.log(f"[MM-{self.trade_id[-6:]}] Notice checking exchange position: {pe}")
            actual_pos = target_qty

        if actual_pos == 0:
            self.log(
                f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 open position. "
                f"Position or order was already cancelled/closed directly on exchange. Ending MM trade cycle cleanly."
            )
            self.sell_order_id = None
            self.sell_placed_qty = 0
            self.sell_filled_qty = self.buy_filled_qty
            self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
            self.stop_requested = True
            self.is_running = False
            return False

        if target_qty > actual_pos:
            self.log(
                f"[MM-{self.trade_id[-6:]}] Adjusting exit sell quantity from {target_qty} to actual exchange position {actual_pos} lots."
            )
            target_qty = actual_pos

        self.size = actual_pos
        self.sell_placed_qty = target_qty
        self.sell_unfilled_qty = target_qty

        ext_ask = self.get_external_best_ask()
        quote = self.fetch_live_quote()
        cur_ask = ext_ask if ext_ask is not None else quote.get("ask")

        # Baseline: Cost-to-Cost price
        self.cost_to_cost_price, rt_fee, fee_ticks, buf = self.calculate_cost_to_cost_price(self.buy_fill_price)

        # Determine target sell price
        if self.custom_target_price:
            raw_target = max(self.cost_to_cost_price, self.custom_target_price)
        else:
            if cur_ask and cur_ask > self.cost_to_cost_price:
                raw_target = max(self.cost_to_cost_price, cur_ask - self.tick_size)
            else:
                raw_target = self.cost_to_cost_price

        target_price = align_price(raw_target, self.tick_size)
        self.sell_limit_price = target_price

        # Verify if an open SELL exit order ALREADY exists on Delta Exchange before placing
        try:
            live_orders = self.client.get_live_orders(query={"product_ids": str(self.product_id)}) or []
            # CRITICAL: Filter by product_id to guarantee only orders for THIS product are adopted
            live_sells = [
                o for o in live_orders 
                if int(o.get("product_id") or 0) == int(self.product_id) 
                and o.get("side") == "sell" 
                and o.get("state") in ("open", "pending")
            ]
            if live_sells:
                primary_sell = live_sells[0]
                self.sell_order_id = primary_sell.get("id")
                self.sell_placed_qty = int(float(primary_sell.get("size") or target_qty))
                self.sell_unfilled_qty = int(float(primary_sell.get("unfilled_size") or self.sell_placed_qty))
                self.sell_limit_price = float(primary_sell.get("limit_price") or self.sell_limit_price or target_price)
                self.log(
                    f"[DUAL-SIDE MM] Adopted existing live SELL exit order {self.sell_order_id} "
                    f"({self.sell_placed_qty} lots @ {self.sell_limit_price}). Avoided duplicate placement!"
                )
                for extra in live_sells[1:]:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=extra.get("id"))
                        self.log(f"[DUAL-SIDE MM] Cancelled duplicate redundant SELL order {extra.get('id')}.")
                    except Exception:
                        pass
                if self.private_ws and self.sell_order_id:
                    self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
                if self.buy_unfilled_qty > 0:
                    self.state = "DUAL_ACTIVE"
                else:
                    self.state = "SELL_OPEN"
                self.sell_peg_status = f"FLOATING_ASK_{self.sell_limit_price}"
                self._persist_state()
                return True
        except Exception as le:
            self.log(f"[DUAL-SIDE MM] Notice checking live sell orders: {le}")

        self.log(
            f"[DUAL-SIDE MM] Placing LIMIT SELL exit: Size={target_qty} lots, Price={target_price} "
            f"(Cost Floor={self.cost_to_cost_price}, Market Ask={cur_ask}, Reduce-Only=True)"
        )

        try:
            sell_res = self.client.place_order(
                product_id=self.product_id,
                size=int(target_qty),
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

            if self.buy_unfilled_qty > 0:
                self.state = "DUAL_ACTIVE"
            else:
                self.state = "SELL_OPEN"

            self.sell_peg_status = f"FLOATING_ASK_{target_price}"
            self.log(f"Limit SELL exit active! Order ID: {self.sell_order_id} @ {target_price} (Dual-Sided Quoting Active)")
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "no_position_for_reduce_only" in err_str:
                self.log(
                    f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). "
                    f"Position was already closed or cancelled on exchange. Stopping trade cycle cleanly."
                )
                self.sell_order_id = None
                self.sell_placed_qty = 0
                self.sell_filled_qty = self.buy_filled_qty
                self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                self.stop_requested = True
                self.is_running = False
                return False

            self.log(f"Post-only sell rejected ({e}). Retrying without post-only...")
            try:
                sell_res = self.client.place_order(
                    product_id=self.product_id,
                    size=int(target_qty),
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

                if self.buy_unfilled_qty > 0:
                    self.state = "DUAL_ACTIVE"
                else:
                    self.state = "SELL_OPEN"

                self.sell_peg_status = f"FLOATING_ASK_{target_price}"
                self.log(f"Limit SELL exit placed! Order ID: {self.sell_order_id} @ {target_price}")
                return True
            except Exception as e2:
                err_str2 = str(e2).lower()
                if "no_position_for_reduce_only" in err_str2:
                    self.log(
                        f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). "
                        f"Position was already closed or cancelled on exchange. Stopping trade cycle cleanly."
                    )
                    self.sell_order_id = None
                    self.sell_placed_qty = 0
                    self.sell_filled_qty = self.buy_filled_qty
                    self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                    self.stop_requested = True
                    self.is_running = False
                    return False

                self.log(f"Critical: Failed to place SELL exit order: {e2}")
                return False

    def _resize_sell_exit_order(self, new_qty: int) -> bool:
        """Expands the resting SELL exit order quantity when additional BUY lots fill."""
        if not self.sell_order_id or new_qty <= 0:
            return False
        try:
            edit_payload = {
                "id": int(self.sell_order_id),
                "product_id": int(self.product_id),
                "limit_price": str(self.sell_limit_price),
                "size": int(new_qty)
            }
            res = self.client.edit_order(edit_payload)
            if res and isinstance(res, dict) and res.get("id"):
                self.sell_placed_qty = new_qty
                self.sell_unfilled_qty = new_qty - self.sell_filled_qty
                self.log(f"[DUAL-SIDE MM] Successfully updated SELL order size to {new_qty} lots!")
                return True
        except Exception as e:
            if "no_position_for_reduce_only" in str(e).lower():
                self.log(f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position during resize. Ending cycle cleanly.")
                self.stop_requested = True
                self.is_running = False
                self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                return False
            self.log(f"Edit size failed ({e}). Re-placing SELL order for {new_qty} lots...")
            self._is_replacing_order = True
            try:
                try:
                    self.client.cancel_order(product_id=self.product_id, order_id=self.sell_order_id)
                except Exception:
                    pass
                self.sell_order_id = None
                self.sell_placed_qty = 0
                target_re_place = max(1, self.buy_filled_qty - self.sell_filled_qty)
                return self._place_sell_exit_order(target_qty=target_re_place)
            finally:
                self._is_replacing_order = False

    def _peg_buy_order(self, now: float):
        """Dynamically floats BUY order with market bids while preventing adverse selection."""
        if not self.buy_order_id or self.buy_unfilled_qty <= 0:
            return
        self.fetch_live_quote()
        comp_bid = self.get_external_best_bid()
        comp_ask = self.get_external_best_ask()
        self.competitor_bid = comp_bid
        self.competitor_ask = comp_ask

        # If user manually set buy price, DO NOT auto-peg to competitor bids!
        if getattr(self, "manual_buy_override", False) and getattr(self, "manual_buy_price", None) is not None:
            self.buy_peg_status = f"MANUAL_PRICE_{self.manual_buy_price}"
            return

        if comp_bid is not None and comp_bid > 0:
            if comp_ask and (comp_bid + self.tick_size) < comp_ask:
                ideal_buy = align_price(comp_bid + self.tick_size, self.tick_size)
            else:
                ideal_buy = align_price(comp_bid, self.tick_size)

            if comp_ask and ideal_buy >= comp_ask:
                ideal_buy = align_price(comp_ask - self.tick_size, self.tick_size)

            if self.max_entry_price:
                ideal_buy = min(self.max_entry_price, ideal_buy)

            if ideal_buy > 0 and abs(ideal_buy - self.buy_limit_price) >= (self.tick_size * 0.99):
                if now - self.last_edit_time >= self.edit_debounce_sec:
                    self.last_edit_time = now
                    if ideal_buy > self.buy_limit_price:
                        self.buy_peg_status = f"FLOATING_UP_{ideal_buy}"
                        self.log(f"[TWO-WAY BID PEG] Competitor bid moved up to {comp_bid}. Auto-switching BUY limit UP from {self.buy_limit_price} to {ideal_buy} to hold #1 priority!")
                    else:
                        self.buy_peg_status = f"RETREATING_DOWN_{ideal_buy}"
                        self.log(f"[TWO-WAY BID PEG] Market bids retreated to {comp_bid}. Dropping BUY limit DOWN from {self.buy_limit_price} to {ideal_buy} to protect against adverse drop!")
                    self.upload_limit_price(ideal_buy, side="buy", is_manual=False)
        else:
            self.buy_peg_status = f"TRACKING_BID_{self.buy_limit_price}"

    def _peg_sell_order(self, now: float):
        """Dynamically floats SELL exit order to maximize profit or anchors strictly at cost floor."""
        if not self.sell_order_id or self.sell_unfilled_qty <= 0:
            open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
            if open_inv > 0 and self.sell_order_id is None:
                self._place_sell_exit_order(target_qty=open_inv)
            return
        ext_ask = self.get_external_best_ask()
        quote = self.fetch_live_quote()
        cur_ask = ext_ask if ext_ask is not None else quote.get("ask")
        self.competitor_ask = cur_ask

        # If user manually set sell price, DO NOT auto-peg to competitor asks!
        if getattr(self, "manual_sell_override", False) and getattr(self, "manual_sell_price", None) is not None:
            self.sell_peg_status = f"MANUAL_PRICE_{self.manual_sell_price}"
            return

        if cur_ask is not None and self.cost_to_cost_price:
            if cur_ask <= self.cost_to_cost_price:
                if self.buy_unfilled_qty == 0:
                    self.state = "WAITING_COST_TO_COST"
                else:
                    self.state = "DUAL_ACTIVE"
                self.sell_peg_status = f"WAITING_COST_FLOOR_{self.cost_to_cost_price}"
                if self.sell_limit_price > self.cost_to_cost_price:
                    if now - self.last_edit_time >= self.edit_debounce_sec:
                        self.last_edit_time = now
                        self.log(f"[TWO-WAY ASK PEG] Market Ask dropped to {cur_ask}. Adjusting SELL limit down to Cost-to-Cost floor: {self.cost_to_cost_price}")
                        self.upload_limit_price(self.cost_to_cost_price, side="sell", is_manual=False)

            elif cur_ask > self.cost_to_cost_price:
                if self.buy_unfilled_qty > 0:
                    self.state = "DUAL_ACTIVE"
                elif self.sell_filled_qty > 0:
                    self.state = "SELL_PARTIAL"
                else:
                    self.state = "SELL_OPEN"
                ideal_sell = align_price(cur_ask - self.tick_size, self.tick_size)
                ideal_sell = max(self.cost_to_cost_price, ideal_sell)

                if abs(ideal_sell - self.sell_limit_price) >= (self.tick_size * 0.99):
                    if now - self.last_edit_time >= self.edit_debounce_sec:
                        self.last_edit_time = now
                        if ideal_sell > self.sell_limit_price:
                            self.sell_peg_status = f"FLOATING_UP_{ideal_sell}"
                            self.log(f"[TWO-WAY ASK PEG] Competitor Ask is higher at {cur_ask}. Floating SELL limit UP from {self.sell_limit_price} to {ideal_sell} to capture extra profit!")
                        else:
                            self.sell_peg_status = f"TIGHTENING_DOWN_{ideal_sell}"
                            self.log(f"[TWO-WAY ASK PEG] Competitor Ask tightened to {cur_ask}. Tightening SELL limit DOWN from {self.sell_limit_price} to {ideal_sell} to hold #1 seller priority.")
                        self.upload_limit_price(ideal_sell, side="sell", is_manual=False)

    def calculate_cost_to_cost_price(self, buy_price: float) -> tuple[float, float, float, float]:
        """
        Calculates the Cost-to-Cost breakeven exit floor.
        Includes:
          1. Round-trip Delta Exchange Maker fee: 0.03% on Buy + 0.03% on Sell = 0.06%
          2. Converted to ticks (rounded up to tick ceiling) so exchange fees are 100% covered.
          3. Plus cost_to_cost_ticks (default 1 tick) as pure profit/cushion buffer.

        Returns:
          (cost_floor_price, roundtrip_fee_usd, fee_ticks_usd, profit_buffer_usd)
        """
        if not buy_price or buy_price <= 0:
            return 0.0, 0.0, 0.0, 0.0
        maker_fee_rate = 0.0003  # 0.03% maker fee per leg on Delta Exchange
        roundtrip_fee = buy_price * (maker_fee_rate * 2)  # 0.06% roundtrip fee
        fee_ticks = math.ceil(roundtrip_fee / self.tick_size) * self.tick_size if self.tick_size > 0 else 0.0
        profit_buffer = (self.tick_size * self.cost_to_cost_ticks)
        floor = align_price(buy_price + fee_ticks + profit_buffer, self.tick_size)
        return floor, roundtrip_fee, fee_ticks, profit_buffer

    def _phase_3_place_sell(self) -> bool:
        """Phase 3: Places Limit SELL Maker order at target price."""
        self.state = "PLACING_SELL"
        if self.buy_filled_qty > 0:
            self.size = self.buy_filled_qty
        self.sell_unfilled_qty = self.size
        self.sell_filled_qty = 0
        ext_ask = self.get_external_best_ask()
        quote = self.fetch_live_quote()
        cur_bid = quote.get("bid")
        cur_ask = ext_ask if ext_ask is not None else quote.get("ask")

        # Baseline: Cost-to-Cost price
        # (entry + Delta Exchange 0.03% buy maker fee + 0.03% sell maker fee + 1 tick buffer)
        self.cost_to_cost_price, rt_fee, fee_ticks, buf = self.calculate_cost_to_cost_price(self.buy_fill_price)

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

        self.log(
            f"Placing LIMIT SELL exit: Size={self.size}, Target Price={target_price} "
            f"(Entry={self.buy_fill_price}, 0.06% Fees={rt_fee:.4f} [Aligned: {fee_ticks:.2f}], "
            f"Buffer=+{buf:.2f}, Cost Floor={self.cost_to_cost_price}, Market Ask={cur_ask})"
        )

        # Check actual live position on Delta Exchange before placing reduce-only exit
        try:
            pos = self.client.get_position(self.product_id)
            actual_pos = abs(int(pos.get("size") or 0)) if pos else 0
        except Exception:
            actual_pos = self.size

        if actual_pos == 0:
            self.log(
                f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position. "
                f"Position or order was already cancelled/closed directly on exchange. Ending MM cycle cleanly."
            )
            self.sell_order_id = None
            self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
            self.stop_requested = True
            self.is_running = False
            return False

        if self.size > actual_pos:
            self.size = actual_pos

        # Check for existing live sell order
        try:
            live_orders = self.client.get_live_orders(query={"product_ids": str(self.product_id)}) or []
            # CRITICAL: Filter by product_id to guarantee only orders for THIS product are adopted
            live_sells = [
                o for o in live_orders 
                if int(o.get("product_id") or 0) == int(self.product_id) 
                and o.get("side") == "sell" 
                and o.get("state") in ("open", "pending")
            ]
            if live_sells:
                primary_sell = live_sells[0]
                self.sell_order_id = primary_sell.get("id")
                self.sell_limit_price = float(primary_sell.get("limit_price") or target_price)
                self.sell_unfilled_qty = int(float(primary_sell.get("unfilled_size") or self.size))
                for extra in live_sells[1:]:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=extra.get("id"))
                    except Exception:
                        pass
                if self.private_ws and self.sell_order_id:
                    self.private_ws.add_order_listener(self.sell_order_id, self._on_sell_order_update)
                self.state = "SELL_OPEN"
                self.sell_peg_status = f"FLOATING_ASK_{self.sell_limit_price}"
                self._persist_state()
                return True
        except Exception:
            pass

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
            err_str = str(e).lower()
            if "no_position_for_reduce_only" in err_str:
                self.log(
                    f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). "
                    f"Position was already closed or cancelled on exchange. Stopping trade cycle cleanly."
                )
                self.sell_order_id = None
                self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                self.stop_requested = True
                self.is_running = False
                return False

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
                err_str2 = str(e2).lower()
                if "no_position_for_reduce_only" in err_str2:
                    self.log(
                        f"[MM-{self.trade_id[-6:]}] Delta Exchange confirms 0 position ('no_position_for_reduce_only'). "
                        f"Position was already closed or cancelled on exchange. Stopping trade cycle cleanly."
                    )
                    self.sell_order_id = None
                    self.state = "COMPLETED_PROFIT" if self.realized_pnl > 0 else "CANCELLED"
                    self.stop_requested = True
                    self.is_running = False
                    return False

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
                active_sell_sz = self.sell_unfilled_qty if self.sell_unfilled_qty > 0 else self.size
                if size > active_sell_sz:
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
                active_buy_sz = self.buy_unfilled_qty if self.buy_unfilled_qty > 0 else (self.total_size - self.buy_filled_qty)
                if size > active_buy_sz:
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
                    oid = int(self.sell_order_id)
                    order = self.client.get_order_by_id(oid)
                    state = order.get("state")
                    unfilled = float(order.get("unfilled_size", 0) or 0)
                    order_sz = float(order.get("size") or self.sell_placed_qty or self.size)
                    this_order_filled = max(0.0, order_sz - unfilled)
                    avg_fill = float(order.get("average_fill_price") or self.sell_limit_price or 0.0)

                    prev_filled = self._order_fills_map.get(oid, 0.0)
                    increment = max(0.0, this_order_filled - prev_filled)

                    if increment > 0:
                        self._order_fills_map[oid] = this_order_filled
                        self.sell_filled_qty += int(increment)
                        self.sell_unfilled_qty = max(0, self.buy_filled_qty - self.sell_filled_qty)
                        if avg_fill > 0:
                            self.sell_fill_price = avg_fill
                            self.weighted_sell_price = avg_fill
                        self.sell_fill_time = datetime.now(timezone.utc).isoformat()

                        chunk_pnl = (self.sell_fill_price - (self.buy_fill_price or 0.0)) * increment * self.contract_value
                        self.realized_pnl += chunk_pnl

                        if (self.sell_filled_qty >= self.buy_filled_qty and self.buy_filled_qty >= self.total_size) or (self.buy_unfilled_qty == 0 and self.sell_filled_qty >= self.buy_filled_qty):
                            actual_pos = 0
                            try:
                                pos = self.client.get_position(self.product_id)
                                actual_pos = abs(int(pos.get("size") or 0)) if pos else 0
                            except Exception:
                                actual_pos = 0

                            if actual_pos > 0:
                                self.buy_filled_qty = max(self.buy_filled_qty, self.sell_filled_qty + actual_pos)
                                self.size = actual_pos
                                self.sell_order_id = None
                                self.sell_placed_qty = 0
                                self.sell_unfilled_qty = actual_pos
                                self.state = "SELL_OPEN"
                                self._place_sell_exit_order(target_qty=actual_pos)
                            else:
                                self.unrealized_pnl = 0.0
                                if self.cost_to_cost_price and self.sell_fill_price > self.cost_to_cost_price:
                                    self.state = "COMPLETED_PROFIT"
                                    self.log(f"PROFIT EXIT! SELL fully filled at {self.sell_fill_price}. Total Realized PnL = +${self.realized_pnl:.5f} USD")
                                else:
                                    self.state = "COMPLETED_COST"
                                    self.log(f"COST-TO-COST EXIT! SELL fully filled at {self.sell_fill_price}. Total Realized PnL = ${self.realized_pnl:.5f} USD (Breakeven)")
                                return
                        else:
                            self.state = "SELL_PARTIAL"
                            self.log(f"PARTIAL SELL EXIT: {self.sell_filled_qty}/{self.buy_filled_qty} lots sold @ {self.sell_fill_price}. Incremental Realized PnL = +${chunk_pnl:.5f} USD. ({self.sell_unfilled_qty} lots remaining to sell)")

                    if unfilled == 0 or state == "closed":
                        open_inv = max(0, self.buy_filled_qty - self.sell_filled_qty)
                        if open_inv > 0 and (oid is None or self.sell_order_id == oid):
                            self.sell_order_id = None
                            self.sell_placed_qty = 0
                            self.sell_unfilled_qty = open_inv
                            self.log(f"Sell order {oid} finished for {int(order_sz)} lots, but {open_inv} open long lots remain! Placing fresh SELL exit order...")
                            self._place_sell_exit_order(target_qty=open_inv)

                    # If order was cancelled on exchange, verify if position is already closed or re-place
                    if state == "cancelled":
                        try:
                            pos = self.client.get_position(self.product_id)
                            open_pos_size = abs(int(pos.get("size") or 0)) if pos else 0
                            if open_pos_size == 0:
                                self.state = "COMPLETED_PROFIT"
                                self.log("Position size is 0 on exchange. MM cycle completed.")
                                return
                            else:
                                self.size = open_pos_size
                                self.sell_unfilled_qty = open_pos_size
                        except Exception:
                            pass
                        self.log(f"Notice: Sell order {self.sell_order_id} was cancelled on exchange. Re-placing exit SELL order for {self.sell_unfilled_qty} lots to maintain protection...")
                        if self._phase_3_place_sell():
                            time.sleep(0.5)
                            continue

                    # Fetch live competitor quotes from orderbook (excludes our own order)
                    ext_ask = self.get_external_best_ask()
                    ext_bid = self.get_external_best_bid()
                    quote = self.fetch_live_quote()
                    cur_bid = ext_bid if ext_bid is not None else quote.get("bid")
                    cur_ask = ext_ask if ext_ask is not None else quote.get("ask")
                    self.competitor_ask = cur_ask
                    self.competitor_bid = cur_bid

                    # If user manually set sell price, DO NOT auto-peg to competitor asks!
                    if getattr(self, "manual_sell_override", False) and getattr(self, "manual_sell_price", None) is not None:
                        self.sell_peg_status = f"MANUAL_PRICE_{self.manual_sell_price}"
                        continue

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
                    err_msg = str(e)
                    self.log(f"Warning in exit monitor / ask peg: {err_msg}")
                    if "order_not_found" in err_msg or "open_order_not_found" in err_msg:
                        try:
                            pos = self.client.get_position(self.product_id)
                            if pos and int(pos.get("size") or 0) == 0:
                                self.state = "COMPLETED_PROFIT"
                                self.log("Position size is 0 on exchange. MM cycle completed.")
                                return
                        except Exception:
                            pass
                        self.log("Exit order not found on exchange. Re-placing SELL order...")
                        if self._phase_3_place_sell():
                            time.sleep(0.5)
                            continue

            if self.stop_requested:
                self.log(f"[STOP MARKET CLOSE] Manual stop requested while holding {self.size} lots. Placing MARKET SELL order...")
                if self.sell_order_id:
                    try:
                        self.client.cancel_order(product_id=self.product_id, order_id=self.sell_order_id)
                    except Exception:
                        pass
                    self.sell_order_id = None
                try:
                    mkt_order = self.client.place_order(
                        product_id=self.product_id,
                        size=self.size,
                        side="sell",
                        order_type=OrderType.MARKET,
                        reduce_only="true"
                    )
                    avg_fill = float(mkt_order.get("average_fill_price") or 0.0)
                    self.sell_filled_qty = self.size
                    self.sell_fill_price = avg_fill if avg_fill > 0 else (self.sell_fill_price or self.buy_fill_price or 0.0)
                    self.sell_fill_time = datetime.now(timezone.utc).isoformat()
                    if self.buy_fill_price and avg_fill > 0:
                        self.realized_pnl += (avg_fill - self.buy_fill_price) * self.size * self.contract_value
                    self.log(f"[STOP MARKET CLOSE] Successfully closed {self.size} lots at MARKET @ {avg_fill}!")
                except Exception as e:
                    self.log(f"[STOP MARKET CLOSE] Error placing market sell order: {e}")
                self.state = "CANCELLED"

        finally:
            if self.private_ws and self.sell_order_id:
                self.private_ws.remove_order_listener(self.sell_order_id, self._on_sell_order_update)


class DeltaMMEngine:
    """
    Central Manager for active Market Making execution cycles with Disk Persistence and Auto-Recovery.
    """
    def __init__(self):
        self.active_cycles: dict[str, MMTradeCycle] = {}
        self.history_cycles: list[dict] = []
        self._lock = threading.Lock()
        # Auto-recover persisted trades on startup
        self.recover_persisted_cycles()

    def on_cycle_state_changed(self, cycle: MMTradeCycle):
        """Called whenever a trade cycle updates its state."""
        with self._lock:
            if cycle.state in ("COMPLETED_PROFIT", "COMPLETED_COST", "CANCELLED", "ERROR", "REJECTED_MAX_OI"):
                if cycle.trade_id in self.active_cycles:
                    self.history_cycles.append(cycle.to_dict())
                    self.active_cycles.pop(cycle.trade_id, None)
            save_all_trades_state(self.active_cycles, self.history_cycles)

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
        norm_sym = symbol.strip().upper()

        # Fast pre-flight check: delta exchange reduce-only mode (max OI breached)
        try:
            temp_client = get_delta_client(account=(account or os.getenv("ACTIVE_ACCOUNT") or "main").lower())
            ticker = temp_client.get_ticker(norm_sym)
            if ticker and ticker.get("oi_reduce_only_mode"):
                raise ValueError(
                    f"Strike '{norm_sym}' cannot be traded. Delta Exchange has placed this contract in REDUCE-ONLY mode "
                    f"because Maximum Open Interest (OI) has been breached. Only position-closing orders are allowed by the exchange. "
                    f"Please select an adjacent strike."
                )
        except ValueError:
            raise
        except Exception as e_chk:
            logger.warning(f"Could not verify ticker OI for {norm_sym}: {e_chk}")

        with self._lock:
            # Prevent duplicate concurrent trade cycles on the exact same strike
            for tid, c in list(self.active_cycles.items()):
                if c.symbol == norm_sym and c.is_running and c.state not in ("COMPLETED_PROFIT", "COMPLETED_COST", "CANCELLED", "ERROR", "REJECTED_MAX_OI"):
                    raise ValueError(f"An active MM trade cycle is already running for {norm_sym} ({c.state}). Cannot open duplicate trade.")

            cycle = MMTradeCycle(
                symbol=norm_sym,
                size=size,
                entry_price=entry_price,
                target_exit_price=target_exit_price,
                cost_to_cost_ticks=cost_to_cost_ticks,
                auto_reprice_buy=auto_reprice_buy,
                two_way_peg=two_way_peg,
                max_entry_price=max_entry_price,
                account=account
            )
            cycle.on_state_change = self.on_cycle_state_changed
            self.active_cycles[cycle.trade_id] = cycle
            save_all_trades_state(self.active_cycles, self.history_cycles)
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
                # Auto-finalize any cycle where all bought lots are sold and no exchange orders remain
                if c.is_running and c.buy_filled_qty > 0 and c.sell_filled_qty >= c.buy_filled_qty and not c.buy_order_id and not c.sell_order_id:
                    c.is_running = False
                    c.stop_requested = True
                    if c.cost_to_cost_price and c.sell_fill_price and c.sell_fill_price > c.cost_to_cost_price:
                        c.state = "COMPLETED_PROFIT"
                    else:
                        c.state = "COMPLETED_COST"
                    c.log(f"[DUAL-SIDE MM] COMPLETE! All {c.buy_filled_qty} executed lots bought and sold. Moving to history.")

                if not c.is_running or c.state in ("COMPLETED_PROFIT", "COMPLETED_COST", "CANCELLED", "ERROR", "REJECTED_MAX_OI"):
                    self.history_cycles.append(c.to_dict())
                    to_remove.append(tid)
                else:
                    active_list.append(c.to_dict())

            for tid in to_remove:
                self.active_cycles.pop(tid, None)

            if to_remove:
                save_all_trades_state(self.active_cycles, self.history_cycles)

            return active_list

    def upload_price(self, trade_id: str, new_price: float, side: str = None) -> dict:
        cycle = self.get_cycle(trade_id)
        if not cycle:
            return {"success": False, "error": f"Trade {trade_id} not found."}
        res = cycle.upload_limit_price(new_price, side=side, is_manual=True)
        save_all_trades_state(self.active_cycles, self.history_cycles)
        return res

    def resume_peg(self, trade_id: str, side: str = None) -> dict:
        cycle = self.get_cycle(trade_id)
        if not cycle:
            return {"success": False, "error": f"Trade {trade_id} not found."}
        res = cycle.resume_auto_peg(side=side)
        save_all_trades_state(self.active_cycles, self.history_cycles)
        return res

    def cancel_trade(self, trade_id: str, market_close: bool = True) -> dict:
        cycle = self.get_cycle(trade_id)
        if not cycle:
            return {"success": False, "error": f"Trade {trade_id} not found."}
        res = cycle.stop(market_close=market_close)
        save_all_trades_state(self.active_cycles, self.history_cycles)
        return res

    def cancel_all_trades(self, market_close: bool = True) -> dict:
        with self._lock:
            cycles = list(self.active_cycles.values())

        if not cycles:
            return {"success": True, "message": "No active MM trades running.", "stopped_count": 0}

        results = []
        for c in cycles:
            try:
                res = c.stop(market_close=market_close)
                results.append({"trade_id": c.trade_id, "symbol": c.symbol, "res": res})
            except Exception as e:
                results.append({"trade_id": c.trade_id, "symbol": c.symbol, "error": str(e)})

        save_all_trades_state(self.active_cycles, self.history_cycles)
        return {
            "success": True,
            "message": f"Successfully stopped {len(cycles)} active trade(s) and closed open positions at market.",
            "stopped_count": len(cycles),
            "results": results
        }

    def recover_persisted_cycles(self):
        """Reads persisted trades from disk and restores uncompleted trade cycles without duplicates."""
        persisted = load_all_trades_state()
        active_trades = persisted.get("active_trades", {})
        self.history_cycles = persisted.get("history", [])

        if not active_trades and not self.history_cycles:
            return

        print(f"\n==================================================", flush=True)
        print(f" [MM-RECOVERY] Found {len(active_trades)} active and {len(self.history_cycles)} history trade(s)", flush=True)
        print(f"==================================================", flush=True)

        # Track active product contracts to guarantee STRICT SINGLE CYCLE PER (PRODUCT_ID, ACCOUNT)
        active_contract_keys: set[tuple[int, str]] = set()

        # 1. Process active trades first (most recent first)
        sorted_active = sorted(
            active_trades.values(),
            key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""),
            reverse=True
        )

        for tdata in sorted_active:
            tid = tdata.get("trade_id")
            state = tdata.get("state")
            pid = tdata.get("product_id")
            acct = (tdata.get("account") or "main").lower()
            ckey = (pid, acct) if pid else None

            if state in ("COMPLETED_PROFIT", "COMPLETED_COST", "CANCELLED", "ERROR", "REJECTED_MAX_OI"):
                self.history_cycles.append(tdata)
                continue

            if ckey and ckey in active_contract_keys:
                print(f"[MM-RECOVERY] Duplicate active cycle detected for {tdata.get('symbol')} ({ckey}). Archiving older duplicate {tid} to history.", flush=True)
                tdata["state"] = "CANCELLED"
                tdata["status_message"] = "Superseded by newer active cycle upon server restart."
                self.history_cycles.append(tdata)
                continue

            try:
                print(f"[MM-RECOVERY] Restoring trade {tid} on {tdata.get('symbol')} (State: {state})...", flush=True)
                cycle = MMTradeCycle.from_persistence_dict(tdata)
                cycle.on_state_change = self.on_cycle_state_changed

                # Reconcile against live Delta Exchange state (positions AND orders)
                needs_resume = cycle.reconcile_with_exchange()

                if needs_resume:
                    if ckey:
                        active_contract_keys.add(ckey)
                    self.active_cycles[cycle.trade_id] = cycle
                    cycle.start_recovered()
                    print(f"[MM-RECOVERY] Successfully resumed MM Trade Cycle {cycle.trade_id} ({cycle.symbol}) in state [{cycle.state}]!", flush=True)
                else:
                    self.history_cycles.append(cycle.to_dict())
                    print(f"[MM-RECOVERY] Trade {cycle.trade_id} already concluded on exchange ({cycle.state}). Saved to history.", flush=True)
            except Exception as rec_err:
                print(f"[MM-RECOVERY] Failed to restore trade {tid}: {rec_err}", flush=True)

        # 2. Inspect history trades: ONLY resurrect if NO active cycle exists for this (pid, acct)
        # AND only resurrect at most 1 history cycle per contract from recent history!
        resurrected_keys: set[tuple[int, str]] = set()
        candidates: dict[tuple[int, str], dict] = {}
        for tdata in list(reversed(self.history_cycles))[:10]:
            pid = tdata.get("product_id")
            acct = (tdata.get("account") or "main").lower()
            ckey = (pid, acct) if pid else None
            if not pid or not ckey:
                continue
            if ckey in active_contract_keys or ckey in candidates:
                continue
            candidates[ckey] = tdata

        for ckey, tdata in candidates.items():
            pid, acct = ckey
            try:
                c = get_delta_client(account=acct)
                p = c.get_position(pid)
                if p and abs(int(p.get("size") or 0)) > 0:
                    resurrected_keys.add(ckey)
                    active_contract_keys.add(ckey)
                    print(f"[MM-RECOVERY] Resurrecting trade {tdata.get('trade_id')} ({tdata.get('symbol')}) from history because Delta holds open position {p.get('size')} lots!", flush=True)
                    if tdata in self.history_cycles:
                        self.history_cycles.remove(tdata)
                    cycle = MMTradeCycle.from_persistence_dict(tdata, client=c)
                    cycle.on_state_change = self.on_cycle_state_changed
                    if cycle.reconcile_with_exchange():
                        self.active_cycles[cycle.trade_id] = cycle
                        cycle.start_recovered()
                        print(f"[MM-RECOVERY] Successfully resurrected {cycle.trade_id} ({cycle.symbol}) in state [{cycle.state}]!", flush=True)
                    else:
                        self.history_cycles.append(cycle.to_dict())
            except Exception as e:
                pass

        # Update disk file with reconciled state
        save_all_trades_state(self.active_cycles, self.history_cycles)


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
