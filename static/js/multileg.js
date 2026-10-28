/**
 * ==============================================================================
 * DELTA EXCHANGE - MULTI-LEG TRADING STUDIO MODULE
 * Strategy Presets, Dynamic Leg Builder, CE/PE Toggles, Exact OTM Margin Bar,
 * Confirmation Dialogs, and 1-Click Multi-Leg Concurrency Execution
 * ==============================================================================
 */

let availableExpiries = [];
let activePreset = 'custom';
let legRows = [];

let currentUnderlyingInfo = {
  underlying: 'BTC',
  spot_price: 0,
  underlying_price: 0,
  futures_price: 0,
  ltp: 0,
  mark_price: 0,
  contract_value: 0.001,
  asset_unit: 'BTC',
  atm_strike: null
};

/**
 * Updates spot ticker display, ATM strike pill, and lot conversion hint
 */
function updateUnderlyingPriceAndLotDisplay() {
  const selUnderlyingEl = document.getElementById('selUnderlying');
  const underlying = selUnderlyingEl ? selUnderlyingEl.value : 'BTC';
  const spotUSD = currentUnderlyingInfo.spot_price || currentUnderlyingInfo.underlying_price || currentUnderlyingInfo.futures_price || currentUnderlyingInfo.ltp || 0;
  const contractVal = currentUnderlyingInfo.contract_value || (underlying === 'BTC' ? 0.001 : (underlying === 'ETH' ? 0.01 : 1.0));
  const unit = currentUnderlyingInfo.asset_unit || underlying;

  const spotINR = spotUSD * usdToInrRate;

  // Update spot badge in bar (matches Delta Exchange Option Chain exactly)
  const spotBadge = document.getElementById('spotPriceBadge');
  if (spotBadge) {
    const spotLabel = document.getElementById('spotPriceLabel');
    if (spotLabel) spotLabel.textContent = `${underlying} Spot:`;
    const spotPriceVal = document.getElementById('spotPriceVal');
    if (spotPriceVal) spotPriceVal.textContent = spotUSD > 0 ? `$${formatNumber(spotUSD, 1)}` : '--';
    const spotPriceValInr = document.getElementById('spotPriceValInr');
    if (spotPriceValInr) spotPriceValInr.textContent = spotUSD > 0 ? `(₹${formatNumber(spotINR, 0)})` : '';
  }

  // Update ATM Strike Ref Label
  const atmRefEl = document.getElementById('atmLabelRef');
  if (atmRefEl) {
    if (currentUnderlyingInfo.atm_strike) {
      atmRefEl.textContent = `🎯 ATM: ${formatNumber(currentUnderlyingInfo.atm_strike, 0)}`;
    } else {
      const manualAtm = document.getElementById('inpAtmStrike')?.value;
      atmRefEl.textContent = manualAtm ? `🎯 ATM: ${manualAtm}` : `🎯 ATM: --`;
    }
  }

  // Update lot size suggestion and calculation dynamically
  const defaultSizeInp = document.getElementById('inpDefaultSize');
  const size = defaultSizeInp ? (parseInt(defaultSizeInp.value) || 1) : 1;
  const totalCrypto = size * contractVal;
  const formattedCrypto = Number(totalCrypto.toFixed(6)).toString();
  const lotWord = size === 1 ? 'Lot' : 'Lots';

  const lotContractSpec = document.getElementById('lotContractSpec');
  if (lotContractSpec) {
    lotContractSpec.textContent = `💡 ${size} ${lotWord} = ${formattedCrypto} ${unit}`;
  }

  updateStrategyMarginRequirement();
}

function setQuickLots(num) {
  const inp = document.getElementById('inpDefaultSize');
  if (inp) inp.value = num;
  updateAllLegsSize();
  updateUnderlyingPriceAndLotDisplay();
}

function onSizeInputChange() {
  updateUnderlyingPriceAndLotDisplay();
}

/**
 * Loads expiration dates and available strikes for selected underlying asset
 */
async function loadExpiriesForUnderlying(underlying = 'BTC') {
  const cleanUnderlying = (underlying || 'BTC').toUpperCase() === 'ETH' ? 'ETH' : 'BTC';
  const selExpiry = document.getElementById('selExpiry');
  if (!selExpiry) return;
  selExpiry.innerHTML = '<option value="">Loading expiries from Master Scrip...</option>';

  try {
    const data = await ApiService.getOptionsExpiries(cleanUnderlying);
    if (data.success) {
      currentUnderlyingInfo = {
        underlying: data.underlying || underlying,
        spot_price: Number(data.spot_price || data.underlying_price || data.futures_price || data.ltp) || 0,
        underlying_price: Number(data.underlying_price || data.spot_price || data.futures_price || data.ltp) || 0,
        futures_price: Number(data.futures_price || data.ltp) || 0,
        ltp: Number(data.ltp) || 0,
        mark_price: Number(data.mark_price) || 0,
        contract_value: Number(data.contract_value) || (underlying === 'BTC' ? 0.001 : (underlying === 'ETH' ? 0.01 : 1.0)),
        asset_unit: data.asset_unit || underlying,
        atm_strike: data.atm_strike
      };

      if (data.expiries && data.expiries.length > 0) {
        availableExpiries = data.expiries;
        let optionsHtml = '';
        data.expiries.forEach((item, idx) => {
          const totalStr = item.total_strikes ? ` (${item.total_strikes} strikes)` : '';
          const labelStr = item.label ? `${item.label}` : item.expiry;
          optionsHtml += `<option value="${item.expiry}" ${idx === 0 ? 'selected' : ''}>${labelStr} [${item.expiry}]${totalStr}</option>`;
        });
        selExpiry.innerHTML = optionsHtml;

        const firstExp = data.expiries[0];
        const initialAtm = firstExp.atm_strike || data.atm_strike || (firstExp.strikes ? firstExp.strikes[Math.floor(firstExp.strikes.length / 2)] : 86000);
        currentUnderlyingInfo.atm_strike = initialAtm;

        const atmInp = document.getElementById('inpAtmStrike');
        if (atmInp) atmInp.value = initialAtm;

        const expiryHint = document.getElementById('expiryCountHint');
        if (expiryHint) {
          expiryHint.textContent = `📅 ${data.expiries.length} Expiries (${firstExp.total_strikes || firstExp.strikes.length} Strikes in selected)`;
        }
      } else {
        selExpiry.innerHTML = '<option value="">No Expiries Available</option>';
      }
    } else {
      selExpiry.innerHTML = '<option value="">Failed to load expiries</option>';
    }
  } catch (err) {
    console.error('Error fetching expiries:', err);
    selExpiry.innerHTML = '<option value="">Error loading expiries</option>';
  }

  updateUnderlyingPriceAndLotDisplay();
  applyStrategyPreset(activePreset);
  if (typeof subscribeActiveSymbols === 'function') {
    subscribeActiveSymbols();
  }
}

/**
 * Returns the exact list of exchange strike prices for a specific expiry date
 */
function getAvailableStrikesForExpiry(expiry) {
  const found = availableExpiries.find(e => e.expiry === expiry);
  if (found && Array.isArray(found.strikes) && found.strikes.length > 0) {
    return found.strikes;
  }
  if (availableExpiries.length > 0 && availableExpiries[0].strikes && availableExpiries[0].strikes.length > 0) {
    return availableExpiries[0].strikes;
  }
  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const atm = parseFloat(document.getElementById('inpAtmStrike')?.value) || (underlying === 'BTC' ? 86000 : (underlying === 'ETH' ? 2500 : 150));
  const step = underlying === 'BTC' ? 500 : (underlying === 'ETH' ? 25 : 5);
  const generated = [];
  for (let i = -7; i <= 7; i++) {
    generated.push(atm + i * step);
  }
  return generated;
}

/**
 * Finds the nearest valid exchange strike price for a given target price and expiry date
 */
