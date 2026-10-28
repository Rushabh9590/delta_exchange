/**
 * ==============================================================================
 * DELTA EXCHANGE - LIVE OPTION CHAIN MODULE
 * Interactive Calls & Puts Matrix, ATM Strike Centering, ITM/OTM Shading,
 * Real-time WebSocket Price Feeds, In-Place Quote Updates, & 1-Click Multi-Leg Studio
 * ==============================================================================
 */

let ocCurrentUnderlying = (function () {
  try {
    return localStorage.getItem('delta_oc_underlying') || 'BTC';
  } catch (e) {
    return 'BTC';
  }
})();
let ocSelectedExpiry = '';
let ocCurrentExpiry = ''; // Sync alias for backwards compatibility
let ocAvailableExpiries = [];
let ocRawChainData = null;
let ocSearchQuery = '';
let ocIsFetching = false;
let ocCurrentSideFilter = 'both';

// High-performance Top-of-Book (L1) In-Memory Cache & Micro-batch Renderer
const ocOrderBooks = new Map(); // symUpper -> { lastBid: 0, lastAsk: 0, lastLtp: 0, bidQty: 0, askQty: 0 }
let ocPendingBatchUpdates = new Map();
let ocRafScheduled = false;

/**
 * Parses L1 Top of Book (l1_orderbook) updates with sub-millisecond execution and zero CPU sorting
 */
function processOptionChainL1Update(sym, data) {
  if (!data) return null;
  const symUpper = (sym || data.symbol || data.sy || '').toUpperCase();
  if (!symUpper) return null;

  const rawBp = data.best_bid !== undefined ? data.best_bid : (data.bp !== undefined ? data.bp : data.bid);
  const rawAp = data.best_ask !== undefined ? data.best_ask : (data.ap !== undefined ? data.ap : data.ask);
  const rawBs = data.bid_qty !== undefined ? data.bid_qty : (data.bs !== undefined ? data.bs : data.bid_size);
  const rawAs = data.ask_qty !== undefined ? data.ask_qty : (data.as !== undefined ? data.as : data.ask_size);

  let bestBid = (rawBp !== undefined && rawBp !== null && rawBp !== '' && rawBp !== '-') ? parseFloat(rawBp) : null;
  let bestAsk = (rawAp !== undefined && rawAp !== null && rawAp !== '' && rawAp !== '-') ? parseFloat(rawAp) : null;
  const bidQty = (rawBs !== undefined && rawBs !== null) ? parseFloat(rawBs) : null;
  const askQty = (rawAs !== undefined && rawAs !== null) ? parseFloat(rawAs) : null;

  // Fallback for legacy arrays if received
  if (bestBid === null && Array.isArray(data.bids) && data.bids.length > 0) {
    bestBid = parseFloat(data.bids[0][0]);
  }
  if (bestAsk === null && Array.isArray(data.asks) && data.asks.length > 0) {
    bestAsk = parseFloat(data.asks[0][0]);
  }

  if (bestBid !== null && isNaN(bestBid)) bestBid = null;
  if (bestAsk !== null && isNaN(bestAsk)) bestAsk = null;

  // Data Integrity Gate: Reject crossed book anomaly (ask < bid) during network transit
  if (bestBid !== null && bestAsk !== null && bestAsk < bestBid) {
    return null;
  }

  let book = ocOrderBooks.get(symUpper);
  if (!book) {
    book = { lastBid: 0, lastAsk: 0, lastLtp: 0, bidQty: 0, askQty: 0 };
    ocOrderBooks.set(symUpper, book);
  }
  if (bestBid !== null) book.lastBid = bestBid;
  if (bestAsk !== null) book.lastAsk = bestAsk;
  if (bidQty !== null) book.bidQty = bidQty;
  if (askQty !== null) book.askQty = askQty;

  return { bestBid, bestAsk, bidQty, askQty };
}

/**
 * Synchronous instant cell updater: updates DOM elements immediately upon L1 packet arrival (<0.05ms)
 */
