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
let selectedCurrency = (function () {
  try {
    return localStorage.getItem('delta_currency') || 'INR';
  } catch (e) {
    return 'INR';
  }
})();
let usdToInrRate = 85.0;
let isFetching = false;
let activeNavTab = 'positions';
let lastSpotPrice = 0;
let serverTimeOffset = 0;

/**
 * Gets the active tab to restore on page reload from URL hash or localStorage
 */
function getInitialNavTab() {
  const validTabs = ['positions', 'studio', 'chain', 'mm'];
  // 1. Prioritize URL hash (e.g. #positions, #studio, #chain, #mm)
  const hash = (window.location.hash || '').replace('#', '').toLowerCase();
  if (validTabs.includes(hash)) {
    return hash;
  }
  // 2. Check localStorage
  try {
    const saved = localStorage.getItem('delta_active_nav_tab');
    if (saved && validTabs.includes(saved)) {
      return saved;
    }
  } catch (e) { }
  // Default to positions
  return 'positions';
}

/**
 * Updates the live ticking exchange server clock in Indian Standard Time (DD/MM/YYYY HH:MM:SS IST)
 */
function updateExchangeClock() {
  // Exchange UTC Epoch + 5h 30m offset for Indian Standard Time (IST)
  const istOffsetMs = (5 * 60 + 30) * 60 * 1000;
  const istTime = new Date(Date.now() + serverTimeOffset + istOffsetMs);

  const day = String(istTime.getUTCDate()).padStart(2, '0');
  const month = String(istTime.getUTCMonth() + 1).padStart(2, '0');
  const year = istTime.getUTCFullYear();

  const hours = String(istTime.getUTCHours()).padStart(2, '0');
  const minutes = String(istTime.getUTCMinutes()).padStart(2, '0');
  const seconds = String(istTime.getUTCSeconds()).padStart(2, '0');

  const formattedTime = `${day}/${month}/${year} ${hours}:${minutes}:${seconds} IST`;

  const clockEl = document.getElementById('exchangeClockTime');
  if (clockEl) {
    clockEl.textContent = formattedTime;
  }
}

/**
 * Sidebar Navigation Drawer Controllers
 */
function toggleSidebarDrawer() {
  const drawer = document.getElementById('sidebarDrawer');
  const overlay = document.getElementById('drawerOverlay');
  if (!drawer || !overlay) return;
  const isOpen = drawer.classList.contains('active');
  if (isOpen) {
    closeSidebarDrawer();
  } else {
    openSidebarDrawer();
  }
}

