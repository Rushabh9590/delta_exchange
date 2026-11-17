"""
==============================================================================
DELTA EXCHANGE - ARMED TARGET LADDER MANAGER
Handles Option Chain Multi-Target Execution:
1. Submits initial Entry Order (Limit/Market, Post-Only, GTC).
2. Arms Take-Profit Target Ladder (held back until entry order fills).
3. Background daemon + Sub-50ms Private WebSocket monitors Entry Order fill status.
4. On fill: Automatically fires reduce-only Limit Target Orders to Delta Exchange.
5. Persists armed cycles to disk for server restart resilience.
==============================================================================
"""

import os
import sys
import time
import json
import uuid
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from delta_rest_client import OrderType, TimeInForce
from login import get_delta_client

logger = logging.getLogger("armed_targets")
STATE_FILE = BASE_DIR / ".armed_targets_state.json"


def align_price(price: float, tick_size) -> float:
    """Rounds price to nearest valid tick size."""
    try:
        t = float(tick_size)
        if t <= 0:
            return round(price, 4)
        dec_places = 0
        t_str = f"{t:.8f}".rstrip("0")
        if "." in t_str:
            dec_places = len(t_str.split(".")[1])
        steps = round(price / t)
        val = steps * t
        return round(val, dec_places)
    except Exception:
        return round(price, 4)