function applyOptionChainCellUpdate(symUpper, u) {
  if (!symUpper || !u) return;

  let book = ocOrderBooks.get(symUpper);
  if (!book) {
    book = { lastBid: 0, lastAsk: 0, lastLtp: 0, bidQty: 0, askQty: 0 };
    ocOrderBooks.set(symUpper, book);
  }

  // 1. LTP
  if (u.ltp !== undefined && u.ltp !== null && Number(u.ltp) > 0) {
    const elLtp = document.getElementById(`ocLtp_${symUpper}`);
    if (elLtp) {
      const numLtp = Number(u.ltp);
      elLtp.textContent = `$${formatNumber(numLtp, numLtp >= 100 ? 1 : (numLtp >= 1 ? 2 : 4))}`;
      if (u.flash && book.lastLtp > 0 && numLtp !== book.lastLtp) {
        const flashCls = numLtp > book.lastLtp ? 'oc-flash-up' : 'oc-flash-down';
        elLtp.classList.add(flashCls);
        setTimeout(() => elLtp.classList.remove(flashCls), 250);
      }
      book.lastLtp = numLtp;
    }
  } else if (u.markPrice && (!document.getElementById(`ocLtp_${symUpper}`)?.textContent || document.getElementById(`ocLtp_${symUpper}`)?.textContent === '--')) {
    const elLtp = document.getElementById(`ocLtp_${symUpper}`);
    if (elLtp) elLtp.textContent = `~$${formatNumber(Number(u.markPrice), 2)}`;
  }

  // 2. Mark Price
  if (u.markPrice !== undefined && u.markPrice !== null && Number(u.markPrice) > 0) {
    const elMark = document.getElementById(`ocMark_${symUpper}`);
    if (elMark) {
      elMark.textContent = `$${formatNumber(Number(u.markPrice), 2)}`;
    }
  }

  // 3. Bid
  if (u.bid !== undefined && u.bid !== null && Number(u.bid) > 0) {
    const elBid = document.getElementById(`ocBid_${symUpper}`);
    if (elBid) {
      const numBid = Number(u.bid);
      elBid.textContent = `$${formatNumber(numBid, numBid >= 100 ? 1 : (numBid >= 1 ? 2 : 4))}`;
      if (u.flash && book.lastBid > 0 && numBid !== book.lastBid) {
        const flashCls = numBid > book.lastBid ? 'oc-flash-up' : 'oc-flash-down';
        elBid.classList.add(flashCls);
        setTimeout(() => elBid.classList.remove(flashCls), 250);
      }
      book.lastBid = numBid;
    }
  }

  // 4. Ask
  if (u.ask !== undefined && u.ask !== null && Number(u.ask) > 0) {
    const elAsk = document.getElementById(`ocAsk_${symUpper}`);
    if (elAsk) {
      const numAsk = Number(u.ask);
      elAsk.textContent = `$${formatNumber(numAsk, numAsk >= 100 ? 1 : (numAsk >= 1 ? 2 : 4))}`;
      if (u.flash && book.lastAsk > 0 && numAsk !== book.lastAsk) {
        const flashCls = numAsk < book.lastAsk ? 'oc-flash-up' : 'oc-flash-down';
        elAsk.classList.add(flashCls);
        setTimeout(() => elAsk.classList.remove(flashCls), 250);
      }
      book.lastAsk = numAsk;
    }
  }

  // 5. Volume
  if (u.vol !== undefined && u.vol !== null) {
    const elVol = document.getElementById(`ocVol_${symUpper}`);
    if (elVol) {
      const numVol = Number(u.vol);
      elVol.textContent = numVol > 0 ? (numVol >= 10 ? formatNumber(numVol, 0) : formatNumber(numVol, 2)) : '0';
    }
  }

  // 6. OI
  if (u.oi !== undefined && u.oi !== null) {
    const elOi = document.getElementById(`ocOi_${symUpper}`);
    if (elOi) {
      const numOi = Number(u.oi);
      elOi.textContent = numOi > 0 ? (numOi >= 10 ? formatNumber(numOi, 0) : formatNumber(numOi, 2)) : '0';
    }
  }
}

function queueOptionChainCellUpdate(sym, updateObj) {
  if (!sym || !updateObj) return;
  applyOptionChainCellUpdate(sym.toUpperCase(), updateObj);
}

/**
 * Handles Side Filtering (Both Matrix / Calls Only / Puts Only) for mobile & desktop
 */
function setOptionChainSideFilter(side = 'both', btnEl = null) {
  ocCurrentSideFilter = side;
  document.querySelectorAll('.oc-side-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.side === side);
  });

  const wrapper = document.getElementById('ocTableWrapper');
  if (wrapper) {
    wrapper.classList.remove('view-calls-only', 'view-puts-only');
    if (side === 'calls') {
      wrapper.classList.add('view-calls-only');
    } else if (side === 'puts') {
      wrapper.classList.add('view-puts-only');
    }

    if (side === 'both' && window.innerWidth <= 768) {
      setTimeout(() => {
        const scrollTarget = (wrapper.scrollWidth - wrapper.clientWidth) / 2;
        if (scrollTarget > 0) wrapper.scrollTo({ left: scrollTarget, behavior: 'smooth' });
      }, 100);
    }
  }
}

/**
 * Loads and renders live option chain for selected asset and expiration
 * @param {string} underlying - 'BTC', 'ETH', etc.
 * @param {string} expiry - Optional DDMMYY expiry code
 * @param {boolean} isBackground - If true, performs non-destructive in-place DOM updates
 */
