/**
 * ==============================================================================
 * DELTA EXCHANGE - OPTION BUYING & ARMED TARGET LADDER MODAL CONTROLLER
 * Features:
 *  - Real-time Bid/Mid/Ask live streaming quote bar
 *  - Entry Order configuration (Side, Size, Limit/Market, Quick Bid/Mid/Ask)
 *  - Multi-Target Ladder Generator (Compounding 1->2->4->8, Linear, Equal)
 *  - Interactive inline-editable targets with live Gain% and Proceeds calculation
 *  - Submission with Armed Targets held back until Entry order fills
 * ==============================================================================
 */

let oomCurrentContract = null;
let oomTargetsData = [];
let oomWsTickUnsub = null;

function formatExpiryPretty(expStr) {
  if (!expStr) return '';
  const s = String(expStr).trim();
  if (s.length === 6) {
    const day = s.slice(0, 2);
    const moNum = parseInt(s.slice(2, 4), 10);
    const yr = '20' + s.slice(4, 6);
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const mo = months[moNum - 1] || s.slice(2, 4);
    return `${day} ${mo} ${yr.slice(2)}`;
  }
  return s;
}

function alignPriceTick(price, tickSize) {
  const p = parseFloat(price);
  const t = parseFloat(tickSize) || 0.1;
  if (isNaN(p) || p <= 0) return 0;
  const steps = Math.round(p / t);
  const val = steps * t;
  let dec = 0;
  const tStr = t.toString();
  if (tStr.includes('.')) dec = tStr.split('.')[1].length;
  return parseFloat(val.toFixed(dec));
}

/**
 * Opens the Option Order Modal with the selected contract specifications
 */
function openOptionOrderModal(contract) {
  try {
    if (!contract || !contract.symbol) {
      console.warn('[openOptionOrderModal] Contract data missing or invalid symbol:', contract);
      if (typeof showToast === 'function') showToast('Contract data missing', 'error');
      return;
    }

    oomCurrentContract = {
      symbol: contract.symbol.toUpperCase(),
      underlying: contract.underlying || (contract.symbol.includes('BTC') ? 'BTC' : 'ETH'),
      strike: parseFloat(contract.strike || 0),
      expiry: contract.expiry || '',
      optionType: (contract.optionType || (contract.symbol.startsWith('C-') ? 'call' : 'put')).toLowerCase(),
      tickSize: parseFloat(contract.tickSize || (contract.symbol.includes('BTC') ? 0.1 : 0.01)),
      contractValue: parseFloat(contract.contractValue || (contract.symbol.includes('BTC') ? 0.001 : 0.01)),
      bid: parseFloat(contract.bid || contract.best_bid || 0),
      ask: parseFloat(contract.ask || contract.best_ask || 0),
      bidQty: contract.bidQty || contract.best_bid_size || 1850,
      askQty: contract.askQty || contract.best_ask_size || 5365,
      side: (contract.side || 'buy').toLowerCase()
    };

    if (oomCurrentContract.bid > 0 && oomCurrentContract.ask > 0) {
      oomCurrentContract.mid = alignPriceTick((oomCurrentContract.bid + oomCurrentContract.ask) / 2, oomCurrentContract.tickSize);
    } else {
      oomCurrentContract.mid = oomCurrentContract.ask || oomCurrentContract.bid || 0;
    }

    // 1. Populate Header
    updateModalTitleAndBadge();

    // 2. Populate Market Banner
    updateMarketBannerQuotes();

    // 3. Set Entry Form Defaults
    setEntrySide(oomCurrentContract.side, false);
    const inpQty = document.getElementById('oomInpQty');
    if (inpQty && (!inpQty.value || inpQty.value === '1')) {
      inpQty.value = 10;
    }

    const selOrderType = document.getElementById('oomOrderType');
    if (selOrderType) selOrderType.value = 'limit';

    const inpPrice = document.getElementById('oomInpPrice');
    if (inpPrice) {
      const defaultPrice = oomCurrentContract.side === 'buy'
        ? (oomCurrentContract.ask > 0 ? oomCurrentContract.ask : oomCurrentContract.mid)
        : (oomCurrentContract.bid > 0 ? oomCurrentContract.bid : oomCurrentContract.mid);
      inpPrice.value = defaultPrice > 0 ? defaultPrice : '';
      inpPrice.step = oomCurrentContract.tickSize;
    }

    const selTif = document.getElementById('oomTimeInForce');
    if (selTif) selTif.value = 'gtc';

    const chkPostOnly = document.getElementById('oomChkPostOnly');
    if (chkPostOnly) chkPostOnly.checked = false;

    const chkTargets = document.getElementById('oomChkMultipleTargets');
    if (chkTargets) chkTargets.checked = true;

    // 4. Generate Target Ladder
    toggleMultipleTargets(false);
    recalculateLadder();

    // 5. Open Modal Overlay
    const overlay = document.getElementById('optionOrderModal');
    if (overlay) {
      overlay.classList.add('active');
      overlay.style.display = 'flex';
    } else {
      console.error('[openOptionOrderModal] Could not find #optionOrderModal in DOM');
    }

    // 6. Listen to live WebSocket ticks for this symbol
    subscribeModalTicks(oomCurrentContract.symbol);
  } catch (err) {
    console.error('[openOptionOrderModal] Exception caught:', err);
    const overlay = document.getElementById('optionOrderModal');
    if (overlay) {
      overlay.classList.add('active');
      overlay.style.display = 'flex';
    }
  }
}