function getClosestStrike(targetPrice, expiry) {
  const strikes = getAvailableStrikesForExpiry(expiry);
  if (!strikes || strikes.length === 0) return Math.round(targetPrice);
  let closest = strikes[0];
  let minDiff = Math.abs(closest - targetPrice);
  for (let i = 1; i < strikes.length; i++) {
    const diff = Math.abs(strikes[i] - targetPrice);
    if (diff < minDiff) {
      minDiff = diff;
      closest = strikes[i];
    }
  }
  return closest;
}

/**
 * Global state for custom floating strike picker
 */
let activeStrikePickerIdx = null;

function closeStrikePicker() {
  const picker = document.getElementById('floatingStrikePicker');
  if (picker) {
    picker.style.display = 'none';
  }
  if (activeStrikePickerIdx !== null) {
    const btn = document.getElementById(`legStrikeBtn_${activeStrikePickerIdx}`);
    if (btn) btn.classList.remove('open');
  }
  activeStrikePickerIdx = null;
}

function selectStrikeFromPicker(idx, strikeVal) {
  closeStrikePicker();
  updateLegStrike(idx, strikeVal);
}

function onFilterStrikes(query) {
  const list = document.getElementById('strikePickerList');
  if (!list) return;
  const q = query.trim().toLowerCase().replace(/,/g, '');
  const items = list.querySelectorAll('.strike-picker-item');
  items.forEach(item => {
    const strike = item.getAttribute('data-strike') || '';
    if (!q || strike.toLowerCase().includes(q)) {
      item.style.display = 'flex';
    } else {
      item.style.display = 'none';
    }
  });
}

function openStrikePicker(idx, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }

  const btn = document.getElementById(`legStrikeBtn_${idx}`);
  if (!btn) return;

  // Toggle close if already open on same button
  if (activeStrikePickerIdx === idx) {
    closeStrikePicker();
    return;
  }

  closeStrikePicker();

  activeStrikePickerIdx = idx;
  btn.classList.add('open');

  let picker = document.getElementById('floatingStrikePicker');
  if (!picker) {
    picker = document.createElement('div');
    picker.id = 'floatingStrikePicker';
    picker.className = 'floating-strike-picker';
    document.body.appendChild(picker);

    picker.addEventListener('click', (e) => e.stopPropagation());
  }

  const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
  const strikes = getAvailableStrikesForExpiry(expiry);
  const curStrikeNum = legRows[idx]?.strike;
  const expObj = availableExpiries.find(e => e.expiry === expiry);
  const atmForThisExp = expObj?.atm_strike;

  let optionsHtml = '';
  strikes.forEach(s => {
    const isSel = (s === curStrikeNum);
    const isAtm = (s === atmForThisExp);
    optionsHtml += `
      <div class="strike-picker-item ${isSel ? 'active' : ''} ${isAtm ? 'atm-strike' : ''}" 
           data-strike="${s}" 
           onclick="selectStrikeFromPicker(${idx}, ${s})">
        <span class="strike-item-val">${formatNumber(s, 0)}</span>
        ${isAtm ? '<span class="strike-item-badge">⭐ ATM</span>' : ''}
      </div>
    `;
  });

  picker.innerHTML = `
    <div class="strike-picker-search-wrap">
      <svg class="strike-search-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <circle cx="11" cy="11" r="8"></circle>
        <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <input type="text" class="strike-picker-search" id="strikePickerSearchInput" placeholder="Filter strikes..." oninput="onFilterStrikes(this.value)" autocomplete="off" />
    </div>
    <div class="strike-picker-list" id="strikePickerList">
      ${optionsHtml}
    </div>
  `;

  // Attach Enter key to select top filtered item
  const searchInp = document.getElementById('strikePickerSearchInput');
  if (searchInp) {
    searchInp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const firstVisible = picker.querySelector('.strike-picker-item:not([style*="display: none"])');
        if (firstVisible) {
          firstVisible.click();
        }
      }
    });
  }

  // Positioning
  const rect = btn.getBoundingClientRect();
  const pickerWidth = Math.max(130, rect.width);
  const pickerMaxHeight = 260;

  picker.style.display = 'flex';
  picker.style.minWidth = `${pickerWidth}px`;

  const spaceBelow = window.innerHeight - rect.bottom;
  if (spaceBelow < pickerMaxHeight && rect.top > pickerMaxHeight) {
    picker.style.top = `${rect.top - pickerMaxHeight - 4}px`;
    picker.style.maxHeight = `${pickerMaxHeight}px`;
  } else {
    picker.style.top = `${rect.bottom + 4}px`;
    picker.style.maxHeight = `${Math.min(pickerMaxHeight, Math.max(160, spaceBelow - 12))}px`;
  }

  let leftPos = rect.left;
  if (leftPos + pickerWidth > window.innerWidth - 10) {
    leftPos = window.innerWidth - pickerWidth - 10;
  }
  picker.style.left = `${Math.max(10, leftPos)}px`;

  // Scroll active item into view & focus search
  requestAnimationFrame(() => {
    const activeItem = picker.querySelector('.strike-picker-item.active');
    if (activeItem) {
      activeItem.scrollIntoView({ block: 'center' });
    }
    if (searchInp) {
      searchInp.focus({ preventScroll: true });
    }
  });
}

/**
 * Generates the HTML dropdown for a leg's strike price, tailored strictly to the selected expiry's strikes
 */
function getStrikesDropdownHtml(idx, selectedStrike, expiry) {
  const strikes = getAvailableStrikesForExpiry(expiry);
  let curStrikeNum = parseFloat(selectedStrike);

  // Auto-snap invalid strike to closest valid strike in this expiry
  if (isNaN(curStrikeNum) || !strikes.includes(curStrikeNum)) {
    curStrikeNum = getClosestStrike(curStrikeNum || 0, expiry);
    if (legRows[idx]) {
      legRows[idx].strike = curStrikeNum;
      const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
      const optType = legRows[idx].option_type || 'call';
      legRows[idx].symbol = `${optType === 'call' ? 'C' : 'P'}-${underlying}-${curStrikeNum}-${expiry}`;
    }
  }

  const expObj = availableExpiries.find(e => e.expiry === expiry);
  const atmForThisExp = expObj?.atm_strike;
  const isAtm = (curStrikeNum === atmForThisExp);

  return `
    <div class="leg-strike-dropdown-wrapper" id="legStrikeWrapper_${idx}">
      <button type="button" class="leg-strike-trigger" id="legStrikeBtn_${idx}" onclick="openStrikePicker(${idx}, event)" title="Click to choose strike price">
        <span class="leg-strike-val">${formatNumber(curStrikeNum, 0)}${isAtm ? ' ⭐' : ''}</span>
        <span class="leg-strike-arrow">▾</span>
      </button>
    </div>
  `;
}

function toggleLegOptionType(idx, optType) {
  if (legRows[idx]) {
    legRows[idx].option_type = optType;
    const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
    const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
    const strike = legRows[idx].strike || parseFloat(document.getElementById('inpAtmStrike')?.value) || 86000;
    legRows[idx].symbol = `${optType === 'call' ? 'C' : 'P'}-${underlying}-${strike}-${expiry}`;
    renderLegRows();
  }
}

function updateLegStrike(idx, strikeVal) {
  if (legRows[idx]) {
    const numStrike = parseFloat(strikeVal);
    legRows[idx].strike = numStrike;
    const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
    const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
    const optType = legRows[idx].option_type || 'call';
    legRows[idx].symbol = `${optType === 'call' ? 'C' : 'P'}-${underlying}-${numStrike}-${expiry}`;
    renderLegRows();
  }
}