async function loadOptionChain(underlying = 'BTC', expiry = '', isBackground = false) {
  if (ocIsFetching && isBackground) return;
  ocIsFetching = true;

  const cleanAsset = (underlying || 'BTC').toUpperCase();
  const safeUnderlying = cleanAsset === 'ETH' ? 'ETH' : 'BTC';
  const prevUnderlying = ocCurrentUnderlying;
  const prevExpiry = ocSelectedExpiry;
  if (prevUnderlying !== safeUnderlying || (expiry && expiry !== prevExpiry)) {
    ocOrderBooks.clear();
  }
  ocCurrentUnderlying = safeUnderlying;
  try {
    localStorage.setItem('delta_oc_underlying', safeUnderlying);
  } catch (e) { }

  // Update asset switcher buttons in Option Chain header
  document.querySelectorAll('.oc-asset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.asset === safeUnderlying);
  });

  const tbody = document.getElementById('ocTbody');
  if (!isBackground && tbody && (!ocRawChainData || prevUnderlying !== underlying)) {
    tbody.innerHTML = '<tr><td colspan="15" style="text-align:center; padding: 40px; color: var(--text-muted);">🔄 Loading live option chain matrix...</td></tr>';
  }

  try {
    const data = await ApiService.getOptionChain(underlying, expiry);
    if (data && data.success) {
      ocRawChainData = data;
      ocSelectedExpiry = data.selected_expiry || '';
      ocCurrentExpiry = ocSelectedExpiry;
      window.ocSelectedExpiry = ocSelectedExpiry;
      window.ocCurrentExpiry = ocSelectedExpiry;
      ocAvailableExpiries = data.expiries || [];

      // Populate Expiry Dropdown
      const selExpiry = document.getElementById('ocSelExpiry');
      if (selExpiry) {
        if (ocAvailableExpiries.length > 0) {
          let opts = '';
          ocAvailableExpiries.forEach(exp => {
            const isSel = exp.expiry === ocSelectedExpiry ? 'selected' : '';
            const totalStr = exp.total_strikes ? ` (${exp.total_strikes} strikes)` : '';
            opts += `<option value="${exp.expiry}" ${isSel}>${exp.label || exp.expiry} [${exp.expiry}]${totalStr}</option>`;
          });
          if (selExpiry.innerHTML !== opts) {
            selExpiry.innerHTML = opts;
          }
        } else {
          selExpiry.innerHTML = '<option value="">No Active Expiries</option>';
        }
      }

      // Update Header Badges (Spot, ATM, Spec)
      const spotUSD = data.spot_price || 0;
      const spotINR = spotUSD * (typeof usdToInrRate !== 'undefined' ? usdToInrRate : 87.5);
      const spotEl = document.getElementById('ocSpotPriceVal');
      if (spotEl) {
        spotEl.textContent = spotUSD > 0 ? `$${formatNumber(spotUSD, 1)} (₹${formatNumber(spotINR, 0)})` : '--';
      }

      const atmEl = document.getElementById('ocAtmVal');
      if (atmEl) {
        atmEl.textContent = data.atm_strike ? formatNumber(data.atm_strike, 0) : '--';
      }

      const lotSpecEl = document.getElementById('ocLotSpec');
      if (lotSpecEl) {
        const val = data.contract_value || (underlying === 'BTC' ? 0.001 : (underlying === 'ETH' ? 0.01 : 1.0));
        lotSpecEl.textContent = `💡 1 Lot = ${val} ${underlying}`;
      }

      // Collect all symbols for WebSocket subscription
      const symbolsToSub = [];
      (data.chain || []).forEach(row => {
        if (row.call && row.call.symbol) symbolsToSub.push(row.call.symbol);
        if (row.put && row.put.symbol) symbolsToSub.push(row.put.symbol);
      });
      if (typeof addSubscribedSymbols === 'function' && symbolsToSub.length > 0) {
        addSubscribedSymbols(symbolsToSub);
      }

      // If user switched underlying/expiry, re-render full table structure
      const needsFullRender = !isBackground || (prevUnderlying !== underlying) || (prevExpiry !== ocSelectedExpiry) || !tbody || tbody.children.length <= 1;

      if (needsFullRender) {
        renderOptionChainTable();
      } else {
        // In-place refresh of all live data cells to prevent UI clearing / blinking
        updateAllChainCellsFromData(data.chain);
      }

      // Fetch batch tickers to ensure all recent quotes are hot
      if (symbolsToSub.length > 0) {
        fetchOptionChainBatchQuotes(symbolsToSub);
      }
    } else {
      if (tbody && !isBackground) {
        tbody.innerHTML = `<tr><td colspan="15" style="text-align:center; padding: 40px; color: var(--accent-red);">❌ Failed to load option chain: ${data?.error || 'Unknown error'}</td></tr>`;
      }
    }
  } catch (err) {
    console.error('[OptionChain] Error loading chain:', err);
    if (tbody && !isBackground) {
      tbody.innerHTML = `<tr><td colspan="15" style="text-align:center; padding: 40px; color: var(--accent-red);">❌ Error loading option chain: ${err.message}</td></tr>`;
    }
  } finally {
    ocIsFetching = false;
  }
}

/**
 * Updates all cells in the Option Chain table in-place without rebuilding DOM
 */