function closeOptionOrderModal() {
  const overlay = document.getElementById('optionOrderModal');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.style.display = 'none';
  }
  if (oomWsTickUnsub) {
    oomWsTickUnsub();
    oomWsTickUnsub = null;
  }
}

function updateModalTitleAndBadge() {
  if (!oomCurrentContract) return;
  const isBuy = oomCurrentContract.side === 'buy';
  const titleEl = document.getElementById('oomModalTitle');
  if (titleEl) {
    titleEl.textContent = `${isBuy ? 'Buy' : 'Sell'} ${oomCurrentContract.symbol}`;
  }

  const badgeEl = document.getElementById('oomContractBadge');
  if (badgeEl) {
    const isPut = oomCurrentContract.optionType === 'put';
    badgeEl.className = `oom-contract-badge ${isPut ? 'put' : 'call'}`;
    const strikeStr = oomCurrentContract.strike ? Number(oomCurrentContract.strike).toLocaleString() : '';
    const expPretty = formatExpiryPretty(oomCurrentContract.expiry);
    badgeEl.textContent = `${isPut ? 'PUT' : 'CALL'} · ${strikeStr} · ${expPretty}`;
  }
}

function updateMarketBannerQuotes() {
  if (!oomCurrentContract) return;
  const bidVal = document.getElementById('oomKpiBid');
  const bidSub = document.getElementById('oomKpiBidSub');
  const midVal = document.getElementById('oomKpiMid');
  const askVal = document.getElementById('oomKpiAsk');
  const askSub = document.getElementById('oomKpiAskSub');
  const tickVal = document.getElementById('oomKpiTick');
  const tickSub = document.getElementById('oomKpiTickSub');

  if (bidVal) bidVal.textContent = oomCurrentContract.bid > 0 ? oomCurrentContract.bid.toFixed(1) : '--';
  if (bidSub) bidSub.textContent = `${Number(oomCurrentContract.bidQty).toLocaleString()} qty`;
  if (midVal) midVal.textContent = oomCurrentContract.mid > 0 ? oomCurrentContract.mid.toFixed(1) : '--';
  if (askVal) askVal.textContent = oomCurrentContract.ask > 0 ? oomCurrentContract.ask.toFixed(1) : '--';
  if (askSub) askSub.textContent = `${Number(oomCurrentContract.askQty).toLocaleString()} qty`;
  if (tickVal) tickVal.textContent = oomCurrentContract.tickSize.toFixed(1);
  if (tickSub) tickSub.textContent = `lot ${oomCurrentContract.contractValue}`;
}