function openSidebarDrawer() {
  document.getElementById('sidebarDrawer')?.classList.add('active');
  document.getElementById('drawerOverlay')?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeSidebarDrawer() {
  document.getElementById('sidebarDrawer')?.classList.remove('active');
  document.getElementById('drawerOverlay')?.classList.remove('active');
  document.body.style.overflow = '';
}

/**
 * Navigate to a tab from sidebar drawer and smoothly close it
 */
function navigateToTab(tabName) {
  switchNavTab(tabName);
  closeSidebarDrawer();
}

/**
 * Switch navigation tabs (Positions vs Multi-Leg Studio vs Option Chain vs MM Strikes)
 * @param {string} tabName - 'positions', 'studio', 'chain', or 'mm'
 * @param {boolean} updateUrl - whether to sync URL hash
 */
function switchNavTab(tabName, updateUrl = true) {
  if (!tabName) tabName = 'positions';
  activeNavTab = tabName;

  // Persist to localStorage for refresh retention
  try {
    localStorage.setItem('delta_active_nav_tab', tabName);
  } catch (e) { }

  // Update URL hash for bookmarking and page refresh persistence
  if (updateUrl) {
    const targetHash = '#' + tabName;
    if (window.location.hash !== targetHash) {
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', targetHash);
      } else {
        window.location.hash = tabName;
      }
    }
  }

  // Top navigation tabs
  document.getElementById('tabNavPositions')?.classList.toggle('active', tabName === 'positions');
  document.getElementById('tabNavStudio')?.classList.toggle('active', tabName === 'studio');
  document.getElementById('tabNavChain')?.classList.toggle('active', tabName === 'chain');
  document.getElementById('tabNavMM')?.classList.toggle('active', tabName === 'mm');

  // Sidebar drawer menu items
  document.getElementById('drawerNavPositions')?.classList.toggle('active', tabName === 'positions');
  document.getElementById('drawerNavStudio')?.classList.toggle('active', tabName === 'studio');
  document.getElementById('drawerNavChain')?.classList.toggle('active', tabName === 'chain');
  document.getElementById('drawerNavMM')?.classList.toggle('active', tabName === 'mm');

  // Content views
  document.getElementById('viewPositions')?.classList.toggle('active', tabName === 'positions');
  document.getElementById('viewStudio')?.classList.toggle('active', tabName === 'studio');
  document.getElementById('viewOptionChain')?.classList.toggle('active', tabName === 'chain');
  document.getElementById('viewMmStrikes')?.classList.toggle('active', tabName === 'mm');

  if (tabName === 'chain' && typeof loadOptionChain === 'function') {
    const curAsset = (typeof ocCurrentUnderlying !== 'undefined' && ocCurrentUnderlying) ? ocCurrentUnderlying : (localStorage.getItem('delta_oc_underlying') || 'BTC');
    const curExp = (typeof ocSelectedExpiry !== 'undefined') ? ocSelectedExpiry : '';
    loadOptionChain(curAsset, curExp);
  } else if (tabName === 'mm' && typeof loadMarketMakingStrikes === 'function') {
    const curAsset = (typeof mmCurrentUnderlying !== 'undefined' && mmCurrentUnderlying) ? mmCurrentUnderlying : 'BTC';
    const curExp = (typeof mmSelectedExpiry !== 'undefined') ? mmSelectedExpiry : '';
    loadMarketMakingStrikes(curAsset, curExp);
  } else if (tabName === 'studio') {
    if (typeof availableExpiries !== 'undefined' && availableExpiries.length === 0) {
      const curUnderlying = document.getElementById('selUnderlying')?.value || 'BTC';
      if (typeof loadExpiriesForUnderlying === 'function') {
        loadExpiriesForUnderlying(curUnderlying);
      }
    }
    if (typeof updateUnderlyingPriceAndLotDisplay === 'function') {
      updateUnderlyingPriceAndLotDisplay();
    }
    if (typeof fetchLegTickers === 'function') {
      fetchLegTickers();
    }
  } else if (tabName === 'positions') {
    if (typeof renderPositionsTable === 'function') {
      renderPositionsTable();
    }
  }
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
  try {
    localStorage.setItem('delta_currency', curr);
  } catch (e) { }
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
let _lastToastText = '';
let _lastToastTs = 0;
function showToast(message, type = 'normal') {
  const now = Date.now();
  if (message === _lastToastText && (now - _lastToastTs) < 1000) {
    return; // Prevent duplicate stacked toast
  }
  _lastToastText = message;
  _lastToastTs = now;

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

    if (data.server_timestamp) {
      serverTimeOffset = (Number(data.server_timestamp) * 1000) - Date.now();
      updateExchangeClock();
    }

    if (data.environment) {
      const envBadge = document.getElementById('envBadge');
      if (envBadge) envBadge.textContent = data.environment;
      const drawerEnvBadge = document.getElementById('drawerEnvBadge');
      if (drawerEnvBadge) drawerEnvBadge.textContent = data.environment;
    }
    if (data.usd_to_inr_rate) {
      usdToInrRate = Number(data.usd_to_inr_rate);
      const rateDisplay = document.getElementById('rateDisplay');
      if (rateDisplay) rateDisplay.textContent = `₹${usdToInrRate.toFixed(2)}`;
      const drawerRateDisplay = document.getElementById('drawerRateDisplay');
      if (drawerRateDisplay) drawerRateDisplay.textContent = `₹${usdToInrRate.toFixed(2)}`;
    }

    if (data.margin_mode) {
      updateMarginModeUI(data.margin_mode);
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

  const notionalUsd = s.total_notional_usd || 0;
  const elNotional = document.getElementById('totalNotionalVal');
  if (elNotional) {
    elNotional.textContent = formatMoney(notionalUsd, selectedCurrency, 2, 2);
  }
  const elNotionalAlt = document.getElementById('totalNotionalValAlt');
  if (elNotionalAlt) elNotionalAlt.textContent = formatAltMoney(notionalUsd);

  const count = s.total_open_positions || 0;
  const elCount = document.getElementById('openPositionsCount');
  if (elCount) elCount.textContent = count;
  const navPosCount = document.getElementById('navPosCount');
  if (navPosCount) navPosCount.textContent = count;
  const drawerPosCount = document.getElementById('drawerPosCount');
  if (drawerPosCount) drawerPosCount.textContent = `${count} Active`;
  const longCount = document.getElementById('longCount');
  if (longCount) longCount.textContent = `${s.longs_count || 0} Long`;
  const shortCount = document.getElementById('shortCount');
  if (shortCount) shortCount.textContent = `${s.shorts_count || 0} Short`;
  const posBadge = document.getElementById('posBadge');
  if (posBadge) posBadge.textContent = `${count} ACTIVE`;

  if (typeof updateStrategyMarginRequirement === 'function') {
    updateStrategyMarginRequirement();
  }
}

// Direct WebSocket Live Stream Instance
let deltaWs = null;
let subscribedSymbols = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD'];
let wsPingSentTime = 0;
let wsPingTimer = null;
let wsEstimatedRtt = 20;
let wsMinRawDiff = null;
let wsClockOffset = null;
window.wsLastDataLatencyMs = null;
let navLatencyThrottle = null;

function startWsLatencyPing() {
  stopWsLatencyPing();
  sendWsPing();
  wsPingTimer = setInterval(sendWsPing, 3000);
}

function stopWsLatencyPing() {
  if (wsPingTimer) {
    clearInterval(wsPingTimer);
    wsPingTimer = null;
  }
}

function sendWsPing() {
  if (!deltaWs || deltaWs.readyState !== WebSocket.OPEN) return;
  try {
    wsPingSentTime = performance.now();
    deltaWs.send(JSON.stringify({ type: 'ping' }));
  } catch (e) { }
}

function scheduleNavLatencyUpdate(latencyMs) {
  if (navLatencyThrottle) return;
  navLatencyThrottle = setTimeout(() => {
    navLatencyThrottle = null;
    updateNavLatencyDisplay(latencyMs);
  }, 250);
}

function updateNavLatencyDisplay(latencyMs) {
  const badge = document.getElementById('navLatencyBadge');
  const valEl = document.getElementById('navLatencyVal');
  if (!badge || !valEl) return;

  if (latencyMs === null || isNaN(latencyMs)) {
    valEl.textContent = '⚡ --ms';
    badge.className = 'badge-latency latency-high';
    badge.title = 'Delta WebSocket Disconnected (Offline)';
    return;
  }

  window.wsLastDataLatencyMs = latencyMs;
  valEl.textContent = `⚡ ${latencyMs}ms WS Data`;

  badge.classList.remove('latency-med', 'latency-high');
  if (latencyMs > 100) {
    badge.classList.add('latency-high');
    badge.title = `WebSocket Market Data Arrival Latency: ${latencyMs}ms (High Latency)\nStream: l1_orderbook (Live Top-of-Book)\nEndpoint: wss://socket.india.delta.exchange`;
  } else if (latencyMs > 40) {
    badge.classList.add('latency-med');
    badge.title = `WebSocket Market Data Arrival Latency: ${latencyMs}ms (Moderate Latency)\nStream: l1_orderbook (Live Top-of-Book)\nEndpoint: wss://socket.india.delta.exchange`;
  } else {
    badge.title = `WebSocket Market Data Arrival Latency: ${latencyMs}ms (Ultra-Low Latency)\nStream: l1_orderbook (Live Top-of-Book)\nEndpoint: wss://socket.india.delta.exchange`;
  }
}

/**
 * Connects directly to Delta Exchange WebSocket for millisecond-level live price streaming
 */
function initDeltaWebSocket() {
  const wsUrl = 'wss://socket.india.delta.exchange';
  try {
    deltaWs = new WebSocket(wsUrl);

    deltaWs.onopen = () => {
      console.log('[Delta WS] Connected to live tick WebSocket stream.');
      subscribeActiveSymbols();
      startWsLatencyPing();
    };

    deltaWs.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const msgType = data.type || data.channel || data.name || '';

        // Delta WebSocket Heartbeat Ping/Pong response
        if (msgType === 'pong' && wsPingSentTime > 0) {
          const rtt = Math.round(performance.now() - wsPingSentTime);
          wsEstimatedRtt = Math.max(2, rtt);
          if (wsMinRawDiff !== null) {
            wsClockOffset = wsMinRawDiff - (wsEstimatedRtt / 2);
          }
          if (window.wsLastDataLatencyMs === null) {
            updateNavLatencyDisplay(Math.round(wsEstimatedRtt / 2));
          }
          return;
        }

        // Calculate true WebSocket market data arrival latency on every tick
        if (data.timestamp) {
          const nowMs = Date.now();
          const tsRaw = Number(data.timestamp);
          const tsMs = tsRaw > 1e14 ? (tsRaw / 1000) : (tsRaw > 1e11 ? tsRaw : tsRaw * 1000);
          const rawDiff = nowMs - tsMs;
          if (wsMinRawDiff === null || rawDiff < wsMinRawDiff) {
            wsMinRawDiff = rawDiff;
            wsClockOffset = wsMinRawDiff - (wsEstimatedRtt / 2);
          }
          const dataLatency = Math.max(1, Math.round(rawDiff - (wsClockOffset !== null ? wsClockOffset : 0)));
          window.wsLastDataLatencyMs = dataLatency;
          scheduleNavLatencyUpdate(dataLatency);
        }

        const sym = (data.symbol || (data.underlying_asset_symbol ? `${data.underlying_asset_symbol}USD` : '')).toUpperCase();
        const currentUnderlying = (document.getElementById('selUnderlying')?.value || 'BTC').toUpperCase();
        const expectedSymbol = `${currentUnderlying}USD`;

        if (
          (msgType === 'v2/ticker' || msgType === 'ticker' || msgType === 'spot_price' || data.close !== undefined || data.mark_price !== undefined) &&
          (sym === expectedSymbol || sym === currentUnderlying || (data.underlying_asset_symbol && data.underlying_asset_symbol.toUpperCase() === currentUnderlying))
        ) {
          applyLiveTickUpdate(data);
        }

        // Live streaming for active strategy leg quotes
        if (typeof handleLegTickUpdate === 'function') {
          handleLegTickUpdate(data);
        }

        // Live streaming for active Option Chain quotes
        if (typeof handleOptionChainTickUpdate === 'function') {
          handleOptionChainTickUpdate(data);
        }

        // Live tick-by-tick streaming for Market Making Strike Selection
        if (typeof handleMmStrikesTickUpdate === 'function') {
          handleMmStrikesTickUpdate(data);
        }
      } catch (err) { }
    };

    deltaWs.onclose = () => {
      console.log('[Delta WS] WebSocket disconnected. Reconnecting in 2s...');
      stopWsLatencyPing();
      updateNavLatencyDisplay(null);
      setTimeout(initDeltaWebSocket, 2000);
    };

    deltaWs.onerror = (err) => {
      console.warn('[Delta WS] WebSocket notice:', err);
    };
  } catch (e) {
    console.error('[Delta WS] Failed to init WebSocket:', e);
  }
}