function updateAllChainCellsFromData(chainRows) {
  if (!chainRows || !Array.isArray(chainRows)) return;
  chainRows.forEach(row => {
    if (row.call && row.call.symbol) {
      const b = ocOrderBooks.get(row.call.symbol.toUpperCase());
      // Preserve live WebSocket best bid/ask if already active in memory
      const bid = (b && b.lastBid > 0) ? b.lastBid : row.call.best_bid;
      const ask = (b && b.lastAsk > 0) ? b.lastAsk : row.call.best_ask;
      updateOptionChainRowQuote(
        row.call.symbol,
        row.call.ltp,
        row.call.mark_price,
        bid,
        ask,
        row.call.volume,
        row.call.open_interest,
        false
      );
    }
    if (row.put && row.put.symbol) {
      const b = ocOrderBooks.get(row.put.symbol.toUpperCase());
      const bid = (b && b.lastBid > 0) ? b.lastBid : row.put.best_bid;
      const ask = (b && b.lastAsk > 0) ? b.lastAsk : row.put.best_ask;
      updateOptionChainRowQuote(
        row.put.symbol,
        row.put.ltp,
        row.put.mark_price,
        bid,
        ask,
        row.put.volume,
        row.put.open_interest,
        false
      );
    }
  });
}

/**
 * Fetches batch live quotes for all visible option contracts in the chain
 */
async function fetchOptionChainBatchQuotes(symbols) {
  if (!symbols || symbols.length === 0) return;
  try {
    const res = await ApiService.getBatchTickers(symbols);
    if (res && res.success && res.tickers) {
      Object.values(res.tickers).forEach(tick => {
        if (tick && tick.symbol) {
          const symUpper = tick.symbol.toUpperCase();
          const b = ocOrderBooks.get(symUpper);
          const bid = (b && b.lastBid > 0) ? b.lastBid : tick.bid;
          const ask = (b && b.lastAsk > 0) ? b.lastAsk : tick.ask;
          updateOptionChainRowQuote(
            tick.symbol,
            tick.ltp,
            tick.mark_price,
            bid,
            ask,
            tick.volume,
            tick.oi,
            false
          );
        }
      });
    }
  } catch (e) { }
}

/**
 * In-place DOM updater for an individual contract row quote
 */
function updateOptionChainRowQuote(sym, ltp, markPrice, bid, ask, vol, oi, flash = false) {
  if (!sym) return;
  queueOptionChainCellUpdate(sym, { ltp, markPrice, bid, ask, vol, oi, flash });
}

/**
 * Search strike change handler
 */
function onOptionChainSearch(val) {
  ocSearchQuery = (val || '').trim();
  renderOptionChainTable();
}

/**
 * Renders the Option Chain Matrix Table (Shows ALL strikes)
 */