function setEntrySide(side, triggerRecalc = true) {
  if (!oomCurrentContract) return;
  oomCurrentContract.side = side.toLowerCase();
  const btnBuy = document.getElementById('oomBtnSideBuy');
  const btnSell = document.getElementById('oomBtnSideSell');
  if (btnBuy && btnSell) {
    if (oomCurrentContract.side === 'buy') {
      btnBuy.classList.add('active', 'buy');
      btnSell.classList.remove('active', 'sell');
    } else {
      btnSell.classList.add('active', 'sell');
      btnBuy.classList.remove('active', 'buy');
    }
  }

  updateModalTitleAndBadge();
  if (triggerRecalc) {
    recalculateLadder();
  }
}

function onOrderTypeChange() {
  const sel = document.getElementById('oomOrderType');
  const isMarket = sel && sel.value === 'market';
  const inpPrice = document.getElementById('oomInpPrice');
  const chkPostOnly = document.getElementById('oomChkPostOnly');
  const quickPrices = document.querySelector('.oom-quick-prices');

  if (inpPrice) inpPrice.disabled = isMarket;
  if (chkPostOnly) chkPostOnly.disabled = isMarket;
  if (quickPrices) quickPrices.style.opacity = isMarket ? '0.4' : '1';
  if (isMarket && inpPrice && oomCurrentContract) {
    inpPrice.value = oomCurrentContract.side === 'buy' ? oomCurrentContract.ask : oomCurrentContract.bid;
  }
  recalculateLadder();
}

function fillQuickPrice(type) {
  if (!oomCurrentContract) return;
  const inpPrice = document.getElementById('oomInpPrice');
  if (!inpPrice) return;
  let val = 0;
  if (type === 'bid') val = oomCurrentContract.bid;
  else if (type === 'mid') val = oomCurrentContract.mid;
  else if (type === 'ask') val = oomCurrentContract.ask;

  if (val > 0) {
    inpPrice.value = val;
    recalculateLadder();
  }
}

function toggleMultipleTargets(triggerRecalc = true) {
  const chk = document.getElementById('oomChkMultipleTargets');
  const panel = document.getElementById('oomLadderPanel');
  const isChecked = chk && chk.checked;
  if (panel) {
    panel.style.display = isChecked ? 'flex' : 'none';
  }
  updateLegsCounter();
  if (triggerRecalc && isChecked) {
    recalculateLadder();
  }
}

function updateLegsCounter() {
  const counter = document.getElementById('oomLegsCounter');
  const chk = document.getElementById('oomChkMultipleTargets');
  if (!counter) return;
  if (chk && chk.checked) {
    const targetCount = oomTargetsData.length || 0;
    counter.textContent = `Legs ${targetCount + 1}`;
  } else {
    counter.textContent = 'Legs 1';
  }
}

/**
 * Generates and recalculates the take-profit ladder
 */
