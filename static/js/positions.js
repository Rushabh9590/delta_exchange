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

  // Sync active class on filter tab buttons
  document.querySelectorAll('.filter-tabs .tab-btn').forEach(btn => {
    const fnAttr = btn.getAttribute('onclick') || '';
    btn.classList.toggle('active', fnAttr.includes(`'${currentFilter}'`));
  });

  const searchVal = (document.getElementById('searchInput')?.value || '').toLowerCase();

  const filtered = currentPositions.filter(pos => {
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

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    emptyState.style.display = 'flex';
    return;
  }

  emptyState.style.display = 'none';

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

  const tfoot = document.getElementById('positionsTfoot');
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
      fetchDashboardData(true);
    } else {
      showToast(`Failed to square off ${symbol}: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Square off error: ${err.message}`, 'error');
  }
}
