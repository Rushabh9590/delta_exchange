/**
 * ==============================================================================
 * DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CONTROLLER
 * Criteria evaluation, live filtering, statistics aggregation, 1-click studio send
 * ==============================================================================
 */

let mmCurrentUnderlying = 'BTC';
let mmSelectedExpiry = '';
let mmLastData = null;
let mmOnlyQualified = false;
let mmSearchTerm = '';
let mmAutoRefreshTimer = null;
let mmIsLoading = false;

/**
 * Initializes the Market Making Strike Selection module on page load
 */
async function initMarketMakingModule() {
  try {
    const res = await ApiService.getMarketMakingConfig();
    if (res && res.success && res.config) {
      const cfg = res.config;
      if (cfg.min_bid_price !== undefined) {
        const inp = document.getElementById('mmInpMinBid');
        if (inp && !inp.value) inp.value = cfg.min_bid_price;
      }
      if (cfg.min_spread_ticks !== undefined) {
        const inp = document.getElementById('mmInpMinTicks');
        if (inp && !inp.value) inp.value = cfg.min_spread_ticks;
      }
      if (cfg.min_net_edge_pct !== undefined) {
        const inp = document.getElementById('mmInpMinEdge');
        if (inp && !inp.value) inp.value = cfg.min_net_edge_pct;
      }
      if (cfg.underlying && !mmCurrentUnderlying) {
        mmCurrentUnderlying = cfg.underlying;
      }
    }
  } catch (e) {
    console.warn('[MM] Could not load default config:', e);
  }
}

/**
 * Fetches and evaluates market making strikes from the server
 */
async function loadMarketMakingStrikes(underlying = null, expiry = null) {
  if (underlying) mmCurrentUnderlying = underlying.toUpperCase();
  if (expiry !== null) mmSelectedExpiry = expiry;

  const minBid = document.getElementById('mmInpMinBid')?.value;
  const minTicks = document.getElementById('mmInpMinTicks')?.value;
  const minEdge = document.getElementById('mmInpMinEdge')?.value;

  // Visual button states
  document.querySelectorAll('.mm-asset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-asset') === mmCurrentUnderlying);
  });

  const tbody = document.getElementById('mmTbody');
  if (tbody && !mmLastData) {
    tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--text-muted);"><span class="spinner"></span> Evaluating Market Making strikes for ${mmCurrentUnderlying}...</td></tr>`;
  }

  mmIsLoading = true;
  try {
    const data = await ApiService.getMarketMakingStrikes({
      underlying: mmCurrentUnderlying,
      expiry: mmSelectedExpiry,
      min_bid: minBid,
      min_ticks: minTicks,
      min_edge: minEdge
    });

    if (data && data.success) {
      mmLastData = data;
      mmSelectedExpiry = data.selected_expiry || mmSelectedExpiry;
      populateMmExpiries(data.expiries || [], mmSelectedExpiry);
      renderMarketMakingView(data);
    } else {
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--red);">Failed to load strike data: ${data?.error || 'Unknown error'}</td></tr>`;
      }
    }
  } catch (err) {
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--red);">Error connecting to API: ${err.message}</td></tr>`;
    }
  } finally {
    mmIsLoading = false;
  }
}

/**
 * Populates expiration dropdown
 */
function populateMmExpiries(expiries, selected) {
  const sel = document.getElementById('mmSelExpiry');
  if (!sel) return;

  const currentVal = sel.value;
  sel.innerHTML = '';

  if (!expiries || expiries.length === 0) {
    sel.innerHTML = '<option value="">No active expiries</option>';
    return;
  }

  expiries.forEach(exp => {
    const opt = document.createElement('option');
    opt.value = exp.expiry;
    opt.textContent = `${exp.label || exp.expiry} (${exp.total_strikes || 0} strikes)`;
    if (exp.expiry === selected) {
      opt.selected = true;
    }
    sel.appendChild(opt);
  });

  if (!sel.value && expiries.length > 0) {
    sel.value = expiries[0].expiry;
    mmSelectedExpiry = expiries[0].expiry;
  }
}

/**
 * Renders the Market Making table and summary metric statistics
 */