function recalculateLadder() {
  if (!oomCurrentContract) return;
  const chk = document.getElementById('oomChkMultipleTargets');
  if (!chk || !chk.checked) {
    updateLegsCounter();
    return;
  }

  const inpEntryPrice = document.getElementById('oomInpPrice');
  const inpTotalQty = document.getElementById('oomInpQty');
  const inpNumTargets = document.getElementById('oomNumTargets');
  const inpGap = document.getElementById('oomGapPct');
  const selLadder = document.getElementById('oomLadderType');

  const entryPrice = parseFloat(inpEntryPrice?.value) || oomCurrentContract.mid || 9.0;
  const totalQty = parseInt(inpTotalQty?.value, 10) || 10;
  const numTargets = Math.max(1, Math.min(20, parseInt(inpNumTargets?.value, 10) || 10));
  const gapPct = parseFloat(inpGap?.value) || 100.0;
  const ladderType = selLadder ? selLadder.value : 'compounding';

  const isBuy = oomCurrentContract.side === 'buy';
  const targetSide = isBuy ? 'SELL' : 'BUY';
  const tickSize = oomCurrentContract.tickSize || 0.1;
  const contractVal = oomCurrentContract.contractValue || 0.001;

  // 1. Distribute Quantities
  const targetQtys = [];
  const baseQty = Math.floor(totalQty / numTargets);
  let remainder = totalQty % numTargets;

  for (let i = 0; i < numTargets; i++) {
    let q = baseQty;
    if (remainder > 0) {
      q += 1;
      remainder--;
    }
    targetQtys.push(Math.max(1, q));
  }

  // 2. Generate Target Prices
  oomTargetsData = [];
  for (let i = 1; i <= numTargets; i++) {
    let price = 0;
    if (ladderType === 'compounding') {
      // Compounding: P_i = P_0 * (1 + gap/100)^i (matches exact screenshot math: 9 -> 18 -> 36 -> 72 -> 576 -> 9216)
      if (isBuy) {
        price = entryPrice * Math.pow(1 + gapPct / 100, i);
      } else {
        price = entryPrice * Math.pow(Math.max(0.01, 1 - gapPct / 100), i);
      }
    } else {
      // Linear: P_i = P_0 * (1 + i * gap/100)
      if (isBuy) {
        price = entryPrice * (1 + (i * gapPct) / 100);
      } else {
        price = Math.max(tickSize, entryPrice * (1 - (i * gapPct) / 100));
      }
    }

    price = alignPriceTick(price, tickSize);
    const qty = targetQtys[i - 1] || 1;
    const gainPct = entryPrice > 0 ? ((price - entryPrice) / entryPrice) * 100 * (isBuy ? 1 : -1) : 0;
    const proceeds = price * qty * contractVal;

    oomTargetsData.push({
      index: i,
      label: `T${i}`,
      side: targetSide,
      qty: qty,
      price: price,
      gainPct: gainPct,
      proceeds: proceeds
    });
  }

  renderLadderTable();
  updateQtySplitValidation();
  updateLegsCounter();
}

