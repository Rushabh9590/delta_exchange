/**
 * ==============================================================================
 * DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CONTROLLER
 * Live WebSocket Tick-by-Tick Evaluation, Real-time Subscriptions, 1-Click Multi-Leg
 * ==============================================================================
 */

let mmCurrentUnderlying = 'BTC';
let mmSelectedExpiry = '';
let mmLastData = null;
let mmOnlyQualified = true;
let mmSearchTerm = '';
let mmIsLoading = false;

/**
 * Initializes the Market Making Strike Selection module on page load
 */
async function initMarketMakingModule() {
  const chk = document.getElementById('mmChkOnlyQual');
  if (chk) chk.checked = true;
  mmOnlyQualified = true;

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
async function loadMarketMakingStrikes(underlying = null, expiry = null, isBackground = false) {
  if (isBackground && mmIsLoading) return;
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
  if (tbody && !mmLastData && !isBackground) {
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
 * Toggles whether only qualified strikes are displayed
 */
function toggleMmOnlyQualified(isChecked) {
  mmOnlyQualified = !!isChecked;
  renderMmTableRows();
}

/**
 * Renders individual table rows based on filters and search
 */
function renderMmTableRows() {
  if (!mmLastData) return;

  const tbody = document.getElementById('mmTbody');
  if (!tbody) return;

  const chk = document.getElementById('mmChkOnlyQual');
  if (chk) mmOnlyQualified = chk.checked;

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
  const badgeText = isCall ? 'CALL' : 'PUT';
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
      <td class="col-symbol text-left font-mono" style="font-weight: 600; font-size: 0.76rem; letter-spacing: -0.2px;">${item.symbol}</td>
      <td class="col-type text-center"><span class="mm-type-badge ${typeClass}">${badgeText}</span></td>
      <td class="col-strike text-right" style="font-weight: 700; color: ${item.is_atm ? 'var(--yellow)' : 'inherit'};">${item.strike}${item.is_atm ? ' (ATM)' : ''}</td>
      <td class="col-bid text-right text-green" id="mmBid_${item.symbol}">${bid}</td>
      <td class="col-ask text-right text-red" id="mmAsk_${item.symbol}">${ask}</td>
      <td class="col-spread text-right" id="mmSpread_${item.symbol}" style="font-weight: 600;">${spread}</td>
      <td class="col-tick text-right text-muted">${tick}</td>
      <td class="col-ticks text-right ticks-highlight" id="mmTicks_${item.symbol}">${ticks}</td>
      <td class="col-mid text-right" id="mmMid_${item.symbol}">${mid}</td>
      <td class="col-edge text-right" id="mmEdge_${item.symbol}" style="font-weight: 600;">${netEdge}</td>
      <td class="col-edgepct text-right ${edgeClass}" id="mmEdgePct_${item.symbol}" style="font-size: 0.88rem;">${edgePct}</td>
      <td class="col-status text-center" id="mmStatus_${item.symbol}">${statusBadge}</td>
      <td class="col-action text-center" style="white-space: nowrap;">
        <button type="button" class="mm-btn-1lot" onclick="open1LotMmModalBySymbol('${item.symbol}')" title="⚡ Launch 1-Lot MM Cycle (No Stop-Loss, No Timeout • Cost-to-Cost or Wait)">
          <span>⚡ 1-Lot MM</span>
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
  const item = mmLastData.all_strikes.find(x => x && x.symbol && x.symbol.toUpperCase() === sym);
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
  const prevQualified = item.qualified === true;
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
    if (bid < criteria.min_bid_price) reasons.push(`Bid ${bid.toFixed(2)} < ${criteria.min_bid_price}`);
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

  const isNowQualified = item.qualified === true;
  const qualStatusChanged = (prevQualified !== isNowQualified);

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

  // Sync checkbox state
  const chk = document.getElementById('mmChkOnlyQual');
  if (chk) {
    mmOnlyQualified = chk.checked;
  }

  const elRow = document.getElementById(`mmRow_${sym}`);

  // Auto-render rows dynamically when qualification status changes
  if (mmOnlyQualified) {
    // 1. If contract is qualified but missing from table DOM or just qualified, render immediately!
    if (isNowQualified && (!elRow || qualStatusChanged)) {
      renderMmTableRows();
      const newRow = document.getElementById(`mmRow_${sym}`);
      if (newRow) {
        const flashClass = isBidHigher ? 'tick-flash-up' : 'tick-flash-down';
        newRow.classList.add(flashClass);
        setTimeout(() => newRow.classList.remove(flashClass), 500);
      }
      return;
    }

    // 2. If contract disqualified and was in the table:
    if (!isNowQualified && (elRow || qualStatusChanged)) {
      renderMmTableRows();
      return;
    }

    // 3. If unqualified and not displayed, done
    if (!isNowQualified) {
      return;
    }
  } else {
    // When showing ALL strikes:
    if (!elRow) {
      renderMmTableRows();
      return;
    }
  }

  // Update existing row in-place for 0-latency microsecond rendering
  if (elRow) {
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

// ==============================================================================
// 1-LOT MARKET MAKING TRADE CONTROLLER & MODAL
// ==============================================================================

let mmActiveTradesCache = [];
let mmSelectedTradeItem = null;
let mmTradePollTimer = null;

/**
 * Opens 1-Lot MM Trade Setup Modal for a selected strike
 */
function open1LotMmModalBySymbol(symbol) {
  if (!symbol) return;
  const sym = symbol.toUpperCase();
  let item = null;

  if (mmLastData && mmLastData.all_strikes) {
    item = mmLastData.all_strikes.find(x => x && x.symbol && x.symbol.toUpperCase() === sym);
  }

  if (!item) {
    if (typeof showToast === 'function') showToast(`Could not find details for ${sym}`, 'error');
    return;
  }

  mmSelectedTradeItem = item;

  const modal = document.getElementById('mmTradeModal');
  const elSymbol = document.getElementById('mmModalSymbol');
  const elAccount = document.getElementById('mmModalAccount');
  const elBid = document.getElementById('mmModalBid');
  const elAsk = document.getElementById('mmModalAsk');
  const inpBuy = document.getElementById('mmModalInpBuyPrice');
  const inpSell = document.getElementById('mmModalInpSellPrice');

  if (elSymbol) elSymbol.textContent = item.symbol;
  if (elAccount) {
    const activeAcct = (window.currentActiveAccount || 'sub').toUpperCase();
    elAccount.textContent = activeAcct;
  }

  const tick = item.tick_size || 0.1;
  const bidVal = item.bid !== null && item.bid > 0 ? item.bid : 0;
  const askVal = item.ask !== null && item.ask > 0 ? item.ask : 0;

  if (elBid) elBid.textContent = bidVal > 0 ? `$${bidVal.toFixed(2)}` : '--';
  if (elAsk) elAsk.textContent = askVal > 0 ? `$${askVal.toFixed(2)}` : '--';

  // Calculate default recommended entry price: Best Bid + 1 Tick (if below ask), else Best Bid
  let recBuy = bidVal > 0 ? bidVal : 1.0;
  if (askVal > 0 && (bidVal + tick) < askVal) {
    recBuy = bidVal + tick;
  }
  recBuy = Math.round(recBuy / tick) * tick;

  // Calculate default recommended target exit: Best Ask - 1 Tick
  let recSell = askVal > 0 ? (askVal - tick) : (recBuy + 1.0);
  if (recSell <= recBuy) recSell = recBuy + tick;
  recSell = Math.round(recSell / tick) * tick;

  if (inpBuy) inpBuy.value = recBuy.toFixed(2);
  if (inpSell) inpSell.value = recSell.toFixed(2);

  if (modal) {
    modal.style.display = 'flex';
  }
}

function close1LotMmModal() {
  const modal = document.getElementById('mmTradeModal');
  if (modal) modal.style.display = 'none';
  mmSelectedTradeItem = null;
}

/**
 * Launches the 1-Lot MM Trade Cycle on Delta Exchange
 */
async function confirmAndLaunch1LotTrade() {
  if (!mmSelectedTradeItem) {
    showToast('No strike selected', 'error');
    return;
  }

  const btn = document.getElementById('btnLaunchMmTrade');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🚀 Placing Order...';
  }

  const inpBuy = document.getElementById('mmModalInpBuyPrice');
  const inpSell = document.getElementById('mmModalInpSellPrice');

  const buyPrice = inpBuy && inpBuy.value ? parseFloat(inpBuy.value) : null;
  const sellPrice = inpSell && inpSell.value ? parseFloat(inpSell.value) : null;
  const activeAcct = window.currentActiveAccount || 'sub';

  const selectedSymbol = mmSelectedTradeItem ? mmSelectedTradeItem.symbol : '';

  try {
    const payload = {
      symbol: selectedSymbol,
      size: 1,
      entry_price: buyPrice,
      target_exit_price: sellPrice,
      account: activeAcct
    };

    const res = await ApiService.startMMTrade(payload);

    if (res && res.success) {
      close1LotMmModal();
      if (typeof showToast === 'function') {
        showToast(`⚡ 1-Lot MM Cycle launched for ${selectedSymbol} on [${activeAcct.toUpperCase()}]!`, 'success');
      }
      pollActiveMmTrades();
    } else {
      if (typeof showToast === 'function') {
        showToast(`Trade failed: ${res?.error || 'Unknown error'}`, 'error');
      }
    }
  } catch (err) {
    if (typeof showToast === 'function') {
      showToast(`Error: ${err.message}`, 'error');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '⚡ Launch 1-Lot MM Order';
    }
  }
}

/**
 * Polls active MM trades from server every 2.5s
 */
async function pollActiveMmTrades() {
  try {
    const res = await ApiService.getActiveMMTrades();
    if (res && res.success && Array.isArray(res.trades)) {
      mmActiveTradesCache = res.trades;
      renderActiveMmTrades(res.trades);
    }
  } catch (e) {
    // Quietly ignore background poll error
  }
}

/**
 * Renders active MM trade cards in dashboard
 */
function renderActiveMmTrades(trades) {
  const panel = document.getElementById('mmActiveTradesPanel');
  const listEl = document.getElementById('mmActiveTradesList');
  if (!panel || !listEl) return;

  // Filter ONLY active running trades (exclude completed, cancelled, closed)
  const activeTrades = (trades || []).filter(t => 
    t && t.is_running && !['COMPLETED_PROFIT', 'COMPLETED_COST', 'CANCELLED', 'ERROR'].includes(t.state)
  );

  if (activeTrades.length === 0) {
    panel.style.display = 'none';
    listEl.innerHTML = '';
    return;
  }

  panel.style.display = 'flex';

  let html = '';
  activeTrades.forEach(t => {
    let stateBadge = '';
    let cardClass = 'mm-trade-card';

    if (t.state === 'BUY_OPEN' || t.state === 'PLACING_BUY') {
      stateBadge = `<span class="mm-trade-badge buying">🎯 BUY PEG: #1 Bid @ $${t.buy_limit_price || '--'}</span>`;
    } else if (t.state === 'BUY_FILLED') {
      stateBadge = `<span class="mm-trade-badge selling">BUY FILLED @ $${t.buy_fill_price || '--'}</span>`;
    } else if (t.state === 'SELL_OPEN' || t.state === 'PLACING_SELL') {
      stateBadge = `<span class="mm-trade-badge selling">🚀 SELL PEG: Float @ $${t.sell_limit_price || '--'}</span>`;
    } else if (t.state === 'WAITING_COST_TO_COST') {
      stateBadge = `<span class="mm-trade-badge cost-wait">🛡️ COST FLOOR: Breakeven @ $${t.cost_to_cost_price || '--'}</span>`;
      cardClass += ' waiting-cost';
    } else if (t.state === 'COMPLETED_PROFIT') {
      stateBadge = `<span class="mm-trade-badge done">PROFIT EXIT +$${t.realized_pnl.toFixed(4)}</span>`;
    } else if (t.state === 'COMPLETED_COST') {
      stateBadge = `<span class="mm-trade-badge done">COST-TO-COST BREAKEVEN</span>`;
    } else if (t.state === 'CANCELLED') {
      stateBadge = `<span class="mm-trade-badge" style="background: rgba(255,255,255,0.1); color: #94a3b8;">CANCELLED</span>`;
    } else {
      stateBadge = `<span class="mm-trade-badge" style="background: rgba(239,68,68,0.2); color: #ef4444;">${t.state}</span>`;
    }

    const curBidStr = t.current_bid ? `$${t.current_bid.toFixed(2)}` : '--';
    const curAskStr = t.current_ask ? `$${t.current_ask.toFixed(2)}` : '--';
    const costStr = t.buy_fill_price ? `$${t.buy_fill_price.toFixed(2)}` : 'Pending Buy';
    const activeOrderPrice = t.sell_limit_price || t.buy_limit_price || 0;

    html += `
      <div class="${cardClass} mm-trade-strip" id="card_${t.trade_id}">
        <div class="mm-strip-left">
          <span class="mm-trade-symbol">${t.symbol}</span>
          ${stateBadge}
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Account:</span> <strong>${(t.account || 'main').toUpperCase()} (1 Lot)</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Cost:</span> <strong style="color: #38bdf8;">${costStr}</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Bid / Ask:</span> <span class="text-green">${curBidStr}</span> / <span class="text-red">${curAskStr}</span></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">PnL:</span> <strong class="${t.realized_pnl >= 0 ? 'text-green' : 'text-red'}">$${t.realized_pnl.toFixed(4)}</strong></span>
          <span class="mm-strip-chip mm-strip-status-chip" title="${t.status_message}"><span style="color: #94a3b8;">Status:</span> <span class="mm-status-text">${t.status_message}</span></span>
        </div>

        ${t.is_running ? `
          <div class="mm-strip-actions">
            <span style="font-size: 0.72rem; color: #94a3b8;">Price:</span>
            <input type="number" step="${t.tick_size || 0.1}" class="mm-upload-input" id="inpUpload_${t.trade_id}" value="${activeOrderPrice > 0 ? activeOrderPrice.toFixed(2) : ''}" placeholder="Price" />
            <button type="button" class="mm-btn-upload" onclick="uploadActiveTradePrice('${t.trade_id}')">Update</button>
            <button type="button" class="mm-btn-cancel" onclick="cancelActiveTrade('${t.trade_id}')">Stop</button>
          </div>
        ` : ''}
      </div>
    `;
  });

  listEl.innerHTML = html;
}

/**
 * Uploads/updates the limit price on an active MM trade
 */
async function uploadActiveTradePrice(tradeId) {
  const inp = document.getElementById(`inpUpload_${tradeId}`);
  if (!inp || !inp.value) {
    showToast('Enter valid price to upload', 'error');
    return;
  }
  const price = parseFloat(inp.value);
  try {
    const res = await ApiService.uploadMMPrice(tradeId, price);
    if (res && res.success) {
      showToast(`Uploaded new limit price $${price} for trade`, 'success');
      pollActiveMmTrades();
    } else {
      showToast(`Upload failed: ${res?.error || 'Unknown error'}`, 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

/**
 * Cancels an active trade
 */
async function cancelActiveTrade(tradeId) {
  if (!confirm('Cancel this active trade order?')) return;
  try {
    const res = await ApiService.cancelMMTrade(tradeId);
    if (res && res.success) {
      showToast('Trade stop requested', 'normal');
      pollActiveMmTrades();
    } else {
      showToast(`Cancel failed: ${res?.error || 'Error'}`, 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

// Auto-initialize config when script loads
document.addEventListener('DOMContentLoaded', () => {
  initMarketMakingModule();
  pollActiveMmTrades();
  if (!mmTradePollTimer) {
    mmTradePollTimer = setInterval(pollActiveMmTrades, 2500);
  }
});

