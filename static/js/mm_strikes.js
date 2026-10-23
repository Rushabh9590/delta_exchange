/**
 * ==============================================================================
 * DELTA EXCHANGE - MARKET MAKING STRIKE SELECTION CONTROLLER
 * Live WebSocket Tick-by-Tick Evaluation, Real-time Subscriptions, 1-Click Multi-Leg
 * ==============================================================================
 */

let mmCurrentUnderlying = 'ALL';
let mmSelectedExpiry = '';
let mmLastData = null;
let mmOnlyQualified = true;
let mmSearchTerm = '';
let mmIsLoading = false;
let mmQualifiedSymbolsOrder = [];

// High-speed In-Memory L2 Order Book Map for live streaming (sym -> { bids: Map, asks: Map, lastBid: num, lastAsk: num })
const mmOrderBooks = new Map();

window.profileLotSizeBtc = 1;
window.profileLotSizeEth = 1;

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

    // Load User Profile Settings (BTC & ETH lot sizes + Telegram alerts)
    try {
      const profRes = await ApiService.getProfileSettings();
      if (profRes && profRes.success) {
        window.profileLotSizeBtc = profRes.lot_size_btc || 1;
        window.profileLotSizeEth = profRes.lot_size_eth || 1;
        if (typeof renderMmTableRows === 'function') {
          renderMmTableRows();
        }

        const tgCfg = profRes.telegram;
        if (tgCfg) {
          const inpSec = document.getElementById('mmInpTgSec');
          if (inpSec && tgCfg.notify_secs !== undefined) {
            inpSec.value = tgCfg.notify_secs;
          }
          const chkTg = document.getElementById('mmChkTgEnabled');
          if (chkTg && tgCfg.enabled !== undefined) {
            chkTg.checked = tgCfg.enabled;
          }
          updateTgStatusBadges(tgCfg);
        }
      }
    } catch (e) {
      console.warn('[MM] Could not load User Profile settings:', e);
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
    mmCurrentUnderlying = ['ALL', 'BTC', 'ETH'].includes(cleanAsset) ? cleanAsset : 'ALL';
  } else if (!['ALL', 'BTC', 'ETH'].includes(mmCurrentUnderlying)) {
    mmCurrentUnderlying = 'ALL';
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
      if (!isBackground) {
        mmOrderBooks.clear();
      }
      mmLastData = data;
      // Pre-seed mmOrderBooks from initial quotes
      if (Array.isArray(data.all_strikes)) {
        data.all_strikes.forEach(item => {
          if (item && item.symbol) {
            const symUpper = item.symbol.toUpperCase();
            const b = item.bid > 0 ? item.bid : 0;
            const a = item.ask > 0 ? item.ask : 0;
            const bidsMap = new Map();
            const asksMap = new Map();
            if (b > 0) bidsMap.set(b, 1);
            if (a > 0) asksMap.set(a, 1);
            mmOrderBooks.set(symUpper, {
              bids: bidsMap,
              asks: asksMap,
              lastBid: b,
              lastAsk: a
            });
          }
        });
      }
      mmSelectedExpiry = data.selected_expiry || mmSelectedExpiry;
      const incomingQualSyms = (data.qualified_strikes || []).map(x => x.symbol);
      incomingQualSyms.forEach(sym => {
        if (!mmQualifiedSymbolsOrder.includes(sym)) {
          mmQualifiedSymbolsOrder.push(sym);
        }
      });
      mmQualifiedSymbolsOrder = mmQualifiedSymbolsOrder.filter(sym => incomingQualSyms.includes(sym));
      populateMmExpiries(data.expiries || [], mmSelectedExpiry);
      renderMarketMakingView(data, isBackground);

      // Subscribe all option symbols to the Delta WebSocket tick stream
      subscribeAllMmOptionSymbols(data.all_strikes || []);
    } else {
      if (tbody && !mmLastData && !isBackground) {
        tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--red);">Failed to load strike data: ${data?.error || 'Unknown error'}<br><button type="button" class="btn btn-sm btn-primary mt-2" onclick="loadMarketMakingStrikes('${mmCurrentUnderlying}', '${mmSelectedExpiry}')">🔄 Retry Now</button></td></tr>`;
      } else {
        console.warn('[MM Strikes] Sync notice:', data?.error);
      }
    }
  } catch (err) {
    if (tbody && !mmLastData && !isBackground) {
      tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; padding:32px; color:var(--red);">Error connecting to API: ${err.message}<br><button type="button" class="btn btn-sm btn-primary mt-2" onclick="loadMarketMakingStrikes('${mmCurrentUnderlying}', '${mmSelectedExpiry}')">🔄 Retry Now</button></td></tr>`;
    } else {
      console.warn('[MM Strikes] Transient connection error:', err.message);
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

  if (!expiries || expiries.length === 0) {
    sel.innerHTML = '<option value="">No active expiries</option>';
    return;
  }

  // Check if existing options match expiries to prevent recreating elements on every background sync
  const currentOptions = Array.from(sel.options).map(o => o.value);
  const newOptions = expiries.map(e => e.expiry);
  const isSame = currentOptions.length === newOptions.length && currentOptions.every((v, i) => v === newOptions[i]);

  if (isSame) {
    // Only update option label text if changed
    expiries.forEach((exp, idx) => {
      const opt = sel.options[idx];
      const newText = `${exp.label || exp.expiry} (${exp.total_strikes || 0} strikes)`;
      if (opt.textContent !== newText) opt.textContent = newText;
    });
    if (selected && sel.value !== selected) {
      sel.value = selected;
    }
    return;
  }

  sel.innerHTML = '';
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
function renderMarketMakingView(data, isBackground = false) {
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

  // Badges (Drawer and Top Nav)
  const qCount = summary.total_qualified || 0;
  const elNavBadge = document.getElementById('navMmBadge');
  if (elNavBadge) elNavBadge.textContent = `${qCount} Opps`;
  const elDrawerBadge = document.getElementById('drawerMmBadge');
  if (elDrawerBadge) elDrawerBadge.textContent = `${qCount} Opps`;

  // Render or Reconcile Table
  const tbody = document.getElementById('mmTbody');
  const needsFullRender = !isBackground || !tbody || tbody.children.length === 0 || tbody.querySelector('td[colspan]');
  if (needsFullRender) {
    renderMmTableRows();
  } else {
    reconcileMmTableRows();
  }
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
 * Returns qualified strikes preserving arrival order (oldest at top, newly qualified appended at bottom)
 */
function getStableQualifiedStrikes() {
  if (!mmLastData || !mmLastData.all_strikes) return [];
  const qualMap = new Map();
  mmLastData.all_strikes.forEach(x => {
    if (x.qualified) qualMap.set(x.symbol, x);
  });

  const stableList = [];
  mmQualifiedSymbolsOrder.forEach(sym => {
    if (qualMap.has(sym)) {
      stableList.push(qualMap.get(sym));
      qualMap.delete(sym);
    }
  });

  // Any newly qualified strikes not yet in order: append at the bottom
  qualMap.forEach(item => {
    stableList.push(item);
    mmQualifiedSymbolsOrder.push(item.symbol);
  });

  return stableList;
}

/**
 * Determines which strikes should be displayed based on filters and search
 */
function getStrikesToDisplay() {
  if (!mmLastData) return [];
  const chk = document.getElementById('mmChkOnlyQual');
  if (chk) mmOnlyQualified = chk.checked;

  const all = mmLastData.all_strikes || [];
  const qual = getStableQualifiedStrikes();

  // If user entered a search query, search all strikes so they can find ANY strike (like 81800)
  if (mmSearchTerm && mmSearchTerm.trim()) {
    const term = mmSearchTerm.trim().toLowerCase();
    const allMatches = all.filter(item => {
      const sym = (item.symbol || '').toLowerCase();
      const st = String(item.strike || '');
      return sym.includes(term) || st.includes(term);
    });

    if (!mmOnlyQualified) return allMatches;

    // In 'Show Only Qualified' mode:
    // If any qualified strikes match, show them;
    // If NO qualified strikes match the query (e.g. searching 81800 before it qualifies),
    // display all matching strikes with their status/reasons so the user can see exact live values!
    const qualMatches = allMatches.filter(x => x.qualified);
    return qualMatches.length > 0 ? qualMatches : allMatches;
  }

  return mmOnlyQualified ? qual : all;
}

/**
 * Renders individual table rows based on filters and search
 */
function renderMmTableRows() {
  if (!mmLastData) return;

  const tbody = document.getElementById('mmTbody');
  if (!tbody) return;

  const filtered = getStrikesToDisplay();

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
 * Smoothly reconciles the table DOM during background updates:
 * - Appends newly qualified strikes seamlessly without reload
 * - Removes strikes that no longer match criteria
 * - In-place updates prices, edges, badges, and action buttons without flicker
 */
function reconcileMmTableRows() {
  const tbody = document.getElementById('mmTbody');
  if (!tbody) return;

  const targetList = getStrikesToDisplay();
  const hasPlaceholder = tbody.children.length === 0 || tbody.querySelector('td[colspan]');

  if (hasPlaceholder) {
    if (targetList.length > 0) {
      renderMmTableRows();
    }
    return;
  }

  if (targetList.length === 0) {
    renderMmTableRows();
    return;
  }

  // Target symbols map
  const targetMap = new Map();
  targetList.forEach(item => {
    if (item && item.symbol) {
      targetMap.set(item.symbol.toUpperCase(), item);
    }
  });

  // 1. Remove rows no longer in target list
  const domRows = Array.from(tbody.querySelectorAll('tr[id^="mmRow_"]'));
  domRows.forEach(row => {
    const sym = (row.getAttribute('data-symbol') || '').toUpperCase();
    if (sym && !targetMap.has(sym)) {
      row.remove();
    }
  });

  // 2. Add newly qualified / matching rows or update existing rows in-place
  targetList.forEach(item => {
    if (!item || !item.symbol) return;
    const sym = item.symbol.toUpperCase();
    const existingRow = document.getElementById(`mmRow_${sym}`);
    if (!existingRow) {
      // Newly qualified strike! Append to table
      tbody.insertAdjacentHTML('beforeend', buildMmRowHtml(item));
    } else {
      // Existing row: update in-place
      updateMmRowInPlace(item, sym, existingRow);
    }
  });
}

/**
 * Checks if a strike symbol has an active MM trade cycle OR an open position
 * to avoid duplicate trading.
 */
function getActiveTradeOrPositionForSymbol(symbol) {
  if (!symbol) return null;
  const sym = String(symbol).toUpperCase().trim();

  // 1. Check active 1-Lot MM Trade Cycles
  if (Array.isArray(mmActiveTradesCache)) {
    const activeMm = mmActiveTradesCache.find(t => 
      t && t.symbol && t.symbol.toUpperCase().trim() === sym &&
      t.is_running && !['COMPLETED_PROFIT', 'COMPLETED_COST', 'CANCELLED', 'ERROR'].includes(t.state)
    );
    if (activeMm) {
      let stateLabel = 'IN TRADE';
      const totSz = activeMm.total_size || activeMm.size || 1;
      const bFill = activeMm.buy_filled_qty || 0;
      const sFill = activeMm.sell_filled_qty || 0;

      if (activeMm.state === 'DUAL_ACTIVE') stateLabel = `DUAL B:${bFill}/${totSz} S:${sFill}`;
      else if (activeMm.state === 'BUY_PARTIAL') stateLabel = `BUY ${bFill}/${totSz}`;
      else if (activeMm.state === 'BUY_OPEN' || activeMm.state === 'PLACING_BUY') stateLabel = 'BUY PEG';
      else if (activeMm.state === 'BUY_FILLED') stateLabel = 'BUY FILLED';
      else if (activeMm.state === 'SELL_PARTIAL') stateLabel = `SELL ${sFill}/${activeMm.size || totSz}`;
      else if (activeMm.state === 'SELL_OPEN' || activeMm.state === 'PLACING_SELL') stateLabel = 'SELL PEG';
      else if (activeMm.state === 'WAITING_COST_TO_COST') stateLabel = 'COST-TO-COST';
      return {
        type: 'mm',
        trade: activeMm,
        label: stateLabel,
        desc: `Active MM Trade Cycle (${stateLabel}) running on ${activeMm.symbol}`
      };
    }
  }

  // 2. Check open positions on account
  if (typeof currentPositions !== 'undefined' && Array.isArray(currentPositions)) {
    const openPos = currentPositions.find(p => {
      const pSym = (p.symbol || p.product_symbol || (p.product && p.product.symbol) || '').toUpperCase().trim();
      const size = Math.abs(parseFloat(p.size || p.contracts || 0));
      return pSym === sym && size > 0;
    });
    if (openPos) {
      const side = parseFloat(openPos.size) > 0 ? 'LONG' : 'SHORT';
      return {
        type: 'position',
        position: openPos,
        label: `OPEN ${side}`,
        desc: `Open ${side} position (${Math.abs(openPos.size)} contracts) on ${sym}`
      };
    }
  }

  return null;
}

let _lastInTradeClickTs = 0;
let _lastInTradeClickSym = '';

/**
 * Click handler when clicking an 'IN TRADE' button on a strike that is already trading.
 * Smoothly scrolls to the active trade card and informs user that duplicates are blocked.
 */
function handleActiveTradeBtnClick(symbol) {
  const now = Date.now();
  if (_lastInTradeClickSym === symbol && (now - _lastInTradeClickTs) < 800) {
    return; // Prevent duplicate rapid clicks or bubbled events
  }
  _lastInTradeClickTs = now;
  _lastInTradeClickSym = symbol;

  const panel = document.getElementById('mmActiveTradesPanel');
  if (panel && panel.style.display !== 'none') {
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    const symClean = String(symbol || '').toUpperCase().trim();
    const cards = panel.querySelectorAll('.mm-trade-card');
    let matchedCard = null;
    cards.forEach(c => {
      const symEl = c.querySelector('.mm-trade-symbol');
      if (symEl && symEl.textContent.trim().toUpperCase() === symClean) {
        matchedCard = c;
      }
    });
    const target = matchedCard || panel;
    target.classList.add('highlight-pulse');
    setTimeout(() => target.classList.remove('highlight-pulse'), 1800);
  }
  if (typeof showToast === 'function') {
    showToast(`⚡ ${symbol} is already active in a live MM trade cycle! Duplicate trade prevented.`, 'info');
  }
}

/**
 * Resolves the dynamic lot size label for action buttons based on user profile settings (.env)
 */
function getMmButtonLabel(symbol) {
  const isBtc = (symbol || '').toUpperCase().includes('BTC');
  const lotSize = isBtc ? (window.profileLotSizeBtc || 1) : (window.profileLotSizeEth || 1);
  return `⚡ ${lotSize}-Lot MM`;
}

/**
 * Updates a single strike row in-place with latest metrics and button states.
 */
function updateMmRowInPlace(item, sym, elRow) {
  if (!item) return;
  sym = (sym || item.symbol || '').toUpperCase();
  elRow = elRow || document.getElementById(`mmRow_${sym}`);
  if (!elRow) return;

  elRow.style.display = '';

  const elBid = document.getElementById(`mmBid_${sym}`);
  if (elBid) elBid.textContent = item.bid > 0 ? item.bid.toFixed(2) : '--';

  const elAsk = document.getElementById(`mmAsk_${sym}`);
  if (elAsk) elAsk.textContent = item.ask > 0 ? item.ask.toFixed(2) : '--';

  const elSpread = document.getElementById(`mmSpread_${sym}`);
  if (elSpread) elSpread.textContent = item.spread > 0 ? item.spread.toFixed(2) : '--';

  const elTicks = document.getElementById(`mmTicks_${sym}`);
  if (elTicks) elTicks.textContent = (item.spread_ticks !== null && item.spread_ticks !== undefined) ? item.spread_ticks.toFixed(1) : '0.0';

  const elMid = document.getElementById(`mmMid_${sym}`);
  if (elMid) elMid.textContent = item.mid_price > 0 ? item.mid_price.toFixed(2) : '--';

  const elEdge = document.getElementById(`mmEdge_${sym}`);
  if (elEdge) elEdge.textContent = (item.net_edge !== null && item.net_edge !== undefined) ? item.net_edge.toFixed(2) : '--';

  const elEdgePct = document.getElementById(`mmEdgePct_${sym}`);
  if (elEdgePct) {
    const pct = item.net_edge_pct !== null && item.net_edge_pct !== undefined ? item.net_edge_pct : 0;
    elEdgePct.textContent = `${pct.toFixed(1)}%`;
    elEdgePct.className = `col-edgepct text-right ${pct >= 50 ? 'edge-highlight-high' : (pct >= 25 ? 'edge-highlight-med' : '')}`;
  }

  const isMaxOi = !!item.oi_reduce_only_mode;
  const activeInfo = getActiveTradeOrPositionForSymbol(sym);
  const isInTrade = !!activeInfo;

  const elStatus = document.getElementById(`mmStatus_${sym}`);
  if (elStatus) {
    const isQualStr = String(item.qualified === true);
    if (elRow.dataset.qualified !== isQualStr || elRow.dataset.maxOi !== String(isMaxOi)) {
      elRow.dataset.qualified = isQualStr;
      elRow.dataset.maxOi = String(isMaxOi);
      const statusBadge = isMaxOi
        ? `<span class="mm-status-badge mm-status-fail" style="background:rgba(239,68,68,0.2); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);" title="Delta Exchange Max Open Interest Breached. Only reduce-only orders allowed. Fresh positions blocked.">⛔ MAX OI</span>`
        : item.qualified
        ? `<span class="mm-status-badge mm-status-qual" title="Meets all selection criteria">✔ QUALIFIED</span>`
        : `<span class="mm-status-badge mm-status-fail" title="${(item.reasons_failed || []).join('; ') || 'Criteria not met'}">NOT MET</span>`;
      elStatus.innerHTML = statusBadge;
    }
  }

  const elAction = document.getElementById(`mmAction_${sym}`);
  if (elAction) {
    const inTradeStr = String(isInTrade);
    const existingBtn = elAction.querySelector('.mm-btn-1lot');
    if (elRow.dataset.inTrade !== inTradeStr || !existingBtn) {
      elRow.dataset.inTrade = inTradeStr;
      const btnLabel = getMmButtonLabel(sym);
      elAction.innerHTML = isMaxOi
        ? `<button type="button" class="mm-btn-1lot" disabled style="opacity:0.55; cursor:not-allowed; background:rgba(239,68,68,0.15); border-color:rgba(239,68,68,0.3); color:#fca5a5;" title="Delta Exchange has restricted this strike to Reduce-Only (Max Open Interest reached). Fresh positions cannot be opened.">
             <span>⛔ MAX OI</span>
           </button>`
        : isInTrade
        ? `<button type="button" class="mm-btn-1lot mm-btn-in-trade" data-action="in-trade" data-symbol="${sym}" onclick="handleActiveTradeBtnClick('${sym}')" title="${activeInfo.desc} • Duplicate trade blocked">
             <span>⚡ IN TRADE</span>
           </button>`
        : `<button type="button" class="mm-btn-1lot" data-action="open-modal" data-symbol="${sym}" onclick="open1LotMmModalBySymbol('${sym}')" title="${btnLabel} (No Stop-Loss, No Timeout • Cost-to-Cost or Wait)">
             <span>${btnLabel}</span>
           </button>`;
    }
  }

  elRow.classList.toggle('row-qualified', !!item.qualified);
  elRow.classList.toggle('row-in-trade', isInTrade);
  elRow.classList.toggle('row-max-oi', isMaxOi);
}

/**
 * Scans all rows currently rendered in the Market Making strikes table
 * and updates their Status badge, metrics, and Action button according to live trade/position state.
 */
function updateStrikesTableActiveTradeStatus() {
  const tbody = document.getElementById('mmTbody');
  if (!tbody || !mmLastData || !mmLastData.all_strikes) return;

  mmLastData.all_strikes.forEach(item => {
    if (!item || !item.symbol) return;
    const sym = item.symbol.toUpperCase();
    const row = document.getElementById(`mmRow_${sym}`);
    if (!row) return;
    updateMmRowInPlace(item, sym, row);
  });
}

/**
 * Constructs HTML string for a single strike row
 */
function buildMmRowHtml(item) {
  const isQual = item.qualified;
  const isCall = item.option_type === 'CALL';
  const typeClass = isCall ? 'mm-type-ce' : 'mm-type-pe';
  const badgeText = isCall ? 'CALL' : 'PUT';

  const isMaxOi = !!item.oi_reduce_only_mode;
  const activeInfo = getActiveTradeOrPositionForSymbol(item.symbol);
  const isInTrade = !!activeInfo;

  const rowClasses = [
    isQual ? 'row-qualified' : '',
    isInTrade ? 'row-in-trade' : '',
    isMaxOi ? 'row-max-oi' : ''
  ].filter(Boolean).join(' ');

  const bid = item.bid !== null && item.bid > 0 ? item.bid.toFixed(2) : '--';
  const ask = item.ask !== null && item.ask > 0 ? item.ask.toFixed(2) : '--';
  const spread = item.spread !== null && item.spread > 0 ? item.spread.toFixed(2) : '--';
  const isEth = (item.underlying === 'ETH') || (item.symbol && item.symbol.includes('ETH'));
  const tickVal = item.tick_size ? item.tick_size : (isEth ? 0.01 : 0.1);
  const tick = isEth ? tickVal.toFixed(2) : tickVal.toFixed(1);
  const ticks = item.spread_ticks !== null ? item.spread_ticks.toFixed(1) : '0.0';
  const mid = item.mid_price !== null && item.mid_price > 0 ? item.mid_price.toFixed(2) : '--';
  const netEdge = item.net_edge !== null ? item.net_edge.toFixed(2) : '--';
  const edgePct = item.net_edge_pct !== null ? `${item.net_edge_pct.toFixed(1)}%` : '0.0%';

  let edgeClass = '';
  if (item.net_edge_pct >= 50) edgeClass = 'edge-highlight-high';
  else if (item.net_edge_pct >= 25) edgeClass = 'edge-highlight-med';

  const statusBadge = isMaxOi
    ? `<span class="mm-status-badge mm-status-fail" style="background:rgba(239,68,68,0.2); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);" title="Delta Exchange Max Open Interest Breached. Only reduce-only orders allowed. Fresh positions blocked.">⛔ MAX OI</span>`
    : isQual
    ? `<span class="mm-status-badge mm-status-qual" title="Meets all selection criteria">✔ QUALIFIED</span>`
    : `<span class="mm-status-badge mm-status-fail" title="${(item.reasons_failed || []).join('; ') || 'Criteria not met'}">NOT MET</span>`;

  const asset = item.underlying || (item.symbol && item.symbol.includes('BTC') ? 'BTC' : 'ETH');
  const assetTag = `<span class="mm-asset-tag ${asset === 'ETH' ? 'tag-eth' : 'tag-btc'}" style="font-size:0.65rem; font-weight:700; padding:1px 5px; border-radius:3px; margin-right:5px; letter-spacing:0.3px; ${asset === 'ETH' ? 'background:rgba(99,102,241,0.18); color:#a5b4fc; border:1px solid rgba(99,102,241,0.35);' : 'background:rgba(245,158,11,0.18); color:#fcd34d; border:1px solid rgba(245,158,11,0.35);'}">${asset}</span>`;

  const btnLabel = getMmButtonLabel(item.symbol);
  const inTradeStr = String(isInTrade);
  const isQualStr = String(isQual);

  const actionButton = isMaxOi
    ? `<button type="button" class="mm-btn-1lot" disabled style="opacity:0.55; cursor:not-allowed; background:rgba(239,68,68,0.15); border-color:rgba(239,68,68,0.3); color:#fca5a5;" title="Delta Exchange has restricted this strike to Reduce-Only (Max Open Interest reached). Fresh positions cannot be opened.">
         <span>⛔ MAX OI</span>
       </button>`
    : isInTrade
    ? `<button type="button" class="mm-btn-1lot mm-btn-in-trade" data-action="in-trade" data-symbol="${item.symbol}" onclick="handleActiveTradeBtnClick('${item.symbol}')" title="${activeInfo.desc} • Duplicate trade blocked">
         <span>⚡ IN TRADE</span>
       </button>`
    : `<button type="button" class="mm-btn-1lot" data-action="open-modal" data-symbol="${item.symbol}" onclick="open1LotMmModalBySymbol('${item.symbol}')" title="${btnLabel} (No Stop-Loss, No Timeout • Cost-to-Cost or Wait)">
         <span>${btnLabel}</span>
       </button>`;

  return `
    <tr id="mmRow_${item.symbol}" class="${rowClasses}" data-symbol="${item.symbol}" data-in-trade="${inTradeStr}" data-qualified="${isQualStr}">
      <td class="col-symbol text-left font-mono" style="font-weight: 600; font-size: 0.76rem; letter-spacing: -0.2px;">${assetTag}${item.symbol}</td>
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
      <td class="col-action text-center" id="mmAction_${item.symbol}" style="white-space: nowrap;">
        ${actionButton}
      </td>
    </tr>
  `;
}

/**
 * Real-time Order Book maintainer for Market Making strikes
 * Processes both L2 snapshots and incremental level updates from Delta Exchange
 */
function processMmStrikesL2Update(sym, data) {
  if (!sym) return null;
  const symUpper = sym.toUpperCase();
  let book = mmOrderBooks.get(symUpper);
  if (!book) {
    book = { bids: new Map(), asks: new Map(), lastBid: 0, lastAsk: 0 };
    mmOrderBooks.set(symUpper, book);
  }

  // 1. Snapshot: reset book
  if (data.action === 'snapshot') {
    book.bids.clear();
    book.asks.clear();
  }

  // 2. Incremental Bids
  if (Array.isArray(data.bids)) {
    for (let i = 0; i < data.bids.length; i++) {
      const p = parseFloat(data.bids[i][0]);
      const s = parseFloat(data.bids[i][1]);
      if (!isNaN(p)) {
        if (s <= 0) {
          book.bids.delete(p);
        } else {
          book.bids.set(p, s);
        }
      }
    }
  }

  // 3. Incremental Asks
  if (Array.isArray(data.asks)) {
    for (let i = 0; i < data.asks.length; i++) {
      const p = parseFloat(data.asks[i][0]);
      const s = parseFloat(data.asks[i][1]);
      if (!isNaN(p)) {
        if (s <= 0) {
          book.asks.delete(p);
        } else {
          book.asks.set(p, s);
        }
      }
    }
  }

  let bestBid = null;
  let bestAsk = null;
  if (book.bids.size > 0) {
    bestBid = Math.max(...book.bids.keys());
    book.lastBid = bestBid;
  }
  if (book.asks.size > 0) {
    bestAsk = Math.min(...book.asks.keys());
    book.lastAsk = bestAsk;
  }

  return { bestBid, bestAsk };
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

  const msgType = data.type || data.channel || data.name || '';
  let rawBid = undefined;
  let rawAsk = undefined;

  // 1. Prioritize official Delta Exchange matching engine top-of-book quotes (from v2/ticker)
  const quotes = data.quotes || {};
  if (quotes.best_bid !== undefined || quotes.best_ask !== undefined) {
    if (quotes.best_bid !== undefined && quotes.best_bid !== null) rawBid = quotes.best_bid;
    if (quotes.best_ask !== undefined && quotes.best_ask !== null) rawAsk = quotes.best_ask;
  } else if (msgType === 'l2_updates' || Array.isArray(data.bids) || Array.isArray(data.asks)) {
    // 2. Fallback to L2 Orderbook updates
    const l2Res = processMmStrikesL2Update(sym, data);
    if (l2Res) {
      if (l2Res.bestBid !== null) rawBid = l2Res.bestBid;
      if (l2Res.bestAsk !== null) rawAsk = l2Res.bestAsk;
    }
  } else {
    // 3. Fallback to direct ticker fields
    if (data.bid !== undefined && data.bid !== null) rawBid = data.bid;
    if (data.ask !== undefined && data.ask !== null) rawAsk = data.ask;
  }

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
    if (!isNaN(newLtp) && newLtp !== item.ltp) {
      item.ltp = newLtp;
    }
  }

  if (rawMark !== undefined && rawMark !== null) {
    const newMark = parseFloat(rawMark);
    if (!isNaN(newMark) && newMark !== item.mark_price) {
      item.mark_price = newMark;
    }
  }

  if (!changed) return;

  // Recalculate MM criteria in real-time
  const prevQualified = item.qualified === true;
  const criteria = getActiveMmCriteria();

  // Dynamic Tick Size: ETH options = 0.01, BTC options = 0.1
  const isEth = (item.underlying === 'ETH') || sym.includes('ETH');
  const tick = item.tick_size ? item.tick_size : (isEth ? 0.01 : 0.1);
  const bid = item.bid || 0;
  const ask = item.ask || 0;

  if (bid > 0 && ask > 0 && ask >= bid) {
    item.spread = ask - bid;
    item.mid_price = (bid + ask) / 2.0;
    item.net_edge = item.spread - (2.0 * tick);
    item.net_edge_pct = item.mid_price > 0 ? (item.net_edge / item.mid_price * 100.0) : 0.0;
    item.spread_ticks = tick > 0 ? (item.spread / tick) : 0.0;

    const reasons = [];
    if (item.oi_reduce_only_mode) reasons.push("Delta Exchange Max OI Breached (Reduce-Only Mode)");
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

  // Update mmQualifiedSymbolsOrder:
  // - If newly qualified: ALWAYS append to the end (bottom)!
  // - If disqualified: remove from order
  if (isNowQualified) {
    if (!mmQualifiedSymbolsOrder.includes(sym)) {
      mmQualifiedSymbolsOrder.push(sym);
    }
  } else {
    const oIdx = mmQualifiedSymbolsOrder.indexOf(sym);
    if (oIdx !== -1) {
      mmQualifiedSymbolsOrder.splice(oIdx, 1);
    }
  }

  // Update mmLastData.qualified_strikes preserving arrival order (newest at bottom)
  mmLastData.qualified_strikes = getStableQualifiedStrikes();

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

  const elNavBadge = document.getElementById('navMmBadge');
  if (elNavBadge) elNavBadge.textContent = `${qualCount} Opps`;
  const elDrawerBadge = document.getElementById('drawerMmBadge');
  if (elDrawerBadge) elDrawerBadge.textContent = `${qualCount} Opps`;

  // Sync checkbox state
  const chk = document.getElementById('mmChkOnlyQual');
  if (chk) {
    mmOnlyQualified = chk.checked;
  }

  const elRow = document.getElementById(`mmRow_${sym}`);
  const tbody = document.getElementById('mmTbody');

  // If search filter is active, check if this contract matches the search term
  if (mmSearchTerm && mmSearchTerm.trim()) {
    const term = mmSearchTerm.trim().toLowerCase();
    const matchesSearch = sym.toLowerCase().includes(term) || String(item.strike || '').includes(term);
    if (!matchesSearch) {
      if (elRow) elRow.remove();
      return;
    }
  }

  // If in 'Show Only Qualified' mode (and no search query overriding it), handle dynamic row entry/exit seamlessly
  if (mmOnlyQualified && (!mmSearchTerm || !mmSearchTerm.trim())) {
    if (isNowQualified && !elRow) {
      // Newly qualified strike! If table currently has empty placeholder, re-render
      if (tbody && tbody.querySelector('td[colspan]')) {
        renderMmTableRows();
        return;
      } else if (tbody) {
        tbody.insertAdjacentHTML('beforeend', buildMmRowHtml(item));
        return;
      }
    } else if (!isNowQualified && elRow) {
      // Strike fell out of qualification
      elRow.remove();
      if (tbody && tbody.children.length === 0) {
        renderMmTableRows();
      }
      return;
    }
  }

  if (!elRow) {
    // If not in DOM, check if it should now be added
    if (isNowQualified || (mmSearchTerm && mmSearchTerm.trim())) {
      if (tbody && tbody.querySelector('td[colspan]')) {
        renderMmTableRows();
      } else if (tbody) {
        tbody.insertAdjacentHTML('beforeend', buildMmRowHtml(item));
      }
    }
    return;
  }

  // Update existing row in-place for 0-latency microsecond rendering with ZERO table rebuilds
  updateMmRowInPlace(item, sym, elRow);
}

/**
 * Handles switching underlying asset
 */
function onMmUnderlyingChange(asset) {
  const cleanAsset = (asset || 'ALL').toUpperCase();
  mmCurrentUnderlying = ['ALL', 'BTC', 'ETH'].includes(cleanAsset) ? cleanAsset : 'ALL';
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
      const isEth = (item.underlying === 'ETH') || (item.symbol && item.symbol.includes('ETH'));
      const tick = item.tick_size ? item.tick_size : (isEth ? 0.01 : 0.1);
      const bid = item.bid || 0;
      const ask = item.ask || 0;

      if (bid > 0 && ask > 0 && ask >= bid) {
        item.spread = ask - bid;
        item.mid_price = (bid + ask) / 2.0;
        item.net_edge = item.spread - (2.0 * tick);
        item.net_edge_pct = item.mid_price > 0 ? (item.net_edge / item.mid_price * 100.0) : 0.0;
        item.spread_ticks = tick > 0 ? (item.spread / tick) : 0.0;

        const reasons = [];
        if (item.oi_reduce_only_mode) reasons.push("Delta Exchange Max OI Breached (Reduce-Only Mode)");
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
    });

    mmLastData.qualified_strikes = getStableQualifiedStrikes();

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
  const sym = symbol.toUpperCase().trim();

  // Guard against starting duplicate trade on active strike
  const activeInfo = getActiveTradeOrPositionForSymbol(sym);
  if (activeInfo) {
    handleActiveTradeBtnClick(sym);
    return;
  }

  let item = null;

  if (mmLastData && mmLastData.all_strikes) {
    item = mmLastData.all_strikes.find(x => x && x.symbol && x.symbol.toUpperCase() === sym);
  }

  if (!item) {
    if (typeof showToast === 'function') showToast(`Could not find details for ${sym}`, 'error');
    return;
  }

  // Guard against Max OI Breached (Delta Exchange Reduce-Only Mode)
  if (item.oi_reduce_only_mode) {
    if (typeof showToast === 'function') {
      showToast(`⛔ ${sym} cannot be traded: Delta Exchange has placed this strike into Reduce-Only mode because Max Open Interest is breached. Please choose an adjacent strike.`, 'error');
    }
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

  const isEth = (item.underlying === 'ETH') || (item.symbol && item.symbol.includes('ETH'));
  const tick = item.tick_size ? item.tick_size : (isEth ? 0.01 : 0.1);
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

  const isBtc = (item.symbol || '').includes('BTC');
  const defaultSize = isBtc ? (window.profileLotSizeBtc || 1) : (window.profileLotSizeEth || 1);
  const inpSize = document.getElementById('mmModalInpSize');
  const noteUnit = document.getElementById('mmModalLotUnitNote');
  if (inpSize) inpSize.value = defaultSize;
  if (noteUnit) noteUnit.textContent = isBtc ? '1 lot = 0.001 BTC' : '1 lot = 0.01 ETH';

  const btnLaunch = document.getElementById('btnLaunchMmTrade');
  if (btnLaunch) {
    btnLaunch.textContent = `⚡ Launch MM Order (${defaultSize} Lot${defaultSize > 1 ? 's' : ''})`;
  }

  if (modal) {
    modal.style.display = 'flex';
  }
}

/**
 * Updates launch button text when size input changes
 */
function onMmModalSizeChange() {
  const inpSize = document.getElementById('mmModalInpSize');
  const btnLaunch = document.getElementById('btnLaunchMmTrade');
  const size = Math.max(1, parseInt(inpSize?.value || '1'));
  if (btnLaunch) {
    btnLaunch.textContent = `⚡ Launch MM Order (${size} Lot${size > 1 ? 's' : ''})`;
  }
}

function close1LotMmModal() {
  const modal = document.getElementById('mmTradeModal');
  if (modal) modal.style.display = 'none';
  mmSelectedTradeItem = null;
}

/**
 * Launches the MM Trade Cycle on Delta Exchange with configured lot size
 */
async function confirmAndLaunch1LotTrade() {
  if (!mmSelectedTradeItem) {
    showToast('No strike selected', 'error');
    return;
  }

  const btn = document.getElementById('btnLaunchMmTrade');
  const inpSize = document.getElementById('mmModalInpSize');
  const tradeSize = Math.max(1, parseInt(inpSize?.value || '1'));

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

  // Prevent duplicate trade execution if trade cycle is already active
  const activeInfo = getActiveTradeOrPositionForSymbol(selectedSymbol);
  if (activeInfo) {
    if (typeof showToast === 'function') {
      showToast(`⚠️ Cannot start trade: ${selectedSymbol} is already active (${activeInfo.label})! Duplicate trade blocked.`, 'error');
    }
    close1LotMmModal();
    if (btn) {
      btn.disabled = false;
      btn.textContent = `⚡ Launch MM Order (${tradeSize} Lot${tradeSize > 1 ? 's' : ''})`;
    }
    return;
  }

  try {
    const payload = {
      symbol: selectedSymbol,
      size: tradeSize,
      entry_price: buyPrice,
      target_exit_price: sellPrice,
      account: activeAcct
    };

    const res = await ApiService.startMMTrade(payload);

    if (res && res.success) {
      close1LotMmModal();
      if (typeof showToast === 'function') {
        showToast(`⚡ ${tradeSize}-Lot MM Cycle launched for ${selectedSymbol} on [${activeAcct.toUpperCase()}]!`, 'success');
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
      btn.textContent = `⚡ Launch MM Order (${tradeSize} Lot${tradeSize > 1 ? 's' : ''})`;
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
 * Formats numeric price cleanly according to tick size
 */
function formatMmPrice(val, tickSize) {
  if (val === null || val === undefined || isNaN(val)) return '';
  const num = Number(val);
  if (num <= 0) return '';
  const tickStr = (tickSize || 0.01).toString();
  const decimals = tickStr.includes('.') ? tickStr.split('.')[1].length : 2;
  return num.toFixed(Math.max(2, Math.min(4, decimals)));
}

/**
 * Handles dropdown change in DUAL_ACTIVE
 */
function onMmTradeSideChanged(tradeId) {
  const sel = document.getElementById(`selSide_${tradeId}`);
  const inp = document.getElementById(`inpUpload_${tradeId}`);
  if (!sel || !inp) return;
  inp.dataset.dirty = 'false';
  if (sel.value === 'buy') {
    inp.value = sel.dataset.buy || '';
  } else {
    inp.value = sel.dataset.sell || '';
  }
}
window.onMmTradeSideChanged = onMmTradeSideChanged;

/**
 * Renders active MM trade cards in dashboard (Preserves input focus and typed values!)
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
    updateStrikesTableActiveTradeStatus();
    return;
  }

  panel.style.display = 'flex';

  // 1. Remove cards that are no longer active
  const activeIds = new Set(activeTrades.map(t => String(t.trade_id)));
  Array.from(listEl.children).forEach(child => {
    const tid = child.id ? child.id.replace('card_', '') : null;
    if (tid && !activeIds.has(tid)) {
      child.remove();
    }
  });

  // 2. Add or update each trade card
  activeTrades.forEach(t => {
    let stateBadge = '';
    let cardClass = 'mm-trade-card';

    if (t.state === 'DUAL_ACTIVE') {
      const tot = t.total_size || t.size || 1;
      const bFill = t.buy_filled_qty || 0;
      const sFill = t.sell_filled_qty || 0;
      const sPlaced = t.sell_placed_qty || (bFill - sFill);
      const bPrice = t.buy_limit_price ? `$${t.buy_limit_price.toFixed(2)}` : '--';
      const sPrice = t.sell_limit_price ? `$${t.sell_limit_price.toFixed(2)}` : '--';
      stateBadge = `<span class="mm-trade-badge dual" title="Dual-Sided: Resting Buy pegging bid while Sell exit floats at ask">⚡ DUAL: BUY ${bFill}/${tot} @ ${bPrice} | SELL ${sPlaced} Lots @ ${sPrice}</span>`;
      cardClass += ' dual-active';
    } else if (t.state === 'BUY_PARTIAL') {
      const tot = t.total_size || t.size || 1;
      const filled = t.buy_filled_qty || 0;
      stateBadge = `<span class="mm-trade-badge buying">⚡ PARTIAL BUY: ${filled}/${tot} Lots Filled @ $${t.buy_fill_price || t.buy_limit_price || '--'}</span>`;
    } else if (t.state === 'BUY_OPEN' || t.state === 'PLACING_BUY') {
      stateBadge = `<span class="mm-trade-badge buying">🎯 BUY PEG: #1 Bid @ $${t.buy_limit_price || '--'}</span>`;
    } else if (t.state === 'BUY_FILLED') {
      stateBadge = `<span class="mm-trade-badge selling">BUY FILLED (${t.size} Lots) @ $${t.buy_fill_price || '--'}</span>`;
    } else if (t.state === 'SELL_PARTIAL') {
      const sold = t.sell_filled_qty || 0;
      stateBadge = `<span class="mm-trade-badge selling">⚡ PARTIAL EXIT: ${sold}/${t.size} Sold @ $${t.sell_fill_price || t.sell_limit_price || '--'}</span>`;
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
    const isManualLocked = Boolean(t.manual_buy_override || t.manual_sell_override);

    let cardEl = document.getElementById(`card_${t.trade_id}`);
    if (cardEl) {
      // ----------------------------------------------------
      // IN-PLACE UPDATE (preserves user typing and focus!)
      // ----------------------------------------------------
      cardEl.className = `${cardClass} mm-trade-strip`;
      cardEl.dataset.symbol = t.symbol;

      const leftEl = cardEl.querySelector('.mm-strip-left');
      if (leftEl) {
        leftEl.innerHTML = `
          <span class="mm-trade-symbol">${t.symbol}</span>
          ${stateBadge}
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Account:</span> <strong>${(t.account || 'main').toUpperCase()} (${t.size || 1} Lot${(t.size || 1) > 1 ? 's' : ''})</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Cost:</span> <strong style="color: #38bdf8;">${costStr}</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Bid / Ask:</span> <span class="text-green">${curBidStr}</span> / <span class="text-red">${curAskStr}</span></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">PnL:</span> <strong class="${t.realized_pnl >= 0 ? 'text-green' : 'text-red'}">$${t.realized_pnl.toFixed(4)}</strong></span>
          <span class="mm-strip-chip mm-strip-status-chip" title="${t.status_message}"><span style="color: #94a3b8;">Status:</span> <span class="mm-status-text">${t.status_message}</span></span>
        `;
      }

      const selSide = cardEl.querySelector(`#selSide_${t.trade_id}`);
      if (selSide) {
        selSide.dataset.buy = t.buy_limit_price != null ? t.buy_limit_price : '';
        selSide.dataset.sell = t.sell_limit_price != null ? t.sell_limit_price : '';
        if (selSide.options && selSide.options.length >= 2) {
          selSide.options[0].text = `SELL Exit ($${t.sell_limit_price ? t.sell_limit_price.toFixed(2) : '--'})`;
          selSide.options[1].text = `BUY Peg ($${t.buy_limit_price ? t.buy_limit_price.toFixed(2) : '--'})`;
        }
      }

      const inp = cardEl.querySelector(`#inpUpload_${t.trade_id}`);
      if (inp) {
        const isFocused = (document.activeElement === inp);
        const isDirty = (inp.dataset.dirty === 'true');
        // Only update input value if user is NOT currently focused or typing in it
        if (!isFocused && !isDirty) {
          let targetPrice = activeOrderPrice;
          if (selSide && selSide.value === 'buy' && t.buy_limit_price != null) {
            targetPrice = t.buy_limit_price;
          } else if (selSide && selSide.value === 'sell' && t.sell_limit_price != null) {
            targetPrice = t.sell_limit_price;
          }
          inp.value = formatMmPrice(targetPrice, t.tick_size);
        }
        inp.step = t.tick_size || 0.1;
      }

      // Update Auto button (for resuming dynamic floating peg)
      const actionsEl = cardEl.querySelector('.mm-strip-actions');
      if (actionsEl) {
        let btnAuto = actionsEl.querySelector(`#btnAuto_${t.trade_id}`);
        if (isManualLocked) {
          if (!btnAuto) {
            btnAuto = document.createElement('button');
            btnAuto.type = 'button';
            btnAuto.id = `btnAuto_${t.trade_id}`;
            btnAuto.className = 'mm-btn-autopeg';
            btnAuto.title = 'Clear manual price lock and resume dynamic floating peg';
            btnAuto.innerText = 'Auto';
            btnAuto.onclick = () => resumeActiveTradePeg(t.trade_id);
            const cancelBtn = actionsEl.querySelector('.mm-btn-cancel');
            actionsEl.insertBefore(btnAuto, cancelBtn);
          }
        } else if (btnAuto) {
          btnAuto.remove();
        }
      }

    } else {
      // ----------------------------------------------------
      // INITIAL INSERTION (when card does not exist yet)
      // ----------------------------------------------------
      const div = document.createElement('div');
      div.className = `${cardClass} mm-trade-strip`;
      div.id = `card_${t.trade_id}`;
      div.dataset.symbol = t.symbol;

      const formattedVal = formatMmPrice(activeOrderPrice, t.tick_size);

      div.innerHTML = `
        <div class="mm-strip-left">
          <span class="mm-trade-symbol">${t.symbol}</span>
          ${stateBadge}
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Account:</span> <strong>${(t.account || 'main').toUpperCase()} (${t.size || 1} Lot${(t.size || 1) > 1 ? 's' : ''})</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Cost:</span> <strong style="color: #38bdf8;">${costStr}</strong></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">Bid / Ask:</span> <span class="text-green">${curBidStr}</span> / <span class="text-red">${curAskStr}</span></span>
          <span class="mm-strip-chip"><span style="color: #94a3b8;">PnL:</span> <strong class="${t.realized_pnl >= 0 ? 'text-green' : 'text-red'}">$${t.realized_pnl.toFixed(4)}</strong></span>
          <span class="mm-strip-chip mm-strip-status-chip" title="${t.status_message}"><span style="color: #94a3b8;">Status:</span> <span class="mm-status-text">${t.status_message}</span></span>
        </div>

        ${t.is_running ? `
          <div class="mm-strip-actions">
            ${t.state === 'DUAL_ACTIVE' ? `
              <select id="selSide_${t.trade_id}" class="mm-upload-input" style="width: auto; padding: 2px 4px;" data-buy="${t.buy_limit_price != null ? t.buy_limit_price : ''}" data-sell="${t.sell_limit_price != null ? t.sell_limit_price : ''}" onchange="onMmTradeSideChanged('${t.trade_id}')">
                <option value="sell" selected>SELL Exit ($${t.sell_limit_price ? t.sell_limit_price.toFixed(2) : '--'})</option>
                <option value="buy">BUY Peg ($${t.buy_limit_price ? t.buy_limit_price.toFixed(2) : '--'})</option>
              </select>
            ` : `<span style="font-size: 0.72rem; color: #94a3b8;">Price:</span>`}
            <input type="number" step="${t.tick_size || 0.1}" class="mm-upload-input" id="inpUpload_${t.trade_id}" value="${formattedVal}" placeholder="Price" oninput="this.dataset.dirty='true'" onkeydown="if(event.key==='Enter')uploadActiveTradePrice('${t.trade_id}')" />
            <button type="button" class="mm-btn-upload" id="btnUpload_${t.trade_id}" onclick="uploadActiveTradePrice('${t.trade_id}')">Update</button>
            ${isManualLocked ? `<button type="button" class="mm-btn-autopeg" id="btnAuto_${t.trade_id}" title="Clear manual price lock and resume dynamic floating peg" onclick="resumeActiveTradePeg('${t.trade_id}')">Auto</button>` : ''}
            <button type="button" class="mm-btn-cancel" onclick="cancelActiveTrade('${t.trade_id}')">Stop</button>
          </div>
        ` : ''}
      `;
      listEl.appendChild(div);
    }
  });

  updateStrikesTableActiveTradeStatus();
}

/**
 * Uploads/updates the limit price on an active MM trade
 */
async function uploadActiveTradePrice(tradeId) {
  const inp = document.getElementById(`inpUpload_${tradeId}`);
  const btn = document.getElementById(`btnUpload_${tradeId}`);
  if (!inp || !inp.value) {
    showToast('Enter valid price to upload', 'error');
    return;
  }
  const price = parseFloat(inp.value);
  if (isNaN(price) || price <= 0) {
    showToast('Please enter a positive numeric price', 'error');
    return;
  }
  const sideEl = document.getElementById(`selSide_${tradeId}`);
  const side = sideEl ? sideEl.value : null;

  const origBtnText = btn ? btn.innerText : 'Update';
  if (btn) {
    btn.disabled = true;
    btn.innerText = '...';
  }

  try {
    const res = await ApiService.uploadMMPrice(tradeId, price, side);
    if (res && res.success) {
      inp.dataset.dirty = 'false';
      showToast(`Uploaded new limit price $${price}${side ? ` (${side.toUpperCase()})` : ''} for trade`, 'success');
      await pollActiveMmTrades();
    } else {
      showToast(`Upload failed: ${res?.error || 'Unknown error'}`, 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = origBtnText;
    }
  }
}
window.uploadActiveTradePrice = uploadActiveTradePrice;

/**
 * Resumes dynamic floating peg for an active MM trade
 */
async function resumeActiveTradePeg(tradeId) {
  const sideEl = document.getElementById(`selSide_${tradeId}`);
  const side = sideEl ? sideEl.value : null;
  const btn = document.getElementById(`btnAuto_${tradeId}`);
  if (btn) {
    btn.disabled = true;
    btn.innerText = '...';
  }
  try {
    const res = await ApiService.resumeMMPeg(tradeId, side);
    if (res && res.success) {
      const inp = document.getElementById(`inpUpload_${tradeId}`);
      if (inp) inp.dataset.dirty = 'false';
      showToast('Resumed dynamic floating peg for trade', 'success');
      await pollActiveMmTrades();
    } else {
      showToast(`Failed: ${res?.error || 'Unknown error'}`, 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = 'Auto';
    }
  }
}
window.resumeActiveTradePeg = resumeActiveTradePeg;

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

/**
 * Updates the Drawer Status Badge and Modal Status Indicator
 */
function updateTgStatusBadges(cfg) {
  const badge = document.getElementById('drawerTgStatusBadge') || document.getElementById('navTgBadge');
  const indicator = document.getElementById('tgModalStatusIndicator');
  if (!cfg) return;

  const isConfigured = Boolean(cfg.is_configured);
  const isEnabled = Boolean(cfg.enabled);

  if (badge) {
    if (!isConfigured) {
      badge.textContent = 'Setup';
      badge.style.background = 'rgba(56, 189, 248, 0.15)';
      badge.style.color = '#38bdf8';
      badge.style.borderColor = 'rgba(56, 189, 248, 0.3)';
    } else if (isEnabled) {
      badge.textContent = 'ON';
      badge.style.background = 'rgba(16, 185, 129, 0.2)';
      badge.style.color = '#10b981';
      badge.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    } else {
      badge.textContent = 'OFF';
      badge.style.background = 'rgba(239, 68, 68, 0.15)';
      badge.style.color = '#ef4444';
      badge.style.borderColor = 'rgba(239, 68, 68, 0.3)';
    }
  }

  if (indicator) {
    if (!isConfigured) {
      indicator.innerHTML = '<span style="color: #fbbf24;">⚠️ Bot not configured</span>';
    } else if (isEnabled) {
      indicator.innerHTML = '<span style="color: #10b981; font-weight: 700;">● ALERTS ON</span>';
    } else {
      indicator.innerHTML = '<span style="color: #ef4444; font-weight: 700;">○ ALERTS OFF</span>';
    }
  }
}

/**
 * Syncs color of tgModalEnabled select dropdown
 */
function onTgModalEnabledChange() {
  const enSelect = document.getElementById('tgModalEnabled');
  if (!enSelect) return;
  if (enSelect.value === 'true') {
    enSelect.style.color = '#10b981';
  } else {
    enSelect.style.color = '#ef4444';
  }
}

/**
 * Handles changes to the Telegram alert duration or enabled toggle in the criteria bar
 */
async function onMmTgConfigChange() {
  const inpSec = document.getElementById('mmInpTgSec');
  const chk = document.getElementById('mmChkTgEnabled');
  const notifySecs = parseInt(inpSec ? inpSec.value : '200') || 200;
  const enabled = chk ? chk.checked : true;

  try {
    const res = await ApiService.saveTelegramConfig({
      notify_secs: notifySecs,
      enabled: enabled
    });
    if (res && res.success) {
      if (typeof showToast === 'function') {
        showToast(`Telegram alerts ${enabled ? 'Enabled (ON)' : 'Disabled (OFF)'}`, 'success');
      }
      updateTgStatusBadges(res.config);
    }
  } catch (e) {
    console.error('Failed to update Telegram settings:', e);
  }
}

/**
 * Opens User Profile & Settings modal (Lot Sizes & Telegram configuration)
 */
async function openProfileModal() {
  const modal = document.getElementById('telegramModal');
  if (!modal) return;
  modal.style.display = 'flex';

  const fb = document.getElementById('tgModalFeedback');
  if (fb) fb.style.display = 'none';

  // Fetch & populate User Profile & Telegram configuration
  try {
    const res = await ApiService.getProfileSettings();
    if (res && res.success) {
      window.profileLotSizeBtc = res.lot_size_btc || 1;
      window.profileLotSizeEth = res.lot_size_eth || 1;

      const inpBtc = document.getElementById('profileLotSizeBtc');
      const inpEth = document.getElementById('profileLotSizeEth');
      if (inpBtc) inpBtc.value = window.profileLotSizeBtc;
      if (inpEth) inpEth.value = window.profileLotSizeEth;

      const cfg = res.telegram || {};
      const tokenInp = document.getElementById('tgModalBotToken');
      const chatInp = document.getElementById('tgModalChatId');
      const secInp = document.getElementById('tgModalNotifySec');
      const enSelect = document.getElementById('tgModalEnabled');

      if (tokenInp) tokenInp.value = cfg.bot_token || '';
      if (chatInp) chatInp.value = cfg.chat_id || '';
      if (secInp) secInp.value = cfg.notify_secs || 200;
      if (enSelect) {
        enSelect.value = cfg.enabled ? 'true' : 'false';
        onTgModalEnabledChange();
      }

      updateTgStatusBadges(cfg);
    }
  } catch (e) {
    console.error('Error opening Profile & Settings modal:', e);
  }
}

// Backwards-compatibility alias
const openTelegramModal = openProfileModal;

/**
 * Toggles visibility of the bot token in the modal
 */
function toggleTgTokenVisibility() {
  const tokenInp = document.getElementById('tgModalBotToken');
  if (!tokenInp) return;
  tokenInp.type = tokenInp.type === 'password' ? 'text' : 'password';
}

/**
 * Closes User Profile & Settings modal
 */
function closeProfileModal() {
  const modal = document.getElementById('telegramModal');
  if (modal) modal.style.display = 'none';
}

// Backwards-compatibility alias
const closeTelegramModal = closeProfileModal;

/**
 * Saves User Profile (Traded Lot Sizes) & Telegram settings to .env
 */
async function saveProfileSettingsFromModal() {
  const lotBtc = Math.max(1, parseInt(document.getElementById('profileLotSizeBtc')?.value || '1'));
  const lotEth = Math.max(1, parseInt(document.getElementById('profileLotSizeEth')?.value || '1'));

  const token = document.getElementById('tgModalBotToken')?.value.trim();
  const chatId = document.getElementById('tgModalChatId')?.value.trim();
  const sec = parseInt(document.getElementById('tgModalNotifySec')?.value || '200');
  const enabled = document.getElementById('tgModalEnabled')?.value === 'true';

  const fb = document.getElementById('tgModalFeedback');

  try {
    const res = await ApiService.saveProfileSettings({
      lot_size_btc: lotBtc,
      lot_size_eth: lotEth,
      bot_token: token,
      chat_id: chatId,
      notify_secs: sec,
      enabled: enabled
    });

    if (res && res.success) {
      window.profileLotSizeBtc = res.lot_size_btc || lotBtc;
      window.profileLotSizeEth = res.lot_size_eth || lotEth;
      if (typeof renderMmTableRows === 'function') {
        renderMmTableRows();
      }

      // Sync criteria strip inputs
      const barSec = document.getElementById('mmInpTgSec');
      if (barSec) barSec.value = sec;
      const barEn = document.getElementById('mmChkTgEnabled');
      if (barEn) barEn.checked = enabled;

      if (res.telegram) {
        updateTgStatusBadges(res.telegram);
      }

      if (fb) {
        fb.style.display = 'block';
        fb.style.background = 'rgba(16, 185, 129, 0.15)';
        fb.style.border = '1px solid rgba(16, 185, 129, 0.4)';
        fb.style.color = '#10b981';
        fb.innerHTML = `✅ Profile &amp; Settings saved to .env! (BTC: <strong>${window.profileLotSizeBtc} lot</strong>, ETH: <strong>${window.profileLotSizeEth} lot</strong>)`;
      }
      if (typeof showToast === 'function') {
        showToast(`Profile & Settings saved! BTC: ${window.profileLotSizeBtc} lot, ETH: ${window.profileLotSizeEth} lot`, 'success');
      }
      setTimeout(closeProfileModal, 1200);
    } else {
      if (fb) {
        fb.style.display = 'block';
        fb.style.background = 'rgba(239, 68, 68, 0.15)';
        fb.style.border = '1px solid rgba(239, 68, 68, 0.4)';
        fb.style.color = '#ef4444';
        fb.innerHTML = `❌ Failed to save: ${res?.error || 'Unknown error'}`;
      }
    }
  } catch (e) {
    if (fb) {
      fb.style.display = 'block';
      fb.style.background = 'rgba(239, 68, 68, 0.15)';
      fb.style.border = '1px solid rgba(239, 68, 68, 0.4)';
      fb.style.color = '#ef4444';
      fb.innerHTML = `❌ Error: ${e.message}`;
    }
  }
}

// Backwards-compatibility alias
const saveTelegramSettingsFromModal = saveProfileSettingsFromModal;

/**
 * Sends a test verification message to Telegram
 */
async function sendTelegramTestAlert() {
  const token = document.getElementById('tgModalBotToken')?.value.trim();
  const chatId = document.getElementById('tgModalChatId')?.value.trim();
  const fb = document.getElementById('tgModalFeedback');
  const btn = document.getElementById('btnTgTest');

  if (fb) {
    fb.style.display = 'block';
    fb.style.background = 'rgba(56, 189, 248, 0.15)';
    fb.style.border = '1px solid rgba(56, 189, 248, 0.4)';
    fb.style.color = '#38bdf8';
    fb.innerHTML = '⏳ Sending test message to Telegram...';
  }

  if (btn) btn.disabled = true;

  try {
    const res = await ApiService.testTelegramAlert({
      bot_token: token,
      chat_id: chatId
    });

    if (res && res.success) {
      if (fb) {
        fb.style.background = 'rgba(16, 185, 129, 0.15)';
        fb.style.border = '1px solid rgba(16, 185, 129, 0.4)';
        fb.style.color = '#10b981';
        fb.innerHTML = '🚀 <strong>Test message delivered successfully to your Telegram chat!</strong>';
      }
      if (typeof showToast === 'function') {
        showToast('Test message sent to Telegram!', 'success');
      }
    } else {
      if (fb) {
        fb.style.background = 'rgba(239, 68, 68, 0.15)';
        fb.style.border = '1px solid rgba(239, 68, 68, 0.4)';
        fb.style.color = '#ef4444';
        fb.innerHTML = `❌ Telegram error: ${res?.error || 'Check Bot Token & Chat ID'}`;
      }
    }
  } catch (e) {
    if (fb) {
      fb.style.background = 'rgba(239, 68, 68, 0.15)';
      fb.style.border = '1px solid rgba(239, 68, 68, 0.4)';
      fb.style.color = '#ef4444';
      fb.innerHTML = `❌ Network Error: ${e.message}`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Auto-initialize config when script loads
document.addEventListener('DOMContentLoaded', () => {
  initMarketMakingModule();
  pollActiveMmTrades();
  if (!mmTradePollTimer) {
    mmTradePollTimer = setInterval(pollActiveMmTrades, 2500);
  }

  // Delegated click handler on tbody for immediate 1-click fallback response
  const tbody = document.getElementById('mmTbody');
  if (tbody) {
    tbody.addEventListener('click', (e) => {
      const btn = e.target.closest('.mm-btn-1lot');
      if (!btn) return;
      // If button has an inline onclick, the browser already executed it directly; avoid double invocation
      if (btn.hasAttribute('onclick')) return;

      const sym = btn.dataset.symbol || btn.closest('tr')?.dataset?.symbol;
      if (!sym) return;
      const action = btn.dataset.action;
      if (action === 'in-trade') {
        handleActiveTradeBtnClick(sym);
      } else {
        open1LotMmModalBySymbol(sym);
      }
    });
  }
});

