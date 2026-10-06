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
let ocAvailableExpiries = [];
let ocRawChainData = null;
let ocSearchQuery = '';
let ocIsFetching = false;
let ocCurrentSideFilter = 'both';

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

  const prevUnderlying = ocCurrentUnderlying;
  const prevExpiry = ocSelectedExpiry;
  ocCurrentUnderlying = underlying;
  try {
    localStorage.setItem('delta_oc_underlying', underlying);
  } catch (e) { }

  // Update asset switcher buttons in Option Chain header
  document.querySelectorAll('.oc-asset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.asset === underlying);
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
      ocAvailableExpiries = data.expiries || [];

      // Populate Expiry Dropdown
      const selExpiry = document.getElementById('ocSelExpiry');
      if (selExpiry && ocAvailableExpiries.length > 0) {
        let opts = '';
        ocAvailableExpiries.forEach(exp => {
          const isSel = exp.expiry === ocSelectedExpiry ? 'selected' : '';
          const totalStr = exp.total_strikes ? ` (${exp.total_strikes} strikes)` : '';
          opts += `<option value="${exp.expiry}" ${isSel}>${exp.label || exp.expiry} [${exp.expiry}]${totalStr}</option>`;
        });
        if (selExpiry.innerHTML !== opts) {
          selExpiry.innerHTML = opts;
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
      updateOptionChainRowQuote(
        row.call.symbol,
        row.call.ltp,
        row.call.mark_price,
        row.call.best_bid,
        row.call.best_ask,
        row.call.volume,
        row.call.open_interest
      );
    }
    if (row.put && row.put.symbol) {
      updateOptionChainRowQuote(
        row.put.symbol,
        row.put.ltp,
        row.put.mark_price,
        row.put.best_bid,
        row.put.best_ask,
        row.put.volume,
        row.put.open_interest
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
          updateOptionChainRowQuote(
            tick.symbol,
            tick.ltp,
            tick.mark_price,
            tick.bid,
            tick.ask,
            tick.volume,
            tick.oi
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
  const symUpper = sym.toUpperCase();

  // 1. LTP
  const elLtp = document.getElementById(`ocLtp_${symUpper}`);
  if (elLtp) {
    if (ltp !== undefined && ltp !== null && Number(ltp) > 0) {
      const numLtp = Number(ltp);
      elLtp.textContent = `$${formatNumber(numLtp, numLtp >= 100 ? 1 : (numLtp >= 1 ? 2 : 4))}`;
      if (flash) {
        elLtp.classList.add('oc-flash-up');
        setTimeout(() => elLtp.classList.remove('oc-flash-up'), 300);
      }
    } else if (markPrice && (!elLtp.textContent || elLtp.textContent === '--')) {
      elLtp.textContent = `~$${formatNumber(markPrice, 2)}`;
    }
  }

  // 2. Mark Price
  const elMark = document.getElementById(`ocMark_${symUpper}`);
  if (elMark && markPrice !== undefined && markPrice !== null && Number(markPrice) > 0) {
    elMark.textContent = `$${formatNumber(markPrice, 2)}`;
  }

  // 3. Bid
  const elBid = document.getElementById(`ocBid_${symUpper}`);
  if (elBid && bid !== undefined && bid !== null && Number(bid) > 0) {
    const numBid = Number(bid);
    elBid.textContent = `$${formatNumber(numBid, numBid >= 100 ? 1 : (numBid >= 1 ? 2 : 4))}`;
  }

  // 4. Ask
  const elAsk = document.getElementById(`ocAsk_${symUpper}`);
  if (elAsk && ask !== undefined && ask !== null && Number(ask) > 0) {
    const numAsk = Number(ask);
    elAsk.textContent = `$${formatNumber(numAsk, numAsk >= 100 ? 1 : (numAsk >= 1 ? 2 : 4))}`;
  }

  // 5. Volume
  const elVol = document.getElementById(`ocVol_${symUpper}`);
  if (elVol && vol !== undefined && vol !== null) {
    const numVol = Number(vol);
    elVol.textContent = numVol > 0 ? (numVol >= 10 ? formatNumber(numVol, 0) : formatNumber(numVol, 2)) : '0';
  }

  // 6. Open Interest (OI)
  const elOi = document.getElementById(`ocOi_${symUpper}`);
  if (elOi && oi !== undefined && oi !== null) {
    const numOi = Number(oi);
    elOi.textContent = numOi > 0 ? (numOi >= 10 ? formatNumber(numOi, 0) : formatNumber(numOi, 2)) : '0';
  }
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
        <td class="col-call-side col-bid ${isCallItm ? 'oc-itm-call' : ''}" id="ocBid_${callKey}" style="color: var(--accent-green); font-size: 0.74rem;">${callBid}</td>
        <td class="col-call-side col-ask ${isCallItm ? 'oc-itm-call' : ''}" id="ocAsk_${callKey}" style="color: var(--accent-red); font-size: 0.74rem;">${callAsk}</td>

        <!-- CALLS: Mark Price -->
        <td class="col-call-side col-mark ${isCallItm ? 'oc-itm-call' : ''}" id="ocMark_${callKey}" style="color: var(--text-secondary); font-size: 0.75rem;">$${formatNumber(callMark, 2)}</td>

        <!-- CALLS: Live Traded LTP -->
        <td class="col-call-side col-ltp ${isCallItm ? 'oc-itm-call' : ''}">
          <span class="oc-ltp-val oc-call-ltp" id="ocLtp_${callKey}" title="Click to add Call Buy order to Multi-Leg Studio" onclick="addOptionLegToStudio('${callSym}', 'buy', 'call', ${strike}, ${callLtp || callMark || 0})">
            ${callLtpDisplay}
          </span>
        </td>

        <!-- CALLS: 1-Click Action Buttons -->
        <td class="col-call-side col-trade ${isCallItm ? 'oc-itm-call' : ''}">
          <div class="oc-actions-cell">
            <button type="button" class="oc-btn-buy" title="Buy Call (Add to Studio)" onclick="addOptionLegToStudio('${callSym}', 'buy', 'call', ${strike}, ${callLtp || callMark || 0})">B</button>
            <button type="button" class="oc-btn-sell" title="Sell Call (Add to Studio)" onclick="addOptionLegToStudio('${callSym}', 'sell', 'call', ${strike}, ${callLtp || callMark || 0})">S</button>
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
            <button type="button" class="oc-btn-buy" title="Buy Put (Add to Studio)" onclick="addOptionLegToStudio('${putSym}', 'buy', 'put', ${strike}, ${putLtp || putMark || 0})">B</button>
            <button type="button" class="oc-btn-sell" title="Sell Put (Add to Studio)" onclick="addOptionLegToStudio('${putSym}', 'sell', 'put', ${strike}, ${putLtp || putMark || 0})">S</button>
          </div>
        </td>

        <!-- PUTS: Live Traded LTP -->
        <td class="col-put-side col-ltp ${isPutItm ? 'oc-itm-put' : ''}">
          <span class="oc-ltp-val oc-put-ltp" id="ocLtp_${putKey}" title="Click to add Put Buy order to Multi-Leg Studio" onclick="addOptionLegToStudio('${putSym}', 'buy', 'put', ${strike}, ${putLtp || putMark || 0})">
            ${putLtpDisplay}
          </span>
        </td>

        <!-- PUTS: Mark Price -->
        <td class="col-put-side col-mark ${isPutItm ? 'oc-itm-put' : ''}" id="ocMark_${putKey}" style="color: var(--text-secondary); font-size: 0.75rem;">$${formatNumber(putMark, 2)}</td>

        <!-- PUTS: Bid / Ask -->
        <td class="col-put-side col-bid ${isPutItm ? 'oc-itm-put' : ''}" id="ocBid_${putKey}" style="color: var(--accent-green); font-size: 0.74rem;">${putBid}</td>
        <td class="col-put-side col-ask ${isPutItm ? 'oc-itm-put' : ''}" id="ocAsk_${putKey}" style="color: var(--accent-red); font-size: 0.74rem;">${putAsk}</td>

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
      limit_price: limitPrice > 0 ? limitPrice : ''
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
  const sym = (data.symbol || '').toUpperCase();
  if (!sym) return;

  const quotes = data.quotes || {};
  const closeVal = data.close;
  const bestBid = quotes.best_bid !== undefined ? quotes.best_bid : data.bid;
  const bestAsk = quotes.best_ask !== undefined ? quotes.best_ask : data.ask;
  const markVal = data.mark_price;
  const volVal = data.volume;
  const oiVal = data.oi !== undefined ? data.oi : (data.oi_contracts !== undefined ? data.oi_contracts : data.open_interest);
  const ltp = closeVal !== undefined && closeVal !== null ? Number(closeVal) : (data.ltp !== undefined && data.ltp !== null ? Number(data.ltp) : null);

  updateOptionChainRowQuote(
    sym,
    ltp,
    markVal !== undefined && markVal !== null ? Number(markVal) : null,
    bestBid !== undefined && bestBid !== null ? Number(bestBid) : null,
    bestAsk !== undefined && bestAsk !== null ? Number(bestAsk) : null,
    volVal !== undefined && volVal !== null ? Number(volVal) : null,
    oiVal !== undefined && oiVal !== null ? Number(oiVal) : null,
    true
  );
}