function renderOptionChainTable() {
  if (!ocRawChainData || !ocRawChainData.chain) return;

  const tbody = document.getElementById('ocTbody');
  if (!tbody) return;

  const chain = ocRawChainData.chain;
  const spotPrice = ocRawChainData.spot_price || 0;
  const atmStrike = ocRawChainData.atm_strike;

  // Show ALL strikes by default
  let visibleRows = chain;

  // Apply Search Query if typed by user
  if (ocSearchQuery) {
    visibleRows = visibleRows.filter(r => r.strike.toString().includes(ocSearchQuery));
  }

  const countBadge = document.getElementById('ocStrikesCountBadge');
  if (countBadge) {
    countBadge.textContent = `${visibleRows.length} Strikes`;
  }

  let html = '';
  visibleRows.forEach((row) => {
    const strike = row.strike;
    const isAtm = (strike === atmStrike) || (row.is_atm);
    const rowClass = isAtm ? 'oc-row-atm' : '';

    // CALL Side Data
    const call = row.call || {};
    const callSym = (call.symbol || '').toUpperCase();
    const isCallItm = (spotPrice > 0 && strike < spotPrice);
    const callLtp = call.ltp !== undefined && call.ltp !== null && Number(call.ltp) > 0 ? Number(call.ltp) : null;
    const callMark = call.mark_price || 0;
    const callBid = call.best_bid !== undefined && call.best_bid !== null && Number(call.best_bid) > 0 ? `$${formatNumber(call.best_bid, call.best_bid >= 100 ? 1 : 2)}` : '--';
    const callAsk = call.best_ask !== undefined && call.best_ask !== null && Number(call.best_ask) > 0 ? `$${formatNumber(call.best_ask, call.best_ask >= 100 ? 1 : 2)}` : '--';
    const callVol = call.volume !== undefined && call.volume !== null ? (Number(call.volume) >= 10 ? formatNumber(call.volume, 0) : formatNumber(call.volume, 2)) : '--';
    const callOi = call.open_interest !== undefined && call.open_interest !== null ? (Number(call.open_interest) >= 10 ? formatNumber(call.open_interest, 0) : formatNumber(call.open_interest, 2)) : '--';

    const callBidNum = (call.best_bid !== undefined && call.best_bid !== null && !isNaN(Number(call.best_bid))) ? Number(call.best_bid) : 0;
    const callAskNum = (call.best_ask !== undefined && call.best_ask !== null && !isNaN(Number(call.best_ask))) ? Number(call.best_ask) : 0;
    const callTick = Number(call.tick_size) || (ocCurrentUnderlying === 'BTC' ? 0.1 : 0.01);
    const callCv = Number(call.contract_value) || (ocCurrentUnderlying === 'BTC' ? 0.001 : 0.01);
    const safeCallSym = callSym || (ocSelectedExpiry ? `C-${ocCurrentUnderlying}-${strike}-${ocSelectedExpiry}` : '');

    // PUT Side Data
    const put = row.put || {};
    const putSym = (put.symbol || '').toUpperCase();
    const isPutItm = (spotPrice > 0 && strike > spotPrice);
    const putLtp = put.ltp !== undefined && put.ltp !== null && Number(put.ltp) > 0 ? Number(put.ltp) : null;
    const putMark = put.mark_price || 0;
    const putBid = put.best_bid !== undefined && put.best_bid !== null && Number(put.best_bid) > 0 ? `$${formatNumber(put.best_bid, put.best_bid >= 100 ? 1 : 2)}` : '--';
    const putAsk = put.best_ask !== undefined && put.best_ask !== null && Number(put.best_ask) > 0 ? `$${formatNumber(put.best_ask, put.best_ask >= 100 ? 1 : 2)}` : '--';
    const putVol = put.volume !== undefined && put.volume !== null ? (Number(put.volume) >= 10 ? formatNumber(put.volume, 0) : formatNumber(put.volume, 2)) : '--';
    const putOi = put.open_interest !== undefined && put.open_interest !== null ? (Number(put.open_interest) >= 10 ? formatNumber(put.open_interest, 0) : formatNumber(put.open_interest, 2)) : '--';

    const putBidNum = (put.best_bid !== undefined && put.best_bid !== null && !isNaN(Number(put.best_bid))) ? Number(put.best_bid) : 0;
    const putAskNum = (put.best_ask !== undefined && put.best_ask !== null && !isNaN(Number(put.best_ask))) ? Number(put.best_ask) : 0;
    const putTick = Number(put.tick_size) || (ocCurrentUnderlying === 'BTC' ? 0.1 : 0.01);
    const putCv = Number(put.contract_value) || (ocCurrentUnderlying === 'BTC' ? 0.001 : 0.01);
    const safePutSym = putSym || (ocSelectedExpiry ? `P-${ocCurrentUnderlying}-${strike}-${ocSelectedExpiry}` : '');

    const callLtpDisplay = callLtp !== null ? `$${formatNumber(callLtp, callLtp >= 100 ? 1 : (callLtp >= 1 ? 2 : 4))}` : (callMark > 0 ? `~$${formatNumber(callMark, 2)}` : '--');
    const putLtpDisplay = putLtp !== null ? `$${formatNumber(putLtp, putLtp >= 100 ? 1 : (putLtp >= 1 ? 2 : 4))}` : (putMark > 0 ? `~$${formatNumber(putMark, 2)}` : '--');

    const callKey = callSym || `C_${strike}`;
    const putKey = putSym || `P_${strike}`;

    html += `
      <tr class="${rowClass}">
        <!-- CALLS: OI & Volume -->
        <td class="col-call-side col-oi ${isCallItm ? 'oc-itm-call' : ''}" id="ocOi_${callKey}" style="color: var(--text-muted); font-size: 0.72rem;">${callOi}</td>
        <td class="col-call-side col-vol ${isCallItm ? 'oc-itm-call' : ''}" id="ocVol_${callKey}" style="color: var(--text-secondary);">${callVol}</td>

        <!-- CALLS: Bid / Ask -->
        <td class="col-call-side col-bid ${isCallItm ? 'oc-itm-call' : ''}" id="ocBid_${callKey}" style="color: var(--accent-green); font-size: 0.74rem; cursor: pointer;" title="Sell Call @ Bid" onclick="triggerOptionOrder('${safeCallSym}', 'sell', 'call', ${strike}, ${callBidNum}, ${callAskNum}, ${callTick}, ${callCv})">${callBid}</td>
        <td class="col-call-side col-ask ${isCallItm ? 'oc-itm-call' : ''}" id="ocAsk_${callKey}" style="color: var(--accent-red); font-size: 0.74rem; cursor: pointer;" title="Buy Call @ Ask" onclick="triggerOptionOrder('${safeCallSym}', 'buy', 'call', ${strike}, ${callBidNum}, ${callAskNum}, ${callTick}, ${callCv})">${callAsk}</td>

        <!-- CALLS: Mark Price -->
        <td class="col-call-side col-mark ${isCallItm ? 'oc-itm-call' : ''}" id="ocMark_${callKey}" style="color: var(--text-secondary); font-size: 0.75rem;">$${formatNumber(callMark, 2)}</td>

        <!-- CALLS: Live Traded LTP -->
        <td class="col-call-side col-ltp ${isCallItm ? 'oc-itm-call' : ''}">
          <span class="oc-ltp-val oc-call-ltp" id="ocLtp_${callKey}" title="Buy Call (Open Order Window)" onclick="triggerOptionOrder('${safeCallSym}', 'buy', 'call', ${strike}, ${callBidNum}, ${callAskNum}, ${callTick}, ${callCv})">
            ${callLtpDisplay}
          </span>
        </td>

        <!-- CALLS: 1-Click Action Buttons -->
        <td class="col-call-side col-trade ${isCallItm ? 'oc-itm-call' : ''}">
          <div class="oc-actions-cell">
            <button type="button" class="oc-btn-buy" title="Buy Call" onclick="triggerOptionOrder('${safeCallSym}', 'buy', 'call', ${strike}, ${callBidNum}, ${callAskNum}, ${callTick}, ${callCv})">B</button>
            <button type="button" class="oc-btn-sell" title="Sell Call" onclick="triggerOptionOrder('${safeCallSym}', 'sell', 'call', ${strike}, ${callBidNum}, ${callAskNum}, ${callTick}, ${callCv})">S</button>
          </div>
        </td>

        <!-- CENTER: STRIKE PRICE -->
        <td class="col-strike oc-strike-cell">
          <span>${formatNumber(strike, 0)}</span>
          ${isAtm ? '<span class="oc-atm-tag">ATM</span>' : ''}
        </td>

        <!-- PUTS: 1-Click Action Buttons -->
        <td class="col-put-side col-trade ${isPutItm ? 'oc-itm-put' : ''}">
          <div class="oc-actions-cell">
            <button type="button" class="oc-btn-buy" title="Buy Put" onclick="triggerOptionOrder('${safePutSym}', 'buy', 'put', ${strike}, ${putBidNum}, ${putAskNum}, ${putTick}, ${putCv})">B</button>
            <button type="button" class="oc-btn-sell" title="Sell Put" onclick="triggerOptionOrder('${safePutSym}', 'sell', 'put', ${strike}, ${putBidNum}, ${putAskNum}, ${putTick}, ${putCv})">S</button>
          </div>
        </td>

        <!-- PUTS: Live Traded LTP -->
        <td class="col-put-side col-ltp ${isPutItm ? 'oc-itm-put' : ''}">
          <span class="oc-ltp-val oc-put-ltp" id="ocLtp_${putKey}" title="Buy Put (Open Order Window)" onclick="triggerOptionOrder('${safePutSym}', 'buy', 'put', ${strike}, ${putBidNum}, ${putAskNum}, ${putTick}, ${putCv})">
            ${putLtpDisplay}
          </span>
        </td>

        <!-- PUTS: Mark Price -->
        <td class="col-put-side col-mark ${isPutItm ? 'oc-itm-put' : ''}" id="ocMark_${putKey}" style="color: var(--text-secondary); font-size: 0.75rem;">$${formatNumber(putMark, 2)}</td>

        <!-- PUTS: Bid / Ask -->
        <td class="col-put-side col-bid ${isPutItm ? 'oc-itm-put' : ''}" id="ocBid_${putKey}" style="color: var(--accent-green); font-size: 0.74rem; cursor: pointer;" title="Sell Put @ Bid" onclick="triggerOptionOrder('${safePutSym}', 'sell', 'put', ${strike}, ${putBidNum}, ${putAskNum}, ${putTick}, ${putCv})">${putBid}</td>
        <td class="col-put-side col-ask ${isPutItm ? 'oc-itm-put' : ''}" id="ocAsk_${putKey}" style="color: var(--accent-red); font-size: 0.74rem; cursor: pointer;" title="Buy Put @ Ask" onclick="triggerOptionOrder('${safePutSym}', 'buy', 'put', ${strike}, ${putBidNum}, ${putAskNum}, ${putTick}, ${putCv})">${putAsk}</td>

        <!-- PUTS: Volume & OI -->
        <td class="col-put-side col-vol ${isPutItm ? 'oc-itm-put' : ''}" id="ocVol_${putKey}" style="color: var(--text-secondary);">${putVol}</td>
        <td class="col-put-side col-oi ${isPutItm ? 'oc-itm-put' : ''}" id="ocOi_${putKey}" style="color: var(--text-muted); font-size: 0.72rem;">${putOi}</td>
      </tr>
    `;
  });

  tbody.innerHTML = html;

  // Apply active side filter (Calls / Puts / Both)
  const wrapper = document.getElementById('ocTableWrapper') || document.querySelector('.oc-table-wrapper');
  if (wrapper) {
    wrapper.classList.remove('view-calls-only', 'view-puts-only');
    if (ocCurrentSideFilter === 'calls') {
      wrapper.classList.add('view-calls-only');
    } else if (ocCurrentSideFilter === 'puts') {
      wrapper.classList.add('view-puts-only');
    } else if (window.innerWidth <= 768) {
      setTimeout(() => {
        const scrollTarget = (wrapper.scrollWidth - wrapper.clientWidth) / 2;
        if (scrollTarget > 0 && wrapper.scrollLeft === 0) {
          wrapper.scrollTo({ left: scrollTarget, behavior: 'smooth' });
        }
      }, 120);
    }
  }
}