function renderMarketMakingView(data) {
  if (!data) return;

  const summary = data.summary || {};
  const criteria = data.criteria || {};

  // Update Summary Metric Cards
  const elScanned = document.getElementById('mmStatScanned');
  if (elScanned) elScanned.textContent = summary.total_scanned || 0;

  const elQual = document.getElementById('mmStatQualified');
  if (elQual) {
    const count = summary.total_qualified || 0;
    elQual.textContent = count;
    elQual.className = `mm-stat-val ${count > 0 ? 'text-green' : 'text-muted'}`;
  }

  const elMaxTicks = document.getElementById('mmStatMaxTicks');
  if (elMaxTicks) {
    elMaxTicks.textContent = (summary.max_spread_ticks || 0).toFixed(1);
  }

  const elMaxEdge = document.getElementById('mmStatMaxEdge');
  if (elMaxEdge) {
    elMaxEdge.textContent = `${(summary.max_net_edge_pct || 0).toFixed(1)}%`;
  }

  // Update Spot & ATM Info Strip
  const elSpot = document.getElementById('mmSpotVal');
  if (elSpot) elSpot.textContent = `$${Number(data.spot_price || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const elAtm = document.getElementById('mmAtmVal');
  if (elAtm) elAtm.textContent = data.atm_strike ? `${data.atm_strike}` : '--';

  // Drawer Badge
  const elDrawerBadge = document.getElementById('drawerMmBadge');
  if (elDrawerBadge) {
    const qCount = summary.total_qualified || 0;
    elDrawerBadge.textContent = `${qCount} Opps`;
  }

  // Render Table
  renderMmTableRows();
}

/**
 * Renders individual table rows based on filters and search
 */
function renderMmTableRows() {
  if (!mmLastData) return;

  const tbody = document.getElementById('mmTbody');
  if (!tbody) return;

  const list = mmOnlyQualified ? (mmLastData.qualified_strikes || []) : (mmLastData.all_strikes || []);

  // Apply Strike / Symbol Search Filter
  let filtered = list;
  if (mmSearchTerm && mmSearchTerm.trim()) {
    const term = mmSearchTerm.trim().toLowerCase();
    filtered = list.filter(item => {
      const sym = (item.symbol || '').toLowerCase();
      const st = String(item.strike || '');
      return sym.includes(term) || st.includes(term);
    });
  }

  if (filtered.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="13" style="text-align: center; padding: 40px; color: var(--text-muted);">
          <div style="font-size: 1rem; margin-bottom: 6px;">No market making strikes match the selected criteria.</div>
          <div style="font-size: 0.78rem;">Try lowering the Min Net Edge % or Min Spread Ticks, or uncheck "Only Qualified".</div>
        </td>
      </tr>
    `;
    return;
  }

  let html = '';
  filtered.forEach(item => {
    const isQual = item.qualified;
    const isCall = item.option_type === 'CALL';
    const typeClass = isCall ? 'mm-type-ce' : 'mm-type-pe';
    const badgeText = isCall ? 'CE (Call)' : 'PE (Put)';
    const rowClass = isQual ? 'row-qualified' : '';

    const bid = item.bid !== null && item.bid > 0 ? item.bid.toFixed(2) : '--';
    const ask = item.ask !== null && item.ask > 0 ? item.ask.toFixed(2) : '--';
    const spread = item.spread !== null && item.spread > 0 ? item.spread.toFixed(2) : '--';
    const tick = item.tick_size ? item.tick_size.toFixed(2) : '0.10';
    const ticks = item.spread_ticks !== null ? item.spread_ticks.toFixed(1) : '0.0';
    const mid = item.mid_price !== null && item.mid_price > 0 ? item.mid_price.toFixed(2) : '--';
    const netEdge = item.net_edge !== null ? item.net_edge.toFixed(2) : '--';
    const edgePct = item.net_edge_pct !== null ? `${item.net_edge_pct.toFixed(1)}%` : '0.0%';

    let edgeClass = '';
    if (item.net_edge_pct >= 50) edgeClass = 'edge-highlight-high';
    else if (item.net_edge_pct >= 25) edgeClass = 'edge-highlight-med';

    const statusBadge = isQual
      ? `<span class="mm-status-badge mm-status-qual" title="Meets all selection criteria">✔ QUALIFIED</span>`
      : `<span class="mm-status-badge mm-status-fail" title="${(item.reasons_failed || []).join('; ') || 'Criteria not met'}">NOT MET</span>`;

    html += `
      <tr class="${rowClass}">
        <td class="text-left font-mono" style="font-weight: 600;">${item.symbol}</td>
        <td class="text-center"><span class="mm-type-badge ${typeClass}">${badgeText}</span></td>
        <td class="text-right" style="font-weight: 700; color: ${item.is_atm ? 'var(--yellow)' : 'inherit'};">${item.strike}${item.is_atm ? ' (ATM)' : ''}</td>
        <td class="text-right text-green">${bid}</td>
        <td class="text-right text-red">${ask}</td>
        <td class="text-right" style="font-weight: 600;">${spread}</td>
        <td class="text-right text-muted">${tick}</td>
        <td class="text-right ticks-highlight">${ticks}</td>
        <td class="text-right">${mid}</td>
        <td class="text-right" style="font-weight: 600;">${netEdge}</td>
        <td class="text-right ${edgeClass}" style="font-size: 0.88rem;">${edgePct}</td>
        <td class="text-center">${statusBadge}</td>
        <td class="text-center">
          <button type="button" class="mm-btn-studio" onclick="addMmStrikeToStudio('${item.symbol}', '${isCall ? 'buy' : 'sell'}', '${isCall ? 'call' : 'put'}', ${item.strike}, '${item.expiry}', ${item.bid || item.ask || 0})">
            <span>+ Trade</span>
          </button>
        </td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

/**
 * Handles switching underlying asset
 */
function onMmUnderlyingChange(asset) {
  mmCurrentUnderlying = asset.toUpperCase();
  mmSelectedExpiry = '';
  loadMarketMakingStrikes(mmCurrentUnderlying, '');
}

/**
 * Handles dropdown expiry change
 */
function onMmExpiryChange(expiry) {
  mmSelectedExpiry = expiry;
  loadMarketMakingStrikes(mmCurrentUnderlying, mmSelectedExpiry);
}

/**
 * Handles changes to threshold filter inputs
 */
let mmFilterDebounce = null;
function onMmCriteriaChange() {
  clearTimeout(mmFilterDebounce);
  mmFilterDebounce = setTimeout(() => {
    loadMarketMakingStrikes(mmCurrentUnderlying, mmSelectedExpiry);
  }, 400);
}

/**
 * Toggles whether only qualified rows are displayed
 */
function toggleMmOnlyQualified(checked) {
  mmOnlyQualified = checked;
  renderMmTableRows();
}

/**
 * Searches strikes by symbol or strike price
 */
function onMmSearch(val) {
  mmSearchTerm = val;
  renderMmTableRows();
}

/**
 * Toggles periodic auto-refresh
 */
function toggleMmAutoRefresh(checked) {
  if (mmAutoRefreshTimer) {
    clearInterval(mmAutoRefreshTimer);
    mmAutoRefreshTimer = null;
  }
  if (checked) {
    mmAutoRefreshTimer = setInterval(() => {
      if (!document.hidden && activeNavTab === 'mm') {
        loadMarketMakingStrikes(mmCurrentUnderlying, mmSelectedExpiry);
      }
    }, 5000);
    if (typeof showToast === 'function') {
      showToast('Market Making live auto-refresh enabled (5s)', 'info');
    }
  } else {
    if (typeof showToast === 'function') {
      showToast('Market Making auto-refresh paused', 'info');
    }
  }
}

/**
 * 1-Click: Transfers selected market making option directly into Multi-Leg Studio
 */
function addMmStrikeToStudio(symbol, side, optionType, strike, expiry, price) {
  if (typeof addNewLegRow !== 'function' && typeof legRows === 'undefined') {
    if (typeof showToast === 'function') {
      showToast('Multi-Leg Trading Studio is not initialized', 'error');
    }
    return;
  }

  // Switch to studio tab
  if (typeof switchNavTab === 'function') {
    switchNavTab('studio');
  }

  const underlyingSelect = document.getElementById('selUnderlying');
  if (underlyingSelect) {
    underlyingSelect.value = mmCurrentUnderlying;
  }

  const defaultSizeInp = document.getElementById('inpDefaultSize');
  const sz = defaultSizeInp ? (parseInt(defaultSizeInp.value) || 1) : 1;

  if (typeof legRows !== 'undefined' && Array.isArray(legRows)) {
    legRows.push({
      side: side.toLowerCase(),
      option_type: optionType.toLowerCase(),
      strike: strike,
      symbol: symbol,
      order_type: 'limit',
      size: sz,
      limit_price: price > 0 ? price : '',
      use_full_margin: false
    });

    if (typeof renderLegRows === 'function') {
      renderLegRows();
    }
    if (typeof updateStrategyMarginRequirement === 'function') {
      updateStrategyMarginRequirement();
    }
    if (typeof showToast === 'function') {
      showToast(`⚡ Added ${symbol} to Multi-Leg Studio (${side.toUpperCase()} ${optionType.toUpperCase()})`, 'success');
    }
  }
}

// Auto-initialize config when script loads
document.addEventListener('DOMContentLoaded', () => {
  initMarketMakingModule();
});
