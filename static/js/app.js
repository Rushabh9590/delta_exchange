/**
 * ==============================================================================
 * DELTA EXCHANGE - GLOBAL APPLICATION ENTRY & REAL-TIME STREAMING
 * Navigation Sub-menu, State Management, Currency Formatting, Live Feed Engine
 * ==============================================================================
 */

// Global Application State
let currentPositions = [];
let currentWallet = {};
let currentSummary = {};
let selectedCurrency = 'INR';
let usdToInrRate = 85.0;
let isFetching = false;
let activeNavTab = 'positions';
let lastSpotPrice = 0;

/**
 * Switch top navigation sub-menu tabs (Positions vs Multi-Leg Studio)
 * @param {string} tabName - 'positions' or 'studio'
 */
function switchNavTab(tabName) {
  activeNavTab = tabName;
  document.getElementById('tabNavPositions')?.classList.toggle('active', tabName === 'positions');
  document.getElementById('tabNavStudio')?.classList.toggle('active', tabName === 'studio');

  document.getElementById('viewPositions')?.classList.toggle('active', tabName === 'positions');
  document.getElementById('viewStudio')?.classList.toggle('active', tabName === 'studio');
}

/**
 * Format numbers with USD or INR currency symbols and locales
 */
function formatMoney(valInUSD, currency = selectedCurrency, minDec = 2, maxDec = 4) {
  if (valInUSD === null || valInUSD === undefined || isNaN(valInUSD)) valInUSD = 0;
  const numUSD = Number(valInUSD);
  const isNeg = numUSD < 0;
  const absUSD = Math.abs(numUSD);

  if (currency === 'INR') {
    const inrVal = absUSD * usdToInrRate;
    const formatted = inrVal.toLocaleString('en-IN', {
      minimumFractionDigits: minDec,
      maximumFractionDigits: maxDec
    });
    return (isNeg ? '-₹' : '₹') + formatted;
  } else {
    const formatted = absUSD.toLocaleString('en-US', {
      minimumFractionDigits: minDec,
      maximumFractionDigits: maxDec
    });
    return (isNeg ? '-$' : '$') + formatted;
  }
}

/**
 * Returns alternative secondary currency formatted value
 */
function formatAltMoney(valInUSD) {
  const altCurr = selectedCurrency === 'INR' ? 'USD' : 'INR';
  return formatMoney(valInUSD, altCurr, 2, 2);
}

/**
 * Standard number formatter
 */
function formatNumber(val, decimals = 2) {
  if (val === null || val === undefined || isNaN(val)) return '0';
  return Number(val).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
}

/**
 * Switch global currency between USD and INR
 * @param {string} curr - 'USD' or 'INR'
 */
function setCurrency(curr) {
  selectedCurrency = curr;
  document.getElementById('btnCurrUSD')?.classList.toggle('active', curr === 'USD');
  document.getElementById('btnCurrINR')?.classList.toggle('active', curr === 'INR');

  const prefix = curr === 'INR' ? '₹' : '$';
  const thEntry = document.getElementById('thEntryPrice');
  if (thEntry) thEntry.textContent = `Entry Price (${prefix})`;
  const thLtp = document.getElementById('thLtp');
  if (thLtp) thLtp.textContent = `LTP / Market (${prefix})`;
  const thMark = document.getElementById('thMarkPrice');
  if (thMark) thMark.textContent = `Mark Price (${prefix})`;
  const thUPnl = document.getElementById('thUnrealizedPnl');
  if (thUPnl) thUPnl.textContent = `Unrealized PnL (${prefix})`;
  const thRPnl = document.getElementById('thRealizedPnl');
  if (thRPnl) thRPnl.textContent = `Realized PnL (${prefix})`;
  const thNotional = document.getElementById('thNotional');
  if (thNotional) thNotional.textContent = `Notional (${prefix})`;

  renderDashboardValues();
  renderPositionsTable();
  if (typeof updateUnderlyingPriceAndLotDisplay === 'function') {
    updateUnderlyingPriceAndLotDisplay();
  }
  if (typeof updateStrategyMarginRequirement === 'function') {
    updateStrategyMarginRequirement();
  }
}