/**
 * Seamless Option Order Window: Opens the modern Buying / Selling & Armed Target Ladder Modal
 */
function triggerOptionOrder(symbol, side = 'buy', optionType = 'call', strike = 0, quoteDataOrBid = 0, askParam = 0, tickParam = 0.1, cvParam = 0.001) {
  try {
    const activeExpiry = ocSelectedExpiry || (typeof ocCurrentExpiry !== 'undefined' ? ocCurrentExpiry : '') || '';
    const activeUnderlying = ocCurrentUnderlying || 'BTC';
    const optType = (optionType || 'call').toLowerCase();
    const optSide = (side || 'buy').toLowerCase();
    const strikeNum = parseFloat(strike) || 0;

    // Resolve symbol if empty
    let sym = (symbol || '').trim().toUpperCase();
    if (!sym && strikeNum > 0 && activeExpiry) {
      sym = `${optType === 'put' ? 'P' : 'C'}-${activeUnderlying}-${strikeNum}-${activeExpiry}`;
    }

    if (!sym && ocRawChainData && Array.isArray(ocRawChainData.chain)) {
      const row = ocRawChainData.chain.find(r => Number(r.strike) === Number(strikeNum));
      if (row) {
        const item = optType === 'put' ? row.put : row.call;
        if (item && item.symbol) {
          sym = item.symbol.toUpperCase();
        }
      }
    }

    if (!sym) {
      if (typeof showToast === 'function') {
        showToast(`Contract symbol not found for ${optType.toUpperCase()} ${strikeNum}`, 'error');
      } else {
        alert(`Contract symbol not found for ${optType.toUpperCase()} ${strikeNum}`);
      }
      return;
    }

    // Extract quote values safely
    let bid = 0;
    let ask = 0;
    let tickSize = (activeUnderlying === 'BTC' ? 0.1 : 0.01);
    let contractValue = (activeUnderlying === 'BTC' ? 0.001 : 0.01);
    let bidQty = 1850;
    let askQty = 5365;

    if (typeof quoteDataOrBid === 'object' && quoteDataOrBid !== null) {
      bid = parseFloat(quoteDataOrBid.bid || 0) || 0;
      ask = parseFloat(quoteDataOrBid.ask || 0) || 0;
      if (quoteDataOrBid.tickSize) tickSize = parseFloat(quoteDataOrBid.tickSize);
      if (quoteDataOrBid.contractValue) contractValue = parseFloat(quoteDataOrBid.contractValue);
      if (quoteDataOrBid.bidQty) bidQty = quoteDataOrBid.bidQty;
      if (quoteDataOrBid.askQty) askQty = quoteDataOrBid.askQty;
    } else {
      bid = parseFloat(quoteDataOrBid || 0) || 0;
      ask = parseFloat(askParam || 0) || 0;
      if (tickParam) tickSize = parseFloat(tickParam) || tickSize;
      if (cvParam) contractValue = parseFloat(cvParam) || contractValue;
    }

    // Check orderbook cache for real-time live quotes if available
    const liveBook = ocOrderBooks.get(sym);
    if (liveBook) {
      if (liveBook.lastBid > 0) bid = liveBook.lastBid;
      if (liveBook.lastAsk > 0) ask = liveBook.lastAsk;
      if (liveBook.bidQty > 0) bidQty = liveBook.bidQty;
      if (liveBook.askQty > 0) askQty = liveBook.askQty;
    }

    const payload = {
      symbol: sym,
      side: optSide,
      optionType: optType,
      strike: strikeNum,
      expiry: activeExpiry,
      underlying: activeUnderlying,
      tickSize: tickSize,
      contractValue: contractValue,
      bid: bid,
      ask: ask,
      bidQty: bidQty,
      askQty: askQty
    };

    console.log('[triggerOptionOrder] Opening order modal with payload:', payload);

    if (typeof openOptionOrderModal === 'function') {
      openOptionOrderModal(payload);
    } else if (typeof window.openOptionOrderModal === 'function') {
      window.openOptionOrderModal(payload);
    } else if (typeof addOptionLegToStudio === 'function') {
      addOptionLegToStudio(sym, optSide, optType, strikeNum, bid || ask || 0);
    } else {
      console.error('Neither openOptionOrderModal nor addOptionLegToStudio is defined.');
      if (typeof showToast === 'function') {
        showToast('Order modal component not initialized. Please refresh page.', 'error');
      }
    }
  } catch (err) {
    console.error('[triggerOptionOrder] Exception:', err);
    if (typeof showToast === 'function') {
      showToast('Error opening order window: ' + err.message, 'error');
    }
  }
}

