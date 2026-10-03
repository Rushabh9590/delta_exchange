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
  const selExpiry = document.getElementById('selExpiry');
  if (!selExpiry) return;
  selExpiry.innerHTML = '<option value="">Loading expiries from Master Scrip...</option>';

  try {
    const data = await ApiService.getOptionsExpiries(underlying);
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

  let html = `<select class="leg-strike-select" onchange="updateLegStrike(${idx}, this.value)">`;
  strikes.forEach(s => {
    const isSel = (s === curStrikeNum) ? 'selected' : '';
    const isAtm = (s === atmForThisExp) ? ' ⭐ ATM' : '';
    html += `<option value="${s}" ${isSel}>${formatNumber(s, 0)}${isAtm}</option>`;
  });
  html += `</select>`;
  return html;
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
  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
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

function applyStrategyPreset(presetName, btn = null) {
  activePreset = presetName;
  if (btn) {
    document.querySelectorAll('.preset-toolbar .preset-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }

  const underlying = document.getElementById('selUnderlying')?.value || 'BTC';
  const expiry = document.getElementById('selExpiry')?.value || (availableExpiries[0]?.expiry || '031026');
  const expObj = availableExpiries.find(e => e.expiry === expiry);

  const rawAtm = parseFloat(document.getElementById('inpAtmStrike')?.value) || expObj?.atm_strike || (underlying === 'BTC' ? 86000 : 2500);
  const atm = getClosestStrike(rawAtm, expiry);
  const size = parseInt(document.getElementById('inpDefaultSize')?.value) || 1;

  legRows = [];

  if (presetName === 'short_straddle') {
    legRows.push({ side: 'sell', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else if (presetName === 'long_straddle') {
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'buy', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else if (presetName === 'short_strangle') {
    const otmCall = getClosestStrike(atm * 1.02, expiry);
    const otmPut = getClosestStrike(atm * 0.98, expiry);
    legRows.push({ side: 'sell', option_type: 'call', strike: otmCall, symbol: `C-${underlying}-${otmCall}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'put', strike: otmPut, symbol: `P-${underlying}-${otmPut}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else if (presetName === 'bull_call') {
    const higherStrike = getClosestStrike(atm * 1.03, expiry);
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'call', strike: higherStrike, symbol: `C-${underlying}-${higherStrike}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else if (presetName === 'bear_put') {
    const lowerStrike = getClosestStrike(atm * 0.97, expiry);
    legRows.push({ side: 'buy', option_type: 'put', strike: atm, symbol: `P-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'put', strike: lowerStrike, symbol: `P-${underlying}-${lowerStrike}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else if (presetName === 'iron_condor') {
    const pBuy = getClosestStrike(atm * 0.94, expiry);
    const pSell = getClosestStrike(atm * 0.97, expiry);
    const cSell = getClosestStrike(atm * 1.03, expiry);
    const cBuy = getClosestStrike(atm * 1.06, expiry);
    legRows.push({ side: 'buy', option_type: 'put', strike: pBuy, symbol: `P-${underlying}-${pBuy}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'put', strike: pSell, symbol: `P-${underlying}-${pSell}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'sell', option_type: 'call', strike: cSell, symbol: `C-${underlying}-${cSell}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
    legRows.push({ side: 'buy', option_type: 'call', strike: cBuy, symbol: `C-${underlying}-${cBuy}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
  } else {
    // Custom Builder - default 1 Option leg with LIMIT order type
    legRows.push({ side: 'buy', option_type: 'call', strike: atm, symbol: `C-${underlying}-${atm}-${expiry}`, order_type: 'limit', size: size, limit_price: '' });
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
  legRows.forEach(leg => leg.size = sz);
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
    limit_price: ''
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
      updateStrategyMarginRequirement();
    }
  }
}

/**
 * Computes exact OTM-adjusted strategy margin requirement matching Delta Exchange formula:
 * Short Option: Premium + Max( 0.05 * Spot - OTM, 0.005 * Spot )
 * Long Option: Premium * Size * Contract Value
 */
function updateStrategyMarginRequirement() {
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
    const premiumUSD = (!isNaN(limPrice) && limPrice > 0) ? limPrice : 0.1;

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
    if (totalRequiredMarginUSD === 0 && legRows.length === 0) {
      healthBadge.className = 'margin-health-badge sufficient';
      healthText.textContent = 'Ready to Build';
    } else if (isSufficient) {
      healthBadge.className = 'margin-health-badge sufficient';
      healthText.textContent = 'Margin Sufficient';
    } else {
      healthBadge.className = 'margin-health-badge insufficient';
      const shortfall = totalRequiredMarginUSD - availUSD;
      healthText.textContent = `Shortfall: ${formatMoney(shortfall, selectedCurrency, 2, 2)}`;
    }
  }
}

function renderLegRows() {
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

        <select class="leg-type-select" onchange="updateLegField(${idx}, 'order_type', this.value)">
          <option value="limit" ${isLimit ? 'selected' : ''}>LIMIT</option>
          <option value="market" ${!isLimit ? 'selected' : ''}>MARKET</option>
        </select>

        <input type="number" step="any" class="leg-price-input" placeholder="Price" value="${leg.limit_price || ''}" ${!isLimit ? 'disabled' : ''} oninput="updateLegField(${idx}, 'limit_price', this.value)" onchange="updateLegField(${idx}, 'limit_price', this.value)" />

        <input type="number" class="leg-size-input" value="${leg.size}" min="1" placeholder="Size" onchange="updateLegField(${idx}, 'size', this.value)" />

        <button class="btn-delete-leg" onclick="deleteLegRow(${idx})" title="Delete Leg">&times;</button>
      </div>
    `;
  });

  container.innerHTML = html;
  updateStrategyMarginRequirement();
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
      showToast(`⚠️ Partial execution: ${data.successful_legs} succeeded, ${data.failed_legs} failed.`, 'error');
    } else {
      showToast(`❌ Execution failed: ${data.error || 'Check details'}`, 'error');
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