function addSubscribedSymbols(symbols) {
  if (!symbols || symbols.length === 0) return;
  let added = false;
  symbols.forEach(s => {
    const symUpper = s.toUpperCase();
    if (!subscribedSymbols.includes(symUpper)) {
      subscribedSymbols.push(symUpper);
      added = true;
    }
  });
  if (added && deltaWs && deltaWs.readyState === WebSocket.OPEN) {
    subscribeActiveSymbols();
  }
}

function subscribeActiveSymbols() {
  if (!deltaWs || deltaWs.readyState !== WebSocket.OPEN) return;
  const currentUnderlying = (document.getElementById('selUnderlying')?.value || 'BTC').toUpperCase();
  const symbolsToSub = Array.from(new Set([...subscribedSymbols, `${currentUnderlying}USD`]));

  // Chunk in batches of 40 symbols to ensure high reliability across all channels
  const chunkSize = 40;
  for (let i = 0; i < symbolsToSub.length; i += chunkSize) {
    const chunk = symbolsToSub.slice(i, i + chunkSize);
    const subMsg = {
      type: 'subscribe',
      payload: {
        channels: [
          { name: 'v2/ticker', symbols: chunk },
          { name: 'mark_price', symbols: chunk },
          { name: 'l1_orderbook', symbols: chunk }
        ]
      }
    };
    deltaWs.send(JSON.stringify(subMsg));
  }

  // Also subscribe to underlying spot price feed
  try {
    deltaWs.send(JSON.stringify({
      type: 'subscribe',
      payload: {
        channels: [
          { name: 'spot_price', symbols: [currentUnderlying] }
        ]
      }
    }));
  } catch (e) { }
}