function onManualSymbolChange(idx, val) {
  if (!legRows[idx]) return;
  legRows[idx].symbol = val.trim();
  const parts = val.trim().split('-');
  if (parts.length >= 4) {
    const typePrefix = parts[0].toUpperCase();
    if (typePrefix === 'C') legRows[idx].option_type = 'call';
    else if (typePrefix === 'P') legRows[idx].option_type = 'put';
    const parsedStrike = parseFloat(parts[2]);
    if (!isNaN(parsedStrike)) legRows[idx].strike = parsedStrike;
  }
  renderLegRows();
}

function onUnderlyingChange() {
  let underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  underlying = underlying.toUpperCase() === 'ETH' ? 'ETH' : 'BTC';
  const sel = document.getElementById('selUnderlying');
  if (sel && sel.value !== underlying) sel.value = underlying;
  try {
    localStorage.setItem('delta_studio_underlying', underlying);
  } catch (e) { }
  loadExpiriesForUnderlying(underlying);
}

/**
 * Triggered whenever the user chooses a different expiration date
 * Synchronizes ATM strikes, recalculates preset strikes, and snaps custom legs to new expiry's strikes
 */
function onExpiryChange() {
  const selExpiry = document.getElementById('selExpiry');
  const newExpiry = selExpiry?.value;
  if (!newExpiry) return;

  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const expObj = availableExpiries.find(e => e.expiry === newExpiry);

  if (expObj) {
    if (expObj.atm_strike) {
      currentUnderlyingInfo.atm_strike = expObj.atm_strike;
      const atmInp = document.getElementById('inpAtmStrike');
      if (atmInp) atmInp.value = expObj.atm_strike;
    }
    const expiryHint = document.getElementById('expiryCountHint');
    if (expiryHint) {
      expiryHint.textContent = `📅 ${availableExpiries.length} Expiries (${expObj.total_strikes || expObj.strikes.length} Strikes in selected)`;
    }
  }

  if (activePreset !== 'custom') {
    applyStrategyPreset(activePreset);
  } else {
    // Custom legs: adapt each leg's strike to the closest valid strike of the newly selected expiry
    legRows.forEach(leg => {
      if (leg.option_type) {
        const closestStrike = getClosestStrike(leg.strike, newExpiry);
        leg.strike = closestStrike;
        leg.symbol = `${leg.option_type === 'call' ? 'C' : 'P'}-${underlying}-${closestStrike}-${newExpiry}`;
      }
    });
    renderLegRows();
  }

  updateUnderlyingPriceAndLotDisplay();
}

let expiryHarvestMode = 'both'; // 'both' | 'ce' | 'pe'

function setExpiryHarvestMode(mode) {
  expiryHarvestMode = mode;
  document.querySelectorAll('.harvest-pill-btn').forEach(btn => btn.classList.remove('active'));
  if (mode === 'both') document.getElementById('pillHarvestBoth')?.classList.add('active');
  else if (mode === 'ce') document.getElementById('pillHarvestCE')?.classList.add('active');
  else if (mode === 'pe') document.getElementById('pillHarvestPE')?.classList.add('active');

  if (activePreset === 'expiry_harvest') {
    loadExpiryHarvestLegs();
  }
}

async function loadExpiryHarvestLegs() {
  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '201026');
  const size = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;
  const container = document.getElementById('legsListContainer');

  if (container) {
    container.innerHTML = '<div style="color:var(--accent-blue); padding:16px; text-align:center; font-weight:600;">Scanning option chain for furthest active 1-tick bids...</div>';
  }

  try {
    const chainData = await ApiService.getOptionChain(underlying, expiry);
    if (!chainData || !chainData.success || !Array.isArray(chainData.chain) || chainData.chain.length === 0) {
      if (typeof showToast === 'function') {
        showToast('Unable to fetch live option chain to scan bids.', 'error');
      }
      renderLegRows();
      return;
    }

    const spotUSD = Number(chainData.spot_price || currentUnderlyingInfo.spot_price || currentUnderlyingInfo.ltp || (underlying === 'BTC' ? 84000 : 2500));
    const chain = chainData.chain;

    // 1. Scan Calls (CE): OTM strikes (strike >= spotUSD)
    let eligibleCalls = [];
    chain.forEach(row => {
      const strike = parseFloat(row.strike);
      const call = row.call;
      if (call && strike >= spotUSD) {
        const bid = call.best_bid !== undefined ? parseFloat(call.best_bid) : (call.bid !== undefined ? parseFloat(call.bid) : 0);
        if (!isNaN(bid) && bid > 0) {
          eligibleCalls.push({
            strike: strike,
            bid: bid,
            ask: parseFloat(call.best_ask || call.ask || 0),
            ltp: parseFloat(call.ltp || 0),
            symbol: call.symbol || `C-${underlying}-${strike}-${expiry}`
          });
        }
      }
    });

    // 2. Scan Puts (PE): OTM strikes (strike <= spotUSD)
    let eligiblePuts = [];
    chain.forEach(row => {
      const strike = parseFloat(row.strike);
      const put = row.put;
      if (put && strike <= spotUSD) {
        const bid = put.best_bid !== undefined ? parseFloat(put.best_bid) : (put.bid !== undefined ? parseFloat(put.bid) : 0);
        if (!isNaN(bid) && bid > 0) {
          eligiblePuts.push({
            strike: strike,
            bid: bid,
            ask: parseFloat(put.best_ask || put.ask || 0),
            ltp: parseFloat(put.ltp || 0),
            symbol: put.symbol || `P-${underlying}-${strike}-${expiry}`
          });
        }
      }
    });

    // Best selection logic:
    // For Calls: Lowest bid first; if tied, highest strike (furthest OTM from spot)
    let selectedCall = null;
    if (eligibleCalls.length > 0) {
      const minBid = Math.min(...eligibleCalls.map(c => c.bid));
      const minBidCalls = eligibleCalls.filter(c => c.bid === minBid);
      minBidCalls.sort((a, b) => b.strike - a.strike);
      selectedCall = minBidCalls[0];
    }

    // For Puts: Lowest bid first; if tied, lowest strike (furthest OTM from spot)
    let selectedPut = null;
    if (eligiblePuts.length > 0) {
      const minBid = Math.min(...eligiblePuts.map(p => p.bid));
      const minBidPuts = eligiblePuts.filter(p => p.bid === minBid);
      minBidPuts.sort((a, b) => a.strike - b.strike);
      selectedPut = minBidPuts[0];
    }

    legRows = [];

    if (expiryHarvestMode === 'both' || expiryHarvestMode === 'ce') {
      if (selectedCall) {
        legRows.push({
          side: 'sell',
          option_type: 'call',
          strike: selectedCall.strike,
          symbol: selectedCall.symbol,
          order_type: 'limit',
          size: size,
          limit_price: selectedCall.bid,
          use_full_margin: false,
          bid: selectedCall.bid,
          ask: selectedCall.ask,
          ltp: selectedCall.ltp
        });
      }
    }

    if (expiryHarvestMode === 'both' || expiryHarvestMode === 'pe') {
      if (selectedPut) {
        legRows.push({
          side: 'sell',
          option_type: 'put',
          strike: selectedPut.strike,
          symbol: selectedPut.symbol,
          order_type: 'limit',
          size: size,
          limit_price: selectedPut.bid,
          use_full_margin: false,
          bid: selectedPut.bid,
          ask: selectedPut.ask,
          ltp: selectedPut.ltp
        });
      }
    }

    renderLegRows();

    if (legRows.length > 0) {
      const legDesc = legRows.map(l => `${l.option_type === 'call' ? 'CE' : 'PE'} ${formatNumber(l.strike, 0)} @ $${l.limit_price}`).join(' & ');
      if (typeof showToast === 'function') {
        showToast(`Expiry 1-Tick Harvest: Loaded ${legDesc}`, 'success');
      }
    } else {
      if (typeof showToast === 'function') {
        showToast('No active resting bids found on far OTM strikes for this expiry.', 'info');
      }
    }
  } catch (err) {
    console.error('Error loading expiry harvest:', err);
    if (typeof showToast === 'function') {
      showToast('Error discovering 1-tick strikes: ' + err.message, 'error');
    }
    renderLegRows();
  }
}

