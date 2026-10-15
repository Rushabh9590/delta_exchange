/**
 * ==============================================================================
 * DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CONTROLLER
 * Live WebSocket Tick-by-Tick Evaluation, Real-time Subscriptions, 1-Click Multi-Leg
 * ==============================================================================
 */

let mmCurrentUnderlying = 'BTC';
let mmSelectedExpiry = '';
let mmLastData = null;
let mmOnlyQualified = false;
let mmSearchTerm = '';
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
 * Fetches initial contract definitions for underlying and expiry,
 * and immediately subscribes all option contracts to the live WebSocket stream.
 */
async function loadMarketMakingStrikes(underlying = null, expiry = null) {
  if (underlying) {
    const cleanAsset = underlying.toUpperCase();
    mmCurrentUnderlying = cleanAsset === 'ETH' ? 'ETH' : 'BTC';
  } else if (mmCurrentUnderlying !== 'ETH') {
    mmCurrentUnderlying = 'BTC';
  }
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
    tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--text-muted);"><span class="spinner"></span> Loading & subscribing to live WebSocket options stream for ${mmCurrentUnderlying}...</td></tr>`;
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

      // Subscribe all option symbols to the Delta WebSocket tick stream
      subscribeAllMmOptionSymbols(data.all_strikes || []);
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
 * Subscribes all option contracts for current expiry to the live WebSocket stream
 */
function subscribeAllMmOptionSymbols(strikesList) {
  if (!strikesList || strikesList.length === 0) return;
  const symbolsToSub = [];

  strikesList.forEach(item => {
    if (item.symbol) {
      symbolsToSub.push(item.symbol.toUpperCase());
    }
  });

  if (symbolsToSub.length > 0 && typeof addSubscribedSymbols === 'function') {
    addSubscribedSymbols(symbolsToSub);
    console.log(`[MM WS SUB] Subscribed to ${symbolsToSub.length} option symbols for ${mmCurrentUnderlying} ${mmSelectedExpiry}`);
  }
}

/**
 * Populates expiration dropdown
 */
