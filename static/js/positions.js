/**
 * ==============================================================================
 * DELTA EXCHANGE - LIVE POSITIONS MODULE
 * Dual-Currency Table Rendering, Filter & Search, JSON Inspect Modal, 1-Click Exit
 * ==============================================================================
 */

let currentFilter = (function () {
  try {
    return localStorage.getItem('delta_positions_filter') || 'all';
  } catch (e) {
    return 'all';
  }
})();

/**
 * Renders the live positions table with full dual-currency values and badges
 */
function renderPositionsTable() {
  const tbody = document.getElementById('positionsTbody');
  const emptyState = document.getElementById('emptyState');
  if (!tbody || !emptyState) return;

  // Sync active badge count on subnav
  const lmPosBadge = document.getElementById('lmPosCountBadge');
  if (lmPosBadge) {
    const activeCount = currentPositions.filter(p => p && p.abs_size && Number(p.abs_size) > 0).length;
    lmPosBadge.textContent = activeCount;
  }

  // Sync active class on filter tab buttons
  document.querySelectorAll('.filter-tabs .tab-btn').forEach(btn => {
    const fnAttr = btn.getAttribute('onclick') || '';
    btn.classList.toggle('active', fnAttr.includes(`'${currentFilter}'`));
  });

  const searchVal = (document.getElementById('searchInput')?.value || '').toLowerCase();

  const filtered = currentPositions.filter(pos => {
    if (!pos || !pos.abs_size || Number(pos.abs_size) <= 0) return false;
    if (currentFilter === 'calls' && pos.type_badge !== 'CALL') return false;
    if (currentFilter === 'puts' && pos.type_badge !== 'PUT') return false;
    if (currentFilter === 'options' && pos.type_badge !== 'CALL' && pos.type_badge !== 'PUT') return false;
    if (currentFilter === 'longs' && pos.side !== 'LONG') return false;
    if (currentFilter === 'shorts' && pos.side !== 'SHORT') return false;

    if (searchVal) {
      const sym = (pos.symbol || '').toLowerCase();
      const desc = (pos.description || '').toLowerCase();
      const prodId = String(pos.product_id || '');
      if (!sym.includes(searchVal) && !desc.includes(searchVal) && !prodId.includes(searchVal)) return false;
    }
    return true;
  });

  const tfoot = document.getElementById('positionsTfoot');
  const tableWrapper = tbody.closest('.table-responsive');
  const tablePosCount = document.getElementById('tablePosCount');

  if (tablePosCount) {
    tablePosCount.textContent = `${filtered.length} Active`;
  }

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    if (tfoot) tfoot.innerHTML = '';
    if (tableWrapper) tableWrapper.style.display = 'none';
    emptyState.style.display = 'flex';
    return;
  }

  emptyState.style.display = 'none';
  if (tableWrapper) tableWrapper.style.display = '';

  let totalTableNotional = 0;
  let totalTableUPnl = 0;
  let totalTableRPnl = 0;
  let totalContracts = 0;

  let html = '';
  filtered.forEach((pos, idx) => {
    totalTableNotional += (pos.notional_usd || 0);
    totalTableUPnl += (pos.unrealized_pnl || 0);
    totalTableRPnl += (pos.realized_pnl || 0);
    totalContracts += (pos.abs_size || 0);

    const isLong = pos.side === 'LONG';
    const sideClass = isLong ? 'side-long' : 'side-short';
    const sideIcon = isLong ? '▲' : '▼';

    let typeBadgeClass = 'badge-perp';
    if (pos.type_badge === 'CALL') typeBadgeClass = 'badge-call';
    else if (pos.type_badge === 'PUT') typeBadgeClass = 'badge-put';

    const uPnl = pos.unrealized_pnl || 0;
    const uPnlClass = uPnl >= 0 ? 'text-green' : 'text-red';
    const pnlPctClass = uPnl >= 0 ? 'pnl-pos-pct' : 'pnl-neg-pct';
    const pnlPct = Number(pos.unrealized_pnl_pct !== undefined ? pos.unrealized_pnl_pct : (pos.pnl_percentage || 0));
    const pnlPctSign = pnlPct >= 0 ? '+' : '';

    const rPnl = pos.realized_pnl || 0;
    const rPnlClass = rPnl >= 0 ? 'text-green' : 'text-red';

    const entryPriceMain = formatMoney(pos.entry_price, selectedCurrency, 2, 4);
    const entryPriceAlt = formatAltMoney(pos.entry_price);

    const ltpMain = formatMoney(pos.ltp, selectedCurrency, 2, 4);
    const ltpAlt = formatAltMoney(pos.ltp);

    const markPriceMain = formatMoney(pos.mark_price, selectedCurrency, 2, 4);
    const markPriceAlt = formatAltMoney(pos.mark_price);

    const uPnlMain = formatMoney(uPnl, selectedCurrency, 2, 4);
    const uPnlAlt = formatAltMoney(uPnl);

    const rPnlMain = formatMoney(rPnl, selectedCurrency, 2, 4);
    const rPnlAlt = formatAltMoney(rPnl);

    const notionalMain = formatMoney(pos.notional_usd, selectedCurrency, 2, 2);
    const notionalAlt = formatAltMoney(pos.notional_usd);

    const underlyingSymbol = pos.underlying_asset || 'BTC';
    const descriptionText = pos.description || `${underlyingSymbol} ${pos.strike_price || ''} ${pos.type_badge} Option`;

    html += `
      <tr>
        <!-- Contract / Symbol -->
        <td>
          <div class="symbol-cell">
            <div class="symbol-name-row">
              <span class="symbol-name">${pos.symbol}</span>
              <span class="badge-type ${typeBadgeClass}">${pos.type_badge}</span>
            </div>
            <div class="symbol-desc">${descriptionText}</div>
          </div>
        </td>

        <!-- Side -->
        <td>
          <span class="side-badge ${sideClass}">
            <span>${sideIcon}</span>
            <span>${pos.side}</span>
          </span>
        </td>

        <!-- Size / Contracts -->
        <td>
          <div class="mono-num" style="font-size: 0.95rem;">${formatNumber(pos.abs_size, 0)} <span style="font-size: 0.72rem; color: var(--text-muted);">contracts</span></div>
          <div style="font-size: 0.75rem; color: var(--text-secondary);">${formatNumber(pos.underlying_qty, 4)} ${underlyingSymbol}</div>
        </td>

        <!-- Entry Price -->
        <td>
          <div class="mono-num" style="font-size: 0.92rem;">${entryPriceMain}</div>
          <div class="sub-curr">${entryPriceAlt}</div>
        </td>

        <!-- LTP / Market Price -->
        <td>
          <div class="mono-num text-blue" style="font-size: 0.92rem; font-weight: 700;">${ltpMain}</div>
          <div class="sub-curr">${ltpAlt}</div>
        </td>

        <!-- Mark Price -->
        <td>
          <div class="mono-num" style="font-size: 0.92rem; color: var(--text-secondary);">${markPriceMain}</div>
          <div class="sub-curr">${markPriceAlt}</div>
        </td>

        <!-- Unrealized PnL -->
        <td>
          <div class="pnl-pill">
            <span class="pnl-value ${uPnlClass}">${uPnlMain}</span>
            <span class="pnl-pct ${pnlPctClass}">${pnlPctSign}${pnlPct.toFixed(2)}%</span>
            <span class="sub-curr">${uPnlAlt}</span>
          </div>
        </td>

        <!-- Realized PnL -->
        <td>
          <div class="mono-num ${rPnlClass}">${rPnlMain}</div>
          <div class="sub-curr">${rPnlAlt}</div>
        </td>

        <!-- Notional Value -->
        <td>
          <div class="mono-num" style="color: var(--text-secondary);">${notionalMain}</div>
          <div class="sub-curr">${notionalAlt}</div>
        </td>

        <!-- Margin Mode -->
        <td>
          <span class="badge-mode">${pos.margin_mode || 'PORTFOLIO'}</span>
        </td>

        <!-- 1-Click Exit -->
        <td>
          <button class="btn-row-squareoff" onclick="squareOffPosition('${pos.product_id}', '${pos.symbol}')">
            ⚡ Exit
          </button>
        </td>

        <!-- Inspect Details -->
        <td>
          <button class="btn-action-view" onclick="inspectPosition(${idx})">Inspect</button>
        </td>
      </tr>
    `;
  });

  tbody.innerHTML = html;

  if (tfoot) {
    if (filtered.length > 0) {
      const totNotionalMain = formatMoney(totalTableNotional, selectedCurrency, 2, 2);
      const totNotionalAlt = formatAltMoney(totalTableNotional);
      const totUPnlMain = formatMoney(totalTableUPnl, selectedCurrency, 2, 2);
      const totUPnlAlt = formatAltMoney(totalTableUPnl);
      const totUPnlClass = totalTableUPnl >= 0 ? 'text-green' : 'text-red';
      const totRPnlMain = formatMoney(totalTableRPnl, selectedCurrency, 2, 2);
      const totRPnlAlt = formatAltMoney(totalTableRPnl);
      const totRPnlClass = totalTableRPnl >= 0 ? 'text-green' : 'text-red';

      tfoot.innerHTML = `
        <tr>
          <td colspan="2" class="tfoot-label">
            TOTAL (${filtered.length} ${filtered.length === 1 ? 'Position' : 'Positions'})
          </td>
          <td>
            <div class="mono-num" style="font-weight: 700;">${formatNumber(totalContracts, 0)} <span style="font-size: 0.72rem; color: var(--text-muted);">contracts</span></div>
          </td>
          <td colspan="3" style="text-align: right; color: var(--text-muted); font-size: 0.78rem; font-weight: 600;">PORTFOLIO TOTALS:</td>
          <td>
            <div class="mono-num ${totUPnlClass}" style="font-weight: 700; font-size: 0.95rem;">${totUPnlMain}</div>
            <div class="sub-curr">${totUPnlAlt}</div>
          </td>
          <td>
            <div class="mono-num ${totRPnlClass}" style="font-weight: 700; font-size: 0.95rem;">${totRPnlMain}</div>
            <div class="sub-curr">${totRPnlAlt}</div>
          </td>
          <td>
            <div class="mono-num text-amber" style="font-weight: 800; font-size: 1.0rem;">${totNotionalMain}</div>
            <div class="sub-curr">${totNotionalAlt}</div>
          </td>
          <td colspan="3"></td>
        </tr>
      `;
    } else {
      tfoot.innerHTML = '';
    }
  }
}