function applyLiveTickUpdate(data) {
  const quotes = data.quotes || {};
  const spot = Number(data.spot_price || data.underlying_price || 0);
  const mark = Number(data.mark_price || 0);
  const ltp = Number(data.close || quotes.best_bid || data.ltp || 0);
  const newSpotPrice = spot > 0 ? spot : (ltp > 0 ? ltp : mark);

  if (newSpotPrice > 0) {
    const spotBadge = document.getElementById('spotPriceBadge');
    if (lastSpotPrice > 0 && spotBadge) {
      if (newSpotPrice > lastSpotPrice) {
        spotBadge.classList.add('flash-up');
        spotBadge.classList.remove('flash-down');
        setTimeout(() => spotBadge.classList.remove('flash-up'), 250);
      } else if (newSpotPrice < lastSpotPrice) {
        spotBadge.classList.add('flash-down');
        spotBadge.classList.remove('flash-up');
        setTimeout(() => spotBadge.classList.remove('flash-down'), 250);
      }
    }
    lastSpotPrice = newSpotPrice;

    currentUnderlyingInfo.spot_price = newSpotPrice;
    currentUnderlyingInfo.underlying_price = newSpotPrice;
    if (ltp > 0) currentUnderlyingInfo.futures_price = ltp;
    if (ltp > 0) currentUnderlyingInfo.ltp = ltp;
    if (mark > 0) currentUnderlyingInfo.mark_price = mark;

    if (data.timestamp) {
      const tsNum = Number(data.timestamp);
      const exchangeTs = tsNum > 1e11 ? tsNum / 1e6 : tsNum;
      serverTimeOffset = (exchangeTs * 1000) - Date.now();
    }

    updateUnderlyingPriceAndLotDisplay();
  }
}