function populateMmExpiries(expiries, selected) {
  const sel = document.getElementById('mmSelExpiry');
  if (!sel) return;

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
 * Returns user criteria thresholds from input elements
 */
function getActiveMmCriteria() {
  const minBidVal = parseFloat(document.getElementById('mmInpMinBid')?.value);
  const minTicksVal = parseFloat(document.getElementById('mmInpMinTicks')?.value);
  const minEdgeVal = parseFloat(document.getElementById('mmInpMinEdge')?.value);

  return {
    min_bid_price: !isNaN(minBidVal) ? minBidVal : 0.3,
    min_spread_ticks: !isNaN(minTicksVal) ? minTicksVal : 5.0,
    min_net_edge_pct: !isNaN(minEdgeVal) ? minEdgeVal : 50.0
  };
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
          <div style="font-size: 0.78rem;">Try lowering the Min Net Edge % or Min Spread Ticks, or uncheck "Show Only Qualified".</div>
        </td>
      </tr>
    `;
    return;
  }

  let html = '';
  filtered.forEach(item => {
    html += buildMmRowHtml(item);
  });

  tbody.innerHTML = html;
}

/**
 * Constructs HTML string for a single strike row
 */
function buildMmRowHtml(item) {
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

  return `
    <tr id="mmRow_${item.symbol}" class="${rowClass}">
      <td class="text-left font-mono" style="font-weight: 600;">${item.symbol}</td>
      <td class="text-center"><span class="mm-type-badge ${typeClass}">${badgeText}</span></td>
      <td class="text-right" style="font-weight: 700; color: ${item.is_atm ? 'var(--yellow)' : 'inherit'};">${item.strike}${item.is_atm ? ' (ATM)' : ''}</td>
      <td class="text-right text-green" id="mmBid_${item.symbol}">${bid}</td>
      <td class="text-right text-red" id="mmAsk_${item.symbol}">${ask}</td>
      <td class="text-right" id="mmSpread_${item.symbol}" style="font-weight: 600;">${spread}</td>
      <td class="text-right text-muted">${tick}</td>
      <td class="text-right ticks-highlight" id="mmTicks_${item.symbol}">${ticks}</td>
      <td class="text-right" id="mmMid_${item.symbol}">${mid}</td>
      <td class="text-right" id="mmEdge_${item.symbol}" style="font-weight: 600;">${netEdge}</td>
      <td class="text-right ${edgeClass}" id="mmEdgePct_${item.symbol}" style="font-size: 0.88rem;">${edgePct}</td>
      <td class="text-center" id="mmStatus_${item.symbol}">${statusBadge}</td>
      <td class="text-center">
        <button type="button" class="mm-btn-studio" onclick="addMmStrikeToStudio('${item.symbol}', '${isCall ? 'buy' : 'sell'}', '${isCall ? 'call' : 'put'}', ${item.strike}, '${item.expiry}', ${item.bid || item.ask || 0})">
          <span>+ Trade</span>
        </button>
      </td>
    </tr>
  `;
}

/**
 * REAL-TIME WEBSOCKET TICK DISPATCHER (Tick-by-Tick)
 * Handles incoming WebSocket ticks from Delta Exchange and recalculates MM criteria instantaneously
 */
function handleMmStrikesTickUpdate(data) {
  if (!data || !mmLastData || !mmLastData.all_strikes) return;

  const sym = (data.symbol || '').toUpperCase();
  if (!sym) return;

  // Check if tick belongs to our current active underlying/expiry
  const item = mmLastData.all_strikes.find(x => x.symbol.toUpperCase() === sym);
  if (!item) return;

  const quotes = data.quotes || {};
  const rawBid = quotes.best_bid !== undefined ? quotes.best_bid : data.bid;
  const rawAsk = quotes.best_ask !== undefined ? quotes.best_ask : data.ask;
  const rawLtp = data.close !== undefined ? data.close : data.ltp;
  const rawMark = data.mark_price;

  let changed = false;
  let isBidHigher = false;

  if (rawBid !== undefined && rawBid !== null) {
    const newBid = parseFloat(rawBid);
    if (!isNaN(newBid) && newBid !== item.bid) {
      isBidHigher = newBid > (item.bid || 0);
      item.bid = newBid;
      changed = true;
    }
  }

  if (rawAsk !== undefined && rawAsk !== null) {
    const newAsk = parseFloat(rawAsk);
    if (!isNaN(newAsk) && newAsk !== item.ask) {
      item.ask = newAsk;
      changed = true;
    }
  }

  if (rawLtp !== undefined && rawLtp !== null) {
    const newLtp = parseFloat(rawLtp);
    if (!isNaN(newLtp)) item.ltp = newLtp;
  }

  if (rawMark !== undefined && rawMark !== null) {
    const newMark = parseFloat(rawMark);
    if (!isNaN(newMark)) item.mark_price = newMark;
  }

  if (!changed) return;

  // Recalculate MM criteria in real-time
  const criteria = getActiveMmCriteria();
  const tick = item.tick_size || 0.1;
  const bid = item.bid;
  const ask = item.ask;

  if (bid > 0 && ask > 0 && ask >= bid) {
    item.spread = ask - bid;
    item.mid_price = (bid + ask) / 2.0;
    item.net_edge = item.spread - (2.0 * tick);
    item.net_edge_pct = item.mid_price > 0 ? (item.net_edge / item.mid_price * 100.0) : 0.0;
    item.spread_ticks = tick > 0 ? (item.spread / tick) : 0.0;

    const reasons = [];
    if (bid < criteria.min_bid_price) reasons.append ? reasons.push(`Bid ${bid.toFixed(2)} < ${criteria.min_bid_price}`) : reasons.push(`Bid < ${criteria.min_bid_price}`);
    if (item.spread_ticks < criteria.min_spread_ticks) reasons.push(`Spread Ticks < ${criteria.min_spread_ticks}`);
    if (item.net_edge_pct < criteria.min_net_edge_pct) reasons.push(`Net Edge % < ${criteria.min_net_edge_pct}%`);

    item.qualified = (reasons.length === 0);
    item.reasons_failed = reasons;
  } else {
    item.spread = 0;
    item.mid_price = 0;
    item.net_edge = 0;
    item.net_edge_pct = 0;
    item.spread_ticks = 0;
    item.qualified = false;
    item.reasons_failed = ['No valid bid/ask quotes'];
  }

  // Update mmLastData.qualified_strikes list
  mmLastData.qualified_strikes = mmLastData.all_strikes
    .filter(x => x.qualified)
    .sort((a, b) => b.net_edge_pct - a.net_edge_pct || b.spread_ticks - a.spread_ticks);

  // Update Summary Metrics
  const qualCount = mmLastData.qualified_strikes.length;
  const maxTicks = Math.max(0, ...mmLastData.qualified_strikes.map(x => x.spread_ticks || 0));
  const maxEdge = Math.max(0, ...mmLastData.qualified_strikes.map(x => x.net_edge_pct || 0));

  const elQual = document.getElementById('mmStatQualified');
  if (elQual) {
    elQual.textContent = qualCount;
    elQual.className = `mm-stat-val ${qualCount > 0 ? 'text-green' : 'text-muted'}`;
  }

  const elMaxTicks = document.getElementById('mmStatMaxTicks');
  if (elMaxTicks) elMaxTicks.textContent = maxTicks.toFixed(1);

  const elMaxEdge = document.getElementById('mmStatMaxEdge');
  if (elMaxEdge) elMaxEdge.textContent = `${maxEdge.toFixed(1)}%`;

  const elDrawerBadge = document.getElementById('drawerMmBadge');
  if (elDrawerBadge) elDrawerBadge.textContent = `${qualCount} Opps`;

  // Update DOM Row in-place for 0-latency microsecond rendering
  const elRow = document.getElementById(`mmRow_${sym}`);
  if (elRow) {
    if (mmOnlyQualified && !item.qualified) {
      elRow.style.display = 'none';
      return;
    }
    elRow.style.display = '';

    const elBid = document.getElementById(`mmBid_${sym}`);
    if (elBid) elBid.textContent = item.bid > 0 ? item.bid.toFixed(2) : '--';

    const elAsk = document.getElementById(`mmAsk_${sym}`);
    if (elAsk) elAsk.textContent = item.ask > 0 ? item.ask.toFixed(2) : '--';

    const elSpread = document.getElementById(`mmSpread_${sym}`);
    if (elSpread) elSpread.textContent = item.spread > 0 ? item.spread.toFixed(2) : '--';

    const elTicks = document.getElementById(`mmTicks_${sym}`);
    if (elTicks) elTicks.textContent = item.spread_ticks.toFixed(1);

    const elMid = document.getElementById(`mmMid_${sym}`);
    if (elMid) elMid.textContent = item.mid_price > 0 ? item.mid_price.toFixed(2) : '--';

    const elEdge = document.getElementById(`mmEdge_${sym}`);
    if (elEdge) elEdge.textContent = item.net_edge.toFixed(2);

    const elEdgePct = document.getElementById(`mmEdgePct_${sym}`);
    if (elEdgePct) {
      elEdgePct.textContent = `${item.net_edge_pct.toFixed(1)}%`;
      elEdgePct.className = `text-right ${item.net_edge_pct >= 50 ? 'edge-highlight-high' : (item.net_edge_pct >= 25 ? 'edge-highlight-med' : '')}`;
    }

    const elStatus = document.getElementById(`mmStatus_${sym}`);
    if (elStatus) {
      elStatus.innerHTML = item.qualified
        ? `<span class="mm-status-badge mm-status-qual" title="Meets all selection criteria">✔ QUALIFIED</span>`
        : `<span class="mm-status-badge mm-status-fail" title="${(item.reasons_failed || []).join('; ') || 'Criteria not met'}">NOT MET</span>`;
    }

    elRow.className = item.qualified ? 'row-qualified' : '';

    // Micro-animation flash indicating live tick received
    const flashClass = isBidHigher ? 'tick-flash-up' : 'tick-flash-down';
    elRow.classList.add(flashClass);
    setTimeout(() => {
      elRow.classList.remove(flashClass);
    }, 180);
  }
}

/**
 * Handles switching underlying asset
 */
function onMmUnderlyingChange(asset) {
  const cleanAsset = (asset || 'BTC').toUpperCase();
  mmCurrentUnderlying = cleanAsset === 'ETH' ? 'ETH' : 'BTC';
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
    if (!mmLastData || !mmLastData.all_strikes) {
      loadMarketMakingStrikes(mmCurrentUnderlying, mmSelectedExpiry);
      return;
    }

    // Instantly re-evaluate all strikes in memory with new criteria
    const criteria = getActiveMmCriteria();
    mmLastData.all_strikes.forEach(item => {
      const bid = item.bid || 0;
      const ticks = item.spread_ticks || 0;
      const edge = item.net_edge_pct || 0;

      const reasons = [];
      if (bid < criteria.min_bid_price) reasons.push(`Bid < ${criteria.min_bid_price}`);
      if (ticks < criteria.min_spread_ticks) reasons.push(`Spread Ticks < ${criteria.min_spread_ticks}`);
      if (edge < criteria.min_net_edge_pct) reasons.push(`Net Edge % < ${criteria.min_net_edge_pct}%`);

      item.qualified = (bid > 0 && item.ask > 0 && reasons.length === 0);
      item.reasons_failed = reasons;
    });

    mmLastData.qualified_strikes = mmLastData.all_strikes
      .filter(x => x.qualified)
      .sort((a, b) => b.net_edge_pct - a.net_edge_pct || b.spread_ticks - a.spread_ticks);

    mmLastData.summary = {
      total_scanned: mmLastData.all_strikes.length,
      total_qualified: mmLastData.qualified_strikes.length,
      max_spread_ticks: Math.max(0, ...mmLastData.qualified_strikes.map(x => x.spread_ticks || 0)),
      max_net_edge_pct: Math.max(0, ...mmLastData.qualified_strikes.map(x => x.net_edge_pct || 0))
    };

    renderMarketMakingView(mmLastData);
  }, 250);
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