/**
 * Filter positions by category button
 */
function setFilter(filter, el) {
  currentFilter = filter;
  try {
    localStorage.setItem('delta_positions_filter', filter);
  } catch (e) { }
  document.querySelectorAll('.filter-tabs .tab-btn').forEach(btn => btn.classList.remove('active'));
  if (el) {
    el.classList.add('active');
  }
  renderPositionsTable();
}

/**
 * Live filter search box input trigger
 */
function filterPositions() {
  renderPositionsTable();
}

/**
 * Inspect position payload and formula breakdown modal
 */
function inspectPosition(index) {
  const pos = currentPositions[index];
  if (!pos) return;
  document.getElementById('modalSymbolTitle').textContent = `Position Details: ${pos.symbol}`;

  const costBasis = pos.cost_basis || (pos.underlying_qty * pos.entry_price);
  const currentValue = pos.underlying_qty * pos.ltp;

  const breakdownHtml = `
Contract: ${pos.symbol} (${pos.type_badge})
Side: ${pos.side} | Size: ${pos.abs_size} contracts (${pos.underlying_qty} ${pos.underlying_asset || 'BTC'})
--------------------------------------------------------------------------------
• Entry Price:        ${formatMoney(pos.entry_price)} (${formatAltMoney(pos.entry_price)})
• Market Price (LTP): ${formatMoney(pos.ltp)} (${formatAltMoney(pos.ltp)})
• Mark Price:         ${formatMoney(pos.mark_price)} (${formatAltMoney(pos.mark_price)})
• Best Bid / Ask:     $${pos.best_bid !== null ? pos.best_bid : '--'} / $${pos.best_ask !== null ? pos.best_ask : '--'}
--------------------------------------------------------------------------------
• Total Cost Basis:   ${formatMoney(costBasis)} (${formatAltMoney(costBasis)})
• Current Value:      ${formatMoney(currentValue)} (${formatAltMoney(currentValue)})
• Unrealized PnL:     ${formatMoney(pos.unrealized_pnl)} (${pos.unrealized_pnl_pct >= 0 ? '+' : ''}${pos.unrealized_pnl_pct}%)
• Realized PnL:       ${formatMoney(pos.realized_pnl)} (${formatAltMoney(pos.realized_pnl)})
--------------------------------------------------------------------------------
[Formula: (Current Price - Entry Price) * Contracts * Contract Value]
`;
  document.getElementById('modalRawJson').textContent = breakdownHtml + "\nRAW DELTA EXCHANGE PAYLOAD:\n" + JSON.stringify(pos.raw, null, 2);
  document.getElementById('detailModal').classList.add('active');
}