/**
 * Ultra-fast live tick fallback for active underlying asset spot price
 */
async function fetchLiveTickerTick() {
  if (activeNavTab !== 'studio' && activeNavTab !== 'chain') return;
  const u = (activeNavTab === 'chain' && typeof ocCurrentUnderlying !== 'undefined') ? ocCurrentUnderlying : (document.getElementById('selUnderlying')?.value || 'BTC');
  try {
    const data = await ApiService.getTicker(u);
    if (data.success) {
      applyLiveTickUpdate(data);
    }
  } catch (err) { }
}

/**
 * Starts continuous live polling and tick streaming
 */
function startLiveStreaming() {
  // Sync account equity & positions every 5 seconds (non-price metric)
  setInterval(() => {
    fetchDashboardData(false);
  }, 5000);

  // Live Exchange Clock 1-second precision ticking
  setInterval(() => {
    updateExchangeClock();
  }, 1000);

  // Fallback Poller: ONLY executes if the WebSocket connection drops or is reconnecting.
  // When WebSocket is active (live), ALL screen data updates exclusively via real-time L1 events with zero interval delay.
  setInterval(() => {
    const isWsLive = (deltaWs && deltaWs.readyState === WebSocket.OPEN);
    if (!isWsLive) {
      if (activeNavTab === 'chain' && typeof loadOptionChain === 'function') {
        loadOptionChain(ocCurrentUnderlying, ocSelectedExpiry, true);
      } else if (activeNavTab === 'mm' && typeof loadMarketMakingStrikes === 'function' && typeof mmIsLoading !== 'undefined' && !mmIsLoading) {
        loadMarketMakingStrikes(mmCurrentUnderlying, mmSelectedExpiry, true);
      } else if (activeNavTab === 'studio' && typeof fetchLegTickers === 'function') {
        fetchLegTickers();
      }
      fetchLiveTickerTick();
    }
  }, 3000);
}

// Initial Boot & Event Binding
window.addEventListener('DOMContentLoaded', () => {
  // Initialize Exchange Clock immediately
  updateExchangeClock();

  // Close modal on overlay click
  document.getElementById('detailModal')?.addEventListener('click', function (e) {
    if (e.target === this) closeModal('detailModal');
  });

  // Apply saved/current currency
  setCurrency(selectedCurrency);

  // Restore and display the active tab (Positions / Multi-Leg Studio / Live Option Chain)
  const initialTab = getInitialNavTab();
  switchNavTab(initialTab, true);

  const studioUnderlying = (function () {
    try {
      return localStorage.getItem('delta_studio_underlying') || 'BTC';
    } catch (e) {
      return 'BTC';
    }
  })();
  const selUnderlying = document.getElementById('selUnderlying');
  if (selUnderlying && studioUnderlying) {
    selUnderlying.value = studioUnderlying;
  }

  fetchDashboardData(false);
  loadExpiriesForUnderlying(studioUnderlying);
  loadActiveAccountStatus();
  startLiveStreaming();
  initDeltaWebSocket();
});

