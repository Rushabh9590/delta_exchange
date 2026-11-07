"""
==============================================================================
DELTA EXCHANGE - ARMED TARGET LADDER MANAGER
Handles Option Chain Multi-Target Execution:
1. Submits initial Entry Order (Limit/Market, Post-Only, GTC).
2. Arms Take-Profit Target Ladder (held back until entry order fills).
3. Background daemon monitors Entry Order fill status.
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

BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
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
    Thread-safe Armed Target Manager with Disk Persistence and Background Fill Watcher.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.active_cycles: dict[str, dict] = {}
        self.history_cycles: list[dict] = []
        self._watcher_thread = None
        self._is_running = True
        self.load_persisted_cycles()
        self.start_watcher()

    def load_persisted_cycles(self):
        """Loads active and past armed cycles from disk."""
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.active_cycles = data.get("active", {})
                raw_hist = data.get("history", [])
                self.history_cycles = raw_hist[-50:] if len(raw_hist) > 50 else raw_hist
        except Exception as e:
            logger.warning(f"Failed to load armed targets state: {e}")

    def save_persisted_cycles(self):
        """Saves current armed cycles to disk safely."""
        try:
            temp_file = STATE_FILE.with_suffix(".tmp")
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active": self.active_cycles,
                "history": self.history_cycles[-50:]
            }
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(temp_file, STATE_FILE)
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

        print(f"[ARMED-LADDER] [{acct.upper()}] Submitting Entry {order_type_clean.upper()} {side_clean.upper()} {size} contracts on {symbol} @ {valid_price or 'MARKET'} (Targets armed: {len(targets)})...", flush=True)
        entry_res = client.place_order(
            product_id=product_id,
            size=int(size),
            side=side_clean,
            limit_price=str(valid_price) if valid_price is not None else None,
            order_type=OrderType.LIMIT if is_limit else OrderType.MARKET,
            time_in_force=tif,
            post_only="true" if (is_limit and post_only) else "false",
            reduce_only="false"
        )

        entry_order_id = entry_res.get("id") if isinstance(entry_res, dict) else None
        entry_state = entry_res.get("state", "open") if isinstance(entry_res, dict) else "open"
        print(f"[ARMED-LADDER SUCCESS] [{acct.upper()}] Entry Order #{entry_order_id} placed! Status: {entry_state.upper()} | {side_clean.upper()} {size} {symbol} @ {valid_price or 'MARKET'}", flush=True)

        # Check immediate fill (Delta returns 'closed' with unfilled_size 0 when filled!)
        is_filled_now = (entry_state in ("filled", "closed")) or (isinstance(entry_res, dict) and entry_res.get("unfilled_size") == 0)

        # 3. Check if Multiple Targets are armed
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
            "status": "ENTRY_PLACED" if not valid_targets else ("TARGETS_SUBMITTED" if is_filled_now else "ARMED_WAITING_FILL"),
            "status_message": "Entry order placed." if not valid_targets else ("Entry filled immediately! Submitting ladder..." if is_filled_now else "Entry order placed. Ladder armed, waiting for fill.")
        }

        # If entry filled immediately (e.g. Market order or crossing Limit)
        if valid_targets and is_filled_now:
            try:
                cycle["status"] = "SUBMITTING_TARGETS"
                cycle["status_message"] = "Entry filled immediately! Submitting target ladder..."
                # Run target submission in background thread so HTTP response returns in <300ms
                t = threading.Thread(
                    target=self._submit_target_ladder,
                    args=(cycle, client),
                    daemon=True,
                    name=f"ArmedLadderSubmit-{cycle_id}"
                )
                t.start()
            except Exception as ladder_err:
                logger.error(f"[ARMED LADDER] Immediate target submission error: {ladder_err}")
                cycle["status"] = "TARGETS_ERROR"
                cycle["status_message"] = f"Entry order placed & filled, but target ladder failed: {ladder_err}"

        # Register Sub-50ms WebSocket listener for instant fill trigger
        if valid_targets and not is_filled_now and entry_order_id:
            try:
                from websocket import get_private_ws_manager
                ws_mgr = get_private_ws_manager(account=acct)
                if ws_mgr:
                    def _on_entry_ws(order_data):
                        st = str(order_data.get("state", "")).lower()
                        unf = order_data.get("unfilled_size")
                        tot = order_data.get("size")
                        filled_qty = (float(tot) - float(unf)) if (tot is not None and unf is not None) else 0.0

                        if st in ("filled", "closed") or (unf is not None and float(unf) == 0):
                            with self._lock:
                                if cycle_id in self.active_cycles and self.active_cycles[cycle_id]["status"] == "ARMED_WAITING_FILL":
                                    print(f"[ARMED LADDER WS] Sub-50ms Entry Order #{entry_order_id} fill on {symbol}! Triggering target ladder...", flush=True)
                                    self._submit_target_ladder(cycle, client)
                                    self.active_cycles.pop(cycle_id, None)
                                    self.history_cycles.append(cycle)
                                    if len(self.history_cycles) > 50:
                                        self.history_cycles = self.history_cycles[-50:]
                                    self.save_persisted_cycles()
                        elif st in ("cancelled", "rejected", "expired"):
                            with self._lock:
                                if cycle_id in self.active_cycles and self.active_cycles[cycle_id]["status"] == "ARMED_WAITING_FILL":
                                    if filled_qty > 0:
                                        print(f"[ARMED LADDER WS] Entry Order #{entry_order_id} on {symbol} cancelled with {int(filled_qty)} lots partially filled. Submitting targets for filled lots...", flush=True)
                                        self._submit_target_ladder(cycle, client, max_qty=int(filled_qty))
                                    else:
                                        print(f"[ARMED LADDER WS] Entry Order #{entry_order_id} on {symbol} CANCELLED with 0 fills. Disarming ladder: NO sell orders placed.", flush=True)
                                        cycle["status"] = "ENTRY_CANCELLED"
                                        cycle["status_message"] = f"Entry order was {st} on exchange with 0 fills. Ladder disarmed (no targets placed)."
                                    self.active_cycles.pop(cycle_id, None)
                                    self.history_cycles.append(cycle)
                                    if len(self.history_cycles) > 50:
                                        self.history_cycles = self.history_cycles[-50:]
                                    self.save_persisted_cycles()
                    ws_mgr.add_order_listener(entry_order_id, _on_entry_ws)
            except Exception as ws_err:
                logger.warning(f"Failed to register WS listener for armed order: {ws_err}")

        with self._lock:
            if cycle["has_targets"] and cycle["status"] == "ARMED_WAITING_FILL":
                self.active_cycles[cycle_id] = cycle
            else:
                self.history_cycles.append(cycle)
                if len(self.history_cycles) > 50:
                    self.history_cycles = self.history_cycles[-50:]
            self.save_persisted_cycles()

        return {
            "success": True,
            "cycle_id": cycle_id,
            "entry_order_id": entry_order_id,
            "entry_state": entry_state,
            "has_targets": len(valid_targets) > 0,
            "targets_count": len(valid_targets),
            "cycle": cycle
        }

    def _submit_target_ladder(self, cycle: dict, client=None, max_qty: int = None):
        """Submits target take-profit limit orders with reduce_only=True."""
        acct = cycle.get("account", "main")
        c = client or get_delta_client(account=acct)
        product_id = cycle.get("product_id")
        targets = cycle.get("targets", [])
        symbol = cycle.get("symbol")

        print(f"[ARMED LADDER TRIGGERED] Submitting reduce-only take-profit targets for {symbol} on [{acct.upper()}]...", flush=True)
        submitted_count = 0
        allocated_qty = 0
        for t in targets:
            target_sz = int(t["size"])
            if max_qty is not None:
                if allocated_qty >= max_qty:
                    t["status"] = "CANCELLED_UNFILLED_ENTRY"
                    continue
                if allocated_qty + target_sz > max_qty:
                    target_sz = max_qty - allocated_qty
                allocated_qty += target_sz

            try:
                res = c.place_order(
                    product_id=product_id,
                    size=target_sz,
                    side=t["side"].lower(),
                    limit_price=str(t["price"]),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    post_only="false",
                    reduce_only="true"
                )
                t["order_id"] = res.get("id") if isinstance(res, dict) else None
                t["status"] = "PLACED"
                t["response"] = res
                submitted_count += 1
                print(f"   ↳ [TARGET {t.get('target_index')}/{len(targets)} PLACED] Order #{t.get('order_id')} | {t['side'].upper()} {t['size']} {symbol} @ ${t['price']} (reduce-only)", flush=True)
            except Exception as e:
                t["status"] = "ERROR"
                t["error"] = str(e)
                print(f"   ↳ [TARGET {t.get('target_index')}/{len(targets)} FAILED] {e}", flush=True)
                logger.error(f"[ARMED LADDER] Error placing target {t.get('target_index')} for {symbol}: {e}")

        cycle["status"] = "TARGETS_SUBMITTED"
        cycle["status_message"] = f"Entry filled! {submitted_count}/{len(targets)} reduce-only target orders placed."
        cycle["updated_at"] = datetime.now(timezone.utc).isoformat()
        print(f"[ARMED LADDER] Entry filled for {symbol}! Successfully placed {submitted_count} reduce-only take-profit targets.", flush=True)
        try:
            with self._lock:
                self.save_persisted_cycles()
        except Exception as save_err:
            logger.warning(f"[ARMED LADDER] Failed to save state after target ladder: {save_err}")

    def start_watcher(self):
        """Starts background daemon to poll in-flight entry orders for fills."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return

        def _watcher_loop():
            while self._is_running:
                time.sleep(2.0)
                try:
                    with self._lock:
                        cycles_to_check = list(self.active_cycles.values())

                    for cycle in cycles_to_check:
                        cid = cycle.get("cycle_id")
                        entry_id = cycle.get("entry", {}).get("order_id")
                        pid = cycle.get("product_id")
                        acct = cycle.get("account", "main")
                        if not entry_id or not pid:
                            continue

                        try:
                            client = get_delta_client(account=acct)
                            order_info = client.get_order(product_id=pid, order_id=entry_id)
                            order_state = str(order_info.get("state", "")).lower() if isinstance(order_info, dict) else ""
                            unfilled = float(order_info.get("unfilled_size", -1)) if isinstance(order_info, dict) else -1
                            if not order_state:
                                continue

                            cycle["entry"]["state"] = order_state

                            is_filled = (order_state in ("filled", "closed")) or (unfilled == 0)

                            if is_filled:
                                print(f"[ARMED LADDER WATCHER] Entry Order #{entry_id} for {cycle.get('symbol')} FILLED (State: {order_state})! Triggering multi-target take-profit ladder...", flush=True)
                                self._submit_target_ladder(cycle, client)
                                with self._lock:
                                    if cid in self.active_cycles:
                                        self.active_cycles.pop(cid, None)
                                        self.history_cycles.append(cycle)
                                        if len(self.history_cycles) > 50:
                                            self.history_cycles = self.history_cycles[-50:]
                                    self.save_persisted_cycles()

                            elif order_state in ("cancelled", "rejected", "expired"):
                                total_sz = float(order_info.get("size", 0) or 0)
                                unfilled_sz = float(order_info.get("unfilled_size", 0) or 0)
                                filled_sz = max(0.0, total_sz - unfilled_sz)

                                with self._lock:
                                    if cid in self.active_cycles:
                                        if filled_sz > 0:
                                            print(f"[ARMED LADDER WATCHER] Entry Order #{entry_id} for {cycle.get('symbol')} was cancelled with {int(filled_sz)} lots filled. Submitting targets for filled lots...", flush=True)
                                            self._submit_target_ladder(cycle, client, max_qty=int(filled_sz))
                                        else:
                                            print(f"[ARMED LADDER WATCHER] Entry Order #{entry_id} for {cycle.get('symbol')} was {order_state.upper()} with 0 fills. Ladder disarmed: NO sell orders placed.", flush=True)
                                            cycle["status"] = "ENTRY_CANCELLED"
                                            cycle["status_message"] = f"Entry order was {order_state} with 0 fills. Ladder disarmed."
                                        self.active_cycles.pop(cid, None)
                                        self.history_cycles.append(cycle)
                                        if len(self.history_cycles) > 50:
                                            self.history_cycles = self.history_cycles[-50:]
                                        self.save_persisted_cycles()
                        except Exception as check_err:
                            pass
                except Exception:
                    pass

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