function renderLadderTable() {
  const tbody = document.getElementById('oomLadderTbody');
  if (!tbody) return;

  const isBuy = oomCurrentContract ? oomCurrentContract.side === 'buy' : true;
  const sideColor = isBuy ? '#ef4444' : '#10b981';

  let html = '';
  oomTargetsData.forEach((t, idx) => {
    const gainSign = t.gainPct >= 0 ? '+' : '';
    const gainClass = t.gainPct >= 0 ? 'oom-gain-badge' : 'oom-gain-badge negative';
    const proceedsFmt = `$${t.proceeds.toFixed(2)}`;

    html += `
      <tr>
        <td style="font-weight:700; color:#64748b; font-family:ui-monospace, monospace;">${t.label}</td>
        <td style="font-weight:800; color:${sideColor};">${t.side}</td>
        <td>
          <input type="number" min="1" step="1" class="oom-table-input" value="${t.qty}"
                 oninput="onTargetRowQtyChange(${idx}, this.value)" style="width: 58px;" />
        </td>
        <td>
          <input type="number" step="${oomCurrentContract?.tickSize || 0.1}" class="oom-table-input" value="${t.price}"
                 oninput="onTargetRowPriceChange(${idx}, this.value)" style="width: 82px;" />
        </td>
        <td>
          <span class="${gainClass}">${gainSign}${t.gainPct.toFixed(1)}%</span>
        </td>
        <td>
          <span class="oom-proceeds-val">${proceedsFmt}</span>
        </td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

function onTargetRowQtyChange(idx, val) {
  const parsed = parseInt(val, 10);
  if (isNaN(parsed) || parsed < 1) return;
  if (oomTargetsData[idx]) {
    oomTargetsData[idx].qty = parsed;
    const contractVal = oomCurrentContract ? oomCurrentContract.contractValue : 0.001;
    oomTargetsData[idx].proceeds = oomTargetsData[idx].price * parsed * contractVal;
    renderLadderTable();
    updateQtySplitValidation();
  }
}

function onTargetRowPriceChange(idx, val) {
  const parsed = parseFloat(val);
  if (isNaN(parsed) || parsed <= 0) return;
  if (oomTargetsData[idx]) {
    oomTargetsData[idx].price = parsed;
    const inpEntryPrice = document.getElementById('oomInpPrice');
    const entryPrice = parseFloat(inpEntryPrice?.value) || oomCurrentContract?.mid || 9.0;
    const isBuy = oomCurrentContract ? oomCurrentContract.side === 'buy' : true;
    oomTargetsData[idx].gainPct = entryPrice > 0 ? ((parsed - entryPrice) / entryPrice) * 100 * (isBuy ? 1 : -1) : 0;
    const contractVal = oomCurrentContract ? oomCurrentContract.contractValue : 0.001;
    oomTargetsData[idx].proceeds = parsed * oomTargetsData[idx].qty * contractVal;
    renderLadderTable();
  }
}

function updateQtySplitValidation() {
  const badge = document.getElementById('oomQtySplitBadge');
  const inpTotalQty = document.getElementById('oomInpQty');
  if (!badge) return;

  const totalQty = parseInt(inpTotalQty?.value, 10) || 10;
  const currentSum = oomTargetsData.reduce((acc, t) => acc + (t.qty || 0), 0);

  badge.textContent = `Qty split ${currentSum} / ${totalQty}`;
  if (currentSum === totalQty) {
    badge.className = 'oom-qty-split-badge ok';
  } else {
    badge.className = 'oom-qty-split-badge err';
  }
}

/**
 * Executes the Order (Entry Order + Armed Ladder Targets)
 */
async function executeOptionOrder() {
  if (!oomCurrentContract) return;

  const btn = document.getElementById('oomBtnExecute');
  const inpQty = document.getElementById('oomInpQty');
  const selOrderType = document.getElementById('oomOrderType');
  const inpPrice = document.getElementById('oomInpPrice');
  const selTif = document.getElementById('oomTimeInForce');
  const chkPostOnly = document.getElementById('oomChkPostOnly');
  const chkTargets = document.getElementById('oomChkMultipleTargets');

  const size = parseInt(inpQty?.value, 10) || 1;
  const orderType = (selOrderType?.value || 'limit').toLowerCase();
  const limitPrice = orderType === 'limit' ? parseFloat(inpPrice?.value) : null;
  const tif = selTif?.value || 'gtc';
  const postOnly = chkPostOnly ? chkPostOnly.checked : false;
  const hasMultipleTargets = chkTargets ? chkTargets.checked : false;

  if (size <= 0) {
    if (typeof showToast === 'function') showToast('Please enter a valid quantity > 0', 'error');
    return;
  }

  if (orderType === 'limit' && (!limitPrice || limitPrice <= 0)) {
    if (typeof showToast === 'function') showToast('Please enter a valid Limit Price', 'error');
    return;
  }

  // Validate Targets Quantity if Multiple Targets enabled
  if (hasMultipleTargets && oomTargetsData.length > 0) {
    const sumQty = oomTargetsData.reduce((acc, t) => acc + (t.qty || 0), 0);
    if (sumQty !== size) {
      if (typeof showToast === 'function') {
        showToast(`Targets Qty (${sumQty}) does not match Total Quantity (${size}). Click Recalculate or adjust rows.`, 'error');
      }
      return;
    }
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:2px;display:inline-block;margin-right:6px;"></span> Executing...`;
  }

  try {
    const activeAcct = (typeof currentAccount !== 'undefined' ? currentAccount : 'main');

    const payload = {
      symbol: oomCurrentContract.symbol,
      side: oomCurrentContract.side,
      size: size,
      order_type: orderType,
      limit_price: limitPrice,
      time_in_force: tif,
      post_only: postOnly,
      account: activeAcct,
      targets: hasMultipleTargets ? oomTargetsData.map(t => ({
        size: t.qty,
        price: t.price,
        side: t.side
      })) : []
    };

    const endpoint = hasMultipleTargets ? '/api/orders/ladder-entry' : '/api/orders/single';
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (data && data.success) {
      closeOptionOrderModal();
      if (typeof showToast === 'function') {
        if (hasMultipleTargets) {
          showToast(`⚡ Entry order placed for ${oomCurrentContract.symbol}! ${oomTargetsData.length} Targets Armed on [${activeAcct.toUpperCase()}].`, 'success');
        } else {
          showToast(`⚡ ${orderType.toUpperCase()} order placed successfully for ${oomCurrentContract.symbol}!`, 'success');
        }
      }
    } else {
      if (typeof showToast === 'function') {
        showToast(`Order failed: ${data?.error || 'Unknown error'}`, 'error');
      }
    }
  } catch (err) {
    if (typeof showToast === 'function') {
      showToast(`Execution error: ${err.message}`, 'error');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Execute';
    }
  }
}

/**
 * Hooks into live WebSocket feed for real-time modal quotes
 */
function subscribeModalTicks(symbol) {
  if (!symbol) return;
  if (typeof addSubscribedSymbols === 'function') {
    addSubscribedSymbols([symbol]);
  } else if (typeof getWsManager === 'function') {
    const ws = getWsManager();
    if (ws && typeof ws.subscribeSymbols === 'function') {
      ws.subscribeSymbols([symbol]);
    }
  }
}

/**
 * Updates modal quote banner when live ticks arrive via WebSocket
 */
function updateOptionOrderModalTick(sym, tick) {
  if (!oomCurrentContract || !sym) return;
  if (oomCurrentContract.symbol.toUpperCase() !== sym.toUpperCase()) return;

  let changed = false;
  if (tick.bid !== undefined && tick.bid !== null && Number(tick.bid) > 0) {
    oomCurrentContract.bid = parseFloat(tick.bid);
    changed = true;
  }
  if (tick.ask !== undefined && tick.ask !== null && Number(tick.ask) > 0) {
    oomCurrentContract.ask = parseFloat(tick.ask);
    changed = true;
  }
  if (tick.bidQty !== undefined && tick.bidQty !== null) {
    oomCurrentContract.bidQty = tick.bidQty;
    changed = true;
  }
  if (tick.askQty !== undefined && tick.askQty !== null) {
    oomCurrentContract.askQty = tick.askQty;
    changed = true;
  }

  if (changed) {
    if (oomCurrentContract.bid > 0 && oomCurrentContract.ask > 0) {
      oomCurrentContract.mid = alignPriceTick((oomCurrentContract.bid + oomCurrentContract.ask) / 2, oomCurrentContract.tickSize);
    }
    updateMarketBannerQuotes();
  }
}

// Global Keyboard Handler (Escape closes modal)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const overlay = document.getElementById('optionOrderModal');
    if (overlay && (overlay.classList.contains('active') || overlay.style.display === 'flex')) {
      closeOptionOrderModal();
    }
  }
});

// Explicitly bind all modal functions to window for bulletproof access from inline HTML & external scripts
window.openOptionOrderModal = openOptionOrderModal;
window.closeOptionOrderModal = closeOptionOrderModal;
window.setEntrySide = setEntrySide;
window.onOrderTypeChange = onOrderTypeChange;
window.fillQuickPrice = fillQuickPrice;
window.toggleMultipleTargets = toggleMultipleTargets;
window.recalculateLadder = recalculateLadder;
window.onTargetRowQtyChange = onTargetRowQtyChange;
window.onTargetRowPriceChange = onTargetRowPriceChange;
window.executeOptionOrder = executeOptionOrder;
window.updateOptionOrderModalTick = updateOptionOrderModalTick;