/**
 * Close modal utility
 */
function closeModal(modalId) {
  document.getElementById(modalId)?.classList.remove('active');
}

/**
 * 1-Click Single Position Square Off with confirmation
 */
async function squareOffPosition(productId, symbol) {
  if (!confirm(`Are you sure you want to SQUARE OFF and exit ${symbol} at MARKET price?`)) return;

  try {
    const data = await ApiService.squareOffPosition(productId, symbol);
    if (data.success) {
      showToast(`⚡ Squared off ${symbol} successfully!`, 'success');
      // Optimistically remove squared off position immediately for instant UI response
      currentPositions = currentPositions.filter(p => String(p.product_id) !== String(productId) && p.symbol !== symbol);
      renderDashboardValues();
      renderPositionsTable();
      setTimeout(() => fetchDashboardData(false), 600);
      setTimeout(() => fetchDashboardData(false), 2000);
    } else {
      showToast(`Failed to square off ${symbol}: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Square off error: ${err.message}`, 'error');
  }
}

/**
 * ==============================================================================
 * LIVE OPEN ORDERS SUB-TAB CONTROLLER
 * ==============================================================================
 */

let currentLiveOrders = [];
let currentOrderFilter = 'all';
let currentLMSubTab = 'positions';
let inlineEditingOrders = {}; // orderId -> { qty: boolean, price: boolean }

/**
 * Switches between 'positions' and 'orders' sub-tabs
 */
function switchLiveManagerSubTab(tab) {
  currentLMSubTab = tab;
  const btnPositions = document.getElementById('btnSubnavPositions');
  const btnOrders = document.getElementById('btnSubnavOrders');
  const panelPositions = document.getElementById('panelPositions');
  const panelOrders = document.getElementById('panelOpenOrders');

  if (tab === 'orders') {
    if (btnPositions) btnPositions.classList.remove('active');
    if (btnOrders) btnOrders.classList.add('active');
    if (panelPositions) panelPositions.style.display = 'none';
    if (panelOrders) panelOrders.style.display = 'block';
    renderOpenOrdersTable();
    fetchLiveOrders(true);
  } else {
    if (btnOrders) btnOrders.classList.remove('active');
    if (btnPositions) btnPositions.classList.add('active');
    if (panelOrders) panelOrders.style.display = 'none';
    if (panelPositions) panelPositions.style.display = 'block';
    renderPositionsTable();
  }
}

/**
 * Fetches all live/open orders from Delta Exchange via API
 */
async function fetchLiveOrders(silent = false) {
  try {
    const activeAcct = (window.currentActiveAccount || 'sub').toLowerCase();
    const data = await ApiService.getLiveOrders(activeAcct);

    if (data && data.success && Array.isArray(data.orders)) {
      currentLiveOrders = data.orders;
    } else {
      currentLiveOrders = [];
    }

    // Update subnav badge count
    const badge = document.getElementById('lmOrdersCountBadge');
    if (badge) badge.textContent = currentLiveOrders.length;

    const tableCount = document.getElementById('tableOrdersCount');
    if (tableCount) tableCount.textContent = `${currentLiveOrders.length} Open`;

    renderOpenOrdersTable();
  } catch (err) {
    if (!silent) {
      console.warn('[fetchLiveOrders] Error:', err);
    }
  }
}

/**
 * Renders the Open Orders table
 */
function renderOpenOrdersTable() {
  const tbody = document.getElementById('ordersTbody');
  const emptyState = document.getElementById('emptyOrdersState');
  if (!tbody || !emptyState) return;

  // Sync active class on filter buttons
  document.querySelectorAll('#panelOpenOrders .filter-tabs .tab-btn').forEach(btn => {
    const fnAttr = btn.getAttribute('onclick') || '';
    btn.classList.toggle('active', fnAttr.includes(`'${currentOrderFilter}'`));
  });

  const searchVal = (document.getElementById('orderSearchInput')?.value || '').toLowerCase().trim();

  const filtered = currentLiveOrders.filter(ord => {
    if (!ord) return false;
    const side = (ord.side || '').toLowerCase();
    const isReduceOnly = Boolean(ord.reduce_only);

    if (currentOrderFilter === 'buy' && side !== 'buy') return false;
    if (currentOrderFilter === 'sell' && side !== 'sell') return false;
    if (currentOrderFilter === 'reduce_only' && !isReduceOnly) return false;

    if (searchVal) {
      const sym = (ord.symbol || '').toLowerCase();
      const idStr = String(ord.id || '');
      if (!sym.includes(searchVal) && !idStr.includes(searchVal)) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    emptyState.style.display = 'flex';
    return;
  }

  emptyState.style.display = 'none';

  let html = '';
  filtered.forEach(ord => {
    const isBuy = ord.side === 'buy';
    const sideClass = isBuy ? 'badge-order-buy' : 'badge-order-sell';
    const sideText = isBuy ? 'BUY' : 'SELL';

    // Format Symbol & Option Badges
    const sym = ord.symbol || `Product #${ord.product_id}`;
    let typeBadgeHtml = '';
    if (sym.startsWith('C-')) {
      typeBadgeHtml = '<span class="badge-type badge-call" style="font-size:0.68rem; padding:1px 5px; margin-left:6px;">CALL</span>';
    } else if (sym.startsWith('P-')) {
      typeBadgeHtml = '<span class="badge-type badge-put" style="font-size:0.68rem; padding:1px 5px; margin-left:6px;">PUT</span>';
    }

    // Format Order Type
    let typeText = 'Limit';
    const rawType = (ord.order_type || '').toLowerCase();
    if (rawType.includes('market')) typeText = 'Market';
    else if (rawType.includes('stop')) typeText = 'Stop';

    // Format Price: PURE USD ONLY ($) with inline editing support
    const isEditingPrice = Boolean(inlineEditingOrders[ord.id]?.price);
    let priceHtml = '--';
    if (isEditingPrice) {
      priceHtml = `
        <div class="inline-edit-group" onclick="event.stopPropagation()">
          <button class="btn-inline-action btn-inline-cancel" onclick="cancelOrderEdit('${ord.id}', 'price')" title="Cancel">✕</button>
          <input type="number" id="inlinePrice_${ord.id}" class="inline-edit-input price-input" value="${ord.limit_price !== null && ord.limit_price !== undefined ? ord.limit_price : ''}" step="any" min="0.0001" placeholder="Price" onkeydown="handleInlineOrderKey(event, '${ord.id}', '${ord.product_id}', 'price')">
          <button class="btn-inline-action btn-inline-save" id="btnSavePrice_${ord.id}" onclick="saveOrderInlineEdit('${ord.id}', '${ord.product_id}', 'price')" title="Confirm Price">✓</button>
        </div>
      `;
    } else if (ord.limit_price !== null && ord.limit_price !== undefined) {
      const px = Number(ord.limit_price);
      priceHtml = `
        <div class="editable-order-cell" onclick="startOrderEdit('${ord.id}', 'price')" title="Click to edit price ($)">
          <span class="mono-num text-green" style="font-weight:700;">$${px}</span>
          <svg class="edit-icon-hint" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 20h9"></path><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path></svg>
        </div>
      `;
    } else {
      priceHtml = '<span class="badge-order-type">MARKET</span>';
    }

    // Trigger Price: Pure USD ($)
    const triggerHtml = ord.stop_price ? `<span class="mono-num text-amber">$${ord.stop_price}</span>` : '<span style="color:var(--text-muted);">--</span>';

    // Quantities (Remaining / Total) with inline editing support
    const isEditingQty = Boolean(inlineEditingOrders[ord.id]?.qty);
    const unfilled = ord.unfilled_size !== undefined ? ord.unfilled_size : ord.size;
    const total = ord.size || 1;
    let qtyHtml = '';
    if (isEditingQty) {
      qtyHtml = `
        <div class="inline-edit-group" onclick="event.stopPropagation()">
          <button class="btn-inline-action btn-inline-cancel" onclick="cancelOrderEdit('${ord.id}', 'qty')" title="Cancel">✕</button>
          <input type="number" id="inlineQty_${ord.id}" class="inline-edit-input" value="${total}" min="1" step="1" placeholder="Qty" onkeydown="handleInlineOrderKey(event, '${ord.id}', '${ord.product_id}', 'qty')">
          <button class="btn-inline-action btn-inline-save" id="btnSaveQty_${ord.id}" onclick="saveOrderInlineEdit('${ord.id}', '${ord.product_id}', 'qty')" title="Confirm Qty">✓</button>
        </div>
      `;
    } else {
      qtyHtml = `
        <div class="editable-order-cell" onclick="startOrderEdit('${ord.id}', 'qty')" title="Click to edit quantity">
          <span class="mono-num"><strong>${unfilled}</strong> <span style="color:var(--text-muted);">/ ${total}</span></span>
          <svg class="edit-icon-hint" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 20h9"></path><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path></svg>
        </div>
      `;
    }

    // Reduce Only Badge
    const roHtml = ord.reduce_only
      ? '<span class="badge-order-ro">RO</span>'
      : '<span style="color:var(--text-muted); font-size:0.8rem;">--</span>';

    // Time Placed
    let timeFormatted = '--';
    if (ord.created_at) {
      try {
        const d = new Date(ord.created_at);
        const mo = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        const tm = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        timeFormatted = `<span style="font-size:0.75rem; color:var(--text-muted);">${mo} ${tm}</span>`;
      } catch (_) {
        timeFormatted = String(ord.created_at).slice(11, 19);
      }
    }

    const isRowEditing = isEditingQty || isEditingPrice;

    html += `
      <tr>
        <td>
          <span class="mono-num text-muted" style="font-size:0.75rem; font-weight:600;">#${ord.id}</span>
        </td>
        <td>
          <div style="display:inline-flex; align-items:center;">
            <span class="symbol-name" style="font-weight:700;">${sym}</span>
            ${typeBadgeHtml}
          </div>
        </td>
        <td>
          <span class="${sideClass}">${sideText}</span>
        </td>
        <td>
          <span class="badge-order-type">${typeText}</span>
        </td>
        <td>
          ${priceHtml}
        </td>
        <td>
          ${triggerHtml}
        </td>
        <td>
          ${qtyHtml}
        </td>
        <td>
          ${roHtml}
        </td>
        <td>
          ${timeFormatted}
        </td>
        <td style="text-align: center; white-space: nowrap;">
          <button class="btn-edit-order" onclick="toggleOrderRowEdit('${ord.id}')" title="${isRowEditing ? 'Save edits' : 'Edit Qty & Price'}">
            ${isRowEditing ? 'Save' : 'Edit'}
          </button>
          <button class="btn-cancel-order" onclick="cancelSingleOrder('${ord.id}', '${ord.product_id}', '${sym}')" title="Cancel this order immediately">
            Cancel
          </button>
        </td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

/**
 * Filter button switcher for orders
 */
function setOrderFilter(filter, el) {
  currentOrderFilter = filter;
  document.querySelectorAll('#panelOpenOrders .filter-tabs .tab-btn').forEach(btn => btn.classList.remove('active'));
  if (el) el.classList.add('active');
  renderOpenOrdersTable();
}

/**
 * Filter input trigger for orders
 */
function filterOpenOrders() {
  renderOpenOrdersTable();
}

/**
 * 1-Click Cancel Single Order
 */
async function cancelSingleOrder(orderId, productId, symbol) {
  if (!orderId) return;

  try {
    const activeAcct = (window.currentActiveAccount || 'sub').toLowerCase();
    const data = await ApiService.cancelLiveOrder(orderId, productId, activeAcct);

    if (data && data.success) {
      if (typeof showToast === 'function') {
        showToast(`⚡ Order #${orderId} for ${symbol || ''} cancelled successfully!`, 'success');
      }
      // Optimistic removal for instant UI feedback
      currentLiveOrders = currentLiveOrders.filter(o => String(o.id) !== String(orderId));
      renderOpenOrdersTable();
      const badge = document.getElementById('lmOrdersCountBadge');
      if (badge) badge.textContent = currentLiveOrders.length;
      setTimeout(() => fetchLiveOrders(true), 600);
    } else {
      const err = data?.error || 'Failed to cancel order.';
      if (typeof showToast === 'function') showToast(`Cancel failed: ${err}`, 'error');
    }
  } catch (err) {
    if (typeof showToast === 'function') showToast(`Error: ${err.message}`, 'error');
  }
}

/**
 * Cancel All Open Orders in 1 Click
 */
async function cancelAllOpenOrders() {
  const activeAcct = (window.currentActiveAccount || 'sub').toUpperCase();
  if (!confirm(`Are you sure you want to CANCEL ALL active open orders on [${activeAcct}]?`)) return;

  const btn = document.getElementById('btnCancelAllOrders');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner" style="width:12px;height:12px;border-width:2px;display:inline-block;margin-right:4px;"></span> Cancelling...`;
  }

  try {
    const data = await ApiService.cancelAllLiveOrders(activeAcct.toLowerCase());
    if (data && data.success) {
      if (typeof showToast === 'function') {
        showToast(`⚡ All open orders on [${activeAcct}] cancelled successfully!`, 'success');
      }
      currentLiveOrders = [];
      renderOpenOrdersTable();
      const badge = document.getElementById('lmOrdersCountBadge');
      if (badge) badge.textContent = '0';
      setTimeout(() => fetchLiveOrders(true), 800);
    } else {
      const err = data?.error || 'Failed to cancel all orders.';
      if (typeof showToast === 'function') showToast(`Cancel all failed: ${err}`, 'error');
    }
  } catch (err) {
    if (typeof showToast === 'function') showToast(`Error: ${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="15" y1="9" x2="9" y2="15"></line>
          <line x1="9" y1="9" x2="15" y2="15"></line>
        </svg>
        <span>Cancel All Orders</span>
      `;
    }
  }
}

/**
 * ==============================================================================
 * INLINE EDITING FOR OPEN ORDERS (Price in USD & Quantity)
 * ==============================================================================
 */

/**
 * Activates inline editing for a specific field ('qty' or 'price')
 */
function startOrderEdit(orderId, field) {
  if (!inlineEditingOrders[orderId]) {
    inlineEditingOrders[orderId] = {};
  }
  inlineEditingOrders[orderId][field] = true;
  renderOpenOrdersTable();
  setTimeout(() => {
    const el = document.getElementById(field === 'qty' ? `inlineQty_${orderId}` : `inlinePrice_${orderId}`);
    if (el) {
      el.focus();
      el.select();
    }
  }, 25);
}

/**
 * Cancels inline editing for a specific field or all fields
 */
function cancelOrderEdit(orderId, field) {
  if (inlineEditingOrders[orderId]) {
    if (field === 'all') {
      delete inlineEditingOrders[orderId];
    } else {
      delete inlineEditingOrders[orderId][field];
      if (!inlineEditingOrders[orderId].qty && !inlineEditingOrders[orderId].price) {
        delete inlineEditingOrders[orderId];
      }
    }
  }
  renderOpenOrdersTable();
}

/**
 * Toggles inline editing for both Qty and Price on an order row
 */
function toggleOrderRowEdit(orderId) {
  if (inlineEditingOrders[orderId]?.qty || inlineEditingOrders[orderId]?.price) {
    const ord = currentLiveOrders.find(o => String(o.id) === String(orderId));
    if (ord) {
      saveOrderInlineEdit(orderId, ord.product_id, 'all');
    }
  } else {
    if (!inlineEditingOrders[orderId]) inlineEditingOrders[orderId] = {};
    inlineEditingOrders[orderId].qty = true;
    inlineEditingOrders[orderId].price = true;
    renderOpenOrdersTable();
    setTimeout(() => {
      const el = document.getElementById(`inlinePrice_${orderId}`) || document.getElementById(`inlineQty_${orderId}`);
      if (el) {
        el.focus();
        el.select();
      }
    }, 25);
  }
}

/**
 * Handles Enter to Save and Escape to Cancel in inline inputs
 */
function handleInlineOrderKey(event, orderId, productId, field) {
  if (event.key === 'Enter') {
    event.preventDefault();
    saveOrderInlineEdit(orderId, productId, field);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    cancelOrderEdit(orderId, field);
  }
}

/**
 * Saves modified quantity and/or price to Delta Exchange
 */
async function saveOrderInlineEdit(orderId, productId, field) {
  const ord = currentLiveOrders.find(o => String(o.id) === String(orderId));
  const activeAcct = (window.currentActiveAccount || 'sub').toLowerCase();

  let newQty = undefined;
  let newPrice = undefined;

  if (field === 'qty' || field === 'all') {
    const inputQty = document.getElementById(`inlineQty_${orderId}`);
    if (inputQty) {
      const val = parseInt(inputQty.value, 10);
      if (isNaN(val) || val <= 0) {
        if (typeof showToast === 'function') showToast('Quantity must be at least 1.', 'error');
        return;
      }
      newQty = val;
    }
  }

  if (field === 'price' || field === 'all') {
    const inputPrice = document.getElementById(`inlinePrice_${orderId}`);
    if (inputPrice) {
      const val = parseFloat(inputPrice.value);
      if (isNaN(val) || val <= 0) {
        if (typeof showToast === 'function') showToast('Price must be greater than 0.', 'error');
        return;
      }
      newPrice = val;
    }
  }

  const btnSave = document.getElementById(field === 'qty' ? `btnSaveQty_${orderId}` : `btnSavePrice_${orderId}`);
  if (btnSave) {
    btnSave.disabled = true;
    btnSave.textContent = '...';
  }

  try {
    const res = await ApiService.editLiveOrder(orderId, productId, {
      limitPrice: newPrice,
      size: newQty,
      account: activeAcct
    });

    if (res && res.success) {
      const changes = [];
      if (newQty !== undefined) changes.push(`Qty: ${newQty}`);
      if (newPrice !== undefined) changes.push(`Price: $${newPrice}`);
      if (typeof showToast === 'function') {
        showToast(`⚡ Order #${orderId} updated (${changes.join(', ')})`, 'success');
      }

      // Update in memory immediately
      if (ord) {
        if (newQty !== undefined) {
          ord.size = newQty;
          ord.unfilled_size = newQty;
        }
        if (newPrice !== undefined) {
          ord.limit_price = newPrice;
        }
      }

      // Clear edit state for this order
      if (inlineEditingOrders[orderId]) {
        if (field === 'all') {
          delete inlineEditingOrders[orderId];
        } else {
          delete inlineEditingOrders[orderId][field];
          if (!inlineEditingOrders[orderId].qty && !inlineEditingOrders[orderId].price) {
            delete inlineEditingOrders[orderId];
          }
        }
      }

      renderOpenOrdersTable();
      setTimeout(() => fetchLiveOrders(true), 400);
    } else {
      const errMsg = res?.error || 'Failed to edit order.';
      if (typeof showToast === 'function') showToast(`Edit failed: ${errMsg}`, 'error');
      if (btnSave) {
        btnSave.disabled = false;
        btnSave.textContent = '✓';
      }
    }
  } catch (err) {
    if (typeof showToast === 'function') showToast(`Error: ${err.message}`, 'error');
    if (btnSave) {
      btnSave.disabled = false;
      btnSave.textContent = '✓';
    }
  }
}