function applyStrategyPreset(presetName, btn = null) {
  activePreset = presetName;
  if (btn) {
    document.querySelectorAll('.preset-toolbar .preset-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }

  const harvestToggle = document.getElementById('expiryHarvestToggleGroup');
  if (presetName === 'expiry_harvest') {
    if (harvestToggle) harvestToggle.style.display = 'inline-flex';
    loadExpiryHarvestLegs();
    return;
  } else {
    if (harvestToggle) harvestToggle.style.display = 'none';
  }

  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
  const expObj = availableExpiries.find(e => e.expiry === expiry);

  const rawAtm = parseFloat(document.getElementById('inpAtmStrike')?.value) || expObj?.atm_strike || (underlying === 'BTC' ? 86000 : 2500);
  const atm = getClosestStrike(rawAtm, expiry);
  const size = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;

  legRows = [];

  if (presetName === 'short_straddle') {
    legRows.push({ side: 'sell', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else if (presetName === 'long_straddle') {
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'buy', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else if (presetName === 'short_strangle') {
    const otmCall = getClosestStrike(atm * 1.02, expiry);
    const otmPut = getClosestStrike(atm * 0.98, expiry);
    legRows.push({ side: 'sell', option_type: 'call', strike: otmCall, symbol: `C-${underlying}-${otmCall}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'put', strike: otmPut, symbol: `P-${underlying}-${otmPut}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else if (presetName === 'bull_call') {
    const higherStrike = getClosestStrike(atm * 1.03, expiry);
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'call', strike: higherStrike, symbol: `C-${underlying}-${higherStrike}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else if (presetName === 'bear_put') {
    const lowerStrike = getClosestStrike(atm * 0.97, expiry);
    legRows.push({ side: 'buy', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'put', strike: lowerStrike, symbol: `P-${underlying}-${lowerStrike}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else if (presetName === 'iron_condor') {
    const pBuy = getClosestStrike(atm * 0.94, expiry);
    const pSell = getClosestStrike(atm * 0.97, expiry);
    const cSell = getClosestStrike(atm * 1.03, expiry);
    const cBuy = getClosestStrike(atm * 1.06, expiry);
    legRows.push({ side: 'buy', option_type: 'put', strike: pBuy, symbol: `P-${underlying}-${pBuy}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'put', strike: pSell, symbol: `P-${underlying}-${pSell}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'sell', option_type: 'call', strike: cSell, symbol: `C-${underlying}-${cSell}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
    legRows.push({ side: 'buy', option_type: 'call', strike: cBuy, symbol: `C-${underlying}-${cBuy}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  } else {
    // Custom Builder - default 1 Option leg with LIMIT order type
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '', use_full_margin: false });
  }

  renderLegRows();
}

function recalculatePresetStrikes() {
  if (activePreset !== 'custom') {
    applyStrategyPreset(activePreset);
  }
  updateUnderlyingPriceAndLotDisplay();
}

function updateAllLegsSize() {
  const sz = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;
  legRows.forEach(leg => {
    leg.size = sz;
    leg.use_full_margin = false;
  });
  renderLegRows();
  updateUnderlyingPriceAndLotDisplay();
}

function addNewLegRow() {
  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
  const expObj = availableExpiries.find(e => e.expiry === expiry);
  const rawAtm = parseFloat(document.getElementById('inpAtmStrike')?.value) || expObj?.atm_strike || (underlying === 'BTC' ? 86000 : 2500);
  const atm = getClosestStrike(rawAtm, expiry);
  const sz = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;
  legRows.push({
    side: 'buy',
    option_type: 'call',
    strike: atm,
    symbol: `C-${underlying}-${atm}-${expiry}`,
    order_type: 'limit',
    size: sz,
    limit_price: '',
    use_full_margin: false
  });
  renderLegRows();
  updateUnderlyingPriceAndLotDisplay();
}

/**
 * Downloads & synchronizes Master Scrips on demand from Delta Exchange
 */
async function syncMasterScrips() {
  const btn = document.getElementById('btnSyncMaster');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '🔄 Syncing Master...';
  }
  try {
    const res = await ApiService.refreshMasterScrips();
    if (res && res.success) {
      const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
      await loadExpiriesForUnderlying(underlying);
      if (typeof showToast === 'function') {
        showToast(`✅ Master Scrips synced! ${res.total_products || 1220} active contracts loaded.`, 'success');
      }
    } else {
      if (typeof showToast === 'function') {
        showToast(`❌ Master Scrips sync failed: ${res?.error || 'Unknown error'}`, 'error');
      }
    }
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast(`❌ Master sync error: ${e.message}`, 'error');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-icon">🔄</span> Sync Master Scrips';
    }
  }
}

function deleteLegRow(idx) {
  legRows.splice(idx, 1);
  renderLegRows();
  updateUnderlyingPriceAndLotDisplay();
}

function toggleLegSide(idx, side) {
  if (legRows[idx]) {
    legRows[idx].side = side;
    renderLegRows();
  }
}

function updateLegField(idx, field, value) {
  if (legRows[idx]) {
    legRows[idx][field] = value;
    if (field === 'order_type') {
      renderLegRows();
    } else if (field === 'limit_price' || field === 'size') {
      if (field === 'size') {
        legRows[idx].use_full_margin = false;
      }
      updateStrategyMarginRequirement();
    }
  }
}

/**
 * Calculates required margin in USD for 1 lot of a specific option leg
 * Uses Delta Exchange Exact Option Margin formula
 */
function getUnitMarginUSDForLeg(leg) {
  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const spotUSD = currentUnderlyingInfo.spot_price || currentUnderlyingInfo.underlying_price || currentUnderlyingInfo.futures_price || currentUnderlyingInfo.ltp || (underlying === 'BTC' ? 86000 : 2500);
  const contractVal = currentUnderlyingInfo.contract_value || (underlying === 'BTC' ? 0.001 : (underlying === 'ETH' ? 0.01 : 1.0));

  const strike = parseFloat(leg.strike) || spotUSD;
  const isBuy = (leg.side || 'buy').toLowerCase() === 'buy';
  const isCall = (leg.option_type || 'call').toLowerCase() === 'call';

  const limPrice = parseFloat(leg.limit_price);
  const estPrice = (leg.ltp && leg.ltp > 0) ? leg.ltp : (leg.mark_price && leg.mark_price > 0 ? leg.mark_price : 0.1);
  const premiumUSD = (!isNaN(limPrice) && limPrice > 0) ? limPrice : estPrice;

  if (isBuy) {
    // Long Option Margin: Premium * 1 * ContractValue
    return premiumUSD * contractVal;
  } else {
    // Short Option Margin (Delta Exchange Exact Formula):
    // Margin per unit = Premium + Max( 0.05 * Spot - OTM, 0.005 * Spot )
    let otmAmount = 0;
    if (isCall) {
      otmAmount = Math.max(0, strike - spotUSD);
    } else {
      otmAmount = Math.max(0, spotUSD - strike);
    }

    const baseMargin = 0.05 * spotUSD;
    const minMarginFloor = 0.005 * spotUSD;
    const otmAdjusted = Math.max(baseMargin - otmAmount, minMarginFloor);
    const shortMarginPerUnit = premiumUSD + otmAdjusted;

    return shortMarginPerUnit * contractVal;
  }
}

/**
 * Automatically recalculates lot sizes for any legs marked with use_full_margin
 */
function recalculateFullMarginLots(skipRender = false) {
  if (!legRows || legRows.length === 0) return false;

  const fullLegs = legRows.filter(l => l.use_full_margin);
  if (fullLegs.length === 0) return false;

  const availUSD = currentWallet.usd_available !== undefined ? Number(currentWallet.usd_available) : 0;
  if (availUSD <= 0) {
    fullLegs.forEach(l => { l.size = 1; });
    return true;
  }

  // Margin reserved for legs that have manual (fixed) lot sizes
  let fixedMarginUSD = 0;
  legRows.forEach(l => {
    if (!l.use_full_margin) {
      const sz = parseInt(l.size) || 1;
      fixedMarginUSD += getUnitMarginUSDForLeg(l) * sz;
    }
  });

  const remainingAvailUSD = Math.max(0, availUSD - fixedMarginUSD);
  const totalUnitMargin = fullLegs.reduce((sum, l) => sum + getUnitMarginUSDForLeg(l), 0);

  if (totalUnitMargin > 0) {
    // 5% reserved buffer: calculate lots using 95% of available margin so 5% stays reserved
    const safeAvailUSD = remainingAvailUSD * 0.95;
    const calculatedLots = Math.floor(safeAvailUSD / totalUnitMargin);
    const finalLots = calculatedLots > 0 ? calculatedLots : 1;
    let changed = false;

    fullLegs.forEach(l => {
      if (l.size !== finalLots) {
        l.size = finalLots;
        changed = true;
      }
    });

    if (changed || skipRender) {
      fullLegs.forEach(l => {
        const idx = legRows.indexOf(l);
        const inp = document.getElementById(`legSizeInp_${idx}`);
        if (inp) {
          inp.value = l.size;
        }
      });
      if (!skipRender) {
        renderLegRows();
      }
      return true;
    }
  }
  return false;
}

/**
 * Toggles Full Margin mode on an individual leg
 */
function toggleLegFullMargin(idx, isChecked) {
  const leg = legRows[idx];
  if (!leg) return;

  leg.use_full_margin = isChecked;

  if (isChecked) {
    recalculateFullMarginLots(false);
    updateStrategyMarginRequirement();
    const lots = leg.size;
    const legUnit = getUnitMarginUSDForLeg(leg);
    const estMarginUSD = lots * legUnit;
    if (typeof showToast === 'function') {
      showToast(`⚡ Leg #${idx + 1} Full Margin enabled: auto-calculated ${lots} Lots (~${formatMoney(estMarginUSD, selectedCurrency, 2, 2)})`, 'success');
    }
  } else {
    renderLegRows();
    updateStrategyMarginRequirement();
    if (typeof showToast === 'function') {
      showToast(`Leg #${idx + 1} switched to manual lot sizing.`, 'info');
    }
  }
}

function onManualLegSizeChange(idx, val) {
  const leg = legRows[idx];
  if (!leg) return;
  leg.use_full_margin = false;
  const num = parseInt(val) || 1;
  leg.size = Math.max(1, num);
  renderLegRows();
}

function onManualLegSizeInput(idx, val) {
  const leg = legRows[idx];
  if (!leg) return;
  if (leg.use_full_margin) {
    leg.use_full_margin = false;
    const chk = document.getElementById(`legFullMarginChk_${idx}`);
    if (chk) chk.checked = false;
    const pill = chk?.closest('.leg-full-margin-pill');
    if (pill) pill.classList.remove('active');
    const inp = document.getElementById(`legSizeInp_${idx}`);
    if (inp) {
      inp.classList.remove('full-margin-active');
      inp.readOnly = false;
    }
  }
  const num = parseInt(val) || 1;
  leg.size = Math.max(1, num);
  updateStrategyMarginRequirement();
}

/**
 * Computes exact OTM-adjusted strategy margin requirement matching Delta Exchange formula:
 * Short Option: Premium + Max( 0.05 * Spot - OTM, 0.005 * Spot )
 * Long Option: Premium * Size * Contract Value
 */
function updateStrategyMarginRequirement() {
  // Recalculate lots dynamically for any leg using Full Margin
  recalculateFullMarginLots(true);

  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const spotUSD = currentUnderlyingInfo.spot_price || currentUnderlyingInfo.underlying_price || currentUnderlyingInfo.futures_price || currentUnderlyingInfo.ltp || (underlying === 'BTC' ? 86000 : 2500);
  const contractVal = currentUnderlyingInfo.contract_value || (underlying === 'BTC' ? 0.001 : (underlying === 'ETH' ? 0.01 : 1.0));

  let totalNotionalUSD = 0;
  let totalRequiredMarginUSD = 0;

  legRows.forEach(leg => {
    const sz = parseInt(leg.size) || 1;
    const strike = parseFloat(leg.strike) || spotUSD;
    const isBuy = (leg.side || 'buy').toLowerCase() === 'buy';
    const isCall = (leg.option_type || 'call').toLowerCase() === 'call';
    const cryptoQty = sz * contractVal;
    const legNotional = strike * cryptoQty;
    totalNotionalUSD += legNotional;

    const limPrice = parseFloat(leg.limit_price);
    const estPrice = (leg.ltp && leg.ltp > 0) ? leg.ltp : (leg.mark_price && leg.mark_price > 0 ? leg.mark_price : 0.1);
    const premiumUSD = (!isNaN(limPrice) && limPrice > 0) ? limPrice : estPrice;

    if (isBuy) {
      // Long Option Margin: Premium * Qty
      const buyMargin = premiumUSD * cryptoQty;
      totalRequiredMarginUSD += buyMargin;
    } else {
      // Short Option Margin (Delta Exchange Exact Formula):
      // Margin per unit = Premium + Max( 0.05 * Spot - OTM, 0.005 * Spot )
      let otmAmount = 0;
      if (isCall) {
        otmAmount = Math.max(0, strike - spotUSD);
      } else {
        otmAmount = Math.max(0, spotUSD - strike);
      }

      const baseMargin = 0.05 * spotUSD;
      const minMarginFloor = 0.005 * (isCall ? spotUSD : strike);
      const otmAdjusted = Math.max(baseMargin - otmAmount, minMarginFloor);
      const shortMarginPerUnit = premiumUSD + otmAdjusted;

      const sellMargin = shortMarginPerUnit * cryptoQty;
      totalRequiredMarginUSD += sellMargin;
    }
  });

  // Live available margin from wallet
  const availUSD = currentWallet.usd_available !== undefined ? Number(currentWallet.usd_available) : 0;
  const isSufficient = (availUSD >= totalRequiredMarginUSD) || (totalRequiredMarginUSD === 0);

  const reqEl = document.getElementById('txtRequiredMarginUSD');
  const reqAltEl = document.getElementById('txtRequiredMarginAlt');
  const availEl = document.getElementById('txtAvailableMarginUSD');
  const availAltEl = document.getElementById('txtAvailableMarginAlt');
  const notionalEl = document.getElementById('txtStrategyNotionalUSD');
  const notionalAltEl = document.getElementById('txtStrategyNotionalAlt');
  const healthBadge = document.getElementById('marginHealthBadge');
  const healthText = document.getElementById('marginHealthText');

  if (reqEl) {
    reqEl.textContent = formatMoney(totalRequiredMarginUSD, selectedCurrency, 2, 2);
    if (reqAltEl) reqAltEl.textContent = `(${formatAltMoney(totalRequiredMarginUSD)})`;
  }
  if (availEl) {
    availEl.textContent = formatMoney(availUSD, selectedCurrency, 2, 2);
    if (availAltEl) availAltEl.textContent = `(${formatAltMoney(availUSD)})`;
  }
  if (notionalEl) {
    notionalEl.textContent = formatMoney(totalNotionalUSD, selectedCurrency, 2, 2);
    if (notionalAltEl) notionalAltEl.textContent = `(${formatAltMoney(totalNotionalUSD)})`;
  }

  if (healthBadge && healthText) {
    const hasFullMargin = legRows.some(l => l.use_full_margin);
    if (totalRequiredMarginUSD === 0 && legRows.length === 0) {
      healthBadge.className = 'margin-health-badge sufficient';
      healthText.textContent = 'Ready to Build';
    } else if (isSufficient) {
      healthBadge.className = 'margin-health-badge sufficient';
      healthText.textContent = hasFullMargin ? 'Margin Sufficient (5% Reserved)' : 'Margin Sufficient';
    } else {
      healthBadge.className = 'margin-health-badge insufficient';
      const shortfall = totalRequiredMarginUSD - availUSD;
      healthText.textContent = `Shortfall: ${formatMoney(shortfall, selectedCurrency, 2, 2)}`;
    }
  }
}

/**
 * Copies live Quote (LTP, Bid, or Ask) of a leg directly to its Limit Price input
 */
function applyBidToLimit(idx) {
  const leg = legRows[idx];
  if (!leg || !leg.bid || leg.bid <= 0) {
    if (typeof showToast === 'function') showToast(`No live Bid quote available yet for ${leg?.symbol || 'leg'}`, 'info');
    return;
  }
  leg.limit_price = leg.bid;
  const priceInput = document.getElementById(`legPriceInp_${idx}`);
  if (priceInput) {
    priceInput.value = leg.bid;
    priceInput.classList.add('flash-limit-set');
    setTimeout(() => priceInput.classList.remove('flash-limit-set'), 350);
  }
  updateStrategyMarginRequirement();
  if (typeof showToast === 'function') {
    showToast(`⚡ Leg #${idx + 1} Limit Price set to live Bid: $${leg.bid}`, 'success');
  }
}

function applyAskToLimit(idx) {
  const leg = legRows[idx];
  if (!leg || !leg.ask || leg.ask <= 0) {
    if (typeof showToast === 'function') showToast(`No live Ask quote available yet for ${leg?.symbol || 'leg'}`, 'info');
    return;
  }
  leg.limit_price = leg.ask;
  const priceInput = document.getElementById(`legPriceInp_${idx}`);
  if (priceInput) {
    priceInput.value = leg.ask;
    priceInput.classList.add('flash-limit-set');
    setTimeout(() => priceInput.classList.remove('flash-limit-set'), 350);
  }
  updateStrategyMarginRequirement();
  if (typeof showToast === 'function') {
    showToast(`⚡ Leg #${idx + 1} Limit Price set to live Ask: $${leg.ask}`, 'success');
  }
}

function applyLtpToLimit(idx) {
  const leg = legRows[idx];
  if (!leg) return;
  const targetPrice = (leg.ltp && leg.ltp > 0) ? leg.ltp : (leg.mark_price && leg.mark_price > 0 ? leg.mark_price : null);
  if (targetPrice !== null) {
    leg.limit_price = targetPrice;
    const priceInput = document.getElementById(`legPriceInp_${idx}`);
    if (priceInput) {
      priceInput.value = targetPrice;
      priceInput.classList.add('flash-limit-set');
      setTimeout(() => priceInput.classList.remove('flash-limit-set'), 350);
    }
    updateStrategyMarginRequirement();
    if (typeof showToast === 'function') {
      showToast(`⚡ Leg #${idx + 1} Limit Price set to live LTP: $${targetPrice}`, 'success');
    }
  } else {
    if (typeof showToast === 'function') {
      showToast(`No live traded LTP quote available yet for ${leg.symbol}`, 'info');
    }
  }
}

// High-performance Top-of-Book (L1) parser for Multi-Leg Studio contracts
function processLegL1Update(sym, data) {
  if (!sym || !data) return null;
  const rawBp = data.best_bid !== undefined ? data.best_bid : (data.bp !== undefined ? data.bp : data.bid);
  const rawAp = data.best_ask !== undefined ? data.best_ask : (data.ap !== undefined ? data.ap : data.ask);

  let bestBid = (rawBp !== undefined && rawBp !== null && rawBp !== '' && rawBp !== '-') ? parseFloat(rawBp) : null;
  let bestAsk = (rawAp !== undefined && rawAp !== null && rawAp !== '' && rawAp !== '-') ? parseFloat(rawAp) : null;

  if (bestBid === null && Array.isArray(data.bids) && data.bids.length > 0) {
    bestBid = parseFloat(data.bids[0][0]);
  }
  if (bestAsk === null && Array.isArray(data.asks) && data.asks.length > 0) {
    bestAsk = parseFloat(data.asks[0][0]);
  }

  if (bestBid !== null && isNaN(bestBid)) bestBid = null;
  if (bestAsk !== null && isNaN(bestAsk)) bestAsk = null;

  if (bestBid !== null && bestAsk !== null && bestAsk < bestBid) return null;
  return { bestBid, bestAsk };
}

/**
 * Updates DOM Quote badges (LTP, Bid, Ask) for a specific leg
 */
function updateLegQuoteDisplay(idx, ltp, markPrice, bid, ask) {
  const leg = legRows[idx];
  if (!leg) return;

  const oldLtp = leg.ltp;
  const oldBid = leg.bid;
  const oldAsk = leg.ask;

  if (ltp !== undefined && ltp !== null && Number(ltp) > 0) leg.ltp = Number(ltp);
  if (markPrice !== undefined && markPrice !== null && Number(markPrice) > 0) leg.mark_price = Number(markPrice);
  if (bid !== undefined && bid !== null && Number(bid) > 0) leg.bid = Number(bid);
  if (ask !== undefined && ask !== null && Number(ask) > 0) leg.ask = Number(ask);

  // 1. Bid Display
  const bidCell = document.getElementById(`legBidCell_${idx}`);
  const bidValEl = document.getElementById(`legBidVal_${idx}`);
  if (bidValEl && bidCell) {
    if (leg.bid && leg.bid > 0) {
      bidValEl.textContent = `$${formatNumber(leg.bid, leg.bid >= 100 ? 1 : (leg.bid >= 1 ? 2 : 4))}`;
      bidCell.classList.remove('empty-quote');
      bidCell.title = `Click to set Limit Price to Bid ($${leg.bid})`;
      if (oldBid !== undefined && oldBid !== null && oldBid > 0 && leg.bid !== oldBid) {
        const cls = leg.bid > oldBid ? 'flash-up' : 'flash-down';
        bidCell.classList.add(cls);
        setTimeout(() => bidCell.classList.remove(cls), 350);
      }
    } else {
      bidValEl.textContent = '--';
      bidCell.classList.add('empty-quote');
    }
  }

  // 2. Ask Display
  const askCell = document.getElementById(`legAskCell_${idx}`);
  const askValEl = document.getElementById(`legAskVal_${idx}`);
  if (askValEl && askCell) {
    if (leg.ask && leg.ask > 0) {
      askValEl.textContent = `$${formatNumber(leg.ask, leg.ask >= 100 ? 1 : (leg.ask >= 1 ? 2 : 4))}`;
      askCell.classList.remove('empty-quote');
      askCell.title = `Click to set Limit Price to Ask ($${leg.ask})`;
      if (oldAsk !== undefined && oldAsk !== null && oldAsk > 0 && leg.ask !== oldAsk) {
        const cls = leg.ask < oldAsk ? 'flash-up' : 'flash-down';
        askCell.classList.add(cls);
        setTimeout(() => askCell.classList.remove(cls), 350);
      }
    } else {
      askValEl.textContent = '--';
      askCell.classList.add('empty-quote');
    }
  }

  // 3. LTP Display
  const ltpCell = document.getElementById(`legLtpCell_${idx}`);
  const ltpValEl = document.getElementById(`legLtpVal_${idx}`);
  if (ltpValEl && ltpCell) {
    const displayVal = (leg.ltp && leg.ltp > 0)
      ? `$${formatNumber(leg.ltp, leg.ltp >= 100 ? 1 : (leg.ltp >= 1 ? 2 : 4))}`
      : ((leg.mark_price && leg.mark_price > 0) ? `~$${formatNumber(leg.mark_price, 2)}` : '--');
    ltpValEl.textContent = displayVal;

    if (leg.ltp && leg.ltp > 0) {
      ltpCell.classList.remove('empty-ltp');
      ltpCell.title = `Click to set Limit Price to LTP ($${leg.ltp})`;
      if (oldLtp !== undefined && oldLtp !== null && oldLtp > 0 && leg.ltp !== oldLtp) {
        const cls = leg.ltp > oldLtp ? 'flash-up' : 'flash-down';
        ltpCell.classList.add(cls);
        setTimeout(() => ltpCell.classList.remove(cls), 350);
      }
    } else {
      ltpCell.classList.add('empty-ltp');
    }
  }
}

function updateLegLtpDisplay(idx, ltp, markPrice) {
  updateLegQuoteDisplay(idx, ltp, markPrice, undefined, undefined);
}

/**
 * Processes real-time WebSocket tick stream for active multi-leg symbols
 */
function handleLegTickUpdate(data) {
  if (!data || legRows.length === 0) return;
  const incomingSym = (data.symbol || '').toUpperCase();
  if (!incomingSym) return;

  const msgType = data.type || data.channel || data.name || '';

  // 1. Handle L1 Top of Book (ob_l1) updates (real-time Bid & Ask)
  if (msgType === 'ob_l1' || msgType === 'l1_orderbook' || msgType === 'l2_updates' || data.bids !== undefined || data.asks !== undefined || data.bp !== undefined) {
    const l1 = processLegL1Update(incomingSym, data);
    if (l1 && (l1.bestBid !== null || l1.bestAsk !== null)) {
      legRows.forEach((leg, idx) => {
        if (leg.symbol && leg.symbol.toUpperCase() === incomingSym) {
          updateLegQuoteDisplay(idx, undefined, undefined, l1.bestBid, l1.bestAsk);
        }
      });
    }
    return;
  }

  // 2. Handle Mark Price updates
  if (msgType === 'mark_price' || (data.mark_price !== undefined && msgType !== 'v2/ticker')) {
    const markVal = Number(data.mark_price);
    if (!isNaN(markVal) && markVal > 0) {
      legRows.forEach((leg, idx) => {
        if (leg.symbol && leg.symbol.toUpperCase() === incomingSym) {
          updateLegQuoteDisplay(idx, undefined, markVal, undefined, undefined);
        }
      });
    }
    return;
  }

  // 3. Handle Trades & Quotes snapshot via v2/ticker
  const quotes = data.quotes || {};
  const closeVal = data.close;
  const bestBid = quotes.best_bid !== undefined ? Number(quotes.best_bid) : (data.bid !== undefined ? Number(data.bid) : undefined);
  const bestAsk = quotes.best_ask !== undefined ? Number(quotes.best_ask) : (data.ask !== undefined ? Number(data.ask) : undefined);
  const markVal = data.mark_price !== undefined ? Number(data.mark_price) : undefined;
  const ltp = closeVal !== undefined ? Number(closeVal) : (data.ltp !== undefined ? Number(data.ltp) : undefined);

  legRows.forEach((leg, idx) => {
    if (leg.symbol && leg.symbol.toUpperCase() === incomingSym) {
      updateLegQuoteDisplay(idx, ltp, markVal, bestBid, bestAsk);
    }
  });
}

/**
 * Fetches batch live quotes for all active leg symbols
 */
let isFetchingLegTickers = false;
async function fetchLegTickers() {
  if (isFetchingLegTickers || legRows.length === 0) return;
  const symbols = legRows.map(l => l.symbol).filter(Boolean);
  if (symbols.length === 0) return;

  isFetchingLegTickers = true;
  try {
    if (typeof addSubscribedSymbols === 'function') {
      addSubscribedSymbols(symbols);
    }

    const res = await ApiService.getBatchTickers(symbols);
    if (res && res.success && res.tickers) {
      legRows.forEach((leg, idx) => {
        const tick = res.tickers[leg.symbol.toUpperCase()];
        if (tick) {
          const newLtp = tick.ltp !== undefined ? tick.ltp : (tick.mark_price || 0);
          updateLegQuoteDisplay(idx, newLtp, tick.mark_price, tick.bid, tick.ask);
        }
      });
    }
  } catch (err) {
    console.warn('[MultiLeg] Error fetching leg tickers:', err);
  } finally {
    isFetchingLegTickers = false;
  }
}

function renderLegRows() {
  closeStrikePicker();
  const container = document.getElementById('legsListContainer');
  if (!container) return;

  if (legRows.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted); padding:16px; text-align:center;">No active legs. Click "+ Add New Leg" to construct your strategy.</div>';
    const stripLegsCount = document.getElementById('stripLegsCount');
    if (stripLegsCount) stripLegsCount.textContent = '0 Legs';
    updateStrategyMarginRequirement();
    return;
  }

  const stripLegsCount = document.getElementById('stripLegsCount');
  if (stripLegsCount) stripLegsCount.textContent = `${legRows.length} Leg${legRows.length !== 1 ? 's' : ''}`;

  let html = '';
  const expiry = document.getElementById('selExpiry')?.value || '021026';

  legRows.forEach((leg, idx) => {
    const isBuy = leg.side.toLowerCase() === 'buy';
    const isLimit = (leg.order_type || 'limit').toLowerCase() === 'limit';
    const isCall = (leg.option_type || 'call').toLowerCase() === 'call';
    const strikesSelectHtml = getStrikesDropdownHtml(idx, leg.strike, expiry);
    const hasLtp = leg.ltp !== undefined && leg.ltp !== null && leg.ltp > 0;
    const hasBid = leg.bid !== undefined && leg.bid !== null && leg.bid > 0;
    const hasAsk = leg.ask !== undefined && leg.ask !== null && leg.ask > 0;

    const displayLtp = hasLtp ? `$${formatNumber(leg.ltp, leg.ltp >= 100 ? 1 : (leg.ltp >= 1 ? 2 : 4))}` : ((leg.mark_price && leg.mark_price > 0) ? `~$${formatNumber(leg.mark_price, 2)}` : '--');
    const displayBid = hasBid ? `$${formatNumber(leg.bid, leg.bid >= 100 ? 1 : (leg.bid >= 1 ? 2 : 4))}` : '--';
    const displayAsk = hasAsk ? `$${formatNumber(leg.ask, leg.ask >= 100 ? 1 : (leg.ask >= 1 ? 2 : 4))}` : '--';

    html += `
      <div class="leg-row">
        <div class="leg-badge-tag">LEG #${idx + 1}</div>

        <div class="side-toggle">
          <button class="side-toggle-btn ${isBuy ? 'active-buy' : ''}" onclick="toggleLegSide(${idx}, 'buy')">BUY</button>
          <button class="side-toggle-btn ${!isBuy ? 'active-sell' : ''}" onclick="toggleLegSide(${idx}, 'sell')">SELL</button>
        </div>

        <div class="opt-type-toggle">
          <button class="opt-type-btn ${isCall ? 'active-ce' : ''}" onclick="toggleLegOptionType(${idx}, 'call')" title="Call Option (CE)">CE</button>
          <button class="opt-type-btn ${!isCall ? 'active-pe' : ''}" onclick="toggleLegOptionType(${idx}, 'put')" title="Put Option (PE)">PE</button>
        </div>

        ${strikesSelectHtml}

        <input type="text" class="leg-symbol-input" value="${leg.symbol}" placeholder="e.g. C-BTC-86200-021026" onchange="onManualSymbolChange(${idx}, this.value)" />

        <!-- Live Bid Cell -->
        <div class="leg-bid-cell ${hasBid ? '' : 'empty-quote'}" id="legBidCell_${idx}" onclick="applyBidToLimit(${idx})" title="Click to copy Bid ($${leg.bid || 0}) to Limit Price">
          <span class="leg-quote-dot bid-dot"></span>
          <span id="legBidVal_${idx}">${displayBid}</span>
        </div>

        <!-- Live Ask Cell -->
        <div class="leg-ask-cell ${hasAsk ? '' : 'empty-quote'}" id="legAskCell_${idx}" onclick="applyAskToLimit(${idx})" title="Click to copy Ask ($${leg.ask || 0}) to Limit Price">
          <span class="leg-quote-dot ask-dot"></span>
          <span id="legAskVal_${idx}">${displayAsk}</span>
        </div>

        <!-- Live LTP Cell -->
        <div class="leg-ltp-cell ${hasLtp ? '' : 'empty-ltp'}" id="legLtpCell_${idx}" onclick="applyLtpToLimit(${idx})" title="Click to copy LTP ($${leg.ltp || 0}) to Limit Price">
          <span class="leg-ltp-dot"></span>
          <span id="legLtpVal_${idx}">${displayLtp}</span>
        </div>

        <select class="leg-type-select" onchange="updateLegField(${idx}, 'order_type', this.value)">
          <option value="limit" ${isLimit ? 'selected' : ''}>LIMIT</option>
          <option value="market" ${!isLimit ? 'selected' : ''}>MARKET</option>
        </select>

        <input type="number" step="any" id="legPriceInp_${idx}" class="leg-price-input" placeholder="Price" value="${leg.limit_price || ''}" ${!isLimit ? 'disabled' : ''} oninput="updateLegField(${idx}, 'limit_price', this.value)" onchange="updateLegField(${idx}, 'limit_price', this.value)" />

        <div class="leg-size-group">
          <input type="number" id="legSizeInp_${idx}" class="leg-size-input ${leg.use_full_margin ? 'full-margin-active' : ''}" value="${leg.size}" min="1" placeholder="Size" ${leg.use_full_margin ? 'readonly title="Auto-calculated using full margin. Untick FULL to enter manually."' : ''} oninput="onManualLegSizeInput(${idx}, this.value)" onchange="onManualLegSizeChange(${idx}, this.value)" />
          <label class="leg-full-margin-pill ${leg.use_full_margin ? 'active' : ''}" title="Tick to use full available margin for this leg">
            <input type="checkbox" id="legFullMarginChk_${idx}" class="leg-full-margin-chk" ${leg.use_full_margin ? 'checked' : ''} onchange="toggleLegFullMargin(${idx}, this.checked)" />
            <span class="full-margin-label-text">FULL</span>
          </label>
        </div>

        <button class="btn-delete-leg" onclick="deleteLegRow(${idx})" title="Delete Leg">&times;</button>
      </div>
    `;
  });

  container.innerHTML = html;
  updateStrategyMarginRequirement();
  fetchLegTickers();
}

/**
 * Formats order execution error messages for human display
 */
function formatOrderError(err) {
  if (!err) return 'Unknown error';
  const errStr = typeof err === 'object' ? JSON.stringify(err) : String(err);
  if (errStr.includes('insufficient_margin')) {
    const matchAvail = errStr.match(/'available_balance':\s*'([0-9.]+)'/) || errStr.match(/"available_balance":\s*"([0-9.]+)"/);
    const matchReq = errStr.match(/'required_additional_balance':\s*'([0-9.]+)'/) || errStr.match(/"required_additional_balance":\s*"([0-9.]+)"/);
    if (matchAvail && matchReq) {
      const avail = parseFloat(matchAvail[1]);
      const addReq = parseFloat(matchReq[1]);
      return `Insufficient Margin: Available $${avail.toFixed(2)}, required additional $${addReq.toFixed(2)} USD. Try reducing size slightly to maintain margin reserve.`;
    }
    return 'Insufficient Margin: Balance lower than Delta Portfolio Margin requirement. Please reduce lot size slightly.';
  }
  return errStr;
}

/**
 * 1-Click Multi-Leg Trade Execution with pre-trade confirmation summary
 */
async function executeMultiLegTrade() {
  if (legRows.length === 0) {
    showToast('Please add at least one leg to execute.', 'error');
    return;
  }

  // Build confirmation summary of all legs
  let summaryText = `⚡ Confirm Multi-Leg Execution (${legRows.length} Leg${legRows.length !== 1 ? 's' : ''}):\n\n`;
  legRows.forEach((l, i) => {
    const sideStr = (l.side || 'BUY').toUpperCase();
    const typeStr = (l.order_type || 'LIMIT').toUpperCase();
    const priceStr = typeStr === 'LIMIT' ? ` @ $${l.limit_price || 0}` : ' @ MARKET';
    summaryText += `• Leg #${i + 1}: ${sideStr} ${l.size}x ${l.symbol} (${typeStr}${priceStr})\n`;
  });
  summaryText += `\nDo you want to proceed with executing these orders?`;

  if (!confirm(summaryText)) {
    return;
  }

  const btn = document.getElementById('btnExecuteMultiLeg');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span>Executing ${legRows.length} Legs Concurrently...</span>`;
  }

  try {
    const payloadLegs = legRows.map((l, i) => ({
      leg_index: i + 1,
      symbol: l.symbol.trim(),
      side: l.side.toLowerCase(),
      size: parseInt(l.size) || 1,
      order_type: l.order_type.toLowerCase(),
      limit_price: l.limit_price ? parseFloat(l.limit_price) : null
    }));

    const data = await ApiService.executeMultiLeg(payloadLegs);

    if (data.overall_status === 'SUCCESS') {
      showToast(`🚀 All ${data.successful_legs} legs executed successfully!`, 'success');
    } else if (data.overall_status === 'PARTIAL') {
      const legErr = data.error || (data.results && data.results.find(r => r && r.error)?.error) || '';
      showToast(`⚠️ Partial execution: ${data.successful_legs} succeeded, ${data.failed_legs} failed. ${formatOrderError(legErr)}`, 'error');
    } else {
      const legErr = data.error || (data.results && data.results.find(r => r && r.error)?.error) || 'Check details';
      showToast(`❌ Execution failed: ${formatOrderError(legErr)}`, 'error');
    }

    fetchDashboardData(true);
  } catch (err) {
    showToast(`Execution Error: ${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
        </svg>
        <span>Execute All Legs (1-Click)</span>
      `;
    }
  }
}

/**
 * 1-Click Emergency Square Off All Positions
 */
async function confirmSquareOffAll() {
  if (!confirm('🚨 EMERGENCY EXIT: Are you sure you want to CLOSE ALL open positions simultaneously at MARKET price?')) return;

  try {
    const data = await ApiService.squareOffAllPositions();
    if (data.success) {
      showToast(`⚡ Squared off all ${data.total_squared_off} positions!`, 'success');
      fetchDashboardData(true);
    } else {
      showToast(`Square off all notice: ${data.message || data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Error squaring off all: ${err.message}`, 'error');
  }
}

// ==============================================================================
// GLOBAL EVENT LISTENERS FOR FLOATING STRIKE PICKER
// ==============================================================================
document.addEventListener('click', (e) => {
  if (activeStrikePickerIdx !== null) {
    const picker = document.getElementById('floatingStrikePicker');
    const trigger = document.getElementById(`legStrikeBtn_${activeStrikePickerIdx}`);
    if (picker && !picker.contains(e.target) && (!trigger || !trigger.contains(e.target))) {
      closeStrikePicker();
    }
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && activeStrikePickerIdx !== null) {
    closeStrikePicker();
  }
});

window.addEventListener('resize', () => {
  if (activeStrikePickerIdx !== null) closeStrikePicker();
});

window.addEventListener('scroll', (e) => {
  if (activeStrikePickerIdx !== null) {
    const picker = document.getElementById('floatingStrikePicker');
    if (picker && e.target && (picker === e.target || picker.contains(e.target))) {
      return; // Internal scroll inside picker list
    }
    closeStrikePicker();
  }
}, true);