// Sync active tab seamlessly on browser Back/Forward or manual hash navigation
window.addEventListener('hashchange', () => {
  const hash = (window.location.hash || '').replace('#', '').toLowerCase();
  const validTabs = ['positions', 'studio', 'chain', 'mm'];
  if (validTabs.includes(hash) && hash !== activeNavTab) {
    switchNavTab(hash, false);
  }
});

// Auto-recover live feeds and background polls when returning to an idle or sleeping tab
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    handleAppWakeup();
  }
});
window.addEventListener('focus', () => {
  handleAppWakeup();
});

let lastWakeupTime = 0;
function handleAppWakeup() {
  const now = Date.now();
  if (now - lastWakeupTime < 2000) return; // Debounce rapid focus/visibility triggers
  lastWakeupTime = now;

  console.log('[App] Tab awakened from idle/sleep. Syncing live state...');

  // Reset any stuck loading flags from network drops
  if (typeof mmIsLoading !== 'undefined') mmIsLoading = false;
  if (typeof ocIsFetching !== 'undefined') ocIsFetching = false;

  // Re-establish WebSocket if dead, closed, or closing
  if (!deltaWs || deltaWs.readyState === WebSocket.CLOSED || deltaWs.readyState === WebSocket.CLOSING) {
    console.log('[App] Reconnecting dead WebSocket after sleep...');
    initDeltaWebSocket();
  }

  // Update Exchange Clock
  updateExchangeClock();

  // Force clean foreground sync for active tab
  if (activeNavTab === 'mm' && typeof loadMarketMakingStrikes === 'function') {
    loadMarketMakingStrikes(mmCurrentUnderlying, null, false);
  } else if (activeNavTab === 'chain' && typeof loadOptionChain === 'function') {
    loadOptionChain(ocCurrentUnderlying, ocSelectedExpiry, false);
  } else if (activeNavTab === 'positions') {
    fetchDashboardData(false);
  } else if (activeNavTab === 'studio') {
    fetchDashboardData(false);
  }
}

/**
 * Loads and displays active account ('main' or 'sub')
 */
async function loadActiveAccountStatus() {
  try {
    const res = await ApiService.getAccountStatus();
    if (res && res.success) {
      const activeAcct = (res.active_account || 'main').toLowerCase();
      window.currentActiveAccount = activeAcct;

      const btnSub = document.getElementById('btnAcctSub');
      const btnMain = document.getElementById('btnAcctMain');

      if (btnSub && btnMain) {
        if (activeAcct === 'sub') {
          btnSub.className = 'acct-btn active sub';
          btnMain.className = 'acct-btn';
        } else {
          btnSub.className = 'acct-btn';
          btnMain.className = 'acct-btn active main';
        }
      }

      const modalAccount = document.getElementById('mmModalAccount');
      if (modalAccount) modalAccount.textContent = activeAcct.toUpperCase();

      if (res.margin_mode) {
        updateMarginModeUI(res.margin_mode);
      }
    }
  } catch (e) {
    console.warn('[ACCOUNT] Could not load account status:', e);
  }
}

/**
 * Updates all Margin Mode indicators across the UI (Top navbar, positions, drawer, modal)
 */