class ArmedTargetManager:
    """
    Thread-safe Armed Target Manager with Disk Persistence, Sub-50ms WebSocket trigger,
    and Background Fill Watcher.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self.active_cycles: dict[str, dict] = {}
        self.history_cycles: list[dict] = []
        self._watcher_thread = None
        self._is_running = True
        self.load_persisted_cycles()
        self.start_watcher()

    def load_persisted_cycles(self):
        """Loads active and past armed cycles from disk, re-attaches WS listeners, and verifies startup state."""
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                loaded_active = data.get("active", {})
                raw_hist = data.get("history", [])
                self.history_cycles = raw_hist[-50:] if len(raw_hist) > 50 else raw_hist

            # Filter out any non-waiting cycles from active
            self.active_cycles = {}
            for cid, c in loaded_active.items():
                st = c.get("status")
                if st in ("ARMED_WAITING_FILL", "SUBMITTING_TARGETS"):
                    self.active_cycles[cid] = c
                else:
                    self.history_cycles.append(c)

            print(f"[ARMED LADDER] Loaded {len(self.active_cycles)} active armed cycles from disk.", flush=True)

            # Re-attach WebSocket listeners and start startup verification
            for cid, c in list(self.active_cycles.items()):
                self._attach_ws_listener(c)
                # Background verification in case entry filled while server was offline
                t = threading.Thread(
                    target=self._verify_cycle_on_startup,
                    args=(c,),
                    daemon=True,
                    name=f"ArmedStartupVerify-{cid}"
                )
                t.start()

        except Exception as e:
            logger.warning(f"Failed to load armed targets state: {e}")

    def save_persisted_cycles(self):
        """Saves current armed cycles to disk safely with Windows retry logic."""
        try:
            temp_file = STATE_FILE.with_suffix(f".tmp_{os.getpid()}_{threading.get_ident()}")
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active": self.active_cycles,
                "history": self.history_cycles[-50:]
            }
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)

            # Retry loop for Windows file locking
            for attempt in range(5):
                try:
                    os.replace(temp_file, STATE_FILE)
                    break
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            else:
                try:
                    os.replace(temp_file, STATE_FILE)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to persist armed targets state: {e}")

    def create_order_with_ladder(
        self,
        symbol: str,
        side: str,
        size: int,
        order_type: str = "limit",
        limit_price: float = None,
        time_in_force: str = "gtc",
        post_only: bool = True,
        account: str = None,
        targets: list = None
    ) -> dict:
        """
        Places initial entry order on Delta Exchange.
        If targets are provided, arms them to be executed when the entry fills.
        """
        acct = (account or os.getenv("ACTIVE_ACCOUNT") or "main").lower()
        client = get_delta_client(account=acct)

        # 1. Resolve product
        products = client.get_products()
        prod = next((p for p in products if p.get("symbol", "").upper() == symbol.upper()), None)
        if not prod:
            raise ValueError(f"Product '{symbol}' not found on Delta Exchange.")

        product_id = prod["id"]
        tick_size = float(prod.get("tick_size") or 0.1)
        contract_val = float(prod.get("contract_value") or 0.001)

        # 2. Place entry order
        side_clean = side.strip().lower()
        order_type_clean = order_type.strip().lower()
        is_limit = order_type_clean == "limit"

        tif = TimeInForce.GTC
        if time_in_force.lower() == "ioc":
            tif = TimeInForce.IOC
        elif time_in_force.lower() == "fok":
            tif = TimeInForce.FOK

        valid_price = None
        if is_limit:
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("Limit price must be greater than 0.")
            valid_price = align_price(float(limit_price), tick_size)

        target_count = len(targets) if (targets and isinstance(targets, list)) else 0
        print(f"[ARMED-LADDER] [{acct.upper()}] Submitting Entry {order_type_clean.upper()} {side_clean.upper()} {size} contracts on {symbol} @ {valid_price or 'MARKET'} (Targets armed: {target_count})...", flush=True)

        entry_res = client.place_order(
            product_id=product_id,
            size=int(size),
            side=side_clean,
            limit_price=str(valid_price) if valid_price is not None else None,
            order_type=OrderType.LIMIT if is_limit else OrderType.MARKET,
            time_in_force=tif,
            post_only="true" if (is_limit and post_only) else "false",
            reduce_only=False
        )

        entry_order_id = entry_res.get("id") if isinstance(entry_res, dict) else None
        entry_state = entry_res.get("state", "open") if isinstance(entry_res, dict) else "open"
        print(f"[ARMED-LADDER SUCCESS] [{acct.upper()}] Entry Order #{entry_order_id} placed! Status: {entry_state.upper()} | {side_clean.upper()} {size} {symbol} @ {valid_price or 'MARKET'}", flush=True)

        # Check immediate fill (Delta returns 'closed' with unfilled_size 0 when filled immediately!)
        is_filled_now = (entry_state in ("filled", "closed")) or (isinstance(entry_res, dict) and entry_res.get("unfilled_size") == 0)

        # 3. Process Multiple Targets
        valid_targets = []
        if targets and isinstance(targets, list):
            for t in targets:
                try:
                    t_qty = int(t.get("size") or t.get("qty") or 0)
                    t_price = float(t.get("price") or 0.0)
                    t_side = str(t.get("side") or ("sell" if side_clean == "buy" else "buy")).lower()
                    if t_qty > 0 and t_price > 0:
                        valid_targets.append({
                            "target_index": len(valid_targets) + 1,
                            "side": t_side,
                            "size": t_qty,
                            "price": align_price(t_price, tick_size),
                            "order_id": None,
                            "status": "ARMED"
                        })
                except (ValueError, TypeError):
                    continue

        cycle_id = f"armed_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        initial_status = "ENTRY_PLACED"
        if valid_targets:
            initial_status = "SUBMITTING_TARGETS" if is_filled_now else "ARMED_WAITING_FILL"

        cycle = {
            "cycle_id": cycle_id,
            "created_at": now_iso,
            "updated_at": now_iso,
            "symbol": symbol.upper(),
            "product_id": product_id,
            "account": acct,
            "tick_size": tick_size,
            "contract_value": contract_val,
            "entry": {
                "order_id": entry_order_id,
                "side": side_clean.upper(),
                "size": int(size),
                "order_type": order_type_clean.upper(),
                "limit_price": valid_price,
                "state": entry_state,
                "response": entry_res
            },
            "has_targets": len(valid_targets) > 0,
            "targets": valid_targets,
            "status": initial_status,
            "status_message": "Entry order placed." if not valid_targets else ("Entry filled immediately! Submitting ladder..." if is_filled_now else "Entry order placed. Ladder armed, waiting for fill.")
        }

        # CRITICAL FIX: Persist cycle in active_cycles FIRST before attaching listeners or spawning threads
        with self._lock:
            if cycle["has_targets"] and cycle["status"] in ("ARMED_WAITING_FILL", "SUBMITTING_TARGETS"):
                self.active_cycles[cycle_id] = cycle
            else:
                self.history_cycles.append(cycle)
                if len(self.history_cycles) > 50:
                    self.history_cycles = self.history_cycles[-50:]
            self.save_persisted_cycles()

        # If filled immediately, execute ladder in background
        if valid_targets and is_filled_now:
            t = threading.Thread(
                target=self._execute_ladder_and_finalize,
                args=(cycle,),
                daemon=True,
                name=f"ArmedLadderImmediate-{cycle_id}"
            )
            t.start()
        elif valid_targets and not is_filled_now and entry_order_id:
            # Register Sub-50ms WebSocket listener for instant fill trigger
            self._attach_ws_listener(cycle)

        return {
            "success": True,
            "cycle_id": cycle_id,
            "entry_order_id": entry_order_id,
            "entry_state": entry_state,
            "has_targets": len(valid_targets) > 0,
            "targets_count": len(valid_targets),
            "cycle": cycle
        }

    def _attach_ws_listener(self, cycle: dict):
        """Attaches private WebSocket order listener for this armed cycle's entry order."""
        entry_id = cycle.get("entry", {}).get("order_id")
        acct = cycle.get("account", "main")
        cid = cycle.get("cycle_id")
        symbol = cycle.get("symbol")
        if not entry_id:
            return

        try:
            from websocket import get_private_ws_manager
            ws_mgr = get_private_ws_manager(account=acct)
            if not ws_mgr:
                return

            def _on_entry_ws(order_data):
                st = str(order_data.get("state", "")).lower()
                unf = order_data.get("unfilled_size")
                tot = order_data.get("size")
                filled_qty = (float(tot) - float(unf)) if (tot is not None and unf is not None) else 0.0

                if st in ("filled", "closed") or (unf is not None and float(unf) == 0):
                    print(f"[ARMED LADDER WS] Sub-50ms Entry Order #{entry_id} fill detected on {symbol}! Triggering target ladder...", flush=True)
                    self._handle_fill(cid)
                elif st in ("cancelled", "rejected", "expired"):
                    print(f"[ARMED LADDER WS] Entry Order #{entry_id} on {symbol} {st.upper()}! Handling cancellation/partial...", flush=True)
                    self._handle_cancellation(cid, filled_qty=int(filled_qty), reason=st)

            ws_mgr.add_order_listener(entry_id, _on_entry_ws)
        except Exception as ws_err:
            logger.warning(f"Failed to attach WS listener for cycle {cid}: {ws_err}")

    def _handle_fill(self, cycle_id: str):
        """Atomically transitions cycle to SUBMITTING_TARGETS and spawns target submission."""
        cycle = None
        with self._lock:
            cycle = self.active_cycles.get(cycle_id)
            if not cycle:
                return
            current_status = cycle.get("status")
            if current_status != "ARMED_WAITING_FILL":
                # Already triggered or finalized by another event
                return
            cycle["status"] = "SUBMITTING_TARGETS"
            cycle["status_message"] = "Entry filled! Submitting target ladder..."
            cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.save_persisted_cycles()

        # Execute ladder outside lock so network calls do not block other threads
        t = threading.Thread(
            target=self._execute_ladder_and_finalize,
            args=(cycle,),
            daemon=True,
            name=f"ArmedLadderExecute-{cycle_id}"
        )
        t.start()

    def _handle_cancellation(self, cycle_id: str, filled_qty: int = 0, reason: str = "cancelled"):
        """Handles cancelled or rejected entry orders with optional partial fill ladder."""
        cycle = None
        with self._lock:
            cycle = self.active_cycles.get(cycle_id)
            if not cycle or cycle.get("status") != "ARMED_WAITING_FILL":
                return

            if filled_qty > 0:
                print(f"[ARMED LADDER] Entry order for {cycle.get('symbol')} was {reason} with {filled_qty} partial fills. Submitting partial targets...", flush=True)
                cycle["status"] = "SUBMITTING_TARGETS"
                cycle["status_message"] = f"Entry order was {reason} with {filled_qty} partial fills. Submitting partial targets..."
                cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.save_persisted_cycles()
                t = threading.Thread(
                    target=self._execute_ladder_and_finalize,
                    args=(cycle, filled_qty),
                    daemon=True,
                    name=f"ArmedLadderPartial-{cycle_id}"
                )
                t.start()
            else:
                print(f"[ARMED LADDER] Entry order for {cycle.get('symbol')} was {reason} with 0 fills. Ladder disarmed (no targets placed).", flush=True)
                cycle["status"] = "ENTRY_CANCELLED"
                cycle["status_message"] = f"Entry order was {reason} on exchange with 0 fills. Ladder disarmed."
                cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.active_cycles.pop(cycle_id, None)
                self.history_cycles.append(cycle)
                if len(self.history_cycles) > 50:
                    self.history_cycles = self.history_cycles[-50:]
                self.save_persisted_cycles()

    def _verify_cycle_on_startup(self, cycle: dict):
        """Verifies entry order status on server startup to catch any fills that occurred while offline."""
        cid = cycle.get("cycle_id")
        entry_id = cycle.get("entry", {}).get("order_id")
        acct = cycle.get("account", "main")
        if not entry_id:
            return

        try:
            client = get_delta_client(account=acct)
            order_info = client.get_order_by_id(entry_id)
            if isinstance(order_info, dict):
                st = str(order_info.get("state", "")).lower()
                unf = float(order_info.get("unfilled_size", -1))
                tot = float(order_info.get("size", 0) or 0)
                filled_qty = max(0, int(tot - unf)) if (tot > 0 and unf >= 0) else 0

                if st in ("filled", "closed") or (unf == 0):
                    print(f"[ARMED LADDER RECOVERY] Entry Order #{entry_id} for {cycle.get('symbol')} was FILLED while server was offline! Submitting target ladder now...", flush=True)
                    self._handle_fill(cid)
                elif st in ("cancelled", "rejected", "expired"):
                    print(f"[ARMED LADDER RECOVERY] Entry Order #{entry_id} for {cycle.get('symbol')} was {st.upper()} while server was offline.", flush=True)
                    self._handle_cancellation(cid, filled_qty=filled_qty, reason=st)
        except Exception as e:
            logger.debug(f"[ARMED STARTUP RECOVERY] Verification notice for cycle {cid}: {e}")

    def _execute_ladder_and_finalize(self, cycle: dict, max_qty: int = None):
        """Places all armed target orders and finalizes the cycle into history."""
        cid = cycle.get("cycle_id")
        acct = cycle.get("account", "main")
        client = get_delta_client(account=acct)

        try:
            self._submit_target_ladder(cycle, client=client, max_qty=max_qty)
        except Exception as err:
            logger.error(f"[ARMED LADDER] Error in _execute_ladder_and_finalize for cycle {cid}: {err}")
            cycle["status"] = "TARGETS_ERROR"
            cycle["status_message"] = f"Failed to submit target ladder: {err}"
        finally:
            with self._lock:
                self.active_cycles.pop(cid, None)
                self.history_cycles.append(cycle)
                if len(self.history_cycles) > 50:
                    self.history_cycles = self.history_cycles[-50:]
                self.save_persisted_cycles()

    def _submit_target_ladder(self, cycle: dict, client=None, max_qty: int = None):
        """Submits target take-profit limit orders concurrently with reduce_only=True."""
        acct = cycle.get("account", "main")
        c = client or get_delta_client(account=acct)
        product_id = cycle.get("product_id")
        targets = cycle.get("targets", [])
        symbol = cycle.get("symbol")

        print(f"[ARMED LADDER TRIGGERED] Submitting reduce-only take-profit targets for {symbol} on [{acct.upper()}]...", flush=True)

        allocated_qty = 0
        items_to_submit = []

        for t in targets:
            target_sz = int(t.get("size") or 0)
            if max_qty is not None:
                if allocated_qty >= max_qty:
                    t["status"] = "CANCELLED_UNFILLED_ENTRY"
                    continue
                if allocated_qty + target_sz > max_qty:
                    target_sz = max_qty - allocated_qty
                allocated_qty += target_sz
            items_to_submit.append((t, target_sz))

        if not items_to_submit:
            cycle["status"] = "TARGETS_SUBMITTED"
            cycle["status_message"] = "No targets were eligible for submission."
            cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
            return 0

        def _place_single_target(item):
            t, target_sz = item
            try:
                res = c.place_order(
                    product_id=product_id,
                    size=target_sz,
                    side=t["side"].lower(),
                    limit_price=str(t["price"]),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    post_only="false",
                    reduce_only=True
                )
                t["order_id"] = res.get("id") if isinstance(res, dict) else None
                t["status"] = "PLACED"
                t["response"] = res
                print(f"   ↳ [TARGET {t.get('target_index')}/{len(targets)} PLACED] Order #{t.get('order_id')} | {t['side'].upper()} {target_sz} {symbol} @ ${t['price']} (reduce-only)", flush=True)
                return True
            except Exception as e:
                t["status"] = "ERROR"
                t["error"] = str(e)
                print(f"   ↳ [TARGET {t.get('target_index')}/{len(targets)} FAILED] {e}", flush=True)
                logger.error(f"[ARMED LADDER] Error placing target {t.get('target_index')} for {symbol}: {e}")
                return False

        # Concurrently submit all targets for sub-300ms execution across all legs
        max_workers = min(10, len(items_to_submit))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_place_single_target, items_to_submit))

        submitted_count = sum(1 for r in results if r)
        cycle["status"] = "TARGETS_SUBMITTED"
        cycle["status_message"] = f"Entry filled! {submitted_count}/{len(targets)} reduce-only target orders placed."
        cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
        print(f"[ARMED LADDER SUCCESS] Entry filled for {symbol}! Successfully placed {submitted_count}/{len(targets)} reduce-only take-profit targets.", flush=True)
        return submitted_count

    def start_watcher(self):
        """Starts background daemon to poll in-flight entry orders for fills."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return

        def _watcher_loop():
            while self._is_running:
                time.sleep(1.5)
                try:
                    with self._lock:
                        cycles_to_check = [c for c in self.active_cycles.values() if c.get("status") == "ARMED_WAITING_FILL"]

                    for cycle in cycles_to_check:
                        cid = cycle.get("cycle_id")
                        entry_id = cycle.get("entry", {}).get("order_id")
                        pid = cycle.get("product_id")
                        acct = cycle.get("account", "main")
                        if not entry_id or not pid:
                            continue

                        try:
                            client = get_delta_client(account=acct)
                            order_info = client.get_order_by_id(entry_id)
                            if not isinstance(order_info, dict):
                                continue

                            order_state = str(order_info.get("state", "")).lower()
                            unfilled = float(order_info.get("unfilled_size", -1))
                            if not order_state:
                                continue

                            cycle["entry"]["state"] = order_state
                            is_filled = (order_state in ("filled", "closed")) or (unfilled == 0)

                            if is_filled:
                                print(f"[ARMED LADDER WATCHER] Entry Order #{entry_id} for {cycle.get('symbol')} FILLED (State: {order_state})! Triggering multi-target take-profit ladder...", flush=True)
                                self._handle_fill(cid)

                            elif order_state in ("cancelled", "rejected", "expired"):
                                total_sz = float(order_info.get("size", 0) or 0)
                                unfilled_sz = float(order_info.get("unfilled_size", 0) or 0)
                                filled_sz = max(0.0, total_sz - unfilled_sz)
                                self._handle_cancellation(cid, filled_qty=int(filled_sz), reason=order_state)

                        except Exception as check_err:
                            logger.debug(f"[ARMED WATCHER] Polling check error for entry #{entry_id}: {check_err}")
                except Exception as loop_err:
                    logger.debug(f"[ARMED WATCHER] Loop error: {loop_err}")

        self._watcher_thread = threading.Thread(target=_watcher_loop, name="Armed-Targets-Watcher", daemon=True)
        self._watcher_thread.start()

    def list_active(self) -> list:
        with self._lock:
            return list(self.active_cycles.values())

    def list_history(self) -> list:
        with self._lock:
            return list(self.history_cycles)

    def cancel_armed_cycle(self, cycle_id: str) -> dict:
        with self._lock:
            cycle = self.active_cycles.get(cycle_id)
            if not cycle:
                return {"success": False, "error": f"Armed cycle {cycle_id} not found."}

            acct = cycle.get("account", "main")
            pid = cycle.get("product_id")
            entry_id = cycle.get("entry", {}).get("order_id")

            cancelled_entry = False
            if entry_id and pid:
                try:
                    client = get_delta_client(account=acct)
                    client.cancel_order(product_id=pid, order_id=entry_id)
                    cancelled_entry = True
                except Exception:
                    pass

            cycle["status"] = "MANUALLY_CANCELLED"
            cycle["status_message"] = "Armed cycle cancelled by user."
            cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.active_cycles.pop(cycle_id, None)
            self.history_cycles.append(cycle)
            if len(self.history_cycles) > 50:
                self.history_cycles = self.history_cycles[-50:]
            self.save_persisted_cycles()

        return {"success": True, "cancelled_entry": cancelled_entry, "cycle": cycle}


# Global singleton instance
armed_target_manager = ArmedTargetManager()