/**
 * Toast notifications with autohide
 */
function showToast(message, type = 'normal') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

/**
 * Main fetch dashboard data via Live API
 */
async function fetchDashboardData(manual = false) {
  if (isFetching) return;
  isFetching = true;
  const btnRefresh = document.getElementById('btnRefresh');
  if (manual && btnRefresh) btnRefresh.classList.add('loading');

  try {
    const data = await ApiService.getDashboard();

    if (!data.success) {
      const connStatus = document.getElementById('connStatus');
      if (connStatus) {
        connStatus.textContent = 'API ERROR';
        connStatus.style.color = 'var(--accent-red)';
      }
      if (manual) showToast(`API Error: ${data.error || 'Failed to fetch data'}`, 'error');
      return;
    }

    if (data.environment) {
      const envBadge = document.getElementById('envBadge');
      if (envBadge) envBadge.textContent = data.environment;
    }
    if (data.usd_to_inr_rate) {
      usdToInrRate = Number(data.usd_to_inr_rate);
      const rateDisplay = document.getElementById('rateDisplay');
      if (rateDisplay) rateDisplay.textContent = `₹${usdToInrRate.toFixed(2)}`;
    }

    const connStatus = document.getElementById('connStatus');
    if (connStatus) {
      connStatus.textContent = 'LIVE FEED';
      connStatus.style.color = 'var(--accent-green)';
    }

    currentSummary = data.summary || {};
    currentWallet = data.wallet || {};
    currentPositions = data.positions || [];

    renderDashboardValues();
    renderPositionsTable();

    const now = new Date();
    const lastSync = document.getElementById('lastUpdatedTime');
    if (lastSync) lastSync.textContent = `Last sync: ${now.toLocaleTimeString()}`;

    if (manual) showToast('Live dashboard refreshed');
  } catch (err) {
    const connStatus = document.getElementById('connStatus');
    if (connStatus) {
      connStatus.textContent = 'RETRYING...';
      connStatus.style.color = 'var(--accent-red)';
    }
  } finally {
    isFetching = false;
    if (btnRefresh) btnRefresh.classList.remove('loading');
  }
}

/**
 * Updates top summary metric cards and wallet balances
 */
function renderDashboardValues() {
  const w = currentWallet || {};
  const s = currentSummary || {};

  const usdBal = w.usd_balance || 0;
  const inrBal = w.inr_balance || (usdBal * usdToInrRate);
  const usdAvail = w.usd_available || 0;
  const inrAvail = w.inr_available || (usdAvail * usdToInrRate);

  const mainBal = selectedCurrency === 'INR' ? inrBal : usdBal;
  const mainAvail = selectedCurrency === 'INR' ? inrAvail : usdAvail;
  const altBal = selectedCurrency === 'INR' ? usdBal : inrBal;
  const altCurr = selectedCurrency === 'INR' ? 'USD' : 'INR';

  const elBal = document.getElementById('walletBalanceMain');
  if (elBal) elBal.textContent = (selectedCurrency === 'INR' ? '₹' : '$') + mainBal.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  
  const elAvail = document.getElementById('walletAvailMain');
  if (elAvail) elAvail.textContent = (selectedCurrency === 'INR' ? '₹' : '$') + mainAvail.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  
  const elAlt = document.getElementById('walletAltMain');
  if (elAlt) elAlt.textContent = (altCurr === 'INR' ? '₹' : '$') + altBal.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const uPnlUsd = s.total_unrealized_pnl || 0;
  const elUPnl = document.getElementById('totalUnrealizedPnl');
  if (elUPnl) {
    elUPnl.textContent = formatMoney(uPnlUsd, selectedCurrency, 2, 2);
    elUPnl.className = `metric-value ${uPnlUsd > 0 ? 'text-green' : (uPnlUsd < 0 ? 'text-red' : '')}`;
  }
  const elUPnlAlt = document.getElementById('totalUnrealizedPnlAlt');
  if (elUPnlAlt) elUPnlAlt.textContent = formatAltMoney(uPnlUsd);

  const rPnlUsd = s.total_realized_pnl || 0;
  const elRPnl = document.getElementById('totalRealizedPnl');
  if (elRPnl) {
    elRPnl.textContent = formatMoney(rPnlUsd, selectedCurrency, 2, 2);
    elRPnl.className = `metric-value ${rPnlUsd > 0 ? 'text-green' : (rPnlUsd < 0 ? 'text-red' : 'text-blue')}`;
  }
  const elRPnlAlt = document.getElementById('totalRealizedPnlAlt');
  if (elRPnlAlt) elRPnlAlt.textContent = formatAltMoney(rPnlUsd);

  const count = s.total_open_positions || 0;
  const elCount = document.getElementById('openPositionsCount');
  if (elCount) elCount.textContent = count;
  const navPosCount = document.getElementById('navPosCount');
  if (navPosCount) navPosCount.textContent = count;
  const longCount = document.getElementById('longCount');
  if (longCount) longCount.textContent = `${s.longs_count || 0} Long`;
  const shortCount = document.getElementById('shortCount');
  if (shortCount) shortCount.textContent = `${s.shorts_count || 0} Short`;
  const posBadge = document.getElementById('posBadge');
  if (posBadge) posBadge.textContent = `${count} ACTIVE`;
}