function updateMarginModeUI(mode) {
  const cleanMode = (mode || 'PORTFOLIO').toUpperCase();
  window.currentMarginMode = cleanMode;

  const topEl = document.getElementById('topMarginModeVal');
  if (topEl) topEl.textContent = cleanMode;

  const pill = document.getElementById('marginModePill');
  if (pill) {
    pill.classList.remove('mode-isolated', 'mode-cross');
    if (cleanMode === 'ISOLATED') {
      pill.classList.add('mode-isolated');
    } else if (cleanMode === 'CROSS') {
      pill.classList.add('mode-cross');
    }
  }

  const drawerEl = document.getElementById('drawerMarginMode');
  if (drawerEl) {
    if (cleanMode === 'PORTFOLIO') drawerEl.textContent = 'Portfolio Margin';
    else if (cleanMode === 'CROSS') drawerEl.textContent = 'Cross Margin';
    else drawerEl.textContent = 'Isolated Margin';
  }

  const posEl = document.getElementById('posDashboardMarginMode');
  if (posEl) posEl.textContent = cleanMode;

  const modalMode = document.getElementById('modalCurrentMarginMode');
  if (modalMode) modalMode.textContent = cleanMode + ' MARGIN';

  const modalAcct = document.getElementById('modalMarginActiveAcct');
  if (modalAcct) {
    const acct = (window.currentActiveAccount || 'sub').toUpperCase();
    modalAcct.textContent = `${acct} ACCOUNT`;
  }

  const cardPort = document.getElementById('cardPortfolioMargin');
  const cardCross = document.getElementById('cardCrossMargin');
  const cardIso = document.getElementById('cardIsolatedMargin');
  const badgePort = document.getElementById('badgePortfolioActive');
  const badgeCross = document.getElementById('badgeCrossActive');
  const badgeIso = document.getElementById('badgeIsolatedActive');

  // Reset all cards
  if (cardPort) {
    cardPort.style.borderColor = 'var(--border-subtle)';
    cardPort.style.background = 'rgba(255, 255, 255, 0.02)';
  }
  if (cardCross) {
    cardCross.style.borderColor = 'var(--border-subtle)';
    cardCross.style.background = 'rgba(255, 255, 255, 0.02)';
  }
  if (cardIso) {
    cardIso.style.borderColor = 'var(--border-subtle)';
    cardIso.style.background = 'rgba(255, 255, 255, 0.02)';
  }

  if (badgePort) badgePort.style.display = 'none';
  if (badgeCross) badgeCross.style.display = 'none';
  if (badgeIso) badgeIso.style.display = 'none';

  // Highlight active mode card
  if (cleanMode === 'PORTFOLIO' && cardPort) {
    cardPort.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    cardPort.style.background = 'rgba(16, 185, 129, 0.08)';
    if (badgePort) badgePort.style.display = 'inline-block';
  } else if (cleanMode === 'CROSS' && cardCross) {
    cardCross.style.borderColor = 'rgba(56, 189, 248, 0.45)';
    cardCross.style.background = 'rgba(56, 189, 248, 0.08)';
    if (badgeCross) badgeCross.style.display = 'inline-block';
  } else if (cleanMode === 'ISOLATED' && cardIso) {
    cardIso.style.borderColor = 'rgba(245, 158, 11, 0.4)';
    cardIso.style.background = 'rgba(245, 158, 11, 0.08)';
    if (badgeIso) badgeIso.style.display = 'inline-block';
  }
}

function openMarginModeModal() {
  const modal = document.getElementById('marginModeModal');
  if (modal) {
    modal.classList.add('active');
    updateMarginModeUI(window.currentMarginMode || 'PORTFOLIO');
  }
}

function closeMarginModeModal() {
  const modal = document.getElementById('marginModeModal');
  if (modal) modal.classList.remove('active');
}

async function requestSetMarginMode(targetMode) {
  try {
    if (typeof showToast === 'function') {
      showToast(`Setting Margin Mode to ${targetMode.toUpperCase()}...`, 'info');
    }
    const res = await ApiService.setMarginMode(targetMode);
    if (res && res.success) {
      updateMarginModeUI(res.margin_mode || targetMode);
      if (typeof showToast === 'function') {
        showToast(res.message || `Margin Mode set to ${targetMode.toUpperCase()}!`, 'success');
      }
      fetchDashboardData(true);
    } else {
      if (typeof showToast === 'function') {
        showToast(`Margin switch error: ${res?.error || 'Failed'}`, 'error');
      }
    }
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast(`Error: ${e.message}`, 'error');
    }
  }
}

/**
 * Switches active account between 'main' and 'sub' on the fly
 */
async function switchActiveAccount(targetAccount) {
  try {
    const res = await ApiService.switchAccount(targetAccount);
    if (res && res.success) {
      window.currentActiveAccount = targetAccount;
      loadActiveAccountStatus();
      if (typeof showToast === 'function') {
        showToast(`⚡ Switched to [${targetAccount.toUpperCase()}] Account`, 'success');
      }
      fetchDashboardData(true);
    } else {
      if (typeof showToast === 'function') {
        showToast(`Account switch failed: ${res?.error || 'Error'}`, 'error');
      }
    }
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast(`Error: ${e.message}`, 'error');
    }
  }
}