// Explicitly bind to window
window.triggerOptionOrder = triggerOptionOrder;

/**
 * Seamless 1-Click Integration: Adds option contract from Option Chain directly into Multi-Leg Studio
 */
function addOptionLegToStudio(symbol, side = 'buy', optionType = 'call', strike = 0, limitPrice = 0) {
  if (!symbol) {
    showToast('Contract symbol not available for this strike.', 'error');
    return;
  }

  const sz = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;

  // Add into legRows in multileg.js
  if (typeof legRows !== 'undefined') {
    legRows.push({
      side: side.toLowerCase(),
      option_type: optionType.toLowerCase(),
      strike: strike,
      symbol: symbol,
      order_type: 'limit',
      size: sz,
      limit_price: limitPrice > 0 ? limitPrice : '',
      use_full_margin: false
    });

    if (typeof renderLegRows === 'function') {
      renderLegRows();
    }
  }

  showToast(`⚡ Added ${side.toUpperCase()} ${symbol} to Multi-Leg Studio!`, 'success');

  // Smoothly switch to Studio tab so user sees their strategy
  switchNavTab('studio');
}

/**
 * Real-time WebSocket tick handler for live option chain
 */
function handleOptionChainTickUpdate(data) {
  if (!data || activeNavTab !== 'chain') return;
  const sym = (data.symbol || data.sy || '').toUpperCase();
  if (!sym) return;

  const msgType = data.type || data.channel || data.name || '';

  // 1. Live Order Book (Best Bid & Best Ask) via L1 Orderbook (l1_orderbook)
  if (msgType === 'l1_orderbook' || msgType === 'ob_l1' || data.best_bid !== undefined || data.bp !== undefined || msgType === 'l2_updates' || data.bids !== undefined || data.asks !== undefined) {
    const l1Res = processOptionChainL1Update(sym, data);
    if (l1Res && (l1Res.bestBid !== null || l1Res.bestAsk !== null)) {
      if (typeof updateOptionOrderModalTick === 'function') {
        updateOptionOrderModalTick(sym, {
          bid: l1Res.bestBid,
          ask: l1Res.bestAsk,
          bidQty: l1Res.bidQty,
          askQty: l1Res.askQty
        });
      }
      queueOptionChainCellUpdate(sym, {
        bid: l1Res.bestBid,
        ask: l1Res.bestAsk,
        flash: true
      });
    }
    return;
  }

  // 2. Live Mark Price
  if (msgType === 'mark_price' || (data.mark_price !== undefined && msgType !== 'v2/ticker')) {
    const markVal = Number(data.mark_price);
    if (!isNaN(markVal) && markVal > 0) {
      queueOptionChainCellUpdate(sym, {
        markPrice: markVal
      });
    }
    return;
  }

  // 3. Live Trades / LTP, Volume, OI & Top Quotes via v2/ticker
  const quotes = data.quotes || {};
  const closeVal = data.close;
  const bestBid = quotes.best_bid !== undefined ? quotes.best_bid : data.bid;
  const bestAsk = quotes.best_ask !== undefined ? quotes.best_ask : data.ask;
  const markVal = data.mark_price;
  const volVal = data.volume;
  const oiVal = data.oi !== undefined ? data.oi : (data.oi_contracts !== undefined ? data.oi_contracts : data.open_interest);
  const ltp = closeVal !== undefined && closeVal !== null ? Number(closeVal) : (data.ltp !== undefined && data.ltp !== null ? Number(data.ltp) : null);

  const updatePayload = { flash: true };
  if (ltp !== null && !isNaN(ltp) && ltp > 0) updatePayload.ltp = ltp;
  if (markVal !== undefined && markVal !== null && !isNaN(Number(markVal)) && Number(markVal) > 0) updatePayload.markPrice = Number(markVal);
  if (bestBid !== undefined && bestBid !== null && !isNaN(Number(bestBid)) && Number(bestBid) > 0) updatePayload.bid = Number(bestBid);
  if (bestAsk !== undefined && bestAsk !== null && !isNaN(Number(bestAsk)) && Number(bestAsk) > 0) updatePayload.ask = Number(bestAsk);
  if (volVal !== undefined && volVal !== null && !isNaN(Number(volVal))) updatePayload.vol = Number(volVal);
  if (oiVal !== undefined && oiVal !== null && !isNaN(Number(oiVal))) updatePayload.oi = Number(oiVal);

  if (typeof updateOptionOrderModalTick === 'function') {
    updateOptionOrderModalTick(sym, {
      bid: bestBid,
      ask: bestAsk,
      bidQty: quotes.best_bid_size,
      askQty: quotes.best_ask_size
    });
  }

  queueOptionChainCellUpdate(sym, updatePayload);
}