/**
 * Ultra-fast live tick streamer for active underlying asset spot price (600ms)
 */
async function fetchLiveTickerTick() {
  if (activeNavTab !== 'studio') return;
  const u = document.getElementById('selUnderlying')?.value || 'BTC';
  try {
    const data = await ApiService.getTicker(u);
    if (data.success) {
      const newSpotPrice = Number(data.spot_price || data.underlying_price || data.futures_price || data.ltp || 0);
      const spotBadge = document.getElementById('spotPriceBadge');

      if (lastSpotPrice > 0 && newSpotPrice > 0 && spotBadge) {
        if (newSpotPrice > lastSpotPrice) {
          spotBadge.classList.add('flash-up');
          setTimeout(() => spotBadge.classList.remove('flash-up'), 350);
        } else if (newSpotPrice < lastSpotPrice) {
          spotBadge.classList.add('flash-down');
          setTimeout(() => spotBadge.classList.remove('flash-down'), 350);
        }
      }
      lastSpotPrice = newSpotPrice;

      currentUnderlyingInfo.spot_price = newSpotPrice;
      currentUnderlyingInfo.underlying_price = newSpotPrice;
      currentUnderlyingInfo.futures_price = Number(data.futures_price) || newSpotPrice;
      currentUnderlyingInfo.ltp = Number(data.ltp) || newSpotPrice;
      currentUnderlyingInfo.mark_price = Number(data.mark_price) || newSpotPrice;
      if (data.contract_value) {
        currentUnderlyingInfo.contract_value = Number(data.contract_value);
      }
      updateUnderlyingPriceAndLotDisplay();
    }
  } catch (err) {}
}

/**
 * Starts continuous live polling and tick streaming
 */
function startLiveStreaming() {
  // Stream dashboard positions & metrics every 2 seconds
  setInterval(() => {
    fetchDashboardData(false);
  }, 2000);

  // High-Frequency Real-Time Tick Streaming for active underlying (every 600ms)
  setInterval(() => {
    fetchLiveTickerTick();
  }, 600);
}

// Initial Boot & Event Binding
window.addEventListener('DOMContentLoaded', () => {
  // Close modal on overlay click
  document.getElementById('detailModal')?.addEventListener('click', function (e) {
    if (e.target === this) closeModal('detailModal');
  });

  setCurrency(selectedCurrency);
  fetchDashboardData(false);
  loadExpiriesForUnderlying('BTC');
  startLiveStreaming();
});
